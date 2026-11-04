"""Register export — mirrors payroll/views.py::PayrollReportView._export_excel's shape."""

from __future__ import annotations

from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font

from django.http import HttpResponse

from core.exports import escape_spreadsheet, finalize_xlsx, money_value, set_money_columns
from payroll.models import PayComponent, PayrollRun, PayrollRunLine

REGISTER_COLUMNS = (
    "Employee ID", "Name", "Department", "Designation", "Status",
    "Payable days", "Night duty days",
    "Salary", "Night duty", "Overtime", "Transport", "Other",
    "Gross earnings", "Deductions", "Tax", "Net pay", "Warnings",
)

# Mirrors payroll/views.py::_EARNING_BREAKDOWN — the five earning columns sum
# to "Gross earnings", which already contains them.
_BREAKDOWN_CODES = ("GROSS_SALARY", "NIGHT_ALLOWANCE", "OT_ALLOWANCE", "SPECIAL_SHIFT_TRANSPORT")


def _earning_breakdown(run: PayrollRun) -> dict:
    """{run_employee_id: [salary, night, ot, transport, other]} in one query.

    Summed in Python, not with a SQL sum annotation: ``amount`` is
    encrypted at rest (docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.4).
    """
    breakdown: dict = {}
    rows = (
        PayrollRunLine.objects
        .filter(run_employee__run=run, component_kind=PayComponent.Kind.EARNING)
        .values_list("run_employee_id", "component_code", "amount")
    )
    for run_employee_id, code, amount in rows:
        amounts = breakdown.setdefault(run_employee_id, [Decimal("0")] * 5)
        try:
            index = _BREAKDOWN_CODES.index(code)
        except ValueError:
            index = 4  # anything else (BONUS/INCENTIVE) lands in "Other"
        amounts[index] += amount
    return breakdown


def build_register_export(run: PayrollRun) -> HttpResponse:
    breakdown = _earning_breakdown(run)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Payroll Register"
    worksheet.freeze_panes = "A2"
    worksheet.append(list(REGISTER_COLUMNS))
    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for re in run.run_employees.order_by("employee_code"):
        amounts = breakdown.get(re.pk, [Decimal("0")] * 5)
        worksheet.append([
            re.employee_code,
            escape_spreadsheet(re.full_name),
            escape_spreadsheet(re.department_name),
            escape_spreadsheet(re.designation_name),
            re.get_status_display(),
            float(re.payable_days),
            re.night_shift_count,
            *(money_value(a) for a in amounts),
            money_value(re.gross_earnings),
            money_value(re.total_deductions),
            money_value(re.tax_deducted),
            money_value(re.net_pay),
            "; ".join(re.calc_warnings) if re.calc_warnings else "",
        ])
    # Whole taka, accounting format, cells stay numeric (docs/MONEY_FORMAT_PLAN.md).
    set_money_columns(worksheet, range(8, 17))

    filename = f"payroll-register-{run.period.year}-{run.period.month:02d}.xlsx"
    return finalize_xlsx(workbook, filename, width_cap=40)


# ---------------------------------------------------------------------------
# Run-detail tab exports — added 2026-09-17. Each mirrors exactly what its
# tab renders on screen (same filtered/scoped data the view already built for
# that request, not a separate whole-run requery), unlike build_register_export
# above which is a standing full-run artifact regardless of the active filter.
# ---------------------------------------------------------------------------

def build_support_info_export(run, rows, ot_types) -> HttpResponse:
    """Support Info & Notes tab — same columns as PayrollReportView's own
    export (it's built on the same build_payroll_report()), plus the three
    Supervisor/HR/Finance note columns this tab adds on screen.
    """
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Support Info"
    worksheet.freeze_panes = "A2"

    headers = (
        [
            "Emp ID", "Name", "Designation", "Department", "Team", "Resource Status", "Billing Status",
            "Joining Date", "Job Location", "Work Location",
            "AL", "CL", "SL", "MTL", "GH", "CPL", "HDL", "LWP",
            "Night Reg.", "Night Custom", "Night Ded.", "Trainee nights (unpaid)", "Transport",
        ]
        + [f"OT: {ot.name}" for ot in ot_types]
        + ["Meal", "Supervisor Note", "HR Note", "Finance Note"]
    )
    worksheet.append(headers)
    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        notes = row.get("notes", {})
        worksheet.append([
            row.get("employee_id") or "",
            escape_spreadsheet(row.get("name") or ""),
            escape_spreadsheet(row.get("designation") or ""),
            escape_spreadsheet(row.get("department") or ""),
            escape_spreadsheet(row.get("team") or ""),
            row.get("resource_status") or "",
            row.get("billing_status") or "",
            row.get("joining_date").isoformat() if row.get("joining_date") else "",
            escape_spreadsheet(row.get("job_location") or ""),
            escape_spreadsheet(row.get("work_location") or ""),
            row["counts"]["AL"],
            row["counts"]["CL"],
            row["counts"]["SL"],
            row["counts"]["MTL"],
            row["counts"]["FL"],
            row["counts"]["CPL"],
            row["counts"]["HDL"],
            row["counts"]["LWP"],
            row["counts"]["NIGHT_REGULAR"],
            row["counts"]["NIGHT_CUSTOM"],
            row["counts"]["NIGHT_DEDICATED"],
            row["counts"]["NIGHT_UNPAID"],
            row["counts"]["TRANSPORT"],
            *row.get("ot_by_type", []),
            row.get("meal_count", 0),
            escape_spreadsheet(notes.get("SUPERVISOR", {}).get("text", "")),
            escape_spreadsheet(notes.get("HR", {}).get("text", "")),
            escape_spreadsheet(notes.get("FINANCE", {}).get("text", "")),
        ])

    filename = f"payroll-run-{run.period.year}-{run.period.month:02d}-support-info.xlsx"
    return finalize_xlsx(workbook, filename, width_cap=40)


def build_calculated_export(run, rows, extra_deduction_components, can_see_money, extra_earning_components=()) -> HttpResponse:
    """Calculated tab — same columns as the on-screen table, including the
    per-extra-deduction-component breakdown (unlike build_register_export's
    lump "Deductions"/"Other" totals). Money columns are omitted entirely for
    a viewer without can_see_money, same as the table itself.
    """
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Calculated"
    worksheet.freeze_panes = "A2"

    headers = ["Emp ID", "Name", "Department", "Payable days", "Night days"]
    if can_see_money:
        headers += ["Salary", "Night duty", "Overtime", "Transport"]
        headers += [c.short_name or c.name for c in extra_earning_components]
        headers += ["Gross", "Meal"]
        headers += [c.short_name or c.name for c in extra_deduction_components]
        headers += ["Total deductions", "Tax", "Net pay"]
    headers.append("Warnings")
    worksheet.append(headers)
    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for re in rows:
        values = [
            re.employee_code or "",
            escape_spreadsheet(re.full_name),
            escape_spreadsheet(re.department_name or ""),
            float(re.payable_days),
            re.night_shift_count,
        ]
        if can_see_money:
            values += [
                money_value(re.salary_amount), money_value(re.night_amount), money_value(re.ot_amount),
                money_value(re.transport_amount),
            ]
            values += [money_value(a) for a in getattr(re, "extra_earning_amounts", [])]
            values += [money_value(re.gross_earnings), money_value(re.meal_amount)]
            values += [money_value(a) for a in re.extra_deduction_amounts]
            values += [money_value(re.total_deductions), money_value(re.tax_deducted), money_value(re.net_pay)]
        values.append("; ".join(re.calc_warnings) if re.calc_warnings else "")
        worksheet.append(values)
    if can_see_money:
        money_count = 6 + len(extra_earning_components) + len(extra_deduction_components) + 3
        set_money_columns(worksheet, range(6, 6 + money_count))

    filename = f"payroll-run-{run.period.year}-{run.period.month:02d}-calculated.xlsx"
    return finalize_xlsx(workbook, filename, width_cap=40)


def build_overtime_export(entries, date_from, date_to) -> HttpResponse:
    """Overtime list (`/payroll/overtime/`) — mirrors the on-screen table
    exactly (payroll/overtime_list.html), minus the interactive checkbox and
    Actions columns. `entries` is the view's own filtered `get_queryset()`,
    unpaginated, so the export always covers everything the active filters
    matched, not just the current page — same convention as every other
    export in this module.
    """
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Overtime"
    worksheet.freeze_panes = "A2"

    worksheet.append([
        "Work date", "Emp ID", "Name", "Department", "Team", "Worked in",
        "Overtime type", "OT hours", "Period", "Status", "Added by",
    ])
    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for entry in entries:
        employee = entry.employee
        hours, remainder = divmod(entry.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        worksheet.append([
            entry.work_date.isoformat(),
            employee.employee_id or "",
            escape_spreadsheet(employee.display_name),
            escape_spreadsheet(employee.department.name) if employee.department_id else "",
            escape_spreadsheet(employee.team.name) if employee.team_id else "",
            escape_spreadsheet(entry.worked_in.name) if entry.worked_in_id else "",
            escape_spreadsheet(entry.overtime_type.name),
            f"{hours:02d}:{minutes:02d}:{seconds:02d}",
            str(entry.period),
            entry.get_status_display(),
            escape_spreadsheet(entry.added_by.display_name) if entry.added_by_id else "",
        ])

    filename = f"payroll-overtime-{date_from.isoformat()}-to-{date_to.isoformat()}.xlsx"
    return finalize_xlsx(workbook, filename, width_cap=30)


def build_exceptions_export(run, checks) -> HttpResponse:
    """Exceptions tab — Employee / Severity / Check / Message, matching the
    on-screen table exactly."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Exceptions"
    worksheet.freeze_panes = "A2"

    worksheet.append(["Employee", "Severity", "Check", "Message"])
    for cell in worksheet[1]:
        cell.font = Font(bold=True)

    for check in checks:
        worksheet.append([
            escape_spreadsheet(str(check.employee)) if check.employee else "",
            check.get_severity_display(),
            check.check_code,
            escape_spreadsheet(check.message or ""),
        ])

    filename = f"payroll-run-{run.period.year}-{run.period.month:02d}-exceptions.xlsx"
    return finalize_xlsx(workbook, filename, width_cap=40)
