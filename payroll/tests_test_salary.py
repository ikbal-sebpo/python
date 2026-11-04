"""Phase D — Data Solutions "Test Salary", a per-day deduction.

Closes PAYROLL_PROCESS.md Q34. The rules under test are the ones confirmed
with the business on 2026-09-19: don't touch trainees, cap at the whole
variable portion, and never touch tax. **Revised 2026-09-21**: the day
count is entered directly instead of derived from a From/To date window —
an employee can fall under test across several disjoint stretches of a
single month, so a single window couldn't represent a period's figure
anyway. One row per employee per period; a re-upload replaces the count.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import Department
from employees.models import JobInfo
from payroll.models import PayComponent, PayrollPeriod, PayRate, VariablePayEntry
from payroll.upload import (
    apply_test_salary_upload, build_test_salary_xlsx_template, parse_test_salary_upload,
)


User = get_user_model()

HEADER = "Employee ID,Days"


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


def _csv(lines):
    return io.BytesIO("\n".join(lines).encode("utf-8"))


class TestSalaryParseTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.employee = _user("a@example.com", "3500", department=self.department)
        self.today = date(2026, 9, 19)

    def _parse(self, lines):
        return parse_test_salary_upload(_csv([HEADER] + lines), "t.csv", today=self.today)

    def test_the_day_count_is_read_directly(self):
        parsed, errors, dups = self._parse(["3500,13"])
        self.assertEqual((errors, dups), ([], []))
        self.assertEqual(parsed[0].days, 13)
        self.assertEqual(parsed[0].employee_id, "3500")

    def test_a_blank_row_is_an_untouched_template_line(self):
        parsed, errors, _ = self._parse(["3500,"])
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [])

    def test_zero_days_is_an_error(self):
        _, errors, _ = self._parse(["3500,0"])
        self.assertEqual(len(errors), 1)
        self.assertIn("between", errors[0].message)

    def test_a_negative_day_count_is_an_error(self):
        _, errors, _ = self._parse(["3500,-2"])
        self.assertEqual(len(errors), 1)

    def test_days_over_the_sanity_cap_is_an_error(self):
        _, errors, _ = self._parse(["3500,32"])
        self.assertEqual(len(errors), 1)
        self.assertIn("between", errors[0].message)

    def test_a_non_whole_day_count_is_an_error(self):
        _, errors, _ = self._parse(["3500,3.5"])
        self.assertEqual(len(errors), 1)

    def test_unknown_employee_is_an_error(self):
        _, errors, _ = self._parse(["9999,5"])
        self.assertEqual(len(errors), 1)

    def test_the_same_employee_twice_is_a_duplicate(self):
        _, _, dups = self._parse(["3500,5", "3500,7"])
        self.assertEqual(len(dups), 1)

    def test_template_has_employee_id_and_days_columns(self):
        import openpyxl

        body = build_test_salary_xlsx_template(self.employee)
        ws = openpyxl.load_workbook(io.BytesIO(body))["Test salary"]
        self.assertEqual([c.value for c in ws[1]], ["Employee ID", "Days"])


class TestSalaryApplyTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.other = Department.objects.create(name="General BPO")
        self.component = PayComponent.objects.create(
            code="TEST_SALARY_DEDUCTION", name="Test salary deduction",
            kind=PayComponent.Kind.DEDUCTION, entry_mode=PayComponent.EntryMode.INPUT,
        )
        PayRate.objects.create(
            rate_type=PayRate.RateType.TEST_SALARY_DAY, component=self.component,
            department=self.department, basis=PayRate.Basis.PER_DAY,
            amount=Decimal("200"), effective_from=date(2026, 1, 1),
        )
        self.period = PayrollPeriod.for_month(2026, 9)   # 16 Aug - 15 Sep
        self.employee = _user("a@example.com", "3500", department=self.department)
        self.uploader = _user("sup@example.com", "1000", department=self.department)
        self.today = date(2026, 9, 19)

    def _apply(self, lines, visible=None):
        parsed, errors, dups = parse_test_salary_upload(
            _csv([HEADER] + lines), "t.csv", today=self.today,
        )
        self.assertEqual((errors, dups), ([], []))
        return apply_test_salary_upload(
            self.uploader, parsed,
            {self.employee.pk} if visible is None else visible,
            period=self.period, component=self.component,
        )

    def test_thirteen_days_deducts_two_thousand_six_hundred(self):
        """The worked example: 13 x 200."""
        report = self._apply(["3500,13"])
        self.assertEqual(len(report.created), 1)
        entry = VariablePayEntry.objects.get()
        self.assertEqual(entry.amount, Decimal("2600.00"))
        self.assertEqual(entry.days, 13)
        self.assertEqual(entry.status, VariablePayEntry.Status.PENDING)

    def test_the_deduction_caps_at_the_whole_variable_portion(self):
        """31 days would be 6,200 — more than the 6,000 portion exists."""
        report = self._apply(["3500,31"])
        self.assertEqual(VariablePayEntry.objects.get().amount, Decimal("6000.00"))
        self.assertTrue(report.created[0].capped)

    def test_a_trainee_row_is_refused(self):
        JobInfo.objects.create(
            employee=self.employee, resource_status=JobInfo.ResourceStatus.TRAINEE,
        )
        report = self._apply(["3500,5"])
        self.assertEqual(len(report.skipped_trainee), 1)
        self.assertFalse(VariablePayEntry.objects.exists())

    def test_no_rate_for_the_department_is_skipped_not_zero(self):
        stranger = _user("s@example.com", "8000", department=self.other)
        report = self._apply(["8000,5"], visible={stranger.pk})
        self.assertEqual(len(report.skipped_no_rate), 1)
        self.assertFalse(VariablePayEntry.objects.exists())

    def test_out_of_scope_rows_are_skipped(self):
        report = self._apply(["3500,5"], visible=set())
        self.assertEqual(len(report.skipped_scope), 1)

    def test_reupload_replaces_the_day_count(self):
        self._apply(["3500,5"])
        self.assertEqual(VariablePayEntry.objects.get().amount, Decimal("1000.00"))
        report = self._apply(["3500,8"])
        self.assertEqual(len(report.updated), 1)
        self.assertEqual(VariablePayEntry.objects.count(), 1)
        entry = VariablePayEntry.objects.get()
        self.assertEqual(entry.days, 8)
        self.assertEqual(entry.amount, Decimal("1600.00"))

    def test_reupload_never_reopens_an_approved_entry(self):
        self._apply(["3500,5"])
        VariablePayEntry.objects.update(status=VariablePayEntry.Status.APPROVED)
        report = self._apply(["3500,8"])
        self.assertEqual(report.total_writes, 0)
        self.assertEqual(len(report.skipped_decided), 1)
        self.assertEqual(VariablePayEntry.objects.get().days, 5)

    def test_two_employees_each_get_their_own_entry_and_cap(self):
        other_employee = _user("b@example.com", "3501", department=self.department)
        report = self._apply(
            ["3500,10", "3501,10"], visible={self.employee.pk, other_employee.pk},
        )
        self.assertEqual(len(report.created), 2)
        self.assertEqual(
            sum(e.amount for e in VariablePayEntry.objects.all()), Decimal("4000.00"),
        )

    def test_the_reason_records_the_arithmetic(self):
        self._apply(["3500,4"])
        reason = VariablePayEntry.objects.get().reason
        self.assertIn("4 day(s) @ 200", reason)


class TestSalaryEngineTests(TestCase):
    """An approved deduction reduces net pay; a pending one does not."""

    def setUp(self):
        from payroll.services import create_run, lock_inputs
        from payroll.models import SalaryStructure

        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.component = PayComponent.objects.create(
            code="TEST_SALARY_DEDUCTION", name="Test salary deduction",
            kind=PayComponent.Kind.DEDUCTION, entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.employee = _user("a@example.com", "3500", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 9)
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("25000"),
            status=SalaryStructure.Status.APPROVED,
        )
        self.run = create_run(self.period, None)
        self._lock = lock_inputs

    def _entry(self, status):
        VariablePayEntry.objects.create(
            employee=self.employee, period=self.period, component=self.component,
            amount=Decimal("2600"), reason="Test salary", source_key="TEST",
            days=13, status=status,
        )

    def _calculate(self):
        from payroll.engine import calculate_run
        from payroll.models import PayrollRunEmployee

        self._lock(self.run, None)
        calculate_run(self.run.pk)
        return PayrollRunEmployee.objects.get(run=self.run, employee=self.employee)

    def test_pending_deduction_does_not_reduce_pay(self):
        self._entry(VariablePayEntry.Status.PENDING)
        row = self._calculate()
        self.assertEqual(row.total_deductions, Decimal("0.00"))
        self.assertEqual(row.net_pay, Decimal("25000.00"))

    def test_approved_deduction_reduces_net_pay(self):
        self._entry(VariablePayEntry.Status.APPROVED)
        row = self._calculate()
        self.assertEqual(row.total_deductions, Decimal("2600.00"))
        self.assertEqual(row.net_pay, Decimal("22400.00"))
        self.assertEqual(row.gross_earnings, Decimal("25000.00"))
