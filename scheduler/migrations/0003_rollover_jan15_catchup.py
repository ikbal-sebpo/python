# 2026-10-01: CpL grants no longer expire and carry forward 100% across
# calendar years (leave/migrations/0012_cpl_no_expiry_full_carry.py), which
# makes `apply_year_end_rollover` a live batch. The command is idempotent, so
# its "Year-end rollover" periodic task now fires on Jan 1 AND Jan 15 — the
# second run catches December grants/usage posted after Jan 1 (late roster
# uploads). scheduler/schedule.py::JOBS carries the new default for fresh
# installs; this migration moves the row 0002 already seeded on existing
# databases, which `seed_beat_schedule` deliberately never touches.
#
# Respects the admin-is-source-of-truth rule: the row is only retimed if its
# crontab is still the seeded default (01:00 on Jan 1). A row someone has
# already changed in the admin is left alone.
from django.db import migrations
from django.utils import timezone

from scheduler.schedule import jobs_by_command

JOB = jobs_by_command()["apply_year_end_rollover"]
OLD_DAY_OF_MONTH = "1"


def _crontab(apps, day_of_month):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    kwargs = {**JOB.crontab_kwargs, "day_of_month": day_of_month}
    schedule, _ = CrontabSchedule.objects.get_or_create(**kwargs)
    return schedule


def _bump_change_marker(apps):
    """The historical PeriodicTask model has none of django_celery_beat's
    save-signals, so poke PeriodicTasks.last_update ourselves — that's what
    a running beat polls to know it must re-read the schedule."""
    PeriodicTasks = apps.get_model("django_celery_beat", "PeriodicTasks")
    PeriodicTasks.objects.update_or_create(
        ident=1, defaults={"last_update": timezone.now()},
    )


def _retime(apps, from_day, to_day):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    task = PeriodicTask.objects.filter(name=JOB.name).select_related("crontab").first()
    if task is None or task.crontab is None:
        return  # not seeded here (fresh install seeds the new default itself)
    c = task.crontab
    at_default = (
        c.minute == JOB.minute and c.hour == JOB.hour
        and c.day_of_week == JOB.day_of_week
        and c.day_of_month == from_day
        and c.month_of_year == JOB.month_of_year
    )
    if not at_default:
        return  # admin-edited; leave it
    task.crontab = _crontab(apps, to_day)
    task.description = JOB.description
    task.save(update_fields=["crontab", "description"])
    _bump_change_marker(apps)


def forwards(apps, schema_editor):
    _retime(apps, OLD_DAY_OF_MONTH, JOB.day_of_month)


def backwards(apps, schema_editor):
    _retime(apps, JOB.day_of_month, OLD_DAY_OF_MONTH)


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0002_seed_beat_schedule"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
