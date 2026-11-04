"""Separated (is_active=False) employees in the roster grid.

Bug (2026-09-11): `/roster/?from=…&to=…&q=<name>` returned nothing for an
employee who has since left, even when the date range sat entirely before
their last working day and their historical `Shift` rows were still on file.
`_resolve_grid_employees` went through `visible_employees`, which clamps to
`is_active=True` unconditionally.

Fix: search mode (any of q / department / team / employee) opts into
`include_separated=True`. The default "my team" grid stays active-only,
permission scoping is untouched, and writes stay blocked.
"""

from __future__ import annotations

import re
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, Team
from employees.models import JobInfo
from roster.models import Shift, ShiftType


User = get_user_model()


def _ensure_groups():
    for name in ("User Admins", "HR", "All Employees", "Senior Leadership"):
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
class SeparatedEmployeeRosterVisibilityTests(TestCase):
    FROM = "2026-07-01"
    TO = "2026-07-31"

    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.dept = Department.objects.create(name="Engineering")
        cls.team = Team.objects.create(name="Platform", department=cls.dept)
        cls.other_team = Team.objects.create(name="Tooling", department=cls.dept)

        cls.manager = _user("mgr@example.com", department=cls.dept, team=cls.team)
        cls.active_report = _user(
            "active@example.com", department=cls.dept, team=cls.team,
        )
        JobInfo.objects.create(employee=cls.active_report, reports_to=cls.manager)

        # Souda left on 2026-08-15 and was deactivated the way
        # deactivate_past_lwd would. JobInfo.reports_to is left intact.
        cls.souda = _user(
            "souda@example.com", department=cls.dept, team=cls.team,
            is_active=False, last_working_date=date(2026, 8, 15),
        )
        cls.souda.first_name = "Souda"
        cls.souda.save()
        JobInfo.objects.create(employee=cls.souda, reports_to=cls.manager)

        # An unrelated manager in the SAME department (so the department
        # clamp can't be what hides Souda — only scope can).
        cls.stranger_mgr = _user(
            "stranger@example.com", department=cls.dept, team=cls.other_team,
        )
        cls.stranger_report = _user(
            "srep@example.com", department=cls.dept, team=cls.other_team,
        )
        JobInfo.objects.create(
            employee=cls.stranger_report, reports_to=cls.stranger_mgr,
        )

        cls.hr = _user("hr@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))

        cls.eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK,
        )
        # Historical shift, well before the last working date.
        cls.shift = Shift.objects.create(
            employee=cls.souda, date=date(2026, 7, 15), shift_type=cls.eve,
        )

        for attr in ("manager", "active_report", "souda", "stranger_mgr", "hr"):
            setattr(cls, attr, User.objects.get(pk=getattr(cls, attr).pk))

    def _get(self, viewer, **params):
        self.client.force_login(viewer)
        params.setdefault("from", self.FROM)
        params.setdefault("to", self.TO)
        resp = self.client.get(reverse("roster_week"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    # -- (a) default view is unchanged -----------------------------------

    def test_default_grid_excludes_separated_employee(self):
        body = self._get(self.manager)
        self.assertIn(f"cell-{self.active_report.pk}-", body)
        self.assertNotIn(f"cell-{self.souda.pk}-", body)

    def test_default_grid_excludes_separated_employee_for_hr(self):
        # HR's default grid is direct-reports-only; the point here is that
        # the see-all branch doesn't leak separated rows in either.
        body = self._get(self.hr)
        self.assertNotIn(f"cell-{self.souda.pk}-", body)

    # -- (b) search surfaces them, with their historical shifts -----------

    def test_manager_search_finds_separated_report(self):
        body = self._get(self.manager, q="souda")
        self.assertIn(f"cell-{self.souda.pk}-", body)

    def test_search_shows_historical_shift_rows(self):
        body = self._get(self.manager, q="souda")
        self.assertIn(f"cell-{self.souda.pk}-20260715", body)
        # A separated employee is outside the manager's (active-only)
        # managed scope, so the cell renders read-only text rather than the
        # editable dropdown (see roster/_cell.html `can_edit_cell`) — the
        # historical shift must still be visible in it.
        cell = re.search(
            rf'id="cell-{self.souda.pk}-20260715".*?</td>', body, re.DOTALL,
        )
        self.assertIsNotNone(cell, "historical cell missing")
        self.assertIn(
            self.eve.name, cell.group(0),
            "the separated employee's historical EVE shift is not rendered",
        )

    def test_hr_search_finds_separated_employee(self):
        body = self._get(self.hr, q="souda")
        self.assertIn(f"cell-{self.souda.pk}-", body)

    def test_employee_param_finds_separated_employee(self):
        body = self._get(self.manager, employee=str(self.souda.pk))
        self.assertIn(f"cell-{self.souda.pk}-", body)

    # -- (c) department / team filters do the same -----------------------

    def test_department_filter_surfaces_separated_employee(self):
        body = self._get(self.hr, department=str(self.dept.pk))
        self.assertIn(f"cell-{self.souda.pk}-", body)

    def test_team_filter_surfaces_separated_employee(self):
        body = self._get(self.hr, department=str(self.dept.pk), team=str(self.team.pk))
        self.assertIn(f"cell-{self.souda.pk}-", body)
        self.assertNotIn(f"cell-{self.stranger_report.pk}-", body)

    # -- (d) scope enforcement is unchanged -------------------------------

    def test_out_of_scope_manager_cannot_search_separated_employee(self):
        body = self._get(self.stranger_mgr, q="souda")
        self.assertNotIn(f"cell-{self.souda.pk}-", body)

    def test_out_of_scope_manager_cannot_target_by_employee_param(self):
        body = self._get(self.stranger_mgr, employee=str(self.souda.pk))
        self.assertNotIn(f"cell-{self.souda.pk}-", body)

    # -- (e) reads widen, writes do not ----------------------------------

    def test_cell_update_still_blocked_for_separated_employee(self):
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("roster_cell_update", args=[self.souda.pk, "2026-07-16"]),
            {"shift_ref": f"type:{self.eve.pk}"},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(
            Shift.objects.filter(employee=self.souda, date=date(2026, 7, 16)).exists()
        )

    def test_cell_update_still_works_for_active_report(self):
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("roster_cell_update", args=[self.active_report.pk, "2026-07-16"]),
            {"shift_ref": f"type:{self.eve.pk}"},
        )
        self.assertEqual(resp.status_code, 200)

    # -- export inherits the same resolution ------------------------------

    def test_export_includes_separated_employee_in_search_mode(self):
        """RosterExportView reuses _resolve_grid_employees, so it inherits
        the widening for free — assert on the workbook, not just the 200."""
        import io

        from openpyxl import load_workbook

        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("roster_export"),
            {"q": "souda", "from": self.FROM, "to": self.TO},
        )
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        text = "\n".join(
            str(c.value)
            for row in wb.active.iter_rows()
            for c in row
            if c.value is not None
        )
        self.assertIn("Souda", text)
        self.assertIn("EVE", text)
