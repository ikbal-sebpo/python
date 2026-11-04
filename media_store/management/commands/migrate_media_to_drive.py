"""Copy the on-disk media tree into Google Drive (or back).

    manage.py migrate_media_to_drive --dry-run
    manage.py migrate_media_to_drive                 # MEDIA_ROOT → Drive, fills DriveObject
    manage.py migrate_media_to_drive --prefix leave/ # one subtree
    manage.py migrate_media_to_drive --reverse       # Drive → MEDIA_ROOT (rollback aid)

Idempotent: a name already in the index is skipped, so it is safe to run
while the site is still on the filesystem backend and again right before the
flip to catch late uploads. Names are kept exactly (``_save`` is called
directly, bypassing ``get_available_name``) because they are the FileField
values in the database. Phase 2 of docs/MEDIA_GOOGLE_DRIVE_PLAN.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from media_store.drive import DriveError
from media_store.models import DriveObject
from media_store.storage import GoogleDriveStorage


def drive_storage() -> GoogleDriveStorage:
    """The configured Drive storage when it is the default, else a fresh one
    built from settings (the pre-flip migration case)."""
    if isinstance(default_storage, GoogleDriveStorage):
        return default_storage
    return GoogleDriveStorage()


class Command(BaseCommand):
    help = "Copy MEDIA_ROOT into Google Drive (idempotent), or back with --reverse."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--prefix", default="", help="Only names starting with this (e.g. leave/)")
        parser.add_argument("--reverse", action="store_true", help="Drive → MEDIA_ROOT instead")

    def handle(self, *args, **options):
        root = Path(settings.MEDIA_ROOT)
        storage = drive_storage()
        if options["reverse"]:
            self._reverse(storage, root, options)
        else:
            self._forward(storage, root, options)

    def _forward(self, storage, root: Path, options):
        if not root.is_dir():
            raise CommandError(f"MEDIA_ROOT {root} does not exist")
        prefix = options["prefix"]
        existing = set(DriveObject.objects.values_list("name", flat=True))
        uploaded = skipped = failed = 0
        for dirpath, _dirs, files in os.walk(root):
            for filename in sorted(files):
                path = Path(dirpath) / filename
                name = path.relative_to(root).as_posix()
                if prefix and not name.startswith(prefix):
                    continue
                if name in existing:
                    skipped += 1
                    continue
                if options["dry_run"]:
                    self.stdout.write(f"would upload {name} ({path.stat().st_size} bytes)")
                    uploaded += 1
                    continue
                try:
                    with path.open("rb") as fh:
                        storage._save(name, File(fh, name=name))
                except DriveError as exc:
                    failed += 1
                    self.stderr.write(f"FAILED {name}: {exc}")
                    continue
                uploaded += 1
                existing.add(name)
        verb = "would upload" if options["dry_run"] else "uploaded"
        self.stdout.write(self.style.SUCCESS(f"{verb} {uploaded}, skipped {skipped} already indexed, failed {failed}"))
        if failed:
            raise CommandError(f"{failed} file(s) failed — re-run to retry just those")

    def _reverse(self, storage, root: Path, options):
        prefix = options["prefix"]
        written = skipped = failed = 0
        rows = DriveObject.objects.order_by("name")
        if prefix:
            rows = rows.filter(name__startswith=prefix)
        for row in rows.iterator():
            target = root / row.name
            if target.exists():
                skipped += 1
                continue
            if options["dry_run"]:
                self.stdout.write(f"would write {row.name} ({row.size} bytes)")
                written += 1
                continue
            try:
                data = storage.open(row.name).read()
            except DriveError as exc:
                failed += 1
                self.stderr.write(f"FAILED {row.name}: {exc}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written += 1
        verb = "would write" if options["dry_run"] else "wrote"
        self.stdout.write(self.style.SUCCESS(f"{verb} {written}, skipped {skipped} already on disk, failed {failed}"))
        if failed:
            raise CommandError(f"{failed} file(s) failed")
