"""Nightly batch: reconcile the ``Line Managers`` group.

dynamic_menu can gate on auth.Group and nothing else — its cache key is
built from the viewer's group pks, so a per-user predicate like "has direct
reports" can't be evaluated at render time. This command materialises that
predicate into a group so the "Pending acks" menu item reaches the people
who are actually assigned MANAGER_ACK steps.

Membership is derived from ``JobInfo.reports_to`` and is NOT hand-editable:
anything added by hand in admin is removed on the next run.

Scheduled as the "Sync line managers" periodic task (02:00 Asia/Dhaka — a
django_celery_beat row, see docs/SCHEDULER_ADMIN_PLAN.md).
``employees/signals.py`` keeps it fresh in real time on JobInfo saves; this
is the full reconcile that also catches bulk writes bypassing signals.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts.services import sync_line_managers


class Command(BaseCommand):
    help = "Reconcile the 'Line Managers' group against JobInfo.reports_to."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report the changes without writing them.",
        )

    def handle(self, *args, **opts):
        result = sync_line_managers(dry_run=opts["dry_run"])
        verb = "would change" if opts["dry_run"] else "changed"
        self.stdout.write(
            f"Line Managers: {result['total']} member(s) expected; "
            f"{verb} +{len(result['added'])} / -{len(result['removed'])}."
        )
        if not result["added"] and not result["removed"]:
            self.stdout.write(self.style.SUCCESS("already in sync."))
