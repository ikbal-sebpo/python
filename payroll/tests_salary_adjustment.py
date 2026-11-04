"""Salary adjustment — the owed balance after a retroactive salary change,
settled N months at a time on the Payroll Admin's say-so
(docs/SALARY_ADJUSTMENT_PLAN.md). The worked example is the user's own:
109,834 → 120,817 effective 1 Jan 2026, paid at the old rate until the
August payroll, the first instalment (3 months, 32,949) in the September
payroll before Orbis, then a revision to 130,000 from the same January.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Department
from dynamic_menu.models import ProtectedView
from employees.models import JobInfo
from payroll.adjustments import SOURCE_ASSUMED, SOURCE_LEGACY, SOURCE_RUN, adjustment_ledgers
from payroll.engine import calculate_run
from payroll.models import (
    EmployeeTaxDeclaration, LegacyPayrollMonth, PayrollControlCheck, PayrollPeriod, PayrollRun, PayrollRunEmployee,
    PayrollRunLine, SalaryStructure, TaxComputation,
)
from payroll.services import create_run, lock_inputs, supersede_salary_structure
from payroll.tests_legacy_payroll import _components, _grant, _live_year
from payroll.tests_tax import _user

User = get_user_model()

OLD, NEW, REVISED = Decimal("109834"), Decimal("120817"), Decimal("130000")


class _Base(TestCase):
    def setUp(self):
        self.components = _components()
        self.tax_year, self.categories = _live_year()
        self.department = Department.objects.create(name="Software Development")
        self.emp = _user("rashel@example.com", "539", department=self.department, joining_date=date(2011, 11, 1))
        JobInfo.objects.create(employee=self.emp, resource_status=JobInfo.ResourceStatus.LIVE)
        SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31),
            reason=SalaryStructure.Reason.ANNUAL_INCREMENT, gross_monthly=OLD, status=SalaryStructure.Status.SUPERSEDED,
        )
        self.current = SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.ANNUAL_INCREMENT,
            gross_monthly=NEW, status=SalaryStructure.Status.APPROVED,
        )
        EmployeeTaxDeclaration.objects.create(employee=self.emp, tax_year=self.tax_year, category=self.categories["MEN"])
        # The three pre-Orbis months exactly as uploaded on 2026-10-09.
        for month, lines, months in (
            (7, {"GROSS_SALARY": "109834", "MEAL_DEDUCTION": "550"}, None),
            (8, {"GROSS_SALARY": "120817", "MEAL_DEDUCTION": "600"}, None),
            (9, {"GROSS_SALARY": "120817", "SALARY_ADJUSTMENT": "32949", "MEAL_DEDUCTION": "600"}, 3),
        ):
            LegacyPayrollMonth.objects.create(
                tax_year=self.tax_year, employee=self.emp, year=2026, month=month, lines=lines,
                tds_deducted=Decimal("3029"), adjustment_months=months,
            )
        self.actor = User.objects.create_superuser(email="a@example.com", username="a@example.com", password="x")

    def _run(self, year, month, *, months=None, settle_all=False):
        run = lock_inputs(create_run(PayrollPeriod.for_month(year, month), self.actor), self.actor)
        run.adjustment_months, run.adjustment_settle_all = months, settle_all
        run.save()
        return calculate_run(run.pk, self.actor.pk)

    def _adjustment_line(self, run):
        re = PayrollRunEmployee.objects.get(run=run, employee=self.emp)
        line = PayrollRunLine.objects.filter(run_employee=re, component_code="SALARY_ADJUSTMENT").first()
        return re, line

    def _revise(self):
        return supersede_salary_structure(
            self.emp, gross_monthly=REVISED, effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.CORRECTION, actor=self.actor,
        )


class LedgerTests(_Base):
    def test_ledger_reproduces_finances_own_schedule(self):
        ledger = adjustment_ledgers([self.emp], PayrollPeriod.for_month(2026, 10))[self.emp.pk]
        # Jan–Jun: no record → assumed paid at 109,834. Jul: imported, 109,834.
        # Aug/Sep: imported at the new rate → nothing owed, not in the list.
        self.assertEqual([m.label for m in ledger.months],
                         ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026", "Jul 2026"])
        self.assertEqual({m.source for m in ledger.months[:6]}, {SOURCE_ASSUMED})
        self.assertEqual(ledger.months[6].source, SOURCE_LEGACY)
        self.assertEqual({m.owed for m in ledger.months}, {Decimal("10983")})
        self.assertEqual(ledger.owed_total, Decimal("76881"))            # 7 × 10,983
        self.assertEqual(ledger.paid_total, Decimal("32949"))            # September's instalment
        self.assertEqual(ledger.months_settled, 3)
        self.assertEqual(ledger.remaining_months, 4)
        self.assertEqual(ledger.instalment(3, False), (3, Decimal("32949")))   # Apr–Jun
        self.assertEqual(ledger.instalment(1, False), (1, Decimal("10983")))
        self.assertEqual(ledger.instalment(None, True), (4, Decimal("43932")))  # everything left
        self.assertEqual(ledger.instalment(9, False), (4, Decimal("43932")))    # capped at remaining

    def test_revision_widens_every_month_and_tops_up_the_settled_ones(self):
        new = self._revise()
        self.current.refresh_from_db()
        self.assertEqual(self.current.status, SalaryStructure.Status.CANCELLED)
        self.assertEqual(self.current.effective_to, date(2026, 1, 1))     # never "closed before it opened"
        self.assertIn("same-date correction", self.current.note)
        self.assertEqual((new.effective_from, new.gross_monthly, new.status),
                         (date(2026, 1, 1), REVISED, SalaryStructure.Status.APPROVED))
        ledger = adjustment_ledgers([self.emp], PayrollPeriod.for_month(2026, 10))[self.emp.pk]
        self.assertEqual(len(ledger.months), 9)                          # Aug and Sep now owe 9,183 each
        self.assertEqual(ledger.owed_total, Decimal("159528"))           # 7 × 20,166 + 2 × 9,183
        self.assertEqual(ledger.balance, Decimal("126579"))
        self.assertEqual(ledger.remaining_months, 6)
        # "3 more months" = Apr–Jun at 20,166 PLUS the Jan–Mar top-up (3 × 9,183).
        self.assertEqual(ledger.instalment(3, False), (3, Decimal("88047")))
        self.assertEqual(ledger.instalment(None, True), (6, Decimal("126579")))

    def test_has_pay_record_needs_a_real_month(self):
        ledger = adjustment_ledgers([self.emp], PayrollPeriod.for_month(2026, 10))[self.emp.pk]
        self.assertTrue(ledger.has_pay_record)                           # July is imported
        LegacyPayrollMonth.objects.filter(employee=self.emp).delete()
        ledger = adjustment_ledgers([self.emp], PayrollPeriod.for_month(2026, 10))[self.emp.pk]
        self.assertEqual(len(ledger.months), 9)                          # all assumed now
        self.assertFalse(ledger.has_pay_record)

    def test_employee_without_a_shortfall_has_no_owed_months(self):
        other = _user("flat@example.com", "540", department=self.department, joining_date=date(2025, 1, 1))
        SalaryStructure.objects.create(
            employee=other, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("50000"), status=SalaryStructure.Status.APPROVED,
        )
        ledgers = adjustment_ledgers([other], PayrollPeriod.for_month(2026, 10))
        self.assertEqual(ledgers[other.pk].months, [])                   # no previous structure → nothing can be owed


class EngineTests(_Base):
    def test_no_instruction_pays_nothing(self):
        run = self._run(2026, 10)
        re, line = self._adjustment_line(run)
        self.assertIsNone(line)
        self.assertEqual(re.adjustment_months, 0)

    def test_instalments_follow_the_admin_month_by_month(self):
        octo = self._run(2026, 10, months=3)
        re, line = self._adjustment_line(octo)
        self.assertEqual(line.amount, Decimal("32949"))
        self.assertEqual((re.adjustment_months, line.quantity, line.unit), (3, Decimal("3"), "month"))
        summary = "3 months (Apr–Jun 2026) at 120,817 = 32,949 · owed to date 76,881 · paid before 32,949 (3 mo) · remaining 10,983 (1 mo)"
        self.assertEqual(line.calc_note.split("\n")[0], summary)
        self.assertEqual(line.calc_note.split("\n")[1:], re.inputs_json["salary_adjustment"]["explanation"])
        self.assertGreater(len(line.calc_note), 255)                     # no longer capped
        self.assertEqual(re.inputs_json["salary_adjustment"]["explanation"], [
            "Owed to date at 120,817: Jan–Jul 2026 paid at 109,834, short 10,983 × 7 = 76,881; total 76,881.",
            "Paid before: 32,949 for 3 months (Jan–Mar 2026), worth 32,949 at 120,817.",
            "This run: Apr–Jun 2026 short 10,983 × 3 = 32,949.",
            "Remaining after this run: Jul 2026 short 10,983; total 10,983 (1 month).",
        ])
        self.assertEqual(re.inputs_json["salary_adjustment"]["remaining_after"], 1)
        # The previous-rate assumption: one run-level summary naming the
        # employee, the months themselves on the row.
        summary = PayrollControlCheck.objects.get(run=octo, check_code="SALARY_ADJUSTMENT_ASSUMED_PAID")
        self.assertIsNone(summary.employee)
        self.assertIn("1 employee(s)", summary.message)
        self.assertIn("539", summary.message)
        self.assertTrue(any("Jan 2026–Jun 2026" in w for w in re.calc_warnings))
        # Taxable: it rides into the true-up's current-period extras.
        tc = TaxComputation.objects.get(run=octo, employee=self.emp)
        self.assertEqual(tc.breakdown_json["ytd"]["current_period_extra"], "32949.00")
        # Recalculate is idempotent — the run never reads its own line.
        calculate_run(octo.pk, self.actor.pk)
        self.assertEqual(self._adjustment_line(octo)[1].amount, Decimal("32949"))

        nov = self._run(2026, 11, settle_all=True)
        re, line = self._adjustment_line(nov)
        self.assertEqual((line.amount, re.adjustment_months), (Decimal("10983"), 1))
        self.assertIn("remaining 0 (0 mo)", line.calc_note.split("\n")[0])
        self.assertEqual(re.inputs_json["salary_adjustment"]["explanation"][-1],
                         "Remaining after this run: nothing — the adjustment is complete.")

        dec = self._run(2026, 12, settle_all=True)
        re, line = self._adjustment_line(dec)
        self.assertIsNone(line)                                           # nothing left
        self.assertEqual(re.adjustment_months, 0)

    def test_no_pay_record_at_all_pays_nothing_and_warns(self):
        """The go-live trap: an employee whose pre-Orbis months were never
        imported must not be paid an adjustment on the assumption that
        nothing was ever paid to them."""
        LegacyPayrollMonth.objects.filter(employee=self.emp).delete()
        octo = self._run(2026, 10, months=3)
        re, line = self._adjustment_line(octo)
        self.assertIsNone(line)
        self.assertEqual(re.adjustment_months, 0)
        check = PayrollControlCheck.objects.get(run=octo, check_code="SALARY_ADJUSTMENT_NO_RECORD", employee=self.emp)
        self.assertIn("Import their pre-Orbis months first", check.message)
        self.assertFalse(PayrollControlCheck.objects.filter(run=octo, check_code="SALARY_ADJUSTMENT_ASSUMED_PAID").exists())

    def test_revision_after_an_orbis_instalment_tops_up_on_the_next_run(self):
        octo = self._run(2026, 10, months=3)                              # 32,949 at the old difference
        self._revise()
        nov = self._run(2026, 11, months=3)
        re, line = self._adjustment_line(nov)
        # Settled so far: 6 months (3 legacy + 3 in October) for 65,898.
        # Owed now: Jan–Jun 6 × 20,166 = 120,996, Jul 20,166, Aug/Sep 9,183
        # each, October paid 120,817 → 9,183. "3 more" = Jul, Aug, Sep →
        # owed(first 9) = 159,528 − 65,898 = 93,630.
        self.assertEqual(line.amount, Decimal("93630"))
        self.assertEqual(re.adjustment_months, 3)
        self.assertEqual(re.inputs_json["salary_adjustment"]["explanation"], [
            "Owed to date at 130,000: Jan–Jul 2026 paid at 109,834, short 20,166 × 7 = 141,162; "
            "Aug–Oct 2026 paid at 120,817, short 9,183 × 3 = 27,549; total 168,711.",
            "Paid before: 65,898 for 6 months (Jan–Jun 2026), worth 120,996 at 130,000 → top-up 55,098.",
            "This run: Jul 2026 short 20,166; Aug–Sep 2026 short 9,183 × 2 = 18,366; new months 38,532 + top-up 55,098 = 93,630.",
            "Remaining after this run: Oct 2026 short 9,183; total 9,183 (1 month).",
        ])
        self.assertEqual(re.inputs_json["salary_adjustment"]["remaining_after"], 1)   # October itself
        self.assertEqual(
            [m["source"] for m in re.inputs_json["salary_adjustment"]["months"]][-1], SOURCE_RUN,
        )


class RunPageTests(_Base):
    def setUp(self):
        super().setUp()
        group, _ = Group.objects.get_or_create(name="Payroll Admins")
        _grant(["payroll_run_detail"], group)
        self.admin = _user("padmin@example.com", "701")
        self.admin.groups.add(group)
        self.client = Client(SERVER_NAME="localhost")

    def test_admin_sets_months_and_the_run_recalculates(self):
        run = lock_inputs(create_run(PayrollPeriod.for_month(2026, 10), self.actor), self.actor)
        self.client.force_login(self.admin)
        url = reverse("payroll_run_detail", args=[run.pk])
        resp = self.client.get(url)
        self.assertContains(resp, reverse("payroll_run_adjustment", args=[run.pk]))   # the header button
        drawer = self.client.get(reverse("payroll_run_adjustment", args=[run.pk]), HTTP_HX_REQUEST="true")
        self.assertContains(drawer, "Months to settle in this run")
        self.assertContains(drawer, 'name="action" value="adjustment"')
        # A direct (non-htmx) hit just lands on the run page.
        self.assertEqual(self.client.get(reverse("payroll_run_adjustment", args=[run.pk])).status_code, 302)
        resp = self.client.post(url, {"action": "adjustment", "months": "3"})
        self.assertEqual(resp.status_code, 302)
        run.refresh_from_db()
        self.assertEqual((run.adjustment_months, run.adjustment_settle_all, run.status),
                         (3, False, PayrollRun.Status.CALCULATED))
        self.assertEqual(self._adjustment_line(run)[1].amount, Decimal("32949"))
        self.assertContains(self.client.get(url), '<span class="badge-info ml-1">3 months</span>')
        # The Calculated tab grows an "Adjustment" column, its export too,
        # and the Adjust-inputs drawer explains the figure line by line.
        page = self.client.get(url + "?tab=calculated")
        self.assertContains(page, "<th class=\"text-right\">Salary adjustment</th>")   # short_name blank in the test seed
        self.assertContains(page, "32,949")
        from io import BytesIO
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(self.client.get(url + "?tab=calculated&export=xlsx").content)).active
        headers = [c.value for c in ws[1]]
        self.assertIn("Salary adjustment", headers)
        row = next(ws.iter_rows(min_row=2, max_row=2, values_only=True))
        self.assertEqual(row[headers.index("Salary adjustment")], 32949)
        self.assertEqual(row[headers.index("Gross")], 32949 + 120817)
        _grant(["payroll_run_employee_adjust"], Group.objects.get(name="Payroll Admins"))
        drawer = self.client.get(reverse("payroll_run_employee_adjust", args=[run.pk, self.emp.pk]), HTTP_HX_REQUEST="true")
        self.assertContains(drawer, "Calculated lines")
        self.assertContains(drawer, "Owed to date at 120,817")
        self.assertContains(drawer, "Remaining after this run: Jul 2026 short 10,983; total 10,983 (1 month).")
        self.client.post(url, {"action": "adjustment", "months": "all"})
        run.refresh_from_db()
        self.assertTrue(run.adjustment_settle_all)
        self.assertEqual(self._adjustment_line(run)[1].amount, Decimal("43932"))
        self.client.post(url, {"action": "adjustment", "months": ""})
        run.refresh_from_db()
        self.assertFalse(run.adjustment_settle_all or run.adjustment_months)
        self.assertIsNone(self._adjustment_line(run)[1])

    def test_outsider_cannot_set_it(self):
        run = lock_inputs(create_run(PayrollPeriod.for_month(2026, 10), self.actor), self.actor)
        outsider = _user("out@example.com", "702")
        _grant(["payroll_run_detail"], Group.objects.get(name="Payroll Admins"))
        self.client.force_login(outsider)
        resp = self.client.post(reverse("payroll_run_detail", args=[run.pk]), {"action": "adjustment", "months": "3"})
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(self.client.get(reverse("payroll_run_adjustment", args=[run.pk]), HTTP_HX_REQUEST="true").status_code, 403)
        run.refresh_from_db()
        self.assertIsNone(run.adjustment_months)
