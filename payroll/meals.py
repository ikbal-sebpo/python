"""Outbound client for the meals system's external meal-count API.

``fetch_meal_counts(start_date, end_date)`` returns a mapping
``{employee_id_str: total_meals}`` for the (inclusive) date range — backs the
payroll report's live Meal column via a direct call.

``pull_meal_counts(period, actor)`` is the other caller: it fetches the same
data and persists it as ``MealCount`` rows (Phase 7 — see docs/PAYROLL_PLAN.md),
so the calculation engine (``payroll/inputs.py::collect_meal_counts``) reads a
frozen snapshot rather than hitting the live API at calculate time. Called
from ``payroll.services.lock_inputs``, which lets a fetch failure block the
``INPUTS_LOCKED`` transition — this is the one payroll input source that
hard-blocks on failure instead of soft-warning, unlike the report's use of
``fetch_meal_counts`` above.

Contract (see docs/PAYROLL_PLAN.md's meals.sebpo.net note — this deliberately
does NOT copy attendance/timely.py's "never raises, return {}" shape):
    - Not configured (``MEAL_COUNTS_API_TOKEN`` unset) is a quiet, expected
      state — logs at INFO once per worker and returns ``{}``. Fine for a
      dashboard-style overlay and for local/dev envs that don't have the
      integration wired up.
    - An actual failure (network error, non-2xx, unparseable/unexpected
      payload) raises ``MealsUnavailable`` instead of swallowing it into an
      empty dict. On a payroll-facing report, a silent ``{}`` is
      indistinguishable from "everyone genuinely had zero meals" — that's
      the wrong default here even though it's the right one for the TimeLy
      dashboard overlay. Callers decide how to surface it (the payroll
      report shows a non-blocking warning banner rather than hard-failing
      the whole page — see payroll/reports.py::build_payroll_report).
    - Cached per date-range for 300 seconds via Django's default cache
      backend (LocMemCache; per-process, same tradeoff as attendance/timely.py).
      A cached empty-but-successful result is cached too; a raised failure
      is never cached, so the next request retries.
    - Employees with zero meals aren't returned by the API at all — treat a
      missing key as 0, same as the API does.
    - ``total_meals`` is already deduction-eligible only — the meals system
      excludes fully company-subsidized/complimentary meals (special-occasion
      catering: Eid, New Year, etc.) from the count itself, even for an
      employee whose regular meals are otherwise salary-deducted. Confirmed
      with the meals dev 2026-09-09. No filtering needed on this side.

Confirmed payload shape (see /home/smrashel/Downloads/mealsapi.txt):
    {
      "start_date": "2026-09-01",
      "end_date": "2026-09-30",
      "employee_count": 2,
      "total_meal_count": 18,
      "results": [
        {"employee_id": "917", "total_meals": 10},
        {"employee_id": "1844", "total_meals": 8}
      ]
    }
"""

from __future__ import annotations

import logging
from datetime import date

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction


logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_CACHE_TTL_SECONDS = 300
_PATH = "/api/v1/external/meal-counts/"

# Module-level latch so we only log "not configured" once per worker.
_unset_warned = False


class MealsUnavailable(Exception):
    """Raised when the meals API is configured but the call actually failed
    (network error, bad status, unparseable/unexpected payload). Distinct
    from "not configured", which is a silent no-op — see module docstring."""


def fetch_meal_counts(start_date: date, end_date: date) -> dict[str, int]:
    """Return ``{employee_id_str: total_meals}`` for [start_date, end_date].

    ``{}`` if the integration isn't configured. Raises ``MealsUnavailable``
    on an actual fetch/parse failure — never returns a silently-wrong ``{}``
    for that case. Successful results (including empty ones) are cached for
    5 minutes per date range; failures are not cached.
    """
    if not getattr(settings, "MEAL_COUNTS_API_TOKEN", ""):
        global _unset_warned
        if not _unset_warned:
            logger.info("Meal count fetch skipped — MEAL_COUNTS_API_TOKEN unset")
            _unset_warned = True
        return {}

    cache_key = f"meals:counts:{start_date.isoformat()}:{end_date.isoformat()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = _fetch(start_date, end_date)
    cache.set(cache_key, result, _CACHE_TTL_SECONDS)
    return result


def _fetch(start_date: date, end_date: date) -> dict[str, int]:
    """Issue the HTTP call. Caller wraps with caching. Raises
    ``MealsUnavailable`` on any failure — see module docstring."""
    base_url = settings.MEAL_COUNTS_API_BASE_URL.rstrip("/")
    token = settings.MEAL_COUNTS_API_TOKEN
    url = f"{base_url}{_PATH}"
    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }
    params = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }

    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("Meal count fetch failed: %s", exc)
        raise MealsUnavailable(str(exc)) from exc
    except ValueError as exc:  # JSONDecodeError subclass
        logger.warning("Meal count response was not JSON: %s", exc)
        raise MealsUnavailable(str(exc)) from exc

    return _normalize_payload(payload)


def _normalize_payload(payload) -> dict[str, int]:
    """Convert the meals API response into ``{employee_id_str: total_meals}``.

    Locked to the confirmed shape: top-level dict with a ``results`` list.
    Anything else raises ``MealsUnavailable`` — see module docstring."""
    if not isinstance(payload, dict):
        msg = f"Meal count response had unexpected top-level type: {type(payload).__name__}"
        logger.warning(msg)
        raise MealsUnavailable(msg)

    results = payload.get("results")
    if not isinstance(results, list):
        msg = f"Meal count response missing 'results' list. keys={list(payload.keys())}"
        logger.warning(msg)
        raise MealsUnavailable(msg)

    out: dict[str, int] = {}
    for record in results:
        if not isinstance(record, dict):
            continue
        emp_id = record.get("employee_id")
        if emp_id is None:
            continue
        try:
            out[str(emp_id)] = int(record.get("total_meals") or 0)
        except (TypeError, ValueError):
            continue
    return out


def pull_meal_counts(period, actor) -> "PayrollInputBatch":
    """Fetch this period's meal counts and persist them as ``MealCount`` rows
    under a ``PayrollInputBatch`` audit envelope — the one input source
    ``payroll.services.lock_inputs`` calls directly, since it's pulled from an
    external system rather than uploaded. Unlike this module's soft-warn use
    on the payroll report, a fetch failure here is NOT swallowed: it leaves a
    FAILED batch behind and raises ``ValidationError``, which
    ``lock_inputs`` lets propagate to block the ``INPUTS_LOCKED`` transition
    (docs/PAYROLL_PLAN.md's meals.sebpo.net note — the engine must never
    silently calculate against a stale or missing meal count).

    Re-pulling an already-pulled period replaces its ``MealCount`` rows
    (delete-and-rebuild, same principle as the engine itself) so a re-lock
    after Reopen always reflects the freshest count.
    """
    from payroll.models import MealCount, PayrollInputBatch

    User = get_user_model()

    try:
        counts = fetch_meal_counts(period.start_date, period.end_date)
    except MealsUnavailable as exc:
        PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.MEAL_COUNT, source=PayrollInputBatch.Source.API,
            period=period, status=PayrollInputBatch.Status.FAILED,
            report_json={"error": str(exc)}, added_by=actor, updated_by=actor,
        )
        raise ValidationError(
            f"Could not fetch meal counts from meals.sebpo.net — inputs stay unlocked. ({exc})"
        ) from exc

    employee_pks_by_code = dict(
        User.objects.filter(employee_id__in=counts.keys()).values_list("employee_id", "pk")
    )
    with transaction.atomic():
        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.MEAL_COUNT, source=PayrollInputBatch.Source.API,
            period=period, status=PayrollInputBatch.Status.APPLIED,
            row_count=len(counts), added_by=actor, updated_by=actor,
        )
        MealCount.objects.filter(period=period).delete()
        MealCount.objects.bulk_create([
            MealCount(employee_id=pk, period=period, total_meals=total, batch=batch)
            for code, total in counts.items()
            if (pk := employee_pks_by_code.get(code)) is not None
        ])
    return batch
