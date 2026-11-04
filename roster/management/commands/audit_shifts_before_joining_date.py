"""Read-only audit: report roster shifts dated before an employee's joining date.

No `Shift` may exist for a date before the employee's `CustomUser.joining_date`
— the mirror image of the last-working-date rule. That rule is enforced going
forward at every write path (`roster.services.prefill_shifts_from_pattern`,
`roster.upload.apply_upload`, `roster.api.shift_writes`,
`roster.views.CellUpdateView`), added 2026-09-17.

This command only reports rows written *before* those guards landed — it
never deletes anything. Safe to run in production. See docs/PLAN.md,
"Joining-date cutoff", for the history::

    python manage.py audit_shifts_before_joining_date
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from roster.services import audit_shifts_before_joining_date


class Command(BaseCommand):
    help = (
        "Report (never delete) roster Shift rows dated before each "
        "employee's CustomUser.joining_date."
    )

    def handle(self, *args, **opts):
        rows = audit_shifts_before_joining_date()
        if not rows:
            self.stdout.write("No shifts before any employee's joining date.")
            return
        total = 0
        for name, email, joining_date, count, earliest, latest in rows:
            total += count
            self.stdout.write(
                f"  · {name} <{email}> (joined {joining_date}) — "
                f"{count} shift(s), {earliest} to {latest}"
            )
        self.stdout.write(self.style.WARNING(
            f"{total} shift(s) before joining date, across {len(rows)} employee(s)."
        ))
