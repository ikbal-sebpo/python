"""One-time import of a staged CplGrant workbook — the (Department,
Employee ID, Working Date, Status, Consumed Date) format this session's
HR-provided historical files (IT & Cybersecurity's own tracker, Data
Solutions' entitlement file) were decoded and staged into.

The Department column is informational only (for human review) — the
employee's own current department is always used. See
leave/services.py::import_cpl_grant_row for the per-row import logic and
idempotency (safe to re-run; already-imported rows are skipped).

Usage:
    python manage.py import_cpl_grant_workbook /path/to/CplGrant.xlsx
    python manage.py import_cpl_grant_workbook /path/to/CplGrant.xlsx --dry-run
"""

from __future__ import annotations

from datetime import datetime

import openpyxl
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leave.services import import_cpl_grant_row

User = get_user_model()


class _DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = "Import a staged CplGrant.xlsx (Department, Employee ID, Working Date, Status, Consumed Date)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to the .xlsx file.")
        parser.add_argument("--dry-run", action="store_true", help="Parse and validate without writing.")
        parser.add_argument(
            "--created-by", type=str, default=None,
            help="Employee ID to attribute the import to (added_by/created_by). Optional.",
        )

    def handle(self, *args, **opts):
        path = opts["path"]
        dry = bool(opts.get("dry_run"))
        created_by = None
        if opts.get("created_by"):
            created_by = User.objects.filter(employee_id=str(opts["created_by"])).first()
            if created_by is None:
                raise CommandError(f"No employee with ID {opts['created_by']}")

        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
        rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r and r[1] is not None]

        self.stdout.write(f"Read {len(rows)} row(s) from {path}")
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        imported = 0
        skipped = 0
        errors = []

        for dept, employee_id, work_date, status, consumed_date in rows:
            if isinstance(work_date, datetime):
                work_date = work_date.date()
            if isinstance(consumed_date, datetime):
                consumed_date = consumed_date.date()
            status = (status or "").strip().upper()

            if dry:
                # Dry-run still resolves employee/holiday to validate the
                # row, but never writes — call with a rolled-back savepoint.
                try:
                    with transaction.atomic():
                        result = import_cpl_grant_row(
                            employee_id, work_date, status, consumed_date, created_by=created_by,
                        )
                        raise _DryRunRollback()
                except _DryRunRollback:
                    pass
            else:
                result = import_cpl_grant_row(
                    employee_id, work_date, status, consumed_date, created_by=created_by,
                )

            if result.get("skipped"):
                skipped += 1
            elif result.get("ok"):
                imported += 1
            else:
                errors.append((employee_id, work_date, result.get("reason")))

        self.stdout.write(self.style.SUCCESS(
            f"{imported} row(s) {'would be ' if dry else ''}imported; "
            f"{skipped} already-imported row(s) skipped; {len(errors)} error(s)."
        ))
        if errors:
            self.stdout.write("")
            self.stdout.write("Errors:")
            for employee_id, work_date, reason in errors:
                self.stdout.write(f"  employee {employee_id} / {work_date}: {reason}")


class _DryRunRollback(Exception):
    pass
