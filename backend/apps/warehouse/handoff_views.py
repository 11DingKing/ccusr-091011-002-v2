"""
物资交接清单视图

流程接口：
    POST   /api/handoffs/                          移交人创建清单（支持幂等键）
    GET    /api/handoffs/                          交接单列表（主管可查全部）
    GET    /api/handoffs/<pk>/                      详情（版本/双方/差异/责任归属/事件）
    PUT    /api/handoffs/<pk>/                      待冻结或退回状态下修订
    POST   /api/handoffs/<pk>/freeze/              移交人冻结清单
    POST   /api/handoffs/<pk>/return/             接收人退回修订
    POST   /api/handoffs/<pk>/items/<item_pk>/confirm/  接收人逐项确认
    POST   /api/handoffs/<pk>/complete/            接收人完成交接
    POST   /api/handoffs/<pk>/corrections/         主管对已完成清单发起更正
"""
import logging

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.authentication.models import User
from apps.core.response import success_response, error_response
from .models import HandoffList
from . import handoff_services
from .handoff_services import HandoffError
from .serializers import (
    HandoffListSerializer, HandoffCreateSerializer, HandoffUpdateSerializer,
    HandoffConfirmItemSerializer, HandoffReturnSerializer, HandoffCorrectSerializer,
)

logger = logging.getLogger('apps')


def _first_error(serializer):
    errors = serializer.errors
    first_error = list(errors.values())[0]
    if isinstance(first_error, list):
        first_error = first_error[0]
    return str(first_error)


def _get_handoff(pk):
    return (
        HandoffList.objects
        .select_related('transferor', 'receiver', 'custodian')
        .prefetch_related(
            'items', 'events__operator',
            'corrections__old_custodian', 'corrections__new_custodian',
            'corrections__operator',
        )
        .filter(pk=pk)
        .first()
    )


class HandoffListView(APIView):
    """交接清单列表/创建"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = HandoffList.objects.select_related(
            'transferor', 'receiver', 'custodian'
        ).prefetch_related('items').all()

        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        handoff_no = request.query_params.get('handoff_no')
        if handoff_no:
            queryset = queryset.filter(handoff_no__icontains=handoff_no)
        goods_code = request.query_params.get('goods_code')
        if goods_code:
            queryset = queryset.filter(items__goods_code__icontains=goods_code)

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = max(int(request.query_params.get('page_size', 10)), 1)
        except (TypeError, ValueError):
            page, page_size = 1, 10

        total = queryset.count()
        handoffs = queryset[(page - 1) * page_size:page * page_size]
        serializer = HandoffListSerializer(handoffs, many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size,
        })

    def post(self, request):
        serializer = HandoffCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        receiver = User.objects.filter(
            pk=serializer.validated_data['receiver'], is_active=True
        ).first()
        if receiver is None:
            return error_response(message='接收人不存在或已停用')

        try:
            handoff, created = handoff_services.create_handoff(
                transferor=request.user,
                receiver=receiver,
                items=serializer.validated_data['items'],
                remark=serializer.validated_data.get('remark', ''),
                idempotency_key=serializer.validated_data.get('idempotency_key', ''),
            )
        except HandoffError as exc:
            return error_response(message=str(exc))

        if not created:
            # 幂等重试：返回已存在的交接单，不重复创建
            logger.info(
                f"Idempotent handoff create hit: key={handoff.idempotency_key} "
                f"handoff={handoff.handoff_no}"
            )
            return success_response(
                data=HandoffListSerializer(_get_handoff(handoff.pk)).data,
                message='该交接单已提交，请勿重复提交'
            )

        logger.info(
            f"User {request.user.username} created handoff {handoff.handoff_no}"
        )
        return success_response(
            data=HandoffListSerializer(_get_handoff(handoff.pk)).data,
            message='创建成功'
        )


class HandoffDetailView(APIView):
    """交接清单详情/修订"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        handoff = _get_handoff(pk)
        if handoff is None:
            return error_response(message='交接清单不存在', code=404)
        return success_response(data=HandoffListSerializer(handoff).data)

    def put(self, request, pk):
        handoff = HandoffList.objects.filter(pk=pk).first()
        if handoff is None:
            return error_response(message='交接清单不存在', code=404)

        serializer = HandoffUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        receiver = None
        if 'receiver' in serializer.validated_data:
            receiver = User.objects.filter(
                pk=serializer.validated_data['receiver'], is_active=True
            ).first()
            if receiver is None:
                return error_response(message='接收人不存在或已停用')

        try:
            handoff = handoff_services.update_draft_handoff(
                handoff, operator=request.user,
                items=serializer.validated_data['items'],
                receiver=receiver,
                remark=serializer.validated_data.get('remark'),
            )
        except HandoffError as exc:
            return error_response(message=str(exc))

        logger.info(
            f"User {request.user.username} revised handoff {handoff.handoff_no}"
        )
        return success_response(
            data=HandoffListSerializer(_get_handoff(handoff.pk)).data,
            message='修订成功'
        )


class HandoffActionView(APIView):
    """冻结/退回/完成 三类整单动作的通用视图"""
    permission_classes = [IsAuthenticated]
    action = None

    def post(self, request, pk):
        handoff = HandoffList.objects.filter(pk=pk).first()
        if handoff is None:
            return error_response(message='交接清单不存在', code=404)

        try:
            if self.action == 'freeze':
                handoff = handoff_services.freeze_handoff(
                    handoff, operator=request.user
                )
                message = '清单已冻结，等待接收人逐项确认'
            elif self.action == 'return':
                serializer = HandoffReturnSerializer(data=request.data)
                if not serializer.is_valid():
                    return error_response(message=_first_error(serializer))
                handoff = handoff_services.return_handoff(
                    handoff, operator=request.user,
                    reason=serializer.validated_data['reason'],
                )
                message = '清单已退回移交人修订'
            elif self.action == 'complete':
                handoff = handoff_services.complete_handoff(
                    handoff, operator=request.user
                )
                message = '交接完成，保管责任已转移'
            else:  # pragma: no cover - 路由固定不会出现
                return error_response(message='不支持的操作')
        except HandoffError as exc:
            return error_response(message=str(exc))

        logger.info(
            f"User {request.user.username} {self.action} handoff {handoff.handoff_no}"
        )
        return success_response(
            data=HandoffListSerializer(_get_handoff(handoff.pk)).data,
            message=message
        )


class HandoffItemConfirmView(APIView):
    """接收人逐项确认数量、封签、存放位置"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, item_pk):
        handoff = HandoffList.objects.filter(pk=pk).first()
        if handoff is None:
            return error_response(message='交接清单不存在', code=404)

        serializer = HandoffConfirmItemSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        try:
            item = handoff_services.confirm_item(
                handoff, item_pk, operator=request.user,
                actual_quantity=serializer.validated_data['actual_quantity'],
                actual_seal_no=serializer.validated_data.get('actual_seal_no', ''),
                actual_location=serializer.validated_data.get('actual_location', ''),
                difference_reason=serializer.validated_data.get('difference_reason', ''),
            )
        except HandoffError as exc:
            return error_response(message=str(exc))

        logger.info(
            f"User {request.user.username} confirmed item {item.goods_code} "
            f"of handoff {handoff.handoff_no}: {item.item_status}"
        )
        return success_response(
            data=HandoffListSerializer(_get_handoff(handoff.pk)).data,
            message=f'已确认：{item.get_item_status_display()}'
        )


class HandoffCorrectionView(APIView):
    """主管对已完成清单追加更正记录"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.is_admin:
            return error_response(message='只有主管可以发起更正', code=403)

        handoff = HandoffList.objects.filter(pk=pk).first()
        if handoff is None:
            return error_response(message='交接清单不存在', code=404)

        serializer = HandoffCorrectSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        new_custodian = None
        if serializer.validated_data.get('new_custodian'):
            new_custodian = User.objects.filter(
                pk=serializer.validated_data['new_custodian'], is_active=True
            ).first()
            if new_custodian is None:
                return error_response(message='新责任人不存在或已停用')

        try:
            correction = handoff_services.correct_handoff(
                handoff, operator=request.user,
                reason=serializer.validated_data['reason'],
                new_custodian=new_custodian,
            )
        except HandoffError as exc:
            return error_response(message=str(exc))

        logger.info(
            f"Admin {request.user.username} corrected handoff "
            f"{handoff.handoff_no} to v{correction.version}"
        )
        return success_response(
            data=HandoffListSerializer(_get_handoff(handoff.pk)).data,
            message='更正成功，已生成更正记录'
        )
