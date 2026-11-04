"""Smoke check for the Drive media store — credentials, scope, folder, key, cache.

    manage.py media_drive_probe

Prints who the token belongs to and the account's quota, then does what the
storage does: encrypts a tiny blob, uploads it into the root folder, reads it
back through the storage (so the cache + decrypt path is exercised), and
trashes it. Runs against the real Drive regardless of MEDIA_BACKEND, so it is
also the pre-flip check in Phase 2.
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from media_store import crypto
from media_store.drive import DriveError
from media_store.models import DriveObject
from media_store.storage import GoogleDriveStorage


class Command(BaseCommand):
    help = "Verify Drive credentials, root folder, encryption key and cache with a round-trip."

    def handle(self, *args, **options):
        storage = GoogleDriveStorage()
        try:
            about = storage.client.about()
        except (DriveError, OSError, KeyError) as exc:
            raise CommandError(f"credentials: {exc}") from exc
        self.stdout.write(f"signed in as: {about.get('user', {}).get('emailAddress')}")
        quota = about.get("storageQuota", {})
        if quota:
            used = int(quota.get("usage", 0)) / 2**30
            limit = quota.get("limit")
            self.stdout.write(
                f"drive quota: {used:,.2f} GiB used"
                + (f" of {int(limit) / 2**30:,.0f} GiB" if limit else " (no per-user limit reported)")
            )
        self.stdout.write(f"root folder: {settings.GOOGLE_DRIVE_ROOT_FOLDER_ID or '(unset)'}")
        self.stdout.write(f"media backend: {settings.MEDIA_BACKEND}; key fingerprint: {crypto.current_fingerprint()}")

        name = "probe/orbis-probe.bin"
        payload = b"orbis media probe"
        try:
            saved = storage._save(name, ContentFile(payload))
            got = storage.open(saved).read()
            storage.delete(saved)
        except DriveError as exc:
            DriveObject.objects.filter(name=name).delete()
            raise CommandError(f"round-trip failed: {exc}") from exc
        if got != payload:
            raise CommandError("round-trip read back different bytes")
        count, total = storage.cache.usage()
        self.stdout.write(self.style.SUCCESS(
            f"encrypt → upload → download → decrypt → trash: OK; "
            f"index rows: {DriveObject.objects.count()}; cache: {count} files, {total / 2**20:,.1f} MiB"
        ))
