"""Signal handlers for offboarding.

Handlers:

* ``Resignation.pre_save`` — stash previous status for transition detection
* ``Resignation.post_save`` — on create → notify manager + HR; on
  PENDING→ACCEPTED → write LWD through to CustomUser, purge any roster
  shifts past the new LWD, AND create the case
* ``CustomUser.pre_save`` / ``CustomUser.post_save`` — detect a changed
  ``last_working_date`` and purge roster shifts past it. Offboarding owns
  LWD-driven semantics (deactivation already lives here), so the hook does
  too rather than sitting in ``accounts.signals``.
* ``ClearanceStep.post_save`` — recalc case status, notify next assignee,
  and on case completion auto-issue letters + notify employee + HR

Deliberately NO signal flips ``is_active`` — that's the middleware + the
nightly ``deactivate_past_lwd`` celery-beat job only.
A surprising "saving a step logged someone out" failure mode would be hard
to debug and is impossible by construction this way.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from offboarding.models import ClearanceStep, OffboardingCase, Resignation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resignation pre_save — stash previous status
# ---------------------------------------------------------------------------

@receiver(pre_save, sender=Resignation, dispatch_uid="offboarding.stash_prev_status")
def _stash_prev_status(sender, instance: Resignation, **kwargs):
    if not instance.pk:
        instance._prev_status = None
        return
    try:
        instance._prev_status = sender.objects.only("status").get(pk=instance.pk).status
    except sender.DoesNotExist:
        instance._prev_status = None


# ---------------------------------------------------------------------------
# Resignation post_save — submit notification + on accept create case + LWD
# ---------------------------------------------------------------------------

@receiver(post_save, sender=Resignation, dispatch_uid="offboarding.on_resignation_save")
def _on_resignation_save(sender, instance: Resignation, created: bool, **kwargs):
    from offboarding.services import (
        create_case_from_resignation,
        notify_hr_initiated_resignation,
        notify_resignation_submitted,
    )

    if created:
        # Branch on who created the row:
        #  - employee themselves → manager + HR get the "submitted" email
        #  - HR / manager on behalf → employee + manager get the "initiated
        #    by HR" email (HR already knows; they triggered it)
        if instance.applied_by_id == instance.employee_id:
            transaction.on_commit(lambda: notify_resignation_submitted(instance))
        else:
            transaction.on_commit(lambda: notify_hr_initiated_resignation(instance))
        # Fall through: the ACCEPTED-transition branch below may also fire if
        # the row was created already-accepted (HR auto-accept path).

    prev = getattr(instance, "_prev_status", None)
    if prev == instance.status:
        return

    if instance.status == Resignation.Status.ACCEPTED and prev != Resignation.Status.ACCEPTED:
        def _on_accept():
            # 1) Write LWD to CustomUser (single-field update — skip cascading
            #    save signals like _recompute_line_managers that would fire on
            #    a full .save()).
            from django.contrib.auth import get_user_model
            from accounts.models import directory_stamp
            get_user_model().objects.filter(pk=instance.employee_id).update(
                last_working_date=instance.last_working_date,
                **directory_stamp("last_working_date"),
            )
            # 1b) Drop any roster shifts that now fall past the LWD. The
            #     .update() above deliberately bypasses CustomUser signals, so
            #     _purge_shifts_on_lwd_change below never sees this write —
            #     hence the explicit call (and hence no double-purge either).
            #     The in-memory employee still carries the old LWD, so stamp
            #     the new one on before handing it over.
            from roster.services import purge_shifts_after_last_working_date
            employee = instance.employee
            employee.last_working_date = instance.last_working_date
            removed = purge_shifts_after_last_working_date(employee, actor=None)
            if removed:
                logger.info(
                    "Purged %s roster shift(s) past LWD %s for %s",
                    removed, instance.last_working_date, employee.email,
                )
            # 2) Build the case + steps.
            #
            # Swallowing the exception keeps the accept transaction intact,
            # but a case that silently fails to exist is worse than a loud
            # error: the employee still gets deactivated on LWD, with no
            # clearance behind it. Record the failure on the resignation so
            # the "Needs offboarding" detector picks the employee back up
            # instead of the problem living only in the log. (The usual
            # cause is no seeded ClearanceWorkflowTemplate — run
            # manage.py seed_offboarding_workflow.)
            try:
                case = create_case_from_resignation(instance)
                logger.info("Created OffboardingCase %s for %s",
                            case.pk, instance.employee.email)
            except Exception:
                logger.exception("Failed to create OffboardingCase for %s",
                                 instance.employee.email)
                Resignation.objects.filter(pk=instance.pk).update(
                    hr_notes=(
                        (instance.hr_notes + "\n\n") if instance.hr_notes else ""
                    ) + (
                        "[system] Clearance case could not be created on accept — "
                        "no clearance workflow is seeded, or step assignment failed. "
                        "This employee has no clearance workflow behind their LWD."
                    ),
                )
        transaction.on_commit(_on_accept)


# ---------------------------------------------------------------------------
# CustomUser pre_save/post_save — purge roster shifts past a changed LWD
#
# Covers the paths that go through a normal ``instance.save()``: the HR edit
# form (accounts.views.UserUpdateView) and the Django admin Employment
# fieldset. The raw ``.update()`` writers (the resignation-accept branch
# above, the middleware, the nightly deactivation batch) don't fire post_save
# at all, so each of those calls the purge itself — no path double-purges.
#
# Same pre_save-stash shape as ``_stash_prev_status``.
# ---------------------------------------------------------------------------

@receiver(pre_save, sender=settings.AUTH_USER_MODEL,
          dispatch_uid="offboarding.stash_prev_lwd")
def _stash_prev_lwd(sender, instance, **kwargs):
    if not instance.pk:
        instance._prev_last_working_date = None
        return
    try:
        instance._prev_last_working_date = (
            sender.objects.only("last_working_date").get(pk=instance.pk).last_working_date
        )
    except sender.DoesNotExist:
        instance._prev_last_working_date = None


@receiver(post_save, sender=settings.AUTH_USER_MODEL,
          dispatch_uid="offboarding.purge_shifts_on_lwd_change")
def _purge_shifts_on_lwd_change(sender, instance, created: bool, **kwargs):
    """An LWD that was just set (or moved earlier) invalidates every roster
    shift beyond it. Clearing the LWD is intentionally a no-op — the weekly
    ``Roster`` pattern is never deleted, so re-opening the week grid
    re-materialises the shifts on its own.

    Safe against recursion: the purge only deletes ``Shift`` rows, it never
    saves a ``CustomUser``.
    """
    if created:
        return
    prev = getattr(instance, "_prev_last_working_date", None)
    new = instance.last_working_date
    if new is None or prev == new:
        return

    from roster.services import purge_shifts_after_last_working_date
    removed = purge_shifts_after_last_working_date(instance, actor=None)
    if removed:
        logger.info(
            "Purged %s roster shift(s) past LWD %s for %s",
            removed, new, instance.email,
        )


# ---------------------------------------------------------------------------
# ClearanceStep post_save — recalc case status, notify, auto-issue letters
# ---------------------------------------------------------------------------

@receiver(post_save, sender=ClearanceStep, dispatch_uid="offboarding.on_step_save")
def _on_step_save(sender, instance: ClearanceStep, created: bool, **kwargs):
    from offboarding.services import (
        auto_issue_letters,
        notify_case_completed,
        notify_step_assigned,
        refresh_case_status,
    )

    if created:
        # Initial creation as part of case build — notify only the FIRST step
        # (order=1). The rest will get notified as they become the next-pending.
        if instance.order == 1:
            transaction.on_commit(lambda: notify_step_assigned(instance))
        return

    if instance.status not in (
        ClearanceStep.Status.DONE,
        ClearanceStep.Status.SKIPPED,
        ClearanceStep.Status.REJECTED,
    ):
        return

    def _after_commit():
        case = OffboardingCase.objects.get(pk=instance.case_id)
        new_status = refresh_case_status(case)

        # Notify the next assignee (if the case is still in progress).
        if new_status == OffboardingCase.Status.IN_PROGRESS:
            next_step = case.steps.filter(
                order__gt=instance.order,
                status=ClearanceStep.Status.PENDING,
            ).order_by("order").first()
            if next_step:
                notify_step_assigned(next_step)

        # On final completion: trigger letter issuance + email employee + HR.
        if new_status == OffboardingCase.Status.COMPLETED:
            should_trigger_letters = (
                instance.template is not None
                and instance.template.triggers_letters
            )
            if should_trigger_letters:
                try:
                    auto_issue_letters(case)
                except Exception:
                    logger.exception("auto_issue_letters failed for case %s", case.pk)
            notify_case_completed(case)

    transaction.on_commit(_after_commit)
