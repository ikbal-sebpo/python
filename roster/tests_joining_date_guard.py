"""No roster shift may exist before an employee's joining date.

Mirror image of offboarding/tests_lwd_shift_purge.py, covering:

* the guards that stop the rows appearing —
  ``roster.services.prefill_shifts_from_pattern`` (the week grid),
  ``roster.upload.apply_upload`` (the bulk importer) and
  ``roster.views.CellUpdateView`` (manual single-cell edits, which also
  gained its first-ever LWD guard alongside this);
* the read-only audit (``roster.services.audit_shifts_before_joining_date``)
  and the purge helper + on-demand backfill command
  (``roster.services.purge_shifts_before_joining_date`` /
  ``manage.py purge_shifts_before_joining_date``).
"""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse

from roster.models import Roster, Shift, ShiftType
from roster.services import (
    audit_shifts_before_joining_date,
    prefill_shifts_from_pattern,
    purge_shifts_before_joining_date,
    shift_is_before_joining_date,
)
from roster.upload import ParsedRow, apply_upload


User = get_user_model()


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        **extra,
    )


def _shift_types():
    work, _ = ShiftType.objects.get_or_create(
        name="Morning",
        defaults={"category": ShiftType.Category.WORK, "is_active": True},
    )
    off, _ = ShiftType.objects.get_or_create(
        name="Weekly Off",
        defaults={"category": ShiftType.Category.OFF, "is_active": True},
    )
    return work, off


class ShiftIsBeforeJoiningDateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.emp = _user("newhire@example.com", joining_date=date(2026, 9, 15))

    def test_before_joining_is_true(self):
        self.assertTrue(shift_is_before_joining_date(self.emp, date(2026, 9, 14)))

    def test_on_or_after_joining_is_false(self):
        self.assertFalse(shift_is_before_joining_date(self.emp, date(2026, 9, 15)))
        self.assertFalse(shift_is_before_joining_date(self.emp, date(2026, 9, 16)))

    def test_no_joining_date_is_never_before(self):
        self.emp.joining_date = None
        self.assertFalse(shift_is_before_joining_date(self.emp, date(2000, 1, 1)))


class PrefillGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()
        cls.emp = _user("newhire2@example.com", joining_date=date(2026, 9, 15))
        for wd in range(7):
            Roster.objects.create(
                employee=cls.emp, weekday=wd,
                shift_type=cls.off if wd in (5, 6) else cls.work,
            )

    def test_prefill_stops_before_the_joining_date(self):
        prefill_shifts_from_pattern(
            None, [self.emp], date(2026, 9, 10), date(2026, 9, 20),
        )
        dates = set(
            Shift.objects.filter(employee=self.emp).values_list("date", flat=True)
        )
        self.assertNotIn(date(2026, 9, 14), dates)
        self.assertIn(date(2026, 9, 15), dates)
        self.assertIn(date(2026, 9, 16), dates)

    def test_prefill_unaffected_without_a_joining_date(self):
        self.emp.joining_date = None
        self.emp.save(update_fields=["joining_date"])
        prefill_shifts_from_pattern(
            None, [self.emp], date(2026, 9, 10), date(2026, 9, 12),
        )
        self.assertEqual(
            Shift.objects.filter(employee=self.emp).count(), 3,
        )


class UploadGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.work, _off = _shift_types()
        cls.viewer = _user("uploader@example.com")
        cls.emp = _user("newhire3@example.com", joining_date=date(2026, 9, 15))

    def _row(self, day, line_no=3):
        return ParsedRow(
            line_no=line_no, col_letter="D", employee=self.emp,
            employee_id=self.emp.employee_id, date=day,
            shift_type=self.work, shift_attribute=None,
            job_location=None, work_location=None,
        )

    def test_rows_before_joining_are_skipped_and_reported(self):
        report = apply_upload(
            self.viewer,
            [self._row(date(2026, 9, 14)), self._row(date(2026, 9, 15), line_no=4)],
            {self.emp.pk},
        )
        self.assertTrue(report.applied)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(len(report.skipped_before_joining), 1)
        self.assertIn("Before joining date", report.skipped_before_joining[0].message)
        self.assertEqual(
            list(Shift.objects.values_list("date", flat=True)), [date(2026, 9, 15)],
        )

    def test_employee_without_joining_date_is_unaffected(self):
        self.emp.joining_date = None
        self.emp.save(update_fields=["joining_date"])
        report = apply_upload(self.viewer, [self._row(date(2026, 9, 14))], {self.emp.pk})
        self.assertEqual(len(report.created), 1)
        self.assertEqual(report.skipped_before_joining, [])


class CellUpdateTenureGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for name in ("User Admins", "HR", "All Employees", "Senior Leadership"):
            Group.objects.get_or_create(name=name)
        cls.work, _off = _shift_types()
        cls.admin = _user("admin@example.com")
        cls.admin.groups.add(Group.objects.get(name="User Admins"))
        cls.emp = _user(
            "tenure@example.com",
            joining_date=date(2026, 9, 15), last_working_date=date(2026, 9, 20),
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_cannot_create_a_shift_before_joining_date(self):
        url = reverse("roster_cell_update", args=[self.emp.pk, "2026-09-14"])
        resp = self.client.post(url, {"shift_ref": f"type:{self.work.pk}"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("joined on", resp["HX-Error-Message"])
        self.assertFalse(Shift.objects.filter(employee=self.emp, date="2026-09-14").exists())

    def test_cannot_create_a_shift_after_last_working_date(self):
        url = reverse("roster_cell_update", args=[self.emp.pk, "2026-09-21"])
        resp = self.client.post(url, {"shift_ref": f"type:{self.work.pk}"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("last working date", resp["HX-Error-Message"])
        self.assertFalse(Shift.objects.filter(employee=self.emp, date="2026-09-21").exists())

    def test_can_create_a_shift_within_tenure(self):
        url = reverse("roster_cell_update", args=[self.emp.pk, "2026-09-16"])
        resp = self.client.post(url, {"shift_ref": f"type:{self.work.pk}"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Shift.objects.filter(employee=self.emp, date="2026-09-16").exists())

    def test_deleting_an_out_of_tenure_row_stays_allowed(self):
        # Both fields must be present as "" — see CellUpdateView: `shift_ref
        # = request.POST.get("shift_ref") or request.POST.get("shift_type")`
        # collapses to None (not "") if "shift_type" is entirely absent, which
        # skips the delete branch entirely. Matches the view's own documented
        # contract even though the grid's own dropdown can't reach it today
        # (its blank option is `disabled` once a shift exists).
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 14), shift_type=self.work)
        url = reverse("roster_cell_update", args=[self.emp.pk, "2026-09-14"])
        resp = self.client.post(url, {"shift_ref": "", "shift_type": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Shift.objects.filter(employee=self.emp, date="2026-09-14").exists())


class PurgeShiftsBeforeJoiningDateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.work, _off = _shift_types()
        cls.emp = _user("purge@example.com", joining_date=date(2026, 9, 15))
        cls.other = _user("other@example.com", joining_date=date(2026, 9, 15))

    def test_deletes_only_dates_strictly_before_joining(self):
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 13), shift_type=self.work)
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 14), shift_type=self.work)
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 15), shift_type=self.work)
        removed = purge_shifts_before_joining_date(self.emp)
        self.assertEqual(removed, 2)
        self.assertEqual(
            list(Shift.objects.filter(employee=self.emp).values_list("date", flat=True)),
            [date(2026, 9, 15)],
        )

    def test_leaves_other_employees_alone(self):
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 14), shift_type=self.work)
        Shift.objects.create(employee=self.other, date=date(2026, 9, 14), shift_type=self.work)
        purge_shifts_before_joining_date(self.emp)
        self.assertTrue(Shift.objects.filter(employee=self.other, date="2026-09-14").exists())

    def test_no_joining_date_is_a_no_op(self):
        self.emp.joining_date = None
        self.emp.save(update_fields=["joining_date"])
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 14), shift_type=self.work)
        self.assertEqual(purge_shifts_before_joining_date(self.emp), 0)


class AuditShiftsBeforeJoiningDateTests(TestCase):
    def test_reports_affected_employees_only(self):
        work, _off = _shift_types()
        clean = _user("clean@example.com", joining_date=date(2026, 9, 15))
        Shift.objects.create(employee=clean, date=date(2026, 9, 15), shift_type=work)
        dirty = _user("dirty@example.com", joining_date=date(2026, 9, 15))
        Shift.objects.create(employee=dirty, date=date(2026, 9, 13), shift_type=work)
        Shift.objects.create(employee=dirty, date=date(2026, 9, 14), shift_type=work)

        rows = audit_shifts_before_joining_date()
        emails = {r[1] for r in rows}
        self.assertIn("dirty@example.com", emails)
        self.assertNotIn("clean@example.com", emails)
        dirty_row = next(r for r in rows if r[1] == "dirty@example.com")
        _name, _email, joining_date, count, earliest, latest = dirty_row
        self.assertEqual(joining_date, date(2026, 9, 15))
        self.assertEqual(count, 2)
        self.assertEqual(earliest, date(2026, 9, 13))
        self.assertEqual(latest, date(2026, 9, 14))


class PurgeBackfillCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.work, _off = _shift_types()
        cls.emp = _user(
            "cmd@example.com", employee_id="99001", joining_date=date(2026, 9, 15),
        )
        cls.other = _user(
            "cmd2@example.com", employee_id="99002", joining_date=date(2026, 9, 15),
        )

    def setUp(self):
        Shift.objects.create(employee=self.emp, date=date(2026, 9, 14), shift_type=self.work)
        Shift.objects.create(employee=self.other, date=date(2026, 9, 14), shift_type=self.work)

    def test_refuses_with_no_scope(self):
        with self.assertRaises(CommandError):
            call_command("purge_shifts_before_joining_date")
        self.assertEqual(Shift.objects.count(), 2)

    def test_dry_run_reports_without_deleting(self):
        out = StringIO()
        call_command(
            "purge_shifts_before_joining_date",
            "--employee-id", "99001", "--dry-run", stdout=out,
        )
        self.assertIn("would purge 1 shift", out.getvalue())
        self.assertEqual(Shift.objects.count(), 2)

    def test_employee_id_scopes_the_purge(self):
        call_command("purge_shifts_before_joining_date", "--employee-id", "99001")
        self.assertFalse(Shift.objects.filter(employee=self.emp).exists())
        self.assertTrue(Shift.objects.filter(employee=self.other).exists())

    def test_all_sweeps_everyone(self):
        call_command("purge_shifts_before_joining_date", "--all")
        self.assertEqual(Shift.objects.count(), 0)

    def test_unknown_employee_id_errors(self):
        with self.assertRaises(CommandError):
            call_command("purge_shifts_before_joining_date", "--employee-id", "no-such-id")
        self.assertEqual(Shift.objects.count(), 2)
