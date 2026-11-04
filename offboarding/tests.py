"""Tests for the offboarding app.

Coverage:
  - Resignation lifecycle: submit → HR accept (case + steps materialised from
    the workflow template) / reject; employee withdraw while PENDING.
  - Clearance workflow sequencing: sequential enforcement in
    ``can_act_on_clearance_step``, step completion advancing
    ``current_step_order``, required-step rejection cancelling the case, and
    the HR-final step triggering ``auto_issue_letters`` (asserted at the
    DocumentRequest boundary — no PDF rendering).
  - Permissions: ``can_view_case`` scoping + ClearanceQueueView filtering.
  - LastWorkingDateMiddleware: deactivate + logout on past LWD, allowlisted
    routes, LWD today/future unaffected.
  - ``deactivate_past_lwd`` management command.
  - HR create-on-behalf flow (/offboarding/create/).

Run with:
  venv/bin/python manage.py test offboarding --settings=core.settings_test

Notes on signal timing: the resignation-accept and step-save signals do their
work inside ``transaction.on_commit``, so state-changing operations are
wrapped in ``self.captureOnCommitCallbacks(execute=True)``.
"""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import DocumentRequest, DocumentType
from employees.models import JobInfo
from offboarding.models import (
    ClearanceStep,
    ClearanceStepTemplate,
    ClearanceWorkflowTemplate,
    OffboardingCase,
    Resignation,
)
from offboarding.permissions import (
    can_act_on_clearance_step,
    can_view_case,
)
from offboarding.services import (
    auto_issue_letters,
    create_case_from_resignation,
)


User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers + shared fixture
# ---------------------------------------------------------------------------

def _user(email, **extra):
    return User.objects.create_user(
        email=email,
        username=email,
        password="test-1234!",
        first_name=email.split("@")[0].title(),
        last_name="Test",
        **extra,
    )


def _join_group(user, name):
    user.groups.add(Group.objects.get(name=name))


@override_settings(OFFBOARDING_NOTIFICATIONS_ENABLED=True)
class _Base(TestCase):
    """Org + workflow fixture used across the suite.

    Pins OFFBOARDING_NOTIFICATIONS_ENABLED=True: core/settings.py reads it
    from .env, so without this a developer who has paused offboarding mail
    locally would see the mail-asserting tests fail for an environmental
    reason that says nothing about the code. Subclasses that need it off
    (NotificationKillSwitchTests) override it back — a subclass's
    override_settings wins over the parent's.

    People:
        manager  — line manager of employee (via JobInfo.reports_to)
        employee — the departing employee
        hr_user  — HR group (edit-all scope; also the HR_HEAD resolution target)
        it_user  — IT clearance group member
        fin_user — Finance group member (no step in the test workflow)
        outsider — no groups, no relationship

    Workflow (is_default, all separation types):
        1. MANAGER_ACK — assigned_kind=MANAGER  → resolves to manager
        2. IT          — assigned_kind=GROUP    → IT group
        3. HR_FINAL    — assigned_kind=HR_HEAD  → resolves to hr_user,
                         triggers_letters=True
    """

    @classmethod
    def setUpTestData(cls):
        for name in ("User Admins", "HR", "IT", "Finance", "Senior Leadership"):
            Group.objects.get_or_create(name=name)
        cls.hr_group = Group.objects.get(name="HR")
        cls.it_group = Group.objects.get(name="IT")

        cls.manager = _user("manager@example.com")
        cls.employee = _user("employee@example.com")
        JobInfo.objects.create(employee=cls.employee, reports_to=cls.manager)

        cls.hr_user = _user("hr@example.com")
        _join_group(cls.hr_user, "HR")
        cls.it_user = _user("it@example.com")
        _join_group(cls.it_user, "IT")
        cls.fin_user = _user("finance@example.com")
        _join_group(cls.fin_user, "Finance")
        cls.sr_leader = _user("cxo@example.com")
        _join_group(cls.sr_leader, "Senior Leadership")
        cls.outsider = _user("outsider@example.com")

        cls.workflow = ClearanceWorkflowTemplate.objects.create(
            code="TEST_WF_2026",
            name="Test offboarding workflow",
            is_default=True,
            is_active=True,
        )
        cls.tpl_manager = ClearanceStepTemplate.objects.create(
            workflow=cls.workflow,
            step_code="MANAGER_ACK",
            step_name="Manager Acknowledgement",
            order=1,
            assigned_kind=ClearanceStepTemplate.AssignedKind.MANAGER,
        )
        cls.tpl_it = ClearanceStepTemplate.objects.create(
            workflow=cls.workflow,
            step_code="IT",
            step_name="IT Clearance",
            order=2,
            assigned_kind=ClearanceStepTemplate.AssignedKind.GROUP,
            assigned_group=cls.it_group,
        )
        cls.tpl_hr_final = ClearanceStepTemplate.objects.create(
            workflow=cls.workflow,
            step_code="HR_FINAL",
            step_name="Final HR Sign-off",
            order=3,
            assigned_kind=ClearanceStepTemplate.AssignedKind.HR_HEAD,
            assigned_group=cls.hr_group,
            triggers_letters=True,
        )

    # -- resignation helpers ------------------------------------------------

    def _submit(self, employee=None, applied_by=None, **overrides):
        employee = employee or self.employee
        defaults = dict(
            employee=employee,
            separation_type=Resignation.SeparationType.VOLUNTARY,
            reason="Moving on.",
            notice_period_days=30,
            notice_start_date=date.today(),
            last_working_date=date.today() + timedelta(days=30),
            applied_by=applied_by or employee,
        )
        defaults.update(overrides)
        return Resignation.objects.create(**defaults)

    def _accept(self, resignation, by=None):
        """Accept the resignation, executing the on_commit signal work
        (LWD write-through + case construction). Returns the case."""
        by = by or self.hr_user
        with self.captureOnCommitCallbacks(execute=True):
            resignation.status = Resignation.Status.ACCEPTED
            resignation.processed_by = by
            resignation.processed_at = timezone.now()
            resignation.save()
        resignation.refresh_from_db()
        return resignation.case

    def _accepted_case(self):
        return self._accept(self._submit())

    def _save_step(self, step, by, status=ClearanceStep.Status.DONE, remarks=""):
        with self.captureOnCommitCallbacks(execute=True):
            step.status = status
            step.remarks = remarks
            step.acted_by = by
            step.acted_at = timezone.now()
            step.save()


# ---------------------------------------------------------------------------
# 1. Resignation lifecycle
# ---------------------------------------------------------------------------

class ResignationLifecycleTests(_Base):

    def test_submit_view_creates_pending_resignation(self):
        self.client.force_login(self.employee)
        response = self.client.post(reverse("offboarding:resignation_submit"), {
            "separation_type": Resignation.SeparationType.VOLUNTARY,
            "reason": "New opportunity.",
            "notice_period_days": 30,
            "notice_start_date": date.today().isoformat(),
            "last_working_date": (date.today() + timedelta(days=30)).isoformat(),
        })
        self.assertRedirects(response, reverse("offboarding:my_offboarding"))
        res = Resignation.objects.get(employee=self.employee)
        self.assertEqual(res.status, Resignation.Status.PENDING)
        self.assertEqual(res.applied_by, self.employee)
        self.assertFalse(OffboardingCase.objects.exists())

        # A second submission is blocked while the first is active.
        response = self.client.post(reverse("offboarding:resignation_submit"), {
            "separation_type": Resignation.SeparationType.VOLUNTARY,
            "reason": "Again.",
            "notice_period_days": 30,
            "notice_start_date": date.today().isoformat(),
            "last_working_date": (date.today() + timedelta(days=30)).isoformat(),
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Resignation.objects.filter(employee=self.employee).count(), 1)

    def test_accept_materialises_case_and_steps_in_order(self):
        res = self._submit()
        case = self._accept(res)

        self.assertEqual(case.workflow_template, self.workflow)
        self.assertEqual(case.status, OffboardingCase.Status.IN_PROGRESS)
        self.assertEqual(case.current_step_order, 1)

        steps = list(case.steps.order_by("order"))
        self.assertEqual([s.step_code for s in steps], ["MANAGER_ACK", "IT", "HR_FINAL"])
        self.assertEqual([s.order for s in steps], [1, 2, 3])
        # MANAGER kind resolves the actual line manager.
        self.assertEqual(steps[0].assigned_user, self.manager)
        # GROUP kind leaves assigned_user empty; group members compete.
        self.assertIsNone(steps[1].assigned_user)
        self.assertEqual(steps[1].assigned_group, self.it_group)
        # HR_HEAD kind resolves the first active HR-group member.
        self.assertEqual(steps[2].assigned_user, self.hr_user)
        self.assertEqual(steps[2].assigned_group, self.hr_group)

        # LWD written through to CustomUser on accept.
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.last_working_date, res.last_working_date)

        # create_case_from_resignation is idempotent.
        again = create_case_from_resignation(res)
        self.assertEqual(again.pk, case.pk)
        self.assertEqual(case.steps.count(), 3)

    def test_duplicate_active_resignation_rejected_by_clean(self):
        self._submit()
        dup = Resignation(
            employee=self.employee,
            separation_type=Resignation.SeparationType.VOLUNTARY,
            reason="Duplicate.",
            notice_start_date=date.today(),
            last_working_date=date.today() + timedelta(days=30),
            applied_by=self.employee,
        )
        with self.assertRaises(ValidationError):
            dup.full_clean()

    def test_withdraw_while_pending_via_view(self):
        res = self._submit()
        self.client.force_login(self.employee)
        response = self.client.post(
            reverse("offboarding:resignation_withdraw", args=[res.pk]),
        )
        self.assertRedirects(response, reverse("offboarding:my_offboarding"))
        res.refresh_from_db()
        self.assertEqual(res.status, Resignation.Status.WITHDRAWN)
        self.assertEqual(res.processed_by, self.employee)

    def test_withdraw_denied_once_accepted(self):
        res = self._submit()
        self._accept(res)
        self.client.force_login(self.employee)
        response = self.client.post(
            reverse("offboarding:resignation_withdraw", args=[res.pk]),
        )
        self.assertEqual(response.status_code, 403)
        res.refresh_from_db()
        self.assertEqual(res.status, Resignation.Status.ACCEPTED)

    def test_hr_reject_via_decide_view(self):
        res = self._submit()
        self.client.force_login(self.hr_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("offboarding:resignation_decide", args=[res.pk]), {
                    "decision": Resignation.Status.REJECTED,
                    "process_note": "Retention agreed.",
                    "notice_period_days": 30,
                    "last_working_date": res.last_working_date.isoformat(),
                    "hr_notes": "",
                },
            )
        self.assertEqual(response.status_code, 302)
        res.refresh_from_db()
        self.assertEqual(res.status, Resignation.Status.REJECTED)
        self.assertEqual(res.processed_by, self.hr_user)
        self.assertFalse(OffboardingCase.objects.exists())
        # Rejecting never writes LWD through.
        self.employee.refresh_from_db()
        self.assertIsNone(self.employee.last_working_date)

    def test_hr_accept_via_decide_view_builds_case(self):
        res = self._submit()
        self.client.force_login(self.hr_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("offboarding:resignation_decide", args=[res.pk]), {
                    "decision": Resignation.Status.ACCEPTED,
                    "process_note": "",
                    "notice_period_days": 30,
                    "last_working_date": res.last_working_date.isoformat(),
                    "hr_notes": "",
                },
            )
        self.assertEqual(response.status_code, 302)
        res.refresh_from_db()
        self.assertEqual(res.status, Resignation.Status.ACCEPTED)
        self.assertTrue(OffboardingCase.objects.filter(resignation=res).exists())

    def test_decide_view_denied_for_non_hr(self):
        res = self._submit()
        for user in (self.employee, self.manager, self.outsider):
            self.client.force_login(user)
            response = self.client.get(
                reverse("offboarding:resignation_decide", args=[res.pk]),
            )
            self.assertEqual(response.status_code, 403, user.email)


# ---------------------------------------------------------------------------
# 1b. Notice-period shortfall detection
# ---------------------------------------------------------------------------

class NoticeShortfallTests(_Base):
    def test_notice_days_given_is_plain_date_subtraction(self):
        res = self._submit(
            notice_start_date=date(2026, 1, 1), last_working_date=date(2026, 1, 16),
        )
        self.assertEqual(res.notice_days_given, 15)

    def test_shortfall_zero_when_notice_given_meets_requirement(self):
        res = self._submit(
            notice_period_days=30,
            notice_start_date=date(2026, 1, 1), last_working_date=date(2026, 1, 31),
        )
        self.assertEqual(res.notice_shortfall_days, 0)
        self.assertFalse(res.fell_short_of_notice)

    def test_shortfall_positive_when_notice_given_is_short(self):
        res = self._submit(
            notice_period_days=60,
            notice_start_date=date(2026, 1, 1), last_working_date=date(2026, 1, 16),
        )
        self.assertEqual(res.notice_shortfall_days, 45)
        self.assertTrue(res.fell_short_of_notice)

    def test_submit_view_prefills_notice_period_from_employee_contract(self):
        self.employee.notice_period_months = 2
        self.employee.save(update_fields=["notice_period_months"])
        self.client.force_login(self.employee)
        response = self.client.get(reverse("offboarding:resignation_submit"))
        self.assertEqual(response.context["form"].fields["notice_period_days"].initial, 60)

    def test_submit_view_falls_back_to_default_without_a_contracted_notice_period(self):
        self.assertIsNone(self.employee.notice_period_months)
        self.client.force_login(self.employee)
        response = self.client.get(reverse("offboarding:resignation_submit"))
        # Model field default (30), untouched — no per-employee override applied.
        self.assertEqual(response.context["form"].fields["notice_period_days"].initial, 30)


# ---------------------------------------------------------------------------
# 2. Clearance workflow sequencing
# ---------------------------------------------------------------------------

class ClearanceSequencingTests(_Base):

    def setUp(self):
        self.case = self._accepted_case()
        self.step1, self.step2, self.step3 = self.case.steps.order_by("order")

    def test_only_current_step_actionable(self):
        # Step 1 is current: its assignee (the manager) may act.
        self.assertTrue(can_act_on_clearance_step(self.manager, self.step1))
        # Step 2 is NOT current: even its assigned group member is blocked.
        self.assertFalse(can_act_on_clearance_step(self.it_user, self.step2))
        # Step 3 is NOT current: even its assigned user is blocked... except
        # hr_user has edit-all scope, which bypasses sequencing. Use a second
        # HR-flavoured assignee without edit-all to prove the sequencing rule.
        self.step3.assigned_user = self.fin_user
        self.assertFalse(can_act_on_clearance_step(self.fin_user, self.step3))
        # Cross-assignment: manager can't act on the IT step.
        self.assertFalse(can_act_on_clearance_step(self.manager, self.step2))

    def test_applicant_can_never_act_on_own_steps(self):
        # Even if the applicant were assigned / in the group, they're blocked.
        self.employee.groups.add(self.it_group)
        self.assertFalse(can_act_on_clearance_step(self.employee, self.step1))
        self.assertFalse(can_act_on_clearance_step(self.employee, self.step2))

    def test_admin_can_act_regardless_of_order(self):
        # HR (edit-all scope) bypasses the sequential rule.
        self.assertTrue(can_act_on_clearance_step(self.hr_user, self.step2))
        self.assertTrue(can_act_on_clearance_step(self.hr_user, self.step3))

    def test_completing_step_advances_current_order(self):
        self._save_step(self.step1, by=self.manager)
        self.case.refresh_from_db()
        self.assertEqual(self.case.current_step_order, 2)
        self.assertEqual(self.case.status, OffboardingCase.Status.IN_PROGRESS)
        # Step 2 is now actionable by the IT group member; step 1 is closed.
        self.assertTrue(can_act_on_clearance_step(self.it_user, self.step2))
        self.step1.refresh_from_db()
        self.assertFalse(can_act_on_clearance_step(self.manager, self.step1))
        # Non-member of the assigned group is still blocked.
        self.assertFalse(can_act_on_clearance_step(self.fin_user, self.step2))

    def test_step_action_view_enforces_permissions_and_advances(self):
        # Before step 1 is done, the IT member gets a 403 on step 2.
        self.client.force_login(self.it_user)
        response = self.client.get(
            reverse("offboarding:clearance_step_action", args=[self.step2.pk]),
        )
        self.assertEqual(response.status_code, 403)

        self._save_step(self.step1, by=self.manager)

        # Finance is never allowed on the IT step.
        self.client.force_login(self.fin_user)
        response = self.client.get(
            reverse("offboarding:clearance_step_action", args=[self.step2.pk]),
        )
        self.assertEqual(response.status_code, 403)

        # The IT member completes step 2 through the view.
        self.client.force_login(self.it_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("offboarding:clearance_step_action", args=[self.step2.pk]), {
                    "status": ClearanceStep.Status.DONE,
                    "remarks": "Access revoked.",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.step2.refresh_from_db()
        self.assertEqual(self.step2.status, ClearanceStep.Status.DONE)
        self.assertEqual(self.step2.acted_by, self.it_user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.current_step_order, 3)

    def test_rejected_required_step_cancels_case(self):
        self._save_step(
            self.step1, by=self.manager,
            status=ClearanceStep.Status.REJECTED, remarks="Handover missing.",
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, OffboardingCase.Status.CANCELLED)
        # No step is actionable on a non-IN_PROGRESS case (edit-all aside).
        self.assertFalse(can_act_on_clearance_step(self.it_user, self.step2))

    def test_final_step_completion_issues_letters_and_completes_case(self):
        # Only two of the three auto-issued codes exist — the missing
        # RELEASE_LETTER must be skipped silently. requires_approval stays
        # True (default) so the documents app never reaches PDF rendering;
        # the boundary is mocked anyway as a guard.
        exp_type = DocumentType.objects.create(
            code="EXP_LETTER", name="experience_letter",
            display_name="Experience Certificate",
        )
        DocumentType.objects.create(
            code="NOC", name="noc", display_name="No Objection Certificate",
        )

        self._save_step(self.step1, by=self.manager)
        self._save_step(self.step2, by=self.it_user)

        with mock.patch("documents.pdf.generate_and_attach_pdf") as pdf_mock:
            self._save_step(self.step3, by=self.hr_user)
        pdf_mock.assert_not_called()

        self.case.refresh_from_db()
        self.assertEqual(self.case.status, OffboardingCase.Status.COMPLETED)
        self.assertIsNotNone(self.case.completed_at)
        self.assertEqual(self.case.current_step_order, 0)

        letters = DocumentRequest.objects.filter(offboarding_case=self.case)
        self.assertEqual(
            sorted(letters.values_list("document_type__code", flat=True)),
            ["EXP_LETTER", "NOC"],
        )
        for req in letters:
            self.assertEqual(req.employee, self.employee)
            self.assertEqual(
                req.input_data["last_working_date"],
                self.case.resignation.last_working_date.isoformat(),
            )
        self.assertEqual(self.case.primary_letter.document_type, exp_type)

        # Re-issuing is idempotent.
        self.assertEqual(auto_issue_letters(self.case), [])
        self.assertEqual(
            DocumentRequest.objects.filter(offboarding_case=self.case).count(), 2,
        )

        # Employee got the case-completed notification.
        self.assertTrue(
            any(self.employee.email in m.to for m in mail.outbox),
            "expected a case-completed email to the employee",
        )


# ---------------------------------------------------------------------------
# 3. Permission scoping — can_view_case + clearance queue
# ---------------------------------------------------------------------------

class PermissionScopingTests(_Base):

    def setUp(self):
        self.case = self._accepted_case()

    def test_can_view_case_matrix(self):
        # Own case.
        self.assertTrue(can_view_case(self.employee, self.case))
        # Global offboarding scope: HR + Senior Leadership.
        self.assertTrue(can_view_case(self.hr_user, self.case))
        self.assertTrue(can_view_case(self.sr_leader, self.case))
        # Line manager sees their downline's case.
        self.assertTrue(can_view_case(self.manager, self.case))
        # Assigned clearance-team member sees the case.
        self.assertTrue(can_view_case(self.it_user, self.case))
        # Unrelated users are denied.
        self.assertFalse(can_view_case(self.outsider, self.case))
        self.assertFalse(can_view_case(self.fin_user, self.case))

    def test_clearance_queue_scoped_to_assigned(self):
        url = reverse("offboarding:clearance_queue")

        # Assigned-group member sees the case.
        self.client.force_login(self.it_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["case"].pk for row in response.context["queue_rows"]],
            [self.case.pk],
        )

        # Group member with no step in the workflow sees nothing.
        self.client.force_login(self.fin_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["queue_rows"]), 0)

        # HR (can_see_all) sees everything.
        self.client.force_login(self.hr_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["queue_rows"]), 1)

    def test_case_detail_view_permissions(self):
        url = reverse("offboarding:case_detail", args=[self.case.pk])

        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(url).status_code, 200)

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 403)


# ---------------------------------------------------------------------------
# 4. LastWorkingDateMiddleware
# ---------------------------------------------------------------------------

class LastWorkingDateMiddlewareTests(_Base):

    def test_past_lwd_user_deactivated_and_logged_out(self):
        leaver = _user("leaver@example.com",
                       last_working_date=date.today() - timedelta(days=1))
        self.client.force_login(leaver)
        response = self.client.get(reverse("offboarding:my_offboarding"))
        self.assertRedirects(response, reverse("offboarding:access_ended"))
        leaver.refresh_from_db()
        self.assertFalse(leaver.is_active)
        # Session is gone: the next request is anonymous.
        response = self.client.get(reverse("offboarding:my_offboarding"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_allowlisted_routes_still_reachable(self):
        leaver = _user("leaver2@example.com",
                       last_working_date=date.today() - timedelta(days=1))
        self.client.force_login(leaver)
        # access_ended passes straight through — no deactivation, no loop.
        response = self.client.get(reverse("offboarding:access_ended"))
        self.assertEqual(response.status_code, 200)
        leaver.refresh_from_db()
        self.assertTrue(leaver.is_active)
        # logout is also allowlisted.
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        leaver.refresh_from_db()
        self.assertTrue(leaver.is_active)

    def test_lwd_today_or_future_unaffected(self):
        for offset in (0, 10):  # LWD today and in the future
            user = _user(f"staying{offset}@example.com",
                         last_working_date=date.today() + timedelta(days=offset))
            self.client.force_login(user)
            response = self.client.get(reverse("offboarding:my_offboarding"))
            self.assertEqual(response.status_code, 200, f"offset={offset}")
            user.refresh_from_db()
            self.assertTrue(user.is_active)


# ---------------------------------------------------------------------------
# 5. deactivate_past_lwd management command
# ---------------------------------------------------------------------------

class DeactivatePastLwdCommandTests(TestCase):

    def setUp(self):
        self.past = _user("past@example.com",
                          last_working_date=date.today() - timedelta(days=2))
        self.today = _user("today@example.com", last_working_date=date.today())
        self.future = _user("future@example.com",
                            last_working_date=date.today() + timedelta(days=5))
        self.no_lwd = _user("nolwd@example.com")
        self.already_inactive = _user(
            "gone@example.com",
            last_working_date=date.today() - timedelta(days=30),
            is_active=False,
        )

    def _refresh_all(self):
        for u in (self.past, self.today, self.future, self.no_lwd, self.already_inactive):
            u.refresh_from_db()

    def test_deactivates_only_past_lwd_active_users(self):
        out = StringIO()
        call_command("deactivate_past_lwd", stdout=out)
        self._refresh_all()
        self.assertFalse(self.past.is_active)
        self.assertTrue(self.today.is_active)
        self.assertTrue(self.future.is_active)
        self.assertTrue(self.no_lwd.is_active)
        self.assertFalse(self.already_inactive.is_active)
        self.assertIn("past@example.com", out.getvalue())
        self.assertIn("deactivated 1 user(s)", out.getvalue())

    def test_dry_run_touches_nothing(self):
        out = StringIO()
        call_command("deactivate_past_lwd", "--dry-run", stdout=out)
        self._refresh_all()
        self.assertTrue(self.past.is_active)
        self.assertIn("would deactivate 1 user(s)", out.getvalue())


# ---------------------------------------------------------------------------
# 6. HR create-on-behalf (/offboarding/create/)
# ---------------------------------------------------------------------------

class HRCreateResignationTests(_Base):

    def _post_data(self, **overrides):
        data = {
            "employee": str(self.employee.pk),
            "separation_type": Resignation.SeparationType.INVOLUNTARY,
            "reason": "Policy violation.",
            "notice_period_days": 0,
            "notice_start_date": date.today().isoformat(),
            "last_working_date": date.today().isoformat(),
            "hr_notes": "",
            "auto_accept": "on",
        }
        data.update(overrides)
        return data

    def test_hr_create_with_auto_accept_builds_case(self):
        self.client.force_login(self.hr_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("offboarding:hr_create_resignation"), self._post_data(),
            )
        self.assertEqual(response.status_code, 302)

        res = Resignation.objects.get(employee=self.employee)
        self.assertEqual(res.status, Resignation.Status.ACCEPTED)
        self.assertEqual(res.applied_by, self.hr_user)
        self.assertEqual(res.processed_by, self.hr_user)
        self.assertEqual(
            res.separation_type, Resignation.SeparationType.INVOLUNTARY,
        )
        case = OffboardingCase.objects.get(resignation=res)
        self.assertEqual(case.status, OffboardingCase.Status.IN_PROGRESS)
        self.assertEqual(case.steps.count(), 3)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.last_working_date, date.today())

    def test_hr_create_without_auto_accept_stays_pending(self):
        self.client.force_login(self.hr_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("offboarding:hr_create_resignation"),
                self._post_data(auto_accept=""),
            )
        self.assertEqual(response.status_code, 302)
        res = Resignation.objects.get(employee=self.employee)
        self.assertEqual(res.status, Resignation.Status.PENDING)
        self.assertFalse(OffboardingCase.objects.exists())

    def test_denied_for_non_hr(self):
        for user in (self.employee, self.manager, self.outsider):
            self.client.force_login(user)
            response = self.client.get(reverse("offboarding:hr_create_resignation"))
            self.assertEqual(response.status_code, 403, user.email)

    def test_conflict_with_existing_active_resignation(self):
        self._submit()  # employee already has a PENDING resignation
        self.client.force_login(self.hr_user)
        response = self.client.post(
            reverse("offboarding:hr_create_resignation"), self._post_data(),
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with errors
        self.assertIn("employee", response.context["form"].errors)
        self.assertEqual(Resignation.objects.filter(employee=self.employee).count(), 1)


# ---------------------------------------------------------------------------
# Unbacked separations — LWD on CustomUser with no Resignation behind it
# ---------------------------------------------------------------------------

class UnbackedSeparationTests(_Base):
    """The gap: last_working_date has three writers but only the Resignation
    → ACCEPTED path builds a clearance case. The other two (user edit form,
    admin Employment fieldset) leave the employee to be deactivated on
    schedule with no clearance at all."""

    def setUp(self):
        # Deliberately no department — an orphan with a NULL department must
        # still surface. Scoping this by department instead of by the
        # canonical managed_scope_pks would silently drop them.
        self.orphan = _user("orphan@example.com",
                            last_working_date=date.today() - timedelta(days=2))

    def test_lwd_without_resignation_is_detected(self):
        from offboarding.services import find_unbacked_separations
        self.assertIn(self.orphan, find_unbacked_separations())

    def test_employee_with_active_resignation_is_not_flagged(self):
        from offboarding.services import find_unbacked_separations
        Resignation.objects.create(
            employee=self.orphan, reason="r", applied_by=self.orphan,
            notice_start_date=date.today() - timedelta(days=32),
            last_working_date=date.today() - timedelta(days=2),
            status=Resignation.Status.PENDING,
        )
        self.assertNotIn(self.orphan, find_unbacked_separations())

    def test_withdrawn_resignation_still_counts_as_unbacked(self):
        """A withdrawn/rejected resignation leaves no case, so an LWD set by
        hand afterwards still needs a fresh one."""
        from offboarding.services import find_unbacked_separations
        Resignation.objects.create(
            employee=self.orphan, reason="r", applied_by=self.orphan,
            notice_start_date=date.today() - timedelta(days=32),
            last_working_date=date.today() - timedelta(days=2),
            status=Resignation.Status.WITHDRAWN,
        )
        self.assertIn(self.orphan, find_unbacked_separations())

    def test_cutoff_excludes_historical_separations(self):
        from offboarding.services import find_unbacked_separations
        self.orphan.last_working_date = date.today() - timedelta(days=120)
        self.orphan.save(update_fields=["last_working_date"])
        self.assertNotIn(self.orphan, find_unbacked_separations())
        # ...but an explicit 0 disables the cutoff entirely.
        self.assertIn(self.orphan, find_unbacked_separations(cutoff_days=0))

    def test_deactivated_orphan_is_still_selectable_by_hr(self):
        """Regression: the form filtered is_active=True, so an orphan became
        unselectable the moment the nightly job deactivated them — exactly
        the people HR most needs to raise a case for."""
        from offboarding.forms import HRCreateResignationForm
        self.orphan.is_active = False
        self.orphan.save(update_fields=["is_active"])
        form = HRCreateResignationForm(viewer=self.hr_user)
        self.assertIn(self.orphan, form.fields["employee"].queryset)

    def test_orphan_not_leaked_to_out_of_scope_viewer(self):
        """Widening the queryset must not become a scope bypass — someone
        with no managed scope still sees nobody."""
        from offboarding.forms import HRCreateResignationForm
        self.orphan.is_active = False
        self.orphan.save(update_fields=["is_active"])
        form = HRCreateResignationForm(viewer=self.outsider)
        self.assertNotIn(self.orphan, form.fields["employee"].queryset)

    def test_queue_prefill_fills_dates_but_not_separation_type(self):
        self.client.force_login(self.hr_user)
        response = self.client.get(
            reverse("offboarding:hr_create_resignation")
            + f"?employee={self.orphan.pk}",
        )
        self.assertEqual(response.status_code, 200)
        initial = response.context["form"].initial
        self.assertEqual(initial["employee"], self.orphan.pk)
        self.assertEqual(initial["last_working_date"], self.orphan.last_working_date)
        # separation_type + reason are deliberately NOT guessed.
        self.assertNotIn("separation_type", initial)
        self.assertNotIn("reason", initial)

    def test_prefill_ignores_out_of_scope_employee(self):
        self.client.force_login(self.hr_user)
        response = self.client.get(
            reverse("offboarding:hr_create_resignation")
            + "?employee=00000000-0000-0000-0000-000000000000",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial, {})


@override_settings(OFFBOARDING_NOTIFICATIONS_ENABLED=False)
class NotificationKillSwitchTests(_Base):

    def test_no_mail_sent_when_disabled(self):
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            Resignation.objects.create(
                employee=self.employee, reason="r", applied_by=self.employee,
                notice_start_date=date.today(),
                last_working_date=date.today() + timedelta(days=30),
            )
        self.assertEqual(mail.outbox, [])


class NotificationEnabledTests(_Base):
    """_Base already pins the flag on; this proves mail still flows."""

    def test_mail_sent_when_enabled(self):
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            Resignation.objects.create(
                employee=self.employee, reason="r", applied_by=self.employee,
                notice_start_date=date.today(),
                last_working_date=date.today() + timedelta(days=30),
            )
        self.assertTrue(mail.outbox)


class OffboardingEmployeeSearchTests(_Base):
    """The Create-resignation picker is searchable by name / employee ID /
    email, and backed by the same candidate queryset the form validates
    against — a search that offers someone the form then rejects is worse
    than not offering them."""

    def setUp(self):
        self.url = reverse("offboarding:ajax_offboarding_employees")
        self.orphan = _user("orphan@example.com", employee_id="99123",
                            last_working_date=date.today() - timedelta(days=2))

    def test_requires_hr_permission(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url, {"q": "a"}).status_code, 403)

    def test_search_by_employee_id(self):
        self.client.force_login(self.hr_user)
        response = self.client.get(self.url, {"q": "99123"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.orphan, response.context["results"])

    def test_search_by_email_and_name(self):
        self.client.force_login(self.hr_user)
        for term in ("orphan@example.com", "Orphan"):
            response = self.client.get(self.url, {"q": term})
            self.assertIn(self.orphan, response.context["results"], term)

    def test_deactivated_orphan_still_searchable(self):
        self.orphan.is_active = False
        self.orphan.save(update_fields=["is_active"])
        self.client.force_login(self.hr_user)
        response = self.client.get(self.url, {"q": "Orphan"})
        self.assertIn(self.orphan, response.context["results"])

    def test_search_results_match_form_queryset(self):
        """Pins the invariant: anything the search offers must validate."""
        from offboarding.forms import HRCreateResignationForm
        self.client.force_login(self.hr_user)
        response = self.client.get(self.url, {"q": "Orphan"})
        form_qs = HRCreateResignationForm(viewer=self.hr_user).fields["employee"].queryset
        for user in response.context["results"]:
            self.assertIn(user, form_qs, user.email)

    def test_blank_query_prompts_rather_than_listing_everyone(self):
        self.client.force_login(self.hr_user)
        response = self.client.get(self.url, {"q": ""})
        self.assertContains(response, "Type a name or employee ID")
