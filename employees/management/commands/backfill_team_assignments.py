"""One-off: seed a current (open-ended) TeamAssignment row for every
employee who has no assignment history yet.

Run ONCE, right after deploying the TeamAssignment model + its sync signal
(employees/signals.py). From that point on every team / resource-status
change is captured automatically — this command only covers employees whose
*current* assignment predates the feature.

Genuine historical moves (someone who joined in 2019 and has changed team
four times since) are NOT reconstructed: that data doesn't exist anywhere in
Orbis, since `CustomUser.team` and `JobInfo.resource_status` have always been
single mutable values. Each backfilled row is dated from the employee's
joining_date and marked INITIAL, which is honest about being a starting
point rather than a real transition.

**Deliberately does not propose salary revisions.** `sync_team_assignment`
would normally ask payroll whether the new assignment implies a package
change; doing that across ~1,500 existing employees at once would dump a
pending proposal into Finance's queue for everybody currently paid
off-package. Backfilling history is a records exercise, not a pay review —
and the run's SALARY_PACKAGE_MISMATCH check already surfaces off-package
salaries without changing anything.

Idempotent: only touches employees with no TeamAssignment row at all.

Usage:
    python manage.py backfill_team_assignments
    python manage.py backfill_team_assignments --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from employees.models import JobInfo, TeamAssignment


class Command(BaseCommand):
    help = "Seed an initial TeamAssignment row for employees who have none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be created without writing anything.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        today = timezone.localdate()

        already = set(
            TeamAssignment.objects.values_list("employee_id", flat=True).distinct()
        )
        status_by_employee = dict(
            JobInfo.objects.values_list("employee_id", "resource_status")
        )
        employees = list(
            CustomUser.objects.exclude(pk__in=already)
            .only("pk", "department_id", "team_id", "joining_date")
            .order_by("employee_id")
        )

        if not employees:
            self.stdout.write(self.style.SUCCESS("Nothing to do — every employee already has one."))
            return

        rows = [
            TeamAssignment(
                employee_id=employee.pk,
                department_id=employee.department_id,
                team_id=employee.team_id,
                resource_status=status_by_employee.get(employee.pk) or "",
                effective_from=employee.joining_date or today,
                reason=TeamAssignment.Reason.INITIAL,
            )
            for employee in employees
        ]

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] would create {len(rows)} TeamAssignment row(s); nothing written."
            ))
            for row in rows[:10]:
                self.stdout.write(f"  · {row.employee_id} team={row.team_id} "
                                  f"status={row.resource_status or '—'} from={row.effective_from}")
            if len(rows) > 10:
                self.stdout.write(f"  … and {len(rows) - 10} more")
            return

        # bulk_create bypasses the post_save signal, which is exactly what we
        # want here — see this module's docstring on not proposing salaries.
        with transaction.atomic():
            TeamAssignment.objects.bulk_create(rows, batch_size=500)
        self.stdout.write(self.style.SUCCESS(f"Created {len(rows)} TeamAssignment row(s)."))
