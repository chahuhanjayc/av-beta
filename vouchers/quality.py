from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Voucher


SALES_TYPES = {"Sales", "Sales Return"}
PURCHASE_TYPES = {"Purchase", "Purchase Return"}
GST_TYPES = SALES_TYPES | PURCHASE_TYPES

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class VoucherQualityIssue:
    code: str
    severity: str
    title: str
    message: str
    voucher: Voucher
    amount: Decimal = Decimal("0.00")
    party_name: str = ""
    task_type: str = "AUDIT"
    priority: str = "normal"

    @property
    def reference_key(self):
        return f"{self.code}:{self.voucher_id}"

    @property
    def voucher_id(self):
        return self.voucher.pk

    @property
    def voucher_url(self):
        return reverse("vouchers:detail", args=[self.voucher.pk])


def build_voucher_quality_report(
    company,
    *,
    start_date=None,
    end_date=None,
    status="open",
    voucher_type="",
    q="",
):
    vouchers = (
        Voucher.objects.filter(company=company)
        .prefetch_related("items__ledger__account_group", "items__stock_item__hsn_sac", "ocr_source")
        .order_by("-date", "-created_at")
    )

    if start_date:
        vouchers = vouchers.filter(date__gte=start_date)
    if end_date:
        vouchers = vouchers.filter(date__lte=end_date)
    if voucher_type:
        vouchers = vouchers.filter(voucher_type=voucher_type)
    if status == "open":
        vouchers = vouchers.exclude(status="APPROVED")
    elif status and status != "all":
        vouchers = vouchers.filter(status=status)
    if q:
        vouchers = vouchers.filter(
            Q(number__icontains=q)
            | Q(narration__icontains=q)
            | Q(source_reference__icontains=q)
            | Q(items__ledger__name__icontains=q)
        ).distinct()

    voucher_list = list(vouchers)
    duplicate_keys = _duplicate_reference_keys(voucher_list)
    issues = []
    clean_voucher_ids = set()

    for voucher in voucher_list:
        voucher_issues = _issues_for_voucher(voucher, duplicate_keys)
        if voucher_issues:
            issues.extend(voucher_issues)
        else:
            clean_voucher_ids.add(voucher.pk)

    issues.sort(
        key=lambda issue: (
            SEVERITY_ORDER.get(issue.severity, 9),
            issue.voucher.date or date.min,
            issue.voucher.number or "",
        ),
        reverse=False,
    )

    critical_count = sum(1 for issue in issues if issue.severity == "critical")
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    score = max(0, 100 - (critical_count * 10) - (warning_count * 4))

    return {
        "score": score,
        "issues": issues,
        "vouchers_checked": len(voucher_list),
        "clean_count": len(clean_voucher_ids),
        "critical_count": critical_count,
        "warning_count": warning_count,
        "status": _score_status(score),
        "by_code": _count_by_code(issues),
        "by_severity": {
            "critical": critical_count,
            "warning": warning_count,
        },
    }


def _issues_for_voucher(voucher, duplicate_keys):
    issues = []
    totals = _voucher_totals(voucher)
    amount = max(totals["debit"], totals["credit"])
    party = _party_ledger(voucher)
    party_name = party.name if party else ""

    def add(code, severity, title, message, task_type="AUDIT"):
        priority = "critical" if severity == "critical" else "high"
        issues.append(
            VoucherQualityIssue(
                code=code,
                severity=severity,
                title=title,
                message=message,
                voucher=voucher,
                amount=amount,
                party_name=party_name,
                task_type=task_type,
                priority=priority,
            )
        )

    if abs(totals["debit"] - totals["credit"]) > Decimal("0.01"):
        add(
            "unbalanced",
            "critical",
            "Unbalanced voucher",
            "Debit and credit totals do not match. Filing and reports should not use this voucher until fixed.",
        )

    if voucher.status != "APPROVED":
        age_days = (timezone.localdate() - voucher.date).days if voucher.date else 0
        severity = "critical" if age_days > 30 else "warning"
        add(
            "unapproved",
            severity,
            "Voucher not approved",
            f"Status is {voucher.get_status_display()}. Maker-checker approval is still pending.",
        )

    tax_sum = (voucher.cgst_amount or Decimal("0.00")) + (voucher.sgst_amount or Decimal("0.00")) + (
        voucher.igst_amount or Decimal("0.00")
    )
    if abs((voucher.total_tax or Decimal("0.00")) - tax_sum) > Decimal("1.00"):
        add(
            "tax_total_mismatch",
            "critical",
            "GST tax total mismatch",
            "CGST, SGST and IGST do not add up to the stored total tax amount.",
            task_type="GST",
        )

    if voucher.voucher_type in GST_TYPES and (voucher.total_tax or Decimal("0.00")) > 0:
        if not voucher.place_of_supply:
            add(
                "missing_place_of_supply",
                "warning",
                "Place of supply missing",
                "GST voucher has tax but no place of supply. This can break GSTR-1/3B classification.",
                task_type="GST",
            )

        if party and amount > Decimal("50000.00") and not party.gstin:
            add(
                "missing_party_gstin",
                "warning",
                "Party GSTIN missing",
                "High-value GST voucher is linked to a party ledger without GSTIN.",
                task_type="GST",
            )

        if party and party.gstin and voucher.company.gstin:
            party_state = party.gstin[:2]
            company_state = voucher.company.gstin[:2]
            if party_state == company_state and (voucher.igst_amount or Decimal("0.00")) > 0:
                add(
                    "gst_state_mismatch",
                    "warning",
                    "GST state tax mismatch",
                    "Same-state transaction has IGST. Review place of supply and tax split.",
                    task_type="GST",
                )
            if party_state != company_state and (
                (voucher.cgst_amount or Decimal("0.00")) > 0
                or (voucher.sgst_amount or Decimal("0.00")) > 0
            ):
                add(
                    "gst_state_mismatch",
                    "warning",
                    "GST state tax mismatch",
                    "Inter-state transaction has CGST/SGST. Review place of supply and tax split.",
                    task_type="GST",
                )

        if voucher.voucher_type in SALES_TYPES:
            missing_hsn_items = [
                item.stock_item.name for item in voucher.items.all()
                if (
                    item.stock_item_id
                    and item.ledger.account_group.nature == "Income"
                    and item.entry_type == "CR"
                    and not item.stock_item.hsn_sac_id
                )
            ]
            if missing_hsn_items:
                add(
                    "missing_hsn_sac",
                    "warning",
                    "HSN/SAC missing on stock item",
                    (
                        "Sales voucher uses stock item(s) without HSN/SAC: "
                        f"{', '.join(missing_hsn_items[:3])}. GSTR-1 HSN summary will be incomplete."
                    ),
                    task_type="GST",
                )

    if voucher.voucher_type in PURCHASE_TYPES and not (voucher.source_reference or "").strip():
        add(
            "missing_supplier_invoice_ref",
            "warning",
            "Supplier invoice number missing",
            "Purchase voucher has no supplier invoice/reference number.",
            task_type="GST",
        )

    duplicate_key = _duplicate_key(voucher, party)
    if duplicate_key and duplicate_key in duplicate_keys:
        add(
            "duplicate_source_reference",
            "critical",
            "Duplicate invoice reference",
            "Same voucher type, party and invoice/reference number appears more than once.",
            task_type="GST",
        )

    if amount > Decimal("10000.00") and not voucher.document and not _has_ocr_source(voucher):
        add(
            "missing_document",
            "warning",
            "Supporting document missing",
            "High-value voucher has no attached scan/PDF or OCR source.",
            task_type="DOCUMENT",
        )

    if voucher.po_mismatch_qty or voucher.po_mismatch_rate:
        add(
            "po_mismatch",
            "critical",
            "PO mismatch unresolved",
            "Voucher is flagged for purchase-order quantity or rate mismatch.",
        )

    if voucher.created_at and voucher.date:
        backdated_days = (voucher.created_at.date() - voucher.date).days
        if backdated_days > 30:
            add(
                "backdated_entry",
                "warning",
                "Backdated entry",
                f"Voucher was created {backdated_days} days after the voucher date.",
            )

    return issues


def _voucher_totals(voucher):
    debit = Decimal("0.00")
    credit = Decimal("0.00")
    for item in voucher.items.all():
        if item.entry_type == "DR":
            debit += item.amount or Decimal("0.00")
        elif item.entry_type == "CR":
            credit += item.amount or Decimal("0.00")
    return {"debit": debit, "credit": credit}


def _party_ledger(voucher):
    items = list(voucher.items.all())
    preferred_entry = "DR" if voucher.voucher_type in SALES_TYPES else "CR"
    preferred_natures = {"Asset"} if voucher.voucher_type in SALES_TYPES else {"Liability"}

    for item in items:
        group = getattr(item.ledger, "account_group", None)
        if item.entry_type == preferred_entry and group and group.nature in preferred_natures:
            return item.ledger

    for item in items:
        group = getattr(item.ledger, "account_group", None)
        if group and group.nature in {"Asset", "Liability"} and group.nature != "Tax":
            return item.ledger

    return None


def _duplicate_reference_keys(vouchers):
    counts = {}
    for voucher in vouchers:
        if voucher.voucher_type not in GST_TYPES:
            continue
        key = _duplicate_key(voucher, _party_ledger(voucher))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _duplicate_key(voucher, party):
    source_reference = (voucher.source_reference or "").strip().lower()
    if not source_reference or not party:
        return None
    return (voucher.voucher_type, party.pk, source_reference)


def _has_ocr_source(voucher):
    try:
        return bool(voucher.ocr_source)
    except Exception:
        return False


def _count_by_code(issues):
    counts = {}
    for issue in issues:
        counts[issue.code] = counts.get(issue.code, 0) + 1
    return counts


def _score_status(score):
    if score >= 90:
        return "Healthy"
    if score >= 70:
        return "Needs review"
    return "High risk"
