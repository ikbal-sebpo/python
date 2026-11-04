"""Recompute sandwich billing for approved leave applications.

Re-runs the sandwich algorithm over each affected employee's approved leaves
and reconciles the balance ledger to match. Use this after a change to the
billing rules in `leave/services.py`, or to repair balances that were written
by an older (buggy) version of the algorithm.

Repair is auditable: corrections go through `recompute_sandwich`, which writes
USED / REVERSED `LeaveBalanceTransaction` rows rather than editing totals in
place. Safe to re-run — it is idempotent (a second pass finds no drift).

Usage:
    python manage.py recompute_leave_billing --dry-run        # report only
    python manage.py recompute_leave_billing                  # fix everyone
    python manage.py recompute_leave_billing --employee 5086  # one employee
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from accounts.models import CustomUser
from leave.models import LeaveApplication
from leave.services import compute_application_billable_days, recompute_sandwich


class Command(BaseCommand):
    help = "Recompute sandwich billing for approved leaves and reconcile balances."

    def add_arguments(self, parser):
        parser.add_argument(
            "--employee",
            type=str,
            default=None,
            help="Limit to a single employee by employee_id. Defaults to all.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report applications whose billing would change without writing.",
        )

    def handle(self, *args, **opts):
        emp_id = opts.get("employee")
        dry = bool(opts.get("dry_run"))

        apps = (
            LeaveApplication.objects
            .filter(status=LeaveApplication.Status.APPROVED)
            .select_related("employee", "leave_type__leave_policy")
        )
        if emp_id:
            apps = apps.filter(employee__employee_id=emp_id)
            if not CustomUser.objects.filter(employee_id=emp_id).exists():
                self.stderr.write(self.style.ERROR(f"No employee with id {emp_id!r}."))
                return

        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        # 1) Find which applications drift from their stored effective_days.
        drifted = []
        for app in apps:
            _, _, total = compute_application_billable_days(app)
            if total != app.effective_days:
                drifted.append((app, app.effective_days, total))

        if not drifted:
            self.stdout.write(self.style.SUCCESS("No drift found. Nothing to fix."))
            return

        # Group by employee for reporting and the per-employee recompute pass.
        by_employee: dict[CustomUser, list] = {}
        for app, old, new in drifted:
            by_employee.setdefault(app.employee, []).append((app, old, new))

        self.stdout.write(
            f"{len(drifted)} application(s) across {len(by_employee)} employee(s) "
            f"need correction:"
        )
        for emp, rows in by_employee.items():
            net = sum((old - new for _, old, new in rows), Decimal("0"))
            self.stdout.write(
                f"  {emp.employee_id} {emp.get_full_name()} — "
                f"{len(rows)} app(s), net {net:+} day(s)"
            )
            for app, old, new in rows:
                self.stdout.write(
                    f"      #{app.pk} {app.leave_type.name} "
                    f"{app.start_date}→{app.end_date}: {old} → {new}"
                )

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
            return

        # 2) Reconcile. recompute_sandwich processes every approved leave within
        # ±WINDOW_DAYS of the anchor date, so calling it once per drifted
        # application's start_date covers all interacting leaves; the second and
        # later calls for the same cluster are no-ops (idempotent).
        for emp, rows in by_employee.items():
            for app, _, _ in rows:
                recompute_sandwich(emp, app.start_date)

        self.stdout.write(self.style.SUCCESS(
            f"Done. Reconciled {len(drifted)} application(s) "
            f"for {len(by_employee)} employee(s)."
        ))
