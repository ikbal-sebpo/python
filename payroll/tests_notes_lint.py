"""Phase E — reconciling free-text notes against structured inputs.

docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase E. The detector matches *money*,
not phrases, so it survives the uploaders that made those phrases obsolete.
The hard part is not matching dates: September 2026's notes are full of
"09/09/2026" and "30 August 2026".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import Department
from payroll.engine import calculate_run
from payroll.models import (
    PayComponent, PayrollControlCheck, PayrollPeriod, PayrollRunEmployeeNote,
    SalaryStructure, VariablePayEntry,
)
from payroll.notes_lint import extract_amounts, lint_run_notes
from payroll.services import create_run, lock_inputs


User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


class MoneyDetectionTests(TestCase):
    """Verbatim strings from September 2026's Supervisor Note column."""

    def test_finds_the_sla_figure(self):
        note = ("Meet SLA-August, 2026: Recommended for the variable part (3000 Taka) "
                "for achieving SLA targets.")
        self.assertEqual(extract_amounts(note), [Decimal("3000")])

    def test_finds_a_go_live_package_with_a_thousands_separator(self):
        note = ("Go live date, Salary as per the new package for MARS (TK. 18,000): "
                "Date: September 07, 2026")
        self.assertEqual(extract_amounts(note), [Decimal("18000")])

    def test_ignores_a_resignation_note_full_of_dates(self):
        note = "Date of Resignation: 09/09/2026, Last Working Day: 10/12/2026"
        self.assertEqual(extract_amounts(note), [])

    def test_ignores_a_suspension_note_with_a_day_and_a_year(self):
        note = ("Temporary Suspension: Placed on temporary suspension on 30 August 2026 "
                "due to performance, attendance, and punctuality issues.")
        self.assertEqual(extract_amounts(note), [])

    def test_a_test_salary_note_names_no_amount_so_is_not_flagged(self):
        """The documented limitation — see notes_lint's module docstring."""
        note = "13 days Test Salary: From August 26 to September 07, 2026"
        self.assertEqual(extract_amounts(note), [])

    def test_various_currency_spellings(self):
        for text, expected in (
            ("bonus of Tk 2500 this month", Decimal("2500")),
            # Regression: an unparenthesised alternation truncated this to 110.
            ("Tk 11000 package", Decimal("11000")),
            ("৳400 transport", Decimal("400")),
            ("BDT 1,250.50 adjustment", Decimal("1250.50")),
            ("deduct 750tk", Decimal("750")),
        ):
            self.assertEqual(extract_amounts(text), [expected], text)

    def test_a_bare_number_is_never_money(self):
        self.assertEqual(extract_amounts("worked 18000 seconds"), [])

    def test_small_figures_are_ignored(self):
        """'3 Taka' is a typo or a day count, not a payment worth chasing."""
        self.assertEqual(extract_amounts("about 3 taka"), [])

    def test_repeated_amounts_are_reported_once(self):
        note = "3000 Taka for July and 3000 Taka for August"
        self.assertEqual(extract_amounts(note), [Decimal("3000")])

    def test_two_different_amounts_are_both_reported(self):
        note = "SLA 3000 Taka plus a TK. 500 adjustment"
        self.assertEqual(extract_amounts(note), [Decimal("3000"), Decimal("500")])


class NoteReconciliationTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.employee = _user("a@example.com", "2737", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 9)
        self.run = create_run(self.period, None)
        self.component = PayComponent.objects.create(
            code="SLA_INCENTIVE", name="SLA achievement incentive",
            kind=PayComponent.Kind.EARNING, entry_mode=PayComponent.EntryMode.INPUT,
        )

    def _note(self, text, role=PayrollRunEmployeeNote.Role.SUPERVISOR):
        return PayrollRunEmployeeNote.objects.create(
            run=self.run, employee=self.employee, role=role, note=text,
        )

    def _variable_pay(self, amount, status=VariablePayEntry.Status.APPROVED):
        return VariablePayEntry.objects.create(
            employee=self.employee, period=self.period, component=self.component,
            amount=amount, reason="x", status=status,
        )

    def test_an_unbacked_amount_is_flagged(self):
        self._note("Recommended for the variable part (3000 Taka) for achieving SLA targets.")
        findings = lint_run_notes(self.run)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["amount"], Decimal("3000"))
        self.assertEqual(findings[0]["employee"], self.employee)

    def test_a_matching_variable_pay_entry_accounts_for_it(self):
        self._note("Recommended for the variable part (3000 Taka).")
        self._variable_pay(Decimal("3000"))
        self.assertEqual(lint_run_notes(self.run), [])

    def test_a_pending_entry_still_accounts_for_it(self):
        """It's structured and visible on the queue — not an orphaned note."""
        self._note("Recommended for the variable part (3000 Taka).")
        self._variable_pay(Decimal("3000"), status=VariablePayEntry.Status.PENDING)
        self.assertEqual(lint_run_notes(self.run), [])

    def test_a_rejected_entry_does_not_account_for_it(self):
        self._note("Recommended for the variable part (3000 Taka).")
        self._variable_pay(Decimal("3000"), status=VariablePayEntry.Status.REJECTED)
        self.assertEqual(len(lint_run_notes(self.run)), 1)

    def test_a_different_amount_does_not_account_for_it(self):
        """Matching on the amount, not merely 'has some input' — an employee
        with unrelated variable pay and an unstructured SLA note is the case
        this exists to catch."""
        self._note("Recommended for the variable part (3000 Taka).")
        self._variable_pay(Decimal("2600"))
        findings = lint_run_notes(self.run)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["amount"], Decimal("3000"))

    def test_a_go_live_note_is_accounted_for_by_the_salary_structure(self):
        self._note("Salary as per the new package for MARS (TK. 18,000): Date: September 07, 2026")
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 9, 7), effective_to=None,
            revision_no=2, reason=SalaryStructure.Reason.TRANSFER,
            gross_monthly=Decimal("18000"), status=SalaryStructure.Status.APPROVED,
        )
        self.assertEqual(lint_run_notes(self.run), [])

    def test_a_superseded_structure_also_accounts_for_it(self):
        self._note("Was on TK. 11,000 before go live")
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 6, 8), effective_to=date(2026, 9, 6),
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("11000"), status=SalaryStructure.Status.SUPERSEDED,
        )
        self.assertEqual(lint_run_notes(self.run), [])

    def test_hr_and_finance_notes_are_linted_too(self):
        self._note("paid 3000 Taka separately", role=PayrollRunEmployeeNote.Role.HR)
        self._note("adjust by TK. 500", role=PayrollRunEmployeeNote.Role.FINANCE)
        roles = {f["role"] for f in lint_run_notes(self.run)}
        self.assertEqual(roles, {"HR", "Finance"})

    def test_a_note_with_no_money_is_never_flagged(self):
        self._note("Date of Resignation: 09/09/2026, Last Working Day: 10/12/2026")
        self.assertEqual(lint_run_notes(self.run), [])

    def test_no_notes_means_no_queries_wasted(self):
        self.assertEqual(lint_run_notes(self.run), [])


class NoteLintControlCheckTests(TestCase):
    """The finding reaches the run as a WARNING, and clears when the input
    it was asking for is actually filed."""

    def setUp(self):
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.component = PayComponent.objects.create(
            code="SLA_INCENTIVE", name="SLA achievement incentive",
            kind=PayComponent.Kind.EARNING, entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.employee = _user("a@example.com", "2737", department=self.department)
        self.period = PayrollPeriod.for_month(2026, 9)
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("27000"), status=SalaryStructure.Status.APPROVED,
        )
        self.run = create_run(self.period, None)
        PayrollRunEmployeeNote.objects.create(
            run=self.run, employee=self.employee,
            role=PayrollRunEmployeeNote.Role.SUPERVISOR,
            note="Meet SLA-August, 2026: Recommended for the variable part (3000 Taka).",
        )

    def _calculate(self):
        if self.run.status != "INPUTS_LOCKED":
            lock_inputs(self.run, None)
        calculate_run(self.run.pk)
        return PayrollControlCheck.objects.filter(
            run=self.run, check_code="UNSTRUCTURED_NOTE_AMOUNT",
        )

    def test_the_run_warns_about_the_unstructured_amount(self):
        checks = self._calculate()
        self.assertEqual(checks.count(), 1)
        check = checks.get()
        self.assertEqual(check.severity, PayrollControlCheck.Severity.WARNING)
        self.assertEqual(check.employee, self.employee)
        self.assertIn("3000", check.message)

    def test_filing_the_real_input_clears_it_on_recalculation(self):
        self._calculate()
        VariablePayEntry.objects.create(
            employee=self.employee, period=self.period, component=self.component,
            amount=Decimal("3000"), reason="SLA achieved — August 2026",
            status=VariablePayEntry.Status.APPROVED,
        )
        self.assertEqual(self._calculate().count(), 0)

    def test_the_note_check_is_never_blocking(self):
        """Scoped to this check_code on purpose — the run legitimately raises
        other BLOCKING checks (no TaxYear is configured in this fixture)."""
        self._calculate()
        self.assertFalse(
            PayrollControlCheck.objects
            .filter(
                run=self.run, check_code="UNSTRUCTURED_NOTE_AMOUNT",
                severity=PayrollControlCheck.Severity.BLOCKING,
            )
            .exists()
        )
