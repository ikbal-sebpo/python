"""Permission matrix tests for accounts.permissions.

Coverage matrix:
- (role × target_relationship × action) for every helper.
- Auto-membership signals: none (legacy groups removed from active logic).

Roles tested:
- superuser
- HR (group)
- Admins (group)
- Senior Leadership (group)         — read-only
- Manager — TL with downline
- Self / regular employee
- Stranger (no relationship to target)

Edit-section axis:
- JobInfo (HR/manager-only; never self-editable)
- Contact (self-editable)
- IdentificationNumber (self-editable; covers NID/passport requirement)
"""

from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.models import AccessLevel, Department, Designation, Team
from accounts.permissions import (
    can_act_on_leave,
    can_apply_leave_for,
    can_approve_dedicated_night_shift,
    can_cancel_leave,
    can_edit_dedicated_night_shift,
    can_edit_all,
    can_edit_employee_basic_info,
    can_edit_employee_profile,
    can_edit_roster_pattern,
    can_request_dedicated_night_shift,
    can_see_all,
    can_see_all_departments,
    can_view_employee,
    department_grant_pks,
    downline_pks,
    editable_section_slugs,
    effective_department_id,
    has_managed_scope,
    managed_scope_pks,
    payroll_visible_employee_pks,
    visible_departments,
    visible_employee_pks,
    visible_employees,
    visible_teams,
)


User = get_user_model()


def _make_groups():
    """Make sure the post-migration groups exist for unit tests."""
    for name in (
        "User Admins", "HR", "Senior Leadership",
        "All Employees", "IT", "Finance",
    ):
        Group.objects.get_or_create(name=name)


def _user(email, **extra):
    """Helper: create a user with sensible test defaults."""
    return User.objects.create_user(
        email=email,
        username=email,
        password="testpass-1234!",
        first_name=email.split("@")[0].title(),
        last_name="Test",
        **extra,
    )


def _join_group(user, name):
    user.groups.add(Group.objects.get(name=name))


# ---------------------------------------------------------------------------
# Fixture setup
# ---------------------------------------------------------------------------

class _Hierarchy(TestCase):
    """Org chart used by every test:

        eng_head (heads Eng dept)
        sr_leader (Senior Leadership; Eng dept member)
        hr_user (HR group)
        admin_user (Admins group)
        superuser (is_superuser)

        pm  ── tl ── ic1
                  └── ic2
        peer_tl (in Eng but doesn't report to pm)
        outsider (in Marketing dept; no relationship)
    """

    @classmethod
    def setUpTestData(cls):
        _make_groups()
        cls.eng = Department.objects.create(name="Engineering")
        cls.mkt = Department.objects.create(name="Marketing")
        cls.designation = Designation.objects.create(name="Engineer")

        # Admin tier
        cls.superuser = User.objects.create_superuser(
            email="root@example.com", username="root",
            password="x", first_name="Root", last_name="X",
        )
        cls.hr_user = _user("hr@example.com", department=cls.eng)
        _join_group(cls.hr_user, "HR")
        cls.admin_user = _user("admins@example.com", department=cls.eng)
        _join_group(cls.admin_user, "User Admins")
        cls.sr_leader = _user("cxo@example.com", department=cls.eng)
        _join_group(cls.sr_leader, "Senior Leadership")

        # Line manager hierarchy in Engineering.
        cls.pm = _user("pm@example.com", department=cls.eng)
        cls.tl = _user("tl@example.com", department=cls.eng)
        cls.ic1 = _user("ic1@example.com", department=cls.eng)
        cls.ic2 = _user("ic2@example.com", department=cls.eng)
        cls.peer_tl = _user("peer-tl@example.com", department=cls.eng)
        cls.outsider = _user("outsider@example.com", department=cls.mkt)

        # Wire reports_to via JobInfo. Order matters — children before parents
        # would fail FK constraint? No: JobInfo.reports_to is a CustomUser FK,
        # not a JobInfo FK. So we can set them in any order.
        from employees.models import JobInfo
        JobInfo.objects.create(employee=cls.tl, reports_to=cls.pm)
        JobInfo.objects.create(employee=cls.ic1, reports_to=cls.tl)
        JobInfo.objects.create(employee=cls.ic2, reports_to=cls.tl)

        # Department head — explicit FK on the dept, fires signal.
        cls.dept_head = _user("eng-head@example.com", department=cls.eng)
        cls.eng.head_of_department = cls.dept_head
        cls.eng.save()

        cls.refresh_users()

    @classmethod
    def refresh_users(cls):
        # Refresh from DB so new group memberships set by signals are visible.
        for attr in (
            "superuser", "hr_user", "admin_user", "sr_leader",
            "pm", "tl", "ic1", "ic2", "peer_tl", "outsider", "dept_head",
        ):
            setattr(cls, attr, User.objects.get(pk=getattr(cls, attr).pk))


# ---------------------------------------------------------------------------
# can_see_all / can_edit_all
# ---------------------------------------------------------------------------

class CanSeeEditAllTests(_Hierarchy):
    def test_superuser_sees_all(self):
        self.assertTrue(can_see_all(self.superuser))
        self.assertTrue(can_edit_all(self.superuser))

    def test_hr_sees_and_edits_all(self):
        self.assertTrue(can_see_all(self.hr_user))
        self.assertTrue(can_edit_all(self.hr_user))

    def test_admins_see_and_edit_all(self):
        self.assertTrue(can_see_all(self.admin_user))
        self.assertTrue(can_edit_all(self.admin_user))

    def test_senior_leadership_sees_but_does_not_edit(self):
        self.assertFalse(can_see_all(self.sr_leader))
        self.assertFalse(can_edit_all(self.sr_leader))

    def test_regular_user_sees_nothing_blanket(self):
        self.assertFalse(can_see_all(self.tl))
        self.assertFalse(can_edit_all(self.tl))
        self.assertFalse(can_see_all(self.ic1))
        self.assertFalse(can_edit_all(self.ic1))


# ---------------------------------------------------------------------------
# Hierarchy helpers
# ---------------------------------------------------------------------------

class HierarchyTraversalTests(_Hierarchy):
    def test_downline_includes_self(self):
        self.assertIn(self.ic1.pk, downline_pks(self.ic1))

    def test_downline_walks_one_level(self):
        pks = downline_pks(self.tl)
        self.assertIn(self.tl.pk, pks)
        self.assertIn(self.ic1.pk, pks)
        self.assertIn(self.ic2.pk, pks)

    def test_downline_walks_transitively(self):
        pks = downline_pks(self.pm)
        self.assertIn(self.pm.pk, pks)
        self.assertIn(self.tl.pk, pks)
        self.assertIn(self.ic1.pk, pks)
        self.assertIn(self.ic2.pk, pks)
        # peer_tl is not a report
        self.assertNotIn(self.peer_tl.pk, pks)

    def test_downline_excludes_unrelated_users(self):
        pks = downline_pks(self.tl)
        self.assertNotIn(self.peer_tl.pk, pks)
        self.assertNotIn(self.outsider.pk, pks)
        self.assertNotIn(self.pm.pk, pks)

    def test_has_managed_scope(self):
        self.assertTrue(has_managed_scope(self.hr_user))
        self.assertTrue(has_managed_scope(self.tl))         # has direct reports
        self.assertFalse(has_managed_scope(self.ic1))       # IC has no scope
        self.assertTrue(has_managed_scope(self.dept_head))  # department-wide grant


# ---------------------------------------------------------------------------
# visible_employees / can_view_employee
# ---------------------------------------------------------------------------

class VisibilityTests(_Hierarchy):
    def test_admin_sees_all_active(self):
        pks = visible_employee_pks(self.hr_user)
        for u in (self.tl, self.ic1, self.outsider, self.peer_tl):
            self.assertIn(u.pk, pks)

    def test_senior_leader_sees_all_active(self):
        pks = visible_employee_pks(self.sr_leader)
        self.assertEqual(pks, {self.sr_leader.pk})

    def test_manager_sees_downline_plus_self(self):
        qs = visible_employees(self.tl)
        pks = set(qs.values_list("pk", flat=True))
        self.assertEqual(pks, {self.tl.pk, self.ic1.pk, self.ic2.pk})

    def test_dept_head_sees_whole_department(self):
        qs = visible_employees(self.dept_head)
        pks = set(qs.values_list("pk", flat=True))
        for u in (self.dept_head, self.pm, self.tl, self.ic1, self.ic2, self.peer_tl):
            self.assertIn(u.pk, pks)
        self.assertNotIn(self.outsider.pk, pks)  # other department

    def test_ic_sees_only_self(self):
        qs = visible_employees(self.ic1)
        pks = set(qs.values_list("pk", flat=True))
        self.assertEqual(pks, {self.ic1.pk})

    def test_can_view_employee_matrix(self):
        # IC sees self only.
        self.assertTrue(can_view_employee(self.ic1, self.ic1))
        self.assertFalse(can_view_employee(self.ic1, self.tl))
        # TL sees IC.
        self.assertTrue(can_view_employee(self.tl, self.ic1))
        self.assertFalse(can_view_employee(self.tl, self.peer_tl))
        self.assertTrue(can_view_employee(self.dept_head, self.peer_tl))  # department-wide grant
        # No hierarchy path means no visibility beyond self.
        self.assertFalse(can_view_employee(self.sr_leader, self.outsider))


# ---------------------------------------------------------------------------
# payroll_visible_employee_pks — includes recently-separated employees
# (docs/PAYROLL_PLAN.md, 2026-09-08: a resigned employee's manager must
# still be able to submit overtime for days worked before separation).
# ---------------------------------------------------------------------------

class PayrollVisibilityIncludesSeparatedTests(_Hierarchy):
    def setUp(self):
        # ic1 "resigned" — deactivated the same way deactivate_past_lwd
        # would, past their last working date. JobInfo.reports_to=tl is
        # untouched, same as in reality.
        self.ic1.is_active = False
        self.ic1.last_working_date = date(2026, 7, 31)
        self.ic1.save()

    def test_ordinary_visibility_excludes_separated_employee(self):
        self.assertNotIn(self.ic1.pk, visible_employee_pks(self.tl))
        self.assertNotIn(self.ic1.pk, visible_employee_pks(self.hr_user))

    def test_payroll_visibility_still_includes_separated_employee(self):
        self.assertIn(self.ic1.pk, payroll_visible_employee_pks(self.tl))
        self.assertIn(self.ic1.pk, payroll_visible_employee_pks(self.hr_user))

    def test_payroll_visibility_department_grant_includes_separated_employee(self):
        self.assertIn(self.ic1.pk, payroll_visible_employee_pks(self.dept_head))

    def test_payroll_visibility_still_excludes_true_outsider(self):
        # Separation doesn't blanket-grant visibility — an employee outside
        # the manager's hierarchy entirely is still excluded either way.
        self.outsider.is_active = False
        self.outsider.save()
        self.assertNotIn(self.outsider.pk, payroll_visible_employee_pks(self.tl))


# ---------------------------------------------------------------------------
# visible_employees(include_separated=...) — the same opt-in at queryset
# grain, used by the roster grid in search mode (roster/views.py) so a
# separated employee's historical shift rows stay readable.
# ---------------------------------------------------------------------------

class VisibleEmployeesIncludeSeparatedTests(_Hierarchy):
    def setUp(self):
        self.ic1.is_active = False
        self.ic1.last_working_date = date(2026, 7, 31)
        self.ic1.save()

    def _pks(self, viewer, **kwargs):
        return set(visible_employees(viewer, **kwargs).values_list("pk", flat=True))

    def test_default_still_excludes_separated_employee(self):
        self.assertNotIn(self.ic1.pk, self._pks(self.tl))
        self.assertNotIn(self.ic1.pk, self._pks(self.hr_user))
        self.assertNotIn(self.ic1.pk, self._pks(self.dept_head))

    def test_include_separated_widens_scoped_branch(self):
        self.assertIn(self.ic1.pk, self._pks(self.tl, include_separated=True))
        self.assertIn(self.ic1.pk, self._pks(self.dept_head, include_separated=True))

    def test_include_separated_widens_see_all_branch(self):
        self.assertIn(self.ic1.pk, self._pks(self.hr_user, include_separated=True))

    def test_include_separated_does_not_widen_permission_scope(self):
        # Only the is_active clamp is conditional — who may see whom is not.
        self.outsider.is_active = False
        self.outsider.save()
        self.assertNotIn(self.outsider.pk, self._pks(self.tl, include_separated=True))
        self.assertNotIn(self.peer_tl.pk, self._pks(self.tl, include_separated=True))

    def test_pks_helper_matches_queryset(self):
        self.assertIn(
            self.ic1.pk,
            visible_employee_pks(self.tl, include_separated=True),
        )
        self.assertNotIn(self.ic1.pk, visible_employee_pks(self.tl))


class ListSearchFindsSeparatedEmployeeTests(_Hierarchy):
    """/employees/ and /users/ hide deactivated people by default, but an
    explicit ?q= search surfaces them (2026-10-01). Scoping is unchanged: a
    viewer only finds a deactivated person who was in their downline/grants,
    and may then open that person's detail page."""

    def setUp(self):
        self.ic1.is_active = False
        self.ic1.last_working_date = date(2026, 7, 31)
        self.ic1.save()
        self.outsider.is_active = False
        self.outsider.save()

    def _rows(self, viewer, url_name, **params):
        self.client.force_login(viewer)
        response = self.client.get(reverse(url_name), params)
        self.assertEqual(response.status_code, 200)
        return {u.pk for u in response.context["object_list"]}

    # --- /employees/ ---------------------------------------------------

    def test_employee_list_default_excludes_separated(self):
        rows = self._rows(self.tl, "employee_list")
        self.assertNotIn(self.ic1.pk, rows)
        self.assertIn(self.ic2.pk, rows)

    def test_employee_list_search_finds_separated_in_scope(self):
        self.assertIn(self.ic1.pk, self._rows(self.tl, "employee_list", q="ic1"))
        self.assertIn(self.ic1.pk, self._rows(self.hr_user, "employee_list", q="ic1"))

    def test_employee_list_search_does_not_widen_scope(self):
        # A team leader can't find a deactivated person from another department.
        self.assertNotIn(
            self.outsider.pk, self._rows(self.tl, "employee_list", q="outsider"),
        )
        # ...but HR (org-wide scope) can.
        self.assertIn(
            self.outsider.pk, self._rows(self.hr_user, "employee_list", q="outsider"),
        )

    def test_employee_list_blank_search_stays_active_only(self):
        self.assertNotIn(self.ic1.pk, self._rows(self.tl, "employee_list", q="   "))

    def test_employee_list_department_filter_alone_stays_active_only(self):
        self.assertNotIn(
            self.ic1.pk, self._rows(self.hr_user, "employee_list", department=self.eng.pk),
        )

    # --- /users/ ---------------------------------------------------------

    def test_user_list_default_excludes_separated(self):
        rows = self._rows(self.admin_user, "user_list")
        self.assertNotIn(self.ic1.pk, rows)
        self.assertIn(self.ic2.pk, rows)

    def test_user_list_search_finds_separated(self):
        self.assertIn(self.ic1.pk, self._rows(self.admin_user, "user_list", q="ic1"))

    # --- detail-page follow-through ------------------------------------

    def test_scoped_manager_can_open_separated_reportee_detail(self):
        self.assertTrue(can_view_employee(self.tl, self.ic1))
        self.client.force_login(self.tl)
        response = self.client.get(reverse("employee_detail", args=[self.ic1.pk]))
        self.assertEqual(response.status_code, 200)

    def test_scoped_manager_cannot_open_separated_outsider_detail(self):
        self.assertFalse(can_view_employee(self.tl, self.outsider))
        self.client.force_login(self.tl)
        response = self.client.get(reverse("employee_detail", args=[self.outsider.pk]))
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# can_edit_employee_profile (per-section axis)
# ---------------------------------------------------------------------------

class ProfileEditTests(_Hierarchy):
    def test_hr_can_edit_basic_info(self):
        self.assertTrue(can_edit_employee_basic_info(self.hr_user, self.ic1))

    def test_manager_can_edit_downline_basic_info(self):
        self.assertTrue(can_edit_employee_basic_info(self.tl, self.ic1))

    def test_manager_cannot_edit_self_basic_info(self):
        self.assertFalse(can_edit_employee_basic_info(self.tl, self.tl))

    def test_stranger_cannot_edit_basic_info(self):
        self.assertFalse(can_edit_employee_basic_info(self.peer_tl, self.ic1))

    def test_hr_can_edit_any_section(self):
        for section in ("JobInfo", "Contact", "IdentificationNumber"):
            self.assertTrue(
                can_edit_employee_profile(self.hr_user, self.ic1, section=section),
                msg=section,
            )

    def test_manager_can_edit_downline_any_section(self):
        for section in ("JobInfo", "Contact", "IdentificationNumber"):
            self.assertTrue(
                can_edit_employee_profile(self.tl, self.ic1, section=section),
                msg=section,
            )

    def test_self_can_edit_personal_sections(self):
        self.ic1.profile_self_service_enabled = True
        self.ic1.save()
        for section in ("Contact", "IdentificationNumber", "Address",
                        "PersonalInfo", "LegalIssue"):
            self.assertTrue(
                can_edit_employee_profile(self.ic1, self.ic1, section=section),
                msg=section,
            )

    def test_self_cannot_edit_when_self_service_off(self):
        # profile_self_service_enabled defaults to False.
        for section in ("Contact", "IdentificationNumber", "Address",
                        "PersonalInfo", "LegalIssue"):
            self.assertFalse(
                can_edit_employee_profile(self.ic1, self.ic1, section=section),
                msg=section,
            )

    def test_self_cannot_edit_jobinfo(self):
        self.ic1.profile_self_service_enabled = True
        self.ic1.save()
        self.assertFalse(
            can_edit_employee_profile(self.ic1, self.ic1, section="JobInfo")
        )

    def test_stranger_cannot_edit_anyone(self):
        self.assertFalse(
            can_edit_employee_profile(self.outsider, self.ic1, section="Contact")
        )
        self.assertFalse(
            can_edit_employee_profile(self.peer_tl, self.ic1, section="Contact")
        )

    def test_senior_leadership_is_read_only(self):
        # Senior Leadership has no cross-hierarchy edit rights.
        self.assertFalse(
            can_edit_employee_profile(self.sr_leader, self.ic1, section="Contact")
        )
        self.assertFalse(
            can_edit_employee_profile(self.sr_leader, self.outsider, section="JobInfo")
        )


# ---------------------------------------------------------------------------
# editable_section_slugs precompute
# ---------------------------------------------------------------------------

class EditableSectionSlugsTests(_Hierarchy):
    def setUp(self):
        from employees.sections import ordered_sections
        self.sections = ordered_sections()

    def test_admin_gets_all_slugs(self):
        slugs = editable_section_slugs(self.hr_user, self.ic1, self.sections)
        self.assertEqual(slugs, {s.slug for s in self.sections})

    def test_self_excludes_jobinfo(self):
        self.ic1.profile_self_service_enabled = True
        self.ic1.save()
        slugs = editable_section_slugs(self.ic1, self.ic1, self.sections)
        # JobInfo slug is "jobinfo"; must not appear.
        self.assertNotIn("jobinfo", slugs)
        # Personal-data slugs do appear.
        for s in ("contact", "address", "id-number", "personal", "legal"):
            self.assertIn(s, slugs)

    def test_self_gets_none_when_self_service_off(self):
        # profile_self_service_enabled defaults to False.
        slugs = editable_section_slugs(self.ic1, self.ic1, self.sections)
        self.assertEqual(slugs, set())

    def test_manager_on_downline_gets_all(self):
        slugs = editable_section_slugs(self.tl, self.ic1, self.sections)
        self.assertEqual(slugs, {s.slug for s in self.sections})

    def test_stranger_gets_none(self):
        slugs = editable_section_slugs(self.outsider, self.ic1, self.sections)
        self.assertEqual(slugs, set())


# ---------------------------------------------------------------------------
# UserUpdateView basic-info scope
# ---------------------------------------------------------------------------

@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class UserUpdateViewTests(_Hierarchy):
    def test_manager_can_open_downline_basic_info_form(self):
        self.client.force_login(self.tl)
        response = self.client.get(reverse("user_update", args=[self.ic1.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit Ic1 Test")
        self.assertContains(response, "Save changes")
        self.assertNotContains(response, "Groups")
        self.assertNotContains(response, "Active</span>")

    def test_peer_manager_cannot_open_basic_info_form(self):
        self.client.force_login(self.peer_tl)
        response = self.client.get(reverse("user_update", args=[self.ic1.pk]))
        self.assertEqual(response.status_code, 403)

    def test_manager_post_cannot_change_privilege_fields(self):
        self.client.force_login(self.tl)
        hr_group = Group.objects.get(name="HR")
        self.assertFalse(self.ic1.groups.filter(pk=hr_group.pk).exists())
        self.assertTrue(self.ic1.is_active)

        response = self.client.post(
            reverse("user_update", args=[self.ic1.pk]),
            {
                "email": self.ic1.email,
                "username": self.ic1.username,
                "first_name": "Updated",
                "last_name": self.ic1.last_name,
                "employee_id": self.ic1.employee_id or "",
                "department": self.ic1.department_id or "",
                "designation": self.ic1.designation_id or "",
                "team": self.ic1.team_id or "",
                "subteam": self.ic1.subteam_id or "",
                "job_location": self.ic1.job_location_id or "",
                "work_location": self.ic1.work_location_id or "",
                "contact_number": "01700000000",
                "joining_date": self.ic1.joining_date or "",
                "probation_month": self.ic1.probation_month or "",
                "confirmation_date": self.ic1.confirmation_date or "",
                "last_working_date": self.ic1.last_working_date or "",
                "is_active": "",
                "groups": [str(hr_group.pk)],
            },
        )
        self.assertRedirects(response, reverse("employee_detail", args=[self.ic1.pk]))

        self.ic1.refresh_from_db()
        self.assertEqual(self.ic1.first_name, "Updated")
        self.assertEqual(self.ic1.contact_number, "01700000000")
        self.assertTrue(self.ic1.is_active)
        self.assertFalse(self.ic1.groups.filter(pk=hr_group.pk).exists())

    def test_manager_department_dropdown_scoped_to_own_department(self):
        """A limited-scope manager sees only their own department in the
        dropdown, so they can't accidentally move a report elsewhere."""
        self.client.force_login(self.tl)
        response = self.client.get(reverse("user_update", args=[self.ic1.pk]))
        self.assertEqual(response.status_code, 200)
        depts = list(response.context["form"].fields["department"].queryset)
        self.assertEqual(depts, [self.eng])
        self.assertNotIn(self.mkt, depts)

    def test_manager_post_cannot_move_employee_to_other_department(self):
        """Scoping is enforced server-side: a tampered POST naming an
        out-of-scope department is rejected, leaving the department unchanged."""
        self.client.force_login(self.tl)
        self.assertEqual(self.ic1.department_id, self.eng.pk)

        response = self.client.post(
            reverse("user_update", args=[self.ic1.pk]),
            {
                "email": self.ic1.email,
                "username": self.ic1.username,
                "first_name": self.ic1.first_name,
                "last_name": self.ic1.last_name,
                "employee_id": self.ic1.employee_id or "",
                "department": str(self.mkt.pk),  # outside the manager's scope
                "designation": self.ic1.designation_id or "",
                "team": self.ic1.team_id or "",
                "subteam": self.ic1.subteam_id or "",
                "job_location": self.ic1.job_location_id or "",
                "work_location": self.ic1.work_location_id or "",
                "contact_number": "01700000000",
                "joining_date": self.ic1.joining_date or "",
                "probation_month": self.ic1.probation_month or "",
                "confirmation_date": self.ic1.confirmation_date or "",
                "last_working_date": self.ic1.last_working_date or "",
            },
        )
        # Invalid choice → form re-renders (200), no redirect, nothing saved.
        self.assertEqual(response.status_code, 200)
        self.ic1.refresh_from_db()
        self.assertEqual(self.ic1.department_id, self.eng.pk)


# ---------------------------------------------------------------------------
# UserCreateView — NID capture, notice period, required-except-Team, rehire warning
# ---------------------------------------------------------------------------

class UserCreateViewTests(_Hierarchy):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from accounts.models import EmploymentType, Grade, JobLocation, WorkLocation
        cls.grade = Grade.objects.create(name="G1")
        cls.job_location = JobLocation.objects.create(name="Dhaka")
        cls.work_location = WorkLocation.objects.create(name="MHK")
        cls.employment_type = EmploymentType.objects.create(name="Full Time")

    def _valid_payload(self, **overrides):
        payload = {
            "email": "new.hire@example.com",
            "first_name": "New",
            "last_name": "Hire",
            "employee_id": "9500",
            "nid_number": "1234567890123",
            "department": self.eng.pk,
            "designation": self.designation.pk,
            "grade": self.grade.pk,
            "job_location": self.job_location.pk,
            "work_location": self.work_location.pk,
            "contact_number": "01700000000",
            "joining_date": "2026-09-01",
            "probation_month": "3",
            "notice_period_months": "2",
            "password1": "S0me-Str0ng-Pass!",
            "password2": "S0me-Str0ng-Pass!",
        }
        payload.update(overrides)
        return payload

    def test_missing_non_team_field_is_a_form_error(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("user_create"), self._valid_payload(job_location=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="new.hire@example.com").exists())

    def test_blank_team_still_succeeds(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(reverse("user_create"), self._valid_payload())
        user = User.objects.get(email="new.hire@example.com")
        self.assertRedirects(response, reverse("employee_detail", args=[user.pk]))
        self.assertIsNone(user.team_id)
        self.assertEqual(user.notice_period_months, 2)

    def test_valid_post_creates_identification_number(self):
        self.client.force_login(self.admin_user)
        self.client.post(reverse("user_create"), self._valid_payload())
        user = User.objects.get(email="new.hire@example.com")

        from employees.models import IdentificationNumber
        nid = IdentificationNumber.objects.get(employee=user)
        self.assertEqual(nid.id_type, IdentificationNumber.IdType.NID)
        self.assertEqual(nid.id_number, "1234567890123")

    def test_reused_nid_from_offboarded_employee_warns_but_still_creates(self):
        from django.utils import timezone

        from employees.models import IdentificationNumber
        from offboarding.models import Resignation

        former = _user("former@example.com", department=self.eng, is_active=False)
        IdentificationNumber.objects.create(
            employee=former, id_type=IdentificationNumber.IdType.NID, id_number="1234567890123",
            added_by=self.admin_user, added_at=timezone.now(),
            updated_by=self.admin_user, updated_at=timezone.now(),
        )
        Resignation.objects.create(
            employee=former, separation_type=Resignation.SeparationType.INVOLUNTARY,
            reason="Terminated", notice_period_days=0,
            notice_start_date=date(2026, 1, 1), last_working_date=date(2026, 1, 1),
            status=Resignation.Status.ACCEPTED,
            applied_by=self.admin_user, processed_by=self.admin_user,
            added_by=self.admin_user, updated_by=self.admin_user,
        )

        self.client.force_login(self.admin_user)
        response = self.client.post(reverse("user_create"), self._valid_payload(), follow=True)
        self.assertTrue(User.objects.filter(email="new.hire@example.com").exists())
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Possible rehire" in m for m in messages))


# ---------------------------------------------------------------------------
# Live AJAX endpoints backing /users/create/ (identity suggest, NID check)
# ---------------------------------------------------------------------------

class UserCreateAjaxTests(_Hierarchy):
    def test_suggest_identity_flags_exhausted_name_space(self):
        """Reproduces the reported case: 'S.M' + 'Rashel' in a Data
        Solutions-style department collides with an existing account and
        can't be widened any further — the endpoint should say so rather
        than silently returning an empty email."""
        User.objects.create_user(
            email="smrashel@sebpo.com", username="smrashel@sebpo.com",
            password="x", first_name="S.M", last_name="Rashel",
        )
        data_solutions = Department.objects.create(name="Data solution and Due Diligence")
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("ajax_suggest_identity"), {
            "first_name": "S.M", "last_name": "Rashel", "department": data_solutions.pk,
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["email"], "")
        self.assertTrue(data["email_exhausted"])

    def test_suggest_identity_returns_email_when_available(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("ajax_suggest_identity"), {
            "first_name": "Brand", "last_name": "New", "department": self.eng.pk,
        })
        data = response.json()
        self.assertEqual(data["email"], "brand.new@sebpo.com")
        self.assertFalse(data["email_exhausted"])

    def test_check_nid_returns_warning_on_match(self):
        from django.utils import timezone

        from employees.models import IdentificationNumber

        IdentificationNumber.objects.create(
            employee=self.tl, id_type=IdentificationNumber.IdType.NID, id_number="1112223334",
            added_by=self.admin_user, added_at=timezone.now(),
            updated_by=self.admin_user, updated_at=timezone.now(),
        )
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("ajax_check_nid"), {"nid_number": "1112223334"})
        data = response.json()
        self.assertIsNotNone(data["warning"])
        self.assertIn("Possible rehire", data["warning"])

    def test_check_nid_returns_none_when_no_match(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("ajax_check_nid"), {"nid_number": "0000000000"})
        self.assertIsNone(response.json()["warning"])

    def test_check_nid_denied_for_non_admin(self):
        self.client.force_login(self.tl)
        response = self.client.get(reverse("ajax_check_nid"), {"nid_number": "1112223334"})
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# can_edit_roster_pattern
# ---------------------------------------------------------------------------

class RosterPatternTests(_Hierarchy):
    def test_admin_can_edit_anyone(self):
        self.assertTrue(can_edit_roster_pattern(self.hr_user, self.ic1))
        self.assertTrue(can_edit_roster_pattern(self.hr_user, self.hr_user))

    def test_manager_can_edit_downline(self):
        self.assertTrue(can_edit_roster_pattern(self.tl, self.ic1))

    def test_non_admin_cannot_edit_own(self):
        # Even managers and dept heads can't self-edit pattern.
        self.assertFalse(can_edit_roster_pattern(self.tl, self.tl))
        self.assertFalse(can_edit_roster_pattern(self.ic1, self.ic1))

    def test_senior_leadership_cannot_edit_pattern(self):
        # Read-only: see all but no roster writes.
        self.assertFalse(can_edit_roster_pattern(self.sr_leader, self.ic1))

    def test_supervisor_plus_designation_can_edit_own(self):
        # "Team leader and above" — Supervisor-tier Designation.access_level.
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        team_lead = Designation.objects.create(name="Team Lead", access_level=supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        self.assertTrue(can_edit_roster_pattern(self.tl, self.tl))

    def test_employee_tier_designation_still_cannot_edit_own(self):
        AccessLevel.objects.create(name="Supervisor", rank=20)
        employee_level = AccessLevel.objects.create(name="Employee", rank=10)
        associate = Designation.objects.create(name="Associate", access_level=employee_level)
        self.ic1.designation = associate
        self.ic1.save()
        self.assertFalse(can_edit_roster_pattern(self.ic1, self.ic1))

    def test_missing_supervisor_tier_fails_closed(self):
        # No "Supervisor" AccessLevel seeded at all — must deny, not default-allow.
        some_level = AccessLevel.objects.create(name="Whatever", rank=999)
        designation = Designation.objects.create(name="Mystery", access_level=some_level)
        self.ic1.designation = designation
        self.ic1.save()
        self.assertFalse(can_edit_roster_pattern(self.ic1, self.ic1))

    def test_department_additional_manager_can_edit_own_roster(self):
        self.eng.additional_managers.add(self.peer_tl)
        self.assertTrue(can_edit_roster_pattern(self.peer_tl, self.peer_tl))

    def test_department_head_can_edit_own_roster(self):
        self.assertTrue(can_edit_roster_pattern(self.dept_head, self.dept_head))

    def test_team_additional_manager_can_edit_own_roster(self):
        # "Acting TL" grant: added as additional_manager on their own team
        # without changing Designation.
        team = Team.objects.create(name="Backend", department=self.eng)
        team.additional_managers.add(self.ic1)
        self.assertTrue(can_edit_roster_pattern(self.ic1, self.ic1))

    def test_own_access_level_override_can_edit_own_roster(self):
        # Manual per-user override on CustomUser.access_level, for someone
        # who can't get the Team.additional_managers grant. Designation
        # stays Employee-tier throughout.
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        self.ic1.access_level = supervisor_level
        self.ic1.save()
        self.assertTrue(can_edit_roster_pattern(self.ic1, self.ic1))

    def test_own_access_level_override_below_supervisor_still_denied(self):
        AccessLevel.objects.create(name="Supervisor", rank=20)
        employee_level = AccessLevel.objects.create(name="Employee", rank=10)
        self.ic1.access_level = employee_level
        self.ic1.save()
        self.assertFalse(can_edit_roster_pattern(self.ic1, self.ic1))


# ---------------------------------------------------------------------------
# can_request_dedicated_night_shift / can_approve_dedicated_night_shift
# (docs/PAYROLL_PLAN.md, 2026-09-07)
# ---------------------------------------------------------------------------

class DedicatedNightShiftPermissionTests(_Hierarchy):
    def setUp(self):
        from employees.models import DedicatedNightShiftAssignment

        self.DedicatedNightShiftAssignment = DedicatedNightShiftAssignment
        self.supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        self.manager_level = AccessLevel.objects.create(name="Manager", rank=30)

    def _assignment_for(self, employee):
        return self.DedicatedNightShiftAssignment(
            employee=employee, start_date="2026-07-01", end_date="2026-09-30",
        )

    def test_employee_tier_cannot_request(self):
        # self.tl's designation ("Engineer", no access_level) is below Supervisor.
        self.assertFalse(can_request_dedicated_night_shift(self.tl, self.ic1))

    def test_supervisor_tier_manager_can_request_for_downline(self):
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        self.assertTrue(can_request_dedicated_night_shift(self.tl, self.ic1))

    def test_supervisor_tier_cannot_request_for_non_managed(self):
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        self.assertFalse(can_request_dedicated_night_shift(self.tl, self.outsider))

    def test_admin_can_request_for_anyone_regardless_of_tier(self):
        self.assertTrue(can_request_dedicated_night_shift(self.hr_user, self.ic1))

    def test_manager_tier_required_to_approve(self):
        program_manager = Designation.objects.create(name="Program Manager", access_level=self.manager_level)
        self.pm.designation = program_manager
        self.pm.save()
        assignment = self._assignment_for(self.ic1)
        self.assertTrue(can_approve_dedicated_night_shift(self.pm, assignment))

    def test_supervisor_tier_alone_cannot_approve(self):
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        assignment = self._assignment_for(self.ic1)
        self.assertFalse(can_approve_dedicated_night_shift(self.tl, assignment))

    def test_manager_cannot_approve_own_assignment(self):
        program_manager = Designation.objects.create(name="Program Manager", access_level=self.manager_level)
        self.pm.designation = program_manager
        self.pm.save()
        own_assignment = self._assignment_for(self.pm)
        self.assertFalse(can_approve_dedicated_night_shift(self.pm, own_assignment))

    def test_admin_can_approve_anyone_including_own(self):
        assignment = self._assignment_for(self.ic1)
        self.assertTrue(can_approve_dedicated_night_shift(self.hr_user, assignment))
        own_assignment = self._assignment_for(self.hr_user)
        self.assertTrue(can_approve_dedicated_night_shift(self.hr_user, own_assignment))

    # -- Confirmed 2026-09-08: nobody self-requests/self-edits, TL/APM tier
    # included — only a full admin can act on their own assignment. --

    def test_tl_tier_cannot_request_for_self(self):
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        self.assertFalse(can_request_dedicated_night_shift(self.tl, self.tl))

    def test_tl_tier_cannot_edit_own_pending_request(self):
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        own_assignment = self._assignment_for(self.tl)
        own_assignment.status = self.DedicatedNightShiftAssignment.Status.PENDING
        self.assertFalse(can_edit_dedicated_night_shift(self.tl, own_assignment))

    def test_manager_tier_cannot_edit_own_approved_assignment(self):
        program_manager = Designation.objects.create(name="Program Manager", access_level=self.manager_level)
        self.pm.designation = program_manager
        self.pm.save()
        own_assignment = self._assignment_for(self.pm)
        own_assignment.status = self.DedicatedNightShiftAssignment.Status.APPROVED
        self.assertFalse(can_edit_dedicated_night_shift(self.pm, own_assignment))

    # -- can_edit_dedicated_night_shift: TL+ bar for both PENDING and
    # APPROVED as of 2026-09-15. The pay control that used to live in this
    # function (APPROVED barred below Manager tier) moved to the view,
    # which demotes a below-Manager editor's save back to PENDING — see
    # employees.tests.DedicatedNightShiftEditViewTests. --

    def test_pending_editable_by_tl_tier_manager(self):
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        assignment = self._assignment_for(self.ic1)
        assignment.status = self.DedicatedNightShiftAssignment.Status.PENDING
        self.assertTrue(can_edit_dedicated_night_shift(self.tl, assignment))

    def test_approved_editable_by_tl_tier_manager(self):
        """Reversed 2026-09-15: a Supervisor-tier manager maintaining these
        windows may now open an APPROVED row. Approval isn't weakened —
        their save costs the row its APPROVED status (view-level)."""
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        assignment = self._assignment_for(self.ic1)
        assignment.status = self.DedicatedNightShiftAssignment.Status.APPROVED
        self.assertTrue(can_edit_dedicated_night_shift(self.tl, assignment))

    def test_approved_still_not_editable_by_tl_tier_outside_scope(self):
        """The tier drop didn't widen *scope* — managed-scope still gates it."""
        team_lead = Designation.objects.create(name="Team Lead", access_level=self.supervisor_level)
        self.tl.designation = team_lead
        self.tl.save()
        assignment = self._assignment_for(self.outsider)
        assignment.status = self.DedicatedNightShiftAssignment.Status.APPROVED
        self.assertFalse(can_edit_dedicated_night_shift(self.tl, assignment))

    def test_approved_not_editable_below_supervisor_tier(self):
        """self.tl's default designation ("Engineer") has no access_level."""
        assignment = self._assignment_for(self.ic1)
        assignment.status = self.DedicatedNightShiftAssignment.Status.APPROVED
        self.assertFalse(can_edit_dedicated_night_shift(self.tl, assignment))

    def test_approved_editable_by_manager_tier(self):
        program_manager = Designation.objects.create(name="Program Manager", access_level=self.manager_level)
        self.pm.designation = program_manager
        self.pm.save()
        assignment = self._assignment_for(self.ic1)
        assignment.status = self.DedicatedNightShiftAssignment.Status.APPROVED
        self.assertTrue(can_edit_dedicated_night_shift(self.pm, assignment))

    def test_declined_and_cancelled_not_editable_even_by_admin(self):
        for status in (
            self.DedicatedNightShiftAssignment.Status.DECLINED,
            self.DedicatedNightShiftAssignment.Status.CANCELLED,
        ):
            assignment = self._assignment_for(self.ic1)
            assignment.status = status
            self.assertFalse(can_edit_dedicated_night_shift(self.hr_user, assignment))


# ---------------------------------------------------------------------------
# Leave: can_act_on_leave / can_cancel_leave / can_apply_leave_for
# ---------------------------------------------------------------------------

class LeaveActionTests(_Hierarchy):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        # Make a LEAVE-category ShiftType + a pending application from ic1.
        from roster.models import ShiftType
        from leave.models import LeaveApplication

        # Reuse the seeded LEAVE-category ShiftType when running against
        # MySQL (where the seed migration ran). Fall back to creation
        # under SQLite test settings, which disable migration replay.
        cls.casual, _ = ShiftType.objects.get_or_create(
            name="Casual Leave",
            defaults={
                "category": ShiftType.Category.LEAVE,
                "is_working_shift": False,
                "is_active": True,
            },
        )
        cls.app = LeaveApplication.objects.create(
            employee=cls.ic1,
            leave_type=cls.casual,
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 1),
            applied_by=cls.ic1,
        )

    def test_manager_can_act_on_downline_pending(self):
        self.assertTrue(can_act_on_leave(self.tl, self.app))

    def test_admin_can_act(self):
        self.assertTrue(can_act_on_leave(self.hr_user, self.app))

    def test_senior_leadership_cannot_act(self):
        # Read-only.
        self.assertFalse(can_act_on_leave(self.sr_leader, self.app))

    def test_applicant_cannot_approve_own(self):
        self.assertFalse(can_act_on_leave(self.ic1, self.app))

    def test_peer_tl_cannot_act(self):
        self.assertFalse(can_act_on_leave(self.peer_tl, self.app))

    def test_self_cancel_pending(self):
        self.assertTrue(can_cancel_leave(self.ic1, self.app))

    def test_self_cancel_disallowed_after_approval(self):
        self.app.status = self.app.Status.APPROVED
        self.app.save()
        # Applicant alone, non-pending → can no longer self-cancel.
        self.assertFalse(can_cancel_leave(self.ic1, self.app))
        # Manager still can.
        self.assertTrue(can_cancel_leave(self.tl, self.app))

    def test_can_apply_leave_for_self(self):
        self.assertTrue(can_apply_leave_for(self.ic1, self.ic1))

    def test_manager_can_apply_for_downline(self):
        self.assertTrue(can_apply_leave_for(self.tl, self.ic1))

    def test_stranger_cannot_apply_for_others(self):
        self.assertFalse(can_apply_leave_for(self.peer_tl, self.ic1))
        self.assertFalse(can_apply_leave_for(self.outsider, self.ic1))


# ---------------------------------------------------------------------------
# Department-wide manager grant (Department.additional_managers)
# ---------------------------------------------------------------------------

class AdditionalManagerGrantTests(_Hierarchy):
    """A non-head manager granted department-wide read+write scope.

    `peer_tl` is in Engineering but does NOT report to anyone in the pm/tl
    chain; we make them an additional manager of Engineering and assert they
    now see and can act on the whole department (but not other departments),
    while non-granted managers are unaffected.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Grant peer_tl department-wide scope over Engineering.
        cls.eng.additional_managers.add(cls.peer_tl)
        # An inactive Eng member must be excluded from the grant.
        cls.inactive = _user("gone@example.com", department=cls.eng, is_active=False)
        cls.refresh_users()

    def test_grant_pks_cover_active_department_minus_self(self):
        pks = department_grant_pks(self.peer_tl)
        # Sees fellow Eng members outside any reports-to relationship.
        for u in (self.pm, self.tl, self.ic1, self.ic2, self.dept_head):
            self.assertIn(u.pk, pks)
        # Excludes self and inactive members.
        self.assertNotIn(self.peer_tl.pk, pks)
        self.assertNotIn(self.inactive.pk, pks)
        # Does not leak into other departments.
        self.assertNotIn(self.outsider.pk, pks)

    def test_visible_employees_widen_to_department(self):
        pks = set(visible_employees(self.peer_tl).values_list("pk", flat=True))
        self.assertIn(self.peer_tl.pk, pks)          # self
        for u in (self.pm, self.tl, self.ic1, self.ic2):
            self.assertIn(u.pk, pks)
        self.assertNotIn(self.outsider.pk, pks)      # other dept
        self.assertTrue(can_view_employee(self.peer_tl, self.ic1))

    def test_managed_scope_and_has_managed_scope(self):
        scope = managed_scope_pks(self.peer_tl)
        self.assertIn(self.ic1.pk, scope)
        self.assertNotIn(self.peer_tl.pk, scope)     # excludes self
        self.assertTrue(has_managed_scope(self.peer_tl))

    def test_full_manage_edit_and_leave_action(self):
        # Full-manage: can edit a non-reportee's HR-managed section...
        self.assertTrue(
            can_edit_employee_profile(self.peer_tl, self.ic1, section="JobInfo")
        )
        # ...and act on their leave.
        from leave.models import LeaveApplication
        from roster.models import ShiftType
        casual, _ = ShiftType.objects.get_or_create(
            name="Casual Leave",
            defaults={
                "category": ShiftType.Category.LEAVE,
                "is_working_shift": False,
                "is_active": True,
            },
        )
        app = LeaveApplication.objects.create(
            employee=self.ic1, leave_type=casual,
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 1),
            applied_by=self.ic1,
        )
        self.assertTrue(can_act_on_leave(self.peer_tl, app))

    def test_grant_does_not_reach_other_departments(self):
        self.assertFalse(can_view_employee(self.peer_tl, self.outsider))
        self.assertFalse(can_edit_employee_profile(self.peer_tl, self.outsider, section="JobInfo"))

    def test_non_granted_manager_unaffected(self):
        # tl still sees only their own downline + self.
        pks = set(visible_employees(self.tl).values_list("pk", flat=True))
        self.assertEqual(pks, {self.tl.pk, self.ic1.pk, self.ic2.pk})
        self.assertEqual(department_grant_pks(self.tl), set())

    def test_payroll_report_widens_to_department(self):
        from payroll.reports import PayrollFilters, build_payroll_report

        # The payroll population excludes accounts with no employee_id
        # (payroll.periods.employed_during_q), and the hierarchy fixture
        # creates none.
        for n, u in enumerate(User.objects.order_by("email"), start=1):
            User.objects.filter(pk=u.pk).update(employee_id=str(n))
        filters = PayrollFilters(start_date=date(2026, 5, 1), end_date=date(2026, 5, 31))
        report = build_payroll_report(filters, viewer=self.peer_tl)
        names = {row["name"] for row in report["rows"]}
        for u in (self.pm, self.tl, self.ic1, self.ic2, self.peer_tl):
            self.assertIn(u.display_name, names)
        self.assertNotIn(self.outsider.display_name, names)


# ---------------------------------------------------------------------------
# Department / team dropdown scope
# ---------------------------------------------------------------------------

class DepartmentScopeTests(_Hierarchy):
    """Who may browse all departments/teams vs. is locked to their own."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eng_team = Team.objects.create(name="Eng Team", department=cls.eng)
        cls.mkt_team = Team.objects.create(name="Mkt Team", department=cls.mkt)

    def _req(self, user, **params):
        req = RequestFactory().get("/", params)
        req.user = user
        return req

    def test_privileged_groups_see_all_departments(self):
        for user in (self.superuser, self.admin_user, self.hr_user, self.sr_leader):
            self.assertTrue(can_see_all_departments(user))
            self.assertEqual(
                set(visible_departments(user).values_list("pk", flat=True)),
                {self.eng.pk, self.mkt.pk},
            )

    def test_plain_user_locked_to_own_department(self):
        self.assertFalse(can_see_all_departments(self.tl))
        self.assertEqual(
            list(visible_departments(self.tl).values_list("pk", flat=True)),
            [self.eng.pk],
        )

    def test_user_without_department_sees_no_departments(self):
        nobody = _user("nodept@example.com")
        self.assertEqual(list(visible_departments(nobody)), [])

    def test_effective_department_forced_for_locked_user(self):
        # A tampered ?department= pointing at another dept is ignored.
        req = self._req(self.tl, department=str(self.mkt.pk))
        self.assertEqual(effective_department_id(self.tl, req), self.eng.pk)

    def test_effective_department_honours_choice_for_privileged(self):
        req = self._req(self.admin_user, department=str(self.mkt.pk))
        self.assertEqual(effective_department_id(self.admin_user, req), self.mkt.pk)
        self.assertIsNone(effective_department_id(self.admin_user, self._req(self.admin_user)))

    def test_visible_teams_scoped_to_department(self):
        # Privileged: teams narrow to the requested department.
        self.assertEqual(
            list(visible_teams(self.admin_user, department_id=self.eng.pk)
                 .values_list("pk", flat=True)),
            [self.eng_team.pk],
        )
        # Locked: always clamped to own department's teams regardless of the arg.
        self.assertEqual(
            list(visible_teams(self.tl, department_id=self.mkt.pk)
                 .values_list("pk", flat=True)),
            [self.eng_team.pk],
        )


# ---------------------------------------------------------------------------
# 0002 standardize_user_admins data migration
# ---------------------------------------------------------------------------

class StandardizeUserAdminsMigrationTests(TestCase):
    """The forwards() reconciliation re-points ACL links and drops "Admins"."""

    def test_forwards_repoints_menu_and_deletes_legacy_group(self):
        import importlib

        from django.apps import apps as global_apps
        from dynamic_menu.models import MainMenu, ProtectedView

        legacy = Group.objects.create(name="Admins")
        member = _user("legacy-admin@example.com")
        member.groups.add(legacy)

        menu = MainMenu.objects.create(title="Users", rank=10)
        menu.groups.add(legacy)
        pv = ProtectedView.objects.create(
            registered_app="secret", view_name="secret_view",
        )
        pv.allowed_groups.add(legacy)

        mig = importlib.import_module(
            "accounts.migrations.0002_standardize_user_admins"
        )
        mig.forwards(global_apps, None)

        self.assertFalse(Group.objects.filter(name="Admins").exists())
        new = Group.objects.get(name="User Admins")
        self.assertEqual(list(menu.groups.values_list("pk", flat=True)), [new.pk])
        self.assertEqual(list(pv.allowed_groups.values_list("pk", flat=True)), [new.pk])
        self.assertIn(new.pk, member.groups.values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# department → team → subteam cascade
# ---------------------------------------------------------------------------

class DepartmentTeamCascadeTests(_Hierarchy):
    """The team dropdown must depend on the department everywhere it appears.

    Two mechanisms, one invariant: filter bars hand the browser every
    pickable team plus each option's department and let
    base.html::wireDepartmentTeamCascade narrow the list (no reload, so a
    hover popover survives the change), while the user create/edit form
    fetches teams over ajax and validates the pair server-side.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eng_team = Team.objects.create(name="Platform", department=cls.eng)
        cls.mkt_team = Team.objects.create(name="Campaigns", department=cls.mkt)

    def _get(self, **params):
        request = RequestFactory().get("/", params)
        request.user = self.admin_user
        return request

    # -- filter bar ---------------------------------------------------------

    def test_filter_context_offers_every_team_for_the_client_cascade(self):
        """Narrowing server-side would strand the browser's option snapshot
        with one department's teams, so switching departments in the popover
        would empty the dropdown for good."""
        from accounts.filters import employee_filter_context

        ctx = employee_filter_context(self.admin_user, self._get(department=self.eng.pk))
        self.assertIn(self.eng_team, ctx["teams"])
        self.assertIn(self.mkt_team, ctx["teams"])

    def test_filter_context_drops_a_team_outside_the_chosen_department(self):
        from accounts.filters import employee_filter_context

        ctx = employee_filter_context(
            self.admin_user, self._get(department=self.eng.pk, team=self.mkt_team.pk),
        )
        self.assertIsNone(ctx["selected_team_id"])

    def test_team_alone_backfills_its_department(self):
        """A hand-typed ?team= with no ?department= used to filter by the
        team while the department dropdown still read "All departments";
        the team's own department is now selected alongside it, so the two
        controls and the filtering agree (accounts.filters.effective_filter_ids)."""
        from accounts.filters import apply_employee_filters, employee_filter_context

        ctx = employee_filter_context(self.admin_user, self._get(team=self.mkt_team.pk))
        self.assertEqual(ctx["selected_department_id"], self.mkt.pk)
        self.assertEqual(ctx["selected_team_id"], self.mkt_team.pk)

        self.ic1.team = self.eng_team
        self.ic1.department = self.eng
        self.ic1.save(update_fields=["team", "department"])
        qs = apply_employee_filters(
            User.objects.all(), self.admin_user, self._get(team=self.eng_team.pk),
        )
        self.assertEqual(list(qs), [self.ic1])

    def test_filtering_still_applies_a_matching_team(self):
        from accounts.filters import apply_employee_filters

        self.ic1.team = self.eng_team
        self.ic1.save(update_fields=["team"])
        qs = apply_employee_filters(
            User.objects.all(), self.admin_user,
            self._get(department=self.eng.pk, team=self.eng_team.pk),
        )
        self.assertEqual(list(qs), [self.ic1])

    def test_leave_filter_popover_tags_options_with_their_department(self):
        """The cascade JS keys off data-department — without it the popover's
        team list stays unfiltered (the reported bug)."""
        self.client.force_login(self.admin_user)
        html = self.client.get(reverse("leave_list")).content.decode()
        self.assertIn('data-team-for="leave-department"', html)
        self.assertIn(f'data-department="{self.eng.pk}"', html)

    # -- ajax ---------------------------------------------------------------

    def test_load_subteams_returns_the_teams_own_subteams(self):
        from accounts.models import SubTeam

        wanted = SubTeam.objects.create(name="Payments", team=self.eng_team)
        SubTeam.objects.create(name="Social", team=self.mkt_team)
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("ajax_load_subteams"), {"team": self.eng_team.pk})
        self.assertEqual(
            response.json()["results"], [{"id": wanted.pk, "label": "Payments"}],
        )

    def test_load_subteams_is_scoped_to_teams_the_viewer_may_see(self):
        from accounts.models import SubTeam

        SubTeam.objects.create(name="Social", team=self.mkt_team)
        self.client.force_login(self.tl)  # Engineering, not privileged
        response = self.client.get(reverse("ajax_load_subteams"), {"team": self.mkt_team.pk})
        self.assertEqual(response.json()["results"], [])

    # -- create / edit form -------------------------------------------------

    def test_create_form_rejects_a_team_from_another_department(self):
        from accounts.forms import CustomUserCreationForm

        form = CustomUserCreationForm(data={
            "email": "new.hire@sebpo.com", "first_name": "New", "last_name": "Hire",
            "employee_id": "9001", "nid_number": "5556667778",
            "department": self.eng.pk, "designation": self.designation.pk,
            "team": self.mkt_team.pk,
            "joining_date": "2026-01-01", "probation_month": 6,
            "notice_period_months": 3, "contact_number": "0170000000",
            "password1": "testpass-1234!", "password2": "testpass-1234!",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("team", form.errors)

    def test_edit_form_offers_only_the_employees_own_department_teams(self):
        from accounts.forms import CustomUserUpdateForm

        self.ic1.team = self.eng_team
        self.ic1.save(update_fields=["team"])
        form = CustomUserUpdateForm(instance=self.ic1)
        self.assertIn(self.eng_team, form.fields["team"].queryset)
        self.assertNotIn(self.mkt_team, form.fields["team"].queryset)

    def test_edit_form_keeps_a_legacy_mismatch_selectable_and_saveable(self):
        """A pair that was already wrong on file must not block an edit that
        doesn't touch it — HR fixing a phone number shouldn't be held hostage
        to org data nobody is changing."""
        from accounts.forms import CustomUserUpdateForm

        self.ic2.team = self.mkt_team  # Marketing team on an Engineering user
        self.ic2.save(update_fields=["team"])
        form = CustomUserUpdateForm(instance=self.ic2)
        self.assertIn(self.mkt_team, form.fields["team"].queryset)

        form = CustomUserUpdateForm(instance=self.ic2, data={
            "email": self.ic2.email, "username": self.ic2.username,
            "first_name": self.ic2.first_name, "last_name": self.ic2.last_name,
            "department": self.eng.pk, "designation": self.designation.pk,
            "team": self.mkt_team.pk, "contact_number": "0171111111",
            "probation_month": 6, "is_active": True,
        })
        self.assertTrue(form.is_valid(), form.errors.as_text())


# ---------------------------------------------------------------------------
# Django admin — CustomUser changelist group filter
# ---------------------------------------------------------------------------

class CustomUserAdminGroupFilterTests(_Hierarchy):
    """`/admin/accounts/customuser/` filters by group and shows each row's groups."""

    url = "/admin/accounts/customuser/"

    def setUp(self):
        self.client.force_login(self.superuser)

    def _result_pks(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {u.pk for u in response.context["cl"].result_list}

    def test_group_filter_narrows_to_members(self):
        hr = Group.objects.get(name="HR")
        pks = self._result_pks(groups__id__exact=hr.pk)
        self.assertIn(self.hr_user.pk, pks)
        self.assertNotIn(self.admin_user.pk, pks)
        self.assertNotIn(self.ic1.pk, pks)

    def test_has_group_filter(self):
        loner = _user("loner@example.com", department=self.eng)
        loner.groups.clear()
        self.assertEqual(self._result_pks(has_group="no"), {loner.pk})
        with_group = self._result_pks(has_group="yes")
        self.assertIn(self.hr_user.pk, with_group)
        self.assertNotIn(loner.pk, with_group)

    def test_groups_column_lists_names_in_constant_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        response = self.client.get(self.url)
        self.assertContains(response, "All Employees, HR")

        # Prefetched: more rows (each with groups) must not add queries.
        with CaptureQueriesContext(connection) as before:
            self.client.get(self.url)
        for i in range(5):
            u = _user(f"extra{i}@example.com", department=self.eng)
            _join_group(u, "HR")
        with self.assertNumQueries(len(before.captured_queries)):
            self.client.get(self.url)
