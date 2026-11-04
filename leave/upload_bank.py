"""Historical AL-balance backfill — one-time bulk import.

Long-tenured employees have a current, real AL balance tracked externally
in an HR file today (e.g. one employee is already at exactly the 60-day
cap). This uploader seeds that as `LeaveBalance.opening` on the employee's
*current* anniversary cycle, via `leave/services.py::get_or_create_balance`
— `opening` is otherwise unused by AL (only FIXED_ANNUAL policies set it),
so it's a clean, non-colliding landing spot for "the balance HR already
tracked outside Orbis."

Structurally mirrors payroll/upload.py's salary-upload section: two
columns (Employee ID | Current AL Balance), a read-only parse pass, and an
apply pass that writes an auditable ledger entry rather than a silent
field write. Deliberately one-shot — a re-upload for an employee who
already has an opening-import transaction is blocked (report as
skipped_existing); later corrections go through a manual ADJUSTMENT via
admin, not a silent re-import that could double-credit a lifetime balance.
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
from leave.models import LeaveBalanceTransaction, LeavePolicy
from leave.services import get_or_create_balance
from roster.models import ShiftType


User = get_user_model()

AL_BALANCE_CAP = Decimal("60")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ParsedBankRow:
    line_no: int
    employee: object   # CustomUser
    employee_id: str
    banked_days: Decimal


@dataclass
class RowError:
    line_no: int
    col_letter: str | None
    raw: dict
    message: str


@dataclass
class BankApplyReport:
    created: list[ParsedBankRow] = field(default_factory=list)
    skipped_existing: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created)


def _get_al_policy() -> LeavePolicy:
    return (
        LeavePolicy.objects
        .select_related("shift_type")
        .get(shift_type__name="AL", shift_type__category=ShiftType.Category.LEAVE)
    )


# ---------------------------------------------------------------------------
# Cell coercion + file reading (same idioms as payroll/upload.py)
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


def _parse_al_balance(raw) -> Decimal:
    if raw is None:
        raise ValueError("Empty AL balance.")
    if isinstance(raw, bool):
        raise ValueError(f"Bad AL balance: {raw!r}.")
    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("Empty AL balance.")
        try:
            value = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"Bad AL balance '{raw}' — expected a number.")
    if value < 0:
        raise ValueError("AL balance can't be negative.")
    if value > AL_BALANCE_CAP:
        raise ValueError(f"AL balance {value} exceeds the {AL_BALANCE_CAP}-day lifetime cap.")
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


# ---------------------------------------------------------------------------
# Parse (read-only) + apply
# ---------------------------------------------------------------------------

def parse_bank_upload(
    file_obj, filename: str,
) -> tuple[list[ParsedBankRow], list[RowError], list[RowError]]:
    """Two columns, header on row 1: Employee ID | Current AL Balance (days)."""
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
        u.employee_id: u for u in User.objects.filter(employee_id__in=emp_ids, is_active=True)
    }

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedBankRow] = []
    seen: dict[str, int] = {}

    for idx, row in enumerate(body, start=2):
        if all(_is_blank(c) for c in row):
            continue
        raw_snapshot = {
            "Employee_ID": row[0] if len(row) > 0 else None,
            "Current_AL_Balance": row[1] if len(row) > 1 else None,
        }
        try:
            emp_id_str = _coerce_employee_id(row[0] if len(row) > 0 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "A", raw_snapshot, str(exc)))
            continue
        employee = employees_by_id.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(idx, "A", raw_snapshot, f"No active employee with ID '{emp_id_str}'."))
            continue
        try:
            banked_days = _parse_al_balance(row[1] if len(row) > 1 else None)
        except ValueError as exc:
            parse_errors.append(RowError(idx, "B", raw_snapshot, str(exc)))
            continue

        if emp_id_str in seen:
            dup_errors.append(RowError(
                idx, "A", raw_snapshot,
                f"Duplicate of row {seen[emp_id_str]} — this employee appears twice in the file.",
            ))
            continue
        seen[emp_id_str] = idx

        parsed.append(ParsedBankRow(
            line_no=idx, employee=employee, employee_id=emp_id_str, banked_days=banked_days,
        ))

    return parsed, parse_errors, dup_errors


def apply_bank_upload(viewer, parsed_rows: list[ParsedBankRow]) -> BankApplyReport:
    """Seed each row's `LeaveBalance.opening` (current AL cycle) via an
    ADJUSTMENT ledger transaction. One-shot per employee — blocked if an
    opening-import transaction already exists for their current cycle.
    """
    report = BankApplyReport()
    if not parsed_rows:
        report.applied = True
        return report

    al_policy = _get_al_policy()
    today = date.today()

    for r in parsed_rows:
        with transaction.atomic():
            bal = get_or_create_balance(r.employee, al_policy, today)
            already_imported = LeaveBalanceTransaction.objects.filter(
                balance=bal,
                reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
                note__startswith="Historical AL bank import",
            ).exists()
            if already_imported:
                report.skipped_existing.append(RowError(
                    r.line_no, "A",
                    {"Employee_ID": r.employee_id, "Current_AL_Balance": str(r.banked_days)},
                    "Already has an opening-balance import for the current cycle — "
                    "use a manual ADJUSTMENT via admin to correct it.",
                ))
                continue

            delta = r.banked_days - bal.opening
            LeaveBalanceTransaction.objects.create(
                balance=bal,
                delta=delta,
                reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
                created_by=viewer,
                note=f"Historical AL bank import — opening set to {r.banked_days}",
            )
            bal.opening = r.banked_days
            bal.save(update_fields=["opening", "updated_at"])
        report.created.append(r)

    report.applied = True
    return report


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

def build_bank_xlsx_template() -> bytes:
    """Prefilled .xlsx template — Employee ID | Current AL Balance (days),
    plus a reference-only Employee Name column, mirroring
    payroll/upload.py::build_salary_xlsx_template's shape."""
    wb = Workbook()
    ws = wb.active
    ws.title = "AL Bank"

    employees = list(
        User.objects.filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField()))
        .order_by("employee_id_num")
    )

    headers = ["Employee ID", "Current AL Balance (days)", "Employee Name"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    row_idx = 2
    for emp in employees:
        eid_cell = ws.cell(row=row_idx, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        ws.cell(row=row_idx, column=3, value=escape_spreadsheet(emp.display_name))
        row_idx += 1

    if row_idx > 2:
        bal_dv = DataValidation(
            type="decimal", operator="between",
            formula1="0", formula2=str(AL_BALANCE_CAP), allow_blank=True,
        )
        bal_dv.error = f"Enter a number between 0 and {AL_BALANCE_CAP}."
        bal_dv.errorTitle = "Invalid AL balance"
        ws.add_data_validation(bal_dv)
        bal_dv.add(f"B2:B{row_idx - 1}")

    widths = {"A": 14, "B": 24, "C": 26}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()
