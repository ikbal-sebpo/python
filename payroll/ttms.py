"""Outbound client for TTMS (ttms.sebpo.net)'s daily-hours API — read-only,
department-scoped worked-hours per employee per date. Backs General BPO's
government-holiday overtime generation (`payroll/gh_overtime.py::
generate_bpo_gh_overtime_for_date`, `manage.py generate_gh_overtime_bpo`),
the same role `payroll/meals.py` plays for meal counts.

Contract, deliberately NOT `attendance/timely.py`'s "never raises, return
{}" shape — same reasoning as meals.py: this feeds payroll money, so a
silent empty result is indistinguishable from "nobody worked," which is the
wrong default here.
    - Not configured (`TTMS_API_TOKEN` unset) raises `TTMSUnavailable`
      rather than degrading quietly. Unlike meals.py's optional dashboard
      column, this data *is* the whole point of the caller — a silent
      no-op would make every holiday look like "nobody in General BPO
      worked," which is misleading, not a graceful degradation.
    - An actual failure (network error, non-2xx, bad auth, unparseable or
      unexpected payload) also raises `TTMSUnavailable`.
    - No caching — called at most a handful of times a day (once per beat
      run, or a manual command invocation), unlike the meals report's
      per-page-load reads.

Confirmed payload shape (tested live against the sample request in
/home/smrashel/Downloads/TTMS_ops_daily_hours_api.txt, 2026-09-17 — that
file only documented the request, not the response):

    POST /api/v1/operations/daily-hours/
    {"department_name": "General BPO", "start_date": "2026-09-04", "end_date": "2026-09-04"}
    ->
    {"filters": {...}, "count": 4, "results": [
        {"department": "General BPO", "team": "VeraData", "employee_id": 756,
         "name": "Md. Masudullah", "date": "2026-09-04",
         "work_hours": "08:00:00", "work_hour": 28800}
    ]}

Omitting `team_name` returns every team in the department in one call — this
client never passes it, so one call per date covers all of General BPO.
`work_hour` is already whole seconds worked that date — NOT a flat flag; a
regular-day sample came back with real partial hours (23040, 19200), which
is why the caller pays exact hours rather than granting a flat day (see
`generate_bpo_gh_overtime_for_date`'s docstring). An unrecognized
`department_name` is not an error — the API returns `200` with an empty
`results` list.
"""

from __future__ import annotations

import logging
from datetime import date

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_PATH = "/api/v1/operations/daily-hours/"


class TTMSUnavailable(Exception):
    """Raised whenever daily-hours data can't be trusted right now — not
    configured, a network/auth failure, or an unexpected payload shape.
    There is no silent-empty path (see module docstring); callers must
    handle this explicitly rather than treating a swallowed failure as
    "nobody worked"."""


def fetch_daily_hours(department_name: str, start_date: date, end_date: date) -> list[dict]:
    """Return the raw `results` list for [start_date, end_date] (inclusive)
    — each row `{"employee_id": int, "name": str, "team": str,
    "date": "YYYY-MM-DD", "work_hours": "HH:MM:SS", "work_hour": <seconds>}`.

    Raises `TTMSUnavailable` if `TTMS_API_BASE_URL`/`TTMS_API_TOKEN` aren't
    set, or on any fetch/parse failure.
    """
    base_url = getattr(settings, "TTMS_API_BASE_URL", "")
    token = getattr(settings, "TTMS_API_TOKEN", "")
    if not base_url or not token:
        raise TTMSUnavailable(
            "TTMS integration not configured — set TTMS_API_BASE_URL/TTMS_API_TOKEN."
        )

    url = f"{base_url.rstrip('/')}{_PATH}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "department_name": department_name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        logger.warning("TTMS daily-hours fetch failed: %s", exc)
        raise TTMSUnavailable(str(exc)) from exc
    except ValueError as exc:  # JSONDecodeError subclass
        logger.warning("TTMS daily-hours response was not JSON: %s", exc)
        raise TTMSUnavailable(str(exc)) from exc

    return _normalize_payload(body)


def _normalize_payload(payload) -> list[dict]:
    if not isinstance(payload, dict):
        msg = f"TTMS daily-hours response had unexpected top-level type: {type(payload).__name__}"
        logger.warning(msg)
        raise TTMSUnavailable(msg)

    results = payload.get("results")
    if not isinstance(results, list):
        msg = f"TTMS daily-hours response missing 'results' list. keys={list(payload.keys())}"
        logger.warning(msg)
        raise TTMSUnavailable(msg)

    out = []
    for record in results:
        if not isinstance(record, dict):
            continue
        if record.get("employee_id") is None or record.get("date") is None:
            continue
        out.append(record)
    return out
