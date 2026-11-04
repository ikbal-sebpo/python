"""Celery application for Orbis.

Standard wiring: settings come from Django settings with the ``CELERY_``
namespace, tasks are autodiscovered from installed apps' ``tasks.py``.
`core/settings.py` loads `.env` itself, so the worker and beat processes only
need DJANGO_SETTINGS_MODULE (defaulted below).

Run under supervisor (see deploy/README.md):

    celery -A core worker --concurrency=2 -l info
    celery -A core beat -l info
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("orbis")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
