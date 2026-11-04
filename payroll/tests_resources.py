"""Unit tests for payroll.admin.PayRateResource (django-import-export bulk
import/export), docs/PAYROLL_PLAN.md 2026-09-16."""

from __future__ import annotations

import tablib
from django.test import TestCase

from accounts.models import AccessLevel, Department
from payroll.admin import PayRateResource
from payroll.models import OvertimeType, PayComponent, PayRate

FIELDS = [
    "rate_type", "overtime_type", "component",
    "department", "team", "sub_project", "work_location", "grade",
    "min_gross", "max_gross", "resource_status", "min_access_level",
    "basis", "amount", "effective_from", "effective_to", "is_active",
]


class PayRateResourceTests(TestCase):
    def setUp(self):
        self.component = PayComponent.objects.create(
            code="ot-pay", name="Overtime Pay", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.overtime_type = OvertimeType.objects.create(
            code="regular-ot", name="Regular OT",
            basis=OvertimeType.Basis.PER_HOUR, component=self.component,
        )
        self.department = Department.objects.create(name="Digital Advertising")
        self.access_level = AccessLevel.objects.create(name="Team Leader", rank=20)

    def _row(self, **overrides):
        row = {
            "rate_type": "NIGHT_REGULAR",
            "overtime_type": "",
            "component": "ot-pay",
            "department": "Digital Advertising",
            "team": "",
            "sub_project": "",
            "work_location": "",
            "grade": "",
            "min_gross": "",
            "max_gross": "",
            "resource_status": "",
            "min_access_level": "",
            "basis": "PER_NIGHT",
            "amount": "350.00",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "is_active": "1",
        }
        row.update(overrides)
        return tuple(row[f] for f in FIELDS)

    def _import(self, rows, dry_run=False):
        ds = tablib.Dataset(*rows, headers=FIELDS)
        return PayRateResource().import_data(ds, dry_run=dry_run)

    def test_import_creates_new_rate(self):
        result = self._import([self._row()])
        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        self.assertEqual(PayRate.objects.count(), 1)
        rate = PayRate.objects.get()
        self.assertEqual(rate.department, self.department)
        self.assertEqual(rate.amount, 350)

    def test_dry_run_does_not_persist(self):
        result = self._import([self._row()], dry_run=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(PayRate.objects.count(), 0)

    def test_reimporting_same_key_updates_not_duplicates(self):
        self._import([self._row()])
        self._import([self._row(amount="400.00")])
        self.assertEqual(PayRate.objects.count(), 1)
        self.assertEqual(PayRate.objects.get().amount, 400)

    def test_row_differing_only_by_resource_status_creates_a_second_row(self):
        # 2026-09-16 finding: a narrower key of (rate_type, overtime_type,
        # department, team) already collided on real data for exactly this
        # case — a trainee carve-out sharing every other scope dimension.
        self._import([self._row()])
        self._import([self._row(resource_status="TRAINEE", amount="0.00")])
        self.assertEqual(PayRate.objects.count(), 2)

    def test_row_differing_only_by_gross_band_creates_a_second_row(self):
        self._import([self._row(min_gross="40000.00")])
        self._import([self._row(max_gross="40000.00")])
        self.assertEqual(PayRate.objects.count(), 2)

    def test_blank_resource_status_cell_imports_as_empty_string_not_null(self):
        result = self._import([self._row(resource_status="")])
        self.assertFalse(result.has_errors())
        self.assertEqual(PayRate.objects.get().resource_status, "")

    def test_overtime_type_required_when_rate_type_is_overtime(self):
        result = self._import([self._row(rate_type="OVERTIME", overtime_type="", basis="PER_HOUR")])
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(PayRate.objects.count(), 0)

    def test_gross_30th_multiplier_out_of_range_is_rejected(self):
        result = self._import([self._row(
            rate_type="OVERTIME", overtime_type="regular-ot",
            basis="GROSS_30TH_PER_DAY", amount="15.00",
        )])
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(PayRate.objects.count(), 0)

    def test_export_round_trips_via_natural_keys(self):
        PayRate.objects.create(
            rate_type=PayRate.RateType.OVERTIME, overtime_type=self.overtime_type,
            component=self.component, department=self.department,
            min_access_level=self.access_level,
            basis=PayRate.Basis.PER_HOUR, amount="50.00", effective_from="2026-01-01",
        )
        dataset = PayRateResource().export()
        row = dataset.dict[0]
        self.assertEqual(row["overtime_type"], "regular-ot")
        self.assertEqual(row["component"], "ot-pay")
        self.assertEqual(row["department"], "Digital Advertising")
        self.assertEqual(row["min_access_level"], "Team Leader")
