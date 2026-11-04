import json
from collections import defaultdict
from datetime import date as _date
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import ListView
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from django.db import transaction
from django.db.models import Count, IntegerField, Prefetch, Q
from django.db.models.functions import Cast

from accounts.filters import employee_filter_context, employee_search_q
from accounts.models import CustomUser, Department, Team
from core.approvals import APPROVALS_CHANGED_EVENT
from core.exports import escape_spreadsheet
from core.pagination import PageSizeMixin, paginate_list
from accounts.permissions import (
    can_edit_gross_salary,
    can_bulk_upload_payroll_notes,
    can_create_payroll_run,
    can_act_on_variable_pay,
    _is_overtime_global_approver,
    can_approve_overtime,
    can_manage_locked_overtime,
    can_review_overtime,
    has_overtime_approval_scope,
    has_overtime_review_scope,
    can_manage_payroll_calculation,
    can_see_all_departments,
    can_see_payroll_money,
    can_submit_tax_declaration,
    can_view_tax_declaration_document,
    can_write_payroll_note,
    effective_department_id,
    payroll_visible_employee_pks,
    visible_departments,
    visible_teams,
)
from dynamic_menu.decorators import dynamic_allowed_users
from payroll import services
from payroll.engine import calculate_run
from payroll.exports import (
    build_calculated_export, build_exceptions_export, build_overtime_export,
    build_register_export, build_support_info_export,
)
from payroll.forms import (
    OvertimeEntryForm, PayrollRunCreateForm, PayrollRunEmployeeAdjustForm, SalaryRevisionForm,
    TaxDeclarationReviewForm, VariablePayEntryEditForm,
)
from payroll.models import (
    EmployeeTaxDeclaration,
    OvertimeEntry, OvertimeType, PayComponent, PayrollControlCheck, PayrollPeriod, PayrollRun,
    PayrollRunEmployee, PayrollRunEmployeeNote, PayrollRunLine, SalaryStructure, TDSEntry,
    VariablePayEntry,
)
from payroll.gh_overtime import (
    DEFAULT_BPO_MIN_HOURS, DEFAULT_GRANT_HOURS, DEFAULT_MIN_HOURS,
    generate_bpo_gh_overtime_for_date, generate_gh_overtime_for_date,
)
from payroll.ttms import TTMSUnavailable
from payroll.periods import period_bounds, period_for_date
from payroll.reports import (
    PayrollFilters,
    build_payroll_report,
    build_payroll_report_rows,
    payroll_report_employees,
    payroll_report_ot_types,
    validate_payroll_report_range,
)
from payroll.services import OvertimeWindow
from payroll.upload_views import ManagedScopeRequiredMixin


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollReportView(LoginRequiredMixin, View):
    """Payroll Support Info, in two requests so the page never blocks on it.

    A plain GET renders only the shell (filters + validation errors). The
    results area then fetches the same URL via htmx (target
    ``#payroll-report-results``), which paginates the employee list before
    building anything — the night-shift classification and the meals API
    call no longer hold up the page itself. ``?export=xlsx`` builds every
    row. Same shape as leave.views.ShiftSummaryView.
    """

    template_name = "payroll/report.html"
    results_template_name = "payroll/_report_results.html"
    results_target = "payroll-report-results"
    export_columns = (
        ("Emp ID", "employee_id"),
        ("Name", "name"),
        ("Designation", "designation"),
        ("Department", "department"),
        ("Team", "team"),
        ("Resource Status", "resource_status"),
        ("Billing Status", "billing_status"),
        ("Joining Date", "joining_date"),
        ("Job Location", "job_location"),
        ("Work Location", "work_location"),
    )
    # OT columns aren't in this tuple — one column per OvertimeType actually
    # present in the report's scope, appended dynamically (see
    # build_payroll_report's ot_types / _export_excel below).
    export_count_columns = (
        "AL", "CL", "SL", "MTL", "FL", "CPL", "HDL", "LWP",
        "NIGHT_REGULAR", "NIGHT_CUSTOM", "NIGHT_DEDICATED", "Trainee nights (unpaid)", "TRANSPORT",
    )

    def get(self, request):
        today = _date.today()
        start_date = parse_date(request.GET.get("start_date") or today.replace(day=1).isoformat())
        end_date = parse_date(request.GET.get("end_date") or today.isoformat())

        # Locked (non-privileged) users are forced to their own department; the
        # team is clamped to a team within that department.
        department_id = effective_department_id(request.user, request)
        team_id = None
        raw_team = (request.GET.get("team") or "").strip()
        if raw_team.isdigit() and visible_teams(
            request.user, request=request, department_id=department_id,
        ).filter(pk=raw_team).exists():
            team_id = int(raw_team)
        q = (request.GET.get("q") or "").strip()

        errors = []
        if not start_date or not end_date:
            errors.append("Valid Start and End dates are required.")
        else:
            try:
                validate_payroll_report_range(start_date, end_date)
            except ValueError as exc:
                errors.append(str(exc))

        filters = None if errors else PayrollFilters(
            start_date=start_date,
            end_date=end_date,
            department_id=department_id,
            team_id=team_id,
            q=q,
        )
        # Payroll Admins get org-wide scope here specifically —
        # visible_employees() only grants that to "User Admins"/
        # "HR" (accounts.permissions.FULL_VISIBILITY_GROUPS),
        # not "Payroll Admins", which would otherwise scope a
        # real Payroll Admin down to just themselves (no
        # reports of their own). Everyone else — including
        # plain "Payroll" group supervisors — keeps the normal
        # self+reportees scoping (confirmed 2026-09-10: a
        # supervisor should stay scoped to their own reportees).
        viewer = None if can_see_payroll_money(request.user, request=request) else request.user

        if request.GET.get("export") == "xlsx" and filters:
            report_data = build_payroll_report(filters, viewer=viewer, request=request)
            return self._export_excel(
                report_data["rows"], report_data["ot_types"], start_date, end_date,
            )

        if self._is_results_request(request):
            ctx = {"errors": errors, "ot_types": [], "meal_counts_unavailable": False}
            if filters:
                employees = payroll_report_employees(filters, viewer=viewer, request=request)
                # OT columns come from the whole scope so they stay put while
                # paging; everything per-employee is built for this page only.
                ot_types = payroll_report_ot_types(filters, [e.pk for e in employees])
                ctx.update(paginate_list(request, employees))
                report_data = build_payroll_report_rows(ctx["rows"], filters, ot_types)
                ctx.update(
                    rows=report_data["rows"],
                    ot_types=ot_types,
                    meal_counts_unavailable=report_data["meal_counts_unavailable"],
                )
            return render(request, self.results_template_name, ctx)

        departments = visible_departments(request.user, request=request)
        # Every pickable team, not just this department's: the template's team
        # <select> is narrowed client-side off each option's data-department
        # (base.html::wireDepartmentTeamCascade). A submitted team is still
        # re-checked against the chosen department above.
        teams = visible_teams(request.user, request=request)

        # Job / work location are report *columns*, not filters — the pair was
        # dropped from the filter bar (2026-09-06) because department + team +
        # the date range is the whole question this report is asked.
        ctx = {
            "page_title": "Payroll Support Info",
            "departments": departments,
            "teams": teams,
            "department_id": department_id or "",
            "team_id": team_id or "",
            "q": q,
            "can_pick_all_departments": can_see_all_departments(request.user, request=request),
            "start_date": start_date,
            "end_date": end_date,
            "errors": errors,
            "results_target": self.results_target,
        }
        return render(request, self.template_name, ctx)

    def _is_results_request(self, request) -> bool:
        """The htmx fetch of the results area (initial load or a pager link) —
        not a history-restore miss, which needs the whole page back."""
        htmx = getattr(request, "htmx", None)
        return bool(htmx) and not htmx.history_restore_request and htmx.target == self.results_target

    def _export_excel(self, rows, ot_types, start_date, end_date):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Payroll Report"
        worksheet.freeze_panes = "A2"

        headers = (
            [label for label, _ in self.export_columns]
            + list(self.export_count_columns)
            + [f"OT: {ot.name}" for ot in ot_types]
            + ["Meal"]
        )
        worksheet.append(headers)

        for cell in worksheet[1]:
            cell.font = Font(bold=True)

        for row in rows:
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
            ])

        for column_cells in worksheet.columns:
            values = [str(cell.value or "") for cell in column_cells]
            worksheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(
                max(len(value) for value in values) + 2,
                24,
            )

        content = BytesIO()
        workbook.save(content)
        content.seek(0)

        filename = f"payroll-report-{start_date.isoformat()}-to-{end_date.isoformat()}.xlsx"
        response = HttpResponse(
            content.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


# ---------------------------------------------------------------------------
# Payroll runs / register (Phase 3)
# ---------------------------------------------------------------------------

@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunListView(PageSizeMixin, LoginRequiredMixin, ListView):
    model = PayrollRun
    template_name = "payroll/run_list.html"
    context_object_name = "runs"
    paginate_by = 25

    def get_queryset(self):
        return PayrollRun.objects.select_related("period").order_by("-period__year", "-period__month", "-added_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_create_run"] = can_create_payroll_run(self.request.user, self.request)
        if ctx["can_create_run"]:
            today = _date.today()
            year, month = period_for_date(today)
            current_start, current_end = period_bounds(year, month)
            ctx["create_run_form"] = PayrollRunCreateForm(
                initial={"start_date": current_start, "end_date": current_end},
            )

        # Non-monetary run-list stats — batched per page (one query per
        # metric across all runs shown), never one query per row.
        runs = ctx["runs"]
        run_ids = [r.pk for r in runs]
        period_ids = [r.period_id for r in runs]

        pending_ot_map = dict(
            OvertimeEntry.objects.filter(period_id__in=period_ids, status__in=OvertimeEntry.UNAPPROVED_STATUSES)
            .values("period_id").annotate(n=Count("id")).values_list("period_id", "n")
        )
        check_rows = (
            PayrollControlCheck.objects.filter(run_id__in=run_ids, status=PayrollControlCheck.Status.OPEN)
            .values("run_id", "severity").annotate(n=Count("id"))
        )
        blocking_map, warning_map = {}, {}
        for row in check_rows:
            bucket = blocking_map if row["severity"] == PayrollControlCheck.Severity.BLOCKING else warning_map
            bucket[row["run_id"]] = row["n"]

        for run in runs:
            run.night_total = run.night_regular_total + run.night_custom_total + run.night_dedicated_total
            run.pending_ot_count = pending_ot_map.get(run.period_id, 0)
            run.open_blocking_check_count = blocking_map.get(run.pk, 0)
            run.open_warning_check_count = warning_map.get(run.pk, 0)
            run.ot_hours_by_type = {
                name: round(seconds / 3600, 1) for name, seconds in run.ot_seconds_by_type_total.items()
            }
            run.ot_hours_total = round(sum(run.ot_seconds_by_type_total.values()) / 3600, 1)

        return ctx

    def post(self, request, *args, **kwargs):
        """Create a run for the chosen [from, to] range (defaults to the
        current standard 16th->15th cycle) and open it. A range that isn't
        an exact standard cycle becomes a CUSTOM period — e.g. a shorter
        Eid-month special run — see ``PayrollPeriod.for_range``.

        HR-Payroll or Payroll Admins only, per the real workflow's first
        step — re-checked here even though the button is already hidden
        from everyone else in the template."""
        if not can_create_payroll_run(request.user, request):
            raise PermissionDenied("You can't create a payroll run.")
        form = PayrollRunCreateForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect("payroll_run_list")

        try:
            period = PayrollPeriod.for_range(form.cleaned_data["start_date"], form.cleaned_data["end_date"])
            run = services.create_run(period, request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
            return redirect("payroll_run_list")
        messages.success(request, f"Created run for {period}.")
        return redirect("payroll_run_detail", pk=run.pk)


# Register earnings breakdown — how gross_earnings was built, so the register
# shows the arithmetic rather than a single opaque total:
#
#     salary + night + overtime + transport + other = GROSS
#     GROSS - deductions - tax                      = NET
#
# Gross ALREADY contains the allowances (engine.py adds every EARNING line into
# gross_earnings), so they are a decomposition of that column, never additions
# on top of it. `other` catches BONUS/INCENTIVE from VariablePayEntry, which
# nothing has produced yet — without it the four named columns would silently
# stop summing to gross the first time Finance approves an incentive.
_EARNING_BREAKDOWN = {
    "GROSS_SALARY": "salary_amount",
    "NIGHT_ALLOWANCE": "night_amount",
    "OT_ALLOWANCE": "ot_amount",
    "SPECIAL_SHIFT_TRANSPORT": "transport_amount",
}


def _line_totals(rows, kind, *, exclude_codes=()):
    """``{(run_employee_id, component_code): total}`` over the lines of
    ``rows`` — one query for the whole list, never one per row.

    The grouping happens here rather than in a SQL sum annotation because
    ``PayrollRunLine.amount`` is encrypted at rest and the database can't
    add tokens (docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.4); ``values_list``
    still decrypts each amount on the way out. An employee can hold several
    lines of the same component (Regular OT and GH OT both land as
    OT_ALLOWANCE), so this sums rather than picks.
    """
    totals: dict[tuple, Decimal] = defaultdict(Decimal)
    pks = [row.pk for row in rows]
    if not pks:
        return totals
    qs = PayrollRunLine.objects.filter(run_employee_id__in=pks, component_kind=kind)
    if exclude_codes:
        qs = qs.exclude(component_code__in=exclude_codes)
    for run_employee_id, code, amount in qs.values_list("run_employee_id", "component_code", "amount"):
        totals[(run_employee_id, code)] += amount
    return totals


def _extra_earning_components(rows):
    """Every earning component present in ``rows`` beyond the four fixed
    columns — Salary adjustment, Festival/Performance bonus, Incentive,
    SLA… — one dynamic column each (2026-10-09), the same "one column per
    type present" pattern as the deductions below and the report's OT
    columns. Until then anything else was folded into Gross with no column
    of its own, which is how an 88,047 adjustment hid inside a 218,047 gross."""
    pks = [row.pk for row in rows]
    if not pks:
        return []
    codes = (
        PayrollRunLine.objects
        .filter(run_employee_id__in=pks, component_kind=PayComponent.Kind.EARNING)
        .exclude(component_code__in=list(_EARNING_BREAKDOWN))
        .values_list("component_code", flat=True).distinct()
    )
    return list(PayComponent.objects.filter(code__in=set(codes)).order_by("sequence", "code"))


def _attach_earning_breakdown(rows, extra_components=()):
    """Set the breakdown attributes on this page's PayrollRunEmployee rows."""
    by_pk = {row.pk: row for row in rows}
    extra_codes = [c.code for c in extra_components]
    index_by_code = {code: i for i, code in enumerate(extra_codes)}
    for row in rows:
        for attr in _EARNING_BREAKDOWN.values():
            setattr(row, attr, Decimal("0"))
        row.other_amount = Decimal("0")
        row.extra_earning_amounts = [Decimal("0")] * len(extra_codes)
    for (run_employee_id, code), total in _line_totals(rows, PayComponent.Kind.EARNING).items():
        row = by_pk[run_employee_id]
        if code in index_by_code:
            row.extra_earning_amounts[index_by_code[code]] += total
        attr = _EARNING_BREAKDOWN.get(code, "other_amount")
        setattr(row, attr, getattr(row, attr) + total)


def _earning_breakdown_totals(rows, extra_components=()):
    """Totals for the same components over every row in ``rows`` (the
    filtered-but-unpaginated list), matching the summary cards' scope.
    """
    totals = dict.fromkeys(list(_EARNING_BREAKDOWN.values()) + ["other_amount"], Decimal("0"))
    extra_totals = [Decimal("0")] * len(extra_components)
    index_by_code = {c.code: i for i, c in enumerate(extra_components)}
    for (_run_employee_id, code), total in _line_totals(rows, PayComponent.Kind.EARNING).items():
        totals[_EARNING_BREAKDOWN.get(code, "other_amount")] += total
        if code in index_by_code:
            extra_totals[index_by_code[code]] += total
    totals["extra_earning_totals"] = extra_totals
    return totals


# Deductions breakdown — same decomposition idea as earnings above, so the
# register never shows an opaque "Deductions" figure with no way to tell
# what's in it (payroll/engine.py Phase 7, 2026-09-09: meal deduction wired,
# see docs/PAYROLL_PLAN.md's meals.sebpo.net note).
#
# Meal always gets its own column (it's the one deduction the engine itself
# computes). Everything else — VariablePayEntry deductions like Disciplinary,
# and any future SalaryLoan recovery — gets one dynamic column per component
# actually present in this filtered scope, the same "one column per type
# present" pattern as the report's OT columns (payroll/reports.py). No
# generic "Other" catch-all: a component that shows up gets named.
#
# TDS is DEDUCTION-kind on PayComponent too, but the engine tracks it as
# re.tax_deducted rather than folding it into re.total_deductions/gross's
# "− Deductions" figure — it already has its own Tax column, so it's
# excluded here rather than showing up as a dynamic column too.
def _extra_deduction_components(rows):
    pks = [row.pk for row in rows]
    if not pks:
        return []
    codes = (
        PayrollRunLine.objects
        .filter(run_employee_id__in=pks, component_kind=PayComponent.Kind.DEDUCTION)
        .exclude(component_code__in=("TDS", "MEAL_DEDUCTION"))
        .values_list("component_code", flat=True).distinct()
    )
    return list(PayComponent.objects.filter(code__in=set(codes)).order_by("sequence", "code"))


def _attach_deduction_breakdown(rows, extra_components):
    by_pk = {row.pk: row for row in rows}
    extra_codes = [c.code for c in extra_components]
    for row in rows:
        row.meal_amount = Decimal("0")
        row.extra_deduction_amounts = [Decimal("0")] * len(extra_codes)
    index_by_code = {code: i for i, code in enumerate(extra_codes)}
    totals = _line_totals(rows, PayComponent.Kind.DEDUCTION, exclude_codes=("TDS",))
    for (run_employee_id, code), total in totals.items():
        row = by_pk[run_employee_id]
        if code == "MEAL_DEDUCTION":
            row.meal_amount += total
        elif code in index_by_code:
            row.extra_deduction_amounts[index_by_code[code]] += total


def _deduction_breakdown_totals(rows, extra_components):
    meal_amount = Decimal("0")
    extra_totals = [Decimal("0")] * len(extra_components)
    index_by_code = {c.code: i for i, c in enumerate(extra_components)}
    totals = _line_totals(rows, PayComponent.Kind.DEDUCTION, exclude_codes=("TDS",))
    for (_run_employee_id, code), total in totals.items():
        if code == "MEAL_DEDUCTION":
            meal_amount += total
        elif code in index_by_code:
            extra_totals[index_by_code[code]] += total
    return {"meal_amount": meal_amount, "extra_deduction_totals": extra_totals}


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunDetailView(LoginRequiredMixin, View):
    template_name = "payroll/run_detail.html"

    def get(self, request, pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)

        # The Calculated tab is money — Payroll Admins only, same tier as
        # Export/Adjust-inputs. Computed early since it also decides which
        # tab values are even selectable below.
        can_manage_calc = can_manage_payroll_calculation(request.user, request=request)

        filter_ctx = employee_filter_context(request.user, request)
        dept_id = filter_ctx["selected_department_id"]
        team_id = filter_ctx["selected_team_id"]
        q = filter_ctx["q"]

        exceptions_qs = PayrollControlCheck.objects.filter(run=run).select_related("employee")
        if dept_id:
            exceptions_qs = exceptions_qs.filter(employee__department_id=dept_id)
        if team_id:
            exceptions_qs = exceptions_qs.filter(employee__team_id=team_id)
        if q:
            exceptions_qs = exceptions_qs.filter(employee_search_q(q, prefix="employee__"))
        exceptions_qs = exceptions_qs.order_by("-severity", "check_code")

        # Support Info first, matching the real workflow: Supervisors amend
        # rosters/leave/OT and note, HR reviews and notes — Payroll Admins
        # land here too, reviewing notes before switching to Calculated to
        # actually run the numbers, not the other way around.
        allowed_tabs = ("support_info", "exceptions", "calculated") if can_manage_calc else ("support_info", "exceptions")
        tab = request.GET.get("tab") if request.GET.get("tab") in allowed_tabs else "support_info"

        calculated_rows: list = []
        calculated_total = 0
        if can_manage_calc:
            calculated_qs = run.run_employees.filter(has_pay=True)
            if dept_id:
                calculated_qs = calculated_qs.filter(employee__department_id=dept_id)
            if team_id:
                calculated_qs = calculated_qs.filter(employee__team_id=team_id)
            if q:
                # full_name / employee_code are the run's snapshot of the
                # employee; the code matches exactly, like employee_id.
                calculated_qs = calculated_qs.filter(
                    Q(full_name__icontains=q) | Q(employee_code__iexact=q)
                    | employee_search_q(q, prefix="employee__")
                )
            # net_pay is encrypted at rest, so the sort happens here rather
            # than in SQL — a run is a few hundred rows, so this is the
            # filtered-but-unpaginated list every later step works from.
            calculated_rows = sorted(calculated_qs, key=lambda row: (-row.net_pay, row.employee_code))
            calculated_total = len(calculated_rows)
        blocking_check_count = PayrollControlCheck.objects.filter(
            run=run, severity=PayrollControlCheck.Severity.BLOCKING, status=PayrollControlCheck.Status.OPEN,
        ).count()

        ctx = {
            "run": run,
            "page_title": f"Payroll run · {run.period}",
            "page_subtitle": (
                f"{run.get_status_display()} · {run.employee_count} employees · "
                f"{run.run_employees.filter(has_pay=True).count()} calculated · "
                f"{blocking_check_count} open blocking check{'s' if blocking_check_count != 1 else ''}"
            ),
            "tab": tab,
            "calculated_total": calculated_total,
            "exceptions_total": exceptions_qs.count(),
            "blocking_check_count": blocking_check_count,
            # Only Payroll Admins see actual money figures — "Payroll" group
            # members (supervisors, payroll-HR) can reach this page for the
            # org/attendance columns but the money columns hide for them.
            "can_see_money": can_see_payroll_money(request.user, request=request),
            # Lock/Reopen/Calculate/Recalculate, and the whole Calculated
            # tab — Payroll Admins only, per the real workflow's last step.
            "can_manage_calc": can_manage_calc,
            # "Upload notes" button on the Support Info tab — same page-level
            # gate as UploadPayrollNotesView itself.
            "can_bulk_upload_notes": can_bulk_upload_payroll_notes(request.user, request=request),
        }
        ctx.update(filter_ctx)

        # Export mirrors whichever tab is active, using the same filtered/
        # scoped queryset the render path below would use — never a separate
        # whole-run requery — so the workbook matches exactly what's on
        # screen for this department/team/search selection. Returns before
        # any pagination happens, unlike build_register_export (Calculated
        # tab's other "Export register" button), which is always the whole,
        # unfiltered run regardless of the active filter.
        if request.GET.get("export") == "xlsx":
            if tab == "support_info":
                support_info = self._support_info_rows(request, run, dept_id, team_id, q)
                return build_support_info_export(run, support_info["rows"], support_info["ot_types"])
            if tab == "calculated" and can_manage_calc:
                extra_earnings = _extra_earning_components(calculated_rows)
                _attach_earning_breakdown(calculated_rows, extra_earnings)
                extra_components = _extra_deduction_components(calculated_rows)
                _attach_deduction_breakdown(calculated_rows, extra_components)
                return build_calculated_export(
                    run, calculated_rows, extra_components, ctx["can_see_money"], extra_earning_components=extra_earnings,
                )
            if tab == "exceptions":
                return build_exceptions_export(run, list(exceptions_qs))

        if tab == "support_info":
            support_info = self._support_info_rows(request, run, dept_id, team_id, q)
            ctx["support_info_ot_types"] = support_info["ot_types"]
            ctx["meal_counts_unavailable"] = support_info["meal_counts_unavailable"]
            ctx.update(paginate_list(request, support_info["rows"]))
        else:
            ctx.update(paginate_list(request, calculated_rows if tab == "calculated" else list(exceptions_qs)))
        if tab == "calculated":
            extra_earnings = _extra_earning_components(calculated_rows)
            _attach_earning_breakdown(ctx["rows"], extra_earnings)
            # Totals row scope matches the active filter (department/team/q),
            # not the whole run — e.g. filtering to one department shows that
            # department's totals, not the company's. calculated_rows is the
            # filtered-but-unpaginated list, so this covers every matching
            # row, not just the current page.
            ctx.update(_earning_breakdown_totals(calculated_rows, extra_earnings))
            ctx["extra_earning_components"] = extra_earnings
            extra_components = _extra_deduction_components(calculated_rows)
            _attach_deduction_breakdown(ctx["rows"], extra_components)
            ctx.update(_deduction_breakdown_totals(calculated_rows, extra_components))
            ctx["extra_deduction_components"] = extra_components
            # 5 solo leading columns + Salary + Night/OT/Transport + one per
            # extra earning component + Gross + Meal + one per extra
            # deduction component + Deductions + Tax + Net pay + Notes —
            # kept in sync with the <thead>/empty-state colspans.
            ctx["addition_column_count"] = 3 + len(extra_earnings)
            ctx["deduction_column_count"] = 2 + len(extra_components)
            ctx["total_column_count"] = 15 + len(extra_earnings) + len(extra_components)
            # Python sums, not a SQL aggregate — the money columns are
            # encrypted at rest (docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.4).
            ctx["filtered_gross_total"] = sum((r.gross_earnings for r in calculated_rows), Decimal("0"))
            ctx["filtered_deductions_total"] = sum((r.total_deductions for r in calculated_rows), Decimal("0"))
            ctx["filtered_tax_total"] = sum((r.tax_deducted for r in calculated_rows), Decimal("0"))
            ctx["filtered_net_total"] = sum((r.net_pay for r in calculated_rows), Decimal("0"))
        return render(request, self.template_name, ctx)

    def _support_info_rows(self, request, run, dept_id, team_id, q):
        """Same org-only columns as PayrollReportView (built on the exact
        same build_payroll_report() function), scoped to this run's period,
        with a per-employee Supervisor/HR/Finance note action. No money
        field exists in this data source at all, so "only Payroll Admins
        sees money" holds by construction.

        Once this run has been calculated at least once, the row set is
        scoped to exactly the employees the engine actually paid
        (PayrollRunEmployee.has_pay, i.e. gross_earnings > 0) — so Support Info and
        Calculated always show the same people. An earlier version instead
        filtered to "has a WORK-category Shift in the period," which was
        wrong: the engine also pays employees who were on paid leave the
        entire period (LEAVE-category shifts, no WORK shift at all) and
        employees with no roster/Shift rows whatsoever (it defaults to a
        full period when there's no attendance data to reduce it) — both
        groups were being silently dropped from Support Info while still
        appearing in Calculated (confirmed 2026-09-10, a 64-employee gap).
        Before the run is ever calculated, there's no PayrollRunEmployee
        set yet to align with, so this falls back to every active/visible
        employee in scope — same as /payroll/report/ itself.

        Supervisors see only their own reportees (same visible_employees
        scoping build_payroll_report already applies for any non-admin
        viewer); the small "Payroll"-group payroll-HR staff see whatever
        broader scope their own managed-scope grants (Department/Team
        additional_managers) cover — same mechanism, not special-cased here.
        Payroll Admins are the one exception: visible_employees() only
        grants org-wide scope to "User Admins"/"HR", so a Payroll Admin
        with no reports of their own would otherwise see just themselves —
        confirmed 2026-09-10 while testing. They get viewer=None (org-wide,
        page access is already gated to Payroll/Payroll Admins upstream).
        """
        try:
            report_data = build_payroll_report(
                PayrollFilters(
                    start_date=run.period.start_date, end_date=run.period.end_date,
                    department_id=dept_id, team_id=team_id, q=q,
                ),
                viewer=None if can_see_payroll_money(request.user, request=request) else request.user,
                request=request,
            )
        except ValueError:
            return {"rows": [], "ot_types": [], "meal_counts_unavailable": False}

        # q is applied inside build_payroll_report itself now (Employee ID/
        # email exact match, name substring — same rule PayrollReportView's
        # own /payroll/report/ search already uses, see that function's own
        # comment) rather than a second, cruder substring re-filter here.
        # The old version only matched name/employee_id as a loose substring
        # and never matched email at all, so e.g. "539" would also match
        # employee_id "45390" — inconsistent with every other search box in
        # the app.
        rows = report_data["rows"]

        calculated_pks = set(
            run.run_employees.filter(has_pay=True).values_list("employee_id", flat=True)
        )
        if calculated_pks:
            rows = [r for r in rows if r["employee_pk"] in calculated_pks]
        in_scope_pks = calculated_pks if calculated_pks else {r["employee_pk"] for r in rows}

        notes_by_employee: dict = {}
        for n in PayrollRunEmployeeNote.objects.filter(run=run, employee_id__in=in_scope_pks):
            notes_by_employee.setdefault(n.employee_id, {})[n.role] = n.note

        # HR/Finance write access is a pure group check — the same answer
        # for every row, so compute it once rather than per row. Supervisor
        # write access genuinely varies per employee (managed-scope), so
        # that one still needs a per-row call below — cheap regardless,
        # since managed_scope_pks() is request-cached after its first call.
        # can_write_payroll_note requires a truthy `employee` arg even for
        # the HR/FINANCE branches (which never actually read it) — passing
        # request.user satisfies that without an extra per-row lookup.
        can_write_hr = can_write_payroll_note(request.user, request.user, "HR", request=request)
        can_write_finance = can_write_payroll_note(request.user, request.user, "FINANCE", request=request)

        employees_by_pk = CustomUser.objects.in_bulk([r["employee_pk"] for r in rows], field_name="pk")
        for row in rows:
            notes = notes_by_employee.get(row["employee_pk"], {})
            employee_obj = employees_by_pk.get(row["employee_pk"])
            row["notes"] = {
                "SUPERVISOR": {
                    "text": notes.get("SUPERVISOR", ""),
                    "can_write": can_write_payroll_note(request.user, employee_obj, "SUPERVISOR", request=request),
                },
                "HR": {"text": notes.get("HR", ""), "can_write": can_write_hr},
                "FINANCE": {"text": notes.get("FINANCE", ""), "can_write": can_write_finance},
            }
        return {
            "rows": rows,
            "ot_types": report_data.get("ot_types", []),
            "meal_counts_unavailable": report_data.get("meal_counts_unavailable", False),
        }

    def post(self, request, pk):
        run = get_object_or_404(PayrollRun, pk=pk)
        action = request.POST.get("action")
        if action in ("lock", "calculate", "reopen", "adjustment") and not can_manage_payroll_calculation(request.user, request=request):
            raise PermissionDenied("Only Payroll Admins can manage a run's inputs/calculation.")
        try:
            if action == "adjustment":
                # Salary adjustment instruction (docs/SALARY_ADJUSTMENT_PLAN.md
                # §4): how many months this run settles. Saved on the run,
                # then the run is (re)calculated so the figures reflect it.
                if run.status not in (PayrollRun.Status.INPUTS_LOCKED, PayrollRun.Status.CALCULATED):
                    raise ValueError("Lock the inputs before setting a salary adjustment for this run.")
                choice = (request.POST.get("months") or "").strip().lower()
                if choice == "all":
                    run.adjustment_months, run.adjustment_settle_all = None, True
                elif choice in ("", "0", "none"):
                    run.adjustment_months, run.adjustment_settle_all = None, False
                else:
                    months = int(choice)
                    if not 1 <= months <= 24:
                        raise ValueError("Months to settle must be between 1 and 24.")
                    run.adjustment_months, run.adjustment_settle_all = months, False
                run.updated_by = request.user
                run.save()
                calculate_run(run.pk, request.user.pk)
                label = run.adjustment_instruction_label or "none"
                messages.success(request, f"Salary adjustment set to {label} and the run recalculated.")
            elif action == "lock":
                services.lock_inputs(run, request.user)
                messages.success(request, "Inputs locked.")
            elif action == "calculate":
                calculate_run(run.pk, request.user.pk)
                messages.success(request, "Run calculated.")
            elif action == "reopen":
                services.reopen_inputs(run, request.user)
                messages.success(request, "Inputs reopened.")
            else:
                messages.error(request, "Unknown action.")
        except (ValidationError, ValueError) as exc:
            message = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            messages.error(request, message)
        return redirect("payroll_run_detail", pk=run.pk)


class PayrollRunAdjustmentDrawerView(LoginRequiredMixin, View):
    """The "Salary adjustment" drawer on the run page
    (docs/SALARY_ADJUSTMENT_PLAN.md §4): an htmx GET renders the months
    picker into #drawer-body; the form inside posts to the run page's
    `adjustment` action, which saves the instruction and recalculates.
    Gated like Lock/Calculate — Payroll Admins only — rather than by a
    ProtectedView row, since it only ever opens from a page that already
    checks the same thing."""

    def get(self, request, pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        if not can_manage_payroll_calculation(request.user, request=request):
            raise PermissionDenied("Only Payroll Admins can set a run's salary adjustment.")
        if request.headers.get("HX-Request") != "true":
            return redirect("payroll_run_detail", pk=run.pk)
        return render(request, "payroll/_drawer_salary_adjustment.html", {
            "run": run,
            "adjustment_month_choices": list(range(1, 13)),
            "action_url": reverse("payroll_run_detail", kwargs={"pk": run.pk}),
        })


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunExportView(LoginRequiredMixin, View):
    def get(self, request, pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        from audit.capture import note
        note(request, obj=run, employees=run.run_employees.count())
        return build_register_export(run)


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunEmployeeAdjustView(LoginRequiredMixin, View):
    """Edit inputs (TDS, bonus/incentive/deduction) for one employee in a
    run's period. Keyed by (run, employee) — never PayrollRunEmployee.pk,
    which doesn't survive a recalculate (delete-and-rebuild). Saving here
    doesn't touch the register; Recalculate re-derives it from these inputs.
    """

    def _employee(self, pk):
        return get_object_or_404(CustomUser, pk=pk)

    def _calculated_tax(self, run, employee):
        """This run's engine-computed tax for `employee`, or None if the run
        hasn't been calculated yet (no PayrollRunEmployee row)."""
        row = PayrollRunEmployee.objects.filter(run=run, employee=employee).only("tax_deducted").first()
        return row.tax_deducted if row else None

    def _render(self, request, run, employee, form, *, status=200, saved=False):
        has_tds_override = TDSEntry.objects.filter(employee=employee, period=run.period).exists()
        # The engine's lines for this employee in this run, with each
        # line's calc_note — the one place the register explains a figure
        # (what months a salary adjustment settled, how OT was priced…).
        run_employee = PayrollRunEmployee.objects.filter(run=run, employee=employee).first()
        calculated_lines = (
            list(PayrollRunLine.objects.filter(run_employee=run_employee).order_by("sequence"))
            if run_employee is not None else []
        )
        ctx = {
            "run": run, "employee": employee, "form": form, "saved": saved,
            "run_employee": run_employee, "calculated_lines": calculated_lines,
            "has_tds_override": has_tds_override,
            "calculated_tax": self._calculated_tax(run, employee),
            "action_url": reverse("payroll_run_employee_adjust", args=[run.pk, employee.pk]),
            "drawer_title": f"Adjust inputs — {employee.get_full_name() or employee.email}",
            "variable_entries": VariablePayEntry.objects.filter(employee=employee, period=run.period)
                .select_related("component").order_by("-added_at"),
        }
        return render(request, "payroll/_drawer_adjust.html", ctx, status=status)

    def _fresh_form(self, run, employee):
        tds_entry = TDSEntry.objects.filter(employee=employee, period=run.period).first()
        if tds_entry:
            # An override is already on record — show it so it can be adjusted.
            initial = {"tds_amount": tds_entry.amount, "tds_note": tds_entry.note}
        else:
            # No override yet — pre-fill with what the engine last calculated
            # (blank if the run hasn't been calculated at all) so HR can see
            # the current tax and tweak it from there, rather than having to
            # go read it off the register first.
            calculated = self._calculated_tax(run, employee)
            initial = {"tds_amount": calculated} if calculated is not None else {}
        return PayrollRunEmployeeAdjustForm(initial=initial)

    def get(self, request, pk, employee_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = self._employee(employee_pk)
        return self._render(request, run, employee, self._fresh_form(run, employee))

    def post(self, request, pk, employee_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = self._employee(employee_pk)
        form = PayrollRunEmployeeAdjustForm(request.POST)
        if not form.is_valid():
            return self._render(request, run, employee, form, status=400)

        tds_amount = form.cleaned_data.get("tds_amount")
        if tds_amount is not None:
            existing_override = TDSEntry.objects.filter(employee=employee, period=run.period).first()
            # The field is pre-filled with the engine-calculated default when
            # there's no override yet (see _fresh_form) — if the user saves
            # without actually changing that number, don't turn "just viewed
            # it" into a permanent override that would survive a future
            # Recalculate producing a different automatic figure.
            is_untouched_default = (
                existing_override is None
                and tds_amount == self._calculated_tax(run, employee)
            )
            if not is_untouched_default:
                TDSEntry.objects.update_or_create(
                    employee=employee, period=run.period,
                    defaults={"amount": tds_amount, "note": form.cleaned_data.get("tds_note", "")},
                )

        component = form.cleaned_data.get("variable_component")
        amount = form.cleaned_data.get("variable_amount")
        if component and amount:
            VariablePayEntry.objects.create(
                employee=employee, period=run.period, component=component, amount=amount,
                reason=form.cleaned_data.get("variable_reason") or "Manual test adjustment",
                status=VariablePayEntry.Status.APPROVED,
                added_by=request.user, updated_by=request.user,
            )

        # Stay in the drawer — re-render with the fresh entry list + a
        # cleared "add" form so multiple entries can be added in one sitting
        # without losing track of what was already saved this session.
        if request.htmx:
            return self._render(request, run, employee, self._fresh_form(run, employee), saved=True)

        messages.success(
            request,
            f"Inputs saved for {employee.get_full_name() or employee.email} — click Recalculate to apply them.",
        )
        return redirect("payroll_run_detail", pk=run.pk)


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunEmployeeNoteEditView(LoginRequiredMixin, View):
    """Drawer to view/edit one employee's Supervisor/HR/Finance notes for a
    run. Keyed by (run, employee) — up to 3 PayrollRunEmployeeNote rows per
    pair, one per role, never addressed by their own pk (mirrors
    PayrollRunEmployeeAdjustView's same (run, employee) keying)."""

    def _employee(self, pk):
        return get_object_or_404(CustomUser, pk=pk)

    def _render(self, request, run, employee):
        existing = {
            n.role: n for n in PayrollRunEmployeeNote.objects.filter(run=run, employee=employee)
        }
        roles = [
            {
                "role": role,
                "field_name": f"note_{role.lower()}",
                "label": label,
                "note": existing[role].note if role in existing else "",
                "can_write": can_write_payroll_note(request.user, employee, role, request=request),
                "updated_by": existing[role].updated_by if role in existing else None,
                "updated_at": existing[role].updated_at if role in existing else None,
            }
            for role, label in PayrollRunEmployeeNote.Role.choices
        ]
        ctx = {
            "run": run, "employee": employee, "roles": roles,
            "action_url": reverse("payroll_run_employee_note_edit", args=[run.pk, employee.pk]),
            "drawer_title": f"Notes — {employee.get_full_name() or employee.email}",
        }
        return render(request, "payroll/_drawer_notes.html", ctx)

    def get(self, request, pk, employee_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = self._employee(employee_pk)
        return self._render(request, run, employee)

    def post(self, request, pk, employee_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = self._employee(employee_pk)

        for role, _label in PayrollRunEmployeeNote.Role.choices:
            field_name = f"note_{role.lower()}"
            if field_name not in request.POST:
                continue
            # Defense-in-depth: the drawer renders this field disabled (so it
            # never actually posts) for anyone without write access to this
            # role — this re-check guards a hand-crafted request too.
            if not can_write_payroll_note(request.user, employee, role, request=request):
                continue
            text = request.POST.get(field_name, "").strip()
            obj, created = PayrollRunEmployeeNote.objects.update_or_create(
                run=run, employee=employee, role=role,
                defaults={"note": text, "updated_by": request.user},
            )
            if created:
                obj.added_by = request.user
                obj.save(update_fields=["added_by"])

        display_name = employee.get_full_name() or employee.email
        if request.htmx:
            # Instead of a full-page HX-Refresh, swap just the 3 note cells
            # for this employee's row via OOB fragments (same pattern as
            # roster/views.py::CellUpdateView), and fire the shared toast +
            # drawer-close events in one HX-Trigger header.
            existing = {n.role: n for n in PayrollRunEmployeeNote.objects.filter(run=run, employee=employee)}
            role_labels = {"SUPERVISOR": "supervisor", "HR": "HR", "FINANCE": "finance"}
            row_ctx = {"employee_pk": employee.pk, "name": display_name}
            body = ""
            for role, _label in PayrollRunEmployeeNote.Role.choices:
                note_ctx = {
                    "text": existing[role].note if role in existing else "",
                    "can_write": can_write_payroll_note(request.user, employee, role, request=request),
                }
                body += render_to_string(
                    "payroll/_support_info_note_cell.html",
                    {
                        "note": note_ctx, "role": role, "role_label": role_labels[role],
                        "run": run, "row": row_ctx, "oob": True,
                    },
                    request=request,
                )
            response = HttpResponse(body)
            response["HX-Trigger"] = json.dumps({
                "orbis:toast": {"message": f"Notes saved for {display_name}.", "type": "success"},
                "orbis:drawer-close": True,
            })
            return response

        messages.success(request, f"Notes saved for {display_name}.")
        return redirect(f"{reverse('payroll_run_detail', args=[run.pk])}?tab=support_info")


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunVariablePayDeleteView(LoginRequiredMixin, View):
    """Removes one VariablePayEntry — an input, not a register row, so this
    is safe at any time; Recalculate is what makes the removal visible.
    """

    def post(self, request, pk, employee_pk, entry_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = get_object_or_404(CustomUser, pk=employee_pk)
        entry = get_object_or_404(VariablePayEntry, pk=entry_pk, employee=employee, period=run.period)
        entry.delete()

        adjust_view = PayrollRunEmployeeAdjustView()
        if request.htmx:
            return adjust_view._render(request, run, employee, adjust_view._fresh_form(run, employee), saved=True)
        messages.success(request, "Entry removed — click Recalculate to apply it.")
        return redirect("payroll_run_detail", pk=run.pk)


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollRunVariablePayEditView(LoginRequiredMixin, View):
    """GET swaps one row into an inline amount/reason edit form; POST saves
    and refreshes the whole drawer (same pattern as add/delete)."""

    def _entry(self, run, employee, entry_pk):
        return get_object_or_404(VariablePayEntry, pk=entry_pk, employee=employee, period=run.period)

    def get(self, request, pk, employee_pk, entry_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = get_object_or_404(CustomUser, pk=employee_pk)
        entry = self._entry(run, employee, entry_pk)
        form = VariablePayEntryEditForm(initial={"amount": entry.amount, "reason": entry.reason})
        ctx = {
            "run": run, "employee": employee, "entry": entry, "form": form,
            "drawer_reload_url": reverse("payroll_run_employee_adjust", args=[run.pk, employee.pk]),
        }
        return render(request, "payroll/_variable_entry_edit_row.html", ctx)

    def post(self, request, pk, employee_pk, entry_pk):
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        employee = get_object_or_404(CustomUser, pk=employee_pk)
        entry = self._entry(run, employee, entry_pk)
        form = VariablePayEntryEditForm(request.POST)
        if not form.is_valid():
            ctx = {
                "run": run, "employee": employee, "entry": entry, "form": form,
                "drawer_reload_url": reverse("payroll_run_employee_adjust", args=[run.pk, employee.pk]),
            }
            return render(request, "payroll/_variable_entry_edit_row.html", ctx, status=400)

        entry.amount = form.cleaned_data["amount"]
        entry.reason = form.cleaned_data["reason"]
        entry.updated_by = request.user
        entry.save(update_fields=["amount", "reason", "updated_by", "updated_at"])

        adjust_view = PayrollRunEmployeeAdjustView()
        if request.htmx:
            return adjust_view._render(request, run, employee, adjust_view._fresh_form(run, employee), saved=True)
        messages.success(request, "Entry updated — click Recalculate to apply it.")
        return redirect("payroll_run_detail", pk=run.pk)


# ---------------------------------------------------------------------------
# Compensation master — Finance-only list/detail (post-uploader)
#
# Deliberately doesn't reuse accounts.filters.effective_department_id /
# employee_filter_context: those lock any user not in
# accounts.permissions.ALL_DEPARTMENTS_GROUPS to their own department, and
# Finance isn't in that group set. Gross salary access here is company-wide
# by design (ProtectedView-gated, not has_managed_scope-scoped — same
# posture as UploadSalaryView), so a Finance viewer must default to seeing
# every department, not just their own. The helpers below are a local,
# narrower stand-in that always treats the viewer as able to browse every
# department/team — text search is still shared with accounts.filters.
# ---------------------------------------------------------------------------

def _finance_department_id(request) -> int | None:
    raw = (request.GET.get("department") or "").strip()
    return int(raw) if raw.isdigit() else None


def _finance_team_id(request, department_id) -> int | None:
    raw = (request.GET.get("team") or "").strip()
    if not raw.isdigit():
        return None
    team_id = int(raw)
    teams = Team.objects.filter(is_active=True)
    if department_id:
        teams = teams.filter(department_id=department_id)
    return team_id if teams.filter(pk=team_id).exists() else None


def _finance_filter_context(request) -> dict:
    dept_id = _finance_department_id(request)
    # Un-narrowed on purpose -- the shared filter bar cascades department ->
    # team client-side; _finance_team_id() still enforces the pairing.
    teams = Team.objects.filter(is_active=True).order_by("name")
    return {
        "departments": Department.objects.filter(is_active=True).order_by("name"),
        "teams": teams,
        "selected_department_id": dept_id,
        "selected_team_id": _finance_team_id(request, dept_id),
        "can_pick_all_departments": True,
        "q": request.GET.get("q", ""),
    }


def _prefixed_q(cond, prefix: str):
    """Re-key a Q built against CustomUser so it works through an FK."""
    prefixed = Q()
    prefixed.connector = cond.connector
    for child in cond.children:
        if isinstance(child, Q):
            prefixed.children.append(_prefixed_q(child, prefix))
        else:
            lookup, value = child
            prefixed.children.append((f"{prefix}{lookup}", value))
    return prefixed


def _apply_finance_filters(qs, request, prefix: str = ""):
    """`prefix` lets a queryset of something other than CustomUser reuse this
    — SalaryStructure passes "employee__" so the same q/department/team bar
    filters through its FK. Empty for a CustomUser queryset."""
    ctx = _finance_filter_context(request)
    cond = employee_search_q(ctx["q"])
    if cond is not None:
        qs = qs.filter(_prefixed_q(cond, prefix) if prefix else cond)
    if ctx["selected_department_id"]:
        qs = qs.filter(**{f"{prefix}department_id": ctx["selected_department_id"]})
    if ctx["selected_team_id"]:
        qs = qs.filter(**{f"{prefix}team_id": ctx["selected_team_id"]})
    return qs


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryStructureListView(PageSizeMixin, LoginRequiredMixin, ListView):
    """One row per employee — their current (APPROVED, open-ended) SalaryStructure.

    Payroll Admins only via ProtectedView (seed_orbis's payroll_salary
    group — Finance until 2026-10-05), company-wide — same access posture
    as UploadSalaryView.
    """

    model = CustomUser
    template_name = "payroll/salary_structure_list.html"
    context_object_name = "employees"
    paginate_by = 25

    def get_queryset(self):
        current_prefetch = Prefetch(
            "salary_structures",
            queryset=SalaryStructure.objects.filter(
                status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
            ),
            to_attr="current_salary_list",
        )
        qs = (
            CustomUser.objects.filter(
                salary_structures__status=SalaryStructure.Status.APPROVED,
                salary_structures__effective_to__isnull=True,
            )
            .exclude(Q(employee_id__isnull=True) | Q(employee_id=""))
            .select_related("department", "team")
            .prefetch_related(current_prefetch)
            .distinct()
        )
        qs = _apply_finance_filters(qs, self.request)
        # employee_id is a CharField of digits, so a plain string sort puts
        # "100" before "20" — cast to int for true ascending numeric order.
        return qs.annotate(employee_id_num=Cast("employee_id", IntegerField())).order_by("employee_id_num")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Gross salary"
        ctx.update(_finance_filter_context(self.request))
        return ctx


def _salary_page_context(employee):
    structures = list(
        SalaryStructure.objects.filter(employee=employee)
        .select_related("processed_by", "batch")
        .order_by("-revision_no", "-effective_from")
    )
    current = next(
        (s for s in structures if s.status == SalaryStructure.Status.APPROVED and s.effective_to is None),
        None,
    )
    return {"employee": employee, "current": current, "history": [s for s in structures if s is not current]}


class _GrossSalaryAccessMixin(LoginRequiredMixin, View):
    """The per-employee gross-salary page and its "Revise salary" drawer:
    Payroll Admins or the payroll-HR pair (accounts.permissions.
    can_edit_gross_salary, 2026-10-09). A code gate rather than the
    ProtectedView row the list keeps, because "in both HR and Payroll" is
    not a group — the loans views' posture."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_edit_gross_salary(request.user, request=request):
            if request.headers.get("HX-Request") == "true":
                raise PermissionDenied("Gross salary is managed by Payroll-HR and Payroll Admins.")
            return redirect("home")
        return super().dispatch(request, *args, **kwargs)


class SalaryStructureDetailView(_GrossSalaryAccessMixin):
    """Current structure prominently, then SUPERSEDED/CANCELLED history, and
    the "Revise salary" button opening SalaryStructureReviseView's drawer."""

    template_name = "payroll/salary_structure_detail.html"

    def get(self, request, employee_pk):
        employee = get_object_or_404(CustomUser.objects.select_related("department", "team"), pk=employee_pk)
        ctx = _salary_page_context(employee)
        ctx["page_title"] = f"Gross salary · {employee.display_name}"
        # The list behind "Back to list" is Payroll Admins only; the pair
        # came from the profile, so send them back there.
        ctx["can_open_list"] = can_see_payroll_money(request.user, request=request)
        return render(request, self.template_name, ctx)


class SalaryStructureReviseView(_GrossSalaryAccessMixin):
    """Record a salary revision for one employee from the gross-salary page
    (docs/PAYROLL_PLAN.md 2026-10-09 "Revise salary"): an htmx GET renders
    the form into #drawer-body, a valid POST writes one APPROVED
    SalaryStructure through services.supersede_salary_structure (the
    upload's write path — a same-date revision cancels the old row) and
    answers 204 + HX-Redirect back to the page; an invalid POST re-renders
    the fragment with status 400. No maker-checker: the user's decision is
    that HR-Payroll update and approve, as Payroll Admins do."""

    template_name = "payroll/_salary_revise_drawer.html"

    def _employee(self, employee_pk):
        return get_object_or_404(CustomUser.objects.select_related("department", "team"), pk=employee_pk)

    def _render(self, request, employee, current, form, *, status=200):
        return render(request, self.template_name, {
            "employee": employee, "current": current, "form": form,
            "action_url": reverse("payroll_salary_revise", kwargs={"employee_pk": employee.pk}),
        }, status=status)

    def get(self, request, employee_pk):
        employee = self._employee(employee_pk)
        if request.headers.get("HX-Request") != "true":
            return redirect("payroll_salary_detail", employee_pk=employee.pk)
        current = _salary_page_context(employee)["current"]
        form = SalaryRevisionForm(employee=employee, current=current, today=timezone.localdate())
        return self._render(request, employee, current, form)

    def post(self, request, employee_pk):
        employee = self._employee(employee_pk)
        current = _salary_page_context(employee)["current"]
        form = SalaryRevisionForm(request.POST, employee=employee, current=current, today=timezone.localdate())
        if not form.is_valid():
            return self._render(request, employee, current, form, status=400)
        data = form.cleaned_data
        structure = services.supersede_salary_structure(
            employee, gross_monthly=data["gross_monthly"], effective_from=data["effective_from"],
            reason=data["reason"], actor=request.user, note=data["note"],
        )
        messages.success(
            request,
            f"{employee.display_name} is on {structure.gross_monthly:,.0f} from {structure.effective_from:%Y-%m-%d} "
            f"(revision #{structure.revision_no}). Recalculate any run this period or later — a revision back to an "
            f"earlier month changes their salary adjustment balance too.",
        )
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("payroll_salary_detail", kwargs={"employee_pk": employee.pk})
            return response
        return redirect("payroll_salary_detail", employee_pk=employee.pk)


# ---------------------------------------------------------------------------
# Overtime entries — list / add / edit / void / delete (added 2026-09-15)
#
# Same access posture as the uploader this sits next to: ManagedScopeRequiredMixin
# for the page, payroll_visible_employee_pks for the rows (separated employees
# included — a supervisor must still be able to fix a leaver's final-period
# overtime, see that helper's docstring), and payroll.services.OvertimeWindow
# for which work dates may still be written. Every POST re-checks all three
# server-side; what the list chose to render is never the gate.
# ---------------------------------------------------------------------------

def _overtime_scope_pks(request):
    """Employee pks whose overtime this viewer may see/act on, or None for
    "everyone". HR-Payroll and Payroll Admins are company-wide approvers
    (docs/OVERTIME_APPROVAL_CHAIN_PLAN.md, decision 1); before 2026-10-17 a
    Payroll Admin with no reportees could not even open this page, which
    made their approval right on paper only."""
    if _is_overtime_global_approver(request.user, request=request):
        return None
    return payroll_visible_employee_pks(request.user, request=request)


def _in_overtime_scope(scope, employee_id) -> bool:
    return scope is None or employee_id in scope


class _OvertimeScopeRequiredMixin(ManagedScopeRequiredMixin):
    """ManagedScopeRequiredMixin, plus the two global approver groups."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and _is_overtime_global_approver(request.user, request=request):
            return View.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def _scope_pks(self):
        return _overtime_scope_pks(self.request)


def _overtime_base_queryset(request):
    scope = _overtime_scope_pks(request)
    qs = OvertimeEntry.objects.all()
    if scope is not None:
        qs = qs.filter(employee_id__in=scope)
    return (
        qs
        .select_related(
            "employee", "employee__department", "employee__team",
            "overtime_type", "worked_in", "period", "added_by",
        )
    )


def _overtime_list_url(request) -> str:
    """Back to the list with the filters that were active when the drawer or
    action was opened — they ride along on the action's own querystring."""
    base = reverse("payroll_overtime_list")
    query = request.GET.urlencode()
    return f"{base}?{query}" if query else base


def _decorate_overtime_rows(rows, window, request):
    """Attach per-row lock state + what this viewer may do to it.

    In-memory apart from the permission helpers' own request-cached scope
    lookups: OvertimeWindow's run table is loaded once, managed scope and
    the approver-department set are each one cached query, so a page of 25
    rows costs no extra queries (a department head's own row is the one
    exception — it reads their JobInfo).

    `can_review` / `can_approve` / `can_reject` are the chain's three
    actions (docs/OVERTIME_APPROVAL_CHAIN_PLAN.md); `can_decide` is any of
    them, which drives the bulk checkbox.
    """
    user = request.user
    for row in rows:
        row.is_locked = window.is_locked(row.work_date)
        row.is_pending = row.status == OvertimeEntry.Status.PENDING
        row.is_reviewed = row.status == OvertimeEntry.Status.REVIEWED
        # Status matters, not just the date: a locked period accepts new
        # overtime and amendments to something not yet approved, but an
        # entry already approved or voided there is settled and only HR-
        # Payroll may touch it — mirrors _OvertimeWriteView._get_entry,
        # which is what actually enforces this.
        row.can_edit = window.allows(row.work_date) and (
            not row.is_locked or row.is_unapproved or window.can_manage_locked
        )
        reviewer = row.is_pending and can_review_overtime(user, row.employee, request=request)
        approver = row.is_unapproved and can_approve_overtime(user, row.employee, request=request)
        row.can_review = reviewer
        row.can_approve = approver
        row.can_reject = reviewer or approver
        row.can_decide = reviewer or approver
    return rows


def _overtime_date_range(request, window):
    """Defaults to exactly the range this viewer can still act on — floor ->
    today — so the page opens on the editable window rather than on history
    nobody can change."""
    raw_from = parse_date(request.GET.get("from") or "")
    raw_to = parse_date(request.GET.get("to") or "")
    return (raw_from or window.floor), (raw_to or window.today)


def _filtered_overtime_queryset(request, window):
    """Every entry the active filters (in ``request.GET``) match, ordered as
    the list shows them. Module-level because three callers need the exact
    same set: the list page, the Excel export, and the bulk decide view's
    "select all N matching entries" (the bulk form posts to a URL carrying
    the list's own querystring, so its request.GET is the list's filters).
    """
    qs = _overtime_base_queryset(request)
    date_from, date_to = _overtime_date_range(request, window)
    if date_from:
        qs = qs.filter(work_date__gte=date_from)
    if date_to:
        qs = qs.filter(work_date__lte=date_to)

    filter_ctx = employee_filter_context(request.user, request)
    if filter_ctx["selected_department_id"]:
        qs = qs.filter(employee__department_id=filter_ctx["selected_department_id"])
    if filter_ctx["selected_team_id"]:
        qs = qs.filter(employee__team_id=filter_ctx["selected_team_id"])
    q = filter_ctx["q"]
    if q:
        qs = qs.filter(employee_search_q(q, prefix="employee__"))

    raw_type = (request.GET.get("overtime_type") or "").strip()
    if raw_type.isdigit():
        qs = qs.filter(overtime_type_id=int(raw_type))
    raw_status = (request.GET.get("status") or "").strip().upper()
    if raw_status in OvertimeEntry.Status.values:
        qs = qs.filter(status=raw_status)
    if request.GET.get("awaiting") == "me":
        # The bell's "waiting on you" set, so the link and the page agree —
        # see core.approvals.overtime_awaiting_q.
        from core.approvals import overtime_awaiting_q

        qs = qs.filter(overtime_awaiting_q(request.user, request=request))

    return qs.order_by("-work_date", "employee__employee_id")


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEntryListView(PageSizeMixin, _OvertimeScopeRequiredMixin, ListView):
    """Replaces "go ask someone with Django admin" for overtime corrections."""

    model = OvertimeEntry
    template_name = "payroll/overtime_list.html"
    context_object_name = "entries"
    paginate_by = 25

    def _window(self):
        if not hasattr(self, "_window_cache"):
            self._window_cache = OvertimeWindow(self.request.user, request=self.request)
        return self._window_cache

    def _date_range(self):
        return _overtime_date_range(self.request, self._window())

    def get(self, request, *args, **kwargs):
        # Same early-return-before-pagination shape as PayrollReportView and
        # PayrollRunDetailView's exports — export always covers everything
        # the active filters matched, not just the current page.
        if request.GET.get("export") == "xlsx":
            date_from, date_to = self._date_range()
            return build_overtime_export(self.get_queryset(), date_from, date_to)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return _filtered_overtime_queryset(self.request, self._window())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        window = self._window()
        date_from, date_to = self._date_range()
        _decorate_overtime_rows(ctx["entries"], window, self.request)
        ctx.update(employee_filter_context(self.request.user, self.request))
        can_review_any = has_overtime_review_scope(self.request.user, request=self.request)
        can_approve_any = has_overtime_approval_scope(self.request.user, request=self.request)
        base = _overtime_base_queryset(self.request)
        ctx.update({
            "page_title": "Overtime",
            "date_from": date_from,
            "date_to": date_to,
            "window": window,
            "overtime_types": OvertimeType.objects.filter(is_active=True),
            "selected_overtime_type_id": (self.request.GET.get("overtime_type") or "").strip(),
            "selected_status": (self.request.GET.get("status") or "").strip().upper(),
            "status_choices": OvertimeEntry.Status.choices,
            "can_review_any": can_review_any,
            "can_approve_any": can_approve_any,
            "can_decide": can_review_any or can_approve_any,
            "awaiting_me": self.request.GET.get("awaiting") == "me",
            # Counted across the viewer's whole scope, not just this page or
            # the current date filter: an unapproved entry the filters
            # happen to hide is exactly the one that gets forgotten and
            # never paid.
            "pending_total": base.filter(status=OvertimeEntry.Status.PENDING).count(),
            "reviewed_total": base.filter(status=OvertimeEntry.Status.REVIEWED).count(),
        })
        return ctx


class _OvertimeWriteView(_OvertimeScopeRequiredMixin):
    """Shared scope/window enforcement for every overtime write."""

    def _window(self):
        if not hasattr(self, "_window_cache"):
            self._window_cache = OvertimeWindow(self.request.user, request=self.request)
        return self._window_cache

    def _get_entry(self, pk):
        entry = get_object_or_404(
            OvertimeEntry.objects.select_related("employee", "overtime_type", "worked_in", "period"), pk=pk,
        )
        if not _in_overtime_scope(self._scope_pks(), entry.employee_id):
            raise PermissionDenied("That overtime entry is outside your managed scope.")
        window = self._window()
        reason = window.check(entry.work_date)
        if reason:
            raise PermissionDenied(reason)
        # A locked period accepts NEW overtime (as PENDING) and amendments to
        # something still pending, but an entry HR has already approved or
        # voided there is settled — only an approver may touch it. Without
        # this, relaxing the window to allow late filing would also have
        # quietly handed supervisors edit rights over approved history.
        if (
            window.is_locked(entry.work_date)
            and not entry.is_unapproved
            and not window.can_manage_locked
        ):
            raise PermissionDenied(
                "That entry is already settled for a locked payroll period — "
                "ask HR-Payroll to change it."
            )
        return entry

    def _employee_queryset(self):
        scope = self._scope_pks()
        qs = CustomUser.objects.all() if scope is None else CustomUser.objects.filter(pk__in=scope)
        return qs.order_by("first_name", "last_name")


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEntryEditView(_OvertimeWriteView):
    """Right-hand drawer — add a new entry (no pk) or revise an existing one.

    A valid POST answers 204 + HX-Redirect rather than the bare 204 +
    orbis:drawer-close the attendance correction drawer uses: the row's own
    visible values change here, so closing the drawer over a stale row would
    just show the old figures until the next manual refresh.
    """

    template_name = "payroll/_overtime_entry_drawer.html"

    def _form(self, data=None, instance=None):
        return OvertimeEntryForm(
            data, instance=instance, viewer=self.request.user, window=self._window(),
            employee_queryset=self._employee_queryset(),
        )

    def _render(self, request, form, entry, *, status=200):
        ctx = {
            "form": form,
            "entry": entry,
            "window": self._window(),
            "action_url": (
                reverse("payroll_overtime_edit", args=[entry.pk]) if entry
                else reverse("payroll_overtime_create")
            ),
        }
        return render(request, self.template_name, ctx, status=status)

    def get(self, request, pk=None):
        entry = self._get_entry(pk) if pk else None
        return self._render(request, self._form(instance=entry), entry)

    def post(self, request, pk=None):
        entry = self._get_entry(pk) if pk else None
        # Snapshot before the form mutates `entry` in place (a ModelForm
        # binds to the same instance), so the reset check below compares
        # against what was actually signed off.
        self._before = (
            {f: getattr(entry, f) for f in OvertimeEntry.DECISION_FIELDS} if entry else {}
        )
        form = self._form(request.POST, instance=entry)
        if not form.is_valid():
            return self._render(request, form, entry, status=400)

        obj = form.save(commit=False)
        if entry is None:
            obj.added_by = request.user
        obj.updated_by = request.user
        reset = False
        if entry is not None and not obj.is_unapproved and obj.status != OvertimeEntry.Status.VOID:
            # A sign-off covers the figures that were signed. Changing them
            # after review/approval starts the chain again — whoever edits,
            # HR-Payroll included (same posture as Q56's "nobody files as
            # approved"). Note-only edits keep the stamps.
            changed = {
                f for f in OvertimeEntry.DECISION_FIELDS
                if getattr(obj, f) != self._before[f]
            }
            if changed:
                obj.reset_to_pending()
                reset = True
        obj.save()

        messages.success(
            request,
            ("Overtime entry updated — it is back to Pending review." if reset
             else "Overtime entry updated.") if entry else "Overtime entry added — pending review.",
        )
        target = _overtime_list_url(request)
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return redirect(target)


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEntryVoidView(_OvertimeWriteView):
    """Void an entry, or restore a voided one.

    Void keeps the row — payroll.inputs.collect_overtime already filters on
    status=APPROVED, so the engine drops it with no engine change, and HR can
    still see that something was filed and cancelled.

    Restore never flips back to APPROVED: it returns the entry to the start
    of the chain (PENDING, stamps cleared). Flipping straight to APPROVED
    was a self-approval hole — a supervisor could take their own PENDING
    entry, void it, restore it, and have it counted without anyone else
    seeing it. Voiding also clears the stamps, so a later Restore can't
    carry a stale sign-off along.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        entry = self._get_entry(pk)
        if entry.status == OvertimeEntry.Status.VOID:
            entry.reset_to_pending()
            note = "Entry restored — it is back to Pending review."
        else:
            entry.reset_to_pending()
            entry.status = OvertimeEntry.Status.VOID
            entry.processed_by = request.user
            entry.processed_at = timezone.now()
            note = "Entry voided — it no longer counts towards payroll."
        entry.updated_by = request.user
        entry.save(update_fields=[
            "status", "reviewed_by", "reviewed_at", "processed_by", "processed_at",
            "updated_by", "updated_at",
        ])
        messages.success(request, note)
        return redirect(_overtime_list_url(request))


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEntryDeleteView(_OvertimeWriteView):
    """Hard delete, for a genuine mistake — same posture as
    PayrollRunVariablePayDeleteView ("an input, not a register row").

    GET renders the confirmation into the same right-hand drawer the edit
    action uses; POST does the delete. One URL for both so the confirmation
    can't drift out of sync with what it confirms — and so the scope/window
    checks in _get_entry() run before a confirmation is even offered, rather
    than letting someone confirm an action that will then 403.

    A browser confirm() would be shorter, but it can't show WHICH entry is
    about to go (the row's own figures) and can't offer Void — the
    reversible option — as a one-click alternative next to it.
    """

    http_method_names = ["get", "post"]

    def get(self, request, pk):
        entry = self._get_entry(pk)
        query = request.GET.urlencode()
        suffix = f"?{query}" if query else ""
        return render(request, "payroll/_overtime_delete_drawer.html", {
            "entry": entry,
            "action_url": reverse("payroll_overtime_delete", args=[entry.pk]) + suffix,
            "void_url": reverse("payroll_overtime_void", args=[entry.pk]) + suffix,
        })

    def post(self, request, pk):
        entry = self._get_entry(pk)
        entry.delete()
        messages.success(request, "Overtime entry deleted.")
        return redirect(_overtime_list_url(request))


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEmployeeSearchView(ManagedScopeRequiredMixin):
    """HTML-fragment search backing the drawer's employee picker.

    Its own endpoint rather than employees.views.load_employees (hardwired to
    manager candidates) or offboarding's (HR-only) — the scope that matters
    here is payroll_visible_employee_pks, same as every other overtime write.
    """

    http_method_names = ["get"]

    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        qs = CustomUser.objects.filter(
            pk__in=payroll_visible_employee_pks(request.user, request=request),
        )
        if q:
            qs = qs.filter(employee_search_q(q))
        return render(request, "employees/_employee_search_results.html", {
            "q": q,
            "results": qs.order_by("first_name", "last_name")[:25],
            "empty_message": "No matching employee in your scope.",
        })


# ---------------------------------------------------------------------------
# The overtime review -> approve chain (2026-10-17,
# docs/OVERTIME_APPROVAL_CHAIN_PLAN.md; replaces Q55's single HR-Payroll step)
#
# Every write lands PENDING, which the engine ignores (collect_overtime
# filters status=APPROVED). A Manager-tier person in scope marks it REVIEWED
# (can_review_overtime); the department head / additional managers, HR-
# Payroll or a Payroll Admin mark it APPROVED (can_approve_overtime) — from
# REVIEWED, or straight from PENDING, in which case the review stamp is
# theirs too. Reject from either stage -> VOID. The run then has to be
# recalculated if its period is already locked, which is exactly the
# silent-non-payment the run detail page's stale-inputs banner warns about.
#
# _review_entry/_approve_entry/_reject_entry are module-level for the same
# reason attendance's _approve_correction is: the single-row and bulk views
# both call them, so the two can't drift apart. Each re-checks the per-row
# permission itself — the view-level mixin only answers "may this person act
# on anything", the row answers "on this one".
# ---------------------------------------------------------------------------

def _review_entry(entry, actor, request=None) -> bool:
    """PENDING -> REVIEWED. False when it wasn't pending or `actor` may not
    review this employee's overtime."""
    if entry.status != OvertimeEntry.Status.PENDING:
        return False
    if not can_review_overtime(actor, entry.employee, request=request):
        return False
    entry.status = OvertimeEntry.Status.REVIEWED
    entry.reviewed_by = actor
    entry.reviewed_at = timezone.now()
    entry.updated_by = actor
    entry.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_by", "updated_at"])
    return True


def _approve_entry(entry, actor, request=None) -> bool:
    """PENDING/REVIEWED -> APPROVED. False when it was already settled or
    `actor` may not approve this employee's overtime. Approving a row
    nobody reviewed stamps the approver as reviewer as well, so the row
    always says who looked at it first."""
    if not entry.is_unapproved:
        return False
    if not can_approve_overtime(actor, entry.employee, request=request):
        return False
    now = timezone.now()
    if entry.reviewed_at is None:
        entry.reviewed_by = actor
        entry.reviewed_at = now
    entry.status = OvertimeEntry.Status.APPROVED
    entry.processed_by = actor
    entry.processed_at = now
    entry.updated_by = actor
    entry.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "processed_by", "processed_at",
        "updated_by", "updated_at",
    ])
    return True


def _reject_entry(entry, actor, request=None) -> bool:
    """PENDING/REVIEWED -> VOID, by a reviewer or an approver. Rejecting is
    the same end state as voiding after the fact, so it reuses VOID rather
    than adding a REJECTED nobody filters on; processed_by records who
    decided."""
    if not entry.is_unapproved:
        return False
    if not (
        can_approve_overtime(actor, entry.employee, request=request)
        or (
            entry.status == OvertimeEntry.Status.PENDING
            and can_review_overtime(actor, entry.employee, request=request)
        )
    ):
        return False
    entry.status = OvertimeEntry.Status.VOID
    entry.processed_by = actor
    entry.processed_at = timezone.now()
    entry.updated_by = actor
    entry.save(update_fields=["status", "processed_by", "processed_at", "updated_by", "updated_at"])
    return True


OVERTIME_DECISIONS = {
    "review": _review_entry,
    "approve": _approve_entry,
    "reject": _reject_entry,
}


class _OvertimeDeciderRequired(_OvertimeScopeRequiredMixin):
    """Deciding is a narrower right than filing: the viewer must be able to
    review or approve *somebody's* overtime to reach these at all. Which
    rows they may actually act on is re-checked per row by the helpers."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not (
            has_overtime_review_scope(request.user, request=request)
            or has_overtime_approval_scope(request.user, request=request)
        ):
            raise PermissionDenied("You can't review or approve overtime.")
        return super().dispatch(request, *args, **kwargs)

    def _respond(self, request):
        response = redirect(_overtime_list_url(request))
        if request.htmx:
            response["HX-Trigger"] = APPROVALS_CHANGED_EVENT
        return response


def _decision_message(decision: str, acted: int, skipped: int, period=None) -> tuple[str, str]:
    """(level, text) for a single or bulk decision."""
    if not acted:
        return "warning", "Nothing to do — those entries were already handled or aren't yours to decide."
    noun = f"{acted} entr{'y' if acted == 1 else 'ies'}" if acted != 1 or skipped else "Entry"
    tail = f", skipped {skipped}" if skipped else ""
    if decision == "review":
        return "success", f"Reviewed {noun}{tail} — the department head still has to approve before it is paid."
    if decision == "approve":
        where = f"the {period} run" if period else "the affected run(s)"
        return "success", f"Approved {noun}{tail} — recalculate {where} so it is actually paid."
    return "success", f"Rejected {noun}{tail} — not paid."


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEntryDecideView(_OvertimeDeciderRequired):
    """Review, approve or reject one entry. `decision` comes from the URL so
    the actions can't be confused by a tampered form field."""

    http_method_names = ["post"]

    def post(self, request, pk, decision):
        act = OVERTIME_DECISIONS.get(decision)
        if act is None:
            raise PermissionDenied("Unknown decision.")
        entry = get_object_or_404(
            OvertimeEntry.objects.select_related("employee", "employee__department", "period"), pk=pk,
        )
        if not _in_overtime_scope(self._scope_pks(), entry.employee_id):
            raise PermissionDenied("That overtime entry is outside your managed scope.")

        acted = act(entry, request.user, request=request)
        level, text = _decision_message(decision, int(acted), 0, entry.period if acted else None)
        getattr(messages, level)(request, text)
        return self._respond(request)


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeEntryBulkDecideView(_OvertimeDeciderRequired):
    """Review/approve/reject many at once — the realistic shape, since an
    upload lands a whole team's week in one go.

    Every id is re-checked server-side; anything that fails (already
    handled, not yours to decide, out of scope, deleted) is skipped rather
    than aborting the batch, and the message counts both. Same posture as
    attendance.views::AttendanceCorrectionBulkActionView.
    """

    http_method_names = ["post"]

    def post(self, request):
        decision = (request.POST.get("decision") or "").lower()
        act = OVERTIME_DECISIONS.get(decision)
        if act is None:
            raise PermissionDenied("Unknown decision.")

        if request.POST.get("select_all") == "1":
            # Django-admin style "select all N matching entries": act on
            # everything the list's filters match, not just the checkboxes on
            # this page — a department head approving a month is hundreds of
            # rows across many pages. The filters ride on the form's action
            # querystring. Already-settled rows are skipped up front; every
            # survivor is still re-checked per row by the helper.
            window = OvertimeWindow(request.user, request=request)
            entries = (
                _filtered_overtime_queryset(request, window)
                .filter(status__in=OvertimeEntry.UNAPPROVED_STATUSES)
            )
            total = entries.count()
        else:
            ids = request.POST.getlist("entry_ids")
            scope = self._scope_pks()
            entries = OvertimeEntry.objects.select_related(
                "employee", "employee__department", "period",
            ).filter(pk__in=[i for i in ids if str(i).isdigit()])
            if scope is not None:
                entries = entries.filter(employee_id__in=scope)
            total = len(ids)

        with transaction.atomic():
            acted = sum(1 for entry in entries.iterator(chunk_size=500) if act(entry, request.user, request=request))
        skipped = total - acted
        level, text = _decision_message(decision, acted, skipped)
        getattr(messages, level)(request, text)
        return self._respond(request)


GH_OT_PAGE_TEMPLATE = "payroll/overtime_generate_gh.html"
GH_OT_FRAGMENT_TEMPLATE = "payroll/_overtime_generate_gh_report.html"


GH_OT_SOURCES = {
    "sw_creative": "Software / Creative Development (attendance)",
    "general_bpo": "General BPO (TTMS)",
}
GH_OT_DEFAULT_SOURCE = "sw_creative"


class GenerateGHOvertimeView(LoginRequiredMixin, View):
    """Superuser-only manual trigger for payroll.gh_overtime's GH OT
    auto-generation — the UI equivalent of `manage.py generate_gh_overtime` /
    `generate_gh_overtime_bpo`, added 2026-09-17 (BPO source added same day)
    as a stopgap until Celery-beat jobs are manageable from Django admin
    (docs/PAYROLL_PLAN.md).

    Two sources, picked via the `source` field (GH_OT_SOURCES):
    - sw_creative: Software/Creative Development, presence from
      AttendancePunch, a flat grant_hours per present employee. Assumes
      AttendancePunch is already synced for the picked date — this view does
      NOT run sync_attendance_punches itself; an older/missed date needs
      that run first, by hand.
    - general_bpo: General BPO, presence AND hours both come live from
      ttms.sebpo.net (payroll/ttms.py) — no local sync step, but a fetch
      failure (TTMSUnavailable) is surfaced as an inline error rather than
      swallowed, same posture as the management command's CommandError.

    GET renders the page with a date picker. POST previews by default
    (action=preview) or writes (action=apply) — same preview-before-write
    shape as the overtime bulk uploader (payroll/upload_views.py).
    """

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            raise PermissionDenied("Generating GH OT from the UI is restricted to superusers.")
        return super().dispatch(request, *args, **kwargs)

    def _respond(self, request, ctx: dict, *, status: int = 200):
        template = GH_OT_FRAGMENT_TEMPLATE if getattr(request, "htmx", False) else GH_OT_PAGE_TEMPLATE
        ctx.setdefault("sources", GH_OT_SOURCES)
        return render(request, template, ctx, status=status)

    def get(self, request):
        return self._respond(request, {
            "source": GH_OT_DEFAULT_SOURCE, "min_hours": DEFAULT_MIN_HOURS, "grant_hours": DEFAULT_GRANT_HOURS,
        })

    def post(self, request):
        source = request.POST.get("source") or GH_OT_DEFAULT_SOURCE
        if source not in GH_OT_SOURCES:
            source = GH_OT_DEFAULT_SOURCE

        raw_date = (request.POST.get("work_date") or "").strip()
        work_date = parse_date(raw_date)
        default_min_hours = DEFAULT_MIN_HOURS if source == "sw_creative" else DEFAULT_BPO_MIN_HOURS
        try:
            min_hours = float(request.POST.get("min_hours") or default_min_hours)
            grant_hours = float(request.POST.get("grant_hours") or DEFAULT_GRANT_HOURS)
        except ValueError:
            min_hours, grant_hours = default_min_hours, DEFAULT_GRANT_HOURS

        ctx = {"source": source, "work_date_raw": raw_date, "min_hours": min_hours, "grant_hours": grant_hours}
        if work_date is None:
            ctx["inline_error"] = f"'{raw_date}' isn't a valid date."
            return self._respond(request, ctx, status=400)

        apply = request.POST.get("action") == "apply"
        if source == "general_bpo":
            try:
                report = generate_bpo_gh_overtime_for_date(
                    work_date, min_hours=min_hours, apply=apply, actor=request.user,
                )
            except TTMSUnavailable as exc:
                ctx["inline_error"] = f"Could not fetch TTMS daily hours — {exc}"
                return self._respond(request, ctx, status=502)
        else:
            report = generate_gh_overtime_for_date(
                work_date, min_hours=min_hours, grant_hours=grant_hours, apply=apply, actor=request.user,
            )
        if apply and report.created:
            messages.success(
                request,
                f"Created {len(report.created)} GH OT entr{'y' if len(report.created) == 1 else 'ies'} "
                f"for {work_date.isoformat()}.",
            )
        ctx["report"] = report
        ctx["work_date"] = work_date
        return self._respond(request, ctx)


# ---------------------------------------------------------------------------
# Variable pay queue — SLA achievement, Test Salary, bonus, incentive,
# disciplinary. Deliberately ONE generic queue over VariablePayEntry rather
# than an SLA-specific page: the approval gate is a property of the input
# table, not of the component (docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md
# Phase D, "One gate"), so every component shares this screen.
#
# The gate here is NOT overtime's. Overtime keeps HR-Payroll (Q55);
# variable pay is approved by the employee's own department head
# (accounts.permissions::can_act_on_variable_pay, confirmed 2026-09-19 —
# HR reviews but does not approve).
# ---------------------------------------------------------------------------

def _variable_pay_base_queryset(request):
    """Rows the viewer may SEE — their managed scope. Whether they may
    *act* on a given row is a narrower, per-row question answered by
    can_act_on_variable_pay, since seeing your team's pending SLA list and
    signing it off are different rights."""
    scope = payroll_visible_employee_pks(request.user, request=request)
    return (
        VariablePayEntry.objects
        .filter(employee_id__in=scope)
        .select_related("employee", "employee__department", "employee__team", "component", "period")
    )


def _variable_pay_list_url(request) -> str:
    query = request.POST.get("next_qs") or ""
    base = reverse("payroll_variable_pay_list")
    return f"{base}?{query}" if query else base


def _approve_variable_pay(entry, actor) -> bool:
    """PENDING -> APPROVED. False when it wasn't pending (already handled).

    Module-level and shared by the single-row and bulk views for the same
    reason attendance's _approve_correction is — two copies of an approval
    rule drift.
    """
    if entry.status != VariablePayEntry.Status.PENDING:
        return False
    entry.status = VariablePayEntry.Status.APPROVED
    entry.processed_by = actor
    entry.processed_at = timezone.now()
    entry.updated_by = actor
    entry.save(update_fields=["status", "processed_by", "processed_at", "updated_by", "updated_at"])
    return True


def _reject_variable_pay(entry, actor) -> bool:
    """PENDING -> REJECTED. Unlike overtime (which reuses VOID) this model
    already has a REJECTED state, so there is nothing to overload."""
    if entry.status != VariablePayEntry.Status.PENDING:
        return False
    entry.status = VariablePayEntry.Status.REJECTED
    entry.processed_by = actor
    entry.processed_at = timezone.now()
    entry.updated_by = actor
    entry.save(update_fields=["status", "processed_by", "processed_at", "updated_by", "updated_at"])
    return True


@method_decorator(dynamic_allowed_users, name="dispatch")
class VariablePayEntryListView(PageSizeMixin, ManagedScopeRequiredMixin, ListView):
    """Pending/decided variable pay across the viewer's scope."""

    model = VariablePayEntry
    template_name = "payroll/variable_pay_list.html"
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        qs = _variable_pay_base_queryset(self.request)

        filter_ctx = employee_filter_context(self.request.user, self.request)
        if filter_ctx["selected_department_id"]:
            qs = qs.filter(employee__department_id=filter_ctx["selected_department_id"])
        if filter_ctx["selected_team_id"]:
            qs = qs.filter(employee__team_id=filter_ctx["selected_team_id"])
        q = filter_ctx["q"]
        if q:
            qs = qs.filter(employee_search_q(q, prefix="employee__"))

        raw_component = (self.request.GET.get("component") or "").strip()
        if raw_component.isdigit():
            qs = qs.filter(component_id=int(raw_component))
        raw_status = (self.request.GET.get("status") or "").strip().upper()
        if raw_status in VariablePayEntry.Status.values:
            qs = qs.filter(status=raw_status)
        raw_period = (self.request.GET.get("period") or "").strip()
        if raw_period.isdigit():
            qs = qs.filter(period_id=int(raw_period))

        return qs.order_by("-period__start_date", "employee__employee_id", "component__code")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        for entry in ctx["entries"]:
            entry.can_decide = (
                entry.status == VariablePayEntry.Status.PENDING
                and can_act_on_variable_pay(self.request.user, entry.employee, request=self.request)
            )
        ctx.update(employee_filter_context(self.request.user, self.request))
        ctx.update({
            "page_title": "Variable pay",
            "components": PayComponent.objects.filter(
                entry_mode=PayComponent.EntryMode.INPUT, is_active=True,
            ).exclude(code="TDS"),
            "selected_component_id": (self.request.GET.get("component") or "").strip(),
            "selected_status": (self.request.GET.get("status") or "").strip().upper(),
            "selected_period_id": (self.request.GET.get("period") or "").strip(),
            "periods": PayrollPeriod.objects.order_by("-start_date")[:12],
            "status_choices": VariablePayEntry.Status.choices,
            "can_decide_any": any(getattr(e, "can_decide", False) for e in ctx["entries"]),
            # Across the whole scope, not just this page or the active
            # filters — a pending row the filters hide is exactly the one
            # that gets forgotten and never paid. Same reasoning as the
            # overtime list's pending_total.
            "pending_total": (
                _variable_pay_base_queryset(self.request)
                .filter(status=VariablePayEntry.Status.PENDING).count()
            ),
        })
        return ctx


@method_decorator(dynamic_allowed_users, name="dispatch")
class VariablePayEntryDecideView(ManagedScopeRequiredMixin):
    """Approve or reject one pending entry. `decision` is in the URL so the
    two actions can't be confused by a tampered form field."""

    http_method_names = ["post"]

    def post(self, request, pk, decision):
        entry = get_object_or_404(
            VariablePayEntry.objects.select_related("employee", "period", "component"), pk=pk,
        )
        if not can_act_on_variable_pay(request.user, entry.employee, request=request):
            raise PermissionDenied(
                "Only the department head may approve variable pay for this employee."
            )

        acted = (
            _approve_variable_pay(entry, request.user) if decision == "approve"
            else _reject_variable_pay(entry, request.user)
        )
        if acted and decision == "approve":
            messages.success(
                request,
                f"Approved — recalculate the {entry.period} run so it is actually paid.",
            )
        elif acted:
            messages.success(request, "Rejected — this entry will not be paid.")
        else:
            messages.warning(request, "That entry was already handled by someone else.")
        return redirect(_variable_pay_list_url(request))


@method_decorator(dynamic_allowed_users, name="dispatch")
class VariablePayEntryBulkDecideView(ManagedScopeRequiredMixin):
    """Approve/reject many at once — the realistic shape, since one SLA
    upload lands a whole department's month in one go (52 rows in September
    2026).

    Every id is re-checked server-side against can_act_on_variable_pay;
    anything that fails (already handled, not yours, deleted) is skipped
    rather than aborting the batch, and the message counts both. Same
    posture as attendance's AttendanceCorrectionBulkActionView and the
    overtime bulk view.
    """

    http_method_names = ["post"]

    def post(self, request):
        ids = request.POST.getlist("entry_ids")
        decision = (request.POST.get("decision") or "").lower()
        if decision not in ("approve", "reject"):
            raise PermissionDenied("Unknown decision.")

        entries = (
            VariablePayEntry.objects
            .select_related("employee", "employee__department", "period")
            .filter(pk__in=[i for i in ids if str(i).isdigit()])
        )
        act = _approve_variable_pay if decision == "approve" else _reject_variable_pay
        acted = sum(
            1 for entry in entries
            if can_act_on_variable_pay(request.user, entry.employee, request=request)
            and act(entry, request.user)
        )
        skipped = len(ids) - acted

        if acted and decision == "approve":
            messages.success(request, (
                f"Approved {acted} entr{'y' if acted == 1 else 'ies'}"
                + (f", skipped {skipped}" if skipped else "")
                + " — recalculate the affected run(s) so they are actually paid."
            ))
        elif acted:
            messages.success(
                request,
                f"Rejected {acted} entr{'y' if acted == 1 else 'ies'}"
                + (f", skipped {skipped}." if skipped else "."),
            )
        else:
            messages.warning(request, "Nothing to do — those entries were already handled, or aren't yours to decide.")
        return redirect(_variable_pay_list_url(request))


# ---------------------------------------------------------------------------
# Pending salary revisions (Phase B, 2026-09-19)
#
# A go-live proposes a PENDING SalaryStructure from the team's package
# (payroll/services.py::propose_salary_for_assignment). This is where Finance
# approves or rejects it. Same Finance-only, company-wide posture as the rest
# of the gross-salary screens — deliberately NOT accounts.filters, which would
# clamp Finance to its own department.
# ---------------------------------------------------------------------------

@method_decorator(dynamic_allowed_users, name="dispatch")
class PendingSalaryStructureListView(PageSizeMixin, LoginRequiredMixin, ListView):
    """Salary revisions proposed but not yet approved."""

    model = SalaryStructure
    template_name = "payroll/salary_pending_list.html"
    context_object_name = "structures"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            SalaryStructure.objects
            .filter(status=SalaryStructure.Status.PENDING)
            .select_related("employee", "employee__department", "employee__team", "added_by")
        )
        qs = _apply_finance_filters(qs, self.request, prefix="employee__")
        return qs.order_by("effective_from", "employee__employee_id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        current = {
            s.employee_id: s.gross_monthly
            for s in SalaryStructure.objects.filter(
                employee_id__in=[s.employee_id for s in ctx["structures"]],
                status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
            )
        }
        for structure in ctx["structures"]:
            structure.current_gross = current.get(structure.employee_id)
            structure.delta = (
                structure.gross_monthly - structure.current_gross
                if structure.current_gross is not None else None
            )
        ctx["page_title"] = "Pending salary changes"
        ctx.update(_finance_filter_context(self.request))
        return ctx


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryStructureDecideView(LoginRequiredMixin, View):
    """Approve or reject one proposed revision. `decision` is in the URL so
    the two can't be confused by a tampered form field."""

    http_method_names = ["post"]

    def post(self, request, pk, decision):
        structure = get_object_or_404(
            SalaryStructure.objects.select_related("employee"), pk=pk,
        )
        if decision == "approve":
            acted = services.approve_salary_structure(structure, request.user)
            message = (
                f"Approved — {structure.employee.display_name} is on "
                f"{structure.gross_monthly} from {structure.effective_from}. "
                "Recalculate any affected run."
            )
        else:
            acted = services.reject_salary_structure(structure, request.user)
            message = "Rejected — their salary is unchanged."

        if acted:
            messages.success(request, message)
        else:
            messages.warning(request, "That proposal was already handled by someone else.")
        return redirect("payroll_salary_pending")


# ---------------------------------------------------------------------------
# Tax declarations — the employee's once-a-year confirmation
# ---------------------------------------------------------------------------

class TaxDeclarationReviewView(LoginRequiredMixin, View):
    """Confirm (and lock) one EmployeeTaxDeclaration — the employee's own, or
    Payroll-HR on their behalf (accounts.permissions.can_submit_tax_declaration).

    Lives on the employee profile: the "Tax declarations" section card's
    "Review & confirm" button loads this as a right-hand drawer (htmx GET);
    a non-htmx GET is the full-page fallback for direct navigation. A valid
    POST stamps submitted_at/by (IN_ORBIS) and answers 204 + HX-Redirect
    back to the profile, so the section re-renders in its locked state with
    the success message — same drawer-or-page split as attendance
    corrections. Once locked, GET and POST both 403.
    """

    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.declaration = get_object_or_404(
            EmployeeTaxDeclaration.objects.select_related("employee", "tax_year", "category"), pk=pk,
        )
        from audit.capture import note
        note(request, subject=self.declaration.employee)
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_submit_tax_declaration(request.user, self.declaration, request=request):
            raise PermissionDenied(
                "This declaration is locked." if self.declaration.is_locked
                else "You can't confirm this employee's tax declaration."
            )
        return super().dispatch(request, *args, **kwargs)

    def _render(self, request, form, *, status=200):
        declaration = self.declaration
        ctx = {
            "form": form,
            "declaration": declaration,
            "target_employee": declaration.employee,
            "confirming_for_other": declaration.employee_id != request.user.pk,
            "action_url": reverse("payroll_tax_declaration_review", args=[declaration.pk]),
        }
        if request.htmx:
            return render(request, "payroll/_tax_declaration_review_drawer.html", ctx, status=status)
        ctx["page_title"] = f"Confirm tax declaration {declaration.tax_year.code}"
        return render(request, "payroll/tax_declaration_review.html", ctx, status=status)

    def get(self, request):
        return self._render(request, TaxDeclarationReviewForm(instance=self.declaration))

    def post(self, request):
        form = TaxDeclarationReviewForm(request.POST, request.FILES, instance=self.declaration)
        if not form.is_valid():
            return self._render(request, form, status=400)
        declaration = form.save(commit=False)
        declaration.mark_submitted(request.user)
        declaration.updated_by = request.user
        declaration.save()
        messages.success(
            request,
            f"Tax declaration {declaration.tax_year.code} confirmed and locked.",
        )
        target = reverse("employee_detail", args=[declaration.employee_id])
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return redirect(target)


class TaxDeclarationDocumentView(LoginRequiredMixin, View):
    """Stream the AIT payment document. Never linked by its media URL —
    production nginx serves /media/ unauthenticated (deploy/README.md), so
    the random filename plus this permission check is what keeps a tax
    document private."""

    def get(self, request, pk):
        declaration = get_object_or_404(EmployeeTaxDeclaration.objects.select_related("employee"), pk=pk)
        from audit.capture import note
        note(request, subject=declaration.employee)
        if not can_view_tax_declaration_document(request.user, declaration, request=request):
            raise PermissionDenied("You can't open this document.")
        if not declaration.ait_document:
            raise Http404("No document on file.")
        return FileResponse(declaration.ait_document.open("rb"), as_attachment=False)
