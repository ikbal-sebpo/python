"""Background-task dispatch (Celery + Redis).

`defer(func, *args, **kwargs)` is the single seam between "this should not
block the request" call sites and the execution backend:

- With TASK_QUEUE_ENABLED (Redis configured), the call is handed to the
  Celery worker (`celery -A core worker` under supervisor) and the request
  returns immediately.
- Otherwise it runs inline — dev and tests keep the exact pre-queue
  semantics, and a broker outage can be survived by flipping the env var.

Rules for deferred functions: module-level (importable by dotted path),
arguments JSON-serializable (pass pks/strings, not model instances — UUID
pks are coerced to str here), and idempotent where possible — failures
retry up to 3 times with backoff.
"""

from __future__ import annotations

import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


def _jsonable(value):
    """Coerce common non-JSON pk types at the top level (UUID → str)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@shared_task(
    name="core.tasks.run_deferred",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 3},
)
def run_deferred(path: str, args: list, kwargs: dict) -> None:
    """Generic executor behind defer(): import the function and call it.

    Exceptions propagate so Celery's retry policy applies — deferred
    functions that must never retry should catch internally (deliver_mail
    swallows per-recipient SMTP errors, for example).
    """
    import_string(path)(*args, **kwargs)


@shared_task(name="core.tasks.run_management_command")
def run_management_command(name: str, *args: str) -> dict:
    """celery-beat entry point for the nightly/periodic management commands.

    The schedule lives in django_celery_beat's tables (managed from the Django
    admin — see docs/SCHEDULER_ADMIN_PLAN.md); each row dispatches this task
    with the command name in ``args``. Keeping them as management commands
    means they stay runnable by hand via manage.py.

    The return value is what scheduler.ScheduledTaskRun records, so the
    command's own output is captured into it — otherwise every nightly logs as
    a bare SUCCESS with nothing to inspect. Output capture is best-effort: a
    command that writes to the real stdout/stderr directly, or fails, still
    runs exactly as before.
    """
    import io

    from django.core.management import call_command

    logger.info("beat: running management command %s %s", name, args or "")
    buffer = io.StringIO()
    try:
        call_command(name, *args, stdout=buffer, stderr=buffer)
    except Exception:
        # Surface whatever the command managed to say before it died, then let
        # Celery mark the task FAILURE (which is what the run log keys off).
        output = buffer.getvalue().strip()
        if output:
            logger.error("beat: %s output before failure:\n%s", name, output)
        raise
    output = buffer.getvalue().strip()
    if output:
        logger.info("beat: %s output:\n%s", name, output)
    return {"command": " ".join([name, *args]), "output": output}


def defer(func, *args, **kwargs) -> None:
    if getattr(settings, "TASK_QUEUE_ENABLED", False):
        path = f"{func.__module__}.{func.__qualname__}"
        try:
            run_deferred.delay(
                path,
                [_jsonable(a) for a in args],
                {k: _jsonable(v) for k, v in kwargs.items()},
            )
            return
        except Exception:
            # Broker down — degrade to inline rather than dropping the work.
            logger.exception(
                "Task queue unavailable; running %s inline.", func.__qualname__,
            )
    func(*args, **kwargs)
