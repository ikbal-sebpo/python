"""Catch-up reconciliation for CpL grants missing because a WORK-on-Holiday
Shift was written via bulk_create (bulk roster uploads bypass Django
signals on purpose, for performance) — the grant-side counterpart of
reconcile_shift_leave_usage.py. Two independent passes, both idempotent,
safe to re-run:

1. **Forward** (`reconcile_missing_cpl_grants`): grant +1 CpL (and create a
   `leave.CplGrant` history row) for a WORK-on-Holiday Shift that has no
   grant yet — but **only for AUTO-treatment departments/teams** (see
   `accounts.permissions.effective_cpl_treatment`). A MANUAL department
   (e.g. Data Solutions & Due Diligence) decides CpL vs. overtime per
   employee/instance outside Orbis and never auto-grants; a NONE department
   (e.g. Digital Advertising) never gets CpL at all — see
   docs/LEAVE_POLICY_IMPLEMENTATION.md §5.12. Idempotent via
   `CplGrant.work_shift` (uniquely enforced); also backfills a `CplGrant`
   for a pre-existing note-matched ACCRUAL transaction instead of granting
   twice.

2. **Reversal** (`reconcile_stale_cpl_grants`): forfeit a still-OPEN,
   AUTO-sourced grant whose Shift has since been bulk-retyped away from
   WORK, or whose date is no longer an active Holiday. A CONSUMED grant is
   never touched by this pass — that consumption is a separate, already-real
   fact.

ALWAYS run --dry-run first and review the per-date counts — this is a real
compensation entitlement, not just a data cleanup. Use --since to bound the
first real run to a deliberate cutover date rather than reconciling all of
history at once.

Runs nightly, alongside reconcile_shift_leave_usage.py, as the "Reconcile CpL
grants" periodic task (00:40 Asia/Dhaka — a django_celery_beat row, see
docs/SCHEDULER_ADMIN_PLAN.md).

Usage:
    python manage.py reconcile_cpl_grants --dry-run
    python manage.py reconcile_cpl_grants --since 2026-01-01
    python manage.py reconcile_cpl_grants --since 2026-01-01 --upto 2026-08-31
"""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from leave.services import reconcile_missing_cpl_grants, reconcile_stale_cpl_grants


class Command(BaseCommand):
    help = "Reconcile bulk_create-written WORK-on-Holiday Shift rows into CpL grants (AUTO-treatment departments only), plus stale reversals."

    def add_arguments(self, parser):
        parser.add_argument("--since", type=str, default=None, help="Only Shift/holiday dates on/after this date (YYYY-MM-DD).")
        parser.add_argument("--upto", type=str, default=None, help="Only Shift/holiday dates on/before this date (YYYY-MM-DD). Default: today.")
        parser.add_argument("--dry-run", action="store_true", help="Tally counts without writing.")

    def handle(self, *args, **opts):
        since = date.fromisoformat(opts["since"]) if opts.get("since") else None
        upto = date.fromisoformat(opts["upto"]) if opts.get("upto") else date.today()
        dry = bool(opts.get("dry_run"))

        self.stdout.write(
            f"Reconciling bulk_create-written WORK-on-Holiday Shift rows: "
            f"{since or 'the beginning of history'} → {upto}"
        )
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        result = reconcile_missing_cpl_grants(since=since, upto=upto, dry_run=dry)

        self.stdout.write("")
        self.stdout.write("New grants by holiday date:")
        for holiday_date, count in sorted(result["by_date"].items()):
            self.stdout.write(f"  {holiday_date}: {count}")

        self.stdout.write(self.style.SUCCESS(
            f"{result['processed']} CpL grant(s) "
            f"{'would be ' if dry else ''}created/backfilled; "
            f"{result['skipped_treatment']} skipped (not an AUTO-treatment department/team)."
        ))

        reversal_result = reconcile_stale_cpl_grants(since=since, upto=upto, dry_run=dry)

        self.stdout.write("")
        self.stdout.write("Stale (forfeited) grants by holiday date:")
        for holiday_date, count in sorted(reversal_result["by_date"].items()):
            self.stdout.write(f"  {holiday_date}: {count}")

        self.stdout.write(self.style.SUCCESS(
            f"{reversal_result['reversed']} stale grant(s) "
            f"{'would be ' if dry else ''}forfeited."
        ))
