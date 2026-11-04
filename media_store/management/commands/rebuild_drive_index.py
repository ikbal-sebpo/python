"""Regenerate ``DriveObject`` from Drive alone.

    manage.py rebuild_drive_index --dry-run
    manage.py rebuild_drive_index

Disaster recovery: a database restore that predates recent uploads loses
their index rows while the blobs are still on Drive. Every blob carries an
encrypted reference (name, MIME, plaintext size, key fingerprint) in its
Drive ``description``; this lists the root folder's Orbis-marked files,
decrypts each reference with ``MEDIA_ENCRYPTION_KEYS`` and upserts the row.
Two blobs claiming one name (an upload whose DB transaction rolled back, then
a re-upload) resolve to the newest ``createdTime``. Never deletes a row.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from media_store import crypto
from media_store.drive import DriveError
from media_store.models import DriveObject

from .migrate_media_to_drive import drive_storage


class Command(BaseCommand):
    help = "Rebuild the DriveObject index from the blobs' encrypted references on Drive."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        client = drive_storage().client
        try:
            blobs = list(client.list_blobs())
        except DriveError as exc:
            raise CommandError(str(exc)) from exc
        newest: dict[str, tuple[str, dict, dict]] = {}
        unreadable = 0
        for blob in blobs:
            try:
                ref = json.loads(crypto.decrypt_text(blob.get("description") or ""))
            except (crypto.MediaDecryptionError, ValueError):
                unreadable += 1
                continue
            created = blob.get("createdTime", "")
            current = newest.get(ref["name"])
            if current is None or created > current[0]:
                newest[ref["name"]] = (created, blob, ref)

        created_rows = updated = unchanged = 0
        for name, (_created, blob, ref) in sorted(newest.items()):
            values = {
                "file_id": blob["id"],
                "drive_name": blob["name"],
                "size": int(ref["size"]),
                "md5": blob.get("md5Checksum", "") or "",
                "mime": ref.get("mime", ""),
                "key_fingerprint": ref.get("fp", ""),
            }
            row = DriveObject.objects.filter(name=name).first()
            if row is None:
                created_rows += 1
                if not options["dry_run"]:
                    DriveObject.objects.create(name=name, **values)
            elif any(getattr(row, k) != v for k, v in values.items()):
                updated += 1
                if not options["dry_run"]:
                    for k, v in values.items():
                        setattr(row, k, v)
                    row.save(update_fields=list(values))
            else:
                unchanged += 1
        verb = "would create" if options["dry_run"] else "created"
        self.stdout.write(self.style.SUCCESS(
            f"blobs on Drive {len(blobs)}, distinct names {len(newest)}, unreadable {unreadable}; "
            f"{verb} {created_rows}, updated {updated}, unchanged {unchanged}"
        ))
        if unreadable:
            self.stderr.write(f"{unreadable} blob(s) carry a reference this key set cannot open")
