""""Waiting on you" — the topbar notification bell (v1, 2026-10-16).

Six approval types: leave applications, timesheet corrections, shift change
requests, dedicated night shift assignments, and the two stages of the
overtime chain (review, then approve — docs/OVERTIME_APPROVAL_CHAIN_PLAN.md). The bell is *derived*, not
stored: every poll recomputes the pending items the viewer can act on, so an item disappears the moment anyone approves or
declines it and there is no read/unread state to drift. A stored
``Notification`` table only becomes necessary for FYI messages ("your leave
was approved") — that is a later phase, see docs/PLAN.md "Notifications".

Scope rule (``approval_inbox_pks``): the viewer's **direct reports** when they
have any, otherwise their managed scope (downline + department/team grants).
That is exactly the default of every one of those list pages' "Pending
approvals" tab (their "Direct reports only" switch), so the bell count
matches the page it links to. Org-wide visibility (HR's ``All``
tab) deliberately does not feed the bell — being able to *see* every request
is not the same as it waiting on you.

Real-time in v1 is htmx polling (``templates/partials/_notification_bell.html``)
plus an ``orbis:approvals-changed`` event after the viewer's own actions;
WebSockets are v2.
"""

from __future__ import annotations

import hashlib

from django.core.cache import cache
from django.urls import reverse
from django.utils.http import urlencode

from accounts.models import CustomUser
from django.db.models import Q

from accounts.permissions import (
    _is_overtime_global_approver,
    direct_report_pks,
    has_dedicated_night_shift_approval_scope,
    has_overtime_review_scope,
    managed_scope_pks,
    meets_dedicated_night_approve_tier,
    meets_overtime_review_tier,
    overtime_approver_department_ids,
)
from attendance.models import AttendanceCorrection
from employees.models import DedicatedNightShiftAssignment, JobInfo
from leave.models import LeaveApplication
from payroll.models import OvertimeEntry
from roster import night_window
from roster.models import ShiftChangeRequest

# HX-Trigger event name: an action view that changes what's pending for the
# viewer sends it so the bell refreshes without waiting for the next poll.
APPROVALS_CHANGED_EVENT = "orbis:approvals-changed"

# Scope is a reporting-line walk; it changes rarely, while the bell polls
# every 30s from every open tab. A new direct report appearing a minute late
# in the bell is fine — the pending items themselves are always live.
_SCOPE_CACHE_SECONDS = 60

# Rows rendered per section in the dropdown; the count is always the full one.
BELL_ITEMS_PER_TYPE = 8


def approval_inbox_pks(user, request=None) -> set:
    """Employee pks whose pending requests are "waiting on" ``user``."""
    if not user or not user.is_authenticated:
        return set()
    key = f"approvals:inbox:{user.pk}"
    pks = cache.get(key)
    if pks is None:
        pks = direct_report_pks(user, request=request) or managed_scope_pks(user, request=request)
        cache.set(key, pks, _SCOPE_CACHE_SECONDS)
    return pks


def night_shift_inbox_pks(user, request=None) -> set:
    """The "Direct reports only" scope for dedicated night shifts, which can
    only be approved at Manager tier (can_approve_dedicated_night_shift).

    A plain direct-reports rule would strand requests: in APM → TL → agent
    the TL can't approve the agent's night shift, so it would be hidden from
    everyone by default. Instead an employee is in ``user``'s inbox when
    ``user`` is the **nearest Manager-tier person up their reporting chain**:
    the direct reports, plus anyone whose managers in between are below
    Manager tier. If nobody in the chain qualifies (the chain ends or loops
    first), the request falls to everyone whose scope covers that employee.
    A viewer with no direct reports gets their whole managed scope, as on
    the other lists.
    """
    if not user or not user.is_authenticated:
        return set()
    key = f"approvals:night_inbox:{user.pk}"
    pks = cache.get(key)
    if pks is None:
        pks = _compute_nearest_tier_inbox(user, meets_dedicated_night_approve_tier, request)
        cache.set(key, pks, _SCOPE_CACHE_SECONDS)
    return pks


def overtime_review_inbox_pks(user, request=None) -> set:
    """Employees whose PENDING overtime is waiting on ``user`` to review —
    the same nearest-up-the-chain walk as night shifts, at the overtime
    review tier (Manager, i.e. APM/PM and above). A PM above the APM may
    still review (can_review_overtime), but isn't nagged while the APM is
    there. Separated employees included: a leaver's final-period overtime
    still needs the chain."""
    if not user or not user.is_authenticated:
        return set()
    key = f"approvals:ot_review_inbox:{user.pk}"
    pks = cache.get(key)
    if pks is None:
        pks = _compute_nearest_tier_inbox(
            user, meets_overtime_review_tier, request, include_separated=True,
        )
        cache.set(key, pks, _SCOPE_CACHE_SECONDS)
    return pks


def overtime_approve_inbox_pks(user, request=None) -> set:
    """Employees whose unapproved overtime is waiting on ``user`` as a
    department head or additional manager. HR-Payroll and Payroll Admins
    can approve anything (can_approve_overtime) but are the fallback, not
    the owner, so company-wide pending overtime deliberately does not land
    in their bell — org-wide visibility isn't "waiting on you"."""
    if not user or not user.is_authenticated:
        return set()
    dept_ids = overtime_approver_department_ids(user, request=request)
    if not dept_ids:
        return set()
    return set(
        CustomUser.objects.filter(department_id__in=dept_ids)
        .exclude(pk=user.pk).values_list("pk", flat=True)
    )


def overtime_awaiting_q(user, request=None) -> Q:
    """Filter for the overtime list's ``?awaiting=me``: exactly the rows the
    bell counts for this viewer, plus — for the global approvers, who have
    no bell section — everything still unapproved."""
    if _is_overtime_global_approver(user, request):
        return Q(status__in=OvertimeEntry.UNAPPROVED_STATUSES) & ~Q(employee=user)
    return (
        Q(status=OvertimeEntry.Status.PENDING,
          employee_id__in=overtime_review_inbox_pks(user, request=request))
        | Q(status__in=OvertimeEntry.UNAPPROVED_STATUSES,
            employee_id__in=overtime_approve_inbox_pks(user, request=request))
    )


def _compute_nearest_tier_inbox(user, tier_check, request=None, include_separated=False) -> set:
    """Employees in ``user``'s scope for whom ``user`` is the nearest person
    up the reporting chain passing ``tier_check`` — see night_shift_inbox_pks
    for the reasoning. ``tier_check`` takes a CustomUser."""
    scope = managed_scope_pks(user, request=request, include_separated=include_separated)
    if not direct_report_pks(user, request=request):
        return scope
    parent = dict(
        JobInfo.objects
        .filter(employee__is_active=True, reports_to__isnull=False)
        .values_list("employee_id", "reports_to_id")
    )

    def chain(pk):
        # Managers above `pk`, nearest first; stops at the viewer or a loop.
        seen = set()
        cur = parent.get(pk)
        while cur is not None and cur not in seen:
            yield cur
            if cur == user.pk:
                return
            seen.add(cur)
            cur = parent.get(cur)

    between = {a for pk in scope for a in chain(pk)} - {user.pk}
    approvers = {
        u.pk for u in CustomUser.objects
        .filter(pk__in=between)
        .select_related("designation__access_level", "access_level")
        if tier_check(u)
    }
    inbox = set()
    for pk in scope:
        for manager in chain(pk):
            if manager == user.pk:
                inbox.add(pk)
                break
            if manager in approvers:
                break  # someone nearer can approve it
        else:
            inbox.add(pk)  # no approver in the chain at all
    return inbox


def pending_leave_qs(user, request=None):
    return LeaveApplication.objects.filter(
        employee_id__in=approval_inbox_pks(user, request=request),
        status=LeaveApplication.Status.PENDING,
    ).exclude(employee=user)


def pending_attendance_correction_qs(user, request=None):
    return AttendanceCorrection.objects.filter(
        employee_id__in=approval_inbox_pks(user, request=request),
        status=AttendanceCorrection.Status.PENDING,
    ).exclude(employee=user)


def night_window_approver_pks(user, request=None) -> set:
    """Employees whose dedicated-night-window shift changes wait on ``user``
    as their department head (or, for a head, as their line manager) —
    roster/night_window.py. Cached like the other inbox scopes."""
    if not user or not user.is_authenticated:
        return set()
    key = f"approvals:night_window_approver:{user.pk}"
    pks = cache.get(key)
    if pks is None:
        pks = night_window.approver_employee_ids(user)
        cache.set(key, pks, _SCOPE_CACHE_SECONDS)
    return pks


def pending_shift_change_qs(user, request=None):
    """The usual inbox for dates outside a dedicated night shift window;
    inside one, only the department head's (2026-11-03) — so a Team
    Leader's bell never carries a request they can't decide, and the head
    sees their whole department's, not just direct reports'."""
    return (
        ShiftChangeRequest.objects
        .filter(status=ShiftChangeRequest.Status.PENDING)
        .annotate(_in_night_window=night_window.in_window_exists())
        .filter(
            Q(_in_night_window=False, employee_id__in=approval_inbox_pks(user, request=request))
            | Q(_in_night_window=True, employee_id__in=night_window_approver_pks(user, request=request))
        )
        .exclude(employee=user)
    )


def pending_dedicated_night_shift_qs(user, request=None):
    """Only meaningful for a Manager-tier viewer — BELL_TYPES gates it on
    has_dedicated_night_shift_approval_scope, same as the list's Pending tab."""
    return DedicatedNightShiftAssignment.objects.filter(
        employee_id__in=night_shift_inbox_pks(user, request=request),
        status=DedicatedNightShiftAssignment.Status.PENDING,
    ).exclude(employee=user)


def pending_overtime_review_qs(user, request=None):
    return OvertimeEntry.objects.filter(
        employee_id__in=overtime_review_inbox_pks(user, request=request),
        status=OvertimeEntry.Status.PENDING,
    ).exclude(employee=user)


def pending_overtime_approve_qs(user, request=None):
    return OvertimeEntry.objects.filter(
        employee_id__in=overtime_approve_inbox_pks(user, request=request),
        status__in=OvertimeEntry.UNAPPROVED_STATUSES,
    ).exclude(employee=user)


def _has_overtime_approve_inbox(user, request=None) -> bool:
    return bool(overtime_approver_department_ids(user, request=request))


def _day_range(start, end) -> str:
    if start == end:
        return start.strftime("%d %b")
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b')}"


def _leave_item(app: LeaveApplication) -> dict:
    return {
        "employee": app.employee,
        "summary": f"{app.leave_type.name} · {_day_range(app.start_date, app.end_date)}",
        "submitted_at": app.applied_at,
        "url": reverse("leave_detail", args=[app.pk]),
    }


def _correction_item(c: AttendanceCorrection) -> dict:
    day = c.date.isoformat()
    query = {"tab": "pending", "date_from": day, "date_to": day}
    if c.employee.employee_id:
        query["q"] = c.employee.employee_id
    return {
        "employee": c.employee,
        "summary": f"Timesheet · {c.date.strftime('%d %b')}",
        "submitted_at": c.requested_at,
        "url": f"{reverse('attendance_correction_list')}?{urlencode(query)}",
    }


def _shift_change_item(r: ShiftChangeRequest) -> dict:
    return {
        "employee": r.employee,
        "summary": f"Shift change · {r.date.strftime('%d %b')} → {r.requested_shift_type.name} ({r.requested_location})",
        "submitted_at": r.applied_at,
        "url": reverse("shift_change_detail", args=[r.pk]),
    }


def _night_shift_item(a: DedicatedNightShiftAssignment) -> dict:
    return {
        "employee": a.employee,
        "summary": f"Dedicated night shift · {_day_range(a.start_date, a.end_date)}",
        "submitted_at": a.added_at,
        "url": f"{reverse('dedicated_night_shift_list')}?tab=pending",
    }


def _overtime_item(e: OvertimeEntry) -> dict:
    hours, rem = divmod(e.seconds, 3600)
    return {
        "employee": e.employee,
        "summary": f"Overtime · {e.work_date.isoformat()} · {hours}:{rem // 60:02d} {e.overtime_type.name}",
        "submitted_at": e.added_at,
        "url": f"{reverse('payroll_overtime_list')}?awaiting=me",
    }


def _always(user, request=None) -> bool:
    return True


# One entry per approval type in the bell. A new type needs its list page's
# Pending tab to apply the same scope rule (the "Direct reports only"
# switch), or the bell and the page will disagree. ``eligible`` hides a type
# from a viewer who could never approve it even within their scope.
BELL_TYPES = [
    {
        "key": "leave",
        "label": "Leave applications",
        "icon": "plane",
        "pending_qs": pending_leave_qs,
        "select_related": ("employee", "leave_type"),
        "order_by": ("-applied_at",),
        "item": _leave_item,
        "list_url_name": "leave_list",
    },
    {
        "key": "timesheet",
        "label": "Timesheet corrections",
        "icon": "clock",
        "pending_qs": pending_attendance_correction_qs,
        "select_related": ("employee",),
        "order_by": ("-requested_at",),
        "item": _correction_item,
        "list_url_name": "attendance_correction_list",
    },
    {
        "key": "shift_change",
        "label": "Shift change requests",
        "icon": "refresh-cw",
        "pending_qs": pending_shift_change_qs,
        "select_related": ("employee", "requested_shift_type"),
        "order_by": ("-applied_at",),
        "item": _shift_change_item,
        "list_url_name": "shift_change_list",
    },
    {
        "key": "night_shift",
        "label": "Dedicated night shift",
        "icon": "moon",
        "pending_qs": pending_dedicated_night_shift_qs,
        "select_related": ("employee",),
        "order_by": ("-added_at",),
        "item": _night_shift_item,
        "list_url_name": "dedicated_night_shift_list",
        "eligible": has_dedicated_night_shift_approval_scope,
    },
    # The overtime chain's two stages are two sections because they wait on
    # different people (the nearest APM/PM, then the department head), and
    # one person may hold both. Both link to the list's ?awaiting=me filter,
    # which applies overtime_awaiting_q — the same sets as these counts.
    {
        "key": "overtime_review",
        "label": "Overtime to review",
        "icon": "timer",
        "pending_qs": pending_overtime_review_qs,
        "select_related": ("employee", "overtime_type"),
        "order_by": ("-added_at",),
        "item": _overtime_item,
        "list_url_name": "payroll_overtime_list",
        "list_query": "awaiting=me",
        "eligible": has_overtime_review_scope,
    },
    {
        "key": "overtime_approve",
        "label": "Overtime to approve",
        "icon": "timer",
        "pending_qs": pending_overtime_approve_qs,
        "select_related": ("employee", "overtime_type"),
        "order_by": ("-added_at",),
        "item": _overtime_item,
        "list_url_name": "payroll_overtime_list",
        "list_query": "awaiting=me",
        "eligible": _has_overtime_approve_inbox,
    },
]


def bell_summary(user, request=None) -> dict:
    """Everything the bell renders: per-type sections, the total, and a
    ``signature`` of the exact pending set (the client compares it across
    tabs to know when to refresh the others)."""
    sections = []
    keys: list[str] = []
    if approval_inbox_pks(user, request=request):
        for spec in BELL_TYPES:
            if not spec.get("eligible", _always)(user, request=request):
                continue
            qs = spec["pending_qs"](user, request=request)
            pks = list(qs.values_list("pk", flat=True))
            if not pks:
                continue
            keys.extend(f"{spec['key']}:{pk}" for pk in pks)
            rows = (
                qs.select_related(*spec["select_related"])
                .order_by(*spec["order_by"])[:BELL_ITEMS_PER_TYPE]
            )
            sections.append({
                "key": spec["key"],
                "label": spec["label"],
                "icon": spec["icon"],
                "count": len(pks),
                "items": [spec["item"](row) for row in rows],
                "more": max(len(pks) - BELL_ITEMS_PER_TYPE, 0),
                "url": f"{reverse(spec['list_url_name'])}?{spec.get('list_query', 'tab=pending')}",
            })
    signature = hashlib.sha1(
        f"{user.pk}|{'|'.join(sorted(keys))}".encode()
    ).hexdigest()[:16]
    return {
        "sections": sections,
        "total": len(keys),
        "signature": signature,
    }
