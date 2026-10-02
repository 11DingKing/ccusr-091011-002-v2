"""
物资交接清单业务流程

状态机：
    draft（待冻结） --freeze--> frozen（已冻结待确认）
    frozen --逐项确认--> 全部 confirmed 后 --complete--> completed（责任转移）
    frozen --return--> returned（退回修订）--freeze--> frozen（版本号递增）

约束：
    * 冻结时快照物资名称/编码，并校验任一物资是否已被其他业务占用；
    * 接收人逐项确认数量、封签、存放位置，存在差异即整单不能生效；
    * 已完成清单不可修改，只能通过更正记录调整；
    * 创建支持幂等键，重复提交/服务重试不会产生第二次交接；
    * 完成交接时登记物资占用，保证同一物资不会被两份未完成交接重复锁定。
"""
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone

from .models import (
    Goods, StockOut,
    HandoffList, HandoffItem, HandoffEvent, HandoffCorrection,
    HandoffStatus, HandoffItemStatus,
)


class HandoffError(Exception):
    """交接流程业务错误"""


def _now():
    return timezone.now()


def _to_quantity(value, code):
    """将输入数量统一为 Decimal，兼容字符串/数字"""
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise HandoffError(f'物资 {code} 的移交数量格式不正确')
    if quantity <= 0:
        raise HandoffError(f'物资 {code} 的移交数量必须大于0')
    return quantity


def generate_handoff_no():
    """生成交接单号：HJ + 时间戳 + 随机后缀，避免撞号"""
    return 'HJ' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:8]


def _record_event(handoff, action, operator, detail=''):
    HandoffEvent.objects.create(
        handoff=handoff, version=handoff.version,
        action=action, operator=operator, detail=detail
    )


def _find_active_handoff_for_goods(goods_ids, exclude_handoff_id=None):
    """
    返回已占用这些物资的其他在途交接明细（物资维度）。

    草稿尚未冻结、不构成有效主张，不锁定物资；
    已冻结待确认与退回修订中的交接持有物资，视为占用。
    """
    qs = HandoffItem.objects.filter(
        goods_id__in=goods_ids,
        handoff__status__in=[HandoffStatus.FROZEN, HandoffStatus.RETURNED],
    )
    if exclude_handoff_id:
        qs = qs.exclude(handoff_id=exclude_handoff_id)
    return qs.select_related('handoff')


def _goods_occupied_by_other_business(handoff, goods_ids):
    """
    检查物资是否被其他业务占用。

    占用来源：
      1. 其他冻结待确认/退回修订中的交接清单；
      2. 尚未终结的出库业务（待审批/已通过，已拒绝与已完成不算占用）。
    已完成的交接代表保管责任已明确归属，不视为占用。
    """
    occupied = [
        {
            'goods_id': item.goods_id,
            'goods_code': item.goods_code,
            'business': f'交接单 {item.handoff.handoff_no}',
        }
        for item in _find_active_handoff_for_goods(goods_ids, exclude_handoff_id=handoff.pk)
    ]

    outbound = StockOut.objects.filter(
        goods_id__in=goods_ids,
        status__in=['pending', 'approved'],
    ).select_related('goods')
    occupied.extend(
        {
            'goods_id': row.goods_id,
            'goods_code': row.goods.code,
            'business': f'出库单(id={row.pk}，{row.get_status_display()})',
        }
        for row in outbound
    )
    return occupied


@transaction.atomic
def create_handoff(*, transferor, receiver, items, remark='', idempotency_key=''):
    """
    创建交接清单（草稿态，内容尚未冻结）。

    items: [{'goods': id, 'expected_quantity': Decimal, 'seal_no': str, 'location': str}]
    幂等：相同 idempotency_key 的重复提交返回原单，不产生新交接。
    """
    if transferor.pk == receiver.pk:
        raise HandoffError('移交人与接收人不能为同一人')
    if not items:
        raise HandoffError('交接清单至少包含一项物资')

    key = (idempotency_key or '').strip() or None
    if key:
        existing = HandoffList.objects.filter(idempotency_key=key).first()
        if existing:
            return existing, False

    handoff = HandoffList(
        handoff_no=generate_handoff_no(),
        version=1,
        status=HandoffStatus.DRAFT,
        transferor=transferor,
        receiver=receiver,
        custodian=transferor,
        idempotency_key=key,
        remark=remark or '',
    )
    if key:
        # 幂等键并发冲突时复用已存在单据；嵌套事务隔离唯一约束错误
        try:
            with transaction.atomic():
                handoff.save()
        except IntegrityError:
            existing = HandoffList.objects.filter(idempotency_key=key).first()
            if existing:
                return existing, False
            raise
    else:
        handoff.save()

    seen_goods = set()
    item_rows = []
    for raw in items:
        goods_id = raw.get('goods')
        try:
            goods = Goods.objects.get(pk=goods_id)
        except Goods.DoesNotExist:
            raise HandoffError(f'物资不存在：{goods_id}')
        if goods_id in seen_goods:
            raise HandoffError(f'清单中物资重复：{goods.code}')
        seen_goods.add(goods_id)

        quantity = _to_quantity(raw.get('expected_quantity'), goods.code)

        item_rows.append(HandoffItem(
            handoff=handoff, goods=goods,
            goods_name=goods.name, goods_code=goods.code,
            expected_quantity=quantity,
            seal_no=(raw.get('seal_no') or '').strip(),
            location=(raw.get('location') or '').strip(),
        ))
    HandoffItem.objects.bulk_create(item_rows)
    _record_event(handoff, 'created', transferor, f'创建清单，共 {len(item_rows)} 项')
    return handoff, True


@transaction.atomic
def update_draft_handoff(handoff, *, operator, items, receiver=None, remark=None):
    """草稿/退回状态下修订清单内容，重新进入草稿态"""
    handoff = HandoffList.objects.select_for_update().get(pk=handoff.pk)
    if handoff.status not in [HandoffStatus.DRAFT, HandoffStatus.RETURNED]:
        raise HandoffError('仅待冻结或已退回的清单可以修订')
    if operator.pk != handoff.transferor_id:
        raise HandoffError('只有移交人可以修订清单')
    if not items:
        raise HandoffError('交接清单至少包含一项物资')

    if receiver is not None:
        if receiver.pk == handoff.transferor_id:
            raise HandoffError('移交人与接收人不能为同一人')
        handoff.receiver = receiver
    if remark is not None:
        handoff.remark = remark or ''

    handoff.items.all().delete()
    seen_goods = set()
    item_rows = []
    for raw in items:
        goods_id = raw.get('goods')
        try:
            goods = Goods.objects.get(pk=goods_id)
        except Goods.DoesNotExist:
            raise HandoffError(f'物资不存在：{goods_id}')
        if goods_id in seen_goods:
            raise HandoffError(f'清单中物资重复：{goods.code}')
        seen_goods.add(goods_id)
        quantity = _to_quantity(raw.get('expected_quantity'), goods.code)
        item_rows.append(HandoffItem(
            handoff=handoff, goods=goods,
            goods_name=goods.name, goods_code=goods.code,
            expected_quantity=quantity,
            seal_no=(raw.get('seal_no') or '').strip(),
            location=(raw.get('location') or '').strip(),
        ))
    HandoffItem.objects.bulk_create(item_rows)
    handoff.status = HandoffStatus.DRAFT
    handoff.save()
    _record_event(handoff, 'created', operator, f'修订清单，共 {len(item_rows)} 项')
    return handoff


@transaction.atomic
def freeze_handoff(handoff, *, operator):
    """移交人冻结清单：校验物资未被占用，冻结后接收人方可确认"""
    handoff = HandoffList.objects.select_for_update().get(pk=handoff.pk)
    if handoff.status not in [HandoffStatus.DRAFT, HandoffStatus.RETURNED]:
        raise HandoffError('当前状态不允许冻结')
    if operator.pk != handoff.transferor_id:
        raise HandoffError('只有移交人可以冻结清单')

    items = list(handoff.items.select_related('goods'))
    if not items:
        raise HandoffError('交接清单至少包含一项物资')

    goods_ids = [item.goods_id for item in items]
    # 锁定涉及物资行，防止与其他交接/出库业务并发通过占用检查
    list(Goods.objects.select_for_update().filter(pk__in=goods_ids))
    occupied = _goods_occupied_by_other_business(handoff, goods_ids)
    if occupied:
        codes = '、'.join(sorted({o['goods_code'] for o in occupied}))
        _record_event(handoff, 'rejected', operator,
                      f'冻结被拒，物资已被其他业务占用：{codes}')
        raise HandoffError(f'物资已被其他业务占用，整单不能冻结：{codes}')

    if handoff.events.filter(
        version=handoff.version, action='returned'
    ).exists():
        # 本版本曾被退回修订，重新冻结产生新版本
        handoff.version += 1

    handoff.status = HandoffStatus.FROZEN
    handoff.frozen_at = _now()
    handoff.save()
    _record_event(handoff, 'frozen', operator,
                  f'冻结清单 v{handoff.version}，共 {len(items)} 项')
    return handoff


@transaction.atomic
def confirm_item(handoff, item, *, operator, actual_quantity,
                 actual_seal_no, actual_location, difference_reason=''):
    """
    接收人逐项确认一项物资的数量、封签和存放位置。

    三项全部与移交快照一致 => confirmed；任一不一致 => disputed（整单不得生效）。
    允许重复提交（幂等）：已确认的项目按最新核对结果覆盖。
    """
    handoff = HandoffList.objects.select_for_update().get(pk=handoff.pk)
    if handoff.status != HandoffStatus.FROZEN:
        raise HandoffError('清单未冻结，不能确认')
    if operator.pk != handoff.receiver_id:
        raise HandoffError('只有接收人可以确认清单')

    try:
        item = handoff.items.select_for_update().get(pk=getattr(item, 'pk', item))
    except HandoffItem.DoesNotExist:
        raise HandoffError('明细不属于该交接清单')

    if actual_quantity is None or actual_quantity < 0:
        raise HandoffError('实收数量不能为空或负数')
    actual_seal_no = (actual_seal_no or '').strip()
    actual_location = (actual_location or '').strip()

    matched = (
        actual_quantity == item.expected_quantity
        and actual_seal_no == item.seal_no
        and actual_location == item.location
    )
    item.actual_quantity = actual_quantity
    item.actual_seal_no = actual_seal_no
    item.actual_location = actual_location
    item.difference_reason = difference_reason or ''
    item.item_status = HandoffItemStatus.CONFIRMED if matched else HandoffItemStatus.DISPUTED
    item.confirmed_at = _now()
    item.save()

    _record_event(
        handoff, 'item_confirmed', operator,
        f'确认 {item.goods_code}：{item.get_item_status_display()}'
        + (f'；差异原因：{difference_reason}' if difference_reason else '')
    )
    return item


@transaction.atomic
def return_handoff(handoff, *, operator, reason):
    """接收人在确认完成前退回清单要求移交人修订"""
    handoff = HandoffList.objects.select_for_update().get(pk=handoff.pk)
    if handoff.status != HandoffStatus.FROZEN:
        raise HandoffError('仅已冻结待确认的清单可以退回')
    if operator.pk != handoff.receiver_id:
        raise HandoffError('只有接收人可以退回清单')
    if not (reason or '').strip():
        raise HandoffError('退回必须填写差异原因')

    handoff.status = HandoffStatus.RETURNED
    handoff.frozen_at = None
    handoff.save()
    # 退回后逐项确认结果作废，等待修订后重新确认
    handoff.items.update(
        item_status=HandoffItemStatus.PENDING,
        actual_quantity=None, actual_seal_no='', actual_location='',
        difference_reason='', confirmed_at=None,
    )
    _record_event(handoff, 'returned', operator, f'退回修订：{reason.strip()}')
    return handoff


@transaction.atomic
def complete_handoff(handoff, *, operator):
    """
    接收人确认全部明细无差异后完成交接，保管责任由移交人转移至接收人。

    生效前再次校验：所有明细必须已确认且无差异，且物资未被其他业务占用。
    """
    handoff = HandoffList.objects.select_for_update().get(pk=handoff.pk)
    if handoff.status != HandoffStatus.FROZEN:
        raise HandoffError('当前状态不允许完成交接')
    if operator.pk != handoff.receiver_id:
        raise HandoffError('只有接收人可以完成交接')

    items = list(handoff.items.all())
    if not items:
        raise HandoffError('交接清单没有明细')

    pending = [i for i in items if i.item_status != HandoffItemStatus.CONFIRMED]
    if pending:
        codes = '、'.join(i.goods_code for i in pending)
        raise HandoffError(f'存在未确认或有差异的物资，整单不得生效：{codes}')

    goods_ids = [i.goods_id for i in items]
    # 生效前最终复检：锁定物资行，确认没有被并发的其他业务占用
    list(Goods.objects.select_for_update().filter(pk__in=goods_ids))
    occupied = _goods_occupied_by_other_business(handoff, goods_ids)
    if occupied:
        codes = '、'.join(sorted({o['goods_code'] for o in occupied}))
        _record_event(handoff, 'rejected', operator,
                      f'完成被拒，物资已被其他业务占用：{codes}')
        raise HandoffError(f'物资已被其他业务占用，整单不得生效：{codes}')

    handoff.status = HandoffStatus.COMPLETED
    handoff.completed_at = _now()
    handoff.custodian = handoff.receiver
    handoff.save()
    _record_event(handoff, 'completed', operator,
                  f'交接完成，保管责任转移至 {handoff.receiver.username}')
    return handoff


@transaction.atomic
def correct_handoff(handoff, *, operator, reason, new_custodian=None):
    """
    已完成清单的更正入口：只追加更正记录，不改动原始确认数据。

    默认将责任归属更正回移交人；也可指定新的责任人。每次更正版本号递增。
    """
    handoff = HandoffList.objects.select_for_update().get(pk=handoff.pk)
    if handoff.status != HandoffStatus.COMPLETED:
        raise HandoffError('只有已完成的清单可以更正')
    if not (reason or '').strip():
        raise HandoffError('更正必须填写原因')

    old_custodian = handoff.custodian
    target = new_custodian or handoff.transferor
    if target.pk == (old_custodian.pk if old_custodian else None):
        raise HandoffError('更正后的责任人与当前责任人相同，无需更正')

    handoff.version += 1
    handoff.custodian = target
    handoff.save()
    correction = HandoffCorrection.objects.create(
        handoff=handoff, version=handoff.version,
        reason=reason.strip(),
        old_custodian=old_custodian, new_custodian=target,
        operator=operator,
    )
    _record_event(
        handoff, 'amended', operator,
        f'更正 v{handoff.version}：责任由 '
        f'{old_custodian.username if old_custodian else "-"} '
        f'变更为 {target.username}；原因：{reason.strip()}'
    )
    return correction
