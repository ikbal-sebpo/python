"""Pre-Orbis annual-leave record import — HR's "AL Calc" sheet, as given.

Writes `leave.models.LegacyAnnualLeaveRecord` rows (docs/LEGACY_60_IMPORT_PLAN.md
§4): a Service-Book-only historical record, never a `LeaveBalance` credit.
Six columns, header on row 1:

    Employee ID | Till <year-1> | <year> AL Taken | <year> AL Balance | 60 Cap Till <year> | Note

The header labels are matched by position, not text, so HR's own column
titles ("Till 2024", "2025 AL Taken", …) work unchanged. The **as-of date**
comes from the upload form (2025-12-31 for the first batch), not the file.

Eligibility (decision 6 in the plan): an employee with under 12 months'
service at the as-of date gets no record — the policy allocates no AL before
the first anniversary. Those rows are reported as skipped, not errors, so HR
can send the whole sheet and read the outcome in the report.

Apply is an upsert on (employee, as_of): this is a plain record, so a
corrected re-upload replaces the row (audit via added_by / updated_by).
Structurally mirrors leave/upload_bank.py, which stays as it was.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import openpyxl
from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import IntegerField
from django.db.models.functions import Cast

from core.exports import escape_spreadsheet
from leave.models import LegacyAnnualLeaveRecord


User = get_user_model()

AL_BALANCE_CAP = Decimal("60")
MIN_SERVICE_MONTHS = 12


@dataclass
class ParsedALHistoryRow:
    line_no: int
    employee: object   # CustomUser
    employee_id: str
    accrued_before_year: Decimal
    taken_in_year: Decimal
    balance_in_year: Decimal
    capped_balance: Decimal
    note: str


@dataclass
class RowError:
    line_no: int
    col_letter: str | None
    raw: dict
    message: str


@dataclass
class RowNotice:
    """Soft — never blocks. Used for the arithmetic cross-check and skips."""
    line_no: int
    employee_id: str
    employee_name: str
    message: str


@dataclass
class ALHistoryApplyReport:
    created: list[ParsedALHistoryRow] = field(default_factory=list)
    updated: list[ParsedALHistoryRow] = field(default_factory=list)
    skipped_ineligible: list[RowNotice] = field(default_factory=list)
    warnings: list[RowNotice] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)


# ---------------------------------------------------------------------------
# Cell coercion + file reading (same idioms as leave/upload_bank.py)
# ---------------------------------------------------------------------------

def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _coerce_employee_id(value) -> str:
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


def _parse_days(raw, label: str, *, cap: Decimal | None = None) -> Decimal:
    if _is_blank(raw):
        raise ValueError(f"Empty {label}.")
    if isinstance(raw, bool):
        raise ValueError(f"Bad {label}: {raw!r}.")
    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        try:
            value = Decimal(str(raw).strip())
        except InvalidOperation:
            raise ValueError(f"Bad {label} '{raw}' — expected a number of days.")
    if value < 0:
        raise ValueError(f"{label.capitalize()} can't be negative.")
    if cap is not None and value > cap:
        raise ValueError(f"{label.capitalize()} {value} exceeds the {cap}-day lifetime cap.")
    return value.quantize(Decimal("0.01"))


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


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    try:
        return d.replace(year=year, month=month)
    except ValueError:
        return d.replace(year=year, month=month, day=28)


def is_eligible_at(joining_date: date, as_of: date) -> bool:
    """12 full months of service on or before the as-of date."""
    return _add_months(joining_date, MIN_SERVICE_MONTHS) <= as_of


# ---------------------------------------------------------------------------
# Parse (read-only) + apply
# ---------------------------------------------------------------------------

def parse_al_history_upload(
    file_obj, filename: str, *, as_of: date,
) -> tuple[list[ParsedALHistoryRow], list[RowError], list[RowError], list[RowNotice], list[RowNotice]]:
    """-> (parsed, parse_errors, duplicates, skipped_ineligible, warnings)."""
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
    # A record is still history for a separated employee — not is_active-only.
    employees_by_id = {u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids)}

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    skipped: list[RowNotice] = []
    warnings: list[RowNotice] = []
    parsed: list[ParsedALHistoryRow] = []
    seen: dict[str, int] = {}
    labels = ("accrued before year", "AL taken in year", "AL balance in year", "capped balance")

    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        cells = [row[i] if len(row) > i else None for i in range(6)]
        raw_snapshot = {
            "Employee_ID": cells[0], "Accrued_before": cells[1], "Taken": cells[2],
            "Balance": cells[3], "Capped": cells[4], "Note": cells[5],
        }
        try:
            emp_id_str = _coerce_employee_id(cells[0])
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No employee with ID '{emp_id_str}'."))
            continue
        values: list[Decimal] = []
        failed = False
        for col_idx, label in enumerate(labels, start=1):
            try:
                values.append(_parse_days(
                    cells[col_idx], label, cap=AL_BALANCE_CAP if label == "capped balance" else None,
                ))
            except ValueError as exc:
                parse_errors.append(RowError(idx, "ABCDE"[col_idx], raw_snapshot, str(exc)))
                failed = True
                break
        if failed:
            continue
        note = "" if _is_blank(cells[5]) else " ".join(str(cells[5]).split())[:255]

        if emp_id_str in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[emp_id_str]} — this employee appears twice in the file.",
            ))
            continue
        seen[emp_id_str] = idx

        if not employee.joining_date:
            parse_errors.append(RowError(
                idx, "A", raw_snapshot, f"{employee.display_name} has no joining date in Orbis — can't check eligibility.",
            ))
            continue
        if employee.joining_date > as_of:
            skipped.append(RowNotice(
                idx, emp_id_str, employee.display_name,
                f"Joined {employee.joining_date.isoformat()}, after the as-of date {as_of.isoformat()} — "
                "not employed then, no record written.",
            ))
            continue
        if not is_eligible_at(employee.joining_date, as_of):
            skipped.append(RowNotice(
                idx, emp_id_str, employee.display_name,
                f"Under {MIN_SERVICE_MONTHS} months' service at {as_of.isoformat()} "
                f"(joined {employee.joining_date.isoformat()}) — no AL allocated per policy, no record written.",
            ))
            continue

        accrued, taken, balance, capped = values
        expected = min(AL_BALANCE_CAP, accrued + balance)
        if capped != expected:
            warnings.append(RowNotice(
                idx, emp_id_str, employee.display_name,
                f"Capped balance {capped} ≠ min(60, {accrued} + {balance}) = {expected} — stored as given, "
                "please double-check HR's arithmetic.",
            ))
        parsed.append(ParsedALHistoryRow(
            line_no=idx, employee=employee, employee_id=emp_id_str,
            accrued_before_year=accrued, taken_in_year=taken, balance_in_year=balance,
            capped_balance=capped, note=note,
        ))

    return parsed, parse_errors, dup_errors, skipped, warnings


def apply_al_history_upload(
    viewer, parsed_rows: list[ParsedALHistoryRow], *, as_of: date, source_file: str = "",
) -> ALHistoryApplyReport:
    """Upsert one LegacyAnnualLeaveRecord per row on (employee, as_of)."""
    report = ALHistoryApplyReport()
    if not parsed_rows:
        report.applied = True
        return report

    existing = {
        r.employee_id: r
        for r in LegacyAnnualLeaveRecord.objects.filter(
            employee_id__in=[r.employee.pk for r in parsed_rows], as_of=as_of,
        )
    }
    with transaction.atomic():
        for r in parsed_rows:
            values = {
                "accrued_before_year": r.accrued_before_year, "taken_in_year": r.taken_in_year,
                "balance_in_year": r.balance_in_year, "capped_balance": r.capped_balance,
                "note": r.note, "source_file": source_file[:255], "updated_by": viewer,
            }
            rec = existing.get(r.employee.pk)
            if rec is None:
                LegacyAnnualLeaveRecord.objects.create(
                    employee=r.employee, as_of=as_of, added_by=viewer, **values,
                )
                report.created.append(r)
            else:
                for k, v in values.items():
                    setattr(rec, k, v)
                rec.save()
                report.updated.append(r)
    report.applied = True
    return report


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

def build_al_history_xlsx_template(*, as_of: date | None = None) -> bytes:
    year = (as_of or date.today()).year
    wb = Workbook()
    ws = wb.active
    ws.title = "AL History"

    employees = list(
        User.objects.filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )
    headers = [
        "Employee ID", f"Till {year - 1}", f"{year} AL Taken", f"{year} AL Balance",
        f"60 Cap Till {year}", "Note", "Employee Name (reference)", "Joining date (reference)",
    ]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    row_idx = 2
    for emp in employees:
        eid_cell = ws.cell(row=row_idx, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        ws.cell(row=row_idx, column=7, value=escape_spreadsheet(emp.display_name))
        if emp.joining_date:
            ws.cell(row=row_idx, column=8, value=emp.joining_date.isoformat())
        row_idx += 1

    if row_idx > 2:
        dv = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
        dv.error = "Enter a non-negative number of days."
        dv.errorTitle = "Invalid days"
        ws.add_data_validation(dv)
        dv.add(f"B2:D{row_idx - 1}")
        cap_dv = DataValidation(
            type="decimal", operator="between", formula1="0", formula2=str(AL_BALANCE_CAP), allow_blank=True,
        )
        cap_dv.error = f"Enter a number between 0 and {AL_BALANCE_CAP}."
        cap_dv.errorTitle = "Invalid capped balance"
        ws.add_data_validation(cap_dv)
        cap_dv.add(f"E2:E{row_idx - 1}")

    for col, width in {"A": 14, "B": 12, "C": 14, "D": 16, "E": 16, "F": 40, "G": 28, "H": 22}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()
