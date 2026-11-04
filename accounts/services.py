from dataclasses import dataclass
from datetime import date

from employees.models import IdentificationNumber
from offboarding.models import Resignation


@dataclass
class PriorEmploymentWarning:
    employee_pk: object
    employee_name: str
    employee_id: str
    employee_is_active: bool
    separation_type_display: str = ""
    last_working_date: date | None = None
    is_involuntary: bool = False  # INVOLUNTARY or ABSCONDING

    def as_message(self) -> str:
        status = "active" if self.employee_is_active else "inactive"
        who = f"{self.employee_name}, Employee ID {self.employee_id}" if self.employee_id else self.employee_name
        msg = f"Possible rehire: this NID matches an existing employee, {who} ({status})."
        if self.separation_type_display:
            msg += f" Prior separation: {self.separation_type_display}, last working date {self.last_working_date}."
        if self.is_involuntary:
            msg += " This was an involuntary/absconding separation — review before proceeding."
        return msg


def check_prior_employment(nid_number: str) -> PriorEmploymentWarning | None:
    """Soft, non-blocking rehire/duplicate-NID check against Orbis's own live
    data only (no pre-Orbis historical import exists yet). Matches across ALL
    employees regardless of is_active (offboarding soft-deactivates, never
    deletes). Never raises; callers decide how to surface the result — this
    never blocks anything.

    Two rows within the same bulk-upload file sharing an NID won't cross-match
    each other, since nothing is written to the DB until apply_upload() runs —
    only pre-existing DB data is caught.
    """
    nid = (nid_number or "").strip()
    if not nid:
        return None
    match = (
        IdentificationNumber.objects
        .filter(id_type=IdentificationNumber.IdType.NID, id_number__iexact=nid)
        .select_related("employee")
        .order_by("-added_at")
        .first()
    )
    if match is None:
        return None
    employee = match.employee
    resignation = (
        Resignation.objects
        .filter(employee=employee, status=Resignation.Status.ACCEPTED)
        .order_by("-last_working_date")
        .first()
    )
    warning = PriorEmploymentWarning(
        employee_pk=employee.pk,
        employee_name=employee.get_full_name() or employee.email,
        employee_id=employee.employee_id or "",
        employee_is_active=employee.is_active,
    )
    if resignation:
        warning.separation_type_display = resignation.get_separation_type_display()
        warning.last_working_date = resignation.last_working_date
        warning.is_involuntary = resignation.separation_type in (
            Resignation.SeparationType.INVOLUNTARY, Resignation.SeparationType.ABSCONDING,
        )
    return warning


# ---------------------------------------------------------------------------
# Line Managers group — derived membership, not hand-maintained
# ---------------------------------------------------------------------------

LINE_MANAGERS_GROUP = "Line Managers"


def line_manager_pks() -> set:
    """PKs of everyone who currently has at least one active direct report.

    Keyed off ``JobInfo.reports_to`` deliberately: that is the exact field
    ``offboarding.services._resolve_assignee`` uses to assign MANAGER_ACK
    steps, so membership matches precisely the people who can be handed the
    work. Reports who have left don't count — a manager whose whole team has
    gone isn't line-managing anyone.
    """
    from employees.models import JobInfo

    return set(
        JobInfo.objects
        .filter(reports_to__isnull=False, employee__is_active=True)
        .values_list("reports_to_id", flat=True)
    )


def reconcile_derived_group(group_name: str, should_be_pks, dry_run: bool = False) -> dict:
    """Make ``group_name``'s membership exactly ``should_be_pks`` (active only).

    Shared by every derived group. Membership is computed, never
    hand-edited: anything added by hand in admin is removed on the next run.
    Returns the add/remove sets so commands can report them.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    User = get_user_model()
    group, _ = Group.objects.get_or_create(name=group_name)

    should_be = set(
        User.objects.filter(pk__in=should_be_pks, is_active=True)
        .values_list("pk", flat=True)
    )
    currently = set(group.user_set.values_list("pk", flat=True))

    to_add, to_remove = should_be - currently, currently - should_be
    if not dry_run:
        if to_add:
            group.user_set.add(*User.objects.filter(pk__in=to_add))
        if to_remove:
            group.user_set.remove(*User.objects.filter(pk__in=to_remove))
    return {"added": to_add, "removed": to_remove, "total": len(should_be)}


def sync_line_managers(dry_run: bool = False) -> dict:
    """Reconcile ``Line Managers`` against ``JobInfo.reports_to``."""
    return reconcile_derived_group(LINE_MANAGERS_GROUP, line_manager_pks(), dry_run)


SUPERVISORS_GROUP = "Supervisors"
SUPERVISOR_MIN_ACCESS_LEVEL = "Supervisor"


def supervisor_pks() -> set:
    """PKs of everyone whose DESIGNATION sits at Supervisor rank or above.

    Seniority comes from ``Designation.access_level.rank``, the same source
    ``employees.services.reports_to_candidates`` uses to decide who may be
    picked as a manager — so "Supervisor and above" means one thing across
    the app. Note this reads the designation's level, not the per-user
    ``CustomUser.access_level`` override, which is unset for most people.

    An employee with no designation, or a designation with no access_level,
    is excluded: we cannot assert seniority we do not have data for.
    """
    from accounts.models import AccessLevel
    from django.contrib.auth import get_user_model

    floor = (
        AccessLevel.objects.filter(name=SUPERVISOR_MIN_ACCESS_LEVEL)
        .values_list("rank", flat=True).first()
    )
    if floor is None:
        return set()
    return set(
        get_user_model().objects
        .filter(is_active=True, designation__access_level__rank__gte=floor)
        .values_list("pk", flat=True)
    )


def sync_supervisors(dry_run: bool = False) -> dict:
    """Reconcile ``Supervisors`` against designation seniority.

    Unlike Line Managers there is no instant signal: designation changes go
    through the user edit form, and hanging a handler off CustomUser.save
    would fire on every profile write. A promotion therefore takes effect at
    the next nightly run rather than immediately.
    """
    return reconcile_derived_group(SUPERVISORS_GROUP, supervisor_pks(), dry_run)


def refresh_line_manager(user_pk) -> None:
    """Add/remove one person from the group based on their current reports.

    Called from the JobInfo signals so a new manager gets the menu item
    immediately. The nightly ``sync_line_managers`` is the full reconcile —
    same instant-signal + nightly-catch-up split the leave ledger uses.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    if not user_pk:
        return
    User = get_user_model()
    group, _ = Group.objects.get_or_create(name=LINE_MANAGERS_GROUP)
    user = User.objects.filter(pk=user_pk).first()
    if user is None:
        return
    if user.is_active and user_pk in line_manager_pks():
        group.user_set.add(user)
    else:
        group.user_set.remove(user)
