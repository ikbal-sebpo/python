"""Overtime bulk upload — flat 5-column layout (PAYROLL_PROCESS.md §7):

    Working date | Employee ID | Worked In | Overtime type | OT hours (hh:mm:ss)

Worked In (added 2026-09-16) is optional — a team name, blank defaults to
the employee's own accounts.Team at apply time. It exists because
payroll.PayRate overtime rates can be team-scoped (e.g. Data Solutions'
MARS team is rated differently from its department default), so which team
the hours were actually worked for has to be recorded per entry rather than
assumed from the employee's profile.

Structurally mirrors roster/upload.py: ParsedRow/RowError/ApplyReport
dataclasses, a read-only parse_upload() dry-run, and apply_upload() writing
inside one transaction.atomic() via bulk_create(update_conflicts=True) so
a re-upload of the same sheet corrects earlier figures rather than
duplicating them. The caller (the view) is responsible for only calling
apply_upload() once report.has_errors is False — this module doesn't
self-gate on that, same posture as roster/upload.py.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

import openpyxl
from dateutil import parser as date_parser
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel
from openpyxl.worksheet.datavalidation import DataValidation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.utils import timezone

from accounts.models import Team, directory_stamp
from employees.models import JobInfo, TeamAssignment
from employees.signals import sync_team_assignment
from accounts.permissions import can_write_payroll_note, visible_employees
from core.exports import escape_spreadsheet, money_value
from core.money import EXCEL_NUMBER_FORMAT
from payroll.models import (
    Bank, LegacyPayrollMonth, OvertimeEntry, OvertimeType, PayRate, PayrollPeriod, PayrollRunEmployeeNote,
    SalaryStructure, TaxChallan, VariablePayEntry,
)
from payroll.periods import first_loan_deduction_month, period_for_date
from payroll.rates import resolve_team_salary_package
from payroll.services import propose_salary_for_assignment, supersede_salary_structure


User = get_user_model()


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    work_date: date
    overtime_type: OvertimeType
    seconds: int
    period: PayrollPeriod            # derived from work_date, not a fixed upload-time period
    worked_in: object = None         # accounts.Team or None — None means "default to employee.team"


@dataclass
class RowError:
    line_no: int
    col_letter: str | None
    raw: dict
    message: str


@dataclass
class ApplyReport:
    created: list[ParsedRow] = field(default_factory=list)
    updated: list[ParsedRow] = field(default_factory=list)
    skipped_scope: list[RowError] = field(default_factory=list)
    # Rejected by payroll.services.OvertimeWindow (2026-09-15): dated in the
    # future, over a year back, or into a period that has already been paid.
    # Per-row skips like skipped_scope rather than whole-file aborts — one
    # stale row shouldn't reject a good sheet.
    skipped_window: list[RowError] = field(default_factory=list)
    # Rows refused because the locked-period entry they'd change is already
    # settled (approved or voided by HR) — see apply_upload.
    skipped_locked: list[RowError] = field(default_factory=list)
    # Written and now awaiting the review chain — every created/updated
    # row since 2026-10-17 (it used to be only the locked-period ones, Q55).
    # Kept as its own list so the report keeps one place to count "needs a
    # decision before it is paid".
    pending: list[ParsedRow] = field(default_factory=list)
    # Re-uploaded with the same figures as an entry already reviewed or
    # approved: left untouched so the sign-off stands. Not a write.
    unchanged: list[ParsedRow] = field(default_factory=list)
    # Re-uploaded with DIFFERENT figures over a reviewed/approved entry: the
    # sign-off covered the old figures, so the row went back to Pending
    # review (also in `updated`). Called out so the uploader knows a
    # decision was undone.
    resubmitted: list[ParsedRow] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)

    @property
    def total_skipped(self) -> int:
        return len(self.skipped_scope) + len(self.skipped_window) + len(self.skipped_locked)


# ---------------------------------------------------------------------------
# Lookups + cell coercion
# ---------------------------------------------------------------------------

def _build_ot_type_cache() -> dict[str, OvertimeType]:
    """One query. Keyed by lowercased code/name/alias — the same idiom as
    roster/upload.py::_build_shift_cache."""
    cache: dict[str, OvertimeType] = {}
    for ot in OvertimeType.objects.filter(is_active=True):
        cache[ot.code.lower()] = ot
        cache.setdefault(ot.name.lower(), ot)
        for alias in ot.aliases or []:
            cache.setdefault(str(alias).lower(), ot)
    return cache


def _build_team_cache() -> dict[str, Team]:
    """One query. Keyed by lowercased name — Team.name is globally unique
    (accounts._Named), so a name alone is unambiguous across departments."""
    return {t.name.lower(): t for t in Team.objects.filter(is_active=True)}


def _coerce_employee_id(value) -> str:
    """xlsx may hand us int/float for numeric employee_id cells. CSV gives str."""
    if value is None:
        raise ValueError("Empty employee_id.")
    if isinstance(value, bool):
        raise ValueError(f"Bad employee_id: {value!r}.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Employee_id '{value}' looks like a decimal — expected an integer or text.")
        return str(int(value))
    s = str(value).strip()
    if not s:
        raise ValueError("Empty employee_id.")
    return s


def _parse_work_date(raw, today: date) -> date:
    """Accept native Excel dates, Excel serial dates, and common text dates,
    within a year of `today` (anything further out is a likely typo)."""
    parsed = _parse_any_date(raw)
    if abs((parsed - today).days) > 365:
        raise ValueError(f"Date {parsed.isoformat()} is more than a year from today — likely a typo.")
    return parsed


def _parse_any_date(raw) -> date:
    """`_parse_work_date` without the ±365-day bound — for historical rows
    (the salary-history importer reaches back to 2006)."""
    if raw is None:
        raise ValueError("Empty date.")
    if isinstance(raw, datetime):
        parsed = raw.date()
    elif isinstance(raw, date):
        parsed = raw
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            parsed = from_excel(raw).date()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Bad date '{raw}' — use a valid Excel date or text date.")
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("Empty date.")
        parsed = None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                parsed = None
        if parsed is None:
            try:
                parsed = date_parser.parse(s, fuzzy=False).date()
            except (ValueError, OverflowError, TypeError):
                raise ValueError(f"Bad date '{s}' — use a valid Excel date or text date.")
    return parsed


def parse_ot_seconds(raw) -> int:
    """Accept str 'HH:MM:SS', datetime.time, or timedelta — openpyxl returns
    a different type depending on the cell's number format (PAYROLL_PLAN.md
    §11 unit-test note 1).

    Public (2026-09-15) because the manual add/edit drawer parses the same
    HH:MM:SS box — payroll/forms.py::OvertimeEntryForm calls this rather than
    growing a second, subtly different parser."""
    if raw is None:
        raise ValueError("Empty OT hours.")
    if isinstance(raw, timedelta):
        seconds = int(raw.total_seconds())
    elif isinstance(raw, time):
        seconds = raw.hour * 3600 + raw.minute * 60 + raw.second
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # openpyxl sometimes hands back a bare day-fraction for duration cells.
        seconds = round(raw * 86400)
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("Empty OT hours.")
        parts = s.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Bad OT hours '{s}' — expected HH:MM:SS.")
        try:
            parts_int = [int(p) for p in parts]
        except ValueError:
            raise ValueError(f"Bad OT hours '{s}' — expected HH:MM:SS.")
        h, m = parts_int[0], parts_int[1]
        sec = parts_int[2] if len(parts_int) == 3 else 0
        seconds = h * 3600 + m * 60 + sec
    if seconds <= 0:
        raise ValueError("OT hours must be greater than zero.")
    return seconds


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def employed_on(employee, work_date: date) -> bool:
    """True if `employee` was actively employed on `work_date` — joined on
    or before it, and (if since separated) their last working date is on
    or after it. Day-granularity version of
    payroll.engine._population_queryset's "employed at any point in the
    period" predicate — catches a genuinely wrong employee ID/date (hired
    later, or long gone) while still accepting a resigned employee's
    legitimate final-period overtime, added 2026-09-08.

    Public (2026-09-15) — shared with the manual add/edit drawer."""
    if employee.joining_date and employee.joining_date > work_date:
        return False
    if employee.last_working_date and employee.last_working_date < work_date:
        return False
    return True


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _load_rows(file_obj, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return [list(row) for row in ws.iter_rows(values_only=True)]
    if name.endswith(".csv"):
        raw = file_obj.read()
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
        return [list(row) for row in csv.reader(io.StringIO(text))]
    raise ValueError("Unsupported file type — upload .xlsx or .csv.")


# ---------------------------------------------------------------------------
# Parse (read-only) + apply
# ---------------------------------------------------------------------------

def parse_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedRow / RowError lists. Read-only.

    Returns (parsed_rows, parse_errors, duplicate_errors). Lookups (employee,
    overtime type) are built once up-front — a few hundred rows costs 2
    queries total, not one per row.

    No period restriction — removed 2026-09-07 (was: rows outside a single
    passed-in `period`'s date range were rejected, "a supervisor uploads
    against one open period at a time"; in practice this just rejected
    legitimate historical/test data during development). Each row's
    `PayrollPeriod` is instead derived from its own `work_date` via
    `period_for_date()` (get-or-created, cached per (year, month) so a
    file spanning one or two periods still costs at most 1-2 extra
    queries, not one per row) — this also matches how the calculation
    engine already reads OvertimeEntry (payroll_inputs.collect_overtime
    filters by work_date range, never by the period FK), so a row's
    stored `period` is descriptive metadata, not the source of truth.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")

    body = rows[1:]  # row 0 is the header
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 2 and not _is_blank(row[1]):
            try:
                emp_ids.add(_coerce_employee_id(row[1]))
            except ValueError:
                continue
    # Deliberately NOT is_active=True — same reasoning as
    # payroll.engine._population_queryset: an employee deactivated by
    # deactivate_past_lwd after resigning must still be payable for
    # overtime worked before their last working date. The per-row
    # `_employed_on()` check below verifies the actual employment window
    # instead, so a stale/wrong employee ID still gets caught.
    employees_by_id = {
        u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)
    }
    ot_type_cache = _build_ot_type_cache()
    team_cache = _build_team_cache()
    period_cache: dict[tuple[int, int], PayrollPeriod] = {}

    def _period_for(work_date: date) -> PayrollPeriod:
        key = period_for_date(work_date)
        period = period_cache.get(key)
        if period is None:
            period = PayrollPeriod.for_month(*key)
            period_cache[key] = period
        return period

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedRow] = []
    seen: dict[tuple[str, date, int], int] = {}  # (emp_id, date, ot_type_pk) → first line_no

    for idx, row in enumerate(body, start=2):  # header is row 1
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Working_date": row[0] if len(row) > 0 else None,
            "Employee_ID": row[1] if len(row) > 1 else None,
            "Worked_In": row[2] if len(row) > 2 else None,
            "Overtime_type": row[3] if len(row) > 3 else None,
            "OT_hours": row[4] if len(row) > 4 else None,
        }
        try:
            work_date = _parse_work_date(row[0] if len(row) > 0 else None, today)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        try:
            emp_id_str = _coerce_employee_id(row[1] if len(row) > 1 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "B", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        if not employed_on(employee, work_date):
            parse_errors.append(RowError(
                idx, "B", raw_snapshot,
                f"{employee.display_name} wasn't employed on {work_date.isoformat()} "
                f"(joined {employee.joining_date.isoformat() if employee.joining_date else 'unknown'}"
                + (f", last working date {employee.last_working_date.isoformat()}"
                   if employee.last_working_date else "")
                + ").",
            ))
            continue
        worked_in_raw = row[2] if len(row) > 2 else None
        worked_in = None
        if not _is_blank(worked_in_raw):
            worked_in = team_cache.get(str(worked_in_raw).strip().lower())
            if worked_in is None:
                parse_errors.append(RowError(idx, "C", raw_snapshot, f"Unknown team '{worked_in_raw}'."))
                continue
        type_raw = row[3] if len(row) > 3 else None
        ot_type = ot_type_cache.get(str(type_raw).strip().lower()) if not _is_blank(type_raw) else None
        if ot_type is None:
            parse_errors.append(RowError(idx, "D", raw_snapshot, f"Unknown overtime type '{type_raw}'."))
            continue
        try:
            seconds = parse_ot_seconds(row[4] if len(row) > 4 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "E", raw_snapshot, str(exc)))
            continue

        key = (emp_id_str, work_date, ot_type.pk)
        if key in seen:
            dup_errors.append(RowError(
                idx, "E", raw_snapshot,
                f"Duplicate of row {seen[key]} — same employee, date and overtime type appears twice.",
            ))
            continue
        seen[key] = idx

        parsed.append(ParsedRow(
            line_no=idx, employee=employee, employee_id=emp_id_str,
            work_date=work_date, overtime_type=ot_type, seconds=seconds,
            period=_period_for(work_date), worked_in=worked_in,
        ))

    return parsed, parse_errors, dup_errors


def apply_upload(
    viewer, parsed_rows: list[ParsedRow], viewer_visible_pks: set,
    *, batch=None, window=None,
) -> ApplyReport:
    """Apply parsed rows to OvertimeEntry inside one transaction.

    Rows whose employee is outside `viewer_visible_pks` are not written; they
    appear in `report.skipped_scope` instead — mirrors roster/upload.py's
    scope-check shape exactly. A single ``bulk_create(update_conflicts=True)``
    inserts new rows and overwrites existing ones (same employee/date/type)
    in one round-trip.

    Each row is written against its own `row.period` (derived from its
    `work_date` in parse_upload — no shared upload-time period anymore, see
    parse_upload's docstring), so a file spanning multiple periods writes
    correctly to each one.

    `window` (payroll.services.OvertimeWindow) is re-applied here even though
    the view already partitioned on it: this is the write path, and a date
    rule enforced only by the caller is one refactor away from not being
    enforced at all. Rows it rejects land in skipped_window/skipped_locked.

    Every written row lands PENDING (the review chain, 2026-10-17). What a
    re-upload does to an existing row depends on that row's state:

    - PENDING: overwritten (still pending).
    - REVIEWED / APPROVED, same figures: left alone (`unchanged`) — the
      sign-off stands.
    - REVIEWED / APPROVED, different figures, open period: overwritten and
      sent back to PENDING with both stamps cleared (`resubmitted`).
    - APPROVED / VOID in a LOCKED period: refused (`skipped_locked`) — that
      is settled history, HR-Payroll's to change.
    - VOID in an open period: revived as PENDING.
    """
    report = ApplyReport()

    in_scope: list[ParsedRow] = []
    for row in parsed_rows:
        if row.employee.pk not in viewer_visible_pks:
            report.skipped_scope.append(RowError(
                row.line_no, None,
                {"Employee_ID": row.employee_id, "Working_date": row.work_date.isoformat()},
                "Outside your managed scope.",
            ))
            continue
        if window is not None:
            decision = window.resolve(row.work_date)
            if not decision.allowed:
                report.skipped_window.append(RowError(
                    row.line_no, "A",
                    {"Employee_ID": row.employee_id, "Working_date": row.work_date.isoformat()},
                    decision.reason,
                ))
                continue
            row.window_status = decision.status
        else:
            row.window_status = OvertimeEntry.Status.PENDING
        in_scope.append(row)

    if not in_scope:
        report.applied = True  # nothing to do, but the upload itself didn't error
        return report

    emp_pks = {r.employee.pk for r in in_scope}
    dates = {r.work_date for r in in_scope}
    # Status AND figures, not just the key: what a re-upload does to a row
    # depends on whether it is settled and whether anything changed (see
    # the branches below and the docstring).
    existing = {
        (emp_id, work_date, type_id): (status, seconds, worked_in_id)
        for emp_id, work_date, type_id, status, seconds, worked_in_id in (
            OvertimeEntry.objects
            .filter(employee_id__in=emp_pks, work_date__in=dates)
            .values_list("employee_id", "work_date", "overtime_type_id", "status", "seconds", "worked_in_id")
        )
    }
    settled = (OvertimeEntry.Status.APPROVED, OvertimeEntry.Status.VOID)
    signed = (OvertimeEntry.Status.REVIEWED, OvertimeEntry.Status.APPROVED)

    now = timezone.now()
    objs: list[OvertimeEntry] = []
    for r in in_scope:
        key = (r.employee.pk, r.work_date, r.overtime_type.pk)
        current = existing.get(key)
        current_status = current[0] if current else None
        # Snapshot at write time, same rule as OvertimeEntryForm.clean():
        # a blank cell defaults to the employee's current team, but a
        # later team transfer doesn't retroactively change this entry.
        worked_in = r.worked_in or r.employee.team

        # A locked period may only ADD overtime or amend one not yet
        # approved. An entry already approved — or deliberately voided —
        # for a locked period is settled: silently re-pointing it would
        # either change pay after approval or drop an approved entry back
        # to unpaid. Those need HR-Payroll, not a re-upload.
        locked = window is not None and window.is_locked(r.work_date)
        if locked and current_status in settled:
            verb = "voided" if current_status == OvertimeEntry.Status.VOID else "approved"
            report.skipped_locked.append(RowError(
                r.line_no, "A",
                {"Employee_ID": r.employee_id, "Working_date": r.work_date.isoformat()},
                f"Already {verb} for a locked period — ask HR-Payroll to change it.",
            ))
            continue

        if current_status in signed:
            same = (
                current[1] == r.seconds
                and current[2] == (worked_in.pk if worked_in is not None else None)
            )
            if same:
                # Nothing to correct — leave the sign-off standing rather
                # than knocking it back to Pending for an identical figure.
                report.unchanged.append(r)
                continue
            report.resubmitted.append(r)

        objs.append(OvertimeEntry(
            employee=r.employee, period=r.period, work_date=r.work_date,
            overtime_type=r.overtime_type, seconds=r.seconds, batch=batch,
            worked_in=worked_in,
            status=r.window_status,
            added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
        ))
        if current is not None:
            report.updated.append(r)
        else:
            report.created.append(r)
        report.pending.append(r)

    # `unique_fields` is required by SQLite/PostgreSQL but rejected by MySQL
    # (MySQL's ON DUPLICATE KEY UPDATE matches on any unique constraint
    # automatically) — same guard as roster/upload.py::apply_upload.
    bulk_kwargs: dict = {
        "update_conflicts": True,
        # "status" included (2026-09-15) so re-uploading a VOID row in an OPEN
        # period revives it; without it the uploader reports "updated" while
        # the entry stays voided and the overtime silently never gets paid.
        #
        # Writing status + the four stamps unconditionally is safe because
        # every row that survives the loop above must end up PENDING with
        # no stamps: a create by definition, a PENDING update is a no-op, a
        # changed REVIEWED/APPROVED row is exactly the reset we want, and an
        # unchanged one was skipped into `unchanged` before getting here.
        # Rows where a reset would be wrong were rejected into skipped_locked.
        "update_fields": [
            "seconds", "period", "batch", "worked_in", "status",
            "reviewed_by", "reviewed_at", "processed_by", "processed_at",
            "updated_by", "updated_at",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        bulk_kwargs["unique_fields"] = ["employee", "work_date", "overtime_type"]

    with transaction.atomic():
        OvertimeEntry.objects.bulk_create(objs, **bulk_kwargs)

    report.applied = True
    return report


def build_overtime_xlsx_template(viewer) -> bytes:
    """Blank .xlsx template for the overtime uploader.

    Unlike build_salary_xlsx_template's one-row-per-employee prefill,
    overtime entries are sparse (most employees have none in a given
    period), so this ships blank data rows formatted for the five parsed
    columns plus dropdowns on Worked In and Overtime type, and a separate
    reference sheet listing the viewer's own managed-scope employees (ID +
    name) so they don't have to look IDs up elsewhere. Scoped to `viewer` —
    a manager's template only lists people they're actually allowed to
    upload for, mirroring roster/upload.py::_template_employees.

    No longer takes a `period` — parse_upload accepts any working date and
    derives each row's period itself (removed 2026-09-07, see its docstring).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Overtime"

    headers = ["Working date", "Employee ID", "Worked In", "Overtime type", "OT hours (hh:mm:ss)"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    blank_rows = 40
    last_row = 1 + blank_rows
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = "yyyy-mm-dd"
        ws.cell(row=row, column=2).number_format = "@"
        ws.cell(row=row, column=5).number_format = "hh:mm:ss"

    ot_type_names = list(
        OvertimeType.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)
    )
    team_names = list(Team.objects.filter(is_active=True).order_by("name").values_list("name", flat=True))
    lookups = wb.create_sheet("Lookups")
    lookups.cell(row=1, column=1, value="Overtime type values")
    for i, name in enumerate(ot_type_names, start=2):
        lookups.cell(row=i, column=1, value=name)
    lookups.cell(row=1, column=2, value="Team values")
    for i, name in enumerate(team_names, start=2):
        lookups.cell(row=i, column=2, value=name)
    lookups.sheet_state = "hidden"

    date_dv = DataValidation(type="date", allow_blank=True)
    date_dv.error = "Enter a valid date."
    date_dv.errorTitle = "Invalid date"
    ws.add_data_validation(date_dv)
    date_dv.add(f"A2:A{last_row}")

    if team_names:
        team_range = f"=Lookups!$B$2:$B${1 + len(team_names)}"
        team_dv = DataValidation(type="list", formula1=team_range, allow_blank=True)
        team_dv.error = "Pick a value from the dropdown."
        team_dv.errorTitle = "Invalid team"
        ws.add_data_validation(team_dv)
        team_dv.add(f"C2:C{last_row}")

    if ot_type_names:
        type_range = f"=Lookups!$A$2:$A${1 + len(ot_type_names)}"
        type_dv = DataValidation(type="list", formula1=type_range, allow_blank=True)
        type_dv.error = "Pick a value from the dropdown."
        type_dv.errorTitle = "Invalid overtime type"
        ws.add_data_validation(type_dv)
        type_dv.add(f"D2:D{last_row}")

    employees = list(
        visible_employees(viewer, request=None)
        .filter(is_active=True, employee_id__isnull=False).exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )
    ref = wb.create_sheet("Employees (reference)")
    ref.cell(row=1, column=1, value="Employee ID")
    ref.cell(row=1, column=2, value="Employee name")
    for i, emp in enumerate(employees, start=2):
        ref.cell(row=i, column=1, value=emp.employee_id).number_format = "@"
        ref.cell(row=i, column=2, value=escape_spreadsheet(emp.display_name))

    widths = {"A": 14, "B": 14, "C": 22, "D": 22, "E": 20}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Gross salary bulk upload — flat 4-column layout
# (Employee ID | Gross monthly salary | Effective from | Reason)
#
# effective_from AND reason are per-row, not batch-level fields on the
# upload form — a single January sweep may raise most of the roster
# (reason=ANNUAL_INCREMENT) while a handful of rows in the same file are
# promotions or corrections, each needing its own reason. effective_from is
# per-row because payroll/engine.py picks which SalaryStructure applies to a
# run via effective_from__lte=period.end_date, so a uniform date for every
# employee would misdate anyone whose actual current salary took effect
# earlier and break payroll runs for periods before that date. effective_to
# is NOT a column — apply_salary_upload derives it automatically when it
# supersedes the employee's prior row (see below).
#
# apply_salary_upload SUPERSEDES rather than overwrites: the employee's
# existing APPROVED/open-ended row is closed (effective_to = the new row's
# effective_from minus one day, status=SUPERSEDED) and a new APPROVED row is
# created with revision_no+1. This preserves real compensation history for
# a detail view, matching what SalaryStructure's own fields (revision_no,
# effective_to, Status.SUPERSEDED) were already built for — see that
# model's clean() error message. Each row is applied in its own savepoint
# so one bad row (e.g. an effective_from that doesn't come after the
# employee's current row) is reported in apply_errors without aborting the
# rest of the batch.
# ---------------------------------------------------------------------------

@dataclass
class ParsedSalaryRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    gross_monthly: Decimal
    effective_from: date
    reason: str                      # a SalaryStructure.Reason value

    @property
    def reason_label(self) -> str:
        return SalaryStructure.Reason(self.reason).label


@dataclass
class SalaryApplyReport:
    created: list[ParsedSalaryRow] = field(default_factory=list)
    updated: list[ParsedSalaryRow] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    apply_errors: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)


_REASON_BY_KEY = {
    key: value
    for value, label in SalaryStructure.Reason.choices
    for key in (value.lower(), label.lower())
}


def _coerce_reason(raw) -> str:
    if _is_blank(raw):
        raise ValueError("Empty reason.")
    key = str(raw).strip().lower()
    reason = _REASON_BY_KEY.get(key)
    if reason is None:
        valid = ", ".join(label for _, label in SalaryStructure.Reason.choices)
        raise ValueError(f"Unknown reason '{raw}' — expected one of: {valid}.")
    return reason


def _parse_gross_monthly(raw) -> Decimal:
    if raw is None:
        raise ValueError("Empty gross monthly salary.")
    if isinstance(raw, bool):
        raise ValueError(f"Bad gross monthly salary: {raw!r}.")
    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        s = str(raw).strip().replace(",", "")
        if not s:
            raise ValueError("Empty gross monthly salary.")
        try:
            value = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"Bad gross monthly salary '{raw}' — expected a number.")
    if value <= 0:
        raise ValueError("Gross monthly salary must be greater than zero.")
    return value.quantize(Decimal("0.01"))


def parse_salary_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedSalaryRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedSalaryRow / RowError lists. Read-only.

    Four columns, header on row 1: Employee ID | Gross monthly salary | Effective from | Reason.
    Reason accepts either the code (e.g. "ANNUAL_INCREMENT") or the display
    label (e.g. "Annual increment"), case-insensitively.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")

    body = rows[1:]  # row 0 is the header
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    # Deliberately NOT is_active=True — same bug, same fix as the overtime
    # uploader (2026-09-08, PAYROLL_PLAN.md): an employee deactivated by
    # deactivate_past_lwd after resigning must still be payable for a
    # backdated correction effective before their last working date (e.g. a
    # mid-year increment that was never entered before they left). The
    # per-row employed_on() check below verifies the actual employment
    # window instead, so a genuinely wrong employee ID/date is still caught.
    employees_by_id = {
        u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)
    }

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedSalaryRow] = []
    seen: dict[str, int] = {}  # employee_id → first line_no

    for idx, row in enumerate(body, start=2):  # header is row 1
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Gross_monthly": row[1] if len(row) > 1 else None,
            "Effective_from": row[2] if len(row) > 2 else None,
            "Reason": row[3] if len(row) > 3 else None,
        }
        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        try:
            gross = _parse_gross_monthly(row[1] if len(row) > 1 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue
        try:
            effective_from = _parse_work_date(row[2] if len(row) > 2 else None, today)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "C", raw_snapshot, str(exc)))
            continue
        if not employed_on(employee, effective_from):
            parse_errors.append(RowError(
                idx, "C", raw_snapshot,
                f"{employee.display_name} wasn't employed on {effective_from.isoformat()} "
                f"(joined {employee.joining_date.isoformat() if employee.joining_date else 'unknown'}"
                + (f", last working date {employee.last_working_date.isoformat()}"
                   if employee.last_working_date else "")
                + ").",
            ))
            continue
        try:
            reason = _coerce_reason(row[3] if len(row) > 3 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "D", raw_snapshot, str(exc)))
            continue

        if emp_id_str in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[emp_id_str]} — this employee appears twice in the file.",
            ))
            continue
        seen[emp_id_str] = idx

        parsed.append(ParsedSalaryRow(
            line_no=idx, employee=employee, employee_id=emp_id_str,
            gross_monthly=gross, effective_from=effective_from, reason=reason,
        ))

    return parsed, parse_errors, dup_errors


def apply_salary_upload(
    viewer, parsed_rows: list[ParsedSalaryRow], *, note: str, batch=None,
) -> SalaryApplyReport:
    """Supersede each employee's current SalaryStructure row.

    Closes the existing APPROVED/open-ended row, if any (effective_to = the
    new row's effective_from minus one day, status=SUPERSEDED), then creates
    a new APPROVED row with revision_no+1 — preserves compensation history
    instead of overwriting it. Each row applies in its own savepoint: a row
    that fails validation (most commonly an effective_from that doesn't
    come after the employee's current row) is recorded in
    report.apply_errors and skipped, without rolling back the rest of the
    batch.
    """
    report = SalaryApplyReport()
    if not parsed_rows:
        report.applied = True
        return report

    emp_pks = {r.employee.pk for r in parsed_rows}
    current_by_emp = {
        s.employee_id: s
        for s in SalaryStructure.objects.filter(
            employee_id__in=emp_pks, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
        )
    }

    for r in parsed_rows:
        created = current_by_emp.get(r.employee.pk) is None
        try:
            # Shared with the go-live approval path (2026-09-19) so the two
            # can't drift on revision numbering or on what effective_to the
            # closed row gets — see payroll/services.py.
            supersede_salary_structure(
                r.employee,
                gross_monthly=r.gross_monthly, effective_from=r.effective_from,
                reason=r.reason, actor=viewer, note=note, batch=batch,
            )
        except ValidationError as exc:
            messages = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            report.apply_errors.append(RowError(
                r.line_no, None,
                {
                    "Employee_ID": r.employee_id, "Gross_monthly": str(r.gross_monthly),
                    "Effective_from": r.effective_from.isoformat(), "Reason": r.reason_label,
                },
                messages,
            ))
            continue
        (report.created if created else report.updated).append(r)

    report.applied = True
    return report


def build_salary_xlsx_template() -> bytes:
    """Prefilled .xlsx template for the gross-salary uploader.

    Columns A–D are exactly parse_salary_upload's four parsed columns, in
    order — Employee ID | Gross monthly salary | Effective from | Reason —
    so the documented file format stays accurate whether Finance types a
    file by hand or downloads this. Columns E–F (Employee Name, Current
    gross) are reference-only, placed after the parsed columns rather than
    interleaved, and ignored by the parser. Reason gets a dropdown sourced
    from SalaryStructure.Reason via a hidden Lookups sheet, mirroring
    roster/upload.py::build_flat_xlsx_template's pattern.

    Every active employee is prefilled with a blank row — Finance deletes
    the rows they don't need to touch this cycle before uploading (an
    unedited row fails to parse: empty gross/date/reason).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Gross Salary"

    # employee_id is a CharField of digits, so a plain string sort puts "100"
    # before "20" — cast to int for true ascending numeric order (same fix
    # as payroll/reports.py::build_shift_summary).
    employees = list(
        User.objects.filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )
    current_by_emp = {
        s.employee_id: s.gross_monthly  # keyed by the FK's raw pk (CustomUser.pk), not the business employee_id
        for s in SalaryStructure.objects.filter(
            status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
        )
    }

    headers = [
        "Employee ID", "Gross monthly salary", "Effective from", "Reason",
        "Employee Name", "Current gross (reference only)",
    ]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    row_idx = 2
    for emp in employees:
        eid_cell = ws.cell(row=row_idx, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        ws.cell(row=row_idx, column=5, value=escape_spreadsheet(emp.get_full_name()))
        current = current_by_emp.get(emp.pk)
        if current is not None:
            ref = ws.cell(row=row_idx, column=6, value=money_value(current))
            ref.number_format = EXCEL_NUMBER_FORMAT
        row_idx += 1

    reason_labels = [label for _, label in SalaryStructure.Reason.choices]
    lookups = wb.create_sheet("Lookups")
    lookups.cell(row=1, column=1, value="Reason values")
    for i, label in enumerate(reason_labels, start=2):
        lookups.cell(row=i, column=1, value=label)
    lookups.sheet_state = "hidden"

    if row_idx > 2:
        date_dv = DataValidation(type="date", allow_blank=True)
        date_dv.error = "Enter a valid date."
        date_dv.errorTitle = "Invalid date"
        ws.add_data_validation(date_dv)
        date_dv.add(f"C2:C{row_idx - 1}")

        reason_range = f"=Lookups!$A$2:$A${1 + len(reason_labels)}"
        reason_dv = DataValidation(type="list", formula1=reason_range, allow_blank=True)
        reason_dv.error = "Pick a value from the dropdown."
        reason_dv.errorTitle = "Invalid reason"
        ws.add_data_validation(reason_dv)
        reason_dv.add(f"D2:D{row_idx - 1}")

        for row in range(2, row_idx):
            ws.cell(row=row, column=3).number_format = "yyyy-mm-dd"

    widths = {"A": 14, "B": 18, "C": 14, "D": 22, "E": 24, "F": 22}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Gross salary HISTORY import — one-shot per employee, whole chain at once
# (Employee ID | Gross monthly salary | Effective from | Effective to | Reason)
#
# Added 2026-10-05 for the legacy HR import (docs/LEGACY_60_IMPORT_PLAN.md
# §3). The regular uploader above can't take a history: it bounds dates to
# ±365 days, allows one row per employee per file, and only supersedes
# *forward*. This sibling accepts the full chain for an employee who has NO
# SalaryStructure rows yet, validates it as a chain (contiguous, exactly one
# open-ended row), and writes it directly — closed rows SUPERSEDED with their
# effective_to, the last row APPROVED/open — so the engine's
# _salary_segments prices every historical period at the rate actually in
# force. An employee with any existing row is skipped (skipped_existing):
# corrections after import go through the forward uploader or admin, which
# keeps "one APPROVED open-ended row per employee" trivially true.
#
# HR's sheets run on calendar years, so the first row usually starts on Jan 1
# of the joining year — before the joining date. That first row is clamped to
# joining_date with a warning rather than rejected (confirmed with HR); any
# later row before joining is a genuine error.
# ---------------------------------------------------------------------------

@dataclass
class ParsedSalaryHistoryRow:
    line_no: int
    gross_monthly: Decimal
    effective_from: date
    effective_to: date | None
    reason: str

    @property
    def reason_label(self) -> str:
        return SalaryStructure.Reason(self.reason).label


@dataclass
class ParsedSalaryHistory:
    """One employee's validated chain, oldest first."""
    employee: object                 # CustomUser
    employee_id: str
    rows: list[ParsedSalaryHistoryRow]

    @property
    def first_line_no(self) -> int:
        return self.rows[0].line_no

    @property
    def current(self) -> ParsedSalaryHistoryRow:
        return self.rows[-1]


@dataclass
class SalaryHistoryWarning:
    line_no: int
    employee_id: str
    message: str


@dataclass
class SalaryHistoryApplyReport:
    created: list[ParsedSalaryHistory] = field(default_factory=list)
    skipped_existing: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[SalaryHistoryWarning] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return sum(len(h.rows) for h in self.created)

    @property
    def employees_written(self) -> int:
        return len(self.created)


def parse_salary_history_upload(
    file_obj, filename: str, *, today: date | None = None,
) -> tuple[list[ParsedSalaryHistory], list[RowError], list[SalaryHistoryWarning]]:
    """Read-only. Five columns, header on row 1:
    Employee ID | Gross monthly salary | Effective from | Effective to | Reason.

    Effective to is blank on exactly one row per employee (the current
    salary). Reason is optional: blank means "New hire" for the oldest row and
    "Annual increment" for every later one. Rows for the same employee may
    appear in any order; they are sorted by Effective from and validated as
    a chain. A broken chain fails every row of that employee (reported once
    per gap) so a partial history is never written.
    """
    today = today or date.today()
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    # Not restricted to is_active — a history is still a history for a
    # separated employee (same posture as parse_salary_upload).
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    parse_errors: list[RowError] = []
    warnings: list[SalaryHistoryWarning] = []
    by_employee: dict[str, list[ParsedSalaryHistoryRow]] = {}

    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Gross_monthly": row[1] if len(row) > 1 else None,
            "Effective_from": row[2] if len(row) > 2 else None,
            "Effective_to": row[3] if len(row) > 3 else None,
            "Reason": row[4] if len(row) > 4 else None,
        }
        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        if emp_id_str not in employees_by_id:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        try:
            gross = _parse_gross_monthly(row[1] if len(row) > 1 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue
        try:
            effective_from = _parse_any_date(row[2] if len(row) > 2 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "C", raw_snapshot, str(exc)))
            continue
        effective_to = None
        to_cell = row[3] if len(row) > 3 else None
        if not _is_blank(to_cell):
            try:
                effective_to = _parse_any_date(to_cell)
            except ValueError as exc:
                parse_errors.append(RowError(idx, "D", raw_snapshot, str(exc)))
                continue
            if effective_to < effective_from:
                parse_errors.append(RowError(
                    idx, "D", raw_snapshot,
                    f"Effective to {effective_to.isoformat()} is before Effective from {effective_from.isoformat()}.",
                ))
                continue
        if effective_from > today:
            parse_errors.append(RowError(
                idx, "C", raw_snapshot, f"Effective from {effective_from.isoformat()} is in the future.",
            ))
            continue
        reason_cell = row[4] if len(row) > 4 else None
        reason = None
        if not _is_blank(reason_cell):
            try:
                reason = _coerce_reason(reason_cell)
            except ValueError as exc:
                parse_errors.append(RowError(idx, "E", raw_snapshot, str(exc)))
                continue
        by_employee.setdefault(emp_id_str, []).append(ParsedSalaryHistoryRow(
            line_no=idx, gross_monthly=gross, effective_from=effective_from,
            effective_to=effective_to, reason=reason or "",
        ))

    parsed: list[ParsedSalaryHistory] = []
    for emp_id_str, emp_rows in by_employee.items():
        employee = employees_by_id[emp_id_str]
        emp_rows.sort(key=lambda r: r.effective_from)
        chain_ok = True

        def _chain_error(r: ParsedSalaryHistoryRow, col: str, message: str) -> None:
            nonlocal chain_ok
            chain_ok = False
            parse_errors.append(RowError(
                r.line_no, col,
                {"Employee_ID": emp_id_str, "Gross_monthly": str(r.gross_monthly),
                 "Effective_from": r.effective_from.isoformat(),
                 "Effective_to": r.effective_to.isoformat() if r.effective_to else None},
                message,
            ))

        joining = employee.joining_date
        first = emp_rows[0]
        if joining and first.effective_from < joining:
            warnings.append(SalaryHistoryWarning(
                first.line_no, emp_id_str,
                f"First row started {first.effective_from.isoformat()}, before the joining date "
                f"{joining.isoformat()} — clamped to the joining date.",
            ))
            first.effective_from = joining
            if first.effective_to is not None and first.effective_to < joining:
                _chain_error(
                    first, "D",
                    f"Row ends {first.effective_to.isoformat()}, before the joining date {joining.isoformat()}.",
                )
        for r in emp_rows[1:]:
            if joining and r.effective_from < joining:
                _chain_error(
                    r, "C",
                    f"Effective from {r.effective_from.isoformat()} is before the joining date "
                    f"{joining.isoformat()} and isn't the oldest row.",
                )
        for a, b in zip(emp_rows, emp_rows[1:]):
            if a.effective_to is None:
                _chain_error(a, "D", "Effective to is blank but a later row exists — only the current salary may be open-ended.")
                continue
            if b.effective_from == a.effective_from:
                _chain_error(b, "C", f"Two rows start on {b.effective_from.isoformat()}.")
                continue
            expected = a.effective_to + timedelta(days=1)
            if b.effective_from != expected:
                kind = "gap" if b.effective_from > expected else "overlap"
                _chain_error(
                    b, "C",
                    f"Chain {kind}: previous row ends {a.effective_to.isoformat()}, "
                    f"this one starts {b.effective_from.isoformat()} (expected {expected.isoformat()}).",
                )
        last = emp_rows[-1]
        if last.effective_to is not None:
            _chain_error(last, "D", "The latest row must have a blank Effective to — it is the current salary.")
        if employee.last_working_date and last.effective_from > employee.last_working_date:
            _chain_error(
                last, "C",
                f"Starts after the employee's last working date {employee.last_working_date.isoformat()}.",
            )
        if not chain_ok:
            continue
        for i, r in enumerate(emp_rows):
            if not r.reason:
                r.reason = (
                    SalaryStructure.Reason.NEW_HIRE if i == 0 else SalaryStructure.Reason.ANNUAL_INCREMENT
                )
        parsed.append(ParsedSalaryHistory(employee=employee, employee_id=emp_id_str, rows=emp_rows))

    parsed.sort(key=lambda h: h.first_line_no)
    return parsed, parse_errors, warnings


def apply_salary_history_upload(
    viewer, parsed: list[ParsedSalaryHistory], *, note: str, batch=None,
) -> SalaryHistoryApplyReport:
    """Write each employee's chain. One-shot: an employee with any existing
    SalaryStructure row (any status) is skipped and reported. Each employee
    is its own atomic block, so one failure never leaves a partial chain."""
    from django.utils import timezone

    report = SalaryHistoryApplyReport()
    if not parsed:
        report.applied = True
        return report

    emp_pks = {h.employee.pk for h in parsed}
    has_rows = set(
        SalaryStructure.objects.filter(employee_id__in=emp_pks).values_list("employee_id", flat=True)
    )
    now = timezone.now()
    for h in parsed:
        if h.employee.pk in has_rows:
            report.skipped_existing.append(RowError(
                h.first_line_no, "A",
                {"Employee_ID": h.employee_id, "Gross_monthly": str(h.current.gross_monthly),
                 "Effective_from": h.current.effective_from.isoformat(), "Effective_to": None},
                "Already has salary records in Orbis — history import is one-shot. "
                "Use the regular gross-salary upload (or admin) to correct them.",
            ))
            continue
        with transaction.atomic():
            for revision_no, r in enumerate(h.rows, start=1):
                is_current = r.effective_to is None
                SalaryStructure.objects.create(
                    employee=h.employee,
                    revision_no=revision_no,
                    effective_from=r.effective_from, effective_to=r.effective_to,
                    reason=r.reason, gross_monthly=r.gross_monthly,
                    status=SalaryStructure.Status.APPROVED if is_current else SalaryStructure.Status.SUPERSEDED,
                    processed_by=viewer, processed_at=now,
                    note=note[:255], batch=batch,
                    added_by=viewer, added_at=now, updated_by=viewer,
                )
        report.created.append(h)

    report.applied = True
    return report


def build_salary_history_xlsx_template() -> bytes:
    """One prefilled row per employee WITHOUT salary records yet (those with
    records are skipped by the importer anyway): Employee ID, blank amount /
    dates / reason, then reference Name + Joining date. HR adds one row per
    historical salary beneath each employee's line."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Salary History"

    with_rows = set(SalaryStructure.objects.values_list("employee_id", flat=True))
    employees = [
        u for u in User.objects.filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
        if u.pk not in with_rows
    ]

    headers = [
        "Employee ID", "Gross monthly salary", "Effective from", "Effective to", "Reason",
        "Employee Name", "Joining date (reference only)",
    ]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    row_idx = 2
    for emp in employees:
        eid_cell = ws.cell(row=row_idx, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        ws.cell(row=row_idx, column=6, value=escape_spreadsheet(emp.get_full_name()))
        if emp.joining_date:
            ws.cell(row=row_idx, column=7, value=emp.joining_date.isoformat())
        row_idx += 1

    reason_labels = [label for _, label in SalaryStructure.Reason.choices]
    lookups = wb.create_sheet("Lookups")
    lookups.cell(row=1, column=1, value="Reason values")
    for i, label in enumerate(reason_labels, start=2):
        lookups.cell(row=i, column=1, value=label)
    lookups.sheet_state = "hidden"

    if row_idx > 2:
        reason_dv = DataValidation(
            type="list", formula1=f"=Lookups!$A$2:$A${1 + len(reason_labels)}", allow_blank=True,
        )
        reason_dv.error = "Pick a value from the dropdown."
        reason_dv.errorTitle = "Invalid reason"
        ws.add_data_validation(reason_dv)
        reason_dv.add(f"E2:E{row_idx + 500}")
        for row in range(2, row_idx):
            ws.cell(row=row, column=3).number_format = "yyyy-mm-dd"
            ws.cell(row=row, column=4).number_format = "yyyy-mm-dd"

    widths = {"A": 14, "B": 20, "C": 14, "D": 14, "E": 22, "F": 26, "G": 26}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Payroll run notes bulk upload — flat 2-column layout (Employee ID | Note)
#
# One upload writes exactly one PayrollRunEmployeeNote.Role — chosen once on
# the upload page (payroll/upload_views.py::UploadPayrollNotesView), not a
# per-row column. This matches how the feature is actually used: a
# supervisor filing Supervisor notes for their whole team in one file,
# rather than mixing Supervisor/HR/Finance rows in the same sheet. Scoped to
# a single PayrollRun, since PayrollRunEmployeeNote FKs to PayrollRun (never
# PayrollRunEmployee — see that model's docstring).
#
# A blank Note cell is a no-op, not a clear — see apply_notes_upload. To
# actually clear a note, use the existing per-employee drawer
# (PayrollRunEmployeeNoteEditView).
#
# Every row is re-checked against accounts.permissions.can_write_payroll_note
# at apply time, not just a flat visible-pks set: HR/Finance write access is
# a pure group check while Supervisor access is per-employee managed-scope —
# the same per-row shape payroll/views.py::PayrollRunEmployeeNoteEditView
# already uses for the non-bulk drawer.
# ---------------------------------------------------------------------------

@dataclass
class ParsedNoteRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    note: str                        # "" means "blank cell" — see apply_notes_upload


@dataclass
class NoteApplyReport:
    created: list[ParsedNoteRow] = field(default_factory=list)
    updated: list[ParsedNoteRow] = field(default_factory=list)
    skipped_scope: list[RowError] = field(default_factory=list)
    # Retained but always empty since 2026-09-19 — a locked run no longer
    # refuses note uploads (see apply_notes_upload). Kept so the report shape
    # and its template stay stable.
    skipped_locked: list[RowError] = field(default_factory=list)
    # Cell was blank — left unchanged rather than clearing an existing note.
    skipped_blank: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)

    @property
    def total_skipped(self) -> int:
        return len(self.skipped_scope) + len(self.skipped_locked) + len(self.skipped_blank)


def parse_notes_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedNoteRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedNoteRow / RowError lists. Read-only.

    Two columns, header on row 1: Employee ID | Note. `today` is accepted for
    the same signature shape as parse_upload/parse_salary_upload, even though
    nothing here is date-sensitive.

    A blank Note cell is NOT a parse error — it's a legitimate "no change for
    this employee" row (see apply_notes_upload's skipped_blank), so a
    partially-filled template still uploads cleanly.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")

    body = rows[1:]  # row 0 is the header
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    # Deliberately NOT is_active=True — same reasoning as parse_upload: a note
    # about a recently-separated employee's final period must still resolve.
    employees_by_id = {
        u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)
    }

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedNoteRow] = []
    seen: dict[str, int] = {}  # employee_id → first line_no

    for idx, row in enumerate(body, start=2):  # header is row 1
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Note": row[1] if len(row) > 1 else None,
        }
        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue

        if emp_id_str in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[emp_id_str]} — this employee appears twice in the file.",
            ))
            continue
        seen[emp_id_str] = idx

        note_raw = row[1] if len(row) > 1 else None
        parsed.append(ParsedNoteRow(
            line_no=idx, employee=employee, employee_id=emp_id_str,
            note="" if _is_blank(note_raw) else str(note_raw).strip(),
        ))

    return parsed, parse_errors, dup_errors


def apply_notes_upload(
    viewer, parsed_rows: list[ParsedNoteRow], role: str, *, run, request=None,
) -> NoteApplyReport:
    """Write parsed rows as PayrollRunEmployeeNote(run=run, role=role).

    **No longer refuses on a locked run** (changed 2026-09-19, Phase E). The
    original posture closed bulk note uploads once inputs were locked, while
    the per-employee drawer next to it had no such gate — so the same edit
    was allowed one at a time and refused in bulk, which mostly taught
    supervisors to send Finance a message instead. Notes never feed the
    engine: they are commentary, not an input, so there is nothing about a
    locked run that a note can corrupt. `skipped_locked` is retained on the
    report (always empty now) so callers and templates don't have to change.

    What *does* now push back on a late note is the Phase E linter
    (payroll/notes_lint.py): a note naming money that no structured row
    accounts for raises a control check on the next calculation.

    A blank row.note is a no-op (skipped_blank) rather than clearing an
    existing note — a stray blank cell in a few-hundred-row file must not
    silently wipe out a note someone already wrote.
    """
    report = NoteApplyReport()

    now = timezone.now()
    with transaction.atomic():
        for r in parsed_rows:
            if not can_write_payroll_note(viewer, r.employee, role, request=request):
                report.skipped_scope.append(RowError(
                    r.line_no, None, {"Employee_ID": r.employee_id, "Note": r.note},
                    "Outside your scope for this note type.",
                ))
                continue
            if not r.note:
                report.skipped_blank.append(RowError(
                    r.line_no, None, {"Employee_ID": r.employee_id, "Note": r.note},
                    "Blank — left unchanged.",
                ))
                continue
            obj, created = PayrollRunEmployeeNote.objects.update_or_create(
                run=run, employee=r.employee, role=role,
                defaults={"note": r.note, "updated_by": viewer, "updated_at": now},
            )
            if created:
                obj.added_by = viewer
                obj.added_at = now
                obj.save(update_fields=["added_by", "added_at"])
                report.created.append(r)
            else:
                report.updated.append(r)

    report.applied = True
    return report


def build_notes_xlsx_template(viewer, role: str) -> bytes:
    """Prefilled .xlsx template for the notes bulk uploader — one blank row
    per employee the viewer could write `role` notes for right now, mirroring
    build_salary_xlsx_template's one-row-per-employee prefill (notes, like
    gross salary, are plausibly wanted for most or all of the relevant
    population, unlike overtime's sparse blank-rows-only template).

    SUPERVISOR is scoped to the viewer's own managed scope (their reportees);
    HR/FINANCE write access is a flat group check independent of managed
    scope, so their template lists every active employee company-wide —
    matching how can_write_payroll_note itself treats the two cases.
    """
    if role == "SUPERVISOR":
        employees_qs = visible_employees(viewer, request=None).filter(is_active=True)
    else:
        employees_qs = User.objects.filter(is_active=True)
    employees = list(
        employees_qs.exclude(employee_id__isnull=True).exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )

    role_label = dict(PayrollRunEmployeeNote.Role.choices).get(role, role)
    wb = Workbook()
    ws = wb.active
    ws.title = f"{role_label} notes"

    headers = ["Employee ID", "Note", "Employee Name"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    row_idx = 2
    for emp in employees:
        eid_cell = ws.cell(row=row_idx, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        ws.cell(row=row_idx, column=3, value=escape_spreadsheet(emp.display_name))
        row_idx += 1

    widths = {"A": 14, "B": 60, "C": 24}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# SLA achievement bulk upload — flat 2-column layout
# (Employee ID | SLA month)
#
# Replaces the free-text Supervisor Note that carried this until now
# ("Meet SLA-August, 2026: Recommended for the variable part (3000 Taka)..."
# on 52 of Data Solutions' 379 rows in September 2026) — see
# docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase C.
#
# **Deliberately no "Met SLA" Y/N column** (dropped 2026-09-21) — a
# supervisor only ever lists the people who achieved it, so a row's mere
# presence in the file already means "met." A blank SLA month is still an
# untouched template line, same as every other uploader in this file.
#
# **The supervisor lists names; the system prices the row.** The 3,000 Tk
# figure is never typed into the sheet — it resolves from a
# PayRate(rate_type=SLA_INCENTIVE) row at apply time, scoped by the
# employee's department/team like every other rate, so it stays
# effective-dated and can differ per team later. What the supervisor is
# actually attesting to is the achievement, not the amount.
#
# **SLA month is per-row and is NOT the payment period.** "Meet SLA-August"
# is paid in the 16 Aug - 15 Sep run, and a missed month can be claimed
# retroactively alongside the current one (employee 3503 claimed both July
# and August in one September note). So the payment period is a form-level
# choice and the SLA month rides on VariablePayEntry.source_key
# ("SLA:2026-08"), which is what makes a retro claim distinguishable from a
# double-payment — the exact thing the note format could not express.
#
# Rows land PENDING and are approved by the employee's DEPARTMENT HEAD
# (accounts.permissions::can_act_on_variable_pay), not HR-Payroll. Overtime's
# Q55 gate is unchanged; the two split by input table. payroll/engine.py
# already filters variable pay to status=APPROVED, so a pending row cannot
# be paid by accident with no engine change.
# ---------------------------------------------------------------------------

SLA_SOURCE_PREFIX = "SLA"


@dataclass
class ParsedSlaRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    sla_month: date                  # first of the month the SLA was met in
    amount: Decimal | None = None    # resolved from PayRate at apply time
    rate_missing: bool = False

    @property
    def source_key(self) -> str:
        return f"{SLA_SOURCE_PREFIX}:{self.sla_month:%Y-%m}"

    @property
    def sla_month_label(self) -> str:
        return f"{self.sla_month:%B %Y}"


@dataclass
class SlaApplyReport:
    created: list[ParsedSlaRow] = field(default_factory=list)
    updated: list[ParsedSlaRow] = field(default_factory=list)
    skipped_scope: list[RowError] = field(default_factory=list)
    # No PayRate(SLA_INCENTIVE) resolves for this employee's department —
    # written as nothing rather than guessed at. A silent 0 would look like
    # a paid row on the register.
    skipped_no_rate: list[RowError] = field(default_factory=list)
    # Already APPROVED or REJECTED for this (employee, period, SLA month):
    # a re-upload must not quietly reopen a decided row.
    skipped_decided: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)

    @property
    def total_skipped(self) -> int:
        return len(self.skipped_scope) + len(self.skipped_no_rate) + len(self.skipped_decided)


def _parse_sla_month(raw, today: date) -> date:
    """Accept a real date cell, 'YYYY-MM', or a month name + year
    ('August 2026') — supervisors copy this out of their own notes, and the
    old free-text format wrote it as 'Meet SLA-August, 2026'.
    Always normalised to the first of that month.
    """
    if _is_blank(raw):
        raise ValueError("Empty SLA month.")
    if isinstance(raw, datetime):
        return raw.date().replace(day=1)
    if isinstance(raw, date):
        return raw.replace(day=1)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # An Excel serial date that came through unconverted.
        return from_excel(raw).date().replace(day=1)
    text = str(raw).strip()
    try:
        parsed = date_parser.parse(text, default=datetime(today.year, today.month, 1))
    except (ValueError, OverflowError):
        raise ValueError(f"Bad SLA month '{raw}' — use YYYY-MM, e.g. 2026-08.")
    month_start = parsed.date().replace(day=1)
    if month_start > today.replace(day=1):
        raise ValueError(f"SLA month '{raw}' is in the future.")
    return month_start


def parse_sla_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedSlaRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedSlaRow / RowError lists. Read-only.

    Two columns, header on row 1: Employee ID | SLA month. A row with a
    blank SLA month is an untouched template line and is skipped silently —
    the same convention every other uploader in this file uses — since a
    supervisor only ever lists the people who achieved it.

    Deliberately does NOT filter on is_active: an employee who resigned
    after earning the SLA month must still be payable, same reasoning as
    parse_upload and payroll.engine._population_queryset.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")

    body = rows[1:]  # row 0 is the header
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedSlaRow] = []
    seen: dict[tuple, int] = {}  # (employee_id, sla_month) -> first line_no

    for idx, row in enumerate(body, start=2):  # header is row 1
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "SLA_month": row[1] if len(row) > 1 else None,
        }
        raw_month = row[1] if len(row) > 1 else None
        if _is_blank(raw_month):
            continue  # untouched template line

        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        try:
            sla_month = _parse_sla_month(raw_month, today)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue

        key = (emp_id_str, sla_month)
        if key in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[key]} — this employee already claims {sla_month:%B %Y}.",
            ))
            continue
        seen[key] = idx

        parsed.append(ParsedSlaRow(
            line_no=idx, employee=employee, employee_id=emp_id_str, sla_month=sla_month,
        ))

    return parsed, parse_errors, dup_errors


def apply_sla_upload(
    viewer, parsed_rows: list[ParsedSlaRow], viewer_visible_pks: set,
    *, period, component, batch=None,
) -> SlaApplyReport:
    """Write parsed rows as PENDING VariablePayEntry rows for `period`.

    Pricing happens HERE, not in the sheet: each row's amount comes from
    ``payroll.rates.resolve_rate(SLA_INCENTIVE)`` for the employee's own
    department/team as at ``period.end_date`` — the same date basis the
    engine uses for every other rate. A row with no resolvable rate is
    skipped into ``skipped_no_rate`` rather than written as zero, because a
    0.00 row is indistinguishable from a paid one on the register.

    Idempotent on ``(employee, period, component, source_key)`` — re-uploading
    the same SLA month updates the amount instead of duplicating, mirroring
    VariablePayEntry's partial unique constraint (MySQL won't enforce it, so
    this is the real guard). An entry already APPROVED or REJECTED is left
    alone and reported in ``skipped_decided``: a re-upload must not silently
    reopen a decision, the same rule apply_upload applies to settled overtime
    in a locked period.

    Rows land PENDING regardless of who uploads — approval is always an
    explicit recorded act by the department head
    (accounts.permissions::can_act_on_variable_pay), never implied by the
    uploader's identity.
    """
    from payroll.rates import resolve_rate

    report = SlaApplyReport()

    in_scope: list[ParsedSlaRow] = []
    for row in parsed_rows:
        if row.employee.pk not in viewer_visible_pks:
            report.skipped_scope.append(RowError(
                row.line_no, None,
                {"Employee_ID": row.employee_id, "SLA_month": row.sla_month.isoformat()},
                "Outside your managed scope.",
            ))
            continue
        in_scope.append(row)

    if not in_scope:
        report.applied = True  # nothing to do, but the upload itself didn't error
        return report

    # One rate lookup per (department, team) pair, not per row — the
    # "~25 queries, not 25,000" discipline payroll/engine.py holds to.
    rate_cache: dict[tuple, object] = {}
    for row in in_scope:
        employee = row.employee
        cache_key = (employee.department_id, employee.team_id)
        if cache_key not in rate_cache:
            rate_cache[cache_key] = resolve_rate(
                rate_type=PayRate.RateType.SLA_INCENTIVE, on_date=period.end_date,
                department=employee.department, team=employee.team,
            )
        rate = rate_cache[cache_key]
        if rate is None:
            report.skipped_no_rate.append(RowError(
                row.line_no, None,
                {"Employee_ID": row.employee_id, "SLA_month": row.sla_month.isoformat()},
                "No SLA incentive rate configured for this employee's department.",
            ))
            continue
        row.amount = rate.amount

    priced = [r for r in in_scope if r.amount is not None]
    if not priced:
        report.applied = True
        return report

    existing = {
        (employee_id, source_key): (pk, status)
        for pk, employee_id, source_key, status in (
            VariablePayEntry.objects
            .filter(
                employee_id__in={r.employee.pk for r in priced}, period=period, component=component,
                source_key__in={r.source_key for r in priced},
            )
            .values_list("pk", "employee_id", "source_key", "status")
        )
    }

    now = timezone.now()
    with transaction.atomic():
        for row in priced:
            key = (row.employee.pk, row.source_key)
            current = existing.get(key)
            if current is not None and current[1] != VariablePayEntry.Status.PENDING:
                report.skipped_decided.append(RowError(
                    row.line_no, None,
                    {"Employee_ID": row.employee_id, "SLA_month": row.sla_month.isoformat()},
                    f"Already {current[1].lower()} for {row.sla_month_label} — ask the department head to change it.",
                ))
                continue
            reason = f"SLA achieved — {row.sla_month_label}"
            if current is not None:
                VariablePayEntry.objects.filter(pk=current[0]).update(
                    amount=row.amount, reason=reason, batch=batch,
                    updated_by=viewer, updated_at=now,
                )
                report.updated.append(row)
            else:
                VariablePayEntry.objects.create(
                    employee=row.employee, period=period, component=component,
                    amount=row.amount, reason=reason, source_key=row.source_key,
                    status=VariablePayEntry.Status.PENDING, batch=batch,
                    added_by=viewer, added_at=now, updated_by=viewer,
                )
                report.created.append(row)

    report.applied = True
    return report


def build_sla_xlsx_template(viewer, *, default_month: date) -> bytes:
    """Prefilled .xlsx template for the SLA uploader — one row per employee
    in the viewer's managed scope, ready to fill in.

    One-row-per-employee like build_salary_xlsx_template / the notes
    template rather than blank rows like the overtime one, so the supervisor
    doesn't have to look up IDs — but **the SLA month cell itself is left
    blank for every row, deliberately not pre-filled with `default_month`**.
    Since a blank month is what excludes a row, pre-filling it would mean an
    unedited download-then-upload pays every managed employee the SLA
    incentive by accident (the "Met SLA" Y/N column used to be the safety
    net for exactly this; removing it moved the safety net here instead).
    The supervisor types the month only for the few who actually qualified.

    There is deliberately no Amount column — see this section's header
    comment. The rate resolves at apply time.
    """
    employees = list(
        visible_employees(viewer, request=None)
        .filter(is_active=True, employee_id__isnull=False).exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "SLA achievement"
    headers = ["Employee ID", "SLA month", "Employee name", "Team"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    for i, emp in enumerate(employees, start=2):
        ws.cell(row=i, column=1, value=emp.employee_id).number_format = "@"
        ws.cell(row=i, column=2).number_format = "@"
        ws.cell(row=i, column=3, value=escape_spreadsheet(emp.display_name))
        ws.cell(row=i, column=4, value=escape_spreadsheet(emp.team.name if emp.team_id else ""))

    widths = {"A": 14, "B": 14, "C": 28, "D": 22}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Test Salary bulk upload — flat 2-column layout
# (Employee ID | Days)
#
# "Test Salary" is a **deduction**, not a pay category — confirmed with the
# business 2026-09-19, closing PAYROLL_PROCESS.md Q34, which had been open
# since 2026-08-13 asking about a "probationary daily rate for new hires".
# Every part of that premise was wrong, which is why it never resolved:
# September's 15 rows had 6 months' to 5 years' tenure, none were Trainees,
# and all already drew a full gross.
#
# What it actually is: Data Solutions employees carry a 6,000 Tk variable
# portion of their (already-stored) gross. A day under test forfeits one
# thirtieth of it — 200 Tk. "13 days Test Salary" means 13 x 200 = 2,600
# deducted. See docs/DSDD_PAYROLL_AUTOMATION_PLAN.md Phase D.
#
# **Revised 2026-09-21 — day count is entered directly, not derived from a
# date window.** The original build read a From/To window per row and
# derived the day count from it, on the theory that a hand-typed count could
# be wrong (it was, once, in September's data) and that a window was needed
# to detect two submissions double-charging the same day. In practice an
# employee can fall under test across several disjoint stretches of a single
# month, so a single From/To window couldn't represent a month's figure
# anyway — supervisors were going to need to hand-sum multiple windows into
# one number regardless. This drops the window and takes that number
# directly:
#
# * **One row per employee per period.** There's no "which window" to keep
#   separate any more — the number already is the period's total, however
#   many disjoint stretches it covers. A second upload for the same
#   employee/period *replaces* the day count rather than adding to it (same
#   `source_key` idempotency as the SLA uploader, just keyed on
#   employee+period rather than a date range).
# * **The number is trusted as entered.** There's no date range left to check
#   against the roster, so the automatic already-unpaid-day (LWP) deduction
#   that the original window-based build had is gone — whoever fills in the
#   sheet is expected to have already netted out any unpaid days.
# * **Capped at the whole variable portion**, 30 x the daily rate, per
#   employee per period. Derived from the rate rather than stored
#   separately, because 200 IS 6,000/30 — the two move together by
#   definition.
# * **Trainees never carry it.** They are paid a flat 11,000 until go-live,
#   so a trainee row is a mistake and is rejected at entry.
# * DSDD only: the rate is seeded department-scoped with no global fallback,
#   so another department surfaces as "no rate configured" rather than a
#   silent wrong deduction.
#
# The amount is computed at apply time, like the SLA uploader's. Re-upload
# the same employee/period to correct a day count — it is idempotent on
# source_key and recomputes the figure.
# ---------------------------------------------------------------------------

TEST_SALARY_SOURCE_KEY = "TEST"
TEST_SALARY_PORTION_DAYS = 30  # the variable portion is 30 x the daily rate
TEST_SALARY_MAX_DAYS = 31      # sanity bound — a period is at most one month long


@dataclass
class ParsedTestSalaryRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    days: int
    amount: Decimal | None = None    # priced at apply time
    capped: bool = False


@dataclass
class TestSalaryApplyReport:
    created: list[ParsedTestSalaryRow] = field(default_factory=list)
    updated: list[ParsedTestSalaryRow] = field(default_factory=list)
    skipped_scope: list[RowError] = field(default_factory=list)
    skipped_no_rate: list[RowError] = field(default_factory=list)
    # Trainees are on a flat package and never carry this deduction.
    skipped_trainee: list[RowError] = field(default_factory=list)
    skipped_decided: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)

    @property
    def total_skipped(self) -> int:
        return (
            len(self.skipped_scope) + len(self.skipped_no_rate)
            + len(self.skipped_trainee) + len(self.skipped_decided)
        )


def _coerce_day_count(value) -> int:
    """Accept an int, a whole-number float, or numeric text."""
    if value is None:
        raise ValueError("Empty day count.")
    if isinstance(value, bool):
        raise ValueError(f"Bad day count: {value!r}.")
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Day count '{value}' must be a whole number.")
        n = int(value)
    else:
        s = str(value).strip()
        if not s:
            raise ValueError("Empty day count.")
        try:
            n = int(s)
        except ValueError:
            raise ValueError(f"Bad day count '{s}' — expected a whole number.")
    if not (1 <= n <= TEST_SALARY_MAX_DAYS):
        raise ValueError(f"Day count must be between 1 and {TEST_SALARY_MAX_DAYS}.")
    return n


def parse_test_salary_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedTestSalaryRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedTestSalaryRow / RowError lists. Read-only.

    Two columns, header on row 1: Employee ID | Days — the total number of
    test-salary days to charge this employee for the period, however many
    separate stretches they actually fell across. A row with a blank Days
    cell is treated as an untouched template line and skipped silently, the
    same way the SLA uploader treats an unticked row.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedTestSalaryRow] = []
    seen: dict[str, int] = {}

    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Days": row[1] if len(row) > 1 else None,
        }
        raw_days = row[1] if len(row) > 1 else None
        if _is_blank(raw_days):
            continue  # untouched template line

        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        try:
            days = _coerce_day_count(raw_days)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue

        if emp_id_str in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[emp_id_str]} — {emp_id_str} already appears in this file.",
            ))
            continue
        seen[emp_id_str] = idx

        parsed.append(ParsedTestSalaryRow(
            line_no=idx, employee=employee, employee_id=emp_id_str, days=days,
        ))

    return parsed, parse_errors, dup_errors


def apply_test_salary_upload(
    viewer, parsed_rows: list[ParsedTestSalaryRow], viewer_visible_pks: set,
    *, period, component, batch=None,
) -> TestSalaryApplyReport:
    """Write parsed day-counts as PENDING VariablePayEntry deductions for `period`.

    Pricing, per row:

        amount = min(days x daily_rate, cap)
        cap    = 30 x daily_rate per employee per period

    One row per employee per period (``source_key`` is a constant, not
    derived from the row) — a re-upload replaces that employee's day count
    and re-prices it rather than adding another row.

    Idempotent on ``(employee, period, component, source_key)`` like the SLA
    uploader; an entry already APPROVED or REJECTED is left alone.
    """
    from payroll.rates import resolve_rate

    report = TestSalaryApplyReport()

    in_scope: list[ParsedTestSalaryRow] = []
    for row in parsed_rows:
        raw = {"Employee_ID": row.employee_id, "Days": row.days}
        if row.employee.pk not in viewer_visible_pks:
            report.skipped_scope.append(RowError(row.line_no, None, raw, "Outside your managed scope."))
            continue
        in_scope.append(row)

    if not in_scope:
        report.applied = True
        return report

    emp_pks = {r.employee.pk for r in in_scope}
    trainee_pks = set(
        JobInfo.objects
        .filter(employee_id__in=emp_pks, resource_status=JobInfo.ResourceStatus.TRAINEE)
        .values_list("employee_id", flat=True)
    )

    existing = {
        e[1]: e for e in VariablePayEntry.objects
        .filter(
            employee_id__in=emp_pks, period=period, component=component,
            source_key=TEST_SALARY_SOURCE_KEY,
        )
        .values_list("pk", "employee_id", "status")
    }

    rate_cache: dict = {}
    now = timezone.now()

    with transaction.atomic():
        for row in in_scope:
            raw = {"Employee_ID": row.employee_id, "Days": row.days}
            employee = row.employee

            if employee.pk in trainee_pks:
                report.skipped_trainee.append(RowError(
                    row.line_no, "A", raw,
                    "Trainees are on a flat package and never carry a test-salary deduction.",
                ))
                continue

            cache_key = (employee.department_id, employee.team_id)
            if cache_key not in rate_cache:
                rate_cache[cache_key] = resolve_rate(
                    rate_type=PayRate.RateType.TEST_SALARY_DAY, on_date=period.end_date,
                    department=employee.department, team=employee.team,
                )
            rate = rate_cache[cache_key]
            if rate is None:
                report.skipped_no_rate.append(RowError(
                    row.line_no, None, raw,
                    "No test-salary rate configured for this employee's department.",
                ))
                continue

            current = existing.get(employee.pk)
            if current is not None and current[2] != VariablePayEntry.Status.PENDING:
                report.skipped_decided.append(RowError(
                    row.line_no, None, raw,
                    f"Already {current[2].lower()} for this period — "
                    "ask the department head to change it.",
                ))
                continue

            cap = rate.amount * TEST_SALARY_PORTION_DAYS
            uncapped = (rate.amount * row.days).quantize(Decimal("0.01"))
            amount = min(uncapped, cap)

            row.amount = amount
            row.capped = amount < uncapped

            reason = (
                f"Test salary: {row.days} day(s) @ {rate.amount:g}"
                + (f", capped at {cap:g}" if row.capped else "")
            )

            if current is not None:
                VariablePayEntry.objects.filter(pk=current[0]).update(
                    amount=amount, reason=reason[:255], days=row.days, batch=batch,
                    updated_by=viewer, updated_at=now,
                )
                report.updated.append(row)
            else:
                VariablePayEntry.objects.create(
                    employee=employee, period=period, component=component,
                    amount=amount, reason=reason[:255], source_key=TEST_SALARY_SOURCE_KEY,
                    days=row.days,
                    status=VariablePayEntry.Status.PENDING, batch=batch,
                    added_by=viewer, added_at=now, updated_by=viewer,
                )
                report.created.append(row)
                existing[employee.pk] = (None, employee.pk, VariablePayEntry.Status.PENDING)

    report.applied = True
    return report


def build_test_salary_xlsx_template(viewer) -> bytes:
    """Blank .xlsx template for the Test Salary uploader.

    Blank rows rather than one per employee (the SLA template's shape):
    a test period is sparse — most employees never have one — so a
    prefilled roster would be almost entirely empty. A reference sheet
    lists the viewer's managed employees instead, mirroring
    build_overtime_xlsx_template.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Test salary"
    for col, label in enumerate(["Employee ID", "Days"], start=1):
        ws.cell(row=1, column=col, value=label)

    blank_rows = 40
    last_row = 1 + blank_rows
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = "@"
        ws.cell(row=row, column=2).number_format = "0"

    days_dv = DataValidation(
        type="whole", operator="between", formula1=1, formula2=TEST_SALARY_MAX_DAYS, allow_blank=True,
    )
    days_dv.error = f"Enter a whole number of days from 1 to {TEST_SALARY_MAX_DAYS}."
    days_dv.errorTitle = "Invalid day count"
    ws.add_data_validation(days_dv)
    days_dv.add(f"B2:B{last_row}")

    employees = list(
        visible_employees(viewer, request=None)
        .filter(is_active=True, employee_id__isnull=False).exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )
    ref = wb.create_sheet("Employees (reference)")
    ref.cell(row=1, column=1, value="Employee ID")
    ref.cell(row=1, column=2, value="Employee name")
    for i, emp in enumerate(employees, start=2):
        ref.cell(row=i, column=1, value=emp.employee_id).number_format = "@"
        ref.cell(row=i, column=2, value=escape_spreadsheet(emp.display_name))

    for col, width in {"A": 14, "B": 10}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Go Live bulk upload — flat 3-column layout
# (Employee ID | Team | Go-live Date)
#
# Added 2026-09-21. Before this, a Data Solutions trainee's go-live (Trainee
# -> MARS/Item Review/any other team, 11,000 -> 18,000 or 16,000) was recorded
# nowhere but a free-text Supervisor Note ("Go live date, Salary as per the
# new package for MARS (TK. 18,000): Date: September 07, 2026") — the exact
# gap employees.TeamAssignment + payroll.TeamSalaryPackage (2026-09-19) exist
# to close, but only from the HR profile-edit drawer, one employee at a time.
#
# This is a thin bulk front end for that same machinery, not a parallel
# implementation:
#
# * **The amount is never typed.** It's resolved from TeamSalaryPackage —
#   MARS is 18,000, every other team is 16,000 (confirmed 2026-09-21,
#   see docs/PAYROLL_PLAN.md) — the same source the engine's
#   SALARY_PACKAGE_MISMATCH check reads, so an upload can never propose a
#   figure that disagrees with the seeded policy.
# * **Trainee -> Live only.** An employee not currently resource_status
#   TRAINEE is rejected — this uploader is the specific go-live transition,
#   not a general team-transfer tool. A later correction (wrong team, wrong
#   date) goes through the employee's profile or Django admin, same as any
#   other TeamAssignment correction today.
# * **The org record and the salary proposal are two different guarantees.**
#   CustomUser.team / JobInfo.resource_status / TeamAssignment are updated
#   unconditionally (bypassing the ordinary post_save signal so the row lands
#   on the note's real go-live date, not the upload date — see
#   employees.signals.sync_team_assignment's `effective_from` kwarg).
#   propose_salary_for_assignment is then asked to price it, but returns None
#   (no proposal) when the employee has no current APPROVED SalaryStructure
#   to revise at all — a brand-new joiner's starting salary comes from the
#   compensation master, not from here. The report distinguishes the two so
#   a missing proposal is visible, not silent.
# * **Re-uploading the same row is a safe no-op**, not a duplicate: once an
#   employee is Live, the not-TRAINEE gate skips them on any later upload —
#   the same protection that makes a correction attempt bounce to the
#   profile drawer instead.
# ---------------------------------------------------------------------------


@dataclass
class ParsedGoLiveRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    new_team: Team
    go_live_date: date
    package_amount: Decimal | None = None    # resolved at apply time
    proposal_created: bool | None = None     # None until apply time


@dataclass
class GoLiveApplyReport:
    applied: list[ParsedGoLiveRow] = field(default_factory=list)
    skipped_scope: list[RowError] = field(default_factory=list)
    # Not currently a Trainee — either never was, or already went live
    # (including via an earlier run of this same uploader).
    skipped_not_trainee: list[RowError] = field(default_factory=list)
    skipped_no_package: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    ran: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_applied(self) -> int:
        return len(self.applied)

    @property
    def total_skipped(self) -> int:
        return len(self.skipped_scope) + len(self.skipped_not_trainee) + len(self.skipped_no_package)


def parse_go_live_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedGoLiveRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedGoLiveRow / RowError lists. Read-only.

    Three columns, header on row 1: Employee ID | Team | Go-live Date
    ("Team" rather than "New Team" — 2026-09-21, to read plainly as "the
    team they're going live to," not as a rename of an existing field). A
    row with a blank Team is treated as an untouched template line and
    skipped silently, the same way every other uploader in this file treats
    an unticked row.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    employees_by_id = {
        u.employee_id: u for u in
        User.objects.filter(employee_id__in=emp_ids).select_related("department", "designation")
    }

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedGoLiveRow] = []
    seen: dict[str, int] = {}

    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Team": row[1] if len(row) > 1 else None,
            "Go_live_Date": row[2] if len(row) > 2 else None,
        }
        raw_team = row[1] if len(row) > 1 else None
        if _is_blank(raw_team):
            continue  # untouched template line

        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue

        team_name = str(raw_team).strip()
        new_team = Team.objects.filter(name=team_name, department=employee.department).first()
        if new_team is None:
            dept_label = employee.department.name if employee.department else "no department on file"
            parse_errors.append(RowError(
                idx, "B", raw_snapshot,
                f"No team '{team_name}' under {dept_label} — check spelling.",
            ))
            continue

        raw_date = row[2] if len(row) > 2 else None
        try:
            go_live_date = _parse_work_date(raw_date, today)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "C", raw_snapshot, str(exc)))
            continue
        if go_live_date > today:
            parse_errors.append(RowError(idx, "C", raw_snapshot, "Go-live date is in the future."))
            continue
        if employee.joining_date and go_live_date < employee.joining_date:
            parse_errors.append(RowError(
                idx, "C", raw_snapshot, "Go-live date is before the employee's joining date.",
            ))
            continue

        if emp_id_str in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[emp_id_str]} — {emp_id_str} already appears in this file.",
            ))
            continue
        seen[emp_id_str] = idx

        parsed.append(ParsedGoLiveRow(
            line_no=idx, employee=employee, employee_id=emp_id_str,
            new_team=new_team, go_live_date=go_live_date,
        ))

    return parsed, parse_errors, dup_errors


def apply_go_live_upload(
    viewer, parsed_rows: list[ParsedGoLiveRow], viewer_visible_pks: set,
) -> GoLiveApplyReport:
    """Write parsed go-lives: update the org record and ask payroll to price it.

    Per row, inside one transaction:

        1. CustomUser.team and JobInfo.resource_status are updated directly
           (queryset .update(), bypassing the ordinary post_save signal) so
           the change doesn't get dated "today" before we can correct it.
        2. employees.signals.sync_team_assignment(employee, reason=GO_LIVE,
           effective_from=go_live_date) closes the Trainee-team row at
           go_live_date - 1 day and opens the new one AT go_live_date, then
           fires the existing propose_salary_for_assignment with that same
           (correct) date.

    Skipped, never partially applied: an employee no longer TRAINEE (already
    live, or never was), or a team with no TeamSalaryPackage configured for
    the go-live date.
    """
    report = GoLiveApplyReport()

    in_scope: list[ParsedGoLiveRow] = []
    for row in parsed_rows:
        raw = {
            "Employee_ID": row.employee_id, "Team": row.new_team.name,
            "Go_live_Date": row.go_live_date.isoformat(),
        }
        if row.employee.pk not in viewer_visible_pks:
            report.skipped_scope.append(RowError(row.line_no, None, raw, "Outside your managed scope."))
            continue
        in_scope.append(row)

    if not in_scope:
        report.ran = True
        return report

    status_by_employee = dict(
        JobInfo.objects.filter(employee_id__in=[r.employee.pk for r in in_scope])
        .values_list("employee_id", "resource_status")
    )

    with transaction.atomic():
        for row in in_scope:
            raw = {
                "Employee_ID": row.employee_id, "Team": row.new_team.name,
                "Go_live_Date": row.go_live_date.isoformat(),
            }
            employee = row.employee
            current_status = status_by_employee.get(employee.pk) or ""

            if current_status != JobInfo.ResourceStatus.TRAINEE:
                display = (
                    JobInfo.ResourceStatus(current_status).label if current_status else "no status on file"
                )
                report.skipped_not_trainee.append(RowError(
                    row.line_no, None, raw,
                    f"Current status is {display}, not Trainee — this uploader only handles "
                    "the Trainee-to-Live transition. Use the employee's profile for a correction.",
                ))
                continue

            package = resolve_team_salary_package(
                on_date=row.go_live_date, department=employee.department, team=row.new_team,
                designation=employee.designation, resource_status=JobInfo.ResourceStatus.LIVE,
            )
            if package is None:
                report.skipped_no_package.append(RowError(
                    row.line_no, "B", raw,
                    f"No salary package configured for {row.new_team.name} — ask Finance to add one.",
                ))
                continue

            # Mutate the in-memory object too, not just the DB — sync_team_
            # assignment reads employee.team_id directly (no re-query), only
            # JobInfo's resource_status is freshly queried inside it.
            employee.team = row.new_team
            User.objects.filter(pk=employee.pk).update(team=row.new_team, **directory_stamp("team"))
            JobInfo.objects.filter(employee_id=employee.pk).update(
                resource_status=JobInfo.ResourceStatus.LIVE,
            )
            assignment = sync_team_assignment(
                employee, reason=TeamAssignment.Reason.GO_LIVE, effective_from=row.go_live_date,
            )
            proposal = propose_salary_for_assignment(assignment, actor=viewer)

            row.package_amount = package.gross_monthly
            row.proposal_created = proposal is not None
            report.applied.append(row)

    report.ran = True
    return report


def build_go_live_xlsx_template(viewer) -> bytes:
    """Blank .xlsx template for the Go Live uploader.

    The reference sheet lists only the viewer's managed Trainee-status
    employees — the only people this uploader can ever apply to — rather
    than every managed employee, since a non-trainee row is always rejected.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Go live"
    for col, label in enumerate(["Employee ID", "Team", "Go-live Date"], start=1):
        ws.cell(row=1, column=col, value=label)

    blank_rows = 40
    last_row = 1 + blank_rows
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = "@"
        ws.cell(row=row, column=3).number_format = "yyyy-mm-dd"

    date_dv = DataValidation(type="date", allow_blank=True)
    date_dv.error = "Enter a valid date."
    date_dv.errorTitle = "Invalid date"
    ws.add_data_validation(date_dv)
    date_dv.add(f"C2:C{last_row}")

    employees = list(
        visible_employees(viewer, request=None)
        .filter(is_active=True, employee_id__isnull=False, job_info__resource_status=JobInfo.ResourceStatus.TRAINEE)
        .exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
        .select_related("department")
    )
    ref = wb.create_sheet("Trainees (reference)")
    ref.cell(row=1, column=1, value="Employee ID")
    ref.cell(row=1, column=2, value="Employee name")
    ref.cell(row=1, column=3, value="Department")
    for i, emp in enumerate(employees, start=2):
        ref.cell(row=i, column=1, value=emp.employee_id).number_format = "@"
        ref.cell(row=i, column=2, value=escape_spreadsheet(emp.display_name))
        ref.cell(row=i, column=3, value=emp.department.name if emp.department else "")

    for col, width in {"A": 14, "B": 16, "C": 16}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Tax-year openings (cutover) — Employee ID | TDS deducted to date | Additional pay to date
#
# docs/TAX_CALCULATION_TRUEUP_PLAN.md §6. The year-to-date true-up needs, for
# each employee, what was already deducted (and what taxable extra pay was
# received) in the months of the tax year BEFORE Orbis started computing TDS
# (TaxYear.orbis_live_from). Finance has those figures per employee in their
# sheet. One row per employee; the additional-pay column is optional (Q4:
# Finance may only be able to supply the TDS figure), blank = 0. Re-upload
# overwrites — these are facts, not a ledger. Payroll Admins only (the file is
# per-employee tax), same ProtectedView gate as the gross-salary uploads.
# ---------------------------------------------------------------------------

@dataclass
class ParsedTaxOpeningRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    tds_deducted: Decimal
    additional_pay: Decimal


@dataclass
class TaxOpeningApplyReport:
    updated: list[ParsedTaxOpeningRow] = field(default_factory=list)
    created_declarations: int = 0
    parse_errors: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return len(self.updated)


def _parse_money_or_zero(raw, label: str) -> Decimal:
    if _is_blank(raw):
        return Decimal("0")
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, got {raw!r}.")
    if value < 0:
        raise ValueError(f"{label} can't be negative.")
    return value.quantize(Decimal("0.01"))


def parse_tax_opening_upload(file_obj, filename: str) -> tuple[list[ParsedTaxOpeningRow], list[RowError]]:
    """Read-only. Header on row 1; columns A–C are read, anything after is
    reference. A duplicate Employee ID is an error (which row would win?)."""
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
    # Separated employees included — they still had TDS deducted this year.
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    parsed: list[ParsedTaxOpeningRow] = []
    parse_errors: list[RowError] = []
    seen: dict[str, int] = {}
    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "TDS_deducted": row[1] if len(row) > 1 else None,
            "Additional_pay": row[2] if len(row) > 2 else None,
        }
        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        if emp_id_str not in employees_by_id:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        if emp_id_str in seen:
            parse_errors.append(RowError(
                idx, "A", raw_snapshot, f"Employee ID '{emp_id_str}' also appears on row {seen[emp_id_str]} — one row per employee.",
            ))
            continue
        seen[emp_id_str] = idx
        try:
            tds = _parse_money_or_zero(row[1] if len(row) > 1 else None, "TDS deducted to date")
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue
        try:
            extra = _parse_money_or_zero(row[2] if len(row) > 2 else None, "Additional pay to date")
        except ValueError as exc:
            parse_errors.append(RowError(idx, "C", raw_snapshot, str(exc)))
            continue
        parsed.append(ParsedTaxOpeningRow(idx, employees_by_id[emp_id_str], emp_id_str, tds, extra))
    return parsed, parse_errors


def apply_tax_opening_upload(viewer, parsed: list[ParsedTaxOpeningRow], *, tax_year, batch=None) -> TaxOpeningApplyReport:
    """Write the two opening fields on each employee's declaration for
    ``tax_year``, creating the declaration (with its allowable-investment
    snapshot, via save()) when the employee has none yet. Overwrites."""
    from payroll.models import EmployeeTaxDeclaration

    report = TaxOpeningApplyReport()
    if not parsed:
        report.applied = True
        return report
    existing = {
        d.employee_id: d for d in
        EmployeeTaxDeclaration.objects.filter(tax_year=tax_year, employee_id__in=[r.employee.pk for r in parsed])
    }
    with transaction.atomic():
        for r in parsed:
            declaration = existing.get(r.employee.pk)
            if declaration is None:
                declaration = EmployeeTaxDeclaration(employee=r.employee, tax_year=tax_year, added_by=viewer)
                report.created_declarations += 1
            declaration.opening_tds_deducted = r.tds_deducted
            declaration.opening_additional_pay = r.additional_pay
            declaration.updated_by = viewer
            declaration.save()
            report.updated.append(r)
    report.applied = True
    return report


def build_tax_opening_xlsx_template(tax_year) -> bytes:
    """One row per active employee: Employee ID, blank TDS / extra pay, then
    reference Name. Finance fills the two amounts from their sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = f"Tax openings {tax_year.code}"
    headers = ["Employee ID", "TDS deducted to date", "Additional pay to date (optional)", "Employee Name"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)
    employees = (
        User.objects.filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )
    row_idx = 2
    for emp in employees:
        eid_cell = ws.cell(row=row_idx, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        ws.cell(row=row_idx, column=2).number_format = EXCEL_NUMBER_FORMAT
        ws.cell(row=row_idx, column=3).number_format = EXCEL_NUMBER_FORMAT
        ws.cell(row=row_idx, column=4, value=escape_spreadsheet(emp.get_full_name()))
        row_idx += 1
    for col, width in {"A": 14, "B": 22, "C": 30, "D": 28}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Salary loans (cutover) — docs/SALARY_LOAN_PLAN.md §5.5
#
# Employee ID | File reference | Approved on | Principal | Instalments |
# Instalment amount (optional) | Disbursed on | First deduction month
# (optional, YYYY-MM) | Recovered before Orbis | Note (optional)
#
# For the ~100 loans live at go-live (Q72). Rows land ACTIVE with
# recorded_source=OUTSIDE_ORBIS (document optional). A file reference already
# in Orbis is CORRECTED — opening and note only — never duplicated, and never
# re-termed once an Orbis instalment exists. Payroll Admins only.
# ---------------------------------------------------------------------------

@dataclass
class ParsedSalaryLoanRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    file_reference: str
    approved_on: date
    principal: Decimal
    installment_count: int
    installment_amount: Decimal
    disbursed_on: date
    first_deduction: tuple[int, int]  # (year, month)
    opening_recovered: Decimal
    note: str
    existing: object = None          # SalaryLoan, when the reference is already in Orbis


@dataclass
class SalaryLoanApplyReport:
    created: list[ParsedSalaryLoanRow] = field(default_factory=list)
    updated: list[ParsedSalaryLoanRow] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)


def _parse_positive_money(raw, label: str) -> Decimal:
    value = _parse_money_or_zero(raw, label)
    if value <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return value


def _parse_month(raw, label: str = "First deduction month") -> tuple[int, int]:
    """'2026-10' / '2026/10' / an Excel date → (year, month) of the payroll
    period named for that month."""
    if isinstance(raw, datetime):
        return raw.year, raw.month
    if isinstance(raw, date):
        return raw.year, raw.month
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        d = from_excel(raw)
        return d.year, d.month
    text = str(raw).strip().replace("/", "-")
    parts = text.split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        year, month = int(parts[0]), int(parts[1])
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            return year, month
    raise ValueError(f"{label} must be YYYY-MM, got {raw!r}.")


def parse_salary_loan_upload(file_obj, filename: str) -> tuple[list[ParsedSalaryLoanRow], list[RowError]]:
    from payroll.models import SalaryLoan

    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")

    emp_ids: set[str] = set()
    refs: set[str] = set()
    for row in body:
        if len(row) >= 1 and not _is_blank(row[0]):
            try:
                emp_ids.add(_coerce_employee_id(row[0]))
            except ValueError:
                continue
        if len(row) >= 2 and not _is_blank(row[1]):
            refs.add(str(row[1]).strip())
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}
    existing_by_ref = {l.file_reference: l for l in SalaryLoan.objects.filter(file_reference__in=refs)}

    cols = ("Employee_ID", "File_reference", "Approved_on", "Principal", "Instalments",
            "Instalment_amount", "Disbursed_on", "First_deduction_month", "Recovered_before_Orbis", "Note")
    parsed: list[ParsedSalaryLoanRow] = []
    parse_errors: list[RowError] = []
    seen_refs: dict[str, int] = {}
    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        cell = lambda i: row[i] if len(row) > i else None  # noqa: E731
        raw_snapshot = {name: cell(i) for i, name in enumerate(cols)}
        try:
            emp_id_str = _coerce_employee_id(cell(0))
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        ref = str(cell(1) or "").strip()
        if not ref:
            parse_errors.append(RowError(idx, "B", raw_snapshot, "File reference is required."))
            continue
        if ref in seen_refs:
            parse_errors.append(RowError(idx, "B", raw_snapshot, f"File reference '{ref}' also appears on row {seen_refs[ref]}."))
            continue
        seen_refs[ref] = idx
        existing = existing_by_ref.get(ref)
        if existing is not None and existing.employee_id != employee.pk:
            parse_errors.append(RowError(
                idx, "B", raw_snapshot, f"File reference '{ref}' already belongs to a different employee in Orbis.",
            ))
            continue
        try:
            approved_on = _parse_any_date(cell(2))
        except ValueError as exc:
            parse_errors.append(RowError(idx, "C", raw_snapshot, str(exc)))
            continue
        try:
            principal = _parse_positive_money(cell(3), "Principal")
        except ValueError as exc:
            parse_errors.append(RowError(idx, "D", raw_snapshot, str(exc)))
            continue
        try:
            count = int(Decimal(str(cell(4)).strip()))
            if count < 1:
                raise ValueError
        except (ValueError, InvalidOperation, TypeError):
            parse_errors.append(RowError(idx, "E", raw_snapshot, "Instalments must be a whole number of at least 1."))
            continue
        try:
            if _is_blank(cell(5)):
                installment = SalaryLoan.default_installment(principal, count)
            else:
                installment = _parse_positive_money(cell(5), "Instalment amount")
            if installment > principal:
                raise ValueError("Instalment amount can't exceed the principal.")
        except ValueError as exc:
            parse_errors.append(RowError(idx, "F", raw_snapshot, str(exc)))
            continue
        try:
            disbursed_on = _parse_any_date(cell(6))
        except ValueError as exc:
            parse_errors.append(RowError(idx, "G", raw_snapshot, str(exc)))
            continue
        try:
            first = _parse_month(cell(7)) if not _is_blank(cell(7)) else first_loan_deduction_month(disbursed_on)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "H", raw_snapshot, str(exc)))
            continue
        try:
            opening = _parse_money_or_zero(cell(8), "Recovered before Orbis")
            if opening > principal:
                raise ValueError("Recovered before Orbis can't exceed the principal.")
        except ValueError as exc:
            parse_errors.append(RowError(idx, "I", raw_snapshot, str(exc)))
            continue
        note = "" if _is_blank(cell(9)) else str(cell(9)).strip()[:2000]
        parsed.append(ParsedSalaryLoanRow(
            idx, employee, emp_id_str, ref, approved_on, principal, count, installment,
            disbursed_on, first, opening, note, existing,
        ))
    return parsed, parse_errors


def apply_salary_loan_upload(viewer, parsed: list[ParsedSalaryLoanRow], *, batch=None) -> SalaryLoanApplyReport:
    """Create each new loan ACTIVE / OUTSIDE_ORBIS; for a reference already in
    Orbis, correct the opening and note only."""
    from payroll.models import PayrollPeriod, SalaryLoan

    report = SalaryLoanApplyReport()
    if not parsed:
        report.applied = True
        return report
    with transaction.atomic():
        for r in parsed:
            if r.existing is not None:
                loan = r.existing
                loan.opening_recovered = r.opening_recovered
                if r.note:
                    loan.note = r.note
                loan.updated_by = viewer
                loan.save(update_fields=["opening_recovered", "note", "updated_by", "updated_at"])
                report.updated.append(r)
                continue
            loan = SalaryLoan(
                employee=r.employee, file_reference=r.file_reference, approved_on=r.approved_on,
                recorded_source=SalaryLoan.RecordedSource.OUTSIDE_ORBIS,
                principal=r.principal, installment_count=r.installment_count,
                installment_amount=r.installment_amount,
                first_deduction_period=PayrollPeriod.for_month(*r.first_deduction),
                opening_recovered=r.opening_recovered, status=SalaryLoan.Status.ACTIVE,
                disbursed_on=r.disbursed_on, disbursed_by=viewer,
                disbursement_note="Cutover upload", note=r.note,
                added_by=viewer, updated_by=viewer,
            )
            loan.full_clean()
            loan.save()
            report.created.append(r)
    report.applied = True
    return report


def build_salary_loan_xlsx_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Salary loans"
    headers = [
        "Employee ID", "File reference", "Approved on", "Principal", "Instalments",
        "Instalment amount (optional)", "Disbursed on", "First deduction month (optional, YYYY-MM)",
        "Recovered before Orbis", "Note (optional)",
    ]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)
    example = ["104", "HR/21092026/01", "2026-09-21", 50000, 10, 5000, "2026-09-25", "2026-10", 5000,
               "Medical expenses — birth of child"]
    for col, value in enumerate(example, start=1):
        c = ws.cell(row=2, column=col, value=value)
        if col == 1:
            c.number_format = "@"
        if col in (4, 6, 9):
            c.number_format = EXCEL_NUMBER_FORMAT
    for col, width in {"A": 14, "B": 20, "C": 14, "D": 14, "E": 12, "F": 24, "G": 14, "H": 36, "I": 22, "J": 40}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Tax challans — docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §3 (permanent uploader)
#
# Challan No | Date | Bank | Total Challan Amount | Payroll month (optional) |
# Note. One row per treasury deposit of TDS, company-level. The tax year is
# the one the deposit date falls in. "Payroll month" links the challan to
# that month's CALCULATED regular run when one exists (an Orbis month);
# for a pre-Orbis month it is just recorded. Re-upload corrects a challan
# by number — the run link is never cleared from here.
# ---------------------------------------------------------------------------

@dataclass
class ParsedChallanRow:
    line_no: int
    challan_number: str
    deposit_date: date
    bank: object                     # Bank
    total_amount: Decimal
    tax_year: object                 # TaxYear
    note: str = ""
    period_year: int | None = None
    period_month: int | None = None
    run: object = None               # PayrollRun or None

    @property
    def month_label(self) -> str:
        return f"{self.period_year}-{self.period_month:02d}" if self.period_year else ""


@dataclass
class TaxChallanParse:
    challans: list[ParsedChallanRow] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)


@dataclass
class TaxChallanApplyReport:
    created: int = 0
    updated: int = 0
    linked_to_runs: int = 0
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return self.created + self.updated


def _bank_key(text) -> str:
    """'BankAsia PLC' / 'Bank Asia PLC' / 'Modhumoti Bank Limited' / 'MDB'
    all resolve: lowercase, alphanumerics only, legal suffixes dropped."""
    k = "".join(ch for ch in str(text or "").lower() if ch.isalnum())
    for suffix in ("plc", "limited", "ltd"):
        if k.endswith(suffix):
            k = k[: -len(suffix)]
    return k


def _bank_lookup() -> tuple[list, dict]:
    banks = list(Bank.objects.filter(is_active=True))
    by_key: dict[str, Bank] = {}
    for b in banks:
        by_key.setdefault(_bank_key(b.name), b)
        if b.short_code:
            by_key.setdefault(_bank_key(b.short_code), b)
    return banks, by_key


def parse_tax_challan_upload(file_obj, filename: str) -> TaxChallanParse:
    """Read-only. Header on row 1, positional columns A–F."""
    from payroll.tax_ledger import (
        CHALLAN_HEADERS, is_legacy_month, month_label, orbis_run_for_month, tax_year_for_date, tax_year_months,
    )

    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")
    banks, bank_by_key = _bank_lookup()
    result = TaxChallanParse()
    seen: dict[str, int] = {}
    unlinked_months: list[str] = []
    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {"Challan_No": row[0] if len(row) > 0 else None, "Date": row[1] if len(row) > 1 else None}
        number = str(row[0]).strip() if len(row) > 0 and not _is_blank(row[0]) else ""
        if not number:
            result.parse_errors.append(RowError(idx, "A", raw_snapshot, "Challan No is required."))
            continue
        if number in seen:
            result.parse_errors.append(RowError(idx, "A", raw_snapshot, f"Challan '{number}' also appears on row {seen[number]}."))
            continue
        seen[number] = idx
        try:
            deposit_date = _parse_any_date(row[1] if len(row) > 1 else None)
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, "B", raw_snapshot, f"Date: {exc}"))
            continue
        tax_year = tax_year_for_date(deposit_date)
        if tax_year is None:
            result.parse_errors.append(RowError(idx, "B", raw_snapshot, f"No tax year covers {deposit_date:%Y-%m-%d} — seed it first."))
            continue
        bank = bank_by_key.get(_bank_key(row[2] if len(row) > 2 else None))
        if bank is None:
            result.parse_errors.append(RowError(
                idx, "C", raw_snapshot,
                f"Unknown bank {row[2]!r} — add it under Payroll › Banks first (known: " + ", ".join(b.name for b in banks) + ").",
            ))
            continue
        try:
            total = _parse_positive_money(row[3] if len(row) > 3 else None, CHALLAN_HEADERS[3])
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, "D", raw_snapshot, str(exc)))
            continue
        period_year = period_month = None
        run = None
        if len(row) > 4 and not _is_blank(row[4]):
            try:
                period_year, period_month = _parse_month(row[4], "Payroll month")
            except ValueError as exc:
                result.parse_errors.append(RowError(idx, "E", raw_snapshot, str(exc)))
                continue
            if (period_year, period_month) not in set(tax_year_months(tax_year)):
                result.parse_errors.append(RowError(
                    idx, "E", raw_snapshot,
                    f"{month_label(period_year, period_month)} is not a month of tax year {tax_year.code} "
                    f"(the year the deposit date falls in).",
                ))
                continue
            run = orbis_run_for_month(period_year, period_month)
            if run is None and not is_legacy_month(tax_year, period_year, period_month):
                unlinked_months.append(f"{number} → {month_label(period_year, period_month)}")
        note = str(row[5]).strip() if len(row) > 5 and not _is_blank(row[5]) else ""
        result.challans.append(ParsedChallanRow(
            idx, number, deposit_date, bank, total, tax_year, note[:255], period_year, period_month, run,
        ))
    if unlinked_months:
        result.warnings.append(
            "No calculated payroll run yet for: " + ", ".join(unlinked_months)
            + " — the challan is recorded; re-upload after the run is calculated to link it."
        )
    return result


def apply_tax_challan_upload(viewer, parsed: TaxChallanParse, *, batch=None) -> TaxChallanApplyReport:
    report = TaxChallanApplyReport(warnings=list(parsed.warnings))
    if not parsed.challans:
        report.applied = True
        return report
    with transaction.atomic():
        for c in parsed.challans:
            existing = TaxChallan.objects.filter(tax_year=c.tax_year, challan_number=c.challan_number).first()
            if existing is None:
                TaxChallan.objects.create(
                    tax_year=c.tax_year, challan_number=c.challan_number, deposit_date=c.deposit_date, bank=c.bank,
                    total_amount=c.total_amount, note=c.note, run=c.run,
                    period_year=c.period_year, period_month=c.period_month,
                    source=TaxChallan.Source.RUN if c.run else TaxChallan.Source.IMPORTED,
                    batch=batch, added_by=viewer, updated_by=viewer,
                )
                report.created += 1
            else:
                existing.deposit_date = c.deposit_date
                existing.bank = c.bank
                existing.total_amount = c.total_amount
                existing.note = c.note
                if c.period_year is not None:
                    # A typed month corrects the stored one; a blank cell
                    # leaves it alone (like the run link, never cleared here).
                    existing.period_year, existing.period_month = c.period_year, c.period_month
                if c.run is not None and existing.run_id is None:
                    existing.run = c.run
                    existing.source = TaxChallan.Source.RUN
                existing.batch = batch or existing.batch
                existing.updated_by = viewer
                existing.save()
                report.updated += 1
            if c.run is not None:
                report.linked_to_runs += 1
    report.applied = True
    return report


def build_tax_challan_xlsx_template() -> bytes:
    from payroll.tax_ledger import CHALLAN_SHEET, write_challans_sheet

    wb = Workbook()
    ws = wb.active
    ws.title = CHALLAN_SHEET
    write_challans_sheet(ws, [])
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Pre-Orbis payroll months (cutover) — docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §3
#
# One sheet, the monthly payroll ledger (payroll/tax_ledger.py): Employee ID
# | Payroll month | one column per pay component (earnings then deductions)
# | TDS deducted | Net pay — exactly what was paid, nothing derived
# (Revision 2: the reimbursement is the policy tier, computed; the challan
# is the separate per-employee allocation sheet below). Only months BEFORE
# TaxYear.orbis_live_from are accepted; an Orbis month comes from its run.
# Re-upload overwrites (facts, not a ledger). Apply also rewrites the
# declaration's two true-up openings from the month rows. One-time at
# cutover; Payroll Admins only.
# ---------------------------------------------------------------------------

@dataclass
class ParsedLegacyMonthRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    year: int
    month: int
    lines: dict                      # component_code -> Decimal
    tds: Decimal
    net_pay: Decimal | None = None
    adjustment_months: int | None = None
    earnings_total: Decimal = Decimal("0")
    deductions_total: Decimal = Decimal("0")

    @property
    def month_label(self) -> str:
        return f"{self.year}-{self.month:02d}"


@dataclass
class LegacyPayrollParse:
    months: list[ParsedLegacyMonthRow] = field(default_factory=list)
    components: list = field(default_factory=list)       # PayComponent, sheet order
    earning_codes: set = field(default_factory=set)
    deduction_codes: set = field(default_factory=set)
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def employee_count(self) -> int:
        return len({r.employee.pk for r in self.months})


@dataclass
class LegacyPayrollApplyReport:
    months_created: int = 0
    months_updated: int = 0
    employees_updated: int = 0
    created_declarations: int = 0
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return self.months_created + self.months_updated


def _load_sheet(file_obj, filename: str, title: str) -> list[list]:
    """One named sheet: by title for an .xlsx (falling back to the first
    sheet when Finance renamed it), the whole file for a .csv."""
    from payroll.tax_ledger import header_key

    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
        for ws in wb.worksheets:
            if header_key(ws.title) == header_key(title):
                return [list(row) for row in ws.iter_rows(values_only=True)]
        return [list(row) for row in wb.worksheets[0].iter_rows(values_only=True)]
    if name.endswith(".csv"):
        raw = file_obj.read()
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
        return [list(row) for row in csv.reader(io.StringIO(text))]
    raise ValueError("Unsupported file type — upload .xlsx or .csv.")


def parse_legacy_payroll_upload(file_obj, filename: str, *, tax_year) -> LegacyPayrollParse:
    """Read-only dry run. File-level problems (no live-from, wrong headers)
    raise ValueError; row problems land in parse_errors; the soft checks
    (month gaps, opening mismatches) in warnings."""
    from payroll.models import EmployeeTaxDeclaration, PayComponent
    from payroll.tax_ledger import (
        FIXED_KEYS, MONTHS_SHEET, ledger_components, legacy_months, month_label, period_end, period_start,
        resolve_ledger_headers,
    )

    if tax_year.orbis_live_from_id is None:
        raise ValueError(
            "Set 'Orbis live from' on the tax year in the admin first — it decides which months are pre-Orbis."
        )
    allowed_months = legacy_months(tax_year)
    if not allowed_months:
        raise ValueError(
            f"Orbis is live from the first month of {tax_year.code} — there are no pre-Orbis months to import."
        )
    allowed_set = set(allowed_months)

    rows = _load_sheet(file_obj, filename, MONTHS_SHEET)
    if not rows:
        raise ValueError("File is empty.")
    if len(rows) < 2:
        raise ValueError("File has a header but no data rows.")

    components = ledger_components()
    result = LegacyPayrollParse(
        components=components,
        earning_codes={c.code for c in components if c.kind == PayComponent.Kind.EARNING},
        deduction_codes={c.code for c in components if c.kind == PayComponent.Kind.DEDUCTION},
    )
    mapping, header_errors = resolve_ledger_headers(rows[0], components)
    if header_errors:
        raise ValueError(" ".join(header_errors))
    col_of = {key: idx for idx, key in mapping.items()}
    component_cols = {key: idx for idx, key in mapping.items() if key not in FIXED_KEYS}
    name_by_code = {c.code: c.name for c in components}

    body = rows[1:]
    emp_ids: set[str] = set()
    for row in body:
        c = col_of["employee_id"]
        if len(row) > c and not _is_blank(row[c]):
            try:
                emp_ids.add(_coerce_employee_id(row[c]))
            except ValueError:
                continue
    # Separated employees included — a July leaver still has July's figures.
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    seen: dict[tuple[str, int, int], int] = {}
    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue

        def cell(key):
            c = col_of.get(key)
            return row[c] if c is not None and len(row) > c else None

        def letter(key):
            c = col_of.get(key)
            return get_column_letter(c + 1) if c is not None else None

        raw_snapshot = {"Employee_ID": cell("employee_id"), "Month": cell("month"), "TDS": cell("tds")}
        try:
            emp_id_str = _coerce_employee_id(cell("employee_id"))
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, letter("employee_id"), raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            result.parse_errors.append(RowError(idx, letter("employee_id"), raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        try:
            year, month = _parse_month(cell("month"), "Payroll month")
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, letter("month"), raw_snapshot, str(exc)))
            continue
        if (year, month) not in allowed_set:
            result.parse_errors.append(RowError(
                idx, letter("month"), raw_snapshot,
                f"{month_label(year, month)} is not a pre-Orbis month of {tax_year.code} — only "
                + ", ".join(month_label(y, m) for y, m in allowed_months)
                + " may be imported; an Orbis month comes from its payroll run.",
            ))
            continue
        key = (emp_id_str, year, month)
        if key in seen:
            result.parse_errors.append(RowError(
                idx, letter("month"), raw_snapshot,
                f"Employee {emp_id_str} / {month_label(year, month)} also appears on row {seen[key]} — one row per employee per month.",
            ))
            continue
        seen[key] = idx

        lines: dict[str, Decimal] = {}
        bad = False
        for code, c in component_cols.items():
            try:
                amount = _parse_money_or_zero(row[c] if len(row) > c else None, name_by_code.get(code, code))
            except ValueError as exc:
                result.parse_errors.append(RowError(idx, get_column_letter(c + 1), raw_snapshot, str(exc)))
                bad = True
                break
            if amount:
                lines[code] = amount
        if bad:
            continue
        try:
            tds = _parse_money_or_zero(cell("tds"), "TDS deducted")
            net_pay = None if _is_blank(cell("net")) else _parse_money_or_zero(cell("net"), "Net pay")
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, letter("tds"), raw_snapshot, str(exc)))
            continue
        adjustment_months = None
        if not _is_blank(cell("adj_months")):
            try:
                adjustment_months = int(Decimal(str(cell("adj_months")).strip()))
                if adjustment_months < 0 or adjustment_months > 24:
                    raise ValueError
            except (ValueError, InvalidOperation):
                result.parse_errors.append(RowError(
                    idx, letter("adj_months"), raw_snapshot,
                    f"Salary adjustment months must be a whole number of months (0–24), got {cell('adj_months')!r}.",
                ))
                continue
        if adjustment_months and not lines.get("SALARY_ADJUSTMENT"):
            result.parse_errors.append(RowError(
                idx, letter("adj_months"), raw_snapshot,
                "Salary adjustment months is set but the row pays no Salary adjustment.",
            ))
            continue
        result.months.append(ParsedLegacyMonthRow(
            idx, employee, emp_id_str, year, month, lines, tds, net_pay, adjustment_months,
            earnings_total=sum((v for k, v in lines.items() if k in result.earning_codes), Decimal("0")),
            deductions_total=sum((v for k, v in lines.items() if k in result.deduction_codes), Decimal("0")),
        ))

    if result.parse_errors:
        return result

    # --- Soft checks (warnings, never blockers) ----------------------------
    unmeasured = [r for r in result.months if r.lines.get("SALARY_ADJUSTMENT") and r.adjustment_months is None]
    if unmeasured:
        result.warnings.append(
            f"{len(unmeasured)} row(s) pay a Salary adjustment with no 'Salary adjustment months' — the engine "
            f"can't tell how many months that instalment settled, so the next instalment will start from month 1. "
            f"Re-upload with the count (e.g. 3) to fix."
        )
    by_employee: dict[int, list[ParsedLegacyMonthRow]] = {}
    for r in result.months:
        by_employee.setdefault(r.employee.pk, []).append(r)
    for pk, emp_rows in by_employee.items():
        emp = emp_rows[0].employee
        have = {(r.year, r.month) for r in emp_rows}
        expected = [
            (y, m) for (y, m) in allowed_months
            if (emp.joining_date is None or emp.joining_date <= period_end(y, m))
            and (emp.last_working_date is None or emp.last_working_date >= period_start(y, m))
        ]
        missing = [month_label(y, m) for (y, m) in expected if (y, m) not in have]
        if missing:
            result.warnings.append(
                f"Employee {emp_rows[0].employee_id}: no row for {', '.join(missing)} although employed then — "
                f"a gap here understates their year-to-date figures."
            )
    declared = {
        d.employee_id: d for d in EmployeeTaxDeclaration.objects.filter(tax_year=tax_year, employee_id__in=list(by_employee))
    }
    for pk, emp_rows in by_employee.items():
        d = declared.get(pk)
        if d is None or not d.opening_tds_deducted:
            continue
        file_total = sum((r.tds for r in emp_rows), Decimal("0"))
        if len(emp_rows) == len(allowed_months) and file_total != d.opening_tds_deducted:
            result.warnings.append(
                f"Employee {emp_rows[0].employee_id}: TDS in this file totals {file_total:,.0f} but the earlier "
                f"openings upload said {d.opening_tds_deducted:,.0f} — the month rows win and the opening is rewritten."
            )
    return result


def apply_legacy_payroll_upload(viewer, parsed: LegacyPayrollParse, *, tax_year, batch=None) -> LegacyPayrollApplyReport:
    """Upsert the month rows, then rewrite each touched employee's true-up
    openings from ALL their legacy months of the year — so a partial
    re-upload still yields full totals and the engine can never disagree
    with the month rows."""
    from payroll.models import EmployeeTaxDeclaration
    from payroll.tax_ledger import taxable_earning_codes

    report = LegacyPayrollApplyReport(warnings=list(parsed.warnings))
    if not parsed.months:
        report.applied = True
        return report
    with transaction.atomic():
        touched: dict[int, object] = {}
        for r in parsed.months:
            touched[r.employee.pk] = r.employee
            lines_json = {code: str(amount) for code, amount in r.lines.items()}
            obj, created = LegacyPayrollMonth.objects.update_or_create(
                employee=r.employee, year=r.year, month=r.month,
                defaults=dict(
                    tax_year=tax_year, lines=lines_json, tds_deducted=r.tds, net_pay=r.net_pay,
                    adjustment_months=r.adjustment_months, batch=batch, updated_by=viewer,
                ),
            )
            if created:
                if obj.added_by_id is None:
                    obj.added_by = viewer
                    obj.save(update_fields=["added_by"])
                report.months_created += 1
            else:
                report.months_updated += 1

        taxable = taxable_earning_codes()
        declarations = {
            d.employee_id: d
            for d in EmployeeTaxDeclaration.objects.filter(tax_year=tax_year, employee_id__in=list(touched))
        }
        sums: dict[int, list[Decimal]] = {pk: [Decimal("0"), Decimal("0")] for pk in touched}
        for lm in LegacyPayrollMonth.objects.filter(tax_year=tax_year, employee_id__in=list(touched)):
            sums[lm.employee_id][0] += lm.tds_deducted or Decimal("0")
            sums[lm.employee_id][1] += lm.taxable_additional_pay(taxable)
        for pk, employee in touched.items():
            declaration = declarations.get(pk)
            if declaration is None:
                declaration = EmployeeTaxDeclaration(employee=employee, tax_year=tax_year, added_by=viewer)
                report.created_declarations += 1
            declaration.opening_tds_deducted = sums[pk][0]
            declaration.opening_additional_pay = sums[pk][1]
            declaration.updated_by = viewer
            declaration.save()
            report.employees_updated += 1
    report.applied = True
    return report


def build_legacy_payroll_xlsx_template(tax_year) -> bytes:
    """One sheet: one prefilled row per employee per pre-Orbis month they
    were employed in (ID, month, name), amounts blank, headers generated
    from the live component list — without the export's derived columns."""
    from django.db.models import Q

    from payroll.tax_ledger import (
        MONTHS_SHEET, LedgerRow, ledger_components, legacy_months, period_end, period_start, write_months_sheet,
    )

    components = ledger_components()
    months = legacy_months(tax_year)
    wb = Workbook()
    ws_m = wb.active
    ws_m.title = MONTHS_SHEET
    rows: list[LedgerRow] = []
    if months:
        first_start = period_start(*months[0])
        last_end = period_end(*months[-1])
        employees = (
            User.objects.filter(employee_id__isnull=False, joining_date__lte=last_end)
            .exclude(employee_id="")
            .filter(Q(last_working_date__isnull=True) | Q(last_working_date__gte=first_start))
            .annotate(employee_id_num=Cast("employee_id", IntegerField()))
            .order_by("employee_id_num")
        )
        for emp in employees:
            for y, m in months:
                if emp.joining_date and emp.joining_date > period_end(y, m):
                    continue
                if emp.last_working_date and emp.last_working_date < period_start(y, m):
                    continue
                rows.append(LedgerRow(
                    employee=emp, employee_id=emp.employee_id, full_name=emp.get_full_name(), year=y, month=m,
                ))
    write_months_sheet(ws_m, rows, components, template=True)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# TDS by challan (per employee) — docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md R2.3
#
# Employee ID | Challan No | Amount: how much of each employee's tax a
# deposit carried. Permanent and monthly — Finance records the deposit on
# the Tax challans card, then Payroll Admins upload who was in it and for
# how much. One table (TaxChallanAllocation) for pre-Orbis months, Orbis
# runs and deposits tied to no month. Errors abort the row (unknown
# challan, unknown employee, amount ≤ 0, duplicate pair); the
# reconciliations are warnings. Re-upload overwrites a (challan, employee)
# pair. The template is prefilled from the month's TDS for every challan
# tied to a month — challan number filled in only when the month has one.
# ---------------------------------------------------------------------------

@dataclass
class ParsedAllocationRow:
    line_no: int
    employee: object                 # CustomUser
    employee_id: str
    challan: object                  # TaxChallan
    amount: Decimal

    @property
    def challan_number(self) -> str:
        return self.challan.challan_number


@dataclass
class ChallanAllocationParse:
    rows: list[ParsedAllocationRow] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def employee_count(self) -> int:
        return len({r.employee.pk for r in self.rows})


@dataclass
class ChallanAllocationApplyReport:
    created: int = 0
    updated: int = 0
    parse_errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return self.created + self.updated


def _ids_preview(ids: list[str], limit: int = 8) -> str:
    shown = ", ".join(ids[:limit])
    return shown + (f" … and {len(ids) - limit} more" if len(ids) > limit else "")


def parse_challan_allocation_upload(file_obj, filename: str, *, tax_year) -> ChallanAllocationParse:
    """Read-only dry run. File-level problems raise ValueError; row problems
    land in parse_errors; the reconciliations (challan totals, month TDS,
    unallocated taxed employees, one employee in two challans of a month)
    in warnings."""
    from payroll.models import TaxChallanAllocation
    from payroll.tax_ledger import ALLOCATION_SHEET, month_label, month_tds_by_employee, resolve_allocation_headers

    rows = _load_sheet(file_obj, filename, ALLOCATION_SHEET)
    if not rows:
        raise ValueError("File is empty.")
    if len(rows) < 2:
        raise ValueError("File has a header but no data rows.")
    mapping, header_errors = resolve_allocation_headers(rows[0])
    if header_errors:
        raise ValueError(" ".join(header_errors))
    col_of = {key: idx for idx, key in mapping.items()}
    known_challans = {c.challan_number: c for c in TaxChallan.objects.filter(tax_year=tax_year).select_related("bank")}

    body = rows[1:]
    emp_ids: set[str] = set()
    for row in body:
        c = col_of["employee_id"]
        if len(row) > c and not _is_blank(row[c]):
            try:
                emp_ids.add(_coerce_employee_id(row[c]))
            except ValueError:
                continue
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    result = ChallanAllocationParse()
    seen: dict[tuple[str, str], int] = {}
    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue

        def cell(key):
            c = col_of.get(key)
            return row[c] if c is not None and len(row) > c else None

        def letter(key):
            return get_column_letter(col_of[key] + 1)

        raw_snapshot = {"Employee_ID": cell("employee_id"), "Challan_No": cell("challan"), "Amount": cell("amount")}
        try:
            emp_id_str = _coerce_employee_id(cell("employee_id"))
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, letter("employee_id"), raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            result.parse_errors.append(RowError(idx, letter("employee_id"), raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        number = str(cell("challan")).strip() if not _is_blank(cell("challan")) else ""
        if not number:
            result.parse_errors.append(RowError(idx, letter("challan"), raw_snapshot, "Challan No is required."))
            continue
        challan = known_challans.get(number)
        if challan is None:
            result.parse_errors.append(RowError(
                idx, letter("challan"), raw_snapshot,
                f"Challan '{number}' is not in Orbis for {tax_year.code} — record it first (Upload Center › Tax challans).",
            ))
            continue
        try:
            amount = _parse_positive_money(cell("amount"), "Amount")
        except ValueError as exc:
            result.parse_errors.append(RowError(idx, letter("amount"), raw_snapshot, str(exc)))
            continue
        key = (emp_id_str, number)
        if key in seen:
            result.parse_errors.append(RowError(
                idx, letter("challan"), raw_snapshot,
                f"Employee {emp_id_str} / challan {number} also appears on row {seen[key]} — one row per employee per challan.",
            ))
            continue
        seen[key] = idx
        result.rows.append(ParsedAllocationRow(idx, employee, emp_id_str, challan, amount))

    if result.parse_errors or not result.rows:
        return result

    # --- Soft checks (warnings, never blockers) ----------------------------
    # The file's rows overlaid on what is already allocated, so a partial
    # re-upload is checked against the whole picture, not just itself.
    file_challans = {r.challan.pk: r.challan for r in result.rows}
    allocated: dict[int, dict[int, Decimal]] = {pk: {} for pk in file_challans}          # challan → employee → amount
    employee_of: dict[int, object] = {}
    for emp_id, challan_id, amount in TaxChallanAllocation.objects.filter(
        challan_id__in=list(file_challans),
    ).values_list("employee_id", "challan_id", "amount"):
        allocated[challan_id][emp_id] = amount or Decimal("0")
    for r in result.rows:
        allocated[r.challan.pk][r.employee.pk] = r.amount
        employee_of[r.employee.pk] = r.employee

    for challan_id, challan in sorted(file_challans.items(), key=lambda kv: kv[1].challan_number):
        total = sum(allocated[challan_id].values(), Decimal("0"))
        if total != challan.total_amount:
            result.warnings.append(
                f"Challan {challan.challan_number}: allocated {total:,.0f} across {len(allocated[challan_id])} employee(s) "
                f"but its total is {challan.total_amount:,.0f} (difference {challan.total_amount - total:,.0f})."
            )

    months = {(c.period_year, c.period_month) for c in file_challans.values() if c.period_year and c.period_month}
    for year, month in sorted(months):
        label = month_label(year, month)
        month_challans = list(
            TaxChallan.objects.filter(tax_year=tax_year, period_year=year, period_month=month)
        )
        # Every allocation under any challan of this month (file overlaid).
        per_employee: dict[int, dict[int, Decimal]] = {}
        for emp_id, challan_id, amount in TaxChallanAllocation.objects.filter(
            challan__in=month_challans,
        ).values_list("employee_id", "challan_id", "amount"):
            per_employee.setdefault(emp_id, {})[challan_id] = amount or Decimal("0")
        for challan in month_challans:
            for emp_id, amount in allocated.get(challan.pk, {}).items():
                per_employee.setdefault(emp_id, {})[challan.pk] = amount
        taxed = month_tds_by_employee(tax_year, year, month)
        if not taxed:
            result.warnings.append(
                f"{label}: no TDS figures in Orbis for that month yet (no imported rows / no calculated run), "
                f"so the allocations can't be checked against it."
            )
            continue
        mismatched: list[str] = []
        split: list[str] = []
        for emp_id, by_challan in per_employee.items():
            if len(by_challan) > 1:
                split.append(taxed[emp_id].employee_id if emp_id in taxed else str(employee_of.get(emp_id, emp_id)))
            if emp_id in taxed and sum(by_challan.values(), Decimal("0")) != taxed[emp_id].tds_deducted:
                mismatched.append(
                    f"{taxed[emp_id].employee_id} (allocated {sum(by_challan.values(), Decimal('0')):,.0f}, "
                    f"TDS {taxed[emp_id].tds_deducted:,.0f})"
                )
        unallocated = [r.employee_id for emp_id, r in taxed.items() if emp_id not in per_employee]
        if mismatched:
            result.warnings.append(
                f"{label}: allocations don't equal the month's TDS for {len(mismatched)} employee(s) — "
                + _ids_preview(mismatched) + "."
            )
        if unallocated:
            result.warnings.append(
                f"{label}: {len(unallocated)} employee(s) with TDS that month are in no challan yet — "
                + _ids_preview(sorted(unallocated)) + ". The certificate needs every one placed."
            )
        if split:
            result.warnings.append(
                f"{label}: {len(split)} employee(s) are allocated to more than one challan of the same month — "
                + _ids_preview(sorted(split)) + ". Finance's rule is exactly one; the amounts still add up."
            )
    return result


def apply_challan_allocation_upload(viewer, parsed: ChallanAllocationParse, *, batch=None) -> ChallanAllocationApplyReport:
    from payroll.models import TaxChallanAllocation

    report = ChallanAllocationApplyReport(warnings=list(parsed.warnings))
    if not parsed.rows:
        report.applied = True
        return report
    with transaction.atomic():
        for r in parsed.rows:
            obj, created = TaxChallanAllocation.objects.update_or_create(
                challan=r.challan, employee=r.employee,
                defaults=dict(
                    amount=r.amount, source=TaxChallanAllocation.Source.IMPORTED, batch=batch, updated_by=viewer,
                ),
            )
            if created:
                if obj.added_by_id is None:
                    obj.added_by = viewer
                    obj.save(update_fields=["added_by"])
                report.created += 1
            else:
                report.updated += 1
    report.applied = True
    return report


def build_challan_allocation_xlsx_template(tax_year) -> bytes:
    """Sheet 1 "TDS by challan": one row per employee per payroll month that
    has at least one challan recorded, amount prefilled with the month's
    TDS (imported rows for a pre-Orbis month, the calculated run for an
    Orbis month), Challan No prefilled when the month has exactly one
    challan. Sheet 2 "Challans": the year's challans for reference."""
    from payroll.tax_ledger import (
        ALLOCATION_SHEET, CHALLAN_SHEET, month_tds_by_employee, write_allocation_sheet, write_challans_sheet,
    )

    challans = list(TaxChallan.objects.filter(tax_year=tax_year).select_related("bank", "run__period"))
    by_month: dict[tuple[int, int], list] = {}
    for c in challans:
        if c.period_year and c.period_month:
            by_month.setdefault((c.period_year, c.period_month), []).append(c)
    rows = []
    for (year, month), month_challans in sorted(by_month.items()):
        number = month_challans[0].challan_number if len(month_challans) == 1 else ""
        for r in sorted(month_tds_by_employee(tax_year, year, month).values(), key=lambda r: r.employee_id):
            rows.append((r.employee_id, number, r.tds_deducted, f"{r.full_name} · {r.month_label}"))
    wb = Workbook()
    ws = wb.active
    ws.title = ALLOCATION_SHEET
    write_allocation_sheet(ws, rows)
    write_challans_sheet(wb.create_sheet(CHALLAN_SHEET), challans)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()
