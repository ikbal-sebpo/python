"""Roster bulk upload — wide/flat parser, applier, and template builder.

Wide layout (one row per employee, two columns per date — see docs/PLAN
and the plan file for the locked schema):

    Row 1: Employee ID | Employee Name | <date1> |        | <date2> |        | ...
    Row 2:             |               | Location | Shift  | Location | Shift  | ...
    Row 3: <emp_id>    | <name>        | <loc>    | <shift>| ...

Flat layout (one row per employee/date, compatible with Shift_Details_Flat.xlsx):

    Row 1: Date | EmployeeID | User Name | Location | Shift Status
    Row 2: 2026-08-19 | 51761 | md.rahman | MHK | EVE

The parser is read-only; the applier writes Shift rows inside one
transaction.atomic() and only if parse_errors is empty (all-or-nothing).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

import openpyxl
from dateutil import parser as date_parser
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel
from openpyxl.worksheet.datavalidation import DataValidation

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone

from accounts.locations import LOCATION_DROPDOWN_VALUES, resolve_location_choice
from accounts.permissions import visible_employees
from core.exports import escape_spreadsheet
from roster import night_window
from roster.models import Holiday, Shift, ShiftAttribute, ShiftChangeLog, ShiftType
from roster.services import shift_is_after_last_working_date, shift_is_before_joining_date


User = get_user_model()


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

LOCATION_TOKENS = sorted(LOCATION_DROPDOWN_VALUES)   # ('MBD','MHK','RAOWA','WFH')


@dataclass
class ParsedRow:
    line_no: int
    col_letter: str
    employee: object                 # CustomUser
    employee_id: str
    date: date
    shift_type: ShiftType
    shift_attribute: ShiftAttribute | None
    job_location: object | None      # JobLocation
    work_location: object | None     # WorkLocation
    is_holiday: bool = False


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
    skipped_leave: list[RowError] = field(default_factory=list)
    skipped_past_lwd: list[RowError] = field(default_factory=list)
    skipped_before_joining: list[RowError] = field(default_factory=list)
    skipped_night_window: list[RowError] = field(default_factory=list)
    overridden_leave: list[ParsedRow] = field(default_factory=list)
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
# Shift-name + location + employee_id + date resolution
# ---------------------------------------------------------------------------

def _build_shift_cache() -> dict[str, tuple[ShiftType, ShiftAttribute | None]]:
    """Two queries: every active ShiftAttribute (with parent type) and every
    active ShiftType with `has_attribute=False`. Returns a dict keyed by
    lowercased name. Attribute names win over type names on collision (the
    same precedence as `_resolve_shift_name`).
    """
    cache: dict[str, tuple[ShiftType, ShiftAttribute | None]] = {}
    for attr in (
        ShiftAttribute.objects
        .filter(is_active=True, shift_type__is_active=True, shift_type__isnull=False)
        .select_related("shift_type")
    ):
        cache[attr.name.lower()] = (attr.shift_type, attr)
    for st in ShiftType.objects.filter(is_active=True, has_attribute=False):
        cache.setdefault(st.name.lower(), (st, None))
    return cache


def _resolve_shift_name(
    name: str,
    cache: dict[str, tuple[ShiftType, ShiftAttribute | None]],
) -> tuple[ShiftType, ShiftAttribute | None]:
    """Resolve a shift cell value from the cache built by ``_build_shift_cache``."""
    if name is None or not str(name).strip():
        raise ValueError("Empty shift value.")
    key = str(name).strip().lower()
    hit = cache.get(key)
    if hit is not None:
        return hit
    # Cache miss: distinguish "type exists but needs a variant" from
    # "unknown name" so the error message stays useful.
    st = ShiftType.objects.filter(name__iexact=name, is_active=True).first()
    if st is not None and st.has_attribute:
        raise ValueError(
            f"Shift '{name}' requires a specific attribute (e.g. variant). "
            f"Pick one of its attribute names."
        )
    raise ValueError(f"Unknown shift name: '{str(name).strip()}'.")


def _build_location_cache() -> dict[str, tuple]:
    """Resolve every canonical dropdown token once (4 calls, ~6 queries total)
    and cache the FK pair. Saves ~3 queries per Shift cell on big uploads.
    """
    return {
        token.upper(): resolve_location_choice(token)
        for token in LOCATION_DROPDOWN_VALUES
    }


def _resolve_location_token(token: str | None, cache: dict[str, tuple]):
    """Empty token → (None, None). Valid token → cached FKs. Else ValueError."""
    if token is None:
        return None, None
    raw = str(token).strip()
    if not raw:
        return None, None
    upper = raw.upper()
    hit = cache.get(upper)
    if hit is None:
        raise ValueError(
            f"Unknown location '{raw}'. Expected one of {sorted(LOCATION_DROPDOWN_VALUES)}."
        )
    return hit


# Backwards-compatible wrappers — the bare helpers still work for the unit
# tests and any single-call path. They build a tiny cache on demand.
def _resolve_shift(name: str) -> tuple[ShiftType, ShiftAttribute | None]:
    return _resolve_shift_name(name, _build_shift_cache())


def _resolve_location(token: str | None):
    return _resolve_location_token(token, _build_location_cache())


def _coerce_employee_id(value) -> str:
    """xlsx may hand us int/float for numeric employee_id cells. CSV gives str.
    Normalise to a clean string suitable for `CustomUser.employee_id` lookup.
    """
    if value is None:
        raise ValueError("Empty employee_id.")
    if isinstance(value, bool):
        raise ValueError(f"Bad employee_id: {value!r}.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(
                f"Employee_id '{value}' looks like a decimal — expected an integer or text."
            )
        return str(int(value))
    s = str(value).strip()
    if not s:
        raise ValueError("Empty employee_id.")
    return s


def _parse_cell_date(raw, today: date) -> date:
    """Accept native Excel dates, Excel serial dates, and common text dates."""
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
        for fmt in (
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%m/%d/%Y",
            "%d/%m/%Y",
            "%m-%d-%Y",
            "%d-%m-%Y",
            "%d-%b-%Y",
            "%d %b %Y",
            "%b %d, %Y",
            "%B %d, %Y",
        ):
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
    if abs((parsed - today).days) > 365:
        raise ValueError(
            f"Date {parsed.isoformat()} is more than a year from today — likely a typo."
        )
    return parsed


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _looks_like_roster_sheet(ws) -> bool:
    """True when the sheet's first two rows match either upload layout:
    the grid (dates from C with Location/Shift sub-headers) or the flat one."""
    head = [list(r) for r in ws.iter_rows(min_row=1, max_row=2, values_only=True)]
    if not head:
        return False
    if _is_flat_header(head[0]):
        return True
    sub = {_norm(c) for c in (head[1] if len(head) > 1 else [])}
    return "location" in sub and "shift" in sub


def _pick_roster_sheet(wb):
    """Choose the worksheet to import: the sheet the workbook was saved on.

    Planners keep many weeks in one workbook, with hidden helper sheets
    (shift guide, lookups, an old copy) often sitting first, so
    worksheets[0] is the wrong guess. The active sheet is what the user was
    looking at; if it isn't roster-shaped we refuse rather than hunting
    through the other sheets for one that is — importing a week the user
    didn't pick would silently overwrite the wrong dates.
    """
    ws = wb.active or wb.worksheets[0]
    if not _looks_like_roster_sheet(ws):
        raise ValueError(
            f"The sheet '{ws.title.strip()}' (the one open when the file was saved) "
            "is not in roster format. Open the week you want to import, save the "
            "file, and upload it again."
        )
    return ws


def _read_xlsx_rows(file_obj) -> list[list]:
    """Return the active worksheet (see `_pick_roster_sheet`) as a list of
    rows; each row a list of cells. Cell values are kept native
    (datetime.date stays a date, int stays int)."""
    wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
    ws = _pick_roster_sheet(wb)
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append(list(row))
    return rows


def _read_csv_rows(file_obj) -> list[list]:
    """Decode and read CSV. Tolerate UTF-8 BOM."""
    raw = file_obj.read()
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw
    reader = csv.reader(io.StringIO(text))
    return [list(row) for row in reader]


def _load_rows(file_obj, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return _read_xlsx_rows(file_obj)
    if name.endswith(".csv"):
        return _read_csv_rows(file_obj)
    raise ValueError(
        f"Unsupported file type: {filename!r}. Upload an .xlsx or .csv file."
    )


# ---------------------------------------------------------------------------
# Header discovery
# ---------------------------------------------------------------------------

def _discover_date_columns(
    header_row: list,
    sub_header_row: list,
    today: date,
) -> list[tuple[date, int, int]]:
    """Walk header_row from index 2 (column C) onward. Every non-empty cell
    marks a date column whose right-hand neighbour is the Shift column.

    Returns a list of (date, location_col_idx, shift_col_idx) tuples.

    Raises ValueError with a header-level message if the structure is wrong:
    sub-headers must read 'Location' / 'Shift' under each date.
    """
    cols: list[tuple[date, int, int]] = []
    for idx in range(2, len(header_row)):
        cell = header_row[idx]
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            continue
        try:
            d = _parse_cell_date(cell, today)
        except ValueError as exc:
            col_letter = get_column_letter(idx + 1)
            raise ValueError(f"Header column {col_letter}: {exc}")
        shift_idx = idx + 1
        loc_label = _norm(sub_header_row[idx] if idx < len(sub_header_row) else None)
        shift_label = _norm(sub_header_row[shift_idx] if shift_idx < len(sub_header_row) else None)
        col_letter = get_column_letter(idx + 1)
        if loc_label != "location":
            raise ValueError(
                f"Sub-header under {col_letter} should be 'Location' (found {loc_label!r})."
            )
        if shift_label != "shift":
            shift_col = get_column_letter(shift_idx + 1)
            raise ValueError(
                f"Sub-header under {shift_col} should be 'Shift' (found {shift_label!r})."
            )
        cols.append((d, idx, shift_idx))
    if not cols:
        raise ValueError(
            "No date columns found. Row 1 should list dates from column C onwards "
            "with 'Location' and 'Shift' as sub-headers in row 2."
        )
    return cols


def _norm(value) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _norm_header(value) -> str:
    return "".join(ch for ch in _norm(value) if ch.isalnum())


def _is_flat_header(row: list) -> bool:
    labels = {_norm_header(cell) for cell in row}
    return (
        "date" in labels
        and "employeeid" in labels
        and ("shiftstatus" in labels or "trackercurrentshiftstatus" in labels)
    )


def _discover_flat_columns(header_row: list) -> dict[str, int]:
    columns = {}
    for idx, cell in enumerate(header_row):
        key = _norm_header(cell)
        if key == "date":
            columns["date"] = idx
        elif key == "employeeid":
            columns["employee_id"] = idx
        elif key in {"userid", "username"}:
            columns["user_name"] = idx
        elif key == "team":
            columns["team"] = idx
        elif key == "location":
            columns["location"] = idx
        elif key in {"shiftstatus", "trackercurrentshiftstatus"}:
            columns["shift"] = idx
    missing = [name for name in ("date", "employee_id", "shift") if name not in columns]
    if missing:
        raise ValueError(
            "Flat roster header is missing: "
            + ", ".join(missing)
            + ". Expected Date, EmployeeID, User Name, Location, Shift Status."
        )
    return columns


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_upload(
    file_obj,
    filename: str,
    *,
    today: date,
) -> tuple[list[ParsedRow], list[RowError], list[RowError]]:
    """Read the uploaded file and produce ParsedRow / RowError lists.

    Returns (parsed_rows, parse_errors, duplicate_errors). Read-only —
    no DB writes.

    Lookups (employee, shift, location, holiday) are built once up-front so
    parsing a 700-employee × 7-day file stays around ~6 queries total instead
    of ~25,000.
    """
    rows = _load_rows(file_obj, filename)
    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedRow] = []
    seen: dict[tuple[str, date], int] = {}     # (emp_id_str, date) → line_no of first sighting

    if not rows:
        raise ValueError("File is empty.")

    if _is_flat_header(rows[0]):
        return _parse_flat_rows(rows, today)

    if len(rows) < 2:
        raise ValueError(
            "File is empty or has only one row. Expected at least: date headers, "
            "Location/Shift sub-headers, and one employee row."
        )

    header_row = rows[0]
    sub_header_row = rows[1] if len(rows) > 1 else []
    date_cols = _discover_date_columns(header_row, sub_header_row, today)

    # --- Build lookup caches (5 queries total, regardless of file size) ---

    # 1. Tag holidays.
    all_dates = {d for d, _, _ in date_cols}
    holiday_dates = set(
        Holiday.objects.filter(date__in=all_dates, is_active=True)
                       .values_list("date", flat=True)
    )

    # 2. Pre-scan employee_id cells (best-effort — invalid values are silently
    #    skipped here and re-raised inside the main loop for error reporting).
    candidate_ids: set[str] = set()
    for raw_row in rows[2:]:
        if not raw_row:
            continue
        try:
            candidate_ids.add(_coerce_employee_id(raw_row[0]))
        except ValueError:
            pass

    # 3. One query for all employees referenced in the file.
    if candidate_ids:
        emp_cache = {
            u.employee_id: u
            for u in User.objects
                         .filter(is_active=True, employee_id__in=candidate_ids)
        }
    else:
        emp_cache = {}

    # 4. Shift names and location tokens.
    shift_cache = _build_shift_cache()
    location_cache = _build_location_cache()

    # --- Main loop: dict lookups only, zero queries per cell ---

    for line_no_zero, raw_row in enumerate(rows[2:], start=3):
        # line_no_zero is the 1-based file line (header rows are lines 1 and 2).
        emp_id_cell = raw_row[0] if len(raw_row) > 0 else None
        name_cell = raw_row[1] if len(raw_row) > 1 else None
        # Skip wholly-empty rows silently.
        if _is_blank(emp_id_cell) and all(_is_blank(raw_row[c]) for d, c, _ in date_cols if c < len(raw_row)):
            continue

        # Coerce + look up the employee.
        try:
            emp_id_str = _coerce_employee_id(emp_id_cell)
        except ValueError as exc:
            parse_errors.append(RowError(
                line_no=line_no_zero, col_letter="A",
                raw={"Employee ID": _safe_str(emp_id_cell), "Employee Name": _safe_str(name_cell)},
                message=str(exc),
            ))
            continue

        employee = emp_cache.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(
                line_no=line_no_zero, col_letter="A",
                raw={"Employee ID": emp_id_str, "Employee Name": _safe_str(name_cell)},
                message=f"No active employee with employee_id '{emp_id_str}'.",
            ))
            continue

        for d, loc_idx, shift_idx in date_cols:
            loc_cell = raw_row[loc_idx] if loc_idx < len(raw_row) else None
            shift_cell = raw_row[shift_idx] if shift_idx < len(raw_row) else None

            loc_blank = _is_blank(loc_cell)
            shift_blank = _is_blank(shift_cell)

            if loc_blank and shift_blank:
                # Manager left this (employee, date) untouched.
                continue

            shift_col_letter = get_column_letter(shift_idx + 1)
            raw_snapshot = {
                "Employee ID": emp_id_str,
                "Employee Name": _safe_str(name_cell),
                "Date": d.isoformat(),
                "Location": _safe_str(loc_cell),
                "Shift": _safe_str(shift_cell),
            }

            if shift_blank:
                parse_errors.append(RowError(
                    line_no=line_no_zero, col_letter=shift_col_letter,
                    raw=raw_snapshot,
                    message=f"Location filled but Shift is empty on {d.isoformat()}.",
                ))
                continue

            # Resolve shift name (dict lookup).
            try:
                shift_type, shift_attr = _resolve_shift_name(shift_cell, shift_cache)
            except ValueError as exc:
                parse_errors.append(RowError(
                    line_no=line_no_zero, col_letter=shift_col_letter,
                    raw=raw_snapshot, message=str(exc),
                ))
                continue

            # Resolve location (dict lookup).
            try:
                jl, wl = _resolve_location_token(loc_cell, location_cache)
            except ValueError as exc:
                loc_col_letter = get_column_letter(loc_idx + 1)
                parse_errors.append(RowError(
                    line_no=line_no_zero, col_letter=loc_col_letter,
                    raw=raw_snapshot, message=str(exc),
                ))
                continue

            # Duplicate (employee, date) within file?
            key = (emp_id_str, d)
            if key in seen:
                dup_errors.append(RowError(
                    line_no=line_no_zero, col_letter=shift_col_letter,
                    raw=raw_snapshot,
                    message=(
                        f"Duplicate of row {seen[key]} — same employee_id {emp_id_str} "
                        f"and date {d.isoformat()} appears twice."
                    ),
                ))
                continue
            seen[key] = line_no_zero

            parsed.append(ParsedRow(
                line_no=line_no_zero, col_letter=shift_col_letter,
                employee=employee, employee_id=emp_id_str, date=d,
                shift_type=shift_type, shift_attribute=shift_attr,
                job_location=jl, work_location=wl,
                is_holiday=(d in holiday_dates),
            ))

    return parsed, parse_errors, dup_errors


def _parse_flat_rows(
    rows: list[list],
    today: date,
) -> tuple[list[ParsedRow], list[RowError], list[RowError]]:
    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    parsed: list[ParsedRow] = []
    seen: dict[tuple[str, date], int] = {}
    columns = _discover_flat_columns(rows[0])

    candidate_ids: set[str] = set()
    all_dates: set[date] = set()
    for raw_row in rows[1:]:
        if not raw_row or all(_is_blank(cell) for cell in raw_row):
            continue
        try:
            candidate_ids.add(_coerce_employee_id(_row_value(raw_row, columns["employee_id"])))
        except ValueError:
            pass
        try:
            all_dates.add(_parse_cell_date(_row_value(raw_row, columns["date"]), today))
        except ValueError:
            pass

    emp_cache = (
        {
            u.employee_id: u
            for u in User.objects
                         .filter(is_active=True, employee_id__in=candidate_ids)
        }
        if candidate_ids else {}
    )
    holiday_dates = set(
        Holiday.objects.filter(date__in=all_dates, is_active=True)
                       .values_list("date", flat=True)
    )
    shift_cache = _build_shift_cache()
    shift_type_cache = {
        st.name.lower(): st for st in ShiftType.objects.filter(is_active=True)
    }
    location_cache = _build_location_cache()

    for line_no, raw_row in enumerate(rows[1:], start=2):
        if not raw_row or all(_is_blank(cell) for cell in raw_row):
            continue

        date_cell = _row_value(raw_row, columns["date"])
        emp_id_cell = _row_value(raw_row, columns["employee_id"])
        user_name_cell = _row_value(raw_row, columns.get("user_name"))
        team_cell = _row_value(raw_row, columns.get("team"))
        loc_cell = _row_value(raw_row, columns.get("location"))
        shift_cell = _row_value(raw_row, columns["shift"])

        raw_snapshot = {
            "Date": _safe_str(date_cell),
            "EmployeeID": _safe_str(emp_id_cell),
            "User Name": _safe_str(user_name_cell),
            "Location": _safe_str(loc_cell),
            "Shift Status": _safe_str(shift_cell),
        }
        if columns.get("team") is not None:
            raw_snapshot["Team"] = _safe_str(team_cell)

        try:
            d = _parse_cell_date(date_cell, today)
        except ValueError as exc:
            parse_errors.append(RowError(
                line_no=line_no,
                col_letter=get_column_letter(columns["date"] + 1),
                raw=raw_snapshot,
                message=str(exc),
            ))
            continue

        try:
            emp_id_str = _coerce_employee_id(emp_id_cell)
        except ValueError as exc:
            parse_errors.append(RowError(
                line_no=line_no,
                col_letter=get_column_letter(columns["employee_id"] + 1),
                raw=raw_snapshot,
                message=str(exc),
            ))
            continue

        employee = emp_cache.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(
                line_no=line_no,
                col_letter=get_column_letter(columns["employee_id"] + 1),
                raw=raw_snapshot,
                message=f"No active employee with employee_id '{emp_id_str}'.",
            ))
            continue

        if _is_blank(shift_cell):
            continue

        try:
            jl, wl = _resolve_location_token(loc_cell, location_cache)
        except ValueError as exc:
            parse_errors.append(RowError(
                line_no=line_no,
                col_letter=get_column_letter(columns.get("location", columns["shift"]) + 1),
                raw=raw_snapshot,
                message=str(exc),
            ))
            continue

        try:
            shift_type, shift_attr = _resolve_flat_shift_name(
                shift_cell,
                shift_cache,
                shift_type_cache,
            )
        except ValueError as exc:
            parse_errors.append(RowError(
                line_no=line_no,
                col_letter=get_column_letter(columns["shift"] + 1),
                raw=raw_snapshot,
                message=str(exc),
            ))
            continue

        key = (emp_id_str, d)
        if key in seen:
            dup_errors.append(RowError(
                line_no=line_no,
                col_letter=get_column_letter(columns["shift"] + 1),
                raw=raw_snapshot,
                message=(
                    f"Duplicate of row {seen[key]} — same employee_id {emp_id_str} "
                    f"and date {d.isoformat()} appears twice."
                ),
            ))
            continue
        seen[key] = line_no

        parsed.append(ParsedRow(
            line_no=line_no,
            col_letter=get_column_letter(columns["shift"] + 1),
            employee=employee,
            employee_id=emp_id_str,
            date=d,
            shift_type=shift_type,
            shift_attribute=shift_attr,
            job_location=jl,
            work_location=wl,
            is_holiday=(d in holiday_dates),
        ))

    return parsed, parse_errors, dup_errors


def _resolve_flat_shift_name(
    name: str,
    cache: dict[str, tuple[ShiftType, ShiftAttribute | None]],
    type_cache: dict[str, ShiftType],
) -> tuple[ShiftType, ShiftAttribute | None]:
    try:
        return _resolve_shift_name(name, cache)
    except ValueError as exc:
        raw = "" if name is None else str(name).strip()
        st = type_cache.get(raw.lower())
        if st is not None:
            return st, None
        raise exc


def _row_value(row: list, idx: int | None):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _safe_str(value) -> str:
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------

def apply_upload(
    viewer,
    parsed_rows: list[ParsedRow],
    viewer_visible_pks: set,
    *,
    override_leave: bool = False,
) -> ApplyReport:
    """Apply parsed rows to the Shift table inside one transaction.

    Rows whose employee is outside `viewer_visible_pks` are not written; they
    appear in `report.skipped_scope` instead.

    Rows for a date past the employee's `last_working_date` are likewise not
    written, and appear in `report.skipped_past_lwd`. This is a hard rule, not
    an overridable one (unlike the leave protection below) — a leaver has no
    roster past their last day, and letting an upload write one is exactly how
    stale post-LWD shifts got into the system in the first place.

    Rows for a date before the employee's `joining_date` are likewise a hard,
    non-overridable rule, and appear in `report.skipped_before_joining` — the
    mirror image of the LWD rule above: nobody has a roster before they were
    hired.

    By default, a row whose (employee, date) already carries an approved-leave
    Shift (LEAVE category) is left untouched and reported in
    `report.skipped_leave` — once a leave is approved, its materialised Shift
    wins over any later roster set. Pass `override_leave=True` to force this
    upload's values through anyway (e.g. a historical backload correcting
    roster data entered as a placeholder before the real leave was known);
    each such row is written normally and also listed in
    `report.overridden_leave` so the override is visible in the result.

    Implementation: one pre-query to learn which (employee, date) pairs already
    exist, then a single ``bulk_create(update_conflicts=True)`` to insert new
    rows and overwrite existing ones in one round-trip. The 700-employee × 7-day
    case settles in ~3 queries total instead of ~10,000.

    Safety: ``bulk_create`` bypasses ``Model.save()``, so the parent-type sync
    that ``Shift.save()`` does (line 228) is skipped. That's fine because
    ``parse_upload`` already populates ``row.shift_type`` from the attribute's
    parent — the invariant is maintained at the source.

    ``bulk_create`` also bypasses signals entirely, so the `ShiftChangeLog`
    audit trail (normally populated by `roster/signals.py`'s pre_save/
    post_save hooks on `Shift.save()`) gets built by hand here instead, in
    the same bulk fashion — one extra pre-fetch of the previous field values
    (folded into the existing_pairs/leave_locked query below) and one extra
    post-write query to learn the new/kept rows' pks, both fixed-count
    regardless of batch size, same as the rest of this function.
    """
    report = ApplyReport()

    in_scope: list[ParsedRow] = []
    for row in parsed_rows:
        if row.employee.pk in viewer_visible_pks:
            in_scope.append(row)
        else:
            report.skipped_scope.append(RowError(
                line_no=row.line_no, col_letter="A",
                raw={
                    "Employee ID": row.employee_id,
                    "Date": row.date.isoformat(),
                    "Shift": row.shift_type.name,
                },
                message="Outside your managed scope.",
            ))

    if not in_scope:
        report.applied = True   # nothing to do, but the upload itself didn't error
        return report

    # Find which (employee, date) pairs already exist so we can report the
    # create-vs-update split, and snapshot their current field values for the
    # audit log below. One query for the whole batch. We also flag pairs that
    # already carry an approved-leave shift so the upload doesn't clobber
    # them — once a leave is approved, its materialised Shift wins over any
    # later roster set (the upload reports these as skipped instead).
    emp_pks = {r.employee.pk for r in in_scope}
    dates = {r.date for r in in_scope}
    existing_pairs = set()
    leave_locked = set()
    previous_by_key = {}
    for row in (
        Shift.objects
        .filter(employee_id__in=emp_pks, date__in=dates)
        .values(
            "employee_id", "date", "shift_type_id", "shift_attribute_id",
            "job_location_id", "work_location_id", "note", "shift_type__category",
        )
    ):
        key = (row["employee_id"], row["date"])
        existing_pairs.add(key)
        previous_by_key[key] = row
        if row["shift_type__category"] == ShiftType.Category.LEAVE:
            leave_locked.add(key)

    # Dedicated night shift windows (roster/night_window.py): inside an
    # APPROVED window only NIG/CUST or a non-working type is written unless
    # the uploader is that employee's department head. One query.
    windows = night_window.windows_by_employee(emp_pks, min(dates), max(dates))
    approver_cache: dict = {}

    now = timezone.now()
    objs: list[Shift] = []
    log_intents: list[dict] = []
    _WATCHED = ("shift_type_id", "shift_attribute_id", "job_location_id", "work_location_id", "note")
    for r in in_scope:
        key = (r.employee.pk, r.date)
        if shift_is_after_last_working_date(r.employee, r.date):
            report.skipped_past_lwd.append(RowError(
                line_no=r.line_no, col_letter="A",
                raw={
                    "Employee ID": r.employee_id,
                    "Date": r.date.isoformat(),
                    "Shift": r.shift_type.name,
                },
                message=(
                    f"Past last working date "
                    f"({r.employee.last_working_date.isoformat()}) — not written."
                ),
            ))
            continue
        if shift_is_before_joining_date(r.employee, r.date):
            report.skipped_before_joining.append(RowError(
                line_no=r.line_no, col_letter="A",
                raw={
                    "Employee ID": r.employee_id,
                    "Date": r.date.isoformat(),
                    "Shift": r.shift_type.name,
                },
                message=(
                    f"Before joining date "
                    f"({r.employee.joining_date.isoformat()}) — not written."
                ),
            ))
            continue
        if (
            night_window.is_restricted_shift_type(r.shift_type)
            and night_window.date_in_windows(windows, r.employee.pk, r.date)
        ):
            if r.employee.pk not in approver_cache:
                approver_cache[r.employee.pk] = night_window.is_night_window_approver(
                    viewer, r.employee,
                )
            if not approver_cache[r.employee.pk]:
                report.skipped_night_window.append(RowError(
                    line_no=r.line_no, col_letter="A",
                    raw={
                        "Employee ID": r.employee_id,
                        "Date": r.date.isoformat(),
                        "Shift": r.shift_type.name,
                    },
                    message=(
                        "Dedicated night shift window — only NIG/CUST; other "
                        "working shifts need the department head."
                    ),
                ))
                continue
        if key in leave_locked and not override_leave:
            report.skipped_leave.append(RowError(
                line_no=r.line_no, col_letter="A",
                raw={
                    "Employee ID": r.employee_id,
                    "Date": r.date.isoformat(),
                    "Shift": r.shift_type.name,
                },
                message="Approved leave already set for this date — not overwritten.",
            ))
            continue
        if key in leave_locked:
            report.overridden_leave.append(r)
        objs.append(Shift(
            employee=r.employee, date=r.date,
            shift_type=r.shift_type,
            shift_attribute=r.shift_attribute,
            job_location=r.job_location,
            work_location=r.work_location,
            # added_* are only written on INSERT (not in update_fields) so
            # they stay untouched when an existing row is overwritten.
            added_by=viewer, added_at=now,
            updated_by=viewer, updated_at=now,
        ))
        new_values = {
            "shift_type_id": r.shift_type.id,
            "shift_attribute_id": r.shift_attribute.id if r.shift_attribute else None,
            "job_location_id": r.job_location.id if r.job_location else None,
            "work_location_id": r.work_location.id if r.work_location else None,
            "note": "",
        }
        previous = previous_by_key.get(key)
        if previous is not None and all(previous[f] == new_values[f] for f in _WATCHED):
            pass  # no-op overwrite — nothing actually changed, skip logging.
        else:
            log_intents.append({"key": key, "previous": previous, "new": new_values})

        if key in existing_pairs:
            report.updated.append(r)
        else:
            report.created.append(r)

    if not objs:
        report.applied = True
        return report

    # `unique_fields` is required by SQLite/PostgreSQL but rejected by MySQL
    # (MySQL's ON DUPLICATE KEY UPDATE matches on any unique constraint
    # automatically). Use the connection feature flag to pick the right shape.
    bulk_kwargs: dict = {
        "update_conflicts": True,
        "update_fields": [
            "shift_type", "shift_attribute",
            "job_location", "work_location",
            "updated_by", "updated_at",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        bulk_kwargs["unique_fields"] = ["employee", "date"]

    with transaction.atomic():
        Shift.objects.bulk_create(objs, **bulk_kwargs)

        if log_intents:
            # bulk_create doesn't populate pks back onto `objs` for an
            # update_conflicts upsert (ambiguous insert-vs-update), so
            # re-fetch the (employee, date) → pk mapping in one query.
            shift_pks = {
                (eid, d): pk for eid, d, pk in
                Shift.objects.filter(employee_id__in=emp_pks, date__in=dates)
                .values_list("employee_id", "date", "id")
            }
            log_objs = []
            for intent in log_intents:
                key = intent["key"]
                previous, new = intent["previous"], intent["new"]
                log_objs.append(ShiftChangeLog(
                    shift_id=shift_pks.get(key),
                    employee_id=key[0], date=key[1],
                    action_type=(
                        ShiftChangeLog.ActionType.UPDATED if previous is not None
                        else ShiftChangeLog.ActionType.CREATED
                    ),
                    previous_shift_type_id=previous["shift_type_id"] if previous else None,
                    previous_shift_attribute_id=previous["shift_attribute_id"] if previous else None,
                    previous_job_location_id=previous["job_location_id"] if previous else None,
                    previous_work_location_id=previous["work_location_id"] if previous else None,
                    previous_note=previous["note"] if previous else "",
                    new_shift_type_id=new["shift_type_id"],
                    new_shift_attribute_id=new["shift_attribute_id"],
                    new_job_location_id=new["job_location_id"],
                    new_work_location_id=new["work_location_id"],
                    new_note=new["note"],
                    changed_by=viewer, changed_at=now,
                ))
            ShiftChangeLog.objects.bulk_create(log_objs)

    report.applied = True
    return report


# ---------------------------------------------------------------------------
# Template builders
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE_DAYS = 7


def _lookup_shift_names() -> list[str]:
    """Every value that the parser would accept in the Shift column."""
    attr_names = list(
        ShiftAttribute.objects
        .filter(is_active=True, shift_type__is_active=True, shift_type__isnull=False)
        .order_by(
            F("shift_type__sort_order").asc(nulls_last=True),
            "shift_type__name",
            F("sort_order").asc(nulls_last=True),
            "name",
        )
        .values_list("name", flat=True)
    )
    type_names = list(
        ShiftType.objects
        .filter(is_active=True, has_attribute=False)
        .order_by(F("sort_order").asc(nulls_last=True), "name")
        .values_list("name", flat=True)
    )
    # Dedupe while preserving order — attributes first, then leftover types.
    seen = set()
    out: list[str] = []
    for n in attr_names + type_names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


SHIFT_GUIDE_COLORS = {
    ShiftType.Category.WORK: "DDEFEF",
    ShiftType.Category.OFF: "E8EEF3",
    ShiftType.Category.LEAVE: "FFF2CC",
    ShiftType.Category.HOLIDAY: "DDEBFF",
}
SHIFT_GUIDE_COLORS_ALT = {
    ShiftType.Category.WORK: "EEF7F7",
    ShiftType.Category.OFF: "F4F7FA",
    ShiftType.Category.LEAVE: "FFF8E5",
    ShiftType.Category.HOLIDAY: "EEF5FF",
}
SHIFT_GUIDE_ACCENTS = {
    ShiftType.Category.WORK: "41B3B3",
    ShiftType.Category.OFF: "8A9AA6",
    ShiftType.Category.LEAVE: "FFC638",
    ShiftType.Category.HOLIDAY: "006F99",
}
SHIFT_GUIDE_ACCENT_TEXT = {
    ShiftType.Category.LEAVE: "0A1A1A",
}


def _format_time(value) -> str:
    return value.strftime("%H:%M") if value else ""


def _shift_type_description(st: ShiftType) -> str:
    if st.description:
        return st.description
    if st.category == ShiftType.Category.WORK:
        if st.shift_start_time and st.shift_end_time:
            return (
                f"Regular working shift from {_format_time(st.shift_start_time)} "
                f"to {_format_time(st.shift_end_time)}. Use when this is the employee's "
                "planned duty window and no more specific shift attribute is required."
            )
        return "Regular working shift. Use when the employee is scheduled to work for this roster date."
    if st.category == ShiftType.Category.OFF:
        return "Scheduled weekly off or rest day. Use when the employee is not expected to work."
    if st.category == ShiftType.Category.LEAVE:
        return "Leave roster status. Usually produced by approved leave, or used when HR needs to mark an approved absence."
    if st.category == ShiftType.Category.HOLIDAY:
        return "Company or public holiday status. Use only when the day should be treated as a holiday roster cell."
    return "Roster status available for assignment."


def _shift_attribute_description(attr: ShiftAttribute) -> str:
    if attr.description:
        return attr.description
    if attr.start_time and attr.end_time:
        return (
            f"Specific {attr.shift_type.name if attr.shift_type_id else 'shift'} variant "
            f"from {_format_time(attr.start_time)} to {_format_time(attr.end_time)}. "
            "Use when the employee needs this exact start/end time."
        )
    return (
        f"Specific variant of {attr.shift_type.name if attr.shift_type_id else 'the parent shift'}. "
        "Use when this option best matches the employee's planned schedule."
    )


def _shift_guide_rows() -> list[dict]:
    rows: list[dict] = []
    for st in (
        ShiftType.objects
        .filter(is_active=True)
        .order_by(F("sort_order").asc(nulls_last=True), "name")
    ):
        rows.append({
            "value": st.name,
            "category": st.get_category_display(),
            "parent": "",
            "time": (
                f"{_format_time(st.shift_start_time)}-{_format_time(st.shift_end_time)}"
                if st.shift_start_time and st.shift_end_time else ""
            ),
            "description": _shift_type_description(st),
            "fill": SHIFT_GUIDE_COLORS.get(st.category, "FFFFFF"),
            "fill_alt": SHIFT_GUIDE_COLORS_ALT.get(st.category, "FFFFFF"),
            "accent": SHIFT_GUIDE_ACCENTS.get(st.category, "0A4958"),
            "accent_text": SHIFT_GUIDE_ACCENT_TEXT.get(st.category, "FFFFFF"),
        })
    for attr in (
        ShiftAttribute.objects
        .filter(is_active=True, shift_type__is_active=True, shift_type__isnull=False)
        .select_related("shift_type")
        .order_by(
            F("shift_type__sort_order").asc(nulls_last=True),
            "shift_type__name",
            F("sort_order").asc(nulls_last=True),
            "start_time",
            "name",
        )
    ):
        rows.append({
            "value": attr.name,
            "category": attr.shift_type.get_category_display(),
            "parent": attr.shift_type.name,
            "time": (
                f"{_format_time(attr.start_time)}-{_format_time(attr.end_time)}"
                if attr.start_time and attr.end_time else ""
            ),
            "description": _shift_attribute_description(attr),
            "fill": SHIFT_GUIDE_COLORS.get(attr.shift_type.category, "FFFFFF"),
            "fill_alt": SHIFT_GUIDE_COLORS_ALT.get(attr.shift_type.category, "FFFFFF"),
            "accent": SHIFT_GUIDE_ACCENTS.get(attr.shift_type.category, "0A4958"),
            "accent_text": SHIFT_GUIDE_ACCENT_TEXT.get(attr.shift_type.category, "FFFFFF"),
        })
    return rows


def _add_shift_guide_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("Shift Guide")
    ws.sheet_view.showGridLines = False
    title = ws.cell(row=1, column=1, value="Shift Guide")
    title.font = Font(bold=True, size=16, color="0A4958")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    ws.cell(
        row=2,
        column=1,
        value=(
            "Use the exact value from the Shift Value column in the roster template. "
            "Shift Attributes are specific time variants; Shift Types are broader statuses."
        ),
    )
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=5)
    ws.cell(row=2, column=1).alignment = Alignment(wrap_text=True)

    headers = ["Shift Value", "Category", "Parent Type", "Time", "Description"]
    header_fill = PatternFill("solid", fgColor="0A4958")
    thin_side = Side(style="thin", color="CAD5DB")
    medium_side = Side(style="medium", color="FFFFFF")
    row_border = Border(bottom=thin_side)
    for col, label in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col, value=label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border = Border(bottom=Side(style="medium", color="41B3B3"))

    for row_idx, row in enumerate(_shift_guide_rows(), start=5):
        fill = PatternFill(
            "solid",
            fgColor=row["fill"] if row_idx % 2 else row["fill_alt"],
        )
        values = [
            row["value"],
            row["category"],
            row["parent"],
            row["time"],
            row["description"],
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col, value=value)
            cell.fill = fill
            cell.border = row_border
            cell.alignment = Alignment(
                horizontal="center" if col in (2, 4) else "left",
                vertical="top",
                wrap_text=(col == 5),
            )
        accent = ws.cell(row=row_idx, column=1)
        accent.fill = PatternFill("solid", fgColor=row["accent"])
        accent.font = Font(bold=True, color=row["accent_text"])
        accent.border = Border(left=medium_side, bottom=thin_side)
        ws.cell(row=row_idx, column=2).font = Font(bold=True, color="0A4958")
        ws.row_dimensions[row_idx].height = 42

    widths = {
        "A": 24,
        "B": 18,
        "C": 18,
        "D": 14,
        "E": 72,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:E{max(ws.max_row, 4)}"


def _template_employees(viewer):
    """Active employees within the viewer's scope, ordered for display."""
    qs = (
        visible_employees(viewer, request=None)
        .filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .select_related("team")
        .order_by("first_name", "last_name")
    )
    return list(qs)


def _template_dates(today: date | None = None) -> list[date]:
    today = today or date.today()
    return [today + timedelta(days=i) for i in range(DEFAULT_TEMPLATE_DAYS)]


def build_xlsx_template(viewer) -> bytes:
    """Generate the prefilled .xlsx template with dropdowns on Shift + Location."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Roster"

    employees = _template_employees(viewer)
    dates = _template_dates()
    shift_names = _lookup_shift_names()

    # Row 1: Employee ID | Employee Name | <date> | (empty) | <date> | (empty) | ...
    ws.cell(row=1, column=1, value="Employee ID")
    ws.cell(row=1, column=2, value="Employee Name")
    for i, d in enumerate(dates):
        col = 3 + i * 2
        ws.cell(row=1, column=col, value=d.isoformat())

    # Row 2: empty | empty | Location | Shift | Location | Shift | ...
    for i, _ in enumerate(dates):
        loc_col = 3 + i * 2
        shift_col = loc_col + 1
        ws.cell(row=2, column=loc_col, value="Location")
        ws.cell(row=2, column=shift_col, value="Shift")

    # Rows 3+: one per employee, employee_id as text (keeps leading zeros).
    for ridx, emp in enumerate(employees, start=3):
        eid_cell = ws.cell(row=ridx, column=1, value=emp.employee_id or "")
        eid_cell.number_format = "@"
        ws.cell(row=ridx, column=2, value=escape_spreadsheet(emp.display_name))

    # Lookups sheet (hidden) — backs the data validation lists.
    lookups = wb.create_sheet("Lookups")
    lookups.cell(row=1, column=1, value="Shift values")
    lookups.cell(row=1, column=2, value="Locations")
    for i, name in enumerate(shift_names, start=2):
        lookups.cell(row=i, column=1, value=name)
    for i, token in enumerate(LOCATION_TOKENS, start=2):
        lookups.cell(row=i, column=2, value=token)
    lookups.sheet_state = "hidden"

    # Data validations — only attach if we have at least one employee row and
    # at least one shift name; otherwise Excel rejects an empty list reference.
    if employees and dates:
        last_row = 2 + len(employees)
        if shift_names:
            shift_range = (
                f"=Lookups!$A$2:$A${1 + len(shift_names)}"
            )
            shift_dv = DataValidation(
                type="list", formula1=shift_range, allow_blank=True,
            )
            shift_dv.error = "Pick a value from the dropdown."
            shift_dv.errorTitle = "Invalid shift"
            ws.add_data_validation(shift_dv)
            for i, _ in enumerate(dates):
                shift_col = 4 + i * 2
                col_letter = get_column_letter(shift_col)
                shift_dv.add(f"{col_letter}3:{col_letter}{last_row}")

        loc_range = f"=Lookups!$B$2:$B${1 + len(LOCATION_TOKENS)}"
        loc_dv = DataValidation(
            type="list", formula1=loc_range, allow_blank=True,
        )
        loc_dv.error = "Pick a value from the dropdown."
        loc_dv.errorTitle = "Invalid location"
        ws.add_data_validation(loc_dv)
        for i, _ in enumerate(dates):
            loc_col = 3 + i * 2
            col_letter = get_column_letter(loc_col)
            loc_dv.add(f"{col_letter}3:{col_letter}{last_row}")

    # Cosmetic: column widths + freeze panes.
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 26
    for i, _ in enumerate(dates):
        loc_col = 3 + i * 2
        shift_col = loc_col + 1
        ws.column_dimensions[get_column_letter(loc_col)].width = 12
        ws.column_dimensions[get_column_letter(shift_col)].width = 14
    ws.freeze_panes = "C3"
    _add_shift_guide_sheet(wb)

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def build_flat_xlsx_template(viewer) -> bytes:
    """Generate a flat .xlsx template: one row per employee/date."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Shift Details Flat"

    employees = _template_employees(viewer)
    dates = _template_dates()
    shift_names = _lookup_shift_names()

    headers = ["Date", "EmployeeID", "User Name", "Location", "Shift Status"]
    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    row_idx = 2
    for d in dates:
        for emp in employees:
            date_cell = ws.cell(row=row_idx, column=1, value=d)
            date_cell.number_format = "yyyy-mm-dd"
            eid_cell = ws.cell(row=row_idx, column=2, value=emp.employee_id or "")
            eid_cell.number_format = "@"
            ws.cell(row=row_idx, column=3, value=escape_spreadsheet(emp.display_name))
            row_idx += 1

    lookups = wb.create_sheet("Lookups")
    lookups.cell(row=1, column=1, value="Shift values")
    lookups.cell(row=1, column=2, value="Locations")
    for i, name in enumerate(shift_names, start=2):
        lookups.cell(row=i, column=1, value=name)
    for i, token in enumerate(LOCATION_TOKENS, start=2):
        lookups.cell(row=i, column=2, value=token)
    lookups.sheet_state = "hidden"

    if row_idx > 2 and shift_names:
        shift_range = f"=Lookups!$A$2:$A${1 + len(shift_names)}"
        shift_dv = DataValidation(
            type="list", formula1=shift_range, allow_blank=True,
        )
        shift_dv.error = "Pick a value from the dropdown."
        shift_dv.errorTitle = "Invalid shift"
        ws.add_data_validation(shift_dv)
        shift_dv.add(f"E2:E{row_idx - 1}")

        loc_range = f"=Lookups!$B$2:$B${1 + len(LOCATION_TOKENS)}"
        loc_dv = DataValidation(
            type="list", formula1=loc_range, allow_blank=True,
        )
        loc_dv.error = "Pick a value from the dropdown."
        loc_dv.errorTitle = "Invalid location"
        ws.add_data_validation(loc_dv)
        loc_dv.add(f"D2:D{row_idx - 1}")

    widths = {
        "A": 14,
        "B": 14,
        "C": 22,
        "D": 12,
        "E": 18,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    _add_shift_guide_sheet(wb)

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def build_csv_template(viewer) -> str:
    """Plain-text wide-format CSV — same schema as the xlsx, no dropdowns."""
    employees = _template_employees(viewer)
    dates = _template_dates()

    out = io.StringIO()
    writer = csv.writer(out)

    # Row 1: Employee ID | Employee Name | <date> | "" | <date> | "" | ...
    header = ["Employee ID", "Employee Name"]
    for d in dates:
        header.extend([d.isoformat(), ""])
    writer.writerow(header)

    # Row 2: "" | "" | Location | Shift | Location | Shift | ...
    sub = ["", ""]
    for _ in dates:
        sub.extend(["Location", "Shift"])
    writer.writerow(sub)

    # Rows 3+: one row per employee, date cells blank.
    for emp in employees:
        row = [emp.employee_id or "", escape_spreadsheet(emp.display_name)]
        for _ in dates:
            row.extend(["", ""])
        writer.writerow(row)

    return out.getvalue()


def _export_shift_value(shift) -> str:
    """The value the parser would accept for this shift — attribute name wins
    over the parent type name (same precedence as ``_resolve_shift_name``)."""
    if shift.shift_attribute_id and shift.shift_attribute:
        return shift.shift_attribute.name
    return shift.shift_type.name


def build_xlsx_export(employees, days, shift_map) -> bytes:
    """Export the currently-viewed roster grid as a filled .xlsx.

    Same wide layout as ``build_xlsx_template`` (Employee ID / Name, then two
    columns per date), but the Location + Shift cells carry each employee's
    actual shift for that day — so the file round-trips straight back through
    the uploader. ``shift_map`` is keyed ``(employee_id, date) -> Shift``.
    """
    from accounts.locations import resolve_to_dropdown_choice

    wb = Workbook()
    ws = wb.active
    ws.title = "Roster"

    # Row 1: Employee ID | Employee Name | <date> | (empty) | ...
    ws.cell(row=1, column=1, value="Employee ID")
    ws.cell(row=1, column=2, value="Employee Name")
    for i, d in enumerate(days):
        ws.cell(row=1, column=3 + i * 2, value=d.isoformat())

    # Row 2: empty | empty | Location | Shift | ...
    for i, _ in enumerate(days):
        loc_col = 3 + i * 2
        ws.cell(row=2, column=loc_col, value="Location")
        ws.cell(row=2, column=loc_col + 1, value="Shift")

    # Rows 3+: one per employee, filled from shift_map.
    for ridx, emp in enumerate(employees, start=3):
        eid_cell = ws.cell(row=ridx, column=1, value=emp.employee_id or "")
        eid_cell.number_format = "@"
        ws.cell(row=ridx, column=2, value=escape_spreadsheet(emp.display_name))
        for i, d in enumerate(days):
            shift = shift_map.get((emp.pk, d))
            if shift is None:
                continue
            loc_col = 3 + i * 2
            location = resolve_to_dropdown_choice(
                shift.job_location, shift.work_location,
            )
            if location:
                ws.cell(row=ridx, column=loc_col, value=location)
            ws.cell(row=ridx, column=loc_col + 1, value=_export_shift_value(shift))

    # Cosmetic: column widths + freeze panes (match the template builder).
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 26
    for i, _ in enumerate(days):
        loc_col = 3 + i * 2
        ws.column_dimensions[get_column_letter(loc_col)].width = 12
        ws.column_dimensions[get_column_letter(loc_col + 1)].width = 14
    ws.freeze_panes = "C3"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def build_flat_xlsx_export(shifts) -> bytes:
    """Export the flat (tracker) view as a filled .xlsx: one row per Shift.

    Same headers as ``build_flat_xlsx_template`` plus Team (the flat parser
    ignores it), so the file round-trips straight back through the uploader.
    The flat view's range is uncapped, so this streams in write-only mode
    rather than holding a cell grid for every row.
    """
    from accounts.locations import resolve_to_dropdown_choice
    from openpyxl.cell import WriteOnlyCell

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Shift Details Flat")
    for col, width in {"A": 14, "B": 14, "C": 26, "D": 22, "E": 12, "F": 18}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    ws.append(["Date", "EmployeeID", "User Name", "Team", "Location", "Shift Status"])

    for shift in shifts.iterator(chunk_size=2000):
        emp = shift.employee
        date_cell = WriteOnlyCell(ws, value=shift.date)
        date_cell.number_format = "yyyy-mm-dd"
        eid_cell = WriteOnlyCell(ws, value=emp.employee_id or "")
        eid_cell.number_format = "@"
        ws.append([
            date_cell,
            eid_cell,
            escape_spreadsheet(emp.display_name),
            escape_spreadsheet(emp.team.name) if emp.team_id else "",
            resolve_to_dropdown_choice(shift.job_location, shift.work_location) or "",
            _export_shift_value(shift),
        ])

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def _xlsx_attachment(body: bytes, filename: str) -> HttpResponse:
    resp = HttpResponse(
        body,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def render_flat_export_response(shifts) -> HttpResponse:
    """Wrap ``build_flat_xlsx_export`` in an attachment response."""
    return _xlsx_attachment(
        build_flat_xlsx_export(shifts), f"roster-flat-{date.today().isoformat()}.xlsx",
    )


def render_export_response(employees, days, shift_map) -> HttpResponse:
    """Wrap ``build_xlsx_export`` in an attachment response."""
    body = build_xlsx_export(employees, days, shift_map)
    stamp = date.today().isoformat()
    resp = HttpResponse(
        body,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    resp["Content-Disposition"] = f'attachment; filename="roster-{stamp}.xlsx"'
    return resp


def render_template_response(viewer, fmt: str) -> HttpResponse:
    fmt = (fmt or "").lower()
    today_stamp = date.today().isoformat()
    if fmt == "xlsx":
        body = build_xlsx_template(viewer)
        resp = HttpResponse(
            body,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="roster-upload-template-{today_stamp}.xlsx"'
        )
        return resp
    if fmt in {"flat-xlsx", "xlsx-flat"}:
        body = build_flat_xlsx_template(viewer)
        resp = HttpResponse(
            body,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="roster-upload-flat-template-{today_stamp}.xlsx"'
        )
        return resp
    if fmt == "csv":
        body = build_csv_template(viewer)
        resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="roster-upload-template-{today_stamp}.csv"'
        )
        return resp
    from django.http import Http404
    raise Http404("Unknown template format.")
