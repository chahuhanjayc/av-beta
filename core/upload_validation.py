from pathlib import Path

from django.core.exceptions import ValidationError


MB = 1024 * 1024

DOCUMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".pdf"}
BANK_STATEMENT_EXTENSIONS = {".csv", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg"}
MIGRATION_EXTENSIONS = {".xlsx", ".xls", ".csv"}
JSON_EXTENSIONS = {".json"}

SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".bmp": (b"BM",),
    ".webp": (b"RIFF",),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}


def extension_for(uploaded_file):
    return Path(uploaded_file.name or "").suffix.lower()


def validate_uploaded_file(uploaded_file, *, allowed_extensions, max_mb, require_signature=True):
    if not uploaded_file:
        return uploaded_file

    ext = extension_for(uploaded_file)
    if ext not in allowed_extensions:
        raise ValidationError(
            f"Unsupported file type '{ext or 'unknown'}'. "
            f"Allowed: {', '.join(sorted(allowed_extensions))}."
        )

    if uploaded_file.size > max_mb * MB:
        raise ValidationError(f"File too large. Maximum allowed size is {max_mb} MB.")

    expected_signatures = SIGNATURES.get(ext)
    if require_signature and expected_signatures:
        position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else None
        try:
            header = uploaded_file.read(16)
        finally:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(position or 0)

        if not any(header.startswith(signature) for signature in expected_signatures):
            raise ValidationError(f"File content does not match the '{ext}' extension.")

        if ext == ".webp":
            position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else None
            try:
                header = uploaded_file.read(16)
            finally:
                if hasattr(uploaded_file, "seek"):
                    uploaded_file.seek(position or 0)
            if len(header) < 12 or header[8:12] != b"WEBP":
                raise ValidationError("File content does not match the '.webp' extension.")

    return uploaded_file
