"""Bulk-materialise this month's NightShiftConsent rows for every currently
qualifying female employee (active + at least one night shift scheduled
this calendar month — see employees/services.py::qualifies_for_night_shift_consent).

Runs on the 1st of every month as the "Generate night-shift consents"
periodic task (a django_celery_beat row — see docs/SCHEDULER_ADMIN_PLAN.md)
— belt-and-suspenders alongside the
dashboard's own lazy get_or_create_night_shift_consent call: an employee
who never opens her dashboard that month would otherwise never get a row,
leaving HR's audit list incomplete. Idempotent (get_or_create): re-running
mid-month for someone newly scheduled onto a night shift after the 1st is
also safe and picks her up.

Usage:
    python manage.py generate_night_shift_consents
    python manage.py generate_night_shift_consents --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts.models import CustomUser
from employees.services import (
    current_night_shift_consent_period,
    get_or_create_night_shift_consent,
    qualifies_for_night_shift_consent,
)


class Command(BaseCommand):
    help = (
        "Create this month's NightShiftConsent rows (default consents=True) "
        "for every active female employee with a scheduled night shift this "
        "calendar month."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without writing.",
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        period = current_night_shift_consent_period()

        candidates = (
            CustomUser.objects
            .filter(is_active=True, personal_info__gender="F")
            .select_related("personal_info")
            .order_by("first_name", "last_name")
        )

        created = 0
        already_existed = 0
        not_eligible = 0

        for employee in candidates:
            if not qualifies_for_night_shift_consent(employee, period):
                not_eligible += 1
                continue
            if dry:
                self.stdout.write(f"  would ensure: {employee.email} · {period:%Y-%m}")
                continue
            _consent, was_created = get_or_create_night_shift_consent(employee, period)
            if was_created:
                created += 1
                self.stdout.write(f"  created: {employee.email} · {period:%Y-%m}")
            else:
                already_existed += 1

        if dry:
            self.stdout.write(self.style.WARNING(f"[dry-run] period={period:%Y-%m}"))
            return

        self.stdout.write(self.style.SUCCESS(
            f"✅ generate_night_shift_consents ({period:%Y-%m}): "
            f"{created} created, {already_existed} already existed, "
            f"{not_eligible} not eligible."
        ))
