"""Dedicated night shift windows on the roster (added 2026-11-03).

While an employee has an APPROVED `employees.DedicatedNightShiftAssignment`
covering a date, that date belongs to the department head:

- a shift change request for the date is decided by the employee's
  department head only (`accounts.permissions.can_act_on_shift_change`) —
  not Team Leaders, not APMs, not `Department.additional_managers`, not HR;
- a direct roster write (grid cell, weekly pattern, bulk upload, external
  API) may set only NIG / CUST or a non-working type (leave, off, holiday);
  any other working shift needs the department head.

When the employee *is* their department's head, their own line manager
(`JobInfo.reports_to`) stands in, since nobody approves their own.

The window is read live from the same APPROVED rows payroll classifies
night pay from (`payroll.inputs._dedicated_windows_by_employee`), never
stamped onto a request or Shift — approving or cancelling a window takes
effect at once. Spec: docs/SHIFT_CHANGE_REQUEST_PLAN.md "Dedicated night
shift windows".
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

# WORK-category ShiftType names a non-approver may still set inside a window.
NIGHT_WINDOW_ALLOWED_WORK_CODES = frozenset({"NIG", "CUST"})


def windows_by_employee(employee_ids, start: date | None = None, end: date | None = None) -> dict:
    """employee_id -> [(start_date, end_date), ...] for APPROVED windows,
    optionally only those overlapping [start, end]. One query."""
    from employees.models import DedicatedNightShiftAssignment

    ids = list(employee_ids)
    if not ids:
        return {}
    qs = DedicatedNightShiftAssignment.objects.filter(
        employee_id__in=ids, status=DedicatedNightShiftAssignment.Status.APPROVED,
    )
    if start is not None:
        qs = qs.filter(end_date__gte=start)
    if end is not None:
        qs = qs.filter(start_date__lte=end)
    result: dict = defaultdict(list)
    for employee_id, s, e in qs.values_list("employee_id", "start_date", "end_date"):
        result[employee_id].append((s, e))
    return dict(result)


def in_window_exists(employee_ref: str = "employee_id", date_ref: str = "date"):
    """`Exists(...)` expression: the outer row's (employee, date) sits in an
    APPROVED window — for annotating ShiftChangeRequest querysets."""
    from django.db.models import Exists, OuterRef

    from employees.models import DedicatedNightShiftAssignment

    return Exists(
        DedicatedNightShiftAssignment.objects.filter(
            employee_id=OuterRef(employee_ref),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
            start_date__lte=OuterRef(date_ref),
            end_date__gte=OuterRef(date_ref),
        )
    )


def date_in_windows(windows: dict, employee_id, day: date) -> bool:
    return any(s <= day <= e for s, e in windows.get(employee_id, ()))


def in_night_window(employee_id, day: date) -> bool:
    return date_in_windows(windows_by_employee([employee_id], day, day), employee_id, day)


def has_current_or_future_window(employee_id, today: date | None = None) -> bool:
    from django.utils import timezone

    today = today or timezone.localdate()
    return bool(windows_by_employee([employee_id], start=today))


def is_restricted_shift_type(shift_type) -> bool:
    """A working shift other than NIG / CUST — what a window reserves for
    the department head."""
    if shift_type is None:
        return False
    from roster.models import ShiftType

    return (
        shift_type.category == ShiftType.Category.WORK
        and shift_type.name not in NIGHT_WINDOW_ALLOWED_WORK_CODES
    )


def night_window_approver_id(employee):
    """Who decides for `employee` inside a window: the head of their
    department; for the head themselves, their line manager. None when
    neither is set (the request then waits — no HR fallback, by decision)."""
    department = getattr(employee, "department", None)
    if department is None:
        return None
    head_id = department.head_of_department_id
    if head_id is None:
        return None
    if head_id != employee.pk:
        return head_id
    job_info = getattr(employee, "job_info", None)
    return getattr(job_info, "reports_to_id", None) if job_info else None


def is_night_window_approver(user, employee) -> bool:
    if not user or not user.is_authenticated or employee is None:
        return False
    if employee.pk == user.pk:
        return False
    approver_id = night_window_approver_id(employee)
    return approver_id is not None and approver_id == user.pk


def approver_employee_ids(user) -> set:
    """Employees whose window-date decisions belong to `user`: everyone in
    the departments they head (themselves excluded), plus any department
    head whose line manager they are."""
    if not user or not user.is_authenticated:
        return set()
    from accounts.models import Department
    from django.contrib.auth import get_user_model

    User = get_user_model()
    headed = list(
        Department.objects.filter(head_of_department=user).values_list("pk", flat=True)
    )
    pks = set()
    if headed:
        pks |= set(
            User.objects.filter(department_id__in=headed)
            .exclude(pk=user.pk).values_list("pk", flat=True)
        )
    pks |= set(
        Department.objects.filter(head_of_department__job_info__reports_to=user)
        .exclude(head_of_department=user)
        .values_list("head_of_department_id", flat=True)
    )
    return pks


def may_set_shift(user, employee, day: date, shift_type, windows: dict | None = None) -> bool:
    """May `user` write `shift_type` onto `employee`'s `day`? Layered on
    top of the normal roster-edit permission, never instead of it. Pass
    `windows` (from windows_by_employee) to avoid a query per call."""
    if not is_restricted_shift_type(shift_type):
        return True
    if windows is None:
        windows = windows_by_employee([employee.pk], day, day)
    if not date_in_windows(windows, employee.pk, day):
        return True
    return is_night_window_approver(user, employee)


def blocked_reason(employee) -> str:
    return (
        # ASCII only: this rides an HX-Error-Message header, where a
        # non-ASCII character gets RFC 2047-encoded and the toast shows junk.
        f"{employee.display_name} is on a dedicated night shift window - only "
        "NIG or CUST can be set here. Any other working shift needs the "
        "department head: file a shift change request."
    )


def conflicting_shifts(assignment) -> list[dict]:
    """Working shifts other than NIG / CUST from today onward inside an
    approved window — what approving it left in place (the approve-time warning,
    option (b) of 2026-11-03). Shift rows first; a date with no row falls
    back to the weekly pattern, which the grid would materialise. Returns
    [{"date", "label", "source"}] in date order."""
    return conflicts_by_assignment([assignment]).get(assignment.pk, [])


def describe_conflicts(employee, conflicts: list[dict], limit: int = 10) -> str:
    """'Rahim has 2 non-night working shifts in this window: 10 Dec MOR,
    11 Dec MOR — they stay as rostered; ask Nadia to confirm or change them.'"""
    from django.contrib.auth import get_user_model

    parts = [f"{c['date']:%d %b} {c['label']}" for c in conflicts[:limit]]
    if len(conflicts) > limit:
        parts.append(f"and {len(conflicts) - limit} more")
    approver_id = night_window_approver_id(employee)
    approver = (
        get_user_model().objects.filter(pk=approver_id).first() if approver_id else None
    )
    who = approver.display_name if approver else "the department head"
    noun = "shift" if len(conflicts) == 1 else "shifts"
    return (
        f"{employee.display_name} has {len(conflicts)} non-night working {noun} "
        f"in this window: {', '.join(parts)} — they stay as rostered; ask {who} "
        "to confirm or change them."
    )


def conflicts_by_assignment(assignments, today: date | None = None) -> dict:
    """assignment pk -> conflicting_shifts()-shaped list, for a page of
    windows at once: two queries regardless of page size (Shift rows over
    the union span, weekly patterns for the same employees). Only APPROVED
    windows are checked — a pending one restricts nothing yet.

    Only **today onward**: days already worked are history (paid, and
    rostered before the rule existed), not something to fix. Prod windows
    started as early as 2024 and run up to ~800 days, so counting from
    `start_date` both buried the real warnings under past days and scanned
    years of Shift rows."""
    from django.utils import timezone

    from roster.models import Roster, Shift

    from employees.models import DedicatedNightShiftAssignment

    today = today or timezone.localdate()
    windows = [
        a for a in assignments
        if a.status == DedicatedNightShiftAssignment.Status.APPROVED and a.end_date >= today
    ]
    if not windows:
        return {}
    emp_ids = {a.employee_id for a in windows}
    span_start = max(today, min(a.start_date for a in windows))
    span_end = max(a.end_date for a in windows)
    shifts = {
        (s.employee_id, s.date): s for s in
        Shift.objects.filter(employee_id__in=emp_ids, date__gte=span_start, date__lte=span_end)
        .select_related("shift_type", "shift_attribute")
    }
    patterns = {
        (r.employee_id, r.weekday): r for r in
        Roster.objects.filter(employee_id__in=emp_ids).select_related("shift_type", "shift_attribute")
    }
    out = {}
    for a in windows:
        found = []
        day = max(a.start_date, today)
        while day <= a.end_date:
            shift = shifts.get((a.employee_id, day))
            source = "roster"
            if shift is None:
                shift = patterns.get((a.employee_id, day.weekday()))
                source = "weekly pattern"
            if shift is not None and is_restricted_shift_type(shift.shift_type):
                label = shift.shift_attribute.name if shift.shift_attribute_id else shift.shift_type.name
                found.append({"date": day, "label": label, "source": source})
            day += timedelta(days=1)
        if found:
            out[a.pk] = found
    return out
