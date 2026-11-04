"""HR / department-manager upload for Compensatory Leave grants
(leave.CplGrant) — the manual/upload path for departments whose CpL
entitlement can't be auto-detected from the roster (e.g. Data Solutions &
Due Diligence, which decides CpL vs. overtime per employee/instance
outside Orbis), and a one-time historical-backfill path for any other
department (e.g. IT & Cybersecurity's own tracker).

Structurally mirrors leave/upload_bank.py: a read-only parse pass, then a
preview or apply pass over the same rows. Unlike upload_bank.py, both
preview and apply route through the exact same per-row writer
(leave/services.py::import_cpl_grant_row) — preview just wraps it in a
rolled-back transaction, so "what preview shows" and "what apply does"
can never drift apart. Idempotent per (employee, holiday) — see
import_cpl_grant_row's own docstring.

Access is per-employee, not org-wide-only like upload_bank.py: a
department manager can upload for their own managed scope, HR/Admins for
anyone (accounts.permissions.can_manage_cpl_grants) — a row for someone
outside the uploader's scope is rejected, not silently skipped, so it
shows up as an error to fix (wrong file, wrong uploader) rather than a
quiet gap.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl
from openpyxl import Workbook

from django.contrib.auth import get_user_model
from django.db import transaction

from accounts.permissions import can_manage_cpl_grants
from leave.models import CplGrant
from leave.services import import_cpl_grant_row

User = get_user_model()


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    line_no: int
    employee_id: str
    employee: object  # CustomUser
    work_date: date
    status: str
    consumed_date: date | None


@dataclass
class RowError:
    line_no: int
    raw: dict
    message: str


@dataclass
class ApplyReport:
    imported: list[ParsedRow] = field(default_factory=list)
    skipped: list[RowError] = field(default_factory=list)
    apply_errors: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors)

    @property
    def total_writes(self) -> int:
        return len(self.imported)


class _Rollback(Exception):
    pass


# ---------------------------------------------------------------------------
# Cell coercion + file reading
# ---------------------------------------------------------------------------

def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _coerce_employee_id(value) -> str:
    if value is None:
        raise ValueError("Empty Employee ID.")
    if isinstance(value, bool):
        raise ValueError(f"Bad Employee ID: {value!r}.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Employee ID '{value}' looks like a decimal — expected an integer or text.")
        return str(int(value))
    s = str(value).strip()
    if not s:
        raise ValueError("Empty Employee ID.")
    return s


def _coerce_date(raw, *, label: str) -> date | None:
    if _is_blank(raw):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised {label} '{raw}' — expected YYYY-MM-DD.")


def _coerce_status(raw) -> str:
    if _is_blank(raw):
        return CplGrant.Status.OPEN
    s = str(raw).strip().upper()
    if s not in (CplGrant.Status.OPEN, CplGrant.Status.CONSUMED):
        raise ValueError(f"Status must be OPEN or CONSUMED, got '{raw}'.")
    return s


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
# Parse (read-only) + preview/apply
# ---------------------------------------------------------------------------

def parse_upload(file_obj, filename: str, *, viewer) -> tuple[list[ParsedRow], list[RowError]]:
    """Four columns, header on row 1: Employee ID | Working Date | Status
    (OPEN/CONSUMED, blank = OPEN) | Consumed Date (required if CONSUMED).

    Resolves the employee and checks `viewer` may act for them
    (accounts.permissions.can_manage_cpl_grants) here, in the read-only
    pass, so the preview accurately reflects what apply will accept.
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
        User.objects.filter(employee_id__in=emp_ids).select_related("department", "team")
    }

    parsed: list[ParsedRow] = []
    errors: list[RowError] = []

    for idx, row in enumerate(body, start=2):
        if not row or all(_is_blank(c) for c in row):
            continue
        raw = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Working_Date": row[1] if len(row) > 1 else None,
            "Status": row[2] if len(row) > 2 else None,
            "Consumed_Date": row[3] if len(row) > 3 else None,
        }
        try:
            employee_id = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            errors.append(RowError(idx, raw, str(exc)))
            continue

        employee = employees_by_id.get(employee_id)
        if employee is None:
            errors.append(RowError(idx, raw, f"No employee with ID {employee_id}."))
            continue

        if not can_manage_cpl_grants(viewer, employee):
            errors.append(RowError(
                idx, raw,
                f"{employee.display_name} ({employee_id}) is outside your scope — "
                "you can't record CpL grants for them.",
            ))
            continue

        try:
            work_date = _coerce_date(row[1] if len(row) > 1 else None, label="Working Date")
        except ValueError as exc:
            errors.append(RowError(idx, raw, str(exc)))
            continue
        if work_date is None:
            errors.append(RowError(idx, raw, "Missing Working Date."))
            continue

        try:
            status = _coerce_status(row[2] if len(row) > 2 else None)
        except ValueError as exc:
            errors.append(RowError(idx, raw, str(exc)))
            continue

        try:
            consumed_date = _coerce_date(row[3] if len(row) > 3 else None, label="Consumed Date")
        except ValueError as exc:
            errors.append(RowError(idx, raw, str(exc)))
            continue
        if status == CplGrant.Status.CONSUMED and consumed_date is None:
            errors.append(RowError(idx, raw, "Status is CONSUMED but Consumed Date is blank."))
            continue

        parsed.append(ParsedRow(
            line_no=idx, employee_id=employee_id, employee=employee,
            work_date=work_date, status=status, consumed_date=consumed_date,
        ))

    return parsed, errors


def _process_rows(parsed_rows: list[ParsedRow], *, viewer, commit: bool) -> ApplyReport:
    report = ApplyReport(applied=commit)
    for row in parsed_rows:
        if commit:
            result = import_cpl_grant_row(
                row.employee_id, row.work_date, row.status, row.consumed_date, created_by=viewer,
            )
        else:
            # Preview: run the exact same writer, then roll back — what's
            # shown is guaranteed to match what apply would actually do.
            try:
                with transaction.atomic():
                    result = import_cpl_grant_row(
                        row.employee_id, row.work_date, row.status, row.consumed_date, created_by=viewer,
                    )
                    raise _Rollback()
            except _Rollback:
                pass

        raw = {
            "Employee_ID": row.employee_id, "Working_Date": str(row.work_date),
            "Status": row.status, "Consumed_Date": str(row.consumed_date or ""),
        }
        if result.get("skipped"):
            report.skipped.append(RowError(row.line_no, raw, result.get("reason", "already imported")))
        elif result.get("ok"):
            report.imported.append(row)
        else:
            report.apply_errors.append(RowError(row.line_no, raw, result.get("reason", "unknown error")))
    return report


def preview_upload(parsed_rows: list[ParsedRow], *, viewer) -> ApplyReport:
    return _process_rows(parsed_rows, viewer=viewer, commit=False)


def apply_upload(parsed_rows: list[ParsedRow], *, viewer) -> ApplyReport:
    return _process_rows(parsed_rows, viewer=viewer, commit=True)


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

def build_cpl_grant_upload_template() -> bytes:
    """Blank .xlsx template — Employee ID | Working Date | Status | Consumed
    Date — the same 4-column shape used throughout this session's staged
    CplGrant.xlsx files."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    headers = ["Employee ID", "Working Date", "Status", "Consumed Date"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)
    ws.append([None, None, "OPEN", None])
    ws.append([None, None, "CONSUMED", None])

    widths = {"A": 14, "B": 16, "C": 12, "D": 16}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()
