"""One-off: heal AL LeaveBalance rows whose cycle_start/cycle_end never got
populated because `get_or_create_balance` collided with a pre-anniversary-
rework legacy row sharing the same `year` value.

Background: `open_al_cycles.py` (and every other caller of
`get_or_create_balance`) looks up a row by (employee, policy, year). Before
the 2026-09-01 anniversary-cycle rework, AL balances were one row per
employee per *calendar* year. For any employee whose current anniversary
cycle happens to start in a year that already had one of those old rows,
the lookup silently returns the stale row instead of creating a fresh one
— cycle_start/cycle_end are only set in `defaults`, which `get_or_create`
ignores on a match. That leaves `live_accrued_al` permanently reading the
old row's frozen `accrued` (dead since the day the batch job that used to
update it was retired), and `close_al_cycles` can never close/carry-forward
it either (it only selects rows with `cycle_end` set). Found 2026-09-02
auditing Imran Ahmed's (employee 623) allocation — see
docs/LEAVE_POLICY_IMPLEMENTATION.md §5.7.

`leave/services.py::get_or_create_balance` now self-heals this the moment
any code calls it for an affected employee (so it can't recur silently
going forward), but existing collided rows need one pass to catch up. This
command just calls `get_or_create_balance` for every employee with a
stale row — the actual heal logic (and its safety guard: only when the
row's cycle genuinely hasn't ended yet) lives in that one place.

Unlike `open_al_cycles.py`, this is NOT restricted to
`is_active=True` employees — a separated employee's balance can still be
collided and still matters for a manual separation payout.

Usage:
    python manage.py heal_al_cycle_collisions
    python manage.py heal_al_cycle_collisions --dry-run
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from leave.models import LeaveBalance, LeavePolicy
from leave.services import anniversary_cycle_window, get_or_create_balance, is_eligible_for_policy


class Command(BaseCommand):
    help = "One-off: heal AL LeaveBalance rows stuck with a NULL cycle_start due to a legacy-row year collision."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing.")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        today = date.today()

        al_policy = (
            LeavePolicy.objects
            .filter(shift_type__name="AL", shift_type__category="LEAVE")
            .first()
        )
        if al_policy is None:
            self.stderr.write(self.style.ERROR("No AL LeavePolicy found — nothing to do."))
            return
        if al_policy.cycle != LeavePolicy.Cycle.ANNIVERSARY:
            self.stderr.write(self.style.ERROR(
                f"AL LeavePolicy.cycle is {al_policy.cycle!r}, not ANNIVERSARY — aborting."
            ))
            return

        stale = (
            LeaveBalance.objects
            .filter(leave_policy=al_policy, cycle_start__isnull=True)
            .select_related("employee")
        )
        self.stdout.write(f"{stale.count()} AL balance row(s) with no cycle window yet.")
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        healed = no_join = ineligible = mismatched_year = still_past = 0

        for bal in stale.iterator():
            emp = bal.employee
            if not emp.joining_date:
                no_join += 1
                self.stdout.write(self.style.WARNING(f"  ⚠ {emp.email} has no joining_date — skipped"))
                continue

            cycle_start, cycle_end = anniversary_cycle_window(emp.joining_date, today)
            if cycle_start.year != bal.year:
                # A genuinely old, dead legacy row (year predates this
                # employee's current cycle) — left NULL forever, by design.
                mismatched_year += 1
                continue
            if cycle_end <= today:
                # Shouldn't happen given the year match above, but the same
                # guard as the service layer — never resurrect a cycle
                # that's already elapsed.
                still_past += 1
                continue

            ok, reason = is_eligible_for_policy(emp, al_policy, as_of=today)
            if not ok:
                ineligible += 1
                self.stdout.write(f"  ⚠ {emp.email} no longer AL-eligible ({reason}) — left as-is")
                continue

            self.stdout.write(f"  {emp.email}: heal → cycle [{cycle_start}, {cycle_end})")
            if dry:
                healed += 1
                continue

            get_or_create_balance(emp, al_policy, today)
            healed += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {healed} row(s) healed{' (dry run)' if dry else ''}; "
            f"{mismatched_year} genuinely historical (left untouched); "
            f"{ineligible} no longer eligible; {no_join} missing joining_date; "
            f"{still_past} already elapsed."
        ))
