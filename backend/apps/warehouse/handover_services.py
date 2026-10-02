"""
交接清单业务流程

状态流转：
    draft（编制中）
      ──冻结──▶ submitted（待确认）
      │            ├──逐项确认全部一致──▶ completed（责任转移至接收人）
      │            ├──退回──▶ returned ──修订──▶ 重新冻结（版本号 +1）
      │            └──（移交人）作废──▶ cancelled
      └──作废──▶ cancelled

已完成（completed）的清单为终态，任何改动只能通过更正记录（HandoverCorrection）。
"""
import hashlib
import json
import random
import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.authentication.models import User
from apps.core.exceptions import (
    BusinessException,
    NotFoundException,
    PermissionException,
)
from .models import (
    Goods,
    HandoverCorrection,
    HandoverEvent,
    HandoverItem,
    HandoverList,
    HandoverVersion,
    IdempotencyRecord,
    ITEM_CONFIRM_DIFFERENT,
    ITEM_CONFIRM_MATCHED,
    ITEM_CONFIRM_PENDING,
    HANDOVER_STATUS_CANCELLED,
    HANDOVER_STATUS_COMPLETED,
    HANDOVER_STATUS_DRAFT,
    HANDOVER_STATUS_RETURNED,
    HANDOVER_STATUS_SUBMITTED,
    RESPONSIBILITY_RECEIVER,
    RESPONSIBILITY_TRANSFEROR,
    StockOut,
)


class HandoverOccupiedError(BusinessException):
    """清单内物资被其他业务占用，整单不得生效。"""

    def __init__(self, occupied):
        super().__init__('清单内物资已被其他业务占用，整单不得生效', code=409)
        self.occupied = occupied


# ---------------------------------------------------------------- 编号与查询

def generate_handover_no():
    """生成交接单号 HO + 时间戳 + 随机串，冲突时重试。"""
    stamp = timezone.now().strftime('%Y%m%d%H%M%S')
    for _ in range(5):
        no = f"HO{stamp}{random.randint(0, 9999):04d}"
        if not HandoverList.objects.filter(handover_no=no).exists():
            return no
    return f"HO{stamp}{uuid.uuid4().hex[:8]}"


def get_handover_or_404(pk):
    try:
        return HandoverList.objects.get(pk=pk)
    except HandoverList.DoesNotExist:
        raise NotFoundException('交接清单不存在')


def _get_active_user(user_id):
    try:
        return User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        raise BusinessException('接收人不存在或已停用')


def _add_event(handover, action, actor, detail='', from_status='', to_status='', version=None):
    HandoverEvent.objects.create(
        handover=handover,
        action=action,
        actor=actor,
        from_status=from_status or '',
        to_status=to_status or '',
        version=handover.version if version is None else version,
        detail=detail or '',
    )


def find_occupied_goods(goods_ids, exclude_handover_id=None):
    """检查物资是否被其他业务占用，返回 {goods_id: 占用原因}。

    占用来源：
    1. 已审批待出库的出库记录（物资已被预留）；
    2. 其他处于冻结待确认状态的交接清单（同一批物资不能同时交接两次）。
    """
    occupied = {}

    approved_outbounds = StockOut.objects.filter(
        goods_id__in=goods_ids, status='approved'
    ).select_related('goods')
    for stock_out in approved_outbounds:
        occupied.setdefault(
            stock_out.goods_id,
            f"物资已被审批通过的出库单(#{stock_out.id})预留占用",
        )

    active_handovers = HandoverItem.objects.filter(
        goods_id__in=goods_ids,
        handover__status=HANDOVER_STATUS_SUBMITTED,
    ).exclude(
        handover_id=exclude_handover_id
    ).select_related('goods', 'handover')
    for item in active_handovers:
        occupied.setdefault(
            item.goods_id,
            f"物资已被冻结中的交接单({item.handover.handover_no})占用",
        )

    return occupied


def _assert_not_occupied(handover, goods_ids):
    occupied = find_occupied_goods(goods_ids, exclude_handover_id=handover.id)
    if occupied:
        goods_map = Goods.objects.in_bulk(occupied.keys())
        detail = [
            {
                'goods': gid,
                'goods_name': goods_map[gid].name if gid in goods_map else str(gid),
                'reason': reason,
            }
            for gid, reason in occupied.items()
        ]
        raise HandoverOccupiedError(detail)


# ---------------------------------------------------------------- 主流程

@transaction.atomic
def create_handover(transferor, receiver_id, transfer_note, items_data):
    """移交人创建编制中的交接清单。"""
    receiver = _get_active_user(receiver_id)
    if receiver.id == transferor.id:
        raise BusinessException('移交人与接收人不能为同一人')
    if not items_data:
        raise BusinessException('交接清单至少包含一项物资')

    goods_ids = [row['goods'] for row in items_data]
    if len(set(goods_ids)) != len(goods_ids):
        raise BusinessException('同一物资在清单中只能出现一次')

    goods_map = _validate_goods(goods_ids)

    handover = HandoverList.objects.create(
        handover_no=generate_handover_no(),
        transferor=transferor,
        receiver=receiver,
        status=HANDOVER_STATUS_DRAFT,
        transfer_note=transfer_note or '',
        responsible_party=RESPONSIBILITY_TRANSFEROR,
    )
    HandoverItem.objects.bulk_create([
        HandoverItem(
            handover=handover,
            goods=goods_map[row['goods']],
            version=0,
            expected_quantity=row['expected_quantity'],
            expected_seal_no=row.get('expected_seal_no', ''),
            expected_location=row.get('expected_location', ''),
        )
        for row in items_data
    ])
    _add_event(
        handover, 'create', transferor,
        detail=f"创建交接清单，共 {len(items_data)} 项物资，接收人：{receiver.username}",
        to_status=HANDOVER_STATUS_DRAFT,
    )
    return handover


def _validate_goods(goods_ids):
    goods_map = Goods.objects.filter(is_active=True).in_bulk(goods_ids)
    missing = [gid for gid in goods_ids if gid not in goods_map]
    if missing:
        raise BusinessException(f'物资不存在或已停用：{missing}')
    return goods_map


@transaction.atomic
def replace_working_items(handover, user, items_data):
    """移交人在编制中/被退回状态下整单修订物资行（仅改动版本 0 的工作副本）。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_transferor(handover, user)
    if not handover.is_editable:
        raise BusinessException('清单已冻结或已完结，不能修订物资行')
    if not items_data:
        raise BusinessException('交接清单至少包含一项物资')

    goods_ids = [row['goods'] for row in items_data]
    if len(set(goods_ids)) != len(goods_ids):
        raise BusinessException('同一物资在清单中只能出现一次')
    goods_map = _validate_goods(goods_ids)

    HandoverItem.objects.filter(handover=handover, version=0).delete()
    HandoverItem.objects.bulk_create([
        HandoverItem(
            handover=handover,
            goods=goods_map[row['goods']],
            version=0,
            expected_quantity=row['expected_quantity'],
            expected_seal_no=row.get('expected_seal_no', ''),
            expected_location=row.get('expected_location', ''),
        )
        for row in items_data
    ])
    return handover


@transaction.atomic
def freeze_handover(handover, user):
    """移交人冻结清单：版本号 +1，固化快照，转入待确认状态。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_transferor(handover, user)

    if handover.status == HANDOVER_STATUS_SUBMITTED:
        raise BusinessException('清单已冻结，等待接收人确认')
    if not handover.is_editable:
        raise BusinessException('当前状态下不能冻结清单')

    working_items = list(
        HandoverItem.objects.filter(handover=handover, version=0)
        .select_related('goods')
    )
    if not working_items:
        raise BusinessException('交接清单没有可冻结的物资行')

    goods_ids = [item.goods_id for item in working_items]

    from_status = handover.status
    new_version = handover.version + 1
    now = timezone.now()

    # 先写入（提升版本号）以获取数据库写锁：SQLite 下 select_for_update 为空操作，
    # 写锁可使并发冻结在此串行化；随后再做占用复核，若被占用则整体回滚。
    HandoverItem.objects.filter(handover=handover, version=0).update(version=new_version)
    _assert_not_occupied(handover, goods_ids)

    snapshot = [
        {
            'item_id': item.id,
            'goods': item.goods_id,
            'goods_code': item.goods.code,
            'goods_name': item.goods.name,
            'expected_quantity': str(item.expected_quantity),
            'expected_seal_no': item.expected_seal_no,
            'expected_location': item.expected_location,
        }
        for item in working_items
    ]
    HandoverVersion.objects.create(
        handover=handover,
        version=new_version,
        frozen_by=user,
        item_count=len(snapshot),
        item_snapshot=snapshot,
    )

    handover.version = new_version
    handover.status = HANDOVER_STATUS_SUBMITTED
    handover.frozen_at = now
    if handover.submitted_at is None:
        handover.submitted_at = now
    handover.save(update_fields=[
        'version', 'status', 'frozen_at', 'submitted_at', 'updated_at'
    ])
    _add_event(
        handover, 'freeze', user,
        detail=f"冻结清单 v{new_version}，共 {len(snapshot)} 项物资",
        from_status=from_status, to_status=HANDOVER_STATUS_SUBMITTED,
    )
    return handover


@transaction.atomic
def adjudicate_item(handover, item_id, user, result, actual):
    """接收人对当前冻结版本中的某一项进行逐项确认。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_receiver(handover, user)
    if handover.status != HANDOVER_STATUS_SUBMITTED:
        raise BusinessException('清单未处于待确认状态，不能逐项确认')

    try:
        item = HandoverItem.objects.get(pk=item_id, handover=handover, version=handover.version)
    except HandoverItem.DoesNotExist:
        raise NotFoundException('清单物资行不存在或不属于当前冻结版本')

    if result == ITEM_CONFIRM_DIFFERENT and not actual.get('difference_reason'):
        raise BusinessException('确认存在差异时必须填写差异说明')

    item.confirm_result = result
    item.actual_quantity = actual.get('actual_quantity')
    item.actual_seal_no = actual.get('actual_seal_no', '')
    item.actual_location = actual.get('actual_location', '')
    item.difference_reason = actual.get('difference_reason', '')
    item.confirmed_by = user
    item.confirmed_at = timezone.now()
    if result == ITEM_CONFIRM_MATCHED and item.actual_quantity is None:
        item.actual_quantity = item.expected_quantity
        item.actual_seal_no = item.actual_seal_no or item.expected_seal_no
        item.actual_location = item.actual_location or item.expected_location
    item.save()

    result_text = '一致' if result == ITEM_CONFIRM_MATCHED else '存在差异'
    detail = f"逐项确认《{item.goods.name}》：{result_text}"
    if item.difference_reason:
        detail += f"；差异说明：{item.difference_reason}"
    _add_event(handover, 'item_confirm', user, detail=detail)
    return item


@transaction.atomic
def return_handover(handover, user, reason):
    """接收人在确认完成前退回清单，移交人据此修订后重新冻结。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_receiver(handover, user)
    if handover.status != HANDOVER_STATUS_SUBMITTED:
        raise BusinessException('只有待确认的清单才能退回')
    if not reason or not reason.strip():
        raise BusinessException('退回时必须填写差异/退回原因')

    # 当前冻结版本行保留为不可变历史，复制出版本 0 的工作副本供修订
    current_items = list(HandoverItem.objects.filter(
        handover=handover, version=handover.version
    ))
    HandoverItem.objects.bulk_create([
        HandoverItem(
            handover=handover,
            goods_id=item.goods_id,
            version=0,
            expected_quantity=item.expected_quantity,
            expected_seal_no=item.expected_seal_no,
            expected_location=item.expected_location,
        )
        for item in current_items
    ])

    handover.status = HANDOVER_STATUS_RETURNED
    handover.returned_at = timezone.now()
    handover.difference_reason = reason.strip()
    handover.save(update_fields=['status', 'returned_at', 'difference_reason', 'updated_at'])
    _add_event(
        handover, 'return', user,
        detail=f"接收人退回清单 v{handover.version}，原因：{reason.strip()}",
        from_status=HANDOVER_STATUS_SUBMITTED, to_status=HANDOVER_STATUS_RETURNED,
    )
    return handover


@transaction.atomic
def complete_handover(handover, user):
    """接收人完成交接：逐项确认全部一致且无物资被占用时，责任才发生转移。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_receiver(handover, user)
    if handover.status == HANDOVER_STATUS_COMPLETED:
        raise BusinessException('清单已完成，请勿重复提交')
    if handover.status != HANDOVER_STATUS_SUBMITTED:
        raise BusinessException('只有待确认的清单才能完成交接')

    current_items = list(
        HandoverItem.objects.filter(handover=handover, version=handover.version)
        .select_related('goods')
    )
    pending = [item for item in current_items if item.confirm_result == ITEM_CONFIRM_PENDING]
    if pending:
        names = '、'.join(item.goods.name for item in pending)
        raise BusinessException(f'以下物资尚未逐项确认，不能完成交接：{names}')

    different = [item for item in current_items if item.confirm_result == ITEM_CONFIRM_DIFFERENT]
    if different:
        lines = '；'.join(
            f"{item.goods.name}({item.difference_reason or '存在差异'})" for item in different
        )
        raise BusinessException(f'以下物资确认存在差异，请退回移交人修订后再交接：{lines}')

    # 获取写锁以串行化并发完成（SQLite 下 select_for_update 为空操作）；
    # 若随后占用复核失败，本事务回滚，该触碰不会留下改动。
    HandoverItem.objects.filter(
        handover=handover, version=handover.version
    ).update(updated_at=timezone.now())
    # 完成前再次复核占用情况，任一物资被占用则整单不生效
    _assert_not_occupied(handover, [item.goods_id for item in current_items])

    handover.status = HANDOVER_STATUS_COMPLETED
    handover.completed_at = timezone.now()
    handover.responsible_party = RESPONSIBILITY_RECEIVER
    handover.difference_reason = ''
    handover.save(update_fields=[
        'status', 'completed_at', 'responsible_party',
        'difference_reason', 'updated_at',
    ])
    _add_event(
        handover, 'complete', user,
        detail=f"全部 {len(current_items)} 项物资逐项确认一致，责任转移至接收人",
        from_status=HANDOVER_STATUS_SUBMITTED, to_status=HANDOVER_STATUS_COMPLETED,
    )
    return handover


@transaction.atomic
def cancel_handover(handover, user, reason):
    """移交人在完成前作废清单。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_transferor(handover, user)
    if handover.status == HANDOVER_STATUS_COMPLETED:
        raise BusinessException('已完成的清单不能作废，只能通过更正记录处理')
    if handover.status == HANDOVER_STATUS_CANCELLED:
        raise BusinessException('清单已作废')

    from_status = handover.status
    handover.status = HANDOVER_STATUS_CANCELLED
    handover.cancelled_at = timezone.now()
    if reason:
        handover.difference_reason = reason.strip()
    handover.save(update_fields=[
        'status', 'cancelled_at', 'difference_reason', 'updated_at'
    ])
    _add_event(
        handover, 'cancel', user,
        detail=f"作废清单。{('原因：' + reason.strip()) if reason else ''}",
        from_status=from_status, to_status=HANDOVER_STATUS_CANCELLED,
    )
    return handover


@transaction.atomic
def create_correction(handover, user, payload):
    """对已完成清单追加更正记录——终态清单的唯一处理途径。"""
    handover = HandoverList.objects.select_for_update().get(pk=handover.pk)
    _require_party(handover, user)
    if handover.status != HANDOVER_STATUS_COMPLETED:
        raise BusinessException('只有已完成的清单才需要更正记录')

    try:
        goods = Goods.objects.get(pk=payload['goods'], is_active=True)
    except Goods.DoesNotExist:
        raise BusinessException('更正物资不存在或已停用')
    if not HandoverItem.objects.filter(handover=handover, goods=goods).exists():
        raise BusinessException('更正物资不在该交接清单内')

    correction = HandoverCorrection.objects.create(
        handover=handover,
        goods=goods,
        reason=payload['reason'],
        quantity_change=payload.get('quantity_change') or Decimal('0'),
        correct_seal_no=payload.get('correct_seal_no', ''),
        correct_location=payload.get('correct_location', ''),
        detail=payload.get('detail', ''),
        created_by=user,
    )
    _add_event(
        handover, 'create', user,
        detail=(
            f"追加更正记录 #{correction.id}：{goods.name}；"
            f"原因：{correction.reason}；数量更正：{correction.quantity_change}"
        ),
        version=handover.version,
    )
    return correction


# ---------------------------------------------------------------- 权限与幂等

def _require_transferor(handover, user):
    if handover.transferor_id != user.id and not user.is_admin:
        raise PermissionException('只有移交人可以执行该操作')


def _require_receiver(handover, user):
    if handover.receiver_id != user.id and not user.is_admin:
        raise PermissionException('只有接收人可以执行该操作')


def _require_party(handover, user):
    """交接双方或主管（用于更正记录等事后处理）。"""
    if (handover.transferor_id != user.id
            and handover.receiver_id != user.id and not user.is_admin):
        raise PermissionException('只有交接双方或主管可以处理该清单')


def payload_fingerprint(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class IdempotencyReplay(Exception):
    """重复提交，携带首次请求的可回放响应。"""

    def __init__(self, body, status_code, replay=True):
        self.body = body
        self.status_code = status_code
        self.replay = replay


class IdempotencyConflict(BusinessException):
    def __init__(self, message):
        super().__init__(message, code=409)


def run_idempotent(request, scope, runner):
    """幂等执行包装器。

    - 携带 Idempotency-Key 时：首次请求落库并执行；服务重启/重复提交直接回放
      首次响应，不会制造第二次交接；
    - 同一键但请求体不同 → 409；
    - 首次请求在提交前崩溃 → 幂等记录与交接单一并回滚，可用同一键安全重试；
    - 业务失败 → 回滚交接单改动但保留失败结果，同键重试得到同样的失败响应；
    - 未携带键时直接执行（状态机本身仍防止重复生效）。
    """
    key = (request.META.get('HTTP_IDEMPOTENCY_KEY')
           or request.data.get('idempotency_key') or '').strip()
    if not key:
        return runner(None)

    fingerprint = payload_fingerprint(request.data)
    # 幂等记录与交接单在同一顶层事务内提交：
    # 服务在提交前崩溃/重启 -> 两者一起回滚，客户端可用同一键安全重试；
    # 提交后重复提交 -> 命中唯一键，回放首次响应。
    failure = None
    with transaction.atomic():
        # 插入放在独立保存点内：唯一键冲突不会污染外层事务
        try:
            with transaction.atomic():
                record = IdempotencyRecord.objects.create(
                    idempotency_key=key,
                    user=request.user,
                    scope=scope,
                    request_path=request.path,
                    request_hash=fingerprint,
                )
        except IntegrityError:
            existing = IdempotencyRecord.objects.filter(idempotency_key=key).first()
            if existing is None:
                raise
            if existing.scope != scope:
                raise IdempotencyConflict('幂等键已用于其他业务操作')
            if existing.request_hash != fingerprint:
                raise IdempotencyConflict('幂等键对应的请求内容不一致')
            raise IdempotencyReplay(existing.response_body, existing.status_code)

        # 业务执行放在独立保存点：业务失败只回滚交接单改动，
        # 失败结果仍随幂等记录提交，使同键重复提交得到同样的失败响应。
        try:
            with transaction.atomic():
                result = runner(record)
        except BusinessException as exc:
            body = {'success': False, 'code': exc.code, 'message': exc.message, 'data': None}
            if isinstance(exc, HandoverOccupiedError):
                body['data'] = {'occupied': exc.occupied}
            record.status_code = exc.code
            record.response_body = body
            record.save(update_fields=['status_code', 'response_body'])
            failure = exc
        else:
            # runner 返回 (handover, response_data, status_code)
            handover, body, status_code = result
            record.handover = handover
            record.status_code = status_code
            record.response_body = body
            record.save(update_fields=['handover', 'status_code', 'response_body'])

    if failure is not None:
        raise failure
    return result
