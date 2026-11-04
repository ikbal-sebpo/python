"""Access tests for roster views."""

from __future__ import annotations

import re
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, Team
from employees.models import JobInfo
from roster.models import Roster, Shift, ShiftAttribute, ShiftChangeLog, ShiftType


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


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class RosterAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.eng = Department.objects.create(name="Engineering")

        cls.plain = _user("plain@example.com")
        cls.manager = _user("mgr@example.com")
        cls.report = _user("rep@example.com")
        JobInfo.objects.create(employee=cls.report, reports_to=cls.manager)

        cls.hr = _user("hr@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))

        for attr in ("plain", "manager", "report", "hr"):
            setattr(cls, attr, User.objects.get(pk=getattr(cls, attr).pk))

    def test_plain_employee_sees_own_read_only_roster(self):
        """A plain employee with no managed scope gets a personal, read-only
        view of just their own row instead of a 403 — the sidebar link is
        open to everyone, so the page itself must never dead-end."""
        self.client.force_login(self.plain)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f"cell-{self.plain.pk}-", body)
        self.assertFalse(resp.context["is_manager"])
        self.assertNotIn("Upload roster", body)

    def test_report_with_no_downline_sees_own_read_only_roster(self):
        """A user who reports to someone but has no reports of their own
        is still a 'plain employee' for roster purposes — same read-only,
        self-only view as any other non-manager."""
        self.client.force_login(self.report)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f"cell-{self.report.pk}-", body)
        self.assertFalse(resp.context["is_manager"])

    def test_manager_with_reports_can_access_roster_week(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)

    def test_hr_can_access_roster_week_without_reports(self):
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)

    def test_manager_default_view_shows_only_direct_reports(self):
        """Default grid = direct reports only; indirect reports are hidden until
        the viewer searches or filters."""
        grandchild = _user("grand@example.com")
        JobInfo.objects.create(employee=grandchild, reports_to=self.report)

        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f"cell-{self.report.pk}-", body)        # direct report shown
        self.assertNotIn(f"cell-{grandchild.pk}-", body)      # indirect hidden

    def test_manager_search_reaches_indirect_report(self):
        """A name search surfaces anyone within the viewer's downline, including
        indirect reports several levels down."""
        grandchild = _user("grand@example.com")
        JobInfo.objects.create(employee=grandchild, reports_to=self.report)

        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"), {"q": "Grand"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"cell-{grandchild.pk}-", resp.content.decode())

    def test_search_cannot_reach_outside_downline(self):
        """Search stays confined to the visible scope — a non-report never
        appears, even by exact name."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"), {"q": "Plain"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(f"cell-{self.plain.pk}-", resp.content.decode())

    def test_hr_default_view_is_direct_reports_only(self):
        """The direct-reports default applies to HR too: full org visibility, but
        an empty default grid when they have no direct reports."""
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(f"cell-{self.manager.pk}-", body)
        self.assertNotIn(f"cell-{self.report.pk}-", body)

    def test_hr_search_reaches_anyone(self):
        """HR's search spans the whole org (their visible scope)."""
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"), {"q": "Rep"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"cell-{self.report.pk}-", resp.content.decode())

    def test_search_matches_employee_id_and_email_exactly(self):
        """ID and email match exactly — "539" never lands on "1539", and an
        email local part ("ab") never lands on "xab@"/"abc@" — while name
        stays a substring search."""
        def make(email, employee_id):
            return User.objects.create_user(
                email=email, username=email, password="x",
                first_name="Qx", last_name="Qx", employee_id=employee_id,
            )

        exact = make("ab@example.com", "539")
        near = make("abc@example.com", "1539")
        also_near = make("xab@example.com", "5390")
        self.client.force_login(self.hr)
        for q in ("539", "ab", "AB@example.com"):
            body = self.client.get(reverse("roster_week"), {"q": q}).content.decode()
            self.assertIn(f"cell-{exact.pk}-", body, q)
            self.assertNotIn(f"cell-{near.pk}-", body, q)
            self.assertNotIn(f"cell-{also_near.pk}-", body, q)

    def test_locked_user_department_dropdown_is_scoped(self):
        """A non-privileged manager sees only their own department, with no
        'All departments' option."""
        self.manager.department = self.eng
        self.manager.save()
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"))
        body = resp.content.decode()
        self.assertNotIn("All departments", body)
        self.assertIn(self.eng.name, body)

    def test_privileged_user_sees_all_departments_option(self):
        """HR keeps the 'All departments' option."""
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"))
        self.assertIn("All departments", resp.content.decode())

    def test_leave_shift_is_visible_in_grid(self):
        """A materialised leave Shift carries a shift_type but no attribute. When
        that leave type has attributes, the cell must still show it (regression:
        it used to render blank because no attribute-option matched)."""
        cl = ShiftType.objects.create(
            name="CL", category=ShiftType.Category.LEAVE,
        )
        # Give it an attribute so it follows the has_attribute rendering path.
        ShiftAttribute.objects.create(
            shift_type=cl, name="CL Full", start_time=time(9, 0), end_time=time(18, 0),
        )
        self.assertTrue(cl.has_attribute)
        Shift.objects.create(
            employee=self.report, date=date(2026, 6, 1),
            shift_type=cl, shift_attribute=None, note="Leave #1",
        )

        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_week"),
            {"employee": str(self.report.pk), "from": "2026-06-01", "to": "2026-06-01"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f'value="type:{cl.pk}" selected disabled', body)
        self.assertIn(f"cell-{self.report.pk}-20260601", body)

    def test_flat_view_shows_daily_rows_in_tracker_format(self):
        eve = ShiftType.objects.create(name="EVE", category=ShiftType.Category.WORK)
        off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        self.report.employee_id = "51761"
        self.report.username = "md.rahman"
        self.report.save(update_fields=["employee_id", "username"])
        Shift.objects.create(employee=self.report, date=date(2026, 8, 19), shift_type=eve)
        Shift.objects.create(employee=self.report, date=date(2026, 8, 20), shift_type=off)

        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_week"),
            {
                "view": "flat",
                "q": "Rep",
                "from": "2026-08-19",
                "to": "2026-08-20",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Tracker Current Shift Status", body)
        self.assertIn("<td>51761</td>", body)
        self.assertIn(f"<td>{self.report.display_name}</td>", body)
        self.assertIn('<td class="font-medium">EVE</td>', body)
        self.assertIn('<td class="font-medium">OFF</td>', body)

    def test_flat_view_status_filter_narrows_to_one_shift_type(self):
        """The Filters popover's flat-only `?status=` picks a single ShiftType.

        It must not widen the employee scope the way `?q=` does — that is
        `_search_active`'s job, and `status` is deliberately not one of its
        keys.
        """
        eve = ShiftType.objects.create(name="EVE-FLAT", category=ShiftType.Category.WORK)
        off = ShiftType.objects.create(name="OFF-FLAT", category=ShiftType.Category.OFF)
        self.report.employee_id = "53770"
        self.report.username = "ifte.samul"
        self.report.save(update_fields=["employee_id", "username"])
        Shift.objects.create(employee=self.report, date=date(2026, 8, 19), shift_type=eve)
        Shift.objects.create(employee=self.report, date=date(2026, 8, 20), shift_type=off)

        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_week"),
            {
                "view": "flat",
                "q": "Rep",
                "status": str(eve.pk),
                "from": "2026-08-19",
                "to": "2026-08-20",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("1 roster entries", body)
        self.assertIn('<td class="font-medium">EVE-FLAT</td>', body)
        self.assertNotIn('<td class="font-medium">OFF-FLAT</td>', body)
        self.assertTrue(resp.context["filters_active"])

    def test_flat_view_paginates_a_long_range(self):
        """The long-range case the flat view exists for: 40 days of one
        employee's shifts page at 25/page instead of being truncated."""
        eve = ShiftType.objects.create(name="EVE-PAGE", category=ShiftType.Category.WORK)
        Shift.objects.bulk_create([
            Shift(employee=self.report, date=date(2026, 8, 1) + timedelta(days=i), shift_type=eve)
            for i in range(40)
        ])

        self.client.force_login(self.manager)
        params = {
            "view": "flat",
            "employee": str(self.report.pk),
            "from": "2026-08-01",
            "to": "2026-09-09",
        }
        resp = self.client.get(reverse("roster_week"), params)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["paginator"].count, 40)
        self.assertEqual(len(resp.context["flat_rows"]), 25)
        self.assertIn("40 roster entries", resp.content.decode())

        resp = self.client.get(reverse("roster_week"), {**params, "page": "2"})
        self.assertEqual(len(resp.context["flat_rows"]), 15)

    def test_flat_view_takes_an_unbounded_date_range(self):
        """No cap in flat view — it is read-only and paginated in SQL, so the
        range costs nothing per day. The grid stays capped (see below)."""
        eve = ShiftType.objects.create(name="EVE-RANGE", category=ShiftType.Category.WORK)
        Shift.objects.create(employee=self.report, date=date(2021, 3, 4), shift_type=eve)
        Shift.objects.create(employee=self.report, date=date(2029, 11, 7), shift_type=eve)

        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_week"),
            {
                "view": "flat",
                "employee": str(self.report.pk),
                "from": "2020-01-01",
                "to": "2030-12-31",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["date_from"], date(2020, 1, 1))
        self.assertEqual(resp.context["date_to"], date(2030, 12, 31))
        self.assertFalse(resp.context["range_clamped"])
        dates = [r["date"] for r in resp.context["flat_rows"]]
        self.assertIn(date(2021, 3, 4), dates)
        self.assertIn(date(2029, 11, 7), dates)

    def test_flat_view_does_not_materialise_pattern_shifts(self):
        """Viewing must not write. The grid prefills pattern rows across its
        (capped) range; doing that on an uncapped flat range would mass-create
        Shift rows — and via bulk_create, bypassing the leave-ledger signal."""
        mor = ShiftType.objects.create(name="MOR-PREFILL", category=ShiftType.Category.WORK)
        for weekday in range(7):
            Roster.objects.create(employee=self.report, weekday=weekday, shift_type=mor)

        self.client.force_login(self.manager)
        params = {
            "employee": str(self.report.pk),
            "from": "2027-01-01",
            "to": "2027-03-31",
        }
        before = Shift.objects.count()
        resp = self.client.get(reverse("roster_week"), {**params, "view": "flat"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Shift.objects.count(), before, "flat view wrote Shift rows")

        # The grid, by contrast, still materialises them.
        resp = self.client.get(reverse("roster_week"), params)
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(Shift.objects.count(), before)

    def test_flat_export_matches_the_flat_view_and_round_trips(self):
        """`/roster/export/?view=flat` downloads the flat view's own rows —
        uncapped range, `?status=` honoured, one row per shift — in the flat
        uploader's header layout, so the file parses straight back."""
        import io

        import openpyxl

        from roster.upload import parse_upload

        eve = ShiftType.objects.create(name="EVE-EXP", category=ShiftType.Category.WORK)
        off = ShiftType.objects.create(name="OFF-EXP", category=ShiftType.Category.OFF)
        self.report.employee_id = "60123"
        self.report.save(update_fields=["employee_id"])
        # 300 days apart: well past the grid's single-employee cap.
        Shift.objects.create(employee=self.report, date=date(2026, 1, 5), shift_type=eve)
        Shift.objects.create(employee=self.report, date=date(2026, 11, 1), shift_type=eve)
        Shift.objects.create(employee=self.report, date=date(2026, 11, 2), shift_type=off)

        self.client.force_login(self.manager)
        params = {
            "view": "flat",
            "employee": str(self.report.pk),
            "from": "2026-01-01",
            "to": "2026-12-31",
            "status": str(eve.pk),
        }
        resp = self.client.get(reverse("roster_export"), params)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("roster-flat-", resp["Content-Disposition"])

        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(
            rows[0], ("Date", "EmployeeID", "User Name", "Team", "Location", "Shift Status"),
        )
        body = rows[1:]
        self.assertEqual([r[0].date() for r in body], [date(2026, 1, 5), date(2026, 11, 1)])
        self.assertEqual({r[1] for r in body}, {"60123"})
        self.assertEqual({r[5] for r in body}, {"EVE-EXP"})

        parsed, errors, dups = parse_upload(
            io.BytesIO(resp.content), "roster.xlsx", today=date(2026, 1, 1),
        )
        self.assertEqual((errors, dups), ([], []))
        self.assertEqual(len(parsed), 2)

        # The page's Export button carries the view and status through.
        page = self.client.get(reverse("roster_week"), params)
        self.assertIn(f"&view=flat&status={eve.pk}", page.content.decode())

    def _org_wide_fixture(self):
        """Two employees outside HR's line, in another department, one of
        them separated inside the range."""
        ops = Department.objects.create(name="Operations")
        eve = ShiftType.objects.create(name="EVE-ORG", category=ShiftType.Category.WORK)
        stranger = _user("stranger@example.com", department=ops, employee_id="70001")
        leaver = _user(
            "leaver@example.com", department=ops, employee_id="70002",
            is_active=False, last_working_date=date(2026, 9, 20),
        )
        Shift.objects.create(employee=stranger, date=date(2026, 9, 18), shift_type=eve)
        Shift.objects.create(employee=leaver, date=date(2026, 9, 17), shift_type=eve)
        return stranger, leaver

    _PAYROLL_PERIOD = {
        "view": "flat", "from": "2026-09-16", "to": "2026-10-15",
        "q": "", "department": "", "team": "", "status": "",
    }

    def test_flat_view_is_org_wide_for_hr_with_no_filters(self):
        """HR's "All departments" submits `department=` — indistinguishable
        from no filter — so the flat view must not fall back to HR's own
        direct reports. Separated employees' shifts in range are included."""
        stranger, leaver = self._org_wide_fixture()
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"), self._PAYROLL_PERIOD)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["org_wide"])
        self.assertEqual(resp.context["paginator"].count, 2)
        body = resp.content.decode()
        self.assertIn("<td>70001</td>", body)
        self.assertIn("<td>70002</td>", body)
        self.assertIn("Showing every employee", body)

    def test_flat_export_is_org_wide_for_superuser(self):
        import io

        import openpyxl

        self._org_wide_fixture()
        root = User.objects.create_superuser(
            email="root@example.com", username="root@example.com", password="x",
        )
        self.client.force_login(root)
        resp = self.client.get(reverse("roster_export"), self._PAYROLL_PERIOD)
        self.assertEqual(resp.status_code, 200)
        rows = list(openpyxl.load_workbook(io.BytesIO(resp.content)).active.iter_rows(values_only=True))
        self.assertEqual({r[1] for r in rows[1:]}, {"70001", "70002"})

    def test_org_wide_flat_view_drops_accounts_without_employee_id(self):
        """Service/test logins ("Intern hr01") have shifts but no employee
        ID — they are not employees and must not reach the payroll export."""
        self._org_wide_fixture()
        intern = _user("intern.hr01@example.com")
        Shift.objects.create(
            employee=intern, date=date(2026, 10, 5),
            shift_type=ShiftType.objects.get(name="EVE-ORG"),
        )
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"), self._PAYROLL_PERIOD)
        self.assertEqual(resp.context["paginator"].count, 2)
        self.assertNotIn(intern.display_name, resp.content.decode())

    def test_org_wide_flat_view_still_honours_department_filter(self):
        stranger, _ = self._org_wide_fixture()
        mine = _user("eng@example.com", department=self.eng, employee_id="70003")
        eve = ShiftType.objects.get(name="EVE-ORG")
        Shift.objects.create(employee=mine, date=date(2026, 9, 18), shift_type=eve)
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("roster_week"), {**self._PAYROLL_PERIOD, "department": str(self.eng.pk)},
        )
        self.assertEqual(resp.context["paginator"].count, 1)
        self.assertIn("<td>70003</td>", resp.content.decode())

    def test_manager_flat_view_keeps_direct_reports_default(self):
        """Only `can_see_all` viewers get the org-wide default; a manager's
        flat view still opens on self + direct reports."""
        self._org_wide_fixture()
        eve = ShiftType.objects.get(name="EVE-ORG")
        Shift.objects.create(employee=self.report, date=date(2026, 9, 18), shift_type=eve)
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"), self._PAYROLL_PERIOD)
        self.assertFalse(resp.context["org_wide"])
        self.assertEqual(resp.context["paginator"].count, 1)

    def test_hr_grid_view_is_not_org_wide(self):
        stranger, _ = self._org_wide_fixture()
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("roster_week"), {"from": "2026-09-16", "to": "2026-09-22"},
        )
        self.assertFalse(resp.context["org_wide"])
        self.assertNotIn(f"cell-{stranger.pk}-", resp.content.decode())

    def test_payroll_period_menu_lists_16th_to_15th_ranges(self):
        from roster.views import _recent_payroll_periods

        periods = _recent_payroll_periods(date(2026, 11, 2))
        self.assertEqual(
            [(p["label"], p["from"], p["to"]) for p in periods],
            [
                ("November 2026", "2026-10-16", "2026-11-15"),
                ("October 2026", "2026-09-16", "2026-10-15"),
                ("September 2026", "2026-08-16", "2026-09-15"),
            ],
        )
        self.assertEqual(
            _recent_payroll_periods(date(2026, 1, 5))[1]["from"], "2025-11-16",
        )

        self.client.force_login(self.hr)
        body = self.client.get(reverse("roster_week"), {"view": "flat"}).content.decode()
        self.assertIn("Payroll period", body)

    def test_export_without_view_flat_stays_the_grid_layout(self):
        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_export"),
            {"employee": str(self.report.pk), "from": "2026-08-01", "to": "2026-08-07"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("roster-flat-", resp["Content-Disposition"])

    def test_grid_view_still_clamps_and_says_so(self):
        """The grid draws a column per day with a dropdown per cell, so its
        cap stays — but a truncated range must be visible, not silent."""
        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_week"),
            {"employee": str(self.report.pk), "from": "2026-01-01", "to": "2030-12-31"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["range_clamped"])
        self.assertEqual(resp.context["range_cap_days"], 186)
        self.assertEqual(
            resp.context["date_to"], date(2026, 1, 1) + timedelta(days=185),
        )
        self.assertIn("Grid view caps the range", resp.content.decode())

    def test_team_options_carry_their_department_for_the_cascade(self):
        """The Filters popover narrows Team to the picked Department in JS,
        so every pickable team must be in the DOM tagged with its department
        — pre-narrowing server-side would leave the cascade nothing to work
        with once the user changes Department without reloading."""
        other = Department.objects.create(name="Support")
        mine = Team.objects.create(name="Grid Team", department=self.eng)
        theirs = Team.objects.create(name="Support Team", department=other)

        self.client.force_login(self.hr)
        resp = self.client.get(reverse("roster_week"), {"department": str(self.eng.pk)})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()

        select = re.search(r'<select id="roster-team".*?</select>', body, re.S).group(0)
        self.assertIn('data-team-for="roster-department"', select)
        # Both teams present (JS filters), each tagged with its department.
        self.assertIn(f'value="{mine.pk}"', select)
        self.assertIn(f'value="{theirs.pk}"', select)
        self.assertIn(f'data-department="{self.eng.pk}"', select)
        self.assertIn(f'data-department="{other.pk}"', select)

    def test_out_of_department_team_is_still_ignored_server_side(self):
        """Widening the rendered options must not widen what's honoured: a
        team outside the chosen department is dropped by _effective_team_id."""
        other = Department.objects.create(name="Support 2")
        theirs = Team.objects.create(name="Support Team 2", department=other)

        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("roster_week"),
            {"department": str(self.eng.pk), "team": str(theirs.pk)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context["selected_team_id"])

    def test_grid_view_skips_flat_row_building(self):
        """Grid mode must not pay for flat rows or a paginator it never uses."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("roster_week"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("flat_rows", resp.context)
        self.assertNotIn("paginator", resp.context)

    def test_cell_update_writes_a_shift_change_log_row(self):
        """CellUpdateView already sets updated_by on save — the audit-log
        signal (roster/signals.py) must capture the change with zero
        wiring in this view, same as the external write API."""
        mor = ShiftType.objects.create(name="MOR-CELLTEST", category=ShiftType.Category.WORK)
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("roster_cell_update", args=[self.report.pk, "2026-06-02"]),
            {"shift_ref": f"type:{mor.pk}"},
        )
        self.assertEqual(resp.status_code, 200)
        log = ShiftChangeLog.objects.get(
            employee=self.report, date=date(2026, 6, 2),
            action_type=ShiftChangeLog.ActionType.CREATED,
        )
        self.assertEqual(log.changed_by_id, self.manager.pk)
        self.assertEqual(log.new_shift_type_id, mor.pk)
