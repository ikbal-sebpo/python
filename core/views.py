from datetime import date, datetime, timedelta

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from accounts.models import CustomUser
from accounts.permissions import (
    can_see_all,
    get_reportee_user_ids,
    has_managed_scope,
    managed_scope_pks,
)
from core import credits
from core import dashboard as dash
from leave.models import LeaveApplication


SHIFT_BUCKETS = ("working", "off", "leave")

# How recent the last TimeLy punch must be for us to still consider the
# employee "in" (vs "Left ..."). Working heuristic — TimeLy's response doesn't
# tell us punch direction, so we infer from recency.
_PRESENCE_LEFT_THRESHOLD = timedelta(hours=1)


def _format_presence(record: dict | None) -> str:
    """Render a TimeLy presence record into a one-line label for the dashboard.

    Matrix:
        - record is None                                → "Pending"
        - last_punch_at older than 1h ago + first_in    → "<office> · HH:MM → HH:MM"
        - last_punch_at older than 1h ago, no first_in  → "HH:MM"
        - first_punch_at parses (still in)              → "In <office> · HH:MM"
        - anything else                                 → "—"
    """
    if record is None:
        return "Pending"

    first_in = _parse_punch(record.get("first_punch_at"))
    last_seen = _parse_punch(record.get("last_punch_at"))
    office = _format_office(record.get("office"))
    now = timezone.localtime().replace(tzinfo=None)

    if last_seen is not None and now - last_seen > _PRESENCE_LEFT_THRESHOLD:
        if first_in is not None:
            return (
                f"{office} · "
                f"{first_in.strftime('%H:%M')} → {last_seen.strftime('%H:%M')}"
            )
        return last_seen.strftime("%H:%M")

    if first_in is not None:
        return f"In {office} · {first_in.strftime('%H:%M')}"

    return "—"


def _parse_punch(value):
    """Parse a TimeLy punch timestamp (naive ISO) into a naive datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_office(office_field) -> str:
    """Pick a display office code from TimeLy's office array.

    Filters out non-physical labels like "REMOTE" (case-insensitive). Returns
    the first remaining code uppercased and trimmed to 6 chars. Falls back to
    the original first element if the filter eats everything, or "—" if the
    list is empty / not a list.
    """
    if not isinstance(office_field, list) or not office_field:
        return "—"
    physical = [str(o) for o in office_field if str(o).strip().lower() != "remote"]
    pick = (physical[0] if physical else str(office_field[0])).strip().upper()
    return (pick[:6] or "—")

_BUCKET_LABELS = {
    "working":            "On working shift",
    "off":                "Day off",
    "leave":              "On leave",
    "new_joiners":        "New joiners this month",
    "birthdays_week":     "Birthdays this week",
    "confirmations_due":  "Confirmations due this month",
    "office_mbd":         "MBD office",
    "office_mhk":         "MHK office",
    "office_raowa":       "RAOWA office",
    "office_unassigned":  "Unassigned office",
}


def dashboard(request):
    user = request.user
    tabs = dash.visible_tabs(user, request=request)
    requested = request.GET.get("tab")
    if requested in tabs:
        active = requested
    else:
        active = dash.default_tab(user, request=request)

    ctx = {
        "page_title": "Dashboard",
        "tabs": tabs,
        "active_tab": active,
    }
    if active == "me":
        ctx["me"] = dash.me_context(user, request=request)
    elif active == "team":
        ctx["team"] = dash.team_context(user, request=request)
    elif active == "org":
        ctx["org"] = dash.org_context(user, request=request)

    return render(request, "dashboard.html", ctx)


def project_credits(request):
    return render(request, "core/credits.html", {
        "page_title": "Project Credits",
        "areas": credits.CREDIT_AREAS,
        "special_thanks": credits.SPECIAL_THANKS,
    })


def team_downline(request, pk):
    """Modal body: everyone in a direct report's recursive team.

    Viewer must manage the target (or have org-wide read scope).
    """
    target = get_object_or_404(CustomUser, pk=pk, is_active=True)
    if not (
        can_see_all(request.user, request=request)
        or target.pk in get_reportee_user_ids(request.user, request=request)
    ):
        raise PermissionDenied("Not in your managed scope.")

    member_pks = get_reportee_user_ids(target)
    members = (
        CustomUser.objects
        .filter(pk__in=member_pks, is_active=True)
        .select_related("designation", "department", "job_info__reports_to")
        .order_by("first_name", "last_name")
    )
    return render(request, "dashboard/_team_downline.html", {
        "target": target,
        "members": members,
    })


def team_reports(request):
    """Full-page recursive drill-down through a manager's direct reports.

    Rooted at the viewer (no ``?root=``) or at any person the viewer manages.
    Each report who manages others links one level deeper; leaf people are
    inert. A breadcrumb trail walks the reports-to chain back up to the viewer.
    """
    root = request.GET.get("root")
    if root:
        target = get_object_or_404(CustomUser, pk=root, is_active=True)
    else:
        target = request.user

    if not (
        target.pk == request.user.pk
        or can_see_all(request.user, request=request)
        or target.pk in get_reportee_user_ids(request.user, request=request)
    ):
        raise PermissionDenied("Not in your managed scope.")

    rows = dash.direct_report_rows(target, request=request)

    # Breadcrumb: walk reports-to up from target to the viewer (self first).
    crumbs = []
    node = target
    seen = {node.pk}
    for _ in range(10):
        if node.pk == request.user.pk:
            break
        crumbs.insert(0, node)
        manager = getattr(getattr(node, "job_info", None), "reports_to", None)
        if manager is None or manager.pk in seen:
            break
        seen.add(manager.pk)
        node = manager

    return render(request, "dashboard/team_reports.html", {
        "page_title": "Direct reports",
        "target": target,
        "is_self": target.pk == request.user.pk,
        "rows": rows,
        "crumbs": crumbs,
        "today": date.today(),
    })


# Buckets a manager-scoped (``scope=team``) drill-down may show.
_TEAM_BUCKETS = ("off", "leave", "confirmations_due")
# Buckets that render as a tab group on the team "Out today" page.
_TEAM_OUT_BUCKETS = ("leave", "off")


def dashboard_status_today(request):
    """Drill-down list for a dashboard card.

    ``scope=org`` (default) is gated by ``can_see_all`` and covers the whole
    active org — the Org-tab cards. ``scope=team`` is gated by
    ``has_managed_scope`` and is clamped to the viewer's recursive reportees —
    the Team-tab "Out today" (off/leave tabs) and "Confirmations due" cards.

    Org handles ``working``/``off``/``leave``, ``new_joiners``/``birthdays_week``,
    and the ``office_*`` buckets; team is restricted to off/leave/confirmations.
    """
    scope = "team" if request.GET.get("scope") == "team" else "org"

    if scope == "team":
        if not has_managed_scope(request.user, request=request):
            return HttpResponseForbidden("No managed team.")
        managed_pks = managed_scope_pks(request.user, request=request)
        active_qs = CustomUser.objects.filter(is_active=True, pk__in=managed_pks)
        default_bucket = "leave"
        allowed = _TEAM_BUCKETS
    else:
        if not can_see_all(request.user, request=request):
            return HttpResponseForbidden("Org-wide view not permitted.")
        active_qs = CustomUser.objects.filter(is_active=True)
        default_bucket = "working"
        allowed = _BUCKET_LABELS

    bucket = request.GET.get("bucket", default_bucket)
    if bucket not in allowed:
        bucket = default_bucket

    today = date.today()

    employees, header_subtitle, right_meta_by_emp = _resolve_bucket(
        bucket, active_qs, today, scope=scope,
    )

    rows = [
        {"employee": emp, "right_meta": right_meta_by_emp.get(emp.pk, "")}
        for emp in employees
    ]

    # Team "Out today" renders off/leave as a two-tab strip.
    tabs = None
    if scope == "team" and bucket in _TEAM_OUT_BUCKETS:
        tabs = [
            {"bucket": "off", "label": "Day off", "active": bucket == "off"},
            {"bucket": "leave", "label": "On leave", "active": bucket == "leave"},
        ]

    return render(
        request,
        "dashboard/status_today.html",
        {
            "page_title": _BUCKET_LABELS[bucket],
            "bucket": bucket,
            "bucket_label": _BUCKET_LABELS[bucket],
            "today": today,
            "rows": rows,
            "row_count": len(rows),
            "total_active": active_qs.count(),
            "header_subtitle": header_subtitle,
            "show_shift_pills": bucket in SHIFT_BUCKETS and scope == "org",
            "show_attendance_link": bucket == "working" and scope == "org",
            # /attendance/ defaults to the viewer's *own* month-to-date, so
            # this link carries an explicit date to land on the team list —
            # yesterday, the latest date TimeLy punches are synced for.
            "attendance_history_date": today - timedelta(days=1),
            "scope": scope,
            "back_tab": "team" if scope == "team" else "org",
            "count_noun": "in your team" if scope == "team" else "active employees",
            "tabs": tabs,
        },
    )


def _resolve_bucket(
    bucket: str, active_qs, today: date, scope: str = "org",
) -> tuple[list, str, dict]:
    """Return (employees, header_subtitle, right_meta_by_emp_pk) for a bucket."""
    base_select = ("designation", "department", "job_info__reports_to", "job_location")

    # Shift-status buckets (today)
    if bucket in SHIFT_BUCKETS:
        buckets = dash.today_status_buckets(active_qs, today=today)
        pks = {
            "working": buckets["working_pks"],
            "off":     buckets["off_pks"],
            "leave":   buckets["leave_pks"],
        }[bucket]
        employees = list(
            CustomUser.objects
            .filter(pk__in=pks)
            .select_related(*base_select)
            .order_by("first_name", "last_name")
        )
        right_meta: dict = {}
        if bucket == "leave" and pks:
            right_meta = {
                la.employee_id: la.leave_type.name
                for la in (
                    LeaveApplication.objects
                    .filter(
                        employee_id__in=pks,
                        status=LeaveApplication.Status.APPROVED,
                        start_date__lte=today,
                        end_date__gte=today,
                    )
                    .select_related("leave_type")
                )
            }
        elif bucket == "working" and employees:
            from attendance.timely import fetch_office_presence
            presence = fetch_office_presence(today)
            right_meta = {
                emp.pk: _format_presence(presence.get(str(emp.employee_id)))
                for emp in employees
                if emp.employee_id
            }
        subtitle = f"today · {today.strftime('%a, %b %-d')}"
        return employees, subtitle, right_meta

    # New joiners this month
    if bucket == "new_joiners":
        month_start = today.replace(day=1)
        employees = list(
            active_qs
            .filter(joining_date__gte=month_start, joining_date__lte=today)
            .select_related(*base_select)
            .order_by("-joining_date")
        )
        right_meta = {
            e.pk: e.joining_date.strftime("%b %-d") if e.joining_date else ""
            for e in employees
        }
        return employees, f"since {month_start.strftime('%b %-d')}", right_meta

    # Confirmations due — team scope uses the rolling next-30-days window (to
    # match the Team card); org scope uses the calendar month-to-end window.
    if bucket == "confirmations_due":
        if scope == "team":
            confirmation_filter = {
                "confirmation_date__gte": today,
                "confirmation_date__lte": today + timedelta(days=30),
            }
            subtitle = "in the next 30 days"
        else:
            month_start = today.replace(day=1)
            end_of_month = dash._end_of_month(today)
            confirmation_filter = {
                "confirmation_date__gte": today,
                "confirmation_date__lt": month_start + timedelta(days=32),
                "confirmation_date__lte": end_of_month,
            }
            subtitle = "due this month"
        employees = list(
            active_qs
            .filter(**confirmation_filter)
            .select_related(*base_select)
            .order_by("confirmation_date")
        )
        right_meta = {
            e.pk: e.confirmation_date.strftime("%b %-d") if e.confirmation_date else ""
            for e in employees
        }
        return employees, subtitle, right_meta

    # Birthdays this week
    if bucket == "birthdays_week":
        week_end = today + timedelta(days=7)
        employees = dash._birthdays_on_dates(today, week_end)
        right_meta = {}
        for e in employees:
            dob = getattr(getattr(e, "personal_info", None), "date_of_birth", None)
            right_meta[e.pk] = dob.strftime("%b %-d") if dob else ""
        return employees, "next 7 days", right_meta

    # Office buckets
    if bucket.startswith("office_"):
        dist = dash.office_distribution(active_qs)
        if bucket == "office_mbd":
            pks = dist["mbd_pks"]
        elif bucket == "office_mhk":
            pks = dist["mhk_pks"]
        elif bucket == "office_raowa":
            pks = dist["raowa_pks"]
        else:  # office_unassigned
            pks = dist["other_pks"] | dist["unknown_pks"]
        employees = list(
            CustomUser.objects
            .filter(pk__in=pks)
            .select_related(*base_select)
            .order_by("first_name", "last_name")
        )
        right_meta = {
            e.pk: (e.job_location.name if e.job_location else "—")
            for e in employees
        }
        return employees, f"{len(employees)} employees", right_meta

    # Fallback (unreachable — caller validates against _BUCKET_LABELS)
    return [], "", {}


def notification_bell(request):
    """The topbar bell's dropdown body (+ an out-of-band badge), polled by
    htmx every 30s and on ``orbis:approvals-changed``. See core/approvals.py.

    Not wrapped in ``login_required``: a poll from a tab whose session has
    expired would follow the login redirect and swap the login page into the
    dropdown. A bare 401 is an htmx error response — nothing is swapped.
    """
    from core.approvals import bell_summary

    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    response = render(request, "partials/_notification_bell_body.html", {
        "bell": bell_summary(request.user, request=request),
    })
    response["Cache-Control"] = "no-store"
    return response
