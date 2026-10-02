"""
库房管理模型
"""
from django.db import models
from apps.authentication.models import User


# 交接单状态
HANDOVER_STATUS_DRAFT = 'draft'              # 编制中（含被退回后的修订）
HANDOVER_STATUS_SUBMITTED = 'submitted'      # 已冻结，待接收人确认
HANDOVER_STATUS_COMPLETED = 'completed'      # 已完成，责任已转移
HANDOVER_STATUS_CANCELLED = 'cancelled'      # 已作废
HANDOVER_STATUS_RETURNED = 'returned'        # 接收人退回，待移交人修订

HANDOVER_EDITABLE_STATUSES = (HANDOVER_STATUS_DRAFT, HANDOVER_STATUS_RETURNED)
HANDOVER_ACTIVE_STATUSES = (
    HANDOVER_STATUS_DRAFT,
    HANDOVER_STATUS_SUBMITTED,
    HANDOVER_STATUS_RETURNED,
)

# 逐项确认结果
ITEM_CONFIRM_PENDING = 'pending'
ITEM_CONFIRM_MATCHED = 'matched'       # 数量、封签、位置一致
ITEM_CONFIRM_DIFFERENT = 'different'   # 存在差异，责任不转移

# 责任归属
RESPONSIBILITY_TRANSFEROR = 'transferor'
RESPONSIBILITY_RECEIVER = 'receiver'


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)
    
    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='操作人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    
    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"


class HandoverList(models.Model):
    """交接清单：一次班次/岗位之间的物资交接主单。

    责任转移以接收人逐项确认并最终确认为准；移交人冻结清单后内容
    不可改动，接收人确认前可退回修订。完成后只允许通过更正记录处理。
    """
    STATUS_CHOICES = [
        (HANDOVER_STATUS_DRAFT, '编制中'),
        (HANDOVER_STATUS_SUBMITTED, '待确认'),
        (HANDOVER_STATUS_COMPLETED, '已完成'),
        (HANDOVER_STATUS_RETURNED, '已退回'),
        (HANDOVER_STATUS_CANCELLED, '已作废'),
    ]
    RESPONSIBILITY_CHOICES = [
        (RESPONSIBILITY_TRANSFEROR, '移交人'),
        (RESPONSIBILITY_RECEIVER, '接收人'),
    ]

    handover_no = models.CharField('交接单号', max_length=40, unique=True)
    transferor = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='handovers_given', verbose_name='移交人'
    )
    receiver = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='handovers_received', verbose_name='接收人'
    )
    status = models.CharField(
        '状态', max_length=20, choices=STATUS_CHOICES, default=HANDOVER_STATUS_DRAFT
    )
    version = models.PositiveIntegerField('当前冻结版本', default=0)
    # 完成后固化的责任归属；未完成时责任仍在移交人
    responsible_party = models.CharField(
        '最终责任归属', max_length=20, choices=RESPONSIBILITY_CHOICES,
        default=RESPONSIBILITY_TRANSFEROR
    )
    # 接收人确认时若存在差异的总体原因
    difference_reason = models.TextField('差异原因', blank=True)
    transfer_note = models.TextField('移交备注', blank=True)
    frozen_at = models.DateTimeField('最近冻结时间', null=True, blank=True)
    submitted_at = models.DateTimeField('首次冻结时间', null=True, blank=True)
    completed_at = models.DateTimeField('完成时间', null=True, blank=True)
    returned_at = models.DateTimeField('退回时间', null=True, blank=True)
    cancelled_at = models.DateTimeField('作废时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_handover_list'
        verbose_name = '交接清单'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.handover_no} - {self.transferor_id}→{self.receiver_id}"

    @property
    def is_frozen(self):
        """是否处于冻结待确认状态"""
        return self.status == HANDOVER_STATUS_SUBMITTED

    @property
    def is_completed(self):
        return self.status == HANDOVER_STATUS_COMPLETED

    @property
    def is_editable(self):
        """清单项是否允许移交人增删修订"""
        return self.status in HANDOVER_EDITABLE_STATUSES

    def pending_item_ids(self):
        """当前冻结版本下尚未逐项确认的物资行"""
        return self.items.filter(
            version=self.version,
            confirm_result=ITEM_CONFIRM_PENDING
        ).values_list('id', flat=True)


class HandoverItem(models.Model):
    """交接清单物资行：记录冻结时的数量、封签号、存放位置以及接收人的逐项确认。"""
    CONFIRM_CHOICES = [
        (ITEM_CONFIRM_PENDING, '待确认'),
        (ITEM_CONFIRM_MATCHED, '一致'),
        (ITEM_CONFIRM_DIFFERENT, '有差异'),
    ]

    handover = models.ForeignKey(
        HandoverList, on_delete=models.CASCADE,
        related_name='items', verbose_name='交接清单'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='handover_items', verbose_name='物资'
    )
    # 该行最近一次冻结所属版本；编制中的草稿行版本为 0
    version = models.PositiveIntegerField('冻结版本', default=0)
    expected_quantity = models.DecimalField('移交数量', max_digits=12, decimal_places=2)
    expected_seal_no = models.CharField('封签号', max_length=100, blank=True)
    expected_location = models.CharField('存放位置', max_length=100, blank=True)
    actual_quantity = models.DecimalField(
        '确认数量', max_digits=12, decimal_places=2, null=True, blank=True
    )
    actual_seal_no = models.CharField('确认封签号', max_length=100, blank=True)
    actual_location = models.CharField('确认存放位置', max_length=100, blank=True)
    confirm_result = models.CharField(
        '逐项确认结果', max_length=20, choices=CONFIRM_CHOICES, default=ITEM_CONFIRM_PENDING
    )
    difference_reason = models.CharField('差异说明', max_length=300, blank=True)
    confirmed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='confirmed_handover_items', verbose_name='逐项确认人'
    )
    confirmed_at = models.DateTimeField('逐项确认时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_handover_item'
        verbose_name = '交接清单物资行'
        verbose_name_plural = verbose_name
        ordering = ['id']
        unique_together = [('handover', 'goods', 'version')]

    def __str__(self):
        return f"{self.handover_id}:{self.goods_id}@v{self.version}"


class HandoverVersion(models.Model):
    """交接清单冻结版本快照（不可变审计记录）。"""
    handover = models.ForeignKey(
        HandoverList, on_delete=models.CASCADE,
        related_name='versions', verbose_name='交接清单'
    )
    version = models.PositiveIntegerField('版本号')
    frozen_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='frozen_handover_versions', verbose_name='冻结人'
    )
    frozen_at = models.DateTimeField('冻结时间', auto_now_add=True)
    item_count = models.PositiveIntegerField('物资行数')
    item_snapshot = models.JSONField('清单快照', default=list)
    remark = models.TextField('冻结说明', blank=True)

    class Meta:
        db_table = 'wh_handover_version'
        verbose_name = '交接版本'
        verbose_name_plural = verbose_name
        ordering = ['version']
        unique_together = [('handover', 'version')]

    def __str__(self):
        return f"{self.handover.handover_no} v{self.version}"


class HandoverEvent(models.Model):
    """交接流程事件时间线，用于审计每次状态流转与差异原因。"""
    ACTION_CHOICES = [
        ('create', '创建清单'),
        ('freeze', '冻结清单'),
        ('item_confirm', '逐项确认'),
        ('return', '退回修订'),
        ('complete', '完成交接'),
        ('cancel', '作废清单'),
    ]

    handover = models.ForeignKey(
        HandoverList, on_delete=models.CASCADE,
        related_name='events', verbose_name='交接清单'
    )
    action = models.CharField('事件类型', max_length=20, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='handover_events', verbose_name='操作人'
    )
    from_status = models.CharField('原状态', max_length=20, blank=True)
    to_status = models.CharField('新状态', max_length=20, blank=True)
    version = models.PositiveIntegerField('事件版本', default=0)
    detail = models.TextField('事件详情', blank=True)
    created_at = models.DateTimeField('发生时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_handover_event'
        verbose_name = '交接事件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.handover_id}:{self.action}"


class HandoverCorrection(models.Model):
    """交接更正记录：已完成清单的唯一处理途径，只追加、不改动原单。"""
    handover = models.ForeignKey(
        HandoverList, on_delete=models.PROTECT,
        related_name='corrections', verbose_name='交接清单'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='handover_corrections', verbose_name='相关物资'
    )
    reason = models.TextField('更正原因')
    quantity_change = models.DecimalField(
        '数量更正(正补负冲)', max_digits=12, decimal_places=2, default=0
    )
    correct_seal_no = models.CharField('更正后封签号', max_length=100, blank=True)
    correct_location = models.CharField('更正后存放位置', max_length=100, blank=True)
    detail = models.TextField('更正说明', blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_handover_corrections', verbose_name='更正人'
    )
    created_at = models.DateTimeField('更正时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_handover_correction'
        verbose_name = '交接更正记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.handover.handover_no} 更正 #{self.id}"


class IdempotencyRecord(models.Model):
    """客户端幂等键：保证服务重试或重复提交不会产生第二次交接。"""
    idempotency_key = models.CharField('幂等键', max_length=128, unique=True)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='idempotency_records', verbose_name='提交用户'
    )
    scope = models.CharField('业务域', max_length=50)
    request_path = models.CharField('请求路径', max_length=200, blank=True)
    request_hash = models.CharField('请求指纹', max_length=64, blank=True)
    response_body = models.JSONField('响应内容', null=True, blank=True)
    status_code = models.PositiveIntegerField('响应状态码', default=0)
    handover = models.ForeignKey(
        HandoverList, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='idempotency_records', verbose_name='关联交接单'
    )
    created_at = models.DateTimeField('首次提交时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_idempotency_record'
        verbose_name = '幂等记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.scope}:{self.idempotency_key}"
