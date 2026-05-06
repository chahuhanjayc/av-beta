from datetime import date
from decimal import Decimal

from django.test import TestCase

from core.models import Company
from payroll.models import Employee, PayrollRun, Payslip, SalaryStructure
from payroll.views import _post_payroll_voucher
from vouchers.models import Voucher


class PayrollProductionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Payroll Co",
            gstin="27PPPPP0000P1Z5",
            short_code="PC",
        )

    def test_multiple_blank_employee_codes_are_allowed(self):
        Employee.objects.create(
            company=self.company,
            name="Asha Mehta",
            employee_code="",
            basic_salary=Decimal("10000.00"),
        )
        Employee.objects.create(
            company=self.company,
            name="Bharat Shah",
            employee_code="",
            basic_salary=Decimal("12000.00"),
        )

        self.assertEqual(Employee.objects.filter(company=self.company).count(), 2)

    def test_payroll_posting_creates_approved_balanced_voucher(self):
        employee = Employee.objects.create(
            company=self.company,
            name="Asha Mehta",
            employee_code="EMP001",
            basic_salary=Decimal("10000.00"),
        )
        structure = SalaryStructure.objects.create(
            company=self.company,
            name="Standard",
        )
        run = PayrollRun.objects.create(
            company=self.company,
            month=4,
            year=2026,
            status=PayrollRun.STATUS_PROCESSED,
        )
        payslip = Payslip(
            payroll_run=run,
            employee=employee,
            salary_structure=structure,
        )
        payslip.compute()
        payslip.save()

        voucher = _post_payroll_voucher(self.company, run, user=None)
        run.posted_voucher = voucher
        run.status = PayrollRun.STATUS_FINALIZED
        run.save(update_fields=["posted_voucher", "status"])

        self.assertEqual(voucher.status, "APPROVED")
        self.assertTrue(voucher.is_balanced())
        self.assertEqual(voucher.date, date(2026, 4, 30))
        self.assertTrue(
            voucher.items.filter(
                entry_type="DR",
                ledger__name="Salary Expense",
            ).exists()
        )
        self.assertTrue(
            voucher.items.filter(
                entry_type="CR",
                ledger__name="Salary Payable",
            ).exists()
        )
        self.assertEqual(
            Voucher.objects.get(pk=run.posted_voucher_id).status,
            "APPROVED",
        )
