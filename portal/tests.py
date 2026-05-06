from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Company, UserCompanyAccess
from ledger.models import AccountGroup, Ledger
from portal.models import BalanceConfirmation, ClientDocumentRequest, PortalUser
from portal.views import _build_portal_dashboard_context, _get_ledger_data, _public_request_pack
from vouchers.models import Voucher, VoucherItem


class PortalLedgerTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Portal Test Co",
            gstin="27AAAAA0000A1Z5",
            short_code="PT",
        )
        self.asset_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Debtors",
            nature="Asset",
        )
        self.income_group = AccountGroup.objects.create(
            company=self.company,
            name="Sales",
            nature="Income",
        )
        self.customer = Ledger.objects.create(
            company=self.company,
            name="Portal Customer",
            account_group=self.asset_group,
        )
        self.sales = Ledger.objects.create(
            company=self.company,
            name="Sales Ledger",
            account_group=self.income_group,
        )
        self.portal_user = PortalUser.objects.create(
            name="Portal User",
            email="portal@example.com",
            password="unused",
            linked_ledger=self.customer,
        )

    def _sales_voucher(self, amount, status="PENDING"):
        initial_status = "PENDING" if status == "APPROVED" else status
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 1),
            status=initial_status,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=amount,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=amount,
        )
        if status == "APPROVED":
            voucher.approve(None)
        return voucher

    def test_portal_statement_excludes_unapproved_vouchers(self):
        self._sales_voucher(Decimal("100.00"), status="APPROVED")
        self._sales_voucher(Decimal("999.00"), status="PENDING")

        transactions, running_balance = _get_ledger_data(self.customer)

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["amount"], Decimal("100.00"))
        self.assertEqual(running_balance, Decimal("100.00"))


class PortalLoginThrottleTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Portal Login Co",
            gstin="27EEEEE0000E1Z5",
            short_code="PL",
        )
        group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Debtors",
            nature="Asset",
        )
        self.ledger = Ledger.objects.create(
            company=self.company,
            name="Portal Customer",
            account_group=group,
        )
        self.portal_user = PortalUser(
            name="Portal User",
            email="login@example.com",
            linked_ledger=self.ledger,
        )
        self.portal_user.set_password("correct-password")
        self.portal_user.save()

    def test_portal_login_locks_session_after_repeated_failures(self):
        login_url = reverse("portal:login")

        for _ in range(5):
            self.client.post(
                login_url,
                {"email": self.portal_user.email, "password": "wrong"},
            )

        response = self.client.post(
            login_url,
            {"email": self.portal_user.email, "password": "correct-password"},
        )

        self.assertEqual(response.status_code, 200, getattr(response, "url", ""))
        self.assertNotIn("portal_user_id", self.client.session)

    @patch("portal.views.send_ledger_email")
    @patch("portal.views._generate_ledger_pdf_bytes", return_value=(b"pdf", "ledger.pdf"))
    def test_portal_user_can_dispute_balance_with_remarks(self, _pdf, _email):
        session = self.client.session
        session["portal_user_id"] = self.portal_user.pk
        session.save()

        response = self.client.post(
            reverse("portal:confirm_balance"),
            {"response_status": "disputed", "remarks": "Invoice INV-10 is already paid."},
        )

        self.assertRedirects(response, reverse("portal:dashboard"))
        confirmation = BalanceConfirmation.objects.get(portal_user=self.portal_user)
        self.assertEqual(confirmation.response_status, BalanceConfirmation.STATUS_DISPUTED)
        self.assertEqual(confirmation.remarks, "Invoice INV-10 is already paid.")


class PortalDashboardExperienceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Portal Command Co",
            gstin="27CCCCC0000C1Z5",
            short_code="PC",
        )
        self.asset_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Debtors",
            nature="Asset",
        )
        self.ledger = Ledger.objects.create(
            company=self.company,
            name="Command Customer",
            account_group=self.asset_group,
        )
        self.portal_user = PortalUser.objects.create(
            name="Command User",
            email="command@example.com",
            password="unused",
            linked_ledger=self.ledger,
        )

    def test_portal_dashboard_prioritizes_client_document_actions(self):
        today = timezone.localdate()
        overdue = ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="GST purchase invoices",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=today - timedelta(days=1),
            source_reference="GST-APR-2026",
        )
        uploaded = ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="Bank statement",
            document_type=ClientDocumentRequest.TYPE_BANK,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            uploaded_at=timezone.now(),
            response_note="Uploaded bank PDF.",
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="Closed TDS challan",
            document_type=ClientDocumentRequest.TYPE_TDS,
            status=ClientDocumentRequest.STATUS_CLOSED,
            closed_at=timezone.now(),
        )

        session = self.client.session
        session["portal_user_id"] = self.portal_user.pk
        session.save()

        response = self.client.get(reverse("portal:dashboard"))

        self.assertEqual(response.status_code, 200, getattr(response, "url", ""))
        self.assertContains(response, "Action Queue")
        self.assertContains(response, overdue.title)
        self.assertContains(response, "Overdue documents")
        self.assertContains(response, "Uploaded And Under Review")
        self.assertContains(response, uploaded.title)
        summary = response.context["request_summary"]
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["overdue"], 1)
        self.assertEqual(summary["uploaded"], 1)
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["completion_score"], 52)

    def test_staff_preview_uses_staff_statement_link_and_hides_portal_logout(self):
        staff = get_user_model().objects.create_superuser(
            email="staff@example.com",
            password="staff-pass",
        )
        UserCompanyAccess.objects.create(user=staff, company=self.company, role="Admin")
        self.client.force_login(staff)
        self.client.get(reverse("core:switch_company", args=[self.company.pk]))
        self.assertEqual(self.client.session.get("current_company_id"), self.company.pk)

        response = self.client.get(reverse("portal:ca_view_ledger", args=[self.portal_user.pk]))

        self.assertEqual(response.status_code, 200, getattr(response, "url", ""))
        self.assertContains(response, "Staff preview")
        self.assertContains(response, reverse("portal:ca_download_pdf", args=[self.portal_user.pk]))
        self.assertNotContains(response, "Logout")

    def test_dashboard_context_exposes_staff_download_url(self):
        context = _build_portal_dashboard_context(self.portal_user, is_staff_view=True)

        self.assertTrue(context["is_staff_view"])
        self.assertEqual(
            context["download_url"],
            reverse("portal:ca_download_pdf", args=[self.portal_user.pk]),
        )

    def test_public_upload_link_shows_same_client_request_pack(self):
        today = timezone.localdate()
        current = ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="Upload GST invoices",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=today,
        )
        next_request = ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="Upload bank statement",
            document_type=ClientDocumentRequest.TYPE_BANK,
            due_date=today + timedelta(days=1),
        )
        other_company = Company.objects.create(name="Other Portal Co", short_code="OP")
        ClientDocumentRequest.objects.create(
            company=other_company,
            portal_user=self.portal_user,
            title="Do not show other company request",
            document_type=ClientDocumentRequest.TYPE_OTHER,
            due_date=today,
        )

        response = self.client.get(reverse("portal:document_request_upload", args=[current.token]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request Pack")
        self.assertContains(response, current.title)
        self.assertContains(response, next_request.title)
        self.assertNotContains(response, "Do not show other company request")
        pack = response.context["request_pack"]
        self.assertEqual(pack["summary"]["open"], 2)
        self.assertEqual(pack["next_request"], next_request)

    def test_public_upload_success_links_to_next_pending_request(self):
        today = timezone.localdate()
        current = ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="Upload GST invoices",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=today,
        )
        next_request = ClientDocumentRequest.objects.create(
            company=self.company,
            portal_user=self.portal_user,
            title="Upload bank statement",
            document_type=ClientDocumentRequest.TYPE_BANK,
            due_date=today + timedelta(days=1),
        )
        upload = SimpleUploadedFile(
            "invoice.pdf",
            b"%PDF-1.4 client evidence\n%%EOF",
            content_type="application/pdf",
        )

        response = self.client.post(
            reverse("portal:document_request_upload", args=[current.token]),
            {"file": upload, "response_note": "April invoices attached."},
        )

        self.assertEqual(response.status_code, 200)
        current.refresh_from_db()
        self.assertEqual(current.status, ClientDocumentRequest.STATUS_UPLOADED)
        self.assertEqual(current.response_note, "April invoices attached.")
        self.assertContains(response, "Next Upload")
        self.assertContains(response, next_request.title)
        self.assertContains(response, reverse("portal:document_request_upload", args=[next_request.token]))
        pack = response.context["request_pack"]
        self.assertEqual(pack["summary"]["open"], 1)
        self.assertEqual(pack["summary"]["uploaded"], 1)
        self.assertEqual(pack["next_request"], next_request)

    def test_public_request_pack_can_group_by_contact_without_portal_user(self):
        current = ClientDocumentRequest.objects.create(
            company=self.company,
            recipient_email="client@example.com",
            title="Upload TDS challan",
            document_type=ClientDocumentRequest.TYPE_TDS,
        )
        sibling = ClientDocumentRequest.objects.create(
            company=self.company,
            recipient_email="client@example.com",
            title="Upload Form 16A",
            document_type=ClientDocumentRequest.TYPE_TDS,
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            recipient_email="other@example.com",
            title="Other client document",
            document_type=ClientDocumentRequest.TYPE_OTHER,
        )

        pack = _public_request_pack(current)

        self.assertEqual(pack["summary"]["open"], 2)
        self.assertIn(current, pack["open_requests"])
        self.assertIn(sibling, pack["open_requests"])
