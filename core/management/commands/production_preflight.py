import json

from django.core.management.base import BaseCommand

from core.production_trust import production_preflight_results


class Command(BaseCommand):
    help = "Run operational preflight checks before exposing a production tenant."

    def add_arguments(self, parser):
        parser.add_argument(
            "--deploy",
            action="store_true",
            help="Include Django deployment checks.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )

    def handle(self, *args, **options):
        results = production_preflight_results(include_deploy=options["deploy"])
        failed = any(item["level"] == "error" for item in results)

        if options["json"]:
            self.stdout.write(json.dumps({"ok": not failed, "checks": results}, indent=2))
        else:
            for item in results:
                style = self.style.ERROR if item["level"] == "error" else self.style.WARNING if item["level"] == "warning" else self.style.SUCCESS
                self.stdout.write(style(f"{item['level'].upper()}: {item['name']} - {item['message']}"))

        if failed:
            raise SystemExit(1)
