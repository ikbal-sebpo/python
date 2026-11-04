# Seed the 16 periodic jobs that used to live in CELERY_BEAT_SCHEDULE in
# core/settings.py, so a fresh install (and CI) has a working schedule with no
# manual step. See docs/SCHEDULER_ADMIN_PLAN.md.
#
# get_or_create by name, never update: from here on the Django admin is the
# source of truth, and this migration must not be able to revert an admin edit
# if it is ever re-applied. `manage.py seed_beat_schedule` is the re-runnable
# entry point (and the only one with --force).
#
# The job definitions come from scheduler/schedule.py::JOBS — a plain frozen
# dataclass tuple with no model imports, so importing it here is safe. The rows
# themselves go through apps.get_model(), per the project's data-migration
# convention.
import json

from django.db import migrations

from scheduler.schedule import BEAT_TASK, JOBS


def seed(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    TaskOrder = apps.get_model("scheduler", "TaskOrder")

    for rank, job in enumerate(JOBS, start=1):
        if PeriodicTask.objects.filter(name=job.name).exists():
            continue

        crontab, _ = CrontabSchedule.objects.get_or_create(**job.crontab_kwargs)
        task = PeriodicTask.objects.create(
            name=job.name,
            task=BEAT_TASK,
            crontab=crontab,
            args=json.dumps(job.task_args),
            kwargs=json.dumps({}),
            enabled=job.enabled,
            description=job.description,
            one_off=False,
        )
        TaskOrder.objects.get_or_create(task=task, defaults={"rank": rank})


def unseed(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name__in=[job.name for job in JOBS], task=BEAT_TASK,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
