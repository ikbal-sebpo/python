"""One-time correction pass for `CPL ITCS Reconciliation (1).xlsx` — IT &
Cybersecurity management's manager-confirmed corrections on top of the
historical CplGrant bulk import (`leave/upload_cpl.py`), for 10 employees /
18 rows where the roster Shift and/or the recorded CplGrant consumption
didn't match what was actually decided.

Unlike the general-purpose `import_cpl_grant_workbook`, this file's
corrections aren't a uniform "one row = one grant+consumption" shape — three
rows redirect consumption onto a *different* date's grant ("CPL adjusted
with <date>" in the manager's notes) and three rows revert an existing CPL
consumption back to plain CL. That per-row classification was worked out by
hand against the file and confirmed with HR/IT (see
`leave/services.py::apply_cpl_reconciliation_row`'s docstring for the three
action types) — it's hardcoded below rather than re-derived from the sheet's
free-text "Manager Decision" column, since this command only ever runs
twice (dev, then production) and a regex-based re-derivation would be more
fragile than a reviewable, explicit table.

The `path` argument is used only as a light cross-check (every row's
employee ID + consumed date must actually appear in the file) so a
transcription mistake in the table below is caught rather than silently
applied.

Usage:
    python manage.py reconcile_cpl_itcs_file "/path/to/CPL ITCS Reconciliation (1).xlsx" --dry-run
    python manage.py reconcile_cpl_itcs_file "/path/to/CPL ITCS Reconciliation (1).xlsx"
"""

from __future__ import annotations

from datetime import date, datetime

import openpyxl
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leave.services import apply_cpl_reconciliation_row

User = get_user_model()


# (source row in the workbook, employee_id, action, grant_work_date,
#  consumed_date, worked_date, work_shift_type_name, source_grant_work_date)
#
# source_grant_work_date is only set for CPL_REDIRECT rows: the row's own
# nominal "worked date" grant, which the original import wrongly credited
# with this consumption and which must be reverted back to OPEN before the
# *actual* target grant (grant_work_date, from the "adjusted with <date>"
# manager note) is consumed instead.
ROWS = [
    (2, "3841", "CPL_LINK", date(2026, 4, 14), date(2026, 6, 1), None, None, None),
    (3, "3842", "CL_REVERT", date(2026, 2, 4), date(2026, 5, 24), None, None, None),
    (4, "4850", "CL_REVERT", date(2026, 2, 21), date(2026, 7, 16), None, None, None),
    (5, "4874", "CPL_LINK", date(2026, 4, 14), date(2026, 6, 27), None, None, None),
    (6, "4927", "CPL_LINK", date(2026, 2, 4), date(2026, 9, 1), None, None, None),
    (7, "4977", "CPL_LINK", date(2026, 2, 4), date(2026, 6, 8), date(2026, 2, 4), "EVE", None),
    (8, "4977", "CPL_LINK", date(2026, 2, 11), date(2026, 8, 13), None, None, None),
    (9, "4977", "CPL_LINK", date(2026, 2, 12), date(2026, 8, 14), None, None, None),
    (10, "4977", "CPL_REDIRECT", date(2026, 5, 1), date(2026, 8, 6), None, None, date(2026, 3, 26)),
    (11, "4977", "CPL_LINK", date(2026, 4, 14), date(2026, 8, 7), date(2026, 4, 14), "EVE", None),
    (12, "5087", "CPL_LINK", date(2026, 5, 1), date(2026, 5, 5), date(2026, 5, 1), "EVE", None),
    (13, "5264", "CL_REVERT", date(2026, 2, 4), date(2026, 3, 4), None, None, None),
    (14, "5264", "CPL_REDIRECT", date(2026, 6, 26), date(2026, 8, 7), None, None, date(2026, 3, 26)),
    (15, "5292", "CPL_REDIRECT", date(2026, 4, 14), date(2026, 6, 3), None, None, date(2026, 2, 4)),
    (16, "5654", "CPL_LINK", date(2026, 2, 4), date(2026, 3, 12), date(2026, 2, 4), "EVE", None),
    (17, "5654", "CPL_LINK", date(2026, 3, 26), date(2026, 4, 13), date(2026, 3, 26), "EVE", None),
    (18, "5654", "CPL_LINK", date(2026, 5, 1), date(2026, 6, 11), date(2026, 5, 1), "EVE", None),
    (19, "5654", "CPL_LINK", date(2026, 6, 26), date(2026, 7, 13), date(2026, 6, 26), "NIG", None),
]


class _DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = "Apply the manager-confirmed CPL ITCS Reconciliation corrections (see ROWS in this file)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to 'CPL ITCS Reconciliation (1).xlsx' (cross-check only).")
        parser.add_argument("--dry-run", action="store_true", help="Compute and print without writing.")
        parser.add_argument(
            "--created-by", type=str, default=None,
            help="Employee ID to attribute the correction to. Optional.",
        )

    def _cross_check(self, path: str):
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.worksheets[0]
        file_pairs = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            emp_id = str(int(row[0])) if isinstance(row[0], float) else str(row[0])
            consumed = row[4]
            if isinstance(consumed, datetime):
                consumed = consumed.date()
            file_pairs.add((emp_id, consumed))

        problems = [
            (src, emp_id, consumed_date)
            for src, emp_id, _, _, consumed_date, _, _, _ in ROWS
            if (emp_id, consumed_date) not in file_pairs
        ]
        if problems:
            raise CommandError(
                "Cross-check against the workbook failed for row(s): "
                + "; ".join(f"source row {src} (employee {e}, consumed {c})" for src, e, c in problems)
            )

    def handle(self, *args, **opts):
        path = opts["path"]
        dry = bool(opts.get("dry_run"))
        created_by = None
        if opts.get("created_by"):
            created_by = User.objects.filter(employee_id=str(opts["created_by"])).first()
            if created_by is None:
                raise CommandError(f"No employee with ID {opts['created_by']}")

        self._cross_check(path)
        self.stdout.write(f"Cross-checked {len(ROWS)} row(s) against {path}")
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        employees = {u.employee_id: u for u in User.objects.filter(
            employee_id__in={r[1] for r in ROWS},
        )}

        applied = 0
        errors: list[tuple[int, str, str]] = []

        for (src, employee_id, action, grant_work_date, consumed_date,
             worked_date, work_shift_type_name, source_grant_work_date) in ROWS:
            employee = employees.get(employee_id)
            if employee is None:
                errors.append((src, employee_id, f"No active employee with ID {employee_id}"))
                continue

            kwargs = dict(
                employee=employee, action=action,
                grant_work_date=grant_work_date, consumed_date=consumed_date,
                worked_date=worked_date, work_shift_type_name=work_shift_type_name,
                source_grant_work_date=source_grant_work_date,
                created_by=created_by,
            )

            if dry:
                try:
                    with transaction.atomic():
                        result = apply_cpl_reconciliation_row(**kwargs)
                        raise _DryRunRollback()
                except _DryRunRollback:
                    pass
            else:
                result = apply_cpl_reconciliation_row(**kwargs)

            if result.get("ok"):
                applied += 1
                changes = result.get("changes") or ["(already correct — no changes)"]
                self.stdout.write(f"row {src} ({employee_id}, {action}):")
                for change in changes:
                    self.stdout.write(f"    {change}")
            else:
                errors.append((src, employee_id, result.get("reason", "unknown error")))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{applied} row(s) {'would be ' if dry else ''}applied; {len(errors)} error(s)."
        ))
        if errors:
            self.stdout.write("")
            self.stdout.write("Errors:")
            for src, employee_id, reason in errors:
                self.stdout.write(f"  row {src} (employee {employee_id}): {reason}")
