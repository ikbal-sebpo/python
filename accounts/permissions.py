"""Centralized permission resolver for Orbis.

Single source of truth for:
- "Who can a user see?" (visibility scope)
- "Who can a user edit?" (write scope)
- "Can this user act on this leave / roster / profile?"

All helpers accept an optional `request` so per-request results memoize on
`request._perm_cache`. Templates rendering N rows pay the DB cost once.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model


User = get_user_model()


# --- Group definitions ----------------------------------------------------

# Canonical admin group name. The real admins live in "User Admins" (the empty
# legacy "Admins" group was retired — see the standardize_user_admins migration).
ADMIN_GROUP = "User Admins"

# Canonical full-visibility readers.
FULL_VISIBILITY_GROUPS = {
    ADMIN_GROUP,
    "HR",
}

# Canonical write-capable groups.
WRITE_SCOPE_GROUPS = {ADMIN_GROUP, "HR"}

# Designation.access_level floor for the "team leader and above can edit their
# own roster" rule — mirrors employees/services.py::MANAGER_MIN_ACCESS_LEVEL_NAME
# ("Team Lead and above" for the reports-to picker).
SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME = "Supervisor"

# Designation.access_level floor to approve a Dedicated Night Shift assignment
# (docs/PAYROLL_PLAN.md, 2026-09-07) — "only an APM or above manager can
# approve." A tier above SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME, which is
# reused as-is for the (lower) request/add gate.
DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME = "Manager"

# Designation.access_level floor to REVIEW an overtime entry (the first step
# of the two-step chain, docs/OVERTIME_APPROVAL_CHAIN_PLAN.md, 2026-10-17) —
# "reviewed by an APM or PM". Its own constant rather than reusing the night
# shift one so the two gates can be retuned independently.
OVERTIME_REVIEW_MIN_ACCESS_LEVEL_NAME = "Manager"

# Groups allowed to browse the whole org structure (all departments / teams) in
# the filter + form dropdowns. Broader than FULL_VISIBILITY_GROUPS: it also
# includes Senior Leadership and Payroll Admins. Everyone else is locked to
# their own department. "Payroll Admins" added 2026-09-10 — they run
# company-wide payroll, so being clamped to their own department on the
# payroll report/Support Info pages (which reuse this same department-filter
# clamp) defeated the entire point of the role; confirmed while testing a
# real Payroll Admin account only saw their own department's headcount.
ALL_DEPARTMENTS_GROUPS = {ADMIN_GROUP, "HR", "Senior Leadership", "Payroll Admins"}


# Sections an employee may edit on their own profile. JobInfo is intentionally
# excluded: department / designation / reports_to / joining_date are HR-managed
# organizational facts, not personal data.
SELF_EDITABLE_SECTIONS = {
    "PersonalInfo", "FamilyInfo", "Contact", "Address", "Email",
    "Education", "Nominee", "Reference", "ProfessionalCertification",
    "Vaccination", "IdentificationNumber", "LegalIssue", "Image",
}


# --- Per-request memoization ----------------------------------------------


def _request_cache(request):
    """Return the per-request memo dict (or None when no request is given)."""
    if request is None:
        return None
    cache = getattr(request, "_perm_cache", None)
    if cache is None:
        cache = {}
        request._perm_cache = cache
    return cache


def _cached(request, key, compute):
    cache = _request_cache(request)
    if cache is not None and key in cache:
        return cache[key]
    value = compute()
    if cache is not None:
        cache[key] = value
    return value


# --- Group membership -----------------------------------------------------


def user_groups(user, request=None) -> set[str]:
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("user_groups", user.pk),
        lambda: set(user.groups.values_list("name", flat=True)),
    )


def user_group_pks(user, request=None) -> set[int]:
    """Group primary keys, memoized per request — for assigned-group checks
    that would otherwise re-query group membership per row/step."""
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("user_group_pks", user.pk),
        lambda: set(user.groups.values_list("pk", flat=True)),
    )


def user_has_full_employee_access(user, request=None) -> bool:
    """Return True when user has org-wide employee visibility."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(user_groups(user, request) & FULL_VISIBILITY_GROUPS)


def can_see_all(user, request=None) -> bool:
    """Backward-compatible alias for org-wide read scope."""
    return user_has_full_employee_access(user, request)


def can_edit_all(user, request=None) -> bool:
    """Org-wide write scope: superuser + Admins + HR."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(user_groups(user, request) & WRITE_SCOPE_GROUPS)


def can_manage_groups(user, request=None) -> bool:
    """Only real superusers may view/edit a user's auth groups — group
    membership drives access/permissions, so even User Admins/HR (who
    otherwise pass can_edit_all) don't get to see or grant it."""
    return bool(user and user.is_authenticated and user.is_superuser)


# --- Hierarchy traversal --------------------------------------------------

def _compute_reportee_user_ids(user, include_separated: bool = False) -> set:
    """BFS through JobInfo.reports_to descendants, excluding the input user.

    `include_separated` drops the `employee__is_active` filter at every BFS
    level — added 2026-09-08 for payroll input scoping (see
    payroll_visible_employee_pks below): a resigned employee's manager must
    still be able to submit overtime/variable pay for days worked before
    separation, and (unlike the deactivate-on-LWD job itself) nothing here
    should silently drop their former reportees from the traversal either.
    Defaults to the existing active-only behaviour everywhere else — leave,
    roster, attendance and general visibility deliberately keep excluding
    separated employees from "who's on my team" scope.
    """
    return set(reportee_depths(user, include_separated=include_separated))


def reportee_depths(user, include_separated: bool = False) -> dict:
    """``{employee pk: depth}`` for the same BFS as ``_compute_reportee_user_ids``
    (1 = direct report, 2 = their report, …). The traversal itself lives here
    so the Directory API's scope endpoint (accounts/api/views.py) reports
    exactly the reportees Orbis's own permission checks use."""
    from employees.models import JobInfo

    seen = {user.pk}
    frontier = {user.pk}
    depths = {}
    depth = 0

    while frontier:
        depth += 1
        qs = JobInfo.objects.filter(reports_to_id__in=frontier)
        if not include_separated:
            qs = qs.filter(employee__is_active=True)
        next_level = set(qs.values_list("employee_id", flat=True))
        # Cycle protection: never revisit nodes already discovered.
        next_level -= seen
        if not next_level:
            break
        depths.update(dict.fromkeys(next_level, depth))
        seen |= next_level
        frontier = next_level

    return depths


def get_reportee_user_ids(user, request=None, include_separated: bool = False) -> set:
    """Return all direct and indirect reportees (cycle-safe)."""
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("reportees", user.pk, include_separated),
        lambda: _compute_reportee_user_ids(user, include_separated=include_separated),
    )


def _compute_direct_report_pks(user) -> set:
    """Immediate reports only — one level of JobInfo.reports_to."""
    from employees.models import JobInfo

    return set(
        JobInfo.objects
        .filter(reports_to_id=user.pk, employee__is_active=True)
        .values_list("employee_id", flat=True)
    )


def direct_report_pks(user, request=None) -> set:
    """Return the user's direct reports only (not the recursive downline)."""
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("direct_reports", user.pk),
        lambda: _compute_direct_report_pks(user),
    )


def downline_pks(user, request=None) -> set:
    """Backward-compatible helper: self + all recursive reportees."""
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("downline", user.pk),
        lambda: get_reportee_user_ids(user, request=request) | {user.pk},
    )


# --- Department-wide manager grants ---------------------------------------

def _compute_department_grant_pks(user, include_separated: bool = False) -> set:
    """Active members of every department this user manages department-wide,
    excluding the user themselves.

    This is the read+write grant for department-wide visibility: department
    heads and additional managers (e.g. payroll reviewers) alike see and act
    on the whole department, regardless of the reports-to chain.

    `include_separated` — see _compute_reportee_user_ids's docstring.
    """
    from django.db.models import Q

    from accounts.models import Department

    dept_ids = set(
        Department.objects
        .filter(Q(head_of_department=user) | Q(additional_managers=user), is_active=True)
        .values_list("pk", flat=True)
    )
    if not dept_ids:
        return set()
    qs = User.objects.filter(department_id__in=dept_ids).exclude(pk=user.pk)
    if not include_separated:
        qs = qs.filter(is_active=True)
    return set(qs.values_list("pk", flat=True))


def department_grant_pks(user, request=None, include_separated: bool = False) -> set:
    """User PKs granted to this user via Department.head_of_department or
    Department.additional_managers."""
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("dept_grant", user.pk, include_separated),
        lambda: _compute_department_grant_pks(user, include_separated=include_separated),
    )


def _compute_team_grant_pks(user, include_separated: bool = False) -> set:
    """Active members of every team this user is an additional manager on,
    excluding the user themselves.

    Same read+write shape as ``_compute_department_grant_pks``, but at team
    grain — for delegated visibility into one or a handful of specific teams
    (e.g. an APM) without granting the whole department.

    `include_separated` — see _compute_reportee_user_ids's docstring.
    """
    from accounts.models import Team

    team_ids = set(
        Team.objects
        .filter(additional_managers=user, is_active=True)
        .values_list("pk", flat=True)
    )
    if not team_ids:
        return set()
    qs = User.objects.filter(team_id__in=team_ids).exclude(pk=user.pk)
    if not include_separated:
        qs = qs.filter(is_active=True)
    return set(qs.values_list("pk", flat=True))


def team_grant_pks(user, request=None, include_separated: bool = False) -> set:
    """User PKs granted to this user via Team.additional_managers."""
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("team_grant", user.pk, include_separated),
        lambda: _compute_team_grant_pks(user, include_separated=include_separated),
    )


def _access_level_at_least(access_level, min_name: str) -> bool:
    """True when the given AccessLevel's rank >= the named tier's rank."""
    from accounts.models import AccessLevel

    if access_level is None:
        return False
    min_rank = (
        AccessLevel.objects.filter(name=min_name)
        .values_list("rank", flat=True).first()
    )
    if min_rank is None:
        # Misconfigured/missing tier — fail closed rather than granting to everyone.
        return False
    return access_level.rank >= min_rank


def _has_min_access_level(user, min_name: str) -> bool:
    """True when user.designation.access_level.rank >= the named tier's rank.

    Follows Designation.access_level (not the denormalized CustomUser.access_level
    set at bulk-upload time) — same path employees/views.py's
    MANAGER_MIN_ACCESS_LEVEL_NAME filter uses.
    """
    designation = getattr(user, "designation", None)
    return _access_level_at_least(getattr(designation, "access_level", None), min_name)


def _has_own_min_access_level(user, min_name: str) -> bool:
    """True when the user's own denormalized CustomUser.access_level meets min_name.

    Manual per-user override, for employees who can't get the "acting TL"
    grant via Team.additional_managers (see _manages_any_team) — e.g. no team
    assigned, or a grant that doesn't fit the team shape. Set directly via the
    Access Level field on the user's admin change page. Deliberately scoped
    to this one check only — it does NOT feed _has_min_access_level, so it
    has no effect on Designation-driven logic like the reports-to picker.
    """
    return _access_level_at_least(getattr(user, "access_level", None), min_name)


def effective_access_level(user):
    """The AccessLevel governing pay-rate banding (payroll.rates.resolve_rate's
    `access_level` param): the higher-ranked of Designation.access_level and
    the employee's own denormalized override, mirroring how
    can_self_edit_roster/`_has_min_access_level` OR `_has_own_min_access_level`
    already treat either as sufficient. None if neither is set. Public (no
    leading underscore) — payroll reads this directly, unlike the boolean
    _has_*_access_level helpers above which are permissions-internal.
    """
    if not user:
        return None
    designation_level = getattr(getattr(user, "designation", None), "access_level", None)
    own_level = getattr(user, "access_level", None)
    if designation_level is None:
        return own_level
    if own_level is None:
        return designation_level
    return own_level if own_level.rank > designation_level.rank else designation_level


def _manages_any_department(user) -> bool:
    """True when user is head_of_department or an additional_manager anywhere."""
    from django.db.models import Q

    from accounts.models import Department

    return Department.objects.filter(
        Q(head_of_department=user) | Q(additional_managers=user), is_active=True,
    ).exists()


def _manages_any_team(user) -> bool:
    """True when user is an additional_manager on any Team.

    Covers "acting TL" employees: HR grants them Team.additional_managers on
    their own team (same field an APM uses for scoped roster/payroll
    visibility — see Team.additional_managers help_text) instead of changing
    their Designation, so reports-to eligibility and payroll grade stay
    accurate while they still get roster self-edit.
    """
    from accounts.models import Team

    return Team.objects.filter(additional_managers=user, is_active=True).exists()


def can_self_edit_roster(user, request=None) -> bool:
    """Team-leader-and-above self-edit rule for roster patterns/cells.

    A user may edit their own roster when their designation's access level is
    Supervisor-or-above, when their own (denormalized) CustomUser.access_level
    is manually set to Supervisor-or-above (see _has_own_min_access_level),
    when they manage a department (as head_of_department or an
    additional_manager), or when they're an additional_manager on any team
    (the "acting TL" grant — see _manages_any_team) — even if their own
    designation is still Employee-tier.
    """
    if not user or not user.is_authenticated:
        return False
    return _cached(
        request, ("self_edit_roster", user.pk),
        lambda: (
            _has_min_access_level(user, SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME)
            or _has_own_min_access_level(user, SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME)
            or _manages_any_department(user)
            or _manages_any_team(user)
        ),
    )


def managed_scope_pks(user, request=None, include_separated: bool = False) -> set:
    """Flat "who may I see / act on" scope (excludes self).

    Strict reports-to downline plus any department- or team-wide manager
    grants. This is the canonical scope for visibility and act/edit checks —
    distinct from ``get_reportee_user_ids`` (strict downline), which the
    dashboard's reports-to *tree* views keep using.

    `include_separated` — see _compute_reportee_user_ids's docstring;
    defaults to the existing active-only scope.
    """
    if not user or not user.is_authenticated:
        return set()
    return _cached(
        request, ("managed_scope", user.pk, include_separated),
        lambda: (
            get_reportee_user_ids(user, request=request, include_separated=include_separated)
            | department_grant_pks(user, request=request, include_separated=include_separated)
            | team_grant_pks(user, request=request, include_separated=include_separated)
        ),
    )


def visible_employee_pks(user, request=None, include_separated: bool = False) -> set:
    """Union of self + managed scope (reportees + department/team grants).

    `include_separated` keeps separated (is_active=False) employees in the
    scope — see payroll_visible_employee_pks (the original caller) and
    roster.views._resolve_grid_employees (search mode). Defaults to the
    active-only scope everywhere else.
    """
    if not user or not user.is_authenticated:
        return set()
    if can_see_all(user, request):
        if include_separated:
            return _cached(
                request, ("all_pks_incl_separated",),
                lambda: set(User.objects.values_list("pk", flat=True)),
            )
        return _cached(
            request, ("all_active_pks",),
            lambda: set(User.objects.filter(is_active=True).values_list("pk", flat=True)),
        )
    return (
        managed_scope_pks(user, request=request, include_separated=include_separated)
        | {user.pk}
    )


def payroll_visible_employee_pks(user, request=None) -> set:
    """Same shape as visible_employee_pks, but includes recently-separated
    employees (is_active=False) in the uploader's scope.

    Added 2026-09-08: a supervisor's overtime upload for a resigned
    employee's final working period was being rejected outright — both at
    parse time (payroll/upload.py used to require is_active=True to even
    resolve the employee_id) and, once that was fixed, at this scope check
    (an inactive leaver had already dropped out of every reportee/
    department/team-grant traversal, so even an admin's "see everyone" path
    excluded them). Payroll specifically must keep paying someone for days
    they actually worked, same reasoning as
    payroll.engine._population_queryset's deliberate non-filtering of
    is_active. Every other visibility use (leave, roster, attendance,
    general employee lists) keeps the active-only default on purpose —
    this is payroll-input-scoping only, not a general policy change.
    (Roster search mode is the one other opt-in — it passes
    include_separated through visible_employees to read historical shift
    rows; see roster.views._resolve_grid_employees.)

    A thin wrapper over visible_employee_pks so the scoping logic stays in
    exactly one place.
    """
    return visible_employee_pks(user, request=request, include_separated=True)


def get_visible_employee_pks(user, request=None) -> set:
    """Canonical helper name for visible employee PKs."""
    return visible_employee_pks(user, request=request)


def get_visible_employees(user, request=None, include_separated: bool = False):
    """Canonical helper returning a QuerySet for visible employees.

    `include_separated=True` drops the is_active clamp (both the see-all and
    the scoped branch) while leaving *who may see whom* untouched — the
    scoping still runs through visible_employee_pks/managed_scope_pks, just
    with separated employees left in. Opted into per-caller; every existing
    call site keeps the active-only default.
    """
    if not user or not user.is_authenticated:
        return User.objects.none()
    if can_see_all(user, request):
        return User.objects.all() if include_separated else User.objects.filter(is_active=True)
    qs = User.objects.filter(
        pk__in=visible_employee_pks(
            user, request=request, include_separated=include_separated,
        ),
    )
    return qs if include_separated else qs.filter(is_active=True)


def visible_employees(user, request=None, include_separated: bool = False):
    """Backward-compatible queryset form for list views and form widgets."""
    return get_visible_employees(user, request=request, include_separated=include_separated)


# --- Org-structure dropdown scope (departments / teams) -------------------


def can_see_all_departments(user, request=None) -> bool:
    """True when the user may browse every department / team in the dropdowns."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(user_groups(user, request) & ALL_DEPARTMENTS_GROUPS)


def scope_department_ids(user, request=None) -> set:
    """Departments actually represented in the user's own visible scope.

    Added 2026-09-16: `visible_departments`/`effective_department_id`/
    `visible_teams` used to clamp a non-privileged user to their own
    `CustomUser.department_id` row, on the assumption that "who you manage"
    and "your own department" are the same thing. That broke for a manager
    whose reports-to downline spans multiple departments (e.g. a C-level
    exec whose direct reports are themselves department heads) -- the
    dashboard's scope count (visible_employee_pks) showed their real,
    multi-department reach, but every department/team-filtered list page
    silently narrowed them down to a single department. See
    docs/PLAN.md history / the employees-list investigation for the
    real-world case (rezaur@sebpo.com: 1359-person scope, 11-row list).

    Privileged users (ALL_DEPARTMENTS_GROUPS) get every active department,
    matching visible_departments' own privileged branch. Everyone else gets
    the departments their *own* visible_employee_pks scope actually touches
    -- which collapses to exactly `{user.department_id}` for the common
    case of a manager whose whole downline sits in one department, so this
    is a no-op for the vast majority of users.
    """
    from accounts.models import Department

    if not user or not user.is_authenticated:
        return set()
    if can_see_all_departments(user, request):
        return set(Department.objects.filter(is_active=True).values_list("pk", flat=True))
    return _cached(
        request, ("scope_dept_ids", user.pk),
        lambda: set(
            User.objects.filter(pk__in=visible_employee_pks(user, request=request))
            .exclude(department__isnull=True)
            .values_list("department_id", flat=True)
            .distinct()
        ),
    )


def visible_departments(user, request=None):
    """Departments the user may pick in a dropdown.

    Privileged users get every active department; everyone else is scoped to
    the departments in their own visible scope (see scope_department_ids) --
    not just their own `department_id` row -- so a manager whose downline
    spans multiple departments can actually pick among them.
    """
    from accounts.models import Department

    qs = Department.objects.filter(is_active=True).order_by("name")
    if can_see_all_departments(user, request):
        return qs
    return qs.filter(pk__in=scope_department_ids(user, request=request))


def effective_cpl_treatment(employee) -> str:
    """How `employee` earns Compensatory Leave for working a holiday.

    Checks the employee's team override first (if set), falling back to
    the parent department's setting, and finally "AUTO" for an employee
    with no department at all. See `accounts.models.Department.CplTreatment`.
    """
    from accounts.models import Department

    team = getattr(employee, "team", None)
    if team is not None and team.cpl_treatment_override:
        return team.cpl_treatment_override
    department = getattr(employee, "department", None)
    if department is not None:
        return department.cpl_treatment
    return Department.CplTreatment.AUTO


def effective_department_id(user, request):
    """The department id to actually filter results by.

    Privileged users honour the ``?department=`` querystring (``None`` = all).
    Everyone else may pick among the departments in their own visible scope
    (see scope_department_ids): a requested department outside that scope is
    ignored (anti-tamper), same as the existing querystring check on the
    team side (accounts.filters.effective_team_id). ``None`` means "don't
    narrow by department" -- safe here because every caller already
    pre-scopes its queryset with visible_employees / visible_employee_pks /
    managed_scope_pks before applying this filter, so a manager whose
    downline spans several departments simply sees their whole (already-
    scoped) list instead of being collapsed onto one department.
    """
    raw = (request.GET.get("department") or "").strip()
    if can_see_all_departments(user, request):
        return int(raw) if raw.isdigit() else None
    allowed = scope_department_ids(user, request=request)
    if raw.isdigit() and int(raw) in allowed:
        return int(raw)
    return next(iter(allowed)) if len(allowed) == 1 else None


def visible_teams(user, request=None, department_id=None):
    """Teams the user may pick, optionally narrowed to one department.

    Non-privileged users are scoped to the departments in their own visible
    scope (see scope_department_ids) -- not just their own `department_id`
    row, matching visible_departments -- optionally narrowed further by the
    ``department_id`` argument (e.g. once a specific in-scope department has
    been picked in a dropdown). This then narrows further still: someone who
    manages a whole department (head_of_department / Department.
    additional_managers) still sees every team in it; someone who manages
    specific teams (Team.additional_managers — the "acting TL" grant) sees
    those plus their own; everyone else — an ordinary employee with no
    management grant — sees only their own team. An ordinary employee has no
    use for every OTHER team in their department: their own visible rows
    (visible_employees) never change based on that pick, so it's a dead-end
    choice that only name-drops org structure they have no reason to see. A
    user with no team of their own falls back to the whole (in-scope)
    department set, since there's nothing narrower to show them.
    """
    from accounts.models import Team

    qs = Team.objects.filter(is_active=True).order_by("name")
    if can_see_all_departments(user, request):
        if department_id:
            qs = qs.filter(department_id=department_id)
        return qs

    scope_dept_ids = scope_department_ids(user, request=request)
    if not scope_dept_ids:
        return qs.none()
    qs = qs.filter(department_id__in=scope_dept_ids)
    # An out-of-scope department_id is ignored (anti-tamper), same as
    # effective_department_id -- not zeroed out, since real callers only
    # ever pass a value effective_department_id has already vetted.
    if department_id and department_id in scope_dept_ids:
        qs = qs.filter(department_id=department_id)

    if _manages_any_department(user):
        return qs

    managed_team_ids = set(
        Team.objects.filter(additional_managers=user, is_active=True).values_list("pk", flat=True)
    )
    own_team_id = getattr(user, "team_id", None)
    if managed_team_ids:
        if own_team_id:
            managed_team_ids.add(own_team_id)
        return qs.filter(pk__in=managed_team_ids)

    if not own_team_id:
        return qs
    return qs.filter(pk=own_team_id)


def has_managed_scope(user, request=None) -> bool:
    """True when user can act on at least one reportee."""
    if not user or not user.is_authenticated:
        return False
    if can_edit_all(user, request):
        return True
    return bool(managed_scope_pks(user, request=request))


def has_dedicated_night_shift_approval_scope(user, request=None) -> bool:
    """True when user could approve at least one Dedicated Night Shift
    assignment right now — the Manager-tier gate from
    can_approve_dedicated_night_shift, without a specific assignment.

    Deliberately stricter than has_managed_scope: a TL/APM-tier manager has
    managed scope (can request a night shift for a report) but can't
    approve one, so they shouldn't see a "pending my approval" count they
    can't act on.
    """
    if not user or not user.is_authenticated:
        return False
    if can_edit_all(user, request):
        return True
    if not meets_dedicated_night_approve_tier(user):
        return False
    return bool(managed_scope_pks(user, request=request))


def meets_dedicated_night_approve_tier(user) -> bool:
    """The Manager-tier half of can_approve_dedicated_night_shift (Designation
    tier or the user's own override), without the HR bypass or a scope check.
    core/approvals.py walks reporting chains with it to find who a night
    shift request is actually waiting on."""
    return (
        _has_min_access_level(user, DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME)
        or _has_own_min_access_level(user, DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME)
    )


# --- Capability checks ----------------------------------------------------

def can_view_employee(user, target, request=None) -> bool:
    """Read-only access to an employee's profile page.

    Uses the separated-inclusive scope (2026-10-01): the employees/users
    lists surface deactivated people on an explicit ?q= search, and a
    manager who could see someone while they were active may still open
    their profile afterwards. Scoping itself is unchanged -- a deactivated
    person outside the viewer's downline / grants stays invisible. Edit
    permissions keep their own, active-only checks.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if can_see_all(user, request):
        return True
    return target.pk in visible_employee_pks(user, request, include_separated=True)


def _in_managed_scope(user, target_pk, request=None) -> bool:
    return target_pk in managed_scope_pks(user, request=request)


def can_edit_employee_profile(user, target, section: str | None = None, request=None) -> bool:
    """Edit a section of an employee's profile.

    `section` is the model class name of the section (e.g., "Contact",
    "JobInfo", "IdentificationNumber"). Self-edit is allowed only for
    sections in SELF_EDITABLE_SECTIONS — JobInfo never qualifies — AND only
    while HR has switched on CustomUser.profile_self_service_enabled for
    this employee (off by default; see can_manage_profile_self_service).
    This gate does not affect a manager editing someone else's sections via
    managed scope below, which is unrelated to self-service.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if can_edit_all(user, request):
        return True
    if target.pk == user.pk:
        return section in SELF_EDITABLE_SECTIONS and target.profile_self_service_enabled
    return _in_managed_scope(user, target.pk, request)


def can_edit_employee_basic_info(user, target, request=None) -> bool:
    """Edit the top-level CustomUser profile fields for an employee."""
    if not user or not user.is_authenticated or not target:
        return False
    if can_edit_all(user, request):
        return True
    if target.pk == user.pk:
        return False
    return _in_managed_scope(user, target.pk, request)


def editable_section_slugs(user, target, sections, request=None) -> set[str]:
    """Pre-compute the set of editable section slugs for one viewer/target.

    `sections` is an iterable of objects with `.slug` and `.model`; matches
    the SectionSpec shape from `employees.sections`. Returns a set of slugs
    the template can do O(1) `{% if spec.slug in editable_section_slugs %}`
    on. Mirrors can_edit_employee_profile's self branch, including the
    profile_self_service_enabled gate.
    """
    if not user or not user.is_authenticated or not target:
        return set()
    if can_edit_all(user, request):
        return {s.slug for s in sections}
    if target.pk == user.pk:
        if not target.profile_self_service_enabled:
            return set()
        return {
            s.slug for s in sections
            if s.model.__name__ in SELF_EDITABLE_SECTIONS
        }
    if _in_managed_scope(user, target.pk, request):
        return {s.slug for s in sections}
    return set()


def can_manage_profile_self_service(user, request=None) -> bool:
    """Toggle CustomUser.profile_self_service_enabled for an employee.

    HR/Admins only, deliberately not managed-scope-inclusive — a plain
    supervisor in _in_managed_scope must not be able to switch this on,
    only HR decides when an employee may self-manage their profile."""
    return can_edit_all(user, request)


def can_edit_roster_pattern(user, target, request=None) -> bool:
    """Edit anyone's weekly pattern or daily cell.

    Same scope as profile edit, plus one exception: a non-admin CAN edit their
    own pattern/cells when they're "team leader and above" — Supervisor+
    Designation.access_level or own CustomUser.access_level override, or a
    department/team head/additional_manager (see can_self_edit_roster).
    Everyone else still can't touch their own roster.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if can_edit_all(user, request):
        return True
    if target.pk == user.pk:
        return can_self_edit_roster(user, request)
    return _in_managed_scope(user, target.pk, request)


def can_act_on_leave(user, application, request=None) -> bool:
    """Approve or decline a leave request.

    Admins (HR / Admins) can approve anyone, including their own request.
    Non-admins can never approve their own; they can approve anyone in their
    managed scope.
    """
    if not user or not user.is_authenticated or not application:
        return False
    if can_edit_all(user, request):
        return True
    if application.employee_id == user.pk:
        return False
    return _in_managed_scope(user, application.employee_id, request)


def can_cancel_leave(user, application, request=None) -> bool:
    """Cancel / withdraw a leave request.

    The applicant may cancel their own request:
      - while it's still pending, or
      - once approved, but ONLY if it hasn't started yet (a future leave).
        Past or in-progress approved leave can't be self-withdrawn — those go
        through a manager / admin.

    Managers / admins can cancel any state (see `can_act_on_leave`).
    """
    from django.utils import timezone

    if not user or not user.is_authenticated or not application:
        return False
    if application.employee_id == user.pk:
        if application.is_pending:
            return True
        if application.is_approved and application.start_date > timezone.localdate():
            return True
    return can_act_on_leave(user, application, request)


def can_edit_leave(user, application, request=None) -> bool:
    """Revise a leave request before anyone has acted on it.

    PENDING only — a pending application has no ledger debit, no roster
    shifts and no sandwich billing yet, so changing it touches nothing
    else. Whoever could have filed it may revise it (can_apply_leave_for):
    the employee, admins, and managers over their managed scope. Once
    approved/declined/cancelled it's closed; cancel and re-apply instead.
    """
    if not application or not application.is_pending:
        return False
    return can_apply_leave_for(user, application.employee, request)


def can_apply_leave_for(user, target, request=None) -> bool:
    """Apply for leave on behalf of another employee.

    Self always allowed; admins always allowed; managers can apply for their
    managed scope.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if target.pk == user.pk:
        return True
    if can_edit_all(user, request):
        return True
    return _in_managed_scope(user, target.pk, request)


def can_request_shift_change(user, target, request=None) -> bool:
    """File a `roster.ShiftChangeRequest` for `target`.

    Exactly the leave rule (can_apply_leave_for): self always allowed,
    admins always allowed, managers for their managed scope. A delegation,
    not a copy, so the two can't drift — docs/SHIFT_CHANGE_REQUEST_PLAN.md.
    """
    return can_apply_leave_for(user, target, request)


def can_act_on_shift_change(user, change_request, request=None) -> bool:
    """Approve or decline a shift change request.

    Exactly the leave rule (can_act_on_leave): HR/Admins can act on anyone
    including their own; nobody else approves their own, but can approve
    anyone in their managed scope. can_act_on_leave only reads
    `.employee_id`, so the request object passes straight through.

    Except on a date inside the employee's APPROVED dedicated night shift
    window (2026-11-03): then only their department head decides — no
    Team Leader, APM, additional manager, HR or superuser — and for the
    head's own request, their line manager (roster/night_window.py).
    """
    if not user or not user.is_authenticated or not change_request:
        return False
    if shift_change_needs_department_head(change_request):
        from roster.night_window import is_night_window_approver

        return is_night_window_approver(user, change_request.employee)
    return can_act_on_leave(user, change_request, request)


def shift_change_needs_department_head(change_request) -> bool:
    """True when the request's date sits in an APPROVED dedicated night
    shift window. Prefers a precomputed `_night_window` (set by list views
    in one batch query) over a per-row lookup."""
    cached = getattr(change_request, "_night_window", None)
    if cached is not None:
        return cached
    from roster.night_window import in_night_window

    result = in_night_window(change_request.employee_id, change_request.date)
    change_request._night_window = result
    return result


def can_cancel_shift_change(user, change_request, request=None) -> bool:
    """Cancel / withdraw a shift change request.

    The applicant may cancel their own while it's still PENDING. Managers /
    admins may cancel a PENDING one per can_act_on_shift_change. Unlike
    leave there is no withdrawing an APPROVED change — the roster cell may
    have been edited again since, so restoring the snapshot could clobber a
    newer legitimate value; the fix is a new request or a roster edit.
    """
    if not user or not user.is_authenticated or not change_request:
        return False
    if not change_request.is_pending:
        return False
    if change_request.employee_id == user.pk:
        return True
    return can_act_on_shift_change(user, change_request, request)


def can_request_attendance_correction(user, target, request=None) -> bool:
    """File an attendance correction request for `target`.

    Same rule as can_apply_leave_for: self always allowed, admins always
    allowed, managers can file for their managed scope.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if target.pk == user.pk:
        return True
    if can_edit_all(user, request):
        return True
    return _in_managed_scope(user, target.pk, request)


def can_act_on_attendance_correction(user, correction, request=None) -> bool:
    """Approve or decline an attendance correction request.

    Same rule as can_act_on_leave: admins can act on anyone including their
    own; nobody else can approve their own correction, but can approve
    anyone in their managed scope.
    """
    if not user or not user.is_authenticated or not correction:
        return False
    if can_edit_all(user, request):
        return True
    if correction.employee_id == user.pk:
        return False
    return _in_managed_scope(user, correction.employee_id, request)


def can_manage_cpl_grants(user, target, request=None) -> bool:
    """Manually record a `leave.CplGrant` for `target` — e.g. for working a
    scheduled weekly off day (not a Holiday, so nothing auto-detects it).

    Never self-service: this is a compensation decision, not something an
    employee can grant themselves. Same rule as `can_act_on_leave`/
    `can_act_on_attendance_correction` otherwise — admins can act on
    anyone, a manager can act on their managed scope.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if can_edit_all(user, request):
        return True
    if target.pk == user.pk:
        return False
    return _in_managed_scope(user, target.pk, request)


def can_set_own_night_shift_consent(user, target, request=None) -> bool:
    """Record a night-shift consent decision for `target` — self-service
    only (own record). HR/Admin on-behalf recording goes through
    can_manage_night_shift_consent instead; the two are checked separately
    by NightShiftConsentUpdateView rather than folded into one function,
    since "is this her own record" and "am I HR" are different questions
    with different UI entry points (dashboard card vs. audit list)."""
    if not user or not user.is_authenticated or not target:
        return False
    return user.pk == target.pk


def can_manage_night_shift_consent(user, request=None) -> bool:
    """HR/Admins — view the night-shift-consent audit list and record a
    decision on an employee's behalf (e.g. captured verbally or on paper).
    Deliberately org-wide (can_edit_all), not managed-scope: this is a
    labour-law compliance record HR must be able to see and act on for
    anyone, not just a direct manager's own reports."""
    return can_edit_all(user, request)


def can_request_dedicated_night_shift(user, target, request=None) -> bool:
    """Request a Dedicated Night Shift assignment for `target`.

    "A TL or above manager may add this dedicated night shift info" —
    reuses the existing SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME ("Supervisor")
    tier rather than a new constant, since that's already the codebase's
    "team-leader-and-above" threshold (see can_self_edit_roster). Unlike
    leave/attendance-correction requests, this isn't self-service — the
    requester must be TL-or-above AND (an admin or managing `target`).

    **Confirmed 2026-09-08: nobody self-requests, TL/APM tier included.**
    `target == user` is never sufficient on its own, at any tier —
    `_in_managed_scope` always excludes self for non-admins, and there's no
    self-carve-out here the way leave/attendance corrections have one. A
    Team Leader who wants to become a Dedicated Night Shift employee needs
    their own manager (or an admin) to file it, same as anyone else. This
    was checked and deliberately kept, not a gap — don't "fix" it into a
    self-service path without asking first.
    """
    if not user or not user.is_authenticated or not target:
        return False
    if can_edit_all(user, request):
        return True
    if not meets_dedicated_night_request_tier(user):
        return False
    return _in_managed_scope(user, target.pk, request)


def meets_dedicated_night_request_tier(user) -> bool:
    """The Team-Leader-tier half of can_request_dedicated_night_shift
    (Designation tier or the user's own override), without the HR bypass or
    a scope check — the request-side twin of
    meets_dedicated_night_approve_tier."""
    return (
        _has_min_access_level(user, SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME)
        or _has_own_min_access_level(user, SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME)
    )


def dedicated_night_shift_team_pks(user, request=None) -> set:
    """Employees whose Dedicated Night Shift windows `user` maintains — the
    list page's "My team" tab and its "New request" picker (added
    2026-11-03, when the page got its Roster sidebar entry). Exactly the
    people can_request_dedicated_night_shift lets them file for: managed
    scope (self excluded) behind the TL+ bar. HR/Admins pass the bar too but
    already have the org-wide "All" tab; for them this is only their own
    downline, if any.
    """
    if not user or not user.is_authenticated:
        return set()
    if not (can_edit_all(user, request) or meets_dedicated_night_request_tier(user)):
        return set()
    return managed_scope_pks(user, request=request)


def can_cancel_dedicated_night_shift(user, assignment, request=None, today=None) -> bool:
    """Withdraw an assignment (→ CANCELLED, terminal) — added 2026-11-03.

    - PENDING: whoever may edit it (can_edit_dedicated_night_shift — the TL+
      bar in managed scope); withdrawing an undecided ask costs nothing.
    - APPROVED: an approved window drives NIGHT_QUARTERLY pay, so cancelling
      it needs the approval bar (can_approve_dedicated_night_shift) and,
      below HR, only while the window hasn't started. Once it has, nights
      were already worked at the dedicated rate: ending it early is an
      end_date edit, which keeps the worked part paid, whereas a cancel
      would un-pay it on the next recalculation. HR/Admins (can_edit_all)
      may still cancel a started window, for an approval that was a mistake.
    - DECLINED/CANCELLED: already terminal.
    """
    if not user or not user.is_authenticated or not assignment:
        return False
    status = assignment.Status
    if assignment.status == status.PENDING:
        return can_edit_dedicated_night_shift(user, assignment, request)
    if assignment.status == status.APPROVED:
        if can_edit_all(user, request):
            return True
        if not can_approve_dedicated_night_shift(user, assignment, request):
            return False
        from django.utils import timezone

        return assignment.start_date > (today or timezone.localdate())
    return False


def can_approve_dedicated_night_shift(user, assignment, request=None) -> bool:
    """Approve or decline a Dedicated Night Shift assignment.

    "Only an APM or above manager can approve" — a tier above
    SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME, hence the new
    DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME constant. Same
    nobody-approves-their-own / managed-scope shape as can_act_on_leave.
    """
    if not user or not user.is_authenticated or not assignment:
        return False
    if can_edit_all(user, request):
        return True
    if assignment.employee_id == user.pk:
        return False
    if not (
        _has_min_access_level(user, DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME)
        or _has_own_min_access_level(user, DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME)
    ):
        return False
    return _in_managed_scope(user, assignment.employee_id, request)


def can_edit_dedicated_night_shift(user, assignment, request=None) -> bool:
    """Edit an existing Dedicated Night Shift assignment's window/note —
    added 2026-09-08 so the profile-page card has the same "update it"
    affordance every other section does.

    - PENDING: the TL+ bar (can_request_dedicated_night_shift) — a manager
      revising their own not-yet-decided ask. Mirrors
      attendance.AttendanceCorrectionEditView's "editable while pending"
      shape exactly.
    - APPROVED: **also** the TL+ bar, as of 2026-09-15 — reverses the
      original "APPROVED needs the approver's own Manager-tier bar" rule.
      Supervisors maintain these windows day to day (moving a from/to date
      as an employee cycles in and out of dedicated duty), and gating the
      edit behind the approval tier left them with no way to do it at all.
      The pay control that rule existed to protect now lives in
      employees.views.DedicatedNightShiftEditView instead: a below-Manager
      editor's save demotes the row back to PENDING and clears the approval
      stamp, so the revised window earns nothing until an APM/PM
      re-approves it. See docs/PAYROLL_PLAN.md.
    - DECLINED/CANCELLED: terminal, not editable here — file a new request.

    This function stays a pure "may they open the editor" check — the
    demotion is the view's job, since only the view knows a save actually
    happened. Callers that need to know whether an edit will cost the row
    its approval ask can_approve_dedicated_night_shift separately (that's
    exactly what the view does).

    Self-editing is excluded at any tier here too, inherited from
    can_request_dedicated_night_shift (whose _in_managed_scope never
    includes target == user for a non-admin) — confirmed 2026-09-08 and
    unchanged by the 2026-09-15 tier drop; see that function's docstring.
    """
    if not assignment:
        return False
    if assignment.status in (assignment.Status.PENDING, assignment.Status.APPROVED):
        return can_request_dedicated_night_shift(user, assignment.employee, request)
    return False


# --- Payroll run employee notes / run lifecycle ----------------------------
#
# The page-level gate (who can open /payroll/runs/<id>/ at all) is a plain
# group-based ProtectedView ("Payroll" + "Payroll Admins" — see
# seed_orbis.py), same pattern as every other payroll view. The checks below
# are the finer-grained, per-action gates inside that page, mirroring the
# real manual payroll workflow (confirmed 2026-09-10):
#   1. HR-Payroll or Payroll Admin creates the run.
#   2. Supervisors review the Support Info tab, edit roster/leave/overtime
#      for their own reportees (already-existing features), and leave a
#      Supervisor note per employee.
#   3. HR-Payroll reviews and leaves an HR note.
#   4. Only Payroll Admins lock inputs, calculate/recalculate, export, and
#      enter the actual TDS/incentive figures those notes describe.
#
# Deliberately NO can_edit_all/"HR"/"User Admins" blanket bypass in any of
# these — only a literal Django superuser does, same as the page-access
# layer (dynamic_menu/decorators.py). "Payroll" group membership alone only
# grants page access (a plain supervisor is in it too); the finer-grained
# checks below are what actually gate money and cross-role writes.

def _is_payroll_admin(user, request=None) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return "Payroll Admins" in user_groups(user, request)


def _is_payroll_hr(user, request=None) -> bool:
    """The curated 2-3 people who both work on payroll and are HR —
    deliberately not "Payroll" alone (a plain supervisor is in it too) or
    "HR" alone (not everyone in the HR department works on payroll)."""
    if not user or not user.is_authenticated:
        return False
    groups = user_groups(user, request)
    return "Payroll" in groups and "HR" in groups


def can_see_payroll_money(user, request=None) -> bool:
    """Only Payroll Admins see actual payroll money figures — Salary/Gross/
    Deductions/Tax/Net — anywhere in the payroll app. "Payroll" group
    members (supervisors + the small payroll-HR team) can reach the
    register/notes pages but never see these figures."""
    return _is_payroll_admin(user, request)


def can_view_salary_history(user, request=None) -> bool:
    """Who may see an employee's gross salary outside the payroll register —
    the Service Book's salary-history part and the bulk-onboarding sheet /
    results workbook (both carry gross). The HR group only, plus a literal
    superuser; User Admins were removed 2026-10-05
    (docs/PAYROLL_DATA_PROTECTION_PLAN.md §3): in the company HR holds the
    employee salary information, account administration does not."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return "HR" in user_groups(user, request)


def can_create_payroll_run(user, request=None) -> bool:
    """Who may click "+ Create run" — HR-Payroll or Payroll Admins, per the
    real workflow's first step. Never a plain supervisor or a generic
    HR-department person who isn't one of the few who work on payroll."""
    return _is_payroll_admin(user, request) or _is_payroll_hr(user, request)


def can_manage_payroll_calculation(user, request=None) -> bool:
    """Who may Lock/Reopen inputs or Calculate/Recalculate a run — Payroll
    Admins only, the same tier as Export/Adjust-inputs/Variable-entries.
    Per the real workflow, HR-Payroll reviews and notes but only Payroll
    Admins actually run the numbers, as many times as needed."""
    return _is_payroll_admin(user, request)


def can_manage_locked_overtime(user, request=None) -> bool:
    """Who may edit, void or delete an overtime entry that is already settled
    (approved/voided) in a LOCKED period — the HR-Payroll pair plus Payroll
    Admins. **No longer an approval right** (2026-10-17): approving is
    can_approve_overtime below, per employee; this gate only decides who may
    touch settled history after lock. The history below is kept as written.

    Narrowed 2026-09-15 (Q55): this used to mean "may still WRITE overtime
    after lock", when a locked period refused everyone else outright. A locked
    period now accepts writes from anyone in scope but forces them to PENDING,
    so the privilege that matters is promoting one to APPROVED. HR's own
    uploads land PENDING too — approval is always an explicit, recorded act
    (OvertimeEntry.processed_by/processed_at), never implied by who uploaded.

    Deliberately NOT the whole "Payroll" group: a plain supervisor is in it
    too (see _is_payroll_hr and the can_write_payroll_note note above), and a
    supervisor approving their own team's late overtime would defeat the
    review entirely. Payroll Admins are included because
    can_manage_payroll_calculation already lets them Reopen the run, edit and
    re-Lock it — refusing them the approval would only make them take the long
    way round.

    Same tier as can_create_payroll_run on purpose: "who may open a run" and
    "who signs off what still lands in it after lock" are one concept, and
    splitting them would let the two drift.

    See payroll/services.py::OvertimeWindow, which decides the status per
    work-date, and payroll/views.py::OvertimeEntryDecideView, which spends
    this permission.
    """
    return _is_payroll_hr(user, request) or _is_payroll_admin(user, request)


# --- Overtime review/approval chain (2026-10-17) ---------------------------
#
# Every overtime write now lands PENDING (Q4 fully reversed, Q56 superseded —
# docs/OVERTIME_APPROVAL_CHAIN_PLAN.md). Two distinct rights follow:
#
#   review  — a Manager-tier person (APM/PM and above, by Designation tier or
#             the per-user override) with the employee in managed scope.
#   approve — the employee's department head or one of the department's
#             additional managers, plus HR-Payroll and Payroll Admins.
#
# Nobody reviews or approves their own row, the house rule everywhere else.
# An approver may approve a row still PENDING (the review stamp is then
# theirs), so a department whose head is also the nearest manager isn't made
# to click twice and one with no Manager-tier person between a TL and the
# head doesn't stall. can_manage_locked_overtime above no longer approves
# anything; it only decides who may edit/void/delete a settled row in a
# locked period.

def meets_overtime_review_tier(user) -> bool:
    """The Manager-tier half of can_review_overtime, without the scope check
    — core/approvals.py walks reporting chains with it to find the nearest
    reviewer, exactly as meets_dedicated_night_approve_tier does."""
    return (
        _has_min_access_level(user, OVERTIME_REVIEW_MIN_ACCESS_LEVEL_NAME)
        or _has_own_min_access_level(user, OVERTIME_REVIEW_MIN_ACCESS_LEVEL_NAME)
    )


def can_review_overtime(user, employee, request=None) -> bool:
    """Mark `employee`'s overtime entry REVIEWED — Manager tier, employee in
    managed scope (separated-inclusive: a leaver's final-period overtime
    still needs reviewing), never your own row. Deliberately no HR / admin
    bypass: HR-Payroll approves instead (can_approve_overtime), and a
    superuser who needs to move a row along approves it."""
    if not user or not user.is_authenticated or not employee:
        return False
    if employee.pk == user.pk:
        return False
    if not meets_overtime_review_tier(user):
        return False
    return employee.pk in managed_scope_pks(user, request=request, include_separated=True)


def overtime_approver_department_ids(user, request=None) -> set:
    """Departments `user` approves overtime for as head or additional
    manager. One query, request-cached, so decorating a page of rows costs
    nothing per row."""
    if not user or not user.is_authenticated:
        return set()

    def compute():
        from django.db.models import Q

        from accounts.models import Department

        return set(
            Department.objects
            .filter(Q(head_of_department=user) | Q(additional_managers=user), is_active=True)
            .values_list("pk", flat=True)
        )

    return _cached(request, ("ot_approver_depts", user.pk), compute)


def _is_overtime_global_approver(user, request=None) -> bool:
    """HR-Payroll and Payroll Admins (superusers included via the latter):
    the fallback approvers for every department, decided 2026-10-17."""
    return _is_payroll_hr(user, request) or _is_payroll_admin(user, request)


def can_approve_overtime(user, employee, request=None) -> bool:
    """Mark `employee`'s overtime entry APPROVED (or reject it) — the
    department head / additional managers of the employee's own department,
    HR-Payroll, Payroll Admins. Never your own row: a head's own overtime
    falls to whoever the head reports to, as can_act_on_variable_pay does."""
    if not user or not user.is_authenticated or not employee:
        return False
    if employee.pk == user.pk:
        return False
    if _is_overtime_global_approver(user, request):
        return True
    dept_id = getattr(employee, "department_id", None)
    if dept_id is None:
        return False
    if dept_id in overtime_approver_department_ids(user, request=request):
        return True
    # The head's own row: the head is excluded above, so check whether the
    # employee heads a department and `user` is their line manager.
    department = getattr(employee, "department", None)
    if department is not None and department.head_of_department_id == employee.pk:
        job_info = getattr(employee, "job_info", None)
        manager_id = getattr(job_info, "reports_to_id", None) if job_info else None
        return manager_id == user.pk
    return False


def can_decide_overtime(user, employee, request=None) -> bool:
    """Review OR approve — the page-level "may act on this row at all"."""
    return (
        can_review_overtime(user, employee, request=request)
        or can_approve_overtime(user, employee, request=request)
    )


def has_overtime_review_scope(user, request=None) -> bool:
    """Could `user` review at least one entry right now? Gates the bell's
    "Overtime to review" section and the list's review controls."""
    if not user or not user.is_authenticated:
        return False
    if not meets_overtime_review_tier(user):
        return False
    return bool(managed_scope_pks(user, request=request))


def has_overtime_approval_scope(user, request=None) -> bool:
    """Could `user` approve at least one entry right now? Department heads /
    additional managers and the global approvers."""
    if not user or not user.is_authenticated:
        return False
    if _is_overtime_global_approver(user, request):
        return True
    return bool(overtime_approver_department_ids(user, request=request))


def can_act_on_variable_pay(user, employee, request=None) -> bool:
    """Approve or reject a payroll.VariablePayEntry for `employee` — SLA
    achievement pay, the Data Solutions "Test Salary" deduction, bonuses,
    incentives and disciplinary adjustments alike.

    **The department head of the employee's own department, and nobody
    else.** Confirmed 2026-09-19: HR is deliberately NOT in this path — HR
    reviews and Finance calculates and pays, but neither approves. That
    makes this the one payroll gate that is *not* can_manage_locked_overtime
    (overtime keeps its HR-Payroll gate from Q55, unchanged); the two split
    by input table, not by component, which is why there is no
    per-component approver field. See
    docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase D, "One gate".

    Deliberately NOT managed scope (`_in_managed_scope`, which
    can_act_on_leave and can_act_on_attendance_correction use): that would
    let any team leader or supervisor in the reporting line sign off their
    own team's money. Department head is a single named person
    (accounts.Department.head_of_department), resolved the same way
    offboarding/services.py::_resolve_assignee's DEPT_HEAD branch does.

    Nobody approves their own row — the house rule everywhere else
    (can_act_on_leave, can_act_on_attendance_correction, Q56's "supervisors
    can file but never approve"). Department heads aren't entitled to SLA
    pay so this shouldn't arise; if it ever does, it falls to the head's own
    JobInfo.reports_to rather than to HR.

    Superusers are a break-glass path so a department with a vacant head
    doesn't freeze — that is system administration, not the HR role the
    business excluded.
    """
    if not user or not user.is_authenticated or not employee:
        return False
    if user.is_superuser:
        return True

    department = getattr(employee, "department", None)
    head = getattr(department, "head_of_department", None) if department else None
    if head is None:
        return False

    if head.pk == employee.pk:
        # The head's own row — falls to whoever they report to.
        job_info = getattr(head, "job_info", None)
        manager = getattr(job_info, "reports_to", None) if job_info else None
        return bool(manager and manager.pk == user.pk)

    return head.pk == user.pk


def can_write_payroll_note(user, employee, role: str, request=None) -> bool:
    """Who may write a PayrollRunEmployeeNote of the given `role` for
    `employee` (role is one of PayrollRunEmployeeNote.Role's values, passed
    as a plain string to avoid a payroll -> accounts model import).

    - SUPERVISOR: the employee's own manager — same managed-scope rule
      can_act_on_leave/can_act_on_attendance_correction already use.
    - HR: the curated "Payroll" + "HR" people (_is_payroll_hr). Confirmed
      2026-09-10 after a non-HR supervisor in "Payroll" was able to write
      an HR note under an earlier, looser version of this check.
    - FINANCE: Payroll Admins (_is_payroll_admin) — the person(s) who run
      payroll and send bank advice.

    The three roles are strictly walled off from each other: a Supervisor
    note must never be editable by HR or Finance, an HR note never by a
    Supervisor or Finance, a Finance note never by anyone else. Confirmed
    2026-09-10 after a user in both "User Admins" and "HR" (which used to
    satisfy a can_edit_all bypass here) was able to edit a Supervisor's
    note through that blanket override — removed.
    """
    if not user or not user.is_authenticated or not employee:
        return False
    if user.is_superuser:
        return True
    if role == "SUPERVISOR":
        return _in_managed_scope(user, employee.pk, request)
    if role == "HR":
        return _is_payroll_hr(user, request)
    if role == "FINANCE":
        return _is_payroll_admin(user, request)
    return False


def writable_payroll_note_roles(user, request=None) -> set:
    """Which PayrollRunEmployeeNote.Role values `user` may write in bulk, at
    all. Drives which single role payroll/upload_views.py's bulk uploader
    defaults to (never a page choice — see that module's _default_note_role)
    — NOT a substitute for can_write_payroll_note's per-employee check,
    which still re-runs for every row (a Supervisor's own managed scope
    varies per employee; this only answers "does the uploader have this
    role in principle").

    SUPERVISOR deliberately checks managed_scope_pks directly, NOT
    has_managed_scope — has_managed_scope's can_edit_all bypass makes it
    True for any HR/User Admin regardless of whether they manage anyone,
    but can_write_payroll_note's own SUPERVISOR branch (_in_managed_scope)
    never honors that bypass for a non-superuser. Using has_managed_scope
    here would grant SUPERVISOR eligibility, and since it now defaults with
    no way to switch, a real HR staffer with zero direct reports would be
    silently defaulted into a role that writes nothing for anyone in their
    file — confirmed while building the auto-default behavior, 2026-09-16.
    """
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return {"SUPERVISOR", "HR", "FINANCE"}
    roles = set()
    if managed_scope_pks(user, request=request):
        roles.add("SUPERVISOR")
    if _is_payroll_hr(user, request=request):
        roles.add("HR")
    if _is_payroll_admin(user, request=request):
        roles.add("FINANCE")
    return roles


def can_bulk_upload_payroll_notes(user, request=None) -> bool:
    """Page-level gate for the notes bulk uploader — true when the viewer can
    write at least one PayrollRunEmployeeNote role. Broader than
    has_managed_scope alone (used by the overtime uploader) since an
    HR-Payroll or Payroll Admins viewer with no reportees of their own must
    still reach this page to file HR/Finance notes in bulk."""
    return bool(writable_payroll_note_roles(user, request=request))


def can_send_roster_email(user, team, request=None) -> bool:
    """Send / configure the weekly roster email for `team`.

    HR/Admins anywhere; otherwise whoever manages the team — its
    department's head or additional managers, the team's own additional
    managers, or anyone with at least one active team member in their
    managed scope (the reports-to chain, e.g. the team's line manager).
    """
    if not user or not user.is_authenticated or team is None:
        return False
    if can_edit_all(user, request):
        return True
    dept = team.department
    if dept is not None and (
        dept.head_of_department_id == user.pk
        or dept.additional_managers.filter(pk=user.pk).exists()
    ):
        return True
    if team.additional_managers.filter(pk=user.pk).exists():
        return True
    member_pks = set(
        User.objects.filter(team=team, is_active=True).values_list("pk", flat=True)
    )
    return bool(member_pks & managed_scope_pks(user, request=request))


# --- Tax declarations ----------------------------------------------------

def can_submit_tax_declaration(user, declaration, request=None) -> bool:
    """Confirm (and thereby lock) an EmployeeTaxDeclaration: the employee it
    belongs to, or Payroll-HR / Payroll Admins on their behalf (a paper
    form handed in). Nobody once it's locked — the Django admin is the only
    way back. Deliberately independent of profile_self_service_enabled:
    every employee must be able to confirm their own tax position whether or
    not HR has opened the rest of their profile for self-editing."""
    if not user or not user.is_authenticated or declaration is None:
        return False
    if declaration.is_locked:
        return False
    if declaration.employee_id == user.pk:
        return True
    return _is_payroll_hr(user, request) or _is_payroll_admin(user, request)


def can_confirm_tax_declarations_for_others(user, request=None) -> bool:
    """The on-behalf half of can_submit_tax_declaration, for template gating."""
    return _is_payroll_hr(user, request) or _is_payroll_admin(user, request)


def can_view_tax_declaration_document(user, declaration, request=None) -> bool:
    """Open the AIT payment evidence: the employee, anyone who may edit that
    section of their profile (HR / managed scope), or Payroll-HR / Payroll
    Admins. The file is streamed by a view, never linked by media URL."""
    if not user or not user.is_authenticated or declaration is None:
        return False
    if declaration.employee_id == user.pk:
        return True
    if _is_payroll_hr(user, request) or _is_payroll_admin(user, request):
        return True
    return can_edit_employee_profile(
        user, declaration.employee, section="EmployeeTaxDeclaration", request=request,
    )


# --- Salary loans (docs/SALARY_LOAN_PLAN.md, 2026-10-08) -------------------

def can_edit_gross_salary(user, request=None) -> bool:
    """Open an employee's gross-salary page and record a revision there
    (payroll/views.py::SalaryStructureReviseView, 2026-10-09): the
    payroll-HR pair or Payroll Admins — the user's decision that HR-Payroll
    "can update and approve" alongside Payroll Admins, no maker-checker.
    The Gross salary *list* and its sidebar entry stay Payroll Admins only
    (the ProtectedView row); the pair reach the page from the employee
    profile's Service Book card."""
    return _is_payroll_admin(user, request) or _is_payroll_hr(user, request)


def can_manage_salary_loans(user, request=None) -> bool:
    """Record / edit / cancel a salary loan and see the loans list with its
    money columns: the payroll-HR pair or Payroll Admins — the same two
    tiers as can_create_payroll_run. Never a plain "Payroll" supervisor or
    a generic HR person who doesn't work on payroll."""
    return _is_payroll_hr(user, request) or _is_payroll_admin(user, request)


def can_disburse_salary_loans(user, request=None) -> bool:
    """Mark a loan disbursed, record a skip or an adjustment — Finance's
    payroll staff, who hold Payroll Admins (Q67). Money leaving the company
    is the same tier that sees money everywhere else in payroll."""
    return _is_payroll_admin(user, request)


def can_view_salary_loan(user, loan, request=None) -> bool:
    """The employee's own profile card and the approval document: the
    employee themself, or anyone who may manage loans."""
    if not user or not user.is_authenticated or loan is None:
        return False
    if loan.employee_id == user.pk:
        return True
    return can_manage_salary_loans(user, request)


# ---------------------------------------------------------------------------
# Access log (audit app)
# ---------------------------------------------------------------------------

#: Who may read the access log. "Audit Viewers" exists only for this — HR is
#: deliberately absent, because the log shows HR's own reads (CTO decision,
#: 2026-10-09; docs/ACCESS_LOG_PLAN.md §3.5).
AUDIT_VIEWER_GROUPS = frozenset({"User Admins", "Audit Viewers"})


def can_view_access_log(user, request=None) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(AUDIT_VIEWER_GROUPS & user_groups(user, request))
