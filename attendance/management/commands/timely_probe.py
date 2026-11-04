"""Smoke / discovery command for the TimeLy office-presence endpoint.

Usage::

    python manage.py timely_probe                  # today
    python manage.py timely_probe --date 2026-04-24

Prints the raw response shape, the first couple of records, and the count
mapped by ``attendance.timely._normalize_payload`` — useful both for the
first-time sanity check on a new VPS and for re-validating the parser if
TimeLy ever changes its response format.

Bypasses the cache and the wrapping ``fetch_office_presence`` so what you see
is the raw upstream truth, not a stale cached blob or our own normaliser
swallowing an error.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from attendance.timely import _normalize_payload, _PATH


class Command(BaseCommand):
    help = "Hit the TimeLy office-presence endpoint and print the raw shape."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=str, default=None,
            help="Date as YYYY-MM-DD. Defaults to today.",
        )

    def handle(self, *args, **opts):
        base_url = getattr(settings, "TIMELY_API_BASE_URL", "")
        token = getattr(settings, "TIMELY_API_TOKEN", "")
        if not base_url:
            raise CommandError(
                "TIMELY_API_BASE_URL is unset — populate it in .env first."
            )
        if not token:
            self.stdout.write(self.style.WARNING(
                "TIMELY_API_TOKEN is empty — call will likely 401."
            ))

        if opts["date"]:
            try:
                target = datetime.strptime(opts["date"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError(f"Bad --date: {exc}")
        else:
            target = date.today()

        url = f"{base_url.rstrip('/')}{_PATH}"
        headers = {"X-ATTENDANCE-API-KEY": token, "Accept": "application/json"}
        params = {"date": target.isoformat()}

        self.stdout.write(f"GET {url}?date={target.isoformat()}")
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            raise CommandError(f"Request failed: {exc}")

        self.stdout.write(f"  HTTP {response.status_code}  ({len(response.content)} bytes)")
        if response.status_code != 200:
            self.stdout.write(self.style.ERROR(response.text[:500]))
            return

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommandError(f"Response was not JSON: {exc}")

        self.stdout.write(f"\nTop-level type: {type(payload).__name__}")
        if isinstance(payload, dict):
            self.stdout.write("Top-level keys:")
            for k, v in payload.items():
                summary = f"len={len(v)}" if isinstance(v, (list, dict, str)) else repr(v)
                self.stdout.write(f"  {k}: {type(v).__name__}  {summary}")
            employees = payload.get("employees")
            if isinstance(employees, list) and employees:
                self.stdout.write("\nFirst record:")
                self.stdout.write(json.dumps(employees[0], indent=2, default=str))
                if len(employees) > 1:
                    self.stdout.write("\nSecond record:")
                    self.stdout.write(json.dumps(employees[1], indent=2, default=str))

        normalized = _normalize_payload(payload)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"_normalize_payload mapped {len(normalized)} employees by employee_id."
        ))
