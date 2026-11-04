"""The topbar notification bell (core/approvals.py) and the "Direct reports
only" switch it shares a scope rule with on the timesheet-corrections, shift
change and dedicated night shift lists (the leave list's own switch is
covered in leave/tests.py).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AccessLevel, Department, Designation
from attendance.models import AttendanceCorrection
from audit.models import AccessLog
from core.approvals import APPROVALS_CHANGED_EVENT, bell_summary, night_shift_inbox_pks
from employees.models import DedicatedNightShiftAssignment, JobInfo
from leave.models import LeaveApplication
from roster.models import ShiftChangeRequest, ShiftType


User = get_user_model()


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].split(".")[0].title(), last_name="T",
        **extra,
    )


def _aware(d: date, hour: int):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour))


class _Fixture(TestCase):
    """manager → alice (direct) → grand (indirect); bob is unrelated."""

    @classmethod
    def setUpTestData(cls):
        for name in ("HR", "All Employees"):
            Group.objects.get_or_create(name=name)
        cls.dept = Department.objects.create(name="Operations")
        # Manager tier — required to approve a dedicated night shift.
        manager_level = AccessLevel.objects.create(name="Manager", rank=30)
        program_manager = Designation.objects.create(name="Program Manager", access_level=manager_level)
        cls.program_manager = program_manager
        cls.manager = _user(
            "manager.bell@example.com", employee_id="61000", department=cls.dept,
            designation=program_manager,
        )
        cls.alice = _user("alice.bell@example.com", employee_id="61001", department=cls.dept)
        cls.grand = _user("grand.bell@example.com", employee_id="61002", department=cls.dept)
        cls.bob = _user("bob.bell@example.com", employee_id="61003", department=cls.dept)
        cls.hr = _user("hr.bell@example.com", employee_id="61004", department=cls.dept)
        cls.hr.groups.add(Group.objects.get(name="HR"))
        JobInfo.objects.create(employee=cls.alice, reports_to=cls.manager)
        JobInfo.objects.create(employee=cls.grand, reports_to=cls.alice)

        cls.leave_type = ShiftType.objects.create(name="CL-BELL", category=ShiftType.Category.LEAVE)
        cls.alice_leave = cls._leave(cls.alice)
        cls.grand_leave = cls._leave(cls.grand)
        cls.bob_leave = cls._leave(cls.bob)
        cls.manager_leave = cls._leave(cls.manager)  # own request — never "waiting on you"

        cls.day = date(2026, 10, 12)
        cls.alice_fix = cls._correction(cls.alice)
        cls.grand_fix = cls._correction(cls.grand)
        cls.bob_fix = cls._correction(cls.bob)

        cls.eve = ShiftType.objects.create(name="EVE-BELL", category=ShiftType.Category.WORK)
        cls.alice_change = cls._shift_change(cls.alice)
        cls.grand_change = cls._shift_change(cls.grand)
        cls.bob_change = cls._shift_change(cls.bob)

        cls.alice_night = cls._night(cls.alice)
        cls.grand_night = cls._night(cls.grand)
        cls.bob_night = cls._night(cls.bob)

    @classmethod
    def _leave(cls, employee, status=LeaveApplication.Status.PENDING):
        return LeaveApplication.objects.create(
            employee=employee, leave_type=cls.leave_type,
            start_date=date(2026, 10, 21), end_date=date(2026, 10, 21),
            effective_days=Decimal("1.00"), working_days=Decimal("1.00"),
            status=status, applied_by=employee,
        )

    @classmethod
    def _correction(cls, employee):
        return AttendanceCorrection.objects.create(
            employee=employee, date=cls.day,
            requested_check_in=_aware(cls.day, 10), requested_check_out=_aware(cls.day, 19),
            reason="forgot", requested_by=employee,
        )

    @classmethod
    def _shift_change(cls, employee):
        return ShiftChangeRequest.objects.create(
            employee=employee, date=date(2026, 10, 22),
            requested_shift_type=cls.eve, requested_location="WFH",
            reason="later start", applied_by=employee,
        )

    @classmethod
    def _night(cls, employee):
        return DedicatedNightShiftAssignment.objects.create(
            employee=employee, start_date=date(2026, 11, 1), end_date=date(2026, 12, 31),
        )

    def setUp(self):
        cache.clear()  # approval_inbox_pks caches the scope for 60s


class CorrectionsDirectReportsSwitchTests(_Fixture):

    def _pending(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse("attendance_correction_list"), {"tab": "pending", **params})
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_pending_tab_defaults_to_direct_reports_only(self):
        resp = self._pending(self.manager)
        self.assertTrue(resp.context["direct_only"])
        self.assertTrue(resp.context["has_direct_reports"])
        self.assertEqual([c.pk for c in resp.context["rows"]], [self.alice_fix.pk])
        self.assertEqual(resp.context["pending_count"], 1)
        self.assertContains(resp, "Direct reports only")
        self.assertFalse(resp.context["filters_active"])
        # Every page's topbar carries the polling bell.
        self.assertContains(resp, f'hx-get="{reverse("notification_bell")}"')

    def test_unticked_shows_the_whole_approval_scope(self):
        resp = self._pending(self.manager, direct="0")
        self.assertFalse(resp.context["direct_only"])
        self.assertEqual(
            {c.pk for c in resp.context["rows"]}, {self.alice_fix.pk, self.grand_fix.pk},
        )
        self.assertEqual(resp.context["pending_count"], 2)
        # The filter popover carries the unticked state through a re-filter.
        self.assertContains(resp, '<input type="hidden" name="direct" value="0">', html=True)

    def test_viewer_without_direct_reports_gets_full_scope_and_no_switch(self):
        resp = self._pending(self.hr)
        self.assertFalse(resp.context["has_direct_reports"])
        self.assertFalse(resp.context["direct_only"])
        self.assertNotContains(resp, "Direct reports only")
        self.assertEqual(
            {c.pk for c in resp.context["rows"]},
            {self.alice_fix.pk, self.grand_fix.pk, self.bob_fix.pk},
        )


class ShiftChangeAndNightShiftSwitchTests(_Fixture):
    """Same switch on the shift change and dedicated night shift lists."""

    CASES = (
        ("shift_change_list", "alice_change", "grand_change"),
        ("dedicated_night_shift_list", "alice_night", "grand_night"),
    )

    def test_shift_change_pending_tab_defaults_to_direct_reports_only(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("shift_change_list"), {"tab": "pending"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["direct_only"])
        self.assertEqual([r.pk for r in resp.context["rows"]], [self.alice_change.pk])
        self.assertEqual(resp.context["pending_count"], 1)
        self.assertContains(resp, "Direct reports only")

    def test_unticked_shows_the_whole_approval_scope(self):
        self.client.force_login(self.manager)
        for route, direct, indirect in self.CASES:
            with self.subTest(route=route):
                resp = self.client.get(reverse(route), {"tab": "pending", "direct": "0"})
                self.assertFalse(resp.context["direct_only"])
                self.assertEqual(
                    {r.pk for r in resp.context["rows"]},
                    {getattr(self, direct).pk, getattr(self, indirect).pk},
                )

    def test_shift_change_tabs_carry_the_unticked_state(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("shift_change_list"), {"tab": "pending", "direct": "0"})
        self.assertIn("direct=0", resp.context["query_suffix"])
        resp = self.client.get(reverse("shift_change_list"), {"tab": "pending"})
        self.assertNotIn("direct=", resp.context["query_suffix"])

    def test_viewer_without_direct_reports_gets_no_switch(self):
        self.client.force_login(self.hr)
        for route, _, _ in self.CASES:
            with self.subTest(route=route):
                resp = self.client.get(reverse(route), {"tab": "pending"})
                self.assertFalse(resp.context["has_direct_reports"])
                self.assertNotContains(resp, "Direct reports only")


class NightShiftNearestApproverTests(_Fixture):
    """A night shift needs Manager tier, so the default scope reaches past
    below-tier managers to the nearest one who can approve it."""

    def _night_pending(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse("dedicated_night_shift_list"), {"tab": "pending", **params})
        self.assertEqual(resp.status_code, 200)
        return {r.pk for r in resp.context["rows"]}

    def test_reaches_past_a_below_tier_direct_report(self):
        # alice (between manager and grand) can't approve night shifts, so
        # grand's request is waiting on the manager.
        self.assertEqual(night_shift_inbox_pks(self.manager), {self.alice.pk, self.grand.pk})
        self.assertEqual(self._night_pending(self.manager), {self.alice_night.pk, self.grand_night.pk})

    def test_stops_at_a_nearer_manager_tier_approver(self):
        self.alice.designation = self.program_manager
        self.alice.save()
        self.assertEqual(night_shift_inbox_pks(self.manager), {self.alice.pk})
        self.assertEqual(self._night_pending(self.manager), {self.alice_night.pk})
        cache.clear()
        self.assertEqual(night_shift_inbox_pks(self.alice), {self.grand.pk})
        # Unticked still shows the whole approval scope.
        self.assertEqual(
            self._night_pending(self.manager, direct="0"),
            {self.alice_night.pk, self.grand_night.pk},
        )

    def test_below_tier_viewer_has_no_night_shift_section(self):
        sections = {s["key"] for s in bell_summary(self.alice)["sections"]}
        self.assertNotIn("night_shift", sections)


class BellSummaryTests(_Fixture):

    def test_manager_sees_direct_reports_only(self):
        bell = bell_summary(self.manager)
        by_key = {s["key"]: s for s in bell["sections"]}
        self.assertEqual(bell["total"], 5)
        self.assertEqual(
            {k: s["count"] for k, s in by_key.items()},
            # night shift: alice + grand — alice is below Manager tier.
            {"leave": 1, "timesheet": 1, "shift_change": 1, "night_shift": 2},
        )
        self.assertEqual(
            by_key["shift_change"]["items"][0]["url"],
            reverse("shift_change_detail", args=[self.alice_change.pk]),
        )
        self.assertEqual(
            {i["employee"] for i in by_key["night_shift"]["items"]}, {self.alice, self.grand},
        )
        self.assertEqual(by_key["leave"]["items"][0]["url"], reverse("leave_detail", args=[self.alice_leave.pk]))
        self.assertEqual(by_key["timesheet"]["items"][0]["employee"], self.alice)

    def test_counts_match_the_pending_tabs_they_link_to(self):
        self.client.force_login(self.manager)
        bell = {s["key"]: s["count"] for s in bell_summary(self.manager)["sections"]}
        pages = {
            "leave": "leave_list",
            "timesheet": "attendance_correction_list",
            "shift_change": "shift_change_list",
            "night_shift": "dedicated_night_shift_list",
        }
        self.assertEqual(set(bell), set(pages))
        for key, route in pages.items():
            with self.subTest(key=key):
                resp = self.client.get(reverse(route), {"tab": "pending"})
                self.assertEqual(bell[key], resp.context["pending_count"])

    def test_without_direct_reports_falls_back_to_managed_scope(self):
        # alice's inbox is grand (her direct report); HR has org-wide
        # visibility but no managed scope — seeing everything isn't "waiting".
        alice = {s["key"]: s["count"] for s in bell_summary(self.alice)["sections"]}
        # No night shift section: alice is below Manager tier, so she could
        # never approve one (has_dedicated_night_shift_approval_scope).
        self.assertEqual(alice, {"leave": 1, "timesheet": 1, "shift_change": 1})
        self.assertEqual(bell_summary(self.hr)["total"], 0)
        self.assertEqual(bell_summary(self.bob)["sections"], [])

    def test_signature_changes_when_the_pending_set_does(self):
        before = bell_summary(self.manager)["signature"]
        self.assertEqual(before, bell_summary(self.manager)["signature"])
        self.alice_fix.status = AttendanceCorrection.Status.DECLINED
        self.alice_fix.save()
        after = bell_summary(self.manager)
        self.assertNotEqual(before, after["signature"])
        self.assertEqual(after["total"], 4)


class BellEndpointTests(_Fixture):

    def test_renders_items_and_out_of_band_badge(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("notification_bell"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertContains(resp, 'data-bell-total="5"')
        self.assertContains(resp, 'id="notification-bell-badge" hx-swap-oob="true"')
        self.assertContains(resp, reverse("leave_detail", args=[self.alice_leave.pk]))
        self.assertNotContains(resp, reverse("leave_detail", args=[self.grand_leave.pk]))

    def test_empty_state(self):
        self.client.force_login(self.bob)
        resp = self.client.get(reverse("notification_bell"))
        self.assertContains(resp, "all caught up")
        self.assertContains(resp, 'data-bell-total="0"')

    def test_anonymous_poll_gets_401_not_the_login_page(self):
        resp = self.client.get(reverse("notification_bell"))
        self.assertEqual(resp.status_code, 401)

    def test_poll_is_not_access_logged(self):
        self.client.force_login(self.manager)
        before = AccessLog.objects.count()
        self.client.get(reverse("notification_bell"))
        self.assertEqual(AccessLog.objects.filter(route="notification_bell").count(), 0)
        self.assertEqual(AccessLog.objects.count(), before)

    def test_htmx_leave_decision_triggers_bell_refresh(self):
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("leave_decline", args=[self.alice_leave.pk]), HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Trigger"], APPROVALS_CHANGED_EVENT)

    def test_htmx_shift_change_decision_triggers_bell_refresh(self):
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("shift_change_decline", args=[self.alice_change.pk]), HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Trigger"], APPROVALS_CHANGED_EVENT)


class OvertimeBellTests(_Fixture):
    """The two overtime sections (2026-10-17): "to review" waits on the
    nearest Manager-tier person up the chain, "to approve" on the
    department head / additional managers. Entries are created per test,
    not in the shared fixture, so the other bell counts stay as they are."""

    def setUp(self):
        super().setUp()
        from payroll.models import OvertimeEntry, OvertimeType, PayComponent, PayrollPeriod
        from payroll.periods import period_for_date

        component = PayComponent.objects.create(
            code="OT-BELL", name="Overtime", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.COMPUTED,
        )
        self.ot_type = OvertimeType.objects.create(
            code="bell_ot", name="Bell OT", basis=OvertimeType.Basis.PER_HOUR, component=component,
        )
        self.today = date(2026, 10, 14)
        self.period = PayrollPeriod.for_month(*period_for_date(self.today))
        self.alice_ot = self._ot(self.alice)
        self.grand_ot = self._ot(self.grand)
        self.bob_ot = self._ot(self.bob)
        self.manager_ot = self._ot(self.manager)
        self.OvertimeEntry = OvertimeEntry

    def _ot(self, employee, status="PENDING"):
        from payroll.models import OvertimeEntry

        return OvertimeEntry.objects.create(
            employee=employee, period=self.period, work_date=self.today,
            overtime_type=self.ot_type, seconds=3600, status=status,
        )

    def test_nearest_manager_tier_gets_the_review_section(self):
        by_key = {s["key"]: s for s in bell_summary(self.manager)["sections"]}
        self.assertEqual(by_key["overtime_review"]["count"], 2)  # alice + grand, never own
        self.assertEqual(
            {i["employee"] for i in by_key["overtime_review"]["items"]}, {self.alice, self.grand},
        )
        self.assertTrue(by_key["overtime_review"]["url"].endswith("?awaiting=me"))
        self.assertNotIn("overtime_approve", by_key)  # not a department head

    def test_below_tier_viewer_has_no_overtime_sections(self):
        keys = {s["key"] for s in bell_summary(self.alice)["sections"]}
        self.assertNotIn("overtime_review", keys)
        self.assertNotIn("overtime_approve", keys)

    def test_department_head_gets_the_approve_section_for_both_stages(self):
        self.dept.head_of_department = self.hr
        self.dept.save()
        cache.clear()
        self.grand_ot.status = "REVIEWED"
        self.grand_ot.save()
        by_key = {s["key"]: s for s in bell_summary(self.hr)["sections"]}
        # alice (pending), grand (reviewed), bob, manager — everyone in the
        # department but the head herself.
        self.assertEqual(by_key["overtime_approve"]["count"], 4)
        self.assertNotIn("overtime_review", by_key)  # hr holds no Manager tier

    def test_review_section_drops_once_reviewed(self):
        self.alice_ot.status = "REVIEWED"
        self.alice_ot.save()
        by_key = {s["key"]: s for s in bell_summary(self.manager)["sections"]}
        self.assertEqual(by_key["overtime_review"]["count"], 1)

    def test_hr_payroll_fallback_gets_no_bell_section(self):
        self.hr.groups.add(Group.objects.get_or_create(name="Payroll")[0])
        self.assertEqual(bell_summary(self.hr)["sections"], [])
