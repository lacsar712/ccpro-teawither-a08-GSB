from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class Garden(models.Model):
    name = models.CharField("茶园名称", max_length=120)
    altitudeBand = models.CharField("海拔带", max_length=60)
    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "茶园"
        verbose_name_plural = "茶园"

    def __str__(self):
        return self.name


class Trough(models.Model):
    STATUS_LOADING = "loading"
    STATUS_WITHERING = "withering"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_LOADING, "装叶中"),
        (STATUS_WITHERING, "萎凋中"),
        (STATUS_READY, "可下槽"),
    ]

    garden = models.ForeignKey(
        Garden,
        on_delete=models.CASCADE,
        related_name="troughs",
        verbose_name="茶园",
    )
    troughCode = models.CharField("槽位编号", max_length=40)
    cultivar = models.CharField("茶树品种", max_length=80)
    loadKg = models.DecimalField("装叶量(kg)", max_digits=10, decimal_places=2)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_LOADING,
    )

    class Meta:
        ordering = ["garden__name", "troughCode"]
        verbose_name = "萎凋槽"
        verbose_name_plural = "萎凋槽"
        constraints = [
            models.UniqueConstraint(
                fields=["garden", "troughCode"],
                name="uniq_trough_code_per_garden",
            ),
        ]

    def __str__(self):
        return f"{self.garden.name}-{self.troughCode}"

    def latest_batch(self):
        return self.batches.order_by("-startedAt", "-id").first()

    def has_open_handover(self):
        return self.handovers.filter(completedAt__isnull=True).exists()

    def clean(self):
        super().clean()
        if self.pk and self.status == self.STATUS_LOADING:
            # 改回「装叶中」前置：该槽不得存在未完成的下槽交接卷
            if UnloadHandover.objects.filter(
                trough_id=self.pk, completedAt__isnull=True
            ).exists():
                raise ValidationError(
                    {
                        "status": "该槽存在未完成的下槽交接卷，须完成交接后方可改回「装叶中」清空下一轮。"
                    }
                )
        if self.status != self.STATUS_READY:
            return
        if not latest_batch_moisture_ok(self):
            raise ValidationError(
                {
                    "status": "无法设为可下槽：最新萎凋批次的实测含水率为空或高于 40%。"
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WitherBatch(models.Model):
    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name="萎凋槽",
    )
    startedAt = models.DateTimeField("开始时间")
    targetMoisture = models.DecimalField(
        "目标含水率(%)", max_digits=5, decimal_places=2
    )
    actualMoisture = models.DecimalField(
        "实测含水率(%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    rollGrade = models.CharField("揉捻等级", max_length=40)

    class Meta:
        ordering = ["-startedAt", "-id"]
        verbose_name = "萎凋批次"
        verbose_name_plural = "萎凋批次"

    def __str__(self):
        return f"{self.trough} @ {self.startedAt:%Y-%m-%d %H:%M}"


def latest_batch_moisture_ok(trough):
    """设为可下槽 / 完成交接共用判定：最新批次实测含水不空且不高于 40。"""
    latest = (
        WitherBatch.objects.filter(trough_id=trough.pk)
        .order_by("-startedAt", "-id")
        .first()
    )
    return (
        latest is not None
        and latest.actualMoisture is not None
        and latest.actualMoisture <= 40
    )


class UnloadHandover(models.Model):
    """下槽交接卷：挂在可下槽槽位上，完成交接后槽位才允许改回装叶中。"""

    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="handovers",
        verbose_name="所属槽位",
    )
    handedAt = models.DateTimeField("交接时刻", default=timezone.now)
    receiverTeam = models.CharField("接收班组", max_length=80)
    outputKg = models.DecimalField("出叶千克", max_digits=10, decimal_places=2)
    signer = models.CharField("签字人", max_length=80)
    completedAt = models.DateTimeField("完成时刻", null=True, blank=True)

    class Meta:
        ordering = ["-handedAt", "-id"]
        verbose_name = "下槽交接卷"
        verbose_name_plural = "下槽交接卷"
        constraints = [
            models.UniqueConstraint(
                fields=["trough"],
                condition=Q(completedAt__isnull=True),
                name="uniq_open_handover_per_trough",
            ),
        ]

    def __str__(self):
        state = "交接中" if self.completedAt is None else "已完成"
        return f"{self.trough} 下槽交接卷({state})"

    @property
    def is_open(self):
        return self.completedAt is None

    def clean(self):
        super().clean()
        errors = {}
        if self.trough_id and self.trough.status != Trough.STATUS_READY:
            errors["trough"] = "开卷时槽位必须为「可下槽」。"
        if self.outputKg is not None:
            if self.outputKg <= 0:
                errors["outputKg"] = "出叶千克须为正数。"
            elif self.trough_id and self.outputKg > self.trough.loadKg:
                errors["outputKg"] = "出叶千克不得超过该槽装叶量。"
        if self.trough_id and UnloadHandover.objects.filter(
            trough_id=self.trough_id, completedAt__isnull=True
        ).exclude(pk=self.pk).exists():
            errors["trough"] = "该槽已有未完成的交接卷，不可再开。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def complete(self):
        """主管完成交接：同事务内复用含水判定后写入完成时刻。"""
        with transaction.atomic():
            locked = UnloadHandover.objects.select_for_update().get(pk=self.pk)
            if locked.completedAt is not None:
                raise ValidationError("该交接卷已完成，不可重复完成。")
            trough = Trough.objects.select_for_update().get(pk=locked.trough_id)
            if not latest_batch_moisture_ok(trough):
                raise ValidationError(
                    "无法完成交接：该槽最新批次实测含水率为空或高于 40%。"
                )
            locked.completedAt = timezone.now()
            locked.save()
        self.completedAt = locked.completedAt
        return locked
