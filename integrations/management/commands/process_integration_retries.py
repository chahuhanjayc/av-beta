import json

from django.core.management.base import BaseCommand, CommandError

from core.models import Company
from integrations.models import IntegrationRequestLog
from integrations.retry_dispatcher import process_due_retry_jobs


class Command(BaseCommand):
    help = "Process due provider retry jobs for dispatchable statutory integrations."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company id or short code to process.")
        parser.add_argument("--service", choices=[key for key, _label in IntegrationRequestLog.SERVICE_CHOICES])
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        company = self._company(options.get("company"))
        result = process_due_retry_jobs(
            company=company,
            service=options.get("service") or "",
            limit=options["limit"],
            dry_run=options["dry_run"],
        )
        if options["as_json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        mode = "dry run" if result["dry_run"] else "processed"
        self.stdout.write(
            self.style.SUCCESS(
                f"Integration retries {mode}: {result['processed']} processed, "
                f"{result['resolved']} resolved, {result['failed']} failed, "
                f"{result['unsupported']} unsupported, {result['skipped']} skipped."
            )
        )

    def _company(self, value):
        if not value:
            return None
        lookup = {"pk": value} if str(value).isdigit() else {"short_code__iexact": value}
        try:
            return Company.objects.get(**lookup)
        except Company.DoesNotExist as exc:
            raise CommandError(f"Company not found: {value}") from exc
