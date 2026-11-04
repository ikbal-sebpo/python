"""Spreadsheet-export helpers shared by every CSV/XLSX writer."""

from io import BytesIO

from openpyxl.utils import get_column_letter

from django.http import HttpResponse

from core.money import EXCEL_NUMBER_FORMAT, to_whole_taka

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_spreadsheet(value):
    """Neutralise CSV/Excel formula injection for user-controlled text.

    A cell beginning with = + - @ (or tab/CR) executes as a formula when the
    export opens in Excel/LibreOffice — display names, titles, and other
    profile fields are user/Graph-controlled, so prefix them with a literal
    apostrophe (rendered invisibly by spreadsheet apps). Non-strings pass
    through untouched.

    Don't apply this to columns a round-trip importer must parse back
    (shift/location tokens in the roster export) — those are validated
    against server-side whitelists on import instead.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def finalize_xlsx(workbook, filename, width_cap=30, header_row=1) -> HttpResponse:
    """Auto-size the active sheet's columns and wrap the workbook in a
    download response — the shared tail of every openpyxl export
    (payroll/exports.py, employees/exports.py, attendance/exports.py,
    leave/exports.py).

    `width_cap` bounds a column at that many characters so one long free-text
    cell (a reason, a warning list) can't blow the sheet out sideways.
    `header_row` is the column-header row; anything above it (a title row)
    is ignored when sizing, so a long title doesn't widen column A.
    """
    worksheet = workbook.active
    for column_cells in worksheet.columns:
        values = [str(cell.value or "") for cell in column_cells if cell.row >= header_row]
        worksheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(
            max(len(value) for value in values) + 2, width_cap,
        )

    return xlsx_response(workbook, filename)


def xlsx_response(workbook, filename) -> HttpResponse:
    """Save `workbook` into a download response — `finalize_xlsx`'s tail, on
    its own for write-only workbooks, which size columns before any row is
    written and so can't be auto-sized after the fact."""
    content = BytesIO()
    workbook.save(content)
    response = HttpResponse(content.getvalue(), content_type=XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def money_value(amount):
    """The numeric cell value for a money amount: whole taka as an int (or
    None for blank). Keep cells numeric — never a pre-formatted string — so
    Finance can sum a column in Excel; pair with ``set_money_columns``."""
    return to_whole_taka(amount)


def set_money_columns(worksheet, columns, *, first_row=2, last_row=None) -> None:
    """Apply the accounting number format (``#,##0``) to the given 1-based
    column indexes from ``first_row`` down to ``last_row`` (default: the
    sheet's last row). Call once after the rows are appended."""
    last_row = last_row or worksheet.max_row
    if last_row < first_row:
        return
    for col in columns:
        for row in range(first_row, last_row + 1):
            worksheet.cell(row=row, column=col).number_format = EXCEL_NUMBER_FORMAT
