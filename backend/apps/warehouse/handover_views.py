"""
交接清单 API

流程接口（移交人/接收人）：
    POST   /api/handovers/                          移交人创建清单（幂等）
    GET    /api/handovers/                          清单查询（主管可查全部）
    GET    /api/handovers/<pk>/                     清单详情（版本、双方、差异、责任归属）
    PUT    /api/handovers/<pk>/items/               移交人修订物资行（确认前）
    POST   /api/handovers/<pk>/freeze/              移交人冻结清单
    POST   /api/handovers/<pk>/items/<item_id>/adjudicate/  接收人逐项确认
    POST   /api/handovers/<pk>/return/              接收人退回修订
    POST   /api/handovers/<pk>/complete/            接收人完成交接（幂等）
    POST   /api/handovers/<pk>/cancel/              移交人作废
    GET    /api/handovers/<pk>/versions/            版本快照
    GET    /api/handovers/<pk>/events/              事件时间线
    GET/POST /api/handovers/<pk>/corrections/       更正记录（已完成清单的唯一处理途径）
    GET    /api/handovers/occupancy-check/          冻结前占用预检
"""
import logging

from django.db import models
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.response import success_response, error_response
from . import handover_services as services
from .handover_services import HandoverOccupiedError, IdempotencyReplay
from .models import HandoverList
from .serializers import (
    HandoverAdjudicateSerializer,
    HandoverCorrectionCreateSerializer,
    HandoverCorrectionSerializer,
    HandoverCreateSerializer,
    HandoverEventSerializer,
    HandoverItemInputSerializer,
    HandoverListSerializer,
    HandoverVersionSerializer,
)

logger = logging.getLogger('apps')


def _envelope(data, message, code=200):
    return {'success': True, 'code': code, 'message': message, 'data': data}


def _first_error(serializer):
    errors = serializer.errors
    value = list(errors.values())[0]
    if isinstance(value, list):
        value = value[0]
    if isinstance(value, dict):
        value = list(value.values())[0]
        if isinstance(value, list):
            value = value[0]
    return str(value)


def _can_view(request, handover):
    return request.user.is_admin or request.user.id in (
        handover.transferor_id, handover.receiver_id
    )


class HandoverListCreateView(APIView):
    """交接清单查询 / 创建"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = HandoverList.objects.select_related(
            'transferor', 'receiver'
        ).prefetch_related('items')

        # 主管可查询全部清单；普通用户仅能看到与自己相关的交接
        if not request.user.is_admin:
            queryset = queryset.filter(
                models.Q(transferor=request.user) | models.Q(receiver=request.user)
            )

        handover_no = request.query_params.get('handover_no')
        if handover_no:
            queryset = queryset.filter(handover_no__icontains=handover_no)
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        transferor = request.query_params.get('transferor')
        if transferor:
            queryset = queryset.filter(transferor_id=transferor)
        receiver = request.query_params.get('receiver')
        if receiver:
            queryset = queryset.filter(receiver_id=receiver)

        queryset = queryset.order_by('-created_at')
        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = min(int(request.query_params.get('page_size', 10)), 100)
        except (TypeError, ValueError):
            page, page_size = 1, 10
        total = queryset.count()
        handovers = queryset[(page - 1) * page_size:page * page_size]

        return success_response(data={
            'list': HandoverListSerializer(handovers, many=True).data,
            'total': total,
            'page': page,
            'page_size': page_size,
        })

    def post(self, request):
        serializer = HandoverCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data

        def runner(_record):
            handover = services.create_handover(
                transferor=request.user,
                receiver_id=data['receiver'],
                transfer_note=data.get('transfer_note', ''),
                items_data=data['items'],
            )
            body = _envelope(
                dict(HandoverListSerializer(handover).data),
                '交接清单创建成功，待移交人冻结', code=201,
            )
            return handover, body, 201

        try:
            _, body, status_code = services.run_idempotent(
                request, 'handover:create', runner
            )
        except IdempotencyReplay as replay:
            logger.info(
                "Idempotent replay handover create key=%s user=%s",
                request.META.get('HTTP_IDEMPOTENCY_KEY'), request.user.username,
            )
            return Response(replay.body, status=replay.status_code)
        except HandoverOccupiedError as exc:
            return error_response(exc.message, code=409, data={'occupied': exc.occupied})

        logger.info("User %s created handover", request.user.username)
        return Response(body, status=status_code)


class HandoverDetailView(APIView):
    """交接清单详情：版本、双方身份、差异原因、最终责任归属一站式查询。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        handover = services.get_handover_or_404(pk)
        if not _can_view(request, handover):
            return error_response(message='无权查看该交接清单', code=403)

        data = HandoverListSerializer(handover).data
        data['versions'] = HandoverVersionSerializer(
            handover.versions.all().select_related('frozen_by'), many=True
        ).data
        data['events'] = HandoverEventSerializer(
            handover.events.all().select_related('actor'), many=True
        ).data
        data['corrections'] = HandoverCorrectionSerializer(
            handover.corrections.all().select_related('goods', 'created_by'), many=True
        ).data
        return success_response(data=data)


class HandoverItemsReviseView(APIView):
    """移交人在确认前（编制中/被退回）整单修订物资行。"""
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        handover = services.get_handover_or_404(pk)
        items_serializer = HandoverItemInputSerializer(
            data=request.data.get('items', []), many=True
        )
        if not items_serializer.is_valid():
            return error_response(message=_first_error(items_serializer))
        if not items_serializer.validated_data:
            return error_response(message='交接清单至少包含一项物资')

        note = request.data.get('transfer_note')
        handover = services.replace_working_items(
            handover, request.user, items_serializer.validated_data
        )
        if note is not None:
            handover.transfer_note = note or ''
            handover.save(update_fields=['transfer_note', 'updated_at'])
        return success_response(
            data=HandoverListSerializer(handover).data, message='清单已修订，待重新冻结'
        )


class HandoverFreezeView(APIView):
    """移交人冻结清单。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        handover = services.get_handover_or_404(pk)
        try:
            handover = services.freeze_handover(handover, request.user)
        except HandoverOccupiedError as exc:
            return error_response(exc.message, code=409, data={'occupied': exc.occupied})
        return success_response(
            data=HandoverListSerializer(handover).data,
            message=f'清单已冻结（v{handover.version}），等待接收人逐项确认',
        )


class HandoverItemAdjudicateView(APIView):
    """接收人逐项确认数量、封签号与存放位置。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, item_id):
        handover = services.get_handover_or_404(pk)
        serializer = HandoverAdjudicateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        item = services.adjudicate_item(
            handover, item_id, request.user,
            result=data['result'],
            actual={
                'actual_quantity': data.get('actual_quantity'),
                'actual_seal_no': data.get('actual_seal_no', ''),
                'actual_location': data.get('actual_location', ''),
                'difference_reason': data.get('difference_reason', ''),
            },
        )
        pending = handover.pending_item_ids().count()
        return success_response(data={
            'item_id': item.id,
            'confirm_result': item.confirm_result,
            'pending_count': pending,
        }, message='逐项确认已记录')


class HandoverReturnView(APIView):
    """接收人确认前退回清单，要求移交人修订。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        handover = services.get_handover_or_404(pk)
        reason = request.data.get('reason', '')
        handover = services.return_handover(handover, request.user, reason)
        return success_response(
            data=HandoverListSerializer(handover).data,
            message='清单已退回移交人修订',
        )


class HandoverCompleteView(APIView):
    """接收人完成交接，责任转移（幂等：重复提交不会产生第二次交接）。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        handover = services.get_handover_or_404(pk)

        def runner(_record):
            completed = services.complete_handover(handover, request.user)
            body = _envelope(
                dict(HandoverListSerializer(completed).data),
                '交接完成，保管责任已转移至接收人',
            )
            return completed, body, 200

        try:
            _, body, status_code = services.run_idempotent(
                request, 'handover:complete', runner
            )
        except IdempotencyReplay as replay:
            logger.info(
                "Idempotent replay handover complete pk=%s key=%s",
                pk, request.META.get('HTTP_IDEMPOTENCY_KEY'),
            )
            return Response(replay.body, status=replay.status_code)
        except HandoverOccupiedError as exc:
            return error_response(exc.message, code=409, data={'occupied': exc.occupied})

        return Response(body, status=status_code)


class HandoverCancelView(APIView):
    """移交人在完成前作废清单。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        handover = services.get_handover_or_404(pk)
        reason = request.data.get('reason', '')
        handover = services.cancel_handover(handover, request.user, reason)
        return success_response(
            data=HandoverListSerializer(handover).data, message='清单已作废'
        )


class HandoverVersionListView(APIView):
    """每次交接的冻结版本列表。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        handover = services.get_handover_or_404(pk)
        if not _can_view(request, handover):
            return error_response(message='无权查看该交接清单', code=403)
        versions = handover.versions.all().select_related('frozen_by')
        return success_response(data=HandoverVersionSerializer(versions, many=True).data)


class HandoverEventListView(APIView):
    """交接事件时间线（含状态流转、差异原因、操作人）。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        handover = services.get_handover_or_404(pk)
        if not _can_view(request, handover):
            return error_response(message='无权查看该交接清单', code=403)
        events = handover.events.all().select_related('actor')
        return success_response(data=HandoverEventSerializer(events, many=True).data)


class HandoverCorrectionListCreateView(APIView):
    """已完成清单的更正记录：只追加、不改动原单。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        handover = services.get_handover_or_404(pk)
        if not _can_view(request, handover):
            return error_response(message='无权查看该交接清单', code=403)
        corrections = handover.corrections.all().select_related('goods', 'created_by')
        return success_response(
            data=HandoverCorrectionSerializer(corrections, many=True).data
        )

    def post(self, request, pk):
        handover = services.get_handover_or_404(pk)
        serializer = HandoverCorrectionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        correction = services.create_correction(
            handover, request.user, serializer.validated_data
        )
        logger.info(
            "User %s added correction #%s to handover %s",
            request.user.username, correction.id, handover.handover_no,
        )
        return success_response(
            data=HandoverCorrectionSerializer(correction).data,
            message='更正记录已追加，原交接清单保持不变',
        )


class HandoverOccupancyCheckView(APIView):
    """冻结/完成前预检物资是否被其他业务占用。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        raw = request.query_params.get('goods', '')
        try:
            goods_ids = [int(v) for v in raw.split(',') if v.strip()]
        except ValueError:
            return error_response(message='goods 参数应为逗号分隔的物资ID')
        if not goods_ids:
            return error_response(message='请提供待检查的物资ID')

        occupied = services.find_occupied_goods(goods_ids)
        return success_response(data={
            'available': not occupied,
            'occupied': [{'goods': gid, 'reason': reason} for gid, reason in occupied.items()],
        })
