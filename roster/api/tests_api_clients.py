"""Roster API with per-app `ApiClient` tokens, and the date-range roster mode.
docs/DIRECTORY_API_PLAN.md §4.3, §4.4, §5.5 (phase 3)."""

from __future__ import annotations

from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import ApiClient
from roster.models import Shift, ShiftType

User = get_user_model()

READ, WRITE, DIRECTORY = (
    ApiClient.Scope.ROSTER_READ, ApiClient.Scope.ROSTER_WRITE, ApiClient.Scope.DIRECTORY_READ,
)
LEGACY = "legacy-timely-token"


def _token(name, *scopes, **extra):
    client = ApiClient.objects.create(name=name, scopes=list(scopes), **extra)
    return client.issue_token()


def _api(token):
    return APIClient(HTTP_AUTHORIZATION=f"Token {token}")


@override_settings(TIMELY_API_TOKEN=LEGACY)
class RosterApiClientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK,
            shift_start_time=time(14, 0), shift_end_time=time(23, 0),
        )
        cls.first = date(2026, 5, 1)
        people = [
            User.objects.create_user(
                email=f"{eid}@sebpo.com", username=f"{eid}@sebpo.com", employee_id=eid, first_name=name,
            )
            for eid, name in (("918", "Bob"), ("917", "Alice"))
        ]
        gone = User.objects.create_user(
            email="919@sebpo.com", username="919@sebpo.com", employee_id="919", is_active=False,
        )
        for offset in range(3):
            for person in (*people, gone):
                Shift.objects.create(employee=person, date=cls.first + timedelta(days=offset), shift_type=eve)
        cls.service = User.objects.create_user(
            email="svc@sebpo.internal", username="svc@sebpo.internal", is_active=False,
        )

    # -- scopes ---------------------------------------------------------------

    def test_roster_read_token_reads_the_roster(self):
        api = _api(_token("timely", READ))
        self.assertEqual(api.get("/api/v1/roster/", {"date": "2026-05-01"}).status_code, 200)
        self.assertEqual(api.get("/api/v1/roster/917/", {
            "start_date": "2026-05-01", "end_date": "2026-05-03",
        }).status_code, 200)
        self.assertEqual(api.get("/api/v1/shift-types/").status_code, 200)
        self.assertEqual(api.get("/api/v1/holidays/").status_code, 200)
        self.assertEqual(api.get("/api/v1/external/report-shifts/", {
            "start_date": "2026-05-01", "end_date": "2026-05-03",
        }).status_code, 200)

    def test_scopes_are_enforced(self):
        directory_only = _api(_token("hr-app", DIRECTORY))
        self.assertEqual(directory_only.get("/api/v1/roster/", {"date": "2026-05-01"}).status_code, 403)
        # The frozen legacy employee list belongs to the directory scope.
        self.assertEqual(directory_only.get("/api/v1/employees/").status_code, 200)
        roster_only = _api(_token("rnc", READ))
        self.assertEqual(roster_only.get("/api/v1/employees/").status_code, 403)

    def test_env_token_keeps_working(self):
        api = _api(LEGACY)
        self.assertEqual(api.get("/api/v1/roster/", {"date": "2026-05-01"}).status_code, 200)
        self.assertEqual(api.get("/api/v1/employees/").status_code, 200)

    def test_revoked_client_is_refused(self):
        token = _token("timely", READ)
        ApiClient.objects.filter(name="timely").update(is_active=False)
        self.assertEqual(_api(token).get("/api/v1/roster/", {"date": "2026-05-01"}).status_code, 401)

    def test_shift_writes_need_roster_write(self):
        url = "/api/v1/external/shifts/batch/"
        body = {"actor_employee_id": "917", "items": []}
        reader = _api(_token("timely", READ))
        self.assertEqual(reader.post(url, body, format="json").status_code, 403)
        writer = _api(_token("dsndd", READ, WRITE, service_account=self.service))
        response = writer.post(url, body, format="json")
        # Past authentication and the scope check, into the view's own validation.
        self.assertEqual(response.status_code, 400)
        self.assertIn("must not be empty", response.json()["detail"])

    # -- range mode -------------------------------------------------------------

    def test_range_is_paged_and_ordered_by_date_then_employee(self):
        api = _api(_token("timely", READ))
        data = api.get("/api/v1/roster/", {"start_date": "2026-05-01", "end_date": "2026-05-03"}).json()
        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 6)  # the inactive employee's rows are left out, as in daily mode
        self.assertEqual(
            [(r["working_day"], r["employee_id"]) for r in data["results"]],
            [(d, e) for d in ("2026-05-01", "2026-05-02", "2026-05-03") for e in ("917", "918")],
        )
        page = api.get("/api/v1/roster/", {
            "start_date": "2026-05-01", "end_date": "2026-05-03", "page_size": 4,
        }).json()
        self.assertEqual(len(page["results"]), 4)
        self.assertIn("page=2", page["next"])

    def test_daily_mode_is_still_a_bare_array(self):
        data = _api(LEGACY).get("/api/v1/roster/", {"date": "2026-05-01"}).json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)

    def test_range_validation(self):
        api = _api(LEGACY)

        def status(**params):
            return api.get("/api/v1/roster/", params).status_code

        self.assertEqual(status(start_date="2026-05-01", end_date="2026-05-31"), 200)  # 31 days
        self.assertEqual(status(start_date="2026-05-01", end_date="2026-06-01"), 400)  # 32 days
        self.assertEqual(status(start_date="2026-05-03", end_date="2026-05-01"), 400)
        self.assertEqual(status(start_date="2026-05-01"), 400)
        self.assertEqual(status(start_date="2026-05-01", end_date="nope"), 400)
        self.assertEqual(status(date="2026-05-01", start_date="2026-05-01", end_date="2026-05-02"), 400)
        self.assertEqual(status(), 400)
