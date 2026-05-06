import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from .models import AuditLog, Company, PracticeTask, UserCompanyAccess
from .production_trust import list_backup_manifests, list_scheduled_backup_runs
from .system_observability import (
    OBSERVABILITY_TASK_PREFIX,
    build_system_observability,
    create_system_observability_tasks,
)


class SystemObservabilityTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="System Observability Co",
            gstin="27OBSRV0000O1Z5",
            short_code="OBS",
        )
        self.staff = get_user_model().objects.create_user(
            email="observability-staff@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        UserCompanyAccess.objects.create(user=self.staff, company=self.company, role="Admin")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.staff,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=30),
        )
        self.client.force_login(self.staff)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_observability_report_contains_core_checks(self):
        with tempfile.TemporaryDirectory() as root:
            media_root = Path(root) / "media"
            with override_settings(MEDIA_ROOT=media_root):
                report = build_system_observability(company=self.company)

        components = {check["component"] for check in report["checks"]}
        self.assertTrue({"Database", "Cache", "Storage", "Backups", "Integrations"}.issubset(components))
        self.assertIn(report["status"], {"healthy", "degraded", "critical"})
        self.assertEqual(report["totals"]["check_count"], len(report["checks"]))
        self.assertIn("score", report)

    def test_staff_can_view_system_observability(self):
        response = self.client.get(reverse("core:system_observability"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Observability")
        self.assertContains(response, "Diagnostic Checks")

    def test_system_observability_api_returns_json(self):
        response = self.client.get(reverse("core:system_observability_api"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("ok", payload)
        self.assertIn("checks", payload)
        self.assertIn("totals", payload)
        self.assertGreaterEqual(payload["totals"]["check_count"], 1)

    def test_observability_task_sync_creates_and_closes_tasks(self):
        report = {
            "score": 70,
            "status_label": "Critical",
            "taskable_checks": [
                {
                    "component": "Database",
                    "name": "Pending migrations",
                    "level": "critical",
                    "message": "2 pending migrations.",
                    "hint": "Run migrate before rollout.",
                    "reference": f"{OBSERVABILITY_TASK_PREFIX}{self.company.pk}:database_pending_migrations",
                }
            ],
            "checks": [],
        }

        result = create_system_observability_tasks(self.company, self.staff, report)

        self.assertEqual(result["created"], 1)
        task = PracticeTask.objects.get(
            company=self.company,
            reference=f"{OBSERVABILITY_TASK_PREFIX}{self.company.pk}:database_pending_migrations",
        )
        self.assertEqual(task.priority, PracticeTask.PRIORITY_CRITICAL)
        self.assertEqual(task.status, PracticeTask.STATUS_OPEN)
        self.assertIn("Run migrate", task.description)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="system_observability",
            ).exists()
        )

        clear_result = create_system_observability_tasks(self.company, self.staff, {
            "score": 100,
            "status_label": "Healthy",
            "taskable_checks": [],
            "checks": [],
        })
        task.refresh_from_db()

        self.assertEqual(clear_result["closed"], 1)
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)

    def test_staff_can_sync_observability_tasks_from_page(self):
        response = self.client.post(
            reverse("core:system_observability"),
            {"action": "sync_observability_tasks"},
        )

        self.assertRedirects(response, reverse("core:system_observability"))
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"{OBSERVABILITY_TASK_PREFIX}{self.company.pk}:",
            ).exists()
        )

    def test_staff_can_run_backup_drill_from_observability(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                BASE_DIR=Path(root),
                MEDIA_ROOT=str(Path(root) / "media"),
                BACKUP_ENCRYPTION_REQUIRED=False,
            ):
                response = self.client.post(
                    reverse("core:system_observability"),
                    {
                        "action": "run_observability_backup_drill",
                        "prune_backups": "1",
                    },
                )
                manifests = list_backup_manifests(output_dir=Path(root) / "backups")

        self.assertRedirects(response, reverse("core:system_observability"))
        self.assertEqual(len(manifests), 1)
        self.assertTrue(manifests[0]["valid"])
        self.assertTrue(manifests[0]["data_file_exists"])

    def test_staff_can_run_scheduled_backup_evidence_from_observability(self):
        with tempfile.TemporaryDirectory() as root:
            offsite_dir = Path(root) / "offsite"
            with override_settings(
                BASE_DIR=Path(root),
                MEDIA_ROOT=str(Path(root) / "media"),
                BACKUP_ENCRYPTION_REQUIRED=False,
                BACKUP_OFFSITE_DIR=str(offsite_dir),
                BACKUP_OFFSITE_REQUIRED=True,
            ):
                response = self.client.post(
                    reverse("core:system_observability"),
                    {
                        "action": "run_observability_scheduled_backup",
                        "prune_backups": "1",
                        "copy_offsite": "1",
                    },
                )
                runs = list_scheduled_backup_runs(output_dir=Path(root) / "backups")
                manifests = list_backup_manifests(output_dir=Path(root) / "backups")

        self.assertRedirects(response, reverse("core:system_observability"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["offsite_status"], "copied")
        self.assertEqual(len(manifests), 1)

    def test_non_staff_cannot_view_system_observability(self):
        user = get_user_model().objects.create_user(
            email="observability-user@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(user=user, company=self.company, role="Admin")
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        response = self.client.get(reverse("core:system_observability"))

        self.assertIn(response.status_code, {302, 303})
        self.assertIn(response.url, {reverse("core:dashboard"), reverse("core:select_company")})

    def test_non_staff_api_is_forbidden(self):
        user = get_user_model().objects.create_user(
            email="observability-api-user@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(user=user, company=self.company, role="Admin")
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        response = self.client.get(reverse("core:system_observability_api"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "staff_required")
