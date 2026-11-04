"""Unit tests for the TimeLy office-presence client and dashboard formatter.

No live network — every test mocks ``attendance.timely.requests.get``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings

import attendance.timely as timely_mod
from attendance.timely import fetch_office_presence

from core.views import _format_office, _format_presence


SAMPLE_PAYLOAD = {
    "date": "2026-04-24",
    "working_in_office_count": 1285,
    "employees": [
        {
            "employee_id": "917",
            "name": "Md. Shamim Shahnewaz",
            "office": ["REMOTE", "mhk"],
            "first_punch_at": "2026-04-24T12:02:41",
            "last_punch_at":  "2026-04-24T21:13:25",
            "punch_count": 22,
        },
        {
            "employee_id": "918",
            "name": "Other Person",
            "office": ["MBD"],
            "first_punch_at": "2026-04-24T09:15:00",
            "last_punch_at":  "2026-04-24T09:15:00",
            "punch_count": 1,
        },
    ],
}


def _mock_response(json_data, status_code: int = 200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error", response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class FetchOfficePresenceTests(TestCase):

    def setUp(self):
        cache.clear()
        # Reset the module-level "unset warned" latch so we can re-test the
        # disabled-config branch in isolation.
        timely_mod._unset_warned = False

    @override_settings(TIMELY_API_BASE_URL="", TIMELY_API_TOKEN="")
    @patch("attendance.timely.requests.get")
    def test_blank_base_url_returns_empty_and_skips_call(self, mock_get):
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(result, {})
        mock_get.assert_not_called()

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_confirmed_payload_parses(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_PAYLOAD)
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertIn("917", result)
        self.assertIn("918", result)
        self.assertEqual(result["917"]["name"], "Md. Shamim Shahnewaz")

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_unexpected_top_level_returns_empty(self, mock_get):
        mock_get.return_value = _mock_response(["not", "a", "dict"])
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(result, {})

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_missing_employees_key_returns_empty(self, mock_get):
        mock_get.return_value = _mock_response({"date": "2026-04-24", "count": 0})
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(result, {})

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_http_error_returns_empty(self, mock_get):
        mock_get.return_value = _mock_response({}, status_code=500)
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(result, {})

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_connection_error_returns_empty(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("nope")
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(result, {})

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_json_decode_error_returns_empty(self, mock_get):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")
        mock_get.return_value = resp
        result = fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(result, {})

    @override_settings(TIMELY_API_BASE_URL="https://t.example", TIMELY_API_TOKEN="x")
    @patch("attendance.timely.requests.get")
    def test_cache_hit_avoids_second_call(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_PAYLOAD)
        fetch_office_presence(date(2026, 4, 24))
        fetch_office_presence(date(2026, 4, 24))
        self.assertEqual(mock_get.call_count, 1)


class FormatOfficeTests(TestCase):

    def test_filters_remote_picks_first_physical(self):
        self.assertEqual(_format_office(["REMOTE", "mhk"]), "MHK")

    def test_uppercases_and_trims(self):
        self.assertEqual(_format_office(["mbd"]), "MBD")

    def test_caps_at_six_chars(self):
        self.assertEqual(_format_office(["raowa-extra"]), "RAOWA-")

    def test_falls_back_to_first_when_only_remote(self):
        self.assertEqual(_format_office(["REMOTE"]), "REMOTE")

    def test_empty_list_returns_dash(self):
        self.assertEqual(_format_office([]), "—")

    def test_non_list_returns_dash(self):
        self.assertEqual(_format_office(None), "—")
        self.assertEqual(_format_office("MBD"), "—")


class FormatPresenceTests(TestCase):

    def test_none_record_renders_pending(self):
        self.assertEqual(_format_presence(None), "Pending")

    def test_record_with_first_punch_renders_in(self):
        # Punch from earlier today within the last hour.
        from django.utils import timezone
        now = timezone.localtime().replace(tzinfo=None)
        first_in = (now - timedelta(minutes=10)).replace(microsecond=0)
        record = {
            "office": ["REMOTE", "mhk"],
            "first_punch_at": first_in.isoformat(),
            "last_punch_at":  first_in.isoformat(),
        }
        result = _format_presence(record)
        self.assertTrue(result.startswith("In MHK · "))

    def test_old_last_punch_renders_left(self):
        from django.utils import timezone
        now = timezone.localtime().replace(tzinfo=None)
        first_in = (now - timedelta(hours=6)).replace(microsecond=0)
        last_seen = (now - timedelta(hours=2)).replace(microsecond=0)
        record = {
            "office": ["MBD"],
            "first_punch_at": first_in.isoformat(),
            "last_punch_at":  last_seen.isoformat(),
        }
        result = _format_presence(record)
        # A stale last punch with a first punch renders "<office> · in → out"
        # (the departed/"left the office" state).
        self.assertTrue(result.startswith("MBD · "))
        self.assertIn("→", result)

    def test_no_punches_renders_dash(self):
        record = {"office": ["MBD"], "first_punch_at": None, "last_punch_at": None}
        self.assertEqual(_format_presence(record), "—")

    def test_unparseable_punch_renders_dash(self):
        record = {"office": ["MBD"], "first_punch_at": "not-a-date", "last_punch_at": ""}
        self.assertEqual(_format_presence(record), "—")


class SyncAttendancePunchesDateRangeTests(TestCase):
    """--date / --from / --to resolve to the right days, each synced in turn."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.user = get_user_model().objects.create_user(
            email="punch@example.com", username="punch@example.com", password="x",
            employee_id="917",
        )

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command

        def presence(day):
            return {"917": {
                "office": ["mhk"],
                "first_punch_at": f"{day}T09:00:00",
                "last_punch_at": f"{day}T18:00:00",
                "punch_count": 4,
            }}

        out = StringIO()
        with patch(
            "attendance.management.commands.sync_attendance_punches.fetch_office_presence",
            side_effect=presence,
        ) as mock_fetch:
            call_command("sync_attendance_punches", *args, stdout=out)
        return [c.args[0] for c in mock_fetch.call_args_list]

    def test_range_syncs_every_day_inclusive(self):
        from attendance.models import AttendancePunch

        days = self._run("--from", "2026-10-21", "--to", "2026-10-23")
        self.assertEqual(days, [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)])
        self.assertEqual(
            sorted(AttendancePunch.objects.filter(employee=self.user).values_list("date", flat=True)),
            days,
        )

    def test_from_alone_runs_through_yesterday(self):
        start = date.today() - timedelta(days=3)
        days = self._run("--from", start.isoformat())
        self.assertEqual(days[0], start)
        self.assertEqual(days[-1], date.today() - timedelta(days=1))

    def test_default_and_single_date_unchanged(self):
        self.assertEqual(self._run(), [date.today() - timedelta(days=1)])
        self.assertEqual(self._run("--date", "2026-10-21"), [date(2026, 10, 21)])

    def test_bad_arguments_are_refused_before_any_fetch(self):
        from django.core.management.base import CommandError

        yesterday = date.today() - timedelta(days=1)
        cases = [
            ("--date", "2026-10-21", "--from", "2026-10-20"),
            ("--to", "2026-10-21"),
            ("--from", "2026-10-22", "--to", "2026-10-21"),
            ("--from", "2026-13-01"),
            ("--date", date.today().isoformat()),
            ("--from", (yesterday - timedelta(days=31)).isoformat()),
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(CommandError):
                    self._run(*args)
