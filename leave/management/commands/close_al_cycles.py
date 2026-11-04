"""Per-employee anniversary-cycle close for AL (and any other ANNIVERSARY-
cycle policy with carry_forward_pct > 0).

Anniversaries are scattered across the calendar (unlike CL/SL's shared
Jan-1 close), so this scans nightly rather than running once a year like
`apply_year_end_rollover.py`. Reuses that command's exact carry-forward /
freeze formula — the only real differences are (a) the selection query is
"whichever employee's cycle just ended today", not "everyone, on Jan 1",
(b) no EncashmentRequest is ever created here in practice, because AL's
`encash_carry_forward_excess` is False (HR confirmed: nothing is cashed —
unused beyond the carry-forward cap is simply forfeited; the excess-for-
encash branch below is kept for structural parity with the old command in
case a future ANNIVERSARY policy ever needs it, but is inert while the flag
is False), and (c) `accrued` isn't read from a monthly-batch-updated field —
it's computed live (`leave/services.py::live_accrued_al`) and persisted
*once*, right here, as this cycle's final frozen number. Live-for-the-open-
cycle / frozen-once-closed is the whole design: `apply_monthly_accrual.py`
no longer processes ANNIVERSARY policies at all (see its own docstring).

Run this AFTER `reconcile_shift_leave_usage` each night, so `used` reflects
real Shift-recorded leave before the carry-forward math reads it — see
docs/LEAVE_POLICY_IMPLEMENTATION.md.

Usage:
    python manage.py close_al_cycles
    python manage.py close_al_cycles --dry-run
    python manage.py close_al_cycles --upto 2026-09-01   # process as of a given date (testing)
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from leave.models import EncashmentRequest, LeaveBalance, LeaveBalanceTransaction, LeavePolicy
from leave.services import get_or_create_balance, live_accrued_al, live_used, reconcile_orphan_shift_usage


class Command(BaseCommand):
    help = "Close AL (ANNIVERSARY-cycle) LeaveBalance rows whose cycle has ended, per employee."

    def add_arguments(self, parser):
        parser.add_argument(
            "--upto",
            type=str,
            default=None,
            help="Process cycles ending on/before this date (YYYY-MM-DD). Default: today.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without writing.",
        )

    def handle(self, *args, **opts):
        upto = (
            date.fromisoformat(opts["upto"]) if opts.get("upto") else date.today()
        )
        dry = bool(opts.get("dry_run"))

        self.stdout.write(f"Closing ANNIVERSARY-cycle rows with cycle_end <= {upto}")
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        policies = list(
            LeavePolicy.objects
            .filter(carry_forward_pct__gt=0, cycle=LeavePolicy.Cycle.ANNIVERSARY)
            .select_related("shift_type")
        )
        if not policies:
            self.stdout.write("No ANNIVERSARY-cycle policies with carry_forward_pct > 0. Nothing to do.")
            return

        closing_rows = (
            LeaveBalance.objects
            .filter(
                leave_policy__in=policies,
                cycle_end__isnull=False,
                cycle_end__lte=upto,
                cycle_closed=False,
            )
            .select_related("employee", "leave_policy")
            .order_by("cycle_end")
        )

        total_carried = Decimal("0")
        total_encashed_requests = 0
        frozen_count = 0
        closed_count = 0

        for bal in closing_rows:
            policy = bal.leave_policy
            cycle_last_day = bal.cycle_end - timedelta(days=1)

            # Live-computed final numbers — used for the preview (dry-run
            # included) and the unused/carry math either way, so a dry-run
            # accurately reflects what a real run would do.
            final_accrued = live_accrued_al(bal.employee, policy, bal, upto=cycle_last_day)
            final_used = live_used(bal.employee, policy, bal, upto=cycle_last_day)
            unused = bal.opening + final_accrued + bal.carried_forward - final_used - bal.encashed
            if unused < 0:
                unused = Decimal("0")

            carry_raw = unused * policy.carry_forward_pct
            carry = carry_raw.quantize(Decimal("0.01"))
            if policy.max_carry_forward_days is not None:
                cap = Decimal(policy.max_carry_forward_days)
                carry = min(carry, cap)
            excess_for_encash = unused - carry

            self.stdout.write(
                f"  {bal.employee.email} · {policy.shift_type.name} "
                f"· cycle {bal.cycle_start}–{bal.cycle_end}: "
                f"accrued={final_accrued} used={final_used} unused={unused} carry={carry} excess={excess_for_encash}"
            )

            if dry:
                continue

            with transaction.atomic():
                # Defensive belt-and-suspenders: properly reconcile (via
                # individual ledger transactions, not just a live read) any
                # orphan Shift usage reconcile_shift_leave_usage hasn't
                # caught yet (that night's run failed or was skipped) — so
                # the closed row's persisted `used` matches final_used
                # exactly rather than under-reporting once frozen.
                reconcile_orphan_shift_usage(
                    since=bal.cycle_start, upto=cycle_last_day,
                    employee=bal.employee, policy=policy,
                )
                bal.refresh_from_db()

                if final_accrued != bal.accrued:
                    LeaveBalanceTransaction.objects.create(
                        balance=bal,
                        delta=final_accrued - bal.accrued,
                        reason=LeaveBalanceTransaction.Reason.ACCRUAL,
                        created_by=None,
                        note=f"Final accrual for cycle {bal.cycle_start}–{bal.cycle_end} (live-computed at close)",
                    )
                    bal.accrued = final_accrued
                    bal.save(update_fields=["accrued", "updated_at"])

                new_bal = get_or_create_balance(bal.employee, policy, bal.cycle_end)

                if carry > 0:
                    LeaveBalanceTransaction.objects.create(
                        balance=new_bal,
                        delta=carry,
                        reason=LeaveBalanceTransaction.Reason.CARRIED_FORWARD,
                        application=None,
                        created_by=None,
                        note=f"Carry-forward from cycle {bal.cycle_start}–{bal.cycle_end}",
                    )
                    new_bal.carried_forward = F("carried_forward") + carry
                    new_bal.save(update_fields=["carried_forward", "updated_at"])
                    new_bal.refresh_from_db()
                    total_carried += carry

                if (policy.freezes_entitlement_at_cap
                    and policy.max_carry_forward_days is not None
                    and new_bal.carried_forward >= Decimal(policy.max_carry_forward_days)
                    and not new_bal.entitlement_frozen):
                    new_bal.entitlement_frozen = True
                    new_bal.save(update_fields=["entitlement_frozen", "updated_at"])
                    LeaveBalanceTransaction.objects.create(
                        balance=new_bal,
                        delta=Decimal("0"),
                        reason=LeaveBalanceTransaction.Reason.FROZEN,
                        application=None,
                        created_by=None,
                        note=(
                            f"Carry-forward reached {policy.max_carry_forward_days}-day "
                            f"cap. Future accrual frozen for the rest of service — "
                            f"one-way, does not clear if the balance later drops."
                        ),
                    )
                    frozen_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠ {bal.employee.email} · {policy.shift_type.name} frozen at cap"
                    ))

                # Inert while encash_carry_forward_excess=False for AL (HR:
                # nothing is cashed — excess beyond the carry cap is
                # forfeited). Kept for parity with apply_year_end_rollover.py.
                if policy.encash_carry_forward_excess and excess_for_encash > 0:
                    EncashmentRequest.objects.create(
                        employee=bal.employee,
                        leave_policy=policy,
                        year=bal.year,
                        days_requested=excess_for_encash,
                        status=EncashmentRequest.Status.PENDING,
                        note="Auto-created at anniversary cycle close",
                        added_by=None,
                        updated_by=None,
                    )
                    total_encashed_requests += 1

                bal.cycle_closed = True
                bal.save(update_fields=["cycle_closed", "updated_at"])
                closed_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {closed_count} cycle(s) closed; carried {total_carried} day(s); "
            f"{total_encashed_requests} encashment request(s) created; "
            f"{frozen_count} balance(s) frozen at cap."
        ))
