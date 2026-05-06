"""
payroll/models.py — Phase 6: Payroll

Models:
  Employee        — Employee master (company-scoped)
  SalaryStructure — Template defining salary component percentages
  PayrollRun      — Monthly payroll batch
  Payslip         — Individual payslip per employee per run
"""

from decimal import Decimal
from django.db import models
from core.models import Company


class Employee(models.Model):
    GENDER_CHOICES = [("M", "Male"), ("F", "Female"), ("O", "Other")]

    company         = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="employees")
    employee_code   = models.CharField(max_length=20, blank=True)
    name            = models.CharField(max_length=200)
    designation     = models.CharField(max_length=100, blank=True)
    department      = models.CharField(max_length=100, blank=True)
    date_of_joining = models.DateField(null=True, blank=True)
    date_of_birth   = models.DateField(null=True, blank=True)
    gender          = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True)
    pan_number      = models.CharField(max_length=10, blank=True, verbose_name="PAN")
    uan_number      = models.CharField(max_length=12, blank=True, verbose_name="UAN (PF)")
    bank_account    = models.CharField(max_length=20, blank=True)
    ifsc_code       = models.CharField(max_length=11, blank=True, verbose_name="IFSC")
    # CTC / salary fields
    basic_salary    = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        help_text="Monthly Basic Salary (₹)."
    )
    hra             = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        help_text="Monthly HRA (₹). Set to 0 to auto-compute from salary structure."
    )
    # Statutory flags
    pf_applicable   = models.BooleanField(default=True, verbose_name="PF Applicable")
    esi_applicable  = models.BooleanField(default=False, verbose_name="ESI Applicable")
    tds_applicable  = models.BooleanField(default=False, verbose_name="TDS Applicable")
    is_active       = models.BooleanField(default=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Employee"
        verbose_name_plural = "Employees"
        ordering            = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "employee_code"],
                condition=~models.Q(employee_code=""),
                name="uniq_employee_code_per_company_when_set",
            )
        ]

    def __str__(self):
        code = f" [{self.employee_code}]" if self.employee_code else ""
        return f"{self.name}{code}"


class SalaryStructure(models.Model):
    """
    Defines percentage-based components applied on top of Basic Salary.
    One structure can be shared across employees or companies can have multiple.
    """
    company         = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="salary_structures")
    name            = models.CharField(max_length=100, help_text="e.g. Standard, Management")
    # Allowances (% of Basic)
    hra_pct         = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("40.00"),
                                          help_text="HRA as % of Basic (e.g. 40)")
    da_pct          = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"),
                                          help_text="DA as % of Basic")
    special_allowance_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    # Deductions (% of Basic)
    pf_employee_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("12.00"),
                                          help_text="Employee PF contribution % of Basic (capped at ₹1800)")
    pf_employer_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("12.00"),
                                          help_text="Employer PF contribution %")
    esi_employee_pct= models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.75"),
                                          help_text="Employee ESI % of Gross (applies if gross ≤ ₹21,000)")
    esi_employer_pct= models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("3.25"),
                                          help_text="Employer ESI %")
    pt_monthly      = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"),
                                          help_text="Professional Tax (fixed monthly amount, ₹)")
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Salary Structure"
        verbose_name_plural = "Salary Structures"
        ordering            = ["name"]
        unique_together     = ("company", "name")

    def __str__(self):
        return self.name


class PayrollRun(models.Model):
    STATUS_DRAFT     = "Draft"
    STATUS_PROCESSED = "Processed"
    STATUS_FINALIZED = "Finalized"
    STATUS_CHOICES   = [
        ("Draft",     "Draft"),
        ("Processed", "Processed"),
        ("Finalized", "Finalized"),
    ]

    MONTH_CHOICES = [
        (1,"January"),(2,"February"),(3,"March"),(4,"April"),
        (5,"May"),(6,"June"),(7,"July"),(8,"August"),
        (9,"September"),(10,"October"),(11,"November"),(12,"December"),
    ]

    company   = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="payroll_runs")
    month     = models.PositiveSmallIntegerField(choices=MONTH_CHOICES)
    year      = models.PositiveIntegerField()
    status    = models.CharField(max_length=15, choices=STATUS_CHOICES, default="Draft")
    posted_voucher = models.ForeignKey(
        "vouchers.Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payroll_runs",
    )
    notes     = models.CharField(max_length=300, blank=True)
    created_at= models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name        = "Payroll Run"
        verbose_name_plural = "Payroll Runs"
        ordering            = ["-year", "-month"]
        unique_together     = ("company", "month", "year")

    def __str__(self):
        return f"{self.get_month_display()} {self.year} — {self.status}"


class Payslip(models.Model):
    """One payslip per employee per PayrollRun."""
    payroll_run     = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="payslips")
    employee        = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="payslips")
    salary_structure= models.ForeignKey(SalaryStructure, null=True, blank=True,
                                         on_delete=models.SET_NULL)
    working_days    = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal("26.0"))
    days_worked     = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal("26.0"))

    # Earnings
    basic           = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    hra             = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    da              = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    special_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    other_earnings  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    gross_salary    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Deductions
    pf_employee     = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    pf_employer     = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    esi_employee    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    esi_employer    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    professional_tax= models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tds             = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    other_deductions= models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_deductions= models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    net_pay         = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    is_paid         = models.BooleanField(default=False)
    payment_date    = models.DateField(null=True, blank=True)
    notes           = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name        = "Payslip"
        verbose_name_plural = "Payslips"
        ordering            = ["employee__name"]
        unique_together     = ("payroll_run", "employee")

    def __str__(self):
        return f"{self.employee.name} — {self.payroll_run}"

    def compute(self):
        """
        Recompute all salary components from the employee's basic salary
        and the linked SalaryStructure.  Call this before saving.
        """
        emp = self.employee
        st  = self.salary_structure

        # Pro-rate if days worked < working days
        ratio = (self.days_worked / self.working_days) if self.working_days > 0 else Decimal("1")

        basic_full = emp.basic_salary
        basic      = (basic_full * ratio).quantize(Decimal("0.01"))

        if st:
            hra      = (basic_full * st.hra_pct / 100 * ratio).quantize(Decimal("0.01"))
            da       = (basic_full * st.da_pct  / 100 * ratio).quantize(Decimal("0.01"))
            special  = (basic_full * st.special_allowance_pct / 100 * ratio).quantize(Decimal("0.01"))
        else:
            hra     = (emp.hra * ratio).quantize(Decimal("0.01"))
            da      = Decimal("0.00")
            special = Decimal("0.00")

        gross = basic + hra + da + special + self.other_earnings

        # PF: 12% of basic, capped at ₹1,800
        pf_emp = Decimal("0.00")
        pf_er  = Decimal("0.00")
        if emp.pf_applicable and st:
            pf_emp = min((basic * st.pf_employee_pct / 100).quantize(Decimal("0.01")), Decimal("1800.00"))
            pf_er  = min((basic * st.pf_employer_pct / 100).quantize(Decimal("0.01")), Decimal("1800.00"))

        # ESI: 0.75% of gross (employee), 3.25% (employer), only if gross ≤ 21,000
        esi_emp = Decimal("0.00")
        esi_er  = Decimal("0.00")
        if emp.esi_applicable and st and gross <= Decimal("21000.00"):
            esi_emp = (gross * st.esi_employee_pct / 100).quantize(Decimal("0.01"))
            esi_er  = (gross * st.esi_employer_pct / 100).quantize(Decimal("0.01"))

        pt = st.pt_monthly if st else Decimal("0.00")
        total_ded = pf_emp + esi_emp + pt + self.tds + self.other_deductions

        self.basic           = basic
        self.hra             = hra
        self.da              = da
        self.special_allowance = special
        self.gross_salary    = gross
        self.pf_employee     = pf_emp
        self.pf_employer     = pf_er
        self.esi_employee    = esi_emp
        self.esi_employer    = esi_er
        self.professional_tax = pt
        self.total_deductions = total_ded
        self.net_pay         = gross - total_ded
