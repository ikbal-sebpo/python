"""Leave signals.

- On Shift save: if the shift is WORK-category on a Holiday-tagged date AND
  the employee's department/team is AUTO-treatment (see
  accounts.permissions.effective_cpl_treatment), the employee earns +1 day
  in the CpL balance, and a leave.CplGrant history row is created. MANUAL
  departments (e.g. Data Solutions & Due Diligence) never auto-grant — CpL
  only enters the ledger via the HR upload; NONE departments (e.g. Digital
  Advertising) never get CpL at all. Idempotent.

- On Holiday save / delete: re-evaluate sandwich billing for any APPROVED
  LeaveApplication overlapping the affected date. Necessary because adding
  or removing a Holiday changes whether dates are working / non-working,
  which in turn changes the sandwich classification.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import F
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from leave.models import (
    CplGrant,
    LeaveBalanceTransaction,
    LeavePolicy,
)
from leave.services import (
    CPL_GRANT_NOTE_FMT,
    grant_or_backfill_cpl_grant,
    reverse_shift_ledger_transaction,
)


@receiver(post_save, sender="roster.Shift")
def grant_cpl_on_holiday_work(sender, instance, created, **kwargs):
    """Whenever a WORK-category Shift exists on a Holiday-tagged date, that
    employee earns +1 day on the CpL balance — but only for a department/
    team whose CpL treatment is AUTO (see
    `accounts.permissions.effective_cpl_treatment`). A MANUAL department
    (e.g. Data Solutions & Due Diligence) decides CpL vs. overtime per
    employee/instance outside Orbis and never auto-grants; a NONE
    department (e.g. Digital Advertising) never gets CpL at all.

    Idempotent — see `leave/services.py::grant_or_backfill_cpl_grant`."""
    shift_type = instance.shift_type
    if shift_type is None or shift_type.category != "WORK":
        return

    # Avoid circular imports — pull Holiday lazily.
    from roster.models import Holiday

    holiday = Holiday.objects.filter(date=instance.date, is_active=True).first()
    if holiday is None:
        return

    from accounts.permissions import effective_cpl_treatment

    if effective_cpl_treatment(instance.employee) != "AUTO":
        return

    cpl_policy = (
        LeavePolicy.objects
        .filter(cpl_grant_on_holiday_work=True)
        .select_related("shift_type")
        .first()
    )
    if cpl_policy is None:
        return

    grant_or_backfill_cpl_grant(
        instance, holiday, cpl_policy,
        created_by=instance.updated_by_id and instance.updated_by,
    )


@receiver(post_delete, sender="roster.Shift")
def revoke_cpl_on_shift_delete(sender, instance, **kwargs):
    """If a WORK Shift on a Holiday date is deleted, revoke the corresponding
    CpL grant. Defensive — the original grant note pins the link (by the
    time this post_delete signal fires, `CplGrant.work_shift` is already
    nulled by the SET_NULL collector, so it can't be used to find the row)."""
    cpl_policy = LeavePolicy.objects.filter(cpl_grant_on_holiday_work=True).first()
    if cpl_policy is None:
        return

    note = CPL_GRANT_NOTE_FMT.format(shift_pk=instance.pk)
    txn = LeaveBalanceTransaction.objects.filter(
        balance__employee=instance.employee,
        balance__leave_policy=cpl_policy,
        reason=LeaveBalanceTransaction.Reason.ACCRUAL,
        note=note,
    ).first()
    if txn is None:
        return

    with transaction.atomic():
        bal = txn.balance
        # Write a reversing ADJUSTMENT rather than deleting, so the ledger
        # stays append-only and auditable.
        LeaveBalanceTransaction.objects.create(
            balance=bal,
            delta=-txn.delta,
            reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
            application=None,
            created_by=None,
            note=f"Reverse {note} (Shift removed)",
        )
        bal.accrued = F("accrued") - txn.delta
        bal.save(update_fields=["accrued", "updated_at"])

        grant = CplGrant.objects.filter(accrual_transaction=txn).first()
        if grant is not None and grant.status == CplGrant.Status.OPEN:
            grant.status = CplGrant.Status.FORFEITED
            grant.work_shift = None
            grant.note = (
                (grant.note + " " if grant.note else "") + "Forfeited: work Shift deleted."
            )
            grant.save(update_fields=["status", "work_shift", "note", "updated_at"])


@receiver(post_save, sender="roster.Shift")
def sync_shift_leave_ledger(sender, instance, created, **kwargs):
    """Keep the leave ledger in sync with this Shift's *current* state,
    synchronously, on every save — this is what makes roster-direct leave
    (a supervisor setting a shift to AL/SL/etc. directly, no
    LeaveApplication) show up on the balance page immediately instead of
    waiting for the nightly reconcile_shift_leave_usage batch (which
    remains as a catch-up for anything written via bulk_create, which
    bypasses signals entirely — bulk roster uploads do this on purpose).

    Two things can be true on any given save:
    1. A previously-reconciled Shift was edited away from its leave type
       (changed to a different leave type, or to WORK/OFF/HOLIDAY) — the
       stale debit must be reversed.
    2. The Shift is now (still, or newly) a LEAVE-category shift with no
       LeaveApplication and no existing debit — it must be reconciled.

    A LeaveApplication-backed Shift is untouched by either path —
    LeaveApplication edits go through leave/services.py::
    _reconcile_application instead (reconcile_single_shift already skips
    any Shift with leave_application_id set)."""
    existing_txn = (
        LeaveBalanceTransaction.objects
        .filter(shift=instance, reason=LeaveBalanceTransaction.Reason.USED)
        .select_related("balance", "balance__leave_policy")
        .first()
    )
    current_policy = getattr(instance.shift_type, "leave_policy", None) if instance.shift_type_id else None

    if existing_txn is not None and (
        current_policy is None or current_policy.pk != existing_txn.balance.leave_policy_id
    ):
        with transaction.atomic():
            reverse_shift_ledger_transaction(
                existing_txn, note=f"Shift #{instance.pk} edited away from its reconciled leave type",
            )
        existing_txn = None  # freed — eligible for fresh reconciliation below if it moved to another leave type

    if existing_txn is None and current_policy is not None:
        from leave.services import reconcile_single_shift  # avoid import-order issues at app load
        reconcile_single_shift(instance, created_by=instance.updated_by or instance.added_by)


@receiver(pre_delete, sender="roster.Shift")
def reverse_shift_ledger_on_delete(sender, instance, **kwargs):
    """Mirror of reverse_shift_ledger_on_retype for deletion. Must be
    pre_delete, not post_delete: `LeaveBalanceTransaction.shift` is
    on_delete=SET_NULL, so by post_delete time the FK we filter on below
    would already be nulled."""
    txn = (
        LeaveBalanceTransaction.objects
        .filter(shift=instance, reason=LeaveBalanceTransaction.Reason.USED)
        .select_related("balance")
        .first()
    )
    if txn is None:
        return

    with transaction.atomic():
        reverse_shift_ledger_transaction(txn, note=f"Shift #{instance.pk} deleted")


@receiver(post_save, sender="roster.Holiday")
def recompute_after_holiday_change(sender, instance, **kwargs):
    """A new (or modified) Holiday changes the working-day status of its date,
    which can flip whether other approved leaves' dates count as edge or
    sandwich. The actual walk (recompute_sandwich per affected employee) is
    deferred to the task queue — it can be a long fan-out and must not block
    the admin request that saved the Holiday."""
    from core.tasks import defer
    from leave.services import invalidate_holiday_cache
    from leave.tasks import recompute_holiday_window

    # Must happen before the recompute — it reads the cached holiday set.
    invalidate_holiday_cache()

    date_iso = instance.date.isoformat()
    transaction.on_commit(lambda: defer(recompute_holiday_window, date_iso))


@receiver(post_delete, sender="roster.Holiday")
def recompute_after_holiday_delete(sender, instance, **kwargs):
    """Mirror of recompute_after_holiday_change for deletion."""
    recompute_after_holiday_change(sender, instance, **kwargs)
