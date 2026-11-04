"""Backfill Digital Advertising's Shift history from two external exports.

The department tracks its own schedule outside Orbis in two spreadsheets:

- "AdOps Work Schedule (Roster)" — one row per employee, a (Shift, Time)
  column pair per date. Gives exact shift + start time (e.g. EVE @ 14:00)
  but only sometimes records leave days.
- "Yearly ... Attendance Details" — one sheet per month, one Shift-status
  column per date (no time). No shift-time detail, but is the reliable
  source for leave days (AL/CL/SL/ML/FL/CPL/HDL/LWP).

Merge rule: the attendance file's leave code wins when present (it's the
leave source of truth); otherwise the roster file's leave code; otherwise
the roster file's (shift, time) is resolved to a specific department-scoped
ShiftAttribute; otherwise the attendance file's bare shift/OFF code is used
as a low-confidence fallback (logged as an exception — no time to pick the
right attribute variant).

Code mapping quirks confirmed with HR:
- "ML" in both files means Maternity Leave -> ShiftType MTL.
- "FL" in the attendance file means Government Holiday -> ShiftType GH
  (NOT the HOLIDAY-category "FL" ShiftType, which is unrelated).
- EVE shifts starting at 13:00/14:00/15:00 each match two seeded
  attributes that differ only by end time (e.g. EVE 2-10 vs EVE 2-11) --
  HR confirmed always prefer the longer/later-ending ("-11") variant.

Historical employees are looked up regardless of `is_active` (someone who
has since resigned still has a real shift history). Existing Shift rows
tied to an approved LeaveApplication (`leave_application_id` set) are never
overwritten -- that reflects the leave ledger and must go through the leave
app's own services, not a spreadsheet reconciliation. Every other existing
row in range is overwritten (HR confirmed these two files are authoritative
for 2026-01-01..2026-08-15), but an overwrite keeps the row's existing
job_location/work_location (neither source file has location data).

Dry-run by default -- pass --apply to write. Always writes an exceptions
CSV (unmatched employees, ledger-locked skips, low-confidence time
resolutions) so HR can review edge cases.

Usage::

    manage.py import_adops_shift_history \\
        --roster "/path/AdOps Work Schedule (Roster)_till 15th August.xlsx" \\
        --attendance "/path/Yearly COMBINED Attendance Details.xlsx" \\
        --as hr@sebpo.com --exceptions-csv /tmp/adops_exceptions.csv

    manage.py import_adops_shift_history --roster ... --attendance ... \\
        --as hr@sebpo.com --exceptions-csv /tmp/adops_exceptions.csv --apply
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date, datetime, timedelta

import openpyxl

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from accounts.models import Department
from roster.models import Shift, ShiftChangeLog, ShiftType
from roster.upload import _build_shift_cache, _coerce_employee_id, _resolve_shift_name

User = get_user_model()

START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 8, 15)

# Codes that appear as bare "Shift" cell values in one or both files, mapped
# to the exact ShiftType/ShiftAttribute *name* string the normal uploader
# would accept. Anything not in this dict is a working-shift code (MOR/EVE/
# NIG) or OFF, handled by _resolve_work_attribute below instead.
LEAVE_CODE_MAP = {
    "AL": "AL",
    "CL": "CL",
    "SL": "SL",
    "LWP": "LWP",
    "ML": "MTL",     # HR: "ML" means Maternity Leave.
    "CPL": "CPL",
    "HDL": "HDL",
    "FL": "GH",       # HR: "FL" here means Government Holiday, not the
                       # HOLIDAY-category "FL" ShiftType.
}

WORK_CODES = {"MOR", "EVE", "NIG"}

# Known start times (minutes since midnight) -> attribute name, per shift
# code. Ambiguous EVE start times resolve to the "-11" (later end hour)
# variant per HR's confirmed default.
_ATTR_START_MINUTES = {
    "MOR": {7 * 60: "MOR 7-4", 8 * 60: "MOR 8-5", 10 * 60: "MOR 10-7"},
    "EVE": {
        13 * 60: "EVE 1-11",
        14 * 60: "EVE 2-11",
        15 * 60: "EVE 3-11",
        16 * 60: "EVE 4-11",
    },
    "NIG": {
        19 * 60: "NIG 7-4",
        20 * 60: "NIG 8-7",
        21 * 60: "NIG 9-7",
        22 * 60: "NIG 10-7",
        23 * 60: "NIG 11-7",
    },
}


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _circular_distance(a: int, b: int) -> int:
    diff = abs(a - b) % (24 * 60)
    return min(diff, 24 * 60 - diff)


def _resolve_work_attribute(code: str, time_value) -> tuple[str, bool]:
    """Return (attribute_name, exact) for a MOR/EVE/NIG (code, time) pair.

    ``exact`` is False when the time didn't match a known start exactly and
    we fell back to the nearest known variant -- callers should log that as
    an exception for manual review.
    """
    starts = _ATTR_START_MINUTES[code]
    minutes = None
    if isinstance(time_value, datetime):
        minutes = time_value.hour * 60 + time_value.minute
    elif hasattr(time_value, "hour"):  # datetime.time
        minutes = time_value.hour * 60 + time_value.minute
    if minutes is not None and minutes in starts:
        return starts[minutes], True
    if minutes is None:
        # No usable time at all -- fall back to the most common variant.
        fallback_minutes = min(starts)
        return starts[fallback_minutes], False
    nearest = min(starts, key=lambda m: _circular_distance(m, minutes))
    return starts[nearest], False


# ---------------------------------------------------------------------------
# Roster (Shift + Time) file
# ---------------------------------------------------------------------------

def _load_roster_file(path: str) -> dict[tuple[str, date], tuple[str, object]]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header1 = next(rows)
    next(rows)  # sub-header row (Shift/Time labels) -- not needed positionally.

    # Row 1 repeats each date across its Shift *and* Time sub-column (see
    # module docstring), so columns must be walked in (shift, time) pairs
    # starting at column C -- not by scanning every cell for a date.
    date_cols: list[tuple[int, date]] = []
    for idx in range(2, len(header1), 2):
        cell = header1[idx]
        if isinstance(cell, datetime):
            d = cell.date()
        elif isinstance(cell, date):
            d = cell
        else:
            continue
        if START_DATE <= d <= END_DATE:
            date_cols.append((idx, d))

    out: dict[tuple[str, date], tuple[str, object]] = {}
    for row in rows:
        if not row or row[0] is None:
            continue
        try:
            eid = _coerce_employee_id(row[0])
        except ValueError:
            continue
        for idx, d in date_cols:
            code = row[idx] if idx < len(row) else None
            if code is None or not str(code).strip():
                continue
            time_value = row[idx + 1] if idx + 1 < len(row) else None
            out[(eid, d)] = (str(code).strip().upper(), time_value)
    return out


# ---------------------------------------------------------------------------
# Attendance (leave) file
# ---------------------------------------------------------------------------

MONTH_SHEETS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug"]


def _load_attendance_file(path: str) -> dict[tuple[str, date], str]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out: dict[tuple[str, date], str] = {}
    for sheet_name in MONTH_SHEETS:
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        rows = ws.iter_rows(min_row=1, max_row=3, values_only=True)
        header1 = next(rows)
        next(rows)  # weekday-name row -- unused.
        header3 = next(rows)

        day_cols: list[tuple[int, date]] = []
        for idx, label in enumerate(header3):
            if idx < 11 or label != "Shift":
                continue
            raw_date = header1[idx] if idx < len(header1) else None
            if isinstance(raw_date, datetime):
                d = raw_date.date()
            elif isinstance(raw_date, date):
                d = raw_date
            else:
                continue
            if START_DATE <= d <= END_DATE:
                day_cols.append((idx, d))
        if not day_cols:
            continue

        for row in ws.iter_rows(min_row=4, values_only=True):
            eid_cell = row[3] if len(row) > 3 else None
            if eid_cell is None:
                continue
            try:
                eid = _coerce_employee_id(eid_cell)
            except ValueError:
                continue
            for idx, d in day_cols:
                code = row[idx] if idx < len(row) else None
                if code is None or not isinstance(code, str) or not code.strip():
                    continue
                out[(eid, d)] = code.strip().upper()
    return out


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class MergeResult:
    def __init__(self):
        self.rows: list[dict] = []          # employee_id, date, shift_value, source
        self.exceptions: list[dict] = []
        self.source_counts: Counter = Counter()


def _merge(roster_map, attendance_map, employee_ids) -> MergeResult:
    result = MergeResult()
    for eid in employee_ids:
        for d in _daterange(START_DATE, END_DATE):
            key = (eid, d)
            att = attendance_map.get(key)
            ros = roster_map.get(key)
            ros_code, ros_time = ros if ros else (None, None)

            shift_value = None
            source = None

            if att in LEAVE_CODE_MAP:
                shift_value = LEAVE_CODE_MAP[att]
                source = "attendance-leave"
            elif ros_code in LEAVE_CODE_MAP:
                shift_value = LEAVE_CODE_MAP[ros_code]
                source = "roster-leave"
            elif ros_code in WORK_CODES:
                shift_value, exact = _resolve_work_attribute(ros_code, ros_time)
                source = "roster-shift"
                if not exact:
                    result.exceptions.append({
                        "employee_id": eid, "date": d.isoformat(),
                        "issue": f"Time '{ros_time}' for {ros_code} didn't match a known "
                                 f"start exactly -- used nearest ({shift_value}).",
                    })
            elif ros_code == "OFF":
                shift_value = "OFF"
                source = "roster-off"
            elif att in WORK_CODES:
                shift_value, _ = _resolve_work_attribute(att, None)
                source = "attendance-shift-fallback"
                result.exceptions.append({
                    "employee_id": eid, "date": d.isoformat(),
                    "issue": f"No roster-file entry; attendance file says {att} with no "
                             f"time -- guessed {shift_value} (most common variant).",
                })
            elif att == "OFF":
                shift_value = "OFF"
                source = "attendance-off"

            if shift_value is None:
                continue

            result.rows.append({
                "employee_id": eid, "date": d, "shift_value": shift_value,
                "source": source,
            })
            result.source_counts[source] += 1
    return result


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Backfill Digital Advertising Shift history (2026-01-01..2026-08-15) "
        "from the AdOps roster + attendance spreadsheets. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--roster", required=True, help="Path to the AdOps Work Schedule .xlsx")
        parser.add_argument("--attendance", required=True, help="Path to the Yearly Attendance Details .xlsx")
        parser.add_argument("--as", dest="actor_email", help="Email of the acting user (required with --apply)")
        parser.add_argument("--exceptions-csv", required=True, help="Path to write the exceptions/review CSV")
        parser.add_argument("--apply", action="store_true", help="Actually write to the database (default: dry-run)")

    def handle(self, *args, **options):
        roster_path = options["roster"]
        attendance_path = options["attendance"]
        apply_changes = options["apply"]
        exceptions_path = options["exceptions_csv"]

        if apply_changes and not options["actor_email"]:
            raise CommandError("--as <email> is required with --apply")

        self.stdout.write("Reading roster file...")
        roster_map = _load_roster_file(roster_path)
        self.stdout.write(f"  {len(roster_map)} (employee, date) cells")

        self.stdout.write("Reading attendance file...")
        attendance_map = _load_attendance_file(attendance_path)
        self.stdout.write(f"  {len(attendance_map)} (employee, date) cells")

        dept = Department.objects.filter(name__icontains="digital advertising").first()
        if dept is None:
            raise CommandError("Digital Advertising department not found")

        file_employee_ids = {eid for eid, _ in roster_map} | {eid for eid, _ in attendance_map}
        users_by_eid = {
            u.employee_id: u
            for u in User.objects.filter(employee_id__in=file_employee_ids)
        }
        unmatched_ids = file_employee_ids - set(users_by_eid)

        exceptions: list[dict] = []
        for eid in sorted(unmatched_ids):
            exceptions.append({
                "employee_id": eid, "date": "", "issue": "No CustomUser with this employee_id.",
            })

        self.stdout.write(f"Merging {len(users_by_eid)} matched employees "
                           f"({len(unmatched_ids)} unmatched)...")
        merged = _merge(roster_map, attendance_map, users_by_eid.keys())
        exceptions.extend(merged.exceptions)

        self.stdout.write("Source breakdown:")
        for source, count in merged.source_counts.most_common():
            self.stdout.write(f"  {source}: {count}")

        # Resolve each row's shift_value string to (ShiftType, ShiftAttribute)
        # via the exact same lookup the normal bulk uploader uses.
        shift_cache = _build_shift_cache()
        emp_pks = {u.pk for u in users_by_eid.values()}
        existing = {
            (row["employee_id"], row["date"]): row
            for row in Shift.objects.filter(
                employee_id__in=emp_pks, date__range=(START_DATE, END_DATE),
            ).values(
                "employee_id", "date", "shift_type_id", "shift_attribute_id",
                "job_location_id", "work_location_id", "note",
                "leave_application_id",
            )
        }

        to_write: list[dict] = []
        skipped_ledger_locked = 0
        for row in merged.rows:
            employee = users_by_eid[row["employee_id"]]
            try:
                shift_type, shift_attr = _resolve_shift_name(row["shift_value"], shift_cache)
            except ValueError as exc:
                exceptions.append({
                    "employee_id": row["employee_id"], "date": row["date"].isoformat(),
                    "issue": f"Computed value '{row['shift_value']}' didn't resolve: {exc}",
                })
                continue

            key = (employee.pk, row["date"])
            prev = existing.get(key)
            if prev and prev["leave_application_id"] is not None:
                skipped_ledger_locked += 1
                exceptions.append({
                    "employee_id": row["employee_id"], "date": row["date"].isoformat(),
                    "issue": "Existing Shift is tied to an approved LeaveApplication -- not overwritten.",
                })
                continue

            to_write.append({
                "employee": employee, "date": row["date"],
                "shift_type": shift_type, "shift_attribute": shift_attr,
                "job_location_id": prev["job_location_id"] if prev else None,
                "work_location_id": prev["work_location_id"] if prev else None,
            })

        self.stdout.write(
            f"\n{len(to_write)} Shift rows to write "
            f"({skipped_ledger_locked} skipped: ledger-locked leave), "
            f"{len(exceptions)} exceptions logged."
        )

        with open(exceptions_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["employee_id", "date", "issue"])
            writer.writeheader()
            writer.writerows(exceptions)
        self.stdout.write(f"Exceptions written to {exceptions_path}")

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only -- no database changes made. Re-run with --apply to write."
            ))
            return

        actor = User.objects.filter(email__iexact=options["actor_email"]).first()
        if actor is None:
            raise CommandError(f"No user with email {options['actor_email']!r}")

        now = timezone.now()
        objs = [
            Shift(
                employee=r["employee"], date=r["date"],
                shift_type=r["shift_type"], shift_attribute=r["shift_attribute"],
                job_location_id=r["job_location_id"], work_location_id=r["work_location_id"],
                added_by=actor, added_at=now, updated_by=actor, updated_at=now,
            )
            for r in to_write
        ]

        bulk_kwargs = {
            "update_conflicts": True,
            "update_fields": ["shift_type", "shift_attribute", "updated_by", "updated_at"],
        }
        if connection.features.supports_update_conflicts_with_target:
            bulk_kwargs["unique_fields"] = ["employee", "date"]

        with transaction.atomic():
            Shift.objects.bulk_create(objs, batch_size=1000, **bulk_kwargs)
            ShiftChangeLog.objects.bulk_create([
                ShiftChangeLog(
                    employee=r["employee"], date=r["date"],
                    action_type=(
                        ShiftChangeLog.ActionType.UPDATED
                        if (r["employee"].pk, r["date"]) in existing
                        else ShiftChangeLog.ActionType.CREATED
                    ),
                    new_shift_type=r["shift_type"], new_shift_attribute=r["shift_attribute"],
                    changed_by=actor, changed_at=now,
                )
                for r in to_write
            ], batch_size=1000)

        self.stdout.write(self.style.SUCCESS(f"Wrote {len(objs)} Shift rows."))
