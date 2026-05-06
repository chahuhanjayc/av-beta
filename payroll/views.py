"""
payroll/views.py — Phase 6: Payroll management
"""

import json
import calendar
from datetime import date as _date
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from core.decorators import admin_required, write_required
from .models import Employee, SalaryStructure, PayrollRun, Payslip
from .forms import EmployeeForm, SalaryStructureForm, PayrollRunForm, PayslipForm


PAGE_SIZE = 30


def _payroll_month_end(run):
    return _date(run.year, run.month, calendar.monthrange(run.year, run.month)[1])


def _payroll_ledger(company, name, group_name, nature):
    from ledger.models import AccountGroup, Ledger

    group, _ = AccountGroup.objects.get_or_create(
        company=company,
        name=group_name,
        defaults={"nature": nature},
    )
    ledger, _ = Ledger.objects.get_or_create(
        company=company,
        name=name,
        defaults={"account_group": group},
    )
    return ledger


def _post_payroll_voucher(company, run, user):
    from vouchers.models import Voucher, VoucherItem

    payslips = list(run.payslips.select_related("employee"))
    if not payslips:
        raise ValueError("Payroll run has no payslips to post.")

    totals = {
        "gross": sum((p.gross_salary for p in payslips), Decimal("0.00")),
        "net": sum((p.net_pay for p in payslips), Decimal("0.00")),
        "pf_employee": sum((p.pf_employee for p in payslips), Decimal("0.00")),
        "pf_employer": sum((p.pf_employer for p in payslips), Decimal("0.00")),
        "esi_employee": sum((p.esi_employee for p in payslips), Decimal("0.00")),
        "esi_employer": sum((p.esi_employer for p in payslips), Decimal("0.00")),
        "professional_tax": sum((p.professional_tax for p in payslips), Decimal("0.00")),
        "tds": sum((p.tds for p in payslips), Decimal("0.00")),
        "other_deductions": sum((p.other_deductions for p in payslips), Decimal("0.00")),
    }

    voucher = Voucher.objects.create(
        company=company,
        voucher_type="Journal",
        date=_payroll_month_end(run),
        narration=f"Payroll accrual for {run.get_month_display()} {run.year}",
    )

    debit_lines = [
        ("Salary Expense", "Indirect Expenses", "Expense", totals["gross"]),
        ("Employer PF Expense", "Indirect Expenses", "Expense", totals["pf_employer"]),
        ("Employer ESI Expense", "Indirect Expenses", "Expense", totals["esi_employer"]),
    ]
    credit_lines = [
        ("Salary Payable", "Current Liabilities", "Liability", totals["net"]),
        ("PF Payable", "Current Liabilities", "Liability", totals["pf_employee"] + totals["pf_employer"]),
        ("ESI Payable", "Current Liabilities", "Liability", totals["esi_employee"] + totals["esi_employer"]),
        ("Professional Tax Payable", "Current Liabilities", "Liability", totals["professional_tax"]),
        ("TDS Payable", "Current Liabilities", "Liability", totals["tds"]),
        ("Other Payroll Deductions Payable", "Current Liabilities", "Liability", totals["other_deductions"]),
    ]

    for name, group_name, nature, amount in debit_lines:
        if amount > 0:
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=_payroll_ledger(company, name, group_name, nature),
                entry_type="DR",
                amount=amount,
            )

    for name, group_name, nature, amount in credit_lines:
        if amount > 0:
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=_payroll_ledger(company, name, group_name, nature),
                entry_type="CR",
                amount=amount,
            )

    voucher.validate_balance()
    voucher.approve(user)
    return voucher


# ═══════════════════════════════════════════════════════════════════════════════
# EMPLOYEE CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def employee_list(request):
    company = request.current_company
    q       = request.GET.get("q", "").strip()
    dept    = request.GET.get("dept", "").strip()
    active  = request.GET.get("active", "1")

    qs = Employee.objects.filter(company=company)
    if active == "1":
        qs = qs.filter(is_active=True)
    elif active == "0":
        qs = qs.filter(is_active=False)
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(employee_code__icontains=q)
    if dept:
        qs = qs.filter(department__icontains=dept)

    departments = Employee.objects.filter(company=company, is_active=True)\
                          .values_list("department", flat=True)\
                          .distinct().order_by("department")

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get("page"))

    return render(request, "payroll/employee_list.html", {
        "page_obj":    page_obj,
        "q":           q,
        "dept":        dept,
        "active":      active,
        "departments": departments,
    })


@login_required
@write_required
def employee_create(request):
    company = request.current_company
    if request.method == "POST":
        form = EmployeeForm(request.POST)
        if form.is_valid():
            emp = form.save(commit=False)
            emp.company = company
            emp.save()
            messages.success(request, f"Employee '{emp.name}' created.")
            return redirect("payroll:employee_list")
    else:
        form = EmployeeForm()
    return render(request, "payroll/employee_form.html", {"form": form, "title": "New Employee"})


@login_required
@write_required
def employee_edit(request, pk):
    company = request.current_company
    emp     = get_object_or_404(Employee, pk=pk, company=company)
    if request.method == "POST":
        form = EmployeeForm(request.POST, instance=emp)
        if form.is_valid():
            form.save()
            messages.success(request, f"Employee '{emp.name}' updated.")
            return redirect("payroll:employee_list")
    else:
        form = EmployeeForm(instance=emp)
    return render(request, "payroll/employee_form.html", {
        "form": form, "title": f"Edit {emp.name}", "emp": emp,
    })


@login_required
@admin_required
def employee_delete(request, pk):
    company = request.current_company
    emp     = get_object_or_404(Employee, pk=pk, company=company)
    if request.method == "POST":
        name = emp.name
        emp.delete()
        messages.success(request, f"Employee '{name}' deleted.")
        return redirect("payroll:employee_list")
    return render(request, "payroll/employee_confirm_delete.html", {"emp": emp})


from django.http import JsonResponse
from django.views.decorators.http import require_POST
from ocr import ocr_utils

@login_required
@write_required
@require_POST
def quick_add(request):
    """AJAX endpoint to create an Employee on-the-fly."""
    company = request.current_company
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"success": False, "error": "Name is required."}, status=400)
        
        emp, created = Employee.objects.get_or_create(
            company=company, name=name,
            defaults={"is_active": True}
        )
        return JsonResponse({
            "success": True,
            "id": emp.pk,
            "name": emp.name,
            "created": created
        })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

@login_required
@write_required
@require_POST
def employee_ocr_upload(request):
    """AJAX view to process PAN or Bank document and return JSON data."""
    if "file" not in request.FILES:
        return JsonResponse({"error": "No file uploaded"}, status=400)
    
    file = request.FILES["file"]
    file_bytes = file.read()
    
    # Use our improved OCR pipeline
    result = ocr_utils.process_pdf(file_bytes)
    
    # DEBUG LOGGING
    import logging
    logger = logging.getLogger('ocr_debug')
    logger.info(f"OCR Result: {result}")
    print(f"DEBUG OCR Result: {result}") # Console output
    
    return JsonResponse(result)

# ═══════════════════════════════════════════════════════════════════════════════
# SALARY STRUCTURE CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def salary_structure_list(request):
    company    = request.current_company
    structures = SalaryStructure.objects.filter(company=company).order_by("name")
    return render(request, "payroll/salary_structure_list.html", {"structures": structures})


@login_required
@write_required
def salary_structure_create(request):
    company = request.current_company
    if request.method == "POST":
        form = SalaryStructureForm(request.POST)
        if form.is_valid():
            st = form.save(commit=False)
            st.company = company
            st.save()
            messages.success(request, f"Salary structure '{st.name}' created.")
            return redirect("payroll:salary_structure_list")
    else:
        form = SalaryStructureForm()
    return render(request, "payroll/salary_structure_form.html", {
        "form": form, "title": "New Salary Structure",
    })


@login_required
@write_required
def salary_structure_edit(request, pk):
    company = request.current_company
    st      = get_object_or_404(SalaryStructure, pk=pk, company=company)
    if request.method == "POST":
        form = SalaryStructureForm(request.POST, instance=st)
        if form.is_valid():
            form.save()
            messages.success(request, f"Salary structure '{st.name}' updated.")
            return redirect("payroll:salary_structure_list")
    else:
        form = SalaryStructureForm(instance=st)
    return render(request, "payroll/salary_structure_form.html", {
        "form": form, "title": f"Edit {st.name}", "st": st,
    })


@login_required
@admin_required
def salary_structure_delete(request, pk):
    company = request.current_company
    st      = get_object_or_404(SalaryStructure, pk=pk, company=company)
    if request.method == "POST":
        name = st.name
        st.delete()
        messages.success(request, f"Salary structure '{name}' deleted.")
        return redirect("payroll:salary_structure_list")
    return render(request, "payroll/salary_structure_confirm_delete.html", {"st": st})


# ═══════════════════════════════════════════════════════════════════════════════
# PAYROLL RUN
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def payroll_run_list(request):
    company = request.current_company
    runs    = PayrollRun.objects.filter(company=company).order_by("-year", "-month")
    return render(request, "payroll/payroll_run_list.html", {"runs": runs})


@login_required
@write_required
def payroll_run_create(request):
    company = request.current_company
    if request.method == "POST":
        form = PayrollRunForm(request.POST)
        if form.is_valid():
            run = form.save(commit=False)
            run.company = company
            run.save()
            messages.success(request, f"Payroll run for {run} created.")
            return redirect("payroll:payroll_run_detail", pk=run.pk)
    else:
        today = _date.today()
        form  = PayrollRunForm(initial={"month": today.month, "year": today.year})
    return render(request, "payroll/payroll_run_form.html", {
        "form": form, "title": "New Payroll Run",
    })


@login_required
def payroll_run_detail(request, pk):
    company  = request.current_company
    run      = get_object_or_404(PayrollRun, pk=pk, company=company)
    payslips = run.payslips.select_related("employee", "salary_structure").order_by("employee__name")
    return render(request, "payroll/payroll_run_detail.html", {
        "run": run, "payslips": payslips,
    })


@login_required
@write_required
def payroll_run_process(request, pk):
    """
    Generate (or regenerate) payslips for all active employees.
    Computes salary using the first available SalaryStructure.
    """
    company = request.current_company
    run     = get_object_or_404(PayrollRun, pk=pk, company=company)

    if run.status == PayrollRun.STATUS_FINALIZED:
        messages.error(request, "Cannot reprocess a finalized payroll run.")
        return redirect("payroll:payroll_run_detail", pk=pk)

    if request.method != "POST":
        employees  = Employee.objects.filter(company=company, is_active=True)
        structures = SalaryStructure.objects.filter(company=company)
        return render(request, "payroll/payroll_run_process_confirm.html", {
            "run":            run,
            "employee_count": employees.count(),
            "structures":     structures,
        })

    default_structure = SalaryStructure.objects.filter(company=company).order_by("name").first()

    with transaction.atomic():
        employees        = Employee.objects.filter(company=company, is_active=True)
        created = updated = 0
        for emp in employees:
            payslip, is_new = Payslip.objects.get_or_create(
                payroll_run=run,
                employee=emp,
                defaults={
                    "salary_structure": default_structure,
                    "working_days":     26,
                    "days_worked":      26,
                },
            )
            if not is_new:
                updated += 1
            else:
                created += 1
            payslip.compute()
            payslip.save()

        run.status       = PayrollRun.STATUS_PROCESSED
        run.processed_at = timezone.now()
        run.save(update_fields=["status", "processed_at"])

    messages.success(request,
        f"Payroll processed: {created} payslips created, {updated} recomputed.")
    return redirect("payroll:payroll_run_detail", pk=pk)


@login_required
@admin_required
def payroll_run_finalize(request, pk):
    company = request.current_company
    run     = get_object_or_404(PayrollRun, pk=pk, company=company)
    if request.method == "POST":
        if run.status == PayrollRun.STATUS_PROCESSED:
            try:
                with transaction.atomic():
                    run = PayrollRun.objects.select_for_update().get(pk=run.pk, company=company)
                    if not run.posted_voucher_id:
                        run.posted_voucher = _post_payroll_voucher(company, run, request.user)
                    run.status = PayrollRun.STATUS_FINALIZED
                    run.save(update_fields=["status", "posted_voucher"])
                messages.success(request, f"Payroll run '{run}' finalized and posted to accounts.")
            except Exception as exc:
                messages.error(request, f"Payroll could not be finalized: {exc}")
        else:
            messages.error(request, "Only Processed payroll runs can be finalized.")
    return redirect("payroll:payroll_run_detail", pk=pk)


# ═══════════════════════════════════════════════════════════════════════════════
# PAYSLIP
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def payslip_detail(request, pk):
    company = request.current_company
    payslip = get_object_or_404(Payslip, pk=pk, payroll_run__company=company)
    return render(request, "payroll/payslip_detail.html", {"payslip": payslip})


@login_required
@write_required
def payslip_edit(request, pk):
    """Edit individual payslip overrides and recompute."""
    company = request.current_company
    payslip = get_object_or_404(Payslip, pk=pk, payroll_run__company=company)

    if payslip.payroll_run.status == PayrollRun.STATUS_FINALIZED:
        messages.error(request, "Cannot edit payslips in a finalized payroll run.")
        return redirect("payroll:payslip_detail", pk=pk)

    if request.method == "POST":
        form = PayslipForm(request.POST, instance=payslip, company=company)
        if form.is_valid():
            ps = form.save(commit=False)
            ps.compute()
            ps.save()
            messages.success(request, f"Payslip for {payslip.employee.name} updated.")
            return redirect("payroll:payslip_detail", pk=pk)
    else:
        form = PayslipForm(instance=payslip, company=company)

    return render(request, "payroll/payslip_form.html", {"form": form, "payslip": payslip})


# ═══════════════════════════════════════════════════════════════════════════════
# PAYROLL SUMMARY REPORT
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def payroll_summary(request):
    company  = request.current_company
    run_pk   = request.GET.get("run", "")
    runs     = PayrollRun.objects.filter(company=company).order_by("-year", "-month")

    selected_run = None
    payslips     = []

    if run_pk:
        selected_run = get_object_or_404(PayrollRun, pk=run_pk, company=company)
        payslips     = selected_run.payslips.select_related("employee").order_by("employee__name")

    return render(request, "payroll/payroll_summary.html", {
        "runs":         runs,
        "selected_run": selected_run,
        "payslips":     payslips,
        "run_pk":       run_pk,
    })
