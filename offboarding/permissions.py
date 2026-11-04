"""Access-control helpers for the offboarding app.

Mirror the leave + documents pattern: thin wrappers around
``accounts.permissions`` so the policy lives in one place.
"""

from __future__ import annotations

from django.db import models

from accounts.permissions import (
    user_groups,
    user_group_pks,
    can_edit_all,
    visible_employee_pks,
)


OFFBOARDING_GLOBAL_SCOPE_GROUPS = {
    "User Admins",
    "HR",
    "Senior Leadership",
}


def can_see_all_offboarding(user, request=None) -> bool:
    """Offboarding-specific global read scope.

    Menu ACL currently grants ``All cases`` to Senior Leadership, so align
    access here to avoid inconsistent visibility versus navigation entry.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(user_groups(user, request) & OFFBOARDING_GLOBAL_SCOPE_GROUPS)


def can_view_case(user, case, request=None) -> bool:
    """Employee can see their own case. HR/Admin/Senior Leadership see all.
    Managers see anyone in their downline.
    Clearance teams
    can see cases they are assigned to act on."""
    if not user or not user.is_authenticated:
        return False
    if case.resignation.employee_id == user.pk:
        return True
    if can_see_all_offboarding(user, request):
        return True
    if case.resignation.employee_id in visible_employee_pks(user, request):
        return True

    # Clearance team members can see cases where they are assigned to ANY step.
    return case.steps.filter(
        models.Q(assigned_user=user)
        | models.Q(assigned_group__in=user_group_pks(user, request))
    ).exists()


def can_view_resignation(user, resignation, request=None) -> bool:
    if not user or not user.is_authenticated:
        return False
    if resignation.employee_id == user.pk:
        return True
    if can_see_all_offboarding(user, request):
        return True
    return resignation.employee_id in visible_employee_pks(user, request)


def can_act_on_resignation(user, resignation=None, request=None) -> bool:
    """HR-only — only edit_all roles can ACCEPT / REJECT."""
    return bool(user and user.is_authenticated and can_edit_all(user, request))


def can_withdraw_resignation(user, resignation, request=None) -> bool:
    """Applicant can withdraw while still PENDING; admins always can."""
    from offboarding.models import Resignation
    if not user or not user.is_authenticated:
        return False
    if can_edit_all(user, request):
        return True
    return (
        resignation.employee_id == user.pk
        and resignation.status == Resignation.Status.PENDING
    )


def can_act_on_clearance_step(user, step, request=None) -> bool:
    """Restrict acting to the assigned user/group OR full-admin roles.
    Enforces sequential workflow: only the current step is actionable."""
    if not user or not user.is_authenticated:
        return False

    # 1. The applicant can NEVER act on their own steps.
    if step.case.resignation.employee_id == user.pk:
        return False

    # 2. Superusers and users with full edit scope can act on anything.
    if can_edit_all(user, request=request):
        return True

    # 3. Only allow acting if the case is IN_PROGRESS.
    from offboarding.models import OffboardingCase, ClearanceStep
    if step.case.status != OffboardingCase.Status.IN_PROGRESS:
        return False

    # 4. Only allow acting if the step itself is not already finished.
    if step.status in [ClearanceStep.Status.DONE, ClearanceStep.Status.SKIPPED]:
        return False

    # 5. Enforce sequential workflow: only the current step is actionable.
    # This prevents Finance from clearing before IT, etc.
    if step.order != step.case.current_step_order:
        return False

    # 6. Finally, check if the user is the assigned person or in the assigned group.
    if step.assigned_user_id == user.pk:
        return True
    if step.assigned_group_id and step.assigned_group_id in user_group_pks(user, request):
        return True

    return False
