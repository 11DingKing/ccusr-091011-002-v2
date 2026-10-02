"""
仓库管理序列化器
"""
from rest_framework import serializers
from apps.authentication.models import User
from .models import (
    Unit, Category, Variety, Goods, StockIn, StockOut, Warning, Approval,
    HandoffList, HandoffItem, HandoffEvent, HandoffCorrection,
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


# ==================== 物资交接清单 ====================

class HandoffItemInputSerializer(serializers.Serializer):
    """交接明细输入"""
    goods = serializers.IntegerField(required=True, error_messages={'required': '请选择物资'})
    expected_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=True,
        error_messages={'required': '请填写移交数量', 'invalid': '移交数量格式不正确'}
    )
    seal_no = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    location = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')


class HandoffCreateSerializer(serializers.Serializer):
    """创建/修订交接清单输入"""
    receiver = serializers.IntegerField(required=True, error_messages={'required': '请选择接收人'})
    items = HandoffItemInputSerializer(many=True, required=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')
    idempotency_key = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=''
    )

    def validate_receiver(self, value):
        if not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('接收人不存在或已停用')
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('交接清单至少包含一项物资')
        return value


class HandoffItemSerializer(serializers.ModelSerializer):
    """交接明细输出"""
    status_display = serializers.CharField(source='get_item_status_display', read_only=True)

    class Meta:
        model = HandoffItem
        fields = [
            'id', 'goods', 'goods_name', 'goods_code',
            'expected_quantity', 'seal_no', 'location',
            'actual_quantity', 'actual_seal_no', 'actual_location',
            'item_status', 'status_display', 'difference_reason', 'confirmed_at'
        ]


class HandoffEventSerializer(serializers.ModelSerializer):
    """交接事件输出"""
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)

    class Meta:
        model = HandoffEvent
        fields = [
            'id', 'version', 'action', 'action_display',
            'operator', 'operator_name', 'detail', 'created_at'
        ]


class HandoffCorrectionSerializer(serializers.ModelSerializer):
    """更正记录输出"""
    old_custodian_name = serializers.CharField(source='old_custodian.username', read_only=True)
    new_custodian_name = serializers.CharField(source='new_custodian.username', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)

    class Meta:
        model = HandoffCorrection
        fields = [
            'id', 'version', 'goods', 'reason',
            'old_custodian', 'old_custodian_name',
            'new_custodian', 'new_custodian_name',
            'operator', 'operator_name', 'created_at'
        ]


class HandoffListSerializer(serializers.ModelSerializer):
    """交接清单输出（含双方身份、版本、差异、责任归属）"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    transferor_name = serializers.CharField(source='transferor.username', read_only=True)
    receiver_name = serializers.CharField(source='receiver.username', read_only=True)
    custodian_name = serializers.CharField(source='custodian.username', read_only=True)
    items = HandoffItemSerializer(many=True, read_only=True)
    events = HandoffEventSerializer(many=True, read_only=True)
    corrections = HandoffCorrectionSerializer(many=True, read_only=True)
    total_count = serializers.SerializerMethodField()
    confirmed_count = serializers.SerializerMethodField()
    disputed_count = serializers.SerializerMethodField()

    class Meta:
        model = HandoffList
        fields = [
            'id', 'handoff_no', 'version', 'status', 'status_display',
            'transferor', 'transferor_name',
            'receiver', 'receiver_name',
            'custodian', 'custodian_name',
            'frozen_at', 'completed_at', 'remark',
            'created_at', 'updated_at',
            'items', 'events', 'corrections',
            'total_count', 'confirmed_count', 'disputed_count',
        ]

    def get_total_count(self, obj):
        return len(obj.items.all())

    def get_confirmed_count(self, obj):
        return sum(1 for i in obj.items.all()
                   if i.item_status == 'confirmed')

    def get_disputed_count(self, obj):
        return sum(1 for i in obj.items.all()
                   if i.item_status == 'disputed')


class HandoffUpdateSerializer(serializers.Serializer):
    """修订交接清单输入"""
    receiver = serializers.IntegerField(required=False)
    items = HandoffItemInputSerializer(many=True, required=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_receiver(self, value):
        if not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('接收人不存在或已停用')
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('交接清单至少包含一项物资')
        return value


class HandoffConfirmItemSerializer(serializers.Serializer):
    """逐项确认输入：数量、封签、存放位置三项核对结果"""
    actual_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=True,
        error_messages={'required': '请填写实收数量', 'invalid': '实收数量格式不正确'}
    )
    actual_seal_no = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=''
    )
    actual_location = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=''
    )
    difference_reason = serializers.CharField(
        required=False, allow_blank=True, default=''
    )


class HandoffReturnSerializer(serializers.Serializer):
    """退回修订输入"""
    reason = serializers.CharField(required=True, error_messages={'required': '请填写退回原因'})

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('退回原因不能为空')
        return value


class HandoffCorrectSerializer(serializers.Serializer):
    """已完成清单的更正输入"""
    reason = serializers.CharField(required=True, error_messages={'required': '请填写更正原因'})
    new_custodian = serializers.IntegerField(required=False, allow_null=True)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('更正原因不能为空')
        return value

    def validate_new_custodian(self, value):
        if value and not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('新责任人不存在或已停用')
        return value
