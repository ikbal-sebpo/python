"""Phase C — SLA achievement upload + the variable-pay approval gate.

docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase C. The two things worth pinning
down here are (1) the supervisor never types an amount — the rate resolves
at apply time — and (2) approval is the *department head*, not HR, which is
the one payroll gate that diverges from overtime's Q55 shape.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from accounts.models import Department, Team
from accounts.permissions import can_act_on_variable_pay
from employees.models import JobInfo
from payroll.models import (
    PayComponent, PayrollPeriod, PayRate, VariablePayEntry,
)
from payroll.upload import apply_sla_upload, build_sla_xlsx_template, parse_sla_upload


User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


def _csv(lines: list[str]) -> io.BytesIO:
    return io.BytesIO("\n".join(lines).encode("utf-8"))


HEADER = "Employee ID,SLA month"


class SlaParseTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.alice = _user("alice@example.com", "2737", department=self.department)
        self.bob = _user("bob@example.com", "2738", department=self.department)
        self.today = date(2026, 9, 19)

    def _parse(self, lines):
        return parse_sla_upload(_csv([HEADER] + lines), "sla.csv", today=self.today)

    def test_listed_rows_are_returned(self):
        """No 'Met SLA' column (removed 2026-09-21) — being listed IS the claim."""
        parsed, errors, dups = self._parse(["2737,2026-08", "2738,2026-08"])
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].sla_month, date(2026, 8, 1))
        self.assertEqual(parsed[0].source_key, "SLA:2026-08")

    def test_a_blank_month_is_an_untouched_template_line(self):
        """The normal case: a supervisor fills in the month for the few who
        qualified and leaves the rest of the prefilled template alone."""
        parsed, errors, dups = self._parse(["2737,2026-08", "2738,"])
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].employee_id, "2737")

    def test_month_name_form_is_accepted(self):
        """Supervisors copy this out of the old free-text note format,
        which wrote it as 'Meet SLA-August, 2026'."""
        parsed, errors, _ = self._parse(["2737,August 2026"])
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].sla_month, date(2026, 8, 1))

    def test_future_month_is_an_error(self):
        _, errors, _ = self._parse(["2737,2026-12"])
        self.assertEqual(len(errors), 1)
        self.assertIn("future", errors[0].message)

    def test_unknown_employee_is_an_error(self):
        _, errors, _ = self._parse(["9999,2026-08"])
        self.assertEqual(len(errors), 1)
        self.assertIn("9999", errors[0].message)

    def test_same_employee_same_month_twice_is_a_duplicate(self):
        _, _, dups = self._parse(["2737,2026-08", "2737,2026-08"])
        self.assertEqual(len(dups), 1)

    def test_same_employee_different_months_is_the_retro_case_not_a_duplicate(self):
        """Employee 3503 claimed July AND August in one September note."""
        parsed, errors, dups = self._parse(["2737,2026-07", "2737,2026-08"])
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 2)
        self.assertEqual({r.source_key for r in parsed}, {"SLA:2026-07", "SLA:2026-08"})

    def test_a_bad_month_value_is_an_error(self):
        """Unlike the old Y/N-gated design, a non-blank row always means the
        supervisor intends to claim it, so a bad value can't be silently ignored."""
        _, errors, _ = self._parse(["2737,not-a-month"])
        self.assertEqual(len(errors), 1)


class SlaApplyTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.other_department = Department.objects.create(name="General BPO")
        self.component = PayComponent.objects.create(
            code="SLA_INCENTIVE", name="SLA achievement incentive", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.period = PayrollPeriod.for_month(2026, 9)
        PayRate.objects.create(
            rate_type=PayRate.RateType.SLA_INCENTIVE, component=self.component,
            department=self.department, basis=PayRate.Basis.PER_OCCURRENCE,
            amount=Decimal("3000"), effective_from=date(2026, 1, 1),
        )
        self.alice = _user("alice@example.com", "2737", department=self.department)
        self.uploader = _user("sup@example.com", "1000", department=self.department)
        self.today = date(2026, 9, 19)

    def _apply(self, lines, visible=None):
        parsed, errors, dups = parse_sla_upload(
            _csv([HEADER] + lines), "sla.csv", today=self.today,
        )
        self.assertEqual((errors, dups), ([], []))
        return apply_sla_upload(
            self.uploader, parsed,
            {self.alice.pk} if visible is None else visible,
            period=self.period, component=self.component,
        )

    def test_amount_comes_from_the_rate_not_the_sheet(self):
        report = self._apply(["2737,2026-08"])
        self.assertEqual(len(report.created), 1)
        entry = VariablePayEntry.objects.get()
        self.assertEqual(entry.amount, Decimal("3000.00"))
        self.assertEqual(entry.source_key, "SLA:2026-08")
        self.assertEqual(entry.reason, "SLA achieved — August 2026")

    def test_rows_land_pending_never_approved(self):
        self._apply(["2737,2026-08"])
        self.assertEqual(VariablePayEntry.objects.get().status, VariablePayEntry.Status.PENDING)

    def test_no_configured_rate_is_skipped_not_written_as_zero(self):
        charlie = _user("charlie@example.com", "5000", department=self.other_department)
        report = self._apply(["5000,2026-08"], visible={charlie.pk})
        self.assertEqual(report.created, [])
        self.assertEqual(len(report.skipped_no_rate), 1)
        self.assertFalse(VariablePayEntry.objects.exists())

    def test_out_of_scope_rows_are_skipped(self):
        report = self._apply(["2737,2026-08"], visible=set())
        self.assertEqual(len(report.skipped_scope), 1)
        self.assertFalse(VariablePayEntry.objects.exists())

    def test_reupload_updates_rather_than_duplicating(self):
        self._apply(["2737,2026-08"])
        report = self._apply(["2737,2026-08"])
        self.assertEqual(len(report.updated), 1)
        self.assertEqual(VariablePayEntry.objects.count(), 1)

    def test_reupload_never_reopens_an_approved_row(self):
        self._apply(["2737,2026-08"])
        VariablePayEntry.objects.update(status=VariablePayEntry.Status.APPROVED)
        report = self._apply(["2737,2026-08"])
        self.assertEqual(report.total_writes, 0)
        self.assertEqual(len(report.skipped_decided), 1)
        self.assertEqual(VariablePayEntry.objects.get().status, VariablePayEntry.Status.APPROVED)

    def test_a_retro_month_coexists_with_the_current_one(self):
        report = self._apply(["2737,2026-07", "2737,2026-08"])
        self.assertEqual(len(report.created), 2)
        self.assertEqual(
            set(VariablePayEntry.objects.values_list("source_key", flat=True)),
            {"SLA:2026-07", "SLA:2026-08"},
        )

    def test_template_lists_scope_and_has_no_amount_or_met_sla_column(self):
        import openpyxl

        body = build_sla_xlsx_template(self.uploader, default_month=date(2026, 8, 1))
        ws = openpyxl.load_workbook(io.BytesIO(body))["SLA achievement"]
        headers = [c.value for c in ws[1]]
        self.assertEqual(headers[:2], ["Employee ID", "SLA month"])
        self.assertNotIn("Amount", headers)
        self.assertNotIn("Met SLA", headers)

    def test_template_leaves_the_sla_month_blank_for_every_row(self):
        """Without a Met SLA column, a pre-filled month would mean an
        unedited download-then-upload pays every managed employee by
        accident. The blank cell is the safety net now."""
        import openpyxl

        body = build_sla_xlsx_template(self.uploader, default_month=date(2026, 8, 1))
        ws = openpyxl.load_workbook(io.BytesIO(body))["SLA achievement"]
        month_values = [row[0].value for row in ws.iter_rows(min_row=2, min_col=2, max_col=2)]
        self.assertTrue(month_values)
        self.assertTrue(all(v is None for v in month_values))


class VariablePayApprovalGateTests(TestCase):
    """The department head approves — deliberately not HR, and not every
    manager in the reporting line."""

    def setUp(self):
        self.head = _user("head@example.com", "100")
        self.department = Department.objects.create(
            name="Data solution and Due Diligence", head_of_department=self.head,
        )
        self.head.department = self.department
        self.head.save()
        self.team = Team.objects.create(name="MARS", department=self.department)
        self.employee = _user(
            "alice@example.com", "2737", department=self.department, team=self.team,
        )
        self.supervisor = _user("sup@example.com", "101", department=self.department)
        JobInfo.objects.create(employee=self.employee, reports_to=self.supervisor)

    def test_department_head_may_act(self):
        self.assertTrue(can_act_on_variable_pay(self.head, self.employee))

    def test_the_employees_own_supervisor_may_not(self):
        """Managed scope is deliberately NOT the rule here — that would let
        any team leader sign off their own team's money."""
        self.assertFalse(can_act_on_variable_pay(self.supervisor, self.employee))

    def test_hr_may_not(self):
        hr = _user("hr@example.com", "900")
        hr_group, _ = Group.objects.get_or_create(name="HR")
        hr.groups.add(hr_group)
        self.assertFalse(can_act_on_variable_pay(hr, self.employee))

    def test_head_cannot_approve_their_own_row_but_their_manager_can(self):
        boss = _user("boss@example.com", "1")
        JobInfo.objects.create(employee=self.head, reports_to=boss)
        self.assertFalse(can_act_on_variable_pay(self.head, self.head))
        self.assertTrue(can_act_on_variable_pay(boss, self.head))

    def test_no_department_head_means_nobody_ordinary_can_act(self):
        orphan_dept = Department.objects.create(name="Unmanaged")
        orphan = _user("orphan@example.com", "800", department=orphan_dept)
        self.assertFalse(can_act_on_variable_pay(self.head, orphan))

    def test_superuser_is_the_break_glass_path(self):
        root = _user("root@example.com", "999", is_superuser=True, is_staff=True)
        self.assertTrue(can_act_on_variable_pay(root, self.employee))


class SlaEnginePaymentTests(TestCase):
    """An approved SLA row is paid; a pending one is not — with no engine
    change, since calculate_run already filters variable pay to APPROVED."""

    def setUp(self):
        from payroll.services import create_run, lock_inputs

        self.gross = PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.sla = PayComponent.objects.create(
            code="SLA_INCENTIVE", name="SLA achievement incentive", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.employee = _user("alice@example.com", "2737", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 9)
        from payroll.models import SalaryStructure

        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("27000"),
            status=SalaryStructure.Status.APPROVED,
        )
        self.run = create_run(self.period, None)
        self._lock = lock_inputs

    def _calculate(self):
        from payroll.engine import calculate_run
        from payroll.models import PayrollRunEmployee

        self._lock(self.run, None)
        calculate_run(self.run.pk)
        return PayrollRunEmployee.objects.get(run=self.run, employee=self.employee)

    def _entry(self, status):
        VariablePayEntry.objects.create(
            employee=self.employee, period=self.period, component=self.sla,
            amount=Decimal("3000"), reason="SLA achieved — August 2026",
            source_key="SLA:2026-08", status=status,
        )

    def test_pending_sla_is_not_paid(self):
        self._entry(VariablePayEntry.Status.PENDING)
        self.assertEqual(self._calculate().gross_earnings, Decimal("27000.00"))

    def test_approved_sla_is_paid(self):
        self._entry(VariablePayEntry.Status.APPROVED)
        self.assertEqual(self._calculate().gross_earnings, Decimal("30000.00"))
