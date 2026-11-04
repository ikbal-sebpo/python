"""On-demand backfill: delete roster shifts dated before an employee's joining date.

Mirror image of `offboarding.management.commands.purge_shifts_past_lwd`. No
`Shift` may exist for a date before `CustomUser.joining_date` — enforced
going forward at every write path (`roster.services.prefill_shifts_from_pattern`,
`roster.upload.apply_upload`, `roster.api.shift_writes`,
`roster.views.CellUpdateView`, added 2026-09-17). This command cleans up rows
written before those guards existed.

Scoping is deliberately explicit and opt-in — unlike `purge_shifts_past_lwd`,
which sweeps every employee unconditionally, this command refuses to touch
anyone unless told to. `manage.py audit_shifts_before_joining_date` lists the
current candidates; some of them are large multi-month blocks that may
actually indicate a wrong `joining_date` rather than a wrongly-written
shift — those need human review of the joining_date itself before deleting
roster history that might be legitimate.

Always preview first — this deletes data::

    python manage.py purge_shifts_before_joining_date --employee-id 54705 --dry-run
    python manage.py purge_shifts_before_joining_date --employee-id 54705 --employee-id 54701
    python manage.py purge_shifts_before_joining_date --all --dry-run   # company-wide, use with care
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from roster.models import Shift
from roster.services import purge_shifts_before_joining_date

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Delete roster Shift rows dated before each employee's "
        "CustomUser.joining_date. Scoped to --employee-id by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--employee-id", action="append", default=[], dest="employee_ids",
            help="CustomUser.employee_id to purge. Repeatable.",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Sweep every employee with a joining_date, not just --employee-id.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would be purged without making changes.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        employee_ids = opts["employee_ids"]
        sweep_all = opts["all"]

        if not employee_ids and not sweep_all:
            raise CommandError(
                "Refusing to run with no scope — pass one or more --employee-id, "
                "or --all to sweep every employee with a joining_date."
            )

        candidates = User.objects.filter(joining_date__isnull=False)
        if employee_ids:
            candidates = candidates.filter(employee_id__in=employee_ids)
            found_ids = set(candidates.values_list("employee_id", flat=True))
            missing = set(employee_ids) - found_ids
            if missing:
                raise CommandError(
                    f"No user with employee_id in {sorted(missing)} (or they have no joining_date)."
                )
        candidates = candidates.order_by("joining_date", "email")

        total = 0
        touched = 0
        for user in candidates:
            if dry_run:
                count = Shift.objects.filter(
                    employee_id=user.pk, date__lt=user.joining_date,
                ).count()
            else:
                count = purge_shifts_before_joining_date(user, actor=None)
            if not count:
                continue
            touched += 1
            total += count
            self.stdout.write(
                f"  · {user.display_name} <{user.email}> "
                f"(joined {user.joining_date}) — {count} shift(s)"
            )

        if not touched:
            self.stdout.write("No shifts before any joining date in scope.")
            return
        verb = "would purge" if dry_run else "purged"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {total} shift(s) across {touched} employee(s)."
        ))
