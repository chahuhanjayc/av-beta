"""Cross-client statutory filing export readiness."""

import csv
from datetime import date
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from tds.filing_pack import build_tds_filing_pack
from tds.models import TDSReturnWorkpaper
from tds.workbench import default_return_period, financial_year_label, quarter_dates, return_due_date

from .filing_pack import build_gst_filing_pack
from .models import PracticeTask


def default_gst_export_period(today=None):
    today = today or timezone.localdate()
    year = today.year
    month = today.month - 1
    if month == 0:
        month = 12
        year -= 1
    return _month_range(date(year, month, 1))


def parse_gst_export_period(raw_period=None, today=None):
    if raw_period:
        try:
            year_text, month_text = raw_period.split("-", 1)
            return _month_range(date(int(year_text), int(month_text), 1))
        except (TypeError, ValueError):
            pass
    return default_gst_export_period(today)


def default_tds_export_filters(today=None):
    fy_start, quarter = default_return_period(today)
    period_start, period_end = quarter_dates(fy_start, quarter)
    return {
        "fy_start": fy_start,
        "fy_label": financial_year_label(fy_start),
        "quarter": quarter,
        "form_type": TDSReturnWorkpaper.FORM_26Q,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": return_due_date(fy_start, quarter),
    }


def parse_tds_export_filters(params, today=None):
    defaults = default_tds_export_filters(today)
    fy_start = defaults["fy_start"]
    try:
        fy_start = int(str(params.get("fy") or params.get("fy_start") or fy_start)[:4])
    except (TypeError, ValueError):
        fy_start = defaults["fy_start"]

    valid_quarters = {choice[0] for choice in TDSReturnWorkpaper.QUARTER_CHOICES}
    valid_forms = {choice[0] for choice in TDSReturnWorkpaper.FORM_TYPE_CHOICES}
    quarter = (params.get("quarter") or defaults["quarter"]).strip().upper()
    form_type = (params.get("form_type") or defaults["form_type"]).strip().upper()
    if quarter not in valid_quarters:
        quarter = defaults["quarter"]
    if form_type not in valid_forms:
        form_type = defaults["form_type"]

    period_start, period_end = quarter_dates(fy_start, quarter)
    return {
        "fy_start": fy_start,
        "fy_label": financial_year_label(fy_start),
        "quarter": quarter,
        "form_type": form_type,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": return_due_date(fy_start, quarter),
    }


def build_statutory_export_center(companies, gst_period_start, gst_period_end, tds_filters):
    rows = []
    totals = {
        "clients": 0,
        "gst_ready": 0,
        "gst_filed": 0,
        "gst_blocked": 0,
        "tds_ready": 0,
        "tds_filed": 0,
        "tds_blocked": 0,
        "critical": 0,
        "warnings": 0,
        "download_ready": 0,
        "combined_score": 0,
    }

    for company in companies:
        gst = _gst_export_status(company, gst_period_start, gst_period_end)
        tds = _tds_export_status(company, tds_filters)
        score = round((gst["score"] + tds["score"]) / 2)
        row = {
            "company": company,
            "gst": gst,
            "tds": tds,
            "score": score,
            "score_class": _score_class(score),
            "attention_count": gst["critical_count"] + tds["critical_count"],
        }
        rows.append(row)

        totals["clients"] += 1
        totals["gst_ready"] += 1 if gst["can_generate"] else 0
        totals["gst_filed"] += 1 if gst["is_filed"] else 0
        totals["gst_blocked"] += 1 if gst["critical_count"] else 0
        totals["tds_ready"] += 1 if tds["can_generate"] else 0
        totals["tds_filed"] += 1 if tds["is_filed"] else 0
        totals["tds_blocked"] += 1 if tds["critical_count"] else 0
        totals["critical"] += gst["critical_count"] + tds["critical_count"]
        totals["warnings"] += gst["warning_count"] + tds["warning_count"]
        totals["download_ready"] += (1 if gst["can_generate"] else 0) + (1 if tds["can_generate"] else 0)
        totals["combined_score"] += score

    rows.sort(key=lambda item: (item["score"], -item["attention_count"], item["company"].name.lower()))
    totals["avg_score"] = round(totals["combined_score"] / totals["clients"]) if totals["clients"] else 0
    return {"rows": rows, "totals": totals}


def statutory_export_csv_response(center, gst_period_value, tds_filters):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="statutory_export_center.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "TAN",
        "GST Period",
        "GST Status",
        "GST Score",
        "GST Critical",
        "GST Warnings",
        "GST Export Ready",
        "GST Filed",
        "GST Primary Blocker",
        "TDS Return",
        "TDS Status",
        "TDS Score",
        "TDS Critical",
        "TDS Warnings",
        "TDS Deductee Rows",
        "TDS Challan Rows",
        "TDS Export Ready",
        "TDS Filed",
        "TDS Primary Blocker",
    ])
    tds_label = f"{tds_filters['form_type']} {tds_filters['quarter']} FY {tds_filters['fy_label']}"
    for row in center["rows"]:
        company = row["company"]
        writer.writerow([
            company.name,
            company.gstin or "",
            company.tan or "",
            gst_period_value,
            row["gst"]["status"],
            row["gst"]["score"],
            row["gst"]["critical_count"],
            row["gst"]["warning_count"],
            "Yes" if row["gst"]["can_generate"] else "No",
            "Yes" if row["gst"]["is_filed"] else "No",
            row["gst"]["primary_blocker"]["title"] if row["gst"]["primary_blocker"] else "",
            tds_label,
            row["tds"]["status"],
            row["tds"]["score"],
            row["tds"]["critical_count"],
            row["tds"]["warning_count"],
            row["tds"]["deductee_rows"],
            row["tds"]["challan_rows"],
            "Yes" if row["tds"]["can_generate"] else "No",
            "Yes" if row["tds"]["is_filed"] else "No",
            row["tds"]["primary_blocker"]["title"] if row["tds"]["primary_blocker"] else "",
        ])
    return response


def create_statutory_export_tasks(rows, user, manageable_company_ids):
    created = 0
    existing = 0
    for row in rows:
        company = row["company"]
        if company.pk not in manageable_company_ids:
            continue
        for kind in ("gst", "tds"):
            status = row[kind]
            for blocker in status["blockers"]:
                task, was_created = PracticeTask.objects.get_or_create(
                    company=company,
                    reference=_task_reference(kind, company.pk, status["period_key"], blocker["code"]),
                    defaults={
                        "title": f"{kind.upper()} export blocker: {blocker['title']}",
                        "task_type": PracticeTask.TYPE_GST if kind == "gst" else PracticeTask.TYPE_TDS,
                        "priority": PracticeTask.PRIORITY_CRITICAL,
                        "status": PracticeTask.STATUS_OPEN,
                        "due_date": status["due_date"],
                        "period_start": status["period_start"],
                        "period_end": status["period_end"],
                        "created_by": user,
                        "description": (
                            f"Resolve statutory export blocker for {status['label']}.\n"
                            f"Status: {status['status']}\n"
                            f"Issue: {blocker['description']}\n"
                            f"Action: {blocker.get('action_label') or 'Review filing pack'}"
                        ),
                    },
                )
                if was_created:
                    created += 1
                else:
                    existing += 1
    return {"created": created, "existing": existing}


def _gst_export_status(company, period_start, period_end):
    period_value = period_start.strftime("%Y-%m")
    try:
        pack = build_gst_filing_pack(company, period_start, period_end)
        blockers = _critical_items(pack["validations"])
        warnings = _warning_items(pack["validations"])
        can_generate = bool(pack["can_generate"])
        is_filed = bool(pack["pack_record"] and pack["pack_record"].is_filed)
        score = _readiness_score(pack["critical_count"], pack["warning_count"], can_generate, is_filed)
        links = _gst_links(company, period_value, can_generate)
        return {
            "label": f"GST {period_value}",
            "period_key": period_value,
            "period_start": period_start,
            "period_end": period_end,
            "due_date": None,
            "status": pack["status"],
            "score": score,
            "score_class": _score_class(score),
            "critical_count": pack["critical_count"],
            "warning_count": pack["warning_count"],
            "blockers": blockers,
            "warnings": warnings,
            "primary_blocker": blockers[0] if blockers else (warnings[0] if warnings else None),
            "can_generate": can_generate,
            "is_filed": is_filed,
            "links": links,
            "error": "",
        }
    except Exception as exc:
        return _error_status("GST", period_value, period_start, period_end, str(exc))


def _tds_export_status(company, filters):
    period_key = f"{filters['form_type']}-{filters['quarter']}-{filters['fy_label']}"
    try:
        pack = build_tds_filing_pack(
            company,
            filters["fy_start"],
            filters["quarter"],
            filters["form_type"],
        )
        blockers = _critical_items(pack["validations"])
        warnings = _warning_items(pack["validations"])
        can_generate = bool(pack["can_generate"])
        is_filed = bool(pack["pack_record"] and pack["pack_record"].is_filed)
        score = _readiness_score(pack["critical_count"], pack["warning_count"], can_generate, is_filed)
        links = _tds_links(company, filters, can_generate)
        return {
            "label": f"TDS {filters['form_type']} {filters['quarter']} FY {filters['fy_label']}",
            "period_key": period_key,
            "period_start": filters["period_start"],
            "period_end": filters["period_end"],
            "due_date": filters["due_date"],
            "status": pack["status"],
            "score": score,
            "score_class": _score_class(score),
            "critical_count": pack["critical_count"],
            "warning_count": pack["warning_count"],
            "blockers": blockers,
            "warnings": warnings,
            "primary_blocker": blockers[0] if blockers else (warnings[0] if warnings else None),
            "can_generate": can_generate,
            "is_filed": is_filed,
            "deductee_rows": len(pack["export_data"]["deductee_rows"]),
            "challan_rows": len(pack["export_data"]["challan_rows"]),
            "links": links,
            "error": "",
        }
    except Exception as exc:
        status = _error_status("TDS", period_key, filters["period_start"], filters["period_end"], str(exc))
        status.update({"deductee_rows": 0, "challan_rows": 0, "due_date": filters["due_date"]})
        return status


def _gst_links(company, period_value, can_generate):
    query = urlencode({"period": period_value, "company": company.pk})
    links = {
        "open": f"{reverse('core:gst_filing_pack')}?{query}",
        "xlsx": "",
        "json": "",
        "portal_json": "",
    }
    if can_generate:
        links["xlsx"] = f"{reverse('core:gst_filing_pack_download', args=['xlsx'])}?{query}"
        links["json"] = f"{reverse('core:gst_filing_pack_download', args=['json'])}?{query}"
        links["portal_json"] = f"{reverse('core:gst_filing_pack_download', args=['gstr1'])}?{query}"
    return links


def _tds_links(company, filters, can_generate):
    query = urlencode({
        "fy": filters["fy_start"],
        "quarter": filters["quarter"],
        "form_type": filters["form_type"],
    })
    open_url = f"{reverse('tds:filing_pack')}?{query}"
    links = {
        "open": _switch_url(company, open_url),
        "xlsx": "",
        "deductees": "",
        "challans": "",
        "json": "",
    }
    if can_generate:
        for kind in ("xlsx", "deductees", "challans", "json"):
            links[kind] = _switch_url(company, f"{reverse('tds:filing_pack_download', args=[kind])}?{query}")
    return links


def _switch_url(company, next_url):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': next_url})}"


def _critical_items(validations):
    return [_validation_item(item) for item in validations if item.get("severity") == "critical"]


def _warning_items(validations):
    return [_validation_item(item) for item in validations if item.get("severity") == "warning"]


def _validation_item(item):
    return {
        "code": item.get("code") or item.get("title") or "validation",
        "title": item.get("title") or "Validation",
        "count": item.get("count", 0),
        "description": item.get("description") or "",
        "action_label": item.get("action_label") or "Open",
        "action_url": item.get("action_url") or "",
        "severity": item.get("severity") or "warning",
    }


def _readiness_score(critical_count, warning_count, can_generate, is_filed=False):
    if is_filed:
        return 100
    score = 100 - (critical_count * 22) - (warning_count * 8)
    if not can_generate:
        score = min(score, 74)
    return max(0, min(100, score))


def _score_class(score):
    if score >= 80:
        return "success"
    if score >= 50:
        return "warning"
    return "danger"


def _error_status(kind, period_key, period_start, period_end, message):
    blocker = {
        "code": "build_error",
        "title": f"{kind} pack error",
        "count": 1,
        "description": message,
        "action_label": "Review configuration",
        "action_url": "",
        "severity": "critical",
    }
    return {
        "label": f"{kind} {period_key}",
        "period_key": period_key,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": None,
        "status": "Pack build failed",
        "score": 0,
        "score_class": "danger",
        "critical_count": 1,
        "warning_count": 0,
        "blockers": [blocker],
        "warnings": [],
        "primary_blocker": blocker,
        "can_generate": False,
        "is_filed": False,
        "links": {},
        "error": message,
    }


def _task_reference(kind, company_id, period_key, code):
    raw = f"STATEXPORT:{kind.upper()}:{company_id}:{period_key}:{code}"
    return raw[:120]


def _month_range(start):
    import calendar

    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start, end
