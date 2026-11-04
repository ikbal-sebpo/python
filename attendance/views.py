from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import ListView

from accounts.filters import (
    apply_employee_filters,
    effective_filter_ids,
    employee_filter_context,
    employee_search_q,
)
from accounts.models import CustomUser, Department, Team
from accounts.permissions import (
    can_act_on_attendance_correction,
    can_edit_all,
    can_see_all,
    can_request_attendance_correction,
    direct_report_pks,
    has_managed_scope,
    managed_scope_pks,
    visible_employees,
)
from attendance.exports import build_timesheet_export
from attendance.forms import AttendanceCorrectionForm
from attendance.models import AttendanceCorrection, AttendancePunch
from core.pagination import PageSizeMixin, paginate_list
from payroll.periods import employed_during_q
from roster.models import Roster, Shift, ShiftAttribute, ShiftType
from roster.services import shift_is_after_last_working_date, shift_is_before_joining_date


# Same cap, and the same reasoning, as roster.views.SINGLE_EMP_MAX_RANGE_DAYS:
# a single-employee view renders one row per day, so a wide window is cheap,
# but it still needs a bound so a hand-typed `?from=` can't ask for years.
SINGLE_EMP_MAX_RANGE_DAYS = 186

# Cap for the department/team date-range mode (one row per employee per
# date) — same value and reasoning as roster.views.MAX_RANGE_DAYS: a 60-person
# team over a month is ~1,900 rows, which pages fine; a year would not.
TEAM_MAX_RANGE_DAYS = 31

# Params that mean "the viewer is driving the team list", so a bare
# /attendance/ (none of these present) is the only thing that defaults into
# the viewer's own single-employee range. `page`/`per_page` are deliberately
# absent — paging the default self-view must not flip it into team mode.
_TEAM_MODE_PARAMS = ("q", "department", "team", "date", "from", "to", "flagged")


class _GetOverride:
    """`request` with `.GET` swapped for `get`; everything else (user,
    session, ...) delegates straight through.

    Lets a bare /attendance/ resolve as if `q=<your employee id>` had been
    submitted, without mutating the real request.GET (which other code —
    URL reversal, `_base_context`, ... — still needs untouched).
    `accounts.filters.apply_employee_filters`/`employee_filter_context` and
    `accounts.permissions.visible_employees` only ever read `.GET` off
    whatever `request` they're handed, so swapping just that is enough.
    """

    def __init__(self, request, get):
        self._request = request
        self.GET = get

    def __getattr__(self, name):
        return getattr(self._request, name)


def _resolve_range_employee(request):
    """Whether `?q=` is an exact match on a *unique* field (employee ID or
    email) — if so, that's who single-employee range mode shows.

    There's no separate "Employee ID" control: the same box that filters
    the team table (name / email / employee ID,
    `accounts.filters.employee_search_q`) doubles as the "pick one person"
    control — but deliberately only on an exact match against employee_id
    or email, not merely "the fuzzy search happens to narrow to one row".
    A stray substring (typing "200" and landing on employee "2001" because
    nobody else matches today) would silently flip modes on a coincidence;
    name is excluded entirely since it isn't unique — two employees could
    share one, and a range view can't merge two people's shifts into one
    result set. Email matches either the full address or just its local
    part (`raw_q` before the `@`), so typing a login name like "jdoe"
    matches "jdoe@sebpo.com" without needing the full address. Team-mode
    narrowing by name/email/partial ID is untouched — `get_queryset` below
    still runs the fuzzy search via `apply_employee_filters`.

    A bare `/attendance/` (no filter params at all) searches for the
    viewer's own employee_id by default, so landing on "my own timesheet"
    needs no typing.

    Returns `(employee_or_None, filter_request)` — `filter_request` is what
    `_base_context` hands to `employee_filter_context` so the search box
    visibly shows the synthesized default on a bare load, not just resolves
    it invisibly.
    """
    bare = not any(request.GET.get(key) for key in _TEAM_MODE_PARAMS)
    raw_q = (request.GET.get("q") or "").strip()
    if bare and not raw_q and getattr(request.user, "employee_id", ""):
        raw_q = request.user.employee_id

    get = request.GET.copy()
    if raw_q:
        get["q"] = raw_q
    filter_request = _GetOverride(request, get)

    employee = None
    if raw_q:
        employee = (
            visible_employees(request.user, request=filter_request)
            .filter(
                Q(employee_id__iexact=raw_q)
                | Q(email__iexact=raw_q)
                | Q(email__istartswith=f"{raw_q}@")
            )
            .select_related("department", "team", "designation")
            .first()
        )
    return employee, filter_request


@dataclass
class _ListParams:
    """Resolved `/attendance/` querystring: which mode, whose rows, what span.

    Three modes, in precedence order:

    - `employee` non-None → single-employee date-range mode (one row per
      date).
    - `group_range` → department/team date-range mode (one row per employee
      per date, `from != to` with a department or team selected — or, for
      an org-wide viewer, on "All departments" too).
    - neither → team mode (one row per employee for `date_to`, the historic
      behaviour).
    """

    employee: CustomUser | None = None
    date_from: date = None
    date_to: date = None
    group_range: bool = False
    errors: list = field(default_factory=list)

    @property
    def range_mode(self) -> bool:
        return self.employee is not None

    @property
    def multi_day(self) -> bool:
        """Any mode whose rows are built per date, not just for `date_to`."""
        return self.range_mode or self.group_range

    @property
    def dates(self):
        span = (self.date_to - self.date_from).days
        return [self.date_from + timedelta(days=i) for i in range(span + 1)]


def _default_to() -> date:
    # Yesterday, not today — today's punches aren't synced into
    # AttendancePunch until the 10:00 beat job runs the following morning.
    return date.today() - timedelta(days=1)


def _resolve_list_params(request, employee, group_scoped: bool = False) -> _ListParams:
    """Parse `?from=`/`?to=` (and legacy `?date=`) into a date span.

    `employee` is already resolved (see `_resolve_range_employee`) — this only
    handles "when". A from != to span is allowed when either `employee` is
    set (single-employee range) or `group_scoped` is True (a department or
    team is effectively selected — `accounts.filters.effective_filter_ids`,
    which also covers a department-locked viewer with no explicit pick,
    and a team pick alone since a team implies its department). Without
    either, a multi-day span would mean one row per employee per date
    across the viewer's whole scope, so it's refused. Error surfacing
    mirrors leave.views._leave_date_range: a malformed or inverted range is
    reported back through `errors` (rendered by the template) and falls
    back to something sane rather than 400ing or silently showing the
    wrong span.
    """
    params = _ListParams(employee=employee)

    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    raw_date = (request.GET.get("date") or "").strip()
    if raw_date and not raw_from and not raw_to:
        # Legacy single-date links (the dashboard's "working" bucket, an old
        # bookmark, a correction redirect) keep working unchanged.
        raw_from = raw_to = raw_date

    parsed = {}
    for key, label, raw in (("from", "Date from", raw_from), ("to", "Date to", raw_to)):
        value = None
        if raw:
            try:
                value = parse_date(raw)
            except ValueError:
                value = None
            if value is None:
                params.errors.append(f"{label}: '{raw}' isn't a valid date.")
        parsed[key] = value

    today = date.today()
    params.date_to = parsed["to"] or _default_to()
    if parsed["from"]:
        params.date_from = parsed["from"]
    else:
        params.date_from = today.replace(day=1) if params.range_mode else params.date_to

    if params.date_from > params.date_to:
        if parsed["from"]:
            params.errors.append("Date from must be on or before Date to.")
        # A *defaulted* inversion isn't the user's doing — on the 1st of the
        # month, month-start is already past yesterday. Clamp, don't scold.
        params.date_from = params.date_to
    elif not params.range_mode and params.date_from != params.date_to:
        if group_scoped:
            params.group_range = True
        else:
            params.errors.append(
                "Pick a department or team, or narrow your search (email or "
                "employee ID) to one employee, to view a date range — showing "
                f"{params.date_to.isoformat()} only.",
            )
            params.date_from = params.date_to

    # Same clamp shape as roster.views._range_from_request: hold `from`, pull
    # `to` back to the cap.
    cap = SINGLE_EMP_MAX_RANGE_DAYS if params.range_mode else TEAM_MAX_RANGE_DAYS
    if (params.date_to - params.date_from).days + 1 > cap:
        params.date_to = params.date_from + timedelta(days=cap - 1)
    return params


def _shift_label(shift_or_pattern) -> str:
    """ShiftAttribute.name when set (e.g. 'EVE 2-11' — SEBPO's shift-attribute
    naming already bakes the start/end into the label), else the ShiftType
    name (e.g. plain 'OFF'/'AL' rows with no attribute)."""
    if shift_or_pattern.shift_attribute_id and shift_or_pattern.shift_attribute:
        return shift_or_pattern.shift_attribute.name
    return shift_or_pattern.shift_type.name


def _resolve_shifts(pks, selected, cutoffs=None):
    """Map each employee pk to their resolved Shift/Roster row for `selected`.

    Same resolution order as core.dashboard.today_status_buckets: a concrete
    Shift row for the date wins, else the employee's Roster weekly pattern.
    None when neither exists (unscheduled).

    The Roster pattern is keyed only by weekday, not by date, so on its own
    it would happily "resolve" a shift for a date outside the employee's
    employment window (before `joining_date` / after `last_working_date`) —
    a real Shift row never can, thanks to the write-time guards in
    roster.services, so only the pattern fallback needs the check. `cutoffs`
    is an optional {pk: (joining_date, last_working_date)} map; pass it
    whenever `pks` might include a very recently hired or offboarded
    employee.
    """
    shift_by_pk = {
        s.employee_id: s
        for s in (
            Shift.objects
            .filter(date=selected, employee_id__in=pks)
            .select_related("shift_type", "shift_attribute")
        )
    }
    pattern_by_pk = {
        r.employee_id: r
        for r in (
            Roster.objects
            .filter(weekday=selected.weekday(), employee_id__in=pks)
            .select_related("shift_type", "shift_attribute")
        )
    }
    resolved = {}
    for pk in pks:
        row = shift_by_pk.get(pk)
        if row is None:
            row = pattern_by_pk.get(pk)
            if row is not None and cutoffs is not None:
                joined, lwd = cutoffs.get(pk, (None, None))
                window = SimpleNamespace(joining_date=joined, last_working_date=lwd)
                if (
                    shift_is_before_joining_date(window, selected)
                    or shift_is_after_last_working_date(window, selected)
                ):
                    row = None
        resolved[pk] = row
    return resolved


def _resolve_shifts_matrix(employees, dates):
    """The same resolution order as `_resolve_shifts`, across a matrix: a
    set of employees × a span of dates → `{(employee_pk, date): row}`.

    A fixed handful of queries regardless of size — the Shift and Roster
    columns over the employee set (and the span), plus the ShiftType and
    ShiftAttribute tables — so a 60-person team over a month costs the same
    as one person over a week. Same employment-window guard on the pattern
    fallback as `_resolve_shifts`; `employees` are full instances, so
    `joining_date`/`last_working_date` are read straight off them.

    The values are lightweight stand-ins carrying just what a `_DateRow`
    reads (`shift_type`, `shift_attribute`, `shift_attribute_id` — see
    `_shift_label`), with the type/attribute objects shared from one fetch:
    hydrating a Shift + its two related rows per cell is what made an
    org-wide payroll-period export take most of gunicorn's timeout.
    """
    pks = [emp.pk for emp in employees]
    types = ShiftType.objects.in_bulk()
    attributes = ShiftAttribute.objects.in_bulk()

    def resolved(type_id, attribute_id):
        return SimpleNamespace(
            shift_type=types[type_id],
            shift_attribute_id=attribute_id,
            shift_attribute=attributes.get(attribute_id),
        )

    columns = ("shift_type_id", "shift_attribute_id")
    shift_by_key = {
        (pk, d): resolved(type_id, attribute_id)
        for pk, d, type_id, attribute_id in (
            Shift.objects
            .filter(employee_id__in=pks, date__gte=dates[0], date__lte=dates[-1])
            .values_list("employee_id", "date", *columns)
        )
    }
    pattern_by_key = {
        (pk, weekday): resolved(type_id, attribute_id)
        for pk, weekday, type_id, attribute_id in (
            Roster.objects.filter(employee_id__in=pks)
            .values_list("employee_id", "weekday", *columns)
        )
    }
    return _resolve_matrix(employees, dates, shift_by_key, pattern_by_key)


def _resolve_matrix(employees, dates, shift_by_key, pattern_by_key):
    """The precedence loop behind `_resolve_shifts_matrix` and
    `_unaccounted_keys`: the `(pk, date)` value wins, else the `(pk,
    weekday)` pattern value inside the employment window, else None. The
    values are opaque — model rows for one caller, bare categories for the
    other — so the two can't resolve a date differently."""
    resolved = {}
    for emp in employees:
        for d in dates:
            row = shift_by_key.get((emp.pk, d))
            if row is None:
                row = pattern_by_key.get((emp.pk, d.weekday()))
                if row is not None and (
                    shift_is_before_joining_date(emp, d)
                    or shift_is_after_last_working_date(emp, d)
                ):
                    row = None
            resolved[(emp.pk, d)] = row
    return resolved


def _unaccounted_keys(employees, dates) -> set:
    """`{(employee_pk, date)}` whose resolved shift is WORK-category with no
    check-in — `_DateRow.is_unaccounted`, computed from bare columns.

    The paged list needs this across every employee (the live badge and
    `?flagged=1`), but full rows only for the page; hydrating a Shift,
    Roster and punch instance per cell is what made an org-wide month cost
    seconds a page.
    """
    pks = [emp.pk for emp in employees]
    span = {"date__gte": dates[0], "date__lte": dates[-1]}
    categories = _resolve_matrix(
        employees, dates,
        {
            (pk, d): category
            for pk, d, category in Shift.objects.filter(employee_id__in=pks, **span)
            .values_list("employee_id", "date", "shift_type__category")
        },
        {
            (pk, weekday): category
            for pk, weekday, category in Roster.objects.filter(employee_id__in=pks)
            .values_list("employee_id", "weekday", "shift_type__category")
        },
    )
    punched = set(
        AttendancePunch.objects
        .filter(employee_id__in=pks, check_in__isnull=False, **span)
        .values_list("employee_id", "date")
    )
    return {
        key for key, category in categories.items()
        if category == ShiftType.Category.WORK and key not in punched
    }


@dataclass
class _DateRow:
    """One date's timesheet for one employee — the row shape of both
    date-range modes (single-employee and department/team) and of every
    export. Mirrors the attributes the team-mode template annotates onto
    each employee, so all modes share `attendance/_timesheet_cells.html`."""

    date: "date"
    employee: CustomUser = None
    shift_label: str = ""
    punch: object = None
    is_unaccounted: bool = False
    pending_correction: object = None
    correction: object = None
    can_request_correction: bool = False

    @property
    def has_pending_correction(self) -> bool:
        return self.pending_correction is not None

    @property
    def is_corrected(self) -> bool:
        return bool(self.punch and self.punch.source == AttendancePunch.Source.MANUAL_CORRECTION)

    @property
    def status_label(self) -> str:
        """The Status/Actions badges, flattened to one word for the export."""
        if self.is_unaccounted:
            return "Unaccounted"
        if self.has_pending_correction:
            return "Correction pending"
        if self.is_corrected:
            return "Corrected"
        return ""


def _build_timesheet_rows(employees, dates, user, request) -> list:
    """One `_DateRow` per (employee, date), ordered employee-then-date so
    each person reads as a contiguous block — the row source for both
    date-range modes and for every export (team mode's single-date export
    is just `dates == [date_to]`).

    Constant query count: the shift matrix (2), punches (1), corrections
    (1), plus the per-employee `can_request_attendance_correction` check
    (computed once per employee, not per row).
    """
    pks = [emp.pk for emp in employees]
    punch_by_key = {
        (punch.employee_id, punch.date): punch
        for punch in AttendancePunch.objects.filter(
            employee_id__in=pks, date__gte=dates[0], date__lte=dates[-1],
        )
    }
    resolved_by_key = _resolve_shifts_matrix(employees, dates)
    corrections = (
        AttendanceCorrection.objects
        .filter(employee_id__in=pks, date__gte=dates[0], date__lte=dates[-1])
        .select_related("requested_by", "processed_by")
        .order_by("processed_at")
    )
    pending_by_key = {
        (c.employee_id, c.date): c
        for c in corrections if c.status == AttendanceCorrection.Status.PENDING
    }
    # Last-approved-wins on the off chance a date got corrected twice —
    # same as team mode, which is why the queryset is processed_at-ordered.
    approved_by_key = {
        (c.employee_id, c.date): c
        for c in corrections if c.status == AttendanceCorrection.Status.APPROVED
    }
    user_can_edit_all = can_edit_all(user, request=request)

    rows = []
    for emp in employees:
        can_request = (
            user_can_edit_all
            or emp.pk == user.pk
            or can_request_attendance_correction(user, emp, request=request)
        )
        for d in dates:
            resolved = resolved_by_key.get((emp.pk, d))
            punch = punch_by_key.get((emp.pk, d))
            row = _DateRow(
                date=d,
                employee=emp,
                shift_label=_shift_label(resolved) if resolved else "",
                punch=punch,
                is_unaccounted=bool(
                    resolved
                    and resolved.shift_type.category == ShiftType.Category.WORK
                    and not (punch and punch.check_in)
                ),
                pending_correction=pending_by_key.get((emp.pk, d)),
                can_request_correction=can_request,
            )
            row.correction = approved_by_key.get((emp.pk, d)) if row.is_corrected else None
            rows.append(row)
    return rows


class AttendanceListView(PageSizeMixin, LoginRequiredMixin, ListView):
    """Three modes off one URL:

    - **Single-employee date range** (`?q=<their exact employee ID>`, or by
      default on a bare `/attendance/`, which searches for the viewer's own
      employee_id and lands on their own month-to-date): one row per *date*
      in `[from, to]` — a personal daily log. Rows are a plain Python list,
      paginated with `core.pagination.paginate_list`.
    - **Department/team date range** (`from != to` with a department or
      team selected, no single employee resolved; added 2026-10-01): one
      row per employee per date, employee-then-date, built by
      `_build_timesheet_rows` and paginated the same way. Capped at
      `TEAM_MAX_RANGE_DAYS`.
    - **Team** (a single date, `?q=` empty or not an exact match): the
      historic shape — one row per visible employee for `date_to`,
      paginated by `ListView`, still narrowed by the same fuzzy
      name/email/ID search + department/team.

    `?export=xlsx` on any of the three downloads the full (unpaginated)
    row set the active filters matched — `attendance/exports.py` — same
    `?export=` convention as `/employees/` and `/payroll/overtime/`.

    There's no separate "Employee ID" param — the same box that filters the
    team table doubles as the "pick one person" control, per
    `_resolve_range_employee`. Reuses the same q/department/team filter bar as
    /employees/ (accounts.filters) in both modes.

    Also flags "unaccounted" employees — a WORK-category shift (roster or
    weekly pattern) with no TimeLy punch at all for that date. TimeLy folds
    WFH in as remote punches, so this isn't an office-only check: it's a
    genuine "did this working day get accounted for anywhere" gap, catching
    the case the user described — scheduled to work, no punch, and no leave
    (leave would have resolved the shift to LEAVE category instead of WORK).
    `?flagged=1` filters the whole scoped list down to just those rows, not
    just the current page, so this doubles as a company-wide reconciliation
    check rather than a per-page spot check — in range mode the same flag
    and the same `?flagged=1` filter apply per date instead of per employee.
    """

    model = CustomUser
    template_name = "attendance/list.html"
    context_object_name = "employees"
    paginate_by = 25
    extra_context = {"page_title": "Timesheet"}

    def get(self, request, *args, **kwargs):
        self.employee, self.filter_request = _resolve_range_employee(request)
        dept_id, _team_id = effective_filter_ids(request.user, self.filter_request)
        # HR / Admins / superusers may range over "All departments" too
        # (2026-11-02) — the payroll-period export needs the whole company,
        # and their "All departments" pick is an empty `department=`.
        self.org_wide = can_see_all(request.user, request=request)
        self.params = _resolve_list_params(
            request, self.employee, group_scoped=dept_id is not None or self.org_wide,
        )
        if request.GET.get("export") == "xlsx":
            return self._export(request)
        if self.params.multi_day:
            return self._render_rows(request)
        return super().get(request, *args, **kwargs)

    # --- shared -----------------------------------------------------------

    def _base_context(self):
        """Everything the template's chrome needs, identical in both modes."""
        p = self.params
        ctx = {
            "page_title": "Timesheet",
            "range_mode": p.range_mode,
            "group_range_mode": p.group_range,
            "range_employee": p.employee,
            "scope_label": self._scope_label(),
            "date_from": p.date_from,
            # The header's own "Add timesheet" action is about one date, and
            # in range mode the most recent end of the span is the sensible
            # one; in team mode it's the only date there is.
            "selected_date": p.date_to,
            "date_to": p.date_to,
            "date_errors": p.errors,
            "flagged_only": self.request.GET.get("flagged") == "1",
        }
        # `filter_request`, not `request` — on a bare load it carries the
        # synthesized `q=<your employee id>` so the search box actually
        # shows it, instead of resolving the default invisibly.
        ctx.update(employee_filter_context(self.request.user, self.filter_request))
        # Drives the header's "Add timesheet" button vs "Timesheet pending"
        # badge — about the viewer themselves, so it's independent of
        # whatever q/department/team filters currently narrow the table
        # (which could otherwise hide the viewer's own row entirely).
        ctx["my_pending_correction"] = AttendanceCorrection.objects.filter(
            employee=self.request.user, date=p.date_to,
            status=AttendanceCorrection.Status.PENDING,
        ).first()
        return ctx

    def _scoped_employees(self):
        """Every employee the active q/department/team filters match, in
        the same order team mode lists them — the row source for the
        department/team range mode and for team mode's export.

        An org-wide viewer's range takes the payroll population for the span
        (`payroll.periods.employed_during_q`) instead of the active-only
        list: someone who left mid-period still has days in it, and accounts
        with no employee ID (service/test logins) are not employees.
        """
        p = self.params
        if self.org_wide and p.group_range:
            qs = (
                visible_employees(
                    self.request.user, request=self.filter_request, include_separated=True,
                )
                .filter(employed_during_q(p.date_from, p.date_to))
            )
        else:
            qs = visible_employees(self.request.user, request=self.filter_request)
        qs = qs.select_related("department", "team", "designation")
        qs = apply_employee_filters(qs, self.request.user, self.filter_request)
        return list(qs.order_by("first_name", "last_name"))

    def _employees_for_rows(self):
        if self.params.range_mode:
            return [self.params.employee]
        return self._scoped_employees()

    def _rows(self, request):
        """All rows for the current mode/filters, with `?flagged=1` applied —
        unpaginated, so the export and the pager share one source."""
        rows = _build_timesheet_rows(
            self._employees_for_rows(), self.params.dates, request.user, request,
        )
        unaccounted_count = sum(1 for row in rows if row.is_unaccounted)
        if request.GET.get("flagged") == "1":
            rows = [row for row in rows if row.is_unaccounted]
        return rows, unaccounted_count

    def _scope_label(self) -> str:
        """Who the list is about, for the range-mode banner and the export
        filename: the employee ID, else the team, else the department."""
        p = self.params
        if p.range_mode:
            return p.employee.employee_id or p.employee.display_name
        dept_id, team_id = effective_filter_ids(self.request.user, self.filter_request)
        if team_id:
            team = Team.objects.filter(pk=team_id).select_related("department").first()
            if team:
                return f"{team.department.name} / {team.name}" if team.department_id else team.name
        if dept_id:
            dept = Department.objects.filter(pk=dept_id).first()
            if dept:
                return dept.name
        return "All departments"

    # --- date-range modes (single employee, or department/team) -----------

    def _render_rows(self, request):
        """Page over `(employee, date)` keys, then build full rows for the
        page's employees only — same rows, order and count as `_rows`, at a
        page's cost rather than the whole span's."""
        employees = self._employees_for_rows()
        dates = self.params.dates
        unaccounted = _unaccounted_keys(employees, dates)
        keys = [(emp.pk, d) for emp in employees for d in dates]
        if request.GET.get("flagged") == "1":
            keys = [key for key in keys if key in unaccounted]

        ctx = self._base_context()
        ctx["unaccounted_count"] = len(unaccounted)
        ctx.update(paginate_list(request, keys))
        page_keys = ctx["rows"]
        if page_keys:
            by_pk = {emp.pk: emp for emp in employees}
            built = {
                (row.employee.pk, row.date): row
                for row in _build_timesheet_rows(
                    [by_pk[pk] for pk in dict.fromkeys(pk for pk, _d in page_keys)],
                    sorted({d for _pk, d in page_keys}),
                    request.user, request,
                )
            }
            ctx["rows"] = [built[key] for key in page_keys]
        return render(request, self.template_name, ctx)

    # --- export (any mode) ------------------------------------------------

    def _export(self, request):
        from audit.capture import note

        rows, _unaccounted_count = self._rows(request)
        p = self.params
        note(request, rows=len(rows), scope=self._scope_label(),
             date_from=p.date_from.isoformat(), date_to=p.date_to.isoformat())
        return build_timesheet_export(rows, p.date_from, p.date_to, self._scope_label())

    # --- team mode (one row per employee, one date) ------------------------

    def get_queryset(self):
        selected = self.params.date_to
        self._selected = selected

        punch_prefetch = Prefetch(
            "attendance_punches",
            queryset=AttendancePunch.objects.filter(date=selected),
            to_attr="punch_for_date",
        )
        qs = (
            visible_employees(self.request.user, request=self.filter_request)
            .select_related("department", "team", "designation")
            .prefetch_related(punch_prefetch)
        )
        qs = apply_employee_filters(qs, self.request.user, self.filter_request)
        qs = qs.order_by("first_name", "last_name")

        pk_rows = list(qs.values_list("pk", "joining_date", "last_working_date"))
        all_pks = [row[0] for row in pk_rows]
        cutoffs = {row[0]: (row[1], row[2]) for row in pk_rows}
        self._resolved_by_pk = _resolve_shifts(all_pks, selected, cutoffs=cutoffs)
        punched_pks = set(
            AttendancePunch.objects
            .filter(date=selected, employee_id__in=all_pks, check_in__isnull=False)
            .values_list("employee_id", flat=True)
        )
        self._unaccounted_pks = {
            pk for pk, resolved in self._resolved_by_pk.items()
            if resolved and resolved.shift_type.category == ShiftType.Category.WORK
            and pk not in punched_pks
        }

        if self.request.GET.get("flagged") == "1":
            qs = qs.filter(pk__in=self._unaccounted_pks)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(self._base_context())
        ctx["unaccounted_count"] = len(self._unaccounted_pks)

        page_pks = [emp.pk for emp in ctx["employees"]]
        pending_correction_by_pk = {
            c.employee_id: c
            for c in (
                AttendanceCorrection.objects
                .filter(
                    employee_id__in=page_pks, date=self._selected,
                    status=AttendanceCorrection.Status.PENDING,
                )
            )
        }
        # For the corrected-record icon's tooltip — who approved it and why.
        # Last-approved-wins on the off chance a date got corrected twice.
        approved_correction_by_pk = {
            c.employee_id: c
            for c in (
                AttendanceCorrection.objects
                .filter(
                    employee_id__in=page_pks, date=self._selected,
                    status=AttendanceCorrection.Status.APPROVED,
                )
                .select_related("requested_by", "processed_by")
                .order_by("processed_at")
            )
        }
        user_can_edit_all = can_edit_all(self.request.user, request=self.request)

        for emp in ctx["employees"]:
            emp.punch = emp.punch_for_date[0] if emp.punch_for_date else None
            resolved = self._resolved_by_pk.get(emp.pk)
            emp.shift_label = _shift_label(resolved) if resolved else ""
            emp.is_unaccounted = emp.pk in self._unaccounted_pks
            emp.pending_correction = pending_correction_by_pk.get(emp.pk)
            emp.has_pending_correction = emp.pending_correction is not None
            emp.is_corrected = bool(
                emp.punch and emp.punch.source == AttendancePunch.Source.MANUAL_CORRECTION
            )
            emp.correction = approved_correction_by_pk.get(emp.pk) if emp.is_corrected else None
            emp.can_request_correction = (
                user_can_edit_all or emp.pk == self.request.user.pk
                or can_request_attendance_correction(self.request.user, emp, request=self.request)
            )
        return ctx


# ---------------------------------------------------------------------------
# Correction request + approve/decline/cancel
# ---------------------------------------------------------------------------

def _parse_date(raw, default):
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return default


def _correction_date_range(request):
    """`(from, to, errors)` for the optional working-date filter — same
    shape as leave.views._leave_date_range, filtering AttendanceCorrection.date
    (a single date, not a start/end pair, hence the distinct `date_from`/
    `date_to` param names rather than reusing `start_date`/`end_date`)."""
    parsed = {}
    errors = []
    for key, label in (("date_from", "Working date from"), ("date_to", "Working date to")):
        raw = (request.GET.get(key) or "").strip()
        value = None
        if raw:
            try:
                value = date.fromisoformat(raw)
            except ValueError:
                value = None
            if value is None:
                errors.append(f"{label}: '{raw}' isn't a valid date.")
        parsed[key] = value

    date_from, date_to = parsed["date_from"], parsed["date_to"]
    if date_from and date_to and date_from > date_to:
        errors.append("Working date from must be on or before Working date to.")
        return None, None, errors
    return date_from, date_to, errors


def _apply_correction_filters(qs, request, date_from=None, date_to=None):
    """q + department + team + working-date range for the corrections queue
    — same fields as accounts.filters.apply_employee_filters, but reached
    through `employee__` since the queryset here is AttendanceCorrection,
    not CustomUser."""
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(employee_search_q(q, prefix="employee__"))
    dept_id, team_id = effective_filter_ids(request.user, request)
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)
    if team_id:
        qs = qs.filter(employee__team_id=team_id)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    return qs


def _hx_redirect(url: str) -> HttpResponse:
    """204 No Content with HX-Redirect — htmx performs a full client-side
    navigation so the underlying list reloads with fresh badges/state.
    Same convention as policies.views._hx_redirect."""
    resp = HttpResponse(status=204)
    resp["HX-Redirect"] = url
    return resp


class AttendanceCorrectionRequestView(LoginRequiredMixin, View):
    """File a correction — or, for a date with no punch at all, add a
    timesheet from scratch. Same model and approval flow either way; the
    drawer's "Currently on file" box (or lack of one) makes the distinction
    self-evident, so there's no separate "add" vs "correct" code path.

    htmx GET returns a right-hand drawer fragment (`?for=<employee_pk>` for
    the on-behalf-of convention, same as leave.views.LeaveApplyView); a
    non-htmx GET falls back to a full page for direct navigation. A
    successful POST returns 204 + HX-Redirect (htmx) or a normal redirect,
    same drawer-or-page split as `policies`.
    """

    def _resolve_employee(self, request):
        raw = request.GET.get("for") or request.POST.get("for")
        if not raw:
            return request.user
        target = get_object_or_404(CustomUser, pk=raw)
        if target.pk == request.user.pk:
            return request.user
        if not can_request_attendance_correction(request.user, target, request=request):
            raise PermissionDenied("You can't request a correction on behalf of that employee.")
        return target

    def _resolve_date(self, request):
        raw = request.GET.get("date") or request.POST.get("date")
        return _parse_date(raw, date.today() - timedelta(days=1))

    def _existing_punch(self, employee, target_date):
        return AttendancePunch.objects.filter(employee=employee, date=target_date).first()

    def _redirect_target(self, target_date):
        return f"{reverse('attendance_list')}?date={target_date.isoformat()}"

    def _render(self, request, form, target, target_date, punch, *, status=200):
        ctx = {
            "form": form,
            "target_employee": target,
            "target_date": target_date,
            "requesting_for_other": target.pk != request.user.pk,
            "existing_punch": punch,
            "action_url": (
                f"{reverse('attendance_correction_request')}?for={target.pk}"
                f"&date={target_date.isoformat()}"
            ),
        }
        if request.htmx:
            return render(request, "attendance/_correction_request_drawer.html", ctx, status=status)
        ctx["page_title"] = "Add timesheet" if not punch else "Request timesheet correction"
        return render(request, "attendance/correction_request.html", ctx, status=status)

    def get(self, request):
        target = self._resolve_employee(request)
        target_date = self._resolve_date(request)
        punch = self._existing_punch(target, target_date)
        initial = {}
        if punch:
            initial = {"requested_check_in": punch.check_in, "requested_check_out": punch.check_out}
        form = AttendanceCorrectionForm(initial=initial)
        return self._render(request, form, target, target_date, punch)

    def post(self, request):
        target = self._resolve_employee(request)
        target_date = self._resolve_date(request)

        if AttendanceCorrection.objects.filter(
            employee=target, date=target_date, status=AttendanceCorrection.Status.PENDING,
        ).exists():
            messages.error(request, "A correction request is already pending for that date.")
            url = self._redirect_target(target_date)
            return _hx_redirect(url) if request.htmx else redirect(url)

        punch = self._existing_punch(target, target_date)
        form = AttendanceCorrectionForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, target, target_date, punch, status=400)

        correction = form.save(commit=False)
        correction.employee = target
        correction.date = target_date
        correction.punch = punch
        correction.original_check_in = punch.check_in if punch else None
        correction.original_check_out = punch.check_out if punch else None
        correction.requested_by = request.user
        correction.status = AttendanceCorrection.Status.PENDING
        correction.save()
        messages.success(
            request,
            f"Correction requested for {target.display_name} on {target_date} — "
            "awaiting approval.",
        )
        url = self._redirect_target(target_date)
        return _hx_redirect(url) if request.htmx else redirect(url)


class AttendanceCorrectionEditView(LoginRequiredMixin, View):
    """Right-hand drawer to revise a still-PENDING correction request.

    Same permission as filing one in the first place
    (can_request_attendance_correction) — this is the requester (or whoever
    could have filed on their behalf) changing their own ask before anyone's
    acted on it, not a reviewer editing it. Once approved/declined/cancelled
    the request is closed and this view 403s.
    """

    def dispatch(self, request, pk, *args, **kwargs):
        self.correction = get_object_or_404(AttendanceCorrection, pk=pk)
        if not self.correction.is_pending:
            raise PermissionDenied("Only a pending request can be edited.")
        if self.correction.employee_id != request.user.pk and not can_request_attendance_correction(
            request.user, self.correction.employee, request=request,
        ):
            raise PermissionDenied("You can't edit this correction request.")
        return super().dispatch(request, pk, *args, **kwargs)

    def _render(self, request, form, *, status=200):
        ctx = {
            "correction": self.correction,
            "form": form,
            "action_url": reverse("attendance_correction_edit", args=[self.correction.pk]),
        }
        return render(request, "attendance/_correction_edit_drawer.html", ctx, status=status)

    def get(self, request, pk):
        return self._render(request, AttendanceCorrectionForm(instance=self.correction))

    def post(self, request, pk):
        form = AttendanceCorrectionForm(request.POST, instance=self.correction)
        if not form.is_valid():
            return self._render(request, form, status=400)
        form.save()
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = "orbis:drawer-close"
            return response
        messages.success(request, "Correction request updated.")
        return redirect(f"{reverse('attendance_list')}?date={self.correction.date.isoformat()}")


class AttendanceCorrectionListView(LoginRequiredMixin, View):
    """Mine / Pending my approval / All, same tab shape as leave.views.LeaveListView."""

    template_name = "attendance/corrections_list.html"

    def get(self, request):
        tab = request.GET.get("tab", "mine")
        date_from, date_to, date_errors = _correction_date_range(request)

        mine_qs = (
            AttendanceCorrection.objects
            .filter(employee=request.user)
            .select_related("employee", "employee__team", "requested_by", "processed_by")
        )
        base_qs = (
            AttendanceCorrection.objects
            .select_related("employee", "employee__team", "employee__department", "requested_by", "processed_by")
        )
        user_can_edit_all = can_edit_all(request.user, request=request)
        pending_qs = base_qs.filter(status=AttendanceCorrection.Status.PENDING).exclude(employee=request.user)
        if not user_can_edit_all:
            pending_qs = pending_qs.filter(employee_id__in=managed_scope_pks(request.user, request=request))
        show_pending_tab = has_managed_scope(request.user, request=request)
        show_all_tab = user_can_edit_all
        all_qs = base_qs if show_all_tab else base_qs.none()
        # "Direct reports only" scope switch — same default-on rule as the
        # leave list (leave.views._direct_reports_only, 2026-10-10): narrows
        # *within* the visible scope, never widens it; hidden (full scope)
        # for a viewer with no direct reports. The topbar bell
        # (core/approvals.py) uses the same rule, so the counts agree.
        direct_pks = direct_report_pks(request.user, request=request)
        direct_only = bool(direct_pks) and request.GET.get("direct") != "0"
        if direct_only:
            pending_qs = pending_qs.filter(employee_id__in=direct_pks)
            all_qs = all_qs.filter(employee_id__in=direct_pks)

        if tab == "pending" and show_pending_tab:
            selected_qs = _apply_correction_filters(pending_qs, request, date_from, date_to)
        elif tab == "all" and show_all_tab:
            selected_qs = _apply_correction_filters(all_qs, request, date_from, date_to)
        else:
            tab = "mine"
            selected_qs = _apply_correction_filters(mine_qs, request, date_from, date_to)

        page = paginate_list(request, selected_qs)
        # Same actionable condition as the row template's Approve/Decline
        # branch — drives the bulk checkbox column + "select all".
        actionable_pks = [
            c.pk for c in page["rows"]
            if c.status == AttendanceCorrection.Status.PENDING
            and (c.employee_id != request.user.pk or user_can_edit_all)
        ]
        ctx = {
            "page_title": "Timesheet corrections",
            "tab": tab,
            "actionable_pks": actionable_pks,
            **page,
            "mine_count": mine_qs.count(),
            "pending_count": pending_qs.count(),
            "all_count": all_qs.count() if show_all_tab else 0,
            "show_pending_tab": show_pending_tab,
            "show_all_tab": show_all_tab,
            "current_user_pk": request.user.pk,
            "user_can_edit_all": user_can_edit_all,
            "has_direct_reports": bool(direct_pks),
            "direct_only": direct_only,
            "filters_active": bool(
                request.GET.get("q") or request.GET.get("department") or request.GET.get("team")
                or request.GET.get("date_from") or request.GET.get("date_to")
            ),
            "date_from_input": (request.GET.get("date_from") or "").strip(),
            "date_to_input": (request.GET.get("date_to") or "").strip(),
            "date_errors": date_errors,
            **employee_filter_context(request.user, request),
        }
        return render(request, self.template_name, ctx)


def _approve_correction(correction: AttendanceCorrection, user, note: str = "") -> None:
    """Write the requested times onto AttendancePunch and mark APPROVED.

    Caller is responsible for the can_act_on_attendance_correction + is_pending
    checks — shared by the single-row and bulk approve views so the two can't
    drift apart.
    """
    punch, _ = AttendancePunch.objects.update_or_create(
        employee=correction.employee, date=correction.date,
        defaults={
            "check_in": correction.requested_check_in,
            "check_out": correction.requested_check_out,
            "time_in_office": correction.effective_time_in_office,
            "source": AttendancePunch.Source.MANUAL_CORRECTION,
        },
    )
    correction.punch = punch
    correction.status = AttendanceCorrection.Status.APPROVED
    correction.processed_by = user
    correction.processed_at = timezone.now()
    correction.process_note = note
    correction.save()


def _decline_correction(correction: AttendanceCorrection, user, note: str = "") -> None:
    correction.status = AttendanceCorrection.Status.DECLINED
    correction.processed_by = user
    correction.processed_at = timezone.now()
    correction.process_note = note
    correction.save()


class _CorrectionActionBase(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.correction = get_object_or_404(AttendanceCorrection, pk=pk)
        return super().dispatch(request, pk, *args, **kwargs)


class AttendanceCorrectionApproveView(_CorrectionActionBase):
    @transaction.atomic
    def post(self, request, pk):
        correction = self.correction
        if not can_act_on_attendance_correction(request.user, correction, request=request) or not correction.is_pending:
            raise PermissionDenied
        _approve_correction(correction, request.user, request.POST.get("process_note", "").strip())
        messages.success(request, f"Correction approved for {correction.employee.display_name}.")
        return redirect(f"{reverse('attendance_correction_list')}?tab=pending")


class AttendanceCorrectionDeclineView(_CorrectionActionBase):
    def post(self, request, pk):
        correction = self.correction
        if not can_act_on_attendance_correction(request.user, correction, request=request) or not correction.is_pending:
            raise PermissionDenied
        _decline_correction(correction, request.user, request.POST.get("process_note", "").strip())
        messages.info(request, f"Correction declined for {correction.employee.display_name}.")
        return redirect(f"{reverse('attendance_correction_list')}?tab=pending")


class AttendanceCorrectionBulkActionView(LoginRequiredMixin, View):
    """Approve or decline several PENDING requests in one submit — the
    checkbox column + "Approve selected"/"Decline selected" toolbar on
    /attendance/corrections/. Each id is still individually re-checked for
    can_act_on_attendance_correction + is_pending (never trust a stale page's
    checkbox state), and anything that fails either check is silently
    skipped rather than aborting the whole batch — a colleague already
    having acted on one of the selected rows shouldn't block the rest.
    """

    http_method_names = ["post"]

    def post(self, request):
        return_tab = request.POST.get("tab") or "pending"
        return_url = f"{reverse('attendance_correction_list')}?tab={return_tab}"

        action = request.POST.get("action")
        if action not in ("approve", "decline"):
            messages.error(request, "Unknown bulk action.")
            return redirect(return_url)

        ids = list(dict.fromkeys(request.POST.getlist("ids")))
        if not ids:
            messages.info(request, "No requests selected.")
            return redirect(return_url)

        acted = 0
        with transaction.atomic():
            corrections = AttendanceCorrection.objects.filter(pk__in=ids).select_related("employee")
            for correction in corrections:
                if not correction.is_pending or not can_act_on_attendance_correction(
                    request.user, correction, request=request,
                ):
                    continue
                if action == "approve":
                    _approve_correction(correction, request.user)
                else:
                    _decline_correction(correction, request.user)
                acted += 1
        # len(ids) rather than a per-row counter, so a submitted id that
        # doesn't even exist (a stale page, tampered form) is counted as
        # skipped too, not silently dropped from the tally.
        skipped = len(ids) - acted

        verb = "Approved" if action == "approve" else "Declined"
        summary = f"{verb} {acted} request(s)."
        if skipped:
            summary += f" Skipped {skipped} (already handled or not yours to act on)."
        (messages.success if action == "approve" else messages.info)(request, summary)
        return redirect(return_url)


class AttendanceCorrectionCancelView(_CorrectionActionBase):
    def post(self, request, pk):
        correction = self.correction
        if correction.employee_id != request.user.pk or not correction.is_pending:
            raise PermissionDenied
        correction.status = AttendanceCorrection.Status.CANCELLED
        correction.processed_by = request.user
        correction.processed_at = timezone.now()
        correction.save()
        messages.info(request, "Correction request cancelled.")
        return redirect(f"{reverse('attendance_correction_list')}?tab=mine")
