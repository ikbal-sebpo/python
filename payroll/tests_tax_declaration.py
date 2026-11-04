"""EmployeeTaxDeclaration as a once-a-year commitment (revised with Finance
2026-10-07): the allowable-investment snapshot, the employee's confirm-and-lock
step, the AIT (Car & Adv. Tax) document rule, and the July seed command.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.dashboard import me_context
from payroll.models import EmployeeTaxDeclaration, SalaryStructure, TaxCategory, TaxYear
from payroll.tax import allowable_investment, project_taxable_income
from payroll.tests_tax import _seed_tax_year, _user

User = get_user_model()
_MEDIA_ROOT = tempfile.mkdtemp(prefix="orbis-tax-declaration-tests-")


def _salary(employee, gross, effective_from=date(2026, 1, 1), **extra):
    return SalaryStructure.objects.create(
        employee=employee, effective_from=effective_from, gross_monthly=Decimal(gross),
        reason=SalaryStructure.Reason.NEW_HIRE, status=SalaryStructure.Status.APPROVED, **extra,
    )


def _pdf(name="ait.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 fake", content_type="application/pdf")


class AllowableInvestmentTests(TestCase):
    """Finance's 2026-10-07 example: gross 120,817 -> allowable 308,286 in the
    sheet, which also carries a 5,000 incentive line Orbis doesn't; without
    it the same arithmetic gives 306,786."""

    def setUp(self):
        self.tax_year, _ = _seed_tax_year()

    def test_projection_matches_finance_sheet(self):
        projection = project_taxable_income(self.tax_year, Decimal("120817"))
        self.assertEqual(projection.mobile_internet_reimburse, Decimal("4000"))
        self.assertEqual(projection.annual_gross, Decimal("1522621"))  # (120817-4000)*12 + 120817
        self.assertEqual(projection.exempted_income, Decimal("500000"))
        self.assertEqual(projection.taxable_income, Decimal("1022621"))

    def test_allowable_is_thirty_percent_floored_to_whole_taka(self):
        self.assertEqual(allowable_investment(self.tax_year, Decimal("120817")), Decimal("306786"))
        # Finance's sheet, incentives included: taxable 1,027,621 -> 308,286.30 -> 308,286
        self.tax_year.allowable_investment_pct = Decimal("0.3000")
        self.assertEqual(
            (Decimal("1027621") * self.tax_year.allowable_investment_pct).quantize(Decimal("1"), rounding="ROUND_DOWN"),
            Decimal("308286"),
        )

    def test_default_investment_makes_rebate_equal_three_percent_of_taxable(self):
        """Why Finance assumes the maximum: 10% of 30% of taxable == 3% of
        taxable, so the rebate collapses to the income-based term."""
        from payroll.tax import compute_tax_for_employee
        employee = _user("max@example.com", "900")
        declaration = EmployeeTaxDeclaration(
            employee=employee, tax_year=self.tax_year,
            category=TaxCategory.objects.get(code="MEN"),
            allowable_investment=allowable_investment(self.tax_year, Decimal("120817")),
        )
        breakdown = compute_tax_for_employee(
            employee, self.tax_year, Decimal("120817"), declaration,
            slabs=list(self.tax_year.slabs.order_by("sequence")),
            threshold_amount=Decimal("400000"),
        )
        self.assertEqual(breakdown.total_declared_investment, Decimal("306786"))
        self.assertEqual(breakdown.rebate_amount, Decimal("30678.60"))  # 10% of 306,786 < 3% of 1,022,621 (30,678.63)


class DeclarationModelTests(TestCase):
    def setUp(self):
        self.tax_year, self.categories = _seed_tax_year()
        self.employee = _user("dina@example.com", "010")
        _salary(self.employee, "120817")

    def _declaration(self, **extra):
        return EmployeeTaxDeclaration.objects.create(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"], **extra,
        )

    def test_save_issues_snapshot_and_defaults_commitment_to_it(self):
        declaration = self._declaration()
        self.assertEqual(declaration.allowable_investment, Decimal("306786"))
        self.assertEqual(declaration.actual_investment, Decimal("306786"))
        self.assertEqual(declaration.declared_investment, Decimal("306786"))
        self.assertFalse(declaration.is_locked)

    def test_snapshot_is_never_recomputed(self):
        declaration = self._declaration()
        SalaryStructure.objects.filter(employee=self.employee).update(
            effective_to=date(2026, 8, 31), status=SalaryStructure.Status.SUPERSEDED,
        )
        _salary(self.employee, "200000", effective_from=date(2026, 9, 1))
        declaration.note = "touched"
        declaration.save()
        declaration.refresh_from_db()
        self.assertEqual(declaration.allowable_investment, Decimal("306786"))

    def test_no_salary_structure_leaves_snapshot_blank_and_rebate_basis_zero(self):
        nobody = _user("nosalary@example.com", "011")
        declaration = EmployeeTaxDeclaration.objects.create(
            employee=nobody, tax_year=self.tax_year, category=self.categories["MEN"],
        )
        self.assertIsNone(declaration.allowable_investment)
        self.assertIsNone(declaration.actual_investment)
        self.assertEqual(declaration.declared_investment, Decimal("0"))

    def test_commitment_cannot_exceed_allowable(self):
        with self.assertRaises(ValidationError) as ctx:
            self._declaration(actual_investment=Decimal("400000"))
        self.assertIn("actual_investment", ctx.exception.message_dict)

    def test_commitment_may_be_lowered(self):
        declaration = self._declaration(actual_investment=Decimal("100000"))
        self.assertEqual(declaration.declared_investment, Decimal("100000"))

    def test_ait_requires_document(self):
        with self.assertRaises(ValidationError) as ctx:
            self._declaration(ait_car_advance_tax=Decimal("25000"))
        self.assertIn("ait_document", ctx.exception.message_dict)

    @override_settings(MEDIA_ROOT=_MEDIA_ROOT)
    def test_ait_with_document_saves_under_a_random_name(self):
        declaration = self._declaration(ait_car_advance_tax=Decimal("25000"), ait_document=_pdf("My Car Tax.pdf"))
        self.assertTrue(declaration.ait_document.name.startswith(f"payroll/tax_declarations/{self.employee.pk}/"))
        self.assertNotIn("My Car Tax", declaration.ait_document.name)
        self.assertTrue(declaration.ait_document.name.endswith(".pdf"))

    def test_deadline_locks_an_unconfirmed_row(self):
        declaration = self._declaration()
        self.tax_year.declaration_deadline = date.today() - timedelta(days=1)
        self.tax_year.save()
        declaration.refresh_from_db()
        self.assertTrue(declaration.is_past_deadline)
        self.assertTrue(declaration.is_locked)
        self.assertFalse(declaration.is_submitted)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ReviewViewTests(TestCase):
    """The employee's confirm-and-lock drawer, and Payroll-HR on their behalf."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.tax_year, self.categories = _seed_tax_year()
        self.employee = _user("emp@example.com", "020")
        _salary(self.employee, "120817")
        self.declaration = EmployeeTaxDeclaration.objects.create(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
        )
        self.stranger = _user("stranger@example.com", "021")
        self.payroll_hr = _user("payrollhr@example.com", "022")
        for name in ("Payroll", "HR"):
            self.payroll_hr.groups.add(Group.objects.get_or_create(name=name)[0])
        self.url = reverse("payroll_tax_declaration_review", args=[self.declaration.pk])
        self.client = Client(SERVER_NAME="localhost")

    def _login(self, user):
        self.client.force_login(user)

    def test_employee_gets_drawer_fragment_and_full_page(self):
        self._login(self.employee)
        drawer = self.client.get(self.url, HTTP_HX_REQUEST="true")
        self.assertEqual(drawer.status_code, 200)
        self.assertContains(drawer, "Confirm &amp; lock")
        self.assertNotContains(drawer, "<html")
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "<html")

    def test_stranger_is_refused(self):
        self._login(self.stranger)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"confirm": "on"}).status_code, 403)

    def test_confirm_lowers_commitment_and_locks(self):
        self._login(self.employee)
        resp = self.client.post(
            self.url, {"actual_investment": "150000", "ait_car_advance_tax": "0", "confirm": "on"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["HX-Redirect"], reverse("employee_detail", args=[self.employee.pk]))
        self.declaration.refresh_from_db()
        self.assertEqual(self.declaration.actual_investment, Decimal("150000"))
        self.assertTrue(self.declaration.is_locked)
        self.assertEqual(self.declaration.submitted_by, self.employee)
        self.assertEqual(self.declaration.submission_source, EmployeeTaxDeclaration.SubmissionSource.IN_ORBIS)
        self.assertFalse(self.declaration.submitted_on_behalf)
        # Locked: the drawer is gone for everyone, including the employee.
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 403)

    def test_blank_commitment_keeps_the_allowable_default(self):
        self._login(self.employee)
        resp = self.client.post(self.url, {"actual_investment": "", "confirm": "on"})
        self.assertEqual(resp.status_code, 302)
        self.declaration.refresh_from_db()
        self.assertEqual(self.declaration.actual_investment, Decimal("306786"))
        self.assertTrue(self.declaration.is_submitted)

    def test_confirmation_checkbox_is_required(self):
        self._login(self.employee)
        resp = self.client.post(self.url, {"actual_investment": "150000"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 400)
        self.declaration.refresh_from_db()
        self.assertFalse(self.declaration.is_submitted)
        self.assertEqual(self.declaration.actual_investment, Decimal("306786"))

    def test_raising_above_allowable_is_rejected(self):
        self._login(self.employee)
        resp = self.client.post(self.url, {"actual_investment": "999999", "confirm": "on"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "allowable investment", status_code=400)

    def test_ait_without_document_is_rejected_and_with_document_accepted(self):
        self._login(self.employee)
        resp = self.client.post(
            self.url, {"ait_car_advance_tax": "25000", "confirm": "on"}, HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "payment document", status_code=400)
        resp = self.client.post(
            self.url, {"ait_car_advance_tax": "25000", "confirm": "on", "ait_document": _pdf()},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        self.declaration.refresh_from_db()
        self.assertEqual(self.declaration.ait_car_advance_tax, Decimal("25000"))
        self.assertTrue(self.declaration.ait_document)

    def test_ait_document_must_be_pdf_or_image(self):
        self._login(self.employee)
        bad = SimpleUploadedFile("macro.xlsm", b"junk", content_type="application/octet-stream")
        resp = self.client.post(
            self.url, {"ait_car_advance_tax": "25000", "confirm": "on", "ait_document": bad},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "PDF, JPG or PNG", status_code=400)

    def test_payroll_hr_confirms_on_behalf(self):
        self._login(self.payroll_hr)
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 200)
        resp = self.client.post(self.url, {"actual_investment": "200000", "confirm": "on"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        self.declaration.refresh_from_db()
        self.assertTrue(self.declaration.submitted_on_behalf)
        self.assertEqual(self.declaration.submitted_by, self.payroll_hr)

    def test_hr_alone_is_not_enough_on_behalf(self):
        hr_only = _user("hronly@example.com", "023")
        hr_only.groups.add(Group.objects.get_or_create(name="HR")[0])
        self._login(hr_only)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_past_deadline_refuses_even_the_employee(self):
        self.tax_year.declaration_deadline = date.today() - timedelta(days=1)
        self.tax_year.save()
        self._login(self.employee)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_document_view_permissions(self):
        self.declaration.ait_car_advance_tax = Decimal("25000")
        self.declaration.ait_document = _pdf()
        self.declaration.save()
        url = reverse("payroll_tax_declaration_document", args=[self.declaration.pk])
        self._login(self.stranger)
        self.assertEqual(self.client.get(url).status_code, 403)
        self._login(self.employee)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4 fake")
        self._login(self.payroll_hr)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_dashboard_card_shows_until_confirmed(self):
        pending = me_context(self.employee)["pending_tax_declaration"]
        self.assertEqual(pending, self.declaration)
        self.declaration.mark_submitted(self.employee)
        self.declaration.save()
        self.assertIsNone(me_context(self.employee)["pending_tax_declaration"])

    def test_dashboard_card_hidden_once_deadline_passed(self):
        self.tax_year.declaration_deadline = date.today() - timedelta(days=1)
        self.tax_year.save()
        self.assertIsNone(me_context(self.employee)["pending_tax_declaration"])


class LockedRowGenericDrawerTests(TestCase):
    """HR's generic profile drawer (employees.SectionEditView/DeleteView)
    respects the lock too — admin is the only way back."""

    def setUp(self):
        self.tax_year, self.categories = _seed_tax_year()
        self.employee = _user("locked@example.com", "030")
        _salary(self.employee, "120817")
        self.declaration = EmployeeTaxDeclaration.objects.create(
            employee=self.employee, tax_year=self.tax_year, category=self.categories["MEN"],
        )
        self.hr = User.objects.create_superuser(email="su@example.com", username="su@example.com", password="x")
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.hr)
        self.edit_url = reverse("section_edit_row", args=[self.employee.pk, "tax-declaration", self.declaration.pk])
        self.delete_url = reverse("section_delete_row", args=[self.employee.pk, "tax-declaration", self.declaration.pk])

    def test_unlocked_row_is_editable_without_confirming(self):
        resp = self.client.post(
            self.edit_url,
            {"tax_year": self.tax_year.pk, "category": self.categories["MEN"].pk,
             "actual_investment": "100000", "source_tax": "0", "ait_car_advance_tax": "0", "note": "paper form"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        # Success swaps the refreshed card on the page, not the drawer body.
        self.assertEqual(resp["HX-Retarget"], "#section-card-tax-declaration")
        self.assertEqual(resp["HX-Reswap"], "outerHTML")
        self.assertContains(resp, 'id="section-card-tax-declaration"')
        self.declaration.refresh_from_db()
        self.assertEqual(self.declaration.actual_investment, Decimal("100000"))
        self.assertFalse(self.declaration.is_submitted)  # HR editing never locks
        self.assertEqual(self.declaration.allowable_investment, Decimal("306786"))  # disabled field untouched

    def test_validation_error_re_renders_inside_the_drawer(self):
        resp = self.client.post(
            self.edit_url,
            {"tax_year": self.tax_year.pk, "category": self.categories["MEN"].pk,
             "actual_investment": "406786", "source_tax": "0", "ait_car_advance_tax": "0", "note": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Can&#x27;t exceed the allowable investment")
        self.assertContains(resp, 'hx-target="#drawer-body"')  # the form itself, not the card
        self.assertNotIn("HX-Retarget", resp)
        self.assertNotContains(resp, 'id="section-card-tax-declaration"')
        self.declaration.refresh_from_db()
        self.assertEqual(self.declaration.actual_investment, Decimal("306786"))

    def test_locked_row_refuses_edit_and_delete(self):
        self.declaration.mark_submitted(self.employee)
        self.declaration.save()
        resp = self.client.post(
            self.edit_url,
            {"tax_year": self.tax_year.pk, "category": self.categories["MEN"].pk,
             "actual_investment": "1", "source_tax": "0", "ait_car_advance_tax": "0", "note": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "locked for the tax year")
        self.declaration.refresh_from_db()
        self.assertEqual(self.declaration.actual_investment, Decimal("306786"))
        self.assertEqual(self.client.post(self.delete_url, HTTP_HX_REQUEST="true").status_code, 403)
        self.assertTrue(EmployeeTaxDeclaration.objects.filter(pk=self.declaration.pk).exists())

    def test_profile_section_shows_lock_state_and_no_row_actions(self):
        self.declaration.mark_submitted(self.employee)
        self.declaration.save()
        resp = self.client.get(reverse("employee_detail", args=[self.employee.pk]))
        self.assertContains(resp, "Confirmed")
        self.assertNotContains(resp, reverse("section_delete_row", args=[self.employee.pk, "tax-declaration", self.declaration.pk]))


class SeedCommandTests(TestCase):
    def setUp(self):
        self.tax_year, self.categories = _seed_tax_year()
        self.paid = _user("paid@example.com", "040", joining_date=date(2025, 1, 1))
        _salary(self.paid, "120817")
        self.unpaid = _user("unpaid@example.com", "041", joining_date=date(2025, 1, 1))

    def _call(self, *args):
        out = StringIO()
        call_command("seed_tax_declarations", *args, stdout=out)
        return out.getvalue()

    def test_creates_rows_with_snapshot(self):
        self._call("--as-of", "2026-07-01")
        paid = EmployeeTaxDeclaration.objects.get(employee=self.paid)
        unpaid = EmployeeTaxDeclaration.objects.get(employee=self.unpaid)
        self.assertEqual(paid.allowable_investment, Decimal("306786"))
        self.assertEqual(paid.actual_investment, Decimal("306786"))
        self.assertFalse(paid.is_submitted)
        self.assertIsNone(unpaid.allowable_investment)

    def test_fill_allowable_only_touches_blank_snapshots(self):
        self._call()
        _salary(self.unpaid, "50000", effective_from=date(2026, 8, 1))
        paid = EmployeeTaxDeclaration.objects.get(employee=self.paid)
        paid.allowable_investment = Decimal("1")
        paid.actual_investment = Decimal("1")
        paid.save()
        self._call("--fill-allowable", "--as-of", "2026-07-01")  # salary starts after as-of: the first later row counts
        paid.refresh_from_db()
        unpaid = EmployeeTaxDeclaration.objects.get(employee=self.unpaid)
        self.assertEqual(paid.allowable_investment, Decimal("1"))
        # 50,000 gross: taxable 417,333.33.. x 30% = 125,199.99.. which Decimal's
        # 28-digit context (and Excel's float) both carry as 125,200.
        self.assertEqual(unpaid.allowable_investment, Decimal("125200"))

    def test_mark_agreed_outside_stamps_unconfirmed_rows_only(self):
        self._call()
        confirmed = EmployeeTaxDeclaration.objects.get(employee=self.paid)
        confirmed.mark_submitted(self.paid)
        confirmed.save()
        self._call("--mark-agreed-outside")
        confirmed.refresh_from_db()
        other = EmployeeTaxDeclaration.objects.get(employee=self.unpaid)
        self.assertEqual(confirmed.submission_source, EmployeeTaxDeclaration.SubmissionSource.IN_ORBIS)
        self.assertEqual(other.submission_source, EmployeeTaxDeclaration.SubmissionSource.OUTSIDE_ORBIS)
        self.assertEqual(other.submitted_at.date(), self.tax_year.start_date)
        self.assertIsNone(other.submitted_by)
        self.assertTrue(other.is_locked)

    def test_dry_run_writes_nothing(self):
        out = self._call("--dry-run")
        self.assertIn("Dry run", out)
        self.assertEqual(EmployeeTaxDeclaration.objects.count(), 0)
