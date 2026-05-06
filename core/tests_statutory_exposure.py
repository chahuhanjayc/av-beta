import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import (
    Company,
    CompanyStatutoryProfile,
    ComplianceNotice,
    PracticeTask,
    StatutoryRuleOverride,
    UserCompanyAccess,
)
from ledger.models import AccountGroup, Ledger
from tds.models import TDSEntry, TDSSection
from vouchers.models import Voucher, VoucherItem


class StatutoryExposureTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Exposure Test Co",
            gstin="27EXPOS0000E1Z5",
            tan="MUME00000E",
            short_code="EXP",
        )
        self.user = get_user_model().objects.create_user(
            email="statutory-exposure@example.com",
            password="exposure-pass",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        self.asset_group = AccountGroup.objects.create(company=self.company, name="Assets", nature="Asset")
        self.liability_group = AccountGroup.objects.create(company=self.company, name="Liabilities", nature="Liability")
        self.income_group = AccountGroup.objects.create(company=self.company, name="Income", nature="Income")
        self.expense_group = AccountGroup.objects.create(company=self.company, name="Expenses", nature="Expense")

        self.customer = Ledger.objects.create(
            company=self.company,
            name="Registered Customer",
            account_group=self.asset_group,
            gstin="27ABCDE1234F1Z5",
        )
        self.supplier = Ledger.objects.create(
            company=self.company,
            name="MSME Supplier",
            account_group=self.liability_group,
            is_msme=True,
            msme_reg_number="UDYAM-MH-00-0000001",
            credit_days=30,
        )
        self.sales = Ledger.objects.create(company=self.company, name="Sales", account_group=self.income_group)
        self.purchases = Ledger.objects.create(company=self.company, name="Purchases", account_group=self.expense_group)
        self.tds_payable = Ledger.objects.create(company=self.company, name="TDS Payable", account_group=self.liability_group)

    def _month_start(self, months_back):
        today = timezone.localdate()
        month_index = today.month - 1 - months_back
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        return date(year, month, 1)

    def _month_end(self, value):
        return value.replace(day=calendar.monthrange(value.year, value.month)[1])

    def _create_exposure_fixtures(self, period_start):
        period_end = self._month_end(period_start)
        sales_voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=period_start + timedelta(days=min(10, period_end.day - 1)),
            total_tax=Decimal("1800.00"),
            place_of_supply="27",
        )
        Voucher.objects.filter(pk=sales_voucher.pk).update(status="APPROVED")

        section = TDSSection.objects.create(
            company=self.company,
            section_code="194C",
            description="Contract payment",
            threshold=Decimal("0.00"),
            rate_company=Decimal("2.00"),
        )
        TDSEntry.objects.create(
            company=self.company,
            section=section,
            deductee_ledger=self.supplier,
            tds_ledger=self.tds_payable,
            transaction_date=period_start + timedelta(days=min(12, period_end.day - 1)),
            deductible_amount=Decimal("50000.00"),
            rate_applied=Decimal("2.00"),
            tds_amount=Decimal("1000.00"),
            is_deposited=False,
        )

        purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=timezone.localdate() - timedelta(days=70),
            outstanding_amount=Decimal("2500.00"),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=self.purchases,
            entry_type="DR",
            amount=Decimal("2500.00"),
        )
        VoucherItem.objects.create(
            voucher=purchase,
            ledger=self.supplier,
            entry_type="CR",
            amount=Decimal("2500.00"),
        )
        Voucher.objects.filter(pk=purchase.pk).update(status="APPROVED", outstanding_amount=Decimal("2500.00"))

        ComplianceNotice.objects.create(
            company=self.company,
            notice_type=ComplianceNotice.TYPE_GST,
            title="GST ASMT-10 response",
            reference_number="GST-NOTICE-1",
            response_due_date=timezone.localdate() + timedelta(days=2),
            status=ComplianceNotice.STATUS_RECEIVED,
        )

    def test_statutory_exposure_surfaces_cross_statute_items(self):
        period_start = self._month_start(2)
        self._create_exposure_fixtures(period_start)

        response = self.client.get(reverse("core:statutory_exposure"), {
            "period": period_start.strftime("%Y-%m"),
            "horizon": "60",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Statutory Deadline")
        self.assertContains(response, "GSTR-3B")
        self.assertContains(response, "TDS deposit exposure")
        self.assertContains(response, "MSME payment exposure")
        self.assertContains(response, "GST ASMT-10 response")
        self.assertContains(response, "Estimated GST, TDS, MSME, and notice exposure")

    def test_statutory_exposure_exports_visible_rows_to_csv(self):
        period_start = self._month_start(2)
        self._create_exposure_fixtures(period_start)

        response = self.client.get(reverse("core:statutory_exposure"), {
            "period": period_start.strftime("%Y-%m"),
            "horizon": "60",
            "export": "csv",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertIn("Company,Category,Severity,Status,Due Date,Title", body)
        self.assertIn("GSTR-3B", body)
        self.assertIn("TDS deposit exposure", body)
        self.assertIn("MSME payment exposure", body)

    def test_statutory_exposure_creates_idempotent_recovery_tasks(self):
        period_start = self._month_start(2)
        self._create_exposure_fixtures(period_start)
        params = {
            "period": period_start.strftime("%Y-%m"),
            "horizon": "60",
            "company": "all",
            "category": "all",
            "severity": "all",
            "q": "",
        }

        response = self.client.post(reverse("core:statutory_exposure"), params)

        self.assertEqual(response.status_code, 302)
        tasks = PracticeTask.objects.filter(company=self.company, reference__startswith="STATEX:")
        self.assertGreaterEqual(tasks.count(), 4)
        self.assertTrue(tasks.filter(title__icontains="GSTR-3B").exists())
        self.assertTrue(tasks.filter(task_type=PracticeTask.TYPE_TDS).exists())
        first_count = tasks.count()

        self.client.post(reverse("core:statutory_exposure"), params)

        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference__startswith="STATEX:").count(),
            first_count,
        )

    def test_statutory_exposure_uses_client_profile_and_due_date_override(self):
        period_start = self._month_start(2)
        self._create_exposure_fixtures(period_start)
        CompanyStatutoryProfile.objects.create(
            company=self.company,
            gst_return_frequency=CompanyStatutoryProfile.GST_FREQUENCY_QRMP,
            gstr1_frequency=CompanyStatutoryProfile.GSTR1_QUARTERLY,
            qrmp_group=CompanyStatutoryProfile.QRMP_GROUP_B,
            gstr3b_qrmp_due_day=24,
            gst_late_fee_per_day=Decimal("10.00"),
            gst_interest_rate_percent=Decimal("12.00"),
        )
        quarter_month = ((period_start.month - 1) // 3) * 3 + 1
        quarter_start = date(period_start.year, quarter_month, 1)
        quarter_end = self._month_end(date(period_start.year, quarter_month + 2, 1))
        due_year = quarter_end.year + 1 if quarter_end.month == 12 else quarter_end.year
        due_month = 1 if quarter_end.month == 12 else quarter_end.month + 1
        StatutoryRuleOverride.objects.create(
            company=self.company,
            rule_type=StatutoryRuleOverride.RULE_GSTR3B,
            period_start=quarter_start,
            period_end=quarter_end,
            original_due_date=date(due_year, due_month, 24),
            override_due_date=date(due_year, due_month, 25),
            late_fee_per_day=Decimal("0.00"),
            reason="GST due-date extension.",
            created_by=self.user,
        )

        response = self.client.get(reverse("core:statutory_exposure"), {
            "period": period_start.strftime("%Y-%m"),
            "horizon": "90",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GSTR-1 Q")
        self.assertContains(response, "GSTR-3B Q")
        self.assertContains(response, date(due_year, due_month, 25).strftime("%d %b %Y"))
        self.assertContains(response, "override:GSTR-3B")
