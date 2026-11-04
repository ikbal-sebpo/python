"""PayRate resolution — most-specific-scope-wins.

Specificity score (PAYROLL_PLAN.md §4): sub_project 8 > team 4 >
work_location 2 > department 1 > grade 1 > global 0, tie-broken by the
latest ``effective_from``. A gross-banded row is preferred over an
unbanded one when both otherwise tie, since a band is strictly more
specific information.

Pure lookup logic only — no run/engine state. The calculation engine
(Phase 3+) is what actually calls this per employee per period.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Q

from payroll.models import PayRate, TeamSalaryPackage

_SPECIFICITY_WEIGHTS = {
    "sub_project_id": 8,
    "team_id": 4,
    "work_location_id": 2,
    "department_id": 1,
    "grade_id": 1,
}

# TeamSalaryPackage shares _score_scope with PayRate but NOT its weights,
# because the two rank resource_status differently and deliberately so
# (2026-09-19).
#
# For a PayRate, resource_status is a carve-out from an otherwise-applicable
# rate (AdOps Trainees earn no night allowance), so it is a tiebreak worth +1.
# For a salary package it is the DOMINANT axis: a Data Solutions trainee is on
# 11,000 whatever team they have been parked on, and only becomes team-priced
# once they go live. Weighting team above status would put a trainee sitting on
# MARS on 18,000 before their go-live date — the exact overpayment Phase B
# exists to prevent.
_PACKAGE_WEIGHTS = {
    "resource_status": 8,
    "team_id": 4,
    "designation_id": 2,
    "department_id": 1,
}


def _score_scope(obj, weights: dict) -> int:
    """How specific a scoped row is: the sum of the weights of the scope
    fields it actually pins down. Shared by every most-specific-wins lookup
    in this module."""
    return sum(weight for field, weight in weights.items() if getattr(obj, field, None))


def _specificity(rate: PayRate) -> int:
    score = _score_scope(rate, _SPECIFICITY_WEIGHTS)
    if rate.min_gross is not None or rate.max_gross is not None:
        score += 1
    if rate.resource_status:
        score += 1
    if rate.min_access_level_id:
        score += 1
    return score


def resolve_rate(
    *,
    rate_type: str,
    on_date: date,
    department=None,
    team=None,
    sub_project=None,
    work_location=None,
    grade=None,
    overtime_type=None,
    gross: Decimal | None = None,
    resource_status: str | None = None,
    access_level=None,
) -> PayRate | None:
    """Return the best-matching active PayRate for the given scope, or None.

    `resource_status` and `access_level` (an accounts.AccessLevel instance,
    the employee's effective one — see accounts.permissions) were added
    2026-09-07 for the AdOps night-shift/OT policy: a Trainee carve-out and a
    "this access level or above" OT band, both resolved as data rather than
    an engine-level special case. Same additive-scope pattern as `gross`.
    """
    qs = (
        PayRate.objects
        .filter(rate_type=rate_type, is_active=True, effective_from__lte=on_date)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .filter(overtime_type=overtime_type)
    )
    for field, value in (
        ("department", department), ("team", team), ("sub_project", sub_project),
        ("work_location", work_location), ("grade", grade),
    ):
        qs = qs.filter(Q(**{field: value}) | Q(**{f"{field}__isnull": True}))
    if gross is not None:
        qs = qs.filter(Q(min_gross__isnull=True) | Q(min_gross__lte=gross))
        qs = qs.filter(Q(max_gross__isnull=True) | Q(max_gross__gt=gross))
    else:
        qs = qs.filter(min_gross__isnull=True, max_gross__isnull=True)
    qs = qs.filter(Q(resource_status="") | Q(resource_status=resource_status or ""))
    if access_level is not None:
        qs = qs.filter(Q(min_access_level__isnull=True) | Q(min_access_level__rank__lte=access_level.rank))
    else:
        qs = qs.filter(min_access_level__isnull=True)

    candidates = list(qs)
    if not candidates:
        return None
    candidates.sort(key=lambda r: (_specificity(r), r.effective_from), reverse=True)
    return candidates[0]


def active_team_salary_packages(on_date: date) -> list[TeamSalaryPackage]:
    """Every package in force on `on_date`, in one query.

    The engine resolves a package per employee and must not spend a query
    each doing it — payroll/engine.py's "~25 queries, not 25,000"
    discipline. Load once with this, then pass the list to
    ``resolve_team_salary_package(packages=...)``, which then filters in
    memory using the very same ranking as the querying path.
    """
    return list(
        TeamSalaryPackage.objects
        .filter(is_active=True, effective_from__lte=on_date)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .select_related("department", "team", "designation")
    )


def resolve_team_salary_package(
    *, on_date: date, department=None, team=None, designation=None,
    resource_status: str | None = None, packages=None,
) -> TeamSalaryPackage | None:
    """Best-matching active TeamSalaryPackage for the scope, or None.

    Same most-specific-wins contract as ``resolve_rate`` above, sharing
    ``_score_scope``: a team-scoped package beats a department-wide one, and
    a package that pins ``resource_status`` beats one that doesn't. Ties
    break on the latest ``effective_from``.

    ``department`` is required in practice — a package with no department is
    not expressible (the field is non-nullable), so an employee with no
    department on file resolves to None rather than picking up somebody
    else's package.
    """
    if department is None:
        return None
    department_id = getattr(department, "pk", department)
    team_id = getattr(team, "pk", team)
    designation_id = getattr(designation, "pk", designation)
    wanted_status = resource_status or ""

    if packages is None:
        qs = (
            TeamSalaryPackage.objects
            .filter(is_active=True, department_id=department_id, effective_from__lte=on_date)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        )
        for field, value in (("team_id", team_id), ("designation_id", designation_id)):
            qs = qs.filter(Q(**{field: value}) | Q(**{f"{field}__isnull": True}))
        qs = qs.filter(Q(resource_status="") | Q(resource_status=wanted_status))
        candidates = list(qs)
    else:
        # Same predicate as the queryset above, applied in memory — the
        # engine's pre-loaded path (active_team_salary_packages).
        candidates = [
            pkg for pkg in packages
            if pkg.department_id == department_id
            and pkg.team_id in (None, team_id)
            and pkg.designation_id in (None, designation_id)
            and pkg.resource_status in ("", wanted_status)
        ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda pkg: (_score_scope(pkg, _PACKAGE_WEIGHTS), pkg.effective_from),
        reverse=True,
    )
    return candidates[0]
