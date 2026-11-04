"""Tests for payroll.tax (Phase 4 in-house ITA-2023 calculation) and its
engine.py integration.

The base worked example (gross_monthly=50000, Men, "assumed maximum"
investment 1,232,400) is reproduced exactly from Finance's real Tax
Calculation.xlsx, hand-verified cell by cell. (Since 2026-10-07 the
declaration carries one ``actual_investment`` commitment instead of six
itemised fields — see tests_tax_declaration.py for the commitment/lock
behaviour; the arithmetic here is unchanged.) One intentional divergence:
Excel's H13 uses ``TEXT(...)`` to format the first progressive slab's width
before multiplying, which truncates it to an integer (17,333 instead of
17,333.333...) — this implementation keeps full decimal precision instead
(1,733.33 vs Excel's 1,733.30). The floor (``minimum_tax``) swallows that
0.03 difference in this particular example, so ``net_tax_payable``/
``monthly_tds`` still match Finance's template to the paisa.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from accounts.models import Department
from employees.models import JobInfo, PersonalInfo
from payroll.engine import calculate_run
from payroll.models import (
    EmployeeTaxDeclaration, PayComponent, PayrollControlCheck, PayrollPeriod, PayrollRunEmployee, PayrollRunLine,
    SalaryStructure, TaxCategory, TaxComputation, TaxExemptionThreshold, TaxSlab, TaxYear, TDSEntry,
)
from payroll.services import create_run, lock_inputs
from payroll.tax import compute_tax_for_employee

User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


def _seed_tax_year():
    """FY2026-27 figures confirmed 2026-08-24 — same values as seed_tax_year."""
    tax_year = TaxYear.objects.create(
        code="2026-27", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30),
        exemption_divisor=Decimal("3"), exemption_cap=Decimal("500000"), minimum_tax=Decimal("5000"),
        rebate_income_pct=Decimal("0.0300"), rebate_investment_pct=Decimal("0.1000"),
        rebate_cap=Decimal("750000"), is_active=True,
    )
    categories = {}
    thresholds = {
        "MEN": Decimal("400000"), "WOMEN_SENIOR": Decimal("450000"),
        "DISABLED_THIRD_GENDER": Decimal("525000"), "FREEDOM_FIGHTER": Decimal("550000"),
        "PARENTS_OF_DISABLED_CHILD": Decimal("450000"),
    }
    for i, (code, amount) in enumerate(thresholds.items()):
        cat = TaxCategory.objects.create(code=code, name=code.title(), sequence=i)
        categories[code] = cat
        TaxExemptionThreshold.objects.create(tax_year=tax_year, category=cat, threshold_amount=amount)
    slabs = [
        (1, Decimal("300000"), Decimal("0.1000")),
        (2, Decimal("400000"), Decimal("0.1500")),
        (3, Decimal("500000"), Decimal("0.2000")),
        (4, Decimal("2000000"), Decimal("0.2500")),
        (5, None, Decimal("0.3000")),
    ]
    for sequence, width, rate in slabs:
        TaxSlab.objects.create(tax_year=tax_year, sequence=sequence, width=width, rate=rate)
    return tax_year, categories


class TaxCalculationTests(TestCase):
    """Unit tests for compute_tax_for_employee — no engine, no DB writes."""

    def setUp(self):
        self.department = Department.objects.create(name="Software Development")
        self.employee = _user("carol@example.com", "001", department=self.department)
        self.tax_year, self.categories = _seed_tax_year()
        self.slabs = list(self.tax_year.slabs.order_by("sequence"))

    def _threshold(self, category_code):
        return TaxExemptionThreshold.objects.get(tax_year=self.tax_year, category=self.categories[category_code]).threshold_amount

    def test_finance_worked_example_reproduced(self):
        """gross_monthly=50000, Men, "assumed maximum" investment 1,232,400 — Finance's real cached example."""
        declaration = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
            actual_investment=Decimal("1232400"),
        )
        breakdown = compute_tax_for_employee(
            self.employee, self.tax_year, Decimal("50000"), declaration,
            slabs=self.slabs, threshold_amount=self._threshold("MEN"),
        )
        self.assertEqual(breakdown.mobile_internet_reimburse, Decimal("2000"))
        self.assertEqual(breakdown.gross_annual_projected, Decimal("626000.00"))
        self.assertEqual(breakdown.exempted_income, Decimal("208666.67"))
        self.assertEqual(breakdown.taxable_income, Decimal("417333.33"))
        self.assertEqual(breakdown.tax_before_rebate, Decimal("1733.33"))
        self.assertEqual(breakdown.rebate_amount, Decimal("12520.00"))  # min(3% of taxable, 10% of investment, cap)
        self.assertEqual(breakdown.net_tax_payable, Decimal("5000.00"))  # floored — minimum_tax
        self.assertEqual(breakdown.annual_tds, Decimal("5000.00"))
        self.assertEqual(breakdown.monthly_tds, Decimal("416.67"))

    def test_zero_investment_still_hits_floor(self):
        """Orbis's real default (no declared investment) — same gross, no rebate at all,
        but the minimum_tax floor still dominates so the final TDS is unchanged."""
        declaration = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
        )
        breakdown = compute_tax_for_employee(
            self.employee, self.tax_year, Decimal("50000"), declaration,
            slabs=self.slabs, threshold_amount=self._threshold("MEN"),
        )
        self.assertEqual(breakdown.rebate_amount, Decimal("0.00"))
        self.assertEqual(breakdown.net_tax_payable, Decimal("5000.00"))
        self.assertEqual(breakdown.monthly_tds, Decimal("416.67"))

    def test_investment_rebate_changes_outcome_above_the_floor(self):
        """Higher salary where tax_before_rebate exceeds the floor — the rebate now matters."""
        declaration_no_investment = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
        )
        no_rebate = compute_tax_for_employee(
            self.employee, self.tax_year, Decimal("150000"), declaration_no_investment,
            slabs=self.slabs, threshold_amount=self._threshold("MEN"),
        )
        self.assertEqual(no_rebate.exempted_income, Decimal("500000.00"))  # 1/3 (634,000) capped at 500,000
        self.assertEqual(no_rebate.taxable_income, Decimal("1402000.00"))
        self.assertEqual(no_rebate.tax_before_rebate, Decimal("150400.00"))  # 30,000 + 60,000 + 60,400
        self.assertEqual(no_rebate.rebate_amount, Decimal("0.00"))
        self.assertEqual(no_rebate.net_tax_payable, Decimal("150400.00"))
        self.assertEqual(no_rebate.monthly_tds, Decimal("12533.33"))

        declaration_with_investment = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
            actual_investment=Decimal("1000000"),
        )
        with_rebate = compute_tax_for_employee(
            self.employee, self.tax_year, Decimal("150000"), declaration_with_investment,
            slabs=self.slabs, threshold_amount=self._threshold("MEN"),
        )
        self.assertEqual(with_rebate.rebate_amount, Decimal("42060.00"))  # min(3%*1,402,000=42,060, 10%*1,000,000=100,000, 750,000)
        self.assertEqual(with_rebate.net_tax_payable, Decimal("108340.00"))
        self.assertEqual(with_rebate.monthly_tds, Decimal("9028.33"))

    def test_mobile_internet_reimburse_tiers(self):
        cases = [
            (Decimal("20000"), Decimal("0")),
            (Decimal("20001"), Decimal("1000")),
            (Decimal("35000"), Decimal("1000")),
            (Decimal("35001"), Decimal("2000")),
            (Decimal("80000"), Decimal("2000")),
            (Decimal("80001"), Decimal("3000")),
            (Decimal("110000"), Decimal("3000")),
            (Decimal("110001"), Decimal("4000")),
            (Decimal("150000"), Decimal("4000")),
            (Decimal("150001"), Decimal("5000")),
            (Decimal("200000"), Decimal("5000")),
            (Decimal("200001"), Decimal("6000")),
        ]
        declaration = EmployeeTaxDeclaration(employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"])
        for gross, expected in cases:
            with self.subTest(gross=gross):
                breakdown = compute_tax_for_employee(
                    self.employee, self.tax_year, gross, declaration,
                    slabs=self.slabs, threshold_amount=self._threshold("MEN"),
                )
                self.assertEqual(breakdown.mobile_internet_reimburse, expected)

    def test_negative_annual_tds_clamped_to_zero_with_warning(self):
        declaration = EmployeeTaxDeclaration(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
            source_tax=Decimal("999999"),
        )
        breakdown = compute_tax_for_employee(
            self.employee, self.tax_year, Decimal("50000"), declaration,
            slabs=self.slabs, threshold_amount=self._threshold("MEN"),
        )
        self.assertLess(breakdown.annual_tds, 0)
        self.assertEqual(breakdown.monthly_tds, Decimal("0.00"))
        self.assertTrue(breakdown.warnings)


class TaxEngineIntegrationTests(TestCase):
    """calculate_run()'s Stage 7 — override-first, calculated fallback."""

    def setUp(self):
        self.software = Department.objects.create(name="Software Development")
        self.employee = _user("erin@example.com", "005", department=self.software)
        JobInfo.objects.create(employee=self.employee, resource_status=JobInfo.ResourceStatus.LIVE)
        self.no_declaration_employee = _user("frank@example.com", "006", department=self.software)
        JobInfo.objects.create(employee=self.no_declaration_employee)

        self.period = PayrollPeriod.for_month(2026, 7)
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        PayComponent.objects.create(
            code="TDS", name="Tax deducted at source", kind=PayComponent.Kind.DEDUCTION,
            entry_mode=PayComponent.EntryMode.INPUT,
        )
        self.tax_year, self.categories = _seed_tax_year()

        for emp in (self.employee, self.no_declaration_employee):
            SalaryStructure.objects.create(
                employee=emp, effective_from=date(2026, 1, 1),
                reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("50000"),
                status=SalaryStructure.Status.APPROVED,
            )

        EmployeeTaxDeclaration.objects.create(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
        )

    def _run(self, actor_email="admin@example.com"):
        actor = User.objects.create_superuser(email=actor_email, username=actor_email, password="x")
        run = create_run(self.period, actor)
        run = lock_inputs(run, actor)
        return calculate_run(run.pk, actor.pk)

    def test_declaration_present_produces_calculated_tds_line_and_computation(self):
        run = self._run()
        re = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        self.assertEqual(re.tax_deducted, Decimal("416.67"))
        line = PayrollRunLine.objects.get(run_employee=re, component_code="TDS")
        self.assertEqual(line.source_kind, "TaxComputation")
        self.assertEqual(line.source_ref, f"TaxComputation:{run.pk}:{self.employee.pk}")
        computation = TaxComputation.objects.get(run=run, employee=self.employee)
        self.assertEqual(computation.monthly_tds, Decimal("416.67"))
        self.assertEqual(computation.category_name, "Men")

    def test_missing_declaration_creates_blocking_check_not_line(self):
        run = self._run(actor_email="admin2@example.com")
        re = PayrollRunEmployee.objects.get(run=run, employee=self.no_declaration_employee)
        self.assertEqual(re.tax_deducted, Decimal("0"))
        self.assertFalse(PayrollRunLine.objects.filter(run_employee=re, component_code="TDS").exists())
        self.assertTrue(
            PayrollControlCheck.objects.filter(
                run=run, employee=self.no_declaration_employee, check_code="MISSING_TAX_DECLARATION",
                severity=PayrollControlCheck.Severity.BLOCKING,
            ).exists()
        )

    def test_tds_entry_override_wins_even_with_declaration_present(self):
        TDSEntry.objects.create(employee=self.employee, period=self.period, amount=Decimal("777.00"), note="manual correction")
        run = self._run(actor_email="admin3@example.com")
        re = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        self.assertEqual(re.tax_deducted, Decimal("777.00"))
        line = PayrollRunLine.objects.get(run_employee=re, component_code="TDS")
        self.assertEqual(line.source_kind, "TDSEntry")
        self.assertFalse(TaxComputation.objects.filter(run=run, employee=self.employee).exists())

    def test_recalculate_is_idempotent_for_tax_computation(self):
        run = self._run(actor_email="admin4@example.com")
        first = TaxComputation.objects.get(run=run, employee=self.employee)
        run2 = calculate_run(run.pk, None)
        second = TaxComputation.objects.get(run=run2, employee=self.employee)
        self.assertEqual(TaxComputation.objects.filter(run=run2, employee=self.employee).count(), 1)
        self.assertEqual(first.monthly_tds, second.monthly_tds)


class TaxCategoryGenderDefaultTests(TestCase):
    """category auto-defaults from PersonalInfo.gender when left blank —
    exercised through the real drawer POST (SectionEditView), since the
    default only applies once `employee` is set, which happens after the
    form's own is_valid()/full_clean() has already run once."""

    def setUp(self):
        self.tax_year, self.categories = _seed_tax_year()
        self.actor = User.objects.create_superuser(email="hr@example.com", username="hr@example.com", password="x")
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.actor)

    def _add_declaration(self, employee, **extra):
        url = f"/employees/{employee.pk}/add/tax-declaration/"
        data = {"tax_year": self.tax_year.pk, **extra}
        return self.client.post(url, data, HTTP_HX_REQUEST="true")

    def test_male_defaults_to_men(self):
        employee = _user("male@example.com", "101")
        PersonalInfo.objects.create(employee=employee, gender=PersonalInfo.Gender.MALE)
        resp = self._add_declaration(employee)
        self.assertEqual(resp.status_code, 200)
        declaration = EmployeeTaxDeclaration.objects.get(employee=employee, tax_year=self.tax_year)
        self.assertEqual(declaration.category.code, "MEN")

    def test_female_defaults_to_women_senior(self):
        employee = _user("female@example.com", "102")
        PersonalInfo.objects.create(employee=employee, gender=PersonalInfo.Gender.FEMALE)
        resp = self._add_declaration(employee)
        self.assertEqual(resp.status_code, 200)
        declaration = EmployeeTaxDeclaration.objects.get(employee=employee, tax_year=self.tax_year)
        self.assertEqual(declaration.category.code, "WOMEN_SENIOR")

    def test_no_personal_info_leaves_category_blank(self):
        employee = _user("nogender@example.com", "103")
        resp = self._add_declaration(employee)
        self.assertEqual(resp.status_code, 200)
        declaration = EmployeeTaxDeclaration.objects.get(employee=employee, tax_year=self.tax_year)
        self.assertIsNone(declaration.category)

    def test_explicit_category_is_not_overridden_by_gender(self):
        employee = _user("male2@example.com", "104")
        PersonalInfo.objects.create(employee=employee, gender=PersonalInfo.Gender.MALE)
        resp = self._add_declaration(employee, category=self.categories["FREEDOM_FIGHTER"].pk)
        self.assertEqual(resp.status_code, 200)
        declaration = EmployeeTaxDeclaration.objects.get(employee=employee, tax_year=self.tax_year)
        self.assertEqual(declaration.category.code, "FREEDOM_FIGHTER")
