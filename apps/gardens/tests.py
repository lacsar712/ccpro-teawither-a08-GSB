from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Garden, Trough, UnloadHandover, WitherBatch


class HandoverRuleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            "boss", "boss@teawither.local", "123456"
        )
        self.worker = User.objects.create_user(
            "hand", "hand@teawither.local", "123456"
        )
        self.garden = Garden.objects.create(name="测试园", altitudeBand="600m")
        # loading 建槽 → 补合格批次 → 转 ready
        self.trough = Trough.objects.create(
            garden=self.garden,
            troughCode="T-1",
            cultivar="福鼎大白",
            loadKg=Decimal("100.00"),
            status=Trough.STATUS_LOADING,
        )
        self.batch = WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now() - timezone.timedelta(hours=10),
            targetMoisture=Decimal("38.00"),
            actualMoisture=Decimal("37.00"),
            rollGrade="一级",
        )
        self.trough.status = Trough.STATUS_READY
        self.trough.save()

    def open_handover(self, output="90.00"):
        return UnloadHandover.objects.create(
            trough=self.trough,
            receiverTeam="甲班",
            outputKg=Decimal(output),
            signer="张三",
        )

    # ---- 开卷 ----

    def test_open_handover_on_ready_trough(self):
        h = self.open_handover()
        self.assertIsNone(h.completedAt)
        self.assertTrue(h.is_open)

    def test_cannot_open_when_trough_not_ready(self):
        self.trough.status = Trough.STATUS_WITHERING
        self.trough.save()
        with self.assertRaises(ValidationError) as ctx:
            self.open_handover()
        self.assertIn("trough", ctx.exception.message_dict)

    def test_cannot_open_second_unfinished_on_same_trough(self):
        self.open_handover()
        with self.assertRaises(ValidationError):
            self.open_handover()

    def test_partial_unique_constraint_blocks_duplicate_open(self):
        # 绕过模型 clean，数据库部分唯一约束仍须兜底
        from django.db import transaction

        self.open_handover()
        duplicate = UnloadHandover(
            trough=self.trough,
            receiverTeam="乙班",
            outputKg=Decimal("80.00"),
            signer="李四",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UnloadHandover.objects.bulk_create([duplicate])

    def test_output_must_be_positive(self):
        with self.assertRaises(ValidationError) as ctx:
            self.open_handover(output="0")
        self.assertIn("outputKg", ctx.exception.message_dict)

    def test_output_must_not_exceed_load(self):
        with self.assertRaises(ValidationError) as ctx:
            self.open_handover(output="100.01")
        self.assertIn("outputKg", ctx.exception.message_dict)

    def test_open_new_handover_after_completion_allowed(self):
        h = self.open_handover()
        h.complete()
        h2 = self.open_handover(output="95.00")
        self.assertIsNone(h2.completedAt)

    # ---- 完成交接 ----

    def test_supervisor_complete_writes_timestamp(self):
        h = self.open_handover()
        h.complete()
        h.refresh_from_db()
        self.assertIsNotNone(h.completedAt)

    def test_complete_rejects_bad_moisture(self):
        h = self.open_handover()
        self.batch.actualMoisture = Decimal("42.00")
        self.batch.save()
        with self.assertRaises(ValidationError):
            h.complete()
        h.refresh_from_db()
        self.assertIsNone(h.completedAt)

    def test_complete_rejects_missing_moisture(self):
        h = self.open_handover()
        self.batch.actualMoisture = None
        self.batch.save()
        with self.assertRaises(ValidationError):
            h.complete()
        h.refresh_from_db()
        self.assertIsNone(h.completedAt)

    def test_complete_idempotent(self):
        h = self.open_handover()
        h.complete()
        with self.assertRaises(ValidationError):
            h.complete()

    # ---- 改回装叶中 ----

    def test_revert_to_loading_blocked_while_handover_open(self):
        self.open_handover()
        self.trough.status = Trough.STATUS_LOADING
        with self.assertRaises(ValidationError) as ctx:
            self.trough.save()
        self.assertIn("status", ctx.exception.message_dict)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)

    def test_revert_to_loading_allowed_after_completion(self):
        h = self.open_handover()
        h.complete()
        self.trough.status = Trough.STATUS_LOADING
        self.trough.save()  # 不应抛错
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_LOADING)

    # ---- HTTP 层 ----

    def test_worker_cannot_complete(self):
        h = self.open_handover()
        self.client.force_login(self.worker)
        resp = self.client.post(reverse("handover_complete", args=[h.pk]))
        # 已登录但非主管：staff_member_required 导向管理员登录页
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)
        h.refresh_from_db()
        self.assertIsNone(h.completedAt)

    def test_worker_sees_list_but_not_complete_button(self):
        h = self.open_handover()
        self.client.force_login(self.worker)
        resp = self.client.get(reverse("handover_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse("handover_complete", args=[h.pk]))

    def test_supervisor_complete_flow(self):
        h = self.open_handover()
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("handover_complete", args=[h.pk]), follow=True
        )
        self.assertEqual(resp.status_code, 200)
        h.refresh_from_db()
        self.assertIsNotNone(h.completedAt)

    def test_supervisor_complete_bad_moisture_flashes_error(self):
        h = self.open_handover()
        self.batch.actualMoisture = Decimal("41.00")
        self.batch.save()
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("handover_complete", args=[h.pk]), follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "含水率")
        h.refresh_from_db()
        self.assertIsNone(h.completedAt)

    def test_trough_list_annotates_open_handover(self):
        self.open_handover()
        self.client.force_login(self.worker)
        resp = self.client.get(reverse("trough_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "交接中")

    def test_create_form_only_offers_ready_troughs(self):
        other = Trough.objects.create(
            garden=self.garden,
            troughCode="T-2",
            cultivar="铁观音",
            loadKg=Decimal("50.00"),
            status=Trough.STATUS_LOADING,
        )
        self.client.force_login(self.worker)
        resp = self.client.get(reverse("handover_create"))
        choices = [
            c[0] for c in resp.context["form"].fields["trough"].choices
        ]
        self.assertIn(self.trough.pk, choices)
        self.assertNotIn(other.pk, choices)
