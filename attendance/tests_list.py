"""Tests for /attendance/ (Timesheet) — the three-mode list.

Single-employee date-range mode (the q/department/team search resolving to
exactly one visible employee, plus `?from=`/`?to=`, and the bare default),
department/team date-range mode (`from != to` with a department or team
selected — one row per employee per date), and the historic team mode (one
row per employee for one date), plus the legacy `?date=` compatibility shim
and the `?export=xlsx` download shared by all three.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, Team
from employees.models import JobInfo
from attendance.models import AttendanceCorrection, AttendancePunch
from attendance.views import SINGLE_EMP_MAX_RANGE_DAYS, TEAM_MAX_RANGE_DAYS
from roster.models import Roster, Shift, ShiftType


User = get_user_model()


def _ensure_groups():
    for name in ("User Admins", "HR", "All Employees"):
        Group.objects.get_or_create(name=name)


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        **extra,
    )


def _multi_dept_manager(test):
    """A manager with one report in each of two departments: no single
    effective department, and no org-wide (`can_see_all`) access."""
    other = Department.objects.create(name="Multi-dept other")
    boss = _user("boss@example.com", employee_id="8000", department=test.dept)
    for email, eid, dept in (
        ("r1@example.com", "8001", test.dept), ("r2@example.com", "8002", other),
    ):
        JobInfo.objects.create(employee=_user(email, employee_id=eid, department=dept), reports_to=boss)
    return boss


def _aware(d: date, hour: int, minute: int = 0):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour, minute))


class _Base(TestCase):

    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.dept = Department.objects.create(name="Digital Advertising")

        cls.hr = _user("hr@example.com", employee_id="1000", department=cls.dept)
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.emp = _user("emp@example.com", employee_id="2001", department=cls.dept)
        cls.stranger = _user("stranger@example.com", employee_id="9999", department=cls.dept)

        cls.work = ShiftType.objects.create(name="GEN", category=ShiftType.Category.WORK)
        cls.off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)

        # Mon-Fri working, Sat/Sun off — the weekly pattern fallback.
        for weekday in range(7):
            Roster.objects.create(
                employee=cls.emp, weekday=weekday,
                shift_type=cls.work if weekday < 5 else cls.off,
            )

        # A fixed, known week: 2026-03-02 is a Monday.
        cls.monday = date(2026, 3, 2)
        cls.url = reverse("attendance_list")

    def _get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(self.url, params)


class DefaultsAndModeTests(_Base):

    def test_bare_url_defaults_to_own_month_to_date(self):
        resp = self._get(self.emp)
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        today = date.today()
        yesterday = today - timedelta(days=1)
        self.assertTrue(ctx["range_mode"])
        self.assertEqual(ctx["range_employee"], self.emp)
        # The synthesized default is visibly filled into the search box,
        # not just resolved invisibly.
        self.assertEqual(ctx["q"], "2001")
        self.assertEqual(ctx["date_to"], yesterday)
        # First of the month, unless that's already past yesterday (the 1st),
        # in which case the inverted-range fallback clamps it.
        expected_from = today.replace(day=1)
        self.assertEqual(ctx["date_from"], min(expected_from, yesterday))

    def test_search_matching_several_people_stays_in_team_mode(self):
        # All three fixture users' emails end "@example.com" — a search that
        # matches more than one person doesn't resolve an employee.
        resp = self._get(self.hr, q="example.com")
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(resp.context["date_to"], date.today() - timedelta(days=1))

    def test_search_matching_one_person_switches_to_range_mode(self):
        resp = self._get(
            self.hr, q="2001",
            **{"from": self.monday.isoformat(), "to": (self.monday + timedelta(days=4)).isoformat()},
        )
        self.assertTrue(resp.context["range_mode"])
        self.assertEqual(resp.context["range_employee"], self.emp)
        # Identity is shown above the table; rows are dates, not people.
        self.assertContains(resp, "Viewing:")
        self.assertContains(resp, self.emp.display_name)
        self.assertContains(resp, "<th class=\"text-left\">Date</th>", html=False)
        self.assertNotContains(resp, "<th class=\"text-left\">Emp ID</th>", html=False)

    def test_department_narrowing_to_one_person_stays_team_mode(self):
        # Mode-switching is exact-ID-only (see test_partial_id_narrows_team_
        # without_switching_modes below) — department/team narrowing the
        # visible scope to one person is still just a narrowed team table.
        solo_dept = Department.objects.create(name="Solo Dept")
        solo = _user("solo@example.com", employee_id="3001", department=solo_dept)
        resp = self._get(self.hr, department=str(solo_dept.pk))
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(
            [e.employee_id for e in resp.context["employees"]], ["3001"],
        )

    def test_partial_id_matches_nothing_and_does_not_switch_modes(self):
        # Employee-ID search is exact, not a substring: "200" must not find
        # "2001". Deliberate since 017d52b (2026-09-13, "Make employee ID
        # search exact-match") — employee_id__icontains had let "539" match
        # "1539"/"2539" everywhere the shared q-search runs.
        #
        # This test predates that change and kept asserting the old
        # substring behaviour, so it has been failing on main since; the
        # mode-switching half of it is the part still worth holding onto —
        # a query that matches nothing must not flip the page into
        # single-employee range mode either.
        resp = self._get(self.hr, q="200")
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual([e.employee_id for e in resp.context["employees"]], [])

    def test_employee_can_pull_up_their_own_range(self):
        resp = self._get(self.emp, q="2001")
        self.assertTrue(resp.context["range_mode"])
        self.assertEqual(resp.context["range_employee"], self.emp)

    def test_page_param_alone_does_not_leave_self_mode(self):
        resp = self._get(self.emp, page="1")
        self.assertTrue(resp.context["range_mode"])


class TeamModeRegressionTests(_Base):

    def test_single_date_team_mode_unchanged(self):
        target = self.monday
        AttendancePunch.objects.create(
            employee=self.emp, date=target,
            check_in=_aware(target, 9), check_out=_aware(target, 18),
            time_in_office=timedelta(hours=9), office_bucket="MHK",
        )
        resp = self._get(self.hr, **{"from": target.isoformat(), "to": target.isoformat()})
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(resp.context["selected_date"], target)
        rows = {e.employee_id: e for e in resp.context["employees"]}
        self.assertIn("2001", rows)
        self.assertIn("1000", rows)
        self.assertEqual(rows["2001"].punch.office_bucket, "MHK")
        self.assertEqual(rows["2001"].shift_label, "GEN")

    def test_legacy_date_param_still_works(self):
        target = self.monday
        resp = self._get(self.hr, date=target.isoformat())
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(resp.context["date_from"], target)
        self.assertEqual(resp.context["date_to"], target)
        self.assertEqual(resp.context["selected_date"], target)

    def test_drill_in_link_renders_search_querystring(self):
        resp = self._get(self.hr, date=self.monday.isoformat())
        self.assertContains(resp, f"{self.url}?q=2001")

    def test_team_mode_unaccounted_flag_unchanged(self):
        # Monday, WORK by pattern, nobody punched.
        resp = self._get(self.hr, date=self.monday.isoformat())
        rows = {e.employee_id: e for e in resp.context["employees"]}
        self.assertTrue(rows["2001"].is_unaccounted)
        # `stranger` has never logged in, so roster.signals never seeded them
        # a default pattern — unscheduled, therefore not flagged.
        self.assertEqual(rows["9999"].shift_label, "")
        self.assertFalse(rows["9999"].is_unaccounted)


class EmploymentWindowGuardTests(_Base):
    """The Roster weekly pattern is keyed only by weekday, so on its own it
    would "resolve" a shift for a date outside the employee's employment
    window. Regression coverage for the bug where /attendance/ showed
    phantom pattern-based shifts before an employee's joining_date (and,
    symmetrically, after their last_working_date) while /roster/ never did,
    since roster only ever reads materialised Shift rows."""

    def test_range_mode_blanks_pattern_before_joining_date_and_after_lwd(self):
        start, end = self.monday, self.monday + timedelta(days=6)
        # Joined Wednesday, left Friday of the same week — pattern should
        # only resolve Wed/Thu/Fri.
        self.emp.joining_date = self.monday + timedelta(days=2)
        self.emp.last_working_date = self.monday + timedelta(days=4)
        self.emp.save()

        resp = self._get(
            self.hr, q="2001",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = resp.context["rows"]
        self.assertEqual(
            [r.shift_label for r in rows],
            ["", "", "GEN", "GEN", "GEN", "", ""],
        )
        # No phantom "unaccounted" flags outside the employment window, but
        # Wed/Thu/Fri are genuinely WORK-scheduled and unpunched.
        self.assertEqual(
            [r.is_unaccounted for r in rows],
            [False, False, True, True, True, False, False],
        )

    def test_team_mode_blanks_pattern_before_joining_date(self):
        target = self.monday
        self.emp.joining_date = self.monday + timedelta(days=1)
        self.emp.save()

        resp = self._get(self.hr, date=target.isoformat())
        rows = {e.employee_id: e for e in resp.context["employees"]}
        self.assertEqual(rows["2001"].shift_label, "")
        self.assertFalse(rows["2001"].is_unaccounted)

    def test_team_mode_still_resolves_pattern_within_employment_window(self):
        target = self.monday
        self.emp.joining_date = self.monday - timedelta(days=30)
        self.emp.save()

        resp = self._get(self.hr, date=target.isoformat())
        rows = {e.employee_id: e for e in resp.context["employees"]}
        self.assertEqual(rows["2001"].shift_label, "GEN")
        self.assertTrue(rows["2001"].is_unaccounted)


class RangeModeRowTests(_Base):

    def test_one_row_per_date_with_resolved_shift_and_punch(self):
        start, end = self.monday, self.monday + timedelta(days=6)
        # A concrete Shift row overrides the weekly pattern on the Saturday.
        Shift.objects.create(
            employee=self.emp, date=self.monday + timedelta(days=5), shift_type=self.work,
        )
        punch_day = self.monday + timedelta(days=1)
        AttendancePunch.objects.create(
            employee=self.emp, date=punch_day,
            check_in=_aware(punch_day, 10), check_out=_aware(punch_day, 19),
            time_in_office=timedelta(hours=9), office_bucket="MBD",
        )

        resp = self._get(
            self.hr, q="2001",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = resp.context["rows"]
        self.assertEqual(len(rows), 7)
        self.assertEqual([r.date for r in rows], [start + timedelta(days=i) for i in range(7)])
        # Mon-Fri from the pattern, Sat from the concrete Shift, Sun off.
        self.assertEqual([r.shift_label for r in rows], ["GEN"] * 5 + ["GEN", "OFF"])
        self.assertEqual(rows[1].punch.office_bucket, "MBD")
        self.assertIsNone(rows[0].punch)

    def test_unaccounted_flagged_per_date(self):
        start, end = self.monday, self.monday + timedelta(days=6)
        punch_day = self.monday + timedelta(days=1)
        AttendancePunch.objects.create(
            employee=self.emp, date=punch_day,
            check_in=_aware(punch_day, 10), check_out=_aware(punch_day, 19),
        )
        resp = self._get(
            self.hr, q="2001",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = resp.context["rows"]
        # Mon, Wed, Thu, Fri are WORK with no punch; Tue has one; Sat/Sun off.
        self.assertEqual(
            [r.is_unaccounted for r in rows],
            [True, False, True, True, True, False, False],
        )
        self.assertEqual(resp.context["unaccounted_count"], 4)

    def test_flagged_filter_narrows_to_unaccounted_rows(self):
        start, end = self.monday, self.monday + timedelta(days=6)
        punch_day = self.monday + timedelta(days=1)
        AttendancePunch.objects.create(
            employee=self.emp, date=punch_day,
            check_in=_aware(punch_day, 10), check_out=_aware(punch_day, 19),
        )
        resp = self._get(
            self.hr, q="2001", flagged="1",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = resp.context["rows"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r.is_unaccounted for r in rows))
        # Count still reflects the whole range, not the filtered page.
        self.assertEqual(resp.context["unaccounted_count"], 4)

    def test_correction_states_shown_per_date_row(self):
        start, end = self.monday, self.monday + timedelta(days=2)
        pending_day, corrected_day = start, start + timedelta(days=1)
        AttendanceCorrection.objects.create(
            employee=self.emp, date=pending_day,
            requested_check_in=_aware(pending_day, 9),
            requested_check_out=_aware(pending_day, 18),
            reason="forgot to punch", requested_by=self.emp,
        )
        punch = AttendancePunch.objects.create(
            employee=self.emp, date=corrected_day,
            check_in=_aware(corrected_day, 9), check_out=_aware(corrected_day, 18),
            source=AttendancePunch.Source.MANUAL_CORRECTION,
        )
        approved = AttendanceCorrection.objects.create(
            employee=self.emp, date=corrected_day, punch=punch,
            requested_check_in=_aware(corrected_day, 9),
            requested_check_out=_aware(corrected_day, 18),
            reason="badge failed", requested_by=self.emp,
            status=AttendanceCorrection.Status.APPROVED,
            processed_by=self.hr, processed_at=timezone.now(),
        )

        resp = self._get(
            self.hr, q="2001",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        rows = resp.context["rows"]
        self.assertTrue(rows[0].has_pending_correction)
        self.assertFalse(rows[0].is_corrected)
        self.assertFalse(rows[1].has_pending_correction)
        self.assertTrue(rows[1].is_corrected)
        self.assertEqual(rows[1].correction, approved)
        self.assertFalse(rows[2].has_pending_correction)
        self.assertFalse(rows[2].is_corrected)

    def test_can_request_correction_reflects_permission(self):
        start = self.monday
        params = {"q": "2001", "from": start.isoformat(), "to": start.isoformat()}
        resp = self._get(self.hr, **params)
        self.assertTrue(resp.context["rows"][0].can_request_correction)

        resp = self._get(self.emp, **params)
        self.assertTrue(resp.context["rows"][0].can_request_correction)


class RangeValidationTests(_Base):

    def test_range_without_employee_errors_and_falls_back_to_to_date(self):
        # A manager whose downline spans two departments has no single
        # effective department — and, unlike HR, no org-wide range.
        start, end = self.monday, self.monday + timedelta(days=4)
        resp = self._get(_multi_dept_manager(self), **{"from": start.isoformat(), "to": end.isoformat()})
        self.assertFalse(resp.context["range_mode"])
        self.assertTrue(resp.context["date_errors"])
        self.assertIn("Pick a department or team", resp.context["date_errors"][0])
        self.assertFalse(resp.context["group_range_mode"])
        self.assertEqual(resp.context["selected_date"], end)
        self.assertEqual(resp.context["date_from"], end)

    def test_inverted_range_errors_and_clamps(self):
        start, end = self.monday + timedelta(days=4), self.monday
        resp = self._get(
            self.hr, q="2001",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        self.assertIn(
            "Date from must be on or before Date to.", resp.context["date_errors"],
        )
        self.assertEqual(resp.context["date_from"], end)
        self.assertEqual(resp.context["date_to"], end)

    def test_malformed_date_reports_and_falls_back(self):
        resp = self._get(self.hr, q="2001", **{"from": "not-a-date"})
        self.assertTrue(any("isn't a valid date" in e for e in resp.context["date_errors"]))
        self.assertTrue(resp.context["range_mode"])

    def test_search_outside_scope_finds_nothing_without_leaking(self):
        # A plain employee's visible scope is themselves only, so searching
        # for someone else's ID resolves to nobody in range mode — team
        # mode with an empty-for-them table, never the stranger's data.
        resp = self._get(self.emp, q="9999")
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(list(resp.context["employees"]), [])

    def test_unmatched_search_falls_back_to_team_mode(self):
        resp = self._get(self.hr, q="does-not-exist")
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(list(resp.context["employees"]), [])

    def test_range_is_capped(self):
        start = date(2026, 1, 1)
        resp = self._get(
            self.hr, q="2001",
            **{"from": start.isoformat(), "to": "2027-12-31"},
        )
        self.assertEqual(resp.context["date_from"], start)
        self.assertEqual(
            resp.context["date_to"],
            start + timedelta(days=SINGLE_EMP_MAX_RANGE_DAYS - 1),
        )
        self.assertEqual(resp.context["paginator"].count, SINGLE_EMP_MAX_RANGE_DAYS)


class RangePaginationTests(_Base):

    def test_date_rows_paginate(self):
        start = self.monday
        end = start + timedelta(days=39)  # 40 days
        params = {"q": "2001", "from": start.isoformat(), "to": end.isoformat()}

        resp = self._get(self.hr, **params)
        self.assertEqual(resp.context["paginator"].count, 40)
        self.assertEqual(len(resp.context["rows"]), 25)
        self.assertTrue(resp.context["is_paginated"])
        self.assertEqual(resp.context["current_page_size"], 25)

        resp = self._get(self.hr, page="2", **params)
        self.assertEqual(len(resp.context["rows"]), 15)
        self.assertEqual(resp.context["rows"][0].date, start + timedelta(days=25))

    def test_per_page_honoured(self):
        start = self.monday
        end = start + timedelta(days=39)
        resp = self._get(
            self.hr, q="2001", per_page="50",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        self.assertEqual(len(resp.context["rows"]), 40)
        self.assertEqual(resp.context["current_page_size"], 50)


def _load_xlsx(resp):
    from io import BytesIO

    from openpyxl import load_workbook

    return load_workbook(BytesIO(resp.content)).active


class GroupRangeModeTests(_Base):
    """Department/team date-range mode: one row per employee per date."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.team = Team.objects.create(name="Bench", department=cls.dept)
        cls.other_dept = Department.objects.create(name="Finance")
        cls.outsider = _user("fin@example.com", employee_id="3001", department=cls.other_dept)
        cls.emp.team = cls.team
        cls.emp.save(update_fields=["team"])

    def _range(self, user, days=4, **params):
        start, end = self.monday, self.monday + timedelta(days=days)
        return self._get(user, **{"from": start.isoformat(), "to": end.isoformat(), **params})

    def test_department_unlocks_range_with_employee_then_date_rows(self):
        resp = self._range(self.hr, department=self.dept.pk)
        self.assertTrue(resp.context["group_range_mode"])
        self.assertFalse(resp.context["range_mode"])
        self.assertFalse(resp.context["date_errors"])
        rows = resp.context["rows"]
        # Three people in the department × 5 dates, employee-major.
        self.assertEqual(resp.context["paginator"].count, 15)
        self.assertEqual(len(rows), 15)
        self.assertEqual(
            [r.employee.pk for r in rows],
            [self.emp.pk] * 5 + [self.hr.pk] * 5 + [self.stranger.pk] * 5,
        )
        self.assertEqual(
            [r.date for r in rows[:5]], [self.monday + timedelta(days=i) for i in range(5)],
        )
        # `emp` has an explicit Mon-Fri pattern; `hr` picks up the default
        # pattern roster.signals seeds on login; `stranger` never logs in and
        # has no pattern at all.
        self.assertEqual([r.shift_label for r in rows[:5]], ["GEN"] * 5)
        self.assertEqual([r.shift_label for r in rows[10:]], [""] * 5)
        self.assertNotIn(self.outsider.pk, {r.employee.pk for r in rows})
        self.assertEqual(resp.context["scope_label"], "Digital Advertising")

    def test_team_alone_backfills_department_and_unlocks_range(self):
        resp = self._range(self.hr, team=self.team.pk)
        self.assertTrue(resp.context["group_range_mode"])
        self.assertEqual(resp.context["selected_department_id"], self.dept.pk)
        self.assertEqual(resp.context["selected_team_id"], self.team.pk)
        self.assertEqual({r.employee.pk for r in resp.context["rows"]}, {self.emp.pk})
        self.assertEqual(resp.context["scope_label"], "Digital Advertising / Bench")

    def test_department_locked_viewer_needs_no_explicit_pick(self):
        # A plain employee's scope is one department, so effective_department_id
        # already resolves it — the range is allowed without ?department=.
        resp = self._range(self.emp)
        self.assertTrue(resp.context["group_range_mode"])
        self.assertFalse(resp.context["date_errors"])
        self.assertEqual({r.employee.pk for r in resp.context["rows"]}, {self.emp.pk})

    def test_all_departments_refused_without_org_wide_access(self):
        resp = self._range(_multi_dept_manager(self))
        self.assertFalse(resp.context["group_range_mode"])
        self.assertTrue(resp.context["date_errors"])
        self.assertEqual(resp.context["date_from"], resp.context["date_to"])

    def test_all_departments_range_for_hr_is_the_payroll_population(self):
        """HR on "All departments" (an empty `department=`) gets a range over
        the whole company: leavers inside the span included, leavers before
        it and accounts with no employee ID left out."""
        leaver = _user(
            "left@example.com", employee_id="4001", department=self.other_dept,
            is_active=False, last_working_date=self.monday + timedelta(days=2),
        )
        long_gone = _user(
            "gone@example.com", employee_id="4002", department=self.other_dept,
            is_active=False, last_working_date=self.monday - timedelta(days=30),
        )
        service = _user("svc@example.com", department=self.dept)
        resp = self._range(self.hr, department="", team="", q="")
        self.assertTrue(resp.context["group_range_mode"])
        self.assertFalse(resp.context["date_errors"])
        self.assertEqual(resp.context["scope_label"], "All departments")
        pks = {r.employee.pk for r in resp.context["rows"]}
        self.assertIn(self.outsider.pk, pks)
        self.assertIn(leaver.pk, pks)
        self.assertNotIn(long_gone.pk, pks)
        self.assertNotIn(service.pk, pks)
        # emp, hr, stranger, outsider, leaver × 5 dates.
        self.assertEqual(resp.context["paginator"].count, 25)

    def test_paged_rows_match_the_full_build(self):
        """The page path builds rows for the page's employees only; every
        page must still equal the matching slice of the unpaged row set the
        export uses, with or without `?flagged=1`."""
        from attendance.views import AttendanceListView

        punch_day = self.monday + timedelta(days=1)
        AttendancePunch.objects.create(
            employee=self.emp, date=punch_day,
            check_in=_aware(punch_day, 10), check_out=_aware(punch_day, 19),
        )
        for flagged in ("", "1"):
            # 4 people × 17 days = 68 rows: three pages, two of them split
            # an employee's block across the page boundary.
            first = self._range(self.hr, days=16, per_page="25", flagged=flagged)
            view = AttendanceListView()
            view.request = first.wsgi_request
            view.get(first.wsgi_request)  # resolves params on the instance
            full, unaccounted = view._rows(first.wsgi_request)
            self.assertEqual(first.context["unaccounted_count"], unaccounted)
            self.assertEqual(first.context["paginator"].count, len(full))
            if not flagged:
                self.assertEqual(first.context["paginator"].num_pages, 3)
            seen = []
            for page in range(1, first.context["paginator"].num_pages + 1):
                resp = self._range(self.hr, days=16, per_page="25", flagged=flagged, page=str(page))
                seen += [
                    (r.employee.pk, r.date, r.shift_label, r.is_unaccounted)
                    for r in resp.context["rows"]
                ]
            self.assertEqual(
                seen,
                [(r.employee.pk, r.date, r.shift_label, r.is_unaccounted) for r in full],
            )

    def test_flagged_filter_and_count_per_row(self):
        punch_day = self.monday + timedelta(days=1)
        AttendancePunch.objects.create(
            employee=self.emp, date=punch_day,
            check_in=_aware(punch_day, 10), check_out=_aware(punch_day, 19),
        )
        resp = self._range(self.hr, department=self.dept.pk)
        # emp: Mon-Fri GEN, one punched → 4; hr: the login-seeded default
        # pattern's 5 working days, no punches → 5; stranger: no shifts → 0.
        self.assertEqual(resp.context["unaccounted_count"], 9)

        resp = self._range(self.hr, department=self.dept.pk, flagged="1")
        rows = resp.context["rows"]
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(r.is_unaccounted for r in rows))
        self.assertEqual({r.employee.pk for r in rows}, {self.emp.pk, self.hr.pk})
        self.assertNotIn((self.emp.pk, punch_day), [(r.employee.pk, r.date) for r in rows])

    def test_range_is_capped_at_team_limit(self):
        start = date(2026, 1, 1)
        resp = self._get(
            self.hr, department=self.dept.pk,
            **{"from": start.isoformat(), "to": "2026-12-31"},
        )
        self.assertTrue(resp.context["group_range_mode"])
        self.assertEqual(resp.context["date_from"], start)
        self.assertEqual(resp.context["date_to"], start + timedelta(days=TEAM_MAX_RANGE_DAYS - 1))
        self.assertEqual(resp.context["paginator"].count, 3 * TEAM_MAX_RANGE_DAYS)

    def test_single_date_with_department_stays_team_mode(self):
        resp = self._get(self.hr, department=self.dept.pk, date=self.monday.isoformat())
        self.assertFalse(resp.context["group_range_mode"])
        self.assertFalse(resp.context["range_mode"])
        self.assertEqual(len(resp.context["employees"]), 3)

    def test_correction_states_shown_per_row(self):
        pending_day = self.monday + timedelta(days=2)
        AttendanceCorrection.objects.create(
            employee=self.emp, date=pending_day,
            requested_check_in=_aware(pending_day, 10), requested_check_out=_aware(pending_day, 19),
            reason="forgot", requested_by=self.emp,
        )
        resp = self._range(self.hr, department=self.dept.pk)
        by_key = {(r.employee.pk, r.date): r for r in resp.context["rows"]}
        self.assertTrue(by_key[(self.emp.pk, pending_day)].has_pending_correction)
        self.assertFalse(by_key[(self.emp.pk, self.monday)].has_pending_correction)
        self.assertTrue(all(r.can_request_correction for r in resp.context["rows"]))


class ExportTests(_Base):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        punch_day = cls.monday + timedelta(days=1)
        AttendancePunch.objects.create(
            employee=cls.emp, date=punch_day,
            check_in=_aware(punch_day, 10, 5), check_out=_aware(punch_day, 19, 30),
            time_in_office=timedelta(hours=9, minutes=25), office_bucket="MBD",
        )

    def _assert_xlsx(self, resp, filename):
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        self.assertIn(f'filename="{filename}"', resp["Content-Disposition"])
        sheet = _load_xlsx(resp)
        header = [c.value for c in sheet[1]]
        self.assertEqual(header[:4], ["Date", "Day", "Emp ID", "Name"])
        self.assertEqual(header[-1], "Status")
        return sheet

    def test_single_employee_range_export(self):
        start, end = self.monday, self.monday + timedelta(days=6)
        resp = self._get(
            self.hr, q="2001", export="xlsx",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        sheet = self._assert_xlsx(resp, f"timesheet-2001-{start}-to-{end}.xlsx")
        self.assertEqual(sheet.max_row, 8)  # header + 7 dates
        tuesday = [c.value for c in sheet[3]]
        self.assertEqual(tuesday[0], (start + timedelta(days=1)).isoformat())
        self.assertEqual(tuesday[1], "Tue")
        self.assertEqual(tuesday[2], "2001")
        self.assertEqual(tuesday[7:12], ["GEN", "MBD", "10:05", "19:30", "09:25:00"])
        self.assertFalse(tuesday[12])  # openpyxl reads an empty cell back as None
        monday = [c.value for c in sheet[2]]
        self.assertEqual(monday[12], "Unaccounted")

    def test_department_range_export_covers_every_row_not_one_page(self):
        start, end = self.monday, self.monday + timedelta(days=30)
        resp = self._get(
            self.hr, department=self.dept.pk, export="xlsx", per_page="25",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        sheet = self._assert_xlsx(resp, f"timesheet-digital-advertising-{start}-to-{end}.xlsx")
        self.assertEqual(sheet.max_row, 1 + 3 * 31)

    def test_team_mode_single_date_export(self):
        day = self.monday + timedelta(days=1)
        resp = self._get(self.hr, department=self.dept.pk, date=day.isoformat(), export="xlsx")
        sheet = self._assert_xlsx(resp, f"timesheet-digital-advertising-{day}-to-{day}.xlsx")
        self.assertEqual(sheet.max_row, 4)  # header + 3 employees
        self.assertEqual({sheet.cell(r, 3).value for r in range(2, 5)}, {"1000", "2001", "9999"})

    def test_all_departments_range_export_for_hr(self):
        start, end = self.monday, self.monday + timedelta(days=4)
        resp = self._get(
            self.hr, export="xlsx", department="", team="", q="",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        sheet = self._assert_xlsx(resp, f"timesheet-all-departments-{start}-to-{end}.xlsx")
        self.assertEqual(sheet.max_row, 1 + 3 * 5)

    def test_export_honours_flagged_filter_and_scope(self):
        start, end = self.monday, self.monday + timedelta(days=6)
        resp = self._get(
            self.emp, export="xlsx", flagged="1",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        sheet = self._assert_xlsx(resp, f"timesheet-digital-advertising-{start}-to-{end}.xlsx")
        # Mon-Fri GEN minus the punched Tuesday = 4 unaccounted days, own rows only.
        self.assertEqual(sheet.max_row, 5)
        self.assertEqual({sheet.cell(r, 3).value for r in range(2, 6)}, {"2001"})
        self.assertEqual({sheet.cell(r, 13).value for r in range(2, 6)}, {"Unaccounted"})
