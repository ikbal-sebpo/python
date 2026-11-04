"""One-off: delete AL LeaveBalance rows that are dead legacy leftovers from
before the 2026-09-01 anniversary-cycle rework.

Background: before that date, AL accrued via the same monthly-batch job as
CL/SL, one row per employee per *calendar* year. The rework moved each
employee's live accrual to a separately-keyed anniversary-cycle row without
converting the old one, so the old row is left behind holding whatever
`accrued`/`used` it had at cutover (e.g. two more months of "Monthly
accrual" credit from July/August 2026). `leave/management/commands/
heal_al_cycle_collisions.py` (run 2026-09-02) already found these rows and
correctly left them alone as "genuinely historical" — see
docs/LEAVE_POLICY_IMPLEMENTATION.md §5.7.

Traced `leave/services.py::live_accrued_al`/`live_used`/`live_available`:
while a cycle is open they never read `bal.accrued`, so these rows are
currently inert. But `get_or_create_balance`'s self-heal matches purely on
`(employee, policy, year)` — the moment an employee's *next* cycle happens
to key to the same year as one of these dead rows, it will silently reuse
it, carrying the residual numeric fields into a live cycle (harmless to the
final `available` figure, since close_al_cycles overwrites rather than
adds, but it understates that one closing transaction's delta). Rather than
wait for that and rely on numeric fields being harmless, this deletes them
outright — confirmed with the user that the data being removed is dead and
superseded by the rework, not real current entitlement.

Selection is intentionally independent of `heal_al_cycle_collisions`'s
run — it doesn't just delete "whatever's still stale after healing", since
a row can be left with `cycle_start IS NULL` for a reason other than being
dead (an employee who no longer clears the 12-month eligibility gate, e.g.
after a joining_date correction, is left alone too and must NOT be
deleted). A row only qualifies here if it fails the same test
`heal_al_cycle_collisions` uses for its own "genuinely historical" bucket:
the employee's *actual current* anniversary cycle (computed fresh, right
now) keys to a different year than this row's `year`.

A qualifying row is further split by whether it carries a real `used`
debit: a row with `used=0` is a stranded ACCRUAL-only leftover from the
retired monthly-batch job (safe to delete — nothing reads it, and it was
never real leave taken). A row with `used!=0` also carries a real
application-driven debit (e.g. an old "Sandwich recompute" billing event)
-- a different kind of history, not deleted here, listed separately for a
human to review.

`LeaveBalanceTransaction.balance` is `on_delete=CASCADE`, so deleting a row
also deletes its transactions (this is exactly the point — that ledger
history is what's confirmed dead/superseded, not real current-cycle usage).

Dry-run by default.

Usage:
    python manage.py purge_dead_al_legacy_rows --dry-run
    python manage.py purge_dead_al_legacy_rows --apply
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leave.models import LeaveBalance, LeaveBalanceTransaction, LeavePolicy
from leave.services import anniversary_cycle_window


class Command(BaseCommand):
    help = "One-off: delete dead pre-anniversary-rework legacy AL LeaveBalance rows. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run).")

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        today = date.today()

        al_policy = (
            LeavePolicy.objects
            .filter(shift_type__name="AL", shift_type__category="LEAVE")
            .first()
        )
        if al_policy is None:
            raise CommandError("No AL LeavePolicy found — nothing to do.")
        if al_policy.cycle != LeavePolicy.Cycle.ANNIVERSARY:
            raise CommandError(f"AL LeavePolicy.cycle is {al_policy.cycle!r}, not ANNIVERSARY — aborting.")

        stale = (
            LeaveBalance.objects
            .filter(leave_policy=al_policy, cycle_start__isnull=True)
            .select_related("employee")
        )
        self.stdout.write(f"{stale.count()} AL balance row(s) with no cycle window.")
        if not apply_changes:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes. Pass --apply to delete."))

        dead: list[LeaveBalance] = []
        needs_review: list[LeaveBalance] = []
        no_join = live_or_ineligible = 0

        for bal in stale.iterator():
            emp = bal.employee
            if not emp.joining_date:
                no_join += 1
                continue
            cycle_start, _cycle_end = anniversary_cycle_window(emp.joining_date, today)
            if cycle_start.year == bal.year:
                # Either this employee's genuine current cycle (should have
                # just been healed by heal_al_cycle_collisions) or a
                # currently-ineligible employee's placeholder — neither is
                # dead. Leave it alone.
                live_or_ineligible += 1
                continue
            if bal.used:
                # A stranded ACCRUAL-only row is moot leftover from the
                # retired monthly-batch job. A row that also carries a real
                # USED debit (e.g. an old application-driven "Sandwich
                # recompute" billing event) is a different kind of history
                # -- not touched here, needs a human look before deciding.
                needs_review.append(bal)
                continue
            dead.append(bal)

        txn_count = LeaveBalanceTransaction.objects.filter(balance__in=dead).count()
        self.stdout.write(
            f"{len(dead)} genuinely dead accrual-only row(s) to delete "
            f"(cascading {txn_count} transaction(s)); "
            f"{len(needs_review)} left alone (carry a real USED debit -- needs manual review); "
            f"{live_or_ineligible} left alone (current cycle or ineligible); "
            f"{no_join} skipped (no joining_date)."
        )
        for bal in dead:
            self.stdout.write(
                f"  {bal.employee.employee_id} {bal.employee.email} year={bal.year} "
                f"opening={bal.opening} accrued={bal.accrued} used={bal.used} "
                f"carried_forward={bal.carried_forward}"
            )
        if needs_review:
            self.stdout.write(self.style.WARNING("\nNeeds manual review (not deleted):"))
            for bal in needs_review:
                self.stdout.write(
                    f"  {bal.employee.employee_id} {bal.employee.email} year={bal.year} "
                    f"opening={bal.opening} accrued={bal.accrued} used={bal.used} "
                    f"carried_forward={bal.carried_forward}"
                )

        if not apply_changes:
            return

        with transaction.atomic():
            LeaveBalance.objects.filter(pk__in=[b.pk for b in dead]).delete()

        self.stdout.write(self.style.SUCCESS(f"Deleted {len(dead)} dead AL balance row(s)."))
