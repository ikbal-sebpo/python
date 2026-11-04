"""Catch-up reconciliation for LEAVE-category Shift rows written via
bulk_create (bulk roster uploads bypass Django signals on purpose, for
performance) — everything a normal `.save()` handles synchronously via
leave/signals.py::sync_shift_leave_ledger. Two independent passes, both
idempotent, safe to re-run:

1. **Forward** (`reconcile_orphan_shift_usage`): debit LeaveBalance for a
   LEAVE-category Shift that never went through a LeaveApplication and has
   no ledger transaction yet — the actual bulk of real leave usage in this
   org (see docs/LEAVE_POLICY_IMPLEMENTATION.md, "Risks worth calling out").
   Idempotent via `LeaveBalanceTransaction.shift` (uniquely enforced).

2. **Reversal** (`reconcile_stale_shift_leave_reversals`): reverse a USED
   debit whose Shift has since been bulk-retyped away from that leave policy
   (e.g. a roster upload changes a reconciled CL day to a worked shift).
   Idempotent — the reversal frees the original transaction's `shift` FK, so
   it can't be reversed twice.

ALWAYS run --dry-run first and review the per-(policy, year) delta report —
some years may already have manual ADJUSTMENT transactions HR used to patch
around this exact gap, and blind unbounded reconciliation risks double-
counting against those. Use --since to bound the first real run to a
deliberate cutover date rather than reconciling all of history at once.

Runs nightly, before close_al_cycles.py (AL's carry-forward math needs an
accurate `used` figure). Scheduled as the "Reconcile shift leave usage"
periodic task (00:45 Asia/Dhaka — a django_celery_beat row, see
docs/SCHEDULER_ADMIN_PLAN.md).

Usage:
    python manage.py reconcile_shift_leave_usage --dry-run
    python manage.py reconcile_shift_leave_usage --since 2026-01-01
    python manage.py reconcile_shift_leave_usage --since 2026-01-01 --upto 2026-08-31
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from leave.services import (
    reconcile_orphan_shift_usage,
    reconcile_stale_shift_leave_reversals,
)


class Command(BaseCommand):
    help = "Reconcile bulk_create-written LEAVE Shift rows into the LeaveBalance ledger (debits + stale reversals)."

    def add_arguments(self, parser):
        parser.add_argument("--since", type=str, default=None, help="Only Shift rows on/after this date (YYYY-MM-DD).")
        parser.add_argument("--upto", type=str, default=None, help="Only Shift rows on/before this date (YYYY-MM-DD). Default: today.")
        parser.add_argument("--dry-run", action="store_true", help="Tally deltas without writing.")

    def handle(self, *args, **opts):
        since = date.fromisoformat(opts["since"]) if opts.get("since") else None
        upto = date.fromisoformat(opts["upto"]) if opts.get("upto") else date.today()
        dry = bool(opts.get("dry_run"))

        self.stdout.write(
            f"Reconciling bulk_create-written LEAVE Shift rows: "
            f"{since or 'the beginning of history'} → {upto}"
        )
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        result = reconcile_orphan_shift_usage(since=since, upto=upto, dry_run=dry)

        self.stdout.write("")
        self.stdout.write("New debits by (leave type, year):")
        for (name, year), delta in sorted(result["by_policy_year"].items()):
            self.stdout.write(f"  {name} {year}: {delta}")

        self.stdout.write(self.style.SUCCESS(
            f"{result['processed']} Shift row(s) "
            f"{'would be ' if dry else ''}reconciled; "
            f"{result['skipped_untracked']} skipped (unpaid/no-quota policy)."
        ))

        reversal_result = reconcile_stale_shift_leave_reversals(since=since, upto=upto, dry_run=dry)

        self.stdout.write("")
        self.stdout.write("Stale reversals by (leave type, year):")
        for (name, year), credit in sorted(reversal_result["by_policy_year"].items()):
            self.stdout.write(f"  {name} {year}: +{credit}")

        self.stdout.write(self.style.SUCCESS(
            f"{reversal_result['reversed']} stale debit(s) "
            f"{'would be ' if dry else ''}reversed."
        ))
