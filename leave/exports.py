"""Shift-summary export for `/leave/reports/shift-summary/?export=xlsx` —
mirrors the on-screen table (leave/shift_summary.html) plus a Department
column, under a title row carrying the report's date range. Same shape as
attendance/exports.py::build_timesheet_export: `rows` is the view's own
fully-filtered, unpaginated `build_shift_summary` result, so the download
covers everything the active filters matched, not just the current page.
"""

from __future__ import annotations

import re
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font

from django.http import HttpResponse

from core.exports import escape_spreadsheet, finalize_xlsx

# (header, row key) in on-screen order, Department inserted after Name.
SHIFT_SUMMARY_COLUMNS = (
    ("Employee ID", "employee_id"),
    ("Name", "employee_name"),
    ("Department", "department"),
    ("Team", "team"),
    ("Days worked", "days_worked"),
    ("OFF", "OFF"),
    ("CL", "CL"),
    ("SL", "SL"),
    ("AL", "AL"),
    ("LWP", "LWP"),
    ("MTL", "MTL"),
    ("GH", "GH"),
    ("CPL", "CPL"),
    # On screen the cycle caveat is only a hover tooltip, which Excel loses.
    ("AL allocated (current cycle)", "al_allocated"),
    ("AL used (current cycle)", "al_cycle_used"),
    ("AL balance (current cycle)", "al_cycle_balance"),
    ("CL allocated", "cl_allocated"),
    ("SL allocated", "sl_allocated"),
    ("Total allocated", "total_leave_allocated"),
    ("Taken", "total_leave_taken"),
    ("Remaining", "leave_remaining"),
)
_TEXT_KEYS = {"employee_id", "employee_name", "department", "team"}

# Day counts: whole numbers show as `12`, half-days as `2.5`.
_DAYS_FORMAT = "0.##"
_TITLE_ROW, _HEADER_ROW = 1, 2


def _slug(label: str) -> str:
    """Filename-safe scope label: 'Digital Advertising / Bench' → 'digital-advertising-bench'."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label or "").strip("-").lower()
    return slug or "all"


def _days(value):
    """Keep the cell numeric so HR can sum it; `_clean_days` hands back Decimals."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value or 0


def build_shift_summary_export(rows, start_date, end_date, scope_label: str) -> HttpResponse:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Shift summary"

    worksheet.append([
        f"Shift summary · {scope_label or 'All departments'} · "
        f"{start_date.isoformat()} to {end_date.isoformat()}"
    ])
    worksheet.cell(row=_TITLE_ROW, column=1).font = Font(bold=True, size=12)

    worksheet.append([header for header, _ in SHIFT_SUMMARY_COLUMNS])
    for cell in worksheet[_HEADER_ROW]:
        cell.font = Font(bold=True)
    worksheet.freeze_panes = f"A{_HEADER_ROW + 1}"

    for row in rows:
        worksheet.append([
            escape_spreadsheet(row.get(key) or "") if key in _TEXT_KEYS
            # .get: the template hardcodes these leave codes, but the builder
            # only emits keys for LEAVE ShiftTypes that actually exist.
            else _days(row.get(key, 0))
            for _, key in SHIFT_SUMMARY_COLUMNS
        ])

    first_numeric = len(_TEXT_KEYS) + 1
    for excel_row in worksheet.iter_rows(min_row=_HEADER_ROW + 1, min_col=first_numeric):
        for cell in excel_row:
            cell.number_format = _DAYS_FORMAT

    filename = (
        f"shift-summary-{_slug(scope_label)}-{start_date.isoformat()}-to-{end_date.isoformat()}.xlsx"
    )
    return finalize_xlsx(workbook, filename, width_cap=30, header_row=_HEADER_ROW)
