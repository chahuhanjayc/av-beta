from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from core.security_control import SECURITY_TASK_PREFIX, build_security_control, create_security_control_tasks


class SecurityControlTests(TestCase):
    password = "CorrectHorseBatteryStaple123!"

    def setUp(self):
        self.company = Company.objects.create(
            name="Security Control Co",
            gstin="27SECUR0000S1Z5",
            short_code="SCC",
        )
        self.admin = get_user_model().objects.create_user(
            email="security-admin@example.com",
            password=self.password,
            last_login=timezone.now(),
        )
        self.viewer = get_user_model().objects.create_user(
            email="security-viewer@example.com",
            password=self.password,
            last_login=timezone.now(),
        )
        self.staff = get_user_model().objects.create_user(
            email="security-staff@example.com",
            password=self.password,
            is_staff=True,
            last_login=timezone.now() - timedelta(days=120),
        )
        self.inactive = get_user_model().objects.create_user(
            email="security-inactive@example.com",
            password=self.password,
            is_active=False,
        )
        UserCompanyAccess.objects.create(user=self.admin, company=self.company, role="Admin")
        UserCompanyAccess.objects.create(user=self.viewer, company=self.company, role="Viewer")
        UserCompanyAccess.objects.create(user=self.staff, company=self.company, role="Accountant")
        UserCompanyAccess.objects.create(user=self.inactive, company=self.company, role="Viewer")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.admin,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    @override_settings(REQUIRE_STAFF_MFA=False, ALLOW_PUBLIC_REGISTRATION=True)
    def test_security_control_detects_access_and_mfa_risks(self):
        assessment = build_security_control(self.company)
        issue_codes = {issue["code"] for issue in assessment["issues"]}

        self.assertEqual(assessment["status"], "Blocked")
        self.assertIn("staff_mfa_disabled", issue_codes)
        self.assertIn("inactive_users_have_access", issue_codes)
        self.assertIn("dormant_access", issue_codes)
        self.assertIn("public_registration_enabled", issue_codes)

    @override_settings(REQUIRE_STAFF_MFA=False)
    def test_security_control_task_sync_creates_audit_tasks(self):
        assessment = build_security_control(self.company)
        result = create_security_control_tasks(self.company, self.admin, assessment)

        self.assertGreaterEqual(result["created"], 2)
        tasks = PracticeTask.objects.filter(company=self.company, reference__startswith=f"{SECURITY_TASK_PREFIX}{self.company.pk}:")
        self.assertTrue(tasks.filter(priority=PracticeTask.PRIORITY_CRITICAL).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                new_data__source="security_control",
            ).exists()
        )

    def test_company_admin_can_view_security_control(self):
        self._login(self.admin)

        response = self.client.get(reverse("core:security_control"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Security Control")
        self.assertContains(response, "Company Access Register")

    def test_viewer_cannot_view_security_control(self):
        self._login(self.viewer)

        response = self.client.get(reverse("core:security_control"))

        self.assertRedirects(response, reverse("core:dashboard"))
