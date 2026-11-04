"""Role-aware data builders for the dashboard at `/`.

Three pure functions — `me_context`, `team_context`, `org_context` — each
return a dict the template renders. The matching `visible_tabs` /
`default_tab` helpers tell the view which tabs to show and which to land on.

Permission gating piggybacks on `accounts.permissions`:
    - Me   → every authenticated user.
    - Team → users with recursive reportees.
    - Org  → `can_see_all` (Admins, HR, superusers).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Count, Q

from django.urls import reverse

from accounts.locations import office_bucket, resolve_to_dropdown_choice
from accounts.models import CustomUser
from accounts.permissions import (
    can_see_all,
    has_dedicated_night_shift_approval_scope,
    has_managed_scope,
    get_reportee_user_ids,
    direct_report_pks,
    visible_employee_pks,
)
from core.approvals import (
    pending_attendance_correction_qs,
    pending_dedicated_night_shift_qs,
    pending_leave_qs,
    pending_shift_change_qs,
)
from employees.models import JobInfo
from employees.services import (
    current_night_shift_consent_period,
    get_or_create_night_shift_consent,
    qualifies_for_night_shift_consent,
)
from leave.models import (
    LeaveApplication,
    LeaveBalance,
    LeavePolicy,
)
from leave.services import live_accrued_al, live_available, live_used
from payroll.models import EmployeeTaxDeclaration
from roster.models import Holiday, Roster, Shift, ShiftType
from roster.services import shift_is_after_last_working_date, shift_is_before_joining_date


# ---------------------------------------------------------------------------
# Tab visibility
# ---------------------------------------------------------------------------

TAB_ORDER = ["me", "team", "org"]


def is_people_manager(user, request=None) -> bool:
    """True iff the user actually manages humans.

    Decoupled from `accounts.permissions.has_managed_scope` because that
    returns True for HR / Admins / superusers via the `can_edit_all`
    short-circuit — useful for write checks, but wrong for tab gating.
    The Team tab is a *manager view* of recursive reportees.
    """
    if not user or not user.is_authenticated:
        return False
    return bool(get_reportee_user_ids(user, request=request))


def visible_tabs(user, request=None) -> list[str]:
    if not user or not user.is_authenticated:
        return []
    tabs = ["me"]
    if is_people_manager(user, request=request):
        tabs.append("team")
    if can_see_all(user, request=request):
        tabs.append("org")
    return tabs


def default_tab(user, request=None) -> str:
    """Highest-scope tab the user can see (Org > Team > Me)."""
    tabs = visible_tabs(user, request=request)
    for candidate in reversed(TAB_ORDER):
        if candidate in tabs:
            return candidate
    return "me"


# ---------------------------------------------------------------------------
# Me tab
# ---------------------------------------------------------------------------

def me_context(user, request=None) -> dict:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)
    can_access_roster = has_managed_scope(user, request=request)

    next_shift = (
        Shift.objects
        .filter(employee=user, date__gt=today, date__lte=today + timedelta(days=7))
        .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        .order_by("date")
        .first()
    )
    next_shift_location = ""
    if next_shift and next_shift.shift_type.category == ShiftType.Category.WORK:
        next_shift_location = resolve_to_dropdown_choice(
            next_shift.job_location, next_shift.work_location,
        ) or resolve_to_dropdown_choice(
            getattr(user, "job_location", None), getattr(user, "work_location", None),
        )

    week_strip = _build_week_strip(user, week_start)
    leave_balances = _build_leave_balances(user, today.year)
    recent_applications = list(
        LeaveApplication.objects
        .filter(employee=user)
        .select_related("leave_type")
        .order_by("-applied_at")[:3]
    )
    upcoming_holidays = list(
        Holiday.objects
        .filter(date__gte=today, is_active=True)
        .order_by("date")[:3]
    )

    personal = getattr(user, "personal_info", None)
    is_birthday_today = bool(
        personal
        and personal.date_of_birth
        and personal.date_of_birth.month == today.month
        and personal.date_of_birth.day == today.day
    )

    probation_days_remaining = None
    if user.confirmation_date and user.confirmation_date > today:
        probation_days_remaining = (user.confirmation_date - today).days

    profile_completeness = _profile_completeness(user)

    # Night shift consent (Bangladesh Labour Act 2006 s.109) — only
    # materialises a row (and only shows the card) for a female employee
    # with a night shift scheduled this calendar month. See
    # employees/services.py and employees/models.py::NightShiftConsent.
    night_shift_consent = None
    if qualifies_for_night_shift_consent(user, current_night_shift_consent_period(today)):
        night_shift_consent, _created = get_or_create_night_shift_consent(user)

    # Tax declaration awaiting the employee's once-a-year confirmation
    # (payroll.EmployeeTaxDeclaration, revised 2026-10-07). One query; the
    # deadline lock is a property, so filter it in Python.
    pending_tax_declaration = next(
        (
            d for d in EmployeeTaxDeclaration.objects
            .filter(employee=user, submitted_at__isnull=True, tax_year__is_active=True)
            .select_related("tax_year")
            if not d.is_past_deadline
        ),
        None,
    )

    # One query for the manager card: the template reads the manager's
    # designation/department, so fetch JobInfo + manager + their FKs together
    # instead of the 3-4 lazy loads getattr(user, "job_info").reports_to costs.
    job_info = (
        JobInfo.objects
        .select_related("reports_to__designation", "reports_to__department")
        .filter(employee=user)
        .first()
    )
    manager = job_info.reports_to if job_info else None

    return {
        "today": today,
        "week_start": week_start,
        "week_end": week_end,
        "next_shift": next_shift,
        "next_shift_location": next_shift_location,
        "week_strip": week_strip,
        "leave_balances": leave_balances,
        "recent_applications": recent_applications,
        "upcoming_holidays": upcoming_holidays,
        "is_birthday_today": is_birthday_today,
        "probation_days_remaining": probation_days_remaining,
        "profile_completeness": profile_completeness,
        "manager": manager,
        "can_access_roster": can_access_roster,
        "night_shift_consent": night_shift_consent,
        "pending_tax_declaration": pending_tax_declaration,
    }


def _build_week_strip(user, week_start: date) -> list[dict]:
    """Mon→Sun list of dicts with the user's shift label per day.

    Falls back to the weekly `Roster` pattern when no concrete `Shift` row
    exists for a given date — same fallback the roster grid uses.
    """
    days = [week_start + timedelta(days=i) for i in range(7)]
    shifts = {
        s.date: s for s in (
            Shift.objects
            .filter(employee=user, date__in=days)
            .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        )
    }
    pattern = {
        r.weekday: r for r in (
            Roster.objects
            .filter(employee=user)
            .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        )
    }
    today = date.today()
    out = []
    for d in days:
        shift = shifts.get(d)
        pat = pattern.get(d.weekday())
        cell_type = None
        cell_attr = None
        if shift is not None:
            cell_type = shift.shift_type
            cell_attr = shift.shift_attribute
        elif pat is not None:
            cell_type = pat.shift_type
            cell_attr = pat.shift_attribute
        # The attribute (e.g. "EVE 2-11") is the specific variant a
        # supervisor actually picked; its parent type ("EVE") is only a
        # fallback for a cell with no attribute set. Same precedence as
        # roster/upload.py::_export_shift_value.
        shift_label = cell_attr.name if cell_attr is not None else (cell_type.name if cell_type else "")

        # Office/WFH label — only meaningful on a working day. Same precedence
        # the roster grid uses: the cell's own location, then the pattern's,
        # then the employee's contractual location.
        location = ""
        if cell_type and cell_type.category == ShiftType.Category.WORK:
            if shift is not None:
                location = resolve_to_dropdown_choice(shift.job_location, shift.work_location)
            elif pat is not None:
                location = resolve_to_dropdown_choice(pat.job_location, pat.work_location)
            if not location:
                location = resolve_to_dropdown_choice(
                    getattr(user, "job_location", None),
                    getattr(user, "work_location", None),
                )

        out.append({
            "date": d,
            "weekday_short": d.strftime("%a"),
            "is_today": d == today,
            "is_weekend": d.weekday() >= 5,
            "shift_type": cell_type,
            "shift_label": shift_label,
            "location": location,
            "is_leave": bool(cell_type and cell_type.category == ShiftType.Category.LEAVE),
            "is_off": bool(cell_type and cell_type.category == ShiftType.Category.OFF),
        })
    return out


def _build_leave_balances(user, year: int) -> list[dict]:
    """One row per existing LeaveBalance for this user/year.

    Dashboard intentionally does NOT auto-materialise missing balance rows
    (that's `LeaveBalanceView`'s job). If a row doesn't exist, it doesn't
    appear here.

    CALENDAR-cycle policies (CL/SL/MtL/...) are keyed by calendar `year`, so
    the naive filter below is correct for them. AL is ANNIVERSARY-cycle,
    keyed by the employee's own cycle *start* year, which is frequently a
    different year than `year` (today's calendar year) — filtering AL by
    `year=year` can miss the employee's actual open cycle row entirely and
    grab an unrelated (possibly dead legacy) row instead. So AL is looked up
    separately, by its open cycle window, the same way `LeaveBalanceView`
    does — but without `get_or_create_balance`'s side effect of creating a
    row, preserving the "doesn't auto-materialise" rule above.
    """
    calendar_balances = (
        LeaveBalance.objects
        .filter(employee=user, year=year, leave_policy__cycle=LeavePolicy.Cycle.CALENDAR)
        .select_related("leave_policy", "leave_policy__shift_type")
    )
    anniversary_balances = (
        LeaveBalance.objects
        .filter(
            employee=user,
            leave_policy__cycle=LeavePolicy.Cycle.ANNIVERSARY,
            cycle_start__isnull=False,
            cycle_end__gt=date.today(),
        )
        .select_related("leave_policy", "leave_policy__shift_type")
    )
    balances = sorted(
        list(calendar_balances) + list(anniversary_balances),
        key=lambda b: b.leave_policy.shift_type.name,
    )
    rows = []
    for bal in balances:
        policy = bal.leave_policy
        # AL (ANNIVERSARY + WORKED_DAY_RATIO) is only ever written to
        # `bal.accrued`/`bal.used` once, at cycle close — reading those
        # fields directly during an open cycle would show 0 all cycle long.
        # Go live the same way `LeaveBalanceView` already does, so this tile
        # always agrees with the full balance page.
        if (policy.cycle == LeavePolicy.Cycle.ANNIVERSARY
                and policy.accrual_method == LeavePolicy.AccrualMethod.WORKED_DAY_RATIO):
            accrued = live_accrued_al(user, policy, bal)
            used = live_used(user, policy, bal)
            available = live_available(user, policy, bal)
        else:
            accrued, available, used = bal.accrued, bal.available, bal.used
        # Total granted for the year — the credit side of `available`
        # (opening + accrued + carried_forward). `opening` is already seeded to
        # the (pro-rated) annual entitlement for fixed-quota policies, so this
        # must NOT also add `max_days_per_year` or it double-counts the quota.
        capacity = bal.opening + accrued + bal.carried_forward
        used_pct = 0
        if capacity > 0:
            used_pct = int(min(100, max(0, round((used / capacity) * 100))))
        rows.append({
            "name": policy.shift_type.name,
            "available": available,
            "used": used,
            "capacity": capacity,
            "used_pct": used_pct,
            "is_paid": policy.is_paid,
            "frozen": bal.entitlement_frozen,
        })
    return rows


_PROFILE_SECTIONS = (
    ("Job info", "has_job_info"),
    ("Personal info", "has_personal_info"),
    ("Address", "has_address"),
    ("Contact", "has_contact"),
    ("Profile picture", "has_picture"),
)


def _profile_completeness(user) -> dict:
    """Five-dimensional profile-completeness score.

    Sections chosen to map to user actions: file the basics, add an address,
    add a contact, upload a picture. Returns the % and the next missing
    section so the card can render a "Finish your profile" CTA.
    """
    has = {
        "has_job_info": hasattr(user, "job_info"),
        "has_personal_info": hasattr(user, "personal_info"),
        "has_address": user.addresses.exists() if hasattr(user, "addresses") else False,
        "has_contact": user.contacts.exists() if hasattr(user, "contacts") else False,
        "has_picture": bool(user.profile_picture),
    }
    filled = sum(1 for _, key in _PROFILE_SECTIONS if has[key])
    total = len(_PROFILE_SECTIONS)
    pct = int(round((filled / total) * 100)) if total else 0
    next_missing = next(
        (label for label, key in _PROFILE_SECTIONS if not has[key]),
        None,
    )
    return {
        "pct": pct,
        "filled": filled,
        "total": total,
        "next_missing": next_missing,
    }


# ---------------------------------------------------------------------------
# Team tab
# ---------------------------------------------------------------------------

def direct_report_rows(target, request=None) -> list[dict]:
    """``[{'user': CustomUser, 'team_size': int}]`` for ``target``'s direct reports.

    ``team_size`` is the recursive subtree size beneath each direct report,
    computed within ``target``'s downline from a single ``JobInfo`` edge query.
    """
    from employees.models import JobInfo

    managed_pks = get_reportee_user_ids(target, request=request)

    direct_reports = list(
        CustomUser.objects
        .filter(pk__in=direct_report_pks(target, request=request), is_active=True)
        .select_related("designation", "department")
        .order_by("first_name", "last_name")
    )

    children: dict = {}
    edges = (
        JobInfo.objects
        .filter(employee_id__in=managed_pks, employee__is_active=True)
        .values_list("reports_to_id", "employee_id")
    )
    for parent_pk, child_pk in edges:
        children.setdefault(parent_pk, []).append(child_pk)

    def _subtree_size(root_pk) -> int:
        seen: set = set()
        frontier = [root_pk]
        while frontier:
            frontier = [
                child for parent in frontier
                for child in children.get(parent, [])
                if child not in seen
            ]
            seen.update(frontier)
        return len(seen)

    return [
        {"user": emp, "team_size": _subtree_size(emp.pk)}
        for emp in direct_reports
    ]


# ---------------------------------------------------------------------------
# Pending-approvals widget (Team tab) — one row per approval type this user
# can act on. Adding a new type (e.g. Shift Change Request) is a single
# registry entry; no template change needed.
# ---------------------------------------------------------------------------

# Every count uses the topbar bell's scope (direct reports first,
# core/approvals.py) — the same default each list page's Pending tab shows,
# so the number here matches the page the row links to.
_pending_leave_qs = pending_leave_qs
_pending_attendance_correction_qs = pending_attendance_correction_qs
_pending_shift_change_qs = pending_shift_change_qs
_pending_dedicated_night_shift_qs = pending_dedicated_night_shift_qs


_APPROVAL_TYPES = [
    {
        "key": "leave",
        "label": "Leave applications",
        "icon": "plane",
        "list_url_name": "leave_list",
        "pending_qs": _pending_leave_qs,
        "eligible": has_managed_scope,
    },
    {
        "key": "timesheet",
        "label": "Timesheet corrections",
        "icon": "clock",
        "list_url_name": "attendance_correction_list",
        "pending_qs": _pending_attendance_correction_qs,
        "eligible": has_managed_scope,
    },
    {
        "key": "shift_change",
        "label": "Shift change requests",
        "icon": "refresh-cw",
        "list_url_name": "shift_change_list",
        "pending_qs": _pending_shift_change_qs,
        "eligible": has_managed_scope,
    },
    {
        "key": "night_shift",
        "label": "Dedicated night shift",
        "icon": "moon",
        "list_url_name": "dedicated_night_shift_list",
        "pending_qs": _pending_dedicated_night_shift_qs,
        "eligible": has_dedicated_night_shift_approval_scope,
    },
]


def pending_approvals_summary(user, request=None) -> list[dict]:
    """One entry per approval type the user is eligible to act on, each with
    its own pending count and a link straight to that type's Pending tab.

    A type is omitted entirely (not shown with a 0) when the user isn't
    eligible to approve it at all — e.g. a TL/APM-tier manager sees Leave
    and Timesheet rows but not Dedicated night shift, which needs
    Manager-tier (see has_dedicated_night_shift_approval_scope).
    """
    entries = []
    for spec in _APPROVAL_TYPES:
        if not spec["eligible"](user, request=request):
            continue
        count = spec["pending_qs"](user, request=request).count()
        entries.append({
            "key": spec["key"],
            "label": spec["label"],
            "icon": spec["icon"],
            "count": count,
            "url": f"{reverse(spec['list_url_name'])}?tab=pending",
        })
    return entries


def team_context(user, request=None) -> dict:
    """Manager-scoped view: recursive reportees.

    Excludes self so an HR manager looking at the Team tab sees only their
    actual managed people, not themselves.
    """
    today = date.today()
    next_30 = today + timedelta(days=30)
    next_7 = today + timedelta(days=7)

    managed_pks = get_reportee_user_ids(user, request=request)

    report_rows = direct_report_rows(user, request=request)
    direct_reports_count = len(report_rows)

    # "Out today / this week" — roster-driven, matching the Out-today drill-down
    # (`today_status_buckets`). An approved leave materialises LEAVE-category
    # Shift rows, so this counts both approved leave and manual roster SL edits.
    managed_qs = CustomUser.objects.filter(is_active=True, pk__in=managed_pks)
    leave_today_count = len(today_status_buckets(managed_qs, today=today)["leave_pks"])
    leave_week_count = len(leave_pks_in_range(managed_qs, today, next_7))

    pending_approvals = pending_approvals_summary(user, request=request)
    pending_total = sum(entry["count"] for entry in pending_approvals)

    confirmations_due = list(
        CustomUser.objects
        .filter(
            pk__in=managed_pks,
            is_active=True,
            confirmation_date__gte=today,
            confirmation_date__lte=next_30,
        )
        .order_by("confirmation_date")[:10]
    )

    # Matches the /employees/ directory's definition of "scope" (self +
    # reports-to downline + any department/team grants), so the dashboard's
    # "total in your scope" subtext agrees with the Employees page count.
    scope_pks = visible_employee_pks(user, request=request)

    return {
        "today": today,
        "managed_count": len(managed_pks),
        "scope_count": len(scope_pks),
        "direct_reports": report_rows,
        "direct_reports_count": direct_reports_count,
        "indirect_count": len(managed_pks) - direct_reports_count,
        "leave_today_count": leave_today_count,
        "leave_week_count": leave_week_count,
        "pending_approvals": pending_approvals,
        "pending_total": pending_total,
        "confirmations_due": confirmations_due,
    }


# ---------------------------------------------------------------------------
# Org tab
# ---------------------------------------------------------------------------

def org_context(user, request=None) -> dict:
    """Org-tab snapshot. Everything here is org-wide (no per-viewer data), so
    the whole context is shared across viewers and cached for a few minutes —
    it pulls every active employee's shift/roster rows per build, which is the
    most expensive dashboard tab at headcount scale."""
    from django.conf import settings as dj_settings
    from django.core.cache import cache

    ttl = dj_settings.DASHBOARD_ORG_CACHE_SECONDS
    cache_key = f"dashboard:org_context:{date.today().isoformat()}"
    if ttl > 0:
        cached_ctx = cache.get(cache_key)
        if cached_ctx is not None:
            return cached_ctx

    ctx = _build_org_context()
    if ttl > 0:
        cache.set(cache_key, ctx, ttl)
    return ctx


def _build_org_context() -> dict:
    today = date.today()
    year = today.year
    month_start = today.replace(day=1)
    week_end = today + timedelta(days=7)

    active_qs = CustomUser.objects.filter(is_active=True)
    headcount = active_qs.aggregate(
        active=Count("pk"),
        on_probation=Count("pk", filter=Q(confirmation_date__gt=today)),
        leaving=Count("pk", filter=Q(last_working_date__isnull=False)),
    )
    total = CustomUser.objects.count()

    new_joiners_count = (
        active_qs
        .filter(joining_date__gte=month_start, joining_date__lte=today)
        .count()
    )

    confirmations_due_qs = active_qs.filter(
        confirmation_date__gte=today,
        confirmation_date__lt=month_start + timedelta(days=32),
        confirmation_date__lte=_end_of_month(today),
    ).order_by("confirmation_date")
    confirmations_due = list(confirmations_due_qs[:10])
    confirmations_due_count = confirmations_due_qs.count()

    pending_total = (
        LeaveApplication.objects
        .filter(status=LeaveApplication.Status.PENDING)
        .count()
    )

    buckets = today_status_buckets(active_qs, today=today)
    status_today = {
        "working_count": len(buckets["working_pks"]),
        "off_count": len(buckets["off_pks"]),
        "leave_count": len(buckets["leave_pks"]),
        "unscheduled_count": buckets["unscheduled_count"],
        "total": headcount["active"],
    }

    birthdays_today_count = len(_birthdays_on_dates(today, today))
    birthdays_week_count = len(_birthdays_on_dates(today, week_end))

    office = office_distribution(active_qs)
    unassigned_pks = office["other_pks"] | office["unknown_pks"]
    office_dist = {
        "mbd_count":        len(office["mbd_pks"]),
        "mhk_count":        len(office["mhk_pks"]),
        "raowa_count":      len(office["raowa_pks"]),
        "unassigned_count": len(unassigned_pks),
        "total":            headcount["active"],
    }

    balance_pressure = _balance_pressure(year)

    data_quality = _data_quality_summary(active_qs)

    return {
        "today": today,
        "year": year,
        "headcount": {**headcount, "total": total},
        "new_joiners_count": new_joiners_count,
        "confirmations_due": confirmations_due,
        "confirmations_due_count": confirmations_due_count,
        "pending_total": pending_total,
        "status_today": status_today,
        "birthdays_today_count": birthdays_today_count,
        "birthdays_week_count": birthdays_week_count,
        "office_dist": office_dist,
        "balance_pressure": balance_pressure,
        "data_quality": data_quality,
    }


def today_status_buckets(active_qs=None, today: date | None = None) -> dict:
    """Bucket every active employee into working / off / leave for today.

    Resolution order per employee mirrors `_build_week_strip`:
        1. Shift row for today  -> use shift_type.category
        2. Roster pattern for today's weekday -> use shift_type.category
        3. Neither -> 'off' bucket, also counted in `unscheduled_count`

    Category mapping: WORK -> working, LEAVE -> leave,
    OFF | HOLIDAY -> off. Guarantees the three bucket sizes sum to the
    active-employee count.
    """
    if today is None:
        today = date.today()
    if active_qs is None:
        active_qs = CustomUser.objects.filter(is_active=True)

    active_pks = set(active_qs.values_list("pk", flat=True))
    if not active_pks:
        return {
            "working_pks": set(),
            "off_pks": set(),
            "leave_pks": set(),
            "unscheduled_count": 0,
        }

    cutoffs = {
        row[0]: (row[1], row[2])
        for row in CustomUser.objects.filter(pk__in=active_pks)
        .values_list("pk", "joining_date", "last_working_date")
    }

    shift_cat = {
        s.employee_id: s.shift_type.category
        for s in (
            Shift.objects
            .filter(date=today, employee_id__in=active_pks)
            .select_related("shift_type")
        )
    }
    pattern_cat = {
        r.employee_id: r.shift_type.category
        for r in (
            Roster.objects
            .filter(weekday=today.weekday(), employee_id__in=active_pks)
            .select_related("shift_type")
        )
    }

    working_pks: set = set()
    off_pks: set = set()
    leave_pks: set = set()
    unscheduled_count = 0
    for pk in active_pks:
        category = shift_cat.get(pk)
        if category is None:
            category = pattern_cat.get(pk)
            if category is not None:
                joined, lwd = cutoffs.get(pk, (None, None))
                window = SimpleNamespace(joining_date=joined, last_working_date=lwd)
                if (
                    shift_is_before_joining_date(window, today)
                    or shift_is_after_last_working_date(window, today)
                ):
                    category = None
        if category is None:
            off_pks.add(pk)
            unscheduled_count += 1
            continue
        if category == ShiftType.Category.WORK:
            working_pks.add(pk)
        elif category == ShiftType.Category.LEAVE:
            leave_pks.add(pk)
        else:  # OFF or HOLIDAY
            off_pks.add(pk)

    return {
        "working_pks": working_pks,
        "off_pks": off_pks,
        "leave_pks": leave_pks,
        "unscheduled_count": unscheduled_count,
    }


def leave_pks_in_range(active_qs, start: date, end: date) -> set:
    """Set of employee pks on LEAVE on *any* day in [start, end] (inclusive).

    Same per-day resolution as `today_status_buckets` (concrete Shift row wins,
    else the Roster weekday pattern) applied across the range, in two queries.
    An approved leave materialises LEAVE-category Shift rows, so this also
    covers approved leave — not just manual roster edits.
    """
    active_pks = set(active_qs.values_list("pk", flat=True))
    if not active_pks:
        return set()

    cutoffs = {
        row[0]: (row[1], row[2])
        for row in CustomUser.objects.filter(pk__in=active_pks)
        .values_list("pk", "joining_date", "last_working_date")
    }

    shift_cat = {
        (s.employee_id, s.date): s.shift_type.category
        for s in (
            Shift.objects
            .filter(date__gte=start, date__lte=end, employee_id__in=active_pks)
            .select_related("shift_type")
        )
    }
    pattern_cat = {
        (r.employee_id, r.weekday): r.shift_type.category
        for r in (
            Roster.objects
            .filter(employee_id__in=active_pks)
            .select_related("shift_type")
        )
    }

    leave_pks: set = set()
    day = start
    while day <= end:
        for pk in active_pks:
            if pk in leave_pks:
                continue
            category = shift_cat.get((pk, day))
            if category is None:
                category = pattern_cat.get((pk, day.weekday()))
                if category is not None:
                    joined, lwd = cutoffs.get(pk, (None, None))
                    window = SimpleNamespace(joining_date=joined, last_working_date=lwd)
                    if (
                        shift_is_before_joining_date(window, day)
                        or shift_is_after_last_working_date(window, day)
                    ):
                        category = None
            if category == ShiftType.Category.LEAVE:
                leave_pks.add(pk)
        day += timedelta(days=1)
    return leave_pks


def office_distribution(active_qs=None) -> dict:
    """Bucket every active employee's job_location into office codes.

    Returns sets of pks per bucket so the dashboard counts and the drill-down
    list view can share the same query (mirrors `today_status_buckets`).
    Buckets: ``mbd_pks``, ``mhk_pks``, ``raowa_pks``, ``other_pks``,
    ``unknown_pks`` (no `job_location`). The five sets are disjoint and their
    union covers every active employee.
    """
    if active_qs is None:
        active_qs = CustomUser.objects.filter(is_active=True)

    buckets: dict[str, set] = {
        "mbd_pks": set(),
        "mhk_pks": set(),
        "raowa_pks": set(),
        "other_pks": set(),
        "unknown_pks": set(),
    }
    code_to_key = {
        "MBD": "mbd_pks",
        "MHK": "mhk_pks",
        "RAOWA": "raowa_pks",
        "OTHER": "other_pks",
        "UNKNOWN": "unknown_pks",
    }
    for emp in active_qs.select_related("job_location").only(
        "pk", "job_location__name", "job_location__description",
    ):
        code = office_bucket(emp.job_location)
        buckets[code_to_key[code]].add(emp.pk)
    return buckets


def _end_of_month(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def _birthdays_on_dates(start: date, end: date) -> list:
    """Active employees whose DOB month/day falls in the inclusive [start,end] window.

    Spans the year wrap (e.g. Dec 30 → Jan 5) by checking each calendar day.
    Cheap because the window is at most 7 days at MVP scale.
    """
    days = (end - start).days + 1
    if days <= 0:
        return []
    window = [start + timedelta(days=i) for i in range(days)]
    months_in_window = {d.month for d in window}
    md_keys = {d.month * 100 + d.day for d in window}
    candidates = (
        CustomUser.objects
        .filter(
            is_active=True,
            personal_info__date_of_birth__month__in=months_in_window,
        )
        .select_related("personal_info")
    )
    rows = []
    for u in candidates:
        dob = u.personal_info.date_of_birth
        if dob is None:
            continue
        key = dob.month * 100 + dob.day
        if key in md_keys:
            rows.append(u)
    rows.sort(
        key=lambda u: (u.personal_info.date_of_birth.month, u.personal_info.date_of_birth.day),
    )
    return rows[:20]


def _balance_pressure(year: int, threshold: Decimal = Decimal("2")) -> dict:
    """Count of distinct employees with `< threshold` available days in CL or SL.

    A coarse early-warning signal — managers sometimes need to know who's
    out of leave headroom before the request lands. Filters by leave-type
    name match (Casual / Sick) since policies are seeded from those names.
    """
    pressure_pks = set()
    qs = (
        LeaveBalance.objects
        .filter(
            year=year,
            leave_policy__shift_type__name__in=["Casual Leave", "Sick Leave"],
        )
        .select_related("leave_policy")
    )
    for bal in qs:
        if bal.available < threshold:
            pressure_pks.add(bal.employee_id)
    return {
        "count": len(pressure_pks),
        "threshold": threshold,
    }


def _data_quality_summary(active_qs) -> dict:
    total = active_qs.count()
    if total == 0:
        return {
            "total": 0,
            "missing_jobinfo": 0, "missing_personalinfo": 0, "missing_address": 0,
            "missing_jobinfo_pct": 0, "missing_personalinfo_pct": 0, "missing_address_pct": 0,
        }
    missing_jobinfo = active_qs.filter(job_info__isnull=True).count()
    missing_personalinfo = active_qs.filter(personal_info__isnull=True).count()
    missing_address = active_qs.annotate(addr_count=Count("addresses")).filter(addr_count=0).count()
    return {
        "total": total,
        "missing_jobinfo": missing_jobinfo,
        "missing_personalinfo": missing_personalinfo,
        "missing_address": missing_address,
        "missing_jobinfo_pct": int(round(missing_jobinfo / total * 100)),
        "missing_personalinfo_pct": int(round(missing_personalinfo / total * 100)),
        "missing_address_pct": int(round(missing_address / total * 100)),
    }
