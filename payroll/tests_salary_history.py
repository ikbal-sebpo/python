"""Gross-salary HISTORY importer (docs/LEGACY_60_IMPORT_PLAN.md §3)."""

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
from payroll.engine import _salary_segments
from payroll.models import PayrollPeriod, SalaryStructure
from payroll.upload import (
    apply_salary_history_upload, build_salary_history_xlsx_template, parse_salary_history_upload,
)

User = get_user_model()
TODAY = date(2026, 10, 5)
HEADER = "Employee ID,Gross monthly salary,Effective from,Effective to,Reason"


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T", **extra,
    )


def _csv(lines):
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


class SalaryHistoryParseTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Admin & Facility")
        self.emp = _user("kazi@example.com", employee_id="104", department=self.dept,
                         joining_date=date(2009, 5, 2))
        self.admin = _user("pa@example.com", employee_id="1")

    def _chain(self):
        return [
            HEADER,
            "104,33146,2009-01-01,2009-12-31,",
            "104,36829,2010-01-01,2010-12-31,",
            "104,145917,2011-01-01,,Promotion",
        ]

    def test_happy_chain_clamps_first_row_and_defaults_reasons(self):
        parsed, errors, warnings = parse_salary_history_upload(_csv(self._chain()), "h.csv", today=TODAY)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        h = parsed[0]
        self.assertEqual([r.effective_from for r in h.rows],
                         [date(2009, 5, 2), date(2010, 1, 1), date(2011, 1, 1)])
        self.assertEqual([r.effective_to for r in h.rows],
                         [date(2009, 12, 31), date(2010, 12, 31), None])
        self.assertEqual([r.reason for r in h.rows], ["NEW_HIRE", "ANNUAL_INCREMENT", "PROMOTION"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("clamped to the joining date", warnings[0].message)

    def test_rows_in_any_order_are_sorted(self):
        lines = [HEADER, "104,145917,2011-01-01,,", "104,33146,2009-05-02,2009-12-31,",
                 "104,36829,2010-01-01,2010-12-31,"]
        parsed, errors, warnings = parse_salary_history_upload(_csv(lines), "h.csv", today=TODAY)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual([r.gross_monthly for r in parsed[0].rows],
                         [Decimal("33146"), Decimal("36829"), Decimal("145917")])

    def test_gap_overlap_and_two_open_rows_fail_the_employee(self):
        gap = [HEADER, "104,1,2009-05-02,2009-12-31,", "104,2,2010-01-02,,"]
        _, errors, _ = parse_salary_history_upload(_csv(gap), "h.csv", today=TODAY)
        self.assertEqual(len(errors), 1)
        self.assertIn("Chain gap", errors[0].message)

        overlap = [HEADER, "104,1,2009-05-02,2009-12-31,", "104,2,2009-12-01,,"]
        _, errors, _ = parse_salary_history_upload(_csv(overlap), "h.csv", today=TODAY)
        self.assertIn("Chain overlap", errors[0].message)

        two_open = [HEADER, "104,1,2009-05-02,,", "104,2,2010-01-01,,"]
        parsed, errors, _ = parse_salary_history_upload(_csv(two_open), "h.csv", today=TODAY)
        self.assertEqual(parsed, [])
        self.assertTrue(any("only the current salary may be open-ended" in e.message for e in errors))

    def test_last_row_must_be_open(self):
        lines = [HEADER, "104,1,2009-05-02,2009-12-31,"]
        parsed, errors, _ = parse_salary_history_upload(_csv(lines), "h.csv", today=TODAY)
        self.assertEqual(parsed, [])
        self.assertIn("blank Effective to", errors[0].message)

    def test_later_row_before_joining_is_an_error_not_a_clamp(self):
        lines = [HEADER, "104,1,2008-01-01,2008-12-31,", "104,2,2009-01-01,,"]
        parsed, errors, warnings = parse_salary_history_upload(_csv(lines), "h.csv", today=TODAY)
        self.assertEqual(parsed, [])
        self.assertTrue(any("isn't the oldest row" in e.message for e in errors))

    def test_old_dates_accepted_future_rejected(self):
        lines = [HEADER, "104,1,2009-05-02,2026-12-31,", "104,2,2027-01-01,,"]
        _, errors, _ = parse_salary_history_upload(_csv(lines), "h.csv", today=TODAY)
        self.assertTrue(any("in the future" in e.message for e in errors))

    def test_unknown_employee_and_bad_amount(self):
        lines = [HEADER, "999,1,2009-05-02,,", "104,-5,2009-05-02,,"]
        parsed, errors, _ = parse_salary_history_upload(_csv(lines), "h.csv", today=TODAY)
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 2)

    def test_xlsx_with_native_dates(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(HEADER.split(","))
        ws.append([104, 33146, date(2009, 5, 2), date(2009, 12, 31), None])
        ws.append([104, 36829, date(2010, 1, 1), None, None])
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        parsed, errors, _ = parse_salary_history_upload(buf, "h.xlsx", today=TODAY)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed[0].rows), 2)


class SalaryHistoryApplyTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Admin & Facility")
        self.emp = _user("kazi@example.com", employee_id="104", department=self.dept,
                         joining_date=date(2009, 5, 2))
        self.other = _user("other@example.com", employee_id="111", department=self.dept,
                           joining_date=date(2006, 3, 1))
        self.admin = _user("pa@example.com", employee_id="1")

    def _parsed(self, lines):
        parsed, errors, _ = parse_salary_history_upload(_csv(lines), "h.csv", today=TODAY)
        self.assertEqual(errors, [])
        return parsed

    def test_writes_chain_with_revisions_statuses_and_dates(self):
        parsed = self._parsed([
            HEADER,
            "104,33146,2009-01-01,2009-12-31,",
            "104,36829,2010-01-01,2010-12-31,",
            "104,145917,2011-01-01,,",
        ])
        report = apply_salary_history_upload(self.admin, parsed, note="import test")
        self.assertEqual(report.total_writes, 3)
        rows = list(SalaryStructure.objects.filter(employee=self.emp).order_by("revision_no"))
        self.assertEqual([r.revision_no for r in rows], [1, 2, 3])
        self.assertEqual([r.status for r in rows], ["SUPERSEDED", "SUPERSEDED", "APPROVED"])
        self.assertEqual(rows[0].effective_from, date(2009, 5, 2))
        self.assertEqual(rows[1].effective_to, date(2010, 12, 31))
        self.assertIsNone(rows[2].effective_to)
        self.assertEqual(rows[2].gross_monthly, Decimal("145917"))
        self.assertEqual(rows[2].processed_by, self.admin)
        self.assertEqual(rows[0].reason, "NEW_HIRE")
        self.assertEqual(rows[1].reason, "ANNUAL_INCREMENT")

    def test_one_shot_skips_employee_with_existing_rows(self):
        SalaryStructure.objects.create(
            employee=self.emp, effective_from=date(2026, 7, 1), reason="ANNUAL_INCREMENT",
            gross_monthly=Decimal("50000"), status="APPROVED",
        )
        parsed = self._parsed([
            HEADER, "104,1,2009-05-02,2009-12-31,", "104,2,2010-01-01,,",
            "111,3,2006-03-01,,",
        ])
        report = apply_salary_history_upload(self.admin, parsed, note="x")
        self.assertEqual(len(report.skipped_existing), 1)
        self.assertEqual(report.skipped_existing[0].raw["Employee_ID"], "104")
        self.assertEqual(report.employees_written, 1)
        self.assertEqual(SalaryStructure.objects.filter(employee=self.emp).count(), 1)
        self.assertEqual(SalaryStructure.objects.filter(employee=self.other).count(), 1)

    def test_engine_prices_a_historical_period_from_the_chain(self):
        parsed = self._parsed([
            HEADER, "104,30000,2009-05-02,2025-12-31,", "104,60000,2026-01-01,,",
        ])
        apply_salary_history_upload(self.admin, parsed, note="x")
        period = PayrollPeriod(start_date=date(2025, 12, 16), end_date=date(2026, 1, 15))
        structures = list(SalaryStructure.objects.filter(employee=self.emp))
        segments, warnings = _salary_segments(structures, period, self.emp)
        self.assertEqual(warnings, [])
        self.assertEqual([(s[0], s[1], s[2].gross_monthly) for s in segments], [
            (date(2025, 12, 16), date(2025, 12, 31), Decimal("30000")),
            (date(2026, 1, 1), date(2026, 1, 15), Decimal("60000")),
        ])

    def test_template_lists_only_employees_without_records(self):
        SalaryStructure.objects.create(
            employee=self.other, effective_from=date(2026, 7, 1), reason="ANNUAL_INCREMENT",
            gross_monthly=Decimal("50000"), status="APPROVED",
        )
        wb = openpyxl.load_workbook(io.BytesIO(build_salary_history_xlsx_template()))
        ws = wb.active
        ids = [row[0].value for row in ws.iter_rows(min_row=2)]
        self.assertIn("104", ids)
        self.assertNotIn("111", ids)
        self.assertEqual([c.value for c in ws[1]][:5], HEADER.split(","))


class SalaryHistoryViewAccessTests(TestCase):
    def setUp(self):
        for name in ("HR", "Payroll Admins"):
            Group.objects.get_or_create(name=name)
        self.hr = _user("hr@example.com", employee_id="1")
        self.hr.groups.add(Group.objects.get(name="HR"))
        self.pa = _user("pa@example.com", employee_id="2")
        self.pa.groups.add(Group.objects.get(name="Payroll Admins"))
        for name in ("payroll_salary_upload_history", "payroll_salary_upload_history_template"):
            pv, _ = ProtectedView.objects.get_or_create(view_name=name, defaults={"registered_app": "payroll"})
            pv.allowed_groups.set([Group.objects.get(name="Payroll Admins")])

    def test_hr_denied_payroll_admin_allowed(self):
        self.client.force_login(self.hr)
        self.assertRedirects(self.client.get("/payroll/salary/upload/history/"), "/")
        self.client.force_login(self.pa)
        self.assertEqual(self.client.get("/payroll/salary/upload/history/").status_code, 200)
        self.assertEqual(self.client.get("/payroll/salary/upload/history/template/").status_code, 200)

    def test_preview_then_apply_through_the_view(self):
        emp = _user("e@example.com", employee_id="104", joining_date=date(2009, 5, 2))
        self.client.force_login(self.pa)
        body = "\n".join([HEADER, "104,1,2009-01-01,2009-12-31,", "104,2,2010-01-01,,"]) + "\n"
        f = io.BytesIO(body.encode()); f.name = "h.csv"
        resp = self.client.post("/payroll/salary/upload/history/", {"file": f, "action": "preview", "note": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "clamped to the joining date")
        self.assertEqual(SalaryStructure.objects.filter(employee=emp).count(), 0)
        f = io.BytesIO(body.encode()); f.name = "h.csv"
        resp = self.client.post("/payroll/salary/upload/history/", {"file": f, "action": "apply", "note": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SalaryStructure.objects.filter(employee=emp).count(), 2)
        current = SalaryStructure.objects.get(employee=emp, status="APPROVED")
        self.assertEqual(current.note, "Salary history import from h.csv")
        self.assertIsNotNone(current.batch)
