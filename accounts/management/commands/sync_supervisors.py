"""Nightly batch: reconcile the ``Supervisors`` group.

Materialises "designation is Supervisor rank or above" into an auth.Group so
ProtectedView and dynamic_menu — which gate on groups and nothing else — can
express it. Membership is derived from Designation.access_level.rank and is
NOT hand-editable: anything added by hand in admin is removed on the next run.

Scheduled as the "Sync supervisors" periodic task (02:10 Asia/Dhaka — a
django_celery_beat row, see docs/SCHEDULER_ADMIN_PLAN.md).
There is no instant signal (see accounts.services.sync_supervisors), so a
promotion takes effect at the next run.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts.services import sync_supervisors


class Command(BaseCommand):
    help = "Reconcile the 'Supervisors' group against Designation.access_level."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report the changes without writing them.")

    def handle(self, *args, **opts):
        result = sync_supervisors(dry_run=opts["dry_run"])
        verb = "would change" if opts["dry_run"] else "changed"
        self.stdout.write(
            f"Supervisors: {result['total']} member(s) expected; "
            f"{verb} +{len(result['added'])} / -{len(result['removed'])}."
        )
        if not result["added"] and not result["removed"]:
            self.stdout.write(self.style.SUCCESS("already in sync."))
