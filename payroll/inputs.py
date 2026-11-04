"""Per-source input collectors for the calculation engine (payroll/engine.py).

One query per input source, bucketed into a dict by employee pk — mirrors
payroll/reports.py::build_payroll_report's query shape, NOT
build_shift_summary's (which raises CrossYearSummaryError on a 16 Dec-15 Jan
period). LWP/half-day resolution joins leave.LeavePolicy.is_paid — never
guesses "is this shift type unpaid" from its name (PAYROLL_PLAN.md Risk 2).

Night-shift detection is derived from actual shift times, not a stored flag:
a shift crosses midnight (end_time < start_time) iff it's a night shift —
verified against real roster data (NIG/CUST both cross midnight; EVE/MOR
never do). See docs/PAYROLL_PLAN.md's night-shift-detection note for why
this replaced a ShiftType.is_night_shift boolean that shipped in Phase 2 and
was never actually populated.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from leave.models import LeavePolicy
from roster.models import Shift, ShiftType


def leave_policy_paid_map() -> dict[int, bool]:
    """shift_type_id -> is_paid, for every seeded LeavePolicy. One query."""
    return {lp.shift_type_id: lp.is_paid for lp in LeavePolicy.objects.all()}


def hdl_shift_type_id() -> int | None:
    """The canonical Half Day Leave ShiftType's pk, or None if not seeded."""
    row = ShiftType.objects.filter(name="HDL").only("id").first()
    return row.pk if row else None


def effective_shift_times(shift):
    """(start_time, end_time) for a Shift: its specific ShiftAttribute's
    times if one is assigned, else the parent ShiftType's own times."""
    if shift.shift_attribute_id and shift.shift_attribute.start_time and shift.shift_attribute.end_time:
        return shift.shift_attribute.start_time, shift.shift_attribute.end_time
    return shift.shift_type.shift_start_time, shift.shift_type.shift_end_time


def is_night_shift(start_time, end_time) -> bool:
    return bool(start_time and end_time and end_time < start_time)


def collect_lwp_days_by_date(period, employee_ids) -> dict[str, dict]:
    """employee_id -> {date: unpaid-leave weight} for the period. HDL days
    count 0.5; every other unpaid LEAVE-category type (LWP, and any future
    one) counts 1.0. A LEAVE-category shift type with no LeavePolicy row at
    all (e.g. ABS) is treated as paid by design (PAYROLL_PROCESS.md §12 Q16,
    resolved) and simply doesn't appear here.

    Per-date rather than one total per employee because the engine prorates
    **per salary segment** (payroll/engine.py stage 3): an employee whose
    gross changed mid-period must have each unpaid day charged against the
    segment it actually fell in, or a day of LWP in the cheaper half is
    priced at the dearer half's rate. Same single query as the old
    employee-total version — this adds ``date`` to the ``.values()`` call,
    not a second round trip.
    """
    paid_map = leave_policy_paid_map()
    hdl_id = hdl_shift_type_id()
    result: dict[str, dict] = defaultdict(dict)
    rows = (
        Shift.objects
        .filter(
            employee_id__in=employee_ids, date__gte=period.start_date, date__lte=period.end_date,
            shift_type__category=ShiftType.Category.LEAVE,
        )
        .values("employee_id", "shift_type_id", "date")
    )
    for row in rows:
        shift_type_id = row["shift_type_id"]
        if paid_map.get(shift_type_id, True):
            continue
        weight = Decimal("0.5") if shift_type_id == hdl_id else Decimal("1")
        by_date = result[row["employee_id"]]
        by_date[row["date"]] = by_date.get(row["date"], Decimal("0")) + weight
    return dict(result)


def collect_lwp_days(period, employee_ids) -> dict[str, Decimal]:
    """employee_id -> total unpaid-leave days for the period.

    A sum over ``collect_lwp_days_by_date`` — kept as the whole-period figure
    that ``PayrollRunEmployee.lwp_days`` and the reports still want, so the
    two can never disagree about which shift types count as unpaid.
    """
    return {
        employee_id: sum(by_date.values(), Decimal("0"))
        for employee_id, by_date in collect_lwp_days_by_date(period, employee_ids).items()
    }


NIGHT_REGULAR = "REGULAR"
NIGHT_CUSTOM = "CUSTOM"
NIGHT_DEDICATED = "DEDICATED"
# Night shifts worked before the employee's confirmation in a department
# that pays no night allowance until then (Digital Advertising) — counted,
# shown as "Trainee nights (unpaid)", never paid. See
# `night_allowance_unpaid_before_confirmation`.
NIGHT_PRE_CONFIRMATION = "PRE_CONFIRMATION"

NIGHT_RATE_TYPES = ("NIGHT_REGULAR", "NIGHT_CUSTOM", "NIGHT_QUARTERLY")
TRAINEE_STATUS = "TRAINEE"


def confirmed_from(employee) -> date | None:
    """The date `employee` is confirmed from, for the night allowance:
    `confirmation_date` when HR has set it, else `joining_date` +
    `probation_month` months when both are known, else None (unknown).
    Decided with Digital Advertising 2026-11-03 — HR is filling in every
    confirmation date before the next payroll, so the fallbacks are a
    bridge, not the rule."""
    if employee.confirmation_date:
        return employee.confirmation_date
    if employee.joining_date and employee.probation_month:
        from dateutil.relativedelta import relativedelta

        return employee.joining_date + relativedelta(months=employee.probation_month)
    return None


def night_allowance_unpaid_before_confirmation(on_date) -> tuple[bool, set]:
    """Which departments pay no night allowance before confirmation:
    (applies_to_every_department, department_ids).

    Read from the data, not a hard-coded department name: a department
    whose active night-rate PayRate rows include a `resource_status=TRAINEE`
    row at 0.00 (Digital Advertising's 2026-09-07 carve-out) is one. Those
    rows now mean "the rate before confirmation" — classify_night_shifts
    keeps pre-confirmation nights out of the paid counts entirely, and the
    engine no longer resolves night rates with TRAINEE, so a confirmed
    employee still marked Trainee is paid from their confirmation date."""
    from django.db.models import Q

    from payroll.models import PayRate

    rows = (
        PayRate.objects
        .filter(
            rate_type__in=NIGHT_RATE_TYPES, is_active=True, resource_status=TRAINEE_STATUS,
            amount=0, effective_from__lte=on_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .values_list("department_id", flat=True)
    )
    dept_ids = set(rows)
    return (None in dept_ids, dept_ids - {None})


def _dedicated_windows_by_employee(employee_ids) -> dict[str, list[tuple]]:
    """employee_id -> list of (start_date, end_date) for their APPROVED
    Dedicated Night Shift assignments. One query, read once per classification
    call — not per shift-day."""
    from employees.models import DedicatedNightShiftAssignment

    result: dict[str, list[tuple]] = defaultdict(list)
    rows = (
        DedicatedNightShiftAssignment.objects
        .filter(employee_id__in=employee_ids, status=DedicatedNightShiftAssignment.Status.APPROVED)
        .values_list("employee_id", "start_date", "end_date")
    )
    for employee_id, start_date, end_date in rows:
        result[employee_id].append((start_date, end_date))
    return dict(result)


def _night_relevant_shift_q():
    """A Q matching exactly the WORK shifts step 1 of `classify_night_shifts`
    keeps — CUST, or crossing midnight under `effective_shift_times` — so the
    database drops the daytime rows (about two-thirds of a period's WORK
    shifts) instead of Python hydrating each one to look at its times.

    Resolved from the two small lookup tables (two queries) rather than
    stored flags, so it stays derived from real times like the rest of this
    module. Mirrors `effective_shift_times` exactly: an attribute with both
    times set decides on its own times; a missing or half-timed attribute
    falls back to the Shift's own shift type. The per-row checks in the
    loop are kept, so this filter can only ever narrow, never misclassify.
    """
    from django.db.models import Q

    from roster.models import ShiftAttribute

    cust_type_ids, crossing_type_ids = [], []
    for pk, name, start, end in ShiftType.objects.values_list(
        "pk", "name", "shift_start_time", "shift_end_time",
    ):
        if name == "CUST":
            cust_type_ids.append(pk)
        elif is_night_shift(start, end):
            crossing_type_ids.append(pk)

    timed_attr_ids, crossing_attr_ids = [], []
    for pk, start, end in ShiftAttribute.objects.values_list("pk", "start_time", "end_time"):
        if start and end:
            timed_attr_ids.append(pk)
            if is_night_shift(start, end):
                crossing_attr_ids.append(pk)

    return (
        Q(shift_type_id__in=cust_type_ids)
        | Q(shift_attribute_id__in=crossing_attr_ids)
        | (Q(shift_type_id__in=crossing_type_ids) & ~Q(shift_attribute_id__in=timed_attr_ids))
    )


def _unpaid_until_confirmed(period, employee_ids) -> dict:
    """employee_id -> confirmation cut-off, for employees in a department
    that pays no night allowance before confirmation; everyone else is
    absent (all their nights count as before). The value is the date nights
    become paid, or — when no date is known — True (treated as confirmed)
    unless they're currently Trainee, then False (nothing paid this period).
    Two queries: the rate rows, then the employees."""
    from accounts.models import CustomUser

    every_dept, dept_ids = night_allowance_unpaid_before_confirmation(period.end_date)
    if not every_dept and not dept_ids:
        return {}
    qs = CustomUser.objects.filter(pk__in=employee_ids)
    if not every_dept:
        qs = qs.filter(department_id__in=dept_ids)
    out = {}
    for emp in qs.select_related("job_info").only(
        "pk", "confirmation_date", "joining_date", "probation_month", "job_info__resource_status",
    ):
        cutoff = confirmed_from(emp)
        if cutoff is not None:
            out[emp.pk] = cutoff
        else:
            job_info = getattr(emp, "job_info", None)
            out[emp.pk] = not (job_info and job_info.resource_status == TRAINEE_STATUS)
    return out


def classify_night_shifts(period, employee_ids) -> dict[str, dict]:
    """employee_id -> {"REGULAR": n, "CUSTOM": n, "DEDICATED": n, "TRANSPORT": n}.

    The single classification shared by the calculation engine (Stage 4,
    payroll/engine.py) and the pre-payroll cross-check report
    (payroll/reports.py::build_payroll_report) — see docs/PAYROLL_PLAN.md's
    2026-09-07 dated section for why these must not be two independent
    implementations of the same rule.

    Per WORK-category shift-day, in order:
      1. Not a CUST shift and doesn't cross midnight -> not night-pay-relevant, skipped.
      2. An APPROVED DedicatedNightShiftAssignment covers the shift's date -> DEDICATED
         (wins over shift type, confirmed 2026-09-07 — a dedicated employee's CUST
         shift during their window still counts as DEDICATED, not CUSTOM).
      3. Else the shift type is CUST -> CUSTOM.
      4. Else (an ordinary night-crossing shift, e.g. NIG) -> REGULAR.
    TRANSPORT counts CUST shift-days where the resolved work location isn't WFH
    (roster._LocatedRosterCell.location_badge), independent of the night-duty
    classification above.

    Before confirmation (2026-11-03, replacing the 2026-09-07 rule that
    keyed this on the Trainee status at calculation time): in a department
    that pays no night allowance until confirmation
    (`night_allowance_unpaid_before_confirmation`), a night shift dated
    before the employee's `confirmed_from` date goes to PRE_CONFIRMATION
    instead of steps 2–4 — per day, so a confirmation mid-period pays from
    that date. TRANSPORT still counts.
    """
    def _empty():
        return {
            NIGHT_REGULAR: 0, NIGHT_CUSTOM: 0, NIGHT_DEDICATED: 0,
            NIGHT_PRE_CONFIRMATION: 0, "TRANSPORT": 0,
        }

    result: dict[str, dict] = defaultdict(_empty)
    dedicated_windows = _dedicated_windows_by_employee(employee_ids)
    unpaid_until_confirmed = _unpaid_until_confirmed(period, employee_ids)
    rows = (
        Shift.objects
        .filter(
            employee_id__in=employee_ids, date__gte=period.start_date, date__lte=period.end_date,
            shift_type__category=ShiftType.Category.WORK,
        )
        .filter(_night_relevant_shift_q())
        .select_related(
            "shift_type", "shift_attribute",
            "work_location", "job_location", "employee__work_location", "employee__job_location",
        )
    )
    for shift in rows:
        is_cust = shift.shift_type.name == "CUST"
        start, end = effective_shift_times(shift)
        crosses_midnight = is_night_shift(start, end)
        if not is_cust and not crosses_midnight:
            continue

        counts = result[shift.employee_id]

        if is_cust and shift.location_badge != "WFH":
            counts["TRANSPORT"] += 1  # transport is paid before confirmation too

        # Before confirmation, in a department that pays no night allowance
        # until then: counted for Support Info, never paid.
        if shift.employee_id in unpaid_until_confirmed:
            confirmed = unpaid_until_confirmed[shift.employee_id]
            if confirmed is False or (confirmed is not True and shift.date < confirmed):
                counts[NIGHT_PRE_CONFIRMATION] += 1
                continue

        dedicated = any(
            window_start <= shift.date <= window_end
            for window_start, window_end in dedicated_windows.get(shift.employee_id, [])
        )
        if dedicated:
            counts[NIGHT_DEDICATED] += 1
        elif is_cust:
            counts[NIGHT_CUSTOM] += 1
        elif crosses_midnight:
            counts[NIGHT_REGULAR] += 1

    return dict(result)


def collect_overtime(period_like, employee_ids) -> dict[str, dict]:
    """employee_id -> {(overtime_type_id, worked_in_id): {"seconds": total
    approved seconds, "days": distinct work-dates with an approved entry}}
    for the window.

    Broken down by (overtime_type, worked_in) because payroll.PayRate
    (rate_type=OVERTIME) rows are themselves scoped per overtime_type AND,
    since 2026-09-16, per team (a department can carve out a team-specific
    OT rate — see docs/PAYROLL_PLAN.md) — so two entries of the same type
    logged against different teams in the same period must resolve
    separately rather than being blended into one rate lookup.
    `worked_in_id` is None for an entry with no team on file (the employee
    has none, or it predates the field and wasn't backfilled), which
    resolve_rate treats the same as "no team scope requested" today.
    `period_like` only needs `.start_date`/`.end_date` — a real
    PayrollPeriod (the engine) or a payroll.reports.PayrollFilters (the
    report) both work, since OvertimeEntry.work_date is filtered by range
    rather than by the `period` FK (the report's date range isn't
    necessarily a real PayrollPeriod row). "seconds" feeds a PER_HOUR rate,
    "days" a PER_OCCURRENCE (flat-per-day) rate — shared by the engine's OT
    stage and build_payroll_report's OT column so both read the same input
    the same way.
    """
    from django.db.models import Count, Sum

    from payroll.models import OvertimeEntry

    rows = (
        OvertimeEntry.objects
        .filter(
            employee_id__in=employee_ids, status=OvertimeEntry.Status.APPROVED,
            work_date__gte=period_like.start_date, work_date__lte=period_like.end_date,
        )
        .values("employee_id", "overtime_type_id", "worked_in_id")
        .annotate(seconds=Sum("seconds"), days=Count("work_date", distinct=True))
    )
    result: dict[str, dict] = defaultdict(dict)
    for row in rows:
        key = (row["overtime_type_id"], row["worked_in_id"])
        result[row["employee_id"]][key] = {
            "seconds": row["seconds"] or 0, "days": row["days"] or 0,
        }
    return dict(result)


def collect_meal_counts(period, employee_ids) -> dict[str, int]:
    """employee_id (pk) -> total_meals, from MealCount rows already pulled for
    this period (payroll/meals.py::pull_meal_counts) — never a live API call
    from here. payroll.services.lock_inputs guarantees a period has been
    pulled (or blocks the lock) before the engine can run against it, so a
    missing MealCount row here just means genuinely zero meals, same as the
    API's own "missing key == 0" contract.
    """
    from payroll.models import MealCount

    return dict(
        MealCount.objects.filter(period=period, employee_id__in=employee_ids)
        .values_list("employee_id", "total_meals")
    )


def eligible_days(employee, period) -> int:
    """Days employed within the period — joining date/period start,
    whichever is later, to last working day/period end, whichever is
    earlier."""
    start = max(period.start_date, employee.joining_date or period.start_date)
    end = min(period.end_date, employee.last_working_date or period.end_date)
    return max(0, (end - start).days + 1)
