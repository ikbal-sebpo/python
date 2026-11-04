"""Tests for ``manage.py merge_reference`` — folding a duplicate Department /
JobLocation / Designation into the real row.

The scenario mirrors the real 2026-09-28 incident: SSO auto-created a
"Human Resource" department (and a street-address JobLocation) for an intern,
HR later moved them to "Human Resources" through the profile form, and the
fake transfer that left in ``TeamAssignment`` must fold back into one row.
"""

from __future__ import annotations

from datetime import date
from io import StringIO
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.management.commands.merge_reference import collapse_plan, discover_references
from accounts.models import Department, Designation, JobLocation, Team
from employees.models import DesignationHistory, TeamAssignment
from roster.models import ShiftAttribute

User = get_user_model()


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T", **extra,
    )


def _merge(kind, source, target, **opts):
    out = StringIO()
    call_command("merge_reference", kind, source=source, target=target, stdout=out, **opts)
    return out.getvalue()


class CollapsePlanTests(TestCase):
    """The pure planner the dry run and the write share."""

    @staticmethod
    def _row(pk, dept, frm, to, team=None, status=""):
        return SimpleNamespace(
            pk=pk, department_id=dept, team_id=team, resource_status=status,
            effective_from=frm, effective_to=to, reason="X",
        )

    IDENTITY = ("department_id", "team_id", "resource_status")

    def test_contiguous_identical_rows_fold(self):
        a = self._row(1, 14, date(2026, 9, 28), date(2026, 10, 9))
        b = self._row(2, 14, date(2026, 10, 10), None)
        plan = collapse_plan([b, a], self.IDENTITY)
        self.assertEqual([(k.pk, d.pk) for k, d in plan], [(1, 2)])

    def test_gap_or_different_identity_does_not_fold(self):
        a = self._row(1, 14, date(2026, 9, 28), date(2026, 10, 9))
        gap = self._row(2, 14, date(2026, 10, 12), None)
        self.assertEqual(collapse_plan([a, gap], self.IDENTITY), [])
        other_team = self._row(3, 14, date(2026, 10, 10), None, team=7)
        self.assertEqual(collapse_plan([a, other_team], self.IDENTITY), [])

    def test_three_contiguous_rows_fold_into_the_first(self):
        a = self._row(1, 14, date(2026, 1, 1), date(2026, 1, 31))
        b = self._row(2, 14, date(2026, 2, 1), date(2026, 2, 28))
        c = self._row(3, 14, date(2026, 3, 1), None)
        plan = collapse_plan([a, b, c], self.IDENTITY)
        self.assertEqual([(k.pk, d.pk) for k, d in plan], [(1, 2), (1, 3)])


class DiscoverReferencesTests(TestCase):
    def test_finds_hidden_protect_fk_and_m2m(self):
        fks, m2ms = discover_references(Department)
        fk_labels = {f"{r.model._meta.label}.{r.field_name}" for r in fks}
        # related_name="+" hides this one from Department._meta.related_objects.
        self.assertIn("employees.TeamAssignment.department", fk_labels)
        self.assertIn("accounts.CustomUser.department", fk_labels)
        self.assertIn("accounts.Team.department", fk_labels)
        self.assertEqual({f"{r.model._meta.label}.{r.field_name}" for r in m2ms},
                         {"roster.ShiftAttribute.departments"})


class MergeDepartmentTests(TestCase):
    def setUp(self):
        self.real = Department.objects.create(name="Human Resources")
        self.dup = Department.objects.create(name="Human Resource", description="from Entra")
        self.intern = _user("intern.hr01@sebpo.com", department=self.dup, joining_date=date(2026, 9, 28))
        # HR later "moved" them to the real department through the profile form.
        self.intern.department = self.real
        self.intern.save()
        self.stayer = _user("stayer@sebpo.com", department=self.dup)
        self.team = Team.objects.create(name="HR Ops", department=self.dup)
        self.attr_both = ShiftAttribute.objects.create(name="Both")
        self.attr_both.departments.add(self.dup, self.real)
        self.attr_dup = ShiftAttribute.objects.create(name="Dup only")
        self.attr_dup.departments.add(self.dup)

    def test_signal_left_a_fake_transfer(self):
        rows = list(TeamAssignment.objects.filter(employee=self.intern).order_by("effective_from"))
        self.assertEqual([(r.department_id, r.effective_to is None) for r in rows],
                         [(self.dup.pk, False), (self.real.pk, True)])

    def test_dry_run_changes_nothing(self):
        out = _merge("department", "Human Resource", "Human Resources")
        self.assertIn("Dry-run only", out)
        self.assertIn("accounts.CustomUser.department: 1 row(s)", out)
        self.assertIn("accounts.Team.department: 1 row(s)", out)
        self.assertIn("roster.ShiftAttribute.departments (M2M): 2 link(s) (1 already linked", out)
        self.assertIn("TeamAssignment rows that become a no-op transfer", out)
        self.assertTrue(Department.objects.filter(pk=self.dup.pk).exists())
        self.assertEqual(TeamAssignment.objects.filter(employee=self.intern).count(), 2)
        self.stayer.refresh_from_db()
        self.assertEqual(self.stayer.department_id, self.dup.pk)

    def test_apply_retargets_folds_history_and_deletes_duplicate(self):
        out = _merge("department", "Human Resource", "Human Resources", apply=True)
        self.assertIn("deleted duplicate", out)
        self.assertFalse(Department.objects.filter(pk=self.dup.pk).exists())

        self.stayer.refresh_from_db()
        self.team.refresh_from_db()
        self.assertEqual(self.stayer.department_id, self.real.pk)
        self.assertEqual(self.team.department_id, self.real.pk)

        # M2M: moved where missing, de-duplicated where the target was already linked.
        self.assertEqual(list(self.attr_both.departments.all()), [self.real])
        self.assertEqual(list(self.attr_dup.departments.all()), [self.real])

        # The intern's fake transfer is one row again: original start, INITIAL, open.
        rows = list(TeamAssignment.objects.filter(employee=self.intern))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.department_id, self.real.pk)
        self.assertEqual(row.effective_from, date(2026, 9, 28))
        self.assertIsNone(row.effective_to)
        self.assertEqual(row.reason, TeamAssignment.Reason.INITIAL)

        # The stayer's single row simply re-points; nothing to fold.
        stayer_rows = TeamAssignment.objects.filter(employee=self.stayer)
        self.assertEqual([r.department_id for r in stayer_rows], [self.real.pk])

        # Target's own fields win; only a blank description is filled from the duplicate.
        self.real.refresh_from_db()
        self.assertEqual(self.real.description, "from Entra")

    def test_keep_source_deactivates_instead_of_deleting(self):
        _merge("department", "id:%d" % self.dup.pk, "id:%d" % self.real.pk, apply=True, keep_source=True)
        self.dup.refresh_from_db()
        self.assertFalse(self.dup.is_active)
        self.assertEqual(User.objects.filter(department=self.dup).count(), 0)

    def test_rejects_same_row_unknown_name_and_ambiguity(self):
        with self.assertRaises(CommandError):
            _merge("department", "Human Resources", "human resources")
        with self.assertRaises(CommandError):
            _merge("department", "Nope", "Human Resources")
        with self.assertRaises(CommandError):
            _merge("department", "id:abc", "Human Resources")


class MergeLocationTests(TestCase):
    def test_location_merge_moves_users_and_fills_blank_description(self):
        mbd = JobLocation.objects.create(name="MBD", code="MBD")
        dup = JobLocation.objects.create(name="Monem Business District, Level-7 Dhaka 1205 BD")
        u = _user("someone@sebpo.com", job_location=dup)
        _merge("location", dup.name, "mbd", apply=True)
        u.refresh_from_db()
        mbd.refresh_from_db()
        self.assertEqual(u.job_location_id, mbd.pk)
        self.assertFalse(JobLocation.objects.filter(pk=dup.pk).exists())
        self.assertEqual(mbd.description, "")  # duplicate had no description to donate


class MergeDesignationTests(TestCase):
    def test_designation_merge_folds_designation_history(self):
        real = Designation.objects.create(name="Senior Executive")
        dup = Designation.objects.create(name="Sr. Executive")
        u = _user("exec@sebpo.com", designation=dup, joining_date=date(2026, 1, 1))
        u.designation = real
        u.save()
        self.assertEqual(DesignationHistory.objects.filter(employee=u).count(), 2)

        _merge("designation", "Sr. Executive", "Senior Executive", apply=True)

        rows = list(DesignationHistory.objects.filter(employee=u))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].designation_id, real.pk)
        self.assertEqual(rows[0].effective_from, date(2026, 1, 1))
        self.assertIsNone(rows[0].effective_to)
        self.assertFalse(Designation.objects.filter(pk=dup.pk).exists())
