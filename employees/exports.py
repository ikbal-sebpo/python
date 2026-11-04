"""Employee list export — mirrors payroll/exports.py::build_register_export's shape."""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font

from django.http import HttpResponse

from core.exports import escape_spreadsheet, finalize_xlsx

EMPLOYEE_COLUMNS = ("Employee ID", "Name", "Email")


def _employee_id_sort_key(employee):
    """Numeric sort on employee_id (a CharField) — a plain string sort would
    put "1044" before "539". Blank/non-numeric ids sort to the end."""
    employee_id = employee.employee_id or ""
    return (0, int(employee_id)) if employee_id.isdigit() else (1, employee_id)


def build_employee_export(queryset) -> HttpResponse:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Employees"
    worksheet.freeze_panes = "A2"
    worksheet.append(list(EMPLOYEE_COLUMNS))
    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    employees = sorted(queryset, key=_employee_id_sort_key)
    for employee in employees:
        worksheet.append([
            employee.employee_id or "",
            escape_spreadsheet(employee.display_name),
            employee.email,
        ])

    return finalize_xlsx(workbook, "employees.xlsx", width_cap=40)
