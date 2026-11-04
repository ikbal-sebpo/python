"""Tests for department-scoped ShiftAttribute dropdown visibility."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department
from employees.models import JobInfo
from roster import services
from roster.forms import RosterPatternRowForm
from roster.models import Roster, ShiftAttribute, ShiftType


User = get_user_model()


def _ensure_groups():
    for name in ("User Admins", "HR", "All Employees", "Senior Leadership"):
        Group.objects.get_or_create(name=name)


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        **extra,
    )


class VisibleShiftTypesTests(TestCase):
    """Direct tests of the shared services.visible_shift_types() filter."""

    @classmethod
    def setUpTestData(cls):
        cls.dept_a = Department.objects.create(name="Digital Advertising")
        cls.dept_b = Department.objects.create(name="Data Solutions and Due Diligence")

        cls.eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK, has_attribute=True,
        )
        cls.attr_global = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 1-10", is_active=True, sort_order=0,
        )
        cls.attr_dept_a = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 2-11", is_active=True, sort_order=0,
        )
        cls.attr_dept_a.departments.add(cls.dept_a)
        cls.attr_dept_b = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 3-11", is_active=True, sort_order=0,
        )
        cls.attr_dept_b.departments.add(cls.dept_b)

    def _attr_names(self, department_id=None, include_attr_ids=()):
        names = set()
        for st in services.visible_shift_types(department_id, include_attr_ids):
            names |= {a.name for a in st.attributes.all()}
        return names

    def test_no_department_id_shows_everything(self):
        self.assertEqual(
            self._attr_names(None),
            {"EVE 1-10", "EVE 2-11", "EVE 3-11"},
        )

    def test_department_a_sees_global_plus_own(self):
        self.assertEqual(
            self._attr_names(self.dept_a.pk),
            {"EVE 1-10", "EVE 2-11"},
        )

    def test_department_b_does_not_see_department_a_exclusive(self):
        names = self._attr_names(self.dept_b.pk)
        self.assertNotIn("EVE 2-11", names)
        self.assertIn("EVE 1-10", names)
        self.assertIn("EVE 3-11", names)

    def test_include_attr_ids_forces_inclusion_despite_scope(self):
        names = self._attr_names(self.dept_b.pk, include_attr_ids={self.attr_dept_a.pk})
        self.assertIn("EVE 2-11", names)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class CellUpdateScopingTests(TestCase):
    """Server-side enforcement: a tampered POST can't sneak in an
    out-of-scope attribute just because it happens to be `is_active`."""

    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.dept_a = Department.objects.create(name="Digital Advertising")
        cls.dept_b = Department.objects.create(name="Data Solutions and Due Diligence")

        cls.manager = _user("mgr@example.com", department=cls.dept_a)
        cls.report = _user("rep@example.com", department=cls.dept_a)
        JobInfo.objects.create(employee=cls.report, reports_to=cls.manager)

        cls.eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK, has_attribute=True,
        )
        cls.attr_dept_b = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 3-11", is_active=True, sort_order=0,
        )
        cls.attr_dept_b.departments.add(cls.dept_b)

    def test_manager_cannot_assign_other_departments_exclusive_attribute(self):
        self.client.force_login(self.manager)
        url = reverse("roster_cell_update", args=[self.report.pk, "2026-09-07"])
        resp = self.client.post(url, {"shift_ref": f"attr:{self.attr_dept_b.pk}"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(
            self.report.shift_set.filter(date="2026-09-07").exists()
            if hasattr(self.report, "shift_set") else False
        )

    def test_attribute_linked_to_many_departments_does_not_multiply(self):
        # Bug found in prod: an attribute scoped to several departments joins
        # once per linked department; without .distinct() the pk__in escape
        # hatch (include_attr_ids) matched every joined row and get()
        # raised ShiftAttribute.MultipleObjectsReturned instead of resolving.
        multi_dept_attr = ShiftAttribute.objects.create(
            shift_type=self.eve, name="EVE many-dept", is_active=True, sort_order=0,
        )
        other_depts = [
            Department.objects.create(name=f"Extra Dept {i}") for i in range(4)
        ]
        multi_dept_attr.departments.add(self.dept_a, *other_depts)

        self.client.force_login(self.manager)
        url = reverse("roster_cell_update", args=[self.report.pk, "2026-09-07"])
        resp = self.client.post(url, {"shift_ref": f"attr:{multi_dept_attr.pk}"})
        self.assertEqual(resp.status_code, 200)

        # Resubmitting the row's own already-assigned value (the
        # include_attr_ids "unchanged" path) must not crash either.
        resp = self.client.post(url, {"shift_ref": f"attr:{multi_dept_attr.pk}"})
        self.assertEqual(resp.status_code, 200)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class RosterPatternRowFormScopingTests(TestCase):
    """The bug found during review: a row's own current value must always
    stay selectable, or an untouched weekday round-trips as blank and gets
    deleted on save."""

    @classmethod
    def setUpTestData(cls):
        cls.dept_a = Department.objects.create(name="Digital Advertising")
        cls.dept_b = Department.objects.create(name="Data Solutions and Due Diligence")
        cls.employee = _user("emp@example.com", department=cls.dept_a)

        cls.eve = ShiftType.objects.create(
            name="EVE", category=ShiftType.Category.WORK, has_attribute=True,
        )
        cls.attr = ShiftAttribute.objects.create(
            shift_type=cls.eve, name="EVE 3-11", is_active=True, sort_order=0,
        )
        # Scoped to dept_b only — out of scope for cls.employee (dept_a).
        cls.attr.departments.add(cls.dept_b)

    def test_out_of_scope_current_value_still_renders_selected(self):
        form = RosterPatternRowForm(
            initial={"weekday": 0, "shift_ref": f"attr:{self.attr.pk}"},
            employee=self.employee,
        )
        rendered = str(form["shift_ref"])
        self.assertIn(f'value="attr:{self.attr.pk}"', rendered)
        self.assertIn("selected", rendered)

    @override_settings(ROOT_URLCONF="core.urls")
    def test_saving_pattern_does_not_delete_out_of_scope_untouched_row(self):
        """End-to-end regression for the exact data-loss scenario: Monday is
        assigned an attribute later scoped away from the employee's
        department; saving the form after only touching Tuesday must not
        wipe Monday's row."""
        off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        Roster.objects.create(
            employee=self.employee, weekday=0,
            shift_type=self.eve, shift_attribute=self.attr,
        )
        for wd in range(1, 7):
            Roster.objects.create(employee=self.employee, weekday=wd, shift_type=off)

        manager = _user("mgr2@example.com", department=self.dept_a)
        JobInfo.objects.create(employee=self.employee, reports_to=manager)
        _ensure_groups()
        self.client.force_login(manager)

        get_resp = self.client.get(reverse("roster_pattern", args=[self.employee.pk]))
        self.assertEqual(get_resp.status_code, 200)
        formset = get_resp.context["formset"]

        data = {
            "form-TOTAL_FORMS": "7", "form-INITIAL_FORMS": "7",
            "form-MIN_NUM_FORMS": "7", "form-MAX_NUM_FORMS": "7",
        }
        for i, form in enumerate(formset.forms):
            for field in ("weekday", "shift_ref", "location"):
                data[f"form-{i}-{field}"] = form.initial.get(field, "")

        post_resp = self.client.post(
            reverse("roster_pattern", args=[self.employee.pk]), data,
        )
        self.assertEqual(post_resp.status_code, 302)

        monday = Roster.objects.filter(employee=self.employee, weekday=0).first()
        self.assertIsNotNone(monday, "Monday's row was deleted — out-of-scope value was lost on save.")
        self.assertEqual(monday.shift_attribute_id, self.attr.pk)
