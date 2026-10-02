"""交接清单流程测试。"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from .models import (
    Goods,
    HandoverCorrection,
    HandoverEvent,
    HandoverItem,
    HandoverList,
    HandoverVersion,
    IdempotencyRecord,
    StockOut,
    Unit,
    Category,
    Variety,
)


class HandoverFixture(TestCase):
    def setUp(self):
        self.transferor = User.objects.create_user("night", "pass123456", role="user")
        self.receiver = User.objects.create_user("day", "pass123456", role="user")
        self.supervisor = User.objects.create_user("chief", "pass123456", role="admin")
        self.outsider = User.objects.create_user("outsider", "pass123456", role="user")

        self.unit = Unit.objects.create(name="台", created_by=self.supervisor)
        self.category = Category.objects.create(
            name="涉案设备", unit=self.unit, created_by=self.supervisor
        )
        self.variety = Variety.objects.create(
            name="记录终端", category=self.category, created_by=self.supervisor
        )
        self.goods_a = Goods.objects.create(
            variety=self.variety, name="执法记录仪A", code="DEV-A",
            quantity=Decimal("1"), location="1号柜",
        )
        self.goods_b = Goods.objects.create(
            variety=self.variety, name="执法记录仪B", code="DEV-B",
            quantity=Decimal("2"), location="1号柜",
        )

    def client_as(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    @property
    def night(self):
        return self.client_as(self.transferor)

    @property
    def day(self):
        return self.client_as(self.receiver)

    @property
    def chief(self):
        return self.client_as(self.supervisor)

    def create_draft(self, client=None, items=None, key=None):
        client = client or self.night
        payload = {
            "receiver": self.receiver.id,
            "transfer_note": "夜班交接",
            "items": items or [
                {"goods": self.goods_a.id, "expected_quantity": "1.00",
                 "expected_seal_no": "SEAL-A", "expected_location": "1号柜"},
                {"goods": self.goods_b.id, "expected_quantity": "2.00",
                 "expected_seal_no": "SEAL-B", "expected_location": "1号柜"},
            ],
        }
        if key:
            payload["idempotency_key"] = key
        return client.post("/api/handovers/", payload, format="json")

    def freeze(self, client, pk):
        return client.post(f"/api/handovers/{pk}/freeze/", {}, format="json")

    def adjudicate(self, client, pk, item_id, result="matched", **extra):
        payload = {"result": result}
        payload.update(extra)
        return client.post(
            f"/api/handovers/{pk}/items/{item_id}/adjudicate/", payload, format="json"
        )


class HappyPathTest(HandoverFixture):
    def test_full_handover_transfers_responsibility(self):
        created = self.create_draft()
        self.assertEqual(created.status_code, 201, created.content)
        hid = created.json()["data"]["id"]
        self.assertEqual(created.json()["data"]["status"], "draft")

        # 冻结前接收人不能逐项确认
        items = created.json()["data"]["items"]
        bad = self.adjudicate(self.day, hid, items[0]["id"])
        self.assertEqual(bad.status_code, 400)

        # 接收人不能冻结别人移交的清单
        forbidden = self.freeze(self.day, hid)
        self.assertEqual(forbidden.status_code, 403)

        frozen = self.freeze(self.night, hid)
        self.assertEqual(frozen.status_code, 200, frozen.content)
        self.assertEqual(frozen.json()["data"]["status"], "submitted")
        self.assertEqual(frozen.json()["data"]["version"], 1)

        # 冻结后物资行不可再修订
        revise = self.night.put(
            f"/api/handovers/{hid}/items/",
            {"items": [{"goods": self.goods_a.id, "expected_quantity": "9.00"}]},
            format="json",
        )
        self.assertEqual(revise.status_code, 400)

        # 接收人逐项确认数量、封签、存放位置
        items = frozen.json()["data"]["items"]
        for item in items:
            resp = self.adjudicate(
                self.day, hid, item["id"], result="matched",
                actual_quantity=item["expected_quantity"],
                actual_seal_no=item["expected_seal_no"],
                actual_location=item["expected_location"],
            )
            self.assertEqual(resp.status_code, 200, resp.content)

        # 全部确认前不能完成：先把其中一项重置为待确认场景由独立用例覆盖
        completed = self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")
        self.assertEqual(completed.status_code, 200, completed.content)
        data = completed.json()["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["responsible_party"], "receiver")
        self.assertIsNotNone(data["completed_at"])

        handover = HandoverList.objects.get(pk=hid)
        self.assertEqual(handover.responsible_party, "receiver")
        self.assertTrue(handover.is_completed)

    def test_cannot_complete_with_pending_items(self):
        hid = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, hid)
        resp = self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("尚未逐项确认", resp.json()["message"])
        self.assertEqual(HandoverList.objects.get(pk=hid).status, "submitted")


class ReturnAndRevisionTest(HandoverFixture):
    def test_difference_blocks_completion_until_returned_and_refrozen(self):
        hid = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, hid)

        items = HandoverItem.objects.filter(handover_id=hid, version=1).order_by("id")
        item_a, item_b = list(items)

        # A 一致；B 封签不符且数量短缺，必须填差异说明
        ok = self.adjudicate(self.day, hid, item_a.id, result="matched")
        self.assertEqual(ok.status_code, 200)
        no_reason = self.adjudicate(
            self.day, hid, item_b.id, result="different",
            actual_quantity="1.00", actual_seal_no="SEAL-B-BROKEN",
        )
        self.assertEqual(no_reason.status_code, 400)
        diff = self.adjudicate(
            self.day, hid, item_b.id, result="different",
            actual_quantity="1.00", actual_seal_no="SEAL-B-BROKEN",
            actual_location="1号柜", difference_reason="封签破损且少一台",
        )
        self.assertEqual(diff.status_code, 200)

        # 存在差异项时整单不能完成
        blocked = self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("差异", blocked.json()["message"])

        # 退回必须填写原因
        no_reason_return = self.day.post(
            f"/api/handovers/{hid}/return/", {}, format="json"
        )
        self.assertEqual(no_reason_return.status_code, 400)

        returned = self.day.post(
            f"/api/handovers/{hid}/return/",
            {"reason": "B机封签破损、数量不符"}, format="json",
        )
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.json()["data"]["status"], "returned")
        self.assertEqual(
            returned.json()["data"]["difference_reason"], "B机封签破损、数量不符"
        )

        # 移交人修订：去掉有争议的 B，仅保留 A
        revise = self.night.put(
            f"/api/handovers/{hid}/items/",
            {"items": [
                {"goods": self.goods_a.id, "expected_quantity": "1.00",
                 "expected_seal_no": "SEAL-A", "expected_location": "1号柜"},
            ]},
            format="json",
        )
        self.assertEqual(revise.status_code, 200, revise.content)

        # v1 历史行保留不变（仍带差异确认结果）
        v1_rows = HandoverItem.objects.filter(handover_id=hid, version=1)
        self.assertEqual(v1_rows.count(), 2)
        self.assertTrue(v1_rows.filter(goods=self.goods_b).exists())

        refrozen = self.freeze(self.night, hid)
        self.assertEqual(refrozen.status_code, 200)
        self.assertEqual(refrozen.json()["data"]["version"], 2)
        self.assertEqual(len(refrozen.json()["data"]["items"]), 1)

        item_a_v2 = HandoverItem.objects.get(handover_id=hid, version=2)
        done = self.adjudicate(self.day, hid, item_a_v2.id, result="matched")
        self.assertEqual(done.status_code, 200)
        completed = self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")
        self.assertEqual(completed.status_code, 200, completed.content)
        self.assertEqual(completed.json()["data"]["responsible_party"], "receiver")

        # 两个冻结版本快照均可审计
        versions = HandoverVersion.objects.filter(handover_id=hid).order_by("version")
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertEqual(versions[0].item_count, 2)
        self.assertEqual(versions[1].item_count, 1)
        self.assertEqual(versions[0].item_snapshot[0]["goods_code"], "DEV-A")


class OccupancyGuardTest(HandoverFixture):
    def test_approved_outbound_blocks_freeze(self):
        hid = self.create_draft().json()["data"]["id"]
        StockOut.objects.create(
            goods=self.goods_a, operator=self.supervisor,
            receiver="办案组", quantity=Decimal("1"), status="approved",
        )
        resp = self.freeze(self.night, hid)
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertIn("占用", body["message"])
        self.assertTrue(
            any(row["goods"] == self.goods_a.id for row in body["data"]["occupied"])
        )
        self.assertEqual(HandoverList.objects.get(pk=hid).status, "draft")

    def test_other_frozen_handover_blocks_freeze(self):
        first = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, first)

        second = self.create_draft().json()["data"]["id"]
        resp = self.freeze(self.night, second)
        self.assertEqual(resp.status_code, 409)
        # 第一单完成后，第二单可以冻结
        items = HandoverItem.objects.filter(handover_id=first, version=1)
        for item in items:
            self.adjudicate(self.day, first, item.id, result="matched")
        self.day.post(f"/api/handovers/{first}/complete/", {}, format="json")
        retry = self.freeze(self.night, second)
        self.assertEqual(retry.status_code, 200, retry.content)

    def test_occupancy_at_complete_blocks_whole_order(self):
        hid = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, hid)
        for item in HandoverItem.objects.filter(handover_id=hid, version=1):
            self.adjudicate(self.day, hid, item.id, result="matched")

        # 冻结之后物资被出库审批占用 → 完成时整单不生效
        StockOut.objects.create(
            goods=self.goods_b, operator=self.supervisor,
            receiver="办案组", quantity=Decimal("1"), status="approved",
        )
        resp = self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(HandoverList.objects.get(pk=hid).status, "submitted")

    def test_occupancy_check_endpoint(self):
        StockOut.objects.create(
            goods=self.goods_a, operator=self.supervisor,
            receiver="办案组", quantity=Decimal("1"), status="approved",
        )
        resp = self.chief.get(
            f"/api/handovers/occupancy-check/?goods={self.goods_a.id},{self.goods_b.id}"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertFalse(data["available"])
        self.assertEqual(len(data["occupied"]), 1)


class CompletedImmutabilityTest(HandoverFixture):
    def _completed_handover(self):
        hid = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, hid)
        for item in HandoverItem.objects.filter(handover_id=hid, version=1):
            self.adjudicate(self.day, hid, item.id, result="matched")
        self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")
        return hid

    def test_completed_list_only_changeable_via_correction(self):
        hid = self._completed_handover()
        before = HandoverList.objects.get(pk=hid)
        completed_at = before.completed_at

        self.assertEqual(self.freeze(self.night, hid).status_code, 400)
        self.assertEqual(
            self.night.post(f"/api/handovers/{hid}/cancel/", {}, format="json").status_code,
            400,
        )
        self.assertEqual(
            self.day.post(f"/api/handovers/{hid}/return/", {"reason": "x"}, format="json").status_code,
            400,
        )
        revise = self.night.put(
            f"/api/handovers/{hid}/items/",
            {"items": [{"goods": self.goods_a.id, "expected_quantity": "1.00"}]},
            format="json",
        )
        self.assertEqual(revise.status_code, 400)

        # 未完成清单不允许更正
        draft = self.create_draft().json()["data"]["id"]
        premature = self.chief.post(
            f"/api/handovers/{draft}/corrections/",
            {"goods": self.goods_a.id, "reason": "测试"}, format="json",
        )
        self.assertEqual(premature.status_code, 400)

        # 已完成清单通过更正记录处理，且只能追加
        correction = self.chief.post(
            f"/api/handovers/{hid}/corrections/",
            {
                "goods": self.goods_b.id,
                "reason": "复盘发现封签号登记错误",
                "correct_seal_no": "SEAL-B-NEW",
                "quantity_change": "-1.00",
                "detail": "以现场录像为准",
            },
            format="json",
        )
        self.assertEqual(correction.status_code, 200, correction.content)
        self.assertEqual(HandoverCorrection.objects.filter(handover_id=hid).count(), 1)

        after = HandoverList.objects.get(pk=hid)
        self.assertEqual(after.status, "completed")
        self.assertEqual(after.completed_at, completed_at)
        self.assertEqual(after.responsible_party, "receiver")

        corrections = self.chief.get(f"/api/handovers/{hid}/corrections/")
        self.assertEqual(corrections.status_code, 200)
        self.assertEqual(corrections.json()["data"][0]["correct_seal_no"], "SEAL-B-NEW")

    def test_outsider_cannot_correct_or_view_completed_handover(self):
        hid = self._completed_handover()
        outsider = self.client_as(self.outsider)
        payload = {"goods": self.goods_a.id, "reason": "无权更正"}
        self.assertEqual(
            outsider.post(f"/api/handovers/{hid}/corrections/", payload, format="json").status_code,
            403,
        )
        self.assertEqual(outsider.get(f"/api/handovers/{hid}/versions/").status_code, 403)
        self.assertEqual(
            HandoverCorrection.objects.filter(handover_id=hid).count(), 0
        )


class IdempotencyTest(HandoverFixture):
    def test_duplicate_create_with_same_key_makes_one_handover(self):
        payload = {
            "receiver": self.receiver.id,
            "items": [{"goods": self.goods_a.id, "expected_quantity": "1.00"}],
            "idempotency_key": "night-shift-2026-03-02",
        }
        first = self.night.post("/api/handovers/", payload, format="json")
        second = self.night.post("/api/handovers/", payload, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            first.json()["data"]["id"], second.json()["data"]["id"]
        )
        self.assertEqual(HandoverList.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)

    def test_same_key_different_payload_conflicts(self):
        payload = {
            "receiver": self.receiver.id,
            "items": [{"goods": self.goods_a.id, "expected_quantity": "1.00"}],
            "idempotency_key": "same-key",
        }
        self.night.post("/api/handovers/", payload, format="json")
        payload["items"] = [{"goods": self.goods_b.id, "expected_quantity": "1.00"}]
        conflict = self.night.post("/api/handovers/", payload, format="json")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(HandoverList.objects.count(), 1)

    def test_business_failure_is_replayed_without_side_effects(self):
        # 移交人=接收人属于服务层业务校验失败：同键重试应回放同一失败结果，
        # 不产生清单，也不重复执行。
        payload = {
            "receiver": self.transferor.id,
            "items": [{"goods": self.goods_a.id, "expected_quantity": "1.00"}],
            "idempotency_key": "self-handover",
        }
        first = self.night.post("/api/handovers/", payload, format="json")
        second = self.night.post("/api/handovers/", payload, format="json")
        self.assertEqual(first.status_code, 400)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(first.json()["message"], second.json()["message"])
        self.assertEqual(HandoverList.objects.count(), 0)
        self.assertEqual(
            IdempotencyRecord.objects.get(idempotency_key="self-handover").status_code,
            400,
        )

    def test_repeated_complete_does_not_create_second_transfer(self):
        hid = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, hid)
        for item in HandoverItem.objects.filter(handover_id=hid, version=1):
            self.adjudicate(self.day, hid, item.id, result="matched")

        payload = {"idempotency_key": "complete-key-1"}
        first = self.day.post(f"/api/handovers/{hid}/complete/", payload, format="json")
        second = self.day.post(
            f"/api/handovers/{hid}/complete/", payload, format="json",
            HTTP_IDEMPOTENCY_KEY="complete-key-1",
        )
        # 第三次不带键：状态机本身也必须拒绝重复生效
        third = self.day.post(f"/api/handovers/{hid}/complete/", {}, format="json")

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            first.json()["data"]["completed_at"],
            second.json()["data"]["completed_at"],
        )
        self.assertEqual(third.status_code, 400)
        complete_events = HandoverEvent.objects.filter(
            handover_id=hid, action="complete"
        )
        self.assertEqual(complete_events.count(), 1)


class SupervisorQueryTest(HandoverFixture):
    def test_detail_exposes_versions_parties_differences_and_responsibility(self):
        hid = self.create_draft().json()["data"]["id"]
        self.freeze(self.night, hid)
        items = list(HandoverItem.objects.filter(handover_id=hid, version=1))
        self.adjudicate(self.day, hid, items[0].id, result="matched")
        self.adjudicate(
            self.day, hid, items[1].id, result="different",
            actual_quantity="1.00", difference_reason="数量短缺",
        )
        self.day.post(
            f"/api/handovers/{hid}/return/", {"reason": "数量不符"}, format="json"
        )

        # 非相关人员无权查看
        self.assertEqual(
            self.client_as(self.outsider).get(f"/api/handovers/{hid}/").status_code, 403
        )

        resp = self.chief.get(f"/api/handovers/{hid}/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["transferor_name"], "night")
        self.assertEqual(data["receiver_name"], "day")
        self.assertEqual(data["status"], "returned")
        self.assertEqual(data["difference_reason"], "数量不符")
        self.assertEqual(data["responsible_party"], "transferor")
        self.assertEqual(len(data["versions"]), 1)
        self.assertEqual(data["versions"][0]["frozen_by_name"], "night")
        actions = [event["action"] for event in data["events"]]
        self.assertIn("freeze", actions)
        self.assertIn("return", actions)
        self.assertIn("item_confirm", actions)

    def test_supervisor_sees_all_handovers_in_list(self):
        self.create_draft()
        night_list = self.night.get("/api/handovers/").json()["data"]["total"]
        chief_list = self.chief.get("/api/handovers/").json()["data"]["total"]
        outsider_list = self.client_as(self.outsider).get("/api/handovers/").json()["data"]
        self.assertEqual(night_list, 1)
        self.assertEqual(chief_list, 1)
        self.assertEqual(outsider_list["total"], 0)


class ValidationTest(HandoverFixture):
    def test_create_requires_items_and_distinct_goods(self):
        resp = self.night.post(
            "/api/handovers/",
            {"receiver": self.receiver.id, "items": []}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        dup = self.night.post(
            "/api/handovers/",
            {"receiver": self.receiver.id, "items": [
                {"goods": self.goods_a.id, "expected_quantity": "1.00"},
                {"goods": self.goods_a.id, "expected_quantity": "1.00"},
            ]},
            format="json",
        )
        self.assertEqual(dup.status_code, 400)

    def test_transferor_and_receiver_must_differ(self):
        resp = self.night.post(
            "/api/handovers/",
            {"receiver": self.transferor.id,
             "items": [{"goods": self.goods_a.id, "expected_quantity": "1.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_requires_authentication(self):
        resp = APIClient().get("/api/handovers/")
        self.assertEqual(resp.status_code, 401)
