import gzip
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.backup_crypto import backup_encryption_configured, encrypt_backup_bytes


class Command(BaseCommand):
    help = "Export an operational JSON backup and media manifest for support/recovery drills."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=str(settings.BASE_DIR / "backups"),
            help="Directory where backup files will be written.",
        )
        parser.add_argument(
            "--include-sessions",
            action="store_true",
            help="Include django_session rows. Defaults to excluding sessions.",
        )
        parser.add_argument(
            "--encrypt",
            action="store_true",
            help="Encrypt the compressed data archive using BACKUP_ENCRYPTION_KEY or BACKUP_ENCRYPTION_PASSPHRASE.",
        )
        parser.add_argument(
            "--no-encrypt",
            action="store_true",
            help="Force a plain compressed backup even if encrypted backups are configured.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete older backup manifest/data pairs after this backup succeeds.",
        )
        parser.add_argument(
            "--retention-count",
            type=int,
            default=getattr(settings, "BACKUP_RETENTION_COUNT", 10),
            help="Number of recent manifest/data pairs to keep when --prune is used.",
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        encrypt = self._should_encrypt(options)
        retention_count = max(1, int(options["retention_count"] or 1))
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S-%f")
        data_suffix = ".json.gz.fernet" if encrypt else ".json.gz"
        data_path = output_dir / f"akshaya-data-{stamp}{data_suffix}"
        manifest_path = output_dir / f"akshaya-manifest-{stamp}.json"

        excludes = ["contenttypes", "auth.permission"]
        if not options["include_sessions"]:
            excludes.append("sessions")

        buffer = io.StringIO()
        call_command(
            "dumpdata",
            natural_foreign=True,
            natural_primary=True,
            exclude=excludes,
            stdout=buffer,
            verbosity=0,
        )

        compressed_payload = gzip.compress(buffer.getvalue().encode("utf-8"))
        compressed_sha256 = hashlib.sha256(compressed_payload).hexdigest()
        encryption_metadata = {
            "encrypted": False,
            "encryption_status": "not_configured",
            "encryption_algorithm": "",
            "encryption_verified": False,
        }
        if encrypt:
            try:
                data_payload, encryption_metadata = encrypt_backup_bytes(compressed_payload)
            except Exception as exc:
                raise CommandError(f"Encrypted backup failed: {exc}") from exc
        else:
            data_payload = compressed_payload

        data_path.write_bytes(data_payload)

        media_files = self._media_manifest()
        data_stat = data_path.stat()
        manifest = {
            "backup_schema_version": 2,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "data_file": data_path.name,
            "data_format": "json.gz.fernet" if encrypt else "json.gz",
            "compressed": True,
            "compressed_sha256": compressed_sha256,
            **encryption_metadata,
            "data_size": data_stat.st_size,
            "data_sha256": self._file_sha256(data_path),
            "database_engine": settings.DATABASES["default"]["ENGINE"],
            "include_sessions": bool(options["include_sessions"]),
            "media_root": str(settings.MEDIA_ROOT),
            "media_file_count": len(media_files),
            "media_total_bytes": sum(item["size"] for item in media_files),
            "media_files": media_files,
            "retention_policy": {
                "keep_manifests": retention_count,
                "cleanup_applied": bool(options["prune"]),
            },
            "retention_cleanup": {"deleted_manifests": 0, "deleted_data_files": 0, "deleted_files": []},
            "retention_hint": "Keep at least 3 recent manifests and one tested restore drill within the last 30 days.",
            "restore_hint": self._restore_hint(encrypt),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if options["prune"]:
            cleanup = self._prune_backups(output_dir, keep=retention_count)
            manifest["retention_cleanup"] = cleanup
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"Database backup written: {data_path}"))
        self.stdout.write(self.style.SUCCESS(f"Manifest written: {manifest_path}"))
        if options["prune"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Retention cleanup deleted {manifest['retention_cleanup']['deleted_manifests']} manifest(s) "
                    f"and {manifest['retention_cleanup']['deleted_data_files']} data file(s)."
                )
            )

    def _media_manifest(self):
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.exists():
            return []

        files = []
        for path in media_root.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            relative = path.relative_to(media_root).as_posix()
            files.append({
                "path": relative,
                "size": stat.st_size,
                "sha256": self._file_sha256(path),
                "modified": datetime.utcfromtimestamp(stat.st_mtime).isoformat(timespec="seconds") + "Z",
            })
        return files

    def _file_sha256(self, path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _should_encrypt(self, options):
        if options["encrypt"] and options["no_encrypt"]:
            raise CommandError("Use only one of --encrypt or --no-encrypt.")
        if options["encrypt"]:
            if not backup_encryption_configured():
                raise CommandError("Set BACKUP_ENCRYPTION_KEY or BACKUP_ENCRYPTION_PASSPHRASE before using --encrypt.")
            return True
        if options["no_encrypt"]:
            return False
        return bool(getattr(settings, "BACKUP_ENCRYPTION_DEFAULT", False) and backup_encryption_configured())

    def _restore_hint(self, encrypted):
        if encrypted:
            return (
                "Use: python manage.py decrypt_operational_backup <manifest.json> --output-file <backup.json.gz>, "
                "then gzip -d and python manage.py loaddata <backup.json>. Media files must be restored separately."
            )
        return "Use: gzip -d <backup.json.gz> and python manage.py loaddata <backup.json>. Media files must be restored separately."

    def _prune_backups(self, output_dir, *, keep):
        manifest_paths = sorted(
            output_dir.glob("akshaya-manifest-*.json"),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )
        keep_paths = set(manifest_paths[:keep])
        keep_data_files = set()
        for manifest_path in keep_paths:
            data_file = self._manifest_data_file(manifest_path)
            if data_file:
                keep_data_files.add(data_file)

        deleted_files = []
        deleted_manifests = 0
        deleted_data_files = 0
        for manifest_path in manifest_paths[keep:]:
            data_file = self._manifest_data_file(manifest_path)
            if data_file and data_file not in keep_data_files and data_file.exists():
                data_file.unlink()
                deleted_data_files += 1
                deleted_files.append(data_file.name)
            if manifest_path.exists():
                manifest_path.unlink()
                deleted_manifests += 1
                deleted_files.append(manifest_path.name)

        return {
            "deleted_manifests": deleted_manifests,
            "deleted_data_files": deleted_data_files,
            "deleted_files": deleted_files,
        }

    def _manifest_data_file(self, manifest_path):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        data_file = data.get("data_file")
        if not data_file:
            return None
        return manifest_path.parent / data_file
