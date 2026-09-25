from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction


def latest_batch_ready_for_unload(trough):
    """最新萎凋批次满足下槽条件：实测含水不空且不高于 40% 时返回该批次。

    「设为可下槽」与「完成下槽交接」共用此判定。
    """
    if trough is None or getattr(trough, "pk", None) is None:
        return None
    latest = (
        WitherBatch.objects.filter(trough=trough)
        .order_by("-startedAt", "-id")
        .first()
    )
    if latest is None or latest.actualMoisture is None:
        return None
    if latest.actualMoisture > Decimal("40"):
        return None
    return latest


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

    def open_handoff(self):
        """该槽当前未完成的下槽交接卷（无则 None）。"""
        return self.handoffs.filter(completedAt__isnull=True).first()

    def clean(self):
        super().clean()
        old_status = None
        if self.pk:
            old_status = (
                Trough.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        if self.status == self.STATUS_READY and old_status != self.STATUS_READY:
            # 设为可下槽（含新建即设为可下槽）：最新批次实测含水须已填且 ≤ 40%。
            if latest_batch_ready_for_unload(self) is None:
                latest = self.latest_batch() if self.pk else None
                if latest is None or latest.actualMoisture is None:
                    msg = "无法设为可下槽：最新萎凋批次尚未填写实测含水率。"
                else:
                    msg = "无法设为可下槽：最新萎凋批次实测含水率高于 40%。"
                raise ValidationError({"status": msg})

        if (
            self.pk
            and self.status == self.STATUS_LOADING
            and old_status != self.STATUS_LOADING
        ):
            # 改回装叶中（清空下一轮）前，必须先完成下槽交接。
            if self.handoffs.filter(completedAt__isnull=True).exists():
                raise ValidationError(
                    {
                        "status": "该槽尚有未完成的下槽交接卷，须完成交接后才能改回「装叶中」清空下一轮。"
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


class UnloadHandoff(models.Model):
    """下槽交接卷：挂在可下槽槽位上，交接完成后该槽才能改回装叶中。"""

    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="handoffs",
        verbose_name="所属槽位",
    )
    handoffAt = models.DateTimeField("交接时刻")
    receivingTeam = models.CharField("接收班组", max_length=80)
    outKg = models.DecimalField("出叶千克", max_digits=10, decimal_places=2)
    signer = models.CharField("签字人", max_length=80)
    completedAt = models.DateTimeField("完成时刻", null=True, blank=True)

    class Meta:
        ordering = ["-handoffAt", "-id"]
        verbose_name = "下槽交接卷"
        verbose_name_plural = "下槽交接卷"
        constraints = [
            # 同一槽位同时只允许一卷未完成交接。
            models.UniqueConstraint(
                fields=["trough"],
                condition=models.Q(completedAt__isnull=True),
                name="uniq_open_handoff_per_trough",
            ),
        ]

    def __str__(self):
        state = "交接中" if self.completedAt is None else "已完成"
        return f"{self.trough} 下槽交接({state})"

    @property
    def is_complete(self):
        return self.completedAt is not None

    def clean(self):
        super().clean()
        if self.completedAt is not None:
            # 已完成卷不再重复执行开卷校验。
            return
        # 开卷（未完成卷）规则。
        if self.trough_id and self.trough.status != Trough.STATUS_READY:
            raise ValidationError({"trough": "开卷时槽位必须处于「可下槽」状态。"})

        if self.outKg is not None and self.outKg <= 0:
            raise ValidationError({"outKg": "出叶千克须为正数。"})

        if (
            self.outKg is not None
            and self.trough_id
            and self.outKg > self.trough.loadKg
        ):
            raise ValidationError(
                {"outKg": f"出叶千克不得超过该槽装叶量（{self.trough.loadKg} kg）。"}
            )

        open_qs = self.trough.handoffs.filter(completedAt__isnull=True) if self.trough_id else None
        if open_qs is not None:
            if self.pk:
                open_qs = open_qs.exclude(pk=self.pk)
            if open_qs.exists():
                raise ValidationError(
                    {"trough": "该槽已有未完成的下槽交接卷，不能再开新卷。"}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def complete(self, user):
        """主管完成交接：复核最新批次实测含水不空且 ≤ 40%，写入完成时刻。

        判定与「设为可下槽」共用 latest_batch_ready_for_unload，整个过程同事务。
        非主管抛 PermissionDenied；含水不满足抛 ValidationError，整卷回滚。
        """
        from django.core.exceptions import PermissionDenied

        # 主管 = 具有后台员工身份（种子账号 admin 为主管，witherer 不是）。
        if user is None or not (user.is_authenticated and user.is_staff):
            raise PermissionDenied("仅主管可完成下槽交接。")

        with transaction.atomic():
            # 行级锁，避免与改回装叶中/再次开卷竞态。
            locked = UnloadHandoff.objects.select_for_update().get(pk=self.pk)
            if locked.completedAt is not None:
                self.completedAt = locked.completedAt
                return self
            if latest_batch_ready_for_unload(locked.trough) is None:
                raise ValidationError(
                    "无法完成交接：该槽最新批次实测含水率为空或高于 40%。"
                )
            from django.utils import timezone

            locked.completedAt = timezone.now()
            locked.save(update_fields=["completedAt"])
            self.completedAt = locked.completedAt
            return locked
