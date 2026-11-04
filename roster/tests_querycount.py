"""Query-scaling regression tests for the roster grid.

The grid is the hottest page in the app. It once lazy-loaded the per-cell
location FKs (job_location / work_location) from the resolved_location_choice
templatetag — O(employees × days) queries. These tests pin the fixed-query
property: doubling the employee count must not change the query count.
"""

from datetime import date, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import CustomUser, JobLocation, WorkLocation
from roster.models import Shift, ShiftType


class RosterGridQueryScalingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = CustomUser.objects.create_superuser(
            email="admin@example.com", username="admin",
            first_name="Admin", last_name="User", password="x",
        )
        cls.work = ShiftType.objects.create(name="DAY", category=ShiftType.Category.WORK)
        cls.job_loc = JobLocation.objects.create(name="MHK")
        cls.work_loc = WorkLocation.objects.create(name="WFH")
        cls.start = date(2026, 6, 1)
        cls.days = [cls.start + timedelta(days=i) for i in range(7)]

    def _add_employees(self, n, offset=0):
        employees = []
        for i in range(n):
            emp = CustomUser.objects.create_user(
                email=f"emp{offset + i}@example.com",
                username=f"emp{offset + i}",
                first_name=f"Emp{offset + i}", last_name="Test", password="x",
                job_location=self.job_loc, work_location=self.work_loc,
            )
            employees.append(emp)
        Shift.objects.bulk_create([
            Shift(
                employee=emp, date=d, shift_type=self.work,
                job_location=self.job_loc, work_location=self.work_loc,
            )
            for emp in employees for d in self.days
        ])
        return employees

    def _grid_query_count(self) -> int:
        # `q=Test` puts the grid in search mode: the default (no-search) view
        # shows only the viewer + direct reports, which would render a 1-row
        # grid and trivially hide any per-cell scaling.
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(
                reverse("roster_week"),
                {
                    "q": "Test",
                    "from": self.start.isoformat(),
                    "to": self.days[-1].isoformat(),
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(
            resp.content.decode().count('name="location_ref"'), 20,
            "Grid did not render the per-cell location dropdowns — the "
            "fixture no longer exercises the hot path this test pins.",
        )
        return len(ctx.captured_queries)

    def test_query_count_constant_in_grid_size(self):
        self.client.force_login(self.viewer)
        self._add_employees(4)
        count_small = self._grid_query_count()

        self._add_employees(8, offset=4)
        count_large = self._grid_query_count()

        # 3x the employees (4 → 12, i.e. 28 → 84 located cells) must not add
        # queries. Allow +1 slack for pagination-style COUNT variance.
        self.assertLessEqual(
            count_large, count_small + 1,
            f"Roster grid queries scale with employees: "
            f"{count_small} → {count_large}. A per-cell lazy load is back.",
        )
