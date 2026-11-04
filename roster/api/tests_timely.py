"""Contract tests for the TimeLy-facing read API: /api/v1/roster/ and
/api/v1/roster/<employee_id>/ (roster/api/timely.py)."""

from __future__ import annotations

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import JobLocation, WorkLocation
from roster.models import Shift, ShiftAttribute, ShiftType

User = get_user_model()

_TIMELY_TOKEN = "test-timely-token"


@override_settings(TIMELY_API_TOKEN=_TIMELY_TOKEN)
class TimelyRosterAPITests(TestCase):
    """Contract tests for /api/v1/roster/ and /api/v1/roster/<employee_id>/."""

    @classmethod
    def setUpTestData(cls):
        # Locations
        mhk = JobLocation.objects.create(name="MHK", code="MHK", is_active=True)
        mbd = JobLocation.objects.create(name="MBD", code="MBD", is_active=True)
        wfo = WorkLocation.objects.create(name="Work from Office (WFO)", is_active=True)

        # Shift types: one WORK, one OFF, one LEAVE.
        cls.eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK,
            shift_start_time=time(14, 0), shift_end_time=time(23, 0),
        )
        cls.eve_attr = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 1-10",
            start_time=time(13, 0), end_time=time(22, 0),
        )
        cls.off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        cls.al = ShiftType.objects.create(name="AL", category=ShiftType.Category.LEAVE)

        # Employees with employee_id set.
        cls.alice = User.objects.create_user(
            email="alice@example.com", username="alice@example.com",
            first_name="Alice", last_name="A", password="x",
            employee_id="917", job_location=mhk, work_location=wfo,
        )
        cls.bob = User.objects.create_user(
            email="bob@example.com", username="bob@example.com",
            first_name="Bob", last_name="B", password="x",
            employee_id="918", job_location=mbd, work_location=wfo,
        )
        # Employee with NO employee_id — must be filtered out.
        cls.carol = User.objects.create_user(
            email="carol@example.com", username="carol@example.com",
            first_name="Carol", last_name="C", password="x",
            employee_id=None,
        )
        # Inactive employee with an id — must also be filtered out.
        cls.dave = User.objects.create_user(
            email="dave@example.com", username="dave@example.com",
            first_name="Dave", last_name="D", password="x",
            employee_id="919", is_active=False,
        )

        # Shifts on the test day.
        cls.day = date(2026, 5, 6)  # Wednesday
        Shift.objects.create(
            employee=cls.alice, date=cls.day,
            shift_type=cls.eve, shift_attribute=cls.eve_attr,
        )
        Shift.objects.create(
            employee=cls.bob, date=cls.day, shift_type=cls.off,
        )
        Shift.objects.create(
            employee=cls.carol, date=cls.day, shift_type=cls.eve,
        )
        Shift.objects.create(
            employee=cls.dave, date=cls.day, shift_type=cls.eve,
        )
        # Alice has shifts on the surrounding days too — for range tests.
        Shift.objects.create(
            employee=cls.alice, date=date(2026, 5, 5),
            shift_type=cls.eve, shift_attribute=cls.eve_attr,
        )
        Shift.objects.create(
            employee=cls.alice, date=date(2026, 5, 9),
            shift_type=cls.al,
        )

    def setUp(self):
        self.api = APIClient()
        self.authed = APIClient(HTTP_AUTHORIZATION=f"Token {_TIMELY_TOKEN}")

    # ----- Auth -----

    def test_anonymous_request_is_401(self):
        resp = self.api.get("/api/v1/roster/?date=2026-05-06")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_is_401(self):
        c = APIClient(HTTP_AUTHORIZATION="Token nope")
        resp = c.get("/api/v1/roster/?date=2026-05-06")
        self.assertEqual(resp.status_code, 401)

    # ----- Daily endpoint -----

    def test_daily_missing_date_is_400(self):
        resp = self.authed.get("/api/v1/roster/")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("date", resp.json())

    def test_daily_bad_date_format_is_400(self):
        resp = self.authed.get("/api/v1/roster/?date=2026-13-99")
        self.assertEqual(resp.status_code, 400)

    def test_daily_returns_only_active_employees_with_employee_id(self):
        resp = self.authed.get("/api/v1/roster/?date=2026-05-06")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        ids = sorted(row["employee_id"] for row in payload)
        # Carol has employee_id=None and Dave is inactive — both filtered.
        self.assertEqual(ids, ["917", "918"])

    def test_daily_work_shift_field_correctness(self):
        resp = self.authed.get("/api/v1/roster/?date=2026-05-06")
        rows = {row["employee_id"]: row for row in resp.json()}
        alice = rows["917"]
        self.assertEqual(alice["working_day"], "2026-05-06")
        self.assertEqual(alice["name"], "Alice A")
        self.assertEqual(alice["shift_attribute"], "EVE 1-10")
        self.assertEqual(alice["shift_start_time"], "13:00")
        self.assertEqual(alice["shift_end_time"], "22:00")
        self.assertEqual(alice["office_location"], "MHK")
        self.assertEqual(alice["work_location"], "WFO")

    def test_daily_off_shift_falls_back_to_type_name_and_nulls_locations(self):
        resp = self.authed.get("/api/v1/roster/?date=2026-05-06")
        rows = {row["employee_id"]: row for row in resp.json()}
        bob = rows["918"]
        self.assertEqual(bob["shift_attribute"], "OFF")
        self.assertIsNone(bob["shift_start_time"])
        self.assertIsNone(bob["shift_end_time"])
        self.assertIsNone(bob["office_location"])
        self.assertIsNone(bob["work_location"])

    def test_daily_no_shifts_returns_empty_list(self):
        resp = self.authed.get("/api/v1/roster/?date=2026-05-07")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    # ----- Per-employee endpoint -----

    def test_employee_range_missing_dates_is_400(self):
        resp = self.authed.get("/api/v1/roster/917/")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertIn("start_date", body)
        self.assertIn("end_date", body)

    def test_employee_range_end_before_start_is_400(self):
        resp = self.authed.get(
            "/api/v1/roster/917/?start_date=2026-05-10&end_date=2026-05-05"
        )
        self.assertEqual(resp.status_code, 400)

    def test_employee_range_too_large_is_400(self):
        resp = self.authed.get(
            "/api/v1/roster/917/?start_date=2026-01-01&end_date=2026-06-01"
        )
        self.assertEqual(resp.status_code, 400)

    def test_employee_range_unknown_id_is_404(self):
        resp = self.authed.get(
            "/api/v1/roster/9999999/?start_date=2026-05-05&end_date=2026-05-09"
        )
        self.assertEqual(resp.status_code, 404)

    def test_employee_range_happy_path_returns_only_that_employee(self):
        resp = self.authed.get(
            "/api/v1/roster/917/?start_date=2026-05-04&end_date=2026-05-10"
        )
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [r["working_day"] for r in rows],
            ["2026-05-05", "2026-05-06", "2026-05-09"],
        )
        # Last row is the AL leave: shift_attribute falls back to "AL".
        self.assertEqual(rows[-1]["shift_attribute"], "AL")
        self.assertIsNone(rows[-1]["office_location"])

    def test_employee_range_inactive_user_is_404(self):
        # Dave has employee_id=919 but is_active=False — endpoint should not
        # leak his data.
        resp = self.authed.get(
            "/api/v1/roster/919/?start_date=2026-05-05&end_date=2026-05-09"
        )
        self.assertEqual(resp.status_code, 404)
