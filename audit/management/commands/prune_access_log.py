"""Nightly: drop access-log rows older than the retention window.

Scheduled as the "Prune access log" periodic task (03:15 Asia/Dhaka, via
core.tasks.run_management_command — a django_celery_beat row managed in the
Django admin, see docs/SCHEDULER_ADMIN_PLAN.md; never also a crontab entry).
This is the ONLY code path that removes AccessLog rows: the model refuses
``delete()`` and the admin is read-only. Deletes in batches so the table
never holds a long lock. Also prints the last-24h row count, so a silent
stop of the middleware shows up in the run's captured output.

    python manage.py prune_access_log --dry-run
    python manage.py prune_access_log --days 365 --archive
"""

from __future__ import annotations

import datetime as dt
import gzip
import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from audit.models import AccessLog

BATCH = 10_000


class Command(BaseCommand):
    help = "Delete AccessLog rows older than ACCESS_LOG_RETENTION_DAYS (default 730)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="Override the retention window.")
        parser.add_argument("--dry-run", action="store_true", help="Count only; delete nothing.")
        parser.add_argument(
            "--archive", action="store_true",
            help="Write the pruned rows to logs/access-log-<YYYY-MM-DD>.jsonl.gz before deleting.",
        )

    def handle(self, *args, **opts):
        days = opts["days"] if opts["days"] is not None else int(getattr(settings, "ACCESS_LOG_RETENTION_DAYS", 730))
        now = timezone.now()
        cutoff = now - dt.timedelta(days=days)
        recent = AccessLog.objects.filter(occurred_at__gte=now - dt.timedelta(days=1)).count()
        self.stdout.write(f"Rows in the last 24h: {recent}")

        old = AccessLog.objects.filter(occurred_at__lt=cutoff)
        total = old.count()
        if not total:
            self.stdout.write(f"Nothing older than {days} days ({cutoff:%Y-%m-%d}).")
            return
        if opts["dry_run"]:
            self.stdout.write(f"[dry-run] would delete {total} row(s) older than {cutoff:%Y-%m-%d}.")
            return

        archive = None
        if opts["archive"]:
            path = settings.LOGS_DIR / f"access-log-{now:%Y-%m-%d}.jsonl.gz"
            archive = gzip.open(path, "at", encoding="utf-8")
            self.stdout.write(f"Archiving to {path}")

        deleted = 0
        try:
            while True:
                pks = list(old.order_by("pk").values_list("pk", flat=True)[:BATCH])
                if not pks:
                    break
                batch = AccessLog.objects.filter(pk__in=pks)
                if archive is not None:
                    for row in batch.values():
                        archive.write(json.dumps(row, cls=DjangoJSONEncoder) + "\n")
                # QuerySet.delete() bypasses the model's per-row refusal on purpose.
                count, _ = batch.delete()
                deleted += count
        finally:
            if archive is not None:
                archive.close()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s) older than {cutoff:%Y-%m-%d} ({days} days)."))
