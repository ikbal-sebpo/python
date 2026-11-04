"""Application-level field encryption for money at rest.

See docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.1. Fernet (AES-128-CBC +
HMAC-SHA256, random IV per write) under ``settings.PAYROLL_FIELD_KEYS`` — an
ordered list, newest first, so ``MultiFernet`` writes with the first key and
can still read values written under any older one until
``manage.py rotate_payroll_keys`` has re-saved them.

The key is deliberately separate from ``SECRET_KEY`` (rotating one never
forces the other) and lives only in ``.env``; the database holds tokens.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

_fernet: MultiFernet | None = None


class DecryptionError(ValueError):
    """A stored value is not a token this key set can open.

    Raised instead of ``cryptography``'s bare ``InvalidToken`` so the message
    can name the two realistic causes: the column was converted without
    purging (plaintext left behind — see the guarded purge migrations) or
    the key in ``.env`` is not the one the row was written with.
    """


def _build() -> MultiFernet:
    keys = [k.strip() for k in getattr(settings, "PAYROLL_FIELD_KEYS", []) or [] if k.strip()]
    if not keys:
        raise ImproperlyConfigured(
            "PAYROLL_FIELD_KEYS is not set. Generate one with `manage.py generate_payroll_key` "
            "and put it in the .env file."
        )
    fernets = []
    for index, key in enumerate(keys):
        try:
            fernets.append(Fernet(key.encode("ascii") if isinstance(key, str) else key))
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"PAYROLL_FIELD_KEYS entry #{index + 1} is not a valid Fernet key: {exc}"
            ) from exc
    return MultiFernet(fernets)


def get_fernet() -> MultiFernet:
    """The process-wide cipher, built once from settings."""
    global _fernet
    if _fernet is None:
        _fernet = _build()
    return _fernet


def reset_fernet_cache() -> None:
    global _fernet
    _fernet = None


@receiver(setting_changed)
def _on_setting_changed(*, setting, **kwargs):
    if setting == "PAYROLL_FIELD_KEYS":
        reset_fernet_cache()


def encrypt(text: str) -> str:
    """Plain text → Fernet token (ASCII), under the newest key."""
    return get_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Fernet token → plain text, trying every configured key."""
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError) as exc:
        raise DecryptionError(
            "Stored value is not a token this PAYROLL_FIELD_KEYS can open — either the row "
            "predates the encryption migrations (plaintext left in an encrypted column) or "
            "the key has changed without `manage.py rotate_payroll_keys`."
        ) from exc


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")
