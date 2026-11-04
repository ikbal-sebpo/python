"""Unit tests for employees.admin.DedicatedNightShiftAssignmentResource
(django-import-export bulk import/export), docs/PAYROLL_PLAN.md 2026-09-08."""

from __future__ import annotations

import io

import openpyxl
import tablib
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from employees.admin import DedicatedNightShiftAssignmentResource
from employees.models import DedicatedNightShiftAssignment

User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        employee_id=employee_id, **extra,
    )


class DedicatedNightShiftAssignmentResourceTests(TestCase):
    def setUp(self):
        self.employee = _user("night@example.com", "1001")
        self.approver = _user("apm@example.com", "1002")

    def _import(self, rows, dry_run=False):
        ds = tablib.Dataset(
            *rows,
            headers=["employee_id", "start_date", "end_date", "status", "approved_by_employee_id"],
        )
        return DedicatedNightShiftAssignmentResource().import_data(ds, dry_run=dry_run)

    def test_import_creates_new_assignment(self):
        result = self._import([("1001", "2026-10-01", "2026-12-31", "APPROVED", "1002")])
        self.assertFalse(result.has_errors())

    def test_blank_decline_reason_and_note_cells_import_as_empty_string_not_null(self):
        # Regression, 2026-09-08: a real .xlsx export/re-import (blank
        # decline_reason/note cells -> openpyxl reads them as None, not "")
        # failed with MySQL "Column 'decline_reason' cannot be null" —
        # django-import-export has no TextField entry in WIDGETS_MAP, so
        # these two fell through to the base Widget (no None->"" handling)
        # instead of the CharWidget every CharField gets automatically.
        ds = tablib.Dataset(
            ("1001", "2026-10-01", "2026-12-31", "PENDING", None, None, None, None),
            headers=[
                "employee_id", "start_date", "end_date", "status",
                "approved_by_employee_id", "approved_at", "decline_reason", "note",
            ],
        )
        result = DedicatedNightShiftAssignmentResource().import_data(ds)
        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        assignment = DedicatedNightShiftAssignment.objects.get()
        self.assertEqual(assignment.decline_reason, "")
        self.assertEqual(assignment.note, "")
        self.assertEqual(assignment.employee, self.employee)

    def test_dry_run_does_not_persist(self):
        result = self._import([("1001", "2026-10-01", "2026-12-31", "PENDING", "")], dry_run=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(DedicatedNightShiftAssignment.objects.count(), 0)

    def test_reimporting_same_employee_and_start_date_updates_not_duplicates(self):
        self._import([("1001", "2026-10-01", "2026-12-31", "PENDING", "")])
        self._import([("1001", "2026-10-01", "2027-01-15", "APPROVED", "1002")])
        self.assertEqual(DedicatedNightShiftAssignment.objects.count(), 1)
        assignment = DedicatedNightShiftAssignment.objects.get()
        self.assertEqual(assignment.end_date.isoformat(), "2027-01-15")
        self.assertEqual(assignment.status, DedicatedNightShiftAssignment.Status.APPROVED)

    def test_different_start_date_creates_a_second_assignment(self):
        # An employee can cycle in and out of dedicated duty repeatedly.
        self._import([("1001", "2026-01-01", "2026-03-31", "APPROVED", "1002")])
        self._import([("1001", "2026-10-01", "2026-12-31", "APPROVED", "1002")])
        self.assertEqual(DedicatedNightShiftAssignment.objects.filter(employee=self.employee).count(), 2)

    def test_end_before_start_is_rejected(self):
        # clean_model_instances=True routes model ValidationErrors through
        # invalid_rows/has_validation_errors(), not has_errors() (that's
        # for exceptions) — import_export 4.x's split, not this resource's.
        result = self._import([("1001", "2026-12-31", "2026-10-01", "PENDING", "")])
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(DedicatedNightShiftAssignment.objects.count(), 0)

    def test_overlapping_approved_windows_for_same_employee_rejected(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.employee, start_date="2026-01-01", end_date="2026-06-30",
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        result = self._import([("1001", "2026-05-01", "2026-08-31", "APPROVED", "1002")])
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(DedicatedNightShiftAssignment.objects.filter(employee=self.employee).count(), 1)

    def test_export_round_trips_via_employee_id(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.employee, start_date="2026-10-01", end_date="2026-12-31",
            status=DedicatedNightShiftAssignment.Status.APPROVED, approved_by=self.approver,
        )
        dataset = DedicatedNightShiftAssignmentResource().export()
        row = dataset.dict[0]
        self.assertEqual(row["employee_id"], "1001")
        self.assertEqual(row["approved_by_employee_id"], "1002")
        self.assertEqual(row["status"], "APPROVED")


class DedicatedNightShiftAssignmentAdminExportDropdownTests(TestCase):
    """The admin's exported .xlsx gets a real Excel dropdown on Status —
    django-import-export's own XLSX export has no concept of cell-level
    validation, so this is added after the fact
    (employees/admin.py::_add_status_dropdown, wired via
    DedicatedNightShiftAssignmentAdmin.get_export_data)."""

    def setUp(self):
        self.employee = _user("night2@example.com", "1003")
        self.admin_user = User.objects.create_superuser(
            email="admin2@example.com", username="admin2@example.com", password="x",
        )
        self.client.force_login(self.admin_user)
        DedicatedNightShiftAssignment.objects.create(
            employee=self.employee, start_date="2026-10-01", end_date="2026-12-31",
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )

    def test_exported_xlsx_has_status_dropdown(self):
        url = reverse("admin:employees_dedicatednightshiftassignment_export")
        # Export format choices are CSV, XLSX, TSV, JSON, YAML, HTML in that
        # order (import_export.formats.base_formats.DEFAULT_FORMATS) — 1 is
        # XLSX. import_export 4.x's SelectableFieldsExportForm also needs
        # every "<resourcename>_<field>" checkbox explicitly checked.
        post_data = {"format": "1", "resource": "0"}
        for field in DedicatedNightShiftAssignmentResource.Meta.fields:
            post_data[f"dedicatednightshiftassignmentresource_{field}"] = "on"
        resp = self.client.post(url, post_data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        header = [cell.value for cell in ws[1]]
        status_col_letter = "ABCDEFGHIJ"[header.index("status")]

        dvs = list(ws.data_validations.dataValidation)
        self.assertEqual(len(dvs), 1)
        dv = dvs[0]
        self.assertEqual(dv.type, "list")
        for choice in DedicatedNightShiftAssignment.Status.values:
            self.assertIn(choice, dv.formula1)
        self.assertIn(f"{status_col_letter}2", dv.sqref)
        # Headroom past the exported row(s) so the same file can be used to
        # append new assignments before re-importing.
        self.assertGreater(ws.max_row + 100, 1)
