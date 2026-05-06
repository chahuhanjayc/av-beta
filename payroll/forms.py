"""
payroll/forms.py — Forms for Payroll management
"""

from django import forms
from .models import Employee, SalaryStructure, PayrollRun, Payslip


# ── Shared widget helpers ─────────────────────────────────────────────────────

def _text(placeholder=""):
    return forms.TextInput(attrs={"class": "form-control form-control-sm",
                                  "placeholder": placeholder})

def _num():
    return forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01"})

def _date():
    return forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"})

def _select():
    return forms.Select(attrs={"class": "form-select form-select-sm"})

def _check():
    return forms.CheckboxInput(attrs={"class": "form-check-input"})

def _textarea():
    return forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2})


# ── Employee ──────────────────────────────────────────────────────────────────

class EmployeeForm(forms.ModelForm):
    class Meta:
        model  = Employee
        fields = [
            "employee_code", "name", "designation", "department",
            "date_of_joining", "date_of_birth", "gender",
            "pan_number", "uan_number", "bank_account", "ifsc_code",
            "basic_salary", "hra",
            "pf_applicable", "esi_applicable", "tds_applicable",
            "is_active",
        ]
        widgets = {
            "employee_code":   _text("EMP001"),
            "name":            _text("Employee full name"),
            "designation":     _text("e.g. Senior Developer"),
            "department":      _text("e.g. Engineering"),
            "date_of_joining": _date(),
            "date_of_birth":   _date(),
            "gender":          _select(),
            "pan_number":      _text("ABCDE1234F"),
            "uan_number":      _text("100XXXXXXXXX"),
            "bank_account":    _text("Account number"),
            "ifsc_code":       _text("SBIN0000000"),
            "basic_salary":    _num(),
            "hra":             _num(),
            "pf_applicable":   _check(),
            "esi_applicable":  _check(),
            "tds_applicable":  _check(),
            "is_active":       _check(),
        }


# ── Salary Structure ──────────────────────────────────────────────────────────

class SalaryStructureForm(forms.ModelForm):
    class Meta:
        model  = SalaryStructure
        fields = [
            "name",
            "hra_pct", "da_pct", "special_allowance_pct",
            "pf_employee_pct", "pf_employer_pct",
            "esi_employee_pct", "esi_employer_pct",
            "pt_monthly",
        ]
        widgets = {
            "name":                    _text("e.g. Standard"),
            "hra_pct":                 _num(),
            "da_pct":                  _num(),
            "special_allowance_pct":   _num(),
            "pf_employee_pct":         _num(),
            "pf_employer_pct":         _num(),
            "esi_employee_pct":        _num(),
            "esi_employer_pct":        _num(),
            "pt_monthly":              _num(),
        }


# ── Payroll Run ───────────────────────────────────────────────────────────────

class PayrollRunForm(forms.ModelForm):
    class Meta:
        model  = PayrollRun
        fields = ["month", "year", "notes"]
        widgets = {
            "month": _select(),
            "year":  forms.NumberInput(attrs={
                "class": "form-control form-control-sm",
                "min": 2000, "max": 2100,
            }),
            "notes": _text("Optional notes"),
        }


# ── Payslip (edit individual payslip overrides) ───────────────────────────────

class PayslipForm(forms.ModelForm):
    class Meta:
        model  = Payslip
        fields = [
            "salary_structure",
            "working_days", "days_worked",
            "other_earnings", "tds", "other_deductions",
            "is_paid", "payment_date", "notes",
        ]
        widgets = {
            "salary_structure": _select(),
            "working_days":     forms.NumberInput(attrs={
                "class": "form-control form-control-sm", "step": "0.5",
            }),
            "days_worked":      forms.NumberInput(attrs={
                "class": "form-control form-control-sm", "step": "0.5",
            }),
            "other_earnings":   _num(),
            "tds":              _num(),
            "other_deductions": _num(),
            "is_paid":          _check(),
            "payment_date":     _date(),
            "notes":            _text("Optional notes"),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["salary_structure"].queryset = SalaryStructure.objects.filter(
                company=company
            ).order_by("name")
        self.fields["salary_structure"].required = False
