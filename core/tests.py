"""Tests for the role-aware dashboard at `/`.

Covers:
- visible_tabs / default_tab gating per role.
- me_context returns the expected keys for any user.
- team_context exposes scoped pending approvals + direct reports.
- org_context returns headcount/leave aggregates without errors.
- The view renders 200 for each role and respects ?tab= overrides.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, WorkLocation
from core import dashboard as dash
from employees.models import JobInfo
from leave.models import LeaveApplication, LeaveBalance, LeavePolicy
from roster.models import Shift, ShiftType


User = get_user_model()


def _ensure_groups():
    for name in (
        "User Admins", "HR", "All Employees", "Senior Leadership",
        "IT", "Finance",
    ):
        Group.objects.get_or_create(name=name)


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        **extra,
    )


class DashboardFixture(TestCase):
    def setUp(self):
        super().setUp()
        # org_context() memoises its whole payload in the process-wide cache
        # for DASHBOARD_ORG_CACHE_SECONDS, keyed only on today's date. That
        # cache is NOT rolled back with the per-test transaction, so any
        # earlier test in the same run that rendered the dashboard leaves a
        # snapshot of *its* fixture behind for us to read back — payroll's
        # `assertRedirects(resp, "/")` follows the redirect and does exactly
        # that. Clear it per test so these assertions see their own data.
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.eng = Department.objects.create(name="Engineering")

        cls.hr = _user("hr@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))

        cls.manager = _user("mgr@example.com")
        cls.report = _user("rep@example.com")
        JobInfo.objects.create(employee=cls.report, reports_to=cls.manager)

        cls.employee = _user("emp@example.com")

        cls.dept_head = _user("dh@example.com", department=cls.eng)
        cls.eng.head_of_department = cls.dept_head
        cls.eng.save()

        # Refresh so signal-added group memberships are visible.
        for attr in ("hr", "manager", "report", "employee", "dept_head"):
            setattr(cls, attr, User.objects.get(pk=getattr(cls, attr).pk))

        # Seed a LEAVE-category ShiftType + LeavePolicy + a balance row so
        # the leave-balance card has data to render for the report user.
        cls.cl_type = ShiftType.objects.create(
            name="Casual Leave", category=ShiftType.Category.LEAVE,
        )
        cls.cl_policy = LeavePolicy.objects.create(
            shift_type=cls.cl_type, max_days_per_year=10, is_paid=True,
        )
        LeaveBalance.objects.create(
            employee=cls.report, leave_policy=cls.cl_policy,
            year=date.today().year, opening=Decimal("10"),
        )

        # Pending leave from the report — this is what should land in the
        # manager's Team-tab approval queue.
        cls.pending_app = LeaveApplication.objects.create(
            employee=cls.report,
            leave_type=cls.cl_type,
            start_date=date.today() + timedelta(days=3),
            end_date=date.today() + timedelta(days=4),
            applied_by=cls.report,
        )


class VisibleTabsTests(DashboardFixture):
    def test_anonymous_user_has_no_tabs(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(dash.visible_tabs(AnonymousUser()), [])

    def test_plain_employee_only_sees_me(self):
        self.assertEqual(dash.visible_tabs(self.employee), ["me"])
        self.assertEqual(dash.default_tab(self.employee), "me")

    def test_manager_with_reports_sees_me_and_team(self):
        tabs = dash.visible_tabs(self.manager)
        self.assertIn("me", tabs)
        self.assertIn("team", tabs)
        self.assertNotIn("org", tabs)
        self.assertEqual(dash.default_tab(self.manager), "team")

    def test_dept_head_without_reports_sees_only_me(self):
        """Heading a department does NOT grant a dashboard Team tab. The Team
        tab is a manager view of recursive reportees only (see commit c41a472,
        which dropped the dept-head special-casing); a dept head with no direct
        reports lands on Me. Their department visibility lives elsewhere (e.g.
        the employee list), not the dashboard."""
        self.assertEqual(dash.visible_tabs(self.dept_head), ["me"])

    def test_hr_without_reports_sees_me_and_org_only(self):
        tabs = dash.visible_tabs(self.hr)
        self.assertEqual(tabs, ["me", "org"])
        self.assertNotIn("team", tabs)
        self.assertEqual(dash.default_tab(self.hr), "org")

    def test_superuser_without_reports_does_not_see_team(self):
        """Regression: superuser/HR/admins used to see Team via the
        `can_edit_all` short-circuit in `has_managed_scope`. Tab gating now
        keys on actual managed scope so this no longer leaks."""
        su = User.objects.create_superuser(
            email="root@x.com", username="root", password="x",
            first_name="R", last_name="X",
        )
        tabs = dash.visible_tabs(su)
        self.assertNotIn("team", tabs)
        self.assertIn("org", tabs)

    def test_hr_who_also_manages_someone_sees_all_three(self):
        """An HR rep who happens to manage a direct report does see Team."""
        JobInfo.objects.create(employee=self.employee, reports_to=self.hr)
        # Refresh so signal-triggered group changes / caches are visible.
        hr = User.objects.get(pk=self.hr.pk)
        tabs = dash.visible_tabs(hr)
        self.assertEqual(set(tabs), {"me", "team", "org"})


class MeContextTests(DashboardFixture):
    def test_keys_present_for_employee_with_no_data(self):
        ctx = dash.me_context(self.employee)
        for key in (
            "today", "week_start", "week_end", "next_shift", "week_strip",
            "leave_balances", "recent_applications", "upcoming_holidays",
            "is_birthday_today", "probation_days_remaining",
            "profile_completeness", "manager", "can_access_roster",
        ):
            self.assertIn(key, ctx)
        self.assertEqual(len(ctx["week_strip"]), 7)
        self.assertEqual(ctx["leave_balances"], [])
        self.assertIsNone(ctx["next_shift"])
        self.assertEqual(ctx["profile_completeness"]["filled"], 0)
        self.assertFalse(ctx["can_access_roster"])

    def test_can_access_roster_true_for_manager(self):
        ctx = dash.me_context(self.manager)
        self.assertTrue(ctx["can_access_roster"])

    def test_can_access_roster_true_for_hr(self):
        ctx = dash.me_context(self.hr)
        self.assertTrue(ctx["can_access_roster"])

    def test_balance_row_renders_for_user_with_balance(self):
        ctx = dash.me_context(self.report)
        self.assertEqual(len(ctx["leave_balances"]), 1)
        row = ctx["leave_balances"][0]
        self.assertEqual(row["name"], "Casual Leave")
        self.assertEqual(row["available"], Decimal("10"))
        # Capacity is the total granted (opening+accrued+carried), NOT
        # max_days_per_year added on top of opening — opening already holds the
        # year's entitlement, so the quota must not be double-counted.
        self.assertEqual(row["capacity"], Decimal("10"))

    def test_week_strip_shows_location_on_work_day(self):
        """Each work day in the strip carries the office/WFH location so the
        user knows where to be; OFF/leave days carry no location."""
        work = ShiftType.objects.create(name="EVE", category=ShiftType.Category.WORK)
        off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        wfh = WorkLocation.objects.create(name="Work from Home (WFH)")
        week_start = date.today() - timedelta(days=date.today().weekday())
        Shift.objects.create(
            employee=self.employee, date=week_start,
            shift_type=work, work_location=wfh,
        )
        Shift.objects.create(
            employee=self.employee, date=week_start + timedelta(days=1),
            shift_type=off,
        )
        strip = {d["date"]: d for d in dash.me_context(self.employee)["week_strip"]}
        self.assertEqual(strip[week_start]["location"], "WFH")
        self.assertEqual(strip[week_start + timedelta(days=1)]["location"], "")


class TeamContextTests(DashboardFixture):
    def test_pending_application_appears_for_manager(self):
        ctx = dash.team_context(self.manager)
        self.assertEqual(ctx["pending_total"], 1)
        leave_row = next(e for e in ctx["pending_approvals"] if e["key"] == "leave")
        self.assertEqual(leave_row["count"], 1)
        self.assertEqual(ctx["direct_reports_count"], 1)

    def test_scope_count_matches_visible_employees_including_self(self):
        """scope_count must agree with what /employees/ shows (self +
        managed scope), unlike managed_count which excludes self."""
        ctx = dash.team_context(self.manager)
        self.assertEqual(ctx["managed_count"], 1)
        self.assertEqual(ctx["scope_count"], ctx["managed_count"] + 1)


class OrgContextTests(DashboardFixture):
    def test_aggregates_render(self):
        ctx = dash.org_context(self.hr)
        self.assertEqual(ctx["pending_total"], 1)
        self.assertGreaterEqual(ctx["headcount"]["active"], 1)
        for key in (
            "new_joiners_count", "confirmations_due", "status_today",
            "birthdays_today_count", "birthdays_week_count",
            "office_dist", "balance_pressure", "data_quality",
        ):
            self.assertIn(key, ctx)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ViewIntegrationTests(DashboardFixture):
    def _get(self, user, query=""):
        self.client.force_login(user)
        url = reverse("home") + (("?" + query) if query else "")
        return self.client.get(url)

    def test_employee_lands_on_me(self):
        resp = self._get(self.employee)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This week")
        self.assertNotContains(resp, "Org-wide pulse")

    def test_manager_defaults_to_team(self):
        resp = self._get(self.manager)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pending approvals")

    def test_hr_defaults_to_org(self):
        resp = self._get(self.hr)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Org-wide pulse")

    def test_unknown_tab_falls_back_to_default(self):
        resp = self._get(self.employee, query="tab=org")
        self.assertEqual(resp.status_code, 200)
        # Employee can't see Org → should land on Me.
        self.assertContains(resp, "This week")

    def test_explicit_me_tab_works_for_hr(self):
        resp = self._get(self.hr, query="tab=me")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This week")


class OfficeBucketTests(TestCase):
    """Substring-based bucketing of raw Azure-AD office strings."""

    def test_raowa_beats_mohakhali(self):
        from accounts.locations import office_bucket
        self.assertEqual(
            office_bucket("RAOWA Complex, Level-10, VIP Road Mohakhali Dhaka North 1206 BD"),
            "RAOWA",
        )

    def test_mbd_matches_monem(self):
        from accounts.locations import office_bucket
        self.assertEqual(
            office_bucket("Monem Business District, Level-7 Dhaka 1205 BD"),
            "MBD",
        )

    def test_mhk_matches_mohakhali(self):
        from accounts.locations import office_bucket
        self.assertEqual(office_bucket("Dhaka Mohakhali"), "MHK")

    def test_mhk_matches_abbas_garden_address(self):
        from accounts.locations import office_bucket
        self.assertEqual(
            office_bucket("House No – 8 Abbas Garden Rd Dhaka North 1206 BD"),
            "MHK",
        )

    def test_other_for_chattogram(self):
        from accounts.locations import office_bucket
        self.assertEqual(office_bucket("Chattogram"), "OTHER")

    def test_unknown_for_none(self):
        from accounts.locations import office_bucket
        self.assertEqual(office_bucket(None), "UNKNOWN")

    def test_unknown_for_blank_string(self):
        from accounts.locations import office_bucket
        self.assertEqual(office_bucket(""), "UNKNOWN")


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ProjectCreditsTests(TestCase):
    def test_requires_login(self):
        resp = self.client.get(reverse("project_credits"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_any_employee_sees_every_area(self):
        from core.credits import CREDIT_AREAS

        self.client.force_login(_user("plain@sebpo.com"))
        resp = self.client.get(reverse("project_credits"))
        self.assertEqual(resp.status_code, 200)
        for area in CREDIT_AREAS:
            self.assertContains(resp, area["title"].replace("&", "&amp;"))
            for person in area["people"]:
                self.assertContains(resp, person["name"])

    def test_footer_links_to_credits_on_app_pages(self):
        self.client.force_login(_user("plain@sebpo.com"))
        resp = self.client.get(reverse("project_credits"))
        self.assertContains(resp, "SEBPO BD. All Rights Reserved.")
        self.assertContains(resp, "Designed and Developed by Software Development Department.")
        self.assertContains(resp, f'href="{reverse("project_credits")}"')
