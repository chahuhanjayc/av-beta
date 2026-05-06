import base64
import hashlib
import os

from django.conf import settings

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


BACKUP_KDF_ITERATIONS = 390000


def backup_encryption_configured():
    return bool(_raw_key() or _passphrase())


def encrypt_backup_bytes(payload):
    fernet, metadata = _backup_fernet()
    encrypted = fernet.encrypt(payload)
    metadata.update({
        "encrypted": True,
        "encryption_status": "encrypted",
        "encryption_algorithm": "fernet",
        "encryption_verified": fernet.decrypt(encrypted) == payload,
    })
    return encrypted, metadata


def decrypt_backup_bytes(payload, manifest):
    fernet, _metadata = _backup_fernet(salt=manifest.get("encryption_salt"))
    return fernet.decrypt(payload)


def _backup_fernet(*, salt=None):
    key = _raw_key()
    if key:
        key_bytes = key.encode("utf-8")
        fernet = Fernet(key_bytes)
        return fernet, {
            "encryption_key_source": "BACKUP_ENCRYPTION_KEY",
            "encryption_key_fingerprint": _fingerprint(key_bytes),
        }

    passphrase = _passphrase()
    if not passphrase:
        raise ValueError("Set BACKUP_ENCRYPTION_KEY or BACKUP_ENCRYPTION_PASSPHRASE before encrypted backups.")

    salt_bytes = _normalise_salt(salt)
    key_bytes = _derive_key(passphrase, salt_bytes)
    return Fernet(key_bytes), {
        "encryption_key_source": "BACKUP_ENCRYPTION_PASSPHRASE",
        "encryption_salt": base64.urlsafe_b64encode(salt_bytes).decode("ascii"),
        "encryption_kdf": "PBKDF2HMAC-SHA256",
        "encryption_kdf_iterations": BACKUP_KDF_ITERATIONS,
        "encryption_key_fingerprint": _fingerprint(key_bytes),
    }


def _raw_key():
    return (getattr(settings, "BACKUP_ENCRYPTION_KEY", "") or "").strip()


def _passphrase():
    return (getattr(settings, "BACKUP_ENCRYPTION_PASSPHRASE", "") or "").strip()


def _normalise_salt(salt):
    if not salt:
        return os.urandom(16)
    if isinstance(salt, bytes):
        return salt
    return base64.urlsafe_b64decode(salt.encode("ascii"))


def _derive_key(passphrase, salt):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=BACKUP_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _fingerprint(key_bytes):
    return hashlib.sha256(key_bytes).hexdigest()[:16]
