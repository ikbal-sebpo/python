"""Year-end rollover: carry forward + encashment auto-create + 60-day freeze.

Only CALENDAR-cycle policies (carry_forward_pct > 0 and cycle=CALENDAR) go
through this once-a-year batch. AL is ANNIVERSARY-cycle and is rolled over
per-employee, on their own cycle anniversary, by
`close_al_cycles.py` instead — that command reuses this same formula, just
scanned nightly rather than run once on Jan 1.

Since 2026-10-01 CpL is the live customer of this batch: its grants no
longer expire and carry forward 100% (no cap, no freeze, no encashment), so
a day earned working Dec 16 can be taken in January.

**Idempotent / re-runnable.** Each (employee, policy) pair's carry is
recorded as CARRIED_FORWARD transactions on the *new* year's balance
tagged with the closing year; a re-run recomputes the target carry and
posts only the difference (positive or negative). That matters because
December grants are often posted late — a roster upload for the last week
of December done on Jan 3 would otherwise be missed by the Jan 1 run — so
the "Year-end rollover" periodic task fires on Jan 1 *and* Jan 15 (01:00
Asia/Dhaka; a django_celery_beat row, see docs/SCHEDULER_ADMIN_PLAN.md), and
HR can re-run it by hand (or via the admin's "Run selected tasks") any time.

For each matching policy, walk every active employee and:

1. Take `carry_forward_pct × (opening + accrued + carried_forward − used)`
   from the prior year, capped at `max_carry_forward_days`. Push to next
   year's `LeaveBalance.carried_forward`.
2. If `freezes_entitlement_at_cap` is True and the new carried_forward
   has reached `max_carry_forward_days`, set `entitlement_frozen=True` on
   the new year's balance and emit a FROZEN transaction.
3. If `encash_carry_forward_excess` is True, any unused balance beyond
   what was carried forward auto-creates a PENDING `EncashmentRequest`.

Run after the close of the calendar year (e.g., Jan 1 of the new year).

Usage:
    python manage.py apply_year_end_rollover           # rolls last year into current year
    python manage.py apply_year_end_rollover --year 2026  # explicitly: 2026 → 2027
    python manage.py apply_year_end_rollover --dry-run
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from accounts.models import CustomUser
from leave.services import get_or_create_balance
from leave.models import (
    EncashmentRequest,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
)


ENCASH_NOTE = "Auto-created at year-end rollover"


def carry_note(closing_year: int) -> str:
    """The note that tags every CARRIED_FORWARD transaction this command
    posts for `closing_year` — the idempotency key for re-runs."""
    return f"Carry-forward from {closing_year}"


class Command(BaseCommand):
    help = "Apply year-end carry-forward + encashment + freeze (re-runnable)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            default=None,
            help="The year being closed out. Default: current year - 1.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without writing.",
        )

    def handle(self, *args, **opts):
        closing_year = opts.get("year") or (date.today().year - 1)
        next_year = closing_year + 1
        dry = bool(opts.get("dry_run"))

        self.stdout.write(f"Rolling over {closing_year} → {next_year}")
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        policies = list(
            LeavePolicy.objects
            .filter(carry_forward_pct__gt=0, cycle=LeavePolicy.Cycle.CALENDAR)
            .select_related("shift_type")
        )
        if not policies:
            self.stdout.write(
                "No CALENDAR-cycle policies with carry_forward_pct > 0. "
                "Nothing to do (AL is rolled over by close_al_cycles.py instead)."
            )
            return

        users = CustomUser.objects.filter(is_active=True)
        total_carried = Decimal("0")
        total_encashed_requests = 0
        frozen_count = 0

        for user in users:
            for policy in policies:
                old = LeaveBalance.objects.filter(
                    employee=user, leave_policy=policy, year=closing_year,
                ).first()
                if old is None:
                    continue

                unused = old.opening + old.accrued + old.carried_forward - old.used - old.encashed
                carry = Decimal("0")
                if unused > 0:
                    carry_raw = unused * policy.carry_forward_pct
                    carry = carry_raw.quantize(Decimal("0.01"))
                    if policy.max_carry_forward_days is not None:
                        cap = Decimal(policy.max_carry_forward_days)
                        carry = min(carry, cap)
                excess_for_encash = max(unused - carry, Decimal("0"))

                # What an earlier run already carried from this closing year
                # (tagged by note) — we post only the difference, so re-runs
                # never double-count and late-posted December activity
                # (grants or usage) is reconciled rather than ignored.
                note = carry_note(closing_year)
                new_bal = LeaveBalance.objects.filter(
                    employee=user, leave_policy=policy, year=next_year,
                ).first()
                already = Decimal("0")
                if new_bal is not None:
                    already = sum(
                        (t.delta for t in new_bal.transactions.filter(
                            reason=LeaveBalanceTransaction.Reason.CARRIED_FORWARD,
                            note=note,
                        )),
                        Decimal("0"),
                    )
                delta = carry - already
                wants_encash = policy.encash_carry_forward_excess and excess_for_encash > 0
                if delta == 0 and not wants_encash:
                    continue  # already fully carried (or nothing to carry)

                self.stdout.write(
                    f"  {user.email} · {policy.shift_type.name}: "
                    f"unused={unused} carry={carry} already={already} "
                    f"post={delta:+} excess={excess_for_encash}"
                )

                if dry:
                    continue

                with transaction.atomic():
                    new_bal = get_or_create_balance(user, policy, date(next_year, 1, 1))

                    if delta != 0:
                        LeaveBalanceTransaction.objects.create(
                            balance=new_bal,
                            delta=delta,
                            reason=LeaveBalanceTransaction.Reason.CARRIED_FORWARD,
                            application=None,
                            created_by=None,
                            note=note,
                        )
                        new_bal.carried_forward = F("carried_forward") + delta
                        new_bal.save(update_fields=["carried_forward", "updated_at"])
                        new_bal.refresh_from_db()
                        total_carried += delta

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
                                f"Carry-forward reached "
                                f"{policy.max_carry_forward_days}-day cap. "
                                f"Future accrual frozen."
                            ),
                        )
                        frozen_count += 1
                        self.stdout.write(self.style.WARNING(
                            f"  ⚠ {user.email} · {policy.shift_type.name} frozen at cap"
                        ))

                    # Encashment — once per (employee, policy, closing year).
                    if (policy.encash_carry_forward_excess and excess_for_encash > 0
                        and not EncashmentRequest.objects.filter(
                            employee=user, leave_policy=policy, year=closing_year,
                            note=ENCASH_NOTE,
                        ).exists()):
                        EncashmentRequest.objects.create(
                            employee=user,
                            leave_policy=policy,
                            year=closing_year,
                            days_requested=excess_for_encash,
                            status=EncashmentRequest.Status.PENDING,
                            note=ENCASH_NOTE,
                            added_by=None,
                            updated_by=None,
                        )
                        total_encashed_requests += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. Carried {total_carried} day(s); "
            f"{total_encashed_requests} encashment request(s) created; "
            f"{frozen_count} balance(s) frozen at cap."
        ))
