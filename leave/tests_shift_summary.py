from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, Team
from leave.models import LeaveBalance, LeavePolicy
from leave.services import get_or_create_balance
from payroll.reports import (
    CrossYearSummaryError,
    ShiftSummaryFilters,
    build_shift_summary,
    build_shift_summary_rows,
    shift_summary_employees,
)
from roster.models import Shift, ShiftType


User = get_user_model()


def _user(email, **extra):
    return User.objects.create_user(
        email=email,
        username=email,
        password="test-1234!",
        first_name=email.split("@")[0].title(),
        last_name="Test",
        **extra,
    )


class _ShiftSummaryFixture(TestCase):
    def setUp(self):
        self.ops = Department.objects.create(name="Operations")
        self.hr = Department.objects.create(name="HR")

        self.work = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        self.off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        self.gh = ShiftType.objects.create(name="GH", category=ShiftType.Category.HOLIDAY)
        self.fl = ShiftType.objects.create(name="FL", category=ShiftType.Category.HOLIDAY)

        self.leave_types = {
            code: ShiftType.objects.create(name=code, category=ShiftType.Category.LEAVE)
            for code in ("AL", "CL", "SL", "LWP", "MTL", "CPL")
        }
        self.policies = {
            "AL": LeavePolicy.objects.create(
                shift_type=self.leave_types["AL"],
                accrual_method=LeavePolicy.AccrualMethod.WORKED_DAY_RATIO,
                accrual_ratio_denominator=18,
                carry_forward_pct=Decimal("0.50"),
                max_carry_forward_days=60,
            ),
            "CL": LeavePolicy.objects.create(
                shift_type=self.leave_types["CL"],
                max_days_per_year=10,
                accrual_method=LeavePolicy.AccrualMethod.FIXED_ANNUAL,
            ),
            "SL": LeavePolicy.objects.create(
                shift_type=self.leave_types["SL"],
                max_days_per_year=14,
                accrual_method=LeavePolicy.AccrualMethod.FIXED_ANNUAL,
            ),
        }

        self.support = Team.objects.create(name="Support", department=self.ops)

        self.alice = _user(
            "alice@example.com",
            employee_id="001",
            department=self.ops,
            team=self.support,
            joining_date=date(2026, 1, 15),
        )
        self.bob = _user(
            "bob@example.com",
            employee_id="002",
            department=self.ops,
            joining_date=date(2025, 1, 1),
        )
        self.carol = _user(
            "carol@example.com",
            employee_id="003",
            department=self.hr,
            joining_date=date(2026, 1, 1),
        )
        self.terminated = _user(
            "term@example.com",
            employee_id="004",
            department=self.ops,
            joining_date=date(2025, 1, 1),
            last_working_date=date(2025, 12, 31),
        )

    def _row(self, rows, employee_id):
        return next(row for row in rows if row["employee_id"] == employee_id)


class ShiftSummaryReportTests(_ShiftSummaryFixture):

    def test_counts_shifts_and_includes_active_employees_without_shifts(self):
        for day in (1, 2, 3):
            Shift.objects.create(
                employee=self.alice,
                date=date(2026, 2, day),
                shift_type=self.work,
            )
        for shift_type, day in (
            (self.off, 4),
            (self.leave_types["CL"], 5),
            (self.leave_types["SL"], 6),
            (self.leave_types["AL"], 7),
            (self.leave_types["LWP"], 8),
            (self.leave_types["MTL"], 9),
            (self.leave_types["CPL"], 10),
            (self.gh, 11),
            (self.fl, 12),
        ):
            Shift.objects.create(
                employee=self.alice,
                date=date(2026, 2, day),
                shift_type=shift_type,
            )

        rows = build_shift_summary(
            ShiftSummaryFilters(date(2026, 1, 1), date(2026, 3, 31), self.ops.pk)
        )

        self.assertEqual([row["employee_id"] for row in rows], ["001", "002"])
        alice = self._row(rows, "001")
        self.assertEqual(alice["team"], "Support")
        self.assertEqual(alice["days_worked"], 3)
        self.assertEqual(alice["OFF"], 1)
        self.assertEqual(alice["CL"], 1)
        self.assertEqual(alice["SL"], 1)
        self.assertEqual(alice["AL"], 1)
        self.assertEqual(alice["LWP"], 1)
        self.assertEqual(alice["MTL"], 1)
        self.assertEqual(alice["CPL"], 1)
        self.assertEqual(alice["GH"], 2)

        bob = self._row(rows, "002")
        self.assertEqual(bob["team"], "")  # no team assigned
        self.assertEqual(bob["days_worked"], 0)
        self.assertEqual(bob["total_leave_taken"], 0)

    def test_allocation_uses_inclusive_prorated_cl_sl_and_al_ledger(self):
        LeaveBalance.objects.create(
            employee=self.alice,
            leave_policy=self.policies["AL"],
            year=2026,
            opening=Decimal("1.00"),
            accrued=Decimal("2.00"),
            carried_forward=Decimal("3.00"),
        )
        LeaveBalance.objects.create(
            employee=self.alice,
            leave_policy=self.policies["CL"],
            year=2026,
            opening=Decimal("10.00"),
            accrued=Decimal("0.00"),
            carried_forward=Decimal("99.00"),
        )

        rows = build_shift_summary(
            ShiftSummaryFilters(date(2026, 1, 1), date(2026, 3, 31), self.ops.pk)
        )
        alice = self._row(rows, "001")

        self.assertEqual(alice["first_working_date"], date(2026, 1, 15))
        self.assertEqual(alice["al_allocated"], Decimal("6.00"))
        self.assertEqual(alice["cl_allocated"], Decimal("2"))
        self.assertEqual(alice["sl_allocated"], Decimal("3"))
        self.assertEqual(alice["total_leave_allocated"], Decimal("11.00"))

    def test_al_allocation_uses_employees_own_anniversary_cycle(self):
        """Revised 2026-09-01: AL is anniversary-cycle in production. The
        allocation must come from each employee's own current cycle, not a
        bare year= match against the report's calendar year — an employee
        whose cycle spans two calendar years would otherwise silently miss
        their real row (or match a stale one) instead. This was found
        reviewing a real employee — see docs/LEAVE_POLICY_IMPLEMENTATION.md.
        """
        al_policy = self.policies["AL"]
        al_policy.cycle = LeavePolicy.Cycle.ANNIVERSARY
        al_policy.save(update_fields=["cycle"])

        # Dave joined 2024-11-01, so his cycle containing the report's end
        # date (2026-03-31) is [2025-11-01, 2026-11-01) — year=2025, NOT
        # 2026 (the report's own calendar year). The old bare year=2026
        # filter would have missed this row entirely.
        dave = _user(
            "dave@example.com", employee_id="005",
            department=self.ops, joining_date=date(2024, 11, 1),
        )
        bal = get_or_create_balance(dave, al_policy, date(2026, 3, 31))
        self.assertEqual(bal.year, 2025)
        bal.opening = Decimal("7.00")
        bal.carried_forward = Decimal("2.00")
        bal.save(update_fields=["opening", "carried_forward"])

        rows = build_shift_summary(
            ShiftSummaryFilters(date(2026, 1, 1), date(2026, 3, 31), self.ops.pk)
        )
        dave_row = self._row(rows, "005")
        # accrued is live (0 worked shifts in this test) -> 7 opening + 0 + 2 carried = 9
        self.assertEqual(dave_row["al_allocated"], Decimal("9"))

    def test_al_column_counts_report_window_cycle_columns_count_cycle(self):
        """Revised 2026-10-30: the AL column is AL taken inside the report's
        window, like every other leave column. Cycle-to-date usage moved to
        its own `al_cycle_used` / `al_cycle_balance` columns, which share
        `al_allocated`'s anniversary-cycle basis."""
        al_policy = self.policies["AL"]
        al_policy.cycle = LeavePolicy.Cycle.ANNIVERSARY
        al_policy.save(update_fields=["cycle"])

        erin = _user(
            "erin@example.com", employee_id="006",
            department=self.ops, joining_date=date(2020, 2, 1),
        )
        # Erin's cycle containing 2026-03-31 is [2026-02-01, 2027-02-01).
        # One AL shift lands in the prior (closed) cycle, one in the
        # current (open) one — both fall inside the report's window.
        Shift.objects.create(employee=erin, date=date(2026, 1, 20), shift_type=self.leave_types["AL"])
        Shift.objects.create(employee=erin, date=date(2026, 2, 10), shift_type=self.leave_types["AL"])

        rows = build_shift_summary(
            ShiftSummaryFilters(date(2026, 1, 1), date(2026, 3, 31), self.ops.pk)
        )
        erin_row = self._row(rows, "006")

        self.assertEqual(erin_row["AL"], 2)
        self.assertEqual(erin_row["total_leave_taken"], 2)
        self.assertEqual(erin_row["al_cycle_used"], Decimal("1"))
        self.assertEqual(
            erin_row["al_cycle_balance"],
            erin_row["al_allocated"] - erin_row["al_cycle_used"],
        )

    def test_al_taken_before_window_in_same_cycle_is_not_counted(self):
        """The reported case (employee 4132): cycle opened months before the
        window, AL taken both earlier in the cycle and inside the window.
        The AL column must show only the window's days."""
        al_policy = self.policies["AL"]
        al_policy.cycle = LeavePolicy.Cycle.ANNIVERSARY
        al_policy.save(update_fields=["cycle"])

        fay = _user(
            "fay@example.com", employee_id="007",
            department=self.ops, joining_date=date(2022, 1, 5),
        )
        # Cycle [2026-01-05, 2027-01-05); window Mar 1 – Mar 31.
        for d in (date(2026, 2, 9), date(2026, 2, 10), date(2026, 3, 16), date(2026, 3, 17)):
            Shift.objects.create(employee=fay, date=d, shift_type=self.leave_types["AL"])

        rows = build_shift_summary(
            ShiftSummaryFilters(date(2026, 3, 1), date(2026, 3, 31), self.ops.pk)
        )
        fay_row = self._row(rows, "007")

        self.assertEqual(fay_row["AL"], 2)
        self.assertEqual(fay_row["total_leave_taken"], 2)
        self.assertEqual(fay_row["al_cycle_used"], Decimal("4"))
        self.assertEqual(
            fay_row["leave_remaining"],
            fay_row["al_cycle_balance"] + fay_row["cl_allocated"] + fay_row["sl_allocated"],
        )

    def test_cross_year_ranges_are_rejected(self):
        with self.assertRaises(CrossYearSummaryError):
            build_shift_summary(
                ShiftSummaryFilters(date(2025, 12, 31), date(2026, 1, 1))
            )

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_shift_summary_view_renders_for_superuser(self):
        admin = _user("admin@example.com", is_staff=True, is_superuser=True)
        self.client.force_login(admin)

        response = self.client.get(reverse("leave_shift_summary"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shift summary")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ShiftSummaryExportTests(_ShiftSummaryFixture):
    """`?export=xlsx` on the shift summary — the full filtered set, a title
    row with the date range, then the on-screen columns plus Department."""

    def _export(self, user, **params):
        from io import BytesIO

        from openpyxl import load_workbook

        self.client.force_login(user)
        params = {"start_date": "2026-01-01", "end_date": "2026-06-30", "export": "xlsx", **params}
        response = self.client.get(reverse("leave_shift_summary"), params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        sheet = load_workbook(BytesIO(response.content)).active
        return response, list(sheet.iter_rows(values_only=True))

    def test_export_has_title_row_header_and_every_row(self):
        admin = _user("admin@example.com", is_staff=True, is_superuser=True)

        # A 1-row page size must not truncate the export.
        response, rows = self._export(admin, per_page="1", page="2")

        self.assertIn("shift-summary-all-2026-01-01-to-2026-06-30.xlsx",
                      response["Content-Disposition"])
        self.assertEqual(rows[0][0], "Shift summary · All departments · 2026-01-01 to 2026-06-30")
        header = rows[1]
        self.assertEqual(header[:4], ("Employee ID", "Name", "Department", "Team"))
        self.assertEqual(header[-1], "Remaining")
        body = {r[0]: r for r in rows[2:] if r[0] in {"001", "002", "003"}}
        self.assertEqual(set(body), {"001", "002", "003"})
        self.assertEqual(body["001"][2:4], ("Operations", "Support"))
        self.assertEqual(body["003"][2], "HR")
        # Day counts stay numeric so they can be summed in Excel.
        self.assertIsInstance(body["001"][header.index("Days worked")], int)

    def test_export_respects_department_filter_and_names_it(self):
        admin = _user("admin@example.com", is_staff=True, is_superuser=True)

        response, rows = self._export(admin, department=str(self.ops.pk))

        self.assertIn("shift-summary-operations-", response["Content-Disposition"])
        self.assertTrue(rows[0][0].startswith("Shift summary · Operations · "))
        self.assertEqual({r[0] for r in rows[2:]}, {"001", "002"})

    def test_export_is_scoped_like_the_page_for_a_plain_employee(self):
        _, rows = self._export(self.alice, department=str(self.hr.pk))

        self.assertEqual([r[0] for r in rows[2:]], ["001"])

    def test_cross_year_export_renders_errors_instead_of_downloading(self):
        admin = _user("admin@example.com", is_staff=True, is_superuser=True)
        self.client.force_login(admin)

        response = self.client.get(reverse("leave_shift_summary"), {
            "start_date": "2025-12-01", "end_date": "2026-01-31", "export": "xlsx",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        self.assertContains(response, "single calendar year")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ShiftSummaryDeferredLoadTests(_ShiftSummaryFixture):
    """The page renders a shell; the table arrives via an htmx request that
    paginates employees *before* building rows (ShiftSummaryView)."""

    PARAMS = {"start_date": "2026-01-01", "end_date": "2026-06-30"}
    RESULTS = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "shift-summary-results"}

    def setUp(self):
        super().setUp()
        self.admin = _user("admin@example.com", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_full_get_renders_shell_with_deferred_loader_and_no_rows(self):
        response = self.client.get(reverse("leave_shift_summary"), self.PARAMS)

        self.assertContains(response, 'hx-trigger="load"')
        self.assertContains(response, "Building summary")
        self.assertNotContains(response, "alice@example.com")
        self.assertNotContains(response, ">001<")

    def test_results_request_returns_fragment_with_rows(self):
        response = self.client.get(reverse("leave_shift_summary"), self.PARAMS, **self.RESULTS)

        self.assertTemplateUsed(response, "leave/_shift_summary_results.html")
        self.assertNotContains(response, "<html")
        self.assertContains(response, ">001<")
        self.assertContains(response, "3 employees")

    def test_history_restore_gets_the_full_page(self):
        response = self.client.get(
            reverse("leave_shift_summary"), self.PARAMS,
            HTTP_HX_HISTORY_RESTORE_REQUEST="true", **self.RESULTS,
        )

        self.assertTemplateUsed(response, "leave/shift_summary.html")
        self.assertTemplateNotUsed(response, "leave/_shift_summary_results.html")

    def test_bad_range_shows_error_without_a_loader(self):
        response = self.client.get(reverse("leave_shift_summary"), {
            "start_date": "2025-12-01", "end_date": "2026-01-31",
        })

        self.assertContains(response, "single calendar year")
        self.assertNotContains(response, 'hx-trigger="load"')

    def test_rows_are_built_only_for_the_current_page(self):
        from unittest import mock

        import payroll.reports as reports

        for n in range(30):
            _user(f"filler{n:02d}@example.com", employee_id=str(100 + n),
                  department=self.ops, joining_date=date(2025, 1, 1))

        with mock.patch.object(reports, "_al_ledger_snapshot",
                               wraps=reports._al_ledger_snapshot) as snapshot:
            response = self.client.get(
                reverse("leave_shift_summary"), {**self.PARAMS, "page": "2"}, **self.RESULTS,
            )

        # 33 employees (+ admin has no employee_id) → page 2 of 25 holds 8.
        self.assertEqual(len(response.context["rows"]), 8)
        self.assertEqual(len(snapshot.call_args.args[0]), 8)
        self.assertEqual(response.context["paginator"].count, 33)
        self.assertEqual(response.context["rows"][0]["employee_id"], "122")

    def test_page_rows_match_the_full_build(self):
        filters = ShiftSummaryFilters(date(2026, 1, 1), date(2026, 6, 30))
        everyone = shift_summary_employees(filters)

        self.assertEqual(
            build_shift_summary_rows(everyone[1:], filters),
            build_shift_summary(filters)[1:],
        )
