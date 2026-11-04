from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, IntegerField, Q
from django.db.models.functions import Cast

from accounts.filters import employee_search_q
from accounts.models import CustomUser
from payroll import inputs as payroll_inputs
from payroll.meals import MealsUnavailable, fetch_meal_counts
from payroll.models import OvertimeType
from payroll.periods import employed_during_q
from roster.models import Shift, ShiftType


@dataclass(frozen=True)
class PayrollFilters:
    start_date: date
    end_date: date
    department_id: int | None = None
    team_id: int | None = None
    job_location_id: int | None = None
    work_location_id: int | None = None
    q: str | None = None


LEAVE_BUCKET_ALIASES = {
    "AL": ("AL", "ANNUAL LEAVE"),
    "CL": ("CL", "CASUAL LEAVE"),
    "SL": ("SL", "SICK LEAVE"),
    "MTL": ("MTL", "MATERNITY LEAVE"),
    "CPL": ("CPL", "COMPENSATORY LEAVE"),
    "HDL": ("HDL", "HALF DAY LEAVE"),
    "LWP": ("LWP", "LEAVE WITHOUT PAY"),
}


def validate_payroll_report_range(start: date, end: date) -> None:
    """Raise ValueError for a range the report refuses. Query-free, so the
    page shell can check it before deferring the table to htmx."""
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    if (end - start).days > 31:
        raise ValueError("Date range cannot exceed 31 days. Please select a valid payroll month.")


def payroll_report_employees(filters: PayrollFilters, viewer=None, request=None) -> list:
    """The employees the report covers, in display order — one query.

    Split out of `build_payroll_report` (2026-10-13) so the page can
    paginate *before* computing anything: the order (employee ID) doesn't
    depend on the computed counts, so the view builds rows for the current
    page only (PayrollReportView).

    When `viewer` is given, rows are limited to employees that viewer may see
    (self + recursive reportees; org-wide for HR/Admins). Without a viewer the
    report is org-wide — callers must gate access themselves.

    The population is the engine's (`employed_during_q`), not `is_active`:
    until 2026-11-02 this was "currently active", so a run's Support Info
    dropped everyone who left during the period (deactivated the day after
    their LWD, still paid that period) and listed joiners who start after it
    — October 2026 showed 1461 against HR's 1468.
    """
    validate_payroll_report_range(filters.start_date, filters.end_date)

    if viewer is not None:
        from accounts.permissions import visible_employees

        qs = visible_employees(viewer, request=request, include_separated=True)
    else:
        qs = CustomUser.objects.all()
    qs = qs.filter(employed_during_q(filters.start_date, filters.end_date))
    qs = qs.select_related(
        "department",
        "designation",
        "team",
        "job_location",
        "work_location",
        "job_info",
    )
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    if filters.team_id:
        qs = qs.filter(team_id=filters.team_id)
    if filters.job_location_id:
        qs = qs.filter(job_location_id=filters.job_location_id)
    if filters.work_location_id:
        qs = qs.filter(work_location_id=filters.work_location_id)
    cond = employee_search_q(filters.q)
    if cond is not None:
        qs = qs.filter(cond)

    return list(qs.order_by("employee_id", "first_name", "last_name", "email"))


def payroll_report_ot_types(filters: PayrollFilters, employee_pks: list) -> list:
    """The OvertimeTypes that get a column: those with approved entries for
    any of `employee_pks` in the window, by name."""
    # One OT column per overtime_type actually present in this filtered
    # scope/window — not a fixed catalog listing, and not a single summed
    # "OT hours" column. Different OT types carry different pay rates
    # (payroll.PayRate.RateType.OVERTIME is scoped per overtime_type), so
    # collapsing them into one total would hide exactly the number HR
    # needs. A department with only Regular OT + GH OT shows 2 columns;
    # add a Special OT entry anywhere in scope and a 3rd appears — added
    # 2026-09-08 on request.
    #
    # Always computed over the *whole* filtered scope, never one page, or
    # the columns would come and go as you page through the report.
    ot_map = payroll_inputs.collect_overtime(filters, employee_pks)
    present_type_ids = {tid for by_key in ot_map.values() for (tid, _team_id) in by_key.keys()}
    return list(
        OvertimeType.objects.filter(pk__in=present_type_ids).order_by("name")
    ) if present_type_ids else []


def build_payroll_report(filters: PayrollFilters, viewer=None, request=None) -> dict:
    """Build the payroll report for every employee in scope (the export).
    The page builds one page at a time via the three functions it wraps."""
    employees = payroll_report_employees(filters, viewer=viewer, request=request)
    ot_types = payroll_report_ot_types(filters, [e.pk for e in employees])
    return build_payroll_report_rows(employees, filters, ot_types)


def build_payroll_report_rows(employees: list, filters: PayrollFilters, ot_types: list) -> dict:
    """Report rows for exactly `employees` (scoped and ordered by
    `payroll_report_employees`); `ot_types` from `payroll_report_ot_types`
    over the whole scope so the OT columns stay fixed across pages."""
    employee_pks = [employee.pk for employee in employees]
    shift_counts = _payroll_shift_counts(employee_pks, filters.start_date, filters.end_date)

    # Night duty (Regular/Custom/Dedicated) + transport-eligible days, and
    # overtime hours — the same classification/collection the calculation
    # engine uses (payroll/inputs.py), so this pre-payroll cross-check report
    # can never silently drift from what a run would actually pay. See
    # docs/PAYROLL_PLAN.md's 2026-09-07 dated section.
    night_counts = payroll_inputs.classify_night_shifts(filters, employee_pks)
    ot_map = payroll_inputs.collect_overtime(filters, employee_pks)

    # Meal counts come from the separate meals system (payroll/meals.py), not
    # from anything Orbis tracks itself — keyed by the same employee_id string
    # used company-wide. Missing key == 0 meals (the API omits zero rows).
    # A real fetch failure raises MealsUnavailable rather than silently
    # handing back {} — on this report a bare {} would be indistinguishable
    # from "everyone genuinely had zero meals". Soft-warn rather than fail
    # the whole report (docs/PAYROLL_PLAN.md's meals.py note + the payroll
    # convention of warning over hard-blocking an ambiguous control point).
    meal_counts_unavailable = False
    try:
        meal_counts = fetch_meal_counts(filters.start_date, filters.end_date)
    except MealsUnavailable:
        meal_counts = {}
        meal_counts_unavailable = True

    rows = []
    for emp in employees:
        job_info = _safe_job_info(emp)
        counts = shift_counts.get(emp.pk, _empty_payroll_counts())
        night = night_counts.get(emp.pk, {})
        counts["NIGHT_REGULAR"] = night.get(payroll_inputs.NIGHT_REGULAR, 0)
        counts["NIGHT_CUSTOM"] = night.get(payroll_inputs.NIGHT_CUSTOM, 0)
        counts["NIGHT_DEDICATED"] = night.get(payroll_inputs.NIGHT_DEDICATED, 0)
        # Nights before confirmation in a department that pays none until
        # then — shown, never paid (payroll_inputs.classify_night_shifts).
        counts["NIGHT_UNPAID"] = night.get(payroll_inputs.NIGHT_PRE_CONFIRMATION, 0)
        counts["TRANSPORT"] = night.get("TRANSPORT", 0)
        # ot_map is keyed by (overtime_type_id, worked_in_id) — collapse
        # back to one hours figure per type across teams for this report,
        # which doesn't break OT out by team (only the engine's rate
        # resolution needs that split).
        emp_ot = ot_map.get(emp.pk, {})
        ot_by_type = [
            (
                Decimal(sum(d["seconds"] for (tid, _team_id), d in emp_ot.items() if tid == ot.pk))
                / Decimal(3600)
            ).quantize(Decimal("0.01"))
            for ot in ot_types
        ]
        rows.append({
            "employee_pk": emp.pk,
            "employee_id": emp.employee_id,
            "name": emp.display_name,
            "designation": emp.designation.name if emp.designation else "",
            "department": emp.department.name if emp.department else "",
            "team": emp.team.name if emp.team else "",
            "job_location": emp.job_location.code if emp.job_location else "",
            "work_location": emp.work_location.name if emp.work_location else "",
            "resource_status": (
                job_info.get_resource_status_display()
                if job_info and job_info.resource_status
                else ""
            ),
            "billing_status": (
                job_info.get_billing_status_display()
                if job_info and job_info.billing_status
                else ""
            ),
            "joining_date": emp.joining_date,
            "counts": counts,
            "ot_by_type": ot_by_type,
            "meal_count": meal_counts.get(emp.employee_id, 0),
        })

    return {
        "holidays": [],
        "rows": rows,
        "ot_types": ot_types,
        "meal_counts_unavailable": meal_counts_unavailable,
    }


def _safe_job_info(employee):
    try:
        return employee.job_info
    except ObjectDoesNotExist:
        return None


def _empty_payroll_counts() -> dict:
    return {
        "AL": 0,
        "CL": 0,
        "SL": 0,
        "MTL": 0,
        "FL": 0,
        "CPL": 0,
        "HDL": 0.0,
        "LWP": 0,
        "NIGHT_REGULAR": 0,
        "NIGHT_CUSTOM": 0,
        "NIGHT_DEDICATED": 0,
        "NIGHT_UNPAID": 0,
        "TRANSPORT": 0,
    }


def _payroll_shift_counts(emp_ids: list, start: date, end: date) -> dict:
    if not emp_ids:
        return {}

    qs = (
        Shift.objects
        .filter(employee_id__in=emp_ids, date__gte=start, date__lte=end)
        .exclude(shift_type__category=ShiftType.Category.WORK)
        .values("employee_id", "shift_type__name", "shift_type__category")
        .annotate(total=Count("id"))
    )

    out = defaultdict(_empty_payroll_counts)
    for row in qs:
        bucket = _bucket_for_shift(row["shift_type__name"], row["shift_type__category"])
        if not bucket:
            continue

        total = row["total"]
        if bucket == "HDL":
            out[row["employee_id"]][bucket] += total * 0.5
        else:
            out[row["employee_id"]][bucket] += total

    return dict(out)


def _bucket_for_shift(name: str | None, category: str | None) -> str | None:
    """LEAVE/HOLIDAY buckets only — WORK-category night/custom/dedicated/
    transport/OT counts come from payroll_inputs.classify_night_shifts /
    collect_overtime instead (build_payroll_report merges them in), sharing
    the engine's exact-match classification rather than this function's
    fuzzy alias matching (docs/PAYROLL_PLAN.md Risk 2)."""
    normalized = _normalize_shift_name(name)

    if category == ShiftType.Category.HOLIDAY:
        return "FL"

    if category == ShiftType.Category.LEAVE:
        for bucket, aliases in LEAVE_BUCKET_ALIASES.items():
            if _matches_any_alias(normalized, aliases):
                return bucket
        return None

    return None


def _matches_any_alias(normalized_name: str, aliases: tuple[str, ...]) -> bool:
    return any(_has_alias(normalized_name, alias) for alias in aliases)


def _has_alias(normalized_name: str, alias: str) -> bool:
    normalized_alias = _normalize_shift_name(alias)
    haystack = f" {normalized_name} "
    needle = f" {normalized_alias} "
    return needle in haystack


def _normalize_shift_name(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()


# ---------------------------------------------------------------------------
# Shift summary report (leave/reports/shift-summary)
# ---------------------------------------------------------------------------

class CrossYearSummaryError(Exception):
    """Raised when a shift-summary range straddles two calendar years.

    Leave entitlements, balances, and pro-rating are all defined per calendar
    year, so a single summary row can't represent two years coherently. The
    view surfaces this as a form error.
    """


@dataclass(frozen=True)
class ShiftSummaryFilters:
    start_date: date
    end_date: date
    department_id: int | None = None
    team_id: int | None = None
    q: str | None = None


# Allocation columns are keyed by the LEAVE ShiftType name. AL is ledger-driven
# (accrued monthly), CL/SL are a fixed annual entitlement pro-rated to the
# portion of the year covered by the report window.
_AL_NAME = "AL"
_CL_NAME = "CL"
_SL_NAME = "SL"


def _round_half(raw: Decimal) -> Decimal:
    """Round to the nearest 0.5 day (half rounds up)."""
    return (raw * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2


def _clean_days(value: Decimal) -> Decimal:
    """Drop trailing decimals for display: whole numbers render as `12`, not
    `12.00`; genuine half-days keep one place (`2.5`). Allocations are already
    rounded to 0.5, so one decimal place is enough."""
    value = Decimal(value)
    if value == value.to_integral_value():
        return value.quantize(Decimal("1"))
    return value.quantize(Decimal("0.1"))


def _prorate_annual(annual_max: int, first_working_date: date,
                    period_end: date, year: int) -> Decimal:
    """Entitlement earned by `period_end`: annual_max scaled by the fraction of
    the calendar year between `first_working_date` and `period_end` (inclusive),
    rounded to 0.5 days."""
    if annual_max <= 0:
        return Decimal("0")
    days_in_year = (date(year, 12, 31) - date(year, 1, 1)).days + 1
    overlap = (period_end - first_working_date).days + 1
    if overlap <= 0:
        return Decimal("0")
    overlap = min(overlap, days_in_year)
    return _round_half(Decimal(annual_max) * Decimal(overlap) / Decimal(days_in_year))


def validate_shift_summary_range(start: date, end: date) -> None:
    """Raise ValueError / CrossYearSummaryError for a range the summary can't
    run on. Query-free, so the page shell can check it before deferring the
    table to its htmx request."""
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    if start.year != end.year:
        raise CrossYearSummaryError(
            "Date range must stay within a single calendar year."
        )


def shift_summary_employees(filters: ShiftSummaryFilters, viewer=None, request=None) -> list:
    """The employees the summary covers, in display order — one cheap query.

    Split out of `build_shift_summary` (2026-10-13) so the page can paginate
    *before* computing anything: the tallies and AL snapshot cost per
    employee, and the sort (employee ID) doesn't depend on them, so the
    view builds rows for the current page only. Scoping rules are below.
    """
    start, end = filters.start_date, filters.end_date
    validate_shift_summary_range(start, end)

    # Active during the window: joined by the end, and not separated before the
    # start (a set last_working_date before `start` means they'd already left).
    qs = CustomUser.objects.select_related("department", "team").filter(
        Q(joining_date__isnull=True) | Q(joining_date__lte=end),
        Q(last_working_date__isnull=True) | Q(last_working_date__gte=start),
    ).exclude(Q(employee_id__isnull=True) | Q(employee_id=""))
    if viewer is not None:
        from accounts.permissions import can_see_all, visible_employee_pks

        # PK restriction (not a swap to visible_employees()) so HR keeps
        # seeing employees separated mid-window, who are already inactive.
        if not can_see_all(viewer, request=request):
            qs = qs.filter(pk__in=visible_employee_pks(viewer, request=request))
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    if filters.team_id:
        qs = qs.filter(team_id=filters.team_id)
    cond = employee_search_q(filters.q)
    if cond is not None:
        qs = qs.filter(cond)
    # employee_id is a CharField of digits, so a plain string sort puts "100"
    # before "20" -- cast to int for true ascending numeric order.
    return list(
        qs.annotate(employee_id_num=Cast("employee_id", IntegerField()))
          .order_by("employee_id_num", "first_name", "last_name", "email")
    )


def build_shift_summary(filters: ShiftSummaryFilters, viewer=None, request=None) -> list[dict]:
    """Per-employee roster + leave summary for a date range within one year.

    One row per employee active during the window (joined on/before the end,
    not separated before the start). Columns: shift-category tallies
    (days_worked / OFF / GH), a count per leave type taken, leave allocation
    (AL from the ledger, CL/SL pro-rated), and taken / remaining totals.

    Every leave-type column, AL included, counts what was taken inside the
    report's start/end window. AL's allocation lives on the employee's own
    anniversary cycle, so it gets its own cycle-basis trio instead —
    `al_allocated`, `al_cycle_used`, `al_cycle_balance` (via
    `_al_ledger_snapshot`). Revised 2026-10-30: the AL column used to show
    cycle-to-date usage, which read as "AL taken in this range" and wasn't
    (docs/LEAVE_POLICY_IMPLEMENTATION.md). CL/SL use the report window /
    calendar-year pro-ration as before.

    When `viewer` is given, rows are limited to self + recursive reportees
    (org-wide for HR/Admins). Without a viewer the summary is org-wide —
    callers must gate access themselves.
    """
    return build_shift_summary_rows(
        shift_summary_employees(filters, viewer=viewer, request=request), filters,
    )


def build_shift_summary_rows(employees: list, filters: ShiftSummaryFilters) -> list[dict]:
    """Summary rows for exactly `employees` (already scoped and ordered by
    `shift_summary_employees`) — the view passes one page, the export all."""
    start, end = filters.start_date, filters.end_date
    year = start.year
    if not employees:
        return []

    emp_ids = [e.pk for e in employees]
    leave_type_names = list(
        ShiftType.objects
        .filter(category=ShiftType.Category.LEAVE)
        .values_list("name", flat=True)
    )

    counts = _shift_summary_counts(emp_ids, start, end, leave_type_names)
    al_snapshot = _al_ledger_snapshot(employees, end)
    cl_max = _annual_entitlement(_CL_NAME)
    sl_max = _annual_entitlement(_SL_NAME)

    rows = []
    for emp in employees:
        first_working_date = emp.joining_date if (emp.joining_date and emp.joining_date > start) else start
        c = counts[emp.pk]

        al_allocated, al_cycle_used = al_snapshot.get(emp.pk, (Decimal("0"), Decimal("0")))
        al_cycle_balance = al_allocated - al_cycle_used
        cl_allocated = _prorate_annual(cl_max, first_working_date, end, year)
        sl_allocated = _prorate_annual(sl_max, first_working_date, end, year)
        total_allocated = al_allocated + cl_allocated + sl_allocated
        total_taken = c["total_leave_taken"]
        # Each part on its own basis: AL against its cycle, CL/SL against
        # their pro-rated allocation and what the window shows taken.
        remaining = (
            al_cycle_balance
            + cl_allocated - c["leave"].get(_CL_NAME, 0)
            + sl_allocated - c["leave"].get(_SL_NAME, 0)
        )

        row = {
            "employee_pk": emp.pk,
            "employee_id": emp.employee_id,
            "employee_name": emp.display_name,
            "department": emp.department.name if emp.department else "",
            "team": emp.team.name if emp.team else "",
            "first_working_date": first_working_date,
            "days_worked": c["days_worked"],
            "OFF": c["OFF"],
            "GH": c["GH"],
            "total_leave_taken": _clean_days(total_taken),
            "al_allocated": _clean_days(al_allocated),
            "al_cycle_used": _clean_days(al_cycle_used),
            "al_cycle_balance": _clean_days(al_cycle_balance),
            "cl_allocated": _clean_days(cl_allocated),
            "sl_allocated": _clean_days(sl_allocated),
            "total_leave_allocated": _clean_days(total_allocated),
            "leave_remaining": _clean_days(remaining),
        }
        for name in leave_type_names:
            row[name] = c["leave"][name]
        rows.append(row)

    return rows


def _shift_summary_counts(emp_ids: list, start: date, end: date,
                          leave_type_names: list[str]) -> dict:
    """Per-employee shift tallies: days_worked (WORK), OFF, GH (HOLIDAY), and a
    count per leave type taken plus the leave total."""
    def _blank():
        return {
            "days_worked": 0,
            "OFF": 0,
            "GH": 0,
            "total_leave_taken": 0,
            "leave": {name: 0 for name in leave_type_names},
        }

    out = defaultdict(_blank)
    # Touch every requested employee so rows render even with zero shifts.
    for pk in emp_ids:
        out[pk]

    # Grouped on the bare shift_type_id and labelled in Python — joining
    # ShiftType for name/category made MySQL group ~400k rows through a
    # temporary table (2.1s vs 0.6s company-wide, measured 2026-10-13).
    shift_types = {
        st.pk: (st.name, st.category)
        for st in ShiftType.objects.only("pk", "name", "category")
    }
    qs = (
        Shift.objects
        .filter(employee_id__in=emp_ids, date__gte=start, date__lte=end)
        .values("employee_id", "shift_type_id")
        .annotate(total=Count("id"))
    )
    for row in qs:
        bucket = out[row["employee_id"]]
        name, category = shift_types[row["shift_type_id"]]
        total = row["total"]
        if category == ShiftType.Category.WORK:
            bucket["days_worked"] += total
        elif category == ShiftType.Category.OFF:
            bucket["OFF"] += total
        elif category == ShiftType.Category.HOLIDAY:
            bucket["GH"] += total
        elif category == ShiftType.Category.LEAVE:
            if name in bucket["leave"]:
                bucket["leave"][name] += total
            bucket["total_leave_taken"] += total

    return out


def _al_ledger_snapshot(employees: list, as_of: date) -> dict:
    """Map employee_pk → (allocated, taken) for AL's *current open cycle*
    as of `as_of`, both computed from each employee's own anniversary
    cycle so they share one basis. Empty when there's no AL policy.

    - allocated: opening + live-computed accrued + carried_forward.
    - taken: `live_used` — the ledger's `used` plus any not-yet-reconciled
      shift usage inside the open cycle window.

    Revised 2026-09-01 — this used to filter `LeaveBalance` by a bare
    `year=<report's calendar year>`, which broke once AL became anniversary-
    cycle: `LeaveBalance.year` now means "the calendar year the employee's
    own cycle started," not the report's year, so an employee whose cycle
    spans two calendar years (e.g. Nov–Nov) would silently match the wrong
    row (or a stale pre-rework artifact) instead of their real one — found
    while reviewing a real employee, see docs/LEAVE_POLICY_IMPLEMENTATION.md.
    Looking up each employee's own row via `get_or_create_balance` (keyed
    off their `joining_date`, same as the balance page) is the fix.

    Also revised 2026-09-02 — "taken" here is cycle-scoped so it shares
    "allocated"'s basis. Since 2026-10-30 it feeds only the report's
    "AL used (cycle)" / "AL balance (cycle)" columns; the plain AL column
    is the report window's own count again.
    """
    from leave.models import LeavePolicy
    from leave.services import get_or_create_balances, is_eligible_for_policy, live_accrued_and_used_bulk

    al_policy = (
        LeavePolicy.objects
        .filter(shift_type__name=_AL_NAME, shift_type__category=ShiftType.Category.LEAVE)
        .first()
    )
    if not al_policy:
        return {}

    upto = min(as_of, date.today())
    eligible = [
        emp for emp in employees
        if emp.joining_date and is_eligible_for_policy(emp, al_policy, as_of=upto)[0]
    ]
    # Batched (2026-10-13): same numbers as get_or_create_balance +
    # live_accrued_al + live_used per employee, in a handful of queries
    # instead of ~3 per employee — see leave/services.py's batch section.
    balances = get_or_create_balances(eligible, al_policy, upto)
    live = live_accrued_and_used_bulk(balances.values(), al_policy, upto=upto)
    out = {}
    for emp_pk, bal in balances.items():
        accrued, taken = live[bal.pk]
        out[emp_pk] = (bal.opening + accrued + bal.carried_forward, taken)
    return out


def _annual_entitlement(shift_type_name: str) -> int:
    """`max_days_per_year` for a leave type by name, or 0 if no policy."""
    from leave.models import LeavePolicy

    policy = (
        LeavePolicy.objects
        .filter(shift_type__name=shift_type_name,
                shift_type__category=ShiftType.Category.LEAVE)
        .first()
    )
    return policy.max_days_per_year if policy else 0
