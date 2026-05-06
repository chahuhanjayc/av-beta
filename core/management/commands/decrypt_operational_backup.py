import gzip
import hashlib
import json
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.backup_crypto import decrypt_backup_bytes


class Command(BaseCommand):
    help = "Decrypt an encrypted operational backup data archive using its manifest."

    def add_arguments(self, parser):
        parser.add_argument("manifest", help="Path to akshaya-manifest-*.json")
        parser.add_argument(
            "--output-file",
            help="Where to write the decrypted .json.gz archive. Defaults next to the manifest.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the output file if it already exists.",
        )

    def handle(self, *args, **options):
        manifest_path = Path(options["manifest"])
        if not manifest_path.exists():
            raise CommandError(f"Manifest not found: {manifest_path}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        data_path = manifest_path.parent / manifest.get("data_file", "")
        if not data_path.exists():
            raise CommandError(f"Backup data file not found: {data_path}")

        expected_data_sha = manifest.get("data_sha256", "")
        if expected_data_sha and self._file_sha256(data_path) != expected_data_sha:
            raise CommandError("Backup data SHA-256 does not match the manifest.")

        output_file = Path(options["output_file"]) if options["output_file"] else self._default_output_path(data_path)
        if output_file.exists() and not options["force"]:
            raise CommandError(f"Output file already exists: {output_file}. Use --force to overwrite.")
        output_file.parent.mkdir(parents=True, exist_ok=True)

        if manifest.get("encrypted"):
            try:
                decrypted = decrypt_backup_bytes(data_path.read_bytes(), manifest)
            except Exception as exc:
                raise CommandError(f"Decrypt failed: {exc}") from exc
            expected_compressed_sha = manifest.get("compressed_sha256", "")
            if expected_compressed_sha and hashlib.sha256(decrypted).hexdigest() != expected_compressed_sha:
                raise CommandError("Decrypted archive SHA-256 does not match the manifest.")
            output_file.write_bytes(decrypted)
        else:
            shutil.copyfile(data_path, output_file)

        self._verify_gzip(output_file)
        self.stdout.write(self.style.SUCCESS(f"Decrypted backup written: {output_file}"))

    def _default_output_path(self, data_path):
        name = data_path.name
        if name.endswith(".fernet"):
            name = name[:-7]
        elif not name.endswith(".gz"):
            name = f"{name}.json.gz"
        return data_path.with_name(f"decrypted-{name}")

    def _file_sha256(self, path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_gzip(self, path):
        try:
            with gzip.open(path, "rb") as handle:
                handle.read(1)
        except Exception as exc:
            raise CommandError(f"Decrypted output is not a valid gzip archive: {exc}") from exc
