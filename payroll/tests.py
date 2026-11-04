from __future__ import annotations

import io
import json
from datetime import date, time, timedelta
from decimal import Decimal

import openpyxl

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AccessLevel, CostCenter, Department, Designation, EmploymentType, Grade, JobLocation, SubProject, Team, WorkLocation
from accounts.permissions import payroll_visible_employee_pks, visible_employee_pks
from dynamic_menu.models import ProtectedView
from employees.models import JobInfo
from employees.sections import SECTIONS
from payroll.models import (
    Bank, BankBranch, EmployeeBankAccount, MealDeductionTier, OvertimeEntry, OvertimeType,
    PayComponent, PayRate, PayrollControlCheck, PayrollPeriod, PayrollRun, PayrollRunEmployeeNote,
    PayrollSettings, PayslipCounter, SalaryStructure,
)
from payroll.periods import period_bounds, period_for_date
from payroll.rates import resolve_rate
from payroll.reports import PayrollFilters, build_payroll_report
from payroll.forms import OvertimeEntryForm
from payroll.services import OvertimeWindow, next_payslip_number
from payroll.upload import (
    ParsedRow, apply_salary_upload, apply_upload, build_overtime_xlsx_template,
    build_salary_xlsx_template, parse_salary_upload, parse_upload,
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


class PayrollReportTests(TestCase):
    def setUp(self):
        self.ops = Department.objects.create(name="Operations")
        self.other_department = Department.objects.create(name="Finance")
        self.team = Team.objects.create(name="Core Ops", department=self.ops)
        self.other_team = Team.objects.create(name="Other Team", department=self.other_department)
        self.designation = Designation.objects.create(name="Analyst")
        # The payroll report renders JobLocation.code (not .name), so the
        # fixture must set a code for the assertion below to see "MHK".
        self.job_location = JobLocation.objects.create(name="Mohakhali", code="MHK")
        self.work_location = WorkLocation.objects.create(name="WFH")
        self.office_location = WorkLocation.objects.create(name="Work From Office")

        self.employee = _user(
            "alice@example.com",
            employee_id="001",
            department=self.ops,
            team=self.team,
            designation=self.designation,
            job_location=self.job_location,
            work_location=self.work_location,
            joining_date=date(2026, 1, 15),
        )
        JobInfo.objects.create(
            employee=self.employee,
            resource_status=JobInfo.ResourceStatus.LIVE,
            billing_status=JobInfo.BillingStatus.BILLABLE,
        )

        self.employee_without_job_info = _user(
            "bob@example.com",
            employee_id="002",
            department=self.ops,
            team=self.team,
        )
        self.other_employee = _user(
            "carol@example.com",
            employee_id="003",
            department=self.other_department,
            team=self.other_team,
        )

        self.shift_types = {
            "AL": ShiftType.objects.create(name="Annual Leave (AL)", category=ShiftType.Category.LEAVE),
            "CL": ShiftType.objects.create(name="Casual Leave", category=ShiftType.Category.LEAVE),
            "SL": ShiftType.objects.create(name="SL", category=ShiftType.Category.LEAVE),
            "MTL": ShiftType.objects.create(name="Maternity Leave", category=ShiftType.Category.LEAVE),
            "CPL": ShiftType.objects.create(name="Compensatory Leave", category=ShiftType.Category.LEAVE),
            "HDL": ShiftType.objects.create(name="Half Day Leave", category=ShiftType.Category.LEAVE),
            "LWP": ShiftType.objects.create(name="Leave Without Pay", category=ShiftType.Category.LEAVE),
            # Real canonical codes with actual midnight-crossing times — the
            # payroll report's night/custom/transport columns key off exact
            # shift_type.name equality and real times (payroll_inputs.
            # classify_night_shifts), not the old fuzzy alias matching.
            "NIG": ShiftType.objects.create(
                name="NIG", category=ShiftType.Category.WORK,
                shift_start_time=time(22, 0), shift_end_time=time(7, 0),
            ),
            "CUST": ShiftType.objects.create(
                name="CUST", category=ShiftType.Category.WORK,
                shift_start_time=time(19, 0), shift_end_time=time(4, 0),
            ),
            "FL": ShiftType.objects.create(name="GH", category=ShiftType.Category.HOLIDAY),
            "HOLIDAY": ShiftType.objects.create(name="Holiday", category=ShiftType.Category.HOLIDAY),
            "MOR": ShiftType.objects.create(name="Morning", category=ShiftType.Category.WORK),
        }

    def _report(self, **filters):
        return build_payroll_report(
            PayrollFilters(
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 31),
                **filters,
            )
        )

    def _row(self, rows, employee_id):
        return next(row for row in rows if row["employee_id"] == employee_id)

    def test_population_is_the_engines_not_currently_active(self):
        # October 2026: Support Info showed 1461 against HR's 1468 because it
        # listed is_active users — leavers deactivated after their LWD were
        # dropped, joiners starting after the period were listed.
        _user("leaver@example.com", employee_id="010", is_active=False,
              last_working_date=date(2026, 5, 20))
        _user("left-earlier@example.com", employee_id="011", is_active=False,
              last_working_date=date(2026, 4, 30))
        _user("joins-later@example.com", employee_id="012", joining_date=date(2026, 6, 1))
        service_account = _user("service-account@example.com", employee_id=None)

        rows = self._report()["rows"]
        ids = {row["employee_id"] for row in rows}

        self.assertIn("010", ids)
        self.assertNotIn("011", ids)
        self.assertNotIn("012", ids)
        self.assertNotIn(service_account.pk, {row["employee_pk"] for row in rows})
        self.assertTrue({"001", "002", "003"} <= ids)

    def test_report_counts_shift_and_leave_buckets_without_hour_fields(self):
        assignments = (
            ("AL", 1),
            ("CL", 2),
            ("SL", 3),
            ("MTL", 4),
            ("CPL", 5),
            ("HDL", 6),
            ("LWP", 7),
            ("NIG", 8),
            ("CUST", 9),
            ("FL", 10),
            ("HOLIDAY", 11),
            ("MOR", 12),
        )
        for key, day in assignments:
            # The employee's contract work_location is WFH (set in setUp) —
            # override to a real office on the CUST day so the Transport
            # column has something to count (WFH never earns transport).
            extra = {"work_location": self.office_location} if key == "CUST" else {}
            Shift.objects.create(
                employee=self.employee,
                date=date(2026, 5, day),
                shift_type=self.shift_types[key],
                **extra,
            )

        report = self._report()

        self.assertEqual(report["holidays"], [])
        alice = self._row(report["rows"], "001")
        self.assertEqual(alice["counts"]["AL"], 1)
        self.assertEqual(alice["counts"]["CL"], 1)
        self.assertEqual(alice["counts"]["SL"], 1)
        self.assertEqual(alice["counts"]["MTL"], 1)
        self.assertEqual(alice["counts"]["CPL"], 1)
        self.assertEqual(alice["counts"]["HDL"], 0.5)
        self.assertEqual(alice["counts"]["LWP"], 1)
        self.assertEqual(alice["counts"]["NIGHT_REGULAR"], 1)
        self.assertEqual(alice["counts"]["NIGHT_CUSTOM"], 1)
        self.assertEqual(alice["counts"]["NIGHT_DEDICATED"], 0)
        self.assertEqual(alice["counts"]["TRANSPORT"], 1)
        # No OvertimeEntry rows at all in this fixture -> no OT columns.
        self.assertEqual(report["ot_types"], [])
        self.assertEqual(alice["ot_by_type"], [])
        self.assertEqual(alice["counts"]["FL"], 2)
        self.assertNotIn("holiday_hours", alice)
        self.assertNotIn("eid_coverage_hrs", alice)
        self.assertNotIn("eid_coverage_days", alice)
        self.assertNotIn("regular_ot_hrs", alice)

    def test_report_ot_columns_are_dynamic_per_type_present(self):
        # One column per OvertimeType actually present in this report's
        # scope/window — not a fixed catalog, and not a single summed total.
        component = PayComponent.objects.create(
            code="OT_DYN", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        regular_ot = OvertimeType.objects.create(
            code="regular_ot_dyn", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR, component=component,
        )
        gh_ot = OvertimeType.objects.create(
            code="gh_ot_dyn", name="GH OT", basis=OvertimeType.Basis.PER_HOUR, component=component,
        )
        period = PayrollPeriod.for_month(2026, 5)
        # Only APPROVED overtime is paid/reported (the review chain,
        # 2026-10-17 — the model default is now PENDING).
        approved = OvertimeEntry.Status.APPROVED
        OvertimeEntry.objects.create(
            employee=self.employee, period=period, work_date=date(2026, 5, 10),
            overtime_type=regular_ot, seconds=2 * 3600, status=approved,
        )
        OvertimeEntry.objects.create(
            employee=self.employee, period=period, work_date=date(2026, 5, 11),
            overtime_type=gh_ot, seconds=3 * 3600, status=approved,
        )
        OvertimeEntry.objects.create(
            employee=self.other_employee, period=period, work_date=date(2026, 5, 12),
            overtime_type=regular_ot, seconds=1 * 3600, status=approved,
        )

        report = self._report()

        # Alphabetical: "GH OT" before "Regular OT".
        self.assertEqual([ot.name for ot in report["ot_types"]], ["GH OT", "Regular OT"])

        alice = self._row(report["rows"], "001")
        self.assertEqual(alice["ot_by_type"], [Decimal("3.00"), Decimal("2.00")])

        carol = self._row(report["rows"], "003")
        self.assertEqual(carol["ot_by_type"], [Decimal("0.00"), Decimal("1.00")])

    def test_report_keeps_employee_metadata_and_handles_missing_job_info(self):
        report = self._report(department_id=self.ops.pk, team_id=self.team.pk)

        alice = self._row(report["rows"], "001")
        bob = self._row(report["rows"], "002")

        self.assertEqual(alice["designation"], "Analyst")
        self.assertEqual(alice["department"], "Operations")
        self.assertEqual(alice["team"], "Core Ops")
        self.assertEqual(alice["job_location"], "MHK")
        self.assertEqual(alice["work_location"], "WFH")
        self.assertEqual(alice["resource_status"], "Live")
        self.assertEqual(alice["billing_status"], "Billable")

        self.assertEqual(bob["resource_status"], "")
        self.assertEqual(bob["billing_status"], "")
        self.assertEqual(bob["counts"]["AL"], 0)
        self.assertEqual([row["employee_id"] for row in report["rows"]], ["001", "002"])

    def test_report_rejects_ranges_longer_than_31_days(self):
        with self.assertRaisesMessage(
            ValueError,
            "Date range cannot exceed 31 days. Please select a valid payroll month.",
        ):
            build_payroll_report(
                PayrollFilters(
                    start_date=date(2026, 5, 1),
                    end_date=date(2026, 6, 2),
                )
            )


class PayrollPeriodMathTests(TestCase):
    def test_period_bounds_covers_16th_to_15th(self):
        self.assertEqual(period_bounds(2026, 7), (date(2026, 6, 16), date(2026, 7, 15)))

    def test_period_bounds_handles_january_rollover(self):
        self.assertEqual(period_bounds(2026, 1), (date(2025, 12, 16), date(2026, 1, 15)))

    def test_period_for_date_is_the_inverse(self):
        self.assertEqual(period_for_date(date(2026, 7, 15)), (2026, 7))
        self.assertEqual(period_for_date(date(2026, 7, 16)), (2026, 8))
        self.assertEqual(period_for_date(date(2026, 12, 16)), (2027, 1))

    def test_for_month_creates_and_reuses_the_same_row(self):
        first = PayrollPeriod.for_month(2026, 7)
        second = PayrollPeriod.for_month(2026, 7)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.start_date, date(2026, 6, 16))
        self.assertEqual(first.days_in_period, 30)

    def test_for_range_matching_a_standard_cycle_reuses_it(self):
        standard = PayrollPeriod.for_month(2026, 8)
        via_range = PayrollPeriod.for_range(date(2026, 7, 16), date(2026, 8, 15))
        self.assertEqual(standard.pk, via_range.pk)
        self.assertEqual(via_range.period_type, PayrollPeriod.PeriodType.STANDARD)

    def test_for_range_off_cycle_creates_a_custom_period(self):
        period = PayrollPeriod.for_range(date(2026, 8, 20), date(2026, 9, 8))
        self.assertEqual(period.period_type, PayrollPeriod.PeriodType.CUSTOM)
        self.assertEqual(period.days_in_period, 20)
        # A standard period can still exist for the same nominal month.
        standard = PayrollPeriod.for_month(2026, 9)
        self.assertNotEqual(standard.pk, period.pk)

    def test_for_range_rejects_end_before_start(self):
        with self.assertRaises(ValidationError):
            PayrollPeriod.for_range(date(2026, 8, 15), date(2026, 7, 16))

    def test_for_range_is_idempotent_for_the_same_custom_dates(self):
        first = PayrollPeriod.for_range(date(2026, 8, 20), date(2026, 9, 8))
        second = PayrollPeriod.for_range(date(2026, 8, 20), date(2026, 9, 8))
        self.assertEqual(first.pk, second.pk)


class PayrollRunCreateViewTests(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name="HR")
        Group.objects.get_or_create(name="Payroll")
        self.hr_user = _user("hr-runs@example.com", employee_id="900")
        # Creating a run requires can_create_payroll_run: "Payroll" AND "HR"
        # (the curated payroll-HR team), not "HR" alone — see
        # accounts/permissions.py::_is_payroll_hr.
        self.hr_user.groups.add(Group.objects.get(name="HR"), Group.objects.get(name="Payroll"))
        for view_name in ("payroll_run_list", "payroll_run_detail"):
            pv, _ = ProtectedView.objects.get_or_create(
                view_name=view_name, defaults={"registered_app": "payroll"},
            )
            pv.allowed_groups.set([Group.objects.get(name="HR")])
        self.client.force_login(self.hr_user)

    def test_post_with_a_standard_range_creates_a_standard_run(self):
        resp = self.client.post("/payroll/runs/", {"start_date": "2026-07-16", "end_date": "2026-08-15"})
        run = PayrollRun.objects.get()
        self.assertRedirects(resp, f"/payroll/runs/{run.pk}/")
        self.assertEqual(run.period.period_type, PayrollPeriod.PeriodType.STANDARD)
        self.assertEqual(run.period.start_date, date(2026, 7, 16))
        self.assertEqual(run.period.end_date, date(2026, 8, 15))

    def test_post_with_an_off_cycle_range_creates_a_custom_run(self):
        resp = self.client.post("/payroll/runs/", {"start_date": "2026-08-20", "end_date": "2026-09-08"})
        run = PayrollRun.objects.get()
        self.assertRedirects(resp, f"/payroll/runs/{run.pk}/")
        self.assertEqual(run.period.period_type, PayrollPeriod.PeriodType.CUSTOM)
        self.assertEqual(run.period.days_in_period, 20)

    def test_post_with_end_before_start_is_rejected_without_creating_a_run(self):
        resp = self.client.post("/payroll/runs/", {"start_date": "2026-08-15", "end_date": "2026-07-16"})
        self.assertRedirects(resp, "/payroll/runs/")
        self.assertFalse(PayrollRun.objects.exists())


class PayrollRunListStatsTests(TestCase):
    """The runs list shows non-monetary stats (night-shift/OT breakdowns,
    pending overtime, open control checks) instead of Gross/Net."""

    def setUp(self):
        Group.objects.get_or_create(name="HR")
        self.hr_user = _user("hr-runlist@example.com", employee_id="910")
        self.hr_user.groups.add(Group.objects.get(name="HR"))
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_run_list", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([Group.objects.get(name="HR")])
        self.client.force_login(self.hr_user)

        self.employee = _user("runlist-emp@example.com", employee_id="1050")
        self.period = PayrollPeriod.for_month(2026, 7)
        self.run = PayrollRun.objects.create(
            period=self.period, status=PayrollRun.Status.CALCULATED, employee_count=1,
            calculated_at=timezone.now(),
            night_regular_total=3, night_custom_total=1, night_dedicated_total=0,
            transport_total=2, ot_seconds_by_type_total={"Regular OT": 18000, "GH OT": 3600},
        )
        PayrollControlCheck.objects.create(
            run=self.run, check_code="NEGATIVE_NET_PAY", severity=PayrollControlCheck.Severity.BLOCKING,
        )
        PayrollControlCheck.objects.create(
            run=self.run, check_code="PENDING_OVERTIME", severity=PayrollControlCheck.Severity.WARNING,
        )
        component = PayComponent.objects.create(
            code="OT_ALLOWANCE_RL", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        ot_type = OvertimeType.objects.create(
            code="regular_ot_rl", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR, component=component,
        )
        OvertimeEntry.objects.create(
            employee=self.employee, period=self.period, work_date=self.period.start_date,
            overtime_type=ot_type, seconds=3600, status=OvertimeEntry.Status.PENDING,
        )

    def test_no_monetary_figures_in_the_response(self):
        resp = self.client.get("/payroll/runs/")
        self.assertNotContains(resp, "Gross")
        self.assertContains(resp, "Night shifts")
        self.assertContains(resp, "Overtime")

    def test_night_and_overtime_breakdowns_are_shown(self):
        resp = self.client.get("/payroll/runs/")
        run = resp.context["runs"][0]
        self.assertEqual(run.night_total, 4)
        self.assertEqual(run.night_regular_total, 3)
        self.assertEqual(run.night_custom_total, 1)
        self.assertEqual(run.ot_hours_total, 6.0)
        self.assertEqual(run.ot_hours_by_type, {"Regular OT": 5.0, "GH OT": 1.0})

    def test_pending_overtime_and_open_checks_are_counted(self):
        resp = self.client.get("/payroll/runs/")
        run = resp.context["runs"][0]
        self.assertEqual(run.pending_ot_count, 1)
        self.assertEqual(run.open_blocking_check_count, 1)
        self.assertEqual(run.open_warning_check_count, 1)

    def test_a_never_calculated_run_shows_dashes_not_errors(self):
        PayrollRun.objects.create(period=PayrollPeriod.for_month(2026, 8), status=PayrollRun.Status.DRAFT)
        resp = self.client.get("/payroll/runs/")
        self.assertEqual(resp.status_code, 200)


class CompensationMasterTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.employee = _user("dana@example.com", employee_id="101", department=self.department)
        self.other_employee = _user("evan@example.com", employee_id="102", department=self.department)

    def test_grade_employment_type_cost_center_sub_project_assign_to_user(self):
        grade = Grade.objects.create(name="Senior Executive", rank=30)
        emp_type = EmploymentType.objects.create(name="Permanent")
        cost_center = CostCenter.objects.create(name="AdOps CC", code="CC-100", gl_code="51001")
        sub_project = SubProject.objects.create(name="Simpli.fi")

        self.employee.grade = grade
        self.employee.employment_type = emp_type
        self.employee.save(update_fields=["grade", "employment_type"])

        job_info = JobInfo.objects.create(
            employee=self.employee, cost_center=cost_center, sub_project=sub_project,
        )

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.grade, grade)
        self.assertEqual(self.employee.employment_type, emp_type)
        self.assertEqual(job_info.cost_center, cost_center)
        self.assertEqual(job_info.sub_project, sub_project)

    def test_job_info_resource_status_accepts_trainee(self):
        job_info = JobInfo.objects.create(
            employee=self.employee, resource_status=JobInfo.ResourceStatus.TRAINEE,
        )
        self.assertEqual(job_info.resource_status, JobInfo.ResourceStatus.TRAINEE)

    def test_payroll_settings_load_is_a_singleton(self):
        first = PayrollSettings.load()
        first.notice_pay_basis = 45
        first.save()
        second = PayrollSettings.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.notice_pay_basis, 45)
        self.assertEqual(PayrollSettings.objects.count(), 1)

    def test_payslip_counter_bump_is_sequential(self):
        self.assertEqual(next_payslip_number(), "PS-1")
        self.assertEqual(next_payslip_number(), "PS-2")
        self.assertEqual(PayslipCounter.objects.get(pk=1).last_value, 2)

    def test_salary_structure_rejects_a_second_current_approved_structure(self):
        SalaryStructure.objects.create(
            employee=self.employee,
            effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("50000.00"),
            status=SalaryStructure.Status.APPROVED,
        )
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                SalaryStructure.objects.create(
                    employee=self.employee,
                    effective_from=date(2026, 6, 1),
                    reason=SalaryStructure.Reason.ANNUAL_INCREMENT,
                    gross_monthly=Decimal("55000.00"),
                    status=SalaryStructure.Status.APPROVED,
                )

    def test_salary_structure_supersede_then_approve_succeeds(self):
        current = SalaryStructure.objects.create(
            employee=self.employee,
            effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("50000.00"),
            status=SalaryStructure.Status.APPROVED,
        )
        current.status = SalaryStructure.Status.SUPERSEDED
        current.effective_to = date(2026, 5, 31)
        current.save()

        new = SalaryStructure.objects.create(
            employee=self.employee,
            effective_from=date(2026, 6, 1),
            reason=SalaryStructure.Reason.ANNUAL_INCREMENT,
            gross_monthly=Decimal("55000.00"),
            status=SalaryStructure.Status.APPROVED,
        )
        self.assertIsNone(new.effective_to)

    def test_different_employees_can_each_have_a_current_structure(self):
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("50000.00"),
            status=SalaryStructure.Status.APPROVED,
        )
        SalaryStructure.objects.create(
            employee=self.other_employee, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("60000.00"),
            status=SalaryStructure.Status.APPROVED,
        )
        self.assertEqual(SalaryStructure.objects.filter(status=SalaryStructure.Status.APPROVED).count(), 2)


class EmployeeBankAccountTests(TestCase):
    def setUp(self):
        self.employee = _user("finn@example.com", employee_id="201")
        self.bank = Bank.objects.create(name="Bank Asia PLC", short_code="BAL")
        self.branch = BankBranch.objects.create(bank=self.bank, name="Gulshan", routing_number="123456789")

    def test_bank_account_registered_as_a_section_spec(self):
        self.assertIn("bank-account", SECTIONS)
        spec = SECTIONS["bank-account"]
        self.assertIs(spec.model, EmployeeBankAccount)
        self.assertFalse(spec.is_singleton)

    def test_branch_routing_number_is_snapshotted_onto_the_account(self):
        account = EmployeeBankAccount.objects.create(
            employee=self.employee, bank=self.bank, branch=self.branch,
            account_type=EmployeeBankAccount.AccountType.SAVINGS,
            account_name="Finn Test", account_number="0011223344",
        )
        self.assertEqual(account.routing_number, "123456789")

    def test_mfs_account_does_not_require_a_bank(self):
        account = EmployeeBankAccount.objects.create(
            employee=self.employee, account_type=EmployeeBankAccount.AccountType.MFS_BKASH,
            account_name="Finn Test", account_number="01700000000",
        )
        self.assertIsNone(account.bank)

    def test_only_one_primary_account_per_employee(self):
        EmployeeBankAccount.objects.create(
            employee=self.employee, account_type=EmployeeBankAccount.AccountType.SAVINGS,
            account_name="Finn Test", account_number="0011223344", is_primary=True,
        )
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                EmployeeBankAccount.objects.create(
                    employee=self.employee, account_type=EmployeeBankAccount.AccountType.MFS_NAGAD,
                    account_name="Finn Test", account_number="01700000000", is_primary=True,
                )


class PayRateResolutionTests(TestCase):
    """Fixtures use the real rate figures confirmed 2026-08-20 from
    Master Sheet_July_2026.xlsx (PAYROLL_PROCESS.md §12 Q37-39)."""

    def setUp(self):
        self.component = PayComponent.objects.create(
            code="NIGHT_ALLOWANCE", name="Night allowance", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.transport_component = PayComponent.objects.create(
            code="SPECIAL_SHIFT_TRANSPORT", name="Special shift transport", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.software = Department.objects.create(name="Software Development")
        self.adops = Department.objects.create(name="Digital Advertising")

        # Night Regular — salary-banded, Software Development only.
        PayRate.objects.create(
            rate_type=PayRate.RateType.NIGHT_REGULAR, component=self.component,
            department=self.software, max_gross=Decimal("40000"),
            basis=PayRate.Basis.PER_NIGHT, amount=Decimal("350"), effective_from=date(2026, 1, 1),
        )
        PayRate.objects.create(
            rate_type=PayRate.RateType.NIGHT_REGULAR, component=self.component,
            department=self.software, min_gross=Decimal("40000"),
            basis=PayRate.Basis.PER_NIGHT, amount=Decimal("450"), effective_from=date(2026, 1, 1),
        )
        # Night Quarterly/Special — flat 500, no salary band, AdOps.
        PayRate.objects.create(
            rate_type=PayRate.RateType.NIGHT_QUARTERLY, component=self.component,
            department=self.adops, basis=PayRate.Basis.PER_NIGHT, amount=Decimal("500"),
            effective_from=date(2026, 1, 1),
        )
        # Special shift transport — flat 400, AdOps.
        PayRate.objects.create(
            rate_type=PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, component=self.transport_component,
            department=self.adops, basis=PayRate.Basis.PER_DAY, amount=Decimal("400"),
            effective_from=date(2026, 1, 1),
        )

    def test_night_regular_below_band_resolves_350(self):
        rate = resolve_rate(
            rate_type=PayRate.RateType.NIGHT_REGULAR, on_date=date(2026, 7, 20),
            department=self.software, gross=Decimal("30000"),
        )
        self.assertEqual(rate.amount, Decimal("350"))

    def test_night_regular_at_and_above_band_resolves_450(self):
        rate = resolve_rate(
            rate_type=PayRate.RateType.NIGHT_REGULAR, on_date=date(2026, 7, 20),
            department=self.software, gross=Decimal("40000"),
        )
        self.assertEqual(rate.amount, Decimal("450"))
        rate_above = resolve_rate(
            rate_type=PayRate.RateType.NIGHT_REGULAR, on_date=date(2026, 7, 20),
            department=self.software, gross=Decimal("55000"),
        )
        self.assertEqual(rate_above.amount, Decimal("450"))

    def test_night_quarterly_is_flat_regardless_of_gross(self):
        low = resolve_rate(
            rate_type=PayRate.RateType.NIGHT_QUARTERLY, on_date=date(2026, 7, 20),
            department=self.adops, gross=Decimal("20000"),
        )
        high = resolve_rate(
            rate_type=PayRate.RateType.NIGHT_QUARTERLY, on_date=date(2026, 7, 20),
            department=self.adops, gross=Decimal("90000"),
        )
        self.assertEqual(low.amount, Decimal("500"))
        self.assertEqual(high.amount, Decimal("500"))

    def test_special_shift_transport_resolves_for_adops(self):
        rate = resolve_rate(
            rate_type=PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, on_date=date(2026, 7, 20),
            department=self.adops,
        )
        self.assertEqual(rate.amount, Decimal("400"))

    def test_no_match_returns_none(self):
        creative = Department.objects.create(name="Creative Development")
        rate = resolve_rate(
            rate_type=PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, on_date=date(2026, 7, 20),
            department=creative,
        )
        self.assertIsNone(rate)

    def test_pay_rate_overtime_requires_overtime_type(self):
        with self.assertRaises(ValidationError):
            PayRate(
                rate_type=PayRate.RateType.OVERTIME, component=self.component,
                basis=PayRate.Basis.PER_HOUR, amount=Decimal("100"), effective_from=date(2026, 1, 1),
            ).clean()

    def _gross_basis_rate(self, amount, rate_type=PayRate.RateType.OVERTIME):
        """GROSS_30TH_PER_DAY reads `amount` as a multiplier, not taka — added
        2026-09-09 for Software/Creative Development (PAYROLL_PROCESS.md Q47)."""
        ot_type = OvertimeType.objects.create(
            code=f"gh_ot_{amount}_{rate_type}", name="GH OT",
            basis=OvertimeType.Basis.PER_HOUR, component=self.component,
        )
        return PayRate(
            rate_type=rate_type,
            overtime_type=ot_type if rate_type == PayRate.RateType.OVERTIME else None,
            component=self.component, basis=PayRate.Basis.GROSS_30TH_PER_DAY,
            amount=amount, effective_from=date(2026, 1, 1),
        )

    def test_gross_basis_accepts_a_multiplier(self):
        self._gross_basis_rate(Decimal("1.5")).clean()  # must not raise

    def test_gross_basis_rejects_a_taka_figure_pasted_in_by_habit(self):
        with self.assertRaises(ValidationError) as ctx:
            self._gross_basis_rate(Decimal("350")).clean()
        self.assertIn("amount", ctx.exception.error_dict)

    def test_gross_basis_rejects_non_overtime_rate_types(self):
        with self.assertRaises(ValidationError) as ctx:
            self._gross_basis_rate(Decimal("1.5"), rate_type=PayRate.RateType.NIGHT_REGULAR).clean()
        self.assertIn("basis", ctx.exception.error_dict)


class MealDeductionTierTests(TestCase):
    def test_rejects_max_not_greater_than_min(self):
        tier = MealDeductionTier(min_gross=Decimal("25000"), max_gross=Decimal("25000"), amount_per_meal=Decimal("30"))
        with self.assertRaises(ValidationError):
            tier.clean()

    def test_confirmed_2026_08_11_tiers_are_representable(self):
        MealDeductionTier.objects.create(min_gross=0, max_gross=25000, amount_per_meal=20)
        MealDeductionTier.objects.create(min_gross=25000, max_gross=50000, amount_per_meal=30)
        MealDeductionTier.objects.create(min_gross=50000, amount_per_meal=50)
        self.assertEqual(MealDeductionTier.objects.count(), 3)

    def test_boundaries_resolve_to_the_lower_tier(self):
        # "≤25,000 → 20; 25,000–50,000 → 30; >50,000 → 50" — the upper
        # bound is inclusive, so a gross exactly on a boundary gets the
        # cheaper rate.
        from payroll.engine import _resolve_meal_tier

        MealDeductionTier.objects.create(min_gross=0, max_gross=25000, amount_per_meal=20)
        MealDeductionTier.objects.create(min_gross=25000, max_gross=50000, amount_per_meal=30)
        MealDeductionTier.objects.create(min_gross=50000, amount_per_meal=50)
        tiers = list(MealDeductionTier.objects.order_by("min_gross"))
        for gross, expected in [
            ("10000", 20), ("25000", 20), ("25000.01", 30),
            ("50000", 30), ("50000.01", 50), ("120000", 50),
        ]:
            with self.subTest(gross=gross):
                self.assertEqual(_resolve_meal_tier(Decimal(gross), tiers).amount_per_meal, expected)


class OvertimeUploadTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.manager = _user("mgr@example.com", employee_id="900", department=self.department)
        self.employee = _user("ot1@example.com", employee_id="1042", department=self.department)
        JobInfo.objects.create(employee=self.manager)
        self.other_department = Department.objects.create(name="Software Development")
        self.outside_employee = _user("outside@example.com", employee_id="2000", department=self.other_department)

        JobInfo.objects.create(employee=self.employee, reports_to=self.manager)
        JobInfo.objects.create(employee=self.outside_employee)

        self.period = PayrollPeriod.for_month(2026, 7)
        self.component = PayComponent.objects.create(
            code="OT", name="Overtime", kind=PayComponent.Kind.EARNING, entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", aliases=["ROT"],
            basis=OvertimeType.Basis.PER_HOUR, component=self.component,
        )

    def _csv(self, lines: list[str]) -> io.BytesIO:
        return io.BytesIO("\n".join(lines).encode("utf-8"))

    def test_parse_happy_path(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].seconds, 9000)
        self.assertEqual(parsed[0].overtime_type, self.ot_type)

    def test_parse_accepts_alias_case_insensitively(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,rot,01:00:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].overtime_type, self.ot_type)

    def test_parse_reads_worked_in_by_team_name(self):
        team = Team.objects.create(name="MARS", department=self.department)
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,MARS,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].worked_in, team)

    def test_parse_rejects_unknown_worked_in_team(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,Not A Real Team,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown team", errors[0].message)

    def test_apply_defaults_worked_in_to_the_employees_own_team_when_blank(self):
        team = Team.objects.create(name="MARS", department=self.department)
        self.employee.team = team
        self.employee.save()
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(errors, [])
        visible = visible_employee_pks(self.manager, request=None)
        apply_upload(self.manager, parsed, visible)
        self.assertEqual(OvertimeEntry.objects.get().worked_in, team)

    def test_apply_keeps_an_explicit_worked_in_over_the_employees_own_team(self):
        team = Team.objects.create(name="MARS", department=self.department)
        other_team = Team.objects.create(name="MARS QC", department=self.department)
        self.employee.team = team
        self.employee.save()
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,MARS QC,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(errors, [])
        visible = visible_employee_pks(self.manager, request=None)
        apply_upload(self.manager, parsed, visible)
        self.assertEqual(OvertimeEntry.objects.get().worked_in, other_team)

    def test_parse_accepts_any_working_date_and_derives_its_own_period(self):
        # No fixed upload-time period restriction (removed 2026-09-07) — a
        # row's PayrollPeriod is derived from its own work_date instead.
        # 2026-08-01 falls in the 16 Jul - 15 Aug period, not self.period
        # (16 Jun - 15 Jul).
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-08-01,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].period.start_date, date(2026, 7, 16))
        self.assertEqual(parsed[0].period.end_date, date(2026, 8, 15))
        self.assertNotEqual(parsed[0].period, self.period)

    def test_apply_writes_row_against_its_own_derived_period(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-08-01,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        visible = visible_employee_pks(self.manager, request=None)
        apply_upload(self.manager, parsed, visible)
        entry = OvertimeEntry.objects.get()
        self.assertEqual(entry.period.start_date, date(2026, 7, 16))
        self.assertEqual(entry.period.end_date, date(2026, 8, 15))

    def test_parse_rejects_unknown_employee_and_type(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,9999,,Regular OT,02:30:00",
            "2026-06-19,1042,,Made Up Type,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 2)

    def test_parse_flags_duplicate_within_file(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
            "2026-06-18,1042,,Regular OT,01:00:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(dups), 1)

    def test_apply_writes_and_reupload_upserts(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        visible = visible_employee_pks(self.manager, request=None)
        report = apply_upload(self.manager, parsed, visible)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(OvertimeEntry.objects.get().seconds, 9000)

        f2 = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,04:00:00",
        ])
        parsed2, _, _ = parse_upload(f2, "ot.csv", today=date(2026, 7, 1))
        report2 = apply_upload(self.manager, parsed2, visible)
        self.assertEqual(len(report2.updated), 1)
        self.assertEqual(OvertimeEntry.objects.count(), 1)
        self.assertEqual(OvertimeEntry.objects.get().seconds, 14400)

    def test_apply_skips_rows_outside_scope(self):
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,2000,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 7, 1))
        visible = visible_employee_pks(self.manager, request=None)
        report = apply_upload(self.manager, parsed, visible)
        self.assertEqual(OvertimeEntry.objects.count(), 0)
        self.assertEqual(len(report.skipped_scope), 1)

    def test_parse_accepts_resigned_employee_for_a_date_before_they_left(self):
        # Deactivated the same way deactivate_past_lwd does, after
        # resignation — must still be payable for work done before LWD.
        self.employee.is_active = False
        self.employee.last_working_date = date(2026, 7, 31)
        self.employee.save()
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 9, 8))
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)

    def test_parse_rejects_date_after_last_working_date(self):
        self.employee.is_active = False
        self.employee.last_working_date = date(2026, 6, 10)
        self.employee.save()
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 9, 8))
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("wasn't employed on", errors[0].message)

    def test_parse_rejects_date_before_joining(self):
        self.employee.joining_date = date(2026, 6, 20)
        self.employee.save()
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 9, 8))
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)

    def test_apply_writes_resigned_employees_overtime_using_payroll_scope(self):
        self.employee.is_active = False
        self.employee.last_working_date = date(2026, 7, 31)
        self.employee.save()
        f = self._csv([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            "2026-06-18,1042,,Regular OT,02:30:00",
        ])
        parsed, errors, dups = parse_upload(f, "ot.csv", today=date(2026, 9, 8))
        self.assertEqual(errors, [])
        # The plain (non-payroll) scope helper would silently drop this row —
        # payroll_visible_employee_pks is what the real view uses.
        ordinary_scope = visible_employee_pks(self.manager, request=None)
        self.assertNotIn(self.employee.pk, ordinary_scope)
        payroll_scope = payroll_visible_employee_pks(self.manager, request=None)
        self.assertIn(self.employee.pk, payroll_scope)
        report = apply_upload(self.manager, parsed, payroll_scope)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(OvertimeEntry.objects.get().employee_id, self.employee.pk)

    def test_template_columns_and_scoped_reference_sheet(self):
        body = build_overtime_xlsx_template(self.manager)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active

        headers = [c.value for c in ws[1]]
        self.assertEqual(
            headers, ["Working date", "Employee ID", "Worked In", "Overtime type", "OT hours (hh:mm:ss)"],
        )

        # No Team rows exist in this test's data, so only the Overtime type
        # dropdown is added — see test_template_offers_a_worked_in_dropdown
        # below for the team-dropdown case.
        type_dvs = [dv for dv in ws.data_validations.dataValidation if dv.type == "list"]
        self.assertEqual(len(type_dvs), 1)
        self.assertIn("D2", type_dvs[0].sqref)
        lookup_values = [c.value for c in wb["Lookups"]["A"][1:] if c.value]
        self.assertEqual(lookup_values, ["Regular OT"])

        # The manager's reference sheet lists themself + their reportee, not
        # the outside employee — same scoping apply_upload enforces at write time.
        ref = wb["Employees (reference)"]
        ref_ids = [r[0] for r in ref.iter_rows(min_row=2, values_only=True) if r[0]]
        self.assertEqual(ref_ids, ["900", "1042"])

    def test_template_offers_a_worked_in_dropdown(self):
        Team.objects.create(name="MARS", department=self.department)
        Team.objects.create(name="MARS QC", department=self.department)

        body = build_overtime_xlsx_template(self.manager)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active

        list_dvs = [dv for dv in ws.data_validations.dataValidation if dv.type == "list"]
        self.assertEqual(len(list_dvs), 2)
        team_dv = next(dv for dv in list_dvs if "C2" in dv.sqref)
        self.assertIn("Lookups!$B$2", team_dv.formula1)
        team_values = [c.value for c in wb["Lookups"]["B"][1:] if c.value]
        self.assertEqual(sorted(team_values), ["MARS", "MARS QC"])

    def test_template_view_gated_the_same_way_as_upload(self):
        group, _ = Group.objects.get_or_create(name="HR")
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_overtime_upload_template", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([group])
        self.manager.groups.add(group)

        self.client.force_login(self.manager)
        resp = self.client.get("/payroll/overtime/upload/template/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


class SalaryUploadTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.finance_user = _user("fin@example.com", employee_id="900", department=self.department)
        self.employee = _user("sal1@example.com", employee_id="1042", department=self.department)
        JobInfo.objects.create(employee=self.finance_user)
        JobInfo.objects.create(employee=self.employee)

    def _csv(self, lines: list[str]) -> io.BytesIO:
        return io.BytesIO("\n".join(lines).encode("utf-8"))

    def test_parse_happy_path(self):
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,85000,2026-03-01,Annual increment",
        ])
        parsed, errors, dups = parse_salary_upload(f, "salary.csv", today=date(2026, 8, 28))
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].gross_monthly, Decimal("85000.00"))
        self.assertEqual(parsed[0].employee, self.employee)
        self.assertEqual(parsed[0].effective_from, date(2026, 3, 1))
        self.assertEqual(parsed[0].reason, SalaryStructure.Reason.ANNUAL_INCREMENT)

    def test_parse_accepts_reason_code_case_insensitively(self):
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,85000,2026-03-01,correction",
        ])
        parsed, errors, dups = parse_salary_upload(f, "salary.csv", today=date(2026, 8, 28))
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].reason, SalaryStructure.Reason.CORRECTION)

    def test_parse_rejects_unknown_employee_bad_amount_and_bad_reason(self):
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "9999,85000,2026-03-01,Correction",
            "1042,not-a-number,2026-03-01,Correction",
            "1042,85000,2026-03-01,Not A Real Reason",
        ])
        parsed, errors, dups = parse_salary_upload(f, "salary.csv", today=date(2026, 8, 28))
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 3)

    def test_parse_flags_duplicate_within_file(self):
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,85000,2026-03-01,Correction",
            "1042,90000,2026-06-01,Correction",
        ])
        parsed, errors, dups = parse_salary_upload(f, "salary.csv", today=date(2026, 8, 28))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(dups), 1)

    def test_a_separated_employee_is_still_payable_for_a_date_before_they_left(self):
        """Real bug, 2026-09-21: the lookup filtered is_active=True, so a
        backdated correction for someone since separated (e.g. a mid-year
        increment never entered before they resigned) failed with 'No
        active employee', the exact class of bug already fixed for the
        overtime uploader on 2026-09-08. Fixed the same way: no is_active
        filter on the lookup, employed_on() checks the actual window."""
        self.employee.last_working_date = date(2026, 8, 9)
        self.employee.is_active = False
        self.employee.save()
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,85000,2026-07-01,Correction",
        ])
        parsed, errors, dups = parse_salary_upload(f, "salary.csv", today=date(2026, 9, 21))
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].employee, self.employee)

    def test_a_separated_employee_is_rejected_for_a_date_after_they_left(self):
        """employed_on() still catches a genuinely wrong date/ID — the
        blanket is_active filter isn't replaced with nothing."""
        self.employee.last_working_date = date(2026, 8, 9)
        self.employee.is_active = False
        self.employee.save()
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,85000,2026-09-01,Correction",
        ])
        parsed, errors, dups = parse_salary_upload(f, "salary.csv", today=date(2026, 9, 21))
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("wasn't employed on", errors[0].message)

    def test_apply_creates_then_supersedes(self):
        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,85000,2026-03-01,Correction",
        ])
        parsed, _, _ = parse_salary_upload(f, "salary.csv", today=date(2026, 8, 28))
        report = apply_salary_upload(self.finance_user, parsed, note="cutover")
        self.assertEqual(len(report.created), 1)
        first = SalaryStructure.objects.get()
        self.assertEqual(first.gross_monthly, Decimal("85000.00"))
        self.assertEqual(first.effective_from, date(2026, 3, 1))
        self.assertEqual(first.status, SalaryStructure.Status.APPROVED)
        self.assertEqual(first.revision_no, 1)
        first_pk = first.pk

        f2 = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            "1042,95000,2026-06-01,Annual increment",
        ])
        parsed2, _, _ = parse_salary_upload(f2, "salary2.csv", today=date(2026, 8, 28))
        report2 = apply_salary_upload(self.finance_user, parsed2, note="")
        self.assertEqual(len(report2.updated), 1)
        self.assertEqual(report2.apply_errors, [])
        self.assertEqual(SalaryStructure.objects.count(), 2)

        first.refresh_from_db()
        self.assertEqual(first.pk, first_pk)
        self.assertEqual(first.status, SalaryStructure.Status.SUPERSEDED)
        self.assertEqual(first.effective_to, date(2026, 5, 31))
        self.assertEqual(first.gross_monthly, Decimal("85000.00"))  # untouched — history preserved

        current = SalaryStructure.objects.get(status=SalaryStructure.Status.APPROVED)
        self.assertEqual(current.gross_monthly, Decimal("95000.00"))
        self.assertEqual(current.effective_from, date(2026, 6, 1))
        self.assertIsNone(current.effective_to)
        self.assertEqual(current.revision_no, 2)
        self.assertEqual(current.reason, SalaryStructure.Reason.ANNUAL_INCREMENT)

    def test_apply_isolates_a_bad_row_without_aborting_the_batch(self):
        other = _user("sal2@example.com", employee_id="1043", department=self.department)
        JobInfo.objects.create(employee=other)
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 6, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("80000.00"), status=SalaryStructure.Status.APPROVED, revision_no=1,
        )

        f = self._csv([
            "Employee ID,Gross monthly salary,Effective from,Reason",
            # Predates the employee's existing effective_from — must fail at apply time.
            "1042,85000,2026-01-01,Correction",
            "1043,60000,2026-03-01,New hire",
        ])
        parsed, _, _ = parse_salary_upload(f, "salary.csv", today=date(2026, 8, 28))
        report = apply_salary_upload(self.finance_user, parsed, note="")

        self.assertEqual(len(report.apply_errors), 1)
        self.assertEqual(report.apply_errors[0].raw["Employee_ID"], "1042")
        self.assertEqual(len(report.created), 1)
        self.assertEqual(report.created[0].employee_id, "1043")
        # The unaffected employee's original row is untouched.
        untouched = self.employee.salary_structures.get(status=SalaryStructure.Status.APPROVED)
        self.assertEqual(untouched.gross_monthly, Decimal("80000.00"))


class SalaryUploadViewAccessTests(TestCase):
    """Gross salary is sensitive — the view must be gated to Payroll Admins via
    ProtectedView, unlike overtime's broader managed-scope gate."""

    def setUp(self):
        for name in ("HR", "Payroll Admins"):
            Group.objects.get_or_create(name=name)
        self.payroll_admins_group = Group.objects.get(name="Payroll Admins")
        self.hr_user = _user("hr@example.com", employee_id="1")
        self.hr_user.groups.add(Group.objects.get(name="HR"))
        self.payroll_admin_user = _user("fin2@example.com", employee_id="2")
        self.payroll_admin_user.groups.add(self.payroll_admins_group)
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_salary_upload", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([self.payroll_admins_group])

    def test_hr_user_denied(self):
        self.client.force_login(self.hr_user)
        resp = self.client.get("/payroll/salary/upload/")
        self.assertRedirects(resp, "/")

    def test_payroll_admin_allowed(self):
        self.client.force_login(self.payroll_admin_user)
        resp = self.client.get("/payroll/salary/upload/")
        self.assertEqual(resp.status_code, 200)

    def test_template_download_gated_the_same_way(self):
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_salary_upload_template", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([self.payroll_admins_group])

        self.client.force_login(self.hr_user)
        self.assertRedirects(self.client.get("/payroll/salary/upload/template/"), "/")

        self.client.force_login(self.payroll_admin_user)
        resp = self.client.get("/payroll/salary/upload/template/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


class SalaryUploadTemplateTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.employee = _user("sal3@example.com", employee_id="1044", department=self.department)
        JobInfo.objects.create(employee=self.employee)

    def test_template_columns_match_parser_and_has_reason_dropdown(self):
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("70000.00"), status=SalaryStructure.Status.APPROVED, revision_no=1,
        )

        body = build_salary_xlsx_template()
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active

        headers = [c.value for c in ws[1]]
        # First four columns must match parse_salary_upload's positional layout exactly.
        self.assertEqual(headers[:4], ["Employee ID", "Gross monthly salary", "Effective from", "Reason"])

        row = next(r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] == "1044")
        self.assertEqual(row[5], 70000.0)  # Current gross (reference only)

        reason_dvs = [dv for dv in ws.data_validations.dataValidation if dv.type == "list"]
        self.assertEqual(len(reason_dvs), 1)
        self.assertIn("D2", reason_dvs[0].sqref)
        lookup_values = [c.value for c in wb["Lookups"]["A"][1:] if c.value]
        self.assertEqual(set(lookup_values), {label for _, label in SalaryStructure.Reason.choices})

    def test_template_orders_by_employee_id_numerically(self):
        # "99" < "1044" numerically but sorts after it as a plain string — proves the cast-to-int fix applies here too.
        other = _user("sal4@example.com", employee_id="99", department=self.department)
        JobInfo.objects.create(employee=other)

        body = build_salary_xlsx_template()
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        ids = [r[0] for r in ws.iter_rows(min_row=2, values_only=True) if r[0] in ("99", "1044")]
        self.assertEqual(ids, ["99", "1044"])


class SalaryStructureListDetailViewTests(TestCase):
    """Payroll-Admins-only (Finance until 2026-10-05), company-wide list/detail on top of SalaryStructure —
    same gate posture as SalaryUploadViewAccessTests, plus the department
    filter must NOT lock the viewer to their own department (Payroll Admins isn't in
    accounts.permissions.ALL_DEPARTMENTS_GROUPS, unlike HR/Admins/Sr. Leadership)."""

    def setUp(self):
        for name in ("HR", "Payroll Admins"):
            Group.objects.get_or_create(name=name)
        self.payroll_admins_group = Group.objects.get(name="Payroll Admins")
        self.hr_user = _user("hr2@example.com", employee_id="1")
        self.hr_user.groups.add(Group.objects.get(name="HR"))
        self.payroll_admin_user = _user("fin3@example.com", employee_id="2")
        self.payroll_admin_user.groups.add(self.payroll_admins_group)
        for view_name in ("payroll_salary_list", "payroll_salary_detail"):
            pv, _ = ProtectedView.objects.get_or_create(
                view_name=view_name, defaults={"registered_app": "payroll"},
            )
            pv.allowed_groups.set([self.payroll_admins_group])

        self.dept_a = Department.objects.create(name="Digital Advertising")
        self.dept_b = Department.objects.create(name="Software Development")
        self.employee_a = _user("sala@example.com", employee_id="3001", department=self.dept_a)
        self.employee_b = _user("salb@example.com", employee_id="3002", department=self.dept_b)
        JobInfo.objects.create(employee=self.employee_a)
        JobInfo.objects.create(employee=self.employee_b)

        SalaryStructure.objects.create(
            employee=self.employee_a, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("70000.00"),
            status=SalaryStructure.Status.APPROVED, revision_no=1,
        )
        # Superseded then re-approved — proves history shows on detail but
        # not as the "current" row, and the list shows only the current one.
        old = SalaryStructure.objects.create(
            employee=self.employee_b, effective_from=date(2026, 1, 1), effective_to=date(2026, 5, 31),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("60000.00"),
            status=SalaryStructure.Status.SUPERSEDED, revision_no=1,
        )
        SalaryStructure.objects.create(
            employee=self.employee_b, effective_from=date(2026, 6, 1),
            reason=SalaryStructure.Reason.ANNUAL_INCREMENT, gross_monthly=Decimal("65000.00"),
            status=SalaryStructure.Status.APPROVED, revision_no=2,
        )
        self.old_structure = old

    def test_hr_user_denied_list_and_detail(self):
        self.client.force_login(self.hr_user)
        self.assertRedirects(self.client.get("/payroll/salary/"), "/")
        self.assertRedirects(self.client.get(f"/payroll/salary/{self.employee_a.pk}/"), "/")

    def test_payroll_admin_sees_current_structures_for_every_department(self):
        self.client.force_login(self.payroll_admin_user)
        resp = self.client.get("/payroll/salary/")
        self.assertEqual(resp.status_code, 200)
        employees = list(resp.context["employees"])
        self.assertEqual({e.pk for e in employees}, {self.employee_a.pk, self.employee_b.pk})
        self.assertTrue(resp.context["can_pick_all_departments"])

        by_pk = {e.pk: e for e in employees}
        self.assertEqual(
            by_pk[self.employee_b.pk].current_salary_list[0].gross_monthly, Decimal("65000.00"),
        )

    def test_department_filter_narrows_without_locking_the_viewer(self):
        self.client.force_login(self.payroll_admin_user)
        resp = self.client.get("/payroll/salary/", {"department": self.dept_a.pk})
        employees = list(resp.context["employees"])
        self.assertEqual({e.pk for e in employees}, {self.employee_a.pk})

    def test_detail_shows_current_and_history(self):
        self.client.force_login(self.payroll_admin_user)
        resp = self.client.get(f"/payroll/salary/{self.employee_b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["current"].gross_monthly, Decimal("65000.00"))
        self.assertEqual([s.pk for s in resp.context["history"]], [self.old_structure.pk])


class PayrollReportFilterBarTests(TestCase):
    """The report's filter bar is department + team + date range, nothing else.

    Job / work location stay as report *columns* — they were dropped as
    filters on 2026-09-06 because that pair never narrowed the question this
    report actually answers, and two dead dropdowns cost more than they gave.
    """

    def setUp(self):
        hr_group, _ = Group.objects.get_or_create(name="HR")
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_report", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([hr_group])
        self.department = Department.objects.create(name="Operations")
        self.team = Team.objects.create(name="Core Ops", department=self.department)
        self.viewer = _user("report-hr@example.com", employee_id="4001")
        self.viewer.groups.add(hr_group)

    def test_filter_bar_keeps_department_team_and_dates_only(self):
        self.client.force_login(self.viewer)
        html = self.client.get("/payroll/report/").content.decode()
        for name in ('name="department"', 'name="team"', 'name="start_date"', 'name="end_date"'):
            self.assertIn(name, html)
        self.assertNotIn('name="job_location"', html)
        self.assertNotIn('name="work_location"', html)

    def test_location_columns_survive_in_the_table(self):
        self.client.force_login(self.viewer)
        # The table is the htmx results fragment since 2026-10-13.
        html = self.client.get(
            "/payroll/report/",
            HTTP_HX_REQUEST="true", HTTP_HX_TARGET="payroll-report-results",
        ).content.decode()
        self.assertIn("Job Location", html)
        self.assertIn("Work Location", html)

    def test_stale_location_querystring_is_ignored_not_an_error(self):
        """A bookmarked URL from before the filters were removed must still
        render the report rather than blow up on the unknown params."""
        self.client.force_login(self.viewer)
        response = self.client.get("/payroll/report/", {"job_location": "9", "work_location": "9"})
        self.assertEqual(response.status_code, 200)


class PayrollReportDeferredLoadTests(TestCase):
    """The page renders a shell; the table arrives via an htmx request that
    paginates employees *before* building rows (PayrollReportView)."""

    PARAMS = {"start_date": "2026-05-01", "end_date": "2026-05-31"}
    RESULTS = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "payroll-report-results"}

    def setUp(self):
        hr_group, _ = Group.objects.get_or_create(name="HR")
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_report", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([hr_group])
        self.department = Department.objects.create(name="Operations")
        self.viewer = _user("report-hr@example.com", employee_id="0001", department=self.department)
        self.viewer.groups.add(hr_group)
        # 0001 (viewer) + 0100..0129 → 31 employees; page 2 of 25 holds 6.
        self.staff = [
            _user(f"staff{n:02d}@example.com", employee_id=f"{100 + n:04d}", department=self.department)
            for n in range(30)
        ]
        self.client.force_login(self.viewer)

    def _results(self, **params):
        return self.client.get("/payroll/report/", {**self.PARAMS, **params}, **self.RESULTS)

    def test_full_get_renders_shell_with_deferred_loader_and_no_rows(self):
        response = self.client.get("/payroll/report/", self.PARAMS)

        self.assertContains(response, 'hx-trigger="load"')
        self.assertContains(response, "Building report")
        self.assertNotContains(response, ">0100<")

    def test_results_request_returns_fragment_for_the_current_page_only(self):
        from unittest import mock

        import payroll.inputs as payroll_inputs

        with mock.patch.object(payroll_inputs, "classify_night_shifts",
                               wraps=payroll_inputs.classify_night_shifts) as classify:
            response = self._results(page="2")

        self.assertTemplateUsed(response, "payroll/_report_results.html")
        self.assertNotContains(response, "<html")
        self.assertEqual(response.context["paginator"].count, 31)
        self.assertEqual([r["employee_id"] for r in response.context["rows"]],
                         [f"{n:04d}" for n in range(124, 130)])
        self.assertEqual(len(classify.call_args.args[1]), 6)

    def test_ot_columns_come_from_the_whole_scope_not_the_page(self):
        component = PayComponent.objects.create(
            code="OT_PAGE", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        ot = OvertimeType.objects.create(
            code="special_ot_page", name="Special OT", basis=OvertimeType.Basis.PER_HOUR, component=component,
        )
        # Only someone on page 2 has overtime…
        OvertimeEntry.objects.create(
            employee=self.staff[-1], period=PayrollPeriod.for_month(2026, 5),
            work_date=date(2026, 5, 10), overtime_type=ot, seconds=2 * 3600,
            status=OvertimeEntry.Status.APPROVED,
        )

        response = self._results()   # …but page 1 still gets the column.

        self.assertEqual(response.context["ot_types"], [ot])
        self.assertContains(response, "Special OT (hrs)")
        self.assertEqual(response.context["rows"][0]["ot_by_type"], [Decimal("0.00")])

    def test_page_rows_match_the_full_build(self):
        filters = PayrollFilters(start_date=date(2026, 5, 1), end_date=date(2026, 5, 31))
        full = build_payroll_report(filters)

        response = self._results(page="2")

        self.assertEqual(response.context["rows"], full["rows"][25:])

    def test_history_restore_gets_the_full_page(self):
        response = self.client.get(
            "/payroll/report/", self.PARAMS,
            HTTP_HX_HISTORY_RESTORE_REQUEST="true", **self.RESULTS,
        )

        self.assertTemplateUsed(response, "payroll/report.html")
        self.assertTemplateNotUsed(response, "payroll/_report_results.html")

    def test_over_long_range_shows_error_without_a_loader(self):
        response = self.client.get("/payroll/report/", {"start_date": "2026-05-01", "end_date": "2026-06-30"})

        self.assertContains(response, "cannot exceed 31 days")
        self.assertNotContains(response, 'hx-trigger="load"')

    def test_export_still_covers_every_row(self):
        response = self.client.get("/payroll/report/", {**self.PARAMS, "export": "xlsx", "page": "2"})

        sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
        self.assertEqual(sheet.max_row - 1, 31)


class PayrollRunEmployeeNoteEditViewTests(TestCase):
    """The htmx save path swaps just the 3 note cells via OOB fragments and
    fires a toast + drawer-close event, instead of a full HX-Refresh — see
    _support_info_note_cell.html and PayrollRunEmployeeNoteEditView.post()."""

    def setUp(self):
        Group.objects.get_or_create(name="Payroll")
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_run_employee_note_edit", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([Group.objects.get(name="Payroll")])

        self.department = Department.objects.create(name="Operations")
        self.supervisor = _user("boss@example.com", employee_id="200", department=self.department)
        self.supervisor.groups.add(Group.objects.get(name="Payroll"))
        self.employee = _user("report@example.com", employee_id="201", department=self.department)
        JobInfo.objects.create(employee=self.employee, reports_to=self.supervisor)

        period = PayrollPeriod.for_month(2026, 7)
        self.run = PayrollRun.objects.create(
            period=period, status=PayrollRun.Status.INPUTS_OPEN,
            added_by=self.supervisor, updated_by=self.supervisor,
        )
        self.url = reverse("payroll_run_employee_note_edit", args=[self.run.pk, self.employee.pk])

    def test_htmx_save_returns_oob_cells_and_toast_without_refresh(self):
        self.client.force_login(self.supervisor)
        resp = self.client.post(
            self.url, {"note_supervisor": "Approved overtime for the 28th."},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("HX-Refresh", resp)
        body = resp.content.decode()
        self.assertIn(f'id="note-cell-SUPERVISOR-{self.employee.pk}"', body)
        self.assertIn('hx-swap-oob="true"', body)
        self.assertIn("Approved overtime for the 28th.", body)

        trigger = json.loads(resp["HX-Trigger"])
        self.assertIn("orbis:toast", trigger)
        self.assertEqual(trigger["orbis:toast"]["type"], "success")
        self.assertIs(trigger["orbis:drawer-close"], True)

        note = PayrollRunEmployeeNote.objects.get(run=self.run, employee=self.employee, role="SUPERVISOR")
        self.assertEqual(note.note, "Approved overtime for the 28th.")

    def test_oob_fragment_omits_write_button_for_roles_the_supervisor_cant_edit(self):
        self.client.force_login(self.supervisor)
        resp = self.client.post(self.url, {"note_supervisor": "Note text"}, HTTP_HX_REQUEST="true")
        body = resp.content.decode()

        hr_start = body.index(f'note-cell-HR-{self.employee.pk}')
        finance_start = body.index(f'note-cell-FINANCE-{self.employee.pk}')
        supervisor_fragment = body[:hr_start]
        hr_fragment = body[hr_start:finance_start]
        finance_fragment = body[finance_start:]

        # A plain supervisor can write their own SUPERVISOR note but not the
        # HR/Finance ones — those two OOB cells must omit the write button.
        self.assertIn("hx-get", supervisor_fragment)
        self.assertNotIn("hx-get", hr_fragment)
        self.assertNotIn("hx-get", finance_fragment)

    def test_non_htmx_post_still_redirects_and_flashes_a_message(self):
        self.client.force_login(self.supervisor)
        resp = self.client.post(self.url, {"note_supervisor": "Note text"})
        self.assertRedirects(
            resp, f"/payroll/runs/{self.run.pk}/?tab=support_info", fetch_redirect_response=False,
        )


# ---------------------------------------------------------------------------
# Overtime input window + management screens (2026-09-15)
# ---------------------------------------------------------------------------

class OvertimeWindowTests(TestCase):
    """The date rules every overtime write path shares — see
    payroll/services.py::OvertimeWindow."""

    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.manager = _user("otw-mgr@example.com", employee_id="900", department=self.department)
        JobInfo.objects.create(employee=self.manager)
        for name in ("HR", "Payroll", "Payroll Admins"):
            Group.objects.get_or_create(name=name)
        self.hr_payroll = _user("otw-hrp@example.com", employee_id="901", department=self.department)
        self.hr_payroll.groups.add(Group.objects.get(name="HR"), Group.objects.get(name="Payroll"))

        # Aug 16 - Sep 15 is the period being compiled; Sep 16 - Oct 15 is the
        # one the calendar is currently in. The user's own worked example.
        self.compiling = PayrollPeriod.for_month(2026, 9)
        self.today = date(2026, 9, 18)

    def _window(self, user, today=None):
        return OvertimeWindow(user, today=today or self.today)

    def _open_run(self, period):
        return PayrollRun.objects.create(period=period, status=PayrollRun.Status.INPUTS_OPEN)

    def _lock(self, run):
        run.status = PayrollRun.Status.INPUTS_LOCKED
        run.save()
        return run

    def test_floor_is_the_open_periods_start(self):
        self._open_run(self.compiling)
        self.assertEqual(self._window(self.manager).floor, date(2026, 8, 16))

    def test_floor_falls_back_to_the_calendar_period_when_no_run_is_open(self):
        # Sep 18 sits in the Sep 16 - Oct 15 period, so that is the floor.
        self.assertEqual(self._window(self.manager).floor, date(2026, 9, 16))

    def test_supervisor_may_file_past_the_open_periods_end(self):
        """The case that must keep working: on Sep 18, with Aug 16 - Sep 15
        still being compiled, Sep 17 is fine — it simply files into the next
        period."""
        self._open_run(self.compiling)
        self.assertIsNone(self._window(self.manager).check(date(2026, 9, 17)))

    def test_supervisor_cannot_file_before_the_floor(self):
        self._open_run(self.compiling)
        reason = self._window(self.manager).check(date(2026, 8, 10))
        self.assertIsNotNone(reason)
        self.assertIn("before the open payroll period", reason)

    def test_future_dates_are_rejected(self):
        self._open_run(self.compiling)
        reason = self._window(self.manager).check(date(2026, 9, 19))
        self.assertIn("in the future", reason)

    # Q55 (2026-09-15) reversed "a locked period refuses everyone but
    # HR-Payroll": it now ACCEPTS late overtime from anyone in scope and
    # files it PENDING, so it is captured for review instead of lost.

    def test_locked_period_accepts_a_supervisor_but_as_pending(self):
        self._lock(self._open_run(self.compiling))
        self._open_run(PayrollPeriod.for_month(2026, 10))  # the new period is open
        decision = self._window(self.manager).resolve(date(2026, 9, 10))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.status, OvertimeEntry.Status.PENDING)

    def test_locked_period_files_hr_payrolls_own_upload_as_pending_too(self):
        """"Once inputs are locked nobody uploads as approved" — HR promotes
        an entry with the explicit Approve action, not by being the uploader."""
        self._lock(self._open_run(self.compiling))
        self._open_run(PayrollPeriod.for_month(2026, 10))
        decision = self._window(self.hr_payroll).resolve(date(2026, 9, 10))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.status, OvertimeEntry.Status.PENDING)

    def test_only_the_most_recently_locked_period_accepts_late_overtime(self):
        """An older locked period has been paid — it is closed for good."""
        self._lock(self._open_run(PayrollPeriod.for_month(2026, 8)))
        self._lock(self._open_run(self.compiling))
        older = self._window(self.manager).resolve(date(2026, 7, 20))
        self.assertFalse(older.allowed)
        self.assertIn("already been completed", older.reason)
        newest = self._window(self.manager).resolve(date(2026, 9, 10))
        self.assertTrue(newest.allowed)

    def test_every_allowed_date_lands_pending_review(self):
        """Since the review chain (2026-10-17) the window is a pure date
        gate: locked or open, an allowed write starts at PENDING. Before
        that an open period handed back APPROVED (Q4)."""
        run = self._lock(self._open_run(self.compiling))
        self.assertEqual(
            self._window(self.manager).resolve(date(2026, 9, 10)).status,
            OvertimeEntry.Status.PENDING,
        )
        run.status = PayrollRun.Status.INPUTS_OPEN
        run.save()
        decision = self._window(self.manager).resolve(date(2026, 9, 10))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.status, OvertimeEntry.Status.PENDING)
        self.assertEqual(
            self._window(self.hr_payroll).resolve(self.today).status,
            OvertimeEntry.Status.PENDING,
        )

    def test_even_hr_payroll_is_bounded_by_the_year_long_sanity_check(self):
        self.assertIsNotNone(self._window(self.hr_payroll).check(date(2024, 1, 1)))

    # --- what the page is allowed to SAY about the window ---------------
    #
    # Reported 2026-09-15: a supervisor read the list page's banner as
    # declaring the open Aug 16 - Sep 15 period locked, while filing into it
    # worked fine. The rules were right; the banner recited the lock rule
    # unconditionally. These pin the state the banner keys off.

    def test_an_open_period_reports_nothing_locked(self):
        self._open_run(self.compiling)
        window = self._window(self.manager)
        self.assertFalse(window.has_locked_runs)
        self.assertFalse(window.current_period_locked)
        # ...and that is not because filing is blocked some other way.
        self.assertIsNone(window.check(date(2026, 9, 10)))

    def test_a_locked_period_is_reported_as_locked(self):
        self._lock(self._open_run(self.compiling))
        window = self._window(self.manager, today=date(2026, 9, 10))
        self.assertTrue(window.has_locked_runs)
        self.assertTrue(window.current_period_locked)

    def test_a_locked_past_period_does_not_report_today_as_locked(self):
        """The distinction the banner needs: history is locked, today isn't."""
        self._lock(self._open_run(self.compiling))
        self._open_run(PayrollPeriod.for_month(2026, 10))
        window = self._window(self.manager)  # today = Sep 18, next period
        self.assertTrue(window.has_locked_runs)
        self.assertFalse(window.current_period_locked)


class OvertimeUploadWindowTests(TestCase):
    """The same window rules, reached through the bulk uploader."""

    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.manager = _user("otuw-mgr@example.com", employee_id="900", department=self.department)
        JobInfo.objects.create(employee=self.manager)
        self.employee = _user("otuw-emp@example.com", employee_id="1042", department=self.department)
        JobInfo.objects.create(employee=self.employee, reports_to=self.manager)
        self.component = PayComponent.objects.create(
            code="OT", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR,
            component=self.component,
        )
        self.compiling = PayrollPeriod.for_month(2026, 9)
        self.today = date(2026, 9, 18)

    def _parse(self, work_date):
        f = io.BytesIO("\n".join([
            "Working date,Employee ID,Worked In,Overtime type,OT hours",
            f"{work_date.isoformat()},1042,,Regular OT,02:30:00",
        ]).encode("utf-8"))
        parsed, errors, dups = parse_upload(f, "ot.csv", today=self.today)
        self.assertEqual((errors, dups), ([], []))
        return parsed

    def test_partition_splits_out_of_window_rows_before_anything_is_written(self):
        PayrollRun.objects.create(period=self.compiling, status=PayrollRun.Status.INPUTS_OPEN)
        window = OvertimeWindow(self.manager, today=self.today)
        allowed, out_of_window = window.partition(self._parse(date(2026, 8, 10)))
        self.assertEqual((len(allowed), len(out_of_window)), (0, 1))

    def test_partition_lets_locked_period_rows_through_as_pending(self):
        PayrollRun.objects.create(period=self.compiling, status=PayrollRun.Status.INPUTS_LOCKED)
        PayrollRun.objects.create(
            period=PayrollPeriod.for_month(2026, 10), status=PayrollRun.Status.INPUTS_OPEN,
        )
        window = OvertimeWindow(self.manager, today=self.today)
        allowed, out_of_window = window.partition(self._parse(date(2026, 9, 10)))
        self.assertEqual((len(allowed), len(out_of_window)), (1, 0))
        self.assertEqual(allowed[0].window_status, OvertimeEntry.Status.PENDING)

    def test_apply_upload_re_checks_the_window_on_the_write_path(self):
        PayrollRun.objects.create(period=self.compiling, status=PayrollRun.Status.INPUTS_OPEN)
        window = OvertimeWindow(self.manager, today=self.today)
        scope = payroll_visible_employee_pks(self.manager, request=None)
        # Deliberately NOT partitioned first — apply_upload must refuse it anyway.
        report = apply_upload(self.manager, self._parse(date(2026, 8, 10)), scope, window=window)
        self.assertEqual(OvertimeEntry.objects.count(), 0)
        self.assertEqual(len(report.skipped_window), 1)

    def test_re_uploading_a_voided_row_revives_it(self):
        """`status` is in apply_upload's update_fields — without it the row
        stays VOID while the report claims "updated", and the overtime
        silently never gets paid."""
        PayrollRun.objects.create(period=self.compiling, status=PayrollRun.Status.INPUTS_OPEN)
        window = OvertimeWindow(self.manager, today=self.today)
        scope = payroll_visible_employee_pks(self.manager, request=None)
        apply_upload(self.manager, self._parse(date(2026, 9, 1)), scope, window=window)
        entry = OvertimeEntry.objects.get()
        entry.status = OvertimeEntry.Status.VOID
        entry.save(update_fields=["status"])

        report = apply_upload(self.manager, self._parse(date(2026, 9, 1)), scope, window=window)
        self.assertEqual(len(report.updated), 1)
        entry.refresh_from_db()
        # Revived at the start of the chain, never straight to paid.
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)


class OvertimeEntryFormTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Digital Advertising")
        self.manager = _user("otf-mgr@example.com", employee_id="900", department=self.department)
        JobInfo.objects.create(employee=self.manager)
        self.employee = _user("otf-emp@example.com", employee_id="1042", department=self.department)
        JobInfo.objects.create(employee=self.employee, reports_to=self.manager)
        self.component = PayComponent.objects.create(
            code="OT", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR,
            component=self.component,
        )
        self.today = date(2026, 9, 18)
        self.compiling = PayrollPeriod.for_month(2026, 9)
        PayrollRun.objects.create(period=self.compiling, status=PayrollRun.Status.INPUTS_OPEN)
        self.window = OvertimeWindow(self.manager, today=self.today)

    def _form(self, instance=None, **overrides):
        data = {
            "employee": str(self.employee.pk),
            "work_date": "2026-09-01",
            "overtime_type": str(self.ot_type.pk),
            "ot_hours": "02:30:00",
            "status": OvertimeEntry.Status.APPROVED,
            "note": "",
        }
        data.update(overrides)
        return OvertimeEntryForm(
            data, instance=instance, viewer=self.manager, window=self.window,
            employee_queryset=User.objects.all(),
        )

    def test_saves_hours_as_seconds_and_derives_the_period(self):
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save()
        self.assertEqual(entry.seconds, 9000)
        self.assertEqual(entry.period, self.compiling)

    def test_blank_worked_in_defaults_to_the_employees_team_on_create(self):
        team = Team.objects.create(name="MARS", department=self.department)
        self.employee.team = team
        self.employee.save()

        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save()
        self.assertEqual(entry.worked_in, team)

    def test_explicit_worked_in_is_kept_over_the_employees_own_team(self):
        team = Team.objects.create(name="MARS", department=self.department)
        other_team = Team.objects.create(name="MARS QC", department=self.department)
        self.employee.team = team
        self.employee.save()

        form = self._form(worked_in=str(other_team.pk))
        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save()
        self.assertEqual(entry.worked_in, other_team)

    def test_rejects_hours_that_are_not_hh_mm_ss(self):
        form = self._form(ot_hours="2.5 hours")
        self.assertFalse(form.is_valid())
        self.assertIn("ot_hours", form.errors)

    def test_rejects_a_date_outside_the_window(self):
        form = self._form(work_date="2026-08-10")
        self.assertFalse(form.is_valid())
        self.assertIn("work_date", form.errors)

    def test_duplicate_gives_a_field_error_not_an_integrity_error(self):
        self.assertTrue(self._form().is_valid())
        self._form().save()
        form = self._form()
        self.assertFalse(form.is_valid())
        self.assertIn("overtime_type", form.errors)
        self.assertIn("already has an entry", form.errors["overtime_type"][0])

    def test_editing_the_date_across_the_period_boundary_re_derives_the_period(self):
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save()
        self.assertEqual(entry.period, self.compiling)

        # Sep 16 belongs to the NEXT period — OvertimeEntry.clean() would
        # reject it against the stored one if the form didn't re-derive.
        edit = self._form(instance=entry, work_date="2026-09-16")
        self.assertTrue(edit.is_valid(), edit.errors)
        edited = edit.save()
        self.assertEqual(edited.period, PayrollPeriod.for_month(2026, 10))

    def test_employee_is_locked_on_edit(self):
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save()
        other = _user("otf-other@example.com", employee_id="1043", department=self.department)
        edit = self._form(instance=entry, employee=str(other.pk))
        self.assertTrue(edit.is_valid(), edit.errors)
        self.assertEqual(edit.save().employee_id, self.employee.pk)


class OvertimeManagementViewTests(TestCase):
    """List / drawer / void / delete, and who may reach them."""

    def setUp(self):
        for name in ("HR", "Payroll", "Payroll Admins", "Supervisors", "User Admins", "Finance"):
            Group.objects.get_or_create(name=name)
        self.allowed_groups = list(Group.objects.filter(
            name__in=["Supervisors", "User Admins", "HR", "Finance", "Payroll", "Payroll Admins"],
        ))
        for view_name in (
            "payroll_overtime_list", "payroll_overtime_create", "payroll_overtime_edit",
            "payroll_overtime_void", "payroll_overtime_delete",
            "payroll_overtime_employee_search", "payroll_overtime_upload",
        ):
            pv, _ = ProtectedView.objects.get_or_create(
                view_name=view_name, defaults={"registered_app": "payroll"},
            )
            pv.allowed_groups.set(self.allowed_groups)

        self.department = Department.objects.create(name="Digital Advertising")
        self.other_department = Department.objects.create(name="Software Development")
        self.manager = _user("otv-mgr@example.com", employee_id="900", department=self.department)
        self.manager.groups.add(Group.objects.get(name="Supervisors"))
        JobInfo.objects.create(employee=self.manager)
        self.employee = _user("otv-emp@example.com", employee_id="1042", department=self.department)
        JobInfo.objects.create(employee=self.employee, reports_to=self.manager)
        self.outsider = _user("otv-out@example.com", employee_id="2000", department=self.other_department)
        JobInfo.objects.create(employee=self.outsider)
        self.hr_payroll = _user("otv-hrp@example.com", employee_id="901", department=self.department)
        self.hr_payroll.groups.add(Group.objects.get(name="HR"), Group.objects.get(name="Payroll"))

        self.component = PayComponent.objects.create(
            code="OT", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR,
            component=self.component,
        )

        # Anchored on the real today so the window rules line up whenever the
        # suite runs: the current calendar period, with its run open.
        self.today = timezone.localdate()
        self.period = PayrollPeriod.for_month(*period_for_date(self.today))
        self.run = PayrollRun.objects.create(period=self.period, status=PayrollRun.Status.INPUTS_OPEN)
        self.entry = self._entry(self.employee, self.today)
        self.outsider_entry = self._entry(self.outsider, self.today)

    def _entry(self, employee, work_date):
        # An already-approved entry: these tests are about edit/void/delete
        # rights on settled rows, not the review chain (OvertimeReviewChainTests).
        return OvertimeEntry.objects.create(
            employee=employee, period=self.period, work_date=work_date,
            overtime_type=self.ot_type, seconds=9000, status=OvertimeEntry.Status.APPROVED,
        )

    def test_list_shows_only_the_viewers_scope(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_list"))
        self.assertEqual(resp.status_code, 200)
        pks = {e.pk for e in resp.context["entries"]}
        self.assertIn(self.entry.pk, pks)
        self.assertNotIn(self.outsider_entry.pk, pks)

    def test_export_returns_xlsx_scoped_to_the_viewers_filters(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_list"), {"export": "xlsx"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertTrue(resp["Content-Disposition"].startswith("attachment;"))

        workbook = openpyxl.load_workbook(io.BytesIO(resp.content))
        worksheet = workbook.active
        rows = list(worksheet.iter_rows(values_only=True))
        self.assertEqual(rows[0][1], "Emp ID")
        emp_ids = {row[1] for row in rows[1:]}
        self.assertIn(self.employee.employee_id, emp_ids)
        # Same visible-scope rule as the on-screen list — the outsider's
        # entry (different department, not in this manager's scope) must
        # never appear in the export either.
        self.assertNotIn(self.outsider.employee_id, emp_ids)

    def test_list_denied_without_the_protected_view_grant(self):
        stranger = _user("otv-stranger@example.com", employee_id="3000", department=self.department)
        self.client.force_login(stranger)
        self.assertRedirects(self.client.get(reverse("payroll_overtime_list")), "/")

    def test_edit_drawer_refuses_an_entry_outside_scope(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_edit", args=[self.outsider_entry.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_void_then_restore(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("payroll_overtime_void", args=[self.entry.pk]))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, OvertimeEntry.Status.VOID)

        self.client.post(reverse("payroll_overtime_void", args=[self.entry.pk]))
        self.entry.refresh_from_db()
        # Restore returns to the start of the chain (2026-10-17).
        self.assertEqual(self.entry.status, OvertimeEntry.Status.PENDING)

    def test_voided_entries_drop_out_of_the_engines_input(self):
        from payroll.inputs import collect_overtime

        self.client.force_login(self.manager)
        self.client.post(reverse("payroll_overtime_void", args=[self.entry.pk]))
        collected = collect_overtime(self.period, [self.employee.pk])
        self.assertEqual(collected, {})

    def test_action_messages_render_as_a_toast(self):
        """Messages go through base.html's existing window.orbisToast() rather
        than the old banner at the top of <main>, which pushed the page down on
        every save. Guards the base.html wiring, not just the view."""
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("payroll_overtime_void", args=[self.entry.pk]), follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'window.orbisToast("Entry voided')

    def test_delete_confirmation_renders_in_the_drawer(self):
        """GET on the delete URL is the confirmation (a drawer fragment), not
        the delete itself — a browser confirm() can't name the row."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_delete", args=[self.entry.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Delete this overtime entry?")
        self.assertContains(resp, self.employee.display_name)
        self.assertContains(resp, "Void instead")
        # Confirmation must not be destructive.
        self.assertTrue(OvertimeEntry.objects.filter(pk=self.entry.pk).exists())

    def test_delete_confirmation_is_refused_for_a_locked_period(self):
        """The scope/window checks run before a confirmation is offered, so
        nobody confirms an action that would then 403."""
        self.run.status = PayrollRun.Status.INPUTS_LOCKED
        self.run.save()
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_delete", args=[self.entry.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_delete_removes_the_row(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("payroll_overtime_delete", args=[self.entry.pk]))
        self.assertFalse(OvertimeEntry.objects.filter(pk=self.entry.pk).exists())

    def test_supervisor_locked_out_once_inputs_lock(self):
        self.run.status = PayrollRun.Status.INPUTS_LOCKED
        self.run.save()
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.get(reverse("payroll_overtime_edit", args=[self.entry.pk])).status_code, 403,
        )
        self.assertEqual(
            self.client.post(reverse("payroll_overtime_delete", args=[self.entry.pk])).status_code, 403,
        )
        self.assertTrue(OvertimeEntry.objects.filter(pk=self.entry.pk).exists())

    def test_hr_payroll_may_still_edit_a_locked_period(self):
        self.run.status = PayrollRun.Status.INPUTS_LOCKED
        self.run.save()
        self.client.force_login(self.hr_payroll)
        resp = self.client.get(reverse("payroll_overtime_edit", args=[self.entry.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_upload_page_states_the_current_floor(self):
        """The uploader's old "any working date is accepted" copy is now a
        lie — it must render the real floor instead."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_upload"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, resp.context["window"].floor.strftime("%Y-%m-%d"))

    def test_create_drawer_renders(self):
        """Covers the drawer fragment and its EmployeeSearchWidget, which
        reverses payroll_overtime_employee_search at render time."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_create"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "overtime-entry-form")

    def test_employee_search_is_scoped(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("payroll_overtime_employee_search"), {"q": ""})
        self.assertEqual(resp.status_code, 200)
        pks = {u.pk for u in resp.context["results"]}
        self.assertIn(self.employee.pk, pks)
        self.assertNotIn(self.outsider.pk, pks)

    def test_add_entry_happy_path(self):
        other_type = OvertimeType.objects.create(
            code="gh_ot", name="GH OT", basis=OvertimeType.Basis.PER_HOUR, component=self.component,
        )
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("payroll_overtime_create"), {
            "employee": str(self.employee.pk),
            "work_date": self.today.isoformat(),
            "overtime_type": str(other_type.pk),
            "ot_hours": "01:00:00",
            "status": OvertimeEntry.Status.APPROVED,
            "note": "Manual entry",
        })
        self.assertEqual(resp.status_code, 302)
        created = OvertimeEntry.objects.get(overtime_type=other_type)
        self.assertEqual(created.seconds, 3600)
        self.assertEqual(created.added_by, self.manager)


class PendingOvertimeAfterLockTests(TestCase):
    """Q55 (2026-09-15): a locked period captures late overtime as PENDING,
    HR approves it, and the run must be recalculated for it to be paid.

    The failure this whole feature exists to prevent: inputs locked, run
    calculated, a team leader files overtime, nobody notices, it silently
    never gets paid.
    """

    def setUp(self):
        for name in ("HR", "Payroll", "Payroll Admins", "Supervisors", "User Admins", "Finance"):
            Group.objects.get_or_create(name=name)
        groups = list(Group.objects.filter(
            name__in=["Supervisors", "User Admins", "HR", "Finance", "Payroll", "Payroll Admins"],
        ))
        for view_name in (
            "payroll_overtime_list", "payroll_overtime_create", "payroll_overtime_edit",
            "payroll_overtime_void", "payroll_overtime_delete", "payroll_overtime_decide",
            "payroll_overtime_bulk_decide", "payroll_overtime_upload",
        ):
            pv, _ = ProtectedView.objects.get_or_create(
                view_name=view_name, defaults={"registered_app": "payroll"},
            )
            pv.allowed_groups.set(groups)

        self.department = Department.objects.create(name="Digital Advertising")
        self.manager = _user("pot-mgr@example.com", employee_id="900", department=self.department)
        self.manager.groups.add(Group.objects.get(name="Supervisors"))
        JobInfo.objects.create(employee=self.manager)
        self.employee = _user("pot-emp@example.com", employee_id="1042", department=self.department)
        JobInfo.objects.create(employee=self.employee, reports_to=self.manager)
        self.hr_payroll = _user("pot-hrp@example.com", employee_id="901", department=self.department)
        self.hr_payroll.groups.add(Group.objects.get(name="HR"), Group.objects.get(name="Payroll"))
        JobInfo.objects.create(employee=self.hr_payroll)

        self.component = PayComponent.objects.create(
            code="OT", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR,
            component=self.component,
        )
        self.today = timezone.localdate()
        self.period = PayrollPeriod.for_month(*period_for_date(self.today))
        self.run = PayrollRun.objects.create(
            period=self.period, status=PayrollRun.Status.INPUTS_LOCKED,
        )

    def _entry(self, status=OvertimeEntry.Status.PENDING, work_date=None):
        return OvertimeEntry.objects.create(
            employee=self.employee, period=self.period,
            work_date=work_date or self.today,
            overtime_type=self.ot_type, seconds=9000, status=status,
        )

    # --- the engine must not pay a pending entry ------------------------

    def test_pending_overtime_is_invisible_to_the_engine(self):
        from payroll.inputs import collect_overtime

        self._entry()
        self.assertEqual(collect_overtime(self.period, [self.employee.pk]), {})

    def test_approving_makes_it_visible_to_the_engine(self):
        from payroll.inputs import collect_overtime
        from payroll.views import _approve_entry

        entry = self._entry()
        _approve_entry(entry, self.hr_payroll)
        collected = collect_overtime(self.period, [self.employee.pk])
        # employee has no team on file, so worked_in stayed None — the
        # aggregation key is (overtime_type_id, worked_in_id).
        self.assertEqual(collected[self.employee.pk][(self.ot_type.pk, None)]["seconds"], 9000)

    # --- approving / rejecting ------------------------------------------

    def test_hr_payroll_can_approve_and_it_records_who_decided(self):
        entry = self._entry()
        self.client.force_login(self.hr_payroll)
        resp = self.client.post(
            reverse("payroll_overtime_decide", args=[entry.pk, "approve"]),
        )
        self.assertEqual(resp.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(entry.processed_by, self.hr_payroll)
        self.assertIsNotNone(entry.processed_at)

    def test_rejecting_voids_it(self):
        entry = self._entry()
        self.client.force_login(self.hr_payroll)
        self.client.post(reverse("payroll_overtime_decide", args=[entry.pk, "reject"]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.VOID)
        self.assertEqual(entry.processed_by, self.hr_payroll)

    def test_a_supervisor_cannot_approve(self):
        """The whole point of the review — otherwise a team leader signs off
        their own team's late overtime."""
        entry = self._entry()
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("payroll_overtime_decide", args=[entry.pk, "approve"]),
        )
        self.assertEqual(resp.status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)

    def test_approving_an_already_handled_entry_is_a_no_op(self):
        from payroll.views import _approve_entry

        entry = self._entry(status=OvertimeEntry.Status.VOID)
        self.assertFalse(_approve_entry(entry, self.hr_payroll))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.VOID)

    def test_bulk_approve_skips_what_it_cannot_act_on(self):
        pending = self._entry()
        already = self._entry(status=OvertimeEntry.Status.VOID, work_date=self.today - timedelta(days=1))
        self.client.force_login(self.hr_payroll)
        self.client.post(reverse("payroll_overtime_bulk_decide"), {
            "entry_ids": [str(pending.pk), str(already.pk)],
            "decision": "approve",
        })
        pending.refresh_from_db()
        already.refresh_from_db()
        self.assertEqual(pending.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(already.status, OvertimeEntry.Status.VOID)

    # --- the stale-run warning ------------------------------------------

    def test_a_never_calculated_run_is_not_reported_stale(self):
        self._entry()
        self.assertFalse(self.run.overtime_changed_since_calculation)

    def test_approving_after_calculation_marks_the_run_stale(self):
        """The exact scenario: run calculated, overtime approved afterwards,
        nobody recalculates — the page must say so."""
        from payroll.views import _approve_entry

        entry = self._entry()
        self.run.calculated_at = timezone.now()
        self.run.status = PayrollRun.Status.CALCULATED
        self.run.save()
        self.assertFalse(self.run.overtime_changed_since_calculation)

        _approve_entry(entry, self.hr_payroll)
        self.assertTrue(self.run.overtime_changed_since_calculation)

        # ...and recalculating clears it.
        self.run.calculated_at = timezone.now()
        self.run.save()
        self.assertFalse(self.run.overtime_changed_since_calculation)

    def test_pending_count_is_reported_on_the_run(self):
        self._entry()
        self._entry(work_date=self.today - timedelta(days=1))
        self.assertEqual(self.run.pending_overtime_count, 2)

    # --- the drawer cannot file straight to approved --------------------

    def test_the_form_forces_pending_even_if_approved_is_submitted(self):
        window = OvertimeWindow(self.hr_payroll, today=self.today)
        form = OvertimeEntryForm(
            data={
                "employee": str(self.employee.pk),
                "work_date": self.today.isoformat(),
                "overtime_type": str(self.ot_type.pk),
                "status": OvertimeEntry.Status.APPROVED,
                "ot_hours": "02:30:00",
                "note": "",
            },
            viewer=self.hr_payroll, window=window,
            employee_queryset=User.objects.all(),
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().status, OvertimeEntry.Status.PENDING)

    # --- re-upload must not disturb a settled entry ---------------------

    def test_reupload_does_not_downgrade_an_approved_entry_in_a_locked_period(self):
        entry = self._entry(status=OvertimeEntry.Status.APPROVED)
        window = OvertimeWindow(self.manager, today=self.today)
        rows = [ParsedRow(
            line_no=2, employee=self.employee, employee_id="1042", period=self.period,
            work_date=self.today, overtime_type=self.ot_type, seconds=7200,
        )]
        rows, _out = window.partition(rows)
        report = apply_upload(self.manager, rows, {self.employee.pk}, window=window)
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(entry.seconds, 9000)  # untouched
        self.assertEqual(len(report.skipped_locked), 1)

    def test_a_new_row_in_a_locked_period_lands_pending(self):
        window = OvertimeWindow(self.manager, today=self.today)
        rows = [ParsedRow(
            line_no=2, employee=self.employee, employee_id="1042", period=self.period,
            work_date=self.today, overtime_type=self.ot_type, seconds=7200,
        )]
        rows, _out = window.partition(rows)
        report = apply_upload(self.manager, rows, {self.employee.pk}, window=window)
        self.assertEqual(len(report.pending), 1)
        entry = OvertimeEntry.objects.get(employee=self.employee, work_date=self.today)
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)

    def test_a_supervisor_cannot_launder_a_pending_entry_via_void_then_restore(self):
        """The self-approval hole: withdraw your own PENDING entry, restore
        it, and have it come back APPROVED without HR ever seeing it.

        Closed twice over — _get_entry refuses a settled entry in a locked
        period, so the restore never reaches the view at all."""
        entry = self._entry()
        self.client.force_login(self.manager)

        self.client.post(reverse("payroll_overtime_void", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.VOID)

        resp = self.client.post(reverse("payroll_overtime_void", args=[entry.pk]))
        self.assertEqual(resp.status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.VOID)

    def test_restoring_in_a_locked_period_returns_to_pending_not_approved(self):
        """And the second layer: even for an approver, who CAN reach it,
        restore returns the entry to the status the window gives it now."""
        entry = self._entry(status=OvertimeEntry.Status.VOID)
        self.client.force_login(self.hr_payroll)
        self.client.post(reverse("payroll_overtime_void", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)

    def test_restore_in_an_open_period_returns_to_pending_too(self):
        """Used to come back APPROVED under Q4; the review chain makes
        restore the same everywhere."""
        self.run.status = PayrollRun.Status.INPUTS_OPEN
        self.run.save()
        entry = self._entry(status=OvertimeEntry.Status.VOID)
        self.client.force_login(self.manager)
        self.client.post(reverse("payroll_overtime_void", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)


class OvertimeReviewChainTests(TestCase):
    """The two-step chain (2026-10-17, docs/OVERTIME_APPROVAL_CHAIN_PLAN.md):
    every write lands PENDING, an APM/PM reviews, the department head (or
    additional manager / HR-Payroll / Payroll Admin) approves.

    Reporting line: employee -> tl (Supervisor tier) -> apm (Manager tier)
    -> head (department head). Nobody acts on their own row.
    """

    def setUp(self):
        for name in ("HR", "Payroll", "Payroll Admins", "Supervisors", "User Admins", "Finance"):
            Group.objects.get_or_create(name=name)
        groups = list(Group.objects.filter(
            name__in=["Supervisors", "User Admins", "HR", "Finance", "Payroll", "Payroll Admins"],
        ))
        for view_name in (
            "payroll_overtime_list", "payroll_overtime_create", "payroll_overtime_edit",
            "payroll_overtime_void", "payroll_overtime_delete", "payroll_overtime_decide",
            "payroll_overtime_bulk_decide", "payroll_overtime_upload",
        ):
            pv, _ = ProtectedView.objects.get_or_create(
                view_name=view_name, defaults={"registered_app": "payroll"},
            )
            pv.allowed_groups.set(groups)

        manager_level = AccessLevel.objects.create(name="Manager", rank=30)
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        pm = Designation.objects.create(name="Program Manager", access_level=manager_level)
        tl_title = Designation.objects.create(name="Team Lead", access_level=supervisor_level)

        self.department = Department.objects.create(name="Digital Advertising")
        self.other_department = Department.objects.create(name="General BPO")
        sup = Group.objects.get(name="Supervisors")
        self.head = _user("rc-head@example.com", employee_id="800", department=self.department, designation=pm)
        self.head.groups.add(sup)
        self.apm = _user("rc-apm@example.com", employee_id="801", department=self.department, designation=pm)
        self.apm.groups.add(sup)
        self.tl = _user("rc-tl@example.com", employee_id="802", department=self.department, designation=tl_title)
        self.tl.groups.add(sup)
        self.employee = _user("rc-emp@example.com", employee_id="1042", department=self.department)
        self.extra = _user("rc-extra@example.com", employee_id="803", department=self.other_department)
        self.extra.groups.add(sup)
        self.hr_payroll = _user("rc-hrp@example.com", employee_id="804", department=self.other_department)
        self.hr_payroll.groups.add(Group.objects.get(name="HR"), Group.objects.get(name="Payroll"))
        self.payroll_admin = _user("rc-padm@example.com", employee_id="805", department=self.other_department)
        self.payroll_admin.groups.add(Group.objects.get(name="Payroll Admins"))
        self.outsider = _user("rc-out@example.com", employee_id="2000", department=self.other_department)
        self.outsider.groups.add(sup)

        JobInfo.objects.create(employee=self.head)
        JobInfo.objects.create(employee=self.apm, reports_to=self.head)
        JobInfo.objects.create(employee=self.tl, reports_to=self.apm)
        JobInfo.objects.create(employee=self.employee, reports_to=self.tl)
        JobInfo.objects.create(employee=self.outsider)
        self.department.head_of_department = self.head
        self.department.save()
        self.department.additional_managers.add(self.extra)

        self.component = PayComponent.objects.create(
            code="OT", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR,
            component=self.component,
        )
        self.today = timezone.localdate()
        self.period = PayrollPeriod.for_month(*period_for_date(self.today))
        self.run = PayrollRun.objects.create(period=self.period, status=PayrollRun.Status.INPUTS_OPEN)

    def _entry(self, employee=None, status=OvertimeEntry.Status.PENDING, work_date=None, **extra):
        return OvertimeEntry.objects.create(
            employee=employee or self.employee, period=self.period,
            work_date=work_date or self.today,
            overtime_type=self.ot_type, seconds=9000, status=status, **extra,
        )

    def _decide(self, user, entry, decision):
        self.client.force_login(user)
        resp = self.client.post(reverse("payroll_overtime_decide", args=[entry.pk, decision]))
        entry.refresh_from_db()
        return resp

    def _row(self, work_date, seconds=7200):
        return ParsedRow(
            line_no=2, employee=self.employee, employee_id="1042", period=self.period,
            work_date=work_date, overtime_type=self.ot_type, seconds=seconds,
        )

    # --- every write starts pending -------------------------------------

    def test_a_supervisors_upload_in_an_open_period_lands_pending_review(self):
        window = OvertimeWindow(self.tl, today=self.today)
        rows, _out = window.partition([self._row(self.today)])
        report = apply_upload(self.tl, rows, {self.employee.pk}, window=window)
        self.assertEqual(len(report.pending), 1)
        entry = OvertimeEntry.objects.get()
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)
        self.assertIsNone(entry.reviewed_by)

    def test_the_drawer_has_no_status_field_and_creates_pending(self):
        self.client.force_login(self.hr_payroll)
        resp = self.client.post(reverse("payroll_overtime_create"), {
            "employee": str(self.employee.pk),
            "work_date": self.today.isoformat(),
            "overtime_type": str(self.ot_type.pk),
            "ot_hours": "01:00:00",
            "status": OvertimeEntry.Status.APPROVED,  # ignored: not a form field
            "note": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(OvertimeEntry.objects.get().status, OvertimeEntry.Status.PENDING)
        self.assertNotIn("status", OvertimeEntryForm().fields)

    # --- review ------------------------------------------------------------

    def test_apm_reviews_and_it_records_who(self):
        entry = self._entry()
        resp = self._decide(self.apm, entry, "review")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(entry.status, OvertimeEntry.Status.REVIEWED)
        self.assertEqual(entry.reviewed_by, self.apm)
        self.assertIsNotNone(entry.reviewed_at)
        self.assertIsNone(entry.processed_by)

    def test_a_team_lead_cannot_review(self):
        """Supervisor tier is below the review bar — the TL filed it."""
        entry = self._entry()
        resp = self._decide(self.tl, entry, "review")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)

    def test_a_manager_outside_scope_cannot_review(self):
        from accounts.permissions import can_review_overtime

        pm_elsewhere = _user("rc-pm2@example.com", employee_id="806", department=self.other_department,
                             designation=self.apm.designation)
        JobInfo.objects.create(employee=pm_elsewhere)
        self.assertFalse(can_review_overtime(pm_elsewhere, self.employee))
        self.assertTrue(can_review_overtime(self.apm, self.employee))
        self.assertTrue(can_review_overtime(self.head, self.employee))  # PM above the APM may too
        self.assertFalse(can_review_overtime(self.apm, self.apm))  # never your own

    def test_reviewed_is_still_invisible_to_the_engine_and_still_counted_pending(self):
        from payroll.inputs import collect_overtime

        entry = self._entry()
        self._decide(self.apm, entry, "review")
        self.assertEqual(collect_overtime(self.period, [self.employee.pk]), {})
        self.assertEqual(self.run.pending_overtime_count, 1)

    # --- approve -----------------------------------------------------------

    def test_apm_cannot_approve(self):
        entry = self._entry(status=OvertimeEntry.Status.REVIEWED, reviewed_by=self.apm,
                            reviewed_at=timezone.now())
        resp = self._decide(self.apm, entry, "approve")
        self.assertEqual(resp.status_code, 302)  # may reach the view (reviewer), but not this action
        self.assertEqual(entry.status, OvertimeEntry.Status.REVIEWED)

    def test_department_head_approves_a_reviewed_entry(self):
        entry = self._entry(status=OvertimeEntry.Status.REVIEWED, reviewed_by=self.apm,
                            reviewed_at=timezone.now())
        self._decide(self.head, entry, "approve")
        self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(entry.processed_by, self.head)
        self.assertEqual(entry.reviewed_by, self.apm)  # the review stamp survives

    def test_approving_straight_from_pending_stamps_the_approver_as_reviewer(self):
        entry = self._entry()
        self._decide(self.head, entry, "approve")
        self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(entry.reviewed_by, self.head)
        self.assertEqual(entry.processed_by, self.head)

    def test_additional_manager_hr_payroll_and_payroll_admin_can_approve(self):
        for actor in (self.extra, self.hr_payroll, self.payroll_admin):
            with self.subTest(actor=actor.email):
                entry = self._entry(work_date=self.today - timedelta(days=(actor.pk.int % 7) + 1))
                self._decide(actor, entry, "approve")
                self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
                self.assertEqual(entry.processed_by, actor)

    def test_nobody_approves_their_own_row_and_the_heads_falls_to_their_manager(self):
        from accounts.permissions import can_approve_overtime

        ceo = _user("rc-ceo@example.com", employee_id="700", department=self.department)
        self.head.job_info.reports_to = ceo
        self.head.job_info.save()
        self.assertFalse(can_approve_overtime(self.head, self.head))
        self.assertTrue(can_approve_overtime(ceo, self.head))
        self.assertFalse(can_approve_overtime(ceo, self.employee))  # not the head of anything
        self.assertFalse(can_approve_overtime(self.hr_payroll, self.hr_payroll))

    def test_approved_entry_is_paid(self):
        from payroll.inputs import collect_overtime

        entry = self._entry()
        self._decide(self.head, entry, "approve")
        collected = collect_overtime(self.period, [self.employee.pk])
        self.assertEqual(collected[self.employee.pk][(self.ot_type.pk, None)]["seconds"], 9000)

    # --- reject ------------------------------------------------------------

    def test_reviewer_rejects_a_pending_entry_but_not_a_reviewed_one(self):
        entry = self._entry()
        self._decide(self.apm, entry, "reject")
        self.assertEqual(entry.status, OvertimeEntry.Status.VOID)
        self.assertEqual(entry.processed_by, self.apm)

        reviewed = self._entry(status=OvertimeEntry.Status.REVIEWED, work_date=self.today - timedelta(days=1))
        self._decide(self.apm, reviewed, "reject")
        self.assertEqual(reviewed.status, OvertimeEntry.Status.REVIEWED)
        self._decide(self.head, reviewed, "reject")
        self.assertEqual(reviewed.status, OvertimeEntry.Status.VOID)

    # --- bulk --------------------------------------------------------------

    def test_bulk_review_skips_rows_the_viewer_cannot_act_on(self):
        mine = self._entry()
        elsewhere = self._entry(employee=self.outsider)
        self.client.force_login(self.apm)
        self.client.post(reverse("payroll_overtime_bulk_decide"), {
            "entry_ids": [str(mine.pk), str(elsewhere.pk)], "decision": "review",
        })
        mine.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertEqual(mine.status, OvertimeEntry.Status.REVIEWED)
        self.assertEqual(elsewhere.status, OvertimeEntry.Status.PENDING)

    def test_select_all_acts_on_every_filtered_row_across_pages(self):
        """Django-admin style: select_all=1 approves everything the list's
        filters match, not just the ids on the page — the filters ride on
        the form's action querystring."""
        for i in range(30):
            self._entry(work_date=self.today - timedelta(days=i))
        other_type = OvertimeType.objects.create(
            code="gh_ot", name="GH OT", basis=OvertimeType.Basis.PER_HOUR, component=self.component,
        )
        excluded = OvertimeEntry.objects.create(
            employee=self.employee, period=self.period, work_date=self.today,
            overtime_type=other_type, seconds=3600,
        )
        self.client.force_login(self.head)
        url = reverse("payroll_overtime_bulk_decide") + f"?overtime_type={self.ot_type.pk}&from={(self.today - timedelta(days=60)).isoformat()}"
        self.client.post(url, {"decision": "approve", "select_all": "1"})
        self.assertEqual(
            OvertimeEntry.objects.filter(overtime_type=self.ot_type, status=OvertimeEntry.Status.APPROVED).count(), 30,
        )
        excluded.refresh_from_db()
        self.assertEqual(excluded.status, OvertimeEntry.Status.PENDING)  # outside the type filter

    def test_unknown_decision_is_refused(self):
        entry = self._entry()
        self.client.force_login(self.head)
        resp = self.client.post(reverse("payroll_overtime_decide", args=[entry.pk, "bless"]))
        self.assertEqual(resp.status_code, 403)

    # --- a sign-off covers the figures that were signed --------------------

    def test_changing_the_hours_of_an_approved_entry_resets_it(self):
        entry = self._entry(status=OvertimeEntry.Status.APPROVED, reviewed_by=self.apm,
                            reviewed_at=timezone.now(), processed_by=self.head, processed_at=timezone.now())
        self.client.force_login(self.hr_payroll)
        resp = self.client.post(reverse("payroll_overtime_edit", args=[entry.pk]), {
            "work_date": self.today.isoformat(),
            "overtime_type": str(self.ot_type.pk),
            "ot_hours": "01:00:00",
            "note": "",
        })
        self.assertEqual(resp.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.seconds, 3600)
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)
        self.assertIsNone(entry.reviewed_by)
        self.assertIsNone(entry.processed_by)

    def test_a_note_only_edit_keeps_the_sign_off(self):
        entry = self._entry(status=OvertimeEntry.Status.APPROVED, reviewed_by=self.apm,
                            reviewed_at=timezone.now(), processed_by=self.head, processed_at=timezone.now())
        self.client.force_login(self.hr_payroll)
        self.client.post(reverse("payroll_overtime_edit", args=[entry.pk]), {
            "work_date": self.today.isoformat(),
            "overtime_type": str(self.ot_type.pk),
            "ot_hours": "02:30:00",
            "note": "Confirmed with the client",
        })
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(entry.processed_by, self.head)
        self.assertEqual(entry.note, "Confirmed with the client")

    def test_reupload_with_the_same_figures_keeps_the_sign_off(self):
        entry = self._entry(status=OvertimeEntry.Status.APPROVED, reviewed_by=self.apm,
                            reviewed_at=timezone.now(), processed_by=self.head, processed_at=timezone.now())
        window = OvertimeWindow(self.tl, today=self.today)
        rows, _out = window.partition([self._row(self.today, seconds=9000)])
        report = apply_upload(self.tl, rows, {self.employee.pk}, window=window)
        self.assertEqual(len(report.unchanged), 1)
        self.assertEqual(report.total_writes, 0)
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.APPROVED)
        self.assertEqual(entry.processed_by, self.head)

    def test_reupload_with_different_figures_resubmits(self):
        entry = self._entry(status=OvertimeEntry.Status.REVIEWED, reviewed_by=self.apm,
                            reviewed_at=timezone.now())
        window = OvertimeWindow(self.tl, today=self.today)
        rows, _out = window.partition([self._row(self.today, seconds=7200)])
        report = apply_upload(self.tl, rows, {self.employee.pk}, window=window)
        self.assertEqual(len(report.resubmitted), 1)
        self.assertEqual(len(report.updated), 1)
        entry.refresh_from_db()
        self.assertEqual(entry.seconds, 7200)
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)
        self.assertIsNone(entry.reviewed_by)

    def test_void_clears_the_stamps_and_restore_goes_back_to_pending(self):
        entry = self._entry(status=OvertimeEntry.Status.APPROVED, reviewed_by=self.apm,
                            reviewed_at=timezone.now(), processed_by=self.head, processed_at=timezone.now())
        self.client.force_login(self.hr_payroll)
        self.client.post(reverse("payroll_overtime_void", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.VOID)
        self.assertIsNone(entry.reviewed_by)
        self.assertEqual(entry.processed_by, self.hr_payroll)
        self.client.post(reverse("payroll_overtime_void", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, OvertimeEntry.Status.PENDING)
        self.assertIsNone(entry.processed_by)

    # --- the list page --------------------------------------------------------

    def test_list_offers_each_viewer_only_the_actions_they_hold(self):
        pending = self._entry()
        reviewed = self._entry(status=OvertimeEntry.Status.REVIEWED, work_date=self.today - timedelta(days=1))

        self.client.force_login(self.apm)
        rows = {r.pk: r for r in self.client.get(reverse("payroll_overtime_list")).context["entries"]}
        self.assertTrue(rows[pending.pk].can_review)
        self.assertFalse(rows[pending.pk].can_approve)
        self.assertTrue(rows[pending.pk].can_reject)
        self.assertFalse(rows[reviewed.pk].can_review)
        self.assertFalse(rows[reviewed.pk].can_reject)

        self.client.force_login(self.head)
        resp = self.client.get(reverse("payroll_overtime_list"))
        rows = {r.pk: r for r in resp.context["entries"]}
        self.assertTrue(rows[pending.pk].can_approve)
        self.assertTrue(rows[reviewed.pk].can_approve)
        self.assertTrue(resp.context["can_approve_any"])
        self.assertContains(resp, "Approve selected")
        self.assertContains(resp, "Review selected")

        self.client.force_login(self.tl)
        resp = self.client.get(reverse("payroll_overtime_list"))
        self.assertFalse(resp.context["can_decide"])
        self.assertNotContains(resp, "Approve selected")

    def test_awaiting_me_filter_matches_each_stage(self):
        pending = self._entry()
        reviewed = self._entry(status=OvertimeEntry.Status.REVIEWED, work_date=self.today - timedelta(days=1))
        approved = self._entry(status=OvertimeEntry.Status.APPROVED, work_date=self.today - timedelta(days=2))

        self.client.force_login(self.apm)
        pks = {r.pk for r in self.client.get(reverse("payroll_overtime_list"), {"awaiting": "me"}).context["entries"]}
        self.assertEqual(pks, {pending.pk})

        self.client.force_login(self.head)
        pks = {r.pk for r in self.client.get(reverse("payroll_overtime_list"), {"awaiting": "me"}).context["entries"]}
        self.assertEqual(pks, {pending.pk, reviewed.pk})

        self.client.force_login(self.hr_payroll)
        pks = {r.pk for r in self.client.get(reverse("payroll_overtime_list"), {"awaiting": "me"}).context["entries"]}
        self.assertEqual(pks, {pending.pk, reviewed.pk})
        self.assertNotIn(approved.pk, pks)

    # --- readiness ----------------------------------------------------------

    def test_check_overtime_approvers_flags_a_department_with_nobody(self):
        from django.core.management import call_command

        self._entry()
        out = io.StringIO()
        call_command("check_overtime_approvers", stdout=out)
        self.assertIn("Every checked department has an approver", out.getvalue())

        self.department.head_of_department = None
        self.department.save()
        self.department.additional_managers.clear()
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            call_command("check_overtime_approvers", stdout=out)
        self.assertIn("Digital Advertising", out.getvalue())
