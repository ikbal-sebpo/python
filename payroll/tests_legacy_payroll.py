"""Pre-Orbis payroll months + tax challans — the two cutover-era uploaders,
the monthly payroll ledger and the month export
(docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md).

The months sheet IS the export's shape: one row per employee per month, one
column per pay component (earnings then deductions, generated from
PayComponent), then TDS and net — plus, on the export only, the two derived
columns (reimbursement = policy tier of the gross, challan = the
TaxChallanAllocation rows). These tests pin that round trip, the
pre-Orbis-only month rule, the soft/hard split of the checks, that apply
rewrites the true-up openings from the taxable earnings only, the permanent
challan uploader (tax year from the deposit date, typed payroll month,
optional link to a calculated run) and the per-employee "TDS by challan"
uploader (Revision 2).
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
from openpyxl import Workbook, load_workbook

from accounts.models import Department
from dynamic_menu.models import ProtectedView
from employees.models import JobInfo
from payroll.engine import calculate_run
from payroll.models import (
    Bank, EmployeeTaxDeclaration, LegacyPayrollMonth, PayComponent, PayrollInputBatch, PayrollPeriod, PayrollRunEmployee,
    SalaryStructure, TaxChallan, TaxChallanAllocation, TaxYear, VariablePayEntry,
)
from payroll.services import create_run, lock_inputs
from payroll.tax_ledger import (
    ALLOCATION_HEADERS, ALLOCATION_SHEET, CHALLAN_HEADERS, CHALLAN_SHEET, MONTHS_SHEET, header_key, is_legacy_month,
    ledger_headers, legacy_months, monthly_ledger_rows, resolve_allocation_headers, resolve_ledger_headers,
)
from payroll.tests_tax import _seed_tax_year, _user

User = get_user_model()
_MEDIA_ROOT = tempfile.mkdtemp(prefix="orbis-legacy-payroll-tests-")

EXPECTED_HEADERS = [
    "Employee ID", "Payroll month (YYYY-MM)",
    "Gross salary", "Salary adjustment", "Overtime allowance", "Festival bonus", "Performance bonus", "Incentive",
    "Meal deduction", "Loan repayment",
    "Mobile & Internet reimbursement", "TDS deducted", "Net pay", "Salary adjustment months", "Challan No", "Employee Name",
]
# The cutover sheet Finance fills: the export minus its two derived columns.
TEMPLATE_HEADERS = [h for h in EXPECTED_HEADERS if h not in ("Mobile & Internet reimbursement", "Challan No")]


def _components():
    """Earnings and deductions the sheet is generated from; TDS stays a
    fixed column; an inactive component must not appear."""
    specs = [
        ("GROSS_SALARY", "Gross salary", PayComponent.Kind.EARNING, PayComponent.EntryMode.MASTER, 10, True),
        ("SALARY_ADJUSTMENT", "Salary adjustment", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, 15, True),
        ("OT_ALLOWANCE", "Overtime allowance", PayComponent.Kind.EARNING, PayComponent.EntryMode.COMPUTED, 30, True),
        ("MEAL_DEDUCTION", "Meal deduction", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.COMPUTED, 50, False),
        ("TDS", "Tax deducted at source", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.INPUT, 60, False),
        ("FESTIVAL_BONUS", "Festival bonus", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, 68, True),
        ("BONUS", "Performance bonus", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, 70, True),
        ("INCENTIVE", "Incentive", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, 80, True),
        ("LOAN_REPAYMENT", "Loan repayment", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.COMPUTED, 97, False),
    ]
    out = {}
    for code, name, kind, mode, seq, taxable in specs:
        out[code] = PayComponent.objects.create(
            code=code, name=name, kind=kind, entry_mode=mode, sequence=seq,
            is_taxable=taxable, is_prorated=(code == "GROSS_SALARY"),
            short_name="Loan" if code == "LOAN_REPAYMENT" else "",
        )
    PayComponent.objects.create(
        code="OLD_THING", name="Retired allowance", kind=PayComponent.Kind.EARNING,
        entry_mode=PayComponent.EntryMode.INPUT, sequence=99, is_active=False,
    )
    return out


def _live_year(live_month=(2026, 10)):
    tax_year, categories = _seed_tax_year()
    tax_year.calculation_method = TaxYear.CalculationMethod.ITA_2023_SALARY_TRUEUP
    tax_year.orbis_live_from = PayrollPeriod.for_month(*live_month)
    tax_year.save()
    return tax_year, categories


def _xlsx(rows, headers, *, title=MONTHS_SHEET, name="upload.xlsx"):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    stream = BytesIO()
    wb.save(stream)
    return SimpleUploadedFile(name, stream.getvalue())


def _grant(view_names, group):
    for name in view_names:
        pv, _ = ProtectedView.objects.get_or_create(view_name=name, defaults={"registered_app": "payroll"})
        pv.allowed_groups.set([group])


CH1 = ("2627-00042394161", date(2026, 8, 14), "BankAsia PLC", 817256, "2026-07", "July payroll")
CH2 = ("2627-00074118871", date(2026, 9, 11), "MDB", 805325, "2026-08", "")


class LedgerShapeTests(TestCase):
    def setUp(self):
        self.components = _components()
        self.tax_year, _ = _live_year()

    def test_headers_are_every_active_component_earnings_first_then_fixed_columns(self):
        self.assertEqual(ledger_headers(), EXPECTED_HEADERS)
        self.assertEqual(ledger_headers(template=True), TEMPLATE_HEADERS)

    def test_header_matching_is_forgiving_but_refuses_unknowns(self):
        self.assertEqual(header_key("Payroll month (YYYY-MM)"), "payroll month")
        self.assertEqual(header_key("Mobile & Internet Bill (Reimburse)"), "mobile internet bill")
        comps = [self.components[c] for c in ("GROSS_SALARY", "INCENTIVE", "LOAN_REPAYMENT")]
        mapping, errors = resolve_ledger_headers(
            ["Employee ID", "Payroll Month", "GROSS SALARY", "incentive", "Loan", "TDS Deducted", "Net take home",
             "Challan Number", "Employee Name"],
            comps,
        )
        self.assertEqual(errors, [])
        # "Challan Number" is a derived export column — reference, skipped.
        self.assertEqual(mapping, {
            0: "employee_id", 1: "month", 2: "GROSS_SALARY", 3: "INCENTIVE", 4: "LOAN_REPAYMENT", 5: "tds", 6: "net",
        })
        mapping, errors = resolve_allocation_headers(["Employee ID", "Challan No", "Amount", "Employee Name"])
        self.assertEqual((mapping, errors), ({0: "employee_id", 1: "challan", 2: "amount"}, []))
        _, errors = resolve_allocation_headers(["Employee ID", "Amount"])
        self.assertEqual(len(errors), 1)
        self.assertIn("Challan No", errors[0])
        _, errors = resolve_ledger_headers(["Employee ID", "Payroll month", "Lunch allowance", "TDS deducted"], comps)
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown column 'Lunch allowance'", errors[0])
        _, errors = resolve_ledger_headers(["Employee ID", "Gross salary"], comps)
        self.assertTrue(any("Payroll month" in e for e in errors) and any("TDS deducted" in e for e in errors))

    def test_legacy_months_are_the_tax_year_months_before_live_from(self):
        self.assertEqual(legacy_months(self.tax_year), [(2026, 7), (2026, 8), (2026, 9)])
        self.assertTrue(is_legacy_month(self.tax_year, 2026, 9))
        self.assertFalse(is_legacy_month(self.tax_year, 2026, 10))
        self.tax_year.orbis_live_from = None
        self.tax_year.save()
        self.assertEqual(legacy_months(self.tax_year), [])

    def test_taxable_additional_pay_counts_taxable_earnings_only(self):
        emp = _user("x@example.com", "900")
        lm = LegacyPayrollMonth(
            tax_year=self.tax_year, employee=emp, year=2026, month=7,
            lines={
                "GROSS_SALARY": "100000", "FESTIVAL_BONUS": "50000", "BONUS": "1000", "INCENTIVE": "5000",
                "SALARY_ADJUSTMENT": "2000", "MEAL_DEDUCTION": "900", "LOAN_REPAYMENT": "5000",
            },
        )
        self.assertEqual(lm.taxable_additional_pay(), Decimal("8000"))


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TaxChallanUploadTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.components = _components()
        self.tax_year, _ = _live_year()
        self.bank_asia = Bank.objects.create(name="Bank Asia PLC", short_code="BAL")
        self.modhumoti = Bank.objects.create(name="Modhumoti Bank Limited", short_code="MDB")
        finance, _ = Group.objects.get_or_create(name="Finance")
        _grant(["payroll_tax_challan_upload", "payroll_tax_challan_upload_template"], finance)
        self.finance = _user("fin@example.com", "730")
        self.finance.groups.add(finance)
        self.outsider = _user("out@example.com", "702")
        self.client = Client(SERVER_NAME="localhost")
        self.url = reverse("payroll_tax_challan_upload")

    def _file(self, rows):
        return _xlsx(rows, CHALLAN_HEADERS, title=CHALLAN_SHEET, name="challans.xlsx")

    def test_outsider_is_refused(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 403)

    def test_template_downloads(self):
        self.client.force_login(self.finance)
        resp = self.client.get(reverse("payroll_tax_challan_upload_template"))
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual([c.value for c in wb[CHALLAN_SHEET][1]], list(CHALLAN_HEADERS))

    def test_preview_then_apply_creates_and_reupload_corrects(self):
        self.client.force_login(self.finance)
        resp = self.client.post(self.url, {"action": "preview", "file": self._file([CH1, CH2])})
        self.assertContains(resp, "Preview only")
        self.assertFalse(TaxChallan.objects.exists())

        resp = self.client.post(self.url, {"action": "apply", "file": self._file([CH1, CH2])})
        self.assertEqual(resp.status_code, 200)
        ch1 = TaxChallan.objects.get(challan_number=CH1[0])
        self.assertEqual(ch1.tax_year, self.tax_year)                      # from the deposit date
        self.assertEqual(ch1.bank, self.bank_asia)                         # "BankAsia PLC" → seeded name
        self.assertEqual(ch1.total_amount, Decimal("817256"))
        self.assertIsNone(ch1.run)                                         # July is pre-Orbis: no run to link
        self.assertEqual((ch1.period_year, ch1.period_month), (2026, 7))   # …but the typed month is kept
        self.assertEqual(ch1.source, TaxChallan.Source.IMPORTED)
        self.assertEqual(TaxChallan.objects.get(challan_number=CH2[0]).bank, self.modhumoti)   # short code
        batch = PayrollInputBatch.objects.get(input_kind=PayrollInputBatch.InputKind.TAX_CHALLAN)
        self.assertEqual(batch.status, PayrollInputBatch.Status.APPLIED)

        corrected = (CH1[0], date(2026, 8, 15), "Bank Asia", 817300, "", "corrected")
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([corrected])})
        self.assertContains(resp, "1</span> updated")
        ch1.refresh_from_db()
        self.assertEqual(ch1.deposit_date, date(2026, 8, 15))
        self.assertEqual(ch1.total_amount, Decimal("817300"))
        self.assertEqual(ch1.note, "corrected")
        self.assertEqual((ch1.period_year, ch1.period_month), (2026, 7))   # blank month cell leaves it alone
        self.assertEqual(TaxChallan.objects.count(), 2)

    def test_errors_abort_the_file(self):
        self.client.force_login(self.finance)
        rows = [
            ("X-1", date(2026, 8, 14), "Standard Chartered", 100, "", ""),      # unknown bank
            ("X-2", date(2026, 8, 14), "BAL", 0, "", ""),                        # zero total
            ("X-3", date(2019, 8, 14), "BAL", 100, "", ""),                      # no tax year covers it
            ("X-4", date(2026, 8, 14), "BAL", 100, "2027-09", ""),               # month outside the year
            ("X-5", date(2026, 8, 14), "BAL", 100, "", ""),
            ("X-5", date(2026, 8, 14), "BAL", 100, "", ""),                      # duplicate
        ]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertContains(resp, "aborted")
        self.assertContains(resp, "Unknown bank")
        self.assertContains(resp, "greater than zero")
        self.assertContains(resp, "No tax year covers")
        self.assertContains(resp, "not a month of tax year")
        self.assertContains(resp, "also appears on row")
        self.assertFalse(TaxChallan.objects.exists())

    def test_orbis_month_without_a_run_warns_and_links_on_reupload(self):
        self.client.force_login(self.finance)
        row = ("2627-9", date(2026, 11, 12), "BAL", 1000, "2026-10", "")
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([row])})
        self.assertContains(resp, "No calculated payroll run yet")
        ch = TaxChallan.objects.get(challan_number="2627-9")
        self.assertIsNone(ch.run)
        self.assertEqual((ch.period_year, ch.period_month), (2026, 10))
        # Now a calculated run exists for October — the re-upload links it.
        dept = Department.objects.create(name="Software Development")
        emp = _user("run@example.com", "600", department=dept, joining_date=date(2025, 1, 1))
        JobInfo.objects.create(employee=emp, resource_status=JobInfo.ResourceStatus.LIVE)
        SalaryStructure.objects.create(
            employee=emp, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("50000"), status=SalaryStructure.Status.APPROVED,
        )
        actor = User.objects.create_superuser(email="a@example.com", username="a@example.com", password="x")
        run = calculate_run(lock_inputs(create_run(PayrollPeriod.for_month(2026, 10), actor), actor).pk, actor.pk)
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([row])})
        self.assertNotContains(resp, "No calculated payroll run yet")
        ch.refresh_from_db()
        self.assertEqual(ch.run, run)
        self.assertEqual(ch.source, TaxChallan.Source.RUN)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class LegacyPayrollUploadViewTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.components = _components()
        self.tax_year, self.categories = _live_year()
        self.bank_asia = Bank.objects.create(name="Bank Asia PLC", short_code="BAL")
        self.modhumoti = Bank.objects.create(name="Modhumoti Bank Limited", short_code="MDB")
        group, _ = Group.objects.get_or_create(name="Payroll Admins")
        _grant(["payroll_legacy_payroll_upload", "payroll_legacy_payroll_upload_template", "payroll_legacy_payroll_export"], group)
        self.admin = _user("padmin@example.com", "701")
        self.admin.groups.add(group)
        self.emp = _user("emp@example.com", "700", joining_date=date(2025, 1, 1))
        self.late_joiner = _user("new@example.com", "710", joining_date=date(2026, 9, 1))   # employed in Sep only
        self.leaver = _user("gone@example.com", "720", joining_date=date(2020, 1, 1), last_working_date=date(2026, 5, 31))
        self.outsider = _user("out@example.com", "702")
        self.client = Client(SERVER_NAME="localhost")
        self.url = reverse("payroll_legacy_payroll_upload")
        self.ch1 = TaxChallan.objects.create(
            tax_year=self.tax_year, challan_number=CH1[0], deposit_date=CH1[1], bank=self.bank_asia,
            total_amount=Decimal(CH1[3]), period_year=2026, period_month=7,
        )
        self.ch2 = TaxChallan.objects.create(
            tax_year=self.tax_year, challan_number=CH2[0], deposit_date=CH2[1], bank=self.modhumoti,
            total_amount=Decimal(CH2[3]), period_year=2026, period_month=8,
        )

    # -- helpers ----------------------------------------------------------
    def _row(self, emp_id, month, *, gross=0, arrear=0, ot=0, festival=0, bonus=0, incentive=0, meal=0, loan=0,
             tds=0, net=None, adj_months=None):
        return [emp_id, month, gross, arrear, ot, festival, bonus, incentive, meal, loan, tds, net, adj_months, "ref name"]

    def _file(self, rows, headers=None):
        return _xlsx(rows, headers or ledger_headers(template=True))

    def _declaration(self):
        return EmployeeTaxDeclaration.objects.get(employee=self.emp, tax_year=self.tax_year)

    # -- access ------------------------------------------------------------
    def test_outsider_is_refused(self):
        self.client.force_login(self.outsider)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("home"))
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 403)
        self.assertEqual(self.client.get(reverse("payroll_legacy_payroll_export") + "?year=2026&month=7").status_code, 302)

    def test_upload_center_shows_both_cards_to_payroll_admins(self):
        pv, _ = ProtectedView.objects.get_or_create(view_name="payroll_upload_center", defaults={"registered_app": "payroll"})
        pv.allowed_groups.set(list(Group.objects.filter(name="Payroll Admins")))
        _grant(["payroll_tax_challan_upload", "payroll_tax_challan_allocation_upload"], Group.objects.get(name="Payroll Admins"))
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("payroll_upload_center"))
        self.assertContains(resp, "Pre-Orbis payroll months (cutover)")
        self.assertContains(resp, "Tax challans")
        self.assertContains(resp, "TDS by challan (per employee)")

    # -- template ----------------------------------------------------------
    def test_template_is_one_sheet_prefilled_with_employed_months_only(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("payroll_legacy_payroll_upload_template"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pre_orbis_payroll_months_2026-27", resp["Content-Disposition"])
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, [MONTHS_SHEET])
        ws = wb[MONTHS_SHEET]
        self.assertEqual([c.value for c in ws[1]], TEMPLATE_HEADERS)     # no derived columns on the sheet Finance fills
        rows = {(r[0], r[1]) for r in ws.iter_rows(min_row=2, values_only=True)}
        self.assertIn(("700", "2026-07"), rows)
        self.assertIn(("700", "2026-09"), rows)
        self.assertIn(("710", "2026-09"), rows)            # joined 1 Sep → Sep period (16 Aug–15 Sep) only
        self.assertNotIn(("710", "2026-08"), rows)
        self.assertFalse(any(eid == "720" for eid, _ in rows))  # left in May
        first = next(ws.iter_rows(min_row=2, max_row=2, values_only=True))
        self.assertIsNone(first[2])                        # amounts blank, not 0

    def test_template_refuses_without_live_from(self):
        self.tax_year.orbis_live_from = None
        self.tax_year.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("payroll_legacy_payroll_upload_template")).status_code, 302)

    # -- parse + apply -----------------------------------------------------
    def test_preview_writes_nothing_then_apply_writes_months_and_openings(self):
        self.client.force_login(self.admin)
        rows = [
            self._row("700", "2026-07", gross=109834, meal=900, loan=5000, tds=2960, net=100974),
            self._row("700", "2026-08", gross=109834, festival=54917, incentive=5000, tds=2960),
            self._row("700", "2026-09", gross=109834, arrear=2000, ot=4500, bonus=1000, meal=900, tds=2960, adj_months=3),
        ]
        resp = self.client.post(self.url, {"action": "preview", "file": self._file(rows)})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Preview only")
        self.assertFalse(LegacyPayrollMonth.objects.exists())
        self.assertFalse(EmployeeTaxDeclaration.objects.filter(employee=self.emp).exists())

        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyPayrollMonth.objects.filter(employee=self.emp).count(), 3)
        jul = LegacyPayrollMonth.objects.get(employee=self.emp, month=7)
        self.assertEqual(jul.lines, {"GROSS_SALARY": "109834.00", "MEAL_DEDUCTION": "900.00", "LOAN_REPAYMENT": "5000.00"})
        self.assertEqual(jul.net_pay, Decimal("100974"))
        sep = LegacyPayrollMonth.objects.get(employee=self.emp, month=9)
        self.assertIsNone(sep.net_pay)
        self.assertEqual(sep.adjustment_months, 3)
        self.assertIsNone(LegacyPayrollMonth.objects.get(employee=self.emp, month=7).adjustment_months)
        d = self._declaration()
        self.assertEqual(d.opening_tds_deducted, Decimal("8880"))
        # additional pay: incentive 5,000 + adjustment 2,000 + OT 4,500 + performance bonus 1,000;
        # NOT gross, NOT festival bonus, and no deduction (meal, loan) ever.
        self.assertEqual(d.opening_additional_pay, Decimal("12500"))
        batch = PayrollInputBatch.objects.get(input_kind=PayrollInputBatch.InputKind.LEGACY_MONTH)
        self.assertEqual(batch.status, PayrollInputBatch.Status.APPLIED)
        self.assertEqual(batch.row_count, 3)
        self.assertEqual(sep.batch, batch)

    def test_reupload_overwrites_one_month_and_recomputes_openings_over_all_months(self):
        self.client.force_login(self.admin)
        rows = [
            self._row("700", "2026-07", gross=100000, tds=1000),
            self._row("700", "2026-08", gross=100000, incentive=3000, tds=1000),
        ]
        self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertEqual(self._declaration().opening_tds_deducted, Decimal("2000"))
        self.client.post(self.url, {"action": "apply", "file": self._file(
            [self._row("700", "2026-08", gross=100000, incentive=7000, tds=1500)],
        )})
        self.assertEqual(LegacyPayrollMonth.objects.filter(employee=self.emp).count(), 2)
        aug = LegacyPayrollMonth.objects.get(employee=self.emp, month=8)
        self.assertEqual(aug.tds_deducted, Decimal("1500"))
        self.assertEqual(aug.lines["INCENTIVE"], "7000.00")
        d = self._declaration()
        self.assertEqual(d.opening_tds_deducted, Decimal("2500"))           # July 1,000 + new August 1,500
        self.assertEqual(d.opening_additional_pay, Decimal("7000"))

    def test_orbis_months_and_months_outside_the_year_are_refused(self):
        self.client.force_login(self.admin)
        rows = [
            self._row("700", "2026-10", gross=1),      # live-from month itself
            self._row("700", "2026-06", gross=1),      # previous tax year
            self._row("700", "2026-07", gross=1),
            self._row("700", "2026-07", gross=2),      # duplicate
            self._row("999", "2026-07", gross=1),      # unknown employee
        ]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertContains(resp, "aborted")
        self.assertContains(resp, "2026-10 is not a pre-Orbis month")
        self.assertContains(resp, "2026-06 is not a pre-Orbis month")
        self.assertContains(resp, "also appears on row")
        self.assertContains(resp, "No employee with ID")
        self.assertFalse(LegacyPayrollMonth.objects.exists())

    def test_unknown_column_fails_the_whole_file(self):
        self.client.force_login(self.admin)
        headers = ledger_headers(template=True)
        headers.insert(3, "Lunch allowance")
        rows = [["700", "2026-07", 1000, 0, 0, 0, 0, 0, 0, 0, 0, 0, None, None, "x"]]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows, headers)})
        self.assertContains(resp, "Unknown column")
        self.assertFalse(LegacyPayrollMonth.objects.exists())

    def test_export_only_columns_are_ignored_on_the_way_back_in(self):
        """Mobile & Internet and Challan No are derived on the export and
        never typed — a file carrying them (an export, or Finance's own
        register) imports without an unknown-column error, and whatever is
        in those cells is ignored."""
        self.client.force_login(self.admin)
        row = ["700", "2026-07", 1000, 0, 0, 0, 0, 0, 0, 0, 9999, 100, None, None, "NOPE-1", "x"]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([row], ledger_headers())})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Unknown column")
        self.assertEqual(LegacyPayrollMonth.objects.get(employee=self.emp, month=7).tds_deducted, Decimal("100"))

    def test_soft_checks_are_warnings_not_blockers(self):
        self.client.force_login(self.admin)
        rows = [self._row("700", "2026-07", gross=1000, tds=100)]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "no row for 2026-08, 2026-09")
        self.assertEqual(LegacyPayrollMonth.objects.count(), 1)

    def test_no_live_from_is_an_inline_error(self):
        self.tax_year.orbis_live_from = None
        self.tax_year.save()
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([self._row("700", "2026-07", gross=1)])})
        self.assertContains(resp, "Orbis live from")
        self.assertFalse(LegacyPayrollMonth.objects.exists())

    def test_csv_works_too(self):
        self.client.force_login(self.admin)
        row = self._row("700", "2026-07", gross=5000, tds=100)
        csv = ",".join(ledger_headers(template=True)) + "\n" + ",".join("" if v is None else str(v) for v in row) + "\n"
        resp = self.client.post(self.url, {"action": "apply", "file": SimpleUploadedFile("m.csv", csv.encode())})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyPayrollMonth.objects.get(employee=self.emp).tds_deducted, Decimal("100"))

    # -- export ------------------------------------------------------------
    def test_export_of_a_legacy_month_round_trips_the_import(self):
        self.client.force_login(self.admin)
        rows = [
            self._row("700", "2026-08", gross=109834, festival=54917, incentive=5000, meal=900, loan=5000,
                      tds=2960, net=160891),
            self._row("710", "2026-08", tds=10),
        ]
        self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        TaxChallanAllocation.objects.create(challan=self.ch2, employee=self.emp, amount=Decimal("2960"))
        # 700 is on a 125k structure, but August was PAID at 109,834 (a
        # January increment first paid later) — the tier follows the paid
        # gross line, not the structure. 710's row has no gross line, so
        # its structure is the fallback.
        for emp, gross in ((self.emp, "125000"), (self.late_joiner, "125000")):
            SalaryStructure.objects.create(
                employee=emp, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.ANNUAL_INCREMENT,
                gross_monthly=Decimal(gross), status=SalaryStructure.Status.APPROVED,
            )
        resp = self.client.get(reverse("payroll_legacy_payroll_export") + "?year=2026&month=8")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tax_ledger_2026-08", resp["Content-Disposition"])
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, [MONTHS_SHEET, CHALLAN_SHEET])
        ws = wb[MONTHS_SHEET]
        self.assertEqual([c.value for c in ws[1]], EXPECTED_HEADERS)
        exported = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
        self.assertEqual(exported["700"][1], "2026-08")
        self.assertEqual(exported["700"][2], 109834)
        self.assertEqual(exported["700"][5], 54917)
        self.assertEqual(exported["700"][7], 5000)
        self.assertEqual(exported["700"][8], 900)
        self.assertEqual(exported["700"][9], 5000)
        self.assertEqual(exported["700"][10], 3000)        # derived: 80k–110k tier of the 109,834 PAID gross, not the 125k structure
        self.assertEqual(exported["700"][11], 2960)
        self.assertEqual(exported["700"][12], 160891)
        self.assertEqual(exported["700"][14], CH2[0])      # derived: the allocation row
        self.assertEqual(exported["710"][10], 4000)        # derived: no gross line → 110k–150k tier of the structure in force
        self.assertIsNone(exported["710"][14])            # no allocation yet
        self.assertIsNone(exported["710"][12])            # net not supplied → blank
        challans = [r[0] for r in wb[CHALLAN_SHEET].iter_rows(min_row=2, values_only=True)]
        self.assertEqual(challans, [CH2[0]])
        # …and the export imports back unchanged.
        before = LegacyPayrollMonth.objects.get(employee=self.emp, month=8).lines
        resp = self.client.post(self.url, {"action": "apply", "file": SimpleUploadedFile("export.xlsx", resp.content)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyPayrollMonth.objects.get(employee=self.emp, month=8).lines, before)

    def test_adjustment_months_column_rules(self):
        self.client.force_login(self.admin)
        # Paid an adjustment, months blank → accepted with a warning.
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(
            [self._row("700", "2026-09", gross=120817, arrear=32949, tds=100)],
        )})
        self.assertContains(resp, "no &#x27;Salary adjustment months&#x27;")
        self.assertIsNone(LegacyPayrollMonth.objects.get(employee=self.emp, month=9).adjustment_months)
        # Months set on a row that pays no adjustment → error; non-integer → error.
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([
            self._row("700", "2026-08", gross=120817, tds=100, adj_months=2),
            self._row("700", "2026-07", gross=109834, arrear=10, tds=100, adj_months="x"),
        ])})
        self.assertContains(resp, "pays no Salary adjustment")
        self.assertContains(resp, "whole number of months")
        # The export carries the count back out.
        self.client.post(self.url, {"action": "apply", "file": self._file(
            [self._row("700", "2026-09", gross=120817, arrear=32949, tds=100, adj_months=3)],
        )})
        resp = self.client.get(reverse("payroll_legacy_payroll_export") + "?year=2026&month=9")
        ws = load_workbook(BytesIO(resp.content))[MONTHS_SHEET]
        self.assertEqual(next(ws.iter_rows(min_row=2, max_row=2, values_only=True))[13], 3)

    def test_export_outside_the_tax_year_redirects(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("payroll_legacy_payroll_export") + "?year=2025&month=1").status_code, 302)


class OrbisMonthLedgerTests(TestCase):
    """An Orbis month is read from its calculated run — same shape as an
    imported month: every earning and deduction line, reimbursement from
    the engine's tier, net pay, challan when one is linked to the run."""

    def setUp(self):
        self.components = _components()
        self.tax_year, categories = _live_year()
        self.department = Department.objects.create(name="Software Development")
        self.employee = _user("run@example.com", "600", department=self.department, joining_date=date(2025, 1, 1))
        JobInfo.objects.create(employee=self.employee, resource_status=JobInfo.ResourceStatus.LIVE)
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("120817"), status=SalaryStructure.Status.APPROVED,
        )
        EmployeeTaxDeclaration.objects.create(employee=self.employee, tax_year=self.tax_year, category=categories["MEN"])
        self.actor = User.objects.create_superuser(email="a@example.com", username="a@example.com", password="x")

    def _run(self, year, month):
        period = PayrollPeriod.for_month(year, month)
        run = lock_inputs(create_run(period, self.actor), self.actor)
        return calculate_run(run.pk, self.actor.pk)

    def test_orbis_month_rows_come_from_the_run(self):
        period = PayrollPeriod.for_month(2026, 10)
        VariablePayEntry.objects.create(
            employee=self.employee, period=period, component=self.components["INCENTIVE"], amount=Decimal("5000"),
            status=VariablePayEntry.Status.APPROVED,
        )
        run = self._run(2026, 10)
        self.assertEqual(monthly_ledger_rows(self.tax_year, 2026, 9), [])     # legacy month, nothing imported
        rows = monthly_ledger_rows(self.tax_year, 2026, 10)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        run_emp = PayrollRunEmployee.objects.get(run=run, employee=self.employee)
        self.assertEqual(r.source, "ORBIS")
        self.assertEqual(r.amount("GROSS_SALARY"), Decimal("120817.00"))
        self.assertEqual(r.amount("INCENTIVE"), Decimal("5000.00"))
        self.assertNotIn("TDS", r.lines)                                      # fixed column, never a line
        self.assertEqual(r.mobile_internet_reimbursement, Decimal("4000"))     # 110k–150k tier
        self.assertEqual(r.tds_deducted, run_emp.tax_deducted)
        self.assertGreater(r.tds_deducted, 0)
        self.assertEqual(r.net_pay, run_emp.net_pay)
        self.assertEqual(r.challan_number, "")
        bank = Bank.objects.create(name="Bank Asia PLC", short_code="BAL")
        ch = TaxChallan.objects.create(
            tax_year=self.tax_year, challan_number="2627-9", deposit_date=date(2026, 11, 12), bank=bank,
            total_amount=Decimal("1"), run=run, source=TaxChallan.Source.RUN,
        )
        # A challan on the run alone says nothing per employee — the
        # allocation row does (and a second one joins in deposit order).
        self.assertEqual(monthly_ledger_rows(self.tax_year, 2026, 10)[0].challan_number, "")
        TaxChallanAllocation.objects.create(challan=ch, employee=self.employee, amount=r.tds_deducted)
        self.assertEqual(monthly_ledger_rows(self.tax_year, 2026, 10)[0].challan_number, "2627-9")
        ch2 = TaxChallan.objects.create(
            tax_year=self.tax_year, challan_number="2627-8", deposit_date=date(2026, 11, 10), bank=bank,
            total_amount=Decimal("1"), period_year=2026, period_month=10,
        )
        TaxChallanAllocation.objects.create(challan=ch2, employee=self.employee, amount=Decimal("1"))
        self.assertEqual(monthly_ledger_rows(self.tax_year, 2026, 10)[0].challan_number, "2627-8, 2627-9")

    def test_month_never_run_is_empty(self):
        self.assertEqual(monthly_ledger_rows(self.tax_year, 2026, 11), [])


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ChallanAllocationUploadTests(TestCase):
    """"TDS by challan (per employee)" — Employee ID | Challan No | Amount,
    one row per employee per deposit, the stored amount (Revision 2 §R2.3).
    Prefilled template from the month's TDS, errors abort, reconciliations
    warn, re-upload overwrites a pair."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.components = _components()
        self.tax_year, self.categories = _live_year()
        self.bank = Bank.objects.create(name="Bank Asia PLC", short_code="BAL")
        group, _ = Group.objects.get_or_create(name="Payroll Admins")
        _grant(["payroll_tax_challan_allocation_upload", "payroll_tax_challan_allocation_upload_template"], group)
        self.admin = _user("padmin@example.com", "701")
        self.admin.groups.add(group)
        self.emp = _user("emp@example.com", "700", joining_date=date(2025, 1, 1))
        self.emp2 = _user("two@example.com", "705", joining_date=date(2025, 1, 1))
        self.outsider = _user("out@example.com", "702")
        self.client = Client(SERVER_NAME="localhost")
        self.url = reverse("payroll_tax_challan_allocation_upload")
        # July has one challan, August two; September none yet.
        self.jul = self._challan("2627-JUL", date(2026, 8, 14), 5000, 2026, 7)
        self.aug_a = self._challan("2627-AUG-A", date(2026, 9, 11), 3000, 2026, 8)
        self.aug_b = self._challan("2627-AUG-B", date(2026, 9, 12), 1500, 2026, 8)
        self.untied = self._challan("2627-YE", date(2027, 6, 30), 100, None, None)
        for emp, tds in ((self.emp, "2960"), (self.emp2, "2040")):
            for month in (7, 8, 9):
                LegacyPayrollMonth.objects.create(
                    tax_year=self.tax_year, employee=emp, year=2026, month=month,
                    lines={"GROSS_SALARY": "100000"}, tds_deducted=Decimal(tds),
                )

    def _challan(self, number, deposit, total, year, month):
        return TaxChallan.objects.create(
            tax_year=self.tax_year, challan_number=number, deposit_date=deposit, bank=self.bank,
            total_amount=Decimal(total), period_year=year, period_month=month,
        )

    def _file(self, rows, headers=ALLOCATION_HEADERS):
        return _xlsx(rows, headers, title=ALLOCATION_SHEET, name="alloc.xlsx")

    def test_outsider_is_refused(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 403)
        self.assertEqual(self.client.get(reverse("payroll_tax_challan_allocation_upload_template")).status_code, 302)

    def test_template_is_prefilled_from_each_months_tds(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("payroll_tax_challan_allocation_upload_template"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tds_by_challan_2026-27", resp["Content-Disposition"])
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, [ALLOCATION_SHEET, CHALLAN_SHEET])
        ws = wb[ALLOCATION_SHEET]
        self.assertEqual([c.value for c in ws[1]], list(ALLOCATION_HEADERS))
        rows = [r[:3] for r in ws.iter_rows(min_row=2, values_only=True)]
        # July: one challan → number prefilled. August: two → blank for the admin.
        # September: no challan recorded → no rows. The untied challan → no rows.
        self.assertEqual(rows, [
            ("700", "2627-JUL", 2960), ("705", "2627-JUL", 2040),
            ("700", None, 2960), ("705", None, 2040),
        ])
        self.assertEqual([r[0] for r in wb[CHALLAN_SHEET].iter_rows(min_row=2, values_only=True)],
                         ["2627-JUL", "2627-AUG-A", "2627-AUG-B", "2627-YE"])
        self.assertEqual([r[4] for r in wb[CHALLAN_SHEET].iter_rows(min_row=2, values_only=True)],
                         ["2026-07", "2026-08", "2026-08", None])

    def test_preview_writes_nothing_then_apply_writes_and_reupload_overwrites(self):
        self.client.force_login(self.admin)
        rows = [("700", "2627-JUL", 2960, "x"), ("705", "2627-JUL", 2040, "x")]
        resp = self.client.post(self.url, {"action": "preview", "file": self._file(rows)})
        self.assertContains(resp, "Preview only")
        self.assertFalse(TaxChallanAllocation.objects.exists())

        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TaxChallanAllocation.objects.count(), 2)
        a = TaxChallanAllocation.objects.get(challan=self.jul, employee=self.emp)
        self.assertEqual(a.amount, Decimal("2960"))
        self.assertEqual(a.source, TaxChallanAllocation.Source.IMPORTED)
        self.assertEqual(a.added_by, self.admin)
        batch = PayrollInputBatch.objects.get(input_kind=PayrollInputBatch.InputKind.CHALLAN_ALLOC)
        self.assertEqual((batch.status, batch.row_count), (PayrollInputBatch.Status.APPLIED, 2))
        self.assertEqual(a.batch, batch)
        self.assertNotContains(resp, "Warnings")           # 2,960 + 2,040 = 5,000 = the challan; both months' TDS match
        # The export now shows the challan on each July row.
        self.assertEqual([r.challan_number for r in monthly_ledger_rows(self.tax_year, 2026, 7)], ["2627-JUL", "2627-JUL"])

        resp = self.client.post(self.url, {"action": "apply", "file": self._file([("700", "2627-JUL", 2900, "")])})
        self.assertContains(resp, "1</span> overwritten")
        self.assertEqual(TaxChallanAllocation.objects.count(), 2)
        self.assertEqual(TaxChallanAllocation.objects.get(challan=self.jul, employee=self.emp).amount, Decimal("2900"))
        self.assertContains(resp, "difference 60")        # 2,900 + 2,040 = 4,940 vs 5,000
        self.assertContains(resp, "700 (allocated 2,900, TDS 2,960)")

    def test_errors_abort_the_file(self):
        self.client.force_login(self.admin)
        rows = [
            ("999", "2627-JUL", 100, ""),                 # unknown employee
            ("700", "NOPE", 100, ""),                     # unknown challan
            ("700", "", 100, ""),                         # blank challan
            ("700", "2627-JUL", 0, ""),                   # zero amount
            ("705", "2627-JUL", 100, ""),
            ("705", "2627-JUL", 200, ""),                 # duplicate pair
        ]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertContains(resp, "aborted")
        self.assertContains(resp, "No employee with ID")
        self.assertContains(resp, "record it first")
        self.assertContains(resp, "Challan No is required")
        self.assertContains(resp, "greater than zero")
        self.assertContains(resp, "also appears on row")
        self.assertFalse(TaxChallanAllocation.objects.exists())

    def test_reconciliations_are_warnings(self):
        self.client.force_login(self.admin)
        rows = [
            ("700", "2627-AUG-A", 2960, ""),
            ("705", "2627-AUG-A", 1000, ""),              # 705's TDS is 2,040: 1,000 here + 1,000 below = 2,000
            ("705", "2627-AUG-B", 1000, ""),              # …and split across two challans of the same month
            ("700", "2627-YE", 50, ""),                   # a deposit tied to no month is fine
        ]
        resp = self.client.post(self.url, {"action": "apply", "file": self._file(rows)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TaxChallanAllocation.objects.count(), 4)
        self.assertContains(resp, "Challan 2627-AUG-A: allocated 3,960 across 2 employee(s) but its total is 3,000 (difference -960)")
        self.assertContains(resp, "Challan 2627-AUG-B: allocated 1,000 across 1 employee(s) but its total is 1,500 (difference 500)")
        self.assertContains(resp, "Challan 2627-YE: allocated 50 across 1 employee(s) but its total is 100")
        self.assertContains(resp, "2026-08: allocations don&#x27;t equal the month&#x27;s TDS for 1 employee(s) — 705 (allocated 2,000, TDS 2,040)")
        self.assertContains(resp, "allocated to more than one challan of the same month — 705")
        self.assertNotContains(resp, "2026-08: 1 employee(s) with TDS")
        # A later partial upload is checked against the whole picture.
        resp = self.client.post(self.url, {"action": "preview", "file": self._file([("700", "2627-AUG-B", 10, "")])})
        self.assertContains(resp, "Challan 2627-AUG-B: allocated 1,010")
        self.assertContains(resp, "700 (allocated 2,970, TDS 2,960)")

    def test_taxed_employees_in_no_challan_and_month_without_figures_warn(self):
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([("700", "2627-JUL", 2960, "")])})
        self.assertContains(resp, "2026-07: 1 employee(s) with TDS that month are in no challan yet — 705")
        octo = self._challan("2627-OCT", date(2026, 11, 12), 100, 2026, 10)
        resp = self.client.post(self.url, {"action": "preview", "file": self._file([("700", "2627-OCT", 100, "")])})
        self.assertContains(resp, "2026-10: no TDS figures in Orbis for that month yet")

    def test_orbis_month_prefills_from_the_calculated_run(self):
        dept = Department.objects.create(name="Software Development")
        emp = _user("run@example.com", "600", department=dept, joining_date=date(2025, 1, 1))
        JobInfo.objects.create(employee=emp, resource_status=JobInfo.ResourceStatus.LIVE)
        SalaryStructure.objects.create(
            employee=emp, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("120817"), status=SalaryStructure.Status.APPROVED,
        )
        EmployeeTaxDeclaration.objects.create(employee=emp, tax_year=self.tax_year, category=self.categories["MEN"])
        actor = User.objects.create_superuser(email="a@example.com", username="a@example.com", password="x")
        run = calculate_run(lock_inputs(create_run(PayrollPeriod.for_month(2026, 10), actor), actor).pk, actor.pk)
        tds = PayrollRunEmployee.objects.get(run=run, employee=emp).tax_deducted
        self.assertGreater(tds, 0)
        self._challan("2627-OCT", date(2026, 11, 12), tds, 2026, 10)
        self.client.force_login(self.admin)
        wb = load_workbook(BytesIO(self.client.get(reverse("payroll_tax_challan_allocation_upload_template")).content))
        rows = [r[:3] for r in wb[ALLOCATION_SHEET].iter_rows(min_row=2, values_only=True)]
        self.assertIn(("600", "2627-OCT", int(tds)), rows)
        resp = self.client.post(self.url, {"action": "apply", "file": self._file([("600", "2627-OCT", tds, "")])})
        self.assertNotContains(resp, "Warnings")
        self.assertEqual(monthly_ledger_rows(self.tax_year, 2026, 10)[0].challan_number, "2627-OCT")

    def test_csv_and_the_page_summary(self):
        self.client.force_login(self.admin)
        csv = ",".join(ALLOCATION_HEADERS) + "\n700,2627-JUL,2960,x\n705,2627-JUL,2040,y\n"
        resp = self.client.post(self.url, {"action": "apply", "file": SimpleUploadedFile("a.csv", csv.encode())})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TaxChallanAllocation.objects.filter(challan=self.jul).count(), 2)
        resp = self.client.get(self.url)
        self.assertContains(resp, "2627-JUL")
        self.assertContains(resp, 'class="badge-success">0</span>')       # fully allocated
        self.assertContains(resp, "2627-AUG-A")
