"""Dedicated night shift windows on the roster — department-head approval
(2026-11-03, docs/SHIFT_CHANGE_REQUEST_PLAN.md "Dedicated night shift
windows", roster/night_window.py).

Run with:
  python manage.py test roster.tests_night_window --settings=core.settings_test
"""

from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.permissions import can_act_on_shift_change, can_cancel_shift_change
from core.approvals import pending_shift_change_qs
from employees.models import DedicatedNightShiftAssignment, JobInfo
from accounts.models import Department, Team
from roster import night_window
from roster.models import Roster, Shift, ShiftChangeRequest, ShiftType
from roster.upload import ParsedRow, apply_upload

User = get_user_model()

TODAY = date.today()
WIN_START = TODAY + timedelta(days=7)
WIN_END = TODAY + timedelta(days=20)
IN_WINDOW = WIN_START + timedelta(days=2)
AFTER_WINDOW = WIN_END + timedelta(days=3)


def _user(email, **extra):
    extra.setdefault("joining_date", TODAY - timedelta(days=400))
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T", **extra,
    )


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class _Base(TestCase):
    """director ← head (Digital Advertising head) ← apm ← tl ← agent;
    `addl` is a Department.additional_managers member; `hr` is HR."""

    @classmethod
    def setUpTestData(cls):
        for name in ("User Admins", "HR", "All Employees"):
            Group.objects.get_or_create(name=name)
        cls.dept = Department.objects.create(name="Digital Advertising")
        cls.team = Team.objects.create(name="AdOps 1", department=cls.dept)

        cls.mor = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        cls.nig = ShiftType.objects.create(name="NIG", category=ShiftType.Category.WORK)
        cls.cust = ShiftType.objects.create(name="CUST", category=ShiftType.Category.WORK)
        cls.off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)

        cls.director = _user("director@example.com")
        cls.head = _user("head@example.com", department=cls.dept)
        cls.apm = _user("apm@example.com", department=cls.dept, team=cls.team)
        cls.tl = _user("tl@example.com", department=cls.dept, team=cls.team)
        cls.agent = _user("agent@example.com", department=cls.dept, team=cls.team)
        cls.addl = _user("addl@example.com", department=cls.dept)
        cls.hr = _user("hr@example.com", department=cls.dept)
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.root = User.objects.create_superuser(
            email="root@example.com", username="root@example.com", password="x",
        )
        cls.dept.head_of_department = cls.head
        cls.dept.save()
        cls.dept.additional_managers.add(cls.addl)
        JobInfo.objects.create(employee=cls.head, reports_to=cls.director)
        JobInfo.objects.create(employee=cls.apm, reports_to=cls.head)
        JobInfo.objects.create(employee=cls.tl, reports_to=cls.apm)
        JobInfo.objects.create(employee=cls.agent, reports_to=cls.tl)

        cls.window = DedicatedNightShiftAssignment.objects.create(
            employee=cls.agent, start_date=WIN_START, end_date=WIN_END,
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )

    def _request(self, employee, day, shift_type=None):
        return ShiftChangeRequest.objects.create(
            employee=employee, date=day,
            original_shift_type=self.nig, requested_shift_type=shift_type or self.mor,
            requested_location="WFH", reason="Swap", applied_by=employee,
        )

    def _fresh(self, user):
        return User.objects.get(pk=user.pk)


class DecisionRuleTests(_Base):
    def test_only_the_department_head_decides_inside_the_window(self):
        req = self._request(self.agent, IN_WINDOW)
        self.assertTrue(can_act_on_shift_change(self._fresh(self.head), req))
        for user in (self.tl, self.apm, self.addl, self.hr, self.root, self.director):
            self.assertFalse(
                can_act_on_shift_change(self._fresh(user), self._request(self.agent, IN_WINDOW)),
                user.email,
            )

    def test_outside_the_window_the_leave_rule_still_applies(self):
        req = self._request(self.agent, AFTER_WINDOW)
        self.assertTrue(can_act_on_shift_change(self._fresh(self.tl), req))
        self.assertTrue(can_act_on_shift_change(self._fresh(self.hr), self._request(self.agent, AFTER_WINDOW)))

    def test_a_pending_or_cancelled_window_restricts_nothing(self):
        for status in (DedicatedNightShiftAssignment.Status.PENDING,
                       DedicatedNightShiftAssignment.Status.CANCELLED):
            self.window.status = status
            self.window.save()
            req = self._request(self.agent, IN_WINDOW)
            self.assertTrue(can_act_on_shift_change(self._fresh(self.tl), req), status)

    def test_the_heads_own_window_goes_to_their_line_manager(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.head, start_date=WIN_START, end_date=WIN_END,
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        req = self._request(self.head, IN_WINDOW)
        self.assertTrue(can_act_on_shift_change(self._fresh(self.director), req))
        for user in (self.head, self.hr, self.root):
            self.assertFalse(can_act_on_shift_change(self._fresh(user), self._request(self.head, IN_WINDOW)))

    def test_cancel_follows_the_rule_but_the_employee_may_withdraw(self):
        req = self._request(self.agent, IN_WINDOW)
        self.assertFalse(can_cancel_shift_change(self._fresh(self.tl), req))
        self.assertTrue(can_cancel_shift_change(self._fresh(self.agent), req))
        self.assertTrue(can_cancel_shift_change(self._fresh(self.head), req))


class ActionViewTests(_Base):
    def test_tl_approve_is_refused_and_head_approve_writes_the_shift(self):
        req = self._request(self.agent, IN_WINDOW)
        self.client.force_login(self.tl)
        resp = self.client.post(reverse("shift_change_approve", args=[req.pk]))
        self.assertEqual(resp.status_code, 403)

        self.client.force_login(self.head)
        self.client.post(reverse("shift_change_approve", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, ShiftChangeRequest.Status.APPROVED)
        self.assertEqual(Shift.objects.get(employee=self.agent, date=IN_WINDOW).shift_type, self.mor)

    def test_detail_page_names_the_approver(self):
        req = self._request(self.agent, IN_WINDOW)
        self.client.force_login(self.tl)
        resp = self.client.get(reverse("shift_change_detail", args=[req.pk]))
        self.assertContains(resp, "dedicated night")
        self.assertContains(resp, self.head.display_name)


class InboxTests(_Base):
    def test_bell_routes_window_requests_to_the_head_only(self):
        req = self._request(self.agent, IN_WINDOW)
        outside = self._request(self.agent, AFTER_WINDOW)
        tl_pks = set(pending_shift_change_qs(self._fresh(self.tl)).values_list("pk", flat=True))
        head_pks = set(pending_shift_change_qs(self._fresh(self.head)).values_list("pk", flat=True))
        self.assertEqual(tl_pks, {outside.pk})
        self.assertIn(req.pk, head_pks)
        self.assertNotIn(outside.pk, head_pks)  # not a direct report of the head

    def test_list_rows_show_actions_only_to_the_head(self):
        req = self._request(self.agent, IN_WINDOW)
        approve_url = reverse("shift_change_approve", args=[req.pk])

        self.client.force_login(self.head)
        resp = self.client.get(reverse("shift_change_list") + "?tab=pending")
        self.assertContains(resp, approve_url)

        self.client.force_login(self.tl)
        resp = self.client.get(reverse("shift_change_list") + "?tab=pending")
        self.assertNotContains(resp, approve_url)
        self.assertContains(resp, "Needs department head")


class GridTests(_Base):
    def _set(self, viewer, shift_type, day=IN_WINDOW):
        self.client.force_login(viewer)
        return self.client.post(
            reverse("roster_cell_update", args=[self.agent.pk, day.isoformat()]),
            {"shift_ref": f"type:{shift_type.pk}"},
        )

    def test_tl_may_set_night_custom_or_off_but_not_a_day_shift(self):
        for st in (self.nig, self.cust, self.off):
            self.assertEqual(self._set(self.tl, st).status_code, 200, st.name)
        resp = self._set(self.tl, self.mor)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("dedicated night shift", resp["HX-Error-Message"])
        self.assertEqual(Shift.objects.get(employee=self.agent, date=IN_WINDOW).shift_type, self.off)

    def test_head_may_set_a_day_shift_and_outside_the_window_is_untouched(self):
        self.assertEqual(self._set(self.head, self.mor).status_code, 200)
        self.assertEqual(self._set(self.tl, self.mor, day=AFTER_WINDOW).status_code, 200)

    def test_location_only_change_on_an_existing_day_shift_is_allowed(self):
        Shift.objects.create(employee=self.agent, date=IN_WINDOW, shift_type=self.mor)
        self.client.force_login(self.tl)
        resp = self.client.post(
            reverse("roster_cell_update", args=[self.agent.pk, IN_WINDOW.isoformat()]),
            {"location_ref": "WFH"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_pattern_refuses_a_new_day_slot_for_non_heads(self):
        def post(viewer):
            self.client.force_login(viewer)
            data = {
                "form-TOTAL_FORMS": "7", "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "7", "form-MAX_NUM_FORMS": "7",
            }
            for wd in range(7):
                data[f"form-{wd}-weekday"] = str(wd)
                data[f"form-{wd}-shift_ref"] = f"type:{(self.mor if wd == 0 else self.nig).pk}"
                data[f"form-{wd}-location"] = ""
            return self.client.post(reverse("roster_pattern", args=[self.agent.pk]), data)

        resp = post(self.tl)
        self.assertEqual(resp.status_code, 200)  # re-rendered with the error
        self.assertContains(resp, "dedicated night shift window")
        self.assertFalse(Roster.objects.filter(employee=self.agent).exists())

        resp = post(self.head)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Roster.objects.get(employee=self.agent, weekday=0).shift_type, self.mor)


class BulkPathTests(_Base):
    def _row(self, shift_type, day=IN_WINDOW):
        return ParsedRow(
            line_no=2, col_letter="C", employee=self.agent, employee_id="A1",
            date=day, shift_type=shift_type, shift_attribute=None,
            job_location=None, work_location=None,
        )

    def test_upload_skips_day_shifts_in_the_window_for_non_heads(self):
        report = apply_upload(
            self._fresh(self.tl),
            [self._row(self.mor), self._row(self.nig, IN_WINDOW + timedelta(days=1)),
             self._row(self.mor, AFTER_WINDOW)],
            {self.agent.pk},
        )
        self.assertEqual(len(report.skipped_night_window), 1)
        self.assertEqual(report.total_writes, 2)
        self.assertFalse(Shift.objects.filter(employee=self.agent, date=IN_WINDOW).exists())

        report = apply_upload(self._fresh(self.head), [self._row(self.mor)], {self.agent.pk})
        self.assertEqual(report.total_writes, 1)

    def test_machine_actor_is_never_the_approver(self):
        bot = _user("bot@example.com")
        agent = self._fresh(self.agent)
        self.assertFalse(night_window.may_set_shift(bot, agent, IN_WINDOW, self.mor))
        self.assertTrue(night_window.may_set_shift(bot, agent, IN_WINDOW, self.nig))
        self.assertTrue(night_window.may_set_shift(bot, agent, AFTER_WINDOW, self.mor))


class WindowApprovalWarningTests(_Base):
    def test_approving_over_day_shifts_warns_and_the_list_flags_them(self):
        Shift.objects.create(employee=self.tl, date=IN_WINDOW, shift_type=self.mor)
        Shift.objects.create(employee=self.tl, date=IN_WINDOW + timedelta(days=1), shift_type=self.nig)
        pending = DedicatedNightShiftAssignment.objects.create(
            employee=self.tl, start_date=WIN_START, end_date=WIN_END,
        )
        self.client.force_login(self.hr)
        resp = self.client.post(reverse("dedicated_night_shift_approve", args=[pending.pk]))
        self.assertEqual(resp.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.status, DedicatedNightShiftAssignment.Status.APPROVED)
        texts = [str(m) for m in get_messages(resp.wsgi_request)]
        warning = next(t for t in texts if "non-night working shift" in t)
        self.assertIn(f"{IN_WINDOW:%d %b} MOR", warning)
        self.assertIn(self.head.display_name, warning)

        resp = self.client.get(reverse("dedicated_night_shift_list") + "?tab=all")
        self.assertContains(resp, "1 non-night shift")


class ConflictHorizonTests(_Base):
    def test_days_already_worked_are_not_flagged(self):
        past = DedicatedNightShiftAssignment.objects.create(
            employee=self.tl, start_date=TODAY - timedelta(days=30), end_date=TODAY + timedelta(days=5),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        Shift.objects.create(employee=self.tl, date=TODAY - timedelta(days=3), shift_type=self.mor)
        Shift.objects.create(employee=self.tl, date=TODAY + timedelta(days=2), shift_type=self.mor)
        found = night_window.conflicts_by_assignment([past])[past.pk]
        self.assertEqual([c["date"] for c in found], [TODAY + timedelta(days=2)])
