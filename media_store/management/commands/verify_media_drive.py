"""Every file the database references must exist in the index and on Drive.

    manage.py verify_media_drive              # index + Drive (one API call per blob)
    manage.py verify_media_drive --no-remote  # index only
    manage.py verify_media_drive --force      # even while MEDIA_BACKEND=filesystem

On the filesystem backend there is nothing to verify (the index is empty by
design), so the weekly beat job exits 0 with a "skipped" line unless
``--force`` — which is how it runs during the Phase 2 pre-flip check.

Walks every ``FileField``/``ImageField`` on every model, collects the
non-empty values, and checks each has a ``DriveObject`` row and (unless
``--no-remote``) that Drive still has the blob, un-trashed, with the md5 the
index recorded. Also reports index rows nothing references (orphans — not an
error; they are what a rolled-back upload leaves behind). Exits non-zero on
any missing file so the weekly beat job shows as failed in
``/admin/scheduler/scheduledtaskrun/``. docs/MEDIA_GOOGLE_DRIVE_PLAN.md §7.
"""

from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import FileField

from media_store.drive import DriveError, DriveNotFound
from media_store.models import DriveObject

from .migrate_media_to_drive import drive_storage


def referenced_names() -> dict[str, list[str]]:
    """``{file name: ["app.Model.field", ...]}`` for every stored reference."""
    refs: dict[str, list[str]] = {}
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if not isinstance(field, FileField):
                continue
            label = f"{model._meta.label}.{field.name}"
            qs = model._default_manager.exclude(**{field.attname: ""}).exclude(**{f"{field.attname}__isnull": True})
            for value in qs.values_list(field.attname, flat=True).iterator():
                refs.setdefault(value, []).append(label)
    return refs


class Command(BaseCommand):
    help = "Check every referenced media file is indexed and present on Drive."

    def add_arguments(self, parser):
        parser.add_argument("--no-remote", action="store_true", help="Skip the per-blob Drive check")
        parser.add_argument("--force", action="store_true", help="Run even on the filesystem backend")

    def handle(self, *args, **options):
        if settings.MEDIA_BACKEND != "drive" and not options["force"]:
            self.stdout.write("skipped: MEDIA_BACKEND is not 'drive' (pass --force to check anyway)")
            return
        refs = referenced_names()
        index = {row.name: row for row in DriveObject.objects.all()}
        missing_index = sorted(n for n in refs if n not in index)
        orphans = sorted(n for n in index if n not in refs)
        missing_remote: list[str] = []
        md5_drift: list[str] = []
        if not options["no_remote"]:
            client = drive_storage().client
            for name in sorted(refs):
                row = index.get(name)
                if row is None:
                    continue
                try:
                    meta = client.get(row.file_id, fields="id,trashed,md5Checksum")
                except DriveNotFound:
                    missing_remote.append(name)
                    continue
                except DriveError as exc:
                    raise CommandError(f"Drive error on {name}: {exc}") from exc
                if meta.get("trashed"):
                    missing_remote.append(name)
                elif row.md5 and meta.get("md5Checksum") and meta["md5Checksum"] != row.md5:
                    md5_drift.append(name)

        for name in missing_index:
            self.stderr.write(f"NOT INDEXED  {name}  <- {', '.join(refs[name])}")
        for name in missing_remote:
            self.stderr.write(f"NOT ON DRIVE {name}  <- {', '.join(refs[name])}")
        for name in md5_drift:
            self.stderr.write(f"MD5 CHANGED  {name}  (blob replaced outside Orbis?)")
        self.stdout.write(
            f"referenced {len(refs)}, indexed {len(index)}, orphans {len(orphans)}, "
            f"missing from index {len(missing_index)}, missing on Drive {len(missing_remote)}, "
            f"md5 drift {len(md5_drift)}"
        )
        problems = len(missing_index) + len(missing_remote) + len(md5_drift)
        if problems:
            raise CommandError(f"{problems} problem(s) — see above")
        self.stdout.write(self.style.SUCCESS("OK"))
