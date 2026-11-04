"""Monthly accrual job for CALENDAR-cycle WORKED_DAY_RATIO policies.

**Revised 2026-09-01 — no longer processes AL** (or any ANNIVERSARY-cycle
policy). AL's `accrued` is now computed live from worked-day counts
(`leave/services.py::live_accrued_al`) everywhere it's read for the *open*
cycle, and persisted once — as the cycle's final number — by
`close_al_cycles.py` at the moment that cycle closes. A monthly batch that
only posts once a month was exactly the lag this replaced (see the
2026-09-01 AL review that surfaced it: a real employee showed a wrong
negative balance because accrual hadn't posted yet for months of already-
recorded leave usage).

This command is kept — not deleted — for a policy that's CALENDAR-cycle
and WORKED_DAY_RATIO, the same "kept dormant, not deleted" treatment
`apply_year_end_rollover.py` already got for CALENDAR-only carry-forward.
No such policy exists today, so this command currently has nothing to do.

For each (active employee, policy) pair, were one to exist:
- Count last month's WORK Shifts for the employee.
- Divide by `policy.accrual_ratio_denominator`. Round to 0.5 days.
- Add to `LeaveBalance.accrued`. Write an ACCRUAL transaction (idempotent
  per month via a note-matching guard — mirrors leave/signals.py::
  grant_cpl_on_holiday_work's dedup pattern; a real bug here once silently
  double-credited AL before this command excluded it).
- Skip when `LeaveBalance.entitlement_frozen` is True (cap reached).

Scheduled as the "Monthly leave accrual (dormant)" periodic task (00:30
Asia/Dhaka on the 1st — a django_celery_beat row, see
docs/SCHEDULER_ADMIN_PLAN.md). Currently dormant — AL was the only
WORKED_DAY_RATIO policy and no longer runs through here; see the note in
scheduler/schedule.py.

Usage:
    python manage.py apply_monthly_accrual           # process previous month
    python manage.py apply_monthly_accrual --month 2026-04
    python manage.py apply_monthly_accrual --dry-run
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from accounts.models import CustomUser
from leave.models import LeaveBalanceTransaction, LeavePolicy
from leave.services import get_or_create_balance, is_eligible_for_policy
from roster.models import Shift, ShiftType


def _previous_month_today(today: date) -> tuple[date, date]:
    """Return (first_day, last_day) of the calendar month before `today`."""
    if today.month == 1:
        y, m = today.year - 1, 12
    else:
        y, m = today.year, today.month - 1
    last = monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


def _round_half(value: Decimal) -> Decimal:
    """Round to nearest 0.5 (banker's-rounding-ish — half goes up)."""
    return (value * Decimal("2")).quantize(Decimal("1")) / Decimal("2")


class Command(BaseCommand):
    help = "Apply monthly accrual to LeaveBalance rows for WORKED_DAY_RATIO policies."

    def add_arguments(self, parser):
        parser.add_argument(
            "--month",
            type=str,
            default=None,
            help="Target month as YYYY-MM. Defaults to the calendar month before today.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without writing.",
        )

    def handle(self, *args, **opts):
        month_arg = opts.get("month")
        dry = bool(opts.get("dry_run"))

        if month_arg:
            try:
                y, m = month_arg.split("-")
                target_year, target_month = int(y), int(m)
            except ValueError:
                self.stderr.write(self.style.ERROR("--month must be YYYY-MM"))
                return
            last = monthrange(target_year, target_month)[1]
            month_start = date(target_year, target_month, 1)
            month_end = date(target_year, target_month, last)
        else:
            month_start, month_end = _previous_month_today(date.today())

        accrual_year = month_end.year
        self.stdout.write(
            f"Accrual window: {month_start} → {month_end} (year={accrual_year})"
        )
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        policies = list(
            LeavePolicy.objects
            .filter(
                accrual_method=LeavePolicy.AccrualMethod.WORKED_DAY_RATIO,
                cycle=LeavePolicy.Cycle.CALENDAR,
            )
            .select_related("shift_type")
        )
        if not policies:
            self.stdout.write(
                "No CALENDAR-cycle accrual-driven policies. Nothing to do "
                "(AL is ANNIVERSARY-cycle and live-computed instead — see this command's docstring)."
            )
            return

        users = CustomUser.objects.filter(is_active=True)
        total_accrued = Decimal("0")
        affected = 0

        for user in users:
            worked = (
                Shift.objects.filter(
                    employee=user,
                    date__gte=month_start,
                    date__lte=month_end,
                    shift_type__category=ShiftType.Category.WORK,
                ).count()
            )
            if worked == 0:
                continue

            for policy in policies:
                # Don't accrue for ineligible employees (e.g., AL for someone
                # who hasn't cleared the 12-month service gate yet).
                eligible, reason = is_eligible_for_policy(user, policy)
                if not eligible:
                    continue

                denom = policy.accrual_ratio_denominator
                if denom <= 0:
                    continue
                raw = Decimal(worked) / Decimal(denom)
                accrual = _round_half(raw)
                if accrual <= 0:
                    continue

                # Route through the service for the standard calendar-year
                # lookup/creation (this loop is CALENDAR-only now, per the
                # policies query above — kept via the service rather than a
                # raw LeaveBalance.objects.get_or_create for consistency).
                bal = get_or_create_balance(user, policy, month_end)
                if bal.entitlement_frozen:
                    self.stdout.write(
                        f"  {user.email} · {policy.shift_type.name} · "
                        f"FROZEN — skipping ({worked} worked days)"
                    )
                    continue

                # Idempotency guard — same note-matching pattern as
                # leave/signals.py::grant_cpl_on_holiday_work. Without this,
                # re-running the same --month twice (or a manual backfill
                # overlapping the scheduled beat job) silently double-credits.
                note = f"Monthly accrual {month_start:%Y-%m}"
                if LeaveBalanceTransaction.objects.filter(
                    balance=bal, reason=LeaveBalanceTransaction.Reason.ACCRUAL, note=note,
                ).exists():
                    self.stdout.write(
                        f"  {user.email} · {policy.shift_type.name} · "
                        f"already accrued for {month_start:%Y-%m} — skipping"
                    )
                    continue

                self.stdout.write(
                    f"  {user.email} · {policy.shift_type.name} · "
                    f"+{accrual} (worked {worked} / {denom})"
                )
                if dry:
                    continue

                with transaction.atomic():
                    LeaveBalanceTransaction.objects.create(
                        balance=bal,
                        delta=accrual,
                        reason=LeaveBalanceTransaction.Reason.ACCRUAL,
                        application=None,
                        created_by=None,
                        note=note,
                    )
                    bal.accrued = F("accrued") + accrual
                    bal.save(update_fields=["accrued", "updated_at"])
                total_accrued += accrual
                affected += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {affected} balance row(s) updated; total +{total_accrued} day(s)."
        ))
