"""Materialise scheduler/schedule.py::JOBS as django_celery_beat rows.

Idempotent and safe on every deploy: an existing PeriodicTask is left alone, so
this can never revert a schedule someone retimed in the admin. Use --force to
deliberately push a code default back onto an existing row.

    manage.py seed_beat_schedule
    manage.py seed_beat_schedule --dry-run
    manage.py seed_beat_schedule --force --only sync_attendance_punches
"""

from django.core.management.base import BaseCommand

from scheduler.services import sync_jobs


class Command(BaseCommand):
    help = "Create the periodic-task rows for Orbis's scheduled jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Overwrite existing rows with the schedule.py defaults. "
                "Discards any admin edit to those tasks."
            ),
        )
        parser.add_argument(
            "--only",
            metavar="NAME",
            help="Limit to one job, by task name or management-command name.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options):
        report = sync_jobs(
            force=options["force"],
            only=options.get("only"),
            dry_run=options["dry_run"],
        )

        prefix = "[dry-run] " if options["dry_run"] else ""
        for name in report.created:
            self.stdout.write(self.style.SUCCESS(f"{prefix}created  {name}"))
        for name in report.updated:
            self.stdout.write(self.style.WARNING(f"{prefix}re-synced {name}"))
        for name in report.unchanged:
            self.stdout.write(f"{prefix}kept     {name}")
        for note in report.notes:
            self.stdout.write(f"{prefix}{note}")

        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{len(report.created)} created, "
                f"{len(report.updated)} re-synced, "
                f"{len(report.unchanged)} left as-is."
            )
        )
        if report.unchanged and not options["force"]:
            self.stdout.write(
                "Existing rows are managed in the admin and were not touched; "
                "use --force to reset one to its schedule.py default."
            )
