"""物资交接清单流程测试"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from .models import (
    Category, Goods, HandoffList, HandoffItem, HandoffStatus, HandoffItemStatus,
    Unit, Variety,
)
from . import handoff_services


class HandoffFixture(TestCase):
    def setUp(self):
        self.transferor = User.objects.create_user("zhang", "pass1234", role="user", real_name="张保管")
        self.receiver = User.objects.create_user("li", "pass1234", role="user", real_name="李接班")
        self.supervisor = User.objects.create_user("wang", "pass1234", role="admin", real_name="王主管")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.transferor)}")
        self.rcv_client = self._client(self.receiver)
        self.admin_client = self._client(self.supervisor)

        unit = Unit.objects.create(name="台", created_by=self.transferor)
        category = Category.objects.create(name="取证设备", unit=unit, created_by=self.transferor)
        variety = Variety.objects.create(name="记录仪", category=category, created_by=self.transferor)
        self.goods_a = Goods.objects.create(
            variety=variety, name="执法记录仪A", code="DEV-A",
            quantity=Decimal("5"), location="A柜-01",
        )
        self.goods_b = Goods.objects.create(
            variety=variety, name="执法记录仪B", code="DEV-B",
            quantity=Decimal("3"), location="A柜-02",
        )

    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def payload(self, **overrides):
        data = {
            "receiver": self.receiver.id,
            "items": [
                {"goods": self.goods_a.id, "expected_quantity": "5",
                 "seal_no": "S-1", "location": "A柜-01"},
                {"goods": self.goods_b.id, "expected_quantity": "3",
                 "seal_no": "S-2", "location": "A柜-02"},
            ],
        }
        data.update(overrides)
        return data

    def create_frozen(self, **overrides):
        handoff, _ = handoff_services.create_handoff(
            transferor=self.transferor, receiver=self.receiver,
            items=self.payload(**overrides)["items"],
        )
        return handoff_services.freeze_handoff(handoff, operator=self.transferor)


class HandoffServiceTest(HandoffFixture):
    def test_full_happy_path_transfers_custody(self):
        handoff = self.create_frozen()
        self.assertEqual(handoff.status, HandoffStatus.FROZEN)
        self.assertEqual(handoff.custodian, self.transferor)

        items = {i.goods_code: i for i in handoff.items.all()}
        handoff_services.confirm_item(
            handoff, items["DEV-A"], operator=self.receiver,
            actual_quantity=Decimal("5"), actual_seal_no="S-1", actual_location="A柜-01",
        )
        handoff_services.confirm_item(
            handoff, items["DEV-B"], operator=self.receiver,
            actual_quantity=Decimal("3"), actual_seal_no="S-2", actual_location="A柜-02",
        )
        completed = handoff_services.complete_handoff(handoff, operator=self.receiver)

        self.assertEqual(completed.status, HandoffStatus.COMPLETED)
        self.assertEqual(completed.custodian, self.receiver)
        self.assertIsNotNone(completed.completed_at)
        actions = list(completed.events.values_list("action", flat=True))
        self.assertEqual(actions, ["created", "frozen", "item_confirmed",
                                   "item_confirmed", "completed"])

    def test_disputed_item_blocks_completion(self):
        handoff = self.create_frozen()
        items = list(handoff.items.all())
        handoff_services.confirm_item(
            handoff, items[0], operator=self.receiver,
            actual_quantity=Decimal("5"), actual_seal_no="S-1", actual_location="A柜-01",
        )
        handoff_services.confirm_item(
            handoff, items[1], operator=self.receiver,
            actual_quantity=Decimal("2"), actual_seal_no="S-2", actual_location="A柜-02",
            difference_reason="数量短少1台",
        )
        items[1].refresh_from_db()
        self.assertEqual(items[1].item_status, HandoffItemStatus.DISPUTED)
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.complete_handoff(handoff, operator=self.receiver)
        handoff.refresh_from_db()
        self.assertEqual(handoff.status, HandoffStatus.FROZEN)
        self.assertEqual(handoff.custodian, self.transferor)

    def test_seal_or_location_mismatch_is_dispute(self):
        handoff = self.create_frozen()
        item = handoff.items.first()
        checked = handoff_services.confirm_item(
            handoff, item, operator=self.receiver,
            actual_quantity=item.expected_quantity,
            actual_seal_no="SEAL-BROKEN", actual_location=item.location,
        )
        self.assertEqual(checked.item_status, HandoffItemStatus.DISPUTED)

    def test_return_then_revise_refreeze_bumps_version(self):
        handoff = self.create_frozen()
        item = handoff.items.first()
        handoff_services.confirm_item(
            handoff, item, operator=self.receiver,
            actual_quantity=Decimal("99"), actual_seal_no="S-1",
            actual_location="A柜-01", difference_reason="数量不符",
        )
        handoff_services.return_handoff(
            handoff, operator=self.receiver, reason="数量与封签需复核"
        )
        handoff.refresh_from_db()
        self.assertEqual(handoff.status, HandoffStatus.RETURNED)
        # 退回后原确认结果作废
        self.assertFalse(handoff.items.exclude(item_status=HandoffItemStatus.PENDING).exists())

        handoff_services.update_draft_handoff(
            handoff, operator=self.transferor,
            items=[{"goods": self.goods_a.id, "expected_quantity": Decimal("5"),
                    "seal_no": "S-1N", "location": "A柜-01"}],
        )
        refrozen = handoff_services.freeze_handoff(handoff, operator=self.transferor)
        self.assertEqual(refrozen.version, 2)
        self.assertEqual(refrozen.status, HandoffStatus.FROZEN)

    def test_goods_occupied_by_other_frozen_handoff_blocks_effect(self):
        first = self.create_frozen()
        # 第二张交接单包含同一物资
        second, _ = handoff_services.create_handoff(
            transferor=self.transferor, receiver=self.receiver,
            items=[{"goods": self.goods_a.id, "expected_quantity": Decimal("5"),
                    "seal_no": "S-1", "location": "A柜-01"}],
        )
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.freeze_handoff(second, operator=self.transferor)

        # 第一张完成后释放占用，第二张仍不能自动生效，需要重新冻结
        items = list(first.items.all())
        for it in items:
            handoff_services.confirm_item(
                first, it, operator=self.receiver,
                actual_quantity=it.expected_quantity,
                actual_seal_no=it.seal_no, actual_location=it.location,
            )
        handoff_services.complete_handoff(first, operator=self.receiver)
        refrozen = handoff_services.freeze_handoff(second, operator=self.transferor)
        self.assertEqual(refrozen.status, HandoffStatus.FROZEN)

    def test_pending_stockout_blocks_freeze(self):
        """其他业务（待审批出库）占用物资时整单不得冻结"""
        from .models import StockOut
        StockOut.objects.create(
            goods=self.goods_a, operator=self.transferor,
            receiver="办案民警", quantity=Decimal("1"), status="pending",
        )
        handoff, _ = handoff_services.create_handoff(
            transferor=self.transferor, receiver=self.receiver,
            items=self.payload()["items"],
        )
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.freeze_handoff(handoff, operator=self.transferor)

    def test_completed_list_requires_correction(self):
        handoff = self.create_frozen()
        for it in handoff.items.all():
            handoff_services.confirm_item(
                handoff, it, operator=self.receiver,
                actual_quantity=it.expected_quantity,
                actual_seal_no=it.seal_no, actual_location=it.location,
            )
        handoff_services.complete_handoff(handoff, operator=self.receiver)

        # 已完成清单不能再冻结/修订
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.freeze_handoff(handoff, operator=self.transferor)

        correction = handoff_services.correct_handoff(
            handoff, operator=self.supervisor, reason="夜班复盘发现责任划分有误"
        )
        handoff.refresh_from_db()
        self.assertEqual(handoff.version, 2)
        self.assertEqual(correction.old_custodian, self.receiver)
        self.assertEqual(correction.new_custodian, self.transferor)
        self.assertEqual(handoff.custodian, self.transferor)
        # 原始明细确认数据未被改动
        self.assertTrue(
            handoff.items.filter(item_status=HandoffItemStatus.CONFIRMED).count() == 2
        )

    def test_idempotent_create_does_not_duplicate(self):
        first, created1 = handoff_services.create_handoff(
            transferor=self.transferor, receiver=self.receiver,
            items=self.payload()["items"], idempotency_key="night-20261002-01",
        )
        second, created2 = handoff_services.create_handoff(
            transferor=self.transferor, receiver=self.receiver,
            items=self.payload()["items"], idempotency_key="night-20261002-01",
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(HandoffList.objects.count(), 1)
        self.assertEqual(HandoffItem.objects.count(), 2)

    def test_role_guards(self):
        handoff = self.create_frozen()
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.freeze_handoff(handoff, operator=self.receiver)
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.return_handoff(
                handoff, operator=self.transferor, reason="x"
            )

    def test_same_transferor_receiver_rejected(self):
        with self.assertRaises(handoff_services.HandoffError):
            handoff_services.create_handoff(
                transferor=self.transferor, receiver=self.transferor,
                items=[{"goods": self.goods_a.id, "expected_quantity": Decimal("1")}],
            )


class HandoffAPITest(HandoffFixture):
    def test_api_full_flow(self):
        created = self.client.post("/api/handoffs/", self.payload(), format="json")
        self.assertEqual(created.status_code, 200, created.content)
        hid = created.json()["data"]["id"]
        self.assertEqual(created.json()["data"]["status"], "draft")

        frozen = self.client.post(f"/api/handoffs/{hid}/freeze/")
        self.assertEqual(frozen.status_code, 200, frozen.content)
        self.assertEqual(frozen.json()["data"]["status"], "frozen")

        detail = self.rcv_client.get(f"/api/handoffs/{hid}/")
        items = detail.json()["data"]["items"]
        for item in items:
            resp = self.rcv_client.post(
                f"/api/handoffs/{hid}/items/{item['id']}/confirm/",
                {"actual_quantity": item["expected_quantity"],
                 "actual_seal_no": item["seal_no"],
                 "actual_location": item["location"]},
                format="json",
            )
            self.assertEqual(resp.status_code, 200, resp.content)

        completed = self.rcv_client.post(f"/api/handoffs/{hid}/complete/")
        self.assertEqual(completed.status_code, 200, completed.content)
        data = completed.json()["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["custodian"], self.receiver.id)
        self.assertEqual(data["transferor_name"], "zhang")
        self.assertEqual(data["receiver_name"], "li")

    def test_api_duplicate_submit_same_idempotency_key(self):
        payload = self.payload(idempotency_key="dup-key-1")
        first = self.client.post("/api/handoffs/", payload, format="json")
        second = self.client.post("/api/handoffs/", payload, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["id"], second.json()["data"]["id"])
        self.assertEqual(HandoffList.objects.count(), 1)

    def test_api_return_revision_flow(self):
        created = self.client.post("/api/handoffs/", self.payload(), format="json")
        hid = created.json()["data"]["id"]
        self.client.post(f"/api/handoffs/{hid}/freeze/")

        bad_reason = self.rcv_client.post(f"/api/handoffs/{hid}/return/", {}, format="json")
        self.assertEqual(bad_reason.status_code, 400)

        returned = self.rcv_client.post(
            f"/api/handoffs/{hid}/return/", {"reason": "封签号对不上"}, format="json"
        )
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.json()["data"]["status"], "returned")

        revised = self.client.put(
            f"/api/handoffs/{hid}/",
            {"receiver": self.receiver.id,
             "items": [{"goods": self.goods_a.id, "expected_quantity": "5",
                        "seal_no": "S-1", "location": "A柜-01"}]},
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.content)
        refrozen = self.client.post(f"/api/handoffs/{hid}/freeze/")
        self.assertEqual(refrozen.json()["data"]["version"], 2)

    def test_api_correction_requires_admin(self):
        handoff = self.create_frozen()
        for it in handoff.items.all():
            handoff_services.confirm_item(
                handoff, it, operator=self.receiver,
                actual_quantity=it.expected_quantity,
                actual_seal_no=it.seal_no, actual_location=it.location,
            )
        handoff_services.complete_handoff(handoff, operator=self.receiver)

        forbidden = self.rcv_client.post(
            f"/api/handoffs/{handoff.pk}/corrections/",
            {"reason": "误操作"}, format="json",
        )
        self.assertEqual(forbidden.status_code, 403)

        ok = self.admin_client.post(
            f"/api/handoffs/{handoff.pk}/corrections/",
            {"reason": "责任归属更正"}, format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()["data"]["version"], 2)
        self.assertEqual(ok.json()["data"]["custodian"], self.transferor.id)
        self.assertEqual(ok.json()["data"]["corrections"][0]["reason"], "责任归属更正")

    def test_api_requires_authentication(self):
        resp = APIClient().get("/api/handoffs/")
        self.assertEqual(resp.status_code, 401)
