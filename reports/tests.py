import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserCompanyAccess
from integrations.models import StatutoryExportLog
from inventory.models import HSN_SAC, StockItem, StockLedger, TaxRate
from ledger.models import AccountGroup, Ledger
from reports.utils import get_balance_sheet, get_gst_report, get_msme_payable_watch, get_profit_loss
from vouchers.models import Voucher, VoucherItem


class FinancialReportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Report Test Co",
            gstin="27AAAAA0000A1Z5",
            short_code="RT",
        )
        self.user = get_user_model().objects.create_superuser(
            email="reports@example.com",
            password="reports-pass",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()
        self.asset_group = AccountGroup.objects.create(
            company=self.company, name="Assets", nature="Asset"
        )
        self.liability_group = AccountGroup.objects.create(
            company=self.company, name="Liabilities", nature="Liability"
        )
        self.equity_group = AccountGroup.objects.create(
            company=self.company, name="Equity", nature="Equity"
        )
        self.expense_group = AccountGroup.objects.create(
            company=self.company, name="Expenses", nature="Expense"
        )
        self.tax_group = AccountGroup.objects.create(
            company=self.company, name="Tax", nature="Tax"
        )

        self.cash = Ledger.objects.create(
            company=self.company,
            name="Cash",
            account_group=self.asset_group,
            opening_balance=Decimal("-50.00"),
        )
        self.supplier = Ledger.objects.create(
            company=self.company, name="Supplier", account_group=self.liability_group
        )
        self.capital = Ledger.objects.create(
            company=self.company,
            name="Capital",
            account_group=self.equity_group,
            opening_balance=Decimal("50.00"),
        )
        self.purchase = Ledger.objects.create(
            company=self.company, name="Purchases", account_group=self.expense_group
        )
        self.input_tax = Ledger.objects.create(
            company=self.company,
            name="Input GST",
            account_group=self.tax_group,
            opening_balance=Decimal("-18.00"),
        )
        self.output_tax = Ledger.objects.create(
            company=self.company,
            name="Output GST",
            account_group=self.tax_group,
            opening_balance=Decimal("18.00"),
        )
        self.income_group = AccountGroup.objects.create(
            company=self.company, name="Income", nature="Income"
        )

    def test_balance_sheet_includes_equity_tax_and_closing_stock(self):
        item = StockItem.objects.create(company=self.company, name="Inventory Item")
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
        )
        VoucherItem.objects.create(
            voucher=voucher, ledger=self.purchase, entry_type="DR", amount=Decimal("100.00")
        )
        VoucherItem.objects.create(
            voucher=voucher, ledger=self.supplier, entry_type="CR", amount=Decimal("100.00")
        )
        voucher.approve(None)
        StockLedger.objects.create(
            stock_item=item,
            voucher=voucher,
            date=voucher.date,
            quantity=Decimal("2.000"),
            rate=Decimal("50.00"),
        )

        data = get_balance_sheet(self.company, date(2026, 4, 30))
        asset_names = {row["name"] for row in data["asset_items"]}
        liability_names = {row["name"] for row in data["liability_items"]}

        self.assertIn("Input GST", asset_names)
        self.assertIn("Closing Stock", asset_names)
        self.assertIn("Capital", liability_names)
        self.assertIn("Output GST", liability_names)
        self.assertEqual(data["difference"], Decimal("0.00"))

    def test_profit_loss_offsets_expensed_purchases_with_closing_stock(self):
        item = StockItem.objects.create(company=self.company, name="Inventory Item")
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
        )
        VoucherItem.objects.create(
            voucher=voucher, ledger=self.purchase, entry_type="DR", amount=Decimal("100.00")
        )
        VoucherItem.objects.create(
            voucher=voucher, ledger=self.supplier, entry_type="CR", amount=Decimal("100.00")
        )
        voucher.approve(None)
        StockLedger.objects.create(
            stock_item=item,
            voucher=voucher,
            date=voucher.date,
            quantity=Decimal("2.000"),
            rate=Decimal("50.00"),
        )

        data = get_profit_loss(self.company, date(2026, 4, 1), date(2026, 4, 30))

        self.assertEqual(data["closing_stock"], Decimal("100.00"))
        self.assertEqual(data["net_profit"], Decimal("0.00"))

    def test_day_book_exports_filtered_csv(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 5),
        )
        VoucherItem.objects.create(voucher=voucher, ledger=self.cash, entry_type="DR", amount=Decimal("100.00"))
        VoucherItem.objects.create(voucher=voucher, ledger=self.capital, entry_type="CR", amount=Decimal("100.00"))
        voucher.approve(None)

        response = self.client.get(
            reverse("reports:day_book"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30", "export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Date,Voucher No,Type,Particulars,Amount,Running Balance", csv_text)
        self.assertIn("2026-04-05", csv_text)
        self.assertIn("Receipt", csv_text)
        self.assertIn("100.00", csv_text)

    def test_cash_flow_forecast_exports_csv(self):
        due_date = date.today() + timedelta(days=5)
        sale = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date.today(),
            due_date=due_date,
            outstanding_amount=Decimal("1500.00"),
        )
        purchase = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date.today(),
            due_date=due_date,
            outstanding_amount=Decimal("500.00"),
        )
        Voucher.objects.filter(pk=sale.pk).update(outstanding_amount=Decimal("1500.00"))
        Voucher.objects.filter(pk=purchase.pk).update(outstanding_amount=Decimal("500.00"))

        response = self.client.get(reverse("reports:cash_flow_forecast"), {"export": "csv"})

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Date,Incoming Receivables,Outgoing Payables,Daily Net,Cumulative Position", csv_text)
        self.assertIn(due_date.isoformat(), csv_text)
        self.assertIn("1500.00", csv_text)
        self.assertIn("500.00", csv_text)

    def test_msme_payable_watch_flags_overdue_and_due_soon(self):
        self.supplier.is_msme = True
        self.supplier.msme_reg_number = "UDYAM-MH-00-0000001"
        self.supplier.credit_days = 30
        self.supplier.save(update_fields=["is_msme", "msme_reg_number", "credit_days"])
        overdue = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
            outstanding_amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=overdue,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=overdue,
            ledger=self.supplier,
            entry_type="CR",
            amount=Decimal("1000.00"),
        )
        Voucher.objects.filter(pk=overdue.pk).update(status="APPROVED", outstanding_amount=Decimal("1000.00"))
        due_soon = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 5, 1),
            outstanding_amount=Decimal("500.00"),
        )
        VoucherItem.objects.create(
            voucher=due_soon,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("500.00"),
        )
        VoucherItem.objects.create(
            voucher=due_soon,
            ledger=self.supplier,
            entry_type="CR",
            amount=Decimal("500.00"),
        )
        Voucher.objects.filter(pk=due_soon.pk).update(status="APPROVED", outstanding_amount=Decimal("500.00"))

        watch = get_msme_payable_watch(self.company, as_of_date=date(2026, 5, 26))

        self.assertEqual(watch["summary"]["total_count"], 2)
        self.assertEqual(watch["summary"]["overdue_count"], 1)
        self.assertEqual(watch["summary"]["due_soon_count"], 1)
        self.assertEqual(watch["summary"]["total_outstanding"], Decimal("1500.00"))
        self.assertEqual(watch["rows"][0]["due_date"], date(2026, 5, 1))

        response = self.client.get(reverse("reports:msme_overdue"), {
            "as_of_date": "2026-05-26",
            "export": "csv",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        csv_body = response.content.decode()
        self.assertIn("Voucher,Vendor,MSME Registration", csv_body)
        self.assertIn("Supplier", csv_body)

    def test_gst_report_uses_customer_ledger_gstin_for_b2b_sales(self):
        customer = Ledger.objects.create(
            company=self.company,
            name="Registered Customer",
            account_group=self.asset_group,
            gstin="29BBBBB1111B1Z5",
        )
        sales = Ledger.objects.create(
            company=self.company,
            name="Sales",
            account_group=self.income_group,
        )
        igst = Ledger.objects.create(
            company=self.company,
            name="IGST Output",
            account_group=self.tax_group,
        )
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 10),
            place_of_supply="29",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=igst,
            entry_type="CR",
            amount=Decimal("180.00"),
        )
        Voucher.objects.filter(pk=voucher.pk).update(status="APPROVED")

        report = get_gst_report(self.company, date(2026, 4, 1), date(2026, 4, 30))

        self.assertEqual(len(report["b2b_rows"]), 1)
        row = report["b2b_rows"][0]
        self.assertEqual(row["buyer_gstin"], "29BBBBB1111B1Z5")
        self.assertEqual(row["buyer_name"], "Registered Customer")
        self.assertEqual(row["supply_type"], "B2B")
        self.assertEqual(row["place_of_supply"], "29")
        self.assertEqual(row["portal_supply_type"], "INTER")
        self.assertEqual(row["rate"], Decimal("18.00"))
        self.assertEqual(report["tot_out_igst"], Decimal("180.00"))
        self.assertEqual(report["doc_issue_summary"]["total_number"], 1)
        self.assertEqual(report["doc_issue_summary"]["net_issued"], 1)

        response = self.client.get(
            reverse("reports:gstr1_export"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30"},
        )
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode())
        self.assertEqual(payload["b2b"][0]["ctin"], "29BBBBB1111B1Z5")
        self.assertEqual(payload["b2b"][0]["inv"][0]["pos"], "29")
        self.assertEqual(payload["b2b"][0]["inv"][0]["itms"][0]["itm_det"]["rt"], 18.0)
        self.assertEqual(payload["doc_issue"]["doc_det"][0]["docs"][0]["totnum"], 1)
        self.assertEqual(payload["doc_issue"]["doc_det"][0]["docs"][0]["net_issue"], 1)
        export_log = StatutoryExportLog.objects.filter(
            company=self.company,
            export_type=StatutoryExportLog.TYPE_GSTR1_JSON,
        ).latest("created_at")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.generated_by, self.user)
        self.assertEqual(len(export_log.file_sha256), 64)
        self.assertEqual(export_log.validation_summary["b2b_rows"], 1)

        csv_response = self.client.get(
            reverse("reports:gst_report"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30", "export": "csv"},
        )
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("GSTR-1 Outward Supplies", csv_text)
        self.assertIn("Registered Customer", csv_text)
        self.assertIn("29BBBBB1111B1Z5", csv_text)
        self.assertIn("GSTR-3B Summary", csv_text)

    def test_gstr1_hsn_summary_uses_stock_item_classification(self):
        customer = Ledger.objects.create(
            company=self.company,
            name="Retail Customer",
            account_group=self.asset_group,
        )
        sales = Ledger.objects.create(
            company=self.company,
            name="Product Sales",
            account_group=self.income_group,
        )
        cgst = Ledger.objects.create(
            company=self.company,
            name="CGST Output",
            account_group=self.tax_group,
        )
        sgst = Ledger.objects.create(
            company=self.company,
            name="SGST Output",
            account_group=self.tax_group,
        )
        hsn = HSN_SAC.objects.create(code="847130", description="Portable computers")
        tax_rate = TaxRate.objects.create(rate=Decimal("18.00"), description="GST 18%")
        stock_item = StockItem.objects.create(
            company=self.company,
            name="Laptop",
            unit="Nos",
            hsn_sac=hsn,
            tax_rate=tax_rate,
        )
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 12),
            place_of_supply="27",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=customer,
            entry_type="DR",
            amount=Decimal("1180.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
            stock_item=stock_item,
            quantity=Decimal("2.000"),
            rate=Decimal("500.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=cgst,
            entry_type="CR",
            amount=Decimal("90.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=sgst,
            entry_type="CR",
            amount=Decimal("90.00"),
        )
        Voucher.objects.filter(pk=voucher.pk).update(status="APPROVED")

        report = get_gst_report(self.company, date(2026, 4, 1), date(2026, 4, 30))

        self.assertEqual(len(report["hsn_summary_rows"]), 1)
        row = report["hsn_summary_rows"][0]
        self.assertEqual(row["hsn_code"], "847130")
        self.assertEqual(row["uqc"], "NOS")
        self.assertEqual(row["quantity"], Decimal("2.000"))
        self.assertEqual(row["taxable_value"], Decimal("1000.00"))
        self.assertEqual(row["cgst"], Decimal("90.00"))
        self.assertEqual(row["sgst"], Decimal("90.00"))
        self.assertEqual(row["rate"], Decimal("18.00"))

        response = self.client.get(
            reverse("reports:gstr1_export"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30"},
        )
        payload = json.loads(response.content.decode())
        self.assertEqual(payload["hsn"]["data"][0]["hsn_sc"], "847130")
        self.assertEqual(payload["hsn"]["data"][0]["uqc"], "NOS")
        self.assertEqual(payload["hsn"]["data"][0]["txval"], 1000.0)

    def test_gstr1_export_splits_current_period_b2c_large_and_small(self):
        customer = Ledger.objects.create(
            company=self.company,
            name="Unregistered Customer",
            account_group=self.asset_group,
        )
        sales = Ledger.objects.create(
            company=self.company,
            name="B2C Sales",
            account_group=self.income_group,
        )
        igst = Ledger.objects.create(
            company=self.company,
            name="IGST Output B2C",
            account_group=self.tax_group,
        )
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 15),
            place_of_supply="29",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=customer,
            entry_type="DR",
            amount=Decimal("118000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=sales,
            entry_type="CR",
            amount=Decimal("100000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=igst,
            entry_type="CR",
            amount=Decimal("18000.00"),
        )
        Voucher.objects.filter(pk=voucher.pk).update(status="APPROVED")

        report = get_gst_report(self.company, date(2026, 4, 1), date(2026, 4, 30))

        self.assertEqual(report["b2cl_threshold"], Decimal("100000.00"))
        self.assertEqual(len(report["b2cl_rows"]), 1)
        self.assertEqual(len(report["b2cs_rows"]), 0)
        self.assertEqual(report["b2cl_rows"][0]["gstr1_bucket"], "B2CL")

        response = self.client.get(
            reverse("reports:gstr1_export"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30"},
        )
        payload = json.loads(response.content.decode())
        self.assertEqual(payload["b2cl"][0]["pos"], "29")
        self.assertEqual(payload["b2cl"][0]["inv"][0]["val"], 118000.0)
        self.assertEqual(payload["b2cs"], [])
        self.assertEqual(payload["_meta"]["b2cl_threshold"], 100000.0)

    def test_gstr1_export_aggregates_b2c_small_by_state_and_rate(self):
        customer = Ledger.objects.create(
            company=self.company,
            name="Walk-in Customer",
            account_group=self.asset_group,
        )
        sales = Ledger.objects.create(
            company=self.company,
            name="Small B2C Sales",
            account_group=self.income_group,
        )
        cgst = Ledger.objects.create(
            company=self.company,
            name="CGST Output Small",
            account_group=self.tax_group,
        )
        sgst = Ledger.objects.create(
            company=self.company,
            name="SGST Output Small",
            account_group=self.tax_group,
        )
        for taxable, tax in [(Decimal("1000.00"), Decimal("90.00")), (Decimal("500.00"), Decimal("45.00"))]:
            voucher = Voucher.objects.create(
                company=self.company,
                voucher_type="Sales",
                date=date(2026, 4, 16),
                place_of_supply="27",
            )
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=customer,
                entry_type="DR",
                amount=taxable + tax + tax,
            )
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=sales,
                entry_type="CR",
                amount=taxable,
            )
            VoucherItem.objects.create(voucher=voucher, ledger=cgst, entry_type="CR", amount=tax)
            VoucherItem.objects.create(voucher=voucher, ledger=sgst, entry_type="CR", amount=tax)
            Voucher.objects.filter(pk=voucher.pk).update(status="APPROVED")

        response = self.client.get(
            reverse("reports:gstr1_export"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30"},
        )

        payload = json.loads(response.content.decode())
        self.assertEqual(payload["b2cl"], [])
        self.assertEqual(len(payload["b2cs"]), 1)
        self.assertEqual(payload["b2cs"][0]["sply_ty"], "INTRA")
        self.assertEqual(payload["b2cs"][0]["pos"], "27")
        self.assertEqual(payload["b2cs"][0]["txval"], 1500.0)
        self.assertEqual(payload["b2cs"][0]["camt"], 135.0)
        self.assertEqual(payload["b2cs"][0]["samt"], 135.0)

    def test_gstr3b_export_writes_statutory_export_evidence(self):
        response = self.client.get(
            reverse("reports:gstr3b_export"),
            {"start_date": "2026-04-01", "end_date": "2026-04-30"},
        )

        self.assertEqual(response.status_code, 200)
        log = StatutoryExportLog.objects.get(
            company=self.company,
            export_type=StatutoryExportLog.TYPE_GSTR3B_JSON,
        )
        self.assertEqual(log.period_start, date(2026, 4, 1))
        self.assertEqual(log.period_end, date(2026, 4, 30))
        self.assertEqual(log.row_count, 1)
        self.assertEqual(len(log.file_sha256), 64)
