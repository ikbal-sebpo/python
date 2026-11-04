"""Make the Celery app importable as `core.celery_app` so shared_task
decorators bind to it when Django starts (standard Celery/Django wiring)."""

from core.celery import app as celery_app

__all__ = ("celery_app",)
