import tempfile
import json
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from .go_live_certificate import (
    GO_LIVE_TASK_PREFIX,
    build_go_live_certificate,
    create_go_live_remediation_tasks,
    go_live_certificate_payload,
)
from .go_live_evidence_pack import build_go_live_evidence_pack, go_live_evidence_pack_bytes
from .models import AuditLog, Company, PracticeTask, UserCompanyAccess


class GoLiveCertificateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Go Live Co",
            gstin="27GOLIV0000G1Z5",
            short_code="GLC",
        )
        self.staff = get_user_model().objects.create_user(
            email="go-live-staff@example.com",
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

    def test_go_live_certificate_builds_gates_and_payload(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                certificate = build_go_live_certificate(company=self.company, include_deploy=False)
                payload = go_live_certificate_payload(certificate)

        self.assertTrue(certificate["certificate_id"].startswith("GLC-"))
        self.assertEqual(certificate["totals"]["gates"], len(certificate["gates"]))
        self.assertIn(certificate["status"], {"blocked", "conditional", "certified"})
        self.assertIn("can_go_live", payload)
        self.assertIn("remediation_pack", payload)
        self.assertTrue(payload["remediation_pack"]["environment"])
        self.assertTrue(payload["remediation_pack"]["commands"])
        self.assertTrue(any(gate["code"] == "backup_policy" for gate in certificate["gates"]))
        self.assertTrue(any(gate["code"] == "workers" for gate in certificate["gates"]))

    def test_staff_can_view_go_live_certificate(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                response = self.client.get(reverse("core:go_live_certificate"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Go-Live Certificate")
        self.assertContains(response, "Certificate Gates")
        self.assertContains(response, "Deployment Pack")

    def test_go_live_task_sync_creates_and_closes_tasks(self):
        certificate = {
            "certificate_id": "GLC-TEST",
            "score": 60,
            "status_label": "Blocked",
            "gates": [
                {
                    "code": "backup_policy",
                    "area": "Recovery",
                    "name": "Backup Policy",
                    "status": "blocked",
                    "status_label": "Blocked",
                    "message": "No backup manifest found.",
                    "recommendation": "Run backup drill.",
                    "evidence": "Backups / Backup policy",
                }
            ],
        }

        result = create_go_live_remediation_tasks(self.company, self.staff, certificate)

        self.assertEqual(result["created"], 1)
        task = PracticeTask.objects.get(
            company=self.company,
            reference=f"{GO_LIVE_TASK_PREFIX}{self.company.pk}:backup_policy",
        )
        self.assertEqual(task.priority, PracticeTask.PRIORITY_CRITICAL)
        self.assertIn("Run backup drill", task.description)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="go_live_certificate",
            ).exists()
        )

        clear_result = create_go_live_remediation_tasks(self.company, self.staff, {
            "certificate_id": "GLC-CLEAR",
            "score": 100,
            "status_label": "Certified",
            "gates": [],
        })
        task.refresh_from_db()

        self.assertEqual(clear_result["closed"], 1)
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)

    def test_staff_can_sync_go_live_tasks_from_page(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                response = self.client.post(
                    reverse("core:go_live_certificate"),
                    {"action": "sync_go_live_tasks", "deploy": "0"},
                )

        self.assertRedirects(response, reverse("core:go_live_certificate") + "?deploy=0")
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"{GO_LIVE_TASK_PREFIX}{self.company.pk}:",
            ).exists()
        )

    def test_go_live_certificate_api_returns_json(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                response = self.client.get(reverse("core:go_live_certificate_api"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("certificate_id", payload)
        self.assertIn("gates", payload)
        self.assertIn("totals", payload)

    def test_non_staff_cannot_view_go_live_certificate(self):
        user = get_user_model().objects.create_user(
            email="go-live-user@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(user=user, company=self.company, role="Admin")
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        response = self.client.get(reverse("core:go_live_certificate"))

        self.assertIn(response.status_code, {302, 303})
        self.assertIn(response.url, {reverse("core:dashboard"), reverse("core:select_company")})

    def test_go_live_certificate_command_emits_json(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                stdout = StringIO()
                call_command(
                    "go_live_certificate",
                    "--company-id",
                    str(self.company.pk),
                    "--runtime-only",
                    "--json",
                    stdout=stdout,
                )

        self.assertIn("certificate_id", stdout.getvalue())
        self.assertIn("gates", stdout.getvalue())

    def test_go_live_evidence_pack_builds_signed_redacted_payload(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                BASE_DIR=Path(root),
                MEDIA_ROOT=str(Path(root) / "media"),
                GST_API_SECRET="do-not-leak-this-secret",
            ):
                pack = build_go_live_evidence_pack(
                    company=self.company,
                    user=self.staff,
                    include_deploy=False,
                )
                payload_text = go_live_evidence_pack_bytes(pack).decode("utf-8")

        self.assertTrue(pack["pack_id"].startswith("GLP-"))
        self.assertEqual(len(pack["sha256"]), 64)
        self.assertIn("certificate", pack)
        self.assertIn("observability", pack)
        self.assertIn("recovery", pack)
        self.assertIn("evidence_vault", pack)
        self.assertIn("integrations", pack)
        self.assertFalse(pack["redaction"]["raw_credentials_included"])
        self.assertNotIn("do-not-leak-this-secret", payload_text)

    def test_staff_can_download_go_live_evidence_pack(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                response = self.client.get(reverse("core:go_live_evidence_pack"), {"deploy": "0"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        payload = json.loads(response.content.decode("utf-8"))
        self.assertTrue(payload["pack_id"].startswith("GLP-"))
        self.assertEqual(payload["company"]["id"], self.company.pk)

    def test_go_live_evidence_pack_command_writes_json(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "packs"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                stdout = StringIO()
                call_command(
                    "go_live_evidence_pack",
                    "--company-id",
                    str(self.company.pk),
                    "--runtime-only",
                    "--output-dir",
                    str(output_dir),
                    "--json",
                    stdout=stdout,
                )
                payload = json.loads(stdout.getvalue())
                self.assertTrue(payload["pack_id"].startswith("GLP-"))
                self.assertTrue(Path(payload["path"]).exists())
