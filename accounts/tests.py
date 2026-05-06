import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse


class RegistrationSecurityTests(TestCase):
    password = "CorrectHorseBatteryStaple123!"

    def tearDown(self):
        cache.clear()

    @override_settings(ALLOW_PUBLIC_REGISTRATION=False, REGISTRATION_INVITE_CODE="")
    def test_registration_is_disabled_without_invite(self):
        response = self.client.get(reverse("accounts:register"))

        self.assertRedirects(response, reverse("accounts:login"))
        self.assertEqual(get_user_model().objects.count(), 0)

    @override_settings(ALLOW_PUBLIC_REGISTRATION=False, REGISTRATION_INVITE_CODE="invite-123")
    def test_invite_registration_creates_normal_user(self):
        response = self.client.post(
            f"{reverse('accounts:register')}?invite=invite-123",
            {
                "email": "client@example.com",
                "first_name": "Client",
                "last_name": "User",
                "password1": self.password,
                "password2": self.password,
            },
        )

        self.assertRedirects(response, reverse("core:select_company"))
        user = get_user_model().objects.get(email="client@example.com")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)


class StaffMFATests(TestCase):
    password = "CorrectHorseBatteryStaple123!"

    def tearDown(self):
        cache.clear()

    @override_settings(
        REQUIRE_STAFF_MFA=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="no-reply@example.com",
    )
    def test_staff_login_requires_email_mfa_before_session_login(self):
        mail.outbox = []
        user = get_user_model().objects.create_user(
            email="staff@example.com",
            password=self.password,
            is_staff=True,
        )

        response = self.client.post(
            reverse("accounts:login"),
            {"email": user.email, "password": self.password},
        )

        self.assertRedirects(response, reverse("accounts:mfa_verify"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 1)

        code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
        response = self.client.post(reverse("accounts:mfa_verify"), {"code": code})

        self.assertRedirects(response, "/core/select-company/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
