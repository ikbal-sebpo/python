""""Revise salary" on the per-employee gross-salary page (2026-10-09): the
payroll-HR pair or Payroll Admins record one APPROVED structure through the
upload's own write path — a same-date revision cancels the current row, a
later date closes it the day before. No maker-checker (user's decision)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Department
from dynamic_menu.models import ProtectedView
from payroll.models import SalaryStructure
from payroll.services import approve_salary_structure, supersede_salary_structure
from payroll.tests_tax import _user

User = get_user_model()


def _grant(view_names, group):
    for name in view_names:
        pv, _ = ProtectedView.objects.get_or_create(view_name=name, defaults={"registered_app": "payroll"})
        pv.allowed_groups.set([group])


class SalaryReviseTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Software Development")
        self.emp = _user("rashel@example.com", "539", department=self.department, joining_date=date(2011, 11, 1))
        SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31), revision_no=15,
            reason=SalaryStructure.Reason.ANNUAL_INCREMENT, gross_monthly=Decimal("109834"),
            status=SalaryStructure.Status.SUPERSEDED,
        )
        self.current = SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2026, 1, 1), revision_no=16,
            reason=SalaryStructure.Reason.ANNUAL_INCREMENT, gross_monthly=Decimal("120817"),
            status=SalaryStructure.Status.APPROVED,
        )
        admins, _ = Group.objects.get_or_create(name="Payroll Admins")
        hr, _ = Group.objects.get_or_create(name="HR")
        payroll, _ = Group.objects.get_or_create(name="Payroll")
        _grant(["payroll_salary_list", "employee_detail"], admins)
        pv = ProtectedView.objects.get(view_name="employee_detail")
        pv.allowed_groups.set([admins, hr, payroll])
        self.admin = _user("padmin@example.com", "701")
        self.admin.groups.add(admins)
        self.pair = _user("hrpay@example.com", "703")
        self.pair.groups.add(hr, payroll)
        self.hr_only = _user("hr@example.com", "704")
        self.hr_only.groups.add(hr)
        self.client = Client(SERVER_NAME="localhost")
        self.detail = reverse("payroll_salary_detail", args=[self.emp.pk])
        self.revise = reverse("payroll_salary_revise", args=[self.emp.pk])

    def _post(self, **data):
        payload = {"gross_monthly": "130000", "effective_from": "2026-01-01", "reason": "CORRECTION", "note": "revised"}
        payload.update(data)
        return self.client.post(self.revise, payload, HTTP_HX_REQUEST="true")

    # -- access ------------------------------------------------------------
    def test_pair_and_admins_open_the_page_hr_alone_does_not(self):
        self.client.force_login(self.admin)
        resp = self.client.get(self.detail)
        self.assertContains(resp, "Revise salary")
        self.assertContains(resp, "Back to list")
        self.client.force_login(self.pair)
        resp = self.client.get(self.detail)
        self.assertContains(resp, "Revise salary")
        self.assertContains(resp, "Back to profile")              # the list is Payroll Admins only
        self.assertEqual(self.client.get(self.revise, HTTP_HX_REQUEST="true").status_code, 200)
        self.client.force_login(self.hr_only)
        self.assertEqual(self.client.get(self.detail).status_code, 302)
        self.assertEqual(self.client.get(self.revise, HTTP_HX_REQUEST="true").status_code, 403)
        self.assertEqual(self._post().status_code, 403)
        self.assertEqual(SalaryStructure.objects.filter(employee=self.emp).count(), 2)

    def test_non_htmx_get_lands_on_the_page(self):
        self.client.force_login(self.pair)
        resp = self.client.get(self.revise)
        self.assertRedirects(resp, self.detail)

    def test_profile_card_links_the_pair_to_the_page(self):
        profile = reverse("employee_detail", args=[self.emp.pk])
        self.client.force_login(self.pair)
        self.assertContains(self.client.get(profile), self.detail)
        self.client.force_login(self.hr_only)
        resp = self.client.get(profile)
        self.assertContains(resp, "View service book")           # HR sees the card…
        self.assertNotContains(resp, self.detail)                 # …but not the gross-salary link

    # -- writes --------------------------------------------------------------
    def test_same_date_revision_cancels_the_current_row(self):
        self.client.force_login(self.pair)
        drawer = self.client.get(self.revise, HTTP_HX_REQUEST="true")
        self.assertContains(drawer, 'value="120817"')              # prefilled with the current figure
        self.assertContains(drawer, 'value="2026-01-01"')
        resp = self._post()
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["HX-Redirect"], self.detail)
        self.current.refresh_from_db()
        self.assertEqual(self.current.status, SalaryStructure.Status.CANCELLED)
        self.assertEqual(self.current.effective_to, date(2026, 1, 1))
        new = SalaryStructure.objects.get(employee=self.emp, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True)
        self.assertEqual((new.gross_monthly, new.effective_from, new.reason, new.revision_no, new.note),
                         (Decimal("130000"), date(2026, 1, 1), "CORRECTION", 17, "revised"))
        self.assertEqual(new.processed_by, self.pair)
        page = self.client.get(self.detail)
        self.assertContains(page, "130,000")
        self.assertContains(page, "Cancelled")

    def test_later_date_closes_the_current_row_the_day_before(self):
        self.client.force_login(self.admin)
        resp = self._post(effective_from="2026-10-01", reason="PROMOTION")
        self.assertEqual(resp.status_code, 204)
        self.current.refresh_from_db()
        self.assertEqual((self.current.status, self.current.effective_to),
                         (SalaryStructure.Status.SUPERSEDED, date(2026, 9, 30)))

    def test_validation_rules(self):
        self.client.force_login(self.admin)
        for data, message in (
            ({"effective_from": "2027-01-01"}, "cannot be in the future"),
            ({"effective_from": "2025-06-01"}, "before the current structure"),
            ({"gross_monthly": "120817"}, "Nothing changed"),
            ({"gross_monthly": "0"}, "greater than or equal to 1"),
        ):
            resp = self._post(**data)
            self.assertEqual(resp.status_code, 400, data)
            self.assertContains(resp, message, status_code=400)
        self.assertEqual(SalaryStructure.objects.filter(employee=self.emp).count(), 2)

    def test_approving_a_pending_same_date_proposal_cancels_too(self):
        pending = supersede_salary_structure(
            self.emp, gross_monthly=Decimal("125000"), effective_from=date(2026, 1, 1),
            reason=SalaryStructure.Reason.CORRECTION, status=SalaryStructure.Status.PENDING,
        )
        self.current.refresh_from_db()
        self.assertEqual(self.current.status, SalaryStructure.Status.APPROVED)   # a proposal changes nothing
        self.assertTrue(approve_salary_structure(pending, self.admin))
        self.current.refresh_from_db()
        self.assertEqual((self.current.status, self.current.effective_to),
                         (SalaryStructure.Status.CANCELLED, date(2026, 1, 1)))
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.revision_no), (SalaryStructure.Status.APPROVED, 17))
