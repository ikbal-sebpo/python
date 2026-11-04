"""Contract tests for POST /api/v1/external/shifts/batch/ and the shared
external read access on the TimeLy roster endpoints. See
/home/smrashel/.claude/plans/bubbly-finding-river.md Section A / A7.
"""

from __future__ import annotations

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from leave.models import LeaveApplication
from roster.api.shift_writes import SERVICE_ACCOUNT_EMAIL
from roster.models import Shift, ShiftAttribute, ShiftChangeLog, ShiftType
from roster.tests_upload import assert_bulk_query_budget

User = get_user_model()

_EXTERNAL_TOKEN = "test-external-token"
_BATCH_URL = "/api/v1/external/shifts/batch/"


def _create_service_account():
    account = User.objects.create(
        email=SERVICE_ACCOUNT_EMAIL, username=SERVICE_ACCOUNT_EMAIL,
        first_name="External", last_name="Integrations Service Account",
        employee_id=None, is_active=False,
    )
    account.set_unusable_password()
    account.save()
    return account


@override_settings(EXTERNAL_API_TOKEN=_EXTERNAL_TOKEN)
class ExternalShiftBatchAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.service_account = _create_service_account()

        cls.actor = User.objects.create_user(
            email="supervisor@example.com", username="supervisor@example.com",
            first_name="Sam", last_name="Supervisor", password="x", employee_id="700",
        )

        cls.morning = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        cls.casual_leave = ShiftType.objects.create(name="CL", category=ShiftType.Category.LEAVE)
        cls.inactive_type = ShiftType.objects.create(
            name="RETIRED", category=ShiftType.Category.WORK, is_active=False,
        )
        cls.morning_early = ShiftAttribute.objects.create(
            shift_type=cls.morning, name="MOR 6-3", start_time=time(6, 0), end_time=time(15, 0),
        )
        cls.leave_attr = ShiftAttribute.objects.create(
            shift_type=cls.casual_leave, name="CL Full", start_time=time(9, 0), end_time=time(18, 0),
        )

        cls.alice = User.objects.create_user(
            email="alice@example.com", username="alice@example.com",
            first_name="Alice", last_name="A", password="x", employee_id="917",
        )
        cls.bob = User.objects.create_user(
            email="bob@example.com", username="bob@example.com",
            first_name="Bob", last_name="B", password="x", employee_id="918",
        )

        cls.day = date(2026, 8, 1)

        # Bob already has a leave-application-owned Shift on this date.
        cls.leave_app = LeaveApplication.objects.create(
            employee=cls.bob, leave_type=cls.casual_leave,
            start_date=cls.day, end_date=cls.day,
            applied_by=cls.bob, status=LeaveApplication.Status.APPROVED,
        )
        cls.locked_shift = Shift.objects.create(
            employee=cls.bob, date=cls.day, shift_type=cls.casual_leave,
            leave_application=cls.leave_app,
        )

    def setUp(self):
        self.anon = APIClient()
        self.authed = APIClient(HTTP_AUTHORIZATION=f"Token {_EXTERNAL_TOKEN}")

    def _batch(self, items, actor_employee_id="__default__"):
        body = {"items": items}
        body["actor_employee_id"] = (
            self.actor.employee_id if actor_employee_id == "__default__" else actor_employee_id
        )
        return body

    # ----- Auth -----

    def test_anonymous_request_is_401(self):
        resp = self.anon.post(
            _BATCH_URL,
            self._batch([{"client_ref": "1", "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_is_401(self):
        c = APIClient(HTTP_AUTHORIZATION="Token nope")
        resp = c.post(
            _BATCH_URL,
            self._batch([{"client_ref": "1", "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    @override_settings(EXTERNAL_API_TOKEN=_EXTERNAL_TOKEN)
    def test_token_valid_but_service_account_missing_is_401(self):
        self.service_account.delete()
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "1", "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 401)
        # Restore for any subsequent tests in this class instance.
        self.service_account = _create_service_account()

    # ----- Structural validation -----

    def test_missing_items_is_400(self):
        resp = self.authed.post(_BATCH_URL, {"actor_employee_id": self.actor.employee_id}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_empty_items_is_400(self):
        resp = self.authed.post(_BATCH_URL, self._batch([]), format="json")
        self.assertEqual(resp.status_code, 400)

    def test_batch_over_cap_is_400(self):
        items = [
            {"client_ref": str(i), "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id}
            for i in range(2001)
        ]
        resp = self.authed.post(_BATCH_URL, self._batch(items), format="json")
        self.assertEqual(resp.status_code, 400)

    def test_query_budget_is_constant_regardless_of_batch_size(self):
        """A 500-item mixed create/update batch must use a small fixed
        number of queries, not one (or several) per item — this is the
        whole point of the bulk_create rewrite. Mirrors
        roster.tests_upload.UploadScalePerfTests.test_apply_query_budget_is_constant.
        """
        extra_employees = User.objects.bulk_create([
            User(email=f"budget{i}@example.com", username=f"budget{i}@example.com",
                 first_name="Budget", last_name=str(i), employee_id=f"B{i}")
            for i in range(500)
        ])
        # Half already have a Shift on the target date (exercises the
        # update path), half don't (exercises the create path).
        Shift.objects.bulk_create([
            Shift(employee=e, date=date(2026, 8, 20), shift_type=self.morning)
            for e in extra_employees[:250]
        ])

        items = [
            {"client_ref": str(i), "employee_id": f"B{i}", "date": "2026-08-20", "orbis_shift_type_id": self.casual_leave.id}
            for i in range(500)
        ]
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            resp = self.authed.post(_BATCH_URL, self._batch(items), format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"], {"ok": 500, "conflict": 0, "error": 0})
        # Fixed reads (auth, actor, lookups, night windows) + savepoint pair +
        # one INSERT per table, which SQLite splits into batches — see
        # assert_bulk_query_budget — plus the request's one access-log row.
        assert_bulk_query_budget(
            self, ctx, rows=500, models=(Shift, ShiftChangeLog),
            other_budget=12, what="batch write", once_tables=("audit_accesslog",),
        )

    def test_missing_actor_employee_id_is_400(self):
        resp = self.authed.post(
            _BATCH_URL,
            {"items": [{"client_ref": "1", "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("actor_employee_id", resp.json()["detail"])

    def test_unknown_actor_employee_id_is_400(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch(
                [{"client_ref": "1", "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id}],
                actor_employee_id="no-such-actor",
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no-such-actor", resp.json()["detail"])
        # Nothing in the batch should have been applied.
        self.assertFalse(Shift.objects.filter(employee=self.alice, date=date(2026, 8, 2)).exists())

    # ----- Per-item outcomes -----

    def test_create_update_delete_and_errors_in_one_batch(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([
                {"client_ref": "create", "employee_id": "917", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id},
                {"client_ref": "unknown-emp", "employee_id": "999", "date": "2026-08-02", "orbis_shift_type_id": self.morning.id},
                {"client_ref": "unknown-type", "employee_id": "917", "date": "2026-08-03", "orbis_shift_type_id": 999999},
                {"client_ref": "inactive-type", "employee_id": "917", "date": "2026-08-04", "orbis_shift_type_id": self.inactive_type.id},
                {"client_ref": "bad-date", "employee_id": "917", "date": "not-a-date", "orbis_shift_type_id": self.morning.id},
                {"client_ref": "leave-locked", "employee_id": "918", "date": "2026-08-01", "orbis_shift_type_id": self.morning.id},
            ]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        by_ref = {r["client_ref"]: r for r in payload["results"]}

        self.assertEqual(by_ref["create"]["status"], "ok")
        self.assertEqual(by_ref["create"]["action"], "created")
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 2))
        self.assertEqual(shift.shift_type_id, self.morning.id)
        # Attribution goes to the resolved actor, not the shared service account.
        self.assertEqual(shift.added_by_id, self.actor.pk)
        self.assertEqual(shift.updated_by_id, self.actor.pk)

        self.assertEqual(by_ref["unknown-emp"]["status"], "error")
        self.assertEqual(by_ref["unknown-type"]["status"], "error")
        self.assertEqual(by_ref["inactive-type"]["status"], "error")
        self.assertEqual(by_ref["bad-date"]["status"], "error")

        self.assertEqual(by_ref["leave-locked"]["status"], "conflict")
        self.assertEqual(by_ref["leave-locked"]["reason"], "leave_application_owned")
        self.locked_shift.refresh_from_db()
        self.assertEqual(self.locked_shift.shift_type_id, self.casual_leave.id)
        self.assertEqual(self.locked_shift.leave_application_id, self.leave_app.pk)

        self.assertEqual(payload["summary"], {"ok": 1, "conflict": 1, "error": 4})

    def test_write_past_last_working_date_is_a_conflict(self):
        """No Shift may exist past an employee's LWD — same cutoff the roster
        prefill, the bulk uploader and the leave materialiser enforce."""
        self.alice.last_working_date = date(2026, 8, 2)
        self.alice.save(update_fields=["last_working_date"])

        resp = self.authed.post(
            _BATCH_URL,
            self._batch([
                {"client_ref": "on-lwd", "employee_id": "917", "date": "2026-08-02",
                 "orbis_shift_type_id": self.morning.id},
                {"client_ref": "past-lwd", "employee_id": "917", "date": "2026-08-03",
                 "orbis_shift_type_id": self.morning.id},
            ]),
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        by_ref = {r["client_ref"]: r for r in resp.json()["results"]}
        self.assertEqual(by_ref["on-lwd"]["action"], "created")
        self.assertEqual(by_ref["past-lwd"]["status"], "conflict")
        self.assertEqual(by_ref["past-lwd"]["reason"], "past_last_working_date")
        self.assertFalse(
            Shift.objects.filter(employee=self.alice, date=date(2026, 8, 3)).exists()
        )

    def test_deleting_a_shift_past_lwd_is_still_allowed(self):
        """The cutoff blocks creating rows past the LWD, never removing them."""
        stale = Shift.objects.create(
            employee=self.alice, date=date(2026, 8, 3), shift_type=self.morning,
        )
        self.alice.last_working_date = date(2026, 8, 2)
        # The CustomUser LWD signal purges on save, so re-create the row to
        # exercise the API's own delete path rather than the signal's.
        self.alice.save(update_fields=["last_working_date"])
        Shift.objects.get_or_create(
            employee=self.alice, date=date(2026, 8, 3),
            defaults={"shift_type": self.morning},
        )

        resp = self.authed.post(
            _BATCH_URL,
            self._batch([
                {"client_ref": "del", "employee_id": "917", "date": "2026-08-03",
                 "orbis_shift_type_id": None},
            ]),
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        self.assertEqual(result["action"], "deleted")
        self.assertFalse(Shift.objects.filter(pk=stale.pk).exists())

    def test_update_existing_row(self):
        Shift.objects.create(employee=self.alice, date=self.day, shift_type=self.morning)
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "u", "employee_id": "917", "date": "2026-08-01", "orbis_shift_type_id": self.casual_leave.id, "note": "changed"}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "updated")
        shift = Shift.objects.get(employee=self.alice, date=self.day)
        self.assertEqual(shift.shift_type_id, self.casual_leave.id)
        self.assertEqual(shift.note, "changed")
        self.assertEqual(shift.updated_by_id, self.actor.pk)

    def test_null_shift_type_deletes_existing_row(self):
        Shift.objects.create(employee=self.alice, date=self.day, shift_type=self.morning)
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "d", "employee_id": "917", "date": "2026-08-01", "orbis_shift_type_id": None}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "deleted")
        self.assertFalse(Shift.objects.filter(employee=self.alice, date=self.day).exists())

    def test_null_shift_type_on_leave_locked_row_is_conflict_not_delete(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "d", "employee_id": "918", "date": "2026-08-01", "orbis_shift_type_id": None}]),
            format="json",
        )
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(Shift.objects.filter(pk=self.locked_shift.pk).exists())

    def test_client_ref_defaulted_when_missing(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"employee_id": "917", "date": "2026-08-05", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        result = resp.json()["results"][0]
        self.assertEqual(result["client_ref"], "item-0")
        self.assertEqual(result["status"], "ok")

    # ----- Shift attribute (time-variant) handling -----

    def test_shift_attribute_is_set_when_given(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-06",
                "orbis_shift_type_id": self.morning.id,
                "orbis_shift_attribute_id": self.morning_early.id,
            }]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "ok")
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 6))
        self.assertEqual(shift.shift_attribute_id, self.morning_early.id)
        self.assertEqual(shift.shift_type_id, self.morning.id)

    def test_shift_attribute_omitted_leaves_it_null(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "a", "employee_id": "917", "date": "2026-08-07", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 7))
        self.assertIsNone(shift.shift_attribute_id)

    def test_unknown_shift_attribute_is_error(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-06",
                "orbis_shift_type_id": self.morning.id, "orbis_shift_attribute_id": 999999,
            }]),
            format="json",
        )
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertFalse(Shift.objects.filter(employee=self.alice, date=date(2026, 8, 6)).exists())

    def test_shift_attribute_belonging_to_different_type_is_error(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-06",
                "orbis_shift_type_id": self.morning.id, "orbis_shift_attribute_id": self.leave_attr.id,
            }]),
            format="json",
        )
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertIn("does not belong to", result["reason"])
        self.assertFalse(Shift.objects.filter(employee=self.alice, date=date(2026, 8, 6)).exists())

    def test_shift_attribute_without_shift_type_is_error(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-06",
                "orbis_shift_attribute_id": self.morning_early.id,
            }]),
            format="json",
        )
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertIn("requires orbis_shift_type_id", result["reason"])

    # ----- Location -----

    def test_location_office_code_resolves_job_and_work_location(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-10",
                "orbis_shift_type_id": self.morning.id, "location": "MHK",
            }]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["results"][0]["status"], "ok")
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 10))
        self.assertIsNotNone(shift.job_location_id)
        self.assertIsNotNone(shift.work_location_id)
        from accounts.locations import office_bucket
        self.assertEqual(office_bucket(shift.job_location), "MHK")

    def test_location_wfh_sets_only_work_location(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-11",
                "orbis_shift_type_id": self.morning.id, "location": "WFH",
            }]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 11))
        self.assertIsNone(shift.job_location_id)
        self.assertIsNotNone(shift.work_location_id)
        self.assertIn("home", shift.work_location.name.lower())

    def test_location_omitted_leaves_both_blank(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "a", "employee_id": "917", "date": "2026-08-12", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 12))
        self.assertIsNone(shift.job_location_id)
        self.assertIsNone(shift.work_location_id)

    def test_location_omitted_on_update_clears_a_previously_set_location(self):
        from accounts.locations import resolve_location_choice
        jl, wl = resolve_location_choice("MHK")
        Shift.objects.create(
            employee=self.alice, date=self.day, shift_type=self.morning,
            job_location=jl, work_location=wl,
        )
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "u", "employee_id": "917", "date": "2026-08-01", "orbis_shift_type_id": self.casual_leave.id}]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        shift = Shift.objects.get(employee=self.alice, date=self.day)
        self.assertIsNone(shift.job_location_id)
        self.assertIsNone(shift.work_location_id)

    def test_unknown_location_is_error(self):
        resp = self.authed.post(
            _BATCH_URL,
            self._batch([{
                "client_ref": "a", "employee_id": "917", "date": "2026-08-10",
                "orbis_shift_type_id": self.morning.id, "location": "MARS",
            }]),
            format="json",
        )
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertIn("Unknown location", result["reason"])
        self.assertFalse(Shift.objects.filter(employee=self.alice, date=date(2026, 8, 10)).exists())

    # ----- Shift change audit log -----

    def test_create_writes_a_created_log_row_attributed_to_actor(self):
        self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "a", "employee_id": "917", "date": "2026-08-09", "orbis_shift_type_id": self.morning.id}]),
            format="json",
        )
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 8, 9))
        log = ShiftChangeLog.objects.get(shift=shift)
        self.assertEqual(log.action_type, ShiftChangeLog.ActionType.CREATED)
        self.assertEqual(log.changed_by_id, self.actor.pk)
        self.assertEqual(log.new_shift_type_id, self.morning.id)
        self.assertIsNone(log.previous_shift_type_id)

    def test_update_writes_an_updated_log_row_with_before_after(self):
        Shift.objects.create(employee=self.alice, date=self.day, shift_type=self.morning)
        self.authed.post(
            _BATCH_URL,
            self._batch([{"client_ref": "u", "employee_id": "917", "date": "2026-08-01", "orbis_shift_type_id": self.casual_leave.id}]),
            format="json",
        )
        shift = Shift.objects.get(employee=self.alice, date=self.day)
        log = ShiftChangeLog.objects.filter(shift=shift, action_type=ShiftChangeLog.ActionType.UPDATED).latest("changed_at")
        self.assertEqual(log.previous_shift_type_id, self.morning.id)
        self.assertEqual(log.new_shift_type_id, self.casual_leave.id)
        self.assertEqual(log.changed_by_id, self.actor.pk)

    def test_delete_writes_a_deleted_log_row_attributed_to_the_deleting_actor(self):
        other_actor = User.objects.create_user(
            email="other-actor@example.com", username="other-actor@example.com",
            first_name="Other", last_name="Actor", password="x", employee_id="701",
        )
        shift = Shift.objects.create(
            employee=self.alice, date=self.day, shift_type=self.morning,
            added_by=other_actor, updated_by=other_actor,
        )
        self.authed.post(
            _BATCH_URL,
            self._batch(
                [{"client_ref": "d", "employee_id": "917", "date": "2026-08-01", "orbis_shift_type_id": None}],
                actor_employee_id=self.actor.employee_id,
            ),
            format="json",
        )
        log = ShiftChangeLog.objects.filter(
            employee=self.alice, date=self.day, action_type=ShiftChangeLog.ActionType.DELETED,
        ).latest("changed_at")
        # Attributed to whoever called the delete, not whoever last saved the row.
        self.assertEqual(log.changed_by_id, self.actor.pk)
        self.assertIsNone(log.shift_id)
        self.assertEqual(log.previous_shift_type_id, self.morning.id)


@override_settings(EXTERNAL_API_TOKEN=_EXTERNAL_TOKEN)
class ExternalSharedReadAccessTests(TestCase):
    """The external token also works on the existing TimeLy-facing read endpoints."""

    @classmethod
    def setUpTestData(cls):
        cls.morning = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        _create_service_account()

    def test_external_token_authenticates_shift_types_endpoint(self):
        c = APIClient(HTTP_AUTHORIZATION=f"Token {_EXTERNAL_TOKEN}")
        resp = c.get("/api/v1/shift-types/")
        self.assertEqual(resp.status_code, 200)

    def test_timely_token_still_rejected_when_only_external_token_configured(self):
        c = APIClient(HTTP_AUTHORIZATION="Token some-timely-token")
        resp = c.get("/api/v1/shift-types/")
        self.assertEqual(resp.status_code, 401)
