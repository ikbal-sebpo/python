"""Signal handlers for employees.

Keeps the derived ``Line Managers`` group current the moment a reporting
line changes, so a newly-promoted manager sees the "Pending acks" menu item
without waiting for the nightly reconcile. Mirrors the instant-signal +
nightly-catch-up split used by the leave ledger.

The pre_save stash mirrors ``offboarding/signals.py::_stash_prev_status``:
when reports_to moves from A to B, BOTH need re-evaluating — B gains a
report, and A may have just lost their last one.

Also keeps ``DesignationHistory`` in sync with ``CustomUser.designation``
(see that model's docstring) — the sender is referenced by string
("accounts.CustomUser") rather than imported, same cross-app convention
``leave/signals.py`` uses for ``roster.Shift``.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from django.utils import timezone

from employees.models import DesignationHistory, JobInfo, TeamAssignment


@receiver(pre_save, sender=JobInfo, dispatch_uid="employees.stash_prev_manager")
def _stash_prev_manager(sender, instance: JobInfo, **kwargs):
    if not instance.pk:
        instance._prev_reports_to_id = None
        instance._prev_resource_status = None
        return
    try:
        row = sender.objects.only("reports_to", "resource_status").get(pk=instance.pk)
        instance._prev_reports_to_id = row.reports_to_id
        instance._prev_resource_status = row.resource_status
    except sender.DoesNotExist:
        instance._prev_reports_to_id = None
        instance._prev_resource_status = None


@receiver(post_save, sender=JobInfo, dispatch_uid="employees.sync_line_manager")
def _sync_line_manager(sender, instance: JobInfo, **kwargs):
    from accounts.services import refresh_line_manager

    for pk in {instance.reports_to_id, getattr(instance, "_prev_reports_to_id", None)}:
        refresh_line_manager(pk)


@receiver(post_delete, sender=JobInfo, dispatch_uid="employees.sync_line_manager_delete")
def _sync_line_manager_on_delete(sender, instance: JobInfo, **kwargs):
    from accounts.services import refresh_line_manager

    refresh_line_manager(instance.reports_to_id)


@receiver(pre_save, sender="accounts.CustomUser", dispatch_uid="employees.stash_prev_designation")
def _stash_prev_designation(sender, instance, **kwargs):
    if instance._state.adding:
        instance._prev_designation_id = None
        return
    try:
        instance._prev_designation_id = (
            sender.objects.only("designation_id").get(pk=instance.pk).designation_id
        )
    except sender.DoesNotExist:
        instance._prev_designation_id = None


@receiver(post_save, sender="accounts.CustomUser", dispatch_uid="employees.sync_designation_history")
def _sync_designation_history(sender, instance, created, **kwargs):
    """Close the prior open DesignationHistory row and open a new one
    whenever ``designation`` actually changes. Covers every path that sets
    it today — profile edits (UserUpdateView), bulk onboarding, and SSO
    auto-provisioning — with no second step for HR to remember.

    A brand-new employee's first row backdates to their joining_date (the
    real "date of commencement on this post") rather than whatever day HR
    happened to save the record; every later change dates from today, the
    date the change was actually made.
    """
    new_designation_id = instance.designation_id
    prev_designation_id = getattr(instance, "_prev_designation_id", None)
    if new_designation_id == prev_designation_id or new_designation_id is None:
        return

    today = timezone.localdate()
    with transaction.atomic():
        current = (
            DesignationHistory.objects
            .select_for_update()
            .filter(employee_id=instance.pk, effective_to__isnull=True)
            .first()
        )
        if current is not None:
            if current.designation_id == new_designation_id:
                return  # already current — defensive, shouldn't happen given the check above
            current.effective_to = max(current.effective_from, today - timedelta(days=1))
            current.save(update_fields=["effective_to", "updated_at"])

        effective_from = today
        reason = DesignationHistory.Reason.CORRECTION
        if prev_designation_id is None:
            reason = DesignationHistory.Reason.INITIAL
            effective_from = instance.joining_date or today

        DesignationHistory.objects.create(
            employee_id=instance.pk,
            designation_id=new_designation_id,
            effective_from=effective_from,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# TeamAssignment — effective-dated team + resource status.
#
# Two senders feed one function because the pair lives on two models:
# `team`/`department` on accounts.CustomUser, `resource_status` on
# employees.JobInfo. A Data Solutions go-live changes both (Trainee -> MARS,
# TRAINEE -> LIVE), usually in one HR action, so the sync reads the *current*
# pair rather than diffing a single field and is a no-op when nothing moved.
# ---------------------------------------------------------------------------

def _current_org_pair(employee):
    """(department_id, team_id, resource_status) as it stands right now."""
    job_info = JobInfo.objects.filter(employee_id=employee.pk).only("resource_status").first()
    return (
        employee.department_id,
        employee.team_id,
        (job_info.resource_status if job_info else "") or "",
    )


def sync_team_assignment(employee, *, reason=None, effective_from=None):
    """Close the open TeamAssignment and open a new one if the employee's
    (department, team, resource status) has actually changed.

    Public so a backfill command or an HR drawer can call it directly, the
    same way the leave app exposes its ledger sync.

    ``effective_from`` defaults to today, matching every existing caller (an
    HR profile edit is dated the moment it's made). Pass an explicit date
    when the real-world change already happened on a known earlier day — the
    Data Solutions go-live bulk uploader is the first caller to do this,
    since a go-live is usually uploaded a few days after the fact and the
    resulting salary proposal needs to be effective from the real date, not
    the upload date (added 2026-09-21).

    A change made on a day whose open row *also* starts on ``effective_from``
    updates that row in place instead of stacking a second one — see
    TeamAssignment's docstring. Without it, a go-live (which touches
    CustomUser and JobInfo separately) would leave a one-day orphan behind.
    """
    department_id, team_id, resource_status = _current_org_pair(employee)
    today = timezone.localdate()
    effective_from = effective_from or today

    with transaction.atomic():
        current = (
            TeamAssignment.objects
            .select_for_update()
            .filter(employee_id=employee.pk, effective_to__isnull=True)
            .first()
        )
        if current is not None and (
            current.department_id == department_id
            and current.team_id == team_id
            and (current.resource_status or "") == resource_status
        ):
            return current  # nothing moved

        # Amend the open row in place rather than stacking a new one when:
        #   * it already starts on effective_from (a second edit the same
        #     day, in the common case where effective_from is today), or
        #   * it has no resource_status yet and only that is being filled in
        #     (JobInfo is often created after CustomUser, so the initial row
        #     is completed rather than superseded — learning someone's status
        #     is not a transfer).
        # Deliberately NOT "the row is still INITIAL": a genuine transfer
        # months after hire must still stack, or the history is lost.
        completing_status = (
            current is not None
            and not (current.resource_status or "")
            and resource_status
            and current.department_id == department_id
            and current.team_id == team_id
        )
        if current is not None and (current.effective_from >= effective_from or completing_status):
            current.department_id = department_id
            current.team_id = team_id
            current.resource_status = resource_status
            if reason:
                current.reason = reason
            current.save(update_fields=[
                "department", "team", "resource_status", "reason", "updated_at",
            ])
            assignment = current

        else:
            new_effective_from = effective_from
            if current is None:
                resolved_reason = reason or TeamAssignment.Reason.INITIAL
                # A first row backdates to joining_date — the date the
                # assignment actually began — matching
                # _sync_designation_history.
                new_effective_from = employee.joining_date or effective_from
            else:
                resolved_reason = reason or TeamAssignment.Reason.TRANSFER
                current.effective_to = max(current.effective_from, effective_from - timedelta(days=1))
                current.save(update_fields=["effective_to", "updated_at"])

            assignment = TeamAssignment.objects.create(
                employee_id=employee.pk,
                department_id=department_id,
                team_id=team_id,
                resource_status=resource_status,
                effective_from=new_effective_from,
                reason=resolved_reason,
            )

    # Outside the atomic block on purpose: a payroll proposal must never be
    # able to roll back the org record of what actually happened. The
    # proposal is idempotent (propose_salary_for_assignment returns the
    # existing PENDING row rather than a second one), so a retry is safe.
    _propose_salary(assignment)
    return assignment


def _propose_salary(assignment):
    """Ask payroll whether this assignment implies a salary revision.

    Imported inside the function: employees must not import payroll at
    module load (payroll already imports employees, and a top-level import
    here would close the cycle). Failures are swallowed with a log rather
    than propagated — HR's profile edit succeeding must not depend on the
    compensation master being configured, and an unproposed revision shows
    up on the run as a SALARY_PACKAGE_MISMATCH check anyway.
    """
    import logging

    try:
        from payroll.services import propose_salary_for_assignment

        propose_salary_for_assignment(assignment)
    except Exception:  # pragma: no cover — defensive, never blocks the HR edit
        logging.getLogger(__name__).exception(
            "Could not propose a salary revision for TeamAssignment %s", assignment.pk,
        )


@receiver(pre_save, sender="accounts.CustomUser", dispatch_uid="employees.stash_prev_org")
def _stash_prev_org(sender, instance, **kwargs):
    if instance._state.adding:
        instance._prev_org = None
        return
    try:
        row = sender.objects.only("department_id", "team_id").get(pk=instance.pk)
        instance._prev_org = (row.department_id, row.team_id)
    except sender.DoesNotExist:
        instance._prev_org = None


@receiver(post_save, sender="accounts.CustomUser", dispatch_uid="employees.sync_team_assignment_user")
def _sync_team_assignment_from_user(sender, instance, created, **kwargs):
    prev = getattr(instance, "_prev_org", None)
    if not created and prev == (instance.department_id, instance.team_id):
        return
    sync_team_assignment(instance)


@receiver(post_save, sender=JobInfo, dispatch_uid="employees.sync_team_assignment_jobinfo")
def _sync_team_assignment_from_job_info(sender, instance: JobInfo, **kwargs):
    if instance.resource_status == getattr(instance, "_prev_resource_status", object()):
        return
    employee = instance.employee
    sync_team_assignment(employee)
