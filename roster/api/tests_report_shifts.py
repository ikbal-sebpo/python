"""Contract tests for the external report-shifts API."""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import JobLocation, WorkLocation
from leave.models import LeaveApplication
from roster.api.shift_writes import SERVICE_ACCOUNT_EMAIL
from roster.models import Shift, ShiftAttribute, ShiftType

User = get_user_model()
TOKEN = "test-external-token"
URL = "/api/v1/external/report-shifts/"


@override_settings(EXTERNAL_API_TOKEN=TOKEN)
class ExternalReportShiftAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User.objects.create(email=SERVICE_ACCOUNT_EMAIL, username=SERVICE_ACCOUNT_EMAIL, is_active=False)
        mhk = JobLocation.objects.create(name="MHK", code="MHK", is_active=True)
        wfo = WorkLocation.objects.create(name="Work from Office (WFO)", is_active=True)
        cls.work = ShiftType.objects.create(name="EVE", full_name="Evening", category=ShiftType.Category.WORK, shift_start_time=time(14), shift_end_time=time(23))
        cls.attribute = ShiftAttribute.objects.create(shift_type=cls.work, name="EVE 1-10", start_time=time(13), end_time=time(22))
        cls.leave = ShiftType.objects.create(name="AL", category=ShiftType.Category.LEAVE)
        cls.alice = User.objects.create_user(email="alice@example.com", username="alice@example.com", password="x", first_name="Alice", last_name="A", employee_id="917", job_location=mhk, work_location=wfo)
        cls.bob = User.objects.create_user(email="bob@example.com", username="bob@example.com", password="x", first_name="Bob", last_name="B", employee_id="918")
        cls.former = User.objects.create_user(email="former@example.com", username="former@example.com", password="x", first_name="Former", last_name="Employee", employee_id="919", is_active=False)
        cls.day = date(2026, 8, 1)
        cls.alice_shift = Shift.objects.create(employee=cls.alice, date=cls.day, shift_type=cls.work, shift_attribute=cls.attribute, note="Coverage")
        leave_application = LeaveApplication.objects.create(employee=cls.bob, leave_type=cls.leave, start_date=cls.day, end_date=cls.day, applied_by=cls.bob, status=LeaveApplication.Status.APPROVED)
        Shift.objects.create(employee=cls.bob, date=cls.day, shift_type=cls.leave, leave_application=leave_application)
        Shift.objects.create(employee=cls.former, date=cls.day, shift_type=cls.work)

    def setUp(self):
        self.authed = APIClient(HTTP_AUTHORIZATION=f"Token {TOKEN}")

    def test_requires_external_token_and_a_valid_bounded_range(self):
        self.assertEqual(APIClient().get(URL, {"start_date": "2026-08-01", "end_date": "2026-08-01"}).status_code, 401)
        self.assertEqual(self.authed.get(URL).status_code, 400)
        self.assertEqual(self.authed.get(URL, {"start_date": "2026-08-02", "end_date": "2026-08-01"}).status_code, 400)
        self.assertEqual(self.authed.get(URL, {"start_date": "2026-01-01", "end_date": "2026-06-01"}).status_code, 400)

    def test_all_active_users_receive_stable_report_fields(self):
        response = self.authed.get(URL, {"start_date": "2026-08-01", "end_date": "2026-08-01"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["meta"]["employee_scope"], "all")
        self.assertFalse(payload["meta"]["include_inactive"])
        rows = {row["employee_id"]: row for row in payload["results"]}
        self.assertEqual(sorted(rows), ["917", "918"])
        alice = rows["917"]
        self.assertEqual(alice["orbis_shift_id"], self.alice_shift.pk)
        self.assertEqual(alice["date"], "2026-08-01")
        self.assertEqual(alice["shift_type_id"], self.work.pk)
        self.assertEqual(alice["shift_code"], "EVE")
        self.assertEqual(alice["shift_full_name"], "Evening")
        self.assertEqual(alice["shift_attribute_id"], self.attribute.pk)
        self.assertEqual(alice["shift_label"], "EVE 1-10")
        self.assertEqual(alice["category"], "WORK")
        self.assertTrue(alice["is_working_shift"])
        self.assertEqual(alice["start_time"], "13:00")
        self.assertEqual(alice["end_time"], "22:00")
        self.assertEqual(alice["office_location"], "MHK")
        self.assertEqual(alice["work_location"], "WFO")
        self.assertEqual(alice["note"], "Coverage")
        self.assertFalse(alice["leave_locked"])
        self.assertIsNotNone(alice["updated_at"])
        self.assertTrue(rows["918"]["leave_locked"])
        self.assertIsNone(rows["918"]["office_location"])

    def test_supports_one_or_many_users_and_inactive_history(self):
        response = self.authed.get(URL, {"start_date": "2026-08-01", "end_date": "2026-08-01", "employee_ids": "917,919"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["employee_ids"], ["917", "919"])
        self.assertEqual([row["employee_id"] for row in response.json()["results"]], ["917", "919"])
        response = self.authed.get(URL, {"start_date": "2026-08-01", "end_date": "2026-08-01", "include_inactive": "true"})
        self.assertEqual(response.json()["count"], 3)

    def test_rejects_unknown_users_and_paginates(self):
        response = self.authed.get(URL, {"start_date": "2026-08-01", "end_date": "2026-08-01", "employee_id": "missing"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("employee_ids", response.json())
        response = self.authed.get(URL, {"start_date": "2026-08-01", "end_date": "2026-08-01", "include_inactive": "true", "page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)
        self.assertIsNotNone(response.json()["next"])
