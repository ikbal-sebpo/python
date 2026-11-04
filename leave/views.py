"""Leave views.

- LeaveListView: tabbed list.
- LeaveApplyView: form with eligibility + balance validation.
- LeavePreviewView: AJAX endpoint returning billable-day breakdown.
- LeaveDetailView: detail with sandwich breakdown.
- LeaveApproveView / LeaveDeclineView / LeaveCancelView: action views,
  rewired through the sandwich engine + balance ledger.
- LeaveBalanceView: self-service balance dashboard.
- EncashmentListView / EncashmentRequestView / EncashmentDecideView /
  EncashmentMarkPaidView: encashment workflow.
"""

from __future__ import annotations

from calendar import Calendar, monthrange
from datetime import date as _date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import DateField, F
from django.db.models.functions import Cast, Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from core.approvals import APPROVALS_CHANGED_EVENT
from core.exports import escape_spreadsheet
from core.pagination import (
    DEFAULT_PAGE_SIZE,
    PageSizeMixin,
    paginate_list,
    resolve_page_size,
)
from accounts.filters import (
    apply_employee_filters, effective_filter_ids, employee_filter_context, employee_search_q,
)


from accounts.models import CustomUser
from accounts.permissions import (
    can_act_on_leave,
    can_apply_leave_for,
    can_cancel_leave,
    can_edit_all,
    can_edit_leave,
    can_manage_cpl_grants,
    can_see_all,
    can_see_all_departments,
    direct_report_pks,
    effective_cpl_treatment,
    has_managed_scope,
    visible_departments,
    visible_employee_pks,
    visible_teams,
)
from leave.exports import build_shift_summary_export
from leave.forms import CplGrantManualForm, LeaveApplicationForm
from leave.models import (
    CplGrant,
    EncashmentRequest,
    LeaveApplication,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
)
from leave.services import (
    compute_application_billable_days,
    consume_balance,
    credit_manual_cpl_grant,
    describe_balance_impact,
    get_or_create_balance,
    is_eligible_for_policy,
    live_accrued_al,
    live_available,
    live_used,
    materialise_shifts_for_application,
    recompute_sandwich,
    restore_balance,
    reverse_shifts_for_application,
    validate_application_attachment,
    validate_application_eligibility,
)
from roster.models import ShiftType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _visible_leave_qs(user, request=None):
    """All LeaveApplication rows the user is allowed to see."""
    if can_see_all(user, request):
        return LeaveApplication.objects.all()
    return LeaveApplication.objects.filter(
        employee_id__in=visible_employee_pks(user, request)
    )


def _month_from_request(request) -> _date:
    raw = request.GET.get("month")
    try:
        parsed = parse_date(f"{raw}-01") if raw else None
    except ValueError:
        parsed = None
    today = timezone.localdate()
    return (parsed or today).replace(day=1)


def _build_leave_calendar(qs, month_start: _date) -> dict:
    _, days_in_month = monthrange(month_start.year, month_start.month)
    month_end = month_start.replace(day=days_in_month)

    calendar_qs = (
        qs.filter(
            start_date__lte=month_end,
            end_date__gte=month_start,
        )
        # The calendar answers "who is out when" — cancelled/declined
        # applications would read as absences, so they stay list-only.
        .exclude(status__in=[
            LeaveApplication.Status.CANCELLED,
            LeaveApplication.Status.DECLINED,
        ])
        .select_related("employee", "leave_type")
        .order_by("start_date", "employee__first_name", "employee__last_name")
    )

    by_day = {
        day: []
        for week in Calendar(firstweekday=6).monthdatescalendar(
            month_start.year, month_start.month
        )
        for day in week
    }
    for app in calendar_qs:
        current = max(app.start_date, min(by_day))
        end = min(app.end_date, max(by_day))
        while current <= end:
            if current in by_day:
                by_day[current].append(app)
            current += timedelta(days=1)

    weeks = []
    for week in Calendar(firstweekday=6).monthdatescalendar(month_start.year, month_start.month):
        weeks.append([
            {
                "date": day,
                "in_month": day.month == month_start.month,
                "applications": by_day[day],
                "preview": by_day[day][:3],
                "overflow": max(len(by_day[day]) - 3, 0),
            }
            for day in week
        ])

    previous_month = (month_start.replace(day=1) - timedelta(days=1)).replace(day=1)
    next_month = (month_end + timedelta(days=1)).replace(day=1)
    return {
        "weeks": weeks,
        "month_start": month_start,
        "previous_month": previous_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "current_month": month_start.strftime("%Y-%m"),
        "today": timezone.localdate(),
    }


def _apply_leave_search(qs, query: str):
    query = (query or "").strip()
    if not query:
        return qs
    return qs.filter(employee_search_q(query, prefix="employee__"))


def _leave_date_range(request):
    """`(from, to, errors)` for the optional leave-date filter.

    Either side may be None, which reads as open-ended. A malformed or
    inverted range is reported back instead of applied, so a typo shows an
    explanation rather than an unexplained empty list.
    """
    parsed = {}
    errors = []
    for key, label in (("start_date", "Working date from"), ("end_date", "Working date to")):
        raw = (request.GET.get(key) or "").strip()
        value = None
        if raw:
            try:
                value = parse_date(raw)
            except ValueError:
                value = None
            if value is None:
                errors.append(f"{label}: '{raw}' isn't a valid date.")
        parsed[key] = value

    date_from, date_to = parsed["start_date"], parsed["end_date"]
    if date_from and date_to and date_from > date_to:
        errors.append("Working date from must be on or before Working date to.")
        return None, None, errors
    return date_from, date_to, errors


def _apply_leave_filters(qs, request, date_from=None, date_to=None):
    qs = _apply_leave_search(qs, request.GET.get("q"))
    dept_id, team_id = effective_filter_ids(request.user, request)
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)
    if team_id:
        qs = qs.filter(employee__team_id=team_id)
    if date_from:
        qs = qs.filter(start_date__gte=date_from)
    if date_to:
        qs = qs.filter(end_date__lte=date_to)
    return qs


def _direct_reports_only(request, direct_pks) -> bool:
    """The Pending / All tabs' "Direct reports only" switch (added 2026-10-10).

    On by default for anyone with at least one direct report — absent or any
    value other than ``0`` means on — so the URL stays clean in the default
    state and an unchecked HTML checkbox (which sends nothing) can't be the
    control; the template navigates to ``?direct=0`` instead. A viewer with
    no direct reports (typically HR / Admins, whose All leaves tab is their
    working list) never gets the switch: it would only ever show an empty
    tab, so the full scope shows and the checkbox is hidden. It is a scope
    switch, not a filter: it lives beside the tabs rather than in the
    Filters popover, isn't part of ``filters_active`` and survives the
    popover's "Clear" link. Same direct-reports default the roster grid uses
    (``roster.views._resolve_grid_employees``), minus the empty-default for
    HR.
    """
    return bool(direct_pks) and request.GET.get("direct") != "0"


def _leave_filter_suffix(request, date_from=None, date_to=None) -> str:
    dept_id, team_id = effective_filter_ids(request.user, request)
    params = {}
    q = (request.GET.get("q") or "").strip()
    if q:
        params["q"] = q
    direct_pks = direct_report_pks(request.user, request=request)
    if direct_pks and not _direct_reports_only(request, direct_pks):
        params["direct"] = "0"
    if dept_id:
        params["department"] = dept_id
    if team_id:
        params["team"] = team_id
    if date_from:
        params["start_date"] = date_from.isoformat()
    if date_to:
        params["end_date"] = date_to.isoformat()
    # The page-size choice rides with the filters so it survives a tab switch.
    per_page = resolve_page_size(request)
    if per_page != DEFAULT_PAGE_SIZE:
        params["per_page"] = per_page
    return f"&{urlencode(params)}" if params else ""


# ---------------------------------------------------------------------------
# List (tabbed)
# ---------------------------------------------------------------------------

class LeaveListView(LoginRequiredMixin, View):
    template_name = "leave/list.html"

    def get(self, request):
        tab = request.GET.get("tab", "mine")
        view_mode = request.GET.get("view", "list")
        q = (request.GET.get("q") or "").strip()
        if view_mode not in {"list", "calendar"}:
            view_mode = "list"
        month_start = _month_from_request(request)
        date_from, date_to, date_errors = _leave_date_range(request)
        base = (
            _visible_leave_qs(request.user, request=request)
            .select_related("employee", "employee__department", "employee__team", "leave_type")
        )

        mine_qs = (
            LeaveApplication.objects
            .filter(employee=request.user)
            # `employee` is always request.user here, but the row template
            # still dereferences it (name, employee ID, team) once per row.
            .select_related("employee", "employee__team", "leave_type")
        )
        pending_qs = base.filter(status=LeaveApplication.Status.PENDING).exclude(employee=request.user)
        direct_pks = direct_report_pks(request.user, request=request)
        direct_only = _direct_reports_only(request, direct_pks)
        user_can_see_all = can_see_all(request.user, request=request)
        show_pending_tab = has_managed_scope(request.user, request=request)
        show_all_tab = user_can_see_all or show_pending_tab
        all_qs = base if show_all_tab else base.none()
        if direct_only:
            # Narrow *within* the visible scope — never widens it. Applies to
            # the Pending and All tabs alike (the viewer's own rows live on
            # "My leaves"). Never true for a viewer with no direct reports.
            pending_qs = pending_qs.filter(employee_id__in=direct_pks)
            all_qs = all_qs.filter(employee_id__in=direct_pks)

        # No per-tab order_by — LeaveApplication.Meta.ordering owns the sort so
        # the three tabs can't drift apart.
        if tab == "pending" and show_pending_tab:
            selected_qs = _apply_leave_filters(pending_qs, request, date_from, date_to)
        elif tab == "all" and show_all_tab:
            selected_qs = _apply_leave_filters(all_qs, request, date_from, date_to)
        else:
            tab = "mine"
            selected_qs = _apply_leave_filters(mine_qs, request, date_from, date_to)

        # The calendar reads the whole filtered set; only the table paginates.
        calendar = _build_leave_calendar(selected_qs, month_start)
        page = paginate_list(request, selected_qs)
        filter_suffix = _leave_filter_suffix(request, date_from, date_to)
        filter_ctx = employee_filter_context(request.user, request)

        ctx = {
            "page_title": "Leave",
            "tab": tab,
            "view_mode": view_mode,
            **page,
            "mine_count": mine_qs.count(),
            "pending_count": pending_qs.count(),
            "all_count": all_qs.count() if show_all_tab else 0,
            "leave_calendar": calendar,
            "show_pending_tab": show_pending_tab,
            "show_all_tab": show_all_tab,
            "direct_only": direct_only,
            "has_direct_reports": bool(direct_pks),
            "q": q,
            # Echo back what was typed, not what parsed — an inverted range
            # parses to None, and blanking the inputs would hide the mistake
            # the error message is asking the user to fix.
            "date_from_input": (request.GET.get("start_date") or "").strip(),
            "date_to_input": (request.GET.get("end_date") or "").strip(),
            "date_errors": date_errors,
            "query_suffix": filter_suffix,
            "filters_active": bool(
                q
                or request.GET.get("department")
                or request.GET.get("team")
                or request.GET.get("start_date")
                or request.GET.get("end_date")
            ),
            "list_url": f"?tab={tab}&view=list{filter_suffix}",
            "calendar_url": f"?tab={tab}&view=calendar&month={calendar['current_month']}{filter_suffix}",
            "previous_month_url": f"?tab={tab}&view=calendar&month={calendar['previous_month']}{filter_suffix}",
            "next_month_url": f"?tab={tab}&view=calendar&month={calendar['next_month']}{filter_suffix}",
            "today_calendar_url": f"?tab={tab}&view=calendar{filter_suffix}",
            "current_user_pk": request.user.pk,
            "user_can_see_all": user_can_see_all,
            "user_can_edit_all": can_edit_all(request.user, request=request),
            **filter_ctx,
        }
        return render(request, self.template_name, ctx)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

class LeaveApplyView(LoginRequiredMixin, CreateView):
    model = LeaveApplication
    form_class = LeaveApplicationForm
    template_name = "leave/apply.html"

    def _resolve_employee(self):
        raw = self.request.GET.get("for") or self.request.POST.get("for")
        if not raw:
            return self.request.user
        target = get_object_or_404(CustomUser, pk=raw)
        if target.pk == self.request.user.pk:
            return self.request.user
        if not can_apply_leave_for(self.request.user, target, request=self.request):
            raise PermissionDenied("You can't apply on behalf of that employee.")
        return target

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["employee"] = self._resolve_employee()
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Apply for leave"
        ctx["target_employee"] = self._resolve_employee()
        ctx["applying_for_other"] = ctx["target_employee"].pk != self.request.user.pk
        return ctx

    def form_valid(self, form):
        target = self._resolve_employee()
        app = form.save(commit=False)
        app.employee = target
        app.applied_by = self.request.user
        app.added_by = self.request.user
        app.updated_by = self.request.user
        app.status = LeaveApplication.Status.PENDING
        app.save()
        impact = describe_balance_impact(app)
        if impact["is_advance"]:
            messages.warning(self.request, impact["message"])
        messages.success(self.request, f"Leave request submitted for {target.display_name}.")
        return redirect("leave_detail", pk=app.pk)


class LeaveEditView(LoginRequiredMixin, UpdateView):
    """Revise a still-PENDING application — same form and page as apply.

    Nothing outside the row changes: a pending application has written no
    ledger transaction, no Shift rows and no sandwich billing (all of that
    happens at approval, which re-validates anyway), so this is a plain save.
    Status, employee and applied_by stay as filed.
    """

    model = LeaveApplication
    form_class = LeaveApplicationForm
    template_name = "leave/apply.html"
    context_object_name = "application"

    def get_object(self, queryset=None):
        app = get_object_or_404(
            LeaveApplication.objects.select_related("employee", "leave_type"),
            pk=self.kwargs["pk"],
        )
        if not app.is_pending:
            raise PermissionDenied("Only a pending leave request can be edited.")
        if not can_edit_leave(self.request.user, app, request=self.request):
            raise PermissionDenied("You can't edit this leave request.")
        return app

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["employee"] = self.object.employee
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Edit leave request"
        ctx["editing"] = True
        ctx["target_employee"] = self.object.employee
        ctx["applying_for_other"] = self.object.employee_id != self.request.user.pk
        return ctx

    def form_valid(self, form):
        app = form.save(commit=False)
        app.updated_by = self.request.user
        app.save()
        impact = describe_balance_impact(app)
        if impact["is_advance"]:
            messages.warning(self.request, impact["message"])
        messages.success(self.request, "Leave request updated.")
        return redirect("leave_detail", pk=app.pk)


# ---------------------------------------------------------------------------
# AJAX preview — returns billable-day breakdown + available balance
# ---------------------------------------------------------------------------

class LeavePreviewView(LoginRequiredMixin, View):
    """POST a proposed leave (employee/leave_type/start/end/is_half_day) and
    receive `{working, sandwich, total, available, leave_type, is_paid,
    cert_required, cert_required_min_days}` for live form feedback."""

    http_method_names = ["post"]

    def post(self, request):
        for_pk = request.POST.get("for") or request.user.pk
        try:
            target = CustomUser.objects.get(pk=for_pk)
        except CustomUser.DoesNotExist:
            return JsonResponse({"error": "Employee not found"}, status=404)

        if target.pk != request.user.pk:
            if not can_apply_leave_for(request.user, target, request=request):
                return JsonResponse({"error": "Permission denied"}, status=403)

        leave_type_id = request.POST.get("leave_type")
        start = parse_date(request.POST.get("start_date") or "")
        end = parse_date(request.POST.get("end_date") or "")
        is_half_day = request.POST.get("is_half_day") in ("on", "true", "1")

        if not (leave_type_id and start and end):
            return JsonResponse({"error": "Missing fields"}, status=400)
        if end < start:
            return JsonResponse({"error": "End must be on/after start"}, status=400)

        try:
            leave_type = ShiftType.objects.select_related("leave_policy").get(
                pk=leave_type_id, is_active=True, category=ShiftType.Category.LEAVE,
            )
        except ShiftType.DoesNotExist:
            return JsonResponse({"error": "Invalid leave type"}, status=400)

        policy = leave_type.leave_policy
        probe = LeaveApplication(
            employee=target,
            leave_type=leave_type,
            start_date=start,
            end_date=end,
            is_half_day=is_half_day,
            applied_by=target,
        )

        try:
            working, sandwich, total = compute_application_billable_days(probe)
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        bal = get_or_create_balance(target, policy, start)
        cert_threshold = policy.cert_required_min_days
        cert_needed = (cert_threshold > 0
                       and (probe.day_count >= cert_threshold))
        impact = describe_balance_impact(probe)

        return JsonResponse({
            "working": str(working),
            "sandwich": str(sandwich),
            "total": str(total),
            "available": str(live_available(target, policy, bal, upto=start)),
            "is_paid": policy.is_paid,
            "cert_required": cert_needed,
            "cert_required_min_days": cert_threshold,
            "is_advance": impact["is_advance"],
            "advance_message": impact["message"] if impact["is_advance"] else "",
            "leave_type_name": leave_type.name,
            "year": start.year,
        })


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

class LeaveDetailView(LoginRequiredMixin, DetailView):
    model = LeaveApplication
    template_name = "leave/detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return LeaveApplication.objects.select_related(
            "employee", "leave_type", "applied_by", "processed_by",
        )

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        user = self.request.user
        if obj.employee_id == user.pk:
            return obj
        if obj.employee_id in visible_employee_pks(user, request=self.request):
            return obj
        if can_see_all(user, request=self.request):
            return obj
        raise Http404

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        app = ctx["application"]
        ctx["page_title"] = f"Leave · {app.leave_type.name} · {app.start_date}"
        ctx["can_act"] = (
            can_act_on_leave(self.request.user, app, request=self.request)
            and app.is_pending
        )
        ctx["can_edit"] = can_edit_leave(self.request.user, app, request=self.request)
        ctx["can_cancel"] = (
            can_cancel_leave(self.request.user, app, request=self.request)
            and app.status != LeaveApplication.Status.CANCELLED
        )
        return ctx


# ---------------------------------------------------------------------------
# Action views — approve / decline / cancel
# ---------------------------------------------------------------------------

class _ActionBase(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.application = get_object_or_404(LeaveApplication, pk=pk)
        return super().dispatch(request, pk, *args, **kwargs)

    def _render_row_response(self, request):
        """htmx → updated row partial; non-htmx → redirect to detail."""
        if not getattr(request, "htmx", False):
            return redirect("leave_detail", pk=self.application.pk)
        html = render_to_string(
            "leave/_row.html",
            {
                "row": self.application,
                "current_user_pk": request.user.pk,
                "oob": True,
                "show_actions": False,
            },
            request=request,
        )
        response = HttpResponse(html)
        # The row left the pending set — refresh the topbar bell now rather
        # than at its next poll (core/approvals.py).
        response["HX-Trigger"] = APPROVALS_CHANGED_EVENT
        return response


class LeaveApproveView(_ActionBase):
    @transaction.atomic
    def post(self, request, pk):
        app = self.application
        if not can_act_on_leave(request.user, app, request=request) or not app.is_pending:
            raise PermissionDenied
        # Re-validate at approve time — state may have changed since the form.
        try:
            validate_application_eligibility(app)
            validate_application_attachment(app)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("leave_detail", pk=app.pk)
        impact = describe_balance_impact(app)
        if not impact["sufficient"]:
            messages.error(request, impact["message"])
            return redirect("leave_detail", pk=app.pk)

        app.status = LeaveApplication.Status.APPROVED
        app.processed_by = request.user
        app.processed_at = timezone.now()
        app.process_note = request.POST.get("process_note", "").strip()
        app.updated_by = request.user
        if impact["is_advance"]:
            app.approved_over_balance = True
            app.balance_shortfall_days = impact["requested"] - impact["available_before"]
        app.save()
        # The recompute orchestrator handles materialisation + the ledger
        # for this app and any adjacent approved apps that were affected by
        # the new sandwich anchor.
        recompute_sandwich(app.employee, app.start_date, request_user=request.user)
        messages.success(request, f"Approved: {app}.")
        return self._render_row_response(request)


class LeaveDeclineView(_ActionBase):
    @transaction.atomic
    def post(self, request, pk):
        app = self.application
        if not can_act_on_leave(request.user, app, request=request) or not app.is_pending:
            raise PermissionDenied
        app.status = LeaveApplication.Status.DECLINED
        app.processed_by = request.user
        app.processed_at = timezone.now()
        app.process_note = request.POST.get("process_note", "").strip()
        app.updated_by = request.user
        app.save()
        # Decline didn't write any shifts/balance, nothing to reverse.
        messages.info(request, f"Declined: {app}.")
        return self._render_row_response(request)


class LeaveCancelView(_ActionBase):
    @transaction.atomic
    def post(self, request, pk):
        app = self.application
        if not can_cancel_leave(request.user, app, request=request):
            raise PermissionDenied
        was_approved = app.is_approved
        app.status = LeaveApplication.Status.CANCELLED
        app.processed_by = request.user
        app.processed_at = timezone.now()
        app.process_note = (
            request.POST.get("process_note", "").strip()
            or app.process_note or "Cancelled"
        )
        app.updated_by = request.user
        app.save()
        if was_approved:
            restore_balance(app, request.user)
            reverse_shifts_for_application(app)
            recompute_sandwich(app.employee, app.start_date, request_user=request.user)
        messages.info(request, f"Cancelled: {app}.")
        return self._render_row_response(request)


# ---------------------------------------------------------------------------
# Balance dashboard (self-service)
# ---------------------------------------------------------------------------

class LeaveBalanceView(LoginRequiredMixin, View):
    """The balance dashboard, and — over htmx — the balance drawer on the
    leave list.

    Both are the same view on purpose. A manager deciding on a leave
    application needs the balance, and the only balance worth showing is the
    one the balance page already computes correctly (live AL accrual, the
    employee's own anniversary cycle, eligibility gating). A second
    implementation for the drawer would drift from it; this one can't.

    `?application=<pk>` additionally renders the arithmetic behind the
    decision — requested days, available, and available-if-approved — via
    `leave/services.py::describe_balance_impact`, which is the same function
    `LeaveApproveView`'s guard raises from.
    """

    template_name = "leave/balance.html"
    drawer_template_name = "leave/_balance_drawer.html"

    def get(self, request):
        # Allow HR / managers to view another employee via ?for=<pk>.
        for_pk = request.GET.get("for")
        target = request.user
        if for_pk and str(for_pk) != str(request.user.pk):
            target = get_object_or_404(CustomUser, pk=for_pk)
            if not (can_see_all(request.user, request=request)
                    or target.pk in visible_employee_pks(request.user, request=request)):
                raise PermissionDenied("You can't view this employee's balance.")

        year = int(request.GET.get("year") or _date.today().year)

        # ?application=<pk> — the drawer opened from the leave list names the
        # application being decided on, so the balance arrives with the
        # arithmetic that decides it rather than as bare numbers a manager
        # has to do sums against.
        application = None
        impact = None
        app_pk = request.GET.get("application")
        if app_pk:
            application = get_object_or_404(
                LeaveApplication.objects.select_related(
                    "employee", "leave_type__leave_policy__shift_type",
                ),
                pk=app_pk,
            )
            # `target` is already permission-checked above; requiring the
            # application to belong to it keeps that the only check needed,
            # and stops the block from being arithmetic on two people.
            if application.employee_id != target.pk:
                raise Http404("That application doesn't belong to this employee.")
            impact = describe_balance_impact(application)

        # Materialise balance rows only for policies that actually track a
        # quota AND that this employee is currently eligible for. FL/LWP/HdL
        # have no quota; MtL is hidden from male / short-tenure employees;
        # AL is hidden until the 12-month service gate clears.
        #
        # ANNIVERSARY-cycle policies (AL) always show the employee's *current*
        # cycle, ignoring the ?year= picker — that control is calendar-shaped
        # and doesn't map onto a joining-date-anchored window. CALENDAR-cycle
        # policies (CL/SL/...) still respect ?year= as before.
        relevant_policies = []
        rows = []
        for policy in LeavePolicy.objects.select_related("shift_type"):
            if not policy.tracks_balance:
                continue
            ok, _ = is_eligible_for_policy(target, policy)
            if not ok:
                continue
            relevant_policies.append(policy)
            as_of = (
                _date.today() if policy.cycle == LeavePolicy.Cycle.ANNIVERSARY
                else _date(year, 1, 1)
            )
            bal = get_or_create_balance(target, policy, as_of)
            # `as_of` above only locates *which row* (respecting the ?year=
            # filter for CALENDAR policies) — the live computations always
            # want "as of today", capped internally at that row's own
            # cycle_end, regardless of which year's row that is. Both
            # fall back to the stored fields for anything that isn't an
            # open cycle (closed, frozen, or — for accrued — a non-AL
            # policy) — safe to call unconditionally here.
            live_upto = _date.today()
            accrued = live_accrued_al(target, policy, bal, upto=live_upto)
            used = live_used(target, policy, bal, upto=live_upto)
            rows.append({
                "policy": policy,
                "name": policy.shift_type.name,
                "is_paid": policy.is_paid,
                "opening": bal.opening,
                "accrued": accrued,
                "carried_forward": bal.carried_forward,
                "used": used,
                "encashed": bal.encashed,
                "available": bal.opening + accrued + bal.carried_forward - used - bal.encashed,
                "frozen": bal.entitlement_frozen,
                # Only meaningful for WORKED_DAY_RATIO (AL) — for FIXED_ANNUAL
                # policies (CL/SL/MtL) `max_days_per_year` is the entitlement
                # itself (already shown via `opening`), not an accrual cap.
                "capped_at": (
                    policy.max_days_per_year
                    if policy.accrual_method == LeavePolicy.AccrualMethod.WORKED_DAY_RATIO
                    and policy.max_days_per_year > 0
                    else None
                ),
                "balance_pk": bal.pk,
                "cycle_start": bal.cycle_start,
                "cycle_end": bal.cycle_end,
                "shows_current_cycle": policy.cycle == LeavePolicy.Cycle.ANNIVERSARY,
                # Highlighted in the drawer — the type actually being applied
                # for, so it doesn't have to be hunted for in the table.
                "is_subject": bool(impact and policy.pk == impact["policy"].pk),
            })
        rows.sort(
            key=lambda r: (
                r["policy"].shift_type.sort_order is None,
                r["policy"].shift_type.sort_order or 0,
                r["policy"].shift_type.name,
            )
        )

        ctx = {
            "page_title": f"Leave balance · {year}",
            "target": target,
            "year": year,
            "rows": rows,
            "is_self": target.pk == request.user.pk,
            "application": application,
            "impact": impact,
            "today": _date.today(),
            "can_add_cpl_grant": can_manage_cpl_grants(request.user, target, request=request),
        }
        template = (
            self.drawer_template_name if getattr(request, "htmx", False)
            else self.template_name
        )
        return render(request, template, ctx)


class LeaveBalanceTransactionsView(LoginRequiredMixin, View):
    """HTML fragment listing the ledger behind one LeaveBalance — drives the
    history drawer on the balance dashboard.

    Each row is widened past the bare ledger columns into the audit trail a
    reader actually wants: **the date the leave was taken** (the linked
    Shift's own date, or the application's date range) rather than the
    timestamp the ledger row happened to be written, plus who applied and
    who approved. Those two timestamps are wildly different for roster-direct
    leave — a bulk reconcile stamps hundreds of rows with one `occurred_at`,
    which is why the drawer used to show three identical "when"s for three
    leaves taken months apart.

    Roster-direct leave (most of the org's real usage, and *all* of the
    Jan–Aug 2026 history imported before Orbis went live) has no
    LeaveApplication, so applied/approved read "—" and the roster cell is the
    trail; the source link goes to that week of the roster instead.
    """

    http_method_names = ["get"]
    template_name = "leave/_balance_history.html"

    def get(self, request, pk):
        bal = get_object_or_404(
            LeaveBalance.objects.select_related("employee", "leave_policy__shift_type"),
            pk=pk,
        )
        if (bal.employee_id != request.user.pk
            and bal.employee_id not in visible_employee_pks(request.user, request=request)
            and not can_see_all(request.user, request=request)):
            raise PermissionDenied("You can't view this balance's history.")

        # Ordered by the date the leave was *taken*, not the model default
        # of `-occurred_at`: a bulk reconcile writes hundreds of rows under
        # one timestamp, so the default sort is arbitrary within a batch and
        # would also make the [:200] cap slice an arbitrary 200.
        txns = (
            bal.transactions
            .annotate(
                effective_date=Coalesce(
                    "shift__date",
                    "application__start_date",
                    Cast("occurred_at", DateField()),
                    output_field=DateField(),
                ),
            )
            .select_related(
                "application__applied_by",
                "application__processed_by",
                "application__leave_type",
                "shift__shift_type",
                "shift__added_by",
                "shift__updated_by",
                "created_by",
            )
            .order_by("-effective_date", "-occurred_at")[:200]
        )
        rows = [self._describe(txn, bal) for txn in txns]
        return render(
            request,
            self.template_name,
            {
                "balance": bal,
                "rows": rows,
                # Drives the footnote explaining the empty applied/approved
                # cells. Only debits/credits for actual leave count — an
                # ACCRUAL or CARRIED_FORWARD row never has an application
                # and needs no explaining.
                "has_untracked": any(
                    r["applied_at"] is None
                    and r["txn"].reason in {
                        LeaveBalanceTransaction.Reason.USED,
                        LeaveBalanceTransaction.Reason.REVERSED,
                    }
                    for r in rows
                ),
                # AL's accrual is computed live from worked days rather than
                # written as ledger rows (leave/services.py::live_accrued_al),
                # so its drawer is legitimately empty of credits — say so
                # instead of leaving a bare "No transactions yet."
                "accrual_is_live": (
                    bal.leave_policy.accrual_method
                    == LeavePolicy.AccrualMethod.WORKED_DAY_RATIO
                ),
            },
        )

    @staticmethod
    def _describe(txn, bal):
        """Flatten one transaction into the drawer's columns."""
        row = {
            "txn": txn,
            "leave_start": None,
            "leave_end": None,
            "source_label": "",
            "source_url": "",
            "applied_by": None,
            "applied_at": None,
            "approved_by": None,
            "approved_at": None,
            "recorded_by": txn.created_by,
            "recorded_at": txn.occurred_at,
        }

        app = txn.application
        shift = txn.shift
        # A CpL grant/consumption pair often has no Shift to key a date off
        # of (a manual weekly-off grant, or an uploaded historical row with
        # no matching roster row yet) — CplGrant.work_date/consumed_date is
        # then the only place either date lives, so check it before falling
        # back to app/shift. When a Shift IS present, that path below still
        # wins (it can also link back to the roster week).
        grant = None
        if shift is None and txn.reason in (
            LeaveBalanceTransaction.Reason.ACCRUAL, LeaveBalanceTransaction.Reason.USED,
        ):
            grant = (
                CplGrant.objects
                .filter(accrual_transaction=txn)
                .first()
                or CplGrant.objects.filter(consumption_transaction=txn).first()
            )

        if app is not None:
            row["leave_start"], row["leave_end"] = app.start_date, app.end_date
            row["source_label"] = f"Application #{app.pk}"
            row["source_url"] = reverse("leave_detail", args=[app.pk])
            row["applied_by"], row["applied_at"] = app.applied_by, app.applied_at
            # PENDING/DECLINED/CANCELLED applications can own ledger rows too
            # (a REVERSED undo, say) — only an approval is an approval.
            if app.status == LeaveApplication.Status.APPROVED:
                row["approved_by"], row["approved_at"] = app.processed_by, app.processed_at
        elif shift is not None:
            row["leave_start"] = row["leave_end"] = shift.date
            row["source_label"] = f"Roster · {shift.shift_type.name}"
            monday = shift.date - timedelta(days=shift.date.weekday())
            row["source_url"] = "{}?{}".format(
                reverse("roster_week"),
                urlencode({
                    "from": monday.isoformat(),
                    "to": (monday + timedelta(days=6)).isoformat(),
                    "employee": str(bal.employee_id),
                }),
            )
            # Nobody "applied" for a roster-direct leave, but somebody did set
            # the cell — that person is the closest thing to an audit trail.
            row["recorded_by"] = txn.created_by or shift.updated_by or shift.added_by
        elif grant is not None:
            if txn.reason == LeaveBalanceTransaction.Reason.ACCRUAL:
                row["leave_start"] = row["leave_end"] = grant.work_date
                pairing = (
                    f"taken {grant.consumed_date}" if grant.consumed_date
                    else "not yet taken"
                )
                row["source_label"] = f"CpL grant ({grant.get_work_context_display()}) — {pairing}"
            else:
                row["leave_start"] = row["leave_end"] = grant.consumed_date
                row["source_label"] = f"CpL consumption — earned {grant.work_date}"
            row["recorded_by"] = txn.created_by or grant.added_by
        return row


# ---------------------------------------------------------------------------
# Compensatory leave register
# ---------------------------------------------------------------------------

class CplGrantListView(LoginRequiredMixin, PageSizeMixin, ListView):
    """Who earned CpL for which holiday (or scheduled weekly off day), and
    when they took it — the in-app view of leave.CplGrant, scoped the same
    way as everything else (self / managed scope / everyone), so it works
    without Django admin access. Same q/department/team filter bar as the
    employee list (accounts.filters), plus status/source dropdowns."""
    model = CplGrant
    template_name = "leave/cpl_grant_list.html"
    context_object_name = "grants"
    paginate_by = 25

    def get_queryset(self):
        request = self.request
        if can_see_all(request.user, request=request):
            employees = CustomUser.objects.all()
        else:
            employees = CustomUser.objects.filter(
                pk__in=visible_employee_pks(request.user, request=request)
            )
        employees = apply_employee_filters(employees, request.user, request)

        qs = (
            CplGrant.objects
            .filter(employee_id__in=employees.values("pk"))
            .select_related(
                "employee", "employee__department", "employee__team",
                "holiday", "work_shift__shift_type", "consumption_shift__shift_type",
            )
            .order_by("-work_date")
        )
        status = request.GET.get("status")
        if status in CplGrant.Status.values:
            qs = qs.filter(status=status)
        source = request.GET.get("source")
        if source in CplGrant.Source.values:
            qs = qs.filter(source=source)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Compensatory Leave Register"
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["source_filter"] = self.request.GET.get("source", "")
        ctx["status_choices"] = CplGrant.Status.choices
        ctx["source_choices"] = CplGrant.Source.choices
        ctx["can_add_grant"] = can_edit_all(self.request.user, request=self.request) or has_managed_scope(
            self.request.user, request=self.request,
        )
        ctx.update(employee_filter_context(self.request.user, self.request))
        return ctx


# ---------------------------------------------------------------------------
# Encashment workflow
# ---------------------------------------------------------------------------

class EncashmentListView(LoginRequiredMixin, PageSizeMixin, ListView):
    """Employees see their own requests; Admins / HR see all."""
    model = EncashmentRequest
    template_name = "leave/encashment_list.html"
    context_object_name = "requests"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            EncashmentRequest.objects
            .select_related("employee", "leave_policy", "leave_policy__shift_type")
            .order_by("-requested_at")
        )
        if can_edit_all(self.request.user, request=self.request):
            return qs
        return qs.filter(employee=self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Encashment"
        ctx["can_decide"] = can_edit_all(self.request.user, request=self.request)
        return ctx


class EncashmentRequestView(LoginRequiredMixin, View):
    """Employee submits a manual encashment request. Year-end auto-creation
    happens in the rollover management command and bypasses this view."""
    template_name = "leave/encashment_request.html"

    def get(self, request):
        ctx = self._get_context()
        return render(request, self.template_name, ctx)

    def post(self, request):
        try:
            policy_pk = int(request.POST.get("leave_policy"))
            days = Decimal(request.POST.get("days_requested"))
            year = int(request.POST.get("year") or _date.today().year)
        except (TypeError, ValueError):
            messages.error(request, "Invalid encashment request.")
            return redirect("encashment_request")

        policy = get_object_or_404(
            LeavePolicy.objects.select_related("shift_type"),
            pk=policy_pk,
            encash_carry_forward_excess=True,
        )
        bal = get_or_create_balance(request.user, policy, _date(year, 1, 1))
        if days <= 0 or days > bal.available:
            messages.error(
                request,
                f"You can request up to {bal.available} day(s) of "
                f"{policy.shift_type.name} encashment.",
            )
            return redirect("encashment_request")

        EncashmentRequest.objects.create(
            employee=request.user,
            leave_policy=policy,
            year=year,
            days_requested=days,
            note=request.POST.get("note", "").strip(),
            added_by=request.user,
            updated_by=request.user,
        )
        messages.success(request, "Encashment request submitted.")
        return redirect("encashment_list")

    def _get_context(self):
        encashable = LeavePolicy.objects.filter(
            encash_carry_forward_excess=True,
        ).select_related("shift_type")
        year = _date.today().year
        rows = []
        for policy in encashable:
            bal = get_or_create_balance(self.request.user, policy, _date.today())
            rows.append({
                "policy": policy,
                "name": policy.shift_type.name,
                "available": bal.available,
            })
        return {
            "page_title": "Request encashment",
            "rows": rows,
            "year": year,
        }


class MonthlyHRReportView(LoginRequiredMixin, View):
    """HR-only CSV export of leave activity for a month.

    Columns: employee_id, employee_email, name, department, leave_type,
    start_date, end_date, working_days, sandwich_days, effective_days,
    status, applied_at, processed_at, processed_by.
    """

    def get(self, request):
        if not can_edit_all(request.user, request=request):
            raise PermissionDenied("Admins / HR only.")

        month = request.GET.get("month") or _date.today().strftime("%Y-%m")
        try:
            year, m = month.split("-")
            year = int(year)
            m = int(m)
        except ValueError:
            return HttpResponse("month must be YYYY-MM", status=400)

        from calendar import monthrange
        last = monthrange(year, m)[1]
        start = _date(year, m, 1)
        end = _date(year, m, last)

        rows = (
            LeaveApplication.objects
            .select_related(
                "employee", "employee__department", "leave_type",
                "applied_by", "processed_by",
            )
            .filter(
                applied_at__date__gte=start,
                applied_at__date__lte=end,
            )
            .order_by("applied_at")
        )

        import csv
        from io import StringIO
        buf = StringIO()
        w = csv.writer(buf)
        w.writerow([
            "employee_id", "email", "name", "department",
            "leave_type", "start_date", "end_date",
            "working_days", "sandwich_days", "effective_days",
            "status", "applied_at", "processed_at", "processed_by",
        ])
        for r in rows:
            w.writerow([
                r.employee.employee_id or "",
                r.employee.email,
                escape_spreadsheet(r.employee.display_name),
                escape_spreadsheet(r.employee.department.name if r.employee.department else ""),
                escape_spreadsheet(r.leave_type.name),
                r.start_date.isoformat(),
                r.end_date.isoformat(),
                r.working_days,
                r.sandwich_days,
                r.effective_days,
                r.status,
                r.applied_at.isoformat(timespec="minutes"),
                r.processed_at.isoformat(timespec="minutes") if r.processed_at else "",
                escape_spreadsheet(r.processed_by.display_name if r.processed_by else ""),
            ])

        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="leave-report-{month}.csv"'
        return resp


class ShiftSummaryView(LoginRequiredMixin, View):
    """The shift summary, in two requests so the page never blocks on it.

    A plain GET renders only the shell (filters + validation errors, no
    queries for the report). The results area then fetches the same URL via
    htmx (`hx-trigger="load"`, target ``#shift-summary-results``), and that
    request paginates the *employee list* before building anything — so the
    per-employee tallies and AL snapshot only run for the 25–100 people on
    the current page. ``?export=xlsx`` builds every row (an explicit
    download); see leave/exports.py.
    """

    template_name = "leave/shift_summary.html"
    results_template_name = "leave/_shift_summary_results.html"
    results_target = "shift-summary-results"

    def get(self, request):
        today = _date.today()
        start_date = parse_date(request.GET.get("start_date") or f"{today.year}-01-01")
        end_date = parse_date(request.GET.get("end_date") or today.isoformat())
        # Locked users are forced to their own department; privileged users honour
        # the querystring (``None`` = all departments).
        department_id, team_id = effective_filter_ids(request.user, request)
        errors = []

        if not start_date:
            errors.append("Start date must be YYYY-MM-DD.")
        if not end_date:
            errors.append("End date must be YYYY-MM-DD.")
        if start_date and end_date and not errors:
            try:
                validate_shift_summary_range(start_date, end_date)
            except (CrossYearSummaryError, ValueError) as exc:
                errors.append(str(exc))

        filters = None if errors else ShiftSummaryFilters(
            start_date=start_date,
            end_date=end_date,
            department_id=int(department_id) if department_id else None,
            team_id=team_id,
            q=request.GET.get("q"),
        )

        # Same branch-before-pagination as attendance/payroll exports: the
        # full filtered row set, never just the current page. A bad range
        # falls through and renders the page with its errors instead.
        if request.GET.get("export") == "xlsx" and filters:
            rows = build_shift_summary(filters, viewer=request.user, request=request)
            return build_shift_summary_export(
                rows, start_date, end_date, _shift_summary_scope_label(department_id, team_id),
            )

        if self._is_results_request(request):
            ctx = {"start_date": start_date, "end_date": end_date, "errors": errors}
            if filters:
                employees = shift_summary_employees(filters, viewer=request.user, request=request)
                # Paginate people first, then build rows for this page only —
                # the order (employee ID) doesn't depend on the computed numbers.
                ctx.update(paginate_list(request, employees))
                ctx["rows"] = build_shift_summary_rows(ctx["rows"], filters)
            return render(request, self.results_template_name, ctx)

        ctx = {
            "page_title": "Shift summary",
            "start_date": start_date,
            "end_date": end_date,
            "errors": errors,
            "results_target": self.results_target,
        }
        ctx.update(employee_filter_context(request.user, request))
        return render(request, self.template_name, ctx)

    def _is_results_request(self, request) -> bool:
        """The htmx fetch of the results area (initial load or a pager link) —
        not a history-restore miss, which needs the whole page back."""
        htmx = getattr(request, "htmx", None)
        return bool(htmx) and not htmx.history_restore_request and htmx.target == self.results_target


def _shift_summary_scope_label(department_id, team_id) -> str:
    """'Operations / Support', 'Operations', or '' (all departments) for the
    export's title row and filename. The ids are already clamped by
    effective_filter_ids, so this only names them."""
    from accounts.models import Department, Team

    department = Department.objects.filter(pk=department_id).first() if department_id else None
    team = Team.objects.filter(pk=team_id).first() if team_id else None
    return " / ".join(obj.name for obj in (department, team) if obj)


class EmployeeLeaveRegisterView(LoginRequiredMixin, View):
    """Per-employee Form-9 style leave register. HR-readable."""

    template_name = "leave/employee_register.html"

    def get(self, request, pk):
        target = get_object_or_404(CustomUser, pk=pk)
        if not (can_see_all(request.user, request=request)
                or target.pk == request.user.pk
                or target.pk in visible_employee_pks(request.user, request=request)):
            raise PermissionDenied("You can't view this employee's register.")

        year = int(request.GET.get("year") or _date.today().year)

        # All approved applications in `year`.
        approvals = (
            LeaveApplication.objects
            .filter(
                employee=target,
                status=LeaveApplication.Status.APPROVED,
                start_date__year=year,
            )
            .select_related("leave_type")
            .order_by("start_date")
        )

        # Year-summary balances — only entitlement-tracking policies.
        tracking_policy_pks = [
            p.pk for p in LeavePolicy.objects.all() if p.tracks_balance
        ]
        balances = (
            LeaveBalance.objects
            .filter(employee=target, year=year, leave_policy_id__in=tracking_policy_pks)
            .select_related("leave_policy", "leave_policy__shift_type")
            .order_by(F("leave_policy__shift_type__sort_order").asc(nulls_last=True), "leave_policy__shift_type__name")
        )

        ctx = {
            "page_title": f"Leave register · {target.display_name} · {year}",
            "target": target,
            "year": year,
            "approvals": approvals,
            "balances": balances,
        }
        return render(request, self.template_name, ctx)


class EncashmentDecideView(LoginRequiredMixin, View):
    """HR approves / rejects / marks paid."""
    http_method_names = ["post"]

    def post(self, request, pk):
        if not can_edit_all(request.user, request=request):
            raise PermissionDenied("Only Admins / HR can process encashments.")
        req = get_object_or_404(EncashmentRequest, pk=pk)
        action = request.POST.get("action")
        if action == "approve":
            try:
                days_approved = Decimal(
                    request.POST.get("days_approved") or req.days_requested
                )
            except Exception:
                days_approved = req.days_requested
            req.status = EncashmentRequest.Status.APPROVED
            req.days_approved = days_approved
        elif action == "reject":
            req.status = EncashmentRequest.Status.REJECTED
        elif action == "mark_paid":
            if req.status not in (
                EncashmentRequest.Status.APPROVED,
                EncashmentRequest.Status.PAID,
            ):
                messages.error(request, "Only APPROVED requests can be marked paid.")
                return redirect("encashment_list")
            # Debit the balance for the approved days.
            policy = req.leave_policy
            bal = get_or_create_balance(req.employee, policy, _date(req.year, 1, 1))
            days = req.days_approved or Decimal("0")
            if days > 0 and req.status == EncashmentRequest.Status.APPROVED:
                from leave.models import LeaveBalanceTransaction
                LeaveBalanceTransaction.objects.create(
                    balance=bal,
                    delta=-days,
                    reason=LeaveBalanceTransaction.Reason.ENCASHED,
                    application=None,
                    created_by=request.user,
                    note=f"Encashment #{req.pk}",
                )
                from django.db.models import F
                bal.encashed = F("encashed") + days
                bal.save(update_fields=["encashed", "updated_at"])
            req.status = EncashmentRequest.Status.PAID
        else:
            messages.error(request, "Unknown action.")
            return redirect("encashment_list")

        req.processed_by = request.user
        req.processed_at = timezone.now()
        req.note = request.POST.get("note", req.note)
        req.updated_by = request.user
        req.save()
        messages.success(request, f"Encashment {req.get_status_display().lower()}.")
        return redirect("encashment_list")


def _hx_redirect(url: str) -> HttpResponse:
    """204 No Content with HX-Redirect — htmx performs a full client-side
    navigation. Same convention as attendance.views._hx_redirect /
    policies.views._hx_redirect."""
    resp = HttpResponse(status=204)
    resp["HX-Redirect"] = url
    return resp


class CplGrantManualCreateView(LoginRequiredMixin, View):
    """Manually record a `CplGrant` for an employee who worked a scheduled
    weekly off day (or another ad-hoc reason) — there's no Holiday to
    auto-detect this from, so it's always a deliberate HR/manager entry
    (see leave/models.py::CplGrant.WorkContext, accounts.permissions::
    can_manage_cpl_grants). Never self-service.

    Same drawer-or-page split as attendance.AttendanceCorrectionRequestView:
    htmx GET returns a right-hand drawer fragment (`?for=<employee_pk>`), a
    non-htmx GET falls back to a full page for direct navigation. A
    successful POST returns 204 + HX-Redirect (htmx) or a normal redirect.
    """

    def _resolve_employee(self, request):
        raw = request.GET.get("for") or request.POST.get("for")
        if not raw:
            raise Http404("An employee must be specified via ?for=<pk>.")
        target = get_object_or_404(CustomUser, pk=raw)
        if not can_manage_cpl_grants(request.user, target, request=request):
            raise PermissionDenied("You can't record a CpL grant for that employee.")
        return target

    def _redirect_target(self, target):
        return f"{reverse('leave_balance')}?for={target.pk}"

    def _render(self, request, form, target, *, status=200):
        ctx = {
            "form": form,
            "target_employee": target,
            "treatment_warning": effective_cpl_treatment(target) == "NONE",
            "action_url": f"{reverse('cpl_grant_manual_create')}?for={target.pk}",
        }
        if request.htmx:
            return render(request, "leave/_cpl_grant_manual_drawer.html", ctx, status=status)
        ctx["page_title"] = f"Add CpL grant — {target.display_name}"
        return render(request, "leave/cpl_grant_manual_create.html", ctx, status=status)

    def get(self, request):
        target = self._resolve_employee(request)
        form = CplGrantManualForm()
        return self._render(request, form, target)

    def post(self, request):
        target = self._resolve_employee(request)
        if not target.department_id:
            messages.error(request, f"{target.display_name} has no department set — can't record a CpL grant.")
            url = self._redirect_target(target)
            return _hx_redirect(url) if request.htmx else redirect(url)

        form = CplGrantManualForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, target, status=400)

        grant = form.save(commit=False)
        grant.employee = target
        grant.department = target.department
        grant.source = CplGrant.Source.MANUAL
        grant.status = CplGrant.Status.OPEN
        grant.added_by = request.user
        grant.updated_by = request.user
        grant.full_clean()
        grant.save()
        credit_manual_cpl_grant(grant, created_by=request.user)

        if effective_cpl_treatment(target) == "NONE":
            messages.warning(
                request,
                f"Note: {target.display_name}'s department is overtime-only for CpL — "
                "recorded anyway since you chose to. Double-check this is the right call.",
            )
        messages.success(
            request,
            f"CpL grant recorded for {target.display_name} — worked {grant.work_date}.",
        )
        url = self._redirect_target(target)
        return _hx_redirect(url) if request.htmx else redirect(url)


from payroll.reports import (
    CrossYearSummaryError,
    ShiftSummaryFilters,
    build_shift_summary,
    build_shift_summary_rows,
    shift_summary_employees,
    validate_shift_summary_range,
)

# The second, unrouted PayrollReportView + templates/leave/payroll_report.html
# that used to live here (a weaker-scoped duplicate of payroll.views.PayrollReportView,
# the real one routed at /payroll/report/) were deleted 2026-09-07 per
# docs/PAYROLL_PLAN.md Risk 12 — dead code flagged for removal since Phase 3.
