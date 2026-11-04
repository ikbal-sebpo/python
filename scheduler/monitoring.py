"""Record every celery-beat-dispatched run into ScheduledTaskRun.

django_celery_beat stamps each message it publishes with a
``periodic_task_name`` header (django_celery_beat/schedulers.py:89 — also set by
its admin's "Run selected tasks" action). That header is the whole trick: it
tells a generic Celery signal handler "this execution belongs to periodic task
X", with no per-task instrumentation.

Anything dispatched another way — every ``core.tasks.defer()`` call, i.e. the
SMTP fan-outs and WeasyPrint renders — carries no such header and is ignored.
Logging those here would bury the nightlies in noise.

Ported from smsbot's scheduler/services/celery_monitoring.py, keeping the
generic recorder and dropping that project's foreclosure-specific reporting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from celery import states
from celery.signals import task_postrun, task_prerun
from django.utils import timezone

from scheduler.models import ScheduledTaskRun

logger = logging.getLogger(__name__)

#: Cap on the stored output/error blobs — a chatty command shouldn't be able to
#: write an unbounded row.
MAX_TEXT_LENGTH = 20_000

#: task_prerun fires in the worker process, task_postrun in the same one, so an
#: in-memory handoff of the start time is enough. A worker killed mid-task loses
#: the entry and the run simply isn't logged (beat's own log still has the
#: dispatch line).
_STARTED_AT: dict[str, Any] = {}


def periodic_task_name_from(bound_task: Any) -> str:
    """Return the beat task name for this execution, or "" if not from beat."""
    request = getattr(bound_task, "request", None)
    headers = getattr(request, "headers", None) or {}
    name = headers.get("periodic_task_name") or getattr(
        request, "periodic_task_name", None
    )
    return str(name) if name else ""


def _truncate(value: str) -> str:
    value = str(value or "")
    if len(value) <= MAX_TEXT_LENGTH:
        return value
    return value[:MAX_TEXT_LENGTH] + "\n… [truncated]"


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth=depth + 1) for v in value]
    return str(value)


def _summary_from(retval: Any) -> dict[str, Any]:
    if retval is None:
        return {}
    if isinstance(retval, BaseException):
        return {"exception_type": type(retval).__name__, "exception": str(retval)}
    safe = _json_safe(retval)
    if isinstance(safe, dict):
        return safe
    if isinstance(safe, list):
        return {"items": safe, "item_count": len(safe)}
    return {"result": safe}


def _counts_from(summary: dict[str, Any]) -> tuple[int, int]:
    """Best-effort record counts, for commands that report them."""

    def first_int(*keys: str) -> int:
        for key in keys:
            if key in summary:
                try:
                    return max(0, int(summary[key]))
                except (TypeError, ValueError):
                    continue
        return 0

    total = first_int("total_records", "total", "processed", "count")
    if not total and "item_count" in summary:
        total = first_int("item_count")

    failed = first_int("failed_records", "failed", "error_count", "errors_count")
    if not failed:
        errors = summary.get("errors")
        if isinstance(errors, (list, tuple, set)):
            failed = len(errors)
        elif errors:
            failed = 1
    return total, failed


def _status_for(state: str | None, *, failed_records: int) -> str:
    if str(state or "").upper() == states.SUCCESS:
        return (
            ScheduledTaskRun.STATUS_PARTIAL_FAILURE
            if failed_records
            else ScheduledTaskRun.STATUS_SUCCESS
        )
    return ScheduledTaskRun.STATUS_FAILURE


def record_run(
    *,
    periodic_task_name: str,
    task: str,
    celery_task_id: str,
    started_at,
    state: str | None,
    retval: Any = None,
) -> ScheduledTaskRun | None:
    """Write one run row. De-duplicated on celery_task_id."""
    periodic_task_name = (periodic_task_name or "").strip()
    if not periodic_task_name:
        return None
    # A RETRY isn't an outcome — wait for the terminal state.
    if str(state or "").upper() == states.RETRY:
        return None

    celery_task_id = str(celery_task_id or "")
    if celery_task_id and ScheduledTaskRun.objects.filter(
        celery_task_id=celery_task_id
    ).exists():
        return None

    summary = _summary_from(retval)
    total_records, failed_records = _counts_from(summary)
    status = _status_for(state, failed_records=failed_records)

    output = str(summary.pop("output", "") or "")
    error_message = ""
    if status == ScheduledTaskRun.STATUS_FAILURE:
        if isinstance(retval, BaseException):
            error_message = f"{type(retval).__name__}: {retval}"
        else:
            try:
                error_message = json.dumps(_json_safe(retval), sort_keys=True)
            except (TypeError, ValueError):
                error_message = str(retval)
        failed_records = failed_records or 1

    return ScheduledTaskRun.objects.create(
        periodic_task_name=periodic_task_name,
        task=task or "",
        celery_task_id=celery_task_id,
        status=status,
        started_at=started_at,
        finished_at=timezone.now(),
        total_records=total_records,
        failed_records=failed_records,
        summary=_json_safe(summary),
        output=_truncate(output),
        error_message=_truncate(error_message),
    )


@task_prerun.connect(weak=False)
def remember_start(sender=None, task_id=None, task=None, **_kwargs) -> None:
    bound = task or sender
    if task_id and periodic_task_name_from(bound):
        _STARTED_AT[str(task_id)] = timezone.now()


@task_postrun.connect(weak=False)
def record_finish(
    sender=None, task_id=None, task=None, retval=None, state=None, **_kwargs
) -> None:
    bound = task or sender
    periodic_task_name = periodic_task_name_from(bound)
    if not periodic_task_name:
        return

    key = str(task_id or "")
    started_at = _STARTED_AT.pop(key, None) or timezone.now()
    try:
        record_run(
            periodic_task_name=periodic_task_name,
            task=getattr(bound, "name", "") or "",
            celery_task_id=key,
            started_at=started_at,
            state=state,
            retval=retval,
        )
    except Exception:
        # Never let bookkeeping break the job that just ran.
        logger.exception(
            "Failed to record ScheduledTaskRun for %s", periodic_task_name
        )
