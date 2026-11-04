"""Shift change requests — docs/SHIFT_CHANGE_REQUEST_PLAN.md §11.

Run with:
  python manage.py test roster.tests_shift_change --settings=core.settings_test
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.locations import get_wfh_worklocation, get_wfo_worklocation
from accounts.models import Department, Team
from core.dashboard import pending_approvals_summary
from employees.models import JobInfo
from leave.models import LeaveApplication
from roster import services
from roster.forms import ShiftChangeRequestForm
from roster.models import (
    Roster, Shift, ShiftAttribute, ShiftChangeLog, ShiftChangeRequest, ShiftType,
)


User = get_user_model()

TODAY = date.today()
NEXT_WEEK = TODAY + timedelta(days=7)


def _ensure_groups():
    for name in ("User Admins", "HR", "All Employees", "Senior Leadership"):
        Group.objects.get_or_create(name=name)


def _user(email, **extra):
    extra.setdefault("joining_date", TODAY - timedelta(days=400))
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        **extra,
    )


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.dept_a = Department.objects.create(name="Digital Advertising")
        cls.dept_b = Department.objects.create(name="Data Solutions and Due Diligence")
        cls.team_a = Team.objects.create(name="AdOps 1", department=cls.dept_a)

        cls.eve = ShiftType.objects.create(name="EVE", category=ShiftType.Category.WORK)
        cls.attr_global = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 1-10", is_active=True, sort_order=0,
        )
        cls.attr_dept_a = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 2-11", is_active=True, sort_order=1,
        )
        cls.attr_dept_a.departments.add(cls.dept_a)
        cls.attr_dept_b = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 3-11", is_active=True, sort_order=2,
        )
        cls.attr_dept_b.departments.add(cls.dept_b)
        cls.off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        cls.al = ShiftType.objects.create(name="AL", category=ShiftType.Category.LEAVE)
        cls.holiday_type = ShiftType.objects.create(name="HDL", category=ShiftType.Category.HOLIDAY)

        cls.manager = _user("mgr@example.com", department=cls.dept_a, team=cls.team_a)
        cls.report = _user("rep@example.com", department=cls.dept_a, team=cls.team_a)
        JobInfo.objects.create(employee=cls.report, reports_to=cls.manager)
        cls.other = _user("other@example.com", department=cls.dept_b)
        cls.hr = _user("hr@example.com", department=cls.dept_a)
        cls.hr.groups.add(Group.objects.get(name="HR"))

        # Weekly pattern for the report: EVE 1-10 Mon–Fri, OFF Sat/Sun.
        for wd in range(7):
            if wd < 5:
                Roster.objects.create(
                    employee=cls.report, weekday=wd,
                    shift_type=cls.eve, shift_attribute=cls.attr_global,
                )
            else:
                Roster.objects.create(employee=cls.report, weekday=wd, shift_type=cls.off)

        for attr in ("manager", "report", "other", "hr"):
            setattr(cls, attr, User.objects.get(pk=getattr(cls, attr).pk))

    # A weekday next week, so the pattern resolves to EVE 1-10.
    @staticmethod
    def weekday_next_week() -> date:
        d = NEXT_WEEK
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d

    def _file(self, employee, *, applied_by=None, day=None, shift_type=None, attr=None,
              location="WFH", status=ShiftChangeRequest.Status.PENDING, original=None):
        day = day or self.weekday_next_week()
        original = original or services.resolve_current_cell(employee, day)
        return ShiftChangeRequest.objects.create(
            employee=employee, date=day,
            original_shift_type=original.shift_type,
            original_shift_attribute=original.shift_attribute,
            original_was_concrete=original.is_concrete,
            original_location=original.location,
            requested_shift_type=shift_type or self.eve,
            requested_shift_attribute=attr if attr is not None else (
                self.attr_dept_a if (shift_type or self.eve) is self.eve else None
            ),
            requested_location=location,
            reason="Need the later start",
            applied_by=applied_by or employee,
            status=status,
        )

    def _post_apply(self, viewer, data, for_pk=None):
        self.client.force_login(viewer)
        url = reverse("shift_change_apply")
        if for_pk:
            url += f"?for={for_pk}"
            data = {**data, "for": str(for_pk)}
        return self.client.post(url, data)

    def _apply_data(self, **overrides):
        data = {
            "date": self.weekday_next_week().isoformat(),
            "shift_ref": f"attr:{self.attr_dept_a.pk}",
            "requested_location": "WFH",
            "reason": "Need the later start",
        }
        data.update(overrides)
        return data


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

class PermissionTests(_Base):
    def test_self_can_file(self):
        resp = self._post_apply(self.report, self._apply_data())
        req = ShiftChangeRequest.objects.get(employee=self.report)
        self.assertRedirects(resp, reverse("shift_change_detail", args=[req.pk]))
        self.assertEqual(req.applied_by, self.report)
        self.assertEqual(req.status, ShiftChangeRequest.Status.PENDING)

    def test_manager_can_file_for_report(self):
        resp = self._post_apply(self.manager, self._apply_data(), for_pk=self.report.pk)
        self.assertEqual(resp.status_code, 302)
        req = ShiftChangeRequest.objects.get(employee=self.report)
        self.assertEqual(req.applied_by, self.manager)

    def test_outsider_cannot_file_for_someone_else(self):
        resp = self._post_apply(self.other, self._apply_data(), for_pk=self.report.pk)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ShiftChangeRequest.objects.exists())

    def test_nobody_approves_their_own_unless_hr(self):
        req = self._file(self.report)
        self.client.force_login(self.report)
        resp = self.client.post(reverse("shift_change_approve", args=[req.pk]))
        self.assertEqual(resp.status_code, 403)
        req.refresh_from_db()
        self.assertTrue(req.is_pending)

        own = self._file(self.hr, day=self.weekday_next_week() + timedelta(days=1))
        self.client.force_login(self.hr)
        resp = self.client.post(reverse("shift_change_approve", args=[own.pk]))
        self.assertEqual(resp.status_code, 302)
        own.refresh_from_db()
        self.assertTrue(own.is_approved)

    def test_manager_approves_report_and_outsider_cannot(self):
        req = self._file(self.report)
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(reverse("shift_change_approve", args=[req.pk])).status_code, 403,
        )
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("shift_change_approve", args=[req.pk]))
        self.assertRedirects(resp, reverse("shift_change_detail", args=[req.pk]))
        req.refresh_from_db()
        self.assertTrue(req.is_approved)
        self.assertEqual(req.processed_by, self.manager)

    def test_decline_records_note(self):
        req = self._file(self.report)
        self.client.force_login(self.manager)
        self.client.post(reverse("shift_change_decline", args=[req.pk]), {"process_note": "Coverage"})
        req.refresh_from_db()
        self.assertEqual(req.status, ShiftChangeRequest.Status.DECLINED)
        self.assertEqual(req.process_note, "Coverage")
        self.assertFalse(Shift.objects.filter(employee=self.report, date=req.date).exists())

    def test_applicant_cancels_pending_but_not_approved(self):
        req = self._file(self.report)
        self.client.force_login(self.report)
        resp = self.client.post(reverse("shift_change_cancel", args=[req.pk]))
        self.assertEqual(resp.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, ShiftChangeRequest.Status.CANCELLED)

        approved = self._file(
            self.report, day=self.weekday_next_week() + timedelta(days=1),
            status=ShiftChangeRequest.Status.APPROVED,
        )
        resp = self.client.post(reverse("shift_change_cancel", args=[approved.pk]))
        self.assertEqual(resp.status_code, 403)
        # Nor can the manager / HR undo an approval from the request.
        self.client.force_login(self.hr)
        self.assertEqual(
            self.client.post(reverse("shift_change_cancel", args=[approved.pk])).status_code, 403,
        )

    def test_detail_visibility(self):
        req = self._file(self.report)
        for viewer, expected in ((self.report, 200), (self.manager, 200), (self.hr, 200), (self.other, 404)):
            self.client.force_login(viewer)
            self.assertEqual(
                self.client.get(reverse("shift_change_detail", args=[req.pk])).status_code,
                expected, viewer.email,
            )


# ---------------------------------------------------------------------------
# Dropdown scoping + filing guards (form)
# ---------------------------------------------------------------------------

class FormTests(_Base):
    def _choices(self, employee):
        form = ShiftChangeRequestForm(employee=employee)
        values = set()
        for value, label in form.fields["shift_ref"].choices:
            if isinstance(label, (list, tuple)):
                values |= {v for v, _ in label}
            else:
                values.add(value)
        return values

    def test_dropdown_is_department_scoped_and_excludes_leave(self):
        values = self._choices(self.report)
        self.assertIn(f"attr:{self.attr_global.pk}", values)
        self.assertIn(f"attr:{self.attr_dept_a.pk}", values)
        self.assertNotIn(f"attr:{self.attr_dept_b.pk}", values)
        self.assertIn(f"type:{self.off.pk}", values)
        self.assertNotIn(f"type:{self.al.pk}", values)
        self.assertNotIn(f"type:{self.holiday_type.pk}", values)

    def test_tampered_out_of_department_attribute_is_rejected(self):
        resp = self._post_apply(self.report, self._apply_data(shift_ref=f"attr:{self.attr_dept_b.pk}"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("shift_ref", resp.context["form"].errors)

    def test_tampered_location_is_rejected(self):
        resp = self._post_apply(self.report, self._apply_data(requested_location="MARS"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("requested_location", resp.context["form"].errors)

    def test_leave_shift_on_date_is_refused(self):
        day = self.weekday_next_week()
        Shift.objects.create(employee=self.report, date=day, shift_type=self.al)
        resp = self._post_apply(self.report, self._apply_data())
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("date", resp.context["form"].errors)
        self.assertIn("Cancel the leave", str(resp.context["form"].errors["date"]))

    def test_pending_leave_application_on_date_is_refused(self):
        day = self.weekday_next_week()
        LeaveApplication.objects.create(
            employee=self.report, leave_type=self.al, start_date=day, end_date=day,
            applied_by=self.report,
        )
        resp = self._post_apply(self.report, self._apply_data())
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("date", resp.context["form"].errors)

    def test_duplicate_pending_is_refused(self):
        self._file(self.report)
        resp = self._post_apply(self.report, self._apply_data(shift_ref=f"type:{self.off.pk}"))
        self.assertEqual(ShiftChangeRequest.objects.count(), 1)
        self.assertIn("already pending", str(resp.context["form"].errors["date"]))

    def test_past_date_refused_for_self_allowed_for_hr(self):
        # Pick a past weekday so the pattern resolves and only the window rule fires.
        past = TODAY - timedelta(days=3)
        while past.weekday() >= 5:
            past -= timedelta(days=1)
        resp = self._post_apply(self.report, self._apply_data(date=past.isoformat()))
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("today or a future date", str(resp.context["form"].errors["date"]))

        resp = self._post_apply(self.hr, self._apply_data(date=past.isoformat()), for_pk=self.report.pk)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ShiftChangeRequest.objects.get().date, past)

    def test_before_joining_date_is_refused(self):
        hr_day = TODAY + timedelta(days=3)
        self.report.joining_date = TODAY + timedelta(days=10)
        self.report.save(update_fields=["joining_date"])
        resp = self._post_apply(self.hr, self._apply_data(date=hr_day.isoformat()), for_pk=self.report.pk)
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("joined on", str(resp.context["form"].errors["date"]))

    def test_noop_request_is_refused(self):
        # Pattern says EVE 1-10; the report's contract location is unset, so
        # resolved location is "" — a request for EVE 1-10 with a location
        # IS a change. Give them a WFH contract first so WFH becomes a no-op.
        self.report.work_location = get_wfh_worklocation()
        self.report.save(update_fields=["work_location"])
        resp = self._post_apply(
            self.report, self._apply_data(shift_ref=f"attr:{self.attr_global.pk}", requested_location="WFH"),
        )
        self.assertFalse(ShiftChangeRequest.objects.exists())
        self.assertIn("change at least one", str(resp.context["form"].non_field_errors()))

    def test_shift_only_and_location_only_are_valid(self):
        self.report.work_location = get_wfh_worklocation()
        self.report.save(update_fields=["work_location"])
        day = self.weekday_next_week()
        # Shift only (location stays WFH).
        resp = self._post_apply(self.report, self._apply_data(requested_location="WFH"))
        self.assertEqual(resp.status_code, 302)
        req = ShiftChangeRequest.objects.get()
        self.assertTrue(req.shift_changed)
        self.assertFalse(req.location_changed)
        self.assertEqual(req.original_shift_attribute, self.attr_global)
        self.assertFalse(req.original_was_concrete)
        self.assertEqual(req.original_location, "WFH")
        # Location only, another day.
        day2 = day + timedelta(days=1)
        resp = self._post_apply(
            self.report, self._apply_data(
                date=day2.isoformat(), shift_ref=f"attr:{self.attr_global.pk}", requested_location="MHK",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        req2 = ShiftChangeRequest.objects.get(date=day2)
        self.assertFalse(req2.shift_changed)
        self.assertTrue(req2.location_changed)

    def test_snapshot_prefers_existing_shift_row(self):
        day = self.weekday_next_week()
        Shift.objects.create(
            employee=self.report, date=day, shift_type=self.off,
            work_location=get_wfh_worklocation(),
        )
        self._post_apply(self.report, self._apply_data())
        req = ShiftChangeRequest.objects.get()
        self.assertEqual(req.original_shift_type, self.off)
        self.assertTrue(req.original_was_concrete)
        self.assertEqual(req.original_location, "WFH")


# ---------------------------------------------------------------------------
# Approval effects
# ---------------------------------------------------------------------------

class ApprovalTests(_Base):
    def _approve(self, req, by=None):
        self.client.force_login(by or self.manager)
        return self.client.post(reverse("shift_change_approve", args=[req.pk]))

    def test_creates_shift_row_when_only_pattern_existed(self):
        req = self._file(self.report, location="WFH")
        self.assertFalse(Shift.objects.filter(employee=self.report, date=req.date).exists())
        self._approve(req)
        shift = Shift.objects.get(employee=self.report, date=req.date)
        self.assertEqual(shift.shift_type, self.eve)
        self.assertEqual(shift.shift_attribute, self.attr_dept_a)
        self.assertIsNone(shift.job_location)
        self.assertEqual(shift.work_location, get_wfh_worklocation())
        self.assertEqual(shift.added_by, self.manager)
        self.assertEqual(shift.updated_by, self.manager)
        self.assertEqual(shift.note, f"Shift change #{req.pk}")
        log = ShiftChangeLog.objects.get(employee=self.report, date=req.date)
        self.assertEqual(log.action_type, ShiftChangeLog.ActionType.CREATED)
        self.assertEqual(log.changed_by, self.manager)
        self.assertEqual(log.new_work_location, get_wfh_worklocation())

    def test_updates_existing_row_and_office_location_maps_to_canonical(self):
        day = self.weekday_next_week()
        Shift.objects.create(
            employee=self.report, date=day, shift_type=self.eve, shift_attribute=self.attr_global,
            work_location=get_wfh_worklocation(), added_by=self.hr,
        )
        req = self._file(self.report, day=day, location="MHK")
        self._approve(req)
        shift = Shift.objects.get(employee=self.report, date=day)
        self.assertEqual(shift.shift_attribute, self.attr_dept_a)
        self.assertEqual(shift.work_location, get_wfo_worklocation())
        self.assertIsNotNone(shift.job_location)
        self.assertEqual(shift.location_badge, "MHK")
        self.assertEqual(shift.added_by, self.hr)  # untouched on update
        self.assertEqual(shift.updated_by, self.manager)
        log = ShiftChangeLog.objects.filter(
            employee=self.report, date=day, action_type=ShiftChangeLog.ActionType.UPDATED,
        ).get()
        self.assertEqual(log.previous_shift_attribute, self.attr_global)
        self.assertEqual(log.new_shift_attribute, self.attr_dept_a)
        self.assertEqual(log.previous_work_location, get_wfh_worklocation())
        self.assertEqual(log.new_work_location, get_wfo_worklocation())

    def test_location_only_change_keeps_shift_pair(self):
        day = self.weekday_next_week()
        req = self._file(self.report, day=day, attr=self.attr_global, location="MBD")
        self._approve(req)
        shift = Shift.objects.get(employee=self.report, date=day)
        self.assertEqual(shift.shift_attribute, self.attr_global)
        self.assertEqual(shift.location_badge, "MBD")

    def test_off_day_request_writes_bare_type(self):
        req = self._file(self.report, shift_type=self.off, attr=None, location="MHK")
        self._approve(req)
        shift = Shift.objects.get(employee=self.report, date=req.date)
        self.assertEqual(shift.shift_type, self.off)
        self.assertIsNone(shift.shift_attribute)

    def test_leave_approved_after_filing_blocks_approval(self):
        req = self._file(self.report)
        LeaveApplication.objects.create(
            employee=self.report, leave_type=self.al, start_date=req.date, end_date=req.date,
            applied_by=self.report, status=LeaveApplication.Status.APPROVED,
        )
        resp = self._approve(req)
        self.assertRedirects(resp, reverse("shift_change_detail", args=[req.pk]))
        req.refresh_from_db()
        self.assertTrue(req.is_pending)
        self.assertFalse(Shift.objects.filter(employee=self.report, date=req.date).exists())

    def test_attribute_rescoped_after_filing_blocks_approval(self):
        req = self._file(self.report)  # EVE 2-11, dept A
        self.attr_dept_a.departments.set([self.dept_b])
        self._approve(req)
        req.refresh_from_db()
        self.assertTrue(req.is_pending)
        self.assertFalse(Shift.objects.filter(employee=self.report, date=req.date).exists())

    def test_apply_service_rejects_leave_category_at_model_level(self):
        req = ShiftChangeRequest(
            employee=self.report, date=self.weekday_next_week(),
            requested_shift_type=self.al, requested_location="WFH",
            reason="x", applied_by=self.report,
        )
        with self.assertRaises(ValidationError):
            req.save()

    def test_htmx_approve_returns_row_fragment(self):
        req = self._file(self.report)
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("shift_change_approve", args=[req.pk]), HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f'id="shift-change-row-{req.pk}"', body)
        self.assertIn("hx-swap-oob", body)
        self.assertIn("Approved", body)


# ---------------------------------------------------------------------------
# List tabs, dashboard, preview endpoint, roster grid
# ---------------------------------------------------------------------------

class ListAndIntegrationTests(_Base):
    def test_tabs_follow_leave_visibility(self):
        mine = self._file(self.report)
        theirs = self._file(self.other, day=self.weekday_next_week() + timedelta(days=1))

        self.client.force_login(self.report)
        resp = self.client.get(reverse("shift_change_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["show_pending_tab"])
        self.assertEqual([r.pk for r in resp.context["rows"]], [mine.pk])

        self.client.force_login(self.manager)
        resp = self.client.get(reverse("shift_change_list"), {"tab": "pending"})
        self.assertTrue(resp.context["show_pending_tab"])
        self.assertEqual([r.pk for r in resp.context["rows"]], [mine.pk])
        self.assertEqual(resp.context["pending_count"], 1)

        self.client.force_login(self.hr)
        resp = self.client.get(reverse("shift_change_list"), {"tab": "all"})
        self.assertEqual({r.pk for r in resp.context["rows"]}, {mine.pk, theirs.pk})

    def test_search_filter(self):
        self._file(self.report)
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("shift_change_list"), {"tab": "all", "q": "nobody"})
        self.assertEqual(resp.context["rows"], [])
        resp = self.client.get(reverse("shift_change_list"), {"tab": "all", "q": "rep"})
        self.assertEqual(len(resp.context["rows"]), 1)

    def test_dashboard_pending_summary_counts_shift_changes(self):
        self._file(self.report)
        entries = {e["key"]: e for e in pending_approvals_summary(self.manager)}
        self.assertIn("shift_change", entries)
        self.assertEqual(entries["shift_change"]["count"], 1)
        self.assertIn("tab=pending", entries["shift_change"]["url"])

    def test_current_endpoint_reports_pattern_and_warnings(self):
        self.client.force_login(self.report)
        day = self.weekday_next_week()
        resp = self.client.post(reverse("shift_change_current"), {"date": day.isoformat()})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["current_label"], "EVE 1-10")
        self.assertEqual(data["current_shift_ref"], f"attr:{self.attr_global.pk}")
        self.assertFalse(data["is_concrete"])
        self.assertEqual(data["warnings"], [])

        self._file(self.report, day=day)
        data = self.client.post(reverse("shift_change_current"), {"date": day.isoformat()}).json()
        self.assertTrue(any("already pending" in w for w in data["warnings"]))

        # On-behalf-of is gated the same way as the apply page.
        self.client.force_login(self.other)
        resp = self.client.post(
            reverse("shift_change_current"), {"date": day.isoformat(), "for": str(self.report.pk)},
        )
        self.assertEqual(resp.status_code, 403)

    def test_grid_shows_request_link_for_own_readonly_row_and_pending_badge(self):
        day = self.weekday_next_week()
        self.client.force_login(self.report)
        resp = self.client.get(
            reverse("roster_week"), {"from": day.isoformat(), "to": day.isoformat()},
        )
        body = resp.content.decode()
        cell = re.search(rf'id="cell-{self.report.pk}-{day:%Y%m%d}".*?</td>', body, re.DOTALL).group(0)
        self.assertNotIn('name="shift_ref"', cell)
        self.assertIn("Request change", cell)
        self.assertIn(f"{reverse('shift_change_apply')}?date={day.isoformat()}", cell)

        req = self._file(self.report, day=day)
        body = self.client.get(
            reverse("roster_week"), {"from": day.isoformat(), "to": day.isoformat()},
        ).content.decode()
        cell = re.search(rf'id="cell-{self.report.pk}-{day:%Y%m%d}".*?</td>', body, re.DOTALL).group(0)
        self.assertIn("Change pending", cell)
        self.assertIn(reverse("shift_change_detail", args=[req.pk]), cell)
        self.assertNotIn("Request change", cell)

        # The manager still gets the editable dropdown on the same row.
        self.client.force_login(self.manager)
        body = self.client.get(
            reverse("roster_week"), {"from": day.isoformat(), "to": day.isoformat()},
        ).content.decode()
        cell = re.search(rf'id="cell-{self.report.pk}-{day:%Y%m%d}".*?</td>', body, re.DOTALL).group(0)
        self.assertIn('name="shift_ref"', cell)
        self.assertIn("Change pending", cell)

    def test_apply_page_renders_for_self_and_prefills_date(self):
        self.client.force_login(self.report)
        day = self.weekday_next_week()
        resp = self.client.get(reverse("shift_change_apply"), {"date": day.isoformat()})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"].initial.get("date"), day.isoformat())
        self.assertFalse(resp.context["applying_for_other"])
