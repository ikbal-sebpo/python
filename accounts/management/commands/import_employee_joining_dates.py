"""Backfill CustomUser.joining_date from HR's employee master spreadsheet.

Part of the Oracle ERP -> Orbis transition. `joining_date` was never
populated for most of the company: 1,430 of 1,511 accounts carry NULL,
which breaks leave accrual (AL earns per worked day), the pro-rating of
CL/SL for mid-year joiners, and the Org dashboard's tenure counts.

HR's master export ("Employee Information for HRIS") is one row per
employee with Employee ID, Name, Designation, Joining Date, Department and
Email Address. Only the joining date is imported -- the sheet's other
columns are read solely to report disagreements, never to write. Its email
column in particular is *worse* than what Orbis holds (sebpo365.com
addresses and at least one double-@ typo), so it must not be trusted as a
source.

By default an account that already has a `joining_date` is left alone and
any disagreement is logged, on the principle that a curated value should
not be silently replaced by a bulk import. Pass --overwrite to make the
file win instead.

Matching is by `employee_id` only. Inactive accounts are included -- a
leaver still has a real joining date, and the Org dashboard counts on it.

Dry-run by default -- pass --apply to write.

Usage::

    manage.py import_employee_joining_dates \\
        --file "/home/smrashel/Downloads/Employee Information for HRIS_Email Address added.xlsx" \\
        --exceptions-csv /tmp/joining_dates_exceptions.csv

    manage.py import_employee_joining_dates --file ... \\
        --exceptions-csv /tmp/joining_dates_exceptions.csv --apply
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import date, datetime

import openpyxl

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

User = get_user_model()

# Column headers as HR ships them. The Employee ID header carries an
# embedded newline ("Employee \nID"), so headers are matched on their
# whitespace-collapsed lowercase form.
COL_EMPLOYEE_ID = "employee id"
COL_NAME = "name"
COL_JOINING_DATE = "joining date"
COL_DEPARTMENT = "department"
COL_EMAIL = "email address"

REQUIRED_COLUMNS = (COL_EMPLOYEE_ID, COL_JOINING_DATE)

# A joining date outside this window is a data-entry error, not a fact.
EARLIEST_PLAUSIBLE = date(2000, 1, 1)


def _norm_header(value) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _coerce_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


class Command(BaseCommand):
    help = (
        "Backfill CustomUser.joining_date from HR's employee master "
        "spreadsheet. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the employee master .xlsx")
        parser.add_argument("--exceptions-csv", required=True, help="Path to write the exceptions/review CSV")
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Let the file replace a joining_date that is already set "
                 "(default: keep the stored value and log the disagreement).",
        )
        parser.add_argument("--apply", action="store_true", help="Actually write to the database (default: dry-run)")

    def handle(self, *args, **options):
        path = options["file"]
        exceptions: list[dict] = []

        if not os.path.exists(path):
            raise CommandError(f"File not found: {path}")

        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        try:
            header = [_norm_header(c) for c in next(rows)]
        except StopIteration:
            raise CommandError("File is empty.")

        idx = {name: header.index(name) for name in header if name}
        missing = [c for c in REQUIRED_COLUMNS if c not in idx]
        if missing:
            raise CommandError(
                f"Missing required column(s): {', '.join(missing)}. Found: {header}"
            )

        def cell(row, name):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        # --- Read the file ----------------------------------------------------
        file_rows: dict[str, dict] = {}
        for line_no, row in enumerate(rows, start=2):
            if row is None or all(c is None for c in row):
                continue
            raw_id = cell(row, COL_EMPLOYEE_ID)
            name = str(cell(row, COL_NAME) or "").strip()
            if raw_id is None or not str(raw_id).strip():
                exceptions.append({
                    "employee_id": "", "name": name,
                    "issue": f"Row {line_no}: blank Employee ID -- skipped.",
                })
                continue
            emp_id = str(int(raw_id)).strip() if isinstance(raw_id, float) and raw_id.is_integer() \
                else str(raw_id).strip()

            joined = _coerce_date(cell(row, COL_JOINING_DATE))
            if joined is None:
                exceptions.append({
                    "employee_id": emp_id, "name": name,
                    "issue": f"Row {line_no}: blank or unparseable Joining Date "
                             f"({cell(row, COL_JOINING_DATE)!r}) -- skipped.",
                })
                continue
            if not (EARLIEST_PLAUSIBLE <= joined <= date.today()):
                exceptions.append({
                    "employee_id": emp_id, "name": name,
                    "issue": f"Row {line_no}: Joining Date {joined} is outside "
                             f"{EARLIEST_PLAUSIBLE}..today -- skipped.",
                })
                continue

            if emp_id in file_rows:
                exceptions.append({
                    "employee_id": emp_id, "name": name,
                    "issue": f"Row {line_no}: duplicate Employee ID (first seen on row "
                             f"{file_rows[emp_id]['line_no']}) -- skipped.",
                })
                continue
            file_rows[emp_id] = {
                "line_no": line_no, "name": name, "joining_date": joined,
                "department": str(cell(row, COL_DEPARTMENT) or "").strip(),
                "email": str(cell(row, COL_EMAIL) or "").strip(),
            }

        self.stdout.write(f"Read {len(file_rows)} usable row(s) from {os.path.basename(path)}.")

        # --- Match against the database ---------------------------------------
        users = {u.employee_id: u for u in User.objects.filter(employee_id__in=file_rows)}
        unmatched = sorted(set(file_rows) - set(users))
        for emp_id in unmatched:
            exceptions.append({
                "employee_id": emp_id, "name": file_rows[emp_id]["name"],
                "issue": "No CustomUser with this employee_id.",
            })

        to_write: list[tuple] = []
        agreed = 0
        disagreed: list[tuple] = []
        for emp_id, rec in file_rows.items():
            user = users.get(emp_id)
            if user is None:
                continue

            # Report-only cross-checks. These columns are never written --
            # the file's email data is demonstrably worse than the database's.
            if rec["email"] and (user.email or "").strip().lower() != rec["email"].lower():
                exceptions.append({
                    "employee_id": emp_id, "name": rec["name"],
                    "issue": f"Email differs -- Orbis has {user.email!r}, file has "
                             f"{rec['email']!r}. Not changed.",
                })
            if rec["department"] and str(user.department or "").strip().lower() != rec["department"].lower():
                exceptions.append({
                    "employee_id": emp_id, "name": rec["name"],
                    "issue": f"Department differs -- Orbis has {str(user.department)!r}, "
                             f"file has {rec['department']!r}. Not changed.",
                })

            if user.joining_date is None:
                to_write.append((user, rec["joining_date"]))
            elif user.joining_date == rec["joining_date"]:
                agreed += 1
            else:
                disagreed.append((user, rec["joining_date"]))
                exceptions.append({
                    "employee_id": emp_id, "name": rec["name"],
                    "issue": (
                        f"joining_date differs -- Orbis has {user.joining_date}, file has "
                        f"{rec['joining_date']}. "
                        + ("Overwritten from file." if options["overwrite"]
                           else "Kept the stored value; re-run with --overwrite to take the file's.")
                    ),
                })
        if options["overwrite"]:
            to_write.extend(disagreed)

        self.stdout.write(
            f"\n{len(users)} matched, {len(unmatched)} unmatched.\n"
            f"  {len(to_write)} joining_date(s) to write\n"
            f"  {agreed} already correct\n"
            f"  {len(disagreed)} disagree "
            f"({'overwritten' if options['overwrite'] else 'kept as stored'})"
        )
        if disagreed:
            for user, file_date in disagreed:
                self.stdout.write(
                    f"    {user.employee_id} {user.get_full_name()}: "
                    f"Orbis {user.joining_date} vs file {file_date}"
                )
        by_year = Counter(d.year for _u, d in to_write)
        if by_year:
            self.stdout.write("  by joining year: " + ", ".join(
                f"{y}={n}" for y, n in sorted(by_year.items())
            ))

        with open(options["exceptions_csv"], "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["employee_id", "name", "issue"])
            writer.writeheader()
            writer.writerows(exceptions)
        self.stdout.write(f"{len(exceptions)} exception(s) written to {options['exceptions_csv']}")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only -- no database changes made. Re-run with --apply to write."
            ))
            return

        with transaction.atomic():
            now = timezone.now()
            for user, joined in to_write:
                user.joining_date = joined
                user.directory_updated_at = now  # bulk_update skips save()
            User.objects.bulk_update(
                [u for u, _ in to_write], ["joining_date", "directory_updated_at"], batch_size=500,
            )

        self.stdout.write(self.style.SUCCESS(f"Wrote {len(to_write)} joining_date(s)."))
