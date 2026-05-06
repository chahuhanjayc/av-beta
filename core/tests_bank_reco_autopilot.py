from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
import zipfile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.bank_reco_autopilot import build_bank_reco_autopilot
from core.models import AuditLog, BankStatement, BankStatementRow, Company, PracticeTask, UserCompanyAccess
from ledger.models import AccountGroup, Ledger
from portal.models import ClientDocumentRequest, PortalUser
from vouchers.models import Voucher, VoucherItem


class BankRecoAutopilotTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Bank Reco Co",
            gstin="27BANKR0000B1Z5",
            short_code="BRC",
        )
        self.no_statement_company = Company.objects.create(
            name="No Statement Co",
            gstin="27NOSTA0000N1Z5",
            short_code="NSC",
        )
        self.user = get_user_model().objects.create_superuser(
            email="bank-reco-admin@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.no_statement_company, role="Admin")
        self.asset_group = AccountGroup.objects.create(
            company=self.company,
            name="Bank Accounts",
            nature="Asset",
        )
        self.debtor_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Debtors",
            nature="Asset",
        )
        self.expense_group = AccountGroup.objects.create(
            company=self.company,
            name="Expenses",
            nature="Expense",
        )
        self.bank = Ledger.objects.create(
            company=self.company,
            name="Main Bank",
            account_group=self.asset_group,
        )
        self.customer = Ledger.objects.create(
            company=self.company,
            name="Bank Customer",
            account_group=self.debtor_group,
            email="bank-client@example.com",
            whatsapp_number="+91 98765 43210",
        )
        self.expense = Ledger.objects.create(
            company=self.company,
            name="Bank Charges",
            account_group=self.expense_group,
        )
        self.statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=date(2026, 4, 30),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_bank_reco_autopilot_builds_cross_client_exceptions(self):
        today = timezone.localdate()
        BankStatementRow.objects.create(
            statement=self.statement,
            date=today - timedelta(days=10),
            description="UPI credit from customer",
            credit=Decimal("1000.00"),
            suggested_ledger=self.customer,
            match_confidence=82,
            row_number=1,
        )
        BankStatementRow.objects.create(
            statement=self.statement,
            date=today,
            description="Possible duplicate charge",
            debit=Decimal("150.00"),
            potential_duplicate=True,
            row_number=2,
        )

        center = build_bank_reco_autopilot(
            Company.objects.filter(pk__in=[self.company.pk, self.no_statement_company.pk]),
            as_of_date=today,
            focus="all",
        )

        self.assertEqual(center["totals"]["company_count"], 2)
        self.assertEqual(center["totals"]["pending_count"], 2)
        self.assertEqual(center["totals"]["high_confidence_count"], 1)
        self.assertEqual(center["totals"]["duplicate_count"], 1)
        self.assertEqual(center["totals"]["no_statement_count"], 1)
        statuses = {row["company"].name: row["status"] for row in center["company_rows"]}
        self.assertEqual(statuses["Bank Reco Co"], "critical")
        self.assertEqual(statuses["No Statement Co"], "no_statement")

    def test_bank_reco_autopilot_renders_and_exports_csv(self):
        BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Unmatched bank receipt",
            credit=Decimal("750.00"),
            row_number=1,
        )

        response = self.client.get(reverse("core:bank_reco_autopilot"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bank Reco Autopilot")
        self.assertContains(response, "Statement Exception Queue")
        self.assertContains(response, "Bank Reco Co")

        csv_response = self.client.get(reverse("core:bank_reco_autopilot"), {"export": "csv", "focus": "all"})
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv; charset=utf-8")
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,Statement Date,Bank Account", csv_text)
        self.assertIn("Bank Reco Co", csv_text)

    def test_bank_reco_autopilot_exports_working_paper_zip(self):
        bank_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Unclear bank receipt for review pack",
            credit=Decimal("990.00"),
            match_confidence=72,
            match_reason="2 voucher candidates; manual review required",
            row_number=1,
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Clarify bank receipt",
            document_type=ClientDocumentRequest.TYPE_BANK,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            source_reference=f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}",
            requested_by=self.user,
            response_note="Receipt proof uploaded.",
        )

        response = self.client.get(reverse("core:bank_reco_autopilot"), {"export": "zip", "focus": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "README.txt",
                    "bank_reco_summary.csv",
                    "bank_reco_exceptions.csv",
                    "bank_reco_client_requests.csv",
                },
            )
            summary = archive.read("bank_reco_summary.csv").decode("utf-8")
            exceptions = archive.read("bank_reco_exceptions.csv").decode("utf-8")
            client_requests = archive.read("bank_reco_client_requests.csv").decode("utf-8")
            readme = archive.read("README.txt").decode("utf-8")

        self.assertIn("Reco Score", summary)
        self.assertIn("Unclear bank receipt for review pack", exceptions)
        self.assertIn(f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}", exceptions)
        self.assertIn("Uploaded", exceptions)
        self.assertIn("Clarify bank receipt", client_requests)
        self.assertIn("Receipt proof uploaded.", client_requests)
        self.assertIn("Bank Reconciliation Working Paper Pack", readme)

    def test_bank_reco_autopilot_creates_idempotent_tasks(self):
        BankStatementRow.objects.create(
            statement=self.statement,
            date=timezone.localdate() - timedelta(days=12),
            description="Old unreconciled row",
            debit=Decimal("250.00"),
            row_number=1,
        )

        response = self.client.post(reverse("core:bank_reco_autopilot"), {"action": "create_tasks", "focus": "all"})

        self.assertEqual(response.status_code, 302)
        task = PracticeTask.objects.get(company=self.company, reference=f"BANKAUTO:{self.company.pk}:STMT:{self.statement.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_BANK)
        self.assertIn("Pending rows: 1", task.description)

        self.client.post(reverse("core:bank_reco_autopilot"), {"action": "create_tasks", "focus": "all"})
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference=f"BANKAUTO:{self.company.pk}:STMT:{self.statement.pk}").count(),
            1,
        )

    def test_bank_reco_autopilot_creates_idempotent_client_requests_for_unclear_rows(self):
        PortalUser.objects.create(
            name="Bank Client",
            email="bank-client@example.com",
            password="hashed",
            linked_ledger=self.customer,
        )
        bank_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=timezone.localdate() - timedelta(days=9),
            description="Unknown UPI credit",
            credit=Decimal("875.00"),
            row_number=1,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "ask_client",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
            },
        )

        self.assertEqual(response.status_code, 302)
        doc_request = ClientDocumentRequest.objects.get(
            company=self.company,
            source_reference=f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}",
        )
        self.assertEqual(doc_request.document_type, ClientDocumentRequest.TYPE_BANK)
        self.assertEqual(doc_request.recipient_email, "bank-client@example.com")
        self.assertEqual(doc_request.status, ClientDocumentRequest.STATUS_OPEN)
        self.assertIsNotNone(doc_request.related_task)
        self.assertEqual(doc_request.related_task.task_type, PracticeTask.TYPE_DOCUMENT)

        self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "ask_client",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
            },
        )
        self.assertEqual(
            ClientDocumentRequest.objects.filter(
                company=self.company,
                source_reference=f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}",
            ).count(),
            1,
        )

        page = self.client.get(reverse("core:bank_reco_autopilot"), {"focus": "all"})
        self.assertContains(page, "1 waiting")

    def test_bank_reco_autopilot_tracks_uploaded_client_evidence_and_filters(self):
        bank_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=timezone.localdate() - timedelta(days=8),
            description="Unclear bank payment",
            debit=Decimal("625.00"),
            row_number=1,
        )
        ClientDocumentRequest.objects.create(
            company=self.company,
            title="Clarify bank payment",
            document_type=ClientDocumentRequest.TYPE_BANK,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            source_reference=f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}",
            requested_by=self.user,
        )

        center = build_bank_reco_autopilot(
            Company.objects.filter(pk=self.company.pk),
            as_of_date=timezone.localdate(),
            focus="all",
        )

        self.assertEqual(center["totals"]["client_request_uploaded_count"], 1)
        self.assertEqual(center["statement_rows"][0]["client_request_uploaded_count"], 1)

        filtered = self.client.get(reverse("core:bank_reco_autopilot"), {"focus": "client_uploaded"})
        self.assertContains(filtered, "1 uploaded")
        self.assertContains(filtered, "Bank Reco Co")

        csv_response = self.client.get(reverse("core:bank_reco_autopilot"), {"focus": "all", "export": "csv"})
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Open Client Requests,Uploaded Evidence", csv_text)

    def test_bank_reco_autopilot_closes_uploaded_evidence_and_task(self):
        bank_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=timezone.localdate() - timedelta(days=8),
            description="Unclear client bank receipt",
            credit=Decimal("725.00"),
            row_number=1,
        )
        task = PracticeTask.objects.create(
            company=self.company,
            title="Client request: bank proof",
            task_type=PracticeTask.TYPE_DOCUMENT,
            status=PracticeTask.STATUS_IN_PROGRESS,
            created_by=self.user,
        )
        doc_request = ClientDocumentRequest.objects.create(
            company=self.company,
            title="Clarify bank receipt",
            document_type=ClientDocumentRequest.TYPE_BANK,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            source_reference=f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}",
            requested_by=self.user,
            related_task=task,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "close_evidence",
                "focus": "client_uploaded",
                "work_ids": [f"statement:{self.statement.pk}"],
            },
        )

        self.assertEqual(response.status_code, 302)
        doc_request.refresh_from_db()
        self.assertEqual(doc_request.status, ClientDocumentRequest.STATUS_CLOSED)
        self.assertIsNotNone(doc_request.closed_at)
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)
        bank_row.refresh_from_db()
        self.assertIn("Client evidence reviewed", bank_row.match_reason)
        self.assertGreaterEqual(bank_row.match_confidence, 50)
        audit = AuditLog.objects.filter(
            company=self.company,
            model_name="ClientDocumentRequest",
            record_id=doc_request.pk,
            new_data__source="bank_reco_autopilot_close_evidence",
        ).first()
        self.assertIsNotNone(audit)

        center = build_bank_reco_autopilot(
            Company.objects.filter(pk=self.company.pk),
            as_of_date=timezone.localdate(),
            focus="all",
        )
        self.assertEqual(center["totals"]["client_request_uploaded_count"], 0)

    def test_bank_reco_autopilot_skips_client_requests_for_internally_actionable_rows(self):
        bank_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=timezone.localdate(),
            description="Bank charge",
            debit=Decimal("75.00"),
            suggested_ledger=self.expense,
            match_confidence=88,
            match_reason="Keyword match: BANK CHARGE",
            row_number=1,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "ask_client",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            ClientDocumentRequest.objects.filter(
                company=self.company,
                source_reference=f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}",
            ).exists()
        )

    def test_bank_reco_autopilot_posts_auto_ready_vouchers(self):
        bank_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Auto ready customer receipt",
            credit=Decimal("1250.00"),
            suggested_ledger=self.customer,
            match_confidence=88,
            match_reason="Ledger suggestion: Bank Customer",
            row_number=1,
        )
        task = PracticeTask.objects.create(
            company=self.company,
            title="Complete bank reconciliation",
            task_type=PracticeTask.TYPE_BANK,
            status=PracticeTask.STATUS_IN_PROGRESS,
            reference=f"BANKAUTO:{self.company.pk}:STMT:{self.statement.pk}",
            created_by=self.user,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "post_auto_ready",
                "focus": "auto_ready",
                "work_ids": [f"statement:{self.statement.pk}"],
            },
        )

        self.assertEqual(response.status_code, 302)
        bank_row.refresh_from_db()
        self.assertTrue(bank_row.is_reconciled)
        self.assertEqual(bank_row.match_confidence, 100)
        self.assertIn("auto-posted", bank_row.match_reason)
        voucher = bank_row.matched_voucher
        self.assertEqual(voucher.voucher_type, "Receipt")
        self.assertEqual(voucher.status, "APPROVED")
        self.assertEqual(voucher.source_system, "bank_reco_autopilot")
        self.assertEqual(voucher.source_reference, f"BANKROW:{self.company.pk}:ROW:{bank_row.pk}")
        self.assertTrue(
            VoucherItem.objects.filter(
                voucher=voucher,
                ledger=self.bank,
                entry_type="DR",
                amount=Decimal("1250.00"),
            ).exists()
        )
        self.assertTrue(
            VoucherItem.objects.filter(
                voucher=voucher,
                ledger=self.customer,
                entry_type="CR",
                amount=Decimal("1250.00"),
            ).exists()
        )
        audit = AuditLog.objects.filter(
            company=self.company,
            model_name="Voucher",
            record_id=voucher.pk,
            new_data__source="bank_reco_autopilot_auto_post",
        ).first()
        self.assertIsNotNone(audit)
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)
        self.assertIn("fully reconciled", task.description)
        task_audit = AuditLog.objects.filter(
            company=self.company,
            model_name="PracticeTask",
            record_id=task.pk,
            new_data__source="bank_reco_autopilot_auto_close_task",
        ).first()
        self.assertIsNotNone(task_audit)

    def test_bank_reco_autopilot_learns_ledger_from_previous_bank_posting(self):
        history_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 20),
            description="UPI RAZORPAY SETTLEMENT ABC123",
            credit=Decimal("1180.00"),
            suggested_ledger=self.customer,
            match_confidence=88,
            match_reason="Ledger suggestion: Bank Customer",
            row_number=1,
        )
        self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "post_auto_ready",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
            },
        )
        history_row.refresh_from_db()
        self.assertTrue(history_row.is_reconciled)

        new_row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="UPI RAZORPAY SETTLEMENT XYZ789",
            credit=Decimal("1325.00"),
            row_number=2,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "auto_match",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
                "statement_ids": [str(self.statement.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        new_row.refresh_from_db()
        self.assertFalse(new_row.is_reconciled)
        self.assertEqual(new_row.suggested_ledger, self.customer)
        self.assertGreaterEqual(new_row.match_confidence, 90)
        self.assertIn("Learned bank rule", new_row.match_reason)

    def test_bank_reco_autopilot_runs_auto_match_on_selected_statement(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 29),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("500.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("500.00"),
        )
        voucher.approve(None)
        row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Customer receipt",
            credit=Decimal("500.00"),
            row_number=1,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "auto_match",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
                "statement_ids": [str(self.statement.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertTrue(row.is_reconciled)
        self.assertEqual(row.matched_voucher, voucher)

    def test_bank_reco_autopilot_uses_reference_token_to_pick_same_amount_candidate(self):
        wrong_voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 29),
            narration="Receipt without bank reference",
            source_reference="RCPT-GENERIC",
        )
        VoucherItem.objects.create(
            voucher=wrong_voucher,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("900.00"),
        )
        VoucherItem.objects.create(
            voucher=wrong_voucher,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("900.00"),
        )
        wrong_voucher.approve(None)

        matched_voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 28),
            narration="Customer receipt against UTR HDFC77889901",
            source_reference="HDFC77889901",
        )
        VoucherItem.objects.create(
            voucher=matched_voucher,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("900.00"),
        )
        VoucherItem.objects.create(
            voucher=matched_voucher,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("900.00"),
        )
        matched_voucher.approve(None)

        row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="NEFT CR HDFC77889901 BANK CUSTOMER",
            credit=Decimal("900.00"),
            row_number=1,
        )

        response = self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "auto_match",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
                "statement_ids": [str(self.statement.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertTrue(row.is_reconciled)
        self.assertEqual(row.matched_voucher, matched_voucher)
        self.assertIn("Reference token match", row.match_reason)

    def test_bank_reco_autopilot_keeps_ambiguous_same_amount_candidates_pending(self):
        for index in range(2):
            voucher = Voucher.objects.create(
                company=self.company,
                voucher_type="Receipt",
                date=date(2026, 4, 29),
                narration=f"Generic receipt {index}",
            )
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=self.bank,
                entry_type="DR",
                amount=Decimal("650.00"),
            )
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=self.customer,
                entry_type="CR",
                amount=Decimal("650.00"),
            )
            voucher.approve(None)

        row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="CUSTOMER RECEIPT",
            credit=Decimal("650.00"),
            row_number=1,
        )

        self.client.post(
            reverse("core:bank_reco_autopilot"),
            {
                "action": "auto_match",
                "focus": "all",
                "work_ids": [f"statement:{self.statement.pk}"],
                "statement_ids": [str(self.statement.pk)],
            },
        )

        row.refresh_from_db()
        self.assertFalse(row.is_reconciled)
        self.assertGreaterEqual(row.match_confidence, 70)
        self.assertIn("voucher candidates", row.match_reason)
