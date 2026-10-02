"""
仓库管理序列化器
"""
from rest_framework import serializers
from .models import (
    Unit, Category, Variety, Goods, StockIn, StockOut, Warning, Approval,
    HandoverList, HandoverItem, HandoverVersion, HandoverEvent, HandoverCorrection,
    ITEM_CONFIRM_MATCHED, ITEM_CONFIRM_DIFFERENT,
)


class UnitSerializer(serializers.ModelSerializer):
    """单位序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UnitCreateSerializer(serializers.Serializer):
    """单位创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=5, required=True, error_messages={
        'required': '请输入单位名称',
        'blank': '单位名称不能为空',
        'min_length': '单位名称至少1个字',
        'max_length': '单位名称最多5个字',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Unit.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('单位名称已存在')
        else:
            if Unit.objects.filter(name=value).exists():
                raise serializers.ValidationError('单位名称已存在')
        return value


class CategorySerializer(serializers.ModelSerializer):
    """品类序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'unit', 'unit_name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CategoryCreateSerializer(serializers.Serializer):
    """品类创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=10, required=True, error_messages={
        'required': '请输入品类名称',
        'blank': '品类名称不能为空',
        'min_length': '品类名称至少1个字',
        'max_length': '品类名称最多10个字',
    })
    unit = serializers.IntegerField(required=True, error_messages={
        'required': '请选择单位',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Category.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('品类名称已存在')
        else:
            if Category.objects.filter(name=value).exists():
                raise serializers.ValidationError('品类名称已存在')
        return value
    
    def validate_unit(self, value):
        if not Unit.objects.filter(pk=value).exists():
            raise serializers.ValidationError('单位不存在')
        return value


class VarietySerializer(serializers.ModelSerializer):
    """品种序列化器"""
    is_in_stock = serializers.BooleanField(read_only=True)
    unit_name = serializers.CharField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Variety
        fields = [
            'id', 'name', 'category', 'category_name', 'unit_name',
            'is_in_stock', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class VarietyCreateSerializer(serializers.Serializer):
    """品种创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=20, required=True, error_messages={
        'required': '请输入品种名称',
        'blank': '品种名称不能为空',
        'min_length': '品种名称至少1个字',
        'max_length': '品种名称最多20个字',
    })
    category = serializers.IntegerField(required=True, error_messages={
        'required': '请选择品类',
    })
    
    def validate_category(self, value):
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value
    
    def validate(self, data):
        instance = self.context.get('instance')
        name = data['name']
        category_id = data['category']
        
        if instance:
            if Variety.objects.filter(name=name, category_id=category_id).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        else:
            if Variety.objects.filter(name=name, category_id=category_id).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        return data


class GoodsSerializer(serializers.ModelSerializer):
    """货物序列化器"""
    variety_name = serializers.CharField(source='variety.name', read_only=True)
    category_name = serializers.CharField(source='variety.category.name', read_only=True)
    unit_name = serializers.CharField(source='variety.category.unit.name', read_only=True)
    is_warning = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Goods
        fields = [
            'id', 'name', 'code', 'variety', 'variety_name',
            'category_name', 'unit_name', 'specification',
            'quantity', 'warning_threshold', 'location',
            'remark', 'is_active', 'is_warning',
            'created_at', 'updated_at'
        ]


class StockInSerializer(serializers.ModelSerializer):
    """入库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    
    class Meta:
        model = StockIn
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'quantity', 'batch_no', 'supplier', 'stock_in_time', 'remark'
        ]


class StockOutSerializer(serializers.ModelSerializer):
    """出库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'receiver', 'receiver_dept', 'quantity', 'status', 'status_display',
            'stock_out_time', 'remark', 'created_at'
        ]


class WarningSerializer(serializers.ModelSerializer):
    """预警记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    
    class Meta:
        model = Warning
        fields = [
            'id', 'goods', 'goods_name', 'type', 'type_display',
            'message', 'is_read', 'created_at'
        ]


class ApprovalSerializer(serializers.ModelSerializer):
    """审批记录序列化器"""
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Approval
        fields = [
            'id', 'stock_out', 'approver', 'approver_name',
            'status', 'status_display', 'remark', 'created_at', 'updated_at'
        ]


# ==================== 交接清单 ====================

class HandoverItemInputSerializer(serializers.Serializer):
    """交接物资行输入"""
    goods = serializers.IntegerField(min_value=1)
    expected_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0
    )
    expected_seal_no = serializers.CharField(max_length=100, allow_blank=True, required=False)
    expected_location = serializers.CharField(max_length=100, allow_blank=True, required=False)

    def validate_expected_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError('移交数量必须大于0')
        return value


class HandoverCreateSerializer(serializers.Serializer):
    """创建/修订交接清单输入"""
    receiver = serializers.IntegerField(min_value=1)
    transfer_note = serializers.CharField(
        max_length=1000, allow_blank=True, required=False, default=''
    )
    items = HandoverItemInputSerializer(many=True)
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('交接清单至少包含一项物资')
        goods_ids = [row['goods'] for row in value]
        if len(set(goods_ids)) != len(goods_ids):
            raise serializers.ValidationError('同一物资在清单中只能出现一次')
        return value


class HandoverListSerializer(serializers.ModelSerializer):
    """交接清单主单输出（含双方身份、版本、差异原因、责任归属）"""
    transferor_name = serializers.CharField(source='transferor.username', read_only=True)
    transferor_real_name = serializers.CharField(source='transferor.real_name', read_only=True)
    receiver_name = serializers.CharField(source='receiver.username', read_only=True)
    receiver_real_name = serializers.CharField(source='receiver.real_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    responsible_party_display = serializers.CharField(
        source='get_responsible_party_display', read_only=True
    )
    items = serializers.SerializerMethodField()
    pending_count = serializers.SerializerMethodField()

    class Meta:
        model = HandoverList
        fields = [
            'id', 'handover_no',
            'transferor', 'transferor_name', 'transferor_real_name',
            'receiver', 'receiver_name', 'receiver_real_name',
            'status', 'status_display', 'version',
            'responsible_party', 'responsible_party_display',
            'difference_reason', 'transfer_note',
            'frozen_at', 'submitted_at', 'completed_at', 'returned_at',
            'cancelled_at', 'created_at', 'updated_at',
            'items', 'pending_count',
        ]

    def _current_version(self, obj):
        # 冻结后展示当前版本行；编制中/退回展示版本 0 的工作副本
        return obj.version if obj.version else 0

    def get_items(self, obj):
        version = self._current_version(obj)
        items = [item for item in obj.items.all() if item.version == version]
        items.sort(key=lambda item: item.id)
        return HandoverItemSerializer(items, many=True).data

    def get_pending_count(self, obj):
        if obj.status != 'submitted':
            return 0
        return sum(
            1 for item in obj.items.all()
            if item.version == obj.version and item.confirm_result == 'pending'
        )


class HandoverItemSerializer(serializers.ModelSerializer):
    """交接物资行输出"""
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    unit_name = serializers.CharField(
        source='goods.variety.category.unit.name', read_only=True
    )
    confirm_result_display = serializers.CharField(
        source='get_confirm_result_display', read_only=True
    )
    confirmed_by_name = serializers.CharField(source='confirmed_by.username', read_only=True)

    class Meta:
        model = HandoverItem
        fields = [
            'id', 'goods', 'goods_code', 'goods_name', 'unit_name', 'version',
            'expected_quantity', 'expected_seal_no', 'expected_location',
            'actual_quantity', 'actual_seal_no', 'actual_location',
            'confirm_result', 'confirm_result_display', 'difference_reason',
            'confirmed_by', 'confirmed_by_name', 'confirmed_at',
        ]


class HandoverVersionSerializer(serializers.ModelSerializer):
    """冻结版本快照输出"""
    frozen_by_name = serializers.CharField(source='frozen_by.username', read_only=True)

    class Meta:
        model = HandoverVersion
        fields = [
            'id', 'version', 'frozen_by', 'frozen_by_name',
            'frozen_at', 'item_count', 'item_snapshot',
        ]


class HandoverEventSerializer(serializers.ModelSerializer):
    """交接事件时间线输出"""
    actor_name = serializers.CharField(source='actor.username', read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = HandoverEvent
        fields = [
            'id', 'action', 'action_display', 'actor', 'actor_name',
            'from_status', 'to_status', 'version', 'detail', 'created_at',
        ]


class HandoverCorrectionSerializer(serializers.ModelSerializer):
    """交接更正记录输出"""
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = HandoverCorrection
        fields = [
            'id', 'handover', 'goods', 'goods_code', 'goods_name',
            'quantity_change', 'correct_seal_no', 'correct_location',
            'reason', 'detail', 'created_by', 'created_by_name', 'created_at',
        ]


class HandoverCorrectionCreateSerializer(serializers.Serializer):
    """更正记录输入"""
    goods = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(min_length=1, max_length=500)
    quantity_change = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=None
    )
    correct_seal_no = serializers.CharField(max_length=100, allow_blank=True, required=False)
    correct_location = serializers.CharField(max_length=100, allow_blank=True, required=False)
    detail = serializers.CharField(max_length=1000, allow_blank=True, required=False)


class HandoverAdjudicateSerializer(serializers.Serializer):
    """逐项确认输入"""
    result = serializers.ChoiceField(choices=[ITEM_CONFIRM_MATCHED, ITEM_CONFIRM_DIFFERENT])
    actual_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    actual_seal_no = serializers.CharField(
        max_length=100, allow_blank=True, required=False, default=''
    )
    actual_location = serializers.CharField(
        max_length=100, allow_blank=True, required=False, default=''
    )
    difference_reason = serializers.CharField(
        max_length=300, required=False, allow_blank=True, default=''
    )
