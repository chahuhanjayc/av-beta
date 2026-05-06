import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9/-]{1,16}$")
FP_RE = re.compile(r"^(0[1-9]|1[0-2])[0-9]{4}$")
STATE_CODE_RE = re.compile(r"^(0[1-9]|[1-2][0-9]|3[0-8]|97|99)$")

ZERO = Decimal("0.00")


def b2cl_limit_for_period(period_end):
    return Decimal("100000.00") if period_end and period_end >= date(2024, 8, 1) else Decimal("250000.00")
STANDARD_GST_RATES = {
    Decimal("0.00"),
    Decimal("0.10"),
    Decimal("0.25"),
    Decimal("1.00"),
    Decimal("1.50"),
    Decimal("3.00"),
    Decimal("5.00"),
    Decimal("6.00"),
    Decimal("7.50"),
    Decimal("12.00"),
    Decimal("18.00"),
    Decimal("28.00"),
}


def build_gstr1_portal_payload(pack):
    b2b = []
    b2b_grouped = defaultdict(list)
    for row in pack["gstr1"]["b2b"]:
        b2b_grouped[row["party_gstin"]].append(_portal_invoice(row))
    for gstin, invoices in sorted(b2b_grouped.items()):
        b2b.append({"ctin": gstin, "inv": invoices})

    b2cl = []
    b2cl_grouped = defaultdict(list)
    for row in pack["gstr1"]["b2cl"]:
        b2cl_grouped[row["pos"]].append(_portal_invoice(row))
    for pos, invoices in sorted(b2cl_grouped.items()):
        b2cl.append({"pos": pos, "inv": invoices})

    b2cs = [
        {
            "sply_ty": row["supply_type"],
            "typ": "OE",
            "pos": row["pos"],
            "rt": _money(row["rate"]),
            "txval": _money(row["taxable_value"]),
            "iamt": _money(row["igst"]),
            "camt": _money(row["cgst"]),
            "samt": _money(row["sgst"]),
            "csamt": _money(ZERO),
        }
        for row in pack["gstr1"]["b2cs"]
    ]

    hsn_rows = []
    for index, row in enumerate(pack["gstr1"]["hsn"], 1):
        hsn_rows.append({
            "num": index,
            "section": row["section"],
            "hsn_sc": row["hsn_code"],
            "desc": row["description"],
            "uqc": _uqc(row["uqc"]),
            "qty": _quantity(row["quantity"]),
            "rt": _money(row["rate"]),
            "txval": _money(row["taxable_value"]),
            "iamt": _money(row.get("igst", ZERO)),
            "camt": _money(row.get("cgst", ZERO)),
            "samt": _money(row.get("sgst", ZERO)),
            "csamt": _money(ZERO),
            "val": _money(row["taxable_value"] + row.get("total_tax", ZERO)),
        })

    documents = []
    for index, row in enumerate(pack["gstr1"]["documents"], 1):
        documents.append({
            "num": index,
            "from": row["from_no"],
            "to": row["to_no"],
            "totnum": row["total"],
            "cancel": row["cancelled"],
            "net_issue": row["net_issued"],
        })

    return {
        "gstin": (pack["company"].gstin or "").strip().upper(),
        "fp": pack["period_end"].strftime("%m%Y"),
        "gt": _money(ZERO),
        "cur_gt": _money(ZERO),
        "b2b": b2b,
        "b2cl": b2cl,
        "b2cs": b2cs,
        "hsn": {"data": hsn_rows},
        "doc_issue": {
            "doc_det": [{
                "doc_num": 1,
                "doc_typ": "Invoices for outward supply",
                "docs": documents,
            }]
        },
        "nil": {"inv": []},
    }


def validate_gstr1_portal_payload(payload, *, period_start=None, period_end=None):
    issues = []

    def add(severity, path, message):
        issues.append({"severity": severity, "path": path, "message": message})

    gstin = str(payload.get("gstin") or "").strip().upper()
    if not GSTIN_RE.match(gstin):
        add("critical", "gstin", "Supplier GSTIN is missing or invalid.")

    fp = str(payload.get("fp") or "")
    if not FP_RE.match(fp):
        add("critical", "fp", "Return period must be in MMYYYY format.")

    invoice_numbers = set()
    for group_index, group in enumerate(payload.get("b2b") or []):
        path = f"b2b[{group_index}]"
        ctin = str(group.get("ctin") or "").strip().upper()
        if not GSTIN_RE.match(ctin):
            add("critical", f"{path}.ctin", "B2B recipient GSTIN is missing or invalid.")
        if ctin == gstin and gstin:
            add("critical", f"{path}.ctin", "Recipient GSTIN cannot be the same as supplier GSTIN.")
        for invoice_index, invoice in enumerate(group.get("inv") or []):
            _validate_invoice(
                invoice,
                f"{path}.inv[{invoice_index}]",
                issues,
                invoice_numbers,
                period_start,
                period_end,
                require_registered=True,
            )

    for group_index, group in enumerate(payload.get("b2cl") or []):
        path = f"b2cl[{group_index}]"
        pos = str(group.get("pos") or "")
        if not STATE_CODE_RE.match(pos):
            add("critical", f"{path}.pos", "B2CL place of supply must be a valid two-digit state code.")
        for invoice_index, invoice in enumerate(group.get("inv") or []):
            _validate_invoice(
                invoice,
                f"{path}.inv[{invoice_index}]",
                issues,
                invoice_numbers,
                period_start,
                period_end,
                require_registered=False,
                require_b2cl_value=True,
            )

    for index, row in enumerate(payload.get("b2cs") or []):
        path = f"b2cs[{index}]"
        if row.get("sply_ty") not in {"INTER", "INTRA"}:
            add("critical", f"{path}.sply_ty", "B2CS supply type must be INTER or INTRA.")
        if not STATE_CODE_RE.match(str(row.get("pos") or "")):
            add("critical", f"{path}.pos", "B2CS place of supply must be a valid two-digit state code.")
        _check_amount(row.get("txval"), f"{path}.txval", issues, allow_zero=False)
        _check_amount(row.get("rt"), f"{path}.rt", issues, allow_zero=True, rate=True)
        _check_tax_split(row, path, issues)

    for index, row in enumerate((payload.get("hsn") or {}).get("data") or []):
        path = f"hsn.data[{index}]"
        if not str(row.get("hsn_sc") or "").strip():
            add("warning", f"{path}.hsn_sc", "HSN/SAC is missing for an HSN summary row.")
        if row.get("section") not in {"B2B", "B2C"}:
            add("warning", f"{path}.section", "HSN summary should be separated into B2B or B2C.")
        _check_amount(row.get("txval"), f"{path}.txval", issues, allow_zero=False)
        _check_amount(row.get("rt"), f"{path}.rt", issues, allow_zero=True, rate=True)

    doc_det = (payload.get("doc_issue") or {}).get("doc_det") or []
    for group_index, group in enumerate(doc_det):
        for doc_index, doc in enumerate(group.get("docs") or []):
            path = f"doc_issue.doc_det[{group_index}].docs[{doc_index}]"
            if doc.get("totnum", 0) < doc.get("cancel", 0):
                add("critical", path, "Cancelled documents cannot exceed total documents.")
            if doc.get("net_issue", 0) != doc.get("totnum", 0) - doc.get("cancel", 0):
                add("critical", path, "Net issued document count must equal total minus cancelled.")

    return issues


def summarize_schema_issues(issues):
    critical = sum(1 for issue in issues if issue["severity"] == "critical")
    warning = sum(1 for issue in issues if issue["severity"] == "warning")
    return {
        "critical_count": critical,
        "warning_count": warning,
        "total_count": critical + warning,
        "issues": issues,
    }


def _portal_invoice(row):
    return {
        "inum": row["invoice_number"],
        "idt": row["date"].strftime("%d-%m-%Y"),
        "val": _money(row["invoice_value"]),
        "pos": row["pos"],
        "rchrg": row["reverse_charge"],
        "inv_typ": "R",
        "itms": [{
            "num": 1,
            "itm_det": {
                "rt": _money(row["rate"]),
                "txval": _money(row["taxable_value"]),
                "iamt": _money(row["igst"]),
                "camt": _money(row["cgst"]),
                "samt": _money(row["sgst"]),
                "csamt": _money(ZERO),
            },
        }],
    }


def _validate_invoice(
    invoice,
    path,
    issues,
    invoice_numbers,
    period_start,
    period_end,
    *,
    require_registered,
    require_b2cl_value=False,
):
    inum = str(invoice.get("inum") or "")
    if not INVOICE_NO_RE.match(inum):
        issues.append({
            "severity": "critical",
            "path": f"{path}.inum",
            "message": "Invoice number must be 1-16 characters and use only letters, numbers, slash, or dash.",
        })
    if inum in invoice_numbers:
        issues.append({
            "severity": "critical",
            "path": f"{path}.inum",
            "message": "Duplicate invoice number exists in this GSTR-1 payload.",
        })
    invoice_numbers.add(inum)

    invoice_date = _parse_portal_date(invoice.get("idt"))
    if not invoice_date:
        issues.append({"severity": "critical", "path": f"{path}.idt", "message": "Invoice date must be DD-MM-YYYY."})
    elif period_start and period_end and not (period_start <= invoice_date <= period_end):
        issues.append({"severity": "critical", "path": f"{path}.idt", "message": "Invoice date is outside the selected return period."})

    value = _to_decimal(invoice.get("val"))
    _check_amount(invoice.get("val"), f"{path}.val", issues, allow_zero=False)
    b2cl_limit = b2cl_limit_for_period(period_end)
    if require_b2cl_value and value <= b2cl_limit:
        issues.append({
            "severity": "critical",
            "path": f"{path}.val",
            "message": f"B2CL invoices must be inter-state unregistered supplies above Rs.{b2cl_limit}.",
        })
    if invoice.get("rchrg") not in {"Y", "N"}:
        issues.append({"severity": "critical", "path": f"{path}.rchrg", "message": "Reverse charge flag must be Y or N."})
    if invoice.get("inv_typ") not in {"R", "DE", "SEWP", "SEWOP", "CBW"}:
        issues.append({"severity": "warning", "path": f"{path}.inv_typ", "message": "Invoice type should match the GST portal invoice type list."})
    if not STATE_CODE_RE.match(str(invoice.get("pos") or "")):
        issues.append({"severity": "critical", "path": f"{path}.pos", "message": "Place of supply must be a valid two-digit state code."})

    items = invoice.get("itms") or []
    if not items:
        issues.append({"severity": "critical", "path": f"{path}.itms", "message": "Invoice must have at least one item row."})
    for index, item in enumerate(items):
        item_path = f"{path}.itms[{index}].itm_det"
        detail = item.get("itm_det") or {}
        _check_amount(detail.get("txval"), f"{item_path}.txval", issues, allow_zero=False)
        _check_amount(detail.get("rt"), f"{item_path}.rt", issues, allow_zero=True, rate=True)
        _check_tax_split(detail, item_path, issues)

    if require_registered and not items:
        issues.append({"severity": "critical", "path": path, "message": "B2B invoice details are incomplete."})


def _check_tax_split(row, path, issues):
    for field in ("iamt", "camt", "samt", "csamt"):
        _check_amount(row.get(field, ZERO), f"{path}.{field}", issues, allow_zero=True)


def _check_amount(value, path, issues, *, allow_zero, rate=False):
    amount = _to_decimal(value)
    if amount < ZERO or (not allow_zero and amount == ZERO):
        issues.append({"severity": "critical", "path": path, "message": "Amount must be positive."})
        return
    if amount != amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        issues.append({"severity": "critical", "path": path, "message": "Amount must be rounded to two decimals."})
    if rate and amount not in STANDARD_GST_RATES:
        issues.append({"severity": "warning", "path": path, "message": "GST rate is outside the common GST rate master."})


def _parse_portal_date(value):
    try:
        return datetime.strptime(str(value), "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def _to_decimal(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("-1")


def _money(value):
    return float(_to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantity(value):
    return float(_to_decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _uqc(value):
    normalized = str(value or "NOS").strip().upper()
    mapping = {
        "NOS": "NOS",
        "NO": "NOS",
        "NUMBERS": "NOS",
        "PIECES": "PCS",
        "PCS": "PCS",
        "KGS": "KGS",
        "KG": "KGS",
        "BOXES": "BOX",
        "BOX": "BOX",
        "DOZEN": "DOZ",
        "METERS": "MTR",
        "METER": "MTR",
        "MTR": "MTR",
    }
    return mapping.get(normalized, normalized[:3] or "NOS")
