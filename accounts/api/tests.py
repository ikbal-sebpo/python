"""Directory API phase 1 — API clients, scopes and the reference lists.
docs/DIRECTORY_API_PLAN.md §4–§5."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import ApiClient, Department, Designation, JobLocation, Team
from audit.models import AccessLog

User = get_user_model()

DEPARTMENTS = reverse("api_directory_departments")


def _client(name="timely", scopes=(ApiClient.Scope.DIRECTORY_READ,), **extra):
    client = ApiClient.objects.create(name=name, scopes=list(scopes), **extra)
    return client, client.issue_token()


class _Api(TestCase):
    def call(self, url, token, **params):
        api = APIClient()
        if token:
            api.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        return api.get(url, params)


class TokenTests(TestCase):
    def test_issue_stores_only_the_hash(self):
        client, token = _client()
        client.refresh_from_db()
        self.assertTrue(token.startswith("orb_"))
        self.assertEqual(client.token_hash, ApiClient.hash_token(token))
        self.assertEqual(client.token_prefix, token[:12])
        self.assertNotIn(token, (client.token_hash, client.token_prefix))
        self.assertIsNotNone(client.token_issued_at)

    def test_reissue_kills_the_old_token(self):
        client, old = _client()
        new = client.issue_token()
        self.assertNotEqual(old, new)
        self.assertFalse(ApiClient.objects.filter(token_hash=ApiClient.hash_token(old)).exists())


class AuthenticationTests(_Api):
    def test_valid_token_with_scope(self):
        _c, token = _client()
        self.assertEqual(self.call(DEPARTMENTS, token).status_code, 200)

    def test_no_header(self):
        self.assertEqual(self.call(DEPARTMENTS, None).status_code, 401)

    def test_unknown_token(self):
        self.assertEqual(self.call(DEPARTMENTS, "orb_not-a-real-token").status_code, 401)

    def test_revoked_client(self):
        client, token = _client()
        client.is_active = False
        client.save()
        self.assertEqual(self.call(DEPARTMENTS, token).status_code, 401)

    def test_missing_scope_is_forbidden(self):
        _c, token = _client(scopes=[ApiClient.Scope.ROSTER_READ])
        self.assertEqual(self.call(DEPARTMENTS, token).status_code, 403)

    @override_settings(TIMELY_API_TOKEN="legacy-timely", EXTERNAL_API_TOKEN="legacy-external")
    def test_env_tokens_are_not_admitted(self):
        self.assertEqual(self.call(DEPARTMENTS, "legacy-timely").status_code, 401)
        self.assertEqual(self.call(DEPARTMENTS, "legacy-external").status_code, 401)

    def test_write_scope_without_service_account_is_refused(self):
        _c, token = _client(scopes=[ApiClient.Scope.DIRECTORY_READ, ApiClient.Scope.ROSTER_WRITE])
        response = self.call(DEPARTMENTS, token)
        self.assertEqual(response.status_code, 401)
        self.assertIn("service account", response.json()["detail"])

    def test_write_client_acts_as_its_service_account(self):
        account = User.objects.create_user(
            email="svc@sebpo.internal", username="svc@sebpo.internal", is_active=False,
        )
        _c, token = _client(
            name="dsndd", service_account=account,
            scopes=[ApiClient.Scope.DIRECTORY_READ, ApiClient.Scope.ROSTER_WRITE],
        )
        self.assertEqual(self.call(DEPARTMENTS, token).status_code, 200)
        row = AccessLog.objects.get(route="api_directory_departments")
        self.assertEqual(row.actor_id, account.pk)
        self.assertEqual(row.detail.get("api_client"), "dsndd")

    def test_last_used_is_recorded_but_throttled(self):
        client, token = _client()
        self.call(DEPARTMENTS, token)
        client.refresh_from_db()
        first = client.last_used_at
        self.assertIsNotNone(first)
        self.call(DEPARTMENTS, token)
        client.refresh_from_db()
        self.assertEqual(client.last_used_at, first)  # within the minute

        ApiClient.objects.filter(pk=client.pk).update(last_used_at=first - timedelta(minutes=5))
        self.call(DEPARTMENTS, token)
        client.refresh_from_db()
        self.assertGreater(client.last_used_at, first - timedelta(minutes=5))

    def test_access_log_names_the_client(self):
        _c, token = _client()
        self.call(DEPARTMENTS, token)
        row = AccessLog.objects.get(route="api_directory_departments")
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor_label, "api:timely")
        self.assertEqual(row.data_class, "API")
        self.assertEqual(row.detail.get("api_client"), "timely")


class ReferenceListTests(_Api):
    @classmethod
    def setUpTestData(cls):
        cls.head = User.objects.create_user(
            email="head@sebpo.com", username="head@sebpo.com",
            first_name="Hannah", last_name="Head", employee_id="100",
        )
        cls.bpo = Department.objects.create(name="General BPO", head_of_department=cls.head)
        cls.old = Department.objects.create(name="Retired Dept", is_active=False)
        Designation.objects.create(name="Executive")
        Team.objects.create(name="Alpha", department=cls.bpo)
        JobLocation.objects.create(name="MHK", code="MHK")
        JobLocation.objects.create(name="Monem Business District, Level-7 Dhaka 1205 BD")
        _c, cls.token = _client()

    def get(self, name, **params):
        response = self.call(reverse(name), self.token, **params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_departments(self):
        rows = self.get("api_directory_departments")
        self.assertEqual([r["name"] for r in rows], ["General BPO", "Retired Dept"])
        self.assertEqual(rows[0], {
            "id": self.bpo.pk, "name": "General BPO", "is_active": True,
            "head_of_department": {"employee_id": "100", "email": "head@sebpo.com", "full_name": "Hannah Head"},
        })
        self.assertIsNone(rows[1]["head_of_department"])

    def test_is_active_filter(self):
        rows = self.get("api_directory_departments", is_active="true")
        self.assertEqual([r["name"] for r in rows], ["General BPO"])
        rows = self.get("api_directory_departments", is_active="false")
        self.assertEqual([r["name"] for r in rows], ["Retired Dept"])

    def test_designations(self):
        self.assertEqual(
            self.get("api_directory_designations"),
            [{"id": Designation.objects.get().pk, "name": "Executive", "is_active": True}],
        )

    def test_teams_carry_their_department(self):
        team = Team.objects.get()
        self.assertEqual(self.get("api_directory_teams"), [{
            "id": team.pk, "name": "Alpha", "is_active": True,
            "department_id": self.bpo.pk, "department_name": "General BPO",
        }])

    def test_offices_use_the_canonical_code(self):
        rows = {r["name"]: r for r in self.get("api_directory_offices")}
        self.assertEqual(rows["MHK"]["code"], "MHK")
        self.assertEqual(rows["Monem Business District, Level-7 Dhaka 1205 BD"]["code"], "MBD")
        self.assertEqual(set(rows["MHK"]), {"id", "name", "code", "is_active"})

    def test_query_count_is_constant(self):
        for i in range(5):
            Team.objects.create(name=f"T{i}", department=self.bpo)
        # auth lookup + last_used touch + list + access-log insert, roughly — the
        # point is it doesn't grow with rows.
        with self.assertNumQueries(4):
            self.call(reverse("api_directory_teams"), self.token)


class ApiClientAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_row = ApiClient.objects.create(name="timely", scopes=[ApiClient.Scope.DIRECTORY_READ])
        cls.hr = User.objects.create_user(
            email="hr@sebpo.com", username="hr@sebpo.com", password="x", is_staff=True,
        )
        cls.admin_user = User.objects.create_user(
            email="ua@sebpo.com", username="ua@sebpo.com", password="x", is_staff=True,
        )
        cls.admin_user.groups.add(Group.objects.get_or_create(name="User Admins")[0])

    def test_hidden_from_staff_outside_user_admins(self):
        self.client.force_login(self.hr)
        url = reverse("admin:accounts_apiclient_changelist")
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_generate_token_shows_it_once(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(reverse("admin:accounts_apiclient_changelist"), {
            "action": "generate_token", "_selected_action": [self.client_row.pk],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        token = response.context["token"]
        self.assertContains(response, token)
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.token_hash, ApiClient.hash_token(token))

    def test_edit_page_has_the_button(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:accounts_apiclient_change", args=[self.client_row.pk]))
        self.assertContains(
            response, reverse("admin:accounts_apiclient_generate_token", args=[self.client_row.pk]),
        )

    def test_button_issues_and_shows_the_token(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("admin:accounts_apiclient_generate_token", args=[self.client_row.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        token = response.context["token"]
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.token_hash, ApiClient.hash_token(token))

    def test_button_is_post_only_and_admin_only(self):
        url = reverse("admin:accounts_apiclient_generate_token", args=[self.client_row.pk])
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.hr)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client_row.refresh_from_db()
        self.assertIsNone(self.client_row.token_hash)


# ---------------------------------------------------------------------------
# Phase 2 — employees. §5.1–§5.4.
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402

from accounts.models import WorkLocation  # noqa: E402
from employees.models import JobInfo  # noqa: E402
from offboarding.services import deactivate_past_lwd_users  # noqa: E402

OID = "3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f"
_OLD = timezone.now() - timedelta(days=30)

RECORD_FIELDS = {
    "employee_id", "entra_object_id", "email", "full_name", "first_name", "last_name",
    "status", "joining_date", "last_working_date",
    "department", "team", "designation", "office", "work_location",
    "supervisor", "direct_reports_count", "updated_at",
}


class EmployeeAPITests(_Api):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name="General BPO")
        cls.other_dept = Department.objects.create(name="Corporate")
        cls.team = Team.objects.create(name="Alpha", department=cls.dept)
        cls.designation = Designation.objects.create(name="Manager")
        cls.office = JobLocation.objects.create(name="MHK", code="MHK")
        cls.wfo = WorkLocation.objects.create(name="Work from Office (WFO)")

        def person(eid, first, boss=None, **extra):
            user = User.objects.create_user(
                email=f"{first.lower()}@sebpo.com", username=f"{first.lower()}@sebpo.com",
                first_name=first, last_name="X", employee_id=eid, department=cls.dept, **extra,
            )
            JobInfo.objects.create(employee=user, reports_to=boss)
            return user

        cls.boss = person("656", "Tanveer")
        cls.me = person(
            "539", "Rashel", boss=cls.boss, team=cls.team, designation=cls.designation,
            job_location=cls.office, work_location=cls.wfo, joining_date=date(2015, 1, 1),
            entra_object_id=OID,
        )
        cls.reports = [person(f"60{i}", f"Report{i}", boss=cls.me) for i in range(3)]
        cls.gone_report = person("699", "Gone", boss=cls.me, is_active=False)
        cls.leaver = person("677", "Tahmina", boss=cls.boss, is_active=False,
                            last_working_date=date(2026, 9, 25))
        cls.orphan = person("700", "Orphan", boss=cls.leaver)
        cls.service = User.objects.create_user(
            email="svc@sebpo.internal", username="svc@sebpo.internal", employee_id=None,
        )
        _c, cls.token = _client()

    def get(self, url, **params):
        return self.call(url, self.token, **params)

    def ok(self, url, **params):
        response = self.get(url, **params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def ids(self, rows):
        return [r["employee_id"] for r in rows]

    EMPLOYEES = reverse("api_directory_employees")
    LOOKUP = reverse("api_directory_employee_lookup")

    # -- list ---------------------------------------------------------------

    def test_list_defaults_to_active_people_with_an_employee_id(self):
        data = self.ok(self.EMPLOYEES)
        self.assertEqual(set(data), {"count", "next", "previous", "results", "as_of"})
        self.assertEqual(self.ids(data["results"]), ["539", "600", "601", "602", "656", "700"])

    def test_status_filter(self):
        self.assertEqual(self.ids(self.ok(self.EMPLOYEES, status="inactive")["results"]), ["677", "699"])
        self.assertEqual(self.ok(self.EMPLOYEES, status="all")["count"], 8)
        self.assertEqual(self.get(self.EMPLOYEES, status="gone").status_code, 400)

    def test_department_and_team_filters(self):
        self.assertEqual(self.ids(self.ok(self.EMPLOYEES, team=self.team.pk)["results"]), ["539"])
        self.assertEqual(self.ok(self.EMPLOYEES, department=self.other_dept.pk)["count"], 0)
        self.assertEqual(self.get(self.EMPLOYEES, team="alpha").status_code, 400)

    def test_record_shape(self):
        record = next(r for r in self.ok(self.EMPLOYEES)["results"] if r["employee_id"] == "539")
        self.assertEqual(set(record), RECORD_FIELDS)  # directory data only (D8)
        self.assertEqual(record["entra_object_id"], OID)
        self.assertEqual(record["full_name"], "Rashel X")
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["joining_date"], "2015-01-01")
        self.assertEqual(record["department"], {"id": self.dept.pk, "name": "General BPO"})
        self.assertEqual(record["team"], {"id": self.team.pk, "name": "Alpha", "department_id": self.dept.pk})
        self.assertEqual(record["designation"], {"id": self.designation.pk, "name": "Manager"})
        self.assertEqual(record["office"], {"id": self.office.pk, "code": "MHK", "name": "MHK"})
        self.assertEqual(record["work_location"], "WFO")
        self.assertEqual(record["supervisor"], {
            "employee_id": "656", "email": "tanveer@sebpo.com", "full_name": "Tanveer X", "status": "active",
        })
        self.assertEqual(record["direct_reports_count"], 3)  # the inactive report isn't counted

    def test_pagination(self):
        data = self.ok(self.EMPLOYEES, page_size=2)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["count"], 6)
        self.assertIn("page=2", data["next"])

    def test_query_count_does_not_grow_with_rows(self):
        def queries(page_size):
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            ApiClient.objects.update(last_used_at=timezone.now())  # keep the touch out of it
            with CaptureQueriesContext(connection) as ctx:
                self.get(self.EMPLOYEES, page_size=page_size)
            return len(ctx)
        self.assertEqual(queries(1), queries(100))

    # -- updated_since --------------------------------------------------------

    def _age_everything(self):
        User.objects.update(directory_updated_at=_OLD)
        JobInfo.objects.update(updated_at=_OLD)

    def test_updated_since_picks_up_a_user_change(self):
        self._age_everything()
        user = User.objects.get(pk=self.me.pk)
        user.department = self.other_dept
        user.save()
        since = (_OLD + timedelta(days=1)).isoformat()
        self.assertEqual(self.ids(self.ok(self.EMPLOYEES, updated_since=since)["results"]), ["539"])

    def test_updated_since_picks_up_a_supervisor_change(self):
        self._age_everything()
        job_info = JobInfo.objects.get(employee=self.orphan)
        job_info.reports_to = self.boss
        job_info.save()
        rows = self.ok(self.EMPLOYEES, updated_since=(_OLD + timedelta(days=1)).isoformat())["results"]
        self.assertEqual(self.ids(rows), ["700"])
        self.assertGreater(rows[0]["updated_at"], (_OLD + timedelta(days=1)).isoformat())

    def test_updated_since_picks_up_a_deactivation(self):
        self._age_everything()
        User.objects.filter(pk=self.me.pk).update(last_working_date=date.today() - timedelta(days=1))
        deactivate_past_lwd_users()
        rows = self.ok(self.EMPLOYEES, status="all", updated_since=(_OLD + timedelta(days=1)).isoformat())
        self.assertEqual(self.ids(rows["results"]), ["539"])

    def test_updated_since_formats(self):
        self._age_everything()
        self.assertEqual(self.ok(self.EMPLOYEES, updated_since=date.today().isoformat())["count"], 0)
        self.assertEqual(self.ok(self.EMPLOYEES, updated_since="2000-01-01T00:00:00")["count"], 6)
        self.assertEqual(self.get(self.EMPLOYEES, updated_since="yesterday").status_code, 400)

    # -- detail / direct reports ---------------------------------------------

    def test_detail_lists_active_direct_reports(self):
        data = self.ok(reverse("api_directory_employee", args=["539"]))
        self.assertEqual(set(data), RECORD_FIELDS | {"direct_reports"})
        self.assertEqual(self.ids(data["direct_reports"]), ["600", "601", "602"])
        self.assertEqual(set(data["direct_reports"][0]), {"employee_id", "email", "full_name", "designation", "status"})

    def test_inactive_employee_keeps_last_day_department_and_supervisor(self):
        data = self.ok(reverse("api_directory_employee", args=["677"]))
        self.assertEqual(data["status"], "inactive")
        self.assertEqual(data["last_working_date"], "2026-09-25")
        self.assertEqual(data["department"]["name"], "General BPO")
        self.assertEqual(data["supervisor"]["employee_id"], "656")
        self.assertEqual(data["direct_reports_count"], 1)  # the orphan still reports to them

    def test_detail_not_found(self):
        self.assertEqual(self.get(reverse("api_directory_employee", args=["999"])).status_code, 404)

    def test_direct_reports_endpoint(self):
        url = reverse("api_directory_direct_reports", args=["539"])
        self.assertEqual(self.ids(self.ok(url)), ["600", "601", "602"])
        self.assertEqual(self.ids(self.ok(url, status="all")), ["600", "601", "602", "699"])
        self.assertEqual(self.get(reverse("api_directory_direct_reports", args=["999"])).status_code, 404)

    def test_detail_access_is_logged_against_the_employee(self):
        self.ok(reverse("api_directory_employee", args=["539"]))
        row = AccessLog.objects.get(route="api_directory_employee")
        self.assertEqual(row.subject_id, self.me.pk)

    # -- lookup ---------------------------------------------------------------

    def test_lookup_by_each_key(self):
        self.assertEqual(self.ok(self.LOOKUP, oid=OID.upper())["matched_by"], "oid")
        data = self.ok(self.LOOKUP, email="RASHEL@sebpo.com")
        self.assertEqual((data["employee_id"], data["matched_by"]), ("539", "email"))
        self.assertEqual(self.ok(self.LOOKUP, employee_id="539")["matched_by"], "employee_id")
        self.assertEqual(self.ids(data["direct_reports"]), ["600", "601", "602"])

    def test_lookup_prefers_oid_and_falls_back(self):
        both = {"oid": OID, "email": "rashel@sebpo.com", "employee_id": "539"}
        self.assertEqual(self.ok(self.LOOKUP, **both)["matched_by"], "oid")
        # Object id not stored yet for this person: email finds them.
        unknown = {"oid": "11111111-2222-3333-4444-555555555555", "email": "report0@sebpo.com"}
        data = self.ok(self.LOOKUP, **unknown)
        self.assertEqual((data["employee_id"], data["matched_by"]), ("600", "email"))

    def test_lookup_conflict(self):
        response = self.get(self.LOOKUP, email="rashel@sebpo.com", employee_id="656")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["matches"], {"email": "539", "employee_id": "656"})

    def test_lookup_returns_inactive_people(self):
        self.assertEqual(self.ok(self.LOOKUP, employee_id="677")["status"], "inactive")

    def test_lookup_errors(self):
        self.assertEqual(self.get(self.LOOKUP).status_code, 400)
        self.assertEqual(self.get(self.LOOKUP, oid="not-a-uuid").status_code, 400)
        self.assertEqual(self.get(self.LOOKUP, email="nobody@sebpo.com").status_code, 404)
        self.assertEqual(self.get(self.LOOKUP, email="svc@sebpo.internal").status_code, 404)

    # -- unknown parameters -----------------------------------------------------

    def test_unknown_parameter_is_rejected_not_ignored(self):
        # The reported trap: `employees/?email=…` used to return everyone.
        response = self.get(self.EMPLOYEES, email="rashel@sebpo.com/scope/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("employees/lookup/?email=", response.json()["email"])
        response = self.get(self.EMPLOYEES, department_name="General BPO")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Allowed: department, page, page_size, status, team, updated_since",
                      response.json()["department_name"])

    def test_every_directory_endpoint_is_strict(self):
        for url in (
            reverse("api_directory_departments"),
            reverse("api_directory_employee", args=["539"]),
            reverse("api_directory_direct_reports", args=["539"]),
            reverse("api_directory_scope", args=["539"]),
            self.LOOKUP,
        ):
            self.assertEqual(self.get(url, employee_id="539", bogus="1").status_code, 400, url)

    def test_known_parameters_still_work(self):
        self.ok(self.LOOKUP, oid=OID, email="rashel@sebpo.com", employee_id="539")
        self.ok(reverse("api_directory_scope", args=["539"]), status="all", page=1, page_size=10)
        self.ok(reverse("api_directory_departments"), is_active="true", format="json")

    def test_parameters_are_checked_after_authentication(self):
        response = self.call(self.EMPLOYEES, None, email="x")
        self.assertEqual(response.status_code, 401)

    # -- managed scope ----------------------------------------------------------

    def scope(self, employee_id="539", **params):
        data = self.ok(reverse("api_directory_scope", args=[employee_id]), **params)
        self.assertIn("as_of", data)
        return {r["employee_id"]: r["scope"] for r in data["results"]}

    def test_scope_walks_the_whole_downline_with_depth(self):
        grandchild = User.objects.create_user(
            email="g@sebpo.com", username="g@sebpo.com", employee_id="800", department=self.other_dept,
        )
        JobInfo.objects.create(employee=grandchild, reports_to=self.reports[0])
        scope = self.scope()
        self.assertEqual(set(scope), {"600", "601", "602", "800"})  # not self, not the inactive report
        self.assertEqual(scope["600"], {"via": ["reports_to"], "depth": 1})
        self.assertEqual(scope["800"], {"via": ["reports_to"], "depth": 2})

    def test_scope_includes_department_and_team_grants(self):
        self.other_dept.head_of_department = self.me
        self.other_dept.save()
        outsider = User.objects.create_user(
            email="o@sebpo.com", username="o@sebpo.com", employee_id="801", department=self.other_dept,
        )
        User.objects.filter(pk=self.reports[1].pk).update(department=self.other_dept)
        side_team = Team.objects.create(name="Beta", department=self.dept)
        side_team.additional_managers.add(self.me)
        teammate = User.objects.create_user(
            email="t@sebpo.com", username="t@sebpo.com", employee_id="802", team=side_team,
        )
        scope = self.scope()
        self.assertEqual(scope["801"], {"via": ["department"], "depth": None})
        self.assertEqual(scope["601"], {"via": ["reports_to", "department"], "depth": 1})
        self.assertEqual(scope["802"], {"via": ["team"], "depth": None})

        from accounts.permissions import managed_scope_pks
        expected = set(User.objects.filter(pk__in=managed_scope_pks(self.me)).values_list("employee_id", flat=True))
        self.assertEqual(set(scope), expected)

    def test_scope_status(self):
        self.assertNotIn("699", self.scope())
        self.assertEqual(self.scope(status="all")["699"], {"via": ["reports_to"], "depth": 1})
        self.assertEqual(set(self.scope(status="inactive")), {"699"})
        self.assertEqual(self.get(reverse("api_directory_scope", args=["539"]), status="x").status_code, 400)

    def test_scope_unknown_or_empty(self):
        self.assertEqual(self.get(reverse("api_directory_scope", args=["999"])).status_code, 404)
        self.assertEqual(self.scope("600"), {})


class DeveloperGuidePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="dev@sebpo.com", username="dev@sebpo.com", password="x")

    def test_requires_sign_in(self):
        response = self.client.get(reverse("developer_api_guide"))
        self.assertEqual(response.status_code, 302)

    def test_renders_each_page_from_the_repo_markdown(self):
        self.client.force_login(self.user)
        for name, args, heading in (
            ("developer_api_guide", [], "Orbis API — developer guide"),
            ("developer_api_guide_page", ["directory"], "Orbis Directory API"),
            ("developer_api_guide_page", ["roster"], "Orbis Roster API"),
        ):
            response = self.client.get(reverse(name, args=args))
            self.assertContains(response, heading)
        guide = self.client.get(reverse("developer_api_guide"))
        self.assertContains(guide, 'href="#2-getting-access"')  # contents built from the headings
        self.assertContains(guide, f'href="{reverse("developer_api_guide_page", args=["directory"])}"')

    def test_unknown_page(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/developers/api/payroll/").status_code, 404)

    def test_markdown_html_is_not_passed_through(self):
        from accounts.api.guide import _markdown
        self.assertNotIn("<script>", _markdown.render("<script>alert(1)</script>"))

    def test_every_api_route_is_documented(self):
        """Each /api/v1/ route must appear in the docs the developer page
        renders — a new endpoint without docs fails here."""
        import re

        from django.urls import URLPattern, URLResolver, get_resolver

        from accounts.api.guide import DOCS_DIR, PAGES

        docs = "\n".join((DOCS_DIR / page.filename).read_text() for page in PAGES)

        def walk(patterns, prefix=""):
            for pattern in patterns:
                route = prefix + str(pattern.pattern)
                if isinstance(pattern, URLResolver):
                    yield from walk(pattern.url_patterns, route)
                elif isinstance(pattern, URLPattern):
                    yield route

        missing = []
        for route in walk(get_resolver().url_patterns):
            if not route.startswith("api/v1/"):
                continue
            documented = re.sub(r"<(?:\w+:)?(\w+)>", r"<\1>", route.removeprefix("api/v1/"))
            if documented.removeprefix("directory/") not in docs and documented not in docs:
                missing.append(route)
        self.assertEqual(missing, [], "Undocumented API routes — add them to docs/")
