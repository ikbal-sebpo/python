"""On-demand backfill: delete roster shifts dated past an employee's LWD.

No `Shift` may exist for a date after the employee's
``CustomUser.last_working_date`` — the person worked through their last day
and nothing beyond it. That rule is enforced at every write path
(``roster.services.prefill_shifts_from_pattern``,
``roster.upload.apply_upload``,
``leave.services.materialise_shifts_for_application``) and re-applied whenever
an LWD is set (``offboarding/signals.py``) or a leaver is deactivated
(``offboarding.services.deactivate_past_lwd_users``).

This command exists to clean up rows written *before* those guards landed.
It is deliberately NOT on the celery-beat schedule — the guards above keep the
data clean going forward, so a recurring sweep would only ever be a no-op.

Always preview first — this deletes data::

    python manage.py purge_shifts_past_lwd --dry-run
    python manage.py purge_shifts_past_lwd
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from offboarding.services import purge_shifts_past_lwd


class Command(BaseCommand):
    help = (
        "Delete roster Shift rows dated after each employee's "
        "CustomUser.last_working_date."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would be purged without making changes.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        rows = purge_shifts_past_lwd(dry_run=dry_run)
        if not rows:
            self.stdout.write("No shifts past any employee's last working date.")
            return
        total = 0
        for name, email, lwd, count in rows:
            total += count
            self.stdout.write(
                f"  · {name} <{email}> (LWD {lwd}) — {count} shift(s)"
            )
        verb = "would purge" if dry_run else "purged"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {total} shift(s) across {len(rows)} employee(s)."
        ))
