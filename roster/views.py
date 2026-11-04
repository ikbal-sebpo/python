"""Roster views — weekly grid with pattern pre-fill, inline cell edit,
weekly-pattern edit page, plus the existing shift-type CRUD."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Q
from django.forms import formset_factory
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from core.approvals import APPROVALS_CHANGED_EVENT
from core.pagination import DEFAULT_PAGE_SIZE, PageSizeMixin, paginate_list, resolve_page_size

from accounts.filters import effective_filter_ids, employee_filter_context, employee_search_q
from accounts.models import CustomUser, Team
from accounts.permissions import (
    can_act_on_shift_change,
    can_cancel_shift_change,
    can_edit_all,
    can_edit_roster_pattern,
    can_request_shift_change,
    can_see_all,
    can_see_all_departments,
    can_send_roster_email,
    direct_report_pks,
    effective_department_id,
    has_managed_scope,
    shift_change_needs_department_head,
    visible_departments,
    visible_employee_pks,
    visible_employees,
    visible_teams,
)
from accounts.views import StaffRequiredMixin
from roster import night_window, services
from roster.forms import RosterPatternRowForm, ShiftChangeRequestForm, ShiftTypeForm
from roster.models import (
    Holiday, Roster, Shift, ShiftAttribute, ShiftChangeLog, ShiftChangeRequest, ShiftType,
)


MAX_RANGE_DAYS = 31
# A single-employee view is one row, so a wider window is cheap to render and
# lets links from the shift-summary report (which can span months) land on the
# day they care about instead of being clamped to the first 31 days.
SINGLE_EMP_MAX_RANGE_DAYS = 186


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _sunday_of(d: date) -> date:
    return _monday_of(d) + timedelta(days=6)


def _parse_date_param(raw: str | None) -> date | None:
    if not raw:
        return None
    return parse_date(raw)


def _parse_int(value):
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _parse_uuid(value):
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _effective_team_id(user, request, department_id):
    """The team id to filter by, clamped to a team the viewer may pick.

    Returns ``None`` when no team is selected or the requested team is outside
    the viewer's allowed (department-scoped) set — this stops a locked user
    from filtering by another department's team via a tampered querystring.
    """
    team_id = _parse_int(request.GET.get("team"))
    if not team_id:
        return None
    allowed = visible_teams(user, request=request, department_id=department_id)
    if allowed.filter(pk=team_id).exists():
        return team_id
    return None


def _apply_grid_filters(qs, request):
    """Apply the department / team / employee filters, scoped to the viewer.

    Department is resolved via ``effective_department_id`` (locked users are
    forced to their own department); team is clamped to that department.
    """
    dept_id = effective_department_id(request.user, request)
    if dept_id:
        qs = qs.filter(department_id=dept_id)
    team_id = _effective_team_id(request.user, request, dept_id)
    if team_id:
        qs = qs.filter(team_id=team_id)
    employee = _parse_uuid(request.GET.get("employee"))
    if employee:
        qs = qs.filter(pk=employee)
    return qs


def _apply_employee_search(qs, q):
    """Name / email / employee-ID filter — the shared `employee_search_q`."""
    cond = employee_search_q(q)
    return qs if cond is None else qs.filter(cond)


# Any of these params in the querystring puts the grid into "search mode": the
# base set widens from the viewer's direct reports to their full visible scope
# (downline, or the whole org for HR/Admins), then filtered by the params.
_SEARCH_KEYS = ("q", "department", "team", "employee")


def _search_active(params) -> bool:
    return any((params.get(k) or "").strip() for k in _SEARCH_KEYS)


def _org_wide_flat(request, view_mode) -> bool:
    """True when the flat view opens on the whole org rather than the
    viewer's direct reports: HR / Admins / superusers (``can_see_all``).

    Their "All departments" pick submits ``department=`` (empty), which
    `_search_active` can't tell apart from "no filter", so without this an
    HR user exporting a payroll period only ever got their own direct
    reports. Flat view only — the grid renders an editable cell per
    employee-day and materialises pattern rows, so it keeps the narrow
    default.
    """
    return view_mode == "flat" and can_see_all(request.user, request=request)


def _resolve_grid_employees(request, *, org_wide=False):
    """The employee queryset backing the grid, honouring the default/search split.

    Default (no search params): the viewer's direct reports only. Search mode:
    the full visible scope filtered by the params. Either way the base is
    `visible_employees`, so results never escape the viewer's downline.

    ``org_wide`` (see `_org_wide_flat`) starts from the full visible scope
    like search mode does, separated employees included — a leaver's shifts
    inside a payroll period still belong in that period's export — minus
    accounts with no employee ID (service/test logins such as "intern.hr01"
    are not employees; same rule as `payroll.periods.employed_during_q`).

    Search mode also widens past the is_active clamp
    (`include_separated=True`): a separated employee's historical Shift rows
    outlive their account, and a date range that predates their last working
    day is a legitimate thing to look up. Permission *scoping* is unchanged —
    only someone who could already see them sees them. The default "my team"
    grid keeps the active-only roster, and writes stay blocked regardless
    (CellUpdateView still requires is_active=True).
    """
    widen = org_wide or _search_active(request.GET)
    qs = visible_employees(request.user, request=request, include_separated=widen)
    if org_wide:
        qs = qs.exclude(employee_id__isnull=True).exclude(employee_id="")
    if not widen:
        # Default view = the viewer's own roster + their direct reports.
        qs = qs.filter(
            pk__in=direct_report_pks(request.user, request=request) | {request.user.pk}
        )
    qs = _apply_grid_filters(qs, request)
    qs = _apply_employee_search(qs, request.GET.get("q"))
    return qs


def _range_from_request(request, capped=True):
    """Return (date_from, date_to) — default Mon-Sun of current week.

    ``capped`` clamps the span to keep the *grid* bounded: it draws a column
    per day with an editable dropdown in every cell, and materialises pattern
    rows across the whole range on view. A single-employee view
    (``?employee=``) gets a much larger cap since it renders just one row.

    The flat view passes ``capped=False`` — it is read-only and paginated in
    SQL, so its cost is per page, not per day of range.
    """
    today = date.today()
    df = _parse_date_param(request.GET.get("from")) or _monday_of(today)
    dt = _parse_date_param(request.GET.get("to")) or _sunday_of(today)
    if dt < df:
        dt = df
    if not capped:
        return df, dt
    cap = SINGLE_EMP_MAX_RANGE_DAYS if request.GET.get("employee") else MAX_RANGE_DAYS
    if (dt - df).days + 1 > cap:
        dt = df + timedelta(days=cap - 1)
    return df, dt


def _recent_payroll_periods(today, count=3):
    """The current payroll period (16th → 15th) and the ``count - 1`` before
    it, newest first, for the toolbar's "Payroll period" quick-range menu.
    Named after the month each closes in, as ``payroll.periods`` does."""
    from payroll.periods import period_bounds, period_for_date

    year, month = period_for_date(today)
    periods = []
    for _ in range(count):
        start, end = period_bounds(year, month)
        periods.append({
            "label": date(year, month, 1).strftime("%B %Y"),
            "from": start.isoformat(),
            "to": end.isoformat(),
        })
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return periods


def _filter_querystring(params):
    """Build the dept/team filter slug used in hx-post URLs so count
    recomputation stays scoped to the viewed subset."""
    bits = []
    for key in ("department", "team", "from", "to", "employee", "q"):
        v = params.get(key)
        if v:
            bits.append(f"{key}={v}")
    return "&".join(bits)


def _resolve_shift_ref(ref, department_id=None, include_attr_ids=()):
    """Parse the cell/pattern `<select>` value — see
    `roster.services.resolve_shift_ref`, which owns the logic (shared with
    the shift-change request form). The views keep the 404 contract."""
    try:
        return services.resolve_shift_ref(ref, department_id, include_attr_ids)
    except services.ShiftRefError as exc:
        raise Http404(exc.messages[0] if exc.messages else "Invalid shift reference.")


def _night_cell_flags(user, emp, in_window: bool) -> dict:
    """`night_window` marks a cell inside an APPROVED dedicated night shift
    window; `night_locked` additionally greys out the working shifts other
    than NIG/CUST for anyone but the department head (roster/night_window.py
    — CellUpdateView enforces the same rule server-side)."""
    return {
        "night_window": in_window,
        "night_locked": in_window and not night_window.is_night_window_approver(user, emp),
    }


def _active_shift_choice_groups(department_id=None, include_attr_ids=()):
    return services.shift_choice_groups(department_id, include_attr_ids)


def _shift_status(shift: Shift | None) -> str:
    return shift.shift_type.name if shift else ""


def _build_flat_rows(shifts):
    """Render one already-paginated page of Shift rows as tracker-table dicts.

    Filtering, ordering and slicing all happen in SQL (see
    ``RosterWeekView._flat_context``) so an unbounded date range only ever
    materialises the rows on screen.
    """
    return [
        {
            "date": s.date,
            "employee_id": s.employee.employee_id or "",
            "name": s.employee.display_name,
            "team": s.employee.team.name if s.employee.team_id else "",
            "status": _shift_status(s),
        }
        for s in shifts
    ]


def _flat_shifts(employees_qs, date_from, date_to, status_id=None):
    """The flat view's Shift queryset — shared by the page and its export so
    the download is exactly the rows the table pages through.

    Takes the employee *queryset* and filters by subquery: org-wide, a
    literal ``IN`` list would carry every employee's UUID.
    """
    shifts = (
        Shift.objects
        .filter(
            employee_id__in=employees_qs.values("pk"),
            date__gte=date_from, date__lte=date_to,
        )
        .select_related("shift_type", "employee", "employee__team")
        # Ordering must be total, or rows shuffle between pages —
        # unique_together (employee, date) makes this one exactly that.
        # Sorting by a joined name column instead costs a filesort over
        # every matching row to fetch a 25-row page.
        .order_by("date", "employee_id")
    )
    if status_id:
        shifts = shifts.filter(shift_type_id=status_id)
    return shifts


# ---------------------------------------------------------------------------
# Weekly grid
# ---------------------------------------------------------------------------

class RosterWeekView(LoginRequiredMixin, View):
    template_name = "roster/week.html"

    def get(self, request):
        # `_resolve_grid_employees` already scopes to self + direct reports
        # (or the full managed scope in search mode), so a plain employee
        # with no downline naturally just sees their own row. Managers/HR
        # get the full grid + toolbar; everyone else gets a read-only
        # personal view — the template branches on `is_manager` below.
        is_manager = has_managed_scope(request.user, request=request)
        view_mode = "flat" if is_manager and request.GET.get("view") == "flat" else "grid"

        # Only the grid is capped. It renders a column per day with an
        # editable dropdown in every cell — O(employees × days) of DOM — and
        # materialises pattern rows for the whole range on view. The flat
        # view is a read-only table sliced by SQL, so it takes any range.
        date_from, date_to = _range_from_request(
            request, capped=(view_mode == "grid"),
        )
        requested_to = _parse_date_param(request.GET.get("to"))
        range_clamped = bool(requested_to and requested_to > date_to)

        org_wide = _org_wide_flat(request, view_mode)
        employees_qs = _resolve_grid_employees(request, org_wide=org_wide)
        if view_mode == "flat":
            # The flat view filters shifts by subquery and never needs the
            # employee rows themselves — except the one-employee header.
            employees = (
                list(employees_qs[:2]) if _parse_uuid(request.GET.get("employee")) else []
            )
            mode_ctx = self._flat_context(request, employees_qs, date_from, date_to)
        else:
            employees = list(
                employees_qs.select_related(
                    "department", "designation", "team", "job_location", "work_location",
                ).order_by("first_name", "last_name")
            )
            # In the default view, pin the viewer's own row to the top; reports
            # stay alphabetical below. (Search/filter views keep plain A–Z order.)
            if not _search_active(request.GET):
                employees.sort(
                    key=lambda e: (e.pk != request.user.pk, e.first_name or "", e.last_name or "")
                )
            mode_ctx = self._grid_context(request, employees, date_from, date_to)

        today = date.today()
        this_mon, this_sun = _monday_of(today), _sunday_of(today)
        next_mon = this_mon + timedelta(days=7)
        next_sun = this_sun + timedelta(days=7)

        # Dropdown selection: locked users are forced to their own department;
        # the team is clamped to that department.
        selected_dept_id = effective_department_id(request.user, request)
        selected_team_id = _effective_team_id(request.user, request, selected_dept_id)

        selected_team = (
            Team.objects.select_related("department").filter(pk=selected_team_id, is_active=True).first()
            if selected_team_id else None
        )

        ctx = {
            "page_title": "Roster",
            # "Email roster" needs one concrete team to send for.
            "can_email_roster": bool(
                selected_team and can_send_roster_email(request.user, selected_team, request=request)
            ),
            "is_manager": is_manager,
            "org_wide": org_wide,
            "payroll_periods": _recent_payroll_periods(today),
            "date_from": date_from,
            "date_to": date_to,
            "filter_qs": _filter_querystring(request.GET),
            "this_mon": this_mon.isoformat(),
            "this_sun": this_sun.isoformat(),
            "next_mon": next_mon.isoformat(),
            "next_sun": next_sun.isoformat(),
            "departments": visible_departments(request.user, request=request),
            # Every team the viewer may pick, NOT pre-narrowed to
            # `selected_dept_id`: the popover cascades client-side, so the
            # full set has to be in the DOM. Locked users are still clamped
            # to their own department inside visible_teams(), and a submitted
            # team is re-checked by _effective_team_id() above.
            "teams": visible_teams(request.user, request=request),
            "selected_department_id": selected_dept_id,
            "selected_team_id": selected_team_id,
            "can_pick_all_departments": can_see_all_departments(
                request.user, request=request,
            ),
            # When the grid is scoped to one employee (e.g. arriving from the
            # shift-summary report), expose the param + object so navigation can
            # keep the scope and the header can show a "showing one / show all".
            "employee_param": request.GET.get("employee") or "",
            "q": request.GET.get("q", ""),
            "search_active": _search_active(request.GET),
            "scoped_employee": (
                employees[0]
                if _parse_uuid(request.GET.get("employee")) and len(employees) == 1
                else None
            ),
            # Pattern-edit gating is per-row; computing once is fine since
            # the same user can edit anyone they can see.
            "can_manage_patterns": can_edit_all(request.user, request=request),
            "view_mode": view_mode,
            # Grid only: say so when the requested range was truncated, rather
            # than silently showing a shorter span than the user asked for.
            "range_clamped": range_clamped,
            "range_cap_days": (
                SINGLE_EMP_MAX_RANGE_DAYS if request.GET.get("employee")
                else MAX_RANGE_DAYS
            ),
            # The button tint covers the flat-only status filter too, but
            # `search_active` stays as-is: it also widens the employee scope,
            # and a status filter must not do that.
            "filters_active": (
                _search_active(request.GET) or bool(mode_ctx.get("flat_status_id"))
            ),
            **mode_ctx,
        }
        return render(request, self.template_name, ctx)

    # -- flat (tracker) view ------------------------------------------------

    def _flat_context(self, request, employees_qs, date_from, date_to):
        """One row per shift, paginated in SQL so the range can be unbounded.

        Deliberately does NOT call ``prefill_shifts_from_pattern``: that
        writes a Shift row for every (employee, day) in the range that has a
        weekly pattern, which is fine for a capped grid but would mass-create
        rows here. This view reads what is actually rostered.
        """
        flat_status_id = _parse_int(request.GET.get("status"))
        shifts = _flat_shifts(employees_qs, date_from, date_to, flat_status_id)

        # Paginator slices the queryset, so only the current page is fetched.
        page = paginate_list(request, shifts)
        return {
            # `paginate_list` names the slice `rows`, which the weekly grid
            # already owns — remap rather than spreading it wholesale.
            "flat_rows": _build_flat_rows(page["rows"]),
            "page_obj": page["page_obj"],
            "paginator": page["paginator"],
            "is_paginated": page["is_paginated"],
            "page_sizes": page["page_sizes"],
            "current_page_size": page["current_page_size"],
            "flat_status_id": flat_status_id,
            "flat_status_types": ShiftType.objects.filter(is_active=True),
        }

    # -- weekly grid --------------------------------------------------------

    def _grid_context(self, request, employees, date_from, date_to):
        days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]

        # Materialise missing Shift rows from each employee's weekly pattern.
        services.prefill_shifts_from_pattern(request.user, employees, date_from, date_to)

        # Pull every Shift in one go, including the attribute (for time display).
        shift_rows = (
            Shift.objects
            .filter(employee__in=employees, date__gte=date_from, date__lte=date_to)
            # job_location/work_location are read per cell by the
            # resolved_location_choice tag — without them here every non-null
            # location FK is a lazy fetch, O(employees × days) queries.
            .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        )
        shift_map = {(s.employee_id, s.date): s for s in shift_rows}

        holidays = {
            h.date: h for h in Holiday.objects.filter(
                date__gte=date_from, date__lte=date_to, is_active=True,
            )
        }

        # Shift-count aggregation for the sticky header — WORK-category only.
        work_shift_types = list(
            ShiftType.objects
            .filter(is_active=True, category=ShiftType.Category.WORK)
            .order_by(F("sort_order").asc(nulls_last=True), "name")
        )
        count_rows = (
            Shift.objects
            .filter(
                employee__in=employees,
                date__gte=date_from, date__lte=date_to,
                shift_type__category=ShiftType.Category.WORK,
            )
            .values("date", "shift_type_id")
            .annotate(n=Count("id"))
        )
        count_map = {(r["date"], r["shift_type_id"]): r["n"] for r in count_rows}

        # Assemble per-day headers.
        day_headers = [
            {
                "date": d,
                "holiday": holidays.get(d),
                "counts": [(st, count_map.get((d, st.pk), 0)) for st in work_shift_types],
            }
            for d in days
        ]

        # Per-row cell list + per-row totals (count by ShiftType.name) +
        # off_count for the "more than 2 OFFs → red employee name" rule.
        #
        # The dropdown is one shared template reused across every cell, so it
        # can only be scoped to a single department. Key it off who's actually
        # on screen, not the viewer's own department — a manager with a
        # cross-department grant editing someone else's row must not have the
        # dropdown scoped to their own department instead. A grid spanning
        # more than one department falls back to unrestricted (safe default).
        grid_dept_ids = {e.department_id for e in employees if e.department_id}
        grid_department_id = grid_dept_ids.pop() if len(grid_dept_ids) == 1 else None
        grid_include_attr_ids = {
            s.shift_attribute_id for s in shift_map.values() if s.shift_attribute_id
        }
        shift_choice_groups = _active_shift_choice_groups(
            department_id=grid_department_id, include_attr_ids=grid_include_attr_ids,
        )
        # Pending shift-change requests in view, so a cell can flag "change
        # pending" and link to the request (docs/SHIFT_CHANGE_REQUEST_PLAN.md).
        pending_changes = {
            (r.employee_id, r.date): r.pk
            for r in ShiftChangeRequest.objects.filter(
                employee__in=employees,
                date__gte=date_from, date__lte=date_to,
                status=ShiftChangeRequest.Status.PENDING,
            ).only("pk", "employee_id", "date")
        }
        windows = night_window.windows_by_employee(
            [e.pk for e in employees], date_from, date_to,
        )
        rows = []
        for emp in employees:
            cells = []
            totals_map: dict[str, int] = {}
            off_count = 0
            is_approver = (
                emp.pk in windows and night_window.is_night_window_approver(request.user, emp)
            )
            # Per-row edit gating: a row the viewer can't edit renders as
            # read-only text with a "Request change" link instead of a
            # dropdown that 403s on change. managed_scope_pks is cached per
            # request, so this is one cheap set lookup per employee.
            can_edit = can_edit_roster_pattern(request.user, emp, request=request)
            for d in days:
                shift = shift_map.get((emp.pk, d))
                in_win = night_window.date_in_windows(windows, emp.pk, d)
                cells.append({
                    "date": d,
                    "shift": shift,
                    "holiday": holidays.get(d),
                    "pending_change_pk": pending_changes.get((emp.pk, d)),
                    "night_window": in_win,
                    "night_locked": in_win and not is_approver,
                })
                if shift is not None:
                    name = shift.shift_type.name
                    totals_map[name] = totals_map.get(name, 0) + 1
                    if shift.shift_type.category == ShiftType.Category.OFF:
                        off_count += 1
            rows.append({
                "employee": emp,
                "cells": cells,
                "totals": sorted(totals_map.items()),
                "off_count": off_count,
                "can_edit": can_edit,
            })

        return {
            "days": days,
            "day_headers": day_headers,
            "rows": rows,
            "shift_choice_groups": shift_choice_groups,
        }

class RosterExportView(LoginRequiredMixin, View):
    """Export the currently-viewed roster as .xlsx, in the view's own shape.

    Honours the same filters (department / team / employee / q) and date range
    as ``RosterWeekView``, so the download is exactly the "selected data" on
    screen. ``?view=flat`` (managers only, as on the page) exports one row per
    shift with the flat view's uncapped range and status filter; otherwise
    the wide grid layout. Both round-trip back through the uploader.
    """

    http_method_names = ["get"]

    def get(self, request):
        from audit.capture import note
        from roster.upload import render_export_response, render_flat_export_response

        if (request.GET.get("view") == "flat"
                and has_managed_scope(request.user, request=request)):
            date_from, date_to = _range_from_request(request, capped=False)
            org_wide = _org_wide_flat(request, "flat")
            employees_qs = _resolve_grid_employees(request, org_wide=org_wide)
            shifts = _flat_shifts(
                employees_qs, date_from, date_to, _parse_int(request.GET.get("status")),
            ).select_related("shift_attribute", "job_location", "work_location")
            note(request, employees=employees_qs.count(), view="flat", org_wide=org_wide,
                 date_from=date_from.isoformat(), date_to=date_to.isoformat())
            return render_flat_export_response(shifts)

        date_from, date_to = _range_from_request(request)
        days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]

        employees = list(
            _resolve_grid_employees(request)
            .order_by("first_name", "last_name")
        )
        shift_rows = (
            Shift.objects
            .filter(employee__in=employees, date__gte=date_from, date__lte=date_to)
            .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        )
        shift_map = {(s.employee_id, s.date): s for s in shift_rows}
        note(request, employees=len(employees), date_from=date_from.isoformat(), date_to=date_to.isoformat())
        return render_export_response(employees, days, shift_map)


# ---------------------------------------------------------------------------
# Cell update — POST-only; returns cell + OOB count
# ---------------------------------------------------------------------------

class CellUpdateView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, employee_pk, day):
        emp = get_object_or_404(CustomUser, pk=employee_pk, is_active=True)

        # Permission: same scope as weekly pattern — admins anywhere, line
        # managers and dept heads on their managed scope, non-admins never
        # write to their own cells. Return a clean 403 carrying a plain-text
        # reason header so htmx can surface it as a toast (a raised
        # PermissionDenied would render Django's HTML 403 page, which htmx
        # swallows silently — leaving the change looking like it just didn't
        # save).
        if not can_edit_roster_pattern(request.user, emp, request=request):
            if emp.pk == request.user.pk:
                reason = "You can't edit your own roster."
            else:
                reason = (
                    f"You don't manage {emp.display_name}, "
                    "so you can't edit their roster."
                )
            resp = HttpResponse(status=403)
            resp["HX-Error-Message"] = reason
            return resp

        d = parse_date(day) if isinstance(day, str) else day
        if not d:
            raise Http404("Invalid date.")

        from accounts.locations import resolve_location_choice

        shift_ref = request.POST.get("shift_ref") or request.POST.get("shift_type")
        location_ref = request.POST.get("location_ref")

        # Snapshot the existing row (if any) so we can preserve the field
        # the user *isn't* editing this round.
        existing_shift = (
            Shift.objects.filter(employee=emp, date=d)
            .select_related("job_location", "work_location")
            .first()
        )
        pattern = (
            Roster.objects.filter(employee=emp, weekday=d.weekday())
            .select_related("job_location", "work_location")
            .first()
        )

        # Resolve shift_type / shift_attribute. When location_ref is the
        # only thing being changed (shift_ref absent), preserve the
        # existing/pattern shift instead of clearing.
        if shift_ref is not None:
            # A cell's own pre-existing attribute (from the Shift row, or the
            # weekly pattern it would otherwise fall back to) may resubmit
            # unchanged even if it's since fallen out of scope for new picks.
            include_attr_ids = set()
            if existing_shift and existing_shift.shift_attribute_id:
                include_attr_ids.add(existing_shift.shift_attribute_id)
            if pattern and pattern.shift_attribute_id:
                include_attr_ids.add(pattern.shift_attribute_id)
            st, attr = _resolve_shift_ref(
                shift_ref, department_id=emp.department_id, include_attr_ids=include_attr_ids,
            )
        elif existing_shift:
            st = existing_shift.shift_type
            attr = existing_shift.shift_attribute
        elif pattern:
            st = pattern.shift_type
            attr = pattern.shift_attribute
        else:
            st, attr = None, None

        # Resolve job_location / work_location.
        if location_ref is not None:
            jl, wl = resolve_location_choice(location_ref)
        elif existing_shift:
            jl = existing_shift.job_location
            wl = existing_shift.work_location
        elif pattern:
            jl = pattern.job_location
            wl = pattern.work_location
        else:
            jl, wl = None, None

        if st is None and attr is None:
            # Genuinely clearing the cell — explicit shift_ref="" with no
            # location_ref or with a blank location_ref both land here.
            # Always allowed, same as the bulk/API guards below: removing a
            # row outside someone's tenure is fine, only adding one isn't.
            Shift.objects.filter(employee=emp, date=d).delete()
        else:
            # No Shift may exist outside an employee's tenure — same
            # invariant enforced on the week-grid prefill, the bulk uploader
            # and the external write API (see roster.services). A manual
            # cell edit is the last write path that didn't check this.
            if services.shift_is_after_last_working_date(emp, d):
                reason = (
                    f"{emp.display_name}'s last working date was "
                    f"{emp.last_working_date.isoformat()} - can't add a shift after that."
                )
                resp = HttpResponse(status=409)
                resp["HX-Error-Message"] = reason
                return resp
            if services.shift_is_before_joining_date(emp, d):
                reason = (
                    f"{emp.display_name} joined on "
                    f"{emp.joining_date.isoformat()} - can't add a shift before that."
                )
                resp = HttpResponse(status=409)
                resp["HX-Error-Message"] = reason
                return resp
            # Dedicated night shift window: only NIG/CUST or a non-working
            # type, unless the viewer is the department head. A location-only
            # change (or re-picking what's already there) keeps the current
            # shift, so it stays allowed — only a *new* shift is gated.
            current = existing_shift or pattern
            unchanged = (
                current is not None
                and current.shift_type_id == getattr(st, "pk", None)
                and current.shift_attribute_id == getattr(attr, "pk", None)
            )
            if not unchanged and not night_window.may_set_shift(request.user, emp, d, st):
                resp = HttpResponse(status=403)
                resp["HX-Error-Message"] = night_window.blocked_reason(emp)
                return resp
            Shift.objects.update_or_create(
                employee=emp, date=d,
                defaults={
                    "shift_type": st,
                    "shift_attribute": attr,
                    "job_location": jl,
                    "work_location": wl,
                    "updated_by": request.user,
                },
            )
            Shift.objects.filter(
                employee=emp, date=d, added_by__isnull=True,
            ).update(added_by=request.user)

        # Recompute counts scoped to what the user was viewing.
        employees = _resolve_grid_employees(request)
        emp_ids = list(employees.values_list("pk", flat=True))

        work_types = list(
            ShiftType.objects
            .filter(is_active=True, category=ShiftType.Category.WORK)
            .order_by(F("sort_order").asc(nulls_last=True), "name")
        )
        day_rows = (
            Shift.objects
            .filter(
                employee_id__in=emp_ids, date=d,
                shift_type__category=ShiftType.Category.WORK,
            )
            .values("shift_type_id").annotate(n=Count("id"))
        )
        day_counts = {r["shift_type_id"]: r["n"] for r in day_rows}
        day_header = {
            "date": d,
            "counts": [(st, day_counts.get(st.pk, 0)) for st in work_types],
        }

        shift = (
            Shift.objects.filter(employee=emp, date=d)
            .select_related(
                "shift_type", "shift_attribute", "job_location", "work_location",
            )
            .first()
        )
        holiday = Holiday.objects.filter(date=d, is_active=True).first()

        shift_choice_groups = _active_shift_choice_groups(
            department_id=emp.department_id,
            include_attr_ids={shift.shift_attribute_id} if shift and shift.shift_attribute_id else (),
        )

        cell_ctx = {
            "employee": emp,
            "date": d,
            "shift": shift,
            "holiday": holiday,
            "oob": False,
            "shift_choice_groups": shift_choice_groups,
            "filter_qs": _filter_querystring(request.GET),
            # The permission check above already passed for this cell.
            "can_edit_cell": True,
            **_night_cell_flags(request.user, emp, night_window.in_night_window(emp.pk, d)),
            "pending_change_pk": (
                ShiftChangeRequest.objects
                .filter(employee=emp, date=d, status=ShiftChangeRequest.Status.PENDING)
                .values_list("pk", flat=True).first()
            ),
        }
        count_ctx = {"day_header": day_header, "oob": True}

        # Recompute this employee's row totals + off_count across the whole
        # visible week so the right-side Total column and the employee-name
        # color stay in sync after an HTMX cell update.
        date_from, date_to = _range_from_request(request)
        row_shifts = (
            Shift.objects
            .filter(employee=emp, date__gte=date_from, date__lte=date_to)
            .select_related("shift_type")
        )
        totals_map: dict[str, int] = {}
        off_count = 0
        for s in row_shifts:
            totals_map[s.shift_type.name] = totals_map.get(s.shift_type.name, 0) + 1
            if s.shift_type.category == ShiftType.Category.OFF:
                off_count += 1
        row_total_ctx = {
            "employee": emp,
            "totals": sorted(totals_map.items()),
            "oob": True,
        }
        emp_name_ctx = {
            "employee": emp,
            "off_count": off_count,
            "can_manage_patterns": can_edit_all(request.user, request=request),
            "oob": True,
        }

        body = (
            render_to_string("roster/_cell.html", cell_ctx, request=request)
            + render_to_string("roster/_count_cell.html", count_ctx, request=request)
            + render_to_string("roster/_row_total.html", row_total_ctx, request=request)
            + render_to_string("roster/_emp_name.html", emp_name_ctx, request=request)
        )
        resp = HttpResponse(body)
        resp["HX-Trigger"] = json.dumps(
            {"orbis:toast": {"message": self._toast_message(emp, d, shift),
                             "type": "success"}}
        )
        return resp

    @staticmethod
    def _toast_message(emp, d, shift) -> str:
        """Human-readable summary of what the save changed, for the toast."""
        from accounts.locations import resolve_to_dropdown_choice

        day_label = d.strftime("%a, %b %d")
        if shift is None:
            return f"Cleared {emp.display_name} — {day_label}"
        if shift.shift_attribute_id:
            shift_label = shift.shift_attribute.name
        elif shift.shift_type_id:
            shift_label = shift.shift_type.name
        else:
            shift_label = "—"
        loc = resolve_to_dropdown_choice(shift.job_location, shift.work_location)
        summary = f"{shift_label} · {loc}" if loc else shift_label
        return f"Updated {emp.display_name} — {day_label}: {summary}"


# ---------------------------------------------------------------------------
# Shift change history — drives the "History" toggle on a roster cell
# ---------------------------------------------------------------------------

class ShiftHistoryView(LoginRequiredMixin, View):
    """JSON list of `ShiftChangeLog` rows for one (employee, date) cell.

    Mirrors `LeaveBalanceTransactionsView` (leave/views.py) — same
    visibility check, same JSON-feed-behind-a-toggle-button shape.
    """

    http_method_names = ["get"]

    def get(self, request, employee_pk, day):
        target = get_object_or_404(CustomUser, pk=employee_pk)
        if (target.pk != request.user.pk
                and target.pk not in visible_employee_pks(request.user, request=request)
                and not can_see_all(request.user, request=request)):
            raise PermissionDenied("You can't view this employee's shift history.")

        d = parse_date(day)
        if d is None:
            raise Http404("Bad date.")

        rows = [
            {
                "changed_at": log.changed_at.isoformat(),
                "action_type": log.get_action_type_display(),
                "changed_by": log.changed_by.display_name if log.changed_by_id else "—",
                "previous_shift": log.previous_shift_attribute.name if log.previous_shift_attribute_id else (log.previous_shift_type.name if log.previous_shift_type_id else None),
                "new_shift": log.new_shift_attribute.name if log.new_shift_attribute_id else (log.new_shift_type.name if log.new_shift_type_id else None),
                "previous_note": log.previous_note,
                "new_note": log.new_note,
            }
            for log in ShiftChangeLog.objects.filter(employee=target, date=d)
            .select_related(
                "changed_by", "previous_shift_type", "new_shift_type",
                "previous_shift_attribute", "new_shift_attribute",
            )[:200]
        ]
        return JsonResponse({"results": rows})


# ---------------------------------------------------------------------------
# Weekly pattern edit page
# ---------------------------------------------------------------------------

_WEEKDAY_LABELS = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday", 5: "Saturday", 6: "Sunday",
}


class RosterPatternView(LoginRequiredMixin, View):
    template_name = "roster/pattern_form.html"
    http_method_names = ["get", "post"]

    def dispatch(self, request, employee_pk, *args, **kwargs):
        self.employee = get_object_or_404(CustomUser, pk=employee_pk)
        if not can_edit_roster_pattern(request.user, self.employee, request=request):
            raise PermissionDenied("You can't edit this employee's weekly pattern.")
        return super().dispatch(request, *args, **kwargs)

    def _existing_rows(self):
        return {
            r.weekday: r for r in
            Roster.objects.filter(employee=self.employee).select_related(
                "shift_attribute", "job_location", "work_location",
            )
        }

    def _initial(self):
        from accounts.locations import resolve_to_dropdown_choice
        existing = self._existing_rows()
        initial = []
        for wd in range(7):
            row = existing.get(wd)
            if row is None:
                initial.append({"weekday": wd, "shift_ref": ""})
                continue
            if row.shift_attribute_id:
                ref = f"attr:{row.shift_attribute_id}"
            else:
                ref = f"type:{row.shift_type_id}"
            initial.append({
                "weekday": wd,
                "shift_ref": ref,
                "location": resolve_to_dropdown_choice(
                    row.job_location, row.work_location,
                ),
            })
        return initial

    def _formset_cls(self):
        return formset_factory(RosterPatternRowForm, extra=0, min_num=7, max_num=7, validate_min=True, validate_max=True)

    def get(self, request, *args, **kwargs):
        Formset = self._formset_cls()
        formset = Formset(initial=self._initial(), form_kwargs={"employee": self.employee})
        return self._render(request, formset)

    def post(self, request, *args, **kwargs):
        from accounts.locations import resolve_location_choice
        Formset = self._formset_cls()
        formset = Formset(request.POST, form_kwargs={"employee": self.employee})
        if formset.is_valid() and self._night_window_ok(request, formset):
            existing_rows = self._existing_rows()
            for form in formset.forms:
                wd = form.cleaned_data.get("weekday")
                ref = form.cleaned_data.get("shift_ref")
                location = form.cleaned_data.get("location") or ""
                existing_row = existing_rows.get(wd)
                # A row's own pre-existing attribute may resubmit unchanged
                # even if it's since fallen out of scope for new picks — see
                # RosterPatternRowForm and docs/PLAN.md.
                include_attr_ids = (
                    {existing_row.shift_attribute_id}
                    if existing_row and existing_row.shift_attribute_id else ()
                )
                st, attr = _resolve_shift_ref(
                    ref, department_id=self.employee.department_id,
                    include_attr_ids=include_attr_ids,
                )
                if st is None and attr is None:
                    Roster.objects.filter(employee=self.employee, weekday=wd).delete()
                    continue
                jl, wl = resolve_location_choice(location)
                Roster.objects.update_or_create(
                    employee=self.employee, weekday=wd,
                    defaults={
                        "shift_type": st,
                        "shift_attribute": attr,
                        "job_location": jl,
                        "work_location": wl,
                        "updated_by": request.user,
                    },
                )
                Roster.objects.filter(
                    employee=self.employee, weekday=wd, added_by__isnull=True,
                ).update(added_by=request.user)
            messages.success(request, f"Weekly pattern saved for {self.employee.display_name}.")
            return redirect("employee_detail", pk=self.employee.pk)
        return self._render(request, formset)

    def _night_window_ok(self, request, formset) -> bool:
        """The pattern fills every date with no Shift row, so a day-shift
        slot would land inside a dedicated night shift window unapproved.
        While the employee has an APPROVED window covering today or later,
        a *changed* slot may only be NIG/CUST or a non-working type unless
        the viewer is the department head (roster/night_window.py). An
        unchanged slot resubmits fine, so the rest of the form stays usable."""
        if night_window.is_night_window_approver(request.user, self.employee):
            return True
        if not night_window.has_current_or_future_window(self.employee.pk):
            return True
        existing_rows = self._existing_rows()
        ok = True
        for form in formset.forms:
            wd = form.cleaned_data.get("weekday")
            ref = form.cleaned_data.get("shift_ref")
            existing_row = existing_rows.get(wd)
            include_attr_ids = (
                {existing_row.shift_attribute_id}
                if existing_row and existing_row.shift_attribute_id else ()
            )
            st, attr = _resolve_shift_ref(
                ref, department_id=self.employee.department_id,
                include_attr_ids=include_attr_ids,
            )
            unchanged = (
                existing_row is not None
                and existing_row.shift_type_id == getattr(st, "pk", None)
                and existing_row.shift_attribute_id == getattr(attr, "pk", None)
            )
            if not unchanged and night_window.is_restricted_shift_type(st):
                form.add_error("shift_ref", night_window.blocked_reason(self.employee))
                ok = False
        return ok

    def _render(self, request, formset):
        # Attach human labels to each form (formset preserves order Mon→Sun).
        for i, form in enumerate(formset.forms):
            form.weekday_label = _WEEKDAY_LABELS[i]
        ctx = {
            "employee": self.employee,
            "formset": formset,
            "page_title": f"Weekly pattern — {self.employee.display_name}",
        }
        return render(request, self.template_name, ctx)


# ---------------------------------------------------------------------------
# Shift type CRUD (in-app, staff-only) — unchanged from previous impl
# ---------------------------------------------------------------------------

class ShiftTypeListView(StaffRequiredMixin, PageSizeMixin, ListView):
    model = ShiftType
    template_name = "roster/shift_type_list.html"
    context_object_name = "shift_types"
    paginate_by = 25
    extra_context = {"page_title": "Shift types"}

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        return qs.order_by(F("sort_order").asc(nulls_last=True), "name")


class ShiftTypeCreateView(StaffRequiredMixin, CreateView):
    model = ShiftType
    form_class = ShiftTypeForm
    template_name = "roster/shift_type_form.html"
    success_url = reverse_lazy("shift_type_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New shift type"
        ctx["submit_label"] = "Create"
        return ctx

    def form_valid(self, form):
        form.instance.added_by = self.request.user
        form.instance.updated_by = self.request.user
        messages.success(self.request, f"Shift type '{form.instance.name}' created.")
        return super().form_valid(form)


class ShiftTypeUpdateView(StaffRequiredMixin, UpdateView):
    model = ShiftType
    form_class = ShiftTypeForm
    template_name = "roster/shift_type_form.html"
    success_url = reverse_lazy("shift_type_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.name}"
        ctx["submit_label"] = "Save changes"
        return ctx

    def form_valid(self, form):
        form.instance.updated_by = self.request.user
        messages.success(self.request, f"Shift type '{form.instance.name}' updated.")
        return super().form_valid(form)


class ShiftTypeDeleteView(StaffRequiredMixin, DeleteView):
    model = ShiftType
    template_name = "roster/shift_type_confirm_delete.html"
    success_url = reverse_lazy("shift_type_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Delete {self.object.name}"
        return ctx

    def form_valid(self, form):
        messages.success(self.request, f"Shift type '{self.object.name}' deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Weekly roster email (drawer on the week grid)
# ---------------------------------------------------------------------------

class RosterEmailView(LoginRequiredMixin, View):
    """Configure, preview and send one team's weekly roster email.

    `?team=<id>&week=YYYY-MM-DD` (any day in the week; defaults to next
    week). htmx GET/POST → drawer fragment; plain GET → full page. POST
    `action` is `save`, `test` (to the viewer only; never counts as sent) or
    `send` — which asks for confirmation first when the week already went out.
    """

    fragment = "roster/_email_drawer.html"

    def _team(self, request):
        from accounts.models import Team
        from accounts.permissions import can_send_roster_email

        team = get_object_or_404(
            Team.objects.select_related("department", "department__head_of_department"),
            pk=_parse_int(request.POST.get("team") or request.GET.get("team")), is_active=True,
        )
        if not can_send_roster_email(request.user, team, request=request):
            raise PermissionDenied
        return team

    def _week(self, request):
        from roster import emails

        raw = _parse_date_param(request.POST.get("week") or request.GET.get("week"))
        return emails.monday_of(raw) if raw else emails.next_week_start()

    def _render(self, request, team, week, form, *, status=200, notice=None, error=None,
                needs_confirm=False):
        from roster import emails
        from roster.models import RosterEmailLog

        _, html, _, ctx = emails.build_team_roster_email(team, week, form.instance)
        ctx = {
            "team": team,
            "week": week,
            "prev_week": week - timedelta(days=7),
            "next_week": week + timedelta(days=7),
            "week_label": ctx["week_label"],
            "subject": ctx["subject"],
            "poc": ctx["poc"],
            "sender": ctx["sender"],
            "from_header": emails.sender_headers(ctx["sender"], ctx["poc"]),
            "preview_html": html,
            "form": form,
            "already_sent": RosterEmailLog.already_sent(team.pk, week),
            "recent_logs": RosterEmailLog.objects.filter(team=team, week_start=week)
                            .select_related("sent_by")[:10],
            "notice": notice,
            "error": error,
            "needs_confirm": needs_confirm,
            "action_url": reverse("roster_email"),
            "excluded": ctx["excluded"],
        }
        template = self.fragment if request.htmx else "roster/email_page.html"
        return render(request, template, ctx, status=status)

    def _form(self, request, team, week, data=None):
        from roster import emails
        from roster.forms import RosterEmailConfigForm
        from roster.models import RosterEmailConfig

        config = RosterEmailConfig.objects.filter(team=team).first() or RosterEmailConfig(team=team)
        members, excluded = emails.resolve_members(team, week, config)
        return RosterEmailConfigForm(
            data, instance=config, team=team, members=members, excluded=excluded,
        )

    def get(self, request):
        team, week = self._team(request), self._week(request)
        return self._render(request, team, week, self._form(request, team, week))

    def post(self, request):
        from roster import emails
        from roster.models import RosterEmailLog

        team, week = self._team(request), self._week(request)
        form = self._form(request, team, week, request.POST)
        if not form.is_valid():
            return self._render(request, team, week, form, status=400)
        form.instance.updated_by = request.user
        config = form.save()
        form = self._form(request, team, week)  # fresh, unbound, for display

        action = request.POST.get("action", "save")
        if action == "test":
            if not request.user.email:
                return self._render(request, team, week, form, error="Your account has no email address.")
            _, copies = emails.send_roster(
                team, week, trigger=RosterEmailLog.Trigger.TEST,
                to=[request.user.email], sent_by=request.user, config=config,
            )
            extra = " (plus a sample team copy)" if copies else ""
            return self._render(request, team, week, form,
                                notice=f"Test email sent to {request.user.email}{extra}.")
        if action == "send":
            if not config.to_list:
                return self._render(request, team, week, form,
                                    error="Add at least one recipient under To before sending.")
            if RosterEmailLog.already_sent(team.pk, week) and request.POST.get("confirm") != "1":
                return self._render(request, team, week, form, needs_confirm=True)
            _, copies = emails.send_roster(
                team, week, trigger=RosterEmailLog.Trigger.MANUAL,
                to=config.to_list, cc=config.cc_list, sent_by=request.user, config=config,
            )
            extra = f", plus {len(copies)} team cop{'y' if len(copies) == 1 else 'ies'}" if copies else ""
            return self._render(request, team, week, form,
                                notice=f"Roster for {emails.week_label(week)} sent to "
                                       f"{len(config.to_list) + len(config.cc_list)} recipient(s){extra}.")
        return self._render(request, team, week, form, notice="Settings saved.")


class RosterEmailPeopleSearchView(LoginRequiredMixin, View):
    """HTML-fragment search behind the drawer's "add someone from another
    team" picker (EmployeeSearchWidget). Org-wide on purpose — the point is
    to list a lead from a different team or department — but only for
    people who could be sending a roster email at all: anyone with managed
    scope, or a department/team manager (can_send_roster_email's other
    branches).
    """

    http_method_names = ["get"]

    def get(self, request):
        from accounts.permissions import _manages_any_department, _manages_any_team

        user = request.user
        if not (
            has_managed_scope(user, request=request)
            or _manages_any_department(user) or _manages_any_team(user)
        ):
            raise PermissionDenied
        q = (request.GET.get("q") or "").strip()
        qs = CustomUser.objects.filter(is_active=True).select_related("team")
        if q:
            qs = qs.filter(employee_search_q(q))
        return render(request, "employees/_employee_search_results.html", {
            "q": q,
            "results": qs.order_by("first_name", "last_name")[:25],
            "empty_message": "No matching active employee.",
        })


# ---------------------------------------------------------------------------
# Shift change requests (docs/SHIFT_CHANGE_REQUEST_PLAN.md) — a copy of the
# leave request/decide flow: tabbed list, apply page with a live "current
# shift" preview, detail page, approve / decline / cancel actions.
# ---------------------------------------------------------------------------

def _visible_change_requests_qs(user, request=None):
    """Every ShiftChangeRequest the user may see — same rule as
    leave.views._visible_leave_qs."""
    if can_see_all(user, request):
        return ShiftChangeRequest.objects.all()
    return ShiftChangeRequest.objects.filter(
        employee_id__in=visible_employee_pks(user, request)
    )


def _apply_change_request_filters(qs, request):
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(employee_search_q(q, prefix="employee__"))
    dept_id, team_id = effective_filter_ids(request.user, request)
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)
    if team_id:
        qs = qs.filter(employee__team_id=team_id)
    return qs


def _change_request_direct_only(request, direct_pks) -> bool:
    """The Pending / All tabs' "Direct reports only" switch — the same
    default-on scope switch as leave.views._direct_reports_only (absent or
    anything but ``0`` = on; never on for a viewer with no direct reports).
    The topbar bell (core/approvals.py) uses the same rule."""
    return bool(direct_pks) and request.GET.get("direct") != "0"


def _change_request_filter_suffix(request) -> str:
    dept_id, team_id = effective_filter_ids(request.user, request)
    params = {}
    q = (request.GET.get("q") or "").strip()
    if q:
        params["q"] = q
    direct_pks = direct_report_pks(request.user, request=request)
    if direct_pks and not _change_request_direct_only(request, direct_pks):
        params["direct"] = "0"
    if dept_id:
        params["department"] = dept_id
    if team_id:
        params["team"] = team_id
    per_page = resolve_page_size(request)
    if per_page != DEFAULT_PAGE_SIZE:
        params["per_page"] = per_page
    return f"&{urlencode(params)}" if params else ""


_CHANGE_REQUEST_RELATED = (
    "employee", "employee__department", "employee__team",
    "original_shift_type", "original_shift_attribute",
    "requested_shift_type", "requested_shift_attribute",
)


class ShiftChangeListView(LoginRequiredMixin, View):
    template_name = "roster/change_requests/list.html"

    def get(self, request):
        tab = request.GET.get("tab", "mine")
        base = (
            _visible_change_requests_qs(request.user, request=request)
            .select_related(*_CHANGE_REQUEST_RELATED)
            .annotate(_in_night_window=night_window.in_window_exists())
        )
        mine_qs = (
            ShiftChangeRequest.objects
            .filter(employee=request.user)
            .select_related(*_CHANGE_REQUEST_RELATED)
            .annotate(_in_night_window=night_window.in_window_exists())
        )
        pending_qs = base.filter(status=ShiftChangeRequest.Status.PENDING).exclude(employee=request.user)
        user_can_see_all = can_see_all(request.user, request=request)
        show_pending_tab = has_managed_scope(request.user, request=request)
        show_all_tab = user_can_see_all or show_pending_tab
        all_qs = base if show_all_tab else base.none()
        direct_pks = direct_report_pks(request.user, request=request)
        direct_only = _change_request_direct_only(request, direct_pks)
        if direct_only:
            # Narrow *within* the visible scope — never widens it. A
            # department head keeps their department's night-window requests
            # too: those wait on them, not on the direct manager.
            head_pks = night_window.approver_employee_ids(request.user)
            narrow = Q(employee_id__in=direct_pks)
            if head_pks:
                narrow |= Q(_in_night_window=True, employee_id__in=head_pks)
            pending_qs = pending_qs.filter(narrow)
            all_qs = all_qs.filter(narrow)

        if tab == "pending" and show_pending_tab:
            selected_qs = _apply_change_request_filters(pending_qs, request)
        elif tab == "all" and show_all_tab:
            selected_qs = _apply_change_request_filters(all_qs, request)
        else:
            tab = "mine"
            selected_qs = _apply_change_request_filters(mine_qs, request)

        page = paginate_list(request, selected_qs)
        for row in page["rows"]:
            # Annotated above, so the permission check never queries per row.
            row._night_window = row._in_night_window
            row.needs_head = row._in_night_window
            row.can_decide = row.is_pending and can_act_on_shift_change(
                request.user, row, request=request,
            )
        filter_suffix = _change_request_filter_suffix(request)
        q = (request.GET.get("q") or "").strip()

        ctx = {
            "page_title": "Shift changes",
            "tab": tab,
            **page,
            "mine_count": mine_qs.count(),
            "pending_count": pending_qs.count(),
            "all_count": all_qs.count() if show_all_tab else 0,
            "show_pending_tab": show_pending_tab,
            "show_all_tab": show_all_tab,
            "q": q,
            "query_suffix": filter_suffix,
            "filters_active": bool(q or request.GET.get("department") or request.GET.get("team")),
            "current_user_pk": request.user.pk,
            "user_can_edit_all": can_edit_all(request.user, request=request),
            "has_direct_reports": bool(direct_pks),
            "direct_only": direct_only,
            **employee_filter_context(request.user, request),
        }
        return render(request, self.template_name, ctx)


class ShiftChangeApplyView(LoginRequiredMixin, CreateView):
    model = ShiftChangeRequest
    form_class = ShiftChangeRequestForm
    template_name = "roster/change_requests/apply.html"

    def _resolve_employee(self):
        raw = self.request.GET.get("for") or self.request.POST.get("for")
        if not raw:
            return self.request.user
        target = get_object_or_404(CustomUser, pk=raw)
        if target.pk == self.request.user.pk:
            return self.request.user
        if not can_request_shift_change(self.request.user, target, request=self.request):
            raise PermissionDenied("You can't request a shift change on behalf of that employee.")
        return target

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["employee"] = self._resolve_employee()
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.actor = self.request.user
        return form

    def get_initial(self):
        initial = super().get_initial()
        # Deep link from the roster grid: ?date=YYYY-MM-DD pre-fills the date.
        raw = self.request.GET.get("date")
        if raw and parse_date(raw):
            initial["date"] = raw
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        target = self._resolve_employee()
        ctx["page_title"] = "Request a shift change"
        ctx["target_employee"] = target
        ctx["applying_for_other"] = target.pk != self.request.user.pk
        return ctx

    def form_valid(self, form):
        target = self._resolve_employee()
        req = form.save(commit=False)
        # employee / original_* / requested_* were set on the instance by
        # form.clean(); only the audit fields remain.
        req.employee = target
        req.applied_by = self.request.user
        req.added_by = self.request.user
        req.updated_by = self.request.user
        req.status = ShiftChangeRequest.Status.PENDING
        req.save()
        messages.success(self.request, f"Shift change request submitted for {target.display_name}.")
        note = _night_window_note(req)
        if note:
            messages.info(self.request, note)
        return redirect("shift_change_detail", pk=req.pk)


class ShiftChangeCurrentView(LoginRequiredMixin, View):
    """POST `for` + `date` → what the roster says for that date right now,
    so the apply page can show the "from" half of the diff and pre-select
    both dropdowns. Same on-behalf-of gate as the apply page."""

    http_method_names = ["post"]

    def post(self, request):
        for_pk = request.POST.get("for") or request.user.pk
        try:
            target = CustomUser.objects.get(pk=for_pk)
        except (CustomUser.DoesNotExist, ValueError, ValidationError):
            return JsonResponse({"error": "Employee not found"}, status=404)
        if target.pk != request.user.pk and not can_request_shift_change(
            request.user, target, request=request,
        ):
            return JsonResponse({"error": "Permission denied"}, status=403)

        day = parse_date(request.POST.get("date") or "")
        if not day:
            return JsonResponse({"error": "Pick a date"}, status=400)

        current = services.resolve_current_cell(target, day)
        warnings = []
        blocked = services.leave_blocks_shift_change(target, day)
        if blocked:
            warnings.append(blocked)
        if ShiftChangeRequest.objects.filter(
            employee=target, date=day, status=ShiftChangeRequest.Status.PENDING,
        ).exists():
            warnings.append(f"A shift change request for {day.isoformat()} is already pending.")
        if day < timezone.localdate() and not can_edit_all(request.user, request=request):
            warnings.append("A shift change can only be requested for today or a future date.")

        return JsonResponse({
            "current_label": current.label or "Unscheduled",
            "current_shift_ref": current.shift_ref,
            "current_location": current.location,
            "is_concrete": current.is_concrete,
            "warnings": warnings,
        })


class ShiftChangeDetailView(LoginRequiredMixin, DetailView):
    model = ShiftChangeRequest
    template_name = "roster/change_requests/detail.html"
    context_object_name = "change_request"

    def get_queryset(self):
        return ShiftChangeRequest.objects.select_related(
            *_CHANGE_REQUEST_RELATED, "applied_by", "processed_by",
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
        req = ctx["change_request"]
        ctx["page_title"] = f"Shift change · {req.employee.display_name} · {req.date}"
        ctx["can_act"] = (
            can_act_on_shift_change(self.request.user, req, request=self.request)
            and req.is_pending
        )
        ctx["can_cancel"] = can_cancel_shift_change(self.request.user, req, request=self.request)
        ctx["night_window_note"] = _night_window_note(req) if req.is_pending else ""
        return ctx


def _night_window_note(req) -> str:
    """Who decides a request dated inside a dedicated night shift window —
    shown on the detail page and the apply form."""
    if not shift_change_needs_department_head(req):
        return ""
    approver_id = night_window.night_window_approver_id(req.employee)
    approver = CustomUser.objects.filter(pk=approver_id).first() if approver_id else None
    who = approver.display_name if approver else "the department head (none is set — ask HR to set one)"
    return (
        f"{req.date:%d %b %Y} is inside {req.employee.display_name}'s dedicated night "
        f"shift window, so only {who} can approve this request."
    )


class _ChangeActionBase(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.change_request = get_object_or_404(
            ShiftChangeRequest.objects.select_related(*_CHANGE_REQUEST_RELATED), pk=pk,
        )
        return super().dispatch(request, pk, *args, **kwargs)

    def _render_row_response(self, request):
        """htmx → updated row partial; non-htmx → redirect to detail."""
        if not getattr(request, "htmx", False):
            return redirect("shift_change_detail", pk=self.change_request.pk)
        html = render_to_string(
            "roster/change_requests/_row.html",
            {
                "row": self.change_request,
                "current_user_pk": request.user.pk,
                "oob": True,
                "show_actions": False,
            },
            request=request,
        )
        response = HttpResponse(html)
        response["HX-Trigger"] = APPROVALS_CHANGED_EVENT  # refresh the topbar bell
        return response


class ShiftChangeApproveView(_ChangeActionBase):
    @transaction.atomic
    def post(self, request, pk):
        req = self.change_request
        if not can_act_on_shift_change(request.user, req, request=request) or not req.is_pending:
            raise PermissionDenied
        try:
            # Re-validates (leave approved since? attribute re-scoped?) and
            # writes the Shift; raises without writing when it no longer fits.
            services.apply_shift_change(req, request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("shift_change_detail", pk=req.pk)
        req.status = ShiftChangeRequest.Status.APPROVED
        req.processed_by = request.user
        req.processed_at = timezone.now()
        req.process_note = request.POST.get("process_note", "").strip()
        req.updated_by = request.user
        req.save()
        messages.success(request, f"Approved: {req}.")
        return self._render_row_response(request)


class ShiftChangeDeclineView(_ChangeActionBase):
    @transaction.atomic
    def post(self, request, pk):
        req = self.change_request
        if not can_act_on_shift_change(request.user, req, request=request) or not req.is_pending:
            raise PermissionDenied
        req.status = ShiftChangeRequest.Status.DECLINED
        req.processed_by = request.user
        req.processed_at = timezone.now()
        req.process_note = request.POST.get("process_note", "").strip()
        req.updated_by = request.user
        req.save()
        messages.info(request, f"Declined: {req}.")
        return self._render_row_response(request)


class ShiftChangeCancelView(_ChangeActionBase):
    @transaction.atomic
    def post(self, request, pk):
        req = self.change_request
        if not can_cancel_shift_change(request.user, req, request=request):
            raise PermissionDenied
        req.status = ShiftChangeRequest.Status.CANCELLED
        req.processed_by = request.user
        req.processed_at = timezone.now()
        req.process_note = (
            request.POST.get("process_note", "").strip()
            or req.process_note or "Cancelled"
        )
        req.updated_by = request.user
        req.save()
        # Only a PENDING request can be cancelled, so nothing was written
        # to the roster and there's nothing to reverse.
        messages.info(request, f"Cancelled: {req}.")
        return self._render_row_response(request)
