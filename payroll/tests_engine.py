"""Unit tests for payroll.engine / payroll.inputs — the Phase 3 calculation engine."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import AccessLevel, Department, Designation, Team, WorkLocation
from employees.models import DedicatedNightShiftAssignment, JobInfo
from leave.models import LeavePolicy
from payroll import inputs as payroll_inputs
from payroll.engine import calculate_run
from payroll.meals import MealsUnavailable, pull_meal_counts
from payroll.models import (
    MealCount, MealDeductionTier, OvertimeEntry, OvertimeType, PayComponent, PayrollControlCheck,
    PayrollInputBatch, PayrollPeriod, PayrollRun, PayrollRunEmployee, PayrollRunLine, PayrollVariance,
    PayRate, SalaryStructure,
)
from payroll.services import create_run, lock_inputs
from roster.models import Shift, ShiftAttribute, ShiftType


User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


def _shift_types():
    al = ShiftType.objects.create(name="AL", category=ShiftType.Category.LEAVE)
    lwp = ShiftType.objects.create(name="LWP", category=ShiftType.Category.LEAVE)
    hdl = ShiftType.objects.create(name="HDL", category=ShiftType.Category.LEAVE)
    nig = ShiftType.objects.create(
        name="NIG", category=ShiftType.Category.WORK,
        shift_start_time=time(22, 0), shift_end_time=time(7, 0),
    )
    mor = ShiftType.objects.create(
        name="MOR", category=ShiftType.Category.WORK,
        shift_start_time=time(7, 0), shift_end_time=time(16, 0),
    )
    LeavePolicy.objects.create(shift_type=al, is_paid=True)
    LeavePolicy.objects.create(shift_type=lwp, is_paid=False)
    LeavePolicy.objects.create(shift_type=hdl, is_paid=False)
    return {"AL": al, "LWP": lwp, "HDL": hdl, "NIG": nig, "MOR": mor}


class PayableDaysTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Ops")
        self.employee = _user("alice@example.com", "001", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 7)

    def test_eligible_days_full_period(self):
        self.assertEqual(payroll_inputs.eligible_days(self.employee, self.period), 30)

    def test_eligible_days_mid_period_joiner(self):
        self.employee.joining_date = date(2026, 7, 1)
        self.employee.save()
        # period is 2026-06-16..2026-07-15; joining mid-period on 07-01
        self.assertEqual(payroll_inputs.eligible_days(self.employee, self.period), 15)

    def test_eligible_days_mid_period_leaver(self):
        self.employee.last_working_date = date(2026, 7, 5)
        self.employee.save()
        self.assertEqual(payroll_inputs.eligible_days(self.employee, self.period), 20)

    def test_lwp_counts_as_full_day(self):
        types = _shift_types()
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=types["LWP"])
        result = payroll_inputs.collect_lwp_days(self.period, [self.employee.pk])
        self.assertEqual(result[self.employee.pk], Decimal("1"))

    def test_hdl_counts_as_half_day(self):
        types = _shift_types()
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=types["HDL"])
        result = payroll_inputs.collect_lwp_days(self.period, [self.employee.pk])
        self.assertEqual(result[self.employee.pk], Decimal("0.5"))

    def test_paid_leave_does_not_count(self):
        types = _shift_types()
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=types["AL"])
        result = payroll_inputs.collect_lwp_days(self.period, [self.employee.pk])
        self.assertEqual(result.get(self.employee.pk, Decimal("0")), Decimal("0"))

    def test_policy_less_leave_type_treated_as_paid(self):
        abs_type = ShiftType.objects.create(name="ABS", category=ShiftType.Category.LEAVE)
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=abs_type)
        result = payroll_inputs.collect_lwp_days(self.period, [self.employee.pk])
        self.assertEqual(result.get(self.employee.pk, Decimal("0")), Decimal("0"))


class NightShiftDetectionTests(TestCase):
    """A shift is a night shift iff it crosses midnight — verified against
    real NIG/CUST/EVE/MOR roster data before this rule was adopted."""

    def setUp(self):
        self.department = Department.objects.create(name="Ops")
        self.employee = _user("bob@example.com", "002", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 7)
        self.nig = ShiftType.objects.create(
            name="NIG", category=ShiftType.Category.WORK,
            shift_start_time=time(22, 0), shift_end_time=time(7, 0),
        )
        self.eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK,
            shift_start_time=time(14, 0), shift_end_time=time(23, 0),
        )
        self.mor = ShiftType.objects.create(
            name="MOR", category=ShiftType.Category.WORK,
            shift_start_time=time(7, 0), shift_end_time=time(16, 0),
        )

    def test_crosses_midnight_is_night_shift(self):
        self.assertTrue(payroll_inputs.is_night_shift(time(22, 0), time(7, 0)))
        self.assertTrue(payroll_inputs.is_night_shift(time(19, 0), time(4, 0)))

    def test_same_day_shift_is_not_night_shift(self):
        self.assertFalse(payroll_inputs.is_night_shift(time(14, 0), time(23, 0)))
        self.assertFalse(payroll_inputs.is_night_shift(time(7, 0), time(16, 0)))

    def test_collect_night_shift_counts_via_shift_type_times(self):
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=self.nig)
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 21), shift_type=self.eve)
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 22), shift_type=self.mor)
        result = payroll_inputs.classify_night_shifts(self.period, [self.employee.pk])
        self.assertEqual(result[self.employee.pk][payroll_inputs.NIGHT_REGULAR], 1)

    def test_shift_attribute_times_override_shift_type_times(self):
        # A "custom" attribute on a normally-daytime type crossing midnight
        # should still count as a night shift — the attribute wins.
        attr = ShiftAttribute.objects.create(
            shift_type=self.eve, name="EVE late", start_time=time(20, 0), end_time=time(5, 0),
        )
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=self.eve, shift_attribute=attr)
        result = payroll_inputs.classify_night_shifts(self.period, [self.employee.pk])
        self.assertEqual(result[self.employee.pk][payroll_inputs.NIGHT_REGULAR], 1)

    def test_prefilter_keeps_exactly_what_the_per_row_rule_counts(self):
        # Every branch of effective_shift_times, through the DB-side prefilter.
        cust = ShiftType.objects.create(  # daytime times, still counted as CUST
            name="CUST", category=ShiftType.Category.WORK,
            shift_start_time=time(9, 0), shift_end_time=time(18, 0),
        )
        nig_early = ShiftType.objects.create(name="NIG early", category=ShiftType.Category.WORK)
        nig_day_attr = ShiftAttribute.objects.create(  # timed attribute wins → daytime
            shift_type=self.nig, name="NIG day", start_time=time(8, 0), end_time=time(17, 0),
        )
        nig_half_attr = ShiftAttribute.objects.create(  # half-timed → falls back to NIG
            shift_type=self.nig, name="NIG half", start_time=time(21, 0),
        )
        mor_half_attr = ShiftAttribute.objects.create(  # half-timed → falls back to MOR
            shift_type=self.mor, name="MOR half", end_time=time(3, 0),
        )
        rows = [
            (self.nig, None),            # REGULAR
            (self.nig, nig_half_attr),   # REGULAR
            (self.nig, nig_day_attr),    # skipped
            (self.mor, mor_half_attr),   # skipped
            (self.mor, None),            # skipped
            (nig_early, None),           # skipped: no times at all
            (cust, None),                # CUSTOM + TRANSPORT
        ]
        # bulk_create: Shift.save() would re-point shift_type at the attribute's.
        Shift.objects.bulk_create(
            Shift(employee=self.employee, date=date(2026, 6, 20 + i), shift_type=st, shift_attribute=attr)
            for i, (st, attr) in enumerate(rows)
        )

        result = payroll_inputs.classify_night_shifts(self.period, [self.employee.pk])

        self.assertEqual(result[self.employee.pk], {
            payroll_inputs.NIGHT_REGULAR: 2, payroll_inputs.NIGHT_CUSTOM: 1,
            payroll_inputs.NIGHT_DEDICATED: 0, payroll_inputs.NIGHT_PRE_CONFIRMATION: 0, "TRANSPORT": 1,
        })


class CalculateRunTests(TestCase):
    def setUp(self):
        self.software = Department.objects.create(name="Software Development")
        self.employee = _user("carol@example.com", "003", department=self.software)
        JobInfo.objects.create(employee=self.employee, resource_status=JobInfo.ResourceStatus.LIVE)
        self.no_structure_employee = _user("dave@example.com", "004", department=self.software)
        JobInfo.objects.create(employee=self.no_structure_employee)

        self.period = PayrollPeriod.for_month(2026, 7)
        self.gross_component = PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.night_component = PayComponent.objects.create(
            code="NIGHT_ALLOWANCE", name="Night allowance", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("50000"),
            status=SalaryStructure.Status.APPROVED, note="PLACEHOLDER — test fixture",
        )
        PayRate.objects.create(
            rate_type=PayRate.RateType.NIGHT_REGULAR, component=self.night_component,
            department=self.software, max_gross=Decimal("40000"),
            basis=PayRate.Basis.PER_NIGHT, amount=Decimal("350"), effective_from=date(2026, 1, 1),
        )
        PayRate.objects.create(
            rate_type=PayRate.RateType.NIGHT_REGULAR, component=self.night_component,
            department=self.software, min_gross=Decimal("40000"),
            basis=PayRate.Basis.PER_NIGHT, amount=Decimal("450"), effective_from=date(2026, 1, 1),
        )
        nig = ShiftType.objects.create(
            name="NIG", category=ShiftType.Category.WORK,
            shift_start_time=time(22, 0), shift_end_time=time(7, 0),
        )
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 20), shift_type=nig)
        Shift.objects.create(employee=self.employee, date=date(2026, 6, 21), shift_type=nig)

    def _run(self):
        actor = User.objects.create_superuser(email="admin@example.com", username="admin@example.com", password="x")
        run = create_run(self.period, actor)
        run = lock_inputs(run, actor)
        return calculate_run(run.pk, actor.pk)

    def test_calculated_employee_gets_gross_and_night_lines(self):
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        self.assertEqual(re.gross_earnings, Decimal("50900.00"))  # 50000 gross + 2 * 450 (gross >= 40000 band)
        self.assertEqual(re.night_shift_count, 2)
        lines = list(PayrollRunLine.objects.filter(run_employee=re).order_by("sequence"))
        self.assertEqual([l.component_code for l in lines], ["GROSS_SALARY", "NIGHT_ALLOWANCE"])
        self.assertIn(
            "Gross salary is a placeholder value (HR's night-shift threshold indicator, not real pay).",
            re.calc_warnings,
        )

    def test_footing_gross_minus_deductions_equals_net(self):
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        self.assertEqual(re.gross_earnings - re.total_deductions - re.tax_deducted, re.net_pay)
        lines_total = sum(
            (l.amount for l in PayrollRunLine.objects.filter(run_employee=re)), Decimal("0")
        )
        self.assertEqual(lines_total, re.gross_earnings)

    def test_missing_salary_structure_creates_blocking_check_and_stays_in_register(self):
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=self.no_structure_employee)
        self.assertEqual(re.gross_earnings, Decimal("0"))
        self.assertTrue(
            PayrollControlCheck.objects.filter(
                run=run, employee=self.no_structure_employee, check_code="MISSING_SALARY_STRUCTURE",
                severity=PayrollControlCheck.Severity.BLOCKING,
            ).exists()
        )

    def test_recalculate_is_idempotent(self):
        run = self._run()
        first_net = run.net_total
        first_count = PayrollRunEmployee.objects.filter(run=run).count()
        run2 = calculate_run(run.pk, None)
        self.assertEqual(run2.net_total, first_net)
        self.assertEqual(PayrollRunEmployee.objects.filter(run=run).count(), first_count)

    def test_variance_created_with_no_baseline_on_first_run(self):
        run = self._run()
        variance = PayrollVariance.objects.get(run=run, employee=self.employee)
        self.assertIsNone(variance.prev_net)
        self.assertEqual(variance.curr_net, PayrollRunEmployee.objects.get(run=run, employee=self.employee).net_pay)

    def test_lock_inputs_requires_inputs_open(self):
        actor = User.objects.create_superuser(email="admin2@example.com", username="admin2@example.com", password="x")
        run = create_run(self.period, actor)
        run = lock_inputs(run, actor)
        with self.assertRaises(Exception):
            lock_inputs(run, actor)


class MealDeductionEngineTests(TestCase):
    """Meal deduction (Phase 7, 2026-09-09) reads persisted MealCount rows —
    never a live API call inside calculate_run() — see docs/PAYROLL_PLAN.md's
    meals.sebpo.net note."""

    def setUp(self):
        self.department = Department.objects.create(name="Software Development")
        self.employee = _user("erin@example.com", "005", department=self.department)
        JobInfo.objects.create(employee=self.employee, resource_status=JobInfo.ResourceStatus.LIVE)
        self.period = PayrollPeriod.for_month(2026, 7)
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        PayComponent.objects.create(
            code="MEAL_DEDUCTION", name="Meal deduction", kind=PayComponent.Kind.DEDUCTION,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("50000"),
            status=SalaryStructure.Status.APPROVED,
        )
        MealDeductionTier.objects.create(min_gross=Decimal("0"), amount_per_meal=Decimal("30"))

    def _run(self, actor=None):
        actor = actor or User.objects.create_superuser(
            email="admin3@example.com", username="admin3@example.com", password="x",
        )
        run = create_run(self.period, actor)
        run = lock_inputs(run, actor)
        return calculate_run(run.pk, actor.pk)

    def test_no_meal_count_row_means_zero_deduction(self):
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        self.assertEqual(re.total_deductions, Decimal("0"))
        self.assertFalse(PayrollRunLine.objects.filter(run_employee=re, component_code="MEAL_DEDUCTION").exists())

    @patch("payroll.meals.fetch_meal_counts")
    def test_persisted_meal_count_produces_a_deduction_line(self, mock_fetch):
        # lock_inputs() (inside _run()) calls pull_meal_counts, which
        # delete-and-rebuilds MealCount for the period — so the fixture has
        # to go through the real pull (mocked at the HTTP boundary) rather
        # than creating a MealCount row directly, or lock_inputs would wipe it.
        mock_fetch.return_value = {self.employee.employee_id: 10}
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        line = PayrollRunLine.objects.get(run_employee=re, component_code="MEAL_DEDUCTION")
        self.assertEqual(line.amount, Decimal("300.00"))  # 10 meals * 30/meal
        self.assertEqual(re.total_deductions, Decimal("300.00"))
        self.assertEqual(re.gross_earnings - re.total_deductions - re.tax_deducted, re.net_pay)


class MealCountPullTests(TestCase):
    """pull_meal_counts persists MealCount rows under a PayrollInputBatch
    audit envelope and is the one input source lock_inputs lets hard-block
    the INPUTS_LOCKED transition on failure — deliberately unlike the payroll
    report's soft-warn use of the same fetch_meal_counts client."""

    def setUp(self):
        self.department = Department.objects.create(name="Ops")
        self.employee = _user("frank@example.com", "006", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 7)
        self.actor = User.objects.create_superuser(
            email="admin4@example.com", username="admin4@example.com", password="x",
        )

    @patch("payroll.meals.fetch_meal_counts")
    def test_successful_pull_persists_meal_counts_under_an_applied_batch(self, mock_fetch):
        mock_fetch.return_value = {"006": 7}
        batch = pull_meal_counts(self.period, self.actor)
        self.assertEqual(batch.status, PayrollInputBatch.Status.APPLIED)
        self.assertEqual(batch.input_kind, PayrollInputBatch.InputKind.MEAL_COUNT)
        row = MealCount.objects.get(employee=self.employee, period=self.period)
        self.assertEqual(row.total_meals, 7)
        self.assertEqual(row.batch, batch)

    @patch("payroll.meals.fetch_meal_counts")
    def test_failed_pull_leaves_a_failed_batch_and_raises(self, mock_fetch):
        mock_fetch.side_effect = MealsUnavailable("boom")
        with self.assertRaises(ValidationError):
            pull_meal_counts(self.period, self.actor)
        self.assertTrue(
            PayrollInputBatch.objects.filter(
                input_kind=PayrollInputBatch.InputKind.MEAL_COUNT,
                status=PayrollInputBatch.Status.FAILED, period=self.period,
            ).exists()
        )
        self.assertFalse(MealCount.objects.filter(period=self.period).exists())

    @patch("payroll.meals.fetch_meal_counts")
    def test_lock_inputs_blocks_when_the_meal_pull_fails(self, mock_fetch):
        mock_fetch.side_effect = MealsUnavailable("boom")
        run = create_run(self.period, self.actor)
        with self.assertRaises(ValidationError):
            lock_inputs(run, self.actor)
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.Status.INPUTS_OPEN)

    @patch("payroll.meals.fetch_meal_counts")
    def test_relocking_after_reopen_repulls_and_replaces_counts(self, mock_fetch):
        mock_fetch.return_value = {"006": 3}
        run = create_run(self.period, self.actor)
        run = lock_inputs(run, self.actor)
        self.assertEqual(MealCount.objects.get(employee=self.employee, period=self.period).total_meals, 3)

        from payroll.services import reopen_inputs
        run = reopen_inputs(run, self.actor)
        mock_fetch.return_value = {"006": 9}
        lock_inputs(run, self.actor)
        self.assertEqual(MealCount.objects.get(employee=self.employee, period=self.period).total_meals, 9)


class AdOpsPolicyEngineTests(TestCase):
    """Night duty (Regular/Custom/Dedicated), per-department OT basis + the
    Supervisor-tier band, and transport allowance — added 2026-09-07,
    docs/PAYROLL_PLAN.md."""

    def setUp(self):
        self.adops = Department.objects.create(name="AdOps")
        self.period = PayrollPeriod.for_month(2026, 7)

        employee_level = AccessLevel.objects.create(name="Employee", rank=10)
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        self.executive = Designation.objects.create(name="Executive", access_level=employee_level)
        self.team_leader = Designation.objects.create(name="Team Leader", access_level=supervisor_level)

        self.gross_component = PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.night_component = PayComponent.objects.create(
            code="NIGHT_ALLOWANCE", name="Night allowance", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_component = PayComponent.objects.create(
            code="OT_ALLOWANCE", name="Overtime allowance", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.transport_component = PayComponent.objects.create(
            code="SPECIAL_SHIFT_TRANSPORT", name="Special shift transport", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.regular_ot = OvertimeType.objects.create(
            code="regular_ot", name="Regular OT", basis=OvertimeType.Basis.PER_HOUR, component=self.ot_component,
        )

        PayRate.objects.create(
            rate_type=PayRate.RateType.OVERTIME, overtime_type=self.regular_ot, department=self.adops,
            component=self.ot_component, basis=PayRate.Basis.PER_HOUR,
            amount=Decimal("200"), effective_from=date(2026, 1, 1),
        )
        PayRate.objects.create(
            rate_type=PayRate.RateType.OVERTIME, overtime_type=self.regular_ot, department=self.adops,
            min_access_level=supervisor_level, component=self.ot_component, basis=PayRate.Basis.PER_HOUR,
            amount=Decimal("250"), effective_from=date(2026, 1, 1),
        )
        for rate_type, amount in (
            (PayRate.RateType.NIGHT_REGULAR, Decimal("350")),
            (PayRate.RateType.NIGHT_CUSTOM, Decimal("500")),
            (PayRate.RateType.NIGHT_QUARTERLY, Decimal("500")),
        ):
            PayRate.objects.create(
                rate_type=rate_type, department=self.adops, resource_status="",
                component=self.night_component, basis=PayRate.Basis.PER_NIGHT,
                amount=amount, effective_from=date(2026, 1, 1),
            )
            PayRate.objects.create(
                rate_type=rate_type, department=self.adops, resource_status=JobInfo.ResourceStatus.TRAINEE,
                component=self.night_component, basis=PayRate.Basis.PER_NIGHT,
                amount=Decimal("0"), effective_from=date(2026, 1, 1),
            )
        PayRate.objects.create(
            rate_type=PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, department=self.adops,
            component=self.transport_component, basis=PayRate.Basis.PER_DAY,
            amount=Decimal("400"), effective_from=date(2026, 1, 1),
        )

        self.nig = ShiftType.objects.create(
            name="NIG", category=ShiftType.Category.WORK,
            shift_start_time=time(22, 0), shift_end_time=time(7, 0),
        )
        self.cust = ShiftType.objects.create(
            name="CUST", category=ShiftType.Category.WORK,
            shift_start_time=time(19, 0), shift_end_time=time(4, 0),
        )
        self.office = WorkLocation.objects.create(name="Work From Office")
        self.wfh = WorkLocation.objects.create(name="WFH")

    def _employee(self, email, employee_id, designation, resource_status=JobInfo.ResourceStatus.LIVE):
        employee = _user(email, employee_id, department=self.adops, designation=designation)
        JobInfo.objects.create(employee=employee, resource_status=resource_status)
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("30000"),
            status=SalaryStructure.Status.APPROVED,
        )
        return employee

    def _run(self):
        actor = User.objects.create_superuser(email="admin3@example.com", username="admin3@example.com", password="x")
        run = create_run(self.period, actor)
        run = lock_inputs(run, actor)
        return calculate_run(run.pk, actor.pk)

    def _lines(self, run, employee):
        re = PayrollRunEmployee.objects.get(run=run, employee=employee)
        return {l.component_code: l for l in PayrollRunLine.objects.filter(run_employee=re)}

    def test_ot_below_supervisor_is_200_per_hour(self):
        emp = self._employee("exec@example.com", "e01", self.executive)
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=self.regular_ot, seconds=3 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        line = self._lines(run, emp)["OT_ALLOWANCE"]
        self.assertEqual(line.quantity, Decimal("3"))
        self.assertEqual(line.rate, Decimal("200"))
        self.assertEqual(line.amount, Decimal("600.00"))

    def test_ot_supervisor_and_above_is_250_per_hour(self):
        emp = self._employee("tl@example.com", "e02", self.team_leader)
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=self.regular_ot, seconds=2 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        line = self._lines(run, emp)["OT_ALLOWANCE"]
        self.assertEqual(line.rate, Decimal("250"))
        self.assertEqual(line.amount, Decimal("500.00"))

    def test_team_scoped_overtime_rate_wins_over_department_default(self):
        """The bug this feature fixes: a team-scoped PayRate row existed but
        engine.py never passed team= into resolve_rate(), so it was dead
        data. worked_in makes the team resolvable per entry."""
        mars = Team.objects.create(name="MARS", department=self.adops)
        PayRate.objects.create(
            rate_type=PayRate.RateType.OVERTIME, overtime_type=self.regular_ot, department=self.adops,
            team=mars, component=self.ot_component, basis=PayRate.Basis.PER_HOUR,
            amount=Decimal("250"), effective_from=date(2026, 1, 1),
        )
        emp = self._employee("mars@example.com", "e04", self.executive)
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=self.regular_ot, seconds=2 * 3600, worked_in=mars, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        line = self._lines(run, emp)["OT_ALLOWANCE"]
        self.assertEqual(line.rate, Decimal("250"))
        self.assertEqual(line.amount, Decimal("500.00"))

    def test_ot_worked_for_two_teams_in_one_period_gets_two_separately_rated_lines(self):
        mars = Team.objects.create(name="MARS", department=self.adops)
        other_team = Team.objects.create(name="Other", department=self.adops)
        PayRate.objects.create(
            rate_type=PayRate.RateType.OVERTIME, overtime_type=self.regular_ot, department=self.adops,
            team=mars, component=self.ot_component, basis=PayRate.Basis.PER_HOUR,
            amount=Decimal("300"), effective_from=date(2026, 1, 1),
        )
        emp = self._employee("split@example.com", "e05", self.executive)
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=self.regular_ot, seconds=2 * 3600, worked_in=mars, status=OvertimeEntry.Status.APPROVED,
        )
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 21),
            overtime_type=self.regular_ot, seconds=1 * 3600, worked_in=other_team, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=emp)
        ot_lines = sorted(
            PayrollRunLine.objects.filter(run_employee=re, source_kind="OvertimeEntry"),
            key=lambda line: line.rate,
        )
        self.assertEqual(len(ot_lines), 2)
        # other_team has no team-scoped rate of its own, so it falls back to
        # the plain department default (200) exactly like before this field
        # existed; mars gets its own more-specific rate (300).
        self.assertEqual(ot_lines[0].rate, Decimal("200"))
        self.assertEqual(ot_lines[0].amount, Decimal("200.00"))
        self.assertEqual(ot_lines[1].rate, Decimal("300"))
        self.assertEqual(ot_lines[1].amount, Decimal("600.00"))

    def test_regular_night_shift(self):
        emp = self._employee("night@example.com", "e03", self.executive)
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        run = self._run()
        line = self._lines(run, emp)["NIGHT_ALLOWANCE"]
        self.assertEqual(line.amount, Decimal("350.00"))
        self.assertEqual(line.source_ref, PayRate.RateType.NIGHT_REGULAR)

    def test_custom_shift_from_office_gets_night_custom_and_transport(self):
        emp = self._employee("cust@example.com", "e04", self.executive)
        Shift.objects.create(employee=emp, date=date(2026, 6, 21), shift_type=self.cust, work_location=self.office)
        run = self._run()
        lines = self._lines(run, emp)
        self.assertEqual(lines["NIGHT_ALLOWANCE"].amount, Decimal("500.00"))
        self.assertEqual(lines["NIGHT_ALLOWANCE"].source_ref, PayRate.RateType.NIGHT_CUSTOM)
        self.assertEqual(lines["SPECIAL_SHIFT_TRANSPORT"].amount, Decimal("400.00"))

    def test_custom_shift_from_home_gets_no_transport(self):
        emp = self._employee("custwfh@example.com", "e05", self.executive)
        Shift.objects.create(employee=emp, date=date(2026, 6, 21), shift_type=self.cust, work_location=self.wfh)
        run = self._run()
        lines = self._lines(run, emp)
        self.assertNotIn("SPECIAL_SHIFT_TRANSPORT", lines)

    def test_dedicated_assignment_wins_over_regular_night(self):
        emp = self._employee("dedicated@example.com", "e06", self.executive)
        DedicatedNightShiftAssignment.objects.create(
            employee=emp, start_date=date(2026, 6, 18), end_date=date(2026, 6, 25),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        run = self._run()
        line = self._lines(run, emp)["NIGHT_ALLOWANCE"]
        self.assertEqual(line.amount, Decimal("500.00"))
        self.assertEqual(line.source_ref, PayRate.RateType.NIGHT_QUARTERLY)

    def test_pending_dedicated_assignment_does_not_apply(self):
        emp = self._employee("pending@example.com", "e07", self.executive)
        DedicatedNightShiftAssignment.objects.create(
            employee=emp, start_date=date(2026, 6, 18), end_date=date(2026, 6, 25),
            status=DedicatedNightShiftAssignment.Status.PENDING,
        )
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        run = self._run()
        line = self._lines(run, emp)["NIGHT_ALLOWANCE"]
        self.assertEqual(line.source_ref, PayRate.RateType.NIGHT_REGULAR)

    def test_trainee_gets_no_night_pay_but_keeps_transport(self):
        emp = self._employee("trainee@example.com", "e08", self.executive, resource_status=JobInfo.ResourceStatus.TRAINEE)
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        Shift.objects.create(employee=emp, date=date(2026, 6, 21), shift_type=self.cust, work_location=self.office)
        run = self._run()
        lines = self._lines(run, emp)
        self.assertNotIn("NIGHT_ALLOWANCE", lines)
        self.assertEqual(lines["SPECIAL_SHIFT_TRANSPORT"].amount, Decimal("400.00"))

    # --- Night allowance only after confirmation (2026-11-03) --------------
    # Digital Advertising: no night allowance before the confirmation date,
    # per day. The department's TRAINEE 0.00 night-rate rows mark the rule.

    def _confirm(self, emp, **fields):
        type(emp).objects.filter(pk=emp.pk).update(**fields)

    def test_nights_before_confirmation_are_unpaid_and_after_are_paid(self):
        emp = self._employee("mid@example.com", "c01", self.executive, resource_status=JobInfo.ResourceStatus.TRAINEE)
        self._confirm(emp, confirmation_date=date(2026, 6, 25))
        for day in (20, 21, 25, 26):  # two before, two on/after confirmation
            Shift.objects.create(employee=emp, date=date(2026, 6, day), shift_type=self.nig)
        counts = payroll_inputs.classify_night_shifts(self.period, [emp.pk])[emp.pk]
        self.assertEqual(counts[payroll_inputs.NIGHT_PRE_CONFIRMATION], 2)
        self.assertEqual(counts[payroll_inputs.NIGHT_REGULAR], 2)

        run = self._run()
        line = self._lines(run, emp)["NIGHT_ALLOWANCE"]
        self.assertEqual(line.quantity, Decimal("2"))
        self.assertEqual(line.amount, Decimal("700.00"))  # ordinary rate, not the TRAINEE 0.00 row
        re = PayrollRunEmployee.objects.get(run=run, employee=emp)
        self.assertTrue(any("before confirmation" in w for w in re.calc_warnings))

    def test_confirmed_but_still_marked_trainee_is_paid(self):
        emp = self._employee("conf@example.com", "c02", self.executive, resource_status=JobInfo.ResourceStatus.TRAINEE)
        self._confirm(emp, confirmation_date=date(2026, 5, 1))
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        self.assertEqual(self._lines(self._run(), emp)["NIGHT_ALLOWANCE"].amount, Decimal("350.00"))

    def test_not_yet_confirmed_live_employee_is_unpaid(self):
        emp = self._employee("future@example.com", "c03", self.executive)
        self._confirm(emp, confirmation_date=date(2026, 8, 1))
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        Shift.objects.create(employee=emp, date=date(2026, 6, 21), shift_type=self.cust, work_location=self.office)
        lines = self._lines(self._run(), emp)
        self.assertNotIn("NIGHT_ALLOWANCE", lines)
        self.assertEqual(lines["SPECIAL_SHIFT_TRANSPORT"].amount, Decimal("400.00"))  # transport still paid

    def test_no_confirmation_date_falls_back_to_joining_plus_probation(self):
        emp = self._employee("prob@example.com", "c04", self.executive, resource_status=JobInfo.ResourceStatus.TRAINEE)
        self._confirm(emp, joining_date=date(2026, 3, 22), probation_month=3)  # → 22 Jun
        for day in (20, 22):
            Shift.objects.create(employee=emp, date=date(2026, 6, day), shift_type=self.nig)
        counts = payroll_inputs.classify_night_shifts(self.period, [emp.pk])[emp.pk]
        self.assertEqual(counts[payroll_inputs.NIGHT_PRE_CONFIRMATION], 1)
        self.assertEqual(counts[payroll_inputs.NIGHT_REGULAR], 1)

    def test_no_date_at_all_pays_unless_currently_trainee(self):
        live = self._employee("live@example.com", "c05", self.executive)
        trainee = self._employee("tr@example.com", "c06", self.executive, resource_status=JobInfo.ResourceStatus.TRAINEE)
        for emp in (live, trainee):
            Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        counts = payroll_inputs.classify_night_shifts(self.period, [live.pk, trainee.pk])
        self.assertEqual(counts[live.pk][payroll_inputs.NIGHT_REGULAR], 1)
        self.assertEqual(counts[trainee.pk][payroll_inputs.NIGHT_PRE_CONFIRMATION], 1)
        self.assertEqual(counts[trainee.pk][payroll_inputs.NIGHT_REGULAR], 0)

    def test_dedicated_nights_before_confirmation_are_unpaid_too(self):
        emp = self._employee("dedtr@example.com", "c07", self.executive)
        self._confirm(emp, confirmation_date=date(2026, 7, 1))
        DedicatedNightShiftAssignment.objects.create(
            employee=emp, start_date=date(2026, 6, 16), end_date=date(2026, 7, 15),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        Shift.objects.create(employee=emp, date=date(2026, 7, 2), shift_type=self.nig)
        counts = payroll_inputs.classify_night_shifts(self.period, [emp.pk])[emp.pk]
        self.assertEqual(counts[payroll_inputs.NIGHT_PRE_CONFIRMATION], 1)
        self.assertEqual(counts[payroll_inputs.NIGHT_DEDICATED], 1)

    def test_department_without_the_carve_out_is_unchanged(self):
        other = Department.objects.create(name="Data Solutions")
        PayRate.objects.create(
            rate_type=PayRate.RateType.NIGHT_REGULAR, department=other,
            component=self.night_component, basis=PayRate.Basis.PER_NIGHT,
            amount=Decimal("200"), effective_from=date(2026, 1, 1),
        )
        emp = _user("ds@example.com", "c08", department=other, designation=self.executive,
                    confirmation_date=date(2026, 9, 1))
        JobInfo.objects.create(employee=emp, resource_status=JobInfo.ResourceStatus.TRAINEE)
        SalaryStructure.objects.create(
            employee=emp, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("20000"), status=SalaryStructure.Status.APPROVED,
        )
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        counts = payroll_inputs.classify_night_shifts(self.period, [emp.pk])[emp.pk]
        self.assertEqual(counts[payroll_inputs.NIGHT_REGULAR], 1)
        self.assertEqual(counts[payroll_inputs.NIGHT_PRE_CONFIRMATION], 0)
        self.assertEqual(self._lines(self._run(), emp)["NIGHT_ALLOWANCE"].amount, Decimal("200.00"))

    def test_support_info_report_shows_the_unpaid_column(self):
        from payroll.reports import PayrollFilters, build_payroll_report_rows

        emp = self._employee("rep@example.com", "c09", self.executive)
        self._confirm(emp, confirmation_date=date(2026, 6, 25))
        Shift.objects.create(employee=emp, date=date(2026, 6, 20), shift_type=self.nig)
        Shift.objects.create(employee=emp, date=date(2026, 6, 26), shift_type=self.nig)
        filters = PayrollFilters(start_date=self.period.start_date, end_date=self.period.end_date)
        row = build_payroll_report_rows([type(emp).objects.get(pk=emp.pk)], filters, [])["rows"][0]
        self.assertEqual(row["counts"]["NIGHT_UNPAID"], 1)
        self.assertEqual(row["counts"]["NIGHT_REGULAR"], 1)

    # --- Software/Creative gross-derived holiday overtime ------------------
    # Finance confirmed in writing 2026-09-09: gross / 30 * 1.5 per OT day,
    # GH OT only. Reopens PAYROLL_PROCESS.md Q27 (previously "dead formula").

    def _gross_ot_setup(self, gross=Decimal("45000"), multiplier=Decimal("1.5")):
        software = Department.objects.create(name="Software Development")
        holiday_ot = OvertimeType.objects.create(
            code="holiday_ot", name="GH OT",
            basis=OvertimeType.Basis.PER_HOUR, component=self.ot_component,
        )
        PayRate.objects.create(
            rate_type=PayRate.RateType.OVERTIME, overtime_type=holiday_ot, department=software,
            component=self.ot_component, basis=PayRate.Basis.GROSS_30TH_PER_DAY,
            amount=multiplier, effective_from=date(2026, 1, 1),
        )
        emp = _user("swdev@example.com", "s01", department=software, designation=self.executive)
        JobInfo.objects.create(employee=emp, resource_status=JobInfo.ResourceStatus.LIVE)
        SalaryStructure.objects.create(
            employee=emp, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=gross,
            status=SalaryStructure.Status.APPROVED,
        )
        return emp, holiday_ot

    def test_gross_derived_ot_pays_per_day_not_per_hour(self):
        emp, holiday_ot = self._gross_ot_setup()
        # Two holiday OT days of wildly different length — the hours must not matter.
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=holiday_ot, seconds=9 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 21),
            overtime_type=holiday_ot, seconds=2 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        line = self._lines(run, emp)["OT_ALLOWANCE"]
        self.assertEqual(line.unit, "day")
        self.assertEqual(line.quantity, Decimal("2"))
        self.assertEqual(line.rate, Decimal("2250.00"))   # 45000 / 30 * 1.5
        self.assertEqual(line.amount, Decimal("4500.00"))
        self.assertIn("45000", line.calc_note)

    def test_gross_derived_ot_tracks_the_employees_own_gross(self):
        emp, _ = self._gross_ot_setup(gross=Decimal("60000"))
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=OvertimeType.objects.get(code="holiday_ot"), seconds=8 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        line = self._lines(run, emp)["OT_ALLOWANCE"]
        self.assertEqual(line.rate, Decimal("3000.00"))   # 60000 / 30 * 1.5
        self.assertEqual(line.amount, Decimal("3000.00"))

    def test_gross_derived_ot_is_not_prorated_for_a_mid_period_joiner(self):
        """OT is never prorated (PAYROLL_PLAN.md §5 stage 4) — the un-prorated
        structure gross drives the rate even when salary itself is cut."""
        emp, holiday_ot = self._gross_ot_setup()
        emp.joining_date = date(2026, 7, 1)
        emp.save(update_fields=["joining_date"])
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 7, 3),
            overtime_type=holiday_ot, seconds=8 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        lines = self._lines(run, emp)
        self.assertEqual(lines["OT_ALLOWANCE"].rate, Decimal("2250.00"))
        self.assertEqual(lines["OT_ALLOWANCE"].amount, Decimal("2250.00"))
        self.assertLess(lines["GROSS_SALARY"].amount, Decimal("45000"))

    def test_multiplier_is_configurable_without_a_code_change(self):
        emp, holiday_ot = self._gross_ot_setup(multiplier=Decimal("2"))
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=holiday_ot, seconds=8 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        self.assertEqual(self._lines(run, emp)["OT_ALLOWANCE"].rate, Decimal("3000.00"))

    def test_unrated_overtime_type_warns_rather_than_inventing_a_rate(self):
        """Special OT exists for these departments but Finance hasn't supplied
        a figure — it must warn, never fall back to the GH OT formula."""
        emp, _ = self._gross_ot_setup()
        special_ot = OvertimeType.objects.create(
            code="special_ot", name="Special OT",
            basis=OvertimeType.Basis.PER_HOUR, component=self.ot_component,
        )
        OvertimeEntry.objects.create(
            employee=emp, period=self.period, work_date=date(2026, 6, 20),
            overtime_type=special_ot, seconds=8 * 3600, status=OvertimeEntry.Status.APPROVED,
        )
        run = self._run()
        self.assertNotIn("OT_ALLOWANCE", self._lines(run, emp))
        run_employee = PayrollRunEmployee.objects.get(run=run, employee=emp)
        self.assertTrue(
            any("no confirmed rate" in w for w in run_employee.calc_warnings),
            run_employee.calc_warnings,
        )


class SalarySegmentTests(TestCase):
    """Mid-period salary changes — docs/DSDD_PAYROLL_AUTOMATION_PLAN.md §2.1 / Phase A.

    The September 2026 shape: a Data Solutions trainee on 11,000 goes live
    into MARS on 18,000 on the 7th, mid-way through the 16 Aug – 15 Sep
    period. Before segmented proration the engine saw only the APPROVED
    18,000 row (the 11,000 one having been marked SUPERSEDED by
    apply_salary_upload) and paid all 31 days at 18,000.
    """

    def setUp(self):
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.employee = _user(
            "trainee@example.com", "55043", department=self.department,
            joining_date=date(2026, 6, 8),
        )
        self.period = PayrollPeriod.for_month(2026, 9)  # 2026-08-16 .. 2026-09-15
        self.run = create_run(self.period, None)
        self.types = _shift_types()

    def _calculate(self):
        lock_inputs(self.run, None)
        calculate_run(self.run.pk)
        return PayrollRunEmployee.objects.get(run=self.run, employee=self.employee)

    def _gross_lines(self, run_employee):
        return list(
            PayrollRunLine.objects
            .filter(run_employee=run_employee, component_code="GROSS_SALARY")
            .order_by("sequence")
        )

    def _go_live(self):
        """The exact pair of rows apply_salary_upload writes for a go-live."""
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 6, 8), effective_to=date(2026, 9, 6),
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
            status=SalaryStructure.Status.SUPERSEDED,
        )
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 9, 7), effective_to=None,
            revision_no=2, reason=SalaryStructure.Reason.TRANSFER, gross_monthly=Decimal("18000"),
            status=SalaryStructure.Status.APPROVED,
        )

    def test_mid_period_change_splits_into_two_prorated_lines(self):
        self._go_live()
        run_employee = self._calculate()
        lines = self._gross_lines(run_employee)

        self.assertEqual(len(lines), 2)
        # 16 Aug - 6 Sep is 22 days, 7 Sep - 15 Sep is 9 days, period is 31.
        self.assertEqual(lines[0].full_amount, Decimal("11000.00"))
        self.assertEqual(lines[0].amount, Decimal("7806.45"))   # 11000 * 22/31
        self.assertEqual(lines[1].full_amount, Decimal("18000.00"))
        self.assertEqual(lines[1].amount, Decimal("5225.81"))   # 18000 *  9/31
        self.assertEqual(run_employee.gross_earnings, Decimal("13032.26"))
        # The pre-Phase-A engine paid the new rate for the whole period.
        self.assertNotEqual(run_employee.gross_earnings, Decimal("18000.00"))

    def test_each_segment_records_its_own_arithmetic(self):
        self._go_live()
        lines = self._gross_lines(self._calculate())
        self.assertIn("16 Aug", lines[0].calc_note)
        self.assertIn("06 Sep", lines[0].calc_note)
        self.assertIn("11000", lines[0].calc_note)
        self.assertIn("07 Sep", lines[1].calc_note)

    def test_lwp_is_charged_to_the_segment_it_fell_in(self):
        """A day of LWP in the 11,000 half must not be priced at 18,000."""
        self._go_live()
        Shift.objects.create(
            employee=self.employee, date=date(2026, 8, 20), shift_type=self.types["LWP"],
        )
        run_employee = self._calculate()
        lines = self._gross_lines(run_employee)
        self.assertEqual(run_employee.lwp_days, Decimal("1.00"))
        # Employed throughout, so the 31 calendar days scale onto the 30-day
        # basis and the unpaid day costs a full 1/30 (not 1/31).
        self.assertEqual(run_employee.payable_days, Decimal("29.00"))
        self.assertEqual(lines[0].amount, Decimal("7439.78"))   # 11000 * (22*30/31 - 1)/30
        self.assertEqual(lines[1].amount, Decimal("5225.81"))   # 18000 * (9*30/31)/30, untouched

    def test_single_structure_is_unchanged_by_segmentation(self):
        """The regression guard: one structure => one line, same as before."""
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 6, 8), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
            status=SalaryStructure.Status.APPROVED,
        )
        run_employee = self._calculate()
        lines = self._gross_lines(run_employee)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].amount, Decimal("11000.00"))
        self.assertEqual(lines[0].calc_note, "")   # no note when there's nothing to split
        self.assertEqual(run_employee.proration_factor, Decimal("1.000000"))

    def test_draft_and_cancelled_rows_are_not_history(self):
        """Only APPROVED/SUPERSEDED describe what was true — a DRAFT never was."""
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 6, 8), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
            status=SalaryStructure.Status.APPROVED,
        )
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 9, 1), effective_to=None,
            revision_no=2, reason=SalaryStructure.Reason.PROMOTION, gross_monthly=Decimal("99000"),
            status=SalaryStructure.Status.DRAFT,
        )
        lines = self._gross_lines(self._calculate())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].full_amount, Decimal("11000.00"))

    def test_mid_period_joiner_with_a_change_prorates_from_the_joining_date(self):
        self.employee.joining_date = date(2026, 9, 1)
        self.employee.save()
        self._go_live()
        run_employee = self._calculate()
        lines = self._gross_lines(run_employee)
        # Employed 1-15 Sep: 1-6 Sep at 11,000 (6d), 7-15 Sep at 18,000 (9d).
        # A partial period is NOT scaled — each employed day is worth 1/30,
        # so the same 15 days pay the same in a 28- and a 31-day period.
        self.assertEqual(run_employee.payable_days, Decimal("15.00"))
        self.assertEqual(lines[0].amount, Decimal("2200.00"))   # 11000 * 6/30
        self.assertEqual(lines[1].amount, Decimal("5400.00"))   # 18000 * 9/30

    def test_uncovered_days_warn_rather_than_silently_paying_the_wrong_rate(self):
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 9, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("18000"),
            status=SalaryStructure.Status.APPROVED,
        )
        run_employee = self._calculate()
        self.assertTrue(
            any("cover only" in w for w in run_employee.calc_warnings),
            run_employee.calc_warnings,
        )
        # Employed all 31 days, so spans scale onto the 30-day basis: the 15
        # covered days become 14.52 basis days.
        self.assertEqual(run_employee.payable_days, Decimal("14.52"))


class ProrationBasisTests(TestCase):
    """The proration divisor is always 30 — confirmed with HR/Finance
    2026-09-19 (docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md §2.4).

    The 16->15 cycle runs 28 to 31 days, so this is not "divide by 30": a
    full period must pay exactly one month's gross whatever the calendar
    says, while a partial period and unpaid leave are each worth 1/30 a day.
    """

    def setUp(self):
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.department = Department.objects.create(name="Ops")
        self.types = _shift_types()

    def _employee(self, **extra):
        return _user("alice@example.com", "001", department=self.department, **extra)

    def _pay(self, employee, period, gross=Decimal("30000")):
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2020, 1, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=gross,
            status=SalaryStructure.Status.APPROVED,
        )
        run = create_run(period, None)
        lock_inputs(run, None)
        calculate_run(run.pk)
        return PayrollRunEmployee.objects.get(run=run, employee=employee)

    def test_full_31_day_period_pays_exactly_one_month(self):
        period = PayrollPeriod.for_month(2026, 9)   # 16 Aug - 15 Sep, 31 days
        self.assertEqual(period.days_in_period, 31)
        row = self._pay(self._employee(), period)
        self.assertEqual(row.gross_earnings, Decimal("30000.00"))
        self.assertEqual(row.proration_factor, Decimal("1.000000"))

    def test_full_28_day_period_also_pays_exactly_one_month(self):
        """The case a naive eligible/30 gets wrong — Feb 16 to Mar 15 is 28
        days, and 28/30 would dock a full month 6.7%."""
        period = PayrollPeriod.for_month(2026, 3)   # 16 Feb - 15 Mar, 28 days
        self.assertEqual(period.days_in_period, 28)
        row = self._pay(self._employee(), period)
        self.assertEqual(row.gross_earnings, Decimal("30000.00"))
        self.assertEqual(row.proration_factor, Decimal("1.000000"))

    def test_full_30_day_period_pays_exactly_one_month(self):
        period = PayrollPeriod.for_month(2026, 7)   # 16 Jun - 15 Jul, 30 days
        self.assertEqual(period.days_in_period, 30)
        row = self._pay(self._employee(), period)
        self.assertEqual(row.gross_earnings, Decimal("30000.00"))

    def test_one_unpaid_day_costs_a_thirtieth_not_a_thirty_first(self):
        period = PayrollPeriod.for_month(2026, 9)   # 31 days
        employee = self._employee()
        Shift.objects.create(
            employee=employee, date=date(2026, 8, 20), shift_type=self.types["LWP"],
        )
        row = self._pay(employee, period)
        # 30000 x 29/30, not 30000 x 30/31 (= 29032.26)
        self.assertEqual(row.gross_earnings, Decimal("29000.00"))

    def test_an_unpaid_day_costs_the_same_in_a_short_period(self):
        period = PayrollPeriod.for_month(2026, 3)   # 28 days
        employee = self._employee()
        Shift.objects.create(
            employee=employee, date=date(2026, 2, 20), shift_type=self.types["LWP"],
        )
        row = self._pay(employee, period)
        self.assertEqual(row.gross_earnings, Decimal("29000.00"))

    def test_half_day_costs_half_a_thirtieth(self):
        period = PayrollPeriod.for_month(2026, 9)
        employee = self._employee()
        Shift.objects.create(
            employee=employee, date=date(2026, 8, 20), shift_type=self.types["HDL"],
        )
        row = self._pay(employee, period)
        self.assertEqual(row.gross_earnings, Decimal("29500.00"))

    def test_a_joiner_earns_a_thirtieth_per_employed_day(self):
        period = PayrollPeriod.for_month(2026, 9)   # 16 Aug - 15 Sep
        employee = self._employee(joining_date=date(2026, 9, 1))  # 15 days employed
        row = self._pay(employee, period)
        self.assertEqual(row.gross_earnings, Decimal("15000.00"))   # 30000 x 15/30

    def test_the_same_partial_days_pay_the_same_in_a_short_period(self):
        """15 employed days is 15 employed days — it must not be worth more
        in March than in September just because March's period is shorter."""
        period = PayrollPeriod.for_month(2026, 3)   # 16 Feb - 15 Mar, 28 days
        employee = self._employee(joining_date=date(2026, 3, 1))  # 15 days employed
        row = self._pay(employee, period)
        self.assertEqual(row.gross_earnings, Decimal("15000.00"))

    def test_mid_period_change_in_a_31_day_period_uses_the_30_day_basis(self):
        """The Data Solutions go-live, repriced on the confirmed divisor."""
        period = PayrollPeriod.for_month(2026, 9)
        employee = self._employee(joining_date=date(2026, 6, 8))
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2026, 6, 8), effective_to=date(2026, 9, 6),
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
            status=SalaryStructure.Status.SUPERSEDED,
        )
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2026, 9, 7), effective_to=None,
            revision_no=2, reason=SalaryStructure.Reason.TRANSFER, gross_monthly=Decimal("18000"),
            status=SalaryStructure.Status.APPROVED,
        )
        run = create_run(period, None)
        lock_inputs(run, None)
        calculate_run(run.pk)
        row = PayrollRunEmployee.objects.get(run=run, employee=employee)
        # Employed throughout, so the 31 calendar days scale onto 30 basis
        # days: 22 -> 21.290323, 9 -> 8.709677. Neither segment is a whole
        # number of days any more, which is the honest consequence of paying
        # a 31-day period as one 30-day month.
        self.assertEqual(row.proration_factor, Decimal("1.000000"))
        self.assertEqual(row.gross_earnings, Decimal("13032.26"))
