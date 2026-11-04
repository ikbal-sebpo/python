"""Go Live bulk upload — a structured front end for employees.TeamAssignment
+ payroll.TeamSalaryPackage (2026-09-19), added 2026-09-21 so a Data
Solutions trainee's go-live no longer has to be typed as a free-text
Supervisor Note. See payroll/upload.py's Go Live section for the design
rationale (amount never typed, Trainee-to-Live only, org record and salary
proposal are separate guarantees).
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import Department, Team
from employees.models import JobInfo, TeamAssignment
from employees.signals import sync_team_assignment
from payroll.models import PayrollPeriod, SalaryStructure, TeamSalaryPackage
from payroll.upload import apply_go_live_upload, build_go_live_xlsx_template, parse_go_live_upload


User = get_user_model()

HEADER = "Employee ID,Team,Go-live Date"


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


def _csv(lines):
    return io.BytesIO("\n".join(lines).encode("utf-8"))


class GoLiveParseTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.other_department = Department.objects.create(name="General BPO")
        self.trainee_team = Team.objects.create(name="Trainee", department=self.department)
        self.mars = Team.objects.create(name="MARS", department=self.department)
        self.employee = _user(
            "a@example.com", "55043", department=self.department, team=self.trainee_team,
            joining_date=date(2026, 6, 8),
        )
        self.today = date(2026, 9, 19)

    def _parse(self, lines):
        return parse_go_live_upload(_csv([HEADER] + lines), "t.csv", today=self.today)

    def test_a_valid_row_is_parsed(self):
        parsed, errors, dups = self._parse(["55043,MARS,2026-09-07"])
        self.assertEqual((errors, dups), ([], []))
        self.assertEqual(parsed[0].employee_id, "55043")
        self.assertEqual(parsed[0].new_team, self.mars)
        self.assertEqual(parsed[0].go_live_date, date(2026, 9, 7))

    def test_a_blank_team_is_an_untouched_template_line(self):
        parsed, errors, _ = self._parse(["55043,,"])
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [])

    def test_unknown_employee_is_an_error(self):
        _, errors, _ = self._parse(["9999,MARS,2026-09-07"])
        self.assertEqual(len(errors), 1)

    def test_unknown_team_is_an_error(self):
        _, errors, _ = self._parse(["55043,Nonexistent Team,2026-09-07"])
        self.assertEqual(len(errors), 1)
        self.assertIn("No team", errors[0].message)

    def test_a_team_belonging_to_a_different_department_is_rejected(self):
        # accounts.Team.name is globally unique, so this proves the lookup
        # checks department membership rather than matching on name alone.
        other_team = Team.objects.create(name="Support", department=self.other_department)
        _, errors, _ = self._parse(["55043,Support,2026-09-07"])
        self.assertEqual(len(errors), 1)
        self.assertIn("No team", errors[0].message)

    def test_a_future_date_is_an_error(self):
        _, errors, _ = self._parse(["55043,MARS,2026-09-20"])
        self.assertEqual(len(errors), 1)
        self.assertIn("future", errors[0].message)

    def test_a_date_before_joining_is_an_error(self):
        _, errors, _ = self._parse(["55043,MARS,2026-01-01"])
        self.assertEqual(len(errors), 1)
        self.assertIn("joining", errors[0].message)

    def test_the_same_employee_twice_is_a_duplicate(self):
        _, _, dups = self._parse(["55043,MARS,2026-09-07", "55043,MARS,2026-09-08"])
        self.assertEqual(len(dups), 1)

    def test_template_has_the_expected_columns(self):
        import openpyxl

        body = build_go_live_xlsx_template(self.employee)
        ws = openpyxl.load_workbook(io.BytesIO(body))["Go live"]
        self.assertEqual([c.value for c in ws[1]], ["Employee ID", "Team", "Go-live Date"])


class GoLiveApplyTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.trainee_team = Team.objects.create(name="Trainee", department=self.department)
        self.mars = Team.objects.create(name="MARS", department=self.department)
        self.bvd = Team.objects.create(name="BVD", department=self.department)
        TeamSalaryPackage.objects.create(
            department=self.department, resource_status=JobInfo.ResourceStatus.TRAINEE,
            gross_monthly=Decimal("11000"), effective_from=date(2026, 1, 1),
        )
        TeamSalaryPackage.objects.create(
            department=self.department, team=self.mars,
            gross_monthly=Decimal("18000"), effective_from=date(2026, 1, 1),
        )
        TeamSalaryPackage.objects.create(
            department=self.department, team=None,
            gross_monthly=Decimal("16000"), effective_from=date(2026, 1, 1),
        )
        self.uploader = _user("sup@example.com", "1000", department=self.department)
        self.today = date(2026, 9, 19)

    def _trainee(self, email, employee_id, *, with_salary=True):
        employee = _user(
            email, employee_id, department=self.department, team=self.trainee_team,
            joining_date=date(2026, 6, 8),
        )
        JobInfo.objects.create(employee=employee, resource_status=JobInfo.ResourceStatus.TRAINEE)
        if with_salary:
            SalaryStructure.objects.create(
                employee=employee, effective_from=date(2026, 6, 8), effective_to=None,
                revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
                status=SalaryStructure.Status.APPROVED,
            )
        return employee

    def _apply(self, lines, visible_pks):
        parsed, errors, dups = parse_go_live_upload(
            _csv([HEADER] + lines), "t.csv", today=self.today,
        )
        self.assertEqual((errors, dups), ([], []))
        return apply_go_live_upload(self.uploader, parsed, visible_pks)

    def test_go_live_to_mars_updates_the_org_record_and_proposes_18000(self):
        employee = self._trainee("a@example.com", "55043")
        report = self._apply(["55043,MARS,2026-09-07"], {employee.pk})

        self.assertEqual(report.total_applied, 1)
        self.assertEqual(report.applied[0].package_amount, Decimal("18000"))
        self.assertTrue(report.applied[0].proposal_created)

        employee.refresh_from_db()
        self.assertEqual(employee.team, self.mars)
        self.assertEqual(employee.job_info.resource_status, JobInfo.ResourceStatus.LIVE)

        rows = list(TeamAssignment.objects.filter(employee=employee).order_by("effective_from"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].team, self.trainee_team)
        self.assertEqual(rows[0].effective_to, date(2026, 9, 6))
        self.assertEqual(rows[1].team, self.mars)
        self.assertEqual(rows[1].effective_from, date(2026, 9, 7))
        self.assertEqual(rows[1].reason, TeamAssignment.Reason.GO_LIVE)
        self.assertIsNone(rows[1].effective_to)

        proposal = SalaryStructure.objects.get(employee=employee, status=SalaryStructure.Status.PENDING)
        self.assertEqual(proposal.gross_monthly, Decimal("18000"))
        self.assertEqual(proposal.effective_from, date(2026, 9, 7))

    def test_go_live_to_any_other_team_resolves_the_department_wide_16000(self):
        employee = self._trainee("b@example.com", "55045")
        report = self._apply(["55045,BVD,2026-09-07"], {employee.pk})
        self.assertEqual(report.applied[0].package_amount, Decimal("16000"))
        proposal = SalaryStructure.objects.get(employee=employee, status=SalaryStructure.Status.PENDING)
        self.assertEqual(proposal.gross_monthly, Decimal("16000"))

    def test_an_employee_not_currently_trainee_is_skipped(self):
        employee = self._trainee("c@example.com", "55047")
        employee.job_info.resource_status = JobInfo.ResourceStatus.LIVE
        employee.job_info.save()
        report = self._apply(["55047,MARS,2026-09-07"], {employee.pk})
        self.assertEqual(len(report.skipped_not_trainee), 1)
        self.assertEqual(report.total_applied, 0)
        employee.refresh_from_db()
        self.assertEqual(employee.team, self.trainee_team)

    def test_out_of_scope_rows_are_skipped(self):
        employee = self._trainee("d@example.com", "55049")
        report = self._apply(["55049,MARS,2026-09-07"], set())
        self.assertEqual(len(report.skipped_scope), 1)

    def test_no_package_configured_is_skipped_not_silently_applied(self):
        other_department = Department.objects.create(name="General BPO")
        other_team = Team.objects.create(name="Support", department=other_department)
        employee = _user(
            "e@example.com", "55051", department=other_department, team=other_team,
            joining_date=date(2026, 6, 8),
        )
        JobInfo.objects.create(employee=employee, resource_status=JobInfo.ResourceStatus.TRAINEE)
        another_team = Team.objects.create(name="Support B", department=other_department)

        report = self._apply(["55051,Support B,2026-09-07"], {employee.pk})
        self.assertEqual(len(report.skipped_no_package), 1)
        self.assertEqual(report.total_applied, 0)
        employee.refresh_from_db()
        self.assertEqual(employee.team, other_team)

    def test_no_salary_structure_on_file_still_moves_the_org_record_but_proposes_nothing(self):
        employee = self._trainee("f@example.com", "55053", with_salary=False)
        report = self._apply(["55053,MARS,2026-09-07"], {employee.pk})

        self.assertEqual(report.total_applied, 1)
        self.assertFalse(report.applied[0].proposal_created)
        employee.refresh_from_db()
        self.assertEqual(employee.team, self.mars)
        self.assertEqual(employee.job_info.resource_status, JobInfo.ResourceStatus.LIVE)
        self.assertTrue(TeamAssignment.objects.filter(employee=employee, reason=TeamAssignment.Reason.GO_LIVE).exists())
        self.assertFalse(SalaryStructure.objects.filter(employee=employee).exists())

    def test_reupload_after_a_successful_go_live_is_a_safe_no_op(self):
        employee = self._trainee("g@example.com", "55055")
        self._apply(["55055,MARS,2026-09-07"], {employee.pk})
        self.assertEqual(TeamAssignment.objects.filter(employee=employee).count(), 2)
        self.assertEqual(SalaryStructure.objects.filter(employee=employee).count(), 2)

        report = self._apply(["55055,MARS,2026-09-07"], {employee.pk})
        self.assertEqual(len(report.skipped_not_trainee), 1)
        self.assertEqual(report.total_applied, 0)
        self.assertEqual(TeamAssignment.objects.filter(employee=employee).count(), 2)
        self.assertEqual(SalaryStructure.objects.filter(employee=employee).count(), 2)


class SyncTeamAssignmentEffectiveFromTests(TestCase):
    """The service-layer extension the go-live uploader depends on."""

    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.trainee_team = Team.objects.create(name="Trainee", department=self.department)
        self.mars = Team.objects.create(name="MARS", department=self.department)

    def test_an_explicit_effective_from_is_used_instead_of_today(self):
        employee = _user(
            "a@example.com", "1", department=self.department, team=self.trainee_team,
            joining_date=date(2026, 6, 8),
        )
        User.objects.filter(pk=employee.pk).update(team=self.mars)
        employee.team = self.mars

        assignment = sync_team_assignment(
            employee, reason=TeamAssignment.Reason.GO_LIVE, effective_from=date(2026, 9, 7),
        )
        self.assertEqual(assignment.effective_from, date(2026, 9, 7))
        self.assertEqual(assignment.reason, TeamAssignment.Reason.GO_LIVE)

        prior = TeamAssignment.objects.get(employee=employee, team=self.trainee_team)
        self.assertEqual(prior.effective_to, date(2026, 9, 6))

    def test_omitting_effective_from_still_defaults_to_today(self):
        employee = _user("b@example.com", "2", department=self.department, team=self.trainee_team)
        from django.utils import timezone

        User.objects.filter(pk=employee.pk).update(team=self.mars)
        employee.team = self.mars
        assignment = sync_team_assignment(employee)
        self.assertEqual(assignment.effective_from, timezone.localdate())
