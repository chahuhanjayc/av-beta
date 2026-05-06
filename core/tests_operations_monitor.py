from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from integrations.models import IntegrationRequestLog, IntegrationRetryJob
from portal.models import ClientDocumentRequest

from .models import AuditLog, Company, PracticeTask, UserCompanyAccess
from .operations_monitor import OPERATIONS_TASK_PREFIX, build_operations_monitor, create_operations_monitor_tasks


class OperationsMonitorTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Operations Monitor Co",
            gstin="27AAAAA0000A1Z5",
            short_code="OPS",
        )
        self.staff = get_user_model().objects.create_superuser(
            email="ops-staff@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(user=self.staff, company=self.company, role="Admin")
        self.client.force_login(self.staff)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_operations_monitor_collects_cross_system_incidents(self):
        today = timezone.localdate()
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload GST invoices",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=today - timezone.timedelta(days=2),
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Uploaded bank statement",
            document_type=ClientDocumentRequest.TYPE_BANK,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            uploaded_at=timezone.now() - timezone.timedelta(days=3),
        )
        IntegrationRetryJob.objects.create(
            company=self.company,
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            provider="IRP Provider",
            status=IntegrationRetryJob.STATUS_FAILED,
            next_attempt_at=timezone.now() - timezone.timedelta(hours=1),
            last_error="IRP authentication failed",
            created_by=self.staff,
        )
        PracticeTask.objects.create(
            company=self.company,
            title="Overdue statutory task",
            task_type=PracticeTask.TYPE_GST,
            priority=PracticeTask.PRIORITY_CRITICAL,
            status=PracticeTask.STATUS_OPEN,
            due_date=today - timezone.timedelta(days=1),
            created_by=self.staff,
        )

        monitor = build_operations_monitor(Company.objects.filter(pk=self.company.pk), current_company=self.company)
        codes = {issue["code"] for issue in monitor["issues"]}

        self.assertIn("provider_retry_backlog", codes)
        self.assertIn("client_requests_overdue", codes)
        self.assertIn("client_upload_review_stale", codes)
        self.assertIn("task_sla_breach", codes)
        self.assertGreaterEqual(monitor["totals"]["critical"], 2)
        self.assertLess(monitor["score"], 100)

    def test_operations_monitor_syncs_incident_tasks_with_audit(self):
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload GST invoices",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=timezone.localdate() - timezone.timedelta(days=1),
        )
        monitor = build_operations_monitor(Company.objects.filter(pk=self.company.pk), current_company=self.company)

        result = create_operations_monitor_tasks(self.staff, monitor)

        self.assertGreater(result["created"], 0)
        task = PracticeTask.objects.filter(
            company=self.company,
            reference__startswith=f"{OPERATIONS_TASK_PREFIX}{self.company.pk}:",
        ).first()
        self.assertIsNotNone(task)
        self.assertTrue(task.title.startswith("Operations Monitor:"))
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="operations_monitor",
            ).exists()
        )

    def test_staff_can_view_operations_monitor(self):
        response = self.client.get(reverse("core:operations_monitor"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operations Monitor")
        self.assertContains(response, "Incident Queue")

    def test_non_staff_cannot_view_operations_monitor(self):
        user = get_user_model().objects.create_user(
            email="ops-user@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(user=user, company=self.company, role="Admin")
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        response = self.client.get(reverse("core:operations_monitor"))

        self.assertIn(response.status_code, {302, 303})
        self.assertIn(response.url, {reverse("core:dashboard"), reverse("core:select_company")})
