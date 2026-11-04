"""One-off: credit back leave-ledger debits left behind by LeaveApplication
rows that were later hard-deleted from the database (found while testing
the sandwich-billing calculation — those test applications were deleted
afterward, but the ledger entries they wrote were not reversed first).

Background: `consume_balance`, `restore_balance`, and `_reconcile_application`
(`leave/services.py`) always create their `LeaveBalanceTransaction` with
`application=application` set and `shift` left unset (application-driven
debits are never shift-linked). `LeaveApplication` is `on_delete=SET_NULL`
on that FK, so deleting an application doesn't delete its transactions --
it just nulls the link, leaving the debit (or, if the application was
properly cancelled first via `restore_balance`, the debit *and* its
matching refund) stranded with no application to point back to.

A transaction can only be identified as having originated from one of
those three call sites by its `note`, since that's the one field they set
distinctly and that survives the FK going null:
  - `consume_balance`            -> blank note
  - `restore_balance`            -> "Cancelled / declined"
  - `_reconcile_application`     -> "Sandwich recompute (additional debit)"
                                     or "Sandwich recompute (refund)"
A transaction from `leave/signals.py::_reverse_shift_ledger_transaction`
(this app's *other* orphan-reversal path, for bulk-retyped Shifts) also has
`shift=None` but was created that way on purpose, with its own distinct
note ("Shift #... edited/deleted") -- never matches the notes above, so it
is never touched here.

Corrected by NET per balance, not by reversing every individual orphaned
transaction: a debit that was properly cancelled-and-refunded via
`restore_balance` before its application was deleted already nets to zero
in the ledger, and reversing it again would double-credit the employee. So
this sums every orphaned transaction per `LeaveBalance` and writes one
`ADJUSTMENT` entry only where that net is nonzero -- crediting back exactly
what's still outstanding.

Idempotent via a marker note: a balance that already has an ADJUSTMENT
transaction with this command's note is skipped on a later run (its
original orphaned USED/REVERSED transactions are still sitting in the
table by design -- this is an additive correction, not a deletion -- so
without this check a second run would recompute the same net and credit it
a second time).

Dry-run by default.

Usage:
    python manage.py reverse_orphaned_application_transactions --dry-run
    python manage.py reverse_orphaned_application_transactions --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q, Sum, F

from leave.models import LeaveBalance, LeaveBalanceTransaction

APP_DRIVEN_NOTES = (
    Q(note="")
    | Q(note="Cancelled / declined")
    | Q(note__startswith="Sandwich recompute")
)
CORRECTION_NOTE = "Correcting orphaned debit from a deleted (sandwich-calculation test) LeaveApplication"


class Command(BaseCommand):
    help = (
        "One-off: credit back the net leftover debit from LeaveBalanceTransaction "
        "rows whose LeaveApplication was later hard-deleted. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually write (default: dry-run).")

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))

        orphaned = (
            LeaveBalanceTransaction.objects
            .filter(
                shift__isnull=True,
                application__isnull=True,
                reason__in=[
                    LeaveBalanceTransaction.Reason.USED,
                    LeaveBalanceTransaction.Reason.REVERSED,
                ],
            )
            .filter(APP_DRIVEN_NOTES)
        )
        self.stdout.write(f"{orphaned.count()} orphaned application-driven transaction(s) found.")
        if not apply_changes:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes. Pass --apply to write."))

        already_corrected = set(
            LeaveBalanceTransaction.objects
            .filter(reason=LeaveBalanceTransaction.Reason.ADJUSTMENT, note=CORRECTION_NOTE)
            .values_list("balance_id", flat=True)
        )

        by_balance = (
            orphaned.values("balance_id")
            .annotate(net=Sum("delta"))
        )
        to_correct = [
            row for row in by_balance
            if row["net"] != 0 and row["balance_id"] not in already_corrected
        ]
        skipped_already_corrected = sum(1 for row in by_balance if row["balance_id"] in already_corrected)
        already_net_zero = by_balance.count() - len(to_correct) - skipped_already_corrected

        self.stdout.write(
            f"{len(to_correct)} balance(s) need a correction; "
            f"{already_net_zero} balance(s) already net to zero (cancelled before deletion — left alone); "
            f"{skipped_already_corrected} balance(s) already corrected by a prior run — skipped."
        )

        balances = {
            bal.pk: bal
            for bal in LeaveBalance.objects
                .filter(pk__in=[row["balance_id"] for row in to_correct])
                .select_related("employee", "leave_policy__shift_type")
        }

        total_credit = 0
        for row in sorted(to_correct, key=lambda r: r["balance_id"]):
            bal = balances[row["balance_id"]]
            net = row["net"]
            credit = -net
            total_credit += credit
            self.stdout.write(
                f"  {bal.employee.employee_id} {bal.employee.email} "
                f"{bal.leave_policy.shift_type.name} {bal.year}: "
                f"net={net:+} -> credit {credit:+}"
            )
            if not apply_changes:
                continue
            with transaction.atomic():
                LeaveBalanceTransaction.objects.create(
                    balance=bal,
                    delta=credit,
                    reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
                    created_by=None,
                    note=CORRECTION_NOTE,
                )
                bal.used = F("used") - credit
                bal.save(update_fields=["used", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"{'Credited' if apply_changes else 'Would credit'} "
            f"{total_credit:+} day(s) total across {len(to_correct)} balance(s)."
        ))
