"""Scheduler app models.

Two small models sitting beside django_celery_beat's own tables:

- ``TaskOrder`` — a display rank for a PeriodicTask, so the admin changelist
  reads in execution order instead of alphabetically.
- ``ScheduledTaskRun`` — one row per beat-dispatched run, so "did the nightly
  fire?" is a database question rather than an SSH log grep.

Neither has a CustomUser FK, so neither migration needs the collation CONVERT
step described in CLAUDE.md — the only FK here points at PeriodicTask, whose pk
is a bigint.
"""

from __future__ import annotations

from django.db import models
from django_celery_beat.models import PeriodicTask


class TaskOrder(models.Model):
    """Display rank for a periodic task in the admin changelist."""

    task = models.OneToOneField(
        PeriodicTask,
        on_delete=models.CASCADE,
        related_name="order_meta",
        verbose_name="Periodic task",
    )
    rank = models.PositiveIntegerField(
        default=0,
        db_index=True,
        verbose_name="Order",
        help_text="Lower numbers appear first. Seeded by time of day.",
    )

    class Meta:
        ordering = ["rank", "task__name"]
        verbose_name = "Task order"
        verbose_name_plural = "Task order"

    def __str__(self) -> str:
        return f"{self.rank} – {self.task.name}"


class ScheduledTaskRun(models.Model):
    """Execution log for a celery-beat-dispatched periodic task.

    Written by scheduler/monitoring.py from Celery's task_prerun/task_postrun
    signals, keyed off the ``periodic_task_name`` message header that
    django_celery_beat injects. Tasks dispatched any other way — everything
    that goes through ``core.tasks.defer()`` — carry no such header and are
    deliberately not logged here.
    """

    STATUS_SUCCESS = "success"
    STATUS_PARTIAL_FAILURE = "partial_failure"
    STATUS_FAILURE = "failure"

    STATUS_CHOICES = (
        (STATUS_SUCCESS, "Success"),
        (STATUS_PARTIAL_FAILURE, "Partial failure"),
        (STATUS_FAILURE, "Failure"),
    )

    periodic_task_name = models.CharField(max_length=200, db_index=True)
    task = models.CharField(max_length=255, db_index=True)
    celery_task_id = models.CharField(max_length=255, blank=True, db_index=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, db_index=True)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    total_records = models.PositiveIntegerField(default=0)
    failed_records = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)
    output = models.TextField(
        blank=True,
        help_text="Captured stdout/stderr of the management command, truncated.",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "periodic_task_name"]
        indexes = [
            models.Index(
                fields=["periodic_task_name", "started_at"],
                name="sched_run_name_start_idx",
            ),
            models.Index(fields=["status", "started_at"], name="sched_run_status_idx"),
        ]
        verbose_name = "Scheduled task run"
        verbose_name_plural = "Scheduled task runs"

    def __str__(self) -> str:
        return (
            f"{self.periodic_task_name} – {self.status} – "
            f"{self.started_at:%Y-%m-%d %H:%M}"
        )

    @property
    def duration(self):
        if not self.started_at or not self.finished_at:
            return None
        return self.finished_at - self.started_at
