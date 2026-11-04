"""Tests for the database-backed beat schedule.

The important ones are BeatScheduleSourceOfTruthTests (the clobbering
invariant) and ScheduleParityTests (proof that moving 16 jobs out of
core/settings.py shifted nothing). See docs/SCHEDULER_ADMIN_PLAN.md.
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from celery import states
from celery.schedules import crontab
from django.conf import settings
from django.core.management import call_command, get_commands
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from core.tasks import run_management_command
from scheduler import admin as scheduler_admin
from scheduler import monitoring
from scheduler.models import ScheduledTaskRun, TaskOrder
from scheduler.schedule import BACKEND_CLEANUP_TASK, BEAT_TASK, JOBS, SCHEDULE_TIMEZONE
from scheduler.services import sync_jobs

DHAKA = ZoneInfo("Asia/Dhaka")

#: The schedule exactly as it stood in core/settings.py::CELERY_BEAT_SCHEDULE
#: before 2026-09-29, keyed by management command. Copied verbatim — including
#: day_of_week="fri", which the seed writes as "5". This is the baseline
#: ScheduleParityTests compares against, so do NOT "tidy" it to match
#: schedule.py; its whole job is to be the independent record.
OLD_SETTINGS_SCHEDULE: dict[str, dict] = {
    "send_weekly_rosters": {"day_of_week": "fri", "hour": 22, "minute": 0},
    "deactivate_past_lwd": {"hour": 0, "minute": 15},
    "sync_line_managers": {"hour": 2, "minute": 0},
    "sync_supervisors": {"hour": 2, "minute": 10},
    "send_policy_reminders": {"hour": 9, "minute": 0},
    "reconcile_shift_leave_usage": {"hour": 0, "minute": 45},
    "reconcile_cpl_grants": {"hour": 0, "minute": 40},
    "apply_monthly_accrual": {"hour": 0, "minute": 30, "day_of_month": 1},
    "open_al_cycles": {"hour": 1, "minute": 0},
    "close_al_cycles": {"hour": 1, "minute": 15},
    "apply_year_end_rollover": {
        "hour": 1, "minute": 0, "day_of_month": 1, "month_of_year": 1,
    },
    "generate_night_shift_consents": {"hour": 1, "minute": 30, "day_of_month": 1},
    "clearsessions": {"hour": 3, "minute": 0},
    "sync_attendance_punches": {"hour": 10, "minute": 0},
    "generate_gh_overtime": {"hour": 10, "minute": 20},
    "generate_gh_overtime_bpo": {"hour": 10, "minute": 25},
}


#: Jobs deliberately retimed AFTER the 2026-09-29 move, keyed by command. The
#: parity tests compare these jobs against the new spec instead of
#: OLD_SETTINGS_SCHEDULE, so the parity proof still covers everything else.
#: Each entry must say why. A retime also needs a scheduler data migration
#: for the already-seeded production row (seed never overwrites), e.g. 0003.
RETIMED_SINCE_MIGRATION: dict[str, dict] = {
    # 2026-10-01: CpL now carries forward 100% with no expiry, making this
    # the batch's live customer. Jan 15 is an idempotent catch-up run for
    # December grants/usage posted after Jan 1 (late roster uploads).
    "apply_year_end_rollover": {
        "hour": 1, "minute": 0, "day_of_month": "1,15", "month_of_year": 1,
    },
}


#: Jobs that did not exist before the 2026-09-29 move, keyed by command. They
#: have no settings-era baseline, so the parity tests compare them against
#: this spec instead. Each entry must say why it exists.
ADDED_SINCE_MIGRATION: dict[str, dict] = {
    # 2026-10-09: access-log retention (audit app, docs/ACCESS_LOG_PLAN.md).
    "prune_access_log": {"hour": 3, "minute": 15},
    # 2026-10-10: media on Google Drive (media_store, docs/MEDIA_GOOGLE_DRIVE_PLAN.md):
    # LRU-trim the local ciphertext cache; weekly index-vs-Drive check (a
    # no-op success until MEDIA_BACKEND=drive).
    "prune_media_cache": {"hour": 3, "minute": 30},
    "verify_media_drive": {"day_of_week": "sun", "hour": 4, "minute": 0},
}


def expected_crontab(command: str) -> crontab:
    """What `command`'s seeded crontab must match: its post-migration retime
    if it has one, its added-later spec if it is new, else the verbatim
    pre-2026-09-29 settings entry."""
    spec = (
        RETIMED_SINCE_MIGRATION.get(command)
        or ADDED_SINCE_MIGRATION.get(command)
        or OLD_SETTINGS_SCHEDULE[command]
    )
    return crontab(**spec)


class BeatScheduleSourceOfTruthTests(SimpleTestCase):
    """The invariant that keeps admin edits from being silently reverted."""

    def test_database_scheduler_is_active(self):
        self.assertEqual(
            settings.CELERY_BEAT_SCHEDULER,
            "django_celery_beat.schedulers:DatabaseScheduler",
        )
        self.assertTrue(settings.DJANGO_CELERY_BEAT_TZ_AWARE)

    def test_settings_beat_schedule_is_empty(self):
        # DatabaseScheduler.setup_schedule() pushes every entry here through
        # PeriodicTask.update_or_create() on each beat startup, so one entry
        # would reset that task's admin-edited schedule/args/enabled on the
        # next deploy. Put new jobs in scheduler/schedule.py::JOBS instead.
        self.assertEqual(
            settings.CELERY_BEAT_SCHEDULE, {},
            msg=(
                "CELERY_BEAT_SCHEDULE must stay empty while the database "
                "scheduler is active — see docs/SCHEDULER_ADMIN_PLAN.md."
            ),
        )

    def test_celery_timezone_is_dhaka(self):
        self.assertEqual(settings.CELERY_TIMEZONE, SCHEDULE_TIMEZONE)

    def test_result_expires_is_none(self):
        # Keeps DatabaseScheduler.install_default_entries() from creating an
        # enabled celery.backend_cleanup task on first start. Orbis has no
        # result backend, and the seed runs before beat does, so the parking
        # logic in services._disable_backend_cleanup could never win that race
        # on a fresh install.
        self.assertIsNone(settings.CELERY_RESULT_EXPIRES)


class JobDefinitionTests(SimpleTestCase):
    def test_every_job_names_a_real_management_command(self):
        available = set(get_commands())
        for job in JOBS:
            with self.subTest(job=job.name):
                self.assertIn(job.command, available)

    def test_job_names_and_commands_are_unique(self):
        names = [job.name for job in JOBS]
        commands = [job.command for job in JOBS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(commands), len(set(commands)))

    def test_jobs_cover_the_old_settings_schedule(self):
        self.assertEqual(
            {job.command for job in JOBS},
            set(OLD_SETTINGS_SCHEDULE) | set(ADDED_SINCE_MIGRATION),
        )

    def test_every_retime_names_a_seeded_job(self):
        self.assertLessEqual(
            set(RETIMED_SINCE_MIGRATION), {job.command for job in JOBS},
        )

    def test_jobs_are_ordered_by_time_of_day(self):
        times = [(int(job.hour), int(job.minute)) for job in JOBS]
        self.assertEqual(times, sorted(times), "JOBS drives rank; keep it sorted.")


class SeedTests(TestCase):
    def test_seed_creates_every_job(self):
        report = sync_jobs()

        self.assertEqual(len(report.created), len(JOBS))
        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))
        for job in JOBS:
            with self.subTest(job=job.name):
                task = PeriodicTask.objects.get(name=job.name)
                self.assertEqual(task.task, BEAT_TASK)
                self.assertEqual(json.loads(task.args), [job.command])
                self.assertEqual(json.loads(task.kwargs), {})
                self.assertTrue(task.enabled)
                self.assertFalse(task.one_off)
                self.assertIsNotNone(task.crontab)
                self.assertTrue(task.description)

    def test_every_crontab_is_dhaka(self):
        sync_jobs()
        zones = {str(c.timezone) for c in CrontabSchedule.objects.all()}
        self.assertEqual(zones, {SCHEDULE_TIMEZONE})

    def test_ranks_follow_job_order(self):
        sync_jobs()
        ranks = {
            order.task.name: order.rank
            for order in TaskOrder.objects.select_related("task")
        }
        self.assertEqual(
            [ranks[job.name] for job in JOBS], list(range(1, len(JOBS) + 1)),
        )

    def test_seed_is_idempotent_and_never_overwrites(self):
        sync_jobs()
        task = PeriodicTask.objects.get(name=JOBS[0].name)

        # Stand in for an admin edit: pause it and move it an hour later.
        task.enabled = False
        moved = CrontabSchedule.objects.create(
            minute="5", hour="4", day_of_week="*", day_of_month="*",
            month_of_year="*", timezone=SCHEDULE_TIMEZONE,
        )
        task.crontab = moved
        task.save()

        report = sync_jobs()

        self.assertEqual(report.created, [])
        self.assertEqual(report.updated, [])
        self.assertEqual(len(report.unchanged), len(JOBS))
        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))

        task.refresh_from_db()
        self.assertFalse(task.enabled, "seed must not re-enable a paused job")
        self.assertEqual(task.crontab_id, moved.pk, "seed must not retime a job")

    def test_seed_does_not_undo_a_manual_reorder(self):
        sync_jobs()
        order = TaskOrder.objects.get(task__name=JOBS[0].name)
        order.rank = 99
        order.save(update_fields=["rank"])

        sync_jobs()

        order.refresh_from_db()
        self.assertEqual(order.rank, 99)

    def test_seed_backfills_a_missing_rank(self):
        sync_jobs()
        TaskOrder.objects.filter(task__name=JOBS[0].name).delete()

        sync_jobs()

        self.assertEqual(TaskOrder.objects.get(task__name=JOBS[0].name).rank, 1)

    def test_force_resyncs_to_the_code_default(self):
        sync_jobs()
        job = JOBS[0]
        task = PeriodicTask.objects.get(name=job.name)
        task.enabled = False
        task.args = json.dumps(["nonsense"])
        task.save()

        report = sync_jobs(force=True, only=job.command)

        self.assertEqual(report.updated, [job.name])
        task.refresh_from_db()
        self.assertTrue(task.enabled)
        self.assertEqual(json.loads(task.args), [job.command])
        # --only must not touch anything else.
        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))

    def test_only_accepts_a_task_name(self):
        report = sync_jobs(only=JOBS[0].name)
        self.assertEqual(report.created, [JOBS[0].name])
        self.assertEqual(PeriodicTask.objects.count(), 1)

    def test_only_reports_an_unknown_job(self):
        report = sync_jobs(only="no-such-job")
        self.assertEqual(PeriodicTask.objects.count(), 0)
        self.assertTrue(report.notes)

    def test_dry_run_writes_nothing(self):
        report = sync_jobs(dry_run=True)
        self.assertEqual(len(report.created), len(JOBS))
        self.assertEqual(PeriodicTask.objects.count(), 0)

    def test_backend_cleanup_is_parked(self):
        # DatabaseScheduler creates this itself; Orbis has no result backend.
        cleanup = PeriodicTask.objects.create(
            name="celery.backend_cleanup",
            task=BACKEND_CLEANUP_TASK,
            crontab=CrontabSchedule.objects.create(
                minute="0", hour="4", day_of_week="*", day_of_month="*",
                month_of_year="*", timezone=SCHEDULE_TIMEZONE,
            ),
            enabled=True,
        )

        sync_jobs()

        cleanup.refresh_from_db()
        self.assertFalse(cleanup.enabled)

    def test_beat_startup_adds_nothing_of_its_own(self):
        """A real DatabaseScheduler startup must leave exactly our 16 rows.

        install_default_entries() adds celery.backend_cleanup whenever
        result_expires is truthy, and it runs AFTER the seed, so the only
        reliable guard is CELERY_RESULT_EXPIRES = None.
        """
        from django_celery_beat.schedulers import DatabaseScheduler

        from core.celery import app

        sync_jobs()
        DatabaseScheduler(app=app, lazy=True).setup_schedule()

        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))
        self.assertEqual(
            list(PeriodicTask.objects.exclude(task=BEAT_TASK)), [],
        )

    def test_management_command_runs(self):
        call_command("seed_beat_schedule", verbosity=0)
        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))
        # Second run must be a no-op, which is what makes it deploy-safe.
        call_command("seed_beat_schedule", verbosity=0)
        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))


class ScheduleParityTests(TestCase):
    """Every seeded crontab must fire when the old settings entry would have.

    Both sides are celery `crontab` objects evaluated in Asia/Dhaka, so this
    compares actual next-fire times rather than string equality — which is how
    it catches `day_of_week="5"` vs `"fri"` being the same thing, and a typo
    not being.
    """

    REFERENCES = (
        dt.datetime(2026, 1, 1, 0, 0, tzinfo=DHAKA),    # year boundary
        dt.datetime(2026, 3, 14, 12, 0, tzinfo=DHAKA),  # mid-month Saturday
        dt.datetime(2026, 9, 29, 23, 59, tzinfo=DHAKA),  # late-evening Tuesday
        dt.datetime(2026, 12, 31, 18, 30, tzinfo=DHAKA),
    )

    @staticmethod
    def _next_fire(schedule, reference: dt.datetime) -> dt.datetime:
        """Next fire time after `reference`, with "now" pinned to it.

        celery's remaining_estimate() consults the wall clock, so both sides
        get nowfun overridden — otherwise the two calls differ by the
        microseconds that elapsed between them.
        """
        schedule.nowfun = lambda: reference
        return reference + schedule.remaining_estimate(reference)

    def test_next_fire_times_match_the_old_settings_schedule(self):
        sync_jobs()
        for job in JOBS:
            old = expected_crontab(job.command)
            new = PeriodicTask.objects.get(name=job.name).crontab.schedule
            for reference in self.REFERENCES:
                with self.subTest(job=job.command, reference=reference):
                    self.assertEqual(
                        self._next_fire(old, reference),
                        self._next_fire(new, reference),
                    )

    def test_expanded_cron_fields_match(self):
        sync_jobs()
        for job in JOBS:
            old = expected_crontab(job.command)
            new = PeriodicTask.objects.get(name=job.name).crontab.schedule
            with self.subTest(job=job.command):
                self.assertEqual(old.minute, new.minute)
                self.assertEqual(old.hour, new.hour)
                self.assertEqual(old.day_of_week, new.day_of_week)
                self.assertEqual(old.day_of_month, new.day_of_month)
                self.assertEqual(old.month_of_year, new.month_of_year)


class ScheduleDisplayTests(SimpleTestCase):
    def test_day_of_week_labels(self):
        cases = {
            "*": "*",
            "": "*",
            "1,2,3,4,5": "Mon–Fri",
            "5": "Fri",
            "0,6": "Sat–Sun",   # Sunday sorts last (Mon=1 … Sun=7)
            "mon-fri": "Mon–Fri",
            "1,3,5": "Mon,Wed,Fri",
            "junk": "*",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(scheduler_admin._dow_to_names(value), expected)

    def test_twelve_hour_times(self):
        self.assertEqual(scheduler_admin._format_time_12h("0", "15"), "12:15 AM")
        self.assertEqual(scheduler_admin._format_time_12h("22", "0"), "10:00 PM")
        self.assertIsNone(scheduler_admin._format_time_12h("*/5", "0"))
        self.assertIsNone(scheduler_admin._format_time_12h("25", "0"))

    def test_format_schedule_falls_back_to_raw_cron(self):
        every_five = CrontabSchedule(
            minute="*/5", hour="*", day_of_week="*", day_of_month="*",
            month_of_year="*", timezone=SCHEDULE_TIMEZONE,
        )
        task = PeriodicTask(name="x", task=BEAT_TASK, crontab=every_five)
        self.assertIn("*/5", scheduler_admin.format_schedule(task))

    def test_format_schedule_pretty_and_narrowed(self):
        friday = CrontabSchedule(
            minute="0", hour="22", day_of_week="5", day_of_month="*",
            month_of_year="*", timezone=SCHEDULE_TIMEZONE,
        )
        task = PeriodicTask(name="x", task=BEAT_TASK, crontab=friday)
        self.assertEqual(scheduler_admin.format_schedule(task), "10:00 PM   Fri")

        new_year = CrontabSchedule(
            minute="0", hour="1", day_of_week="*", day_of_month="1",
            month_of_year="1", timezone=SCHEDULE_TIMEZONE,
        )
        task = PeriodicTask(name="y", task=BEAT_TASK, crontab=new_year)
        self.assertEqual(
            scheduler_admin.format_schedule(task), "1:00 AM   on 1 Jan",
        )

    def test_format_schedule_says_daily_not_star(self):
        every_day = CrontabSchedule(
            minute="15", hour="0", day_of_week="*", day_of_month="*",
            month_of_year="*", timezone=SCHEDULE_TIMEZONE,
        )
        task = PeriodicTask(name="x", task=BEAT_TASK, crontab=every_day)
        self.assertEqual(scheduler_admin.format_schedule(task), "12:15 AM   Daily")

    def test_format_schedule_monthly(self):
        first_of_month = CrontabSchedule(
            minute="30", hour="0", day_of_week="*", day_of_month="1",
            month_of_year="*", timezone=SCHEDULE_TIMEZONE,
        )
        task = PeriodicTask(name="x", task=BEAT_TASK, crontab=first_of_month)
        self.assertEqual(scheduler_admin.format_schedule(task), "12:30 AM   on day 1")

    def test_format_command_reads_the_args(self):
        task = PeriodicTask(
            name="x", task=BEAT_TASK, args=json.dumps(["sync_supervisors"]),
        )
        self.assertEqual(scheduler_admin.format_command(task), "sync_supervisors")

        task = PeriodicTask(name="y", task="some.other.task", args="[]")
        self.assertEqual(scheduler_admin.format_command(task), "—")

        task = PeriodicTask(name="z", task=BEAT_TASK, args="not json")
        self.assertEqual(scheduler_admin.format_command(task), "(unparseable args)")


class AdminSmokeTests(TestCase):
    """The admin screens are the whole point of this change, so render them."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.superuser = get_user_model().objects.create_superuser(
            email="root@sebpo.com", username="root@sebpo.com", password="x",
            first_name="Root", last_name="Admin",
        )

    def setUp(self):
        sync_jobs()
        self.client.force_login(self.superuser)

    def test_periodic_task_changelist_renders_in_rank_order(self):
        response = self.client.get("/admin/django_celery_beat/periodictask/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for job in JOBS:
            self.assertIn(job.name, body)
        # The Command column must show the management command, not the
        # identical run_management_command on every row.
        self.assertIn("sync_attendance_punches", body)
        positions = [body.index(job.name) for job in JOBS]
        self.assertEqual(positions, sorted(positions), "rank ordering lost")

    def test_periodic_task_change_form_renders(self):
        task = PeriodicTask.objects.get(name=JOBS[0].name)
        response = self.client.get(
            f"/admin/django_celery_beat/periodictask/{task.pk}/change/"
        )
        self.assertEqual(response.status_code, 200)

    def test_crontab_change_form_puts_hour_before_minute(self):
        crontab_row = PeriodicTask.objects.get(name=JOBS[0].name).crontab
        response = self.client.get(
            f"/admin/django_celery_beat/crontabschedule/{crontab_row.pk}/change/"
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertLess(body.index('name="hour"'), body.index('name="minute"'))

    def test_unused_schedule_models_are_not_registered(self):
        for model in ("intervalschedule", "solarschedule", "clockedschedule"):
            with self.subTest(model=model):
                response = self.client.get(f"/admin/django_celery_beat/{model}/")
                self.assertEqual(response.status_code, 404)

    def test_run_log_changelist_renders_and_is_read_only(self):
        monitoring.record_run(
            periodic_task_name="Clear expired sessions",
            task=BEAT_TASK,
            celery_task_id="t1",
            started_at=timezone.now(),
            state=states.SUCCESS,
            retval={"command": "clearsessions", "output": "ok"},
        )
        response = self.client.get("/admin/scheduler/scheduledtaskrun/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Clear expired sessions", response.content.decode())

        run = ScheduledTaskRun.objects.get()
        # Read-only: the change view redirects to the view-only page, and the
        # add view is gone entirely.
        self.assertEqual(
            self.client.get("/admin/scheduler/scheduledtaskrun/add/").status_code, 403,
        )
        response = self.client.get(
            f"/admin/scheduler/scheduledtaskrun/{run.pk}/change/", follow=True,
        )
        self.assertEqual(response.status_code, 200)


class RunWithArgumentsAdminTests(TestCase):
    """One ad-hoc dispatch with extra args; the row itself never changes."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.superuser = get_user_model().objects.create_superuser(
            email="root@sebpo.com", username="root@sebpo.com", password="x",
            first_name="Root", last_name="Admin",
        )

    def setUp(self):
        sync_jobs()
        self.client.force_login(self.superuser)
        self.task = PeriodicTask.objects.get(args=json.dumps(["sync_attendance_punches"]))
        self.url = f"/admin/django_celery_beat/periodictask/{self.task.pk}/run-with-arguments/"

    def test_change_form_links_to_it(self):
        response = self.client.get(f"/admin/django_celery_beat/periodictask/{self.task.pk}/change/")
        self.assertContains(response, self.url)

    def test_action_redirects_to_the_form_for_one_task(self):
        response = self.client.post("/admin/django_celery_beat/periodictask/", {
            "action": "run_with_arguments", "_selected_action": [self.task.pk],
        })
        self.assertRedirects(response, self.url)

    def test_dispatches_with_extra_args_and_leaves_the_row_alone(self):
        from unittest.mock import patch

        self.assertContains(self.client.get(self.url), "sync_attendance_punches")
        with patch.object(run_management_command, "apply_async") as mock_async:
            response = self.client.post(self.url, {"extra_args": "--from 2026-10-21 --to 2026-10-22"})
        self.assertEqual(response.status_code, 302)
        mock_async.assert_called_once()
        kwargs = mock_async.call_args.kwargs
        self.assertEqual(
            kwargs["args"],
            ["sync_attendance_punches", "--from", "2026-10-21", "--to", "2026-10-22"],
        )
        self.assertEqual(kwargs["headers"], {"periodic_task_name": self.task.name})
        self.task.refresh_from_db()
        self.assertEqual(json.loads(self.task.args), ["sync_attendance_punches"])

    def test_unknown_flag_is_rejected_without_dispatch(self):
        from unittest.mock import patch

        with patch.object(run_management_command, "apply_async") as mock_async:
            response = self.client.post(self.url, {"extra_args": "--when 2026-10-21"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unrecognized arguments")
        mock_async.assert_not_called()

    def test_non_command_task_is_refused(self):
        other = PeriodicTask.objects.exclude(task=BEAT_TASK).first() or PeriodicTask.objects.create(
            name="Not a command", task="celery.backend_cleanup", crontab=self.task.crontab,
        )
        response = self.client.get(
            f"/admin/django_celery_beat/periodictask/{other.pk}/run-with-arguments/",
        )
        self.assertEqual(response.status_code, 302)


class _FakeRequest:
    def __init__(self, periodic_task_name=None, task_id="abc"):
        self.headers = (
            {"periodic_task_name": periodic_task_name} if periodic_task_name else {}
        )
        self.id = task_id


class _FakeTask:
    def __init__(self, name, periodic_task_name=None, task_id="abc"):
        self.name = name
        self.request = _FakeRequest(periodic_task_name, task_id)


class RunHistoryTests(TestCase):
    def _record(self, **overrides):
        kwargs = {
            "periodic_task_name": "Sync supervisors",
            "task": BEAT_TASK,
            "celery_task_id": "task-1",
            "started_at": timezone.now(),
            "state": states.SUCCESS,
            "retval": {"command": "sync_supervisors", "output": "3 updated"},
        }
        kwargs.update(overrides)
        return monitoring.record_run(**kwargs)

    def test_records_a_successful_run(self):
        run = self._record()
        self.assertEqual(run.status, ScheduledTaskRun.STATUS_SUCCESS)
        self.assertEqual(run.output, "3 updated")
        self.assertEqual(run.summary, {"command": "sync_supervisors"})
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.failed_records, 0)

    def test_records_a_failure_with_the_exception(self):
        run = self._record(state=states.FAILURE, retval=ValueError("boom"))
        self.assertEqual(run.status, ScheduledTaskRun.STATUS_FAILURE)
        self.assertIn("ValueError: boom", run.error_message)
        self.assertEqual(run.failed_records, 1)

    def test_partial_failure_from_reported_counts(self):
        run = self._record(retval={"total_records": 10, "failed_records": 2})
        self.assertEqual(run.status, ScheduledTaskRun.STATUS_PARTIAL_FAILURE)
        self.assertEqual(run.total_records, 10)
        self.assertEqual(run.failed_records, 2)

    def test_deduplicates_on_celery_task_id(self):
        self.assertIsNotNone(self._record())
        self.assertIsNone(self._record())
        self.assertEqual(ScheduledTaskRun.objects.count(), 1)

    def test_retry_is_not_an_outcome(self):
        self.assertIsNone(self._record(state=states.RETRY))
        self.assertEqual(ScheduledTaskRun.objects.count(), 0)

    def test_non_beat_tasks_are_ignored(self):
        # Everything dispatched via core.tasks.defer() lands here — no header,
        # so no row. Otherwise every SMTP fan-out would be logged.
        deferred = _FakeTask("core.tasks.run_deferred")
        self.assertEqual(monitoring.periodic_task_name_from(deferred), "")

        monitoring.record_finish(
            task=deferred, task_id="abc", retval=None, state=states.SUCCESS,
        )
        self.assertEqual(ScheduledTaskRun.objects.count(), 0)

    def test_signal_handlers_record_a_beat_dispatch(self):
        task = _FakeTask(BEAT_TASK, periodic_task_name="Clear expired sessions")
        monitoring.remember_start(task=task, task_id="abc")
        monitoring.record_finish(
            task=task,
            task_id="abc",
            retval={"command": "clearsessions", "output": ""},
            state=states.SUCCESS,
        )
        run = ScheduledTaskRun.objects.get()
        self.assertEqual(run.periodic_task_name, "Clear expired sessions")
        self.assertEqual(run.status, ScheduledTaskRun.STATUS_SUCCESS)

    def test_output_is_truncated(self):
        run = self._record(retval={"output": "x" * (monitoring.MAX_TEXT_LENGTH + 500)})
        self.assertLessEqual(
            len(run.output), monitoring.MAX_TEXT_LENGTH + len("\n… [truncated]"),
        )
        self.assertTrue(run.output.endswith("[truncated]"))

    def test_duration_is_none_without_finish(self):
        run = ScheduledTaskRun(started_at=timezone.now())
        self.assertIsNone(run.duration)


class RunManagementCommandTaskTests(TestCase):
    def test_returns_command_and_captured_output(self):
        result = run_management_command("seed_beat_schedule")
        self.assertEqual(result["command"], "seed_beat_schedule")
        self.assertIn("created", result["output"])
        self.assertEqual(PeriodicTask.objects.count(), len(JOBS))

    def test_passes_through_extra_args(self):
        result = run_management_command("seed_beat_schedule", "--dry-run")
        self.assertEqual(result["command"], "seed_beat_schedule --dry-run")
        self.assertIn("dry-run", result["output"])
        self.assertEqual(PeriodicTask.objects.count(), 0)

    def test_a_failing_command_still_raises(self):
        with self.assertRaises(Exception):
            run_management_command("no_such_command")
