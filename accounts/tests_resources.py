"""Unit tests for accounts.resources — django-import-export Team resource."""

from __future__ import annotations

import tablib
from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import Department, Team
from accounts.resources import TeamResource

User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        employee_id=employee_id, **extra,
    )


class TeamResourceAdditionalManagersTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Digital Advertising")
        self.team = Team.objects.create(name="AdCellerant", department=self.dept)
        self.tl = _user("acting-tl@example.com", "1095")
        self.other_manager = _user("other-manager@example.com", "999")
        self.team.additional_managers.add(self.other_manager)

    def _import(self, employee_id, dry_run=False):
        ds = tablib.Dataset(
            ("AdCellerant", str(employee_id)),
            headers=["name", "additional_managers"],
        )
        return TeamResource().import_data(ds, dry_run=dry_run, raise_errors=True)

    def test_import_adds_manager_without_replacing_existing(self):
        self._import(self.tl.employee_id)
        self.team.refresh_from_db()
        managers = set(self.team.additional_managers.all())
        self.assertEqual(managers, {self.tl, self.other_manager})

    def test_dry_run_does_not_persist(self):
        result = self._import(self.tl.employee_id, dry_run=True)
        self.assertFalse(result.has_errors())
        self.team.refresh_from_db()
        self.assertEqual(list(self.team.additional_managers.all()), [self.other_manager])

    def test_reimporting_same_manager_is_idempotent(self):
        self._import(self.tl.employee_id)
        self._import(self.tl.employee_id)
        self.team.refresh_from_db()
        self.assertEqual(self.team.additional_managers.count(), 2)
