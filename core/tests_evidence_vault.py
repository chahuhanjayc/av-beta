import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.evidence_vault import (
    VAULT_TASK_PREFIX,
    create_evidence_vault_tasks,
    list_vault_entries,
    seal_evidence_vault,
    verify_vault_chain,
)
from core.models import AuditLog, Company, GSTEvidenceDocument, PracticeTask, UserCompanyAccess


class EvidenceVaultTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Evidence Vault Co",
            gstin="27VAULT0000V1Z5",
            short_code="EVC",
        )
        self.user = get_user_model().objects.create_user(
            email="vault@example.com",
            password="secret",
            is_staff=True,
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )

    def _login(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def _create_gst_evidence(self):
        doc = GSTEvidenceDocument.objects.create(
            company=self.company,
            period_start=timezone.localdate().replace(day=1),
            period_end=timezone.localdate(),
            evidence_type=GSTEvidenceDocument.TYPE_GSTR3B_ACK,
            return_type=GSTEvidenceDocument.RETURN_GSTR3B,
            title="GSTR-3B acknowledgement",
            uploaded_by=self.user,
        )
        doc.file.save("gstr3b-ack.txt", ContentFile(b"portal acknowledgement"), save=True)
        return doc

    def test_vault_seal_records_gst_evidence_and_detects_file_tamper(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                MEDIA_ROOT=str(Path(root) / "media"),
                EVIDENCE_VAULT_DIR=Path(root) / "vault",
                BASE_DIR=Path(root),
            ):
                doc = self._create_gst_evidence()
                result = seal_evidence_vault(self.company, self.user)
                clean = verify_vault_chain(self.company)
                entries = list_vault_entries(self.company)

                Path(doc.file.path).write_bytes(b"tampered acknowledgement")
                broken = verify_vault_chain(self.company)

        self.assertEqual(result["created"], 1)
        self.assertEqual(clean["status"], "Sealed")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_model"], "GSTEvidenceDocument")
        self.assertEqual(broken["status"], "Broken")
        self.assertTrue(any(issue["code"].startswith("artifact_") for issue in broken["issues"]))

    def test_vault_task_sync_creates_audit_task_for_broken_chain(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(
                MEDIA_ROOT=str(Path(root) / "media"),
                EVIDENCE_VAULT_DIR=Path(root) / "vault",
                BASE_DIR=Path(root),
            ):
                doc = self._create_gst_evidence()
                seal_evidence_vault(self.company, self.user)
                Path(doc.file.path).write_bytes(b"changed after seal")
                verification = verify_vault_chain(self.company)
                result = create_evidence_vault_tasks(self.company, self.user, verification)

        self.assertEqual(result["created"], 1)
        task = PracticeTask.objects.get(company=self.company, reference__startswith=VAULT_TASK_PREFIX)
        self.assertEqual(task.task_type, PracticeTask.TYPE_AUDIT)
        self.assertEqual(task.priority, PracticeTask.PRIORITY_CRITICAL)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                new_data__source="evidence_vault",
            ).exists()
        )

    def test_evidence_center_shows_vault_controls(self):
        self._login()

        response = self.client.get(reverse("integrations:evidence_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Immutable Evidence Vault")
        self.assertContains(response, "Seal Evidence Vault")
