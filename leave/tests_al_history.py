"""Pre-Orbis AL record import + its Service-Book-only visibility
(docs/LEGACY_60_IMPORT_PLAN.md §4)."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from accounts.models import Department
from dynamic_menu.models import ProtectedView
from employees.services import build_service_book_context
from leave.models import LeaveBalance, LegacyAnnualLeaveRecord
from leave.upload_al_history import (
    apply_al_history_upload, build_al_history_xlsx_template, is_eligible_at, parse_al_history_upload,
)

User = get_user_model()
AS_OF = date(2025, 12, 31)
HEADER = "Employee ID,Till 2024,2025 AL Taken,2025 AL Balance,60 Cap Till 2025,Note"


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T", **extra,
    )


def _csv(lines):
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


class ALHistoryParseApplyTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Admin & Facility")
        self.hr = _user("hr@example.com", employee_id="900")
        self.old = _user("kazi@example.com", employee_id="104", department=self.dept, joining_date=date(2009, 5, 2))
        self.boundary = _user("b@example.com", employee_id="200", department=self.dept, joining_date=date(2024, 12, 31))
        self.short = _user("s@example.com", employee_id="201", department=self.dept, joining_date=date(2025, 1, 1))
        self.later = _user("l@example.com", employee_id="202", department=self.dept, joining_date=date(2026, 5, 7))

    def _parse(self, lines):
        return parse_al_history_upload(_csv(lines), "al.csv", as_of=AS_OF)

    def test_eligibility_boundary(self):
        self.assertTrue(is_eligible_at(date(2024, 12, 31), AS_OF))
        self.assertFalse(is_eligible_at(date(2025, 1, 1), AS_OF))
        self.assertTrue(is_eligible_at(date(2024, 2, 29), date(2025, 2, 28)))

    def test_eligible_written_ineligible_skipped_not_errored(self):
        parsed, errors, dups, skipped, warnings = self._parse([
            HEADER,
            "104,204,0,12,60,",
            "200,12,0,12,24,",
            "201,12,0,12,24,",
            "202,0,0,12,12,",
        ])
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual([r.employee_id for r in parsed], ["104", "200"])
        self.assertEqual([n.employee_id for n in skipped], ["201", "202"])
        self.assertIn("Under 12 months", skipped[0].message)
        self.assertIn("after the as-of date", skipped[1].message)
        self.assertEqual(warnings, [])

    def test_cap_over_60_is_an_error_and_arithmetic_mismatch_only_warns(self):
        parsed, errors, _, _, warnings = self._parse([HEADER, "104,204,0,12,61,"])
        self.assertEqual(parsed, [])
        self.assertIn("exceeds the 60-day", errors[0].message)
        parsed, errors, _, _, warnings = self._parse([HEADER, "104,33,12,0,30,15 taken in 2025"])
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("≠ min(60, 33.00 + 0.00) = 33.00", warnings[0].message)
        self.assertEqual(parsed[0].note, "15 taken in 2025")

    def test_duplicate_and_unknown_employee_and_negative(self):
        _, errors, dups, _, _ = self._parse([HEADER, "104,1,0,12,13,", "104,1,0,12,13,", "999,1,0,12,13,", "200,-1,0,12,11,"])
        self.assertEqual(len(dups), 1)
        self.assertEqual(len(errors), 2)

    def test_apply_upserts_and_never_touches_the_ledger(self):
        parsed, *_ = self._parse([HEADER, "104,204,0,12,60,"])
        report = apply_al_history_upload(self.hr, parsed, as_of=AS_OF, source_file="al.xlsx")
        self.assertEqual(len(report.created), 1)
        rec = LegacyAnnualLeaveRecord.objects.get(employee=self.old, as_of=AS_OF)
        self.assertEqual((rec.accrued_before_year, rec.capped_balance, rec.added_by, rec.source_file),
                         (Decimal("204"), Decimal("60"), self.hr, "al.xlsx"))
        parsed, *_ = self._parse([HEADER, "104,204,3,9,60,corrected"])
        report = apply_al_history_upload(self.hr, parsed, as_of=AS_OF)
        self.assertEqual(len(report.updated), 1)
        self.assertEqual(LegacyAnnualLeaveRecord.objects.filter(employee=self.old).count(), 1)
        rec.refresh_from_db()
        self.assertEqual((rec.taken_in_year, rec.note), (Decimal("3"), "corrected"))
        self.assertEqual(LeaveBalance.objects.filter(employee=self.old).count(), 0)

    def test_inactive_employee_accepted(self):
        self.old.is_active = False
        self.old.save(update_fields=["is_active"])
        parsed, errors, *_ = self._parse([HEADER, "104,204,0,12,60,"])
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)

    def test_template_headers_follow_as_of_year(self):
        wb = openpyxl.load_workbook(io.BytesIO(build_al_history_xlsx_template(as_of=AS_OF)))
        self.assertEqual([c.value for c in wb.active[1]][:5], HEADER.split(",")[:5])


class ALHistoryVisibilityTests(TestCase):
    """The record appears in the Service Book only, which is HR + superuser only."""

    def setUp(self):
        for name in ("HR", "User Admins", "All Employees"):
            Group.objects.get_or_create(name=name)
        self.dept = Department.objects.create(name="Admin & Facility")
        self.hr = _user("hr@example.com", employee_id="900", department=self.dept)
        self.hr.groups.add(Group.objects.get(name="HR"))
        self.admin = _user("ua@example.com", employee_id="901", department=self.dept)
        self.admin.groups.add(Group.objects.get(name="User Admins"))
        self.emp = _user("kazi@example.com", employee_id="104", department=self.dept, joining_date=date(2009, 5, 2))
        LegacyAnnualLeaveRecord.objects.create(
            employee=self.emp, as_of=AS_OF, accrued_before_year=204, taken_in_year=0,
            balance_in_year=12, capped_balance=60, note="",
        )
        # The Service Book needs the AL policy to roll the record forward.
        from leave.models import LeavePolicy
        from roster.models import ShiftType

        al_type, _ = ShiftType.objects.get_or_create(name="AL", defaults={"category": "LEAVE"})
        LeavePolicy.objects.get_or_create(
            shift_type=al_type, defaults={"max_days_per_year": 12, "max_carry_forward_days": 60},
        )
        for name in ("leave_al_history_upload", "leave_al_history_upload_template"):
            pv, _ = ProtectedView.objects.get_or_create(view_name=name, defaults={"registered_app": "leave"})
            pv.allowed_groups.set([Group.objects.get(name="HR")])

    def test_service_book_rolls_the_record_forward_from_orbis_usage(self):
        from leave.models import LeavePolicy
        from roster.models import Shift, ShiftType

        al_type, _ = ShiftType.objects.get_or_create(name="AL", defaults={"category": "LEAVE"})
        policy, _ = LeavePolicy.objects.get_or_create(
            shift_type=al_type, defaults={"max_days_per_year": 12, "max_carry_forward_days": 60},
        )
        ctx = build_service_book_context(self.emp)
        row = ctx["sb_al_legacy"][0]
        self.assertEqual((row["accrued_prior"], row["taken"], row["year_balance"], row["carried"], row["year"]),
                         (Decimal("60"), Decimal("0"), Decimal("12"), Decimal("60"), 2026))
        # Five roster-direct AL days in 2026 -> taken 5, year balance 7, still capped at 60.
        for day in range(1, 6):
            Shift.objects.create(employee=self.emp, date=date(2026, 3, day), shift_type=al_type)
        row = build_service_book_context(self.emp)["sb_al_legacy"][0]
        self.assertEqual((row["taken"], row["year_balance"], row["carried"]),
                         (Decimal("5"), Decimal("7"), Decimal("60")))
        self.assertEqual(ctx["sb_al_periods"], [])

    def test_service_book_rolled_forward_below_the_cap(self):
        from leave.models import LeavePolicy
        from roster.models import ShiftType

        al_type, _ = ShiftType.objects.get_or_create(name="AL", defaults={"category": "LEAVE"})
        LeavePolicy.objects.get_or_create(
            shift_type=al_type, defaults={"max_days_per_year": 12, "max_carry_forward_days": 60},
        )
        rec = LegacyAnnualLeaveRecord.objects.get(employee=self.emp)
        rec.capped_balance = Decimal("33")
        rec.save()
        row = build_service_book_context(self.emp)["sb_al_legacy"][0]
        self.assertEqual((row["accrued_prior"], row["year_balance"], row["carried"]),
                         (Decimal("33"), Decimal("12"), Decimal("45")))
        self.assertEqual(row["year"], 2026)
        self.assertIn("HR records as at 2025-12-31", row["note"])

    def test_service_book_adds_a_computed_row_per_year_chained(self):
        from unittest.mock import patch
        from leave.models import LeavePolicy
        from roster.models import Shift, ShiftType

        al_type, _ = ShiftType.objects.get_or_create(name="AL", defaults={"category": "LEAVE"})
        LeavePolicy.objects.get_or_create(
            shift_type=al_type, defaults={"max_days_per_year": 12, "max_carry_forward_days": 60},
        )
        rec = LegacyAnnualLeaveRecord.objects.get(employee=self.emp)
        rec.capped_balance = Decimal("33")
        rec.save()
        for day in range(1, 4):
            Shift.objects.create(employee=self.emp, date=date(2027, 2, day), shift_type=al_type)

        class _FakeDate(date):
            @classmethod
            def today(cls):
                return date(2027, 6, 1)

        with patch("employees.services.date", _FakeDate):
            rows = build_service_book_context(self.emp)["sb_al_legacy"]
        self.assertEqual([r["year"] for r in rows], [2026, 2027])
        self.assertEqual((rows[0]["accrued_prior"], rows[0]["carried"]), (Decimal("33"), Decimal("45")))
        self.assertEqual((rows[1]["accrued_prior"], rows[1]["taken"], rows[1]["year_balance"], rows[1]["carried"]),
                         (Decimal("45"), Decimal("3"), Decimal("9"), Decimal("54")))
        self.assertEqual(rows[1]["note"], "")

    def test_service_book_formats_wages_accounting_style(self):
        from core.money import format_taka
        from payroll.models import SalaryStructure

        SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2026, 1, 1), reason="ANNUAL_INCREMENT",
            gross_monthly=Decimal("450000.00"), status="APPROVED",
        )
        self.assertEqual(format_taka(Decimal("1234567.50")), "1,234,568")
        self.assertEqual(format_taka(None), "—")
        self.client.force_login(self.hr)
        resp = self.client.get(f"/employees/{self.emp.pk}/service-book/")
        self.assertContains(resp, "450,000")
        self.assertNotContains(resp, "450000.00")

    def test_service_book_page_hr_only(self):
        url = f"/employees/{self.emp.pk}/service-book/"
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url + "pdf/").status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.hr)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Annual Leave record before Orbis")
        self.assertNotContains(resp, "Annual Leave availed")

    def test_profile_page_hides_service_book_card_from_self(self):
        self.client.force_login(self.emp)
        resp = self.client.get(f"/employees/{self.emp.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "View service book")

    def test_balance_page_does_not_show_the_record(self):
        self.client.force_login(self.hr)
        resp = self.client.get("/leave/balance/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "before Orbis")
        self.assertNotContains(resp, "Import pre-Orbis AL")

    def test_leave_list_offers_hr_the_import_button(self):
        self.client.force_login(self.hr)
        resp = self.client.get("/leave/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Import pre-Orbis AL")
        self.client.force_login(self.emp)
        resp = self.client.get("/leave/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Import pre-Orbis AL")

    def test_as_of_must_be_a_year_end(self):
        from leave.forms import ALHistoryUploadForm

        self.assertFalse(ALHistoryUploadForm({"as_of": "2026-10-05"}).is_valid())
        self.assertTrue(ALHistoryUploadForm({"as_of": "2025-12-31"}).is_valid())
        self.client.force_login(self.hr)
        f = io.BytesIO(("\n".join([HEADER, "104,204,3,9,60,"]) + "\n").encode()); f.name = "al.csv"
        resp = self.client.post("/leave/al-history/upload/", {"file": f, "action": "apply", "as_of": "2026-10-05"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "must be a 31 December")
        self.assertEqual(LegacyAnnualLeaveRecord.objects.filter(employee=self.emp, as_of=date(2026, 10, 5)).count(), 0)

    def test_upload_view_gated_and_round_trip(self):
        self.client.force_login(self.emp)
        self.assertRedirects(self.client.get("/leave/al-history/upload/"), "/")
        self.client.force_login(self.hr)
        self.assertEqual(self.client.get("/leave/al-history/upload/").status_code, 200)
        self.assertEqual(self.client.get("/leave/al-history/upload/template/?as_of=2025-12-31").status_code, 200)
        f = io.BytesIO(("\n".join([HEADER, "104,204,3,9,60,"]) + "\n").encode()); f.name = "al.csv"
        resp = self.client.post("/leave/al-history/upload/", {"file": f, "action": "apply", "as_of": "2025-12-31"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LegacyAnnualLeaveRecord.objects.get(employee=self.emp).taken_in_year, Decimal("3"))
