"""Outbound client for SEBPO's TimeLy attendance API.

Single public function: ``fetch_office_presence(date_obj)`` returns a mapping
``{employee_id_str: presence_record}`` for the given date. The dashboard uses
this to overlay punch-in info on the "working today" bucket.

Contract:
    - Never raises. Every failure path returns ``{}`` and logs at WARNING (or
      INFO for the "not configured" case). Dashboard rendering must not break
      because TimeLy is down.
    - Cached per-date for 60 seconds via Django's default cache backend.
    - On multi-worker gunicorn, ``LocMemCache`` is per-process — worst case is
      N cache misses per minute (N = worker count). With 3 workers and a 60s
      TTL that's ≤180 calls/hour, well within tolerance. No Redis needed.

Confirmed payload shape (from production sample 2026-09-16, updated from the
original 2026-04-24 sample to add duration/duration_seconds/shift/shifts —
see below):
    {
      "date": "2026-04-24",
      "working_in_office_count": 1285,
      "employees": [
        {
          "employee_id": "917",
          "name": "...",
          "office": ["REMOTE", "mhk"],
          "shift": "Morning",
          "first_punch_at": "2026-04-24T12:02:41",
          "last_punch_at":  "2026-04-24T21:13:25",
          "punch_count": 22,
          "duration": "09:10:44",
          "duration_seconds": 33044,
          "shifts": [
            {
              "shift": "Morning",
              "punch_in": "2026-04-24T12:02:41",
              "punch_out": "2026-04-24T21:13:25",
              "duration": "09:10:44",
              "duration_seconds": 33044,
              "punch_count": 22,
              "office": ["REMOTE", "mhk"]
            }
          ]
        },
        ...
      ]
    }

`duration_seconds` was added 2026-09-16 at our request, after discovering
first_punch_at/last_punch_at can be truncated to the queried calendar date
for a shift crossing midnight — a night-shift employee's punch-out the next
morning was getting dropped, understating their duration by hours.
duration_seconds isn't affected (confirmed across ~4,000 employee-days: it
always equals last_punch_at − first_punch_at once both are correct — the
truncation was in the timestamps, not the subtraction). `sync_attendance_punches`
prefers duration_seconds over deriving it locally for exactly this reason.
`shifts` is a per-shift breakdown (unused so far — every record observed to
date has exactly one entry, i.e. no split shifts yet) that duration_seconds
sums over; kept in raw_payload as insurance in case that ever matters.
"""

from __future__ import annotations

import logging
from datetime import date

import requests
from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5
_CACHE_TTL_SECONDS = 60
_PATH = "/api/attendance/office-presence"

# Module-level latch so we only log "not configured" once per worker — there's
# no point repeating it every dashboard hit.
_unset_warned = False


def fetch_office_presence(date_obj: date) -> dict[str, dict]:
    """Return ``{employee_id_str: record_dict}`` for ``date_obj``.

    Empty dict on every failure path. Cached for 60 seconds per date.
    """
    if not getattr(settings, "TIMELY_API_BASE_URL", ""):
        global _unset_warned
        if not _unset_warned:
            logger.info("TimeLy presence fetch skipped — TIMELY_API_BASE_URL unset")
            _unset_warned = True
        return {}

    cache_key = f"timely:office_presence:{date_obj.isoformat()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = _fetch(date_obj)
    cache.set(cache_key, result, _CACHE_TTL_SECONDS)
    return result


def _fetch(date_obj: date) -> dict[str, dict]:
    """Issue the HTTP call. Caller wraps with caching."""
    base_url = settings.TIMELY_API_BASE_URL.rstrip("/")
    token = getattr(settings, "TIMELY_API_TOKEN", "")
    url = f"{base_url}{_PATH}"
    headers = {
        "X-ATTENDANCE-API-KEY": token,
        "Accept": "application/json",
    }
    params = {"date": date_obj.isoformat()}

    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("TimeLy presence fetch failed: %s", exc)
        return {}
    except ValueError as exc:  # JSONDecodeError subclass
        logger.warning("TimeLy presence response was not JSON: %s", exc)
        return {}

    return _normalize_payload(payload)


def _normalize_payload(payload) -> dict[str, dict]:
    """Convert the TimeLy response into ``{employee_id_str: record}``.

    Locked to the confirmed shape: top-level dict with an ``employees`` list.
    Anything else logs a warning and returns ``{}``.
    """
    if not isinstance(payload, dict):
        logger.warning(
            "TimeLy presence response had unexpected top-level type: %s",
            type(payload).__name__,
        )
        return {}

    employees = payload.get("employees")
    if not isinstance(employees, list):
        logger.warning(
            "TimeLy presence response missing 'employees' list. keys=%s",
            list(payload.keys()),
        )
        return {}

    result: dict[str, dict] = {}
    for record in employees:
        if not isinstance(record, dict):
            continue
        emp_id = record.get("employee_id")
        if emp_id is None:
            continue
        result[str(emp_id)] = record
    return result
