"""One-off: backfill cycle_start/cycle_end/cycle_closed on every pre-existing
CALENDAR-cycle LeaveBalance row (CL/SL/MtL/FL/CpL/LWP/HdL).

Run this ONCE, before `open_al_cycles.py` and before deploying
`close_al_cycles.py`. It freezes every historical CALENDAR-cycle row as a
closed calendar-year snapshot: [Jan 1, Jan 1 next year), cycle_closed if
the year has already ended.

ANNIVERSARY-cycle rows (AL) are deliberately NOT touched by this command.
Pre-rework AL rows were calendar-year-keyed by the old (buggy)
apply_monthly_accrual.py — applying a [Jan 1, Jan 1) window to them would
be actively wrong (they were never real anniversary cycles) and, worse,
would eventually make close_al_cycles.py mistake a years-old artifact for
a live cycle once its fabricated cycle_end date arrives. AL's actual
current cycle is opened separately and correctly by
open_al_cycles.py, anchored on joining_date + today — the old
calendar-keyed AL rows are simply left with cycle_start/cycle_end NULL
forever (still fully readable via admin/queries, just outside the new
cycle-close automation).

Idempotent by construction: only CALENDAR-cycle rows with cycle_start still
NULL are touched. Any row created after this point goes through
leave/services.py::get_or_create_balance, which always populates
cycle_start/cycle_end at creation — so this command naturally never
touches (and never needs to touch) a row created by the new code path.

Usage:
    python manage.py backfill_leave_balance_cycle_windows
    python manage.py backfill_leave_balance_cycle_windows --dry-run
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from leave.models import LeaveBalance, LeavePolicy


class Command(BaseCommand):
    help = "One-off: populate cycle_start/cycle_end/cycle_closed on pre-existing CALENDAR-cycle LeaveBalance rows."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing.")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        current_year = date.today().year

        qs = (
            LeaveBalance.objects
            .filter(cycle_start__isnull=True, leave_policy__cycle=LeavePolicy.Cycle.CALENDAR)
            .select_related("leave_policy__shift_type")
        )
        total = qs.count()
        self.stdout.write(f"{total} CALENDAR-cycle LeaveBalance row(s) with no cycle window yet.")

        skipped_anniversary = LeaveBalance.objects.filter(
            cycle_start__isnull=True, leave_policy__cycle=LeavePolicy.Cycle.ANNIVERSARY,
        ).count()
        if skipped_anniversary:
            self.stdout.write(
                f"{skipped_anniversary} ANNIVERSARY-cycle row(s) (AL) left untouched by design — "
                f"see this command's docstring."
            )

        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))
            for bal in qs[:20]:
                self.stdout.write(
                    f"  {bal.employee} · {bal.leave_policy.shift_type.name} · {bal.year} "
                    f"→ [{date(bal.year, 1, 1)}, {date(bal.year + 1, 1, 1)}) "
                    f"closed={bal.year < current_year}"
                )
            if total > 20:
                self.stdout.write(f"  ... and {total - 20} more")
            return

        updated = 0
        for bal in qs.iterator():
            bal.cycle_start = date(bal.year, 1, 1)
            bal.cycle_end = date(bal.year + 1, 1, 1)
            bal.cycle_closed = bal.year < current_year
            bal.save(update_fields=["cycle_start", "cycle_end", "cycle_closed", "updated_at"])
            updated += 1

        self.stdout.write(self.style.SUCCESS(f"Done. {updated} row(s) backfilled."))
