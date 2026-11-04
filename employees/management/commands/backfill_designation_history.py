"""One-off: seed a current (open-ended) DesignationHistory row for every
employee who already has a designation but no history row yet.

Run this ONCE, right after deploying the DesignationHistory model + its
sync signal (employees/signals.py). From that point on, every future
designation change is captured automatically — this command only covers
employees whose *current* designation predates the feature.

Genuine historical promotion dates (e.g. an employee who joined in 2011 and
was promoted several times since) are NOT reconstructed by this command —
that data doesn't exist anywhere in Orbis. HR backfills those manually via
the "Designation history" section drawer on the employee's profile, at
their own pace.

Idempotent: only touches employees with no DesignationHistory row at all.

Usage:
    python manage.py backfill_designation_history
    python manage.py backfill_designation_history --dry-run
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from accounts.models import CustomUser
from employees.models import DesignationHistory


class Command(BaseCommand):
    help = "One-off: seed a current DesignationHistory row for employees who have a designation but no history yet."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing.")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))

        qs = (
            CustomUser.objects
            .filter(designation__isnull=False)
            .exclude(designation_history__isnull=False)
            .select_related("designation")
        )
        total = qs.count()
        self.stdout.write(f"{total} employee(s) with a designation but no history row yet.")

        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))
            for user in qs[:20]:
                effective_from = user.joining_date or date.today()
                self.stdout.write(f"  {user} · {user.designation} from {effective_from}")
            if total > 20:
                self.stdout.write(f"  ... and {total - 20} more")
            return

        created = 0
        for user in qs.iterator():
            DesignationHistory.objects.create(
                employee=user,
                designation=user.designation,
                effective_from=user.joining_date or date.today(),
                reason=DesignationHistory.Reason.INITIAL,
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(f"Done. {created} row(s) created."))
