"""Phase B — effective-dated team assignment + team salary packages.

docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase B. The payoff is the last test:
HR moves a trainee onto MARS, and payroll ends up paying 22 days at 11,000
plus 9 days at 18,000 without anyone typing a number or a date.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import Department, Team
from employees.models import JobInfo, TeamAssignment
from employees.signals import sync_team_assignment
from payroll.engine import calculate_run
from payroll.models import (
    PayComponent, PayrollControlCheck, PayrollPeriod, PayrollRunEmployee, PayrollRunLine,
    SalaryStructure, TeamSalaryPackage,
)
from payroll.rates import active_team_salary_packages, resolve_team_salary_package
from payroll.services import (
    approve_salary_structure, create_run, lock_inputs, propose_salary_for_assignment,
    reject_salary_structure,
)


User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


class TeamAssignmentSignalTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.trainee_team = Team.objects.create(name="Trainee", department=self.department)
        self.mars = Team.objects.create(name="MARS", department=self.department)

    def test_a_new_employee_gets_an_initial_row_backdated_to_joining(self):
        employee = _user(
            "a@example.com", "1", department=self.department, team=self.trainee_team,
            joining_date=date(2026, 6, 8),
        )
        assignment = TeamAssignment.objects.get(employee=employee)
        self.assertEqual(assignment.reason, TeamAssignment.Reason.INITIAL)
        self.assertEqual(assignment.effective_from, date(2026, 6, 8))
        self.assertEqual(assignment.team, self.trainee_team)
        self.assertIsNone(assignment.effective_to)

    def test_changing_team_closes_the_old_row_and_opens_a_new_one(self):
        employee = _user(
            "a@example.com", "1", department=self.department, team=self.trainee_team,
            joining_date=date(2026, 6, 8),
        )
        # Simulate the row having been opened before today so the change
        # genuinely stacks rather than amending in place.
        TeamAssignment.objects.filter(employee=employee).update(
            effective_from=timezone.localdate() - timedelta(days=30),
        )
        employee.team = self.mars
        employee.save()

        rows = list(TeamAssignment.objects.filter(employee=employee).order_by("effective_from"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].team, self.trainee_team)
        self.assertEqual(rows[0].effective_to, timezone.localdate() - timedelta(days=1))
        self.assertEqual(rows[1].team, self.mars)
        self.assertIsNone(rows[1].effective_to)

    def test_a_same_day_second_change_amends_rather_than_stacking(self):
        """A go-live touches CustomUser.team AND JobInfo.resource_status.
        Without the in-place amend that would leave a one-day orphan."""
        employee = _user("a@example.com", "1", department=self.department, team=self.trainee_team)
        JobInfo.objects.create(employee=employee, resource_status=JobInfo.ResourceStatus.TRAINEE)
        employee.team = self.mars
        employee.save()
        job_info = employee.job_info
        job_info.resource_status = JobInfo.ResourceStatus.LIVE
        job_info.save()

        rows = list(TeamAssignment.objects.filter(employee=employee))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].team, self.mars)
        self.assertEqual(rows[0].resource_status, JobInfo.ResourceStatus.LIVE)

    def test_saving_without_an_org_change_is_a_no_op(self):
        employee = _user("a@example.com", "1", department=self.department, team=self.trainee_team)
        employee.first_name = "Renamed"
        employee.save()
        self.assertEqual(TeamAssignment.objects.filter(employee=employee).count(), 1)

    def test_sync_is_idempotent_when_called_directly(self):
        employee = _user("a@example.com", "1", department=self.department, team=self.trainee_team)
        first = sync_team_assignment(employee)
        second = sync_team_assignment(employee)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TeamAssignment.objects.filter(employee=employee).count(), 1)


class TeamSalaryPackageResolutionTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.mars = Team.objects.create(name="MARS", department=self.department)
        self.item_review = Team.objects.create(name="Item Review", department=self.department)
        self.on = date(2026, 9, 7)
        self.trainee_pkg = TeamSalaryPackage.objects.create(
            department=self.department, resource_status=JobInfo.ResourceStatus.TRAINEE,
            gross_monthly=Decimal("11000"), effective_from=date(2026, 1, 1),
        )
        self.mars_pkg = TeamSalaryPackage.objects.create(
            department=self.department, team=self.mars,
            gross_monthly=Decimal("18000"), effective_from=date(2026, 1, 1),
        )
        TeamSalaryPackage.objects.create(
            department=self.department, team=self.item_review,
            gross_monthly=Decimal("16000"), effective_from=date(2026, 1, 1),
        )

    def _resolve(self, **kwargs):
        return resolve_team_salary_package(on_date=self.on, department=self.department, **kwargs)

    def test_team_scoped_package_wins_for_a_live_employee(self):
        self.assertEqual(self._resolve(team=self.mars).gross_monthly, Decimal("18000"))
        self.assertEqual(self._resolve(team=self.item_review).gross_monthly, Decimal("16000"))

    def test_trainee_status_beats_the_team_they_already_sit_on(self):
        """A trainee parked on MARS before go-live is still on 11,000."""
        resolved = self._resolve(team=self.mars, resource_status=JobInfo.ResourceStatus.TRAINEE)
        self.assertEqual(resolved.gross_monthly, Decimal("11000"))

    def test_no_department_resolves_to_nothing(self):
        self.assertIsNone(resolve_team_salary_package(on_date=self.on, department=None))

    def test_a_team_with_no_package_falls_through_to_nothing(self):
        other = Team.objects.create(name="BVD", department=self.department)
        self.assertIsNone(self._resolve(team=other))

    def test_expired_package_is_not_resolved(self):
        self.mars_pkg.effective_to = date(2026, 8, 1)
        self.mars_pkg.save()
        self.assertIsNone(self._resolve(team=self.mars))

    def test_the_prefetched_path_agrees_with_the_querying_path(self):
        """The engine resolves per employee from a pre-loaded list to keep
        its query count flat — the two must not diverge."""
        packages = active_team_salary_packages(self.on)
        for team, status in (
            (self.mars, ""), (self.item_review, ""),
            (self.mars, JobInfo.ResourceStatus.TRAINEE), (None, ""),
        ):
            queried = self._resolve(team=team, resource_status=status)
            prefetched = resolve_team_salary_package(
                on_date=self.on, department=self.department, team=team,
                resource_status=status, packages=packages,
            )
            self.assertEqual(
                getattr(queried, "pk", None), getattr(prefetched, "pk", None),
                f"diverged for team={team} status={status!r}",
            )


class SalaryProposalTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.mars = Team.objects.create(name="MARS", department=self.department)
        TeamSalaryPackage.objects.create(
            department=self.department, team=self.mars,
            gross_monthly=Decimal("18000"), effective_from=date(2026, 1, 1),
        )
        self.employee = _user(
            "a@example.com", "1", department=self.department, joining_date=date(2026, 6, 8),
        )
        SalaryStructure.objects.create(
            employee=self.employee, effective_from=date(2026, 6, 8), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
            status=SalaryStructure.Status.APPROVED,
        )

    def _assignment(self, **extra):
        return TeamAssignment.objects.create(
            employee=self.employee, department=self.department, team=self.mars,
            effective_from=date(2026, 9, 7), reason=TeamAssignment.Reason.GO_LIVE, **extra,
        )

    def test_a_go_live_proposes_a_pending_revision(self):
        proposal = propose_salary_for_assignment(self._assignment())
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.status, SalaryStructure.Status.PENDING)
        self.assertEqual(proposal.gross_monthly, Decimal("18000"))
        self.assertEqual(proposal.effective_from, date(2026, 9, 7))

    def test_a_proposal_does_not_change_what_they_are_paid(self):
        propose_salary_for_assignment(self._assignment())
        current = SalaryStructure.objects.get(
            employee=self.employee, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
        )
        self.assertEqual(current.gross_monthly, Decimal("11000"))

    def test_no_proposal_when_they_are_already_on_the_package(self):
        SalaryStructure.objects.filter(employee=self.employee).update(gross_monthly=Decimal("18000"))
        self.assertIsNone(propose_salary_for_assignment(self._assignment()))

    def test_proposing_twice_does_not_stack(self):
        assignment = self._assignment()
        first = propose_salary_for_assignment(assignment)
        second = propose_salary_for_assignment(assignment)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            SalaryStructure.objects.filter(status=SalaryStructure.Status.PENDING).count(), 1,
        )

    def test_approving_supersedes_the_old_row(self):
        proposal = propose_salary_for_assignment(self._assignment())
        self.assertTrue(approve_salary_structure(proposal))
        proposal.refresh_from_db()
        old = SalaryStructure.objects.get(revision_no=1)
        self.assertEqual(proposal.status, SalaryStructure.Status.APPROVED)
        self.assertEqual(proposal.revision_no, 2)
        self.assertEqual(old.status, SalaryStructure.Status.SUPERSEDED)
        self.assertEqual(old.effective_to, date(2026, 9, 6))

    def test_approving_twice_is_refused(self):
        proposal = propose_salary_for_assignment(self._assignment())
        self.assertTrue(approve_salary_structure(proposal))
        self.assertFalse(approve_salary_structure(proposal))

    def test_rejecting_leaves_pay_untouched(self):
        proposal = propose_salary_for_assignment(self._assignment())
        self.assertTrue(reject_salary_structure(proposal))
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, SalaryStructure.Status.CANCELLED)
        current = SalaryStructure.objects.get(
            employee=self.employee, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
        )
        self.assertEqual(current.gross_monthly, Decimal("11000"))


class GoLiveEndToEndTests(TestCase):
    """The whole point of Phase A + B, in one test."""

    def setUp(self):
        PayComponent.objects.create(
            code="GROSS_SALARY", name="Gross salary", kind=PayComponent.Kind.EARNING,
            entry_mode=PayComponent.EntryMode.MASTER, is_prorated=True,
        )
        self.department = Department.objects.create(name="Data solution and Due Diligence")
        self.trainee_team = Team.objects.create(name="Trainee", department=self.department)
        self.mars = Team.objects.create(name="MARS", department=self.department)
        TeamSalaryPackage.objects.create(
            department=self.department, resource_status=JobInfo.ResourceStatus.TRAINEE,
            gross_monthly=Decimal("11000"), effective_from=date(2026, 1, 1),
        )
        TeamSalaryPackage.objects.create(
            department=self.department, team=self.mars,
            gross_monthly=Decimal("18000"), effective_from=date(2026, 1, 1),
        )
        self.period = PayrollPeriod.for_month(2026, 9)   # 16 Aug - 15 Sep

    def test_hr_moves_a_trainee_to_mars_and_payroll_splits_the_period(self):
        employee = _user(
            "trainee@example.com", "55043", department=self.department,
            team=self.trainee_team, joining_date=date(2026, 6, 8),
        )
        JobInfo.objects.create(employee=employee, resource_status=JobInfo.ResourceStatus.TRAINEE)
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2026, 6, 8), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("11000"),
            status=SalaryStructure.Status.APPROVED,
        )

        # HR's actual action: move them to MARS and mark them Live. The
        # signal records the dated assignment and payroll proposes the
        # revision — nobody types 18,000 or a date.
        employee.team = self.mars
        employee.save()
        employee.job_info.resource_status = JobInfo.ResourceStatus.LIVE
        employee.job_info.save()

        proposal = SalaryStructure.objects.get(
            employee=employee, status=SalaryStructure.Status.PENDING,
        )
        self.assertEqual(proposal.gross_monthly, Decimal("18000"))

        # HR is usually a few days late, so Finance backdates to the real
        # go-live before approving — the one number a human still supplies.
        proposal.effective_from = date(2026, 9, 7)
        proposal.save()
        self.assertTrue(approve_salary_structure(proposal))

        run = create_run(self.period, None)
        lock_inputs(run, None)
        calculate_run(run.pk)

        row = PayrollRunEmployee.objects.get(run=run, employee=employee)
        lines = list(
            PayrollRunLine.objects.filter(run_employee=row, component_code="GROSS_SALARY")
            .order_by("sequence")
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].full_amount, Decimal("11000.00"))
        self.assertEqual(lines[1].full_amount, Decimal("18000.00"))
        self.assertEqual(row.gross_earnings, Decimal("13032.26"))
        # Not the 18,000 the pre-Phase-A engine would have paid.
        self.assertNotEqual(row.gross_earnings, Decimal("18000.00"))

    def test_an_off_package_salary_is_flagged_not_blocked(self):
        employee = _user(
            "odd@example.com", "55044", department=self.department, team=self.mars,
            joining_date=date(2026, 1, 1),
        )
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2026, 1, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("25000"),
            status=SalaryStructure.Status.APPROVED,
        )
        run = create_run(self.period, None)
        lock_inputs(run, None)
        calculate_run(run.pk)

        check = PayrollControlCheck.objects.get(run=run, check_code="SALARY_PACKAGE_MISMATCH")
        self.assertEqual(check.severity, PayrollControlCheck.Severity.WARNING)
        self.assertEqual(check.employee, employee)
        self.assertIn("25000", check.message)
        # Still paid — a mismatch is information, not a block.
        row = PayrollRunEmployee.objects.get(run=run, employee=employee)
        self.assertEqual(row.gross_earnings, Decimal("25000.00"))

    def test_an_on_package_salary_raises_no_check(self):
        employee = _user(
            "ok@example.com", "55045", department=self.department, team=self.mars,
            joining_date=date(2026, 1, 1),
        )
        SalaryStructure.objects.create(
            employee=employee, effective_from=date(2026, 1, 1), effective_to=None,
            revision_no=1, reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("18000"),
            status=SalaryStructure.Status.APPROVED,
        )
        run = create_run(self.period, None)
        lock_inputs(run, None)
        calculate_run(run.pk)
        self.assertFalse(
            PayrollControlCheck.objects.filter(
                run=run, check_code="SALARY_PACKAGE_MISMATCH", employee=employee,
            ).exists()
        )
