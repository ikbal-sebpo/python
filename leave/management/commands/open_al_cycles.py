"""Open every ANNIVERSARY-cycle-eligible employee's *current* cycle
LeaveBalance row (AL today; any future ANNIVERSARY-cycle policy for free).

Runs nightly as the "Open AL cycles" periodic task (01:00 Asia/Dhaka, right
before "Close AL cycles" — django_celery_beat rows, see
docs/SCHEDULER_ADMIN_PLAN.md) so that an employee crossing a policy's eligibility gate
gets their first cycle row automatically — no manual step, and no
dependence on someone happening to visit their balance page, apply-leave
form, or an encashment/payroll-report path first (those all *also*
lazily call `get_or_create_balance`, but nothing guaranteed they'd fire
promptly). Renamed from `open_initial_al_cycles` 2026-09-02 when it moved
from a one-off rollout script to a recurring job — see
docs/LEAVE_POLICY_IMPLEMENTATION.md §5.7.

Selects policies by `cycle=ANNIVERSARY`, not by shift-type name — mirrors
`close_al_cycles.py`'s own selection and sidesteps the short-code-vs-
long-name pitfall `seed_leave_policies.py` documents (ShiftTypes are named
"AL" in production, but "Annual Leave" in some environments' fixtures).

For every (eligible employee, ANNIVERSARY policy) pair, this computes
`anniversary_cycle_window(joining_date, today)` and creates the current
cycle's row via `get_or_create_balance` — anchored on `joining_date` +
*today*, not on any prior LeaveBalance.year, so it correctly reconstructs
"which cycle is this employee in right now" regardless of how many
anniversaries elapsed while the old calendar-year system was in use.

Idempotent: an employee who already has their current cycle's row is a
no-op (`get_or_create_balance` just returns it — self-healing a NULL
`cycle_start` on a colliding legacy row if it finds one, see that
function's docstring). Safe to run nightly forever, and safe to run by
hand too.

Deliberately does NOT backfill `accrued`/`used` for the already-elapsed
part of the current cycle from history — a newly-opened cycle starts
clean from today forward. Pre-cutover totals are what the historical bank
upload (leave/upload_bank.py) captures instead, via `LeaveBalance.opening`.

Usage:
    python manage.py open_al_cycles
    python manage.py open_al_cycles --dry-run
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from accounts.models import CustomUser
from leave.models import LeaveBalance, LeavePolicy
from leave.services import anniversary_cycle_window, get_or_create_balance, is_eligible_for_policy


class Command(BaseCommand):
    help = "Open every ANNIVERSARY-cycle policy's current cycle LeaveBalance row for eligible employees. Runs nightly; safe to run by hand too."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing.")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        today = date.today()

        policies = list(
            LeavePolicy.objects
            .filter(cycle=LeavePolicy.Cycle.ANNIVERSARY)
            .select_related("shift_type")
        )
        if not policies:
            self.stdout.write("No ANNIVERSARY-cycle policies. Nothing to do.")
            return

        opened = 0
        skipped_ineligible = 0
        skipped_no_join_date = 0

        for policy in policies:
            for employee in CustomUser.objects.filter(is_active=True):
                ok, _reason = is_eligible_for_policy(employee, policy, as_of=today)
                if not ok:
                    skipped_ineligible += 1
                    continue
                if not employee.joining_date:
                    skipped_no_join_date += 1
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠ {employee.email} is {policy.shift_type.name}-eligible but has "
                        f"no joining_date — skipped"
                    ))
                    continue

                cycle_start, cycle_end = anniversary_cycle_window(employee.joining_date, today)
                # A properly cycle-tracked row already existing is a no-op,
                # not something worth counting as "opened" — check first so
                # the summary reflects rows that actually needed opening or
                # healing, in both dry-run and real runs (dry-run used to
                # always report 0 regardless, since the counter only
                # incremented past the `continue` below).
                already_open = LeaveBalance.objects.filter(
                    employee=employee, leave_policy=policy,
                    year=cycle_start.year, cycle_start__isnull=False,
                ).exists()
                if dry:
                    if not already_open:
                        self.stdout.write(
                            f"  {employee.email} · {policy.shift_type.name}: open → cycle [{cycle_start}, {cycle_end})"
                        )
                        opened += 1
                    continue

                get_or_create_balance(employee, policy, today)
                if not already_open:
                    opened += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {opened} cycle(s) newly opened/healed{' (dry run)' if dry else ''}; "
            f"{skipped_ineligible} not yet eligible; "
            f"{skipped_no_join_date} missing joining_date."
        ))
