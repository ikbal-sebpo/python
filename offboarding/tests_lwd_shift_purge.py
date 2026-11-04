"""No roster shift may exist past an employee's last working date.

Covers both halves of the rule:

* the purge — ``roster.services.purge_shifts_after_last_working_date`` and the
  three places it gets wired in (resignation ACCEPTED, a ``CustomUser`` save
  that changes the LWD, and the nightly ``deactivate_past_lwd`` backstop),
  plus the on-demand ``manage.py purge_shifts_past_lwd`` backfill;
* the guards that stop the rows coming back —
  ``roster.services.prefill_shifts_from_pattern`` (the week grid),
  ``roster.upload.apply_upload`` (the bulk importer) and
  ``leave.services.materialise_shifts_for_application``.

Run with:
  venv/bin/python manage.py test offboarding.tests_lwd_shift_purge \
      --settings=core.settings_test

Signal-timing note (same as offboarding/tests.py): the resignation-accept
signal does its work inside ``transaction.on_commit``, so that test wraps the
save in ``self.captureOnCommitCallbacks(execute=True)``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase

from leave.models import (
    LeaveApplication,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
)
from leave.services import materialise_shifts_for_application
from offboarding.models import Resignation
from offboarding.services import deactivate_past_lwd_users, purge_shifts_past_lwd
from roster.models import Roster, Shift, ShiftChangeLog, ShiftType
from roster.services import (
    prefill_shifts_from_pattern,
    purge_shifts_after_last_working_date,
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


def _casual_leave_policy():
    """A minimal balance-tracking LEAVE type (mirrors leave/tests.py's CL)."""
    st, _ = ShiftType.objects.get_or_create(
        name="Casual Leave",
        defaults={"category": ShiftType.Category.LEAVE, "is_active": True},
    )
    st.category = ShiftType.Category.LEAVE
    st.is_working_shift = False
    st.save()
    policy, _ = LeavePolicy.objects.get_or_create(
        shift_type=st,
        defaults={
            "applies_to_gender": LeavePolicy.Gender.ANY,
            "min_service_months": 0,
            "prorate_on_join": True,
            "bridges_sandwich": True,
            "cycle": LeavePolicy.Cycle.CALENDAR,
            "accrual_method": LeavePolicy.AccrualMethod.FIXED_ANNUAL,
            "max_days_per_year": 10,
            "is_paid": True,
        },
    )
    return policy


class PurgeShiftsAfterLWDTests(TestCase):
    """The purge helper itself."""

    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def setUp(self):
        self.lwd = date(2026, 8, 9)
        self.emp = _user("leaver@example.com", last_working_date=self.lwd)

    def _shift(self, day, shift_type=None):
        return Shift.objects.create(
            employee=self.emp, date=day, shift_type=shift_type or self.work,
        )

    def test_deletes_only_dates_strictly_after_lwd(self):
        before = self._shift(self.lwd - timedelta(days=1))
        on_lwd = self._shift(self.lwd)
        after1 = self._shift(self.lwd + timedelta(days=1))
        after2 = self._shift(self.lwd + timedelta(days=30))

        removed = purge_shifts_after_last_working_date(self.emp)

        self.assertEqual(removed, 2)
        surviving = set(Shift.objects.values_list("pk", flat=True))
        self.assertEqual(surviving, {before.pk, on_lwd.pk})
        self.assertFalse(Shift.objects.filter(pk__in=[after1.pk, after2.pk]).exists())

    def test_no_lwd_is_a_no_op(self):
        self.emp.last_working_date = None
        self.emp.save(update_fields=["last_working_date"])
        self._shift(date(2026, 12, 25))
        self.assertEqual(purge_shifts_after_last_working_date(self.emp), 0)
        self.assertEqual(Shift.objects.count(), 1)

    def test_returns_zero_when_nothing_to_purge(self):
        self._shift(self.lwd)
        self.assertEqual(purge_shifts_after_last_working_date(self.emp), 0)
        self.assertEqual(Shift.objects.count(), 1)

    def test_leaves_other_employees_alone(self):
        other = _user("stays@example.com")
        Shift.objects.create(
            employee=other, date=self.lwd + timedelta(days=5), shift_type=self.work,
        )
        self._shift(self.lwd + timedelta(days=5))

        self.assertEqual(purge_shifts_after_last_working_date(self.emp), 1)
        self.assertTrue(Shift.objects.filter(employee=other).exists())

    def test_writes_shift_change_log_rows(self):
        """A real queryset .delete() — pre_delete signals must fire."""
        self._shift(self.lwd + timedelta(days=1))
        ShiftChangeLog.objects.all().delete()

        purge_shifts_after_last_working_date(self.emp)

        self.assertEqual(
            ShiftChangeLog.objects.filter(
                employee=self.emp,
                action_type=ShiftChangeLog.ActionType.DELETED,
            ).count(),
            1,
        )

    def test_actor_is_stamped_before_delete(self):
        hr = _user("hr-actor@example.com")
        self._shift(self.lwd + timedelta(days=1))

        purge_shifts_after_last_working_date(self.emp, actor=hr)

        log = ShiftChangeLog.objects.filter(
            employee=self.emp, action_type=ShiftChangeLog.ActionType.DELETED,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.changed_by_id, hr.pk)

    def test_purging_a_leave_shift_reverses_the_ledger_debit(self):
        policy = _casual_leave_policy()
        # Roster-direct leave (no LeaveApplication) — the post_save signal
        # writes the USED debit synchronously.
        self._shift(self.lwd + timedelta(days=1), shift_type=policy.shift_type)

        bal = LeaveBalance.objects.get(employee=self.emp, leave_policy=policy)
        self.assertEqual(bal.used, Decimal("1"))
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                balance=bal, reason=LeaveBalanceTransaction.Reason.USED,
            ).count(),
            1,
        )

        purge_shifts_after_last_working_date(self.emp)

        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("0"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                balance=bal, reason=LeaveBalanceTransaction.Reason.REVERSED,
            ).exists()
        )


class PrefillGuardTests(TestCase):
    """The week grid must not re-materialise what the purge removed."""

    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def setUp(self):
        # 2026-09-07 is a Monday.
        self.monday = date(2026, 9, 7)
        self.emp = _user("prefill@example.com")
        for wd in range(7):
            Roster.objects.create(
                employee=self.emp, weekday=wd,
                shift_type=self.work if wd < 5 else self.off,
            )

    def test_prefill_stops_at_the_last_working_date(self):
        self.emp.last_working_date = self.monday + timedelta(days=2)   # Wednesday
        self.emp.save(update_fields=["last_working_date"])

        created = prefill_shifts_from_pattern(
            None, [self.emp], self.monday, self.monday + timedelta(days=6),
        )

        self.assertEqual(created, 3)   # Mon, Tue, Wed only
        self.assertEqual(
            list(Shift.objects.order_by("date").values_list("date", flat=True)),
            [self.monday, self.monday + timedelta(days=1), self.monday + timedelta(days=2)],
        )

    def test_prefill_unaffected_without_an_lwd(self):
        created = prefill_shifts_from_pattern(
            None, [self.emp], self.monday, self.monday + timedelta(days=6),
        )
        self.assertEqual(created, 7)

    def test_weekly_pattern_rows_are_never_deleted(self):
        """Keeping the pattern is deliberate — a corrected LWD restores it."""
        self.emp.last_working_date = self.monday
        self.emp.save(update_fields=["last_working_date"])
        prefill_shifts_from_pattern(
            None, [self.emp], self.monday, self.monday + timedelta(days=6),
        )
        self.assertEqual(Roster.objects.filter(employee=self.emp).count(), 7)


class UploadGuardTests(TestCase):
    """The bulk roster importer is how the stale rows got in originally."""

    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def setUp(self):
        self.lwd = date(2026, 8, 9)
        self.emp = _user("uploaded@example.com", last_working_date=self.lwd)
        self.emp.employee_id = "51761"
        self.emp.save(update_fields=["employee_id"])
        self.viewer = _user("supervisor@example.com")

    def _row(self, day, line_no=3):
        return ParsedRow(
            line_no=line_no, col_letter="D", employee=self.emp,
            employee_id=self.emp.employee_id, date=day,
            shift_type=self.work, shift_attribute=None,
            job_location=None, work_location=None,
        )

    def test_rows_past_lwd_are_skipped_and_reported(self):
        report = apply_upload(
            self.viewer,
            [self._row(self.lwd), self._row(self.lwd + timedelta(days=1), line_no=4)],
            {self.emp.pk},
        )

        self.assertTrue(report.applied)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(len(report.skipped_past_lwd), 1)
        self.assertIn("Past last working date", report.skipped_past_lwd[0].message)
        self.assertEqual(
            list(Shift.objects.values_list("date", flat=True)), [self.lwd],
        )

    def test_employee_without_lwd_is_unaffected(self):
        self.emp.last_working_date = None
        self.emp.save(update_fields=["last_working_date"])

        report = apply_upload(
            self.viewer, [self._row(self.lwd + timedelta(days=1))], {self.emp.pk},
        )

        self.assertEqual(len(report.created), 1)
        self.assertEqual(report.skipped_past_lwd, [])


class LeaveMaterialisationGuardTests(TestCase):
    """An approved leave running past a later-set LWD must not re-create rows."""

    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def test_billable_dates_are_clipped_at_the_lwd(self):
        policy = _casual_leave_policy()
        # Mon 2026-09-07 → Fri 2026-09-11, LWD lands mid-range on the Tuesday.
        start = date(2026, 9, 7)
        end = date(2026, 9, 11)
        emp = _user("onleave@example.com", last_working_date=start + timedelta(days=1))
        for wd in range(7):
            Roster.objects.create(
                employee=emp, weekday=wd,
                shift_type=self.work if wd < 5 else self.off,
            )
        app = LeaveApplication.objects.create(
            employee=emp, leave_type=policy.shift_type,
            start_date=start, end_date=end,
            applied_by=emp,
            status=LeaveApplication.Status.APPROVED,
        )

        written = materialise_shifts_for_application(app, approver=None)

        self.assertEqual(written, 2)
        self.assertEqual(
            list(Shift.objects.order_by("date").values_list("date", flat=True)),
            [start, start + timedelta(days=1)],
        )


class LWDChangeSignalTests(TestCase):
    """A plain CustomUser.save() that moves the LWD triggers the purge.

    Covers the HR edit form (accounts.views.UserUpdateView) and the Django
    admin Employment fieldset — both go through a normal save().
    """

    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def setUp(self):
        self.emp = _user("hredit@example.com")
        self.future = date.today() + timedelta(days=10)
        Shift.objects.create(
            employee=self.emp, date=self.future, shift_type=self.work,
        )

    def test_setting_an_lwd_purges_later_shifts(self):
        self.emp.last_working_date = date.today()
        self.emp.save()
        self.assertFalse(Shift.objects.filter(date=self.future).exists())

    def test_moving_the_lwd_earlier_purges_again(self):
        self.emp.last_working_date = self.future + timedelta(days=5)
        self.emp.save()
        self.assertTrue(Shift.objects.filter(date=self.future).exists())

        self.emp.last_working_date = self.future - timedelta(days=1)
        self.emp.save()
        self.assertFalse(Shift.objects.filter(date=self.future).exists())

    def test_saving_without_changing_the_lwd_purges_nothing(self):
        self.emp.last_working_date = self.future + timedelta(days=5)
        self.emp.save()
        self.emp.first_name = "Renamed"
        self.emp.save()
        self.assertTrue(Shift.objects.filter(date=self.future).exists())

    def test_clearing_the_lwd_purges_nothing(self):
        self.emp.last_working_date = self.future + timedelta(days=5)
        self.emp.save()
        self.emp.last_working_date = None
        self.emp.save()
        self.assertTrue(Shift.objects.filter(date=self.future).exists())

    def test_creating_a_user_with_an_lwd_does_not_error(self):
        other = _user("newleaver@example.com", last_working_date=date.today())
        self.assertIsNotNone(other.pk)


class ResignationAcceptPurgeTests(TestCase):
    """Accepting a resignation writes the LWD through and purges."""

    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()
        for name in ("User Admins", "HR", "All Employees"):
            Group.objects.get_or_create(name=name)
        # Without a seeded workflow the accept branch logs a swallowed
        # "couldn't create the case" error — irrelevant here, just noisy.
        call_command("seed_offboarding_workflow", stdout=StringIO())

    def test_accept_purges_shifts_past_the_new_lwd(self):
        emp = _user("resigning@example.com")
        hr = _user("hr-accept@example.com")
        hr.groups.add(Group.objects.get(name="HR"))
        lwd = date.today() + timedelta(days=30)
        kept = Shift.objects.create(employee=emp, date=lwd, shift_type=self.work)
        stale = Shift.objects.create(
            employee=emp, date=lwd + timedelta(days=1), shift_type=self.work,
        )

        resignation = Resignation.objects.create(
            employee=emp, applied_by=emp,
            notice_start_date=date.today(),
            last_working_date=lwd,
            reason="moving on",
        )
        resignation.status = Resignation.Status.ACCEPTED
        with self.captureOnCommitCallbacks(execute=True):
            resignation.save()

        emp.refresh_from_db()
        self.assertEqual(emp.last_working_date, lwd)
        self.assertTrue(Shift.objects.filter(pk=kept.pk).exists())
        self.assertFalse(Shift.objects.filter(pk=stale.pk).exists())


class DeactivateBackstopPurgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def setUp(self):
        self.lwd = date.today() - timedelta(days=3)
        self.emp = _user("gone@example.com", last_working_date=self.lwd)
        self.kept = Shift.objects.create(
            employee=self.emp, date=self.lwd, shift_type=self.work,
        )
        self.stale = Shift.objects.create(
            employee=self.emp, date=self.lwd + timedelta(days=1), shift_type=self.work,
        )

    def test_nightly_deactivation_also_purges(self):
        touched = deactivate_past_lwd_users()

        self.assertEqual([e for e, _lwd in touched], [self.emp.email])
        self.emp.refresh_from_db()
        self.assertFalse(self.emp.is_active)
        self.assertTrue(Shift.objects.filter(pk=self.kept.pk).exists())
        self.assertFalse(Shift.objects.filter(pk=self.stale.pk).exists())

    def test_dry_run_deletes_nothing(self):
        deactivate_past_lwd_users(dry_run=True)

        self.emp.refresh_from_db()
        self.assertTrue(self.emp.is_active)
        self.assertEqual(Shift.objects.count(), 2)


class PurgeBackfillCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.work, cls.off = _shift_types()

    def setUp(self):
        self.lwd = date(2026, 8, 9)
        self.emp = _user("backfill@example.com", last_working_date=self.lwd)
        Shift.objects.create(
            employee=self.emp, date=self.lwd, shift_type=self.work,
        )
        for i in (1, 2, 3):
            Shift.objects.create(
                employee=self.emp, date=self.lwd + timedelta(days=i),
                shift_type=self.work,
            )

    def test_dry_run_reports_without_deleting(self):
        rows = purge_shifts_past_lwd(dry_run=True)

        self.assertEqual(len(rows), 1)
        _name, email, lwd, count = rows[0]
        self.assertEqual((email, lwd, count), (self.emp.email, self.lwd, 3))
        self.assertEqual(Shift.objects.count(), 4)

    def test_real_run_deletes(self):
        rows = purge_shifts_past_lwd()

        self.assertEqual(rows[0][3], 3)
        self.assertEqual(
            list(Shift.objects.values_list("date", flat=True)), [self.lwd],
        )

    def test_command_dry_run_is_opt_in(self):
        out = StringIO()
        call_command("purge_shifts_past_lwd", "--dry-run", stdout=out)
        text = out.getvalue()

        self.assertIn("would purge 3 shift(s)", text)
        self.assertIn(self.emp.email, text)
        self.assertEqual(Shift.objects.count(), 4)

        out = StringIO()
        call_command("purge_shifts_past_lwd", stdout=out)
        self.assertIn("purged 3 shift(s)", out.getvalue())
        self.assertEqual(Shift.objects.count(), 1)
