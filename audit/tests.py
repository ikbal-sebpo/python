"""Access log tests. docs/ACCESS_LOG_PLAN.md §6."""

from __future__ import annotations

import datetime as dt
from unittest import mock

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone

from accounts.models import CustomUser
from audit.models import AccessLog, Action, DataClass
from audit.registry import EXCLUDED_ROUTES, SENSITIVE_PREFIXES, rule_for
from dynamic_menu.models import ProtectedView
from scheduler.schedule import JOBS

PASSWORD = "testpass-1234!"


def _user(email, **extra):
    return CustomUser.objects.create_user(
        email=email, username=email, password=PASSWORD,
        first_name=email.split("@")[0].title(), last_name="Test", **extra,
    )


def _join(user, name):
    group, _ = Group.objects.get_or_create(name=name)
    user.groups.add(group)
    return group


def _all_routes():
    names = set()

    def walk(patterns, ns=None):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                walk(p.url_patterns, p.namespace or ns)
            elif p.name:
                names.add(f"{ns}:{p.name}" if ns else p.name)

    walk(get_resolver().url_patterns)
    return names


class MiddlewareTests(TestCase):
    def setUp(self):
        self.admin = _user("admin@sebpo.com", is_superuser=True, is_staff=True)
        self.target = _user("target@sebpo.com", employee_id="EMP-9001")
        self.client.login(username="admin@sebpo.com", password=PASSWORD)
        AccessLog.objects.all().delete()  # drop the LOGIN row; tests look at request rows

    def _rows(self):
        return list(AccessLog.objects.order_by("pk"))

    def test_anonymous_request_is_not_logged(self):
        self.client.logout()
        AccessLog.objects.all().delete()
        self.client.get(reverse("employee_list"))
        self.assertEqual(AccessLog.objects.count(), 0)

    def test_profile_view_records_actor_subject_and_class(self):
        response = self.client.get(reverse("employee_detail", args=[self.target.pk]))
        self.assertEqual(response.status_code, 200)
        row = AccessLog.objects.get(route="employee_detail")
        self.assertEqual(row.actor, self.admin)
        self.assertEqual(row.actor_label, "admin@sebpo.com")
        self.assertEqual(row.subject, self.target)
        self.assertIn("EMP-9001", row.subject_label)
        self.assertEqual(row.data_class, DataClass.PROFILE)
        self.assertEqual(row.action, Action.VIEW)
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.method, "GET")
        self.assertFalse(row.is_htmx)
        self.assertTrue(row.session)
        self.assertEqual(row.ip_address, "127.0.0.1")

    def test_excluded_reference_route_is_not_logged(self):
        self.client.get(reverse("ajax_load_teams"))
        self.assertFalse(AccessLog.objects.filter(route="ajax_load_teams").exists())

    def test_protected_view_bounce_is_recorded_as_denied(self):
        viewer = _user("plain@sebpo.com")
        self.client.login(username="plain@sebpo.com", password=PASSWORD)
        ProtectedView.objects.filter(view_name="payroll_salary_list").delete()
        response = self.client.get(reverse("payroll_salary_list"))
        self.assertEqual(response.status_code, 302)  # bounced to the dashboard
        row = AccessLog.objects.get(route="payroll_salary_list")
        self.assertEqual(row.action, Action.DENIED)
        self.assertEqual(row.data_class, DataClass.SALARY)
        self.assertEqual(row.actor, viewer)

    def test_htmx_fragment_is_flagged(self):
        self.client.get(reverse("employee_detail", args=[self.target.pk]), HTTP_HX_REQUEST="true")
        self.assertTrue(AccessLog.objects.get(route="employee_detail").is_htmx)

    def test_secret_query_keys_are_stripped(self):
        self.client.get(reverse("employee_list") + "?next=/payroll/&q=jane&token=abc")
        row = AccessLog.objects.get(route="employee_list")
        self.assertEqual(row.query, "q=jane")

    def test_post_is_a_write(self):
        self.client.post(reverse("employee_list"))
        row = AccessLog.objects.get(route="employee_list")
        self.assertEqual(row.action, Action.WRITE)
        self.assertEqual(row.method, "POST")

    @override_settings(TIMELY_API_TOKEN="timely-secret")
    def test_api_service_account_is_a_label_not_a_user(self):
        self.client.logout()
        AccessLog.objects.all().delete()
        response = self.client.get(reverse("api_shift_types"), HTTP_AUTHORIZATION="Token timely-secret")
        self.assertEqual(response.status_code, 200)
        row = AccessLog.objects.get(route="api_shift_types")
        self.assertIsNone(row.actor)
        self.assertEqual(row.actor_label, "timely-service-account")
        self.assertEqual(row.data_class, DataClass.API)

    def test_a_failed_write_never_breaks_the_request(self):
        with mock.patch("audit.middleware.AccessLog.objects.create", side_effect=RuntimeError("db down")):
            with self.assertLogs("audit.middleware", level="ERROR"):
                response = self.client.get(reverse("employee_detail", args=[self.target.pk]))
        self.assertEqual(response.status_code, 200)

    def test_detail_with_a_money_key_is_refused_without_breaking_the_request(self):
        from audit.capture import note
        # Simulate a view enriching with a forbidden key: the row is dropped, the response survives.
        from audit import middleware as mw
        original = mw.AccessLogMiddleware.process_view

        def poisoned(self_, request, *a, **k):
            original(self_, request, *a, **k)
            note(request, amount=1000)

        with mock.patch.object(mw.AccessLogMiddleware, "process_view", poisoned):
            with self.assertLogs("audit.middleware", level="ERROR"):
                response = self.client.get(reverse("employee_list"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AccessLog.objects.filter(route="employee_list").exists())

    @override_settings(ACCESS_LOG_ENABLED=False)
    def test_kill_switch(self):
        self.client.get(reverse("employee_list"))
        self.assertEqual(AccessLog.objects.count(), 0)


class AuthSignalTests(TestCase):
    def setUp(self):
        self.user = _user("someone@sebpo.com")

    def test_login_logout_and_failure_rows(self):
        self.client.login(username="someone@sebpo.com", password="wrong")
        failed = AccessLog.objects.get(action=Action.LOGIN_FAILED)
        self.assertEqual(failed.data_class, DataClass.AUTH)
        self.assertIsNone(failed.actor)
        self.assertEqual(failed.actor_label, "someone@sebpo.com")

        self.client.login(username="someone@sebpo.com", password=PASSWORD)
        login = AccessLog.objects.get(action=Action.LOGIN)
        self.assertEqual(login.actor, self.user)
        self.assertEqual(login.detail, {"backend": "password"})

        self.client.logout()
        self.assertTrue(AccessLog.objects.filter(action=Action.LOGOUT, actor=self.user).exists())


class ModelTests(TestCase):
    def test_rows_are_append_only(self):
        row = AccessLog.objects.create(method="GET", path="/x/", actor_label="a@b")
        row.path = "/y/"
        with self.assertRaises(TypeError):
            row.save()
        with self.assertRaises(TypeError):
            row.delete()
        self.assertEqual(AccessLog.objects.get(pk=row.pk).path, "/x/")

    def test_money_keys_in_detail_are_refused(self):
        with self.assertRaises(ValueError):
            AccessLog.objects.create(method="GET", path="/x/", detail={"Gross": 100})
        AccessLog.objects.create(method="GET", path="/x/", detail={"rows": 100, "file": "a.xlsx"})


class RegistryGuardTests(TestCase):
    def test_sensitive_routes_are_classified(self):
        unclassified = sorted(
            route for route in _all_routes()
            if route.startswith(SENSITIVE_PREFIXES) and route not in EXCLUDED_ROUTES
            and (rule_for(route) is None or rule_for(route).data_class == DataClass.PAGE)
        )
        self.assertEqual(unclassified, [], "add these routes to audit/registry.py")

    def test_excluded_routes_exist(self):
        routes = _all_routes()
        missing = sorted(r for r in EXCLUDED_ROUTES if r not in routes and not r.startswith("admin:"))
        self.assertEqual(missing, [])

    def test_admin_routes_refine_by_model(self):
        self.assertEqual(rule_for("admin:payroll_salarystructure_change").data_class, DataClass.SALARY)
        self.assertEqual(rule_for("admin:payroll_salarystructure_change").object_type, "payroll_salarystructure")
        self.assertEqual(rule_for("admin:auth_group_changelist").data_class, DataClass.ADMIN)
        self.assertEqual(rule_for("admin:index").data_class, DataClass.ADMIN)

    def test_prune_job_is_seeded(self):
        self.assertIn("prune_access_log", {job.command for job in JOBS})


class ViewerTests(TestCase):
    def setUp(self):
        pv, _ = ProtectedView.objects.get_or_create(view_name="audit_access_log", defaults={"registered_app": "audit"})
        self.viewers = Group.objects.get_or_create(name="Audit Viewers")[0]
        pv.allowed_groups.set([self.viewers])
        self.viewer = _user("viewer@sebpo.com")
        self.viewer.groups.add(self.viewers)
        self.plain = _user("plain@sebpo.com")
        self.subject = _user("subject@sebpo.com", employee_id="EMP-1")
        AccessLog.objects.create(method="GET", path="/employees/x/", route="employee_detail",
                                 actor=self.plain, actor_label="plain@sebpo.com",
                                 subject=self.subject, subject_label="Subject Test · EMP-1",
                                 data_class=DataClass.PROFILE)
        AccessLog.objects.create(method="GET", path="/leave/", route="leave_list",
                                 actor=self.plain, actor_label="plain@sebpo.com", data_class=DataClass.LEAVE)

    def test_audit_viewer_can_open_and_filter(self):
        self.client.login(username="viewer@sebpo.com", password=PASSWORD)
        response = self.client.get(reverse("audit_access_log"), {"subject": str(self.subject.pk)})
        self.assertEqual(response.status_code, 200)
        rows = list(response.context["rows"])
        self.assertEqual([r.route for r in rows], ["employee_detail"])

        response = self.client.get(reverse("audit_access_log"), {"data_class": "LEAVE", "actor": "plain"})
        self.assertEqual([r.route for r in response.context["rows"]], ["leave_list"])

    def test_export_is_logged_as_an_audit_export(self):
        self.client.login(username="viewer@sebpo.com", password=PASSWORD)
        response = self.client.get(reverse("audit_access_log"), {"export": "xlsx"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        row = AccessLog.objects.get(route="audit_access_log")
        self.assertEqual(row.action, Action.EXPORT)
        self.assertEqual(row.data_class, DataClass.AUDIT)
        self.assertIn("file", row.detail)

    def test_plain_user_is_bounced(self):
        self.client.login(username="plain@sebpo.com", password=PASSWORD)
        response = self.client.get(reverse("audit_access_log"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AccessLog.objects.get(route="audit_access_log").action, Action.DENIED)

    def test_profile_card_lists_who_viewed(self):
        superuser = _user("su@sebpo.com", is_superuser=True, is_staff=True)
        self.client.login(username="su@sebpo.com", password=PASSWORD)
        response = self.client.get(reverse("employee_detail", args=[self.subject.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_view_access_log"])
        self.assertEqual([r.actor_label for r in response.context["access_history"]], ["plain@sebpo.com"])
        self.assertContains(response, "Access history")

        self.client.login(username="plain@sebpo.com", password=PASSWORD)
        response = self.client.get(reverse("employee_detail", args=[self.plain.pk]))
        self.assertNotContains(response, "Access history")


class PruneTests(TestCase):
    def test_prune_removes_only_rows_past_retention(self):
        old = AccessLog.objects.create(method="GET", path="/old/")
        AccessLog.objects.filter(pk=old.pk).update(occurred_at=timezone.now() - dt.timedelta(days=800))
        AccessLog.objects.create(method="GET", path="/new/")
        call_command("prune_access_log", "--dry-run", verbosity=0)
        self.assertEqual(AccessLog.objects.count(), 2)
        call_command("prune_access_log", verbosity=0)
        self.assertEqual([r.path for r in AccessLog.objects.all()], ["/new/"])
        call_command("prune_access_log", "--days", "0", verbosity=0)
        self.assertEqual(AccessLog.objects.count(), 0)
