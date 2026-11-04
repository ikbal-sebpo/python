"""Shared employee list-filtering: open search (q) + department + team.

Mirrors the roster grid's filter behaviour so list/report pages (users,
employees, shift-summary) apply the same scoped controls. The scoping is
security-aware: locked users are clamped to their own department/teams, and a
requested team outside the viewer's allowed set is ignored (anti-tamper).
"""

from django.db.models import Q

from accounts.permissions import (
    can_see_all_departments,
    effective_department_id,
    visible_departments,
    visible_teams,
)


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def effective_team_id(user, request, department_id):
    """Requested ?team=, but only if it's a team the viewer may pick."""
    team_id = _parse_int(request.GET.get("team"))
    if not team_id:
        return None
    allowed = visible_teams(user, request=request, department_id=department_id)
    return team_id if allowed.filter(pk=team_id).exists() else None


def effective_filter_ids(user, request):
    """``(department_id, team_id)`` the list should actually be narrowed by.

    A team always implies its department: the filter bar's own cascade never
    lets someone pick a team without a department, but a hand-typed
    ``?team=<id>`` with no ``?department=`` used to filter the rows by the
    team while the department dropdown still read "All departments". Now
    the team's own ``department_id`` is backfilled instead, so filtering and
    the dropdowns agree — and a page that needs "a department is selected"
    (``/attendance/``'s date-range mode) is satisfied by a team pick too.
    Safe because ``effective_team_id`` has already vetted the team against
    the viewer's visible set, and a visible team's department is in scope
    by construction.
    """
    dept_id = effective_department_id(user, request)
    team_id = effective_team_id(user, request, dept_id)
    if team_id and not dept_id:
        from accounts.models import Team

        dept_id = (
            Team.objects.filter(pk=team_id).values_list("department_id", flat=True).first()
        )
    return dept_id, team_id


def search_active(request) -> bool:
    """True when the open search box (?q=) is non-blank.

    Deliberately ignores ?department / ?team: a department pick alone should
    still show the current headcount, whereas typing a name is an explicit
    lookup -- list views pass this as ``visible_employees(include_separated=)``
    so a search can find a deactivated employee (same default/search split as
    roster.views._resolve_grid_employees). Scoping is unaffected: the
    is_active clamp is the only thing dropped, so a viewer still only finds
    people who were in their downline / grants.
    """
    return bool((request.GET.get("q") or "").strip())


def employee_search_q(q, prefix=""):
    """Q object for the open search box (name / email / employee ID), or None.

    ID and email are unique, so they match exactly ("539" never lands on
    "1539"); email matches either the full address or just its local part
    (q before the @), so a login name like "jdoe" finds "jdoe@sebpo.com".
    Name stays a substring search. `prefix` (e.g. "employee__") targets a
    model that reaches CustomUser through an FK.
    """
    q = (q or "").strip()
    if not q:
        return None
    return (
        Q(**{f"{prefix}employee_id__iexact": q})
        | Q(**{f"{prefix}email__iexact": q})
        | Q(**{f"{prefix}email__istartswith": f"{q}@"})
        | Q(**{f"{prefix}first_name__icontains": q})
        | Q(**{f"{prefix}last_name__icontains": q})
    )


def apply_employee_filters(qs, user, request):
    """Apply ?q + ?department + ?team to an employee queryset (safely scoped)."""
    cond = employee_search_q(request.GET.get("q"))
    if cond is not None:
        qs = qs.filter(cond)
    dept_id, team_id = effective_filter_ids(user, request)
    if dept_id:
        qs = qs.filter(department_id=dept_id)
    if team_id:
        qs = qs.filter(team_id=team_id)
    return qs


def employee_filter_context(user, request):
    """Context vars the shared filter bar template needs (dropdowns + selection).

    ``teams`` is deliberately every team the viewer may pick, NOT just the
    chosen department's: the filter controls narrow the team <select>
    client-side (base.html::wireDepartmentTeamCascade, keyed off each
    option's ``data-department``) so the list re-scopes the moment a
    department is picked -- including inside a hover popover, which a page
    reload would close. A server-narrowed list would leave that snapshot
    holding a single department's teams and go empty on the next switch.
    Filtering is unaffected: ``effective_team_id`` still drops a submitted
    team that doesn't belong to the chosen department.
    """
    dept_id, team_id = effective_filter_ids(user, request)
    return {
        "departments": visible_departments(user, request=request),
        "teams": visible_teams(user, request=request),
        "selected_department_id": dept_id,
        "selected_team_id": team_id,
        "can_pick_all_departments": can_see_all_departments(user, request=request),
        "q": request.GET.get("q", ""),
    }
