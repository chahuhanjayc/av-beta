from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AuditLog,
    BankStatement,
    BankStatementRow,
    ClientEngagement,
    Company,
    CompanySettings,
    ComplianceFiling,
    ComplianceNotice,
    FilingReview,
    GSTEvidenceDocument,
    GSTFilingPack,
    GSTPeriodReview,
    GSTPostFilingTracker,
    PracticeTask,
    UserCompanyAccess,
)
from gstr2b.models import PortalGSTR2BEntry
from inventory.models import CompanySettings as InventorySettings
from inventory.models import StockItem, TaxRate
from ledger.models import AccountGroup, Ledger
from ocr.models import OCRSubmission
from portal.models import ClientDocumentRequest, PortalUser
from tds.models import TDSEntry, TDSSection
from tds.workbench import default_return_period, quarter_dates
from vouchers.models import Voucher, VoucherItem
from clients.models import ClientSubscription


class ProductionDemoSmokeTests(TestCase):
    """High-value smoke checks for a client-demo deployment."""

    def setUp(self):
        self.company = Company.objects.create(
            name="Demo Smoke Co",
            gstin="27SMOKE0000S1Z5",
            short_code="DSC",
        )
        CompanySettings.objects.get_or_create(company=self.company)
        InventorySettings.objects.get_or_create(
            company=self.company,
            defaults={"prevent_negative_stock": False},
        )
        self.user = get_user_model().objects.create_superuser(
            email="demo-smoke@example.com",
            password="smoke-pass",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.company,
            role="Admin",
        )

        self.asset_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Debtors",
            nature="Asset",
        )
        self.bank_group = AccountGroup.objects.create(
            company=self.company,
            name="Bank Accounts",
            nature="Asset",
        )
        self.income_group = AccountGroup.objects.create(
            company=self.company,
            name="Sales Accounts",
            nature="Income",
        )
        self.expense_group = AccountGroup.objects.create(
            company=self.company,
            name="Purchase Accounts",
            nature="Expense",
        )
        self.liability_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Creditors",
            nature="Liability",
        )
        self.customer = Ledger.objects.create(
            company=self.company,
            name="Smoke Customer",
            account_group=self.asset_group,
        )
        self.bank = Ledger.objects.create(
            company=self.company,
            name="Smoke Bank",
            account_group=self.bank_group,
        )
        self.sales = Ledger.objects.create(
            company=self.company,
            name="Smoke Sales",
            account_group=self.income_group,
        )
        self.purchase = Ledger.objects.create(
            company=self.company,
            name="Smoke Purchases",
            account_group=self.expense_group,
        )
        self.vendor = Ledger.objects.create(
            company=self.company,
            name="Smoke Vendor",
            account_group=self.liability_group,
        )
        self.tax_rate = TaxRate.objects.create(
            rate=Decimal("18.00"),
            description="Smoke GST 18%",
        )
        self.stock_item = StockItem.objects.create(
            company=self.company,
            name="Smoke Item",
            tax_rate=self.tax_rate,
            selling_price=Decimal("1000.00"),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.id
        session.save()

    def test_demo_critical_pages_render_without_server_errors(self):
        urls = [
            reverse("core:healthz"),
            reverse("core:dashboard"),
            reverse("core:production_trust_center"),
            reverse("core:accounting_close"),
            reverse("core:ca_command_center"),
            reverse("core:client_operating_readiness"),
            reverse("core:statutory_export_center"),
            reverse("core:partner_review_cockpit"),
            reverse("core:client_360", args=[self.company.pk]),
            reverse("core:client_engagements"),
            reverse("core:client_engagement_update", args=[self.company.pk]),
            reverse("core:ca_client_profitability"),
            reverse("core:filing_review_center"),
            reverse("core:gst_filing_pack"),
            reverse("core:gst_post_filing_dashboard"),
            reverse("core:gst_post_filing"),
            reverse("core:filing_readiness"),
            reverse("core:compliance_calendar"),
            reverse("core:app_settings"),
            reverse("core:gst_workbench"),
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
            reverse("core:practice_tasks"),
            reverse("core:compliance_filings"),
            reverse("core:compliance_notices"),
            reverse("core:audit_log"),
            reverse("migration:exit_control"),
            reverse("integrations:dashboard"),
            reverse("integrations:e_invoice_cockpit"),
            reverse("integrations:e_way_bill_cockpit"),
            reverse("integrations:evidence_center"),
            reverse("integrations:gst_result_import"),
            reverse("ledger:list"),
            reverse("ledger:create"),
            reverse("vouchers:list"),
            reverse("vouchers:quality"),
            reverse("vouchers:create"),
            reverse("core:collections_command_center"),
            reverse("core:bank_reco_autopilot"),
            reverse("vouchers:outstanding"),
            reverse("gstr2b:upload"),
            reverse("gstr2b:results"),
            reverse("inventory:list"),
            reverse("inventory:summary"),
            reverse("inventory:valuation"),
            reverse("inventory:godown_list"),
            reverse("inventory:batch_list"),
            reverse("reports:home"),
            reverse("reports:profit_loss_simple"),
            reverse("reports:balance_sheet_simple"),
            reverse("reports:trial_balance_simple"),
            reverse("reports:day_book"),
            reverse("reports:msme_overdue"),
            reverse("orders:order_list"),
            reverse("costcenter:cost_center_list"),
            reverse("payroll:employee_list"),
            reverse("fixedassets:asset_list"),
            reverse("portal:client_requests"),
            reverse("portal:client_request_reminders"),
            reverse("portal:client_request_campaign"),
            reverse("portal:client_request_create"),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_core_accounting_inventory_and_report_smoke_flow(self):
        purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 5, 1),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("500.00"),
            stock_item=self.stock_item,
            quantity=Decimal("1.000"),
            rate=Decimal("500.00"),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("500.00"),
        )
        purchase.approve(None)

        invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 5, 1),
            place_of_supply="27",
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
            stock_item=self.stock_item,
            quantity=Decimal("1.000"),
            rate=Decimal("1000.00"),
        )

        invoice.create_tax_lines()
        invoice.approve(None)
        invoice.sync_outstanding()
        invoice.refresh_from_db()

        self.assertEqual(invoice.status, "APPROVED")
        self.assertTrue(invoice.is_balanced())
        self.assertEqual(invoice.total_tax, Decimal("180.00"))
        self.assertEqual(invoice.outstanding_amount, Decimal("1180.00"))

        detail = self.client.get(reverse("vouchers:detail", args=[invoice.pk]))
        ledger_statement = self.client.get(reverse("ledger:statement", args=[self.customer.pk]))
        profit_loss = self.client.get(reverse("reports:profit_loss_simple"))
        balance_sheet = self.client.get(reverse("reports:balance_sheet_simple"))

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(ledger_statement.status_code, 200)
        self.assertEqual(profit_loss.status_code, 200)
        self.assertEqual(balance_sheet.status_code, 200)

    def test_universal_search_includes_new_pages_and_existing_records(self):
        old_voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Journal",
            date=date(2026, 4, 10),
            narration="Old migration opening adjustment",
            source_reference="OLD-REF-CTRLK",
        )
        task = PracticeTask.objects.create(
            company=self.company,
            title="Review client request evidence",
            task_type=PracticeTask.TYPE_DOCUMENT,
            reference="TASK-CTRLK",
        )
        doc_request = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload Ctrl K evidence",
            document_type=ClientDocumentRequest.TYPE_OTHER,
            source_reference="DOCREQ-CTRLK",
            requested_by=self.user,
        )

        empty_payload = self.client.get(reverse("core:api_search")).json()
        empty_nav = {item["name"] for item in empty_payload["navigation"]}
        self.assertIn("New Voucher", empty_nav)
        self.assertIn("Filing Review Center", empty_nav)
        self.assertIn("GST Filing Pack", empty_nav)
        self.assertIn("New Client Request", empty_nav)
        self.assertIn("Client Request Reminders", empty_nav)

        new_payload = self.client.get(reverse("core:api_search"), {"q": "new"}).json()
        new_nav = {item["name"] for item in new_payload["navigation"]}
        self.assertIn("New Voucher", new_nav)
        self.assertIn("New Client Request", new_nav)

        quality_payload = self.client.get(reverse("core:api_search"), {"q": "voucher quality"}).json()
        self.assertIn("Voucher Quality", {item["name"] for item in quality_payload["navigation"]})

        settings_payload = self.client.get(reverse("core:api_search"), {"q": "app settings"}).json()
        self.assertIn("App Settings", {item["name"] for item in settings_payload["navigation"]})

        partner_payload = self.client.get(reverse("core:api_search"), {"q": "partner review"}).json()
        self.assertIn("Partner Review Cockpit", {item["name"] for item in partner_payload["navigation"]})

        voucher_payload = self.client.get(reverse("core:api_search"), {"q": "OLD-REF-CTRLK"}).json()
        self.assertTrue(any(item["id"] == old_voucher.pk for item in voucher_payload["vouchers"]))

        task_payload = self.client.get(reverse("core:api_search"), {"q": "TASK-CTRLK"}).json()
        self.assertTrue(any(item["id"] == task.pk for item in task_payload["tasks"]))

        request_payload = self.client.get(reverse("core:api_search"), {"q": "DOCREQ-CTRLK"}).json()
        self.assertTrue(any(item["id"] == doc_request.pk for item in request_payload["client_requests"]))

    def test_ca_command_center_renders_without_selected_company(self):
        second_company = Company.objects.create(
            name="Second Smoke Co",
            gstin="27SMOKE0001S1Z5",
            short_code="SSC",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=second_company,
            role="Admin",
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload overdue GST support",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=date(2026, 5, 1),
            source_reference="CA-NEXT-ACTION",
            requested_by=self.user,
        )
        bank_statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=timezone.localdate(),
            notes="Command center bank queue",
        )
        BankStatementRow.objects.create(
            statement=bank_statement,
            date=timezone.localdate(),
            description="Unreconciled bank row",
            debit=Decimal("250.00"),
            credit=Decimal("0.00"),
        )
        overdue_invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=timezone.localdate() - timedelta(days=20),
            due_date=timezone.localdate() - timedelta(days=5),
            outstanding_amount=Decimal("1800.00"),
        )
        Voucher.objects.filter(pk=overdue_invoice.pk).update(status="APPROVED", outstanding_amount=Decimal("1800.00"))
        tds_section = TDSSection.objects.create(
            company=self.company,
            nature="TDS",
            section_code="194C",
            description="Contractor payments",
            threshold=Decimal("30000.00"),
            rate_individual=Decimal("1.00"),
            rate_company=Decimal("2.00"),
        )
        TDSEntry.objects.create(
            company=self.company,
            section=tds_section,
            deductee_ledger=self.vendor,
            transaction_date=date(timezone.localdate().year - 1, 4, 1),
            deductee_type="Company",
            deductible_amount=Decimal("50000.00"),
            rate_applied=Decimal("2.00"),
            tds_amount=Decimal("1000.00"),
            pan_number="ABCDE1234F",
            is_deposited=False,
        )
        self.vendor.is_msme = True
        self.vendor.msme_reg_number = "UDYAM-MH-00-0000001"
        self.vendor.save(update_fields=["is_msme", "msme_reg_number"])
        old_purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=timezone.localdate() - timedelta(days=60),
            outstanding_amount=Decimal("1200.00"),
        )
        VoucherItem.objects.create(
            voucher=old_purchase,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("1200.00"),
        )
        VoucherItem.objects.create(
            voucher=old_purchase,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("1200.00"),
        )
        Voucher.objects.filter(pk=old_purchase.pk).update(status="APPROVED", outstanding_amount=Decimal("1200.00"))
        session = self.client.session
        session.pop("current_company_id", None)
        session.save()

        response = self.client.get(reverse("core:ca_command_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.name)
        self.assertContains(response, second_company.name)
        self.assertContains(response, "Portfolio Health")
        self.assertContains(response, "Why / Next")
        self.assertContains(response, "Next Best Actions")
        self.assertContains(response, "Client document requests overdue")
        self.assertContains(response, "Bank reconciliation pending")
        self.assertContains(response, "Receivables overdue")
        self.assertContains(response, "TDS deposit due")
        self.assertContains(response, "MSME payment overdue")

        export = self.client.get(reverse("core:ca_command_center"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "text/csv")
        self.assertIn("Next Best Action", export.content.decode())
        self.assertIn("Client Work Queue", export.content.decode())
        self.assertIn("Primary Action", export.content.decode())
        self.assertIn("Risk Drivers", export.content.decode())

    def test_client_profitability_scores_pricing_workload_and_exports(self):
        second_company = Company.objects.create(
            name="Quiet Advisory Co",
            gstin="27QUIET0000Q1Z5",
            short_code="QAC",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=second_company,
            role="Admin",
        )
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            plan=ClientSubscription.PLAN_BASIC,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
            last_payment_amount=Decimal("500.00"),
        )
        for index in range(8):
            PracticeTask.objects.create(
                company=self.company,
                title=f"Heavy client task {index}",
                task_type=PracticeTask.TYPE_GST,
                priority=PracticeTask.PRIORITY_CRITICAL,
                status=PracticeTask.STATUS_OPEN,
                due_date=timezone.localdate() - timedelta(days=1),
                created_by=self.user,
            )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload pricing support",
            document_type=ClientDocumentRequest.TYPE_OTHER,
            status=ClientDocumentRequest.STATUS_OPEN,
            due_date=timezone.localdate() - timedelta(days=1),
            requested_by=self.user,
        )
        session = self.client.session
        session.pop("current_company_id", None)
        session.save()

        response = self.client.get(reverse("core:ca_client_profitability"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client Profitability & Workload Control")
        self.assertContains(response, self.company.name)
        self.assertContains(response, second_company.name)
        self.assertContains(response, "Underpriced")
        self.assertContains(response, "Overloaded")
        self.assertContains(response, "Renegotiate")
        self.assertContains(response, "Capture Fee")

        filtered = self.client.get(reverse("core:ca_client_profitability"), {"band": "underpriced"})
        self.assertEqual(filtered.status_code, 200)
        self.assertContains(filtered, self.company.name)
        self.assertContains(filtered, "Underpriced")

        export = self.client.get(reverse("core:ca_client_profitability"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "text/csv")
        body = export.content.decode()
        self.assertIn("Recommended Action", body)
        self.assertIn("Renegotiate", body)

    def test_client_engagement_retainer_feeds_profitability_and_360(self):
        session = self.client.session
        session.pop("current_company_id", None)
        session.save()

        list_response = self.client.get(reverse("core:client_engagements"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Client Engagements")
        self.assertContains(list_response, self.company.name)

        update_url = reverse("core:client_engagement_update", args=[self.company.pk])
        response = self.client.post(
            update_url,
            {
                "status": ClientEngagement.STATUS_ACTIVE,
                "service_package": ClientEngagement.PACKAGE_FULL_ACCOUNTING,
                "monthly_retainer": "12000.00",
                "billing_cycle": ClientEngagement.BILLING_MONTHLY,
                "renewal_date": "2026-06-15",
                "partner_owner": self.user.pk,
                "manager_owner": self.user.pk,
                "risk_rating": ClientEngagement.RISK_HIGH,
                "scope_summary": "Monthly accounting, GST, TDS, and management review.",
                "out_of_scope": "Income tax scrutiny and audit support.",
                "internal_notes": "Review pricing after first quarter.",
                "last_reviewed_at": "2026-05-03",
            },
        )

        self.assertRedirects(response, reverse("core:client_engagements"))
        engagement = ClientEngagement.objects.get(company=self.company)
        self.assertEqual(engagement.monthly_retainer, Decimal("12000.00"))
        self.assertEqual(engagement.partner_owner, self.user)

        profitability = self.client.get(reverse("core:ca_client_profitability"))
        self.assertEqual(profitability.status_code, 200)
        self.assertContains(profitability, "Rs. 12000")
        self.assertContains(profitability, "Engagement retainer")
        self.assertContains(profitability, "Full Accounting")

        client_360 = self.client.get(reverse("core:client_360", args=[self.company.pk]))
        self.assertEqual(client_360.status_code, 200)
        self.assertContains(client_360, "Monthly accounting, GST, TDS, and management review.")
        self.assertContains(client_360, "High")

        export = self.client.get(reverse("core:client_engagements"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        body = export.content.decode()
        self.assertIn("Monthly accounting", body)
        self.assertIn("12000.00", body)

    def test_engagement_alerts_surface_and_create_idempotent_tasks(self):
        today = timezone.localdate()
        ClientEngagement.objects.create(
            company=self.company,
            status=ClientEngagement.STATUS_ACTIVE,
            service_package=ClientEngagement.PACKAGE_BASIC,
            monthly_retainer=Decimal("1000.00"),
            billing_cycle=ClientEngagement.BILLING_MONTHLY,
            renewal_date=today + timedelta(days=10),
            risk_rating=ClientEngagement.RISK_HIGH,
            scope_summary="",
            out_of_scope="",
        )
        for index in range(8):
            PracticeTask.objects.create(
                company=self.company,
                title=f"Engagement overload task {index}",
                task_type=PracticeTask.TYPE_OTHER,
                priority=PracticeTask.PRIORITY_CRITICAL,
                status=PracticeTask.STATUS_OPEN,
                due_date=today - timedelta(days=1),
                created_by=self.user,
            )

        response = self.client.get(reverse("core:client_engagements"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Priority Engagement Alerts")
        self.assertContains(response, "Renewal due")
        self.assertContains(response, "Scope creep risk")
        self.assertContains(response, "Underpriced workload")
        self.assertContains(response, "Create Alert Tasks")

        client_360 = self.client.get(reverse("core:client_360", args=[self.company.pk]))
        self.assertEqual(client_360.status_code, 200)
        self.assertContains(client_360, "Engagement Alerts")
        self.assertContains(client_360, "Partner owner missing")

        task_url = reverse("core:client_engagement_alert_tasks")
        response = self.client.post(task_url, {"company_id": self.company.pk, "next": reverse("core:client_engagements")})
        self.assertRedirects(response, reverse("core:client_engagements"))
        created_refs = set(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"ENGAGE:{self.company.pk}:",
            ).values_list("reference", flat=True)
        )
        self.assertIn(f"ENGAGE:{self.company.pk}:underpriced_workload", created_refs)
        self.assertIn(f"ENGAGE:{self.company.pk}:scope_creep_risk", created_refs)
        self.assertIn(f"ENGAGE:{self.company.pk}:renewal_due", created_refs)
        count_after_first_post = len(created_refs)

        self.client.post(task_url, {"company_id": self.company.pk, "next": reverse("core:client_engagements")})
        self.assertEqual(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"ENGAGE:{self.company.pk}:",
            ).count(),
            count_after_first_post,
        )

        export = self.client.get(reverse("core:client_engagements"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        self.assertIn("Alerts", export.content.decode())
        self.assertIn("Underpriced workload", export.content.decode())

    def test_partner_review_cockpit_ranks_signoff_actions_and_exports(self):
        today = timezone.localdate()
        ClientEngagement.objects.create(
            company=self.company,
            status=ClientEngagement.STATUS_ACTIVE,
            service_package=ClientEngagement.PACKAGE_BASIC,
            monthly_retainer=Decimal("1000.00"),
            billing_cycle=ClientEngagement.BILLING_MONTHLY,
            renewal_date=today + timedelta(days=10),
            risk_rating=ClientEngagement.RISK_HIGH,
            scope_summary="",
            out_of_scope="",
        )
        for index in range(8):
            PracticeTask.objects.create(
                company=self.company,
                title=f"Partner overload task {index}",
                task_type=PracticeTask.TYPE_OTHER,
                priority=PracticeTask.PRIORITY_CRITICAL,
                status=PracticeTask.STATUS_OPEN,
                due_date=today - timedelta(days=1),
                created_by=self.user,
            )
        ComplianceFiling.objects.create(
            company=self.company,
            filing_type=ComplianceFiling.TYPE_GSTR3B,
            title="GSTR-3B partner approval",
            status=ComplianceFiling.STATUS_READY_FOR_REVIEW,
            priority=PracticeTask.PRIORITY_HIGH,
            due_date=today,
            period_start=today.replace(day=1),
            period_end=today,
            created_by=self.user,
        )
        ComplianceNotice.objects.create(
            company=self.company,
            notice_type=ComplianceNotice.TYPE_GST,
            title="GST response ready",
            status=ComplianceNotice.STATUS_RESPONSE_READY,
            priority=PracticeTask.PRIORITY_CRITICAL,
            response_due_date=today,
            created_by=self.user,
        )
        Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            status="PENDING",
            date=today,
            outstanding_amount=Decimal("2500.00"),
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Client uploaded notice evidence",
            document_type=ClientDocumentRequest.TYPE_GST_NOTICE,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            due_date=today,
            requested_by=self.user,
        )
        session = self.client.session
        session.pop("current_company_id", None)
        session.save()

        response = self.client.get(reverse("core:partner_review_cockpit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Partner Review Cockpit")
        self.assertContains(response, "Statutory filing ready for sign-off")
        self.assertContains(response, "Notice response ready for partner sign-off")
        self.assertContains(response, "Sales invoices awaiting approval")
        self.assertContains(response, "Client uploads need review before reply")
        self.assertContains(response, "Underpriced workload")
        self.assertContains(response, "Exact action")

        commercial = self.client.get(reverse("core:partner_review_cockpit"), {"focus": "commercial"})
        self.assertEqual(commercial.status_code, 200)
        self.assertContains(commercial, "Sales invoices awaiting approval")
        self.assertContains(commercial, "Underpriced workload")

        export = self.client.get(reverse("core:partner_review_cockpit"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        body = export.content.decode()
        self.assertIn("Exact Action", body)
        self.assertIn("Notice response ready for partner sign-off", body)

    def test_client_360_combines_partner_review_signals(self):
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            plan=ClientSubscription.PLAN_BASIC,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
            last_payment_amount=Decimal("500.00"),
        )
        PracticeTask.objects.create(
            company=self.company,
            title="Close GST review",
            task_type=PracticeTask.TYPE_GST,
            priority=PracticeTask.PRIORITY_CRITICAL,
            status=PracticeTask.STATUS_OPEN,
            due_date=timezone.localdate() - timedelta(days=1),
            created_by=self.user,
        )
        ComplianceFiling.objects.create(
            company=self.company,
            filing_type=ComplianceFiling.TYPE_GSTR3B,
            title="GSTR-3B May review",
            status=ComplianceFiling.STATUS_IN_PROGRESS,
            priority=PracticeTask.PRIORITY_HIGH,
            due_date=timezone.localdate() - timedelta(days=1),
            created_by=self.user,
        )
        ComplianceNotice.objects.create(
            company=self.company,
            notice_type=ComplianceNotice.TYPE_GST,
            title="GST ASMT response",
            status=ComplianceNotice.STATUS_RECEIVED,
            priority=PracticeTask.PRIORITY_HIGH,
            response_due_date=timezone.localdate() - timedelta(days=1),
            created_by=self.user,
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload purchase bills",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            status=ClientDocumentRequest.STATUS_OPEN,
            due_date=timezone.localdate() - timedelta(days=1),
            requested_by=self.user,
        )
        sales = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=timezone.localdate(),
            due_date=timezone.localdate() - timedelta(days=1),
            outstanding_amount=Decimal("1200.00"),
        )
        Voucher.objects.filter(pk=sales.pk).update(status="APPROVED", outstanding_amount=Decimal("1200.00"))
        session = self.client.session
        session.pop("current_company_id", None)
        session.save()

        response = self.client.get(reverse("core:client_360", args=[self.company.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client 360")
        self.assertContains(response, self.company.name)
        self.assertContains(response, "Commercial")
        self.assertContains(response, "GST Review")
        self.assertContains(response, "Next Actions")
        self.assertContains(response, "Close GST review")
        self.assertContains(response, "GSTR-3B May review")
        self.assertContains(response, "GST ASMT response")
        self.assertContains(response, "Upload purchase bills")

    def test_ca_command_center_surfaces_tds_return_readiness_blockers(self):
        fy_start, quarter = default_return_period(timezone.localdate())
        period_start, _period_end = quarter_dates(fy_start, quarter)
        section = TDSSection.objects.create(
            company=self.company,
            nature="TDS",
            section_code="194J",
            description="Professional fees",
            threshold=Decimal("30000.00"),
            rate_individual=Decimal("10.00"),
            rate_company=Decimal("10.00"),
        )
        TDSEntry.objects.create(
            company=self.company,
            section=section,
            deductee_ledger=self.vendor,
            transaction_date=period_start,
            deductee_type="Company",
            deductible_amount=Decimal("50000.00"),
            rate_applied=Decimal("10.00"),
            tds_amount=Decimal("5000.00"),
            pan_number="BADPAN",
            is_deposited=False,
        )

        response = self.client.get(reverse("core:ca_command_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TDS Return Readiness")
        self.assertContains(response, "TDS return blockers open")
        self.assertContains(response, "Open TDS Workpaper")

    def test_practice_task_can_be_completed_from_work_queue(self):
        task = PracticeTask.objects.create(
            company=self.company,
            title="File GSTR-3B",
            task_type=PracticeTask.TYPE_GST,
            assigned_to=self.user,
        )

        response = self.client.post(
            reverse("core:practice_task_set_status", args=[task.pk]),
            {"status": PracticeTask.STATUS_DONE},
        )

        self.assertRedirects(response, reverse("core:practice_tasks"))
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)
        self.assertIsNotNone(task.completed_at)

    def test_accounting_close_workbench_flags_blockers_and_creates_tasks(self):
        Voucher.objects.create(
            company=self.company,
            date=date(2026, 4, 12),
            voucher_type="Sales",
            status="DRAFT",
            narration="Draft sales close blocker",
        )
        statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=date(2026, 4, 30),
        )
        BankStatementRow.objects.create(
            statement=statement,
            date=date(2026, 4, 18),
            description="Unreconciled receipt",
            debit=Decimal("0.00"),
            credit=Decimal("5000.00"),
            balance=Decimal("5000.00"),
            row_number=1,
        )

        url = f"{reverse('core:accounting_close')}?period=2026-04&company={self.company.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Voucher approval")
        self.assertContains(response, "Bank reconciliation")
        self.assertContains(response, "Critical")

        export_response = self.client.get(f"{url}&export=csv")
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("Client,Period,Close Score,Close Status,Check Code", export_text)
        self.assertIn("voucher_approval", export_text)
        self.assertIn("bank_reconciliation", export_text)

        create_response = self.client.post(
            reverse("core:accounting_close"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "create_tasks",
            },
        )

        self.assertRedirects(
            create_response,
            f"{reverse('core:accounting_close')}?period=2026-04&company={self.company.pk}",
        )
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"CLOSE:{self.company.pk}:2026-04:voucher_approval",
            ).exists()
        )
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"CLOSE:{self.company.pk}:2026-04:bank_reconciliation",
            ).exists()
        )

    def test_filing_readiness_flags_blockers_and_creates_tasks(self):
        Voucher.objects.create(
            company=self.company,
            date=date(2026, 4, 12),
            voucher_type="Sales",
            status="DRAFT",
            narration="Draft sales filing blocker",
        )
        statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=date(2026, 4, 30),
        )
        BankStatementRow.objects.create(
            statement=statement,
            date=date(2026, 4, 18),
            description="Unreconciled receipt",
            debit=Decimal("0.00"),
            credit=Decimal("5000.00"),
            balance=Decimal("5000.00"),
            row_number=1,
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload April GST invoice",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=date(2026, 4, 25),
            requested_by=self.user,
        )

        url = f"{reverse('core:filing_readiness')}?period=2026-04&company={self.company.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Filing Readiness")
        self.assertContains(response, "Books close readiness")
        self.assertContains(response, "Bank reconciliation")
        self.assertContains(response, "Client document chase")

        export_response = self.client.get(f"{url}&export=csv")
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("Client,Period,Readiness Score,Readiness Status,Check Code", export_text)
        self.assertIn("close_workbench", export_text)
        self.assertIn("client_documents", export_text)

        create_response = self.client.post(
            reverse("core:filing_readiness"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "create_tasks",
            },
        )

        self.assertRedirects(
            create_response,
            f"{reverse('core:filing_readiness')}?period=2026-04&company={self.company.pk}",
        )
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"READY:{self.company.pk}:2026-04:close_workbench",
            ).exists()
        )

    def test_filing_readiness_signoff_persists_snapshot(self):
        response = self.client.post(
            reverse("core:filing_readiness"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "review_status",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Ready for filing.",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('core:filing_readiness')}?period=2026-04&company={self.company.pk}",
        )
        review = GSTPeriodReview.objects.get(company=self.company, period_start=date(2026, 4, 1))
        self.assertEqual(review.status, GSTPeriodReview.STATUS_SIGNED_OFF)
        self.assertEqual(review.reviewed_by, self.user)
        self.assertIn("filing_readiness", review.summary_snapshot)
        self.assertEqual(review.summary_snapshot["filing_readiness"]["company_id"], self.company.pk)

    def test_filing_review_center_blocks_waives_and_approves_for_filing(self):
        PortalGSTR2BEntry.objects.create(
            company=self.company,
            gstin="27ABCDE1234F1Z5",
            supplier_name="Review Supplier",
            invoice_number="2B-REVIEW-001",
            invoice_date=date(2026, 4, 15),
            taxable_value=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            match_status="missing_in_books",
        )

        url = f"{reverse('core:filing_review_center')}?period=2026-04&company={self.company.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Filing Review Center")
        self.assertContains(response, "GST filing readiness")
        self.assertContains(response, "Critical")

        blocked = self.client.post(
            reverse("core:filing_review_center"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "review_type": FilingReview.TYPE_GST_MONTHLY,
                "action": "approve",
                "notes": "Trying to approve before waiver.",
            },
        )

        self.assertRedirects(
            blocked,
            f"{reverse('core:filing_review_center')}?period=2026-04&company={self.company.pk}&review_type={FilingReview.TYPE_GST_MONTHLY}",
        )
        self.assertFalse(
            FilingReview.objects.filter(
                company=self.company,
                period_start=date(2026, 4, 1),
                status=FilingReview.STATUS_APPROVED,
            ).exists()
        )

        for code in ("close_workbench", "gst_readiness"):
            self.client.post(
                reverse("core:filing_review_center"),
                {
                    "company_id": self.company.pk,
                    "period": "2026-04",
                    "review_type": FilingReview.TYPE_GST_MONTHLY,
                    "action": "waive",
                    "code": code,
                    "waiver_note": "Supplier invoice will be booked in next period after client confirmation.",
                },
            )
        review = FilingReview.objects.get(company=self.company, period_start=date(2026, 4, 1))
        self.assertIn("close_workbench", review.waived_blockers)
        self.assertIn("gst_readiness", review.waived_blockers)

        approved = self.client.post(
            reverse("core:filing_review_center"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "review_type": FilingReview.TYPE_GST_MONTHLY,
                "action": "approve",
                "notes": "Approved with documented waiver.",
            },
        )

        self.assertRedirects(
            approved,
            f"{reverse('core:filing_review_center')}?period=2026-04&company={self.company.pk}&review_type={FilingReview.TYPE_GST_MONTHLY}",
        )
        review.refresh_from_db()
        self.assertEqual(review.status, FilingReview.STATUS_APPROVED)
        self.assertEqual(review.approved_by, self.user)
        self.assertTrue(review.blocker_snapshot["approval"]["ready_to_file"])
        self.assertEqual(
            set(
                ComplianceFiling.objects.filter(
                    company=self.company,
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30),
                ).values_list("status", flat=True)
            ),
            {ComplianceFiling.STATUS_READY_FOR_REVIEW},
        )

    def test_gst_filing_pack_requires_review_and_generates_downloads(self):
        self.customer.gstin = "27ABCDE1234F1Z5"
        self.customer.save(update_fields=["gstin"])

        invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 12),
            place_of_supply="27",
        )
        tax_group = AccountGroup.objects.create(
            company=self.company,
            name="Duties and Taxes",
            nature="Tax",
        )
        cgst = Ledger.objects.create(
            company=self.company,
            name="Output CGST",
            account_group=tax_group,
        )
        sgst = Ledger.objects.create(
            company=self.company,
            name="Output SGST",
            account_group=tax_group,
        )
        igst = Ledger.objects.create(
            company=self.company,
            name="Output IGST",
            account_group=tax_group,
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=cgst,
            entry_type="CR",
            amount=Decimal("90.00"),
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=sgst,
            entry_type="CR",
            amount=Decimal("90.00"),
        )
        invoice.approve(None)

        b2c_customer = Ledger.objects.create(
            company=self.company,
            name="B2C Interstate Customer",
            account_group=self.asset_group,
        )
        b2cl_invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 13),
            place_of_supply="29",
        )
        VoucherItem.objects.create(
            voucher=b2cl_invoice,
            ledger=b2c_customer,
            entry_type="DR",
            amount=Decimal("118000.00"),
        )
        VoucherItem.objects.create(
            voucher=b2cl_invoice,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("100000.00"),
        )
        VoucherItem.objects.create(
            voucher=b2cl_invoice,
            ledger=igst,
            entry_type="CR",
            amount=Decimal("18000.00"),
        )
        b2cl_invoice.approve(None)

        url = f"{reverse('core:gst_filing_pack')}?period=2026-04&company={self.company.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GST Filing Pack")
        self.assertContains(response, "Review Pending")

        blocked = self.client.post(
            reverse("core:gst_filing_pack"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "generate_pack",
            },
        )

        self.assertRedirects(blocked, url)
        self.assertFalse(GSTFilingPack.objects.filter(company=self.company).exists())

        FilingReview.objects.create(
            company=self.company,
            review_type=FilingReview.TYPE_GST_MONTHLY,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            status=FilingReview.STATUS_APPROVED,
            readiness_score=100,
            risk_score=0,
            approved_by=self.user,
        )

        generated = self.client.post(
            reverse("core:gst_filing_pack"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "generate_pack",
                "notes": "Ready for filing.",
            },
        )

        self.assertRedirects(generated, url)
        pack = GSTFilingPack.objects.get(company=self.company, period_start=date(2026, 4, 1))
        self.assertEqual(pack.status, GSTFilingPack.STATUS_READY)
        self.assertEqual(pack.generated_by, self.user)
        self.assertEqual(pack.summary_snapshot["gstr1"]["b2b_count"], 1)
        self.assertEqual(pack.summary_snapshot["gstr1"]["b2cl_count"], 1)
        self.assertEqual(pack.summary_snapshot["gstr1"]["b2cl_threshold"], "100000.00")

        xlsx = self.client.get(
            reverse("core:gst_filing_pack_download", args=["xlsx"]),
            {"period": "2026-04", "company": self.company.pk},
        )
        self.assertEqual(xlsx.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            xlsx["Content-Type"],
        )

        draft_json = self.client.get(
            reverse("core:gst_filing_pack_download", args=["json"]),
            {"period": "2026-04", "company": self.company.pk},
        )
        self.assertEqual(draft_json.status_code, 200)
        self.assertEqual(draft_json.json()["gstr1"]["b2b"][0]["ctin"], "27ABCDE1234F1Z5")

        portal_json = self.client.get(
            reverse("core:gst_filing_pack_download", args=["gstr1"]),
            {"period": "2026-04", "company": self.company.pk},
        )
        self.assertEqual(portal_json.status_code, 200)
        portal_payload = portal_json.json()
        self.assertEqual(portal_payload["gstin"], self.company.gstin)
        self.assertEqual(portal_payload["b2b"][0]["ctin"], "27ABCDE1234F1Z5")
        self.assertEqual(portal_payload["b2b"][0]["inv"][0]["itms"][0]["itm_det"]["txval"], 1000.0)
        self.assertEqual(portal_payload["b2cl"][0]["pos"], "29")
        self.assertEqual(portal_payload["b2cl"][0]["inv"][0]["val"], 118000.0)

        filed = self.client.post(
            reverse("core:gst_filing_pack"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "mark_filed",
                "arn_ack_number": "ARN-GST-APR-001",
            },
        )
        self.assertRedirects(filed, url)
        pack.refresh_from_db()
        self.assertEqual(pack.status, GSTFilingPack.STATUS_FILED)
        self.assertEqual(pack.arn_ack_number, "ARN-GST-APR-001")
        self.assertEqual(pack.filed_by, self.user)

    def test_gst_post_filing_center_tracks_arns_and_notice_room(self):
        GSTFilingPack.objects.create(
            company=self.company,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            status=GSTFilingPack.STATUS_READY,
            generated_by=self.user,
        )
        url = f"{reverse('core:gst_post_filing')}?period=2026-04&company={self.company.pk}"

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GST Post-Filing Center")
        self.assertContains(response, "Post-Filing Controls")

        saved = self.client.post(
            reverse("core:gst_post_filing"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "save_tracker",
                "gstr1_status": GSTPostFilingTracker.STATUS_ACCEPTED,
                "gstr1_arn": "ARN-GSTR1-APR",
                "gstr1_filed_at": "2026-05-11 10:15",
                "gstr3b_status": GSTPostFilingTracker.STATUS_ACCEPTED,
                "gstr3b_arn": "ARN-GSTR3B-APR",
                "gstr3b_filed_at": "2026-05-20 12:30",
                "ims_status": GSTPostFilingTracker.IMS_COMPLETED,
                "payment_status": GSTPostFilingTracker.PAYMENT_PAID,
                "payment_challan_reference": "CIN-APR-001",
                "payment_date": "2026-05-20",
                "itc_at_risk": "180.00",
                "portal_evidence_reference": "Evidence/GST/Apr-2026",
                "notes": "ARN and challan verified.",
            },
        )

        self.assertRedirects(saved, url)
        tracker = GSTPostFilingTracker.objects.get(company=self.company, period_start=date(2026, 4, 1))
        self.assertEqual(tracker.gstr3b_arn, "ARN-GSTR3B-APR")
        self.assertEqual(tracker.payment_challan_reference, "CIN-APR-001")
        pack = GSTFilingPack.objects.get(company=self.company, period_start=date(2026, 4, 1))
        self.assertEqual(pack.status, GSTFilingPack.STATUS_FILED)
        self.assertEqual(pack.arn_ack_number, "ARN-GSTR3B-APR")
        self.assertTrue(
            ComplianceFiling.objects.filter(
                company=self.company,
                filing_type=ComplianceFiling.TYPE_GSTR3B,
                status=ComplianceFiling.STATUS_FILED,
                arn_ack_number="ARN-GSTR3B-APR",
            ).exists()
        )

        evidence_upload = self.client.post(
            reverse("core:gst_post_filing"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "upload_evidence",
                "evidence_title": "GSTR-3B acknowledgement",
                "evidence_type": GSTEvidenceDocument.TYPE_GSTR3B_ACK,
                "return_type": GSTEvidenceDocument.RETURN_GSTR3B,
                "evidence_arn_ack_number": "ARN-GSTR3B-APR",
                "external_reference": "Evidence/GST/Apr-2026/GSTR3B.pdf",
                "evidence_file": SimpleUploadedFile(
                    "gstr3b-ack.pdf",
                    b"%PDF-1.4\nGST acknowledgement",
                    content_type="application/pdf",
                ),
            },
        )

        self.assertRedirects(evidence_upload, url)
        evidence = GSTEvidenceDocument.objects.get(company=self.company, title="GSTR-3B acknowledgement")
        self.assertEqual(evidence.tracker, tracker)
        self.assertEqual(evidence.arn_ack_number, "ARN-GSTR3B-APR")

        created_notice = self.client.post(
            reverse("core:gst_post_filing"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "create_notice",
                "notice_title": "DRC-01 response",
                "notice_related_return": ComplianceFiling.TYPE_GSTR3B,
                "notice_reference": "DRC01-APR",
                "notice_issue_date": "2026-04-29",
                "notice_due_date": "2026-05-15",
                "notice_status": ComplianceNotice.STATUS_DATA_PENDING,
                "notice_priority": PracticeTask.PRIORITY_CRITICAL,
                "notice_description": "Liability clarification requested.",
            },
        )

        self.assertRedirects(created_notice, url)
        notice = ComplianceNotice.objects.get(company=self.company, reference_number="DRC01-APR")
        self.assertEqual(notice.related_filing.filing_type, ComplianceFiling.TYPE_GSTR3B)
        self.assertIsNotNone(notice.related_task)

        updated_notice = self.client.post(
            reverse("core:gst_post_filing"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "action": "update_notice",
                "notice_id": notice.pk,
                "status": ComplianceNotice.STATUS_CLOSED,
                "portal_status": "Responded",
                "response_summary": "Response filed with acknowledgement.",
            },
        )

        self.assertRedirects(updated_notice, url)
        notice.refresh_from_db()
        self.assertEqual(notice.status, ComplianceNotice.STATUS_CLOSED)
        self.assertEqual(notice.closed_by, self.user)

        dashboard = self.client.get(f"{reverse('core:gst_post_filing_dashboard')}?period=2026-04")
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "GST Post-Filing Dashboard")
        self.assertContains(dashboard, "Evidence: 1")

    def test_bank_reconciliation_bulk_creates_selected_suggested_vouchers(self):
        rent = Ledger.objects.create(
            company=self.company,
            name="Rent Expense",
            account_group=self.expense_group,
        )
        statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=date(2026, 4, 30),
        )
        row = BankStatementRow.objects.create(
            statement=statement,
            date=date(2026, 4, 15),
            description="OFFICE RENT PAYMENT",
            debit=Decimal("1200.00"),
            credit=Decimal("0.00"),
            row_number=1,
        )

        self.client.post(reverse("core:bank_statement_auto_match", args=[statement.pk]))
        row.refresh_from_db()
        self.assertEqual(row.suggested_ledger, rent)
        self.assertGreaterEqual(row.match_confidence, 70)

        response = self.client.post(
            reverse("core:bank_statement_bulk_action", args=[statement.pk]),
            {
                "action": "create_selected_vouchers",
                "row_ids": [row.pk],
            },
        )

        self.assertRedirects(response, reverse("core:bank_statement_detail", args=[statement.pk]))
        row.refresh_from_db()
        self.assertTrue(row.is_reconciled)
        self.assertIsNotNone(row.matched_voucher)
        self.assertEqual(row.matched_voucher.status, "APPROVED")
        self.assertEqual(row.matched_voucher.voucher_type, "Payment")

    def test_voucher_quality_flags_issues_and_creates_tasks(self):
        for _ in range(2):
            voucher = Voucher.objects.create(
                company=self.company,
                voucher_type="Purchase",
                date=date(2026, 4, 18),
                source_reference="SUP-INV-42",
                place_of_supply="27",
            )
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=self.purchase,
                entry_type="DR",
                amount=Decimal("15000.00"),
            )
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=self.vendor,
                entry_type="CR",
                amount=Decimal("15000.00"),
            )
            voucher.approve(None)

        tax_group = AccountGroup.objects.create(
            company=self.company,
            name="Voucher Quality GST Output",
            nature="Tax",
        )
        cgst = Ledger.objects.create(company=self.company, name="Quality CGST", account_group=tax_group)
        sgst = Ledger.objects.create(company=self.company, name="Quality SGST", account_group=tax_group)
        sales_voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 19),
            place_of_supply="27",
        )
        VoucherItem.objects.create(
            voucher=sales_voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=sales_voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
            stock_item=self.stock_item,
            quantity=Decimal("1.000"),
            rate=Decimal("1000.00"),
        )
        VoucherItem.objects.create(voucher=sales_voucher, ledger=cgst, entry_type="CR", amount=Decimal("90.00"))
        VoucherItem.objects.create(voucher=sales_voucher, ledger=sgst, entry_type="CR", amount=Decimal("90.00"))
        Voucher.objects.filter(pk=sales_voucher.pk).update(
            status="APPROVED",
            total_tax=Decimal("180.00"),
            cgst_amount=Decimal("90.00"),
            sgst_amount=Decimal("90.00"),
        )

        url = (
            f"{reverse('vouchers:quality')}"
            "?start_date=2026-04-01&end_date=2026-04-30&status=all"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Duplicate invoice reference")
        self.assertContains(response, "Supporting document missing")
        self.assertContains(response, "HSN/SAC missing on stock item")

        export_response = self.client.get(f"{url}&export=csv")
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("Voucher,Date,Type,Severity,Issue Code,Issue,Message", export_text)
        self.assertIn("duplicate_source_reference", export_text)
        self.assertIn("missing_hsn_sac", export_text)

        create_response = self.client.post(url, {"action": "create_tasks"})

        self.assertRedirects(create_response, url)
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"VQ:{self.company.pk}:2026-04-01:2026-04-30:duplicate_source_reference",
            ).exists()
        )

    def test_compliance_calendar_generates_filings_and_tasks(self):
        output = StringIO()

        call_command(
            "generate_compliance_calendar",
            months=1,
            from_date="2026-04-01",
            company_id=[self.company.pk],
            stdout=output,
        )

        filings = ComplianceFiling.objects.filter(company=self.company)
        self.assertEqual(filings.count(), 4)
        self.assertEqual(
            set(filings.values_list("filing_type", flat=True)),
            {
                ComplianceFiling.TYPE_GSTR1,
                ComplianceFiling.TYPE_GST_IMS,
                ComplianceFiling.TYPE_GSTR3B,
                ComplianceFiling.TYPE_TDS_PAYMENT,
            },
        )
        self.assertEqual(PracticeTask.objects.filter(company=self.company, compliance_filings__isnull=False).count(), 4)

    def test_operational_backup_manifest_has_hashes_and_media_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media_root = root / "media"
            backup_dir = root / "backups"
            media_root.mkdir()
            (media_root / "evidence.txt").write_text("backup evidence", encoding="utf-8")

            with override_settings(MEDIA_ROOT=media_root):
                output = StringIO()
                call_command("export_operational_backup", output_dir=str(backup_dir), stdout=output)

            manifest_path = next(backup_dir.glob("akshaya-manifest-*.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["backup_schema_version"], 2)
        self.assertEqual(len(manifest["data_sha256"]), 64)
        self.assertGreater(manifest["data_size"], 0)
        self.assertEqual(manifest["media_file_count"], 1)
        self.assertEqual(len(manifest["media_files"][0]["sha256"]), 64)

    def test_compliance_calendar_ui_previews_and_generates_work(self):
        url = reverse("core:compliance_calendar")
        payload = {
            "companies": [self.company.pk],
            "from_date": "2026-06-01",
            "months": "1",
            "assigned_to": "",
            "reviewer": "",
            "include_ims": "on",
            "include_gstr1": "on",
            "include_gstr3b": "on",
            "include_tds_payment": "on",
            "include_tds_returns": "on",
            "ims_review_day": "10",
            "gstr1_day": "11",
            "gstr3b_day": "20",
            "tds_payment_day": "7",
            "gstr9_due": "",
            "gstr9c_due": "",
            "itr_due": "",
            "tax_audit_due": "",
            "mca_aoc4_due": "",
            "mca_mgt7_due": "",
        }

        preview = self.client.post(url, {**payload, "action": "preview"})

        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Will create")
        self.assertEqual(ComplianceFiling.objects.filter(company=self.company).count(), 0)

        generated = self.client.post(url, {**payload, "action": "generate"})

        self.assertEqual(generated.status_code, 200)
        filings = ComplianceFiling.objects.filter(company=self.company)
        self.assertEqual(filings.count(), 6)
        self.assertEqual(
            set(filings.values_list("filing_type", flat=True)),
            {
                ComplianceFiling.TYPE_GST_IMS,
                ComplianceFiling.TYPE_GSTR1,
                ComplianceFiling.TYPE_GSTR3B,
                ComplianceFiling.TYPE_TDS_PAYMENT,
                ComplianceFiling.TYPE_TDS_24Q,
                ComplianceFiling.TYPE_TDS_26Q,
            },
        )
        self.assertEqual(PracticeTask.objects.filter(company=self.company, compliance_filings__isnull=False).count(), 6)

        export_response = self.client.get(url, {"export": "csv"})
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("Client,Filing,Type,Priority,Status,Due Date,Days Overdue", export_text)
        self.assertIn("GSTR-1", export_text)
        self.assertIn("TDS 26Q", export_text)

    def test_compliance_filing_can_be_marked_filed_from_cross_client_view(self):
        filing = ComplianceFiling.objects.create(
            company=self.company,
            title="GSTR-3B - April 2026",
            filing_type=ComplianceFiling.TYPE_GSTR3B,
            due_date=date(2026, 5, 20),
            assigned_to=self.user,
        )

        response = self.client.post(
            reverse("core:compliance_filing_set_status", args=[filing.pk]),
            {"status": ComplianceFiling.STATUS_FILED},
        )

        self.assertRedirects(response, reverse("core:compliance_filings"))
        filing.refresh_from_db()
        self.assertEqual(filing.status, ComplianceFiling.STATUS_FILED)
        self.assertEqual(filing.filed_by, self.user)
        self.assertIsNotNone(filing.related_task)
        self.assertEqual(filing.related_task.status, PracticeTask.STATUS_DONE)

    def test_compliance_notice_can_be_closed_from_cross_client_view(self):
        notice = ComplianceNotice.objects.create(
            company=self.company,
            title="GST notice response",
            notice_type=ComplianceNotice.TYPE_GST,
            response_due_date=date(2026, 5, 25),
            assigned_to=self.user,
        )

        response = self.client.post(
            reverse("core:compliance_notice_set_status", args=[notice.pk]),
            {"status": ComplianceNotice.STATUS_CLOSED},
        )

        self.assertRedirects(response, reverse("core:compliance_notices"))
        notice.refresh_from_db()
        self.assertEqual(notice.status, ComplianceNotice.STATUS_CLOSED)
        self.assertEqual(notice.closed_by, self.user)
        self.assertIsNotNone(notice.related_task)
        self.assertEqual(notice.related_task.status, PracticeTask.STATUS_DONE)

    def test_gst_workbench_can_create_period_filings(self):
        response = self.client.post(
            reverse("core:gst_workbench_create_filings"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('core:gst_workbench')}?period=2026-04&company={self.company.pk}",
        )
        filings = ComplianceFiling.objects.filter(
            company=self.company,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
        )
        self.assertEqual(filings.count(), 3)
        self.assertEqual(
            set(filings.values_list("filing_type", flat=True)),
            {
                ComplianceFiling.TYPE_GST_IMS,
                ComplianceFiling.TYPE_GSTR1,
                ComplianceFiling.TYPE_GSTR3B,
            },
        )
        self.assertEqual(PracticeTask.objects.filter(company=self.company, task_type=PracticeTask.TYPE_GST).count(), 3)

    def test_gst_workbench_signoff_creates_period_review_snapshot(self):
        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Checked sales, ITC, and 2B.",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('core:gst_workbench')}?period=2026-04&company={self.company.pk}",
        )
        review = GSTPeriodReview.objects.get(company=self.company, period_start=date(2026, 4, 1))
        self.assertEqual(review.status, GSTPeriodReview.STATUS_SIGNED_OFF)
        self.assertEqual(review.reviewed_by, self.user)
        self.assertEqual(review.notes, "Checked sales, ITC, and 2B.")
        self.assertIn("risk_score", review.summary_snapshot)
        audit = AuditLog.objects.filter(
            company=self.company,
            model_name="GSTPeriodReview",
            record_id=review.pk,
            action=AuditLog.ACTION_CREATE,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.new_data["status"], GSTPeriodReview.STATUS_SIGNED_OFF)

    def test_gst_workbench_signoff_blocks_unresolved_guardrails(self):
        PortalGSTR2BEntry.objects.create(
            company=self.company,
            gstin="27ABCDE1234F1Z5",
            supplier_name="Smoke Supplier",
            invoice_number="2B-PENDING",
            invoice_date=date(2026, 4, 15),
            taxable_value=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            match_status="matched",
            action_status="pending",
        )

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off too early.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=date(2026, 4, 1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

    def test_gst_workbench_blocks_e_invoice_signoff_when_irn_deadline_crossed(self):
        self.company.e_invoice_enabled = True
        self.company.e_invoice_reporting_deadline_days = 1
        self.company.e_invoice_warning_days = 0
        self.company.save(update_fields=[
            "e_invoice_enabled",
            "e_invoice_reporting_deadline_days",
            "e_invoice_warning_days",
        ])
        invoice_date = timezone.localdate() - timedelta(days=2)
        period_value = invoice_date.strftime("%Y-%m")
        self.customer.gstin = "27ABCDE1234F1Z5"
        self.customer.save(update_fields=["gstin", "updated_at"])
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=invoice_date,
            total_tax=Decimal("180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1180.00"),
        )
        Voucher.objects.filter(pk=voucher.pk).update(status="APPROVED")

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, period_value]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "E-Invoice IRN Missing")
        self.assertContains(detail, "E-invoice IRP deadline crossed")

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": period_value,
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off without IRN.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, period_value]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=invoice_date.replace(day=1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

    def test_gst_workbench_does_not_require_irn_for_b2c_sales(self):
        self.company.e_invoice_enabled = True
        self.company.e_invoice_reporting_deadline_days = 1
        self.company.e_invoice_warning_days = 0
        self.company.save(update_fields=[
            "e_invoice_enabled",
            "e_invoice_reporting_deadline_days",
            "e_invoice_warning_days",
        ])
        invoice_date = timezone.localdate() - timedelta(days=2)
        period_value = invoice_date.strftime("%Y-%m")
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=invoice_date,
            place_of_supply="27",
            total_tax=Decimal("180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1180.00"),
        )
        Voucher.objects.filter(pk=voucher.pk).update(status="APPROVED")

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, period_value]))

        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "E-Invoice IRN Missing")
        self.assertNotContains(detail, "Sales invoices without IRN")

    def test_gst_workbench_blocks_sales_without_place_of_supply(self):
        Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 10),
            status="APPROVED",
            total_tax=Decimal("180.00"),
        )

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Sales POS Missing")
        self.assertContains(detail, "Sales invoices missing place of supply")

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off without POS.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=date(2026, 4, 1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

    def test_gst_workbench_blocks_sales_stock_items_without_hsn(self):
        tax_group = AccountGroup.objects.create(
            company=self.company,
            name="GST Output Ledgers",
            nature="Tax",
        )
        cgst = Ledger.objects.create(company=self.company, name="CGST Output", account_group=tax_group)
        sgst = Ledger.objects.create(company=self.company, name="SGST Output", account_group=tax_group)
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 10),
            place_of_supply="27",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
            stock_item=self.stock_item,
            quantity=Decimal("1.000"),
            rate=Decimal("1000.00"),
        )
        VoucherItem.objects.create(voucher=voucher, ledger=cgst, entry_type="CR", amount=Decimal("90.00"))
        VoucherItem.objects.create(voucher=voucher, ledger=sgst, entry_type="CR", amount=Decimal("90.00"))
        Voucher.objects.filter(pk=voucher.pk).update(
            status="APPROVED",
            total_tax=Decimal("180.00"),
            cgst_amount=Decimal("90.00"),
            sgst_amount=Decimal("90.00"),
        )

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "HSN/SAC Missing")
        self.assertContains(detail, "Sales stock items missing HSN/SAC")

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off without HSN.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=date(2026, 4, 1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

    def test_gst_workbench_blocks_itc_180_day_unpaid_purchase(self):
        tax_group = AccountGroup.objects.create(
            company=self.company,
            name="GST Input Ledgers",
            nature="Tax",
        )
        input_tax = Ledger.objects.create(
            company=self.company,
            name="Input GST",
            account_group=tax_group,
        )
        purchase_date = timezone.localdate() - timedelta(days=181)
        period_value = timezone.localdate().strftime("%Y-%m")
        purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=purchase_date,
            total_tax=Decimal("180.00"),
            is_itc_claimed=True,
            outstanding_amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=input_tax,
            entry_type="DR",
            amount=Decimal("180.00"),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("1180.00"),
        )
        Voucher.objects.filter(pk=purchase.pk).update(
            status="APPROVED",
            total_tax=Decimal("180.00"),
            is_itc_claimed=True,
            outstanding_amount=Decimal("1180.00"),
        )

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, period_value]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "ITC 180-Day Reversal")
        self.assertContains(detail, "ITC 180-day payment reversal due")

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": period_value,
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off without ITC reversal.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, period_value]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=timezone.localdate().replace(day=1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

        task_response = self.client.post(
            reverse("core:gst_workbench_exception_task"),
            {
                "company_id": self.company.pk,
                "period": period_value,
                "kind": "itc_180_reversal_due",
                "object_id": purchase.pk,
                "action_type": "resolve",
            },
        )
        self.assertRedirects(
            task_response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, period_value]),
        )
        task = PracticeTask.objects.get(company=self.company, reference=f"GSTEX:ITC180:{purchase.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("ITC 180-day reversal", task.title)

    def test_gst_workbench_blocks_rcm_purchase_without_tax_amount(self):
        purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 12),
            reverse_charge=True,
            status="APPROVED",
            total_tax=Decimal("0.00"),
        )

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "RCM Tax Missing")
        self.assertContains(detail, "RCM purchases missing GST tax")

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off without RCM tax.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=date(2026, 4, 1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

        task_response = self.client.post(
            reverse("core:gst_workbench_exception_task"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "kind": "rcm_missing_tax",
                "object_id": purchase.pk,
                "action_type": "resolve",
            },
        )
        self.assertRedirects(
            task_response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        task = PracticeTask.objects.get(company=self.company, reference=f"GSTEX:RCM:{purchase.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("RCM tax", task.title)

    def test_gst_workbench_blocks_high_value_movement_without_eway_bill(self):
        invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 12),
            dispatch_pincode=400001,
            ship_to_pincode=400002,
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("60000.00"),
        )
        VoucherItem.objects.create(
            voucher=invoice,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("60000.00"),
        )
        Voucher.objects.filter(pk=invoice.pk).update(status="APPROVED")

        detail = self.client.get(reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "E-Way Bill Missing")
        self.assertContains(detail, "E-way bill details missing")

        response = self.client.post(
            reverse("core:gst_workbench_signoff"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "status": GSTPeriodReview.STATUS_SIGNED_OFF,
                "notes": "Trying to sign off without e-way bill.",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        self.assertFalse(
            GSTPeriodReview.objects.filter(
                company=self.company,
                period_start=date(2026, 4, 1),
                status=GSTPeriodReview.STATUS_SIGNED_OFF,
            ).exists()
        )

        task_response = self.client.post(
            reverse("core:gst_workbench_exception_task"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "kind": "eway_bill_missing",
                "object_id": invoice.pk,
                "action_type": "resolve",
            },
        )
        self.assertRedirects(
            task_response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        task = PracticeTask.objects.get(company=self.company, reference=f"GSTEX:EWAY:{invoice.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("e-way bill", task.title)

    def test_gst_workbench_execution_pack_downloads_ready_irp_and_eway_jsons(self):
        self.company.e_invoice_enabled = True
        self.company.address = "Seller Street, Mumbai, Maharashtra"
        self.company.save(update_fields=["e_invoice_enabled", "address"])
        self.customer.gstin = "27ABCDE1234F1Z5"
        self.customer.address = "Buyer Street, Mumbai, Maharashtra"
        self.customer.save(update_fields=["gstin", "address", "updated_at"])
        tax_group = AccountGroup.objects.create(
            company=self.company,
            name="Duties and Taxes",
            nature="Tax",
        )
        cgst = Ledger.objects.create(company=self.company, name="Output CGST", account_group=tax_group)
        sgst = Ledger.objects.create(company=self.company, name="Output SGST", account_group=tax_group)
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 10),
            status="PENDING",
            place_of_supply="27",
            cgst_amount=Decimal("9000.00"),
            sgst_amount=Decimal("9000.00"),
            total_tax=Decimal("18000.00"),
            dispatch_pincode=400001,
            ship_to_pincode=400002,
            transport_mode="1",
            transport_distance_km=25,
            vehicle_number="MH12AB1234",
            vehicle_type="R",
        )
        VoucherItem.objects.create(voucher=voucher, ledger=self.customer, entry_type="DR", amount=Decimal("118000.00"))
        VoucherItem.objects.create(voucher=voucher, ledger=self.sales, entry_type="CR", amount=Decimal("100000.00"))
        VoucherItem.objects.create(voucher=voucher, ledger=cgst, entry_type="CR", amount=Decimal("9000.00"))
        VoucherItem.objects.create(voucher=voucher, ledger=sgst, entry_type="CR", amount=Decimal("9000.00"))
        voucher.approve(None)

        response = self.client.get(
            reverse("core:gst_workbench_execution_pack", args=[self.company.pk, "2026-04"])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertIn("manifest.csv", names)
            self.assertTrue(any(name.startswith("e_invoice/") for name in names))
            self.assertTrue(any(name.startswith("e_way_bill/") for name in names))
            manifest = archive.read("manifest.csv").decode("utf-8")
            self.assertIn("e_invoice", manifest)
            self.assertIn("e_way_bill", manifest)

    def test_gst_period_detail_updates_2b_action(self):
        entry = PortalGSTR2BEntry.objects.create(
            company=self.company,
            gstin="27ABCDE1234F1Z5",
            supplier_name="Smoke Supplier",
            invoice_number="2B-001",
            invoice_date=date(2026, 4, 15),
            taxable_value=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            match_status="missing_in_books",
        )

        response = self.client.post(
            reverse("core:gst_workbench_2b_action", args=[entry.pk]),
            {"action_status": "pending", "action_note": "Asked client"},
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        entry.refresh_from_db()
        self.assertEqual(entry.action_status, "pending")
        self.assertEqual(entry.action_note, "Asked client")

    def test_gst_period_detail_exports_exception_csv(self):
        PortalGSTR2BEntry.objects.create(
            company=self.company,
            gstin="27ABCDE1234F1Z5",
            supplier_name="Smoke Supplier",
            invoice_number="2B-CSV",
            invoice_date=date(2026, 4, 15),
            taxable_value=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            match_status="missing_in_books",
        )

        response = self.client.get(
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
            {"export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode()
        self.assertIn("Sign-off Blocker", content)
        self.assertIn("2B Missing in Books", content)
        self.assertIn("GSTEX:2B:", content)

    def test_gst_period_detail_creates_exception_task(self):
        entry = PortalGSTR2BEntry.objects.create(
            company=self.company,
            gstin="27ABCDE1234F1Z5",
            supplier_name="Smoke Supplier",
            invoice_number="2B-002",
            invoice_date=date(2026, 4, 20),
            taxable_value=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            match_status="missing_in_books",
        )

        response = self.client.post(
            reverse("core:gst_workbench_exception_task"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "kind": "2b_missing_in_books",
                "object_id": entry.pk,
                "action_type": "client_chase",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        task = PracticeTask.objects.get(company=self.company, reference=f"GSTEX:2B:{entry.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertEqual(task.period_start, date(2026, 4, 1))
        self.assertTrue(task.title.startswith("Client chase:"))
        audit = AuditLog.objects.filter(
            company=self.company,
            model_name="PracticeTask",
            record_id=task.pk,
            action=AuditLog.ACTION_CREATE,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.new_data["kind"], "2b_missing_in_books")
        doc_request = ClientDocumentRequest.objects.get(company=self.company, source_reference=f"GSTEX:2B:{entry.pk}")
        self.assertEqual(doc_request.status, ClientDocumentRequest.STATUS_OPEN)
        self.assertEqual(doc_request.related_task, task)

    def test_gst_period_detail_creates_e_invoice_exception_task(self):
        self.company.e_invoice_enabled = True
        self.company.save(update_fields=["e_invoice_enabled"])
        invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 20),
            status="APPROVED",
            total_tax=Decimal("180.00"),
        )

        response = self.client.post(
            reverse("core:gst_workbench_exception_task"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "kind": "e_invoice_missing_irn",
                "object_id": invoice.pk,
                "action_type": "resolve",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        task = PracticeTask.objects.get(company=self.company, reference=f"GSTEX:EINV:{invoice.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("Generate IRN", task.title)

    def test_gst_period_detail_creates_sales_readiness_task(self):
        invoice = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 20),
            status="APPROVED",
            total_tax=Decimal("180.00"),
        )

        response = self.client.post(
            reverse("core:gst_workbench_exception_task"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
                "kind": "sales_missing_pos",
                "object_id": invoice.pk,
                "action_type": "resolve",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        task = PracticeTask.objects.get(company=self.company, reference=f"GSTEX:SALEPOS:{invoice.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("Add place of supply", task.title)

    def test_gst_workbench_chase_pack_creates_period_client_requests(self):
        entry = PortalGSTR2BEntry.objects.create(
            company=self.company,
            gstin="27ABCDE1234F1Z5",
            supplier_name="Smoke Supplier",
            invoice_number="2B-003",
            invoice_date=date(2026, 4, 18),
            taxable_value=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),
            match_status="missing_in_books",
        )
        purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 19),
            status="APPROVED",
            narration="Vendor ITC not visible in 2B",
            total_tax=Decimal("90.00"),
            is_itc_claimed=False,
        )

        response = self.client.post(
            reverse("core:gst_workbench_chase_pack"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        references = {
            f"GSTEX:2B:{entry.pk}",
            f"GSTEX:VCH:{purchase.pk}",
        }
        self.assertEqual(
            set(ClientDocumentRequest.objects.filter(company=self.company).values_list("source_reference", flat=True)),
            references,
        )
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference__in=references).count(),
            2,
        )
        self.assertEqual(
            ClientDocumentRequest.objects.filter(company=self.company, related_task__isnull=False).count(),
            2,
        )

        self.client.post(
            reverse("core:gst_workbench_chase_pack"),
            {
                "company_id": self.company.pk,
                "period": "2026-04",
            },
        )
        self.assertEqual(ClientDocumentRequest.objects.filter(company=self.company).count(), 2)
        self.assertEqual(PracticeTask.objects.filter(company=self.company, reference__in=references).count(), 2)

    def test_client_document_request_upload_creates_ocr_submission(self):
        task = PracticeTask.objects.create(
            company=self.company,
            title="Client chase: Missing invoice",
            task_type=PracticeTask.TYPE_GST,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
        )
        doc_request = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload missing GST invoice",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=date(2026, 5, 3),
            source_reference="GSTEX:2B:upload-test",
            related_task=task,
        )
        upload = SimpleUploadedFile(
            "invoice.pdf",
            b"%PDF-1.4\n% smoke\n",
            content_type="application/pdf",
        )

        response = self.client.post(
            reverse("portal:document_request_upload", args=[doc_request.token]),
            {"file": upload, "response_note": "Uploaded invoice"},
        )

        self.assertEqual(response.status_code, 200)
        doc_request.refresh_from_db()
        self.assertEqual(doc_request.status, ClientDocumentRequest.STATUS_UPLOADED)
        self.assertEqual(doc_request.response_note, "Uploaded invoice")
        self.assertIsNotNone(doc_request.uploaded_submission)
        self.assertEqual(OCRSubmission.objects.filter(company=self.company).count(), 1)
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_IN_PROGRESS)

    def test_gst_workbench_can_close_uploaded_client_evidence(self):
        task = PracticeTask.objects.create(
            company=self.company,
            title="Client chase: GST evidence",
            task_type=PracticeTask.TYPE_GST,
            status=PracticeTask.STATUS_IN_PROGRESS,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            created_by=self.user,
        )
        doc_request = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload GST evidence",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            due_date=date(2026, 4, 25),
            source_reference="GSTEX:UPLOAD:1",
            related_task=task,
            requested_by=self.user,
        )

        response = self.client.post(
            reverse("core:gst_workbench_document_request_status", args=[doc_request.pk]),
            {
                "period": "2026-04",
                "action": "close_reviewed",
            },
        )

        self.assertRedirects(
            response,
            reverse("core:gst_workbench_detail", args=[self.company.pk, "2026-04"]),
        )
        doc_request.refresh_from_db()
        self.assertEqual(doc_request.status, ClientDocumentRequest.STATUS_CLOSED)
        self.assertIsNotNone(doc_request.closed_at)
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)

    def test_client_request_room_filters_creates_tasks_and_closes_requests(self):
        doc_request = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload April GST invoice",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=date(2026, 4, 25),
            source_reference="REQ-APR-GST",
            requested_by=self.user,
        )

        url = f"{reverse('portal:client_requests')}?company={self.company.pk}&status=active"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client Requests")
        self.assertContains(response, "Upload April GST invoice")
        self.assertContains(response, "REQ-APR-GST")

        export_response = self.client.get(f"{url}&export=csv")
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("Company,Client,Client Email,Client WhatsApp,Request", export_text)
        self.assertIn("Upload April GST invoice", export_text)
        self.assertIn("REQ-APR-GST", export_text)
        self.assertIn("/portal/request/", export_text)

        create_response = self.client.post(
            url,
            {
                "action": "create_tasks",
                "request_ids": [doc_request.pk],
            },
        )

        self.assertRedirects(create_response, url)
        doc_request.refresh_from_db()
        self.assertIsNotNone(doc_request.related_task)
        self.assertEqual(doc_request.related_task.task_type, PracticeTask.TYPE_DOCUMENT)

        close_response = self.client.post(
            url,
            {
                "action": "close",
                "request_ids": [doc_request.pk],
            },
        )

        self.assertRedirects(close_response, url)
        doc_request.refresh_from_db()
        self.assertEqual(doc_request.status, ClientDocumentRequest.STATUS_CLOSED)
        doc_request.related_task.refresh_from_db()
        self.assertEqual(doc_request.related_task.status, PracticeTask.STATUS_DONE)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_client_request_reminders_mark_sent_and_close_uploaded(self):
        task = PracticeTask.objects.create(
            company=self.company,
            title="Client request: overdue invoice",
            task_type=PracticeTask.TYPE_DOCUMENT,
        )
        overdue = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload overdue GST invoice",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=date(2026, 4, 25),
            source_reference="REM-OVERDUE",
            requested_by=self.user,
            related_task=task,
        )
        email_request = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Upload GST purchase support",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            due_date=date(2026, 5, 2),
            recipient_email="client-reminder@example.com",
            recipient_whatsapp_number="+919876543210",
            requested_by=self.user,
        )
        uploaded = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Uploaded bank statement",
            document_type=ClientDocumentRequest.TYPE_BANK,
            due_date=date(2026, 5, 1),
            status=ClientDocumentRequest.STATUS_UPLOADED,
            requested_by=self.user,
        )

        url = f"{reverse('portal:client_request_reminders')}?company={self.company.pk}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client Request Reminders")
        self.assertContains(response, "Upload overdue GST invoice")
        self.assertContains(response, "client-reminder@example.com")
        self.assertContains(response, "https://wa.me/919876543210")
        self.assertContains(response, "Uploaded bank statement")

        export_response = self.client.get(f"{url}&export=csv")
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("Company,Kind,Request,Document Type,Status,Due Date,Email To,WhatsApp URL", export_text)
        self.assertIn("Upload overdue GST invoice", export_text)
        self.assertIn("client-reminder@example.com", export_text)
        self.assertIn("https://wa.me/919876543210", export_text)

        mark_response = self.client.post(
            url,
            {
                "action": "mark_reminded",
                "request_ids": [overdue.pk],
            },
        )

        self.assertRedirects(mark_response, url)
        overdue.refresh_from_db()
        self.assertEqual(overdue.reminder_count, 1)
        self.assertIsNotNone(overdue.last_reminded_at)
        task.refresh_from_db()
        self.assertIn("Reminder sent by", task.description)

        email_response = self.client.post(
            url,
            {
                "action": "send_email",
                "request_ids": [email_request.pk],
            },
        )

        self.assertRedirects(email_response, url)
        email_request.refresh_from_db()
        self.assertEqual(email_request.reminder_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["client-reminder@example.com"])
        self.assertIn("Upload link:", mail.outbox[0].body)

        close_response = self.client.post(
            url,
            {
                "action": "close_uploaded",
                "request_ids": [uploaded.pk],
            },
        )

        self.assertRedirects(close_response, url)
        uploaded.refresh_from_db()
        self.assertEqual(uploaded.status, ClientDocumentRequest.STATUS_CLOSED)

    def test_client_request_creator_uses_template_and_returns_upload_link(self):
        portal_user = PortalUser.objects.create(
            name="Smoke Portal Client",
            email="smoke-portal-client@example.com",
            password="unused",
            linked_ledger=self.customer,
        )
        create_url = (
            f"{reverse('portal:client_request_create')}"
            f"?company={self.company.pk}&template=gst_invoice"
        )
        form_page = self.client.get(create_url)

        self.assertEqual(form_page.status_code, 200)
        self.assertContains(form_page, "Upload GST purchase invoices")

        response = self.client.post(
            reverse("portal:client_request_create"),
            {
                "template": "gst_invoice",
                "company": self.company.pk,
                "portal_user": portal_user.pk,
                "document_type": ClientDocumentRequest.TYPE_GST_INVOICE,
                "title": "Upload April GST invoices",
                "due_date": "2026-04-25",
                "source_reference": "MANUAL-APR-GST",
                "notes": "Upload all pending supplier invoices.",
                "create_task": "on",
            },
        )

        doc_request = ClientDocumentRequest.objects.get(source_reference="MANUAL-APR-GST")
        self.assertRedirects(
            response,
            f"{reverse('portal:client_request_create')}?created={doc_request.pk}",
        )
        self.assertEqual(doc_request.requested_by, self.user)
        self.assertEqual(doc_request.portal_user, portal_user)
        self.assertIsNotNone(doc_request.related_task)

        created_page = self.client.get(f"{reverse('portal:client_request_create')}?created={doc_request.pk}")
        self.assertContains(created_page, "Upload link ready")
        self.assertContains(created_page, reverse("portal:document_request_upload", args=[doc_request.token]))

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="System <system@example.com>",
    )
    def test_client_request_campaign_creates_deduped_requests_tasks_and_emails(self):
        portal_user_one = PortalUser.objects.create(
            name="Campaign Client One",
            email="campaign-one@example.com",
            password="unused",
            linked_ledger=self.customer,
        )
        customer_two = Ledger.objects.create(
            company=self.company,
            name="Campaign Customer Two",
            account_group=self.asset_group,
            whatsapp_number="+919876543210",
        )
        portal_user_two = PortalUser.objects.create(
            name="Campaign Client Two",
            email="campaign-two@example.com",
            password="unused",
            linked_ledger=customer_two,
        )

        form_page = self.client.get(
            f"{reverse('portal:client_request_campaign')}?company={self.company.pk}&template=gst_invoice"
        )
        self.assertEqual(form_page.status_code, 200)
        self.assertContains(form_page, "Upload GST purchase invoices")

        payload = {
            "template": "gst_invoice",
            "company": self.company.pk,
            "portal_users": [portal_user_one.pk, portal_user_two.pk],
            "document_type": ClientDocumentRequest.TYPE_GST_INVOICE,
            "title": "Upload May GST invoices",
            "due_date": "2026-05-25",
            "source_reference_prefix": "GST-MAY-2026",
            "notes": "Upload all pending GST purchase invoices.",
            "create_task": "on",
            "send_email": "on",
        }
        response = self.client.post(reverse("portal:client_request_campaign"), payload)

        self.assertRedirects(
            response,
            f"{reverse('portal:client_requests')}?company={self.company.pk}&status=active",
        )
        requests = ClientDocumentRequest.objects.filter(
            company=self.company,
            source_reference__startswith="GST-MAY-2026",
        ).order_by("portal_user__email")
        self.assertEqual(requests.count(), 2)
        self.assertTrue(all(req.related_task_id for req in requests))
        self.assertTrue(all(req.reminder_count == 1 for req in requests))
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            AuditLog.objects.filter(
                company=self.company,
                model_name="ClientDocumentRequest",
                new_data__source="client_request_campaign",
            ).count(),
            2,
        )
        self.assertEqual(
            ClientDocumentRequest.objects.get(portal_user=portal_user_two).recipient_whatsapp_number,
            "+919876543210",
        )

        second_response = self.client.post(reverse("portal:client_request_campaign"), payload)
        self.assertRedirects(
            second_response,
            f"{reverse('portal:client_requests')}?company={self.company.pk}&status=active",
        )
        self.assertEqual(
            ClientDocumentRequest.objects.filter(company=self.company, source_reference__startswith="GST-MAY-2026").count(),
            2,
        )
