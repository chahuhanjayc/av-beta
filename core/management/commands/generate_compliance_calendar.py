import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.compliance_calendar import generate_compliance_calendar
from core.models import Company


class Command(BaseCommand):
    help = "Generate recurring compliance filing records and linked practice tasks."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=3, help="Number of monthly periods to generate.")
        parser.add_argument("--from-date", help="First period month in YYYY-MM-DD format. Defaults to current month.")
        parser.add_argument("--company-id", action="append", type=int, help="Limit to one or more company ids.")
        parser.add_argument("--assign-to-email", help="Assign generated filings/tasks to this user email.")
        parser.add_argument("--reviewer-email", help="Set this user email as filing reviewer.")
        parser.add_argument("--due-month-offset", type=int, default=1, help="Month offset for monthly template due dates.")
        parser.add_argument("--gstr1-day", type=int, default=11, help="Template due day for GSTR-1.")
        parser.add_argument("--gstr3b-day", type=int, default=20, help="Template due day for GSTR-3B.")
        parser.add_argument("--tds-payment-day", type=int, default=7, help="Template due day for monthly TDS payment.")
        parser.add_argument("--ims-review-day", type=int, default=10, help="Internal target day for GST IMS review.")
        parser.add_argument("--skip-ims", action="store_true", help="Do not create IMS review work.")
        parser.add_argument("--skip-gstr1", action="store_true", help="Do not create GSTR-1 work.")
        parser.add_argument("--skip-gstr3b", action="store_true", help="Do not create GSTR-3B work.")
        parser.add_argument("--skip-tds-payment", action="store_true", help="Do not create monthly TDS payment work.")
        parser.add_argument("--skip-tds-returns", action="store_true", help="Do not create quarterly TDS 24Q/26Q work.")
        parser.add_argument("--gstr9-due", help="Create GSTR-9 annual filing due on this YYYY-MM-DD date.")
        parser.add_argument("--gstr9c-due", help="Create GSTR-9C annual filing due on this YYYY-MM-DD date.")
        parser.add_argument("--itr-due", help="Create ITR annual filing due on this YYYY-MM-DD date.")
        parser.add_argument("--tax-audit-due", help="Create tax audit filing due on this YYYY-MM-DD date.")
        parser.add_argument("--mca-aoc4-due", help="Create MCA AOC-4 annual filing due on this YYYY-MM-DD date.")
        parser.add_argument("--mca-mgt7-due", help="Create MCA MGT-7 annual filing due on this YYYY-MM-DD date.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be created without writing.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    def handle(self, *args, **options):
        companies = self._companies(options.get("company_id"))
        assigned_to = self._user_by_email(options.get("assign_to_email"), "assign-to")
        reviewer = self._user_by_email(options.get("reviewer_email"), "reviewer")

        try:
            payload = generate_compliance_calendar(
                companies=companies,
                months=options["months"],
                from_date=options.get("from_date"),
                assigned_to=assigned_to,
                reviewer=reviewer,
                dry_run=options["dry_run"],
                include_ims=not options["skip_ims"],
                include_gstr1=not options["skip_gstr1"],
                include_gstr3b=not options["skip_gstr3b"],
                include_tds_payment=not options["skip_tds_payment"],
                include_tds_returns=not options["skip_tds_returns"],
                due_month_offset=options["due_month_offset"],
                ims_review_day=options["ims_review_day"],
                gstr1_day=options["gstr1_day"],
                gstr3b_day=options["gstr3b_day"],
                tds_payment_day=options["tds_payment_day"],
                gstr9_due=options.get("gstr9_due"),
                gstr9c_due=options.get("gstr9c_due"),
                itr_due=options.get("itr_due"),
                tax_audit_due=options.get("tax_audit_due"),
                mca_aoc4_due=options.get("mca_aoc4_due"),
                mca_mgt7_due=options.get("mca_mgt7_due"),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2, default=str))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Created {payload['created']} filings/tasks; {payload['existing']} already existed."
            ))
            if options["dry_run"]:
                self.stdout.write("Dry run only. No records were saved.")

    def _companies(self, company_ids):
        qs = Company.objects.all().order_by("name")
        if company_ids:
            qs = qs.filter(pk__in=company_ids)
        if not qs.exists():
            raise CommandError("No companies matched the calendar generation scope.")
        return qs

    def _user_by_email(self, email, label):
        if not email:
            return None
        user = get_user_model().objects.filter(email__iexact=email).first()
        if not user:
            raise CommandError(f"No {label} user found for email {email}.")
        return user
