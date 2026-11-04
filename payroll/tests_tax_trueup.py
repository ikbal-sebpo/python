"""Tax calculation v2 — year-to-date true-up (docs/TAX_CALCULATION_TRUEUP_PLAN.md).

Finance's 2026-10-07 example is the reference: gross 120,817, Men, incentives
5,000 to date, AIT 25,000 (documented), TDS already deducted 1,943 × 3,
October → monthly deduction 1,942.84 in the sheet, 1,943 in whole taka.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from accounts.models import Department
from dynamic_menu.models import ProtectedView
from employees.models import JobInfo
from payroll.engine import calculate_run
from payroll.models import (
    EmployeeTaxDeclaration, PayComponent, PayrollControlCheck, PayrollInputBatch, PayrollPeriod,
    PayrollRunEmployee, PayrollRunLine, SalaryStructure, TaxComputation, TaxYear, TDSEntry, VariablePayEntry,
)
from payroll.services import create_run, lock_inputs
from payroll.tax import compute_tax_for_employee, months_remaining_for
from payroll.tests_tax import _seed_tax_year, _user

User = get_user_model()
_MEDIA_ROOT = tempfile.mkdtemp(prefix="orbis-tax-trueup-tests-")
TRUEUP = TaxYear.CalculationMethod.ITA_2023_SALARY_TRUEUP


def _trueup_year():
    tax_year, categories = _seed_tax_year()
    tax_year.calculation_method = TRUEUP
    tax_year.save()
    return tax_year, categories


class FormulaTests(TestCase):
    def setUp(self):
        self.tax_year, self.categories = _trueup_year()
        self.employee = _user("ref@example.com", "500")
        self.slabs = list(self.tax_year.slabs.order_by("sequence"))

    def _compute(self, declaration, **kw):
        return compute_tax_for_employee(
            self.employee, self.tax_year, Decimal("120817"), declaration,
            slabs=self.slabs, threshold_amount=Decimal("400000"), **kw,
        )

    def test_finance_october_example_to_the_taka(self):
        declaration = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
            allowable_investment=Decimal("308286"), actual_investment=Decimal("308286"),
            ait_car_advance_tax=Decimal("25000"),
        )
        b = self._compute(
            declaration, additional_pay_ytd=Decimal("5000"), tds_already_deducted=Decimal("5829"), months_remaining=9,
        )
        self.assertEqual(b.gross_annual_projected, Decimal("1527621.00"))
        self.assertEqual(b.taxable_income, Decimal("1027621.00"))
        self.assertEqual(b.tax_before_rebate, Decimal("79143.15"))
        self.assertEqual(b.rebate_amount, Decimal("30828.60"))
        self.assertEqual(b.net_tax_payable, Decimal("48314.55"))
        self.assertEqual(b.annual_tds, Decimal("23314.55"))        # − AIT 25,000
        self.assertEqual(b.tax_remaining, Decimal("17485.55"))     # − TDS deducted 5,829
        self.assertEqual(b.months_remaining, 9)
        self.assertEqual(b.monthly_tds, Decimal("1943"))           # 1,942.84 → whole taka

    def test_flat_method_ignores_the_ytd_inputs(self):
        self.tax_year.calculation_method = TaxYear.CalculationMethod.ITA_2023_SALARY
        declaration = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
            allowable_investment=Decimal("306786"), actual_investment=Decimal("306786"),
        )
        flat = self._compute(declaration, additional_pay_ytd=Decimal("99999"), tds_already_deducted=Decimal("99999"), months_remaining=3)
        self.assertEqual(flat.additional_pay_ytd, Decimal("0"))
        self.assertEqual(flat.months_remaining, 12)
        self.assertEqual(flat.gross_annual_projected, Decimal("1522621.00"))
        self.assertEqual(flat.monthly_tds, (flat.annual_tds / 12).quantize(Decimal("0.01")))

    def test_over_deducted_clamps_to_zero_with_warning(self):
        declaration = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
            allowable_investment=Decimal("306786"), actual_investment=Decimal("306786"),
        )
        b = self._compute(declaration, tds_already_deducted=Decimal("90000"), months_remaining=6)
        self.assertLess(b.tax_remaining, 0)
        self.assertEqual(b.monthly_tds, Decimal("0"))
        self.assertTrue(any("over-deducted" in w for w in b.warnings))

    def test_months_remaining_follows_the_calendar(self):
        expected = {7: 12, 8: 11, 9: 10, 10: 9, 11: 8, 12: 7, 1: 6, 2: 5, 3: 4, 4: 3, 5: 2, 6: 1}
        for month, remaining in expected.items():
            year = 2026 if month >= 7 else 2027
            self.assertEqual(months_remaining_for(self.tax_year, PayrollPeriod.for_month(year, month)), remaining, month)


class EngineTrueUpTests(TestCase):
    """Three consecutive periods on one employee — the YTD inputs come from the
    earlier calculated runs, extras raise the next month, overrides count."""

    def setUp(self):
        self.department = Department.objects.create(name="Software Development")
        self.employee = _user("ytd@example.com", "600", department=self.department, joining_date=date(2025, 1, 1))
        JobInfo.objects.create(employee=self.employee, resource_status=JobInfo.ResourceStatus.LIVE)
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        PayComponent.objects.create(
            code="TDS", name="Tax deducted at source", kind=PayComponent.Kind.DEDUCTION,
            entry_mode=PayComponent.EntryMode.INPUT, is_taxable=False,
        )
        self.incentive = PayComponent.objects.create(
            code="INCENTIVE", name="Incentive", kind=PayComponent.Kind.EARNING, entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.bonus = PayComponent.objects.create(
            code="FESTIVAL_BONUS", name="Festival bonus", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.performance_bonus = PayComponent.objects.create(
            code="BONUS", name="Performance bonus", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.tax_year, self.categories = _trueup_year()
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("120817"), status=SalaryStructure.Status.APPROVED,
        )
        self.declaration = EmployeeTaxDeclaration.objects.create(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
        )
        self.n = 0

    def _run(self, year, month):
        self.n += 1
        actor = User.objects.create_superuser(email=f"a{self.n}@example.com", username=f"a{self.n}@example.com", password="x")
        period = PayrollPeriod.for_month(year, month)
        run = lock_inputs(create_run(period, actor), actor)
        return calculate_run(run.pk, actor.pk)

    def _tax(self, run):
        return TaxComputation.objects.get(run=run, employee=self.employee)

    def _variable(self, component, period, amount):
        VariablePayEntry.objects.create(
            employee=self.employee, period=period, component=component, amount=Decimal(amount),
            status=VariablePayEntry.Status.APPROVED,
        )

    def test_first_month_is_the_flat_figure_then_extras_raise_the_next(self):
        july = self._run(2026, 7)
        t1 = self._tax(july)
        self.assertEqual(t1.months_remaining, 12)
        self.assertEqual(t1.tds_already_deducted, Decimal("0"))
        self.assertEqual(t1.additional_pay_ytd, Decimal("0"))
        flat_monthly = t1.monthly_tds
        self.assertGreater(flat_monthly, 0)

        # August: 30,000 incentive this period → it enters the projection now.
        self._variable(self.incentive, PayrollPeriod.for_month(2026, 8), "30000")
        august = self._run(2026, 8)
        t2 = self._tax(august)
        self.assertEqual(t2.months_remaining, 11)
        self.assertEqual(t2.tds_already_deducted, flat_monthly)
        self.assertEqual(t2.additional_pay_ytd, Decimal("30000"))
        # Extra tax on 30,000 at the 15% band = 4,500, spread over 11 months
        # on top of the flat share: (annual − deducted) / 11.
        self.assertEqual(t2.monthly_tds, ((t2.annual_tds - flat_monthly) / 11).quantize(Decimal("1")))
        self.assertGreater(t2.monthly_tds, flat_monthly)

        # September: nothing new; August's incentive is still in the YTD total.
        september = self._run(2026, 9)
        t3 = self._tax(september)
        self.assertEqual(t3.months_remaining, 10)
        self.assertEqual(t3.additional_pay_ytd, Decimal("30000"))
        self.assertEqual(t3.tds_already_deducted, flat_monthly + t2.monthly_tds)
        self.assertEqual(t3.breakdown_json["ytd"]["prior_periods"], [
            str(PayrollPeriod.for_month(2026, 7)), str(PayrollPeriod.for_month(2026, 8)),
        ])
        line = PayrollRunLine.objects.get(run_employee__run=september, component_code="TDS")
        self.assertIn("deducted YTD=", line.calc_note)

    def test_festival_bonus_is_not_additional_pay(self):
        self._variable(self.bonus, PayrollPeriod.for_month(2026, 7), "120817")
        july = self._run(2026, 7)
        self.assertEqual(self._tax(july).additional_pay_ytd, Decimal("0"))

    def test_performance_bonus_is_additional_pay(self):
        """2026-10-08: BONUS is the *performance* bonus (SEBPO pays both), so
        it counts like an incentive — only FESTIVAL_BONUS is excluded."""
        self._variable(self.performance_bonus, PayrollPeriod.for_month(2026, 7), "10000")
        july = self._run(2026, 7)
        self.assertEqual(self._tax(july).additional_pay_ytd, Decimal("10000"))

    def test_manual_override_counts_as_deducted_next_month(self):
        TDSEntry.objects.create(employee=self.employee, period=PayrollPeriod.for_month(2026, 7), amount=Decimal("777"))
        self._run(2026, 7)
        august = self._run(2026, 8)
        self.assertEqual(self._tax(august).tds_already_deducted, Decimal("777"))

    def test_gap_month_warns_once_per_run(self):
        self._run(2026, 7)
        september = self._run(2026, 9)  # August never ran
        checks = PayrollControlCheck.objects.filter(run=september, check_code="TAX_YTD_GAP")
        self.assertEqual(checks.count(), 1)
        self.assertIn("Aug 2026", checks.first().message)
        self.assertEqual(checks.first().severity, PayrollControlCheck.Severity.WARNING)

    def test_openings_and_live_from_replace_the_pre_orbis_months(self):
        self.tax_year.orbis_live_from = PayrollPeriod.for_month(2026, 10)
        self.tax_year.save()
        self.declaration.opening_tds_deducted = Decimal("5829")
        self.declaration.opening_additional_pay = Decimal("5000")
        # Finance's sheet took the allowable figure with the 5,000 incentive
        # inside taxable income (308,286); Orbis's July snapshot is salary-only
        # (306,786), so pin the sheet's value to reproduce it end to end.
        self.declaration.allowable_investment = Decimal("308286")
        self.declaration.actual_investment = Decimal("308286")
        self.declaration.ait_car_advance_tax = Decimal("25000")
        self.declaration.ait_document = SimpleUploadedFile("ait.pdf", b"%PDF-1.4", content_type="application/pdf")
        with override_settings(MEDIA_ROOT=_MEDIA_ROOT):
            self.declaration.save()
            october = self._run(2026, 10)
        t = self._tax(october)
        # No July–September runs exist, but they are before live-from: no gap warning.
        self.assertFalse(PayrollControlCheck.objects.filter(run=october, check_code="TAX_YTD_GAP").exists())
        self.assertFalse(PayrollControlCheck.objects.filter(run=october, check_code="MISSING_TAX_OPENING").exists())
        self.assertEqual(t.additional_pay_ytd, Decimal("5000"))
        self.assertEqual(t.tds_already_deducted, Decimal("5829"))
        self.assertEqual(t.months_remaining, 9)
        self.assertEqual(t.monthly_tds, Decimal("1943"))  # Finance's example, end to end
        re = PayrollRunEmployee.objects.get(run=october, employee=self.employee)
        self.assertEqual(re.tax_deducted, Decimal("1943"))

    def test_missing_openings_warn_when_live_from_is_mid_year(self):
        self.tax_year.orbis_live_from = PayrollPeriod.for_month(2026, 10)
        self.tax_year.save()
        october = self._run(2026, 10)
        self.assertTrue(PayrollControlCheck.objects.filter(
            run=october, check_code="MISSING_TAX_OPENING", employee=self.employee,
            severity=PayrollControlCheck.Severity.WARNING,
        ).exists())

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)


class OpeningUploadTests(TestCase):
    def setUp(self):
        self.tax_year, self.categories = _trueup_year()
        self.emp = _user("open@example.com", "700", joining_date=date(2025, 1, 1))
        SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("120817"), status=SalaryStructure.Status.APPROVED,
        )
        self.admin = _user("padmin@example.com", "701")
        payroll_admins, _ = Group.objects.get_or_create(name="Payroll Admins")
        self.admin.groups.add(payroll_admins)
        for name in ("payroll_tax_opening_upload", "payroll_tax_opening_upload_template", "payroll_upload_center"):
            pv, _ = ProtectedView.objects.get_or_create(view_name=name, defaults={"is_active": True})
            pv.allowed_groups.set([payroll_admins])
        self.outsider = _user("out@example.com", "702")
        self.client = Client(SERVER_NAME="localhost")
        self.url = reverse("payroll_tax_opening_upload")

    def _xlsx(self, rows):
        wb = Workbook()
        ws = wb.active
        ws.append(["Employee ID", "TDS deducted to date", "Additional pay to date"])
        for r in rows:
            ws.append(list(r))
        stream = BytesIO()
        wb.save(stream)
        return SimpleUploadedFile("openings.xlsx", stream.getvalue())

    def test_outsider_is_refused(self):
        self.client.force_login(self.outsider)
        # dynamic_allowed_users: full-page refusals redirect home, htmx ones 403.
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("home"))
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 403)

    def test_preview_writes_nothing_then_apply_creates_and_overwrites(self):
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {"action": "preview", "file": self._xlsx([("700", 5829, None)])})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(EmployeeTaxDeclaration.objects.filter(employee=self.emp).exists())

        resp = self.client.post(self.url, {"action": "apply", "file": self._xlsx([("700", 5829, None)])})
        self.assertEqual(resp.status_code, 200)
        d = EmployeeTaxDeclaration.objects.get(employee=self.emp, tax_year=self.tax_year)
        self.assertEqual(d.opening_tds_deducted, Decimal("5829"))
        self.assertEqual(d.opening_additional_pay, Decimal("0"))        # blank column = 0
        self.assertEqual(d.allowable_investment, Decimal("306786"))     # created via save(): snapshot issued
        self.assertEqual(PayrollInputBatch.objects.filter(input_kind=PayrollInputBatch.InputKind.TAX_OPENING).count(), 1)

        self.client.post(self.url, {"action": "apply", "file": self._xlsx([("700", "7,000", 12500)])})
        d.refresh_from_db()
        self.assertEqual(d.opening_tds_deducted, Decimal("7000"))
        self.assertEqual(d.opening_additional_pay, Decimal("12500"))

    def test_bad_rows_abort_the_whole_file(self):
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {"action": "apply", "file": self._xlsx([("700", 100, None), ("700", 200, None), ("999", 1, None), ("700", "abc", None)])})
        self.assertContains(resp, "aborted")
        self.assertContains(resp, "also appears on row")
        self.assertContains(resp, "No employee with ID")
        self.assertFalse(EmployeeTaxDeclaration.objects.filter(employee=self.emp).exists())

    def test_template_downloads(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("payroll_tax_opening_upload_template"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tax_openings_2026-27", resp["Content-Disposition"])
