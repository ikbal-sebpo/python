"""Pull one day's TimeLy office-presence data and upsert it into AttendancePunch.

Scheduled at 10:00 Asia/Dhaka for the *previous* day (the "Sync attendance
punches" periodic task — a django_celery_beat row, see
docs/SCHEDULER_ADMIN_PLAN.md) — TimeLy's punch data for a day isn't settled until
~7:00 AM the next morning, so 10:00 leaves a safety margin before we treat it
as final. "Today" is deliberately never persisted here; the dashboard's
`working` bucket keeps reading it live via attendance.timely.fetch_office_presence
(60s-cached) since the day isn't finalized yet.

Usage:
    python manage.py sync_attendance_punches                  # yesterday
    python manage.py sync_attendance_punches --date 2026-09-05
    python manage.py sync_attendance_punches --from 2026-10-21 --to 2026-10-22
    python manage.py sync_attendance_punches --dry-run

A range resyncs each day in turn, exactly as separate --date runs would (a
TimeLy outage spanning several days is one run, not one per day). --to
defaults to yesterday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.locations import office_bucket
from accounts.models import CustomUser
from attendance.models import AttendancePunch
from attendance.timely import fetch_office_presence


def _parse_punch(value):
    """Parse a TimeLy punch timestamp (naive ISO, local Asia/Dhaka time) into
    an aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timezone.is_aware(naive):
        return naive
    return timezone.make_aware(naive)


def _pick_duration(record, check_in, check_out):
    """Prefer TimeLy's own duration_seconds — first_punch_at/last_punch_at
    can be truncated to the queried calendar date for a shift crossing
    midnight, which a plain check_out - check_in subtraction can't detect
    but duration_seconds isn't affected by. Falls back to the subtraction
    when the API omits the field."""
    raw = record.get("duration_seconds")
    if raw is not None:
        try:
            return timedelta(seconds=float(raw))
        except (TypeError, ValueError):
            pass
    if check_in and check_out and check_out > check_in:
        return check_out - check_in
    return None


def _pick_office(office_field) -> str:
    """Raw office label for storage — same 'skip REMOTE' preference as the
    dashboard's own _format_office (core/views.py), duplicated here rather
    than imported since that one is private to the dashboard view."""
    if not isinstance(office_field, list) or not office_field:
        return ""
    physical = [str(o) for o in office_field if str(o).strip().lower() != "remote"]
    pick = physical[0] if physical else str(office_field[0])
    return pick.strip()


# A resync is for recovering from an outage, not a backfill of history — a
# typo'd year would otherwise hammer TimeLy with hundreds of requests. It also
# has to finish inside CELERY_TASK_TIME_LIMIT (300s) when dispatched from the
# scheduler admin's "Run once with arguments" page.
MAX_RANGE_DAYS = 31


def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"{flag} must be YYYY-MM-DD, got {value!r}.")


class Command(BaseCommand):
    help = "Sync one day's TimeLy office-presence records into AttendancePunch."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=str, default=None,
            help="Date as YYYY-MM-DD. Defaults to yesterday.",
        )
        parser.add_argument(
            "--from", dest="date_from", type=str, default=None,
            help="First date of a range to resync (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--to", dest="date_to", type=str, default=None,
            help="Last date of the range, inclusive. Defaults to yesterday.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without writing.",
        )

    def _target_dates(self, opts) -> list[date]:
        yesterday = date.today() - timedelta(days=1)
        if opts.get("date") and (opts.get("date_from") or opts.get("date_to")):
            raise CommandError("Use either --date or --from/--to, not both.")
        if opts.get("date_to") and not opts.get("date_from"):
            raise CommandError("--to needs --from.")

        if opts.get("date_from"):
            start = _parse_date(opts["date_from"], "--from")
            end = _parse_date(opts["date_to"], "--to") if opts.get("date_to") else yesterday
        elif opts.get("date"):
            start = end = _parse_date(opts["date"], "--date")
        else:
            start = end = yesterday

        if end < start:
            raise CommandError(f"--to ({end}) is before --from ({start}).")
        # Today is never persisted (see the module docstring) — its punches
        # aren't settled, and the dashboard reads it live anyway.
        if end > yesterday:
            raise CommandError(f"{end} isn't settled yet; the latest syncable date is {yesterday}.")
        days = (end - start).days + 1
        if days > MAX_RANGE_DAYS:
            raise CommandError(f"Range is {days} days; the limit is {MAX_RANGE_DAYS}.")
        return [start + timedelta(days=i) for i in range(days)]

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        for target_date in self._target_dates(opts):
            self._sync_day(target_date, dry)

    def _sync_day(self, target_date: date, dry: bool):
        self.stdout.write(f"Syncing TimeLy office presence for {target_date}")
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no writes"))

        presence = fetch_office_presence(target_date)
        if not presence:
            self.stdout.write(self.style.WARNING(
                "No presence data returned (TimeLy unset, down, or nothing recorded). Nothing to do."
            ))
            return

        employee_ids = list(presence.keys())
        emp_by_employee_id = dict(
            CustomUser.objects
            .filter(employee_id__in=employee_ids)
            .values_list("employee_id", "pk")
        )
        pk_to_user = {u.pk: u for u in CustomUser.objects.filter(pk__in=emp_by_employee_id.values())}
        corrected_sources = dict(
            AttendancePunch.objects
            .filter(date=target_date, employee_id__in=emp_by_employee_id.values())
            .values_list("employee_id", "source")
        )

        matched = skipped = created_count = updated_count = corrected_count = 0

        for emp_id, record in presence.items():
            user_pk = emp_by_employee_id.get(emp_id)
            if user_pk is None:
                skipped += 1
                continue
            matched += 1

            check_in = _parse_punch(record.get("first_punch_at"))
            check_out = _parse_punch(record.get("last_punch_at"))
            raw_office = _pick_office(record.get("office"))
            defaults = {
                "check_in": check_in,
                "check_out": check_out,
                "time_in_office": _pick_duration(record, check_in, check_out),
                "raw_office": raw_office,
                "office_bucket": office_bucket(raw_office) if raw_office else "",
                "punch_count": record.get("punch_count"),
                "source": AttendancePunch.Source.TIMELY_API,
                "raw_payload": record,
            }

            # A row already fixed by an approved AttendanceCorrection is
            # authoritative from here on — TimeLy's own numbers for that date
            # are the ones known to be wrong. Leave the corrected timestamps
            # alone; refreshing office/punch_count/raw_payload around them is
            # still fine, those weren't what got corrected.
            if corrected_sources.get(user_pk) == AttendancePunch.Source.MANUAL_CORRECTION:
                corrected_count += 1
                for key in ("check_in", "check_out", "time_in_office", "source"):
                    defaults.pop(key)

            if dry:
                self.stdout.write(f"  {pk_to_user[user_pk].email}: {defaults}")
                continue

            _, was_created = AttendancePunch.objects.update_or_create(
                employee_id=user_pk, date=target_date, defaults=defaults,
            )
            if was_created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {matched} matched ({created_count} created, {updated_count} updated, "
            f"{corrected_count} corrected row(s) left untouched), "
            f"{skipped} unmapped employee_id(s) skipped."
        ))
