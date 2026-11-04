"""Timesheet export for `/attendance/?export=xlsx` — mirrors the on-screen
table exactly (attendance/list.html + _timesheet_cells.html), minus the
interactive Actions column. Same shape as payroll/exports.py::
build_overtime_export: `rows` is the view's own fully-filtered,
unpaginated row list (`attendance.views._build_timesheet_rows`), so the
download always covers everything the active filters matched, not just the
current page.
"""

from __future__ import annotations

import re

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from django.http import HttpResponse

from core.exports import escape_spreadsheet, xlsx_response

# Same bound as `core.exports.finalize_xlsx(width_cap=30)`.
_WIDTH_CAP = 30

TIMESHEET_COLUMNS = (
    "Date", "Day", "Emp ID", "Name", "Department", "Team", "Designation",
    "Shift", "Office", "Punch in", "Punch out", "Time in office", "Status",
)


def _slug(label: str) -> str:
    """Filename-safe scope label: 'Digital Advertising / Bench' → 'digital-advertising-bench'."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label or "").strip("-").lower()
    return slug or "all"


def _hhmm(value) -> str:
    """Punch timestamps as HH:MM in the active timezone, matching the
    template's `|time:"H:i"` — the date is already its own column."""
    if value is None:
        return ""
    from django.utils import timezone

    return timezone.localtime(value).strftime("%H:%M")


def _duration(value) -> str:
    """A `DurationField` as HH:MM:SS, matching the template's default render."""
    if value is None:
        return ""
    total = int(value.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_timesheet_export(rows, date_from, date_to, scope_label: str) -> HttpResponse:
    """Write-only workbook: an org-wide payroll period is ~45k rows, and the
    regular in-memory sheet plus `finalize_xlsx`'s cell-by-cell auto-size
    took most of the request. Widths come from a pass over the values
    instead, since a write-only sheet can't be sized after rows are written.
    """
    values = [_row_values(row) for row in rows]
    widths = [len(header) for header in TIMESHEET_COLUMNS]
    for row_values in values:
        for index, value in enumerate(row_values):
            widths[index] = max(widths[index], len(str(value or "")))

    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Timesheet")
    worksheet.freeze_panes = "A2"
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = min(width + 2, _WIDTH_CAP)

    bold = Font(bold=True)
    header = []
    for label in TIMESHEET_COLUMNS:
        cell = WriteOnlyCell(worksheet, value=label)
        cell.font = bold
        header.append(cell)
    worksheet.append(header)
    for row_values in values:
        worksheet.append(row_values)

    filename = (
        f"timesheet-{_slug(scope_label)}-{date_from.isoformat()}-to-{date_to.isoformat()}.xlsx"
    )
    return xlsx_response(workbook, filename)


def _row_values(row) -> list:
    employee = row.employee
    punch = row.punch
    return [
        row.date.isoformat(),
        row.date.strftime("%a"),
        employee.employee_id or "",
        escape_spreadsheet(employee.display_name),
        escape_spreadsheet(employee.department.name) if employee.department_id else "",
        escape_spreadsheet(employee.team.name) if employee.team_id else "",
        escape_spreadsheet(str(employee.designation)) if employee.designation_id else "",
        escape_spreadsheet(row.shift_label),
        punch.office_bucket if punch else "",
        _hhmm(punch.check_in) if punch else "",
        _hhmm(punch.check_out) if punch else "",
        _duration(punch.time_in_office) if punch else "",
        row.status_label,
    ]
