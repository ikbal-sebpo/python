"""Nightly batch: re-email outstanding policy adherents.

Walks every Active policy and reminds adherents who are still PENDING /
SUPERSEDED and whose last notification is older than ``--interval-days`` (or who
were never notified). Mirrors ``offboarding.deactivate_past_lwd``.

All the work is in ``policies.services.send_scheduled_reminders``, which the
Celery worker executes unchanged.

Scheduled as the "Send policy reminders" periodic task (09:00 Asia/Dhaka, via
core.tasks.run_management_command). The schedule is a django_celery_beat row
managed in the Django admin — see docs/SCHEDULER_ADMIN_PLAN.md. Do NOT also
install a crontab entry for it — beat is the only scheduler. The command has
its own de-dup, but a second scheduler would still re-send early.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from policies.services import send_scheduled_reminders


class Command(BaseCommand):
    help = "Email outstanding policy adherents not notified within --interval-days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval-days", type=int, default=3,
            help="Only remind adherents whose last notification is older than "
                 "this many days (default: 3).",
        )

    def handle(self, *args, **opts):
        interval = opts["interval_days"]
        count = send_scheduled_reminders(interval_days=interval)
        self.stdout.write(self.style.SUCCESS(
            f"Reminded {count} outstanding adherent(s) "
            f"(interval {interval} day(s))."
        ))
