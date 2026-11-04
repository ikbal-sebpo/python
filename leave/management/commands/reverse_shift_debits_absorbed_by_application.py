"""One-off: credit back roster-direct leave debits left stranded by the
double-debit bug fixed 2026-09-22 (see docs/LEAVE_POLICY_IMPLEMENTATION.md).

Background: a supervisor setting a Shift directly to a leave type (e.g. SL,
someone calling in sick) debits the ledger synchronously via
leave/signals.py::sync_shift_leave_ledger, with the transaction linked via
`LeaveBalanceTransaction.shift`. If the employee later filed the matching
LeaveApplication for the same day and it was approved,
leave/services.py::_reconcile_application wrote a *second*, independent
debit linked via `LeaveBalanceTransaction.application` — before this fix,
nothing checked whether the day was already covered by the roster-direct
debit, and materialise_shifts_for_application claiming the Shift row (by
setting `Shift.leave_application`) never reversed the stale one.

_reconcile_application now absorbs (reverses) any such stranded shift-linked
debit before writing its own, going forward — see
leave/services.py::_absorb_shift_debits. This command is the one-time sweep
for stranded debits written before that fix: any shift-linked USED
transaction whose Shift has since been claimed by a LeaveApplication
(`Shift.leave_application` is set) is, by definition, a day now also
covered by that application's own debit — a leftover duplicate.

Idempotent: reversing a transaction frees its `shift` FK (via
reverse_shift_ledger_transaction), so a row already reversed no longer
matches the query on a second run.

Dry-run by default.

Usage:
    python manage.py reverse_shift_debits_absorbed_by_application --dry-run
    python manage.py reverse_shift_debits_absorbed_by_application --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from leave.models import LeaveBalanceTransaction
from leave.services import reverse_shift_ledger_transaction

CORRECTION_NOTE = "Absorbed into LeaveApplication on approval (retroactive repair, 2026-09-22)"


class Command(BaseCommand):
    help = (
        "One-off: credit back roster-direct leave debits that were left stranded "
        "alongside a LeaveApplication's own debit for the same day. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually write (default: dry-run).")

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))

        stranded = (
            LeaveBalanceTransaction.objects
            .filter(
                reason=LeaveBalanceTransaction.Reason.USED,
                application__isnull=True,
                shift__isnull=False,
                shift__leave_application__isnull=False,
            )
            .select_related(
                "balance__employee", "balance__leave_policy__shift_type",
                "shift", "shift__leave_application",
            )
            .order_by("shift__date")
        )
        count = stranded.count()
        self.stdout.write(f"{count} stranded roster-direct debit(s) found.")
        if not apply_changes:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes. Pass --apply to write."))

        total_credit = 0
        for txn in stranded:
            emp = txn.balance.employee
            self.stdout.write(
                f"  {emp.employee_id} {emp.email} "
                f"{txn.balance.leave_policy.shift_type.name} {txn.shift.date}: "
                f"Shift #{txn.shift_id} claimed by Leave #{txn.shift.leave_application_id} "
                f"-> credit {-txn.delta:+}"
            )
            total_credit += -txn.delta
            if not apply_changes:
                continue
            reverse_shift_ledger_transaction(txn, note=CORRECTION_NOTE)

        self.stdout.write(self.style.SUCCESS(
            f"{'Credited' if apply_changes else 'Would credit'} "
            f"{total_credit:+} day(s) total across {count} transaction(s)."
        ))
