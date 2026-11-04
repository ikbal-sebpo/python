"""Seeding the database beat schedule from scheduler/schedule.py::JOBS.

``sync_jobs`` is deliberately get-or-create shaped: an existing PeriodicTask row
is left completely alone, because from the first seed onwards the Django admin
is the source of truth. That's what lets the data migration and the management
command both run on every deploy without ever reverting an admin edit.

``force=True`` opts into the opposite behaviour for one run — re-sync a row back
to the code default in schedule.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from django.db import transaction
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from scheduler.models import TaskOrder
from scheduler.schedule import BACKEND_CLEANUP_TASK, BEAT_TASK, JOBS, Job


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated)


def _crontab_for(job: Job) -> CrontabSchedule:
    """Reuse an identical crontab row rather than creating near-duplicates."""
    schedule, _ = CrontabSchedule.objects.get_or_create(**job.crontab_kwargs)
    return schedule


def _task_defaults(job: Job, schedule: CrontabSchedule) -> dict:
    return {
        "task": BEAT_TASK,
        "crontab": schedule,
        "interval": None,
        "solar": None,
        "clocked": None,
        "args": json.dumps(job.task_args),
        "kwargs": json.dumps({}),
        "enabled": job.enabled,
        "description": job.description,
        "one_off": False,
    }


@transaction.atomic
def sync_jobs(
    *,
    force: bool = False,
    only: str | None = None,
    dry_run: bool = False,
) -> SyncReport:
    """Create (or, with ``force``, re-sync) the PeriodicTask rows for JOBS.

    ``only`` matches a job by name or by management-command name.
    """
    report = SyncReport()

    jobs = JOBS
    if only:
        needle = only.strip().lower()
        jobs = tuple(
            job for job in JOBS
            if needle in (job.name.lower(), job.command.lower())
        )
        if not jobs:
            report.notes.append(f"No job matches {only!r}.")
            return report

    for rank, job in enumerate(JOBS, start=1):
        if job not in jobs:
            continue

        existing = PeriodicTask.objects.filter(name=job.name).first()

        if existing and not force:
            report.unchanged.append(job.name)
            # Only fill in a missing rank — never move a row someone reordered
            # in the admin. Same reasoning as leaving the schedule alone.
            _ensure_rank(existing, rank, adjust=False, dry_run=dry_run)
            continue

        if dry_run:
            (report.updated if existing else report.created).append(job.name)
            continue

        schedule = _crontab_for(job)
        if existing:
            for attr, value in _task_defaults(job, schedule).items():
                setattr(existing, attr, value)
            existing.save()
            report.updated.append(job.name)
            task = existing
        else:
            task = PeriodicTask.objects.create(
                name=job.name, **_task_defaults(job, schedule)
            )
            report.created.append(job.name)

        _ensure_rank(task, rank, dry_run=dry_run)

    if not only:
        note = _disable_backend_cleanup(dry_run=dry_run)
        if note:
            report.notes.append(note)

    return report


def _ensure_rank(
    task: PeriodicTask, rank: int, *, adjust: bool = True, dry_run: bool = False,
) -> None:
    """Give the task a TaskOrder row.

    ``adjust=False`` creates a missing row but leaves an existing rank alone,
    so a deploy-time seed can't undo a manual reorder.
    """
    if dry_run:
        return
    order = TaskOrder.objects.filter(task=task).first()
    if order is None:
        TaskOrder.objects.create(task=task, rank=rank)
    elif adjust and order.rank != rank:
        order.rank = rank
        order.save(update_fields=["rank"])


def _disable_backend_cleanup(*, dry_run: bool = False) -> str:
    """Park celery.backend_cleanup.

    DatabaseScheduler installs this entry itself (``result_expires`` defaults to
    1 day). Orbis has no result backend, so there is nothing for it to clean —
    disable it rather than leaving an unexplained live job in the admin list.
    """
    cleanup = PeriodicTask.objects.filter(task=BACKEND_CLEANUP_TASK).first()
    if cleanup is None or not cleanup.enabled:
        return ""
    if not dry_run:
        cleanup.enabled = False
        cleanup.description = (
            "Disabled by scheduler.services: Orbis stores no task results "
            "(CELERY_TASK_IGNORE_RESULT), so there is no result backend to "
            "clean up. django_celery_beat creates this row automatically."
        )
        cleanup.save(update_fields=["enabled", "description"])
    return f"Disabled {BACKEND_CLEANUP_TASK}."
