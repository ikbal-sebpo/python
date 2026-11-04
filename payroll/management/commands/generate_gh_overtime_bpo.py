"""Auto-file GH OT entries for General BPO from ttms.sebpo.net's actual
worked hours on a government holiday.

General BPO — like Software Development and Creative Development — earns
overtime solely for working a government holiday, and never files it
through the normal upload. Unlike those two departments, General BPO's
presence data isn't sourced from AttendancePunch/TimeLy: it comes live from
TTMS's ops daily-hours API (payroll/ttms.py), and each entry's seconds is
the employee's own TTMS-reported worked time for that date (not a flat
grant) — General BPO's `holiday_ot` PayRate is PER_HOUR (added by HR
2026-09-17), so paying exact hours is the correct amount.

The actual logic lives in payroll/gh_overtime.py::generate_bpo_gh_overtime_for_date,
sharing its write/idempotency/window rules with the Software/Creative flow
(payroll/gh_overtime.py::generate_gh_overtime_for_date, manage.py
generate_gh_overtime) via one internal helper, so the two can't drift on
what actually gets written.

Scheduled at 10:25 Asia/Dhaka (the "Generate GH overtime (General BPO)"
periodic task — a django_celery_beat row, see
docs/SCHEDULER_ADMIN_PLAN.md) — right after the
Software/Creative job at 10:20. Unlike that job, this one does NOT depend on
a local sync step: TTMS is called live, so there's nothing to run first.
Checks whether the target date is an active roster.Holiday first and no-ops
otherwise, so it's safe to run every day rather than needing holiday-aware
scheduling.

Idempotent and additive-only — see payroll/gh_overtime.py's module
docstring. That's what makes this command the manual fail-safe too, for
when the scheduled run fails:

    python manage.py generate_gh_overtime_bpo                        # yesterday
    python manage.py generate_gh_overtime_bpo --date 2026-09-04
    python manage.py generate_gh_overtime_bpo --from 2026-08-16 --to 2026-09-15
    python manage.py generate_gh_overtime_bpo --dry-run

Raises a clean CommandError (not a traceback) if TTMS_API_BASE_URL/
TTMS_API_TOKEN aren't configured or the fetch fails — deliberately not
swallowed, since a silently-empty result would be indistinguishable from
"nobody worked" on a payroll write (see payroll/ttms.py's module docstring).
On the scheduled beat run, that failure surfaces as a failed Celery task in
logs/celery.err.log; run the command by hand once the underlying issue is
fixed.

TODO: same as generate_gh_overtime — no auto-reversal if a later TTMS
correction changes reported hours for a date already generated.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from payroll.gh_overtime import (
    BPO_DEPARTMENT_NAME, DEFAULT_BPO_MIN_HOURS, OT_TYPE_CODE, generate_bpo_gh_overtime_for_date,
)
from payroll.ttms import TTMSUnavailable


class Command(BaseCommand):
    help = (
        "Auto-create GH OT entries for General BPO from ttms.sebpo.net's actual "
        "worked hours on a government holiday."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=str, default=None,
            help="Single date as YYYY-MM-DD. Defaults to yesterday.",
        )
        parser.add_argument(
            "--from", dest="date_from", type=str, default=None,
            help="Range start YYYY-MM-DD — use with --to for a manual catch-up sweep.",
        )
        parser.add_argument(
            "--to", dest="date_to", type=str, default=None,
            help="Range end YYYY-MM-DD (inclusive) — use with --from.",
        )
        parser.add_argument(
            "--min-hours", type=float, default=DEFAULT_BPO_MIN_HOURS,
            help=(
                "Minimum worked hours (from TTMS) to count as present "
                f"(default {DEFAULT_BPO_MIN_HOURS} — any positive amount)."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without writing.",
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        min_hours = float(opts["min_hours"])

        date_from, date_to = opts.get("date_from"), opts.get("date_to")
        if bool(date_from) != bool(date_to):
            raise CommandError("--from and --to must be given together.")
        if date_from:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if end < start:
                raise CommandError("--to must not be before --from.")
            dates = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        elif opts.get("date"):
            dates = [date.fromisoformat(opts["date"])]
        else:
            dates = [date.today() - timedelta(days=1)]

        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        total_present = total_created = 0
        total_skip_existing = total_skip_not_employed = total_skip_window = 0

        for d in dates:
            try:
                report = generate_bpo_gh_overtime_for_date(
                    d, min_hours=min_hours, apply=not dry, actor=None,
                )
            except TTMSUnavailable as exc:
                raise CommandError(f"{d}: could not fetch TTMS daily hours — {exc}")

            if not report.is_holiday:
                self.stdout.write(f"{d}: not a government holiday — skipping.")
                continue
            if report.ot_type_missing:
                self.stdout.write(self.style.ERROR(
                    f"OvertimeType '{OT_TYPE_CODE}' not found — run seed_payroll_components first."
                ))
                return
            if report.departments_missing:
                self.stdout.write(self.style.WARNING(f"Department '{BPO_DEPARTMENT_NAME}' not found — skipping."))
                continue
            if not report.rows:
                self.stdout.write(f"{d}: no worked hours reported by TTMS for General BPO.")
                continue

            for row in report.created:
                verb = "would create" if dry else "created"
                hours = (row.seconds or 0) / 3600
                self.stdout.write(
                    f"  {verb}: {row.employee.email} · {d} · {hours:.2f}h · team={row.employee.team or '—'}"
                )
            for row in report.window_rejected:
                self.stdout.write(self.style.WARNING(f"  {row.employee.email} · {d}: {row.reason}"))

            self.stdout.write(f"{d}: {len(report.created)} {'would be ' if dry else ''}created.")

            total_present += len(report.rows)
            total_created += len(report.created)
            total_skip_existing += len(report.already_existing)
            total_skip_not_employed += len(report.not_employed)
            total_skip_window += len(report.window_rejected)

        self.stdout.write(self.style.SUCCESS(
            (f"[dry-run] " if dry else "") + f"Done. {total_present} present, {total_created} created, "
            f"{total_skip_existing} already had an entry, {total_skip_not_employed} not employed that day, "
            f"{total_skip_window} rejected by the payroll window."
        ))
