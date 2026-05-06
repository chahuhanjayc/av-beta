from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.evidence_vault import seal_evidence_vault, verify_vault_chain
from core.models import Company


class Command(BaseCommand):
    help = "Append current company evidence into the immutable Evidence Vault ledger."

    def add_arguments(self, parser):
        parser.add_argument("--company-id", type=int, required=True)
        parser.add_argument("--user-email", default="")
        parser.add_argument("--vault-dir", default=None)
        parser.add_argument("--backup-dir", default=None)
        parser.add_argument("--verify-only", action="store_true")

    def handle(self, *args, **options):
        try:
            company = Company.objects.get(pk=options["company_id"])
        except Company.DoesNotExist as exc:
            raise CommandError(f"Company not found: {options['company_id']}") from exc

        user = None
        if options["user_email"]:
            user = get_user_model().objects.filter(email=options["user_email"]).first()

        if options["verify_only"]:
            verification = verify_vault_chain(company, output_dir=options["vault_dir"])
            self.stdout.write(self.style.SUCCESS(f"Evidence Vault status: {verification['status']}"))
            self.stdout.write(self.style.SUCCESS(f"Entries: {verification['entries']}"))
            self.stdout.write(self.style.SUCCESS(f"Head hash: {verification['head_hash'] or '-'}"))
            return

        result = seal_evidence_vault(
            company,
            user=user,
            output_dir=options["vault_dir"],
            backup_dir=options["backup_dir"],
        )
        verification = result["verification"]
        self.stdout.write(self.style.SUCCESS(f"Evidence Vault sealed: {result['created']} new, {result['skipped']} existing."))
        self.stdout.write(self.style.SUCCESS(f"Status: {verification['status']}"))
        self.stdout.write(self.style.SUCCESS(f"Head hash: {verification['head_hash'] or '-'}"))
