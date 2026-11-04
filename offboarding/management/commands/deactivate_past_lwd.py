"""Nightly batch: deactivate users whose last_working_date has passed.

Backstop for ``offboarding.middleware.LastWorkingDateMiddleware`` — catches
users who don't trigger the middleware (e.g. never log in after LWD). The
middleware handles the fast path; this command handles the slow path.

Scheduled as the "Deactivate past LWD" periodic task (00:15 Asia/Dhaka, via
core.tasks.run_management_command). The schedule is a django_celery_beat row
managed in the Django admin — see docs/SCHEDULER_ADMIN_PLAN.md. Do NOT also
install a crontab entry for it — beat is the only scheduler, and a second one
would double-run the batch. Still runnable by hand for ad-hoc recalculation::

    python manage.py deactivate_past_lwd --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from offboarding.services import deactivate_past_lwd_users


class Command(BaseCommand):
    help = "Deactivate users whose CustomUser.last_working_date < today."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would be deactivated without making changes.",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        touched = deactivate_past_lwd_users(dry_run=dry_run)
        if not touched:
            self.stdout.write("No users past LWD.")
            return
        for email, lwd in touched:
            self.stdout.write(f"  · {email} (LWD {lwd})")
        verb = "would deactivate" if dry_run else "deactivated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {len(touched)} user(s)."
        ))
