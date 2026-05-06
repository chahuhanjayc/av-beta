import json
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.urls import NoReverseMatch, reverse

from core.models import Company, UserCompanyAccess


SMOKE_URLS = [
    ("core:healthz", "Core health"),
    ("accounts:login", "Login page"),
    ("core:select_company", "Company selection"),
    ("core:dashboard", "Dashboard"),
    ("core:operations_monitor", "Operations Monitor"),
    ("core:system_observability", "System Observability"),
    ("core:go_live_certificate", "Go-Live Certificate"),
    ("core:go_live_evidence_pack", "Go-Live Evidence Pack"),
    ("core:production_trust_center", "Production Trust"),
    ("core:security_control", "Security Control"),
    ("core:accounting_close", "Accounting Close Workbench"),
    ("core:ca_command_center", "CA Command Center"),
    ("core:ca_approval_inbox", "CA Approval Inbox"),
    ("core:statutory_exposure", "Statutory Exposure"),
    ("core:statutory_export_center", "Statutory Export Center"),
    ("core:client_operating_readiness", "Operating Readiness"),
    ("core:client_pilot_launch", "Pilot Launch Control"),
    ("core:client_success_cockpit", "Client Success Cockpit"),
    ("core:client_portal_health", "Client Portal Health"),
    ("core:pilot_adoption_evidence", "Pilot Adoption Evidence"),
    ("core:pilot_feedback_register", "Pilot Feedback Register"),
    ("core:market_proof_pack", "Market Proof Pack"),
    ("core:market_proof_evidence_pack", "Market Proof Evidence Pack"),
    ("core:market_external_evidence", "External Evidence Register"),
    ("core:market_case_studies", "Market Case Studies"),
    ("core:partner_review_cockpit", "Partner Review Cockpit"),
    ("core:ca_client_profitability", "Client Profitability"),
    ("core:client_engagements", "Client Engagements"),
    ("core:filing_review_center", "Filing Review Center"),
    ("core:gst_filing_pack", "GST Filing Pack"),
    ("core:gst_post_filing_dashboard", "GST Post-Filing Dashboard"),
    ("core:gst_post_filing", "GST Post-Filing Center"),
    ("core:filing_readiness", "Filing Readiness"),
    ("core:compliance_calendar", "Compliance Calendar"),
    ("core:gst_workbench", "GST Workbench"),
    ("core:practice_tasks", "Practice Work Queue"),
    ("core:compliance_filings", "Compliance Filings"),
    ("core:compliance_notices", "Compliance Notices"),
    ("core:audit_log", "Audit Trail"),
    ("core:compliance_health", "Compliance Health"),
    ("migration:exit_control", "Tally Exit Control"),
    ("core:setup_wizard", "Setup Wizard"),
    ("core:demo_workspace", "Demo Mode"),
    ("core:app_settings", "App Settings"),
    ("core:company_settings", "Company Settings"),
    ("integrations:statutory_control", "Integration Control Room"),
    ("integrations:provider_readiness", "Provider Go-Live Readiness"),
    ("integrations:bank_feed_import", "Bank Feed Import"),
    ("integrations:traces_result_import", "TRACES Result Import"),
    ("integrations:dashboard", "Integrations"),
    ("ledger:list", "Ledgers"),
    ("ledger:create", "New Ledger"),
    ("vouchers:list", "Vouchers"),
    ("vouchers:quality", "Voucher Quality"),
    ("vouchers:create", "New Voucher"),
    ("core:collections_command_center", "Collections Center"),
    ("core:bank_reco_autopilot", "Bank Reco Autopilot"),
    ("vouchers:outstanding", "Outstanding Statement"),
    ("gstr2b:upload", "GSTR-2B Upload"),
    ("gstr2b:results", "GSTR-2B Results"),
    ("inventory:list", "Inventory Items"),
    ("inventory:summary", "Inventory Summary"),
    ("inventory:valuation", "Inventory Valuation"),
    ("inventory:godown_list", "Godowns"),
    ("inventory:batch_list", "Batches"),
    ("reports:home", "Reports Home"),
    ("reports:profit_loss_simple", "Profit and Loss"),
    ("reports:balance_sheet_simple", "Balance Sheet"),
    ("reports:trial_balance_simple", "Trial Balance"),
    ("reports:day_book", "Day Book"),
    ("reports:gst_report", "GST Report"),
    ("orders:order_list", "Orders"),
    ("costcenter:cost_center_list", "Cost Centers"),
    ("payroll:employee_list", "Payroll Employees"),
    ("fixedassets:asset_list", "Fixed Assets"),
    ("tds:return_workbench", "TDS Return Workbench"),
    ("tds:filing_pack", "TDS Filing Pack"),
    ("tds:post_filing_center", "TDS Post-Filing Center"),
    ("tds:entry_list", "TDS Entries"),
    ("tds:tds_register", "TDS Register"),
    ("portal:client_requests", "Client Requests"),
    ("portal:client_request_reminders", "Client Request Reminders"),
    ("portal:client_request_create", "New Client Request"),
    ("portal:ca_dashboard", "CA Client Portal Dashboard"),
]


class Command(BaseCommand):
    help = "Run an authenticated smoke pass against the real configured database."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        parser.add_argument("--host", default="127.0.0.1", help="Host header for the Django test client.")
        parser.add_argument("--user-email", help="Use this active user for authenticated page checks.")
        parser.add_argument("--company-id", type=int, help="Use this company id as the selected company.")

    def handle(self, *args, **options):
        user, company, temporary_access = self._resolve_user_and_company(options)
        client = Client(HTTP_HOST=options["host"])
        client.force_login(user)
        session = client.session
        session["current_company_id"] = company.pk
        session.save()

        results = []
        results.append(self._check_login_post(options["host"], company))
        for url_name, label in SMOKE_URLS:
            results.append(self._check_get(client, url_name, label))
        results.append(
            self._check_get_path(
                client,
                reverse("core:gst_workbench_detail", args=[company.pk, "2026-04"]),
                "GST Workbench Detail",
            )
        )

        if temporary_access:
            temporary_access.delete()

        failed = [item for item in results if not item["ok"]]
        payload = {
            "ok": not failed,
            "user": user.email,
            "company": company.name,
            "checked": len(results),
            "failed": len(failed),
            "results": results,
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            for item in results:
                style = self.style.SUCCESS if item["ok"] else self.style.ERROR
                self.stdout.write(style(f"{item['status']} {item['method']} {item['label']} {item['url']}"))
                if item.get("error"):
                    self.stdout.write(self.style.ERROR(f"  {item['error']}"))

        if failed:
            raise SystemExit(1)

    def _resolve_user_and_company(self, options):
        User = get_user_model()
        company = None
        if options["company_id"]:
            company = Company.objects.filter(pk=options["company_id"]).first()
            if not company:
                raise CommandError(f"Company id {options['company_id']} not found.")
        else:
            company = Company.objects.order_by("name").first()
        if not company:
            raise CommandError("No company exists for authenticated smoke checks.")

        user = None
        if options["user_email"]:
            user = User.objects.filter(email__iexact=options["user_email"], is_active=True).first()
            if not user:
                raise CommandError(f"Active user {options['user_email']} not found.")
        else:
            access = UserCompanyAccess.objects.select_related("user", "company").filter(
                user__is_active=True,
                company=company,
            ).first()
            if access:
                user = access.user
            else:
                user = User.objects.filter(is_active=True, is_superuser=True).order_by("email").first()
                if not user:
                    user = User.objects.filter(is_active=True).order_by("email").first()

        if not user:
            raise CommandError("No active user exists for authenticated smoke checks.")

        temporary_access = None
        if not UserCompanyAccess.objects.filter(user=user, company=company).exists():
            temporary_access = UserCompanyAccess.objects.create(user=user, company=company, role="Admin")
        return user, company, temporary_access

    def _check_login_post(self, host, company):
        User = get_user_model()
        email = f"live-smoke-{uuid.uuid4().hex[:12]}@example.test"
        password = f"Smoke-{uuid.uuid4().hex[:16]}-Pass"
        user = None
        client = Client(HTTP_HOST=host)
        try:
            user = User.objects.create_user(email=email, password=password, is_active=True)
            UserCompanyAccess.objects.create(user=user, company=company, role="Viewer")
            url = reverse("accounts:login") + "?next=/core/gst-workbench/"
            response = client.post(url, {"email": email, "password": password})
            return {
                "ok": response.status_code in {200, 302},
                "method": "POST",
                "label": "Login submit",
                "url": url,
                "status": response.status_code,
                "error": "" if response.status_code in {200, 302} else response.content[:300].decode("utf-8", errors="ignore"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "method": "POST",
                "label": "Login submit",
                "url": reverse("accounts:login"),
                "status": "ERR",
                "error": str(exc),
            }
        finally:
            if user:
                UserCompanyAccess.objects.filter(user=user).delete()
                user.delete()

    def _check_get(self, client, url_name, label):
        try:
            url = reverse(url_name)
        except NoReverseMatch as exc:
            return {"ok": False, "method": "GET", "label": label, "url": url_name, "status": "ERR", "error": str(exc)}

        try:
            response = client.get(url)
            expected_statuses = {200}
            if url_name == "accounts:login":
                expected_statuses.add(302)
            ok = response.status_code in expected_statuses
            return {
                "ok": ok,
                "method": "GET",
                "label": label,
                "url": url,
                "status": response.status_code,
                "error": "" if ok else response.content[:300].decode("utf-8", errors="ignore"),
            }
        except Exception as exc:
            return {"ok": False, "method": "GET", "label": label, "url": url, "status": "ERR", "error": str(exc)}

    def _check_get_path(self, client, url, label):
        try:
            response = client.get(url)
            ok = response.status_code == 200
            return {
                "ok": ok,
                "method": "GET",
                "label": label,
                "url": url,
                "status": response.status_code,
                "error": "" if ok else response.content[:300].decode("utf-8", errors="ignore"),
            }
        except Exception as exc:
            return {"ok": False, "method": "GET", "label": label, "url": url, "status": "ERR", "error": str(exc)}
