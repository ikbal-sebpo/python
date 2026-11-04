"""Whole-file encryption for media blobs.

Same primitive and key-list convention as ``core.crypto`` (Fernet under an
ordered, newest-first key list) but a **separate** key —
``settings.MEDIA_ENCRYPTION_KEYS`` — so rotating the payroll key never forces
a re-upload of every media blob, and vice versa. The key lives only in
``.env``; Drive holds tokens. Spec: docs/MEDIA_GOOGLE_DRIVE_PLAN.md §4.4.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

_fernet: MultiFernet | None = None


class MediaDecryptionError(ValueError):
    """A blob (or an index reference) is not a token this key set can open —
    the key in ``.env`` is not the one it was written with."""


def _keys() -> list[str]:
    keys = [k.strip() for k in getattr(settings, "MEDIA_ENCRYPTION_KEYS", []) or [] if k.strip()]
    if not keys:
        raise ImproperlyConfigured(
            "MEDIA_ENCRYPTION_KEYS is not set. Generate one with `manage.py generate_payroll_key` "
            "and put it in the .env file (comma-separated, newest first)."
        )
    return keys


def _build() -> MultiFernet:
    fernets = []
    for index, key in enumerate(_keys()):
        try:
            fernets.append(Fernet(key.encode("ascii")))
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"MEDIA_ENCRYPTION_KEYS entry #{index + 1} is not a valid Fernet key: {exc}"
            ) from exc
    return MultiFernet(fernets)


def get_fernet() -> MultiFernet:
    global _fernet
    if _fernet is None:
        _fernet = _build()
    return _fernet


def reset_fernet_cache() -> None:
    global _fernet
    _fernet = None


@receiver(setting_changed)
def _on_setting_changed(*, setting, **kwargs):
    if setting == "MEDIA_ENCRYPTION_KEYS":
        reset_fernet_cache()


def fingerprint(key: str) -> str:
    """Stable, non-reversible id of one key — stored on ``DriveObject`` so a
    rotation can tell which blobs still sit under an old key."""
    return hashlib.sha256(key.strip().encode("ascii")).hexdigest()[:16]


def current_fingerprint() -> str:
    return fingerprint(_keys()[0])


def encrypt_bytes(data: bytes) -> bytes:
    return get_fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return get_fernet().decrypt(token)
    except InvalidToken as exc:
        raise MediaDecryptionError(
            "Blob is not a token this MEDIA_ENCRYPTION_KEYS can open — the key has changed "
            "without `manage.py rotate_media_keys`, or the object on Drive was replaced."
        ) from exc


def encrypt_text(text: str) -> str:
    return encrypt_bytes(text.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    return decrypt_bytes(token.encode("ascii")).decode("utf-8")
