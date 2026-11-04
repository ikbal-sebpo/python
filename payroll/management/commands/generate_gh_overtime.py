"""Auto-file 8h GH OT entries for Software Development / Creative
Development employees who actually worked a government holiday.

Those two departments earn overtime *solely* via GH OT — a department-wide
GROSS_30TH_PER_DAY rate, no Regular OT at all (docs/PAYROLL_PLAN.md's
2026-09-09 "Software/Creative Development overtime" section) — so they never
file overtime through the normal upload. Presence is read from
AttendancePunch instead: any scoped employee with a check-in and at least
--min-hours of time_in_office on a roster.Holiday date gets one 8h GH OT
entry, worked_in set explicitly to their own accounts.Team.

The actual logic lives in payroll/gh_overtime.py, shared with the
superuser-only UI trigger (payroll/views.py::GenerateGHOvertimeView, added
2026-09-17) so the two entry points can't drift.

Scheduled at 10:20 Asia/Dhaka (the "Generate GH overtime" periodic task — a
django_celery_beat row, see docs/SCHEDULER_ADMIN_PLAN.md), a few minutes
after sync_attendance_punches populates
the previous day's AttendancePunch rows at 10:00. Checks whether the target
date is an active roster.Holiday first and no-ops otherwise, so it's safe to
run every day rather than needing holiday-aware scheduling.

Idempotent and additive-only — see payroll/gh_overtime.py's docstring. That's
what makes this command the documented manual fail-safe too — for when the
scheduled run fails, or AttendancePunch is corrected after the fact. Note
this command does NOT sync attendance itself; run sync_attendance_punches
first for any date that isn't already synced:

    python manage.py sync_attendance_punches --date 2026-08-26
    python manage.py generate_gh_overtime --date 2026-08-26           # yesterday if no --date
    python manage.py generate_gh_overtime --from 2026-08-16 --to 2026-09-15
    python manage.py generate_gh_overtime --dry-run

TODO (2026-09-17, tracked for follow-up — see docs/PAYROLL_PLAN.md): no
auto-reversal yet. If a correction later removes the punch that qualified
someone, the OT entry this command created isn't auto-voided — HR has to
void/delete it by hand from /payroll/overtime/.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from payroll.gh_overtime import (
    DEFAULT_GRANT_HOURS, DEFAULT_MIN_HOURS, OT_TYPE_CODE, generate_gh_overtime_for_date,
)


class Command(BaseCommand):
    help = (
        "Auto-create 8h GH OT entries for Software/Creative Development "
        "employees present (>= --min-hours office time) on a government holiday."
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
            "--min-hours", type=float, default=DEFAULT_MIN_HOURS,
            help=f"Minimum office hours to count as present (default {DEFAULT_MIN_HOURS}).",
        )
        parser.add_argument(
            "--hours", type=float, default=DEFAULT_GRANT_HOURS,
            help=f"OT hours to grant per qualifying employee (default {DEFAULT_GRANT_HOURS}).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without writing.",
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        min_hours = float(opts["min_hours"])
        grant_hours = float(opts["hours"])

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
        ot_type_missing = False

        for d in dates:
            report = generate_gh_overtime_for_date(
                d, min_hours=min_hours, grant_hours=grant_hours, apply=not dry, actor=None,
            )

            if not report.is_holiday:
                self.stdout.write(f"{d}: not a government holiday — skipping.")
                continue
            if report.ot_type_missing:
                self.stdout.write(self.style.ERROR(
                    f"OvertimeType '{OT_TYPE_CODE}' not found — run seed_payroll_components first."
                ))
                ot_type_missing = True
                break
            for missing in report.departments_missing:
                self.stdout.write(self.style.WARNING(f"Department '{missing}' not found — skipping."))
            if not report.rows:
                self.stdout.write(f"{d}: no present employees in scope.")
                continue

            for row in report.created:
                verb = "would create" if dry else "created"
                self.stdout.write(
                    f"  {verb}: {row.employee.email} · {d} · {grant_hours}h · team={row.employee.team or '—'}"
                )
            for row in report.window_rejected:
                self.stdout.write(self.style.WARNING(f"  {row.employee.email} · {d}: {row.reason}"))

            self.stdout.write(f"{d}: {len(report.created)} {'would be ' if dry else ''}created.")

            total_present += len(report.rows)
            total_created += len(report.created)
            total_skip_existing += len(report.already_existing)
            total_skip_not_employed += len(report.not_employed)
            total_skip_window += len(report.window_rejected)

        if ot_type_missing:
            return

        self.stdout.write(self.style.SUCCESS(
            (f"[dry-run] " if dry else "") + f"Done. {total_present} present, {total_created} created, "
            f"{total_skip_existing} already had an entry, {total_skip_not_employed} not employed that day, "
            f"{total_skip_window} rejected by the payroll window."
        ))
