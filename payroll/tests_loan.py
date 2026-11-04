"""Salary loans — docs/SALARY_LOAN_PLAN.md.

The reference case is Finance's file note HR/21092026/01: BDT 50,000
repaid in 10 monthly instalments of 5,000 by payroll deduction.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from accounts.models import Department
from dynamic_menu.models import ProtectedView
from employees.models import JobInfo
from payroll.engine import calculate_run
from payroll.loans import LoanBalance, decide_instalment, loan_balances, loan_history
from payroll.models import (
    EmployeeTaxDeclaration, PayComponent, PayrollControlCheck, PayrollInputBatch, PayrollPeriod, PayrollRun,
    PayrollRunEmployee, PayrollRunLine, SalaryLoan, SalaryLoanAdjustment, SalaryLoanSkip, SalaryStructure,
    TaxYear, VariablePayEntry,
)
from payroll.services import create_run, lock_inputs
from payroll.tests_tax import _seed_tax_year, _user
from payroll.views import _extra_deduction_components

User = get_user_model()
_MEDIA_ROOT = tempfile.mkdtemp(prefix="orbis-loan-tests-")
PDF = b"%PDF-1.4 test"


def _pdf(name="note.pdf"):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


def _loan(employee, *, principal="50000", count=10, instalment="5000", first=(2026, 10), opening="0",
          status=SalaryLoan.Status.ACTIVE, ref="HR/21092026/01", disbursed_on=date(2026, 9, 25)):
    return SalaryLoan.objects.create(
        employee=employee, file_reference=ref, approved_on=date(2026, 9, 21), approval_document=_pdf(),
        principal=Decimal(principal), installment_count=count, installment_amount=Decimal(instalment),
        first_deduction_period=PayrollPeriod.for_month(*first) if first else None,
        opening_recovered=Decimal(opening), status=status,
        disbursed_on=disbursed_on if status == SalaryLoan.Status.ACTIVE else None,
    )


class FormulaTests(TestCase):
    def test_default_instalment_rounds_up_to_the_taka(self):
        self.assertEqual(SalaryLoan.default_installment(Decimal("50000"), 10), Decimal("5000"))
        self.assertEqual(SalaryLoan.default_installment(Decimal("50000"), 12), Decimal("4167"))
        self.assertEqual(SalaryLoan.default_installment(Decimal("50000"), 0), Decimal("0"))

    def test_normal_instalment_is_the_agreed_amount(self):
        d = decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("35000"), available_net=Decimal("90000"))
        self.assertEqual(d.amount, Decimal("5000"))
        self.assertEqual(d.warning_code, "")

    def test_last_instalment_is_the_remainder(self):
        d = decide_instalment(installment_amount=Decimal("4167"), outstanding=Decimal("4163"), available_net=Decimal("90000"))
        self.assertEqual(d.amount, Decimal("4163"))

    def test_reduced_to_available_net_and_carried_forward(self):
        d = decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("35000"), available_net=Decimal("1200"))
        self.assertEqual(d.amount, Decimal("1200"))
        self.assertEqual(d.warning_code, "LOAN_INSTALMENT_REDUCED")
        self.assertEqual(d.shortfall, Decimal("3800"))
        zero = decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("35000"), available_net=Decimal("-10"))
        self.assertEqual(zero.amount, Decimal("0"))
        self.assertEqual(zero.warning_code, "LOAN_INSTALMENT_REDUCED")

    def test_skip_deducts_nothing_and_warns(self):
        d = decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("35000"), available_net=Decimal("90000"), skipped=True)
        self.assertEqual(d.amount, Decimal("0"))
        self.assertEqual(d.warning_code, "LOAN_INSTALMENT_SKIPPED")

    def test_leaver_takes_the_whole_balance(self):
        d = decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("35000"), available_net=Decimal("90000"), leaving=True)
        self.assertEqual(d.amount, Decimal("35000"))
        self.assertTrue(d.full_balance)
        short = decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("35000"), available_net=Decimal("20000"), leaving=True)
        self.assertEqual(short.amount, Decimal("20000"))
        self.assertEqual(short.warning_code, "LOAN_OUTSTANDING_AT_SEPARATION")
        self.assertEqual(short.shortfall, Decimal("15000"))

    def test_settled_loan_deducts_nothing(self):
        self.assertEqual(decide_instalment(installment_amount=Decimal("5000"), outstanding=Decimal("0"), available_net=Decimal("1")).amount, 0)

    def test_balance_derivation_with_opening_and_adjustments(self):
        emp = _user("bal@example.com", "800")
        loan = _loan(emp, opening="15000")
        SalaryLoanAdjustment.objects.create(loan=loan, delta=Decimal("5000"), reason=SalaryLoanAdjustment.Reason.PREPAYMENT)
        SalaryLoanAdjustment.objects.create(loan=loan, delta=Decimal("-1000"), reason=SalaryLoanAdjustment.Reason.CORRECTION)
        b = loan_balances([loan])[loan.pk]
        self.assertEqual(b.recovered, Decimal("19000"))
        self.assertEqual(b.outstanding, Decimal("31000"))
        self.assertEqual(b.instalments_deducted, 3)   # 15,000 / 5,000 before Orbis
        self.assertFalse(b.is_settled)
        self.assertEqual(b.next_instalment, Decimal("5000"))
        waived = SalaryLoanAdjustment.objects.create(loan=loan, delta=Decimal("31000"), reason=SalaryLoanAdjustment.Reason.WAIVER)
        b = loan_balances([loan])[loan.pk]
        self.assertTrue(b.is_settled)
        self.assertEqual(b.outstanding, Decimal("0"))
        kinds = [h.kind for h in loan_history(loan)]
        self.assertEqual(kinds.count("adjustment"), 3)
        self.assertIn("opening", kinds)
        self.assertIsNotNone(waived.pk)


class ModelTests(TestCase):
    def setUp(self):
        self.emp = _user("model@example.com", "810")
        self.admin = _user("fin@example.com", "811")

    def test_disbursement_defaults_first_deduction_to_the_payroll_after_the_disbursement_month(self):
        # Q71 refined 2026-10-08: month M's payroll (16/M-1 → 15/M) is the salary
        # already being paid out when the money goes out in M, so the first
        # instalment comes from M+1's payroll — whatever day of M it was.
        loan = _loan(self.emp, status=SalaryLoan.Status.PENDING_DISBURSEMENT, first=None)
        loan.mark_disbursed(self.admin, date(2026, 3, 3))
        self.assertEqual((loan.first_deduction_period.year, loan.first_deduction_period.month), (2026, 4))  # Mar 16 – Apr 15
        loan1 = _loan(self.emp, status=SalaryLoan.Status.PENDING_DISBURSEMENT, first=None, ref="HR/1")
        loan1.mark_disbursed(self.admin, date(2026, 9, 14))
        self.assertEqual((loan1.first_deduction_period.year, loan1.first_deduction_period.month), (2026, 10))
        loan2 = _loan(self.emp, status=SalaryLoan.Status.PENDING_DISBURSEMENT, first=None, ref="HR/2")
        loan2.mark_disbursed(self.admin, date(2026, 9, 16))
        self.assertEqual((loan2.first_deduction_period.year, loan2.first_deduction_period.month), (2026, 10))
        loan3 = _loan(self.emp, status=SalaryLoan.Status.PENDING_DISBURSEMENT, first=None, ref="HR/3")
        loan3.mark_disbursed(self.admin, date(2026, 12, 20))
        self.assertEqual((loan3.first_deduction_period.year, loan3.first_deduction_period.month), (2027, 1))
        self.assertEqual(loan2.status, SalaryLoan.Status.ACTIVE)
        self.assertEqual(loan2.disbursed_by, self.admin)

    def test_hr_pick_wins_over_the_default(self):
        loan = _loan(self.emp, status=SalaryLoan.Status.PENDING_DISBURSEMENT, first=(2026, 12))
        loan.mark_disbursed(self.admin, date(2026, 9, 16))
        self.assertEqual(loan.first_deduction_period.month, 12)

    def test_only_pending_can_be_disbursed_or_cancelled(self):
        loan = _loan(self.emp)
        with self.assertRaises(ValidationError):
            loan.mark_disbursed(self.admin, date(2026, 9, 16))
        with self.assertRaises(ValidationError):
            loan.cancel(self.admin, "typo")

    def test_clean_rules(self):
        loan = SalaryLoan(
            employee=self.emp, file_reference="X/1", approved_on=date(2026, 9, 1),
            principal=Decimal("10000"), installment_count=2, installment_amount=Decimal("20000"),
        )
        with self.assertRaises(ValidationError) as ctx:
            loan.full_clean()
        self.assertIn("installment_amount", ctx.exception.message_dict)
        self.assertIn("approval_document", ctx.exception.message_dict)   # IN_ORBIS needs the scan
        loan.recorded_source = SalaryLoan.RecordedSource.OUTSIDE_ORBIS
        loan.installment_amount = Decimal("5000")
        loan.full_clean()  # cutover rows may lack the document


class EngineLoanTests(TestCase):
    """The reference loan across consecutive runs. Gross 120,817 keeps net
    comfortably above the instalment; the tax fixture is the true-up one so
    the deduction order (after TDS) is exercised for real."""

    def setUp(self):
        self.department = Department.objects.create(name="Software Development")
        self.employee = _user("loan@example.com", "900", department=self.department, joining_date=date(2025, 1, 1))
        JobInfo.objects.create(employee=self.employee, resource_status=JobInfo.ResourceStatus.LIVE)
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        PayComponent.objects.create(
            code="TDS", name="Tax deducted at source", kind=PayComponent.Kind.DEDUCTION,
            entry_mode=PayComponent.EntryMode.INPUT, is_taxable=False,
        )
        self.loan_component = PayComponent.objects.create(
            code="LOAN_REPAYMENT", name="Loan repayment", short_name="Loan", kind=PayComponent.Kind.DEDUCTION,
            entry_mode=PayComponent.EntryMode.COMPUTED, is_taxable=False, affects_gross=False, sequence=97,
        )
        self.disciplinary = PayComponent.objects.create(
            code="DISCIPLINARY", name="Disciplinary deduction", kind=PayComponent.Kind.DEDUCTION,
            entry_mode=PayComponent.EntryMode.INPUT, is_taxable=False,
        )
        tax_year, categories = _seed_tax_year()
        tax_year.calculation_method = TaxYear.CalculationMethod.ITA_2023_SALARY_TRUEUP
        tax_year.save()
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("120817"), status=SalaryStructure.Status.APPROVED,
        )
        EmployeeTaxDeclaration.objects.create(employee=self.employee, tax_year=tax_year, category=categories["MEN"])
        self.n = 0

    def _run(self, year, month):
        self.n += 1
        actor = User.objects.create_superuser(email=f"a{self.n}@example.com", username=f"a{self.n}@example.com", password="x")
        period = PayrollPeriod.for_month(year, month)
        run = lock_inputs(create_run(period, actor), actor)
        return calculate_run(run.pk, actor.pk)

    def _loan_line(self, run):
        return PayrollRunLine.objects.filter(run_employee__run=run, component_code="LOAN_REPAYMENT").first()

    def _re(self, run):
        return PayrollRunEmployee.objects.get(run=run, employee=self.employee)

    def test_three_periods_then_balance_and_notes(self):
        loan = _loan(self.employee, first=(2026, 10))
        september = self._run(2026, 9)          # before first deduction period
        self.assertIsNone(self._loan_line(september))

        october = self._run(2026, 10)
        line = self._loan_line(october)
        self.assertEqual(line.amount, Decimal("5000"))
        self.assertEqual(line.source_ref, f"SalaryLoan:{loan.pk}")
        self.assertIn("Instalment 1 of 10", line.calc_note)
        self.assertIn("outstanding after 45000", line.calc_note)
        re = self._re(october)
        self.assertEqual(Decimal(re.inputs_json["loans"][0]["instalment"]), Decimal("5000"))
        # Deduction order: after tax, in total_deductions not tax_deducted.
        self.assertGreater(re.tax_deducted, 0)
        self.assertEqual(re.total_deductions, Decimal("5000"))
        self.assertEqual(re.net_pay, re.gross_earnings - Decimal("5000") - re.tax_deducted)

        november = self._run(2026, 11)
        self.assertIn("Instalment 2 of 10", self._loan_line(november).calc_note)
        december = self._run(2026, 12)
        self.assertIn("Instalment 3 of 10", self._loan_line(december).calc_note)
        b = loan_balances([loan])[loan.pk]
        self.assertEqual(b.recovered, Decimal("15000"))
        self.assertEqual(b.outstanding, Decimal("35000"))
        self.assertEqual(b.instalments_deducted, 3)
        # The register grows a "Loan" column with no view change.
        self.assertEqual([c.code for c in _extra_deduction_components([self._re(december)])], ["LOAN_REPAYMENT"])

    def test_recalculating_a_period_does_not_double_count(self):
        loan = _loan(self.employee, first=(2026, 10))
        october = self._run(2026, 10)
        calculate_run(october.pk)
        calculate_run(october.pk)
        self.assertEqual(loan_balances([loan])[loan.pk].recovered, Decimal("5000"))
        november = self._run(2026, 11)
        self.assertIn("Instalment 2 of 10", self._loan_line(november).calc_note)

    def test_pending_disbursement_deducts_nothing(self):
        _loan(self.employee, first=(2026, 10), status=SalaryLoan.Status.PENDING_DISBURSEMENT)
        self.assertIsNone(self._loan_line(self._run(2026, 10)))

    def test_opening_counts_and_the_loan_settles_with_a_remainder(self):
        loan = _loan(self.employee, first=(2026, 10), opening="42000")   # 8,000 left → 5,000 then 3,000
        october = self._run(2026, 10)
        self.assertEqual(self._loan_line(october).amount, Decimal("5000"))
        self.assertIn("Instalment 9 of 10", self._loan_line(october).calc_note)
        november = self._run(2026, 11)
        self.assertEqual(self._loan_line(november).amount, Decimal("3000"))
        self.assertTrue(loan_balances([loan])[loan.pk].is_settled)
        december = self._run(2026, 12)
        self.assertIsNone(self._loan_line(december))

    def test_instalment_reduced_to_available_net_then_shortfall_recovered(self):
        loan = _loan(self.employee, first=(2026, 10))
        VariablePayEntry.objects.create(
            employee=self.employee, period=PayrollPeriod.for_month(2026, 10), component=self.disciplinary,
            amount=Decimal("114000"), reason="test", status=VariablePayEntry.Status.APPROVED,
        )
        october = self._run(2026, 10)
        re = self._re(october)
        line = self._loan_line(october)
        available = re.gross_earnings - Decimal("114000") - re.tax_deducted
        self.assertGreater(available, 0)
        self.assertLess(available, Decimal("5000"))
        self.assertEqual(line.amount, available)
        self.assertEqual(re.net_pay, Decimal("0"))
        self.assertTrue(PayrollControlCheck.objects.filter(run=october, check_code="LOAN_INSTALMENT_REDUCED").exists())
        self.assertIn("reduced", line.calc_note)
        november = self._run(2026, 11)
        self.assertEqual(self._loan_line(november).amount, Decimal("5000"))
        self.assertEqual(loan_balances([loan])[loan.pk].recovered, available + Decimal("5000"))

    def test_skip_deducts_nothing_and_warns(self):
        loan = _loan(self.employee, first=(2026, 10))
        hr = _user("hr@example.com", "901")
        SalaryLoanSkip.objects.create(loan=loan, period=PayrollPeriod.for_month(2026, 10), reason="Eid", approved_by_hr=hr)
        october = self._run(2026, 10)
        self.assertIsNone(self._loan_line(october))
        self.assertTrue(PayrollControlCheck.objects.filter(run=october, check_code="LOAN_INSTALMENT_SKIPPED").exists())
        self.assertTrue(self._re(october).inputs_json["loans"][0]["skipped"])
        november = self._run(2026, 11)
        self.assertIn("Instalment 1 of 10", self._loan_line(november).calc_note)

    def test_leaver_pays_the_whole_balance_in_the_last_payroll(self):
        loan = _loan(self.employee, first=(2026, 10))
        self._run(2026, 10)
        self.employee.last_working_date = date(2026, 11, 10)
        self.employee.save(update_fields=["last_working_date"])
        november = self._run(2026, 11)
        line = self._loan_line(november)
        self.assertEqual(line.amount, Decimal("45000"))
        self.assertIn("Final payroll", line.calc_note)
        self.assertTrue(loan_balances([loan])[loan.pk].is_settled)
        self.assertFalse(PayrollControlCheck.objects.filter(run=november, check_code="LOAN_OUTSTANDING_AT_SEPARATION").exists())

    def test_leaver_short_of_balance_warns_with_shortfall(self):
        _loan(self.employee, first=(2026, 10), principal="500000", count=10, instalment="50000")
        self.employee.last_working_date = date(2026, 10, 1)   # ~16 days' pay only
        self.employee.save(update_fields=["last_working_date"])
        october = self._run(2026, 10)
        re = self._re(october)
        self.assertEqual(re.net_pay, Decimal("0"))
        check = PayrollControlCheck.objects.get(run=october, check_code="LOAN_OUTSTANDING_AT_SEPARATION")
        self.assertIn("still outstanding", check.message)

    def test_two_loans_two_lines(self):
        _loan(self.employee, first=(2026, 10))
        _loan(self.employee, first=(2026, 10), ref="HR/2", principal="12000", count=3, instalment="4000")
        october = self._run(2026, 10)
        lines = PayrollRunLine.objects.filter(run_employee__run=october, component_code="LOAN_REPAYMENT")
        self.assertEqual(sorted(l.amount for l in lines), [Decimal("4000"), Decimal("5000")])
        self.assertEqual(self._re(october).total_deductions, Decimal("9000"))

    def test_missing_component_blocks_with_a_check(self):
        self.loan_component.delete()
        _loan(self.employee, first=(2026, 10))
        october = self._run(2026, 10)
        self.assertTrue(PayrollControlCheck.objects.filter(run=october, check_code="MISSING_LOAN_COMPONENT").exists())
        self.assertIsNone(self._loan_line(october))



def _groups(*names):
    return [Group.objects.get_or_create(name=n)[0] for n in names]


def _protect(view_names, groups):
    for name in view_names:
        pv, _ = ProtectedView.objects.get_or_create(view_name=name, defaults={"is_active": True})
        pv.allowed_groups.set(groups)


LOAN_VIEWS = (
    "payroll_loan_list", "payroll_loan_create", "payroll_loan_edit", "payroll_loan_disburse", "payroll_loan_cancel",
    "payroll_loan_adjust", "payroll_loan_skip", "payroll_loan_history", "payroll_loan_employee_search",
)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class LoanViewTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        hr, payroll, admins = _groups("HR", "Payroll", "Payroll Admins")
        _protect(LOAN_VIEWS, [hr, payroll, admins])
        self.emp = _user("emp@example.com", "1000")
        self.colleague = _user("col@example.com", "1001")
        self.payroll_hr = _user("phr@example.com", "1002")
        self.payroll_hr.groups.add(hr, payroll)
        self.supervisor = _user("sup@example.com", "1003")
        self.supervisor.groups.add(payroll)
        self.hr_only = _user("hro@example.com", "1004")
        self.hr_only.groups.add(hr)
        self.admin = _user("adm@example.com", "1005")
        self.admin.groups.add(admins)
        self.client = Client(SERVER_NAME="localhost")
        self.list_url = reverse("payroll_loan_list")

    def test_access_tiers(self):
        for user, status in ((self.payroll_hr, 200), (self.admin, 200), (self.supervisor, 403), (self.hr_only, 403)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.list_url).status_code, status, user.email)
        self.client.force_login(self.emp)   # not in any group → dynamic_allowed_users redirect
        self.assertEqual(self.client.get(self.list_url).status_code, 302)

    def test_record_loan_then_disburse_sets_first_period(self):
        self.client.force_login(self.payroll_hr)
        resp = self.client.post(reverse("payroll_loan_create"), {
            "employee": str(self.emp.pk), "file_reference": "HR/21092026/01", "approved_on": "2026-09-21",
            "approval_document": _pdf(), "principal": "50000", "installment_count": "10",
            "installment_amount": "", "first_deduction_month": "", "note": "Birth of child",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        loan = SalaryLoan.objects.get(file_reference="HR/21092026/01")
        self.assertEqual(loan.installment_amount, Decimal("5000"))   # defaulted
        self.assertTrue(loan.is_pending)
        self.assertIsNone(loan.first_deduction_period)
        self.assertEqual(loan.added_by, self.payroll_hr)

        # Payroll-HR may not disburse; Payroll Admins may.
        resp = self.client.get(reverse("payroll_loan_disburse", args=[loan.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 403)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("payroll_loan_disburse", args=[loan.pk]), {"disbursed_on": "2026-09-16", "note": "BEFTN"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        loan.refresh_from_db()
        self.assertTrue(loan.is_active)
        self.assertEqual((loan.first_deduction_period.year, loan.first_deduction_period.month), (2026, 10))
        self.assertEqual(loan.disbursed_by, self.admin)

    def test_invalid_form_renders_inside_the_drawer(self):
        self.client.force_login(self.payroll_hr)
        resp = self.client.post(reverse("payroll_loan_create"), {
            "employee": str(self.emp.pk), "file_reference": "HR/X", "approved_on": "2026-09-21",
            "principal": "10000", "installment_count": "2", "installment_amount": "20000",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "can&#x27;t exceed the principal", status_code=400)
        self.assertContains(resp, "Attach the signed file note", status_code=400)
        self.assertTemplateUsed(resp, "payroll/_loan_drawer.html")

    def test_document_visibility(self):
        loan = _loan(self.emp)
        url = reverse("payroll_loan_document", args=[loan.pk])
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.colleague)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.payroll_hr)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_profile_card_for_self_and_managers_only(self):
        loan = _loan(self.emp, opening="15000")
        url = reverse("employee_detail", args=[self.emp.pk])
        self.client.force_login(self.emp)
        resp = self.client.get(url)
        self.assertContains(resp, "Salary loans")
        self.assertContains(resp, "3 of 10 instalments deducted")
        self.assertContains(resp, loan.file_reference)
        self.client.force_login(self.payroll_hr)
        self.assertContains(self.client.get(url), "Salary loans")

    def test_adjustment_and_skip_are_admin_only_and_validated(self):
        loan = _loan(self.emp)
        self.client.force_login(self.payroll_hr)
        self.assertEqual(self.client.get(reverse("payroll_loan_adjust", args=[loan.pk]), HTTP_HX_REQUEST="true").status_code, 403)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("payroll_loan_adjust", args=[loan.pk]), {
            "occurred_on": "2026-10-01", "reason": "WAIVER", "delta": "60000", "note": "",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "is outstanding", status_code=400)
        resp = self.client.post(reverse("payroll_loan_adjust", args=[loan.pk]), {
            "occurred_on": "2026-10-01", "reason": "PREPAYMENT", "delta": "10000", "note": "cash",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(loan_balances([loan])[loan.pk].outstanding, Decimal("40000"))

        period = PayrollPeriod.for_month(2027, 1)
        hr_user = self.hr_only
        resp = self.client.post(reverse("payroll_loan_skip", args=[loan.pk]), {
            "period": str(period.pk), "approved_by_hr": str(hr_user.pk), "reason": "Hardship",
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204, resp.content[:300])
        self.assertTrue(SalaryLoanSkip.objects.filter(loan=loan, period=period).exists())
        resp = self.client.get(reverse("payroll_loan_history", args=[loan.pk]), HTTP_HX_REQUEST="true")
        self.assertContains(resp, "First deduction")
        self.assertContains(resp, str(loan.first_deduction_period))
        self.assertContains(resp, "Repaid outside payroll")
        self.assertContains(resp, "Skipped")

    def test_list_tabs_and_search(self):
        active = _loan(self.emp)
        _loan(self.colleague, ref="HR/2", status=SalaryLoan.Status.PENDING_DISBURSEMENT, first=None)
        settled = _loan(self.colleague, ref="HR/3", opening="50000")
        self.client.force_login(self.payroll_hr)
        resp = self.client.get(self.list_url)
        self.assertContains(resp, active.file_reference)
        self.assertNotContains(resp, "HR/2</div>")
        resp = self.client.get(self.list_url + "?tab=settled")
        self.assertContains(resp, settled.file_reference)
        resp = self.client.get(self.list_url + "?tab=pending&q=1001")
        self.assertContains(resp, "HR/2")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class LoanUploadTests(TestCase):
    def setUp(self):
        (admins,) = _groups("Payroll Admins")
        _protect(("payroll_loan_upload", "payroll_loan_upload_template", "payroll_upload_center"), [admins])
        self.admin = _user("up@example.com", "1100")
        self.admin.groups.add(admins)
        self.emp = _user("row@example.com", "1101")
        self.client = Client(SERVER_NAME="localhost")
        self.url = reverse("payroll_loan_upload")

    def _xlsx(self, rows):
        wb = Workbook()
        ws = wb.active
        ws.append(["Employee ID", "File reference", "Approved on", "Principal", "Instalments", "Instalment amount",
                   "Disbursed on", "First deduction month", "Recovered before Orbis", "Note"])
        for r in rows:
            ws.append(list(r))
        stream = BytesIO()
        wb.save(stream)
        return SimpleUploadedFile("loans.xlsx", stream.getvalue())

    def test_apply_creates_active_cutover_rows_and_reupload_corrects_opening(self):
        self.client.force_login(self.admin)
        row = ("1101", "HR/21092026/01", "2026-09-21", 50000, 10, None, "2026-09-25", None, 10000, "Medical")
        self.assertEqual(self.client.post(self.url, {"action": "preview", "file": self._xlsx([row])}).status_code, 200)
        self.assertFalse(SalaryLoan.objects.exists())
        resp = self.client.post(self.url, {"action": "apply", "file": self._xlsx([row])})
        self.assertEqual(resp.status_code, 200)
        loan = SalaryLoan.objects.get(file_reference="HR/21092026/01")
        self.assertEqual(loan.status, SalaryLoan.Status.ACTIVE)
        self.assertEqual(loan.recorded_source, SalaryLoan.RecordedSource.OUTSIDE_ORBIS)
        self.assertEqual(loan.installment_amount, Decimal("5000"))
        self.assertEqual((loan.first_deduction_period.year, loan.first_deduction_period.month), (2026, 10))  # 25 Sep → October payroll
        self.assertEqual(loan.opening_recovered, Decimal("10000"))
        self.assertFalse(loan.approval_document)
        self.assertEqual(PayrollInputBatch.objects.filter(input_kind=PayrollInputBatch.InputKind.SALARY_LOAN).count(), 1)

        row2 = ("1101", "HR/21092026/01", "2026-09-21", 99999, 3, 1, "2026-09-25", "2026-12", 15000, "Updated")
        self.client.post(self.url, {"action": "apply", "file": self._xlsx([row2])})
        loan.refresh_from_db()
        self.assertEqual(loan.opening_recovered, Decimal("15000"))
        self.assertEqual(loan.principal, Decimal("50000"))      # terms never re-written
        self.assertEqual(loan.first_deduction_period.month, 10)
        self.assertEqual(loan.note, "Updated")

    def test_bad_rows_abort(self):
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {"action": "apply", "file": self._xlsx([
            ("9999", "HR/1", "2026-09-21", 50000, 10, None, "2026-09-25", None, 0, None),
            ("1101", "HR/2", "2026-09-21", 50000, 10, None, "2026-09-25", "nope", 0, None),
            ("1101", "HR/2", "2026-09-21", 50000, 10, None, "2026-09-25", None, 0, None),
            ("1101", "HR/3", "2026-09-21", 50000, 10, 60000, "2026-09-25", None, 0, None),
        ])})
        self.assertContains(resp, "aborted")
        self.assertContains(resp, "No employee with ID")
        self.assertContains(resp, "must be YYYY-MM")
        self.assertContains(resp, "also appears on row")
        self.assertContains(resp, "exceed the principal")
        self.assertFalse(SalaryLoan.objects.exists())

    def test_template_downloads(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("payroll_loan_upload_template"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
