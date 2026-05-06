"""
Sales demo workspace seeding.

The goal is to create realistic, repeatable sample data that exercises the
application's CA-practice, accounting, compliance, and client-portal workflows.
The seeder is intentionally idempotent: it updates master/setup records and
skips demo vouchers that already exist by source reference.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import (
    ClientEngagement,
    Company,
    CompanySettings,
    CompanyStatutoryProfile,
    ComplianceFiling,
    ComplianceNotice,
    FilingReview,
    GSTFilingPack,
    GSTPeriodReview,
    GSTPostFilingTracker,
    MarketProofCaseStudy,
    MarketProofExternalEvidence,
    PilotFeedback,
    PracticeTask,
    UserCompanyAccess,
)
from costcenter.models import Budget, BudgetHead, CostCenter
from fixedassets.models import AssetGroup, FixedAsset
from inventory.models import Batch, Godown, StockItem
from ledger.models import AccountGroup, Ledger
from orders.models import Order, OrderItem
from payroll.models import Employee, PayrollRun, SalaryStructure
from tds.models import TDSEntry, TDSReturnWorkpaper, TDSSection
from vouchers.models import Voucher, VoucherItem


DEMO_SOURCE = "demo_sales_mode"
PRIMARY_DEMO_COMPANY_NAME = "[Demo] Mehta Appliances LLP"

DEMO_COMPANY_SPECS = [
    {
        "name": PRIMARY_DEMO_COMPANY_NAME,
        "short_code": "MAL",
        "gstin": "27AAFCM4567K1Z5",
        "tan": "MUMA12345B",
        "bank_name": "HDFC Bank",
        "upi_id": "mehta.demo@hdfc",
        "package": ClientEngagement.PACKAGE_CFO,
        "retainer": Decimal("85000.00"),
        "risk": ClientEngagement.RISK_HIGH,
        "scope": "Full accounting, GST, TDS, e-invoice, collections, and monthly partner review.",
    },
    {
        "name": "[Demo] Nirmal Textiles LLP",
        "short_code": "NTX",
        "gstin": "24AAHFN5678R1Z2",
        "tan": "AHMN98765C",
        "bank_name": "ICICI Bank",
        "upi_id": "nirmal.demo@icici",
        "package": ClientEngagement.PACKAGE_GST_TDS,
        "retainer": Decimal("42000.00"),
        "risk": ClientEngagement.RISK_MEDIUM,
        "scope": "GST, TDS, client document chase, and migration support from Tally.",
    },
    {
        "name": "[Demo] Orion Components Pvt Ltd",
        "short_code": "ORN",
        "gstin": "29AACCO1122P1Z9",
        "tan": "BLRO54321D",
        "bank_name": "Axis Bank",
        "upi_id": "orion.demo@axis",
        "package": ClientEngagement.PACKAGE_FULL_ACCOUNTING,
        "retainer": Decimal("68000.00"),
        "risk": ClientEngagement.RISK_CRITICAL,
        "scope": "Tally exit pilot, bank feed import, GST filing pack, notices, and inventory accounting.",
    },
]


@dataclass
class DemoWorkspaceResult:
    primary_company: Company
    companies: list
    counts: dict


def demo_company_names():
    return [spec["name"] for spec in DEMO_COMPANY_SPECS]


def demo_workspace_snapshot():
    companies = list(Company.objects.filter(name__in=demo_company_names()).order_by("name"))
    company_ids = [company.pk for company in companies]
    primary_company = next(
        (company for company in companies if company.name == PRIMARY_DEMO_COMPANY_NAME),
        None,
    )

    try:
        from portal.models import ClientDocumentRequest
    except ImportError:
        ClientDocumentRequest = None

    counts = {
        "companies": len(companies),
        "ledgers": Ledger.objects.filter(company_id__in=company_ids).count(),
        "vouchers": Voucher.objects.filter(company_id__in=company_ids).count(),
        "stock_items": StockItem.objects.filter(company_id__in=company_ids).count(),
        "orders": Order.objects.filter(company_id__in=company_ids).count(),
        "filings": ComplianceFiling.objects.filter(company_id__in=company_ids).count(),
        "notices": ComplianceNotice.objects.filter(company_id__in=company_ids).count(),
        "tasks": PracticeTask.objects.filter(company_id__in=company_ids).count(),
        "document_requests": (
            ClientDocumentRequest.objects.filter(company_id__in=company_ids).count()
            if ClientDocumentRequest
            else 0
        ),
        "market_proof": MarketProofExternalEvidence.objects.filter(company_id__in=company_ids).count()
        + MarketProofCaseStudy.objects.filter(company_id__in=company_ids).count(),
    }
    return DemoWorkspaceResult(
        primary_company=primary_company,
        companies=companies,
        counts=counts,
    )


@transaction.atomic
def seed_demo_workspace(user=None):
    """Create or refresh the complete demo workspace."""
    today = timezone.localdate()
    fy_start = _financial_year_start(today)
    companies = []

    for index, spec in enumerate(DEMO_COMPANY_SPECS):
        company = _ensure_company(spec, today, fy_start, user)
        _grant_access(company, user)
        _seed_company_foundation(company, spec, user, today, index)
        companies.append(company)

    primary = next(company for company in companies if company.name == PRIMARY_DEMO_COMPANY_NAME)
    _seed_primary_operating_data(primary, user, today)
    _seed_portfolio_operating_data(companies, user, today)

    return demo_workspace_snapshot()


def _financial_year_start(today):
    year = today.year if today.month >= 4 else today.year - 1
    return date(year, 4, 1)


def _month_bounds(today, offset=0):
    month_index = today.month - 1 + offset
    year = today.year + (month_index // 12)
    month = (month_index % 12) + 1
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def _period_label(period_start):
    return period_start.strftime("%b %Y")


def _ensure_company(spec, today, fy_start, user=None):
    company = Company.objects.filter(name=spec["name"]).first()
    if not company:
        company = Company.objects.create(
            name=spec["name"],
            short_code=spec["short_code"],
            financial_year_start=fy_start,
        )

    whatsapp_number = _unique_company_value(
        company,
        "whatsapp_intake_number",
        f"+91{9000000000 + (company.pk % 900000000)}",
        f"+91{8000000000 + (company.pk % 900000000)}",
    )
    portal_token = _unique_company_value(
        company,
        "portal_token",
        f"demo-portal-token-{company.pk}",
        f"demo-portal-token-alt-{company.pk}",
    )

    company.short_code = spec["short_code"]
    company.gstin = spec["gstin"]
    company.tan = spec["tan"]
    company.tds_responsible_person = "Rohan Shah"
    company.tds_responsible_designation = "Finance Controller"
    company.address = (
        "Demo registered office, Andheri East, Mumbai, Maharashtra 400093"
        if spec["short_code"] == "MAL"
        else "Demo client office for sales presentations"
    )
    company.financial_year_start = fy_start
    company.whatsapp_intake_number = whatsapp_number
    company.invoice_email_from_name = f"{company.name} Accounts"
    company.invoice_email_from_address = f"billing.demo{company.pk}@akshayavistara.test"
    company.invoice_email_reply_to = f"accounts.demo{company.pk}@akshayavistara.test"
    company.invoice_email_subject = "Invoice {voucher_number} from {company_name}"
    company.invoice_email_body = (
        "Dear {client_name},\n\n"
        "Please find attached invoice {voucher_number} from {company_name} for {amount}.\n"
        "You can reply to this email or use the client portal for supporting documents.\n\n"
        "Regards,\n{company_name}"
    )
    company.payment_reminder_email_subject = "Payment reminder: {voucher_number} from {company_name}"
    company.payment_reminder_email_body = (
        "Dear {client_name},\n\n"
        "Outstanding amount: {outstanding}\n"
        "Due date: {due_date}\n"
        "{aging_line}\n\n"
        "Please confirm payment status or share UTR details.\n\n"
        "Regards,\n{company_name}"
    )
    company.e_invoice_enabled = True
    company.e_invoice_aato_crore = Decimal("18.50")
    company.e_invoice_reporting_deadline_days = 30
    company.e_invoice_warning_days = 25
    company.bank_name = spec["bank_name"]
    company.account_number = str(50200000000000 + company.pk)
    company.ifsc_code = "HDFC0001234"
    company.upi_id = spec["upi_id"]
    company.portal_token = portal_token
    company.save()

    CompanySettings.objects.get_or_create(company=company)
    CompanyStatutoryProfile.objects.update_or_create(
        company=company,
        defaults={
            "gst_registered": True,
            "gst_return_frequency": CompanyStatutoryProfile.GST_FREQUENCY_MONTHLY,
            "gstr1_frequency": CompanyStatutoryProfile.GSTR1_MONTHLY,
            "qrmp_group": CompanyStatutoryProfile.QRMP_GROUP_A,
            "tds_applicable": True,
            "tds_24q_enabled": True,
            "tds_26q_enabled": True,
            "msme_watch_enabled": True,
            "rules_notes": "Demo rules: monthly GST, 24Q/26Q enabled, MSME watch active.",
        },
    )
    ClientEngagement.objects.update_or_create(
        company=company,
        defaults={
            "status": ClientEngagement.STATUS_ACTIVE,
            "service_package": spec["package"],
            "monthly_retainer": spec["retainer"],
            "billing_cycle": ClientEngagement.BILLING_MONTHLY,
            "renewal_date": today + timedelta(days=75),
            "partner_owner": user if user and user.is_authenticated else None,
            "manager_owner": user if user and user.is_authenticated else None,
            "risk_rating": spec["risk"],
            "scope_summary": spec["scope"],
            "out_of_scope": "Statutory representation beyond demo cases.",
            "internal_notes": "Sales demo account with seeded adoption, compliance, and accounting activity.",
            "last_reviewed_at": today - timedelta(days=4),
        },
    )
    return company


def _unique_company_value(company, field_name, preferred, fallback):
    current = getattr(company, field_name)
    if current:
        return current
    if not Company.objects.filter(**{field_name: preferred}).exclude(pk=company.pk).exists():
        return preferred
    return fallback


def _grant_access(company, user):
    if user and user.is_authenticated:
        UserCompanyAccess.objects.update_or_create(
            user=user,
            company=company,
            defaults={"role": "Admin"},
        )


def _seed_company_foundation(company, spec, user, today, index):
    ledgers = _ensure_ledgers(company)
    _ensure_cost_centers(company, ledgers, today)
    _ensure_inventory(company, today)
    _ensure_payroll(company, today)
    _ensure_fixed_assets(company, ledgers, today)
    _ensure_tds_sections(company, ledgers)
    _seed_lightweight_company_activity(company, spec, user, today, index, ledgers)


def _ensure_group(company, name, nature, threshold=Decimal("0.00")):
    group, _ = AccountGroup.objects.get_or_create(
        company=company,
        name=name,
        defaults={"nature": nature, "threshold_limit": threshold},
    )
    changed = False
    if group.nature != nature:
        group.nature = nature
        changed = True
    if group.threshold_limit != threshold:
        group.threshold_limit = threshold
        changed = True
    if changed:
        group.save(update_fields=["nature", "threshold_limit"])
    return group


def _ensure_ledger(company, groups, name, group_name, **defaults):
    ledger, _ = Ledger.objects.get_or_create(
        company=company,
        name=name,
        defaults={"account_group": groups[group_name], **defaults},
    )
    update_fields = []
    if ledger.account_group_id != groups[group_name].pk:
        ledger.account_group = groups[group_name]
        update_fields.append("account_group")
    for field, value in defaults.items():
        if getattr(ledger, field) != value:
            setattr(ledger, field, value)
            update_fields.append(field)
    if update_fields:
        ledger.save(update_fields=update_fields)
    return ledger


def _ensure_ledgers(company):
    groups = {
        "bank": _ensure_group(company, "Bank Accounts", "Asset"),
        "cash": _ensure_group(company, "Cash in Hand", "Asset"),
        "debtors": _ensure_group(company, "Sundry Debtors", "Asset"),
        "inventory": _ensure_group(company, "Inventory", "Asset"),
        "fixed_assets": _ensure_group(company, "Fixed Assets", "Asset"),
        "creditors": _ensure_group(company, "Sundry Creditors", "Liability"),
        "statutory": _ensure_group(company, "Duties & Taxes", "Tax", Decimal("250000.00")),
        "payroll_payable": _ensure_group(company, "Payroll Payables", "Liability"),
        "sales": _ensure_group(company, "Sales Accounts", "Income"),
        "services": _ensure_group(company, "Service Income", "Income"),
        "purchases": _ensure_group(company, "Purchase Accounts", "Expense"),
        "expenses": _ensure_group(company, "Indirect Expenses", "Expense", Decimal("100000.00")),
        "payroll": _ensure_group(company, "Payroll Expenses", "Expense"),
        "equity": _ensure_group(company, "Capital Account", "Equity"),
    }
    ledgers = {
        "bank": _ensure_ledger(
            company,
            groups,
            "HDFC Demo Current Account",
            "bank",
            opening_balance=Decimal("-1850000.00"),
        ),
        "cash": _ensure_ledger(
            company,
            groups,
            "Cash in Hand",
            "cash",
            opening_balance=Decimal("-75000.00"),
        ),
        "capital": _ensure_ledger(
            company,
            groups,
            "Partner Capital",
            "equity",
            opening_balance=Decimal("1925000.00"),
        ),
        "customer_kuber": _ensure_ledger(
            company,
            groups,
            "Kuber Retail Pvt Ltd",
            "debtors",
            gstin="27AAECK1234Q1Z2",
            email="finance@kuber-retail.example",
            whatsapp_number="+919810001001",
            credit_limit=Decimal("350000.00"),
            credit_days=30,
            address="Bandra Kurla Complex, Mumbai",
        ),
        "customer_metro": _ensure_ledger(
            company,
            groups,
            "Metro Wholesale Co.",
            "debtors",
            gstin="27AAHFM5678R1Z4",
            email="accounts@metro-wholesale.example",
            whatsapp_number="+919810001002",
            credit_limit=Decimal("250000.00"),
            credit_days=21,
            address="Vashi, Navi Mumbai",
        ),
        "customer_lotus": _ensure_ledger(
            company,
            groups,
            "Lotus Online Commerce",
            "debtors",
            gstin="29AABCL8899P1Z8",
            email="payables@lotus-commerce.example",
            whatsapp_number="+919810001003",
            credit_limit=Decimal("500000.00"),
            credit_days=45,
            address="Indiranagar, Bengaluru",
        ),
        "supplier_packaging": _ensure_ledger(
            company,
            groups,
            "Prakash Packaging Industries",
            "creditors",
            gstin="27AAEFP1111M1Z6",
            email="billing@prakash-packaging.example",
            whatsapp_number="+919820002001",
            pan_number="AAEFP1111M",
        ),
        "supplier_msme": _ensure_ledger(
            company,
            groups,
            "MSME Steel Works",
            "creditors",
            gstin="27AAGFM2222N1Z7",
            email="accounts@msme-steel.example",
            whatsapp_number="+919820002002",
            pan_number="AAGFM2222N",
            is_msme=True,
            msme_reg_number="UDYAM-MH-19-0012345",
        ),
        "supplier_consultant": _ensure_ledger(
            company,
            groups,
            "Apex Compliance Consultants",
            "creditors",
            gstin="27AAJFA3333P1Z1",
            email="billing@apex-consultants.example",
            whatsapp_number="+919820002003",
            pan_number="AAJFA3333P",
            tds_section="194J",
            tds_rate=Decimal("10.00"),
            tds_threshold=Decimal("30000.00"),
        ),
        "sales_goods": _ensure_ledger(company, groups, "Sales - Appliances", "sales"),
        "sales_services": _ensure_ledger(company, groups, "Sales - Service Contracts", "services"),
        "purchase_goods": _ensure_ledger(company, groups, "Purchase - Trading Goods", "purchases"),
        "purchase_packaging": _ensure_ledger(company, groups, "Purchase - Packaging Material", "purchases"),
        "professional_fees": _ensure_ledger(company, groups, "Professional Fees", "expenses"),
        "freight": _ensure_ledger(company, groups, "Freight and Delivery", "expenses"),
        "rent": _ensure_ledger(company, groups, "Office Rent", "expenses"),
        "bank_charges": _ensure_ledger(company, groups, "Bank Charges", "expenses"),
        "salary_expense": _ensure_ledger(company, groups, "Salary Expense", "payroll"),
        "salary_payable": _ensure_ledger(company, groups, "Salary Payable", "payroll_payable"),
        "cgst_output": _ensure_ledger(company, groups, "Output CGST", "statutory"),
        "sgst_output": _ensure_ledger(company, groups, "Output SGST", "statutory"),
        "igst_output": _ensure_ledger(company, groups, "Output IGST", "statutory"),
        "cgst_input": _ensure_ledger(company, groups, "Input CGST", "statutory"),
        "sgst_input": _ensure_ledger(company, groups, "Input SGST", "statutory"),
        "igst_input": _ensure_ledger(company, groups, "Input IGST", "statutory"),
        "tds_payable_194j": _ensure_ledger(company, groups, "TDS Payable 194J", "statutory"),
        "gst_cash": _ensure_ledger(company, groups, "GST Cash Ledger", "statutory"),
        "inventory": _ensure_ledger(company, groups, "Inventory Stock", "inventory"),
        "computers": _ensure_ledger(company, groups, "Computers and Equipment", "fixed_assets"),
        "depreciation": _ensure_ledger(company, groups, "Depreciation Expense", "expenses"),
        "accum_depr": _ensure_ledger(company, groups, "Accumulated Depreciation", "fixed_assets"),
    }
    return ledgers


def _ensure_cost_centers(company, ledgers, today):
    centers = [
        ("GST", "GST Compliance Cell", "Department", "GST filing and notice response team"),
        ("SALES", "Sales Support", "Department", "Client billing and collections desk"),
        ("OPS", "Operations", "Department", "Migration, bank feed, and close operations"),
    ]
    for code, name, category, description in centers:
        center, _ = CostCenter.objects.update_or_create(
            company=company,
            name=name,
            defaults={"code": code, "category": category, "description": description, "is_active": True},
        )
        Budget.objects.update_or_create(
            cost_center=center,
            year=today.year,
            month=today.month,
            defaults={"monthly_limit": Decimal("250000.00")},
        )
    BudgetHead.objects.update_or_create(
        company=company,
        ledger=ledgers["professional_fees"],
        cost_center=CostCenter.objects.get(company=company, name="GST Compliance Cell"),
        financial_year=f"{today.year}-{str(today.year + 1)[-2:]}",
        period="Annual",
        defaults={"budgeted_amount": Decimal("900000.00"), "notes": "Demo advisory budget"},
    )


def _ensure_inventory(company, today):
    godown, _ = Godown.objects.update_or_create(
        company=company,
        name="Main Demo Warehouse",
        defaults={"location": "Bhiwandi fulfillment hub", "is_primary": True, "is_active": True},
    )
    items = [
        ("Smart Meter Controller", "Nos", Decimal("260.000"), Decimal("1800.00"), Decimal("2650.00"), Decimal("35.000")),
        ("Industrial Sensor Kit", "Nos", Decimal("120.000"), Decimal("4200.00"), Decimal("6100.00"), Decimal("20.000")),
        ("Warranty Service Pack", "Nos", Decimal("40.000"), Decimal("900.00"), Decimal("1800.00"), Decimal("10.000")),
    ]
    for name, unit, opening_qty, purchase_price, selling_price, low_stock in items:
        item, _ = StockItem.objects.update_or_create(
            company=company,
            name=name,
            defaults={
                "unit": unit,
                "opening_quantity": opening_qty,
                "purchase_price": purchase_price,
                "selling_price": selling_price,
                "low_stock_threshold": low_stock,
                "is_active": True,
                "prevent_negative_stock": False,
            },
        )
        Batch.objects.update_or_create(
            company=company,
            stock_item=item,
            godown=godown,
            batch_number=f"DEMO-{today.strftime('%Y%m')}-{item.pk}",
            defaults={
                "expiry_date": today + timedelta(days=365),
                "purchase_rate": purchase_price,
                "quantity": opening_qty,
            },
        )


def _ensure_payroll(company, today):
    SalaryStructure.objects.update_or_create(
        company=company,
        name="Demo Standard Payroll",
        defaults={
            "hra_pct": Decimal("40.00"),
            "special_allowance_pct": Decimal("20.00"),
            "pf_employee_pct": Decimal("12.00"),
            "pf_employer_pct": Decimal("12.00"),
            "pt_monthly": Decimal("200.00"),
        },
    )
    employees = [
        ("DEMO-E001", "Nisha Patel", "Accounts Executive", "Compliance", Decimal("42000.00"), True),
        ("DEMO-E002", "Arjun Menon", "GST Analyst", "Compliance", Decimal("52000.00"), True),
        ("DEMO-E003", "Priya Shah", "Client Success Manager", "Operations", Decimal("68000.00"), True),
    ]
    for code, name, designation, department, salary, tds_applicable in employees:
        Employee.objects.update_or_create(
            company=company,
            employee_code=code,
            defaults={
                "name": name,
                "designation": designation,
                "department": department,
                "date_of_joining": today - timedelta(days=420),
                "pan_number": f"DEMO{code[-4:]}A",
                "bank_account": str(4010000000 + int(code[-3:])),
                "ifsc_code": "HDFC0001234",
                "basic_salary": salary,
                "hra": (salary * Decimal("0.40")).quantize(Decimal("0.01")),
                "pf_applicable": True,
                "tds_applicable": tds_applicable,
                "is_active": True,
            },
        )
    PayrollRun.objects.update_or_create(
        company=company,
        month=today.month,
        year=today.year,
        defaults={
            "status": PayrollRun.STATUS_PROCESSED,
            "notes": "Demo payroll processed for sales walkthrough.",
            "processed_at": timezone.now() - timedelta(days=1),
        },
    )


def _ensure_fixed_assets(company, ledgers, today):
    group, _ = AssetGroup.objects.update_or_create(
        company=company,
        name="Office Technology",
        defaults={
            "asset_ledger": ledgers["computers"],
            "depreciation_ledger": ledgers["depreciation"],
            "accumulated_depr_ledger": ledgers["accum_depr"],
            "is_active": True,
        },
    )
    assets = [
        ("DEMO-LAP-01", "Partner Review Laptop", Decimal("95000.00")),
        ("DEMO-SCN-01", "Document OCR Scanner", Decimal("72000.00")),
    ]
    for code, name, value in assets:
        asset = FixedAsset.objects.filter(company=company, asset_code=code).first()
        defaults = {
            "asset_group": group,
            "name": name,
            "purchase_date": today - timedelta(days=210),
            "purchase_value": value,
            "salvage_value": Decimal("5000.00"),
            "useful_life_years": 3,
            "depreciation_method": FixedAsset.METHOD_SLM,
            "location": "Demo office",
            "serial_number": f"AV-{code}",
            "status": FixedAsset.STATUS_ACTIVE,
            "notes": "Seeded for demo asset register and depreciation walkthrough.",
        }
        if asset:
            for field, value in defaults.items():
                setattr(asset, field, value)
            asset.save()
        else:
            FixedAsset.objects.create(company=company, asset_code=code, **defaults)


def _ensure_tds_sections(company, ledgers):
    section, _ = TDSSection.objects.update_or_create(
        company=company,
        section_code="194J",
        defaults={
            "nature": "TDS",
            "description": "Professional or technical services",
            "threshold": Decimal("30000.00"),
            "rate_individual": Decimal("10.00"),
            "rate_company": Decimal("10.00"),
            "is_active": True,
        },
    )
    return section


def _seed_lightweight_company_activity(company, spec, user, today, index, ledgers):
    current_start, current_end = _month_bounds(today, 0)
    prev_start, prev_end = _month_bounds(today, -1)
    two_back_start, two_back_end = _month_bounds(today, -2)

    _ensure_practice_task(
        company,
        f"DEMO-{spec['short_code']}-TASK-GST",
        {
            "title": f"{_period_label(prev_start)} GSTR-3B review",
            "task_type": PracticeTask.TYPE_GST,
            "priority": PracticeTask.PRIORITY_HIGH if index else PracticeTask.PRIORITY_CRITICAL,
            "status": PracticeTask.STATUS_IN_PROGRESS,
            "due_date": today + timedelta(days=2 + index),
            "period_start": prev_start,
            "period_end": prev_end,
            "assigned_to": user if user and user.is_authenticated else None,
            "created_by": user if user and user.is_authenticated else None,
            "description": "Demo task showing statutory ownership, due date, and review status.",
        },
    )
    _ensure_practice_task(
        company,
        f"DEMO-{spec['short_code']}-TASK-DOCS",
        {
            "title": "Chase bank statement and purchase register",
            "task_type": PracticeTask.TYPE_DOCUMENT,
            "priority": PracticeTask.PRIORITY_NORMAL,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": today - timedelta(days=1 + index),
            "period_start": prev_start,
            "period_end": prev_end,
            "assigned_to": user if user and user.is_authenticated else None,
            "created_by": user if user and user.is_authenticated else None,
            "description": "Demo client-request blocker for portal and reminder walkthrough.",
        },
    )
    _ensure_compliance_filing(
        company,
        f"DEMO-{spec['short_code']}-GSTR1-{prev_start:%Y%m}",
        {
            "filing_type": ComplianceFiling.TYPE_GSTR1,
            "title": f"GSTR-1 {_period_label(prev_start)}",
            "status": ComplianceFiling.STATUS_READY_FOR_REVIEW if index != 1 else ComplianceFiling.STATUS_CLIENT_PENDING,
            "priority": PracticeTask.PRIORITY_HIGH,
            "period_start": prev_start,
            "period_end": prev_end,
            "due_date": today + timedelta(days=4 + index),
            "assigned_to": user if user and user.is_authenticated else None,
            "reviewer": user if user and user.is_authenticated else None,
            "created_by": user if user and user.is_authenticated else None,
            "source": ComplianceFiling.SOURCE_IMPORT,
            "portal_status": "Draft prepared from demo books",
            "notes": "Sales demo filing with pending review signals.",
        },
    )
    _ensure_compliance_filing(
        company,
        f"DEMO-{spec['short_code']}-TDS26Q-{two_back_start:%Y%m}",
        {
            "filing_type": ComplianceFiling.TYPE_TDS_26Q,
            "title": f"TDS 26Q Q4 review pack",
            "status": ComplianceFiling.STATUS_IN_PROGRESS,
            "priority": PracticeTask.PRIORITY_NORMAL,
            "period_start": two_back_start,
            "period_end": prev_end,
            "due_date": today + timedelta(days=12 + index),
            "assigned_to": user if user and user.is_authenticated else None,
            "reviewer": user if user and user.is_authenticated else None,
            "created_by": user if user and user.is_authenticated else None,
            "source": ComplianceFiling.SOURCE_MANUAL,
            "portal_status": "Challan reconciliation pending",
            "notes": "Demo TDS filing workpaper path.",
        },
    )
    _ensure_notice(
        company,
        f"DEMO-{spec['short_code']}-GST-NOTICE",
        {
            "notice_type": ComplianceNotice.TYPE_GST,
            "title": "GST ASMT-10 variance explanation",
            "issue_date": today - timedelta(days=18 + index),
            "response_due_date": today + timedelta(days=5 - index),
            "status": ComplianceNotice.STATUS_DATA_PENDING if index != 2 else ComplianceNotice.STATUS_ESCALATED,
            "priority": PracticeTask.PRIORITY_HIGH if index != 2 else PracticeTask.PRIORITY_CRITICAL,
            "assigned_to": user if user and user.is_authenticated else None,
            "created_by": user if user and user.is_authenticated else None,
            "portal_status": "Response draft under preparation",
            "description": "Demo notice for response tracking and escalation workflow.",
            "response_summary": "Books-to-portal reconciliation is being assembled.",
        },
    )
    _ensure_client_document_request(
        company,
        f"DEMO-{spec['short_code']}-DOC-BANK",
        {
            "title": f"Bank statement for {_period_label(prev_start)}",
            "document_type": "bank",
            "status": "open",
            "due_date": today - timedelta(days=index),
            "recipient_email": f"client.demo{company.pk}@example.test",
            "recipient_whatsapp_number": company.whatsapp_intake_number,
            "requested_by": user if user and user.is_authenticated else None,
            "notes": "Demo request room for reminder workflow.",
        },
    )
    _ensure_gst_review_stack(company, user, prev_start, prev_end, index)
    _ensure_market_signals(company, user, today, spec, index)
    _ensure_tds_workpaper(company, user, today, prev_start, prev_end)


def _seed_primary_operating_data(company, user, today):
    ledgers = _ensure_ledgers(company)
    stock_items = {item.name: item for item in StockItem.objects.filter(company=company)}
    ops_center = CostCenter.objects.filter(company=company, name="Operations").first()
    sales_center = CostCenter.objects.filter(company=company, name="Sales Support").first()
    gst_center = CostCenter.objects.filter(company=company, name="GST Compliance Cell").first()

    sale_overdue = _ensure_voucher(
        company,
        "DEMO-SALES-OVERDUE",
        "Sales",
        today - timedelta(days=52),
        today - timedelta(days=22),
        "Demo overdue B2B invoice for collections and aging walkthrough.",
        [
            _line(ledgers["customer_metro"], "DR", "153400.00", "Invoice to Metro Wholesale Co."),
            _line(ledgers["sales_goods"], "CR", "130000.00", "Industrial sensor kits", stock_items.get("Industrial Sensor Kit"), Decimal("20.000"), Decimal("6500.00"), sales_center),
            _line(ledgers["cgst_output"], "CR", "11700.00", "Output CGST"),
            _line(ledgers["sgst_output"], "CR", "11700.00", "Output SGST"),
        ],
        status="APPROVED",
        user=user,
        place_of_supply="27",
        cgst_amount=Decimal("11700.00"),
        sgst_amount=Decimal("11700.00"),
        total_tax=Decimal("23400.00"),
        e_invoice_status="IRN_PENDING",
    )
    sale_recent = _ensure_voucher(
        company,
        "DEMO-SALES-PARTIAL",
        "Sales",
        today - timedelta(days=18),
        today + timedelta(days=7),
        "Demo current month B2B invoice with partial receipt.",
        [
            _line(ledgers["customer_kuber"], "DR", "118000.00", "Invoice to Kuber Retail"),
            _line(ledgers["sales_goods"], "CR", "100000.00", "Smart meter controllers", stock_items.get("Smart Meter Controller"), Decimal("40.000"), Decimal("2500.00"), sales_center),
            _line(ledgers["cgst_output"], "CR", "9000.00", "Output CGST"),
            _line(ledgers["sgst_output"], "CR", "9000.00", "Output SGST"),
        ],
        status="APPROVED",
        user=user,
        place_of_supply="27",
        cgst_amount=Decimal("9000.00"),
        sgst_amount=Decimal("9000.00"),
        total_tax=Decimal("18000.00"),
        e_invoice_status="IRN_GENERATED",
        e_invoice_irn="DEMOIRNMEHTA0001",
        e_invoice_ack_no="DEMOACK10001",
        e_invoice_ack_date=timezone.now() - timedelta(days=17),
        e_way_bill_status="GENERATED",
        e_way_bill_no="DEMOEWB10001",
        e_way_bill_valid_until=timezone.now() + timedelta(days=4),
    )
    sale_interstate = _ensure_voucher(
        company,
        "DEMO-SALES-INTERSTATE",
        "Sales",
        today - timedelta(days=3),
        today + timedelta(days=27),
        "Demo interstate e-invoice/e-way bill pending action.",
        [
            _line(ledgers["customer_lotus"], "DR", "177000.00", "Interstate supply to Lotus"),
            _line(ledgers["sales_services"], "CR", "150000.00", "Warranty service packs", stock_items.get("Warranty Service Pack"), Decimal("80.000"), Decimal("1875.00"), sales_center),
            _line(ledgers["igst_output"], "CR", "27000.00", "Output IGST"),
        ],
        status="APPROVED",
        user=user,
        place_of_supply="29",
        igst_amount=Decimal("27000.00"),
        total_tax=Decimal("27000.00"),
        e_invoice_status="PENDING_REVIEW",
        e_way_bill_status="READY",
    )
    purchase_msme = _ensure_voucher(
        company,
        "DEMO-PURCHASE-MSME",
        "Purchase",
        today - timedelta(days=48),
        today - timedelta(days=4),
        "Demo MSME purchase invoice now close to payment risk.",
        [
            _line(ledgers["purchase_goods"], "DR", "82000.00", "Trading goods", cost_center=ops_center),
            _line(ledgers["cgst_input"], "DR", "7380.00", "Input CGST"),
            _line(ledgers["sgst_input"], "DR", "7380.00", "Input SGST"),
            _line(ledgers["supplier_msme"], "CR", "96760.00", "MSME Steel Works payable"),
        ],
        status="APPROVED",
        user=user,
        place_of_supply="27",
        cgst_amount=Decimal("7380.00"),
        sgst_amount=Decimal("7380.00"),
        total_tax=Decimal("14760.00"),
    )
    purchase_packaging = _ensure_voucher(
        company,
        "DEMO-PURCHASE-PACKAGING",
        "Purchase",
        today - timedelta(days=12),
        today + timedelta(days=18),
        "Demo vendor purchase matched to GST input.",
        [
            _line(ledgers["purchase_packaging"], "DR", "45000.00", "Packaging material", cost_center=ops_center),
            _line(ledgers["cgst_input"], "DR", "4050.00", "Input CGST"),
            _line(ledgers["sgst_input"], "DR", "4050.00", "Input SGST"),
            _line(ledgers["supplier_packaging"], "CR", "53100.00", "Vendor payable"),
        ],
        status="APPROVED",
        user=user,
        place_of_supply="27",
        cgst_amount=Decimal("4050.00"),
        sgst_amount=Decimal("4050.00"),
        total_tax=Decimal("8100.00"),
        is_itc_claimed=True,
    )
    professional_fee = _ensure_voucher(
        company,
        "DEMO-PURCHASE-TDS",
        "Purchase",
        today - timedelta(days=8),
        today + timedelta(days=22),
        "Demo professional fee invoice with TDS deduction.",
        [
            _line(ledgers["professional_fees"], "DR", "100000.00", "GST advisory support", cost_center=gst_center),
            _line(ledgers["cgst_input"], "DR", "9000.00", "Input CGST"),
            _line(ledgers["sgst_input"], "DR", "9000.00", "Input SGST"),
            _line(ledgers["supplier_consultant"], "CR", "108000.00", "Net payable after TDS"),
            _line(ledgers["tds_payable_194j"], "CR", "10000.00", "TDS Payable 194J"),
        ],
        status="APPROVED",
        user=user,
        place_of_supply="27",
        cgst_amount=Decimal("9000.00"),
        sgst_amount=Decimal("9000.00"),
        total_tax=Decimal("18000.00"),
    )

    _ensure_voucher(
        company,
        "DEMO-RECEIPT-PARTIAL",
        "Receipt",
        today - timedelta(days=4),
        None,
        "Partial receipt against Kuber Retail demo invoice.",
        [
            _line(ledgers["bank"], "DR", "60000.00", "Bank receipt"),
            _line(ledgers["customer_kuber"], "CR", "60000.00", "Receipt allocation", reference_voucher=sale_recent),
        ],
        status="APPROVED",
        user=user,
    )
    _ensure_voucher(
        company,
        "DEMO-PAYMENT-PACKAGING",
        "Payment",
        today - timedelta(days=2),
        None,
        "Vendor payment against packaging purchase.",
        [
            _line(ledgers["supplier_packaging"], "DR", "25000.00", "Part payment", reference_voucher=purchase_packaging),
            _line(ledgers["bank"], "CR", "25000.00", "Bank payment"),
        ],
        status="APPROVED",
        user=user,
    )
    _ensure_voucher(
        company,
        "DEMO-JOURNAL-SALARY",
        "Journal",
        today - timedelta(days=1),
        None,
        "Demo salary accrual posted from processed payroll.",
        [
            _line(ledgers["salary_expense"], "DR", "195000.00", "Monthly salary accrual", cost_center=ops_center),
            _line(ledgers["salary_payable"], "CR", "195000.00", "Salary payable"),
        ],
        status="APPROVED",
        user=user,
    )
    _ensure_voucher(
        company,
        "DEMO-DRAFT-BANK-CHARGES",
        "Journal",
        today,
        None,
        "Demo draft bank charge awaiting approval.",
        [
            _line(ledgers["bank_charges"], "DR", "1850.00", "Bank charges", cost_center=ops_center),
            _line(ledgers["bank"], "CR", "1850.00", "Bank charge deduction"),
        ],
        status="DRAFT",
        user=user,
    )

    _ensure_tds_entry(company, user, today, professional_fee, ledgers)
    sale_recent.sync_outstanding()
    sale_overdue.sync_outstanding()
    sale_interstate.sync_outstanding()
    purchase_msme.sync_outstanding()
    purchase_packaging.sync_outstanding()
    professional_fee.sync_outstanding()
    _ensure_orders(company, ledgers, stock_items, today)


def _seed_portfolio_operating_data(companies, user, today):
    for index, company in enumerate(companies):
        prev_start, prev_end = _month_bounds(today, -1)
        _ensure_client_document_request(
            company,
            f"DEMO-{company.short_code}-DOC-UPLOADED",
            {
                "title": "Purchase invoices uploaded for GST review",
                "document_type": "gst_invoice",
                "status": "uploaded",
                "due_date": today - timedelta(days=2),
                "recipient_email": f"client.demo{company.pk}@example.test",
                "recipient_whatsapp_number": company.whatsapp_intake_number,
                "requested_by": user if user and user.is_authenticated else None,
                "notes": "Demo uploaded request that needs closure after review.",
                "response_note": "Client uploaded zip of purchase invoices.",
                "uploaded_at": timezone.now() - timedelta(hours=8 + index),
            },
        )
        _ensure_compliance_filing(
            company,
            f"DEMO-{company.short_code}-GSTR3B-{prev_start:%Y%m}",
            {
                "filing_type": ComplianceFiling.TYPE_GSTR3B,
                "title": f"GSTR-3B {_period_label(prev_start)}",
                "status": ComplianceFiling.STATUS_READY_FOR_REVIEW if index == 0 else ComplianceFiling.STATUS_IN_PROGRESS,
                "priority": PracticeTask.PRIORITY_CRITICAL if index == 2 else PracticeTask.PRIORITY_HIGH,
                "period_start": prev_start,
                "period_end": prev_end,
                "due_date": today + timedelta(days=2 + index),
                "assigned_to": user if user and user.is_authenticated else None,
                "reviewer": user if user and user.is_authenticated else None,
                "created_by": user if user and user.is_authenticated else None,
                "source": ComplianceFiling.SOURCE_CALENDAR,
                "portal_status": "Prepared from demo statutory calendar",
                "notes": "Demo filing due soon for dashboard and notification center.",
            },
        )


def _line(
    ledger,
    entry_type,
    amount,
    narration,
    stock_item=None,
    quantity=Decimal("0.000"),
    rate=Decimal("0.00"),
    cost_center=None,
    reference_voucher=None,
):
    return {
        "ledger": ledger,
        "entry_type": entry_type,
        "amount": Decimal(amount),
        "narration": narration,
        "stock_item": stock_item,
        "quantity": quantity,
        "rate": rate,
        "cost_center": cost_center,
        "reference_voucher": reference_voucher,
    }


def _ensure_voucher(
    company,
    source_reference,
    voucher_type,
    voucher_date,
    due_date,
    narration,
    lines,
    status="APPROVED",
    user=None,
    **extra_fields,
):
    existing = Voucher.objects.filter(
        company=company,
        source_system=DEMO_SOURCE,
        source_reference=source_reference,
    ).first()
    if existing:
        return existing

    initial_status = "REJECTED" if voucher_type == "Payment" else "DRAFT"
    voucher = Voucher.objects.create(
        company=company,
        voucher_type=voucher_type,
        date=voucher_date,
        due_date=due_date,
        narration=narration,
        status=initial_status,
        source_system=DEMO_SOURCE,
        source_reference=source_reference,
        **extra_fields,
    )
    for line in lines:
        VoucherItem.objects.create(voucher=voucher, **line)
    voucher.validate_balance()

    update_payload = {"status": status}
    if status == "APPROVED":
        update_payload["verified_by_id"] = user.pk if user and user.is_authenticated else None
        update_payload["verified_at"] = timezone.now()
    Voucher.objects.filter(pk=voucher.pk).update(**update_payload)
    voucher.refresh_from_db()
    if voucher.voucher_type in {"Sales", "Purchase"}:
        voucher.sync_outstanding()
        voucher.refresh_from_db()
    return voucher


def _ensure_orders(company, ledgers, stock_items, today):
    sales_order = Order.objects.filter(company=company, narration__icontains="DEMO-SO-PRIMARY").first()
    if not sales_order:
        sales_order = Order.objects.create(
            company=company,
            order_type="Sales",
            party_ledger=ledgers["customer_lotus"],
            order_date=today - timedelta(days=5),
            expected_date=today + timedelta(days=6),
            status=Order.STATUS_CONFIRMED,
            narration="DEMO-SO-PRIMARY: Lotus rollout order for sales walkthrough.",
        )
        OrderItem.objects.create(
            order=sales_order,
            stock_item=stock_items.get("Smart Meter Controller"),
            quantity=Decimal("60.000"),
            rate=Decimal("2650.00"),
            fulfilled_qty=Decimal("20.000"),
        )
    purchase_order = Order.objects.filter(company=company, narration__icontains="DEMO-PO-PRIMARY").first()
    if not purchase_order:
        purchase_order = Order.objects.create(
            company=company,
            order_type="Purchase",
            party_ledger=ledgers["supplier_msme"],
            order_date=today - timedelta(days=7),
            expected_date=today + timedelta(days=3),
            status=Order.STATUS_PARTIAL,
            narration="DEMO-PO-PRIMARY: MSME replenishment purchase order.",
        )
        OrderItem.objects.create(
            order=purchase_order,
            stock_item=stock_items.get("Industrial Sensor Kit"),
            quantity=Decimal("50.000"),
            rate=Decimal("4200.00"),
            fulfilled_qty=Decimal("20.000"),
        )


def _ensure_practice_task(company, reference, defaults):
    PracticeTask.objects.update_or_create(
        company=company,
        reference=reference,
        defaults=defaults,
    )


def _ensure_compliance_filing(company, source_reference, defaults):
    ComplianceFiling.objects.update_or_create(
        company=company,
        source_reference=source_reference,
        defaults=defaults,
    )


def _ensure_notice(company, reference_number, defaults):
    ComplianceNotice.objects.update_or_create(
        company=company,
        reference_number=reference_number,
        defaults=defaults,
    )


def _ensure_client_document_request(company, source_reference, defaults):
    try:
        from portal.models import ClientDocumentRequest
    except ImportError:
        return None

    request = ClientDocumentRequest.objects.filter(
        company=company,
        source_reference=source_reference,
    ).first()
    if not request:
        return ClientDocumentRequest.objects.create(
            company=company,
            source_reference=source_reference,
            **defaults,
        )
    for field, value in defaults.items():
        setattr(request, field, value)
    request.save()
    return request


def _ensure_gst_review_stack(company, user, period_start, period_end, index):
    review, _ = GSTPeriodReview.objects.update_or_create(
        company=company,
        period_start=period_start,
        period_end=period_end,
        defaults={
            "status": GSTPeriodReview.STATUS_IN_REVIEW,
            "risk_score": 64 + (index * 8),
            "summary_snapshot": {
                "sales_value": "1285000.00",
                "itc_risk": "42000.00",
                "open_exceptions": 3 + index,
            },
            "notes": "Demo GST review with realistic exceptions and partner signoff flow.",
            "prepared_by": user if user and user.is_authenticated else None,
        },
    )
    filing_review, _ = FilingReview.objects.update_or_create(
        company=company,
        review_type=FilingReview.TYPE_GST_MONTHLY,
        period_start=period_start,
        period_end=period_end,
        defaults={
            "status": FilingReview.STATUS_UNDER_REVIEW if index != 1 else FilingReview.STATUS_SENT_BACK,
            "readiness_score": 82 - (index * 7),
            "risk_score": 28 + (index * 12),
            "blocker_snapshot": {
                "missing_documents": 2 + index,
                "unreconciled_itc": "18500.00",
                "sales_amendments": 1,
            },
            "notes": "Demo filing review for CA partner presentation.",
            "prepared_by": user if user and user.is_authenticated else None,
            "reviewed_by": user if user and user.is_authenticated else None,
        },
    )
    pack, _ = GSTFilingPack.objects.update_or_create(
        company=company,
        period_start=period_start,
        period_end=period_end,
        defaults={
            "review": filing_review,
            "status": GSTFilingPack.STATUS_READY,
            "summary_snapshot": {
                "gstr1_taxable": "1285000.00",
                "gstr3b_tax": "231300.00",
                "itc_claim": "168500.00",
            },
            "validation_snapshot": {"warnings": 2 + index, "blockers": index},
            "notes": "Demo GST pack ready for download and review.",
            "generated_by": user if user and user.is_authenticated else None,
        },
    )
    GSTPostFilingTracker.objects.update_or_create(
        company=company,
        period_start=period_start,
        period_end=period_end,
        defaults={
            "pack": pack,
            "gstr1_status": GSTPostFilingTracker.STATUS_FILED if index == 0 else GSTPostFilingTracker.STATUS_PENDING,
            "gstr1_arn": "DEMO-GSTR1-ARN-001" if index == 0 else "",
            "gstr1_filed_at": timezone.now() - timedelta(days=3) if index == 0 else None,
            "gstr3b_status": GSTPostFilingTracker.STATUS_PENDING,
            "ims_status": GSTPostFilingTracker.IMS_EXCEPTIONS if index == 2 else GSTPostFilingTracker.IMS_IN_PROGRESS,
            "payment_status": GSTPostFilingTracker.PAYMENT_SHORT_PAID if index == 2 else GSTPostFilingTracker.PAYMENT_PENDING,
            "itc_at_risk": Decimal("42000.00") + Decimal(index * 9000),
            "portal_evidence_reference": "Demo evidence vault path pending",
            "notes": "Demo post-filing tracker for ARN, IMS, challan, and evidence walkthrough.",
            "updated_by": user if user and user.is_authenticated else None,
        },
    )
    return review


def _ensure_market_signals(company, user, today, spec, index):
    PilotFeedback.objects.update_or_create(
        company=company,
        summary=f"Demo adoption signal - {spec['short_code']}",
        defaults={
            "feedback_type": PilotFeedback.TYPE_CONVERSION_SIGNAL,
            "sentiment": PilotFeedback.SENTIMENT_POSITIVE if index != 2 else PilotFeedback.SENTIMENT_NEUTRAL,
            "confidence_score": 9 - index,
            "severity": PilotFeedback.SEVERITY_MEDIUM if index != 2 else PilotFeedback.SEVERITY_HIGH,
            "status": PilotFeedback.STATUS_IN_PROGRESS if index == 2 else PilotFeedback.STATUS_OPEN,
            "detail": "CA partner demo signal showing Tally-exit objection handling and client adoption proof.",
            "client_contact": "Demo CFO",
            "competitor_reference": PilotFeedback.COMPETITOR_TALLY,
            "evidence_reference": f"DEMO-PILOT-EVIDENCE-{spec['short_code']}",
            "occurred_on": today - timedelta(days=5 + index),
            "assigned_to": user if user and user.is_authenticated else None,
            "recorded_by": user if user and user.is_authenticated else None,
        },
    )
    MarketProofCaseStudy.objects.update_or_create(
        company=company,
        title=f"{spec['short_code']} Tally exit demo case study",
        defaults={
            "status": MarketProofCaseStudy.STATUS_READY if index != 0 else MarketProofCaseStudy.STATUS_APPROVED,
            "outcome": MarketProofCaseStudy.OUTCOME_CONVERTED,
            "migration_source": MarketProofCaseStudy.SOURCE_TALLY,
            "client_contact": "Demo CFO",
            "client_role": "Finance Head",
            "testimonial_quote": "The demo workspace shows how month-end, GST, and client chase come together.",
            "publish_consent": index == 0,
            "anonymized": True,
            "consent_reference": f"DEMO-CONSENT-{spec['short_code']}" if index == 0 else "",
            "evidence_reference": f"DEMO-EVIDENCE-{spec['short_code']}",
            "before_process_hours": Decimal("42.00"),
            "after_process_hours": Decimal("18.00") + Decimal(index * 2),
            "monthly_documents": 420 + (index * 90),
            "monthly_invoices": 280 + (index * 40),
            "gst_periods_completed": 3 + index,
            "tally_parallel_run_days": 21 - (index * 3),
            "cutover_date": today - timedelta(days=30 - index),
            "commercial_value": spec["retainer"],
            "value_summary": "Demo proof of reduced client chase, faster GST review, and stronger partner visibility.",
            "owner": user if user and user.is_authenticated else None,
            "created_by": user if user and user.is_authenticated else None,
        },
    )
    MarketProofExternalEvidence.objects.update_or_create(
        company=company,
        title=f"{spec['short_code']} statutory and adoption proof",
        defaults={
            "category": MarketProofExternalEvidence.CATEGORY_PILOT,
            "status": MarketProofExternalEvidence.STATUS_VERIFIED if index == 0 else MarketProofExternalEvidence.STATUS_RECEIVED,
            "source": MarketProofExternalEvidence.SOURCE_CA,
            "evidence_reference": f"DEMO-EXT-EVIDENCE-{spec['short_code']}",
            "artifact_sha256": f"{company.pk:064x}"[-64:],
            "notes": "Demo market proof record for sales conversations.",
            "due_date": today + timedelta(days=6 + index),
            "expires_on": today + timedelta(days=120),
            "owner": user if user and user.is_authenticated else None,
            "verified_by": user if user and user.is_authenticated and index == 0 else None,
            "verified_at": timezone.now() - timedelta(days=1) if index == 0 else None,
            "created_by": user if user and user.is_authenticated else None,
        },
    )


def _ensure_tds_workpaper(company, user, today, period_start, period_end):
    fy_start = _financial_year_start(today).year
    TDSReturnWorkpaper.objects.update_or_create(
        company=company,
        form_type=TDSReturnWorkpaper.FORM_26Q,
        financial_year_start=fy_start,
        quarter=TDSReturnWorkpaper.Q1,
        defaults={
            "period_start": period_start,
            "period_end": period_end,
            "due_date": today + timedelta(days=18),
            "status": TDSReturnWorkpaper.STATUS_READY_FOR_REVIEW,
            "traces_statement_status": TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT,
            "challan_status": TDSReturnWorkpaper.CHALLAN_MATCHED,
            "form16_status": TDSReturnWorkpaper.FORM16_NOT_APPLICABLE,
            "fvu_status": TDSReturnWorkpaper.FVU_WARNINGS,
            "summary_snapshot": {
                "deduction_total": "10000.00",
                "challan_total": "10000.00",
                "deductees": 1,
            },
            "validation_snapshot": {
                "warnings": 1,
                "blockers": 0,
            },
            "prepared_by": user if user and user.is_authenticated else None,
            "reviewed_by": user if user and user.is_authenticated else None,
            "notes": "Demo TDS workpaper ready for partner review.",
        },
    )


def _ensure_tds_entry(company, user, today, voucher, ledgers):
    section, _ = TDSSection.objects.get_or_create(
        company=company,
        section_code="194J",
        defaults={
            "description": "Professional or technical services",
            "threshold": Decimal("30000.00"),
            "rate_company": Decimal("10.00"),
        },
    )
    entry = TDSEntry.objects.filter(
        company=company,
        voucher=voucher,
        section=section,
        deductee_ledger=ledgers["supplier_consultant"],
    ).first()
    defaults = {
        "tds_ledger": ledgers["tds_payable_194j"],
        "transaction_date": voucher.date,
        "deductee_type": "Company",
        "deductible_amount": Decimal("100000.00"),
        "rate_applied": Decimal("10.00"),
        "tds_amount": Decimal("10000.00"),
        "pan_number": "AAJFA3333P",
        "is_deposited": False,
        "notes": "Demo TDS entry from professional fee invoice.",
    }
    if entry:
        for field, value in defaults.items():
            setattr(entry, field, value)
        entry.save()
    else:
        TDSEntry.objects.create(
            company=company,
            voucher=voucher,
            section=section,
            deductee_ledger=ledgers["supplier_consultant"],
            **defaults,
        )
