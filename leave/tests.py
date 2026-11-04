"""Comprehensive matrix tests for the Leave Policy v2.0 implementation.

Coverage areas:
  - Sandwich algorithm (within-app, cross-app, edges, holiday-in-middle,
    half-days, leave on weekend boundaries)
  - Eligibility (AL service, MtL gender/service/children, attachment threshold,
    CpL no-expiry + year-end carry)
  - Balance ledger (consume, restore, insufficient balance)
  - Approve / cancel flow (recompute_sandwich integration)
  - Management commands (apply_monthly_accrual, apply_year_end_rollover)

Run with:
  DJANGO_SETTINGS_MODULE=core.settings python manage.py test leave.tests
  python manage.py test leave.tests --settings=core.settings_test
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, Team
from employees.models import JobInfo, PersonalInfo
from leave.models import (
    CplGrant,
    EncashmentRequest,
    LeaveApplication,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
)
from leave.services import (
    anniversary_cycle_window,
    apply_cpl_reconciliation_row,
    compute_application_billable_days,
    consume_balance,
    describe_balance_impact,
    eligible_leave_policies,
    get_or_create_balance,
    is_eligible_for_policy,
    live_accrued_al,
    live_available,
    live_used,
    materialise_shifts_for_application,
    prorate_entitlement,
    recompute_sandwich,
    reconcile_missing_cpl_grants,
    reconcile_stale_cpl_grants,
    restore_balance,
    reverse_shifts_for_application,
    validate_application_against_balance,
    validate_application_attachment,
)
from roster.models import Holiday, Roster, Shift, ShiftType


User = get_user_model()


# ---------------------------------------------------------------------------
# Test data setup
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


def _make_shift_types():
    """Create the ShiftTypes the test suite needs."""
    morning, _ = ShiftType.objects.get_or_create(
        name="Morning",
        defaults={"category": ShiftType.Category.WORK, "is_active": True},
    )
    off, _ = ShiftType.objects.get_or_create(
        name="Weekly Off",
        defaults={"category": ShiftType.Category.OFF, "is_active": True},
    )
    return morning, off


def _make_leave_policies():
    """Create the eight LEAVE-category ShiftTypes + LeavePolicy rows.

    Mirrors the values the seed migration writes, but inline so the test
    suite is self-contained.
    """
    base_defaults = {
        "applies_to_gender": LeavePolicy.Gender.ANY,
        "min_service_months": 0,
        "max_surviving_children": None,
        "prorate_on_join": True,
        "bridges_sandwich": True,
        "cycle": LeavePolicy.Cycle.CALENDAR,
        "accrual_method": LeavePolicy.AccrualMethod.NONE,
        "accrual_ratio_denominator": 0,
        "carry_forward_pct": Decimal("0"),
        "max_carry_forward_days": None,
        "freezes_entitlement_at_cap": False,
        "encash_carry_forward_excess": False,
        "cert_required_min_days": 0,
        "cpl_grant_on_holiday_work": False,
        "cpl_use_within_days": None,
        "max_days_per_year": 0,
        "is_paid": True,
        "allow_advance_against_entitlement": False,
    }
    specs = {
        "Annual Leave": {
            # HR-confirmed rule (2026-09-01): all unused AL carries forward
            # every cycle, capped only by the 60-day lifetime bank — no 50%
            # cut, no auto-encashment of the excess (see
            # leave/management/commands/close_al_cycles.py).
            "min_service_months": 12, "prorate_on_join": False,
            "cycle": LeavePolicy.Cycle.ANNIVERSARY,
            "accrual_method": LeavePolicy.AccrualMethod.WORKED_DAY_RATIO,
            "accrual_ratio_denominator": 18,
            "carry_forward_pct": Decimal("1.00"),
            "max_carry_forward_days": 60,
            "freezes_entitlement_at_cap": True,
            "encash_carry_forward_excess": False,
            # 2026-09-10: a request may advance against days not yet
            # accrued this cycle, capped at max_days_per_year + carried
            # forward — see LeaveBalanceValidationTests.
            "allow_advance_against_entitlement": True,
        },
        "Casual Leave": {
            "max_days_per_year": 10,
            "accrual_method": LeavePolicy.AccrualMethod.FIXED_ANNUAL,
            "allow_advance_against_entitlement": True,
        },
        "Sick Leave": {
            "max_days_per_year": 14,
            "accrual_method": LeavePolicy.AccrualMethod.FIXED_ANNUAL,
            "cert_required_min_days": 3,
            "allow_advance_against_entitlement": True,
        },
        "Maternity Leave": {
            "max_days_per_year": 112,
            "applies_to_gender": LeavePolicy.Gender.FEMALE,
            "min_service_months": 6, "max_surviving_children": 1,
            "prorate_on_join": False,
            "cert_required_min_days": 1,
        },
        "Compensatory Leave": {
            # 2026-10-01: grants never expire (was: use within 3 working
            # days) and carry forward 100% across calendar years — see
            # CplNoExpiryTests.
            "cpl_grant_on_holiday_work": True, "cpl_use_within_days": 0,
            "carry_forward_pct": Decimal("1.00"),
        },
        "Leave Without Pay": {
            "is_paid": False, "bridges_sandwich": False,
        },
        "Half Day Leave": {
            "is_paid": False, "bridges_sandwich": False,
        },
        "Festival Leave": {},
    }
    out = {}
    for name, override in specs.items():
        st, _ = ShiftType.objects.get_or_create(
            name=name,
            defaults={"category": ShiftType.Category.LEAVE, "is_active": True},
        )
        st.category = ShiftType.Category.LEAVE
        st.is_working_shift = False
        st.save()
        kwargs = {**base_defaults, **override}
        policy, created = LeavePolicy.objects.get_or_create(
            shift_type=st, defaults=kwargs,
        )
        if not created:
            for k, v in kwargs.items():
                setattr(policy, k, v)
            policy.save()
        out[name] = policy
    return out


def _seed_default_pattern(employee, work_shift, off_shift):
    """Mon-Fri = WORK, Sat-Sun = OFF."""
    for wd in range(5):
        Roster.objects.update_or_create(
            employee=employee, weekday=wd,
            defaults={"shift_type": work_shift},
        )
    for wd in (5, 6):
        Roster.objects.update_or_create(
            employee=employee, weekday=wd,
            defaults={"shift_type": off_shift},
        )


class _Base(TestCase):
    """1 employee with Mon-Fri working pattern, 2-year tenure."""

    @classmethod
    def setUpTestData(cls):
        for name in ("User Admins", "HR", "All Employees", "Senior Leadership", "IT", "Finance"):
            Group.objects.get_or_create(name=name)

        cls.morning, cls.off = _make_shift_types()
        cls.policies = _make_leave_policies()
        cls.dept = Department.objects.create(name="Engineering")

        cls.alice = _user(
            "alice@example.com",
            department=cls.dept,
            joining_date=date.today() - timedelta(days=2 * 365 + 30),
        )
        PersonalInfo.objects.create(employee=cls.alice, gender="M")
        JobInfo.objects.create(employee=cls.alice, surviving_children_count=0)
        _seed_default_pattern(cls.alice, cls.morning, cls.off)


# ---------------------------------------------------------------------------
# Sandwich algorithm
# ---------------------------------------------------------------------------

class SandwichTests(_Base):
    """Test compute_application_billable_days against the policy spec.
    All examples assume Sat/Sun off (Alice's pattern).
    """

    def _app(self, start, end, leave_type=None, **kw):
        return LeaveApplication(
            employee=self.alice,
            leave_type=leave_type or self.policies["Casual Leave"].shift_type,
            start_date=start, end_date=end,
            applied_by=self.alice,
            **kw,
        )

    def test_single_working_day(self):
        d = date(2026, 5, 4)  # Monday
        self.assertEqual(d.weekday(), 0)
        app = self._app(d, d)
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(w, Decimal("1"))
        self.assertEqual(s, Decimal("0"))
        self.assertEqual(t, Decimal("1"))

    def test_friday_only(self):
        d = date(2026, 5, 8)  # Friday
        app = self._app(d, d)
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(t, Decimal("1"))
        self.assertEqual(s, Decimal("0"))

    def test_within_app_sandwich_fri_to_mon(self):
        """Fri–Mon (single app): Fri + Sat + Sun + Mon = 4 billed."""
        app = self._app(date(2026, 5, 8), date(2026, 5, 11))
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(w, Decimal("2"))
        self.assertEqual(s, Decimal("2"))
        self.assertEqual(t, Decimal("4"))

    def test_edge_weekend_at_start(self):
        """Sat–Mon: Sat/Sun are at the edge, not bridged. Bills 1 (Mon)."""
        app = self._app(date(2026, 5, 9), date(2026, 5, 11))
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(t, Decimal("1"))

    def test_edge_weekend_at_end(self):
        """Fri–Sat: Fri working + Sat at edge."""
        app = self._app(date(2026, 5, 8), date(2026, 5, 9))
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(t, Decimal("1"))

    def test_holiday_in_middle_bridges(self):
        """Mon-Fri with Wed Holiday → 4 working + 1 sandwich = 5 billed."""
        Holiday.objects.create(date=date(2026, 5, 6), name="Test")
        app = self._app(date(2026, 5, 4), date(2026, 5, 8))
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(w, Decimal("4"))
        self.assertEqual(s, Decimal("1"))
        self.assertEqual(t, Decimal("5"))

    def test_holiday_at_edge_does_not_count(self):
        """Mon only with Tuesday a Holiday outside the range. Bills 1."""
        Holiday.objects.create(date=date(2026, 5, 5), name="Tue")
        app = self._app(date(2026, 5, 4), date(2026, 5, 4))
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(t, Decimal("1"))

    def test_cross_app_sandwich(self):
        """Approve Fri (app A), then check Mon (app B). B should pick up bridge."""
        a = self._app(date(2026, 5, 8), date(2026, 5, 8))
        a.status = LeaveApplication.Status.APPROVED
        a.save()

        b = self._app(date(2026, 5, 11), date(2026, 5, 11))
        w, s, t = compute_application_billable_days(b)
        self.assertEqual(w, Decimal("1"))
        self.assertEqual(s, Decimal("2"))
        self.assertEqual(t, Decimal("3"))

    def test_cross_app_sandwich_different_types_does_not_bridge(self):
        """Regression (2026-09-17): Fri CL (app A) + Mon SL (app B) must NOT
        bridge the Sat/Sun in between — the sandwich rule only bridges a gap
        when the leave immediately before and after are the SAME leave type.
        A real case: employee took CL Sep 11, day-offs Sep 12-13, then SL
        Sep 14 — the day-offs were wrongly billed as SL before this fix."""
        a = self._app(date(2026, 5, 8), date(2026, 5, 8))  # Friday, CL
        a.status = LeaveApplication.Status.APPROVED
        a.save()

        b = self._app(
            date(2026, 5, 11), date(2026, 5, 11),
            leave_type=self.policies["Sick Leave"].shift_type,
        )  # Monday, SL
        w, s, t = compute_application_billable_days(b)
        self.assertEqual(w, Decimal("1"))
        self.assertEqual(s, Decimal("0"))
        self.assertEqual(t, Decimal("1"))

        # And app A itself must not have picked up the bridge either.
        w_a, s_a, t_a = compute_application_billable_days(a)
        self.assertEqual(s_a, Decimal("0"))
        self.assertEqual(t_a, Decimal("1"))

    def test_worked_days_in_gap_break_sandwich(self):
        """Regression: a weekend is NOT sandwiched when real worked days sit
        between it and the next leave. Approve Fri (app A May 8), then a leave
        on the FOLLOWING Wed (app B May 13) — Mon/Tue (May 11/12) were worked,
        so the May 9/10 weekend must not be billed to B. B bills 1, not 3."""
        a = self._app(date(2026, 5, 8), date(2026, 5, 8))  # Friday
        a.status = LeaveApplication.Status.APPROVED
        a.save()

        b = self._app(date(2026, 5, 13), date(2026, 5, 13))  # next Wednesday
        w, s, t = compute_application_billable_days(b)
        self.assertEqual(w, Decimal("1"))
        self.assertEqual(s, Decimal("0"))
        self.assertEqual(t, Decimal("1"))

    def test_half_day_billing(self):
        d = date(2026, 5, 4)
        app = self._app(d, d, is_half_day=True, half_day_period="AM")
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(w, Decimal("0.5"))
        self.assertEqual(t, Decimal("0.5"))

    def test_holiday_does_not_count_as_working(self):
        Holiday.objects.create(date=date(2026, 5, 4), name="Holiday")
        app = self._app(date(2026, 5, 4), date(2026, 5, 4))
        w, s, t = compute_application_billable_days(app)
        self.assertEqual(t, Decimal("0"))


# ---------------------------------------------------------------------------
# Approve / cancel flow with materialisation
# ---------------------------------------------------------------------------

class ApproveCancelFlowTests(_Base):
    def setUp(self):
        super().setUp()
        bal = get_or_create_balance(self.alice, self.policies["Casual Leave"], date(2026, 1, 1))
        bal.opening = Decimal("10")
        bal.save(update_fields=["opening"])

    def _approve(self, start, end, leave_type=None):
        leave_type = leave_type or self.policies["Casual Leave"].shift_type
        app = LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=leave_type,
            start_date=start, end_date=end,
            applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
        )
        recompute_sandwich(self.alice, start)
        app.refresh_from_db()
        return app

    def test_within_app_writes_shifts_and_debits_balance(self):
        app = self._approve(date(2026, 5, 8), date(2026, 5, 11))
        self.assertEqual(app.effective_days, Decimal("4"))

        bal = LeaveBalance.objects.get(
            employee=self.alice, leave_policy=self.policies["Casual Leave"], year=2026,
        )
        self.assertEqual(bal.used, Decimal("4"))
        self.assertEqual(bal.available, Decimal("6"))

        shift_dates = set(
            Shift.objects.filter(
                employee=self.alice,
                date__gte=date(2026, 5, 8),
                date__lte=date(2026, 5, 11),
            ).values_list("date", flat=True)
        )
        self.assertEqual(shift_dates, {
            date(2026, 5, 8), date(2026, 5, 9),
            date(2026, 5, 10), date(2026, 5, 11),
        })

    def test_cross_app_sandwich_retroactive_billing(self):
        a = self._approve(date(2026, 5, 8), date(2026, 5, 8))
        self.assertEqual(a.effective_days, Decimal("1"))

        b = self._approve(date(2026, 5, 11), date(2026, 5, 11))
        b.refresh_from_db()
        a.refresh_from_db()

        self.assertEqual(b.effective_days, Decimal("3"))
        self.assertEqual(a.effective_days, Decimal("1"))

        bal = LeaveBalance.objects.get(
            employee=self.alice, leave_policy=self.policies["Casual Leave"], year=2026,
        )
        self.assertEqual(bal.used, Decimal("4"))

    def test_cancel_releases_bridge(self):
        a = self._approve(date(2026, 5, 8), date(2026, 5, 8))
        b = self._approve(date(2026, 5, 11), date(2026, 5, 11))

        a.status = LeaveApplication.Status.CANCELLED
        a.save()
        restore_balance(a, self.alice)
        reverse_shifts_for_application(a)
        recompute_sandwich(self.alice, a.start_date)

        b.refresh_from_db()
        self.assertEqual(b.effective_days, Decimal("1"))

        bal = LeaveBalance.objects.get(
            employee=self.alice, leave_policy=self.policies["Casual Leave"], year=2026,
        )
        self.assertEqual(bal.used, Decimal("1"))


# ---------------------------------------------------------------------------
# Self-withdraw permission (can_cancel_leave)
# ---------------------------------------------------------------------------

class SelfWithdrawPermissionTests(_Base):
    """An employee may withdraw their own approved leave only while it's still
    in the future; past / in-progress approved leave requires a manager."""

    def _make(self, status, start, end=None):
        return LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=self.policies["Casual Leave"].shift_type,
            start_date=start,
            end_date=end or start,
            applied_by=self.alice,
            status=status,
        )

    def test_can_cancel_own_pending(self):
        from accounts.permissions import can_cancel_leave

        app = self._make(LeaveApplication.Status.PENDING, date.today() + timedelta(days=5))
        self.assertTrue(can_cancel_leave(self.alice, app))

    def test_can_cancel_own_approved_future(self):
        from accounts.permissions import can_cancel_leave

        app = self._make(LeaveApplication.Status.APPROVED, date.today() + timedelta(days=5))
        self.assertTrue(can_cancel_leave(self.alice, app))

    def test_cannot_cancel_own_approved_past(self):
        from accounts.permissions import can_cancel_leave

        app = self._make(LeaveApplication.Status.APPROVED, date.today() - timedelta(days=5))
        self.assertFalse(can_cancel_leave(self.alice, app))

    def test_cannot_cancel_own_approved_in_progress(self):
        from accounts.permissions import can_cancel_leave

        app = self._make(
            LeaveApplication.Status.APPROVED,
            date.today() - timedelta(days=1),
            date.today() + timedelta(days=2),
        )
        self.assertFalse(can_cancel_leave(self.alice, app))

    def test_cannot_cancel_own_approved_starting_today(self):
        from accounts.permissions import can_cancel_leave

        app = self._make(LeaveApplication.Status.APPROVED, date.today())
        self.assertFalse(can_cancel_leave(self.alice, app))


class LeaveEditTests(_Base):
    """A PENDING application can be revised by whoever could file it
    (can_edit_leave → can_apply_leave_for); anything else is closed."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.stranger = _user("stranger@example.com", department=cls.dept)

    def setUp(self):
        super().setUp()
        self.start = date.today() + timedelta(days=14)
        bal = get_or_create_balance(self.alice, self.policies["Casual Leave"], self.start)
        bal.opening = Decimal("10")
        bal.save(update_fields=["opening"])
        self.app = LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=self.policies["Casual Leave"].shift_type,
            start_date=self.start,
            end_date=self.start,
            applied_by=self.alice,
            note="original",
        )
        self.url = reverse("leave_edit", args=[self.app.pk])

    def _post(self, user, **overrides):
        client = Client()
        client.force_login(user)
        data = {
            "leave_type": self.app.leave_type_id,
            "start_date": self.start.isoformat(),
            "end_date": (self.start + timedelta(days=1)).isoformat(),
            "note": "revised",
        }
        data.update(overrides)
        return client.post(self.url, data)

    def test_owner_can_revise_pending(self):
        resp = self._post(self.alice)
        self.assertRedirects(resp, reverse("leave_detail", args=[self.app.pk]))
        self.app.refresh_from_db()
        self.assertEqual(self.app.end_date, self.start + timedelta(days=1))
        self.assertEqual(self.app.note, "revised")
        self.assertEqual(self.app.status, LeaveApplication.Status.PENDING)
        self.assertEqual(self.app.applied_by, self.alice)
        # Still pending: nothing written to the ledger or roster.
        self.assertFalse(LeaveBalanceTransaction.objects.filter(application=self.app).exists())
        self.assertFalse(Shift.objects.filter(leave_application=self.app).exists())

    def test_get_renders_form_prefilled(self):
        client = Client()
        client.force_login(self.alice)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Save changes")
        self.assertContains(resp, "original")

    def test_stranger_cannot_edit(self):
        resp = self._post(self.stranger)
        self.assertEqual(resp.status_code, 403)
        self.app.refresh_from_db()
        self.assertEqual(self.app.note, "original")

    def test_closed_application_cannot_be_edited(self):
        for status in (
            LeaveApplication.Status.APPROVED,
            LeaveApplication.Status.DECLINED,
            LeaveApplication.Status.CANCELLED,
        ):
            LeaveApplication.objects.filter(pk=self.app.pk).update(status=status)
            self.assertEqual(self._post(self.alice).status_code, 403, status)

    def test_edit_does_not_overlap_itself(self):
        # Keeping the same dates must not trip the overlap check on its own row.
        resp = self._post(self.alice, end_date=self.start.isoformat())
        self.assertEqual(resp.status_code, 302)

    def test_edit_into_another_pending_request_is_blocked(self):
        other_day = self.start + timedelta(days=7)
        LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=self.app.leave_type,
            start_date=other_day, end_date=other_day,
            applied_by=self.alice,
        )
        resp = self._post(self.alice, end_date=other_day.isoformat())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already have a pending or approved")

    def test_detail_shows_edit_button_only_while_pending(self):
        client = Client()
        client.force_login(self.alice)
        detail = reverse("leave_detail", args=[self.app.pk])
        self.assertContains(client.get(detail), self.url)
        LeaveApplication.objects.filter(pk=self.app.pk).update(status=LeaveApplication.Status.DECLINED)
        self.assertNotContains(client.get(detail), self.url)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

class EligibilityTests(_Base):
    def test_al_service_gate_blocks_new_joiner(self):
        new_joiner = _user(
            "new@example.com", department=self.dept,
            joining_date=date.today() - timedelta(days=200),
        )
        ok, reason = is_eligible_for_policy(new_joiner, self.policies["Annual Leave"])
        self.assertFalse(ok)
        self.assertIn("12 months", reason)

    def test_al_service_gate_passes_for_long_tenure(self):
        ok, _ = is_eligible_for_policy(self.alice, self.policies["Annual Leave"])
        self.assertTrue(ok)

    def test_mtl_gender_blocks_male(self):
        ok, reason = is_eligible_for_policy(self.alice, self.policies["Maternity Leave"])
        self.assertFalse(ok)
        self.assertIn("female", reason.lower())

    def test_mtl_full_pass_for_eligible_female(self):
        bea = _user("bea@example.com", department=self.dept,
                    joining_date=date.today() - timedelta(days=400))
        PersonalInfo.objects.create(employee=bea, gender="F")
        JobInfo.objects.create(employee=bea, surviving_children_count=1)
        ok, _ = is_eligible_for_policy(bea, self.policies["Maternity Leave"])
        self.assertTrue(ok)

    def test_mtl_blocked_with_two_children(self):
        bea = _user("bea2@example.com", department=self.dept,
                    joining_date=date.today() - timedelta(days=400))
        PersonalInfo.objects.create(employee=bea, gender="F")
        JobInfo.objects.create(employee=bea, surviving_children_count=2)
        ok, reason = is_eligible_for_policy(bea, self.policies["Maternity Leave"])
        self.assertFalse(ok)
        self.assertIn("surviving children", reason)

    def test_mtl_blocked_with_short_service(self):
        bea = _user("bea3@example.com", department=self.dept,
                    joining_date=date.today() - timedelta(days=120))
        PersonalInfo.objects.create(employee=bea, gender="F")
        JobInfo.objects.create(employee=bea, surviving_children_count=0)
        ok, reason = is_eligible_for_policy(bea, self.policies["Maternity Leave"])
        self.assertFalse(ok)
        self.assertIn("6 months", reason)

    def test_eligible_leave_policies_filters_dropdown(self):
        eligible = list(eligible_leave_policies(self.alice).values_list(
            "shift_type__name", flat=True,
        ))
        self.assertNotIn("Maternity Leave", eligible)
        self.assertIn("Annual Leave", eligible)
        self.assertIn("Casual Leave", eligible)


class AttachmentValidatorTests(_Base):
    def _probe(self, days, leave_type_name, attachment=None):
        d0 = date(2026, 5, 4)
        return LeaveApplication(
            employee=self.alice,
            leave_type=self.policies[leave_type_name].shift_type,
            start_date=d0,
            end_date=d0 + timedelta(days=days - 1),
            applied_by=self.alice,
            attachment=attachment or "",
        )

    def test_sl_one_day_no_attachment_allowed(self):
        validate_application_attachment(self._probe(1, "Sick Leave"))

    def test_sl_two_day_no_attachment_allowed(self):
        validate_application_attachment(self._probe(2, "Sick Leave"))

    def test_sl_three_day_no_attachment_rejected(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_application_attachment(self._probe(3, "Sick Leave"))

    def test_sl_five_day_with_attachment_allowed(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        probe = self._probe(5, "Sick Leave")
        probe.attachment = SimpleUploadedFile("note.pdf", b"x", content_type="application/pdf")
        validate_application_attachment(probe)

    def test_mtl_always_requires_cert(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_application_attachment(self._probe(1, "Maternity Leave"))


# ---------------------------------------------------------------------------
# Balance ledger
# ---------------------------------------------------------------------------

class BalanceLedgerTests(_Base):
    def test_consume_writes_used_transaction(self):
        bal = get_or_create_balance(self.alice, self.policies["Casual Leave"], date(2026, 1, 1))
        bal.opening = Decimal("10"); bal.save(update_fields=["opening"])

        app = LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=self.policies["Casual Leave"].shift_type,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 4),
            applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
            effective_days=Decimal("1"), working_days=Decimal("1"),
        )
        consume_balance(app, self.alice)
        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("1"))
        txn = LeaveBalanceTransaction.objects.get(application=app)
        self.assertEqual(txn.delta, Decimal("-1"))
        self.assertEqual(txn.reason, LeaveBalanceTransaction.Reason.USED)

    def test_consume_idempotent(self):
        bal = get_or_create_balance(self.alice, self.policies["Casual Leave"], date(2026, 1, 1))
        bal.opening = Decimal("10"); bal.save(update_fields=["opening"])
        app = LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=self.policies["Casual Leave"].shift_type,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 4),
            applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
            effective_days=Decimal("1"), working_days=Decimal("1"),
        )
        consume_balance(app, self.alice)
        consume_balance(app, self.alice)
        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("1"))

    def test_restore_reverses_used(self):
        bal = get_or_create_balance(self.alice, self.policies["Casual Leave"], date(2026, 1, 1))
        bal.opening = Decimal("10"); bal.save(update_fields=["opening"])
        app = LeaveApplication.objects.create(
            employee=self.alice,
            leave_type=self.policies["Casual Leave"].shift_type,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 4),
            applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
            effective_days=Decimal("3"), working_days=Decimal("3"),
        )
        consume_balance(app, self.alice)
        restore_balance(app, self.alice)
        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("0"))

    def test_unpaid_policies_skip_balance_check(self):
        app = LeaveApplication(
            employee=self.alice,
            leave_type=self.policies["Leave Without Pay"].shift_type,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 4),
            applied_by=self.alice,
        )
        validate_application_against_balance(app)

    def test_validate_against_balance_rejects_when_short_and_no_advance(self):
        """Maternity Leave doesn't allow advances — a short live balance still blocks,
        same as every policy did before allow_advance_against_entitlement existed."""
        bal = get_or_create_balance(self.alice, self.policies["Maternity Leave"], date(2026, 1, 1))
        bal.opening = Decimal("1"); bal.save(update_fields=["opening"])

        app = LeaveApplication(
            employee=self.alice,
            leave_type=self.policies["Maternity Leave"].shift_type,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 6),
            applied_by=self.alice,
        )
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_application_against_balance(app)


class AdvanceAgainstEntitlementTests(_Base):
    """CL/SL/AL allow a request to exceed the live/accrued-so-far balance, so
    long as it fits within max_days_per_year + opening + carried_forward −
    used − encashed for this cycle/year — see leave/services.py::
    describe_balance_impact and docs/LEAVE_POLICY_IMPLEMENTATION.md.

    All requests below are 2026-05-04 (Mon) → 2026-05-06 (Wed): 3 working
    days, no weekend involved, so `total` is always exactly 3 — the tests
    vary `used`/`carried_forward` instead of the date span to move the
    ceiling around a fixed request size.
    """

    def _app(self, policy):
        return LeaveApplication(
            employee=self.alice,
            leave_type=policy.shift_type,
            start_date=date(2026, 5, 4), end_date=date(2026, 5, 6),
            applied_by=self.alice,
        )

    def test_cl_advance_allowed_within_annual_cap(self):
        """opening=1 (a token nonzero seed — bal.opening=bal.accrued=0 would
        hit the 'unseeded' bypass instead of the real check) + 5 already
        used of the 10/year cap leaves a ceiling of 6 — a request of 3 is
        short of the live balance (-4) but fits the ceiling."""
        policy = self.policies["Casual Leave"]
        bal = get_or_create_balance(self.alice, policy, date(2026, 1, 1))
        bal.opening = Decimal("1"); bal.accrued = Decimal("0"); bal.used = Decimal("5")
        bal.save(update_fields=["opening", "accrued", "used"])

        app = self._app(policy)
        impact = describe_balance_impact(app)
        self.assertTrue(impact["sufficient"])
        self.assertTrue(impact["is_advance"])
        self.assertEqual(impact["ceiling"], Decimal("6"))
        validate_application_against_balance(app)  # does not raise

    def test_cl_still_blocked_past_annual_cap(self):
        """opening=1, 9 already used of the 10/year cap leaves a ceiling of
        2 — a request of 3 is arithmetically impossible this year."""
        policy = self.policies["Casual Leave"]
        bal = get_or_create_balance(self.alice, policy, date(2026, 1, 1))
        bal.opening = Decimal("1"); bal.accrued = Decimal("0"); bal.used = Decimal("9")
        bal.save(update_fields=["opening", "accrued", "used"])

        app = self._app(policy)
        impact = describe_balance_impact(app)
        self.assertFalse(impact["sufficient"])
        self.assertEqual(impact["ceiling"], Decimal("2"))
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_application_against_balance(app)

    def test_al_advance_allowed_within_cycle_cap_plus_carried_forward(self):
        """6 carried forward from previous cycles + this cycle's 12-day cap,
        minus 6 already used this cycle = a ceiling of 12. entitlement_frozen
        short-circuits live accrual to the stored (here: 0) accrued figure,
        isolating the ceiling arithmetic from real Shift-derived accrual.
        The shared test fixture leaves AL's max_days_per_year at its 0
        ('uncapped') default — set it to the real seeded 12 here, same as
        LiveAccrualTests.test_live_accrued_clamps_at_max_days_per_year_cap."""
        policy = self.policies["Annual Leave"]
        policy.max_days_per_year = 12
        policy.save(update_fields=["max_days_per_year"])
        bal = get_or_create_balance(self.alice, policy, date(2026, 5, 4))
        bal.opening = Decimal("0")
        bal.accrued = Decimal("0")
        bal.carried_forward = Decimal("6")
        bal.used = Decimal("6")
        bal.entitlement_frozen = True
        bal.save(update_fields=["opening", "accrued", "carried_forward", "used", "entitlement_frozen"])

        app = self._app(policy)
        impact = describe_balance_impact(app)
        self.assertTrue(impact["sufficient"])
        self.assertTrue(impact["is_advance"])
        self.assertEqual(impact["ceiling"], Decimal("12"))
        validate_application_against_balance(app)  # does not raise

    def test_al_still_blocked_past_cycle_cap_plus_carried_forward(self):
        """Same 6 carried forward, but 16 already used this cycle leaves a
        ceiling of 2 — a request of 3 exceeds even the best-case total."""
        policy = self.policies["Annual Leave"]
        policy.max_days_per_year = 12
        policy.save(update_fields=["max_days_per_year"])
        bal = get_or_create_balance(self.alice, policy, date(2026, 5, 4))
        bal.opening = Decimal("0")
        bal.accrued = Decimal("0")
        bal.carried_forward = Decimal("6")
        bal.used = Decimal("16")
        bal.entitlement_frozen = True
        bal.save(update_fields=["opening", "accrued", "carried_forward", "used", "entitlement_frozen"])

        app = self._app(policy)
        impact = describe_balance_impact(app)
        self.assertFalse(impact["sufficient"])
        self.assertEqual(impact["ceiling"], Decimal("2"))
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_application_against_balance(app)


# ---------------------------------------------------------------------------
# Pro-rating
# ---------------------------------------------------------------------------

class ProRatingTests(_Base):
    def test_full_year_for_existing_joiner(self):
        prorated = prorate_entitlement(self.policies["Casual Leave"], date(2025, 1, 1), 2026)
        self.assertEqual(prorated, Decimal("10"))

    def test_july_joiner_gets_half(self):
        prorated = prorate_entitlement(self.policies["Casual Leave"], date(2026, 7, 1), 2026)
        self.assertEqual(prorated, Decimal("5"))

    def test_october_joiner_rounds_to_half(self):
        prorated = prorate_entitlement(self.policies["Casual Leave"], date(2026, 10, 1), 2026)
        self.assertEqual(prorated, Decimal("2.5"))

    def test_al_does_not_prorate(self):
        prorated = prorate_entitlement(self.policies["Annual Leave"], date(2026, 7, 1), 2026)
        self.assertEqual(prorated, Decimal("0"))


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------

class ManagementCommandTests(_Base):
    def test_apply_monthly_accrual_skips_al(self):
        """Revised 2026-09-01: AL's accrual is computed live (see
        LiveAccrualTests below), not by this monthly batch — it must leave
        AL's LeaveBalance rows alone entirely."""
        for d in range(1, 19):
            Shift.objects.create(
                employee=self.alice, date=date(2026, 4, d),
                shift_type=self.morning,
                added_by=self.alice, updated_by=self.alice,
            )
        out = StringIO()
        call_command("apply_monthly_accrual", "--month", "2026-04", stdout=out)

        self.assertFalse(
            LeaveBalance.objects.filter(
                employee=self.alice, leave_policy=self.policies["Annual Leave"],
            ).exists()
        )

    def test_apply_year_end_rollover_skips_al(self):
        """AL is ANNIVERSARY-cycle now — the calendar-batch rollover must
        leave it alone entirely (close_al_cycles.py handles it instead,
        see below)."""
        LeaveBalance.objects.create(
            employee=self.alice,
            leave_policy=self.policies["Annual Leave"],
            year=2025,
            opening=Decimal("0"),
            accrued=Decimal("20"),
            used=Decimal("10"),
        )
        out = StringIO()
        call_command("apply_year_end_rollover", "--year", "2025", stdout=out)

        self.assertFalse(
            LeaveBalance.objects.filter(
                employee=self.alice, leave_policy=self.policies["Annual Leave"], year=2026,
            ).exists()
        )
        self.assertFalse(
            EncashmentRequest.objects.filter(
                employee=self.alice, leave_policy=self.policies["Annual Leave"],
            ).exists()
        )

    def test_open_al_cycles_is_scheduled_nightly(self):
        """Must run automatically (celery-beat), not depend on someone
        visiting a page — see docs/LEAVE_POLICY_IMPLEMENTATION.md §5.7.

        The schedule moved into the database on 2026-09-29
        (docs/SCHEDULER_ADMIN_PLAN.md), so the guarantee this test protects is
        now "the job is in the seeded set, daily, and before close_al_cycles" —
        an admin could still pause it, which is the point of the move.
        """
        from scheduler.schedule import jobs_by_command

        jobs = jobs_by_command()
        job = jobs.get("open_al_cycles")
        self.assertIsNotNone(job, "open_al_cycles missing from scheduler.schedule.JOBS")
        self.assertEqual(job.day_of_week, "*", "must run every night")
        self.assertEqual(job.day_of_month, "*")

        # Ordering the AL math depends on: usage reconciled, then open, then
        # close. Same-day, so compare times rather than trusting the listing.
        def at(command):
            return (int(jobs[command].hour), int(jobs[command].minute))

        self.assertLess(at("reconcile_shift_leave_usage"), at("open_al_cycles"))
        self.assertLess(at("open_al_cycles"), at("close_al_cycles"))

    def test_open_al_cycles_opens_a_first_row_for_a_newly_eligible_employee(self):
        # Alice is already >12 months tenured (see _Base) with no AL row yet.
        self.assertFalse(
            LeaveBalance.objects.filter(
                employee=self.alice, leave_policy=self.policies["Annual Leave"],
            ).exists()
        )
        out = StringIO()
        call_command("open_al_cycles", stdout=out)

        bal = LeaveBalance.objects.get(employee=self.alice, leave_policy=self.policies["Annual Leave"])
        cycle_start, cycle_end = anniversary_cycle_window(self.alice.joining_date, date.today())
        self.assertEqual(bal.cycle_start, cycle_start)
        self.assertEqual(bal.cycle_end, cycle_end)
        self.assertFalse(bal.cycle_closed)

    def test_open_al_cycles_skips_employee_under_12_months(self):
        new_joiner = _user(
            "new-joiner-open-al@example.com", department=self.dept,
            joining_date=date.today() - timedelta(days=100),
        )
        call_command("open_al_cycles", stdout=StringIO())

        self.assertFalse(
            LeaveBalance.objects.filter(
                employee=new_joiner, leave_policy=self.policies["Annual Leave"],
            ).exists()
        )

    def test_open_al_cycles_is_idempotent(self):
        call_command("open_al_cycles", stdout=StringIO())
        first = LeaveBalance.objects.get(employee=self.alice, leave_policy=self.policies["Annual Leave"])
        first.accrued = Decimal("3")
        first.save(update_fields=["accrued"])

        call_command("open_al_cycles", stdout=StringIO())

        self.assertEqual(
            LeaveBalance.objects.filter(
                employee=self.alice, leave_policy=self.policies["Annual Leave"],
            ).count(),
            1,
        )
        first.refresh_from_db()
        self.assertEqual(first.accrued, Decimal("3"))  # untouched by the second run

    def _jan1_employee(self, email):
        """A Jan-1-joined employee makes anniversary_cycle_window boundaries
        line up exactly with plain calendar dates, so the test can assert
        against literal dates instead of recomputing the window."""
        return _user(email, department=self.dept, joining_date=date(2020, 1, 1))

    def test_close_al_cycles_carries_all_unused_no_encashment(self):
        # HR-confirmed rule (2026-09-01): 100% of unused AL carries forward
        # every cycle — no 50% cut, no auto-encashment of the rest.
        # accrued is now live-computed from real Shift data at close time
        # (not read from a stored field), so this fixture needs real WORK
        # shifts: 180 WORK days ÷ 18 = 10 accrued.
        al = self.policies["Annual Leave"]
        bob = self._jan1_employee("bob-close-al@example.com")
        cycle_start = date(2024, 1, 1)
        cycle_end = date(2025, 1, 1)
        Shift.objects.bulk_create([
            Shift(employee=bob, date=cycle_start + timedelta(days=i), shift_type=self.morning)
            for i in range(180)
        ])
        old = LeaveBalance.objects.create(
            employee=bob, leave_policy=al,
            year=cycle_start.year, cycle_start=cycle_start, cycle_end=cycle_end,
            opening=Decimal("0"), accrued=Decimal("0"), used=Decimal("4"),
        )
        out = StringIO()
        call_command("close_al_cycles", "--upto", "2025-01-01", stdout=out)

        old.refresh_from_db()
        self.assertTrue(old.cycle_closed)
        self.assertEqual(old.accrued, Decimal("10"))  # frozen at the live-computed value

        new = LeaveBalance.objects.get(employee=bob, leave_policy=al, cycle_start=cycle_end)
        self.assertEqual(new.cycle_end, date(2026, 1, 1))
        self.assertEqual(new.carried_forward, Decimal("6"))  # 10 accrued - 4 used, all of it, not 50%
        self.assertFalse(EncashmentRequest.objects.filter(employee=bob, leave_policy=al).exists())

    def test_close_al_cycles_reconciles_stragglers_before_freezing(self):
        # Defensive belt-and-suspenders: an AL Shift written via bulk_create
        # (bypasses leave/signals.py, same as a bulk roster upload) that
        # reconcile_shift_leave_usage hasn't caught yet must still be
        # picked up — properly, via its own ledger transaction — before
        # the cycle's `used` is frozen, not silently under-counted.
        al = self.policies["Annual Leave"]
        al_type = al.shift_type
        bob = self._jan1_employee("bob-close-al-straggler@example.com")
        cycle_start = date(2024, 1, 1)
        cycle_end = date(2025, 1, 1)
        old = LeaveBalance.objects.create(
            employee=bob, leave_policy=al,
            year=cycle_start.year, cycle_start=cycle_start, cycle_end=cycle_end,
            opening=Decimal("0"), accrued=Decimal("0"), used=Decimal("4"),
        )
        Shift.objects.bulk_create([
            Shift(employee=bob, date=date(2024, 6, 1), shift_type=al_type),
        ])
        straggler = Shift.objects.get(employee=bob, date=date(2024, 6, 1))

        call_command("close_al_cycles", "--upto", "2025-01-01", stdout=StringIO())

        old.refresh_from_db()
        self.assertEqual(old.used, Decimal("5"))  # 4 already-reconciled + 1 straggler
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                balance=old, shift=straggler, reason=LeaveBalanceTransaction.Reason.USED,
            ).exists()
        )

    def test_close_al_cycles_freezes_at_60_cap(self):
        al = self.policies["Annual Leave"]
        bob = self._jan1_employee("bob-close-al-cap@example.com")
        cycle_start = date(2024, 1, 1)
        cycle_end = date(2025, 1, 1)
        old = LeaveBalance.objects.create(
            employee=bob, leave_policy=al,
            year=cycle_start.year, cycle_start=cycle_start, cycle_end=cycle_end,
            opening=Decimal("121"), used=Decimal("0"),
        )
        out = StringIO()
        call_command("close_al_cycles", "--upto", "2025-01-01", stdout=out)

        new = LeaveBalance.objects.get(employee=bob, leave_policy=al, cycle_start=cycle_end)
        self.assertEqual(new.carried_forward, Decimal("60"))
        self.assertTrue(new.entitlement_frozen)


# ---------------------------------------------------------------------------
# get_or_create_balance self-healing a legacy-row year collision (2026-09-02)
# ---------------------------------------------------------------------------

class GetOrCreateBalanceCollisionTests(_Base):
    """A pre-anniversary-rework AL row shares (employee, policy, year) with
    the employee's current live cycle whenever the numbers happen to
    coincide — get_or_create_balance must heal cycle_start/cycle_end on
    that row in place instead of silently returning it broken forever."""

    def test_heals_a_colliding_legacy_row_for_the_live_cycle(self):
        al = self.policies["Annual Leave"]
        today = date.today()
        cycle_start, cycle_end = anniversary_cycle_window(self.alice.joining_date, today)

        legacy = LeaveBalance.objects.create(
            employee=self.alice, leave_policy=al, year=cycle_start.year,
            opening=Decimal("2"), accrued=Decimal("5"), used=Decimal("1"),
        )

        bal = get_or_create_balance(self.alice, al, today)

        self.assertEqual(bal.pk, legacy.pk)  # healed in place, not a new row
        self.assertEqual(bal.cycle_start, cycle_start)
        self.assertEqual(bal.cycle_end, cycle_end)
        self.assertFalse(bal.cycle_closed)
        # Numeric fields untouched — only the window dates get healed.
        self.assertEqual(bal.opening, Decimal("2"))
        self.assertEqual(bal.accrued, Decimal("5"))
        self.assertEqual(bal.used, Decimal("1"))

    def test_does_not_resurrect_a_genuinely_past_cycle(self):
        """A legacy row whose year matches a cycle that's already fully
        elapsed (requested via a historical `as_of`, e.g. an old report)
        must stay untouched — only the live, still-open cycle gets healed."""
        al = self.policies["Annual Leave"]
        past_as_of = self.alice.joining_date + timedelta(days=400)
        cycle_start, cycle_end = anniversary_cycle_window(self.alice.joining_date, past_as_of)
        self.assertLess(cycle_end, date.today())  # sanity: genuinely in the past

        legacy = LeaveBalance.objects.create(
            employee=self.alice, leave_policy=al, year=cycle_start.year,
            accrued=Decimal("3"),
        )

        bal = get_or_create_balance(self.alice, al, past_as_of)

        self.assertEqual(bal.pk, legacy.pk)
        self.assertIsNone(bal.cycle_start)
        self.assertIsNone(bal.cycle_end)


# ---------------------------------------------------------------------------
# Live-computed AL accrual + instant Shift-derived usage debit (2026-09-01)
# ---------------------------------------------------------------------------

class LiveAccrualTests(_Base):
    def test_live_accrued_counts_worked_days_in_open_cycle(self):
        al = self.policies["Annual Leave"]
        cycle_start, cycle_end = anniversary_cycle_window(self.alice.joining_date, date.today())
        bal = get_or_create_balance(self.alice, al, date.today())
        self.assertEqual(bal.cycle_start, cycle_start)

        # 36 WORK days inside the cycle → 36/18 = 2 accrued. `upto` is
        # pinned explicitly (not left to default to today) since today
        # could fall anywhere in the cycle depending on Alice's fixture
        # joining_date, which would otherwise cut the count short.
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=cycle_start + timedelta(days=i), shift_type=self.morning)
            for i in range(36)
        ])
        upto = cycle_start + timedelta(days=35)
        self.assertEqual(live_accrued_al(self.alice, al, bal, upto=upto), Decimal("2"))

    def test_live_accrued_ignores_shifts_outside_the_cycle(self):
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, date.today())
        # 18 WORK days one day before the cycle even starts.
        Shift.objects.bulk_create([
            Shift(
                employee=self.alice,
                date=bal.cycle_start - timedelta(days=i + 1),
                shift_type=self.morning,
            )
            for i in range(18)
        ])
        self.assertEqual(live_accrued_al(self.alice, al, bal), Decimal("0"))

    def test_live_accrued_falls_back_to_stored_value_when_closed(self):
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, date.today())
        bal.accrued = Decimal("7")
        bal.cycle_closed = True
        bal.save(update_fields=["accrued", "cycle_closed"])
        # Even with real worked shifts in the window, a closed cycle must
        # not recompute — it's a frozen historical snapshot.
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=bal.cycle_start, shift_type=self.morning),
        ])
        self.assertEqual(live_accrued_al(self.alice, al, bal), Decimal("7"))

    def test_live_accrued_falls_back_to_stored_value_when_frozen(self):
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, date.today())
        bal.accrued = Decimal("3")
        bal.entitlement_frozen = True
        bal.save(update_fields=["accrued", "entitlement_frozen"])
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=bal.cycle_start, shift_type=self.morning),
        ])
        self.assertEqual(live_accrued_al(self.alice, al, bal), Decimal("3"))

    def test_live_accrued_no_op_for_non_al_policy(self):
        cl = self.policies["Casual Leave"]
        bal = get_or_create_balance(self.alice, cl, date.today())
        bal.accrued = Decimal("4")
        bal.save(update_fields=["accrued"])
        self.assertEqual(live_accrued_al(self.alice, cl, bal), Decimal("4"))

    def test_live_available_combines_live_accrued_with_stored_fields(self):
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, date.today())
        bal.opening = Decimal("1")
        bal.used = Decimal("1")
        bal.save(update_fields=["opening", "used"])
        # 18 WORK days → 1 accrued live. available = 1 opening + 1 accrued - 1 used = 1.
        # `upto` pinned explicitly — see test_live_accrued_counts_worked_days_in_open_cycle.
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=bal.cycle_start + timedelta(days=i), shift_type=self.morning)
            for i in range(18)
        ])
        upto = bal.cycle_start + timedelta(days=17)
        self.assertEqual(live_available(self.alice, al, bal, upto=upto), Decimal("1"))

    def test_live_accrued_counts_paid_leave_days_same_as_worked(self):
        # 2026-09-02 refinement: a paid-leave day (is_paid=True — CL here)
        # counts toward the worked-day numerator same as an actual WORK
        # shift, so taking paid leave doesn't cost AL accrual.
        al = self.policies["Annual Leave"]
        cl_type = self.policies["Casual Leave"].shift_type
        bal = get_or_create_balance(self.alice, al, date.today())
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=bal.cycle_start + timedelta(days=i), shift_type=cl_type)
            for i in range(18)
        ])
        upto = bal.cycle_start + timedelta(days=17)
        self.assertEqual(live_accrued_al(self.alice, al, bal, upto=upto), Decimal("1"))

    def test_live_accrued_ignores_unpaid_leave_days(self):
        # Unpaid statuses (is_paid=False — LWP here) still don't count —
        # only genuinely unpaid time reduces accrual under the refined rule.
        al = self.policies["Annual Leave"]
        lwp_type = self.policies["Leave Without Pay"].shift_type
        bal = get_or_create_balance(self.alice, al, date.today())
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=bal.cycle_start + timedelta(days=i), shift_type=lwp_type)
            for i in range(18)
        ])
        upto = bal.cycle_start + timedelta(days=17)
        self.assertEqual(live_accrued_al(self.alice, al, bal, upto=upto), Decimal("0"))

    def test_live_accrued_clamps_at_max_days_per_year_cap(self):
        # A shallow copy avoids mutating the shared class-level fixture
        # (`self.policies["Annual Leave"]` is the same Python object across
        # every test method in this class) — live_accrued_al only ever reads
        # attributes off the `policy` argument, never refetches from the DB,
        # so a copy is all that's needed.
        al = copy.copy(self.policies["Annual Leave"])
        al.max_days_per_year = 12
        bal = get_or_create_balance(self.alice, al, date.today())
        # 234 WORK days / 18 = 13, which the 12-day cap must clamp down.
        Shift.objects.bulk_create([
            Shift(employee=self.alice, date=bal.cycle_start + timedelta(days=i), shift_type=self.morning)
            for i in range(234)
        ])
        upto = bal.cycle_start + timedelta(days=233)
        self.assertEqual(live_accrued_al(self.alice, al, bal, upto=upto), Decimal("12"))

    def test_live_used_counts_unreconciled_bulk_created_shifts(self):
        # bulk_create bypasses leave/signals.py::sync_shift_leave_ledger —
        # simulates a bulk roster upload the nightly catch-up hasn't
        # processed yet.
        al = self.policies["Annual Leave"]
        al_type = al.shift_type
        bal = get_or_create_balance(self.alice, al, date.today())
        d = bal.cycle_start
        Shift.objects.bulk_create([Shift(employee=self.alice, date=d, shift_type=al_type)])
        self.assertEqual(bal.used, Decimal("0"))  # not yet reconciled into the ledger
        self.assertEqual(live_used(self.alice, al, bal, upto=d), Decimal("1"))

    def test_live_used_does_not_double_count_already_reconciled_shifts(self):
        al = self.policies["Annual Leave"]
        al_type = al.shift_type
        d = date(2026, 5, 4)
        Shift.objects.create(  # normal .create() -> signal reconciles it instantly
            employee=self.alice, date=d, shift_type=al_type,
            added_by=self.alice, updated_by=self.alice,
        )
        bal = get_or_create_balance(self.alice, al, d)
        self.assertEqual(bal.used, Decimal("1"))
        self.assertEqual(live_used(self.alice, al, bal, upto=d), Decimal("1"))

    def test_live_used_falls_back_to_stored_value_when_closed(self):
        al = self.policies["Annual Leave"]
        al_type = al.shift_type
        bal = get_or_create_balance(self.alice, al, date.today())
        bal.used = Decimal("2")
        bal.cycle_closed = True
        bal.save(update_fields=["used", "cycle_closed"])
        Shift.objects.bulk_create([Shift(employee=self.alice, date=bal.cycle_start, shift_type=al_type)])
        self.assertEqual(live_used(self.alice, al, bal), Decimal("2"))

    def test_live_used_ignores_leave_application_backed_shifts(self):
        # A LeaveApplication-backed Shift is excluded from the "pending
        # orphan" count — consume_balance already debited it, correctly,
        # including any half-day fraction that a raw Shift count would
        # get wrong.
        al = self.policies["Annual Leave"]
        d = date(2026, 5, 4)
        app = LeaveApplication.objects.create(
            employee=self.alice, leave_type=al.shift_type,
            start_date=d, end_date=d, applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
            effective_days=Decimal("0.5"), working_days=Decimal("0.5"),
            is_half_day=True, half_day_period=LeaveApplication.HalfDayPeriod.AM,
        )
        materialise_shifts_for_application(app, self.alice)
        consume_balance(app, self.alice)

        bal = get_or_create_balance(self.alice, al, d)
        self.assertEqual(bal.used, Decimal("0.5"))
        # live_used must not add +1 on top for the same (application-backed) shift.
        self.assertEqual(live_used(self.alice, al, bal, upto=d), Decimal("0.5"))


class InstantShiftLedgerTests(_Base):
    """The moment a Shift is saved as AL, the debit lands immediately —
    no waiting for the nightly reconcile_shift_leave_usage batch."""

    def test_saving_a_shift_as_al_debits_immediately(self):
        al_type = self.policies["Annual Leave"].shift_type
        d = date(2026, 5, 4)
        Shift.objects.create(
            employee=self.alice, date=d, shift_type=al_type,
            added_by=self.alice, updated_by=self.alice,
        )
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, d)
        self.assertEqual(bal.used, Decimal("1"))
        txn = LeaveBalanceTransaction.objects.get(reason=LeaveBalanceTransaction.Reason.USED, balance=bal)
        self.assertEqual(txn.delta, Decimal("-1"))

    def test_editing_a_shift_away_from_al_reverses_the_debit(self):
        al_type = self.policies["Annual Leave"].shift_type
        d = date(2026, 5, 4)
        shift = Shift.objects.create(
            employee=self.alice, date=d, shift_type=al_type,
            added_by=self.alice, updated_by=self.alice,
        )
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, d)
        self.assertEqual(bal.used, Decimal("1"))

        shift.shift_type = self.morning  # supervisor corrects it to a working day
        shift.save()

        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("0"))

    def test_deleting_a_shift_reverses_the_debit(self):
        al_type = self.policies["Annual Leave"].shift_type
        d = date(2026, 5, 4)
        shift = Shift.objects.create(
            employee=self.alice, date=d, shift_type=al_type,
            added_by=self.alice, updated_by=self.alice,
        )
        al = self.policies["Annual Leave"]
        bal = get_or_create_balance(self.alice, al, d)
        self.assertEqual(bal.used, Decimal("1"))

        shift.delete()

        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("0"))

    def test_leave_application_backed_shift_is_untouched(self):
        # A Shift materialised from an approved LeaveApplication (leave_
        # application set) must not be double-processed by the instant
        # signal — consume_balance/restore_balance already own that path.
        al = self.policies["Annual Leave"]
        d = date(2026, 5, 4)
        app = LeaveApplication.objects.create(
            employee=self.alice, leave_type=al.shift_type,
            start_date=d, end_date=d, applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
            effective_days=Decimal("1"), working_days=Decimal("1"),
        )
        materialise_shifts_for_application(app, self.alice)
        consume_balance(app, self.alice)

        bal = get_or_create_balance(self.alice, al, d)
        self.assertEqual(bal.used, Decimal("1"))
        # Only one USED transaction — the application-driven one, not a
        # second Shift-driven one.
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                balance=bal, reason=LeaveBalanceTransaction.Reason.USED,
            ).count(),
            1,
        )

    def test_application_approval_absorbs_prior_roster_direct_debit(self):
        # Regression test: a supervisor marks the roster cell SL directly
        # (e.g. an employee calls in sick), then the employee later files
        # the matching LeaveApplication for the record and it's approved —
        # the same day must only ever be charged once, not once per path.
        sl = self.policies["Sick Leave"]
        d = date(2026, 5, 4)
        Shift.objects.create(
            employee=self.alice, date=d, shift_type=sl.shift_type,
            added_by=self.alice, updated_by=self.alice,
        )
        bal = get_or_create_balance(self.alice, sl, d)
        self.assertEqual(bal.used, Decimal("1"))

        app = LeaveApplication.objects.create(
            employee=self.alice, leave_type=sl.shift_type,
            start_date=d, end_date=d, applied_by=self.alice,
            status=LeaveApplication.Status.APPROVED,
            effective_days=Decimal("1"), working_days=Decimal("1"),
        )
        recompute_sandwich(self.alice, d)  # what LeaveApproveView.post calls

        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("1"))

        txns = LeaveBalanceTransaction.objects.filter(balance=bal)
        self.assertEqual(sum(t.delta for t in txns), Decimal("-1"))
        self.assertTrue(
            txns.filter(
                reason=LeaveBalanceTransaction.Reason.REVERSED,
                application__isnull=True, shift__isnull=True,
            ).exists()
        )

        shift = Shift.objects.get(employee=self.alice, date=d)
        self.assertEqual(shift.leave_application_id, app.pk)


class CplGrantTests(_Base):
    """Department-aware CpL auto-grant + history (leave.CplGrant), and the
    grant/usage batch catch-ups (reconcile_missing_cpl_grants /
    reconcile_stale_cpl_grants)."""

    def _work_shift(self, employee, d, *, shift_type=None):
        return Shift.objects.create(
            employee=employee, date=d, shift_type=shift_type or self.morning,
            added_by=employee, updated_by=employee,
        )

    def test_auto_department_grants_cpl_and_creates_history_row(self):
        # cls.dept ("Engineering") defaults to AUTO cpl_treatment.
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)

        cpl = self.policies["Compensatory Leave"]
        bal = get_or_create_balance(self.alice, cpl, d)
        self.assertEqual(bal.accrued, Decimal("1"))

        grant = CplGrant.objects.get(work_shift=shift)
        self.assertEqual(grant.status, CplGrant.Status.OPEN)
        self.assertEqual(grant.source, CplGrant.Source.AUTO)
        self.assertEqual(grant.employee, self.alice)
        self.assertIsNotNone(grant.accrual_transaction)

    def test_none_department_never_grants_cpl(self):
        self.dept.cpl_treatment = Department.CplTreatment.NONE
        self.dept.save()
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)

        self.assertFalse(CplGrant.objects.filter(work_shift=shift).exists())
        cpl = self.policies["Compensatory Leave"]
        bal = get_or_create_balance(self.alice, cpl, d)
        self.assertEqual(bal.accrued, Decimal("0"))

    def test_manual_department_never_auto_grants_cpl(self):
        self.dept.cpl_treatment = Department.CplTreatment.MANUAL
        self.dept.save()
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)

        self.assertFalse(CplGrant.objects.filter(work_shift=shift).exists())

    def test_team_override_beats_department_treatment(self):
        team = Team.objects.create(name="Reporting", department=self.dept,
                                    cpl_treatment_override=Department.CplTreatment.NONE)
        self.alice.team = team
        self.alice.save()
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)

        self.assertFalse(CplGrant.objects.filter(work_shift=shift).exists())

    def test_deleting_work_shift_forfeits_the_grant(self):
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)
        grant = CplGrant.objects.get(work_shift=shift)
        accrual_txn = grant.accrual_transaction

        shift.delete()

        grant.refresh_from_db()
        self.assertEqual(grant.status, CplGrant.Status.FORFEITED)
        self.assertIsNone(grant.work_shift)
        cpl = self.policies["Compensatory Leave"]
        bal = get_or_create_balance(self.alice, cpl, d)
        self.assertEqual(bal.accrued, Decimal("0"))
        self.assertTrue(
            LeaveBalanceTransaction.objects.filter(
                balance=accrual_txn.balance, reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
            ).exists()
        )

    def test_consumption_pairs_oldest_open_grant_even_when_consumed_in_advance(self):
        """Confirmed against real Data Solutions data: a department may grant
        CpL in advance within the same rostering week, so the consumption
        Shift's date can be BEFORE the holiday it's compensating for. Pairing
        must go by grant age (oldest open first), not date order."""
        d1, d2 = date(2026, 5, 6), date(2026, 6, 3)
        Holiday.objects.create(date=d1, name="Holiday 1")
        Holiday.objects.create(date=d2, name="Holiday 2")
        self._work_shift(self.alice, d1)
        self._work_shift(self.alice, d2)

        cpl = self.policies["Compensatory Leave"]
        self.assertEqual(CplGrant.objects.filter(employee=self.alice, status=CplGrant.Status.OPEN).count(), 2)

        # Consumed the day BEFORE the second holiday — advance consumption.
        consume_date = d2 - timedelta(days=3)
        Shift.objects.create(
            employee=self.alice, date=consume_date, shift_type=cpl.shift_type,
            added_by=self.alice, updated_by=self.alice,
        )

        # The OLDER grant (d1) is paired, not the more recently-granted d2 one.
        older_grant = CplGrant.objects.get(holiday__date=d1)
        newer_grant = CplGrant.objects.get(holiday__date=d2)
        self.assertEqual(older_grant.status, CplGrant.Status.CONSUMED)
        self.assertEqual(older_grant.consumed_date, consume_date)
        self.assertEqual(newer_grant.status, CplGrant.Status.OPEN)

    def test_reconcile_missing_cpl_grants_catches_bulk_created_shifts(self):
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        # bulk_create bypasses post_save signals on purpose.
        Shift.objects.bulk_create([Shift(
            employee=self.alice, date=d, shift_type=self.morning,
            added_by=self.alice, updated_by=self.alice,
        )])
        shift = Shift.objects.get(employee=self.alice, date=d)
        self.assertFalse(CplGrant.objects.filter(work_shift=shift).exists())

        dry = reconcile_missing_cpl_grants(dry_run=True)
        self.assertEqual(dry["processed"], 1)
        self.assertFalse(CplGrant.objects.filter(work_shift=shift).exists())  # dry-run wrote nothing

        result = reconcile_missing_cpl_grants()
        self.assertEqual(result["processed"], 1)
        grant = CplGrant.objects.get(work_shift=shift)
        self.assertEqual(grant.status, CplGrant.Status.OPEN)

    def test_reconcile_missing_cpl_grants_skips_none_treatment_department(self):
        self.dept.cpl_treatment = Department.CplTreatment.NONE
        self.dept.save()
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        Shift.objects.bulk_create([Shift(
            employee=self.alice, date=d, shift_type=self.morning,
            added_by=self.alice, updated_by=self.alice,
        )])

        result = reconcile_missing_cpl_grants()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["skipped_treatment"], 1)

    def test_reconcile_stale_cpl_grants_forfeits_bulk_retyped_shift(self):
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)
        grant = CplGrant.objects.get(work_shift=shift)
        self.assertEqual(grant.status, CplGrant.Status.OPEN)

        # Bulk-retype away from WORK — bypasses revoke_cpl_on_shift_delete's
        # save-time reversal since .update() issues no signals.
        Shift.objects.filter(pk=shift.pk).update(shift_type=self.off)

        result = reconcile_stale_cpl_grants()
        self.assertEqual(result["reversed"], 1)
        grant.refresh_from_db()
        self.assertEqual(grant.status, CplGrant.Status.FORFEITED)
        cpl = self.policies["Compensatory Leave"]
        bal = get_or_create_balance(self.alice, cpl, d)
        self.assertEqual(bal.accrued, Decimal("0"))

    def test_reconcile_stale_cpl_grants_leaves_consumed_grants_alone(self):
        d = date(2026, 5, 6)
        Holiday.objects.create(date=d, name="Test Holiday")
        shift = self._work_shift(self.alice, d)
        cpl = self.policies["Compensatory Leave"]
        Shift.objects.create(
            employee=self.alice, date=d + timedelta(days=2), shift_type=cpl.shift_type,
            added_by=self.alice, updated_by=self.alice,
        )
        grant = CplGrant.objects.get(work_shift=shift)
        self.assertEqual(grant.status, CplGrant.Status.CONSUMED)

        Shift.objects.filter(pk=shift.pk).update(shift_type=self.off)
        result = reconcile_stale_cpl_grants()
        self.assertEqual(result["reversed"], 0)
        grant.refresh_from_db()
        self.assertEqual(grant.status, CplGrant.Status.CONSUMED)


class CplNoExpiryTests(_Base):
    """2026-10-01 CpL policy change: a granted day never expires (the old
    3-working-day `cpl_use_within_days` deadline is off), an employee may
    apply as long as ≥1 day is in balance — and nothing less — and unused
    CpL carries 100% into the next calendar year via the (now re-runnable)
    `apply_year_end_rollover` batch."""

    def _grant(self, d, name="Test Holiday"):
        Holiday.objects.create(date=d, name=name)
        return Shift.objects.create(
            employee=self.alice, date=d, shift_type=self.morning,
            added_by=self.alice, updated_by=self.alice,
        )

    def _cpl_app(self, d):
        return LeaveApplication(
            employee=self.alice,
            leave_type=self.policies["Compensatory Leave"].shift_type,
            start_date=d, end_date=d,
            applied_by=self.alice,
        )

    def test_grant_older_than_three_working_days_is_still_applicable(self):
        self._grant(date(2026, 5, 6))  # Wednesday
        app = self._cpl_app(date(2026, 6, 1))  # Monday, ~18 working days later
        impact = describe_balance_impact(app)
        self.assertEqual(impact["bypass"], "")
        self.assertEqual(impact["expiry_window_days"], 0)
        self.assertEqual(impact["available_before"], Decimal("1"))
        self.assertTrue(impact["sufficient"])
        validate_application_against_balance(app)  # does not raise

    def test_zero_grants_is_rejected_not_bypassed(self):
        """CpL's balance *is* its grants — a zero must hit the hard block,
        not the 'policy not seeded yet' bypass that CL/SL/AL rely on."""
        app = self._cpl_app(date(2026, 6, 1))
        impact = describe_balance_impact(app)
        self.assertEqual(impact["bypass"], "")
        self.assertFalse(impact["sufficient"])
        self.assertEqual(impact["available_before"], Decimal("0"))
        self.assertIn("Insufficient Compensatory Leave balance", impact["message"])
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_application_against_balance(app)

    def test_unseeded_bypass_still_applies_to_entitlement_policies(self):
        cl = self.policies["Casual Leave"]
        bal = get_or_create_balance(self.alice, cl, date(2026, 1, 1))
        bal.opening = Decimal("0"); bal.save(update_fields=["opening"])
        app = LeaveApplication(
            employee=self.alice, leave_type=cl.shift_type,
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 1),
            applied_by=self.alice,
        )
        self.assertEqual(describe_balance_impact(app)["bypass"], "unseeded")

    def test_year_end_rollover_carries_cpl_fully_and_is_rerunnable(self):
        cpl = self.policies["Compensatory Leave"]
        self._grant(date(2026, 12, 16), "Victory Day")
        self.assertEqual(
            get_or_create_balance(self.alice, cpl, date(2026, 12, 16)).accrued, Decimal("1"),
        )

        call_command("apply_year_end_rollover", "--year", "2026", stdout=StringIO())
        nb = LeaveBalance.objects.get(employee=self.alice, leave_policy=cpl, year=2027)
        self.assertEqual(nb.carried_forward, Decimal("1.00"))
        cf_txns = nb.transactions.filter(reason=LeaveBalanceTransaction.Reason.CARRIED_FORWARD)
        self.assertEqual(cf_txns.count(), 1)

        # Re-run with nothing changed → no double count, no new transaction.
        call_command("apply_year_end_rollover", "--year", "2026", stdout=StringIO())
        nb.refresh_from_db()
        self.assertEqual(nb.carried_forward, Decimal("1.00"))
        self.assertEqual(cf_txns.count(), 1)

        # A December grant posted *after* the Jan 1 run (late roster upload)
        # → the catch-up run posts just the +1 difference.
        self._grant(date(2026, 12, 25), "Christmas Day")
        call_command("apply_year_end_rollover", "--year", "2026", stdout=StringIO())
        nb.refresh_from_db()
        self.assertEqual(nb.carried_forward, Decimal("2.00"))
        self.assertEqual(cf_txns.count(), 2)
        self.assertEqual(cf_txns.order_by("-id").first().delta, Decimal("1.00"))
        self.assertFalse(EncashmentRequest.objects.filter(employee=self.alice).exists())

        # ...and the employee can take both days in January 2027.
        app = self._cpl_app(date(2027, 1, 5))  # Tuesday
        impact = describe_balance_impact(app)
        self.assertEqual(impact["available_before"], Decimal("2.00"))
        self.assertTrue(impact["sufficient"])
        validate_application_against_balance(app)


class CplReconciliationRowTests(_Base):
    """apply_cpl_reconciliation_row — the one-time ITCS-reconciliation
    correction helper (leave/services.py), covering its three action types."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # apply_cpl_reconciliation_row looks CL/CPL up by their real seeded
        # short ShiftType.name ("CL"/"CPL") — this file's shared
        # _make_leave_policies() fixture names them by their long form
        # instead, so rename in place for these tests.
        cpl_shift_type = cls.policies["Compensatory Leave"].shift_type
        cpl_shift_type.name = "CPL"
        cpl_shift_type.save(update_fields=["name"])
        cl_shift_type = cls.policies["Casual Leave"].shift_type
        cl_shift_type.name = "CL"
        cl_shift_type.save(update_fields=["name"])

    def test_cpl_link_creates_worked_shift_and_links_floating_consumption(self):
        """Mirrors the common real-world shape: a prior historical import
        already recorded the grant as CONSUMED on the right date, but as a
        floating ledger entry with no Shift behind either side."""
        work_date = date(2026, 5, 6)
        consumed_date = date(2026, 6, 1)
        holiday = Holiday.objects.create(date=work_date, name="Test Holiday")
        cpl_policy = self.policies["Compensatory Leave"]
        bal = get_or_create_balance(self.alice, cpl_policy, work_date)

        accrual_txn = LeaveBalanceTransaction.objects.create(
            balance=bal, delta=Decimal("1"), reason=LeaveBalanceTransaction.Reason.ACCRUAL,
            note="seed accrual",
        )
        consumption_txn = LeaveBalanceTransaction.objects.create(
            balance=bal, delta=Decimal("-1"), reason=LeaveBalanceTransaction.Reason.USED,
            note="floating historical import",
        )
        bal.accrued = Decimal("1")
        bal.used = Decimal("1")
        bal.save(update_fields=["accrued", "used", "updated_at"])

        grant = CplGrant.objects.create(
            employee=self.alice, department=self.dept, work_date=work_date,
            work_context=CplGrant.WorkContext.HOLIDAY, holiday=holiday,
            source=CplGrant.Source.UPLOAD, status=CplGrant.Status.CONSUMED,
            consumed_date=consumed_date, accrual_transaction=accrual_txn,
            consumption_transaction=consumption_txn,
        )

        result = apply_cpl_reconciliation_row(
            employee=self.alice, action="CPL_LINK",
            grant_work_date=work_date, consumed_date=consumed_date,
            worked_date=work_date, work_shift_type_name=self.morning.name,
        )
        self.assertTrue(result["ok"], result)

        work_shift = Shift.objects.get(employee=self.alice, date=work_date)
        self.assertEqual(work_shift.shift_type, self.morning)
        consumption_shift = Shift.objects.get(employee=self.alice, date=consumed_date)
        self.assertEqual(consumption_shift.shift_type, cpl_policy.shift_type)

        grant.refresh_from_db()
        self.assertEqual(grant.work_shift, work_shift)
        self.assertEqual(grant.consumption_shift, consumption_shift)
        self.assertEqual(grant.status, CplGrant.Status.CONSUMED)

        consumption_txn.refresh_from_db()
        self.assertEqual(consumption_txn.shift, consumption_shift)
        # No duplicate debit — the floating transaction was reused.
        self.assertEqual(
            LeaveBalanceTransaction.objects.filter(
                balance=bal, reason=LeaveBalanceTransaction.Reason.USED,
            ).count(),
            1,
        )
        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("1"))

        result2 = apply_cpl_reconciliation_row(
            employee=self.alice, action="CPL_LINK",
            grant_work_date=work_date, consumed_date=consumed_date,
            worked_date=work_date, work_shift_type_name=self.morning.name,
        )
        self.assertEqual(result2["changes"], [])

    def test_cl_revert_reverses_cpl_consumption_and_debits_cl_instead(self):
        work_date = date(2026, 5, 6)
        consumed_date = date(2026, 5, 24)
        Holiday.objects.create(date=work_date, name="Test Holiday")
        cpl_policy = self.policies["Compensatory Leave"]
        cl_policy = self.policies["Casual Leave"]

        work_shift = Shift.objects.create(
            employee=self.alice, date=work_date, shift_type=self.morning,
            added_by=self.alice, updated_by=self.alice,
        )
        consumption_shift = Shift.objects.create(
            employee=self.alice, date=consumed_date, shift_type=cpl_policy.shift_type,
            added_by=self.alice, updated_by=self.alice,
        )
        grant = CplGrant.objects.get(work_shift=work_shift)
        self.assertEqual(grant.status, CplGrant.Status.CONSUMED)
        self.assertEqual(grant.consumption_shift, consumption_shift)

        cpl_bal = get_or_create_balance(self.alice, cpl_policy, work_date)
        self.assertEqual(cpl_bal.used, Decimal("1"))

        result = apply_cpl_reconciliation_row(
            employee=self.alice, action="CL_REVERT",
            grant_work_date=work_date, consumed_date=consumed_date,
        )
        self.assertTrue(result["ok"], result)

        grant.refresh_from_db()
        self.assertEqual(grant.status, CplGrant.Status.OPEN)
        self.assertIsNone(grant.consumption_shift)
        self.assertIsNone(grant.consumed_date)
        self.assertIsNone(grant.consumption_transaction)

        consumption_shift.refresh_from_db()
        self.assertEqual(consumption_shift.shift_type, cl_policy.shift_type)
        self.assertIsNone(consumption_shift.shift_attribute)

        cpl_bal.refresh_from_db()
        self.assertEqual(cpl_bal.used, Decimal("0"))
        cl_bal = get_or_create_balance(self.alice, cl_policy, consumed_date)
        self.assertEqual(cl_bal.used, Decimal("1"))

        result2 = apply_cpl_reconciliation_row(
            employee=self.alice, action="CL_REVERT",
            grant_work_date=work_date, consumed_date=consumed_date,
        )
        self.assertEqual(result2["changes"], [])

    def test_cpl_redirect_moves_consumption_to_referenced_grant(self):
        """The FIFO auto-pairer consumed the older (source) grant when the
        consumption Shift was first saved — exactly the real bug a "CPL
        adjusted with <date>" manager note corrects, by moving the
        consumption onto the grant the note actually points at."""
        source_work_date = date(2026, 3, 26)
        target_work_date = date(2026, 5, 1)
        consumed_date = date(2026, 8, 6)
        Holiday.objects.create(date=source_work_date, name="Holiday 1")
        Holiday.objects.create(date=target_work_date, name="Holiday 2")

        source_shift = Shift.objects.create(
            employee=self.alice, date=source_work_date, shift_type=self.morning,
            added_by=self.alice, updated_by=self.alice,
        )
        target_shift = Shift.objects.create(
            employee=self.alice, date=target_work_date, shift_type=self.morning,
            added_by=self.alice, updated_by=self.alice,
        )
        cpl_policy = self.policies["Compensatory Leave"]
        consumption_shift = Shift.objects.create(
            employee=self.alice, date=consumed_date, shift_type=cpl_policy.shift_type,
            added_by=self.alice, updated_by=self.alice,
        )

        source_grant = CplGrant.objects.get(work_shift=source_shift)
        target_grant = CplGrant.objects.get(work_shift=target_shift)
        self.assertEqual(source_grant.status, CplGrant.Status.CONSUMED)
        self.assertEqual(target_grant.status, CplGrant.Status.OPEN)

        bal = get_or_create_balance(self.alice, cpl_policy, source_work_date)
        self.assertEqual(bal.used, Decimal("1"))

        result = apply_cpl_reconciliation_row(
            employee=self.alice, action="CPL_REDIRECT",
            grant_work_date=target_work_date, consumed_date=consumed_date,
            source_grant_work_date=source_work_date,
        )
        self.assertTrue(result["ok"], result)

        source_grant.refresh_from_db()
        target_grant.refresh_from_db()
        self.assertEqual(source_grant.status, CplGrant.Status.OPEN)
        self.assertIsNone(source_grant.consumption_shift)
        self.assertEqual(target_grant.status, CplGrant.Status.CONSUMED)
        self.assertEqual(target_grant.consumption_shift, consumption_shift)
        self.assertEqual(target_grant.consumed_date, consumed_date)

        bal.refresh_from_db()
        self.assertEqual(bal.used, Decimal("1"))  # one reversed, one (re)written — net unchanged

        result2 = apply_cpl_reconciliation_row(
            employee=self.alice, action="CPL_REDIRECT",
            grant_work_date=target_work_date, consumed_date=consumed_date,
            source_grant_work_date=source_work_date,
        )
        self.assertEqual(result2["changes"], [])


class CplGrantManualEntryTests(TestCase):
    """Manual CplGrant entry (leave.views.CplGrantManualCreateView) — for
    working a scheduled weekly off day (or another ad-hoc reason), which
    has no Holiday to auto-detect it from and so is always a deliberate
    HR/manager entry."""

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="HR")
        _make_shift_types()
        _make_leave_policies()
        cls.dept = Department.objects.create(name="CplManualDept")
        cls.hr = _user("hr-cplmanual@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.manager = _user("manager-cplmanual@example.com", department=cls.dept)
        cls.alice = _user("alice-cplmanual@example.com", department=cls.dept)
        JobInfo.objects.create(employee=cls.alice, reports_to=cls.manager)
        cls.stranger = _user("stranger-cplmanual@example.com", department=cls.dept)

    def _post(self, client, target, **overrides):
        url = reverse("cpl_grant_manual_create") + f"?for={target.pk}"
        payload = {"work_date": "2026-05-01", "work_context": "WEEKLY_OFF", "note": "Audit prep"}
        payload.update(overrides)
        return client.post(url, payload)

    def test_employee_cannot_self_serve(self):
        client = Client()
        client.force_login(self.alice)
        resp = self._post(client, self.alice)
        self.assertEqual(resp.status_code, 403)

    def test_unrelated_employee_cannot_create(self):
        client = Client()
        client.force_login(self.stranger)
        resp = self._post(client, self.alice)
        self.assertEqual(resp.status_code, 403)

    def test_manager_can_create_for_reportee(self):
        client = Client()
        client.force_login(self.manager)
        resp = self._post(client, self.alice)
        self.assertEqual(resp.status_code, 302)
        grant = CplGrant.objects.get(employee=self.alice)
        self.assertEqual(grant.source, CplGrant.Source.MANUAL)
        self.assertEqual(grant.work_context, CplGrant.WorkContext.WEEKLY_OFF)
        self.assertEqual(grant.status, CplGrant.Status.OPEN)
        self.assertEqual(grant.added_by, self.manager)
        self.assertIsNone(grant.holiday)
        # Regression: a manual grant must actually credit the ledger, not
        # just record history — caught live when a real admin-created grant
        # didn't show up on the balance page.
        self.assertIsNotNone(grant.accrual_transaction)
        self.assertEqual(grant.accrual_transaction.delta, Decimal("1"))
        cpl_policy = LeavePolicy.objects.get(shift_type__name="Compensatory Leave")
        bal = get_or_create_balance(self.alice, cpl_policy, grant.work_date)
        self.assertEqual(bal.accrued, Decimal("1"))

        # Regression: the balance drawer used to show a blank "Leave date"
        # for a manual grant's ACCRUAL row (no Shift/Application to read a
        # date off of) — CplGrant.work_date must fill that gap.
        from leave.views import LeaveBalanceTransactionsView
        row = LeaveBalanceTransactionsView._describe(grant.accrual_transaction, bal)
        self.assertEqual(row["leave_start"], grant.work_date)
        self.assertEqual(row["leave_end"], grant.work_date)
        self.assertIn("not yet taken", row["source_label"])

    def test_hr_can_create_for_anyone(self):
        client = Client()
        client.force_login(self.hr)
        resp = self._post(client, self.alice)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(CplGrant.objects.filter(employee=self.alice).exists())

    def test_note_is_required(self):
        client = Client()
        client.force_login(self.hr)
        resp = self._post(client, self.alice, note="")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(CplGrant.objects.filter(employee=self.alice).exists())

    def test_future_work_date_rejected(self):
        client = Client()
        client.force_login(self.hr)
        future = (date.today() + timedelta(days=5)).isoformat()
        resp = self._post(client, self.alice, work_date=future)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(CplGrant.objects.filter(employee=self.alice).exists())

    def test_none_department_still_allows_with_warning(self):
        self.dept.cpl_treatment = Department.CplTreatment.NONE
        self.dept.save()
        client = Client()
        client.force_login(self.hr)
        resp = self._post(client, self.alice)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(CplGrant.objects.filter(employee=self.alice).exists())


class CplGrantListViewTests(TestCase):
    """Front-end Compensatory Leave Register (leave.views.CplGrantListView)
    — exists so viewing who earned/consumed CpL doesn't require Django
    admin access."""

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="HR")
        cls.dept = Department.objects.create(name="CplRegisterDept")
        cls.other_dept = Department.objects.create(name="CplRegisterOtherDept")
        cls.hr = _user("hr-cplregister@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.alice = _user("alice-cplregister@example.com", department=cls.dept)
        cls.stranger = _user("stranger-cplregister@example.com", department=cls.other_dept)

        cls.alice_grant = CplGrant.objects.create(
            employee=cls.alice, department=cls.dept,
            work_date=date(2026, 5, 1), work_context=CplGrant.WorkContext.WEEKLY_OFF,
            source=CplGrant.Source.MANUAL, status=CplGrant.Status.OPEN,
        )
        cls.stranger_grant = CplGrant.objects.create(
            employee=cls.stranger, department=cls.other_dept,
            work_date=date(2026, 5, 2), work_context=CplGrant.WorkContext.WEEKLY_OFF,
            source=CplGrant.Source.MANUAL, status=CplGrant.Status.CONSUMED,
            consumed_date=date(2026, 5, 10),
        )

    def test_hr_sees_all_grants(self):
        client = Client()
        client.force_login(self.hr)
        resp = client.get(reverse("cpl_grant_list"))
        self.assertEqual(resp.status_code, 200)
        pks = {g.pk for g in resp.context["grants"]}
        self.assertEqual(pks, {self.alice_grant.pk, self.stranger_grant.pk})

    def test_self_scoped_employee_only_sees_own_grants(self):
        client = Client()
        client.force_login(self.alice)
        resp = client.get(reverse("cpl_grant_list"))
        self.assertEqual(resp.status_code, 200)
        pks = {g.pk for g in resp.context["grants"]}
        self.assertEqual(pks, {self.alice_grant.pk})

    def test_status_filter(self):
        client = Client()
        client.force_login(self.hr)
        resp = client.get(reverse("cpl_grant_list"), {"status": "CONSUMED"})
        self.assertEqual(resp.status_code, 200)
        pks = {g.pk for g in resp.context["grants"]}
        self.assertEqual(pks, {self.stranger_grant.pk})


class CplGrantUploadTests(TestCase):
    """HR/department-manager upload (leave.upload_cpl, leave.upload_views::
    CplGrantUploadView) — the MANUAL/UPLOAD path for departments whose CpL
    can't be auto-detected, and a one-time historical backfill for AUTO
    departments."""

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="HR")
        cls.morning, cls.off = _make_shift_types()
        _make_leave_policies()
        # MANUAL treatment — the realistic case this uploader is for. If
        # left at the default AUTO, creating the WORK shift below would
        # already auto-grant via leave/signals.py::grant_cpl_on_holiday_work
        # before the upload even runs, defeating the point of these tests.
        cls.dept = Department.objects.create(name="CplUploadDept", cpl_treatment=Department.CplTreatment.MANUAL)
        cls.other_dept = Department.objects.create(name="CplUploadOtherDept", cpl_treatment=Department.CplTreatment.MANUAL)
        cls.hr = _user("hr-cplupload@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.manager = _user("manager-cplupload@example.com", department=cls.dept)
        cls.alice = _user("alice-cplupload@example.com", employee_id="90001", department=cls.dept)
        JobInfo.objects.create(employee=cls.alice, reports_to=cls.manager)
        cls.stranger = _user("stranger-cplupload@example.com", employee_id="90002", department=cls.other_dept)
        cls.plain_employee = _user("plain-cplupload@example.com", department=cls.dept)

        cls.holiday = Holiday.objects.create(date=date(2026, 5, 6), name="Upload Test Holiday")
        Shift.objects.create(
            employee=cls.alice, date=cls.holiday.date, shift_type=cls.morning,
            added_by=cls.alice, updated_by=cls.alice,
        )
        Shift.objects.create(
            employee=cls.stranger, date=cls.holiday.date, shift_type=cls.morning,
            added_by=cls.stranger, updated_by=cls.stranger,
        )

    @staticmethod
    def _xlsx(rows):
        import io as _io
        import openpyxl as _openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile

        wb = _openpyxl.Workbook()
        ws = wb.active
        ws.append(["Employee ID", "Working Date", "Status", "Consumed Date"])
        for row in rows:
            ws.append(list(row))
        buf = _io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return SimpleUploadedFile(
            "upload.xlsx", buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_plain_employee_cannot_access_upload_page(self):
        client = Client()
        client.force_login(self.plain_employee)
        resp = client.get(reverse("cpl_grant_upload"))
        self.assertEqual(resp.status_code, 403)

    def test_manager_can_preview_and_apply_for_reportee(self):
        client = Client()
        client.force_login(self.manager)
        f = self._xlsx([["90001", "2026-05-06", "OPEN", ""]])
        preview = client.post(reverse("cpl_grant_upload"), {"file": f, "action": "preview"})
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(CplGrant.objects.filter(employee=self.alice).exists())

        f2 = self._xlsx([["90001", "2026-05-06", "OPEN", ""]])
        applied = client.post(reverse("cpl_grant_upload"), {"file": f2, "action": "apply"})
        self.assertEqual(applied.status_code, 200)
        grant = CplGrant.objects.get(employee=self.alice)
        self.assertEqual(grant.source, CplGrant.Source.UPLOAD)
        self.assertIsNotNone(grant.accrual_transaction)

    def test_manager_cannot_upload_for_employee_outside_scope(self):
        client = Client()
        client.force_login(self.manager)
        f = self._xlsx([["90002", "2026-05-06", "OPEN", ""]])
        resp = client.post(reverse("cpl_grant_upload"), {"file": f, "action": "apply"})
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("outside your scope", content)
        self.assertFalse(CplGrant.objects.filter(employee=self.stranger).exists())

    def test_hr_can_upload_for_anyone(self):
        client = Client()
        client.force_login(self.hr)
        f = self._xlsx([["90002", "2026-05-06", "OPEN", ""]])
        resp = client.post(reverse("cpl_grant_upload"), {"file": f, "action": "apply"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(CplGrant.objects.filter(employee=self.stranger).exists())

    def test_reupload_is_idempotent(self):
        client = Client()
        client.force_login(self.hr)
        f = self._xlsx([["90001", "2026-05-06", "OPEN", ""]])
        client.post(reverse("cpl_grant_upload"), {"file": f, "action": "apply"})
        self.assertEqual(CplGrant.objects.filter(employee=self.alice).count(), 1)

        f2 = self._xlsx([["90001", "2026-05-06", "OPEN", ""]])
        resp2 = client.post(reverse("cpl_grant_upload"), {"file": f2, "action": "apply"})
        self.assertIn("already imported", resp2.content.decode())
        self.assertEqual(CplGrant.objects.filter(employee=self.alice).count(), 1)

    def test_missing_employee_id_reports_parse_error_and_aborts(self):
        client = Client()
        client.force_login(self.hr)
        f = self._xlsx([["", "2026-05-06", "OPEN", ""]])
        resp = client.post(reverse("cpl_grant_upload"), {"file": f, "action": "apply"})
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("aborted", content.lower())
        self.assertEqual(CplGrant.objects.count(), 0)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class LeaveListSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="HR")
        cls.leave_type = ShiftType.objects.create(
            name="CL-SEARCH",
            category=ShiftType.Category.LEAVE,
        )
        cls.hr = _user("hr-leave-search@example.com")
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.ops = Department.objects.create(name="Operations")
        cls.finance = Department.objects.create(name="Finance")
        cls.ops_team = Team.objects.create(name="Ops A", department=cls.ops)
        cls.finance_team = Team.objects.create(name="Finance A", department=cls.finance)
        cls.alice = _user("alice.leave@example.com", employee_id="51761")
        cls.alice.username = "alice.leave"
        cls.alice.department = cls.ops
        cls.alice.team = cls.ops_team
        cls.alice.save(update_fields=["username", "department", "team"])
        cls.bob = _user("bob.leave@example.com", employee_id="53770")
        cls.bob.username = "bob.leave"
        cls.bob.department = cls.finance
        cls.bob.team = cls.finance_team
        cls.bob.save(update_fields=["username", "department", "team"])
        cls.alice_app = LeaveApplication.objects.create(
            employee=cls.alice,
            leave_type=cls.leave_type,
            start_date=date(2026, 8, 19),
            end_date=date(2026, 8, 19),
            effective_days=Decimal("1.00"),
            working_days=Decimal("1.00"),
            status=LeaveApplication.Status.PENDING,
            applied_by=cls.alice,
        )
        cls.bob_app = LeaveApplication.objects.create(
            employee=cls.bob,
            leave_type=cls.leave_type,
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 20),
            effective_days=Decimal("1.00"),
            working_days=Decimal("1.00"),
            status=LeaveApplication.Status.PENDING,
            applied_by=cls.bob,
        )

    @staticmethod
    def _row_marker(application) -> str:
        """The row's own DOM id — what "this application is listed" actually
        means. These assertions used to look for the employee's email, which
        broke the day the list swapped that column for "Name · Employee ID";
        the row id doesn't depend on what the cell happens to display."""
        return f'id="leave-row-{application.pk}"'

    def assertRowListed(self, resp, application):
        self.assertIn(self._row_marker(application), resp.content.decode())

    def assertRowNotListed(self, resp, application):
        self.assertNotIn(self._row_marker(application), resp.content.decode())

    def test_all_tab_search_filters_by_employee_identity(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "q": "51761"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRowListed(resp, self.alice_app)
        self.assertRowNotListed(resp, self.bob_app)
        self.assertContains(resp, "Name, email, or employee ID")

    def test_all_tab_filter_by_department(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "department": self.ops.pk},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRowListed(resp, self.alice_app)
        self.assertRowNotListed(resp, self.bob_app)

    def test_all_tab_filter_by_team(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {
                "tab": "all",
                "department": self.finance.pk,
                "team": self.finance_team.pk,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRowNotListed(resp, self.alice_app)
        self.assertRowListed(resp, self.bob_app)

    # -- Leave-date range filter ------------------------------------------

    def test_all_tab_filter_by_leave_date_from(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "start_date": "2026-08-20"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRowNotListed(resp, self.alice_app)
        self.assertRowListed(resp, self.bob_app)

    def test_all_tab_filter_by_leave_date_to(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "end_date": "2026-08-19"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRowListed(resp, self.alice_app)
        self.assertRowNotListed(resp, self.bob_app)

    def test_leave_date_filter_requires_full_containment(self):
        """A leave straddling the window edge is out — the filter is
        start_date >= from AND end_date <= to, not an overlap test."""
        carol = _user("carol.leave@example.com", employee_id="55901")
        carol.department = self.ops
        carol.team = self.ops_team
        carol.save(update_fields=["department", "team"])
        carol_app = LeaveApplication.objects.create(
            employee=carol,
            leave_type=self.leave_type,
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 21),
            effective_days=Decimal("4.00"),
            working_days=Decimal("4.00"),
            status=LeaveApplication.Status.PENDING,
            applied_by=carol,
        )
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "start_date": "2026-08-19", "end_date": "2026-08-20"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRowNotListed(resp, carol_app)
        self.assertRowListed(resp, self.alice_app)
        self.assertRowListed(resp, self.bob_app)

    def test_inverted_leave_date_range_reports_error_and_is_not_applied(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "start_date": "2026-08-31", "end_date": "2026-08-01"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Working date from must be on or before Working date to.")
        self.assertRowListed(resp, self.alice_app)
        self.assertRowListed(resp, self.bob_app)

    def test_unparseable_leave_date_reports_error(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "start_date": "2026-02-30"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn&#x27;t a valid date.")
        self.assertRowListed(resp, self.alice_app)

    def test_all_tab_sorts_by_leave_date_newest_first(self):
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("leave_list"), {"tab": "all"})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertLess(
            body.index(self._row_marker(self.bob_app)),
            body.index(self._row_marker(self.alice_app)),
        )

    def test_pending_tab_uses_the_same_sort_as_the_other_tabs(self):
        """Ordering lives on LeaveApplication.Meta, so no tab can drift."""
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("leave_list"), {"tab": "pending"})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertLess(
            body.index(self._row_marker(self.bob_app)),
            body.index(self._row_marker(self.alice_app)),
        )

    # --- "Direct reports only" switch on the Pending tab (2026-10-10) ------

    def _pending_fixture(self):
        """manager → alice (direct) → grand (indirect); bob is unrelated."""
        # Everyone in one department: a non-privileged viewer's list is
        # clamped to their scope's department (effective_department_id),
        # which is unrelated to the switch under test.
        manager = _user("manager.leave@example.com", employee_id="51700", department=self.ops)
        grand = _user("grand.leave@example.com", employee_id="51762", department=self.ops)
        JobInfo.objects.create(employee=self.alice, reports_to=manager)
        JobInfo.objects.create(employee=grand, reports_to=self.alice)
        grand_app = LeaveApplication.objects.create(
            employee=grand,
            leave_type=self.leave_type,
            start_date=date(2026, 8, 21),
            end_date=date(2026, 8, 21),
            effective_days=Decimal("1.00"),
            working_days=Decimal("1.00"),
            status=LeaveApplication.Status.PENDING,
            applied_by=grand,
        )
        return manager, grand_app

    def test_pending_tab_defaults_to_direct_reports_only(self):
        manager, grand_app = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "pending"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["direct_only"])
        self.assertTrue(resp.context["has_direct_reports"])
        self.assertRowListed(resp, self.alice_app)
        self.assertRowNotListed(resp, grand_app)
        self.assertRowNotListed(resp, self.bob_app)
        self.assertEqual(resp.context["pending_count"], 1)
        self.assertContains(resp, "Direct reports only")
        # A scope switch, not a filter — the popover must not light up.
        self.assertFalse(resp.context["filters_active"])

    def test_pending_tab_unticked_shows_the_whole_approval_scope(self):
        manager, grand_app = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "pending", "direct": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["direct_only"])
        self.assertRowListed(resp, self.alice_app)
        self.assertRowListed(resp, grand_app)
        # Still never wider than the viewer's visible scope.
        self.assertRowNotListed(resp, self.bob_app)
        self.assertEqual(resp.context["pending_count"], 2)
        self.assertFalse(resp.context["filters_active"])

    def test_direct_switch_survives_the_list_calendar_toggle(self):
        manager, _ = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "pending", "direct": "0"})
        self.assertIn("direct=0", resp.context["calendar_url"])
        self.assertIn("direct=0", resp.context["list_url"])
        resp = self.client.get(reverse("leave_list"), {"tab": "pending"})
        self.assertNotIn("direct=", resp.context["calendar_url"])

    def test_all_tab_defaults_to_direct_reports_only(self):
        manager, grand_app = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "all"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["direct_only"])
        self.assertRowListed(resp, self.alice_app)
        self.assertRowNotListed(resp, grand_app)
        self.assertEqual(resp.context["all_count"], 1)
        self.assertContains(resp, "Direct reports only")
        self.assertFalse(resp.context["filters_active"])

    def test_all_tab_unticked_shows_the_whole_visible_scope(self):
        manager, grand_app = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "all", "direct": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertRowListed(resp, self.alice_app)
        self.assertRowListed(resp, grand_app)
        self.assertRowNotListed(resp, self.bob_app)
        self.assertEqual(resp.context["all_count"], 2)

    def test_direct_switch_carries_between_the_pending_and_all_tabs(self):
        manager, _ = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "pending", "direct": "0"})
        self.assertContains(resp, "?tab=all&view=list&amp;direct=0")

    def test_unticked_switch_survives_the_popover_search_form_and_clear(self):
        """The Filters popover is a GET form that rebuilds the querystring, so
        it must carry ?direct=0 as a hidden input (reported 2026-10-10: a
        search with the box unticked came back ticked). Ticked is the default
        and needs nothing. The popover's Clear link keeps it too."""
        manager, grand_app = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "all", "direct": "0", "q": "x"})
        self.assertContains(resp, '<input type="hidden" name="direct" value="0">')
        # The Clear link (only rendered while a filter is active).
        self.assertContains(resp, 'href="?tab=all&view=list&direct=0"')
        resp = self.client.get(reverse("leave_list"), {"tab": "all", "q": "x"})
        self.assertNotContains(resp, 'name="direct"')
        self.assertContains(resp, 'href="?tab=all&view=list"')
        resp = self.client.get(reverse("leave_list"), {"tab": "all"})
        self.assertNotContains(resp, 'name="direct"')
        # Submitting the form with the hidden input present keeps it unticked.
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "direct": "0", "q": "grand", "department": "", "team": ""},
        )
        self.assertFalse(resp.context["direct_only"])
        self.assertRowListed(resp, grand_app)

    def test_my_leaves_tab_has_no_direct_switch(self):
        manager, _ = self._pending_fixture()
        self.client.force_login(manager)
        resp = self.client.get(reverse("leave_list"), {"tab": "mine"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Direct reports only")

    def test_viewer_with_no_direct_reports_gets_the_full_scope_and_no_switch(self):
        """HR / Admins usually have no direct reports and the All tab is their
        working list — the switch would only ever show an empty tab, so it is
        hidden and the full scope shows. Deliberately unlike the roster grid's
        empty-default for HR (decided 2026-10-10)."""
        self._pending_fixture()
        self.client.force_login(self.hr)
        for tab in ("pending", "all"):
            resp = self.client.get(reverse("leave_list"), {"tab": tab})
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.context["has_direct_reports"])
            self.assertFalse(resp.context["direct_only"])
            self.assertRowListed(resp, self.alice_app)
            self.assertRowListed(resp, self.bob_app)
            self.assertNotContains(resp, "Direct reports only")
            self.assertNotIn("direct=", resp.context["calendar_url"])
        # An explicit ?direct=0 is a no-op for them, not an error.
        resp = self.client.get(reverse("leave_list"), {"tab": "all", "direct": "0"})
        self.assertRowListed(resp, self.alice_app)
        self.assertNotIn("direct=", resp.context["list_url"])

    def test_list_is_paginated_with_a_page_size_picker(self):
        for day in range(1, 27):
            LeaveApplication.objects.create(
                employee=self.alice,
                leave_type=self.leave_type,
                start_date=date(2026, 7, day),
                end_date=date(2026, 7, day),
                effective_days=Decimal("1.00"),
                working_days=Decimal("1.00"),
                status=LeaveApplication.Status.PENDING,
                applied_by=self.alice,
            )
        self.client.force_login(self.hr)
        resp = self.client.get(reverse("leave_list"), {"tab": "all"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["paginator"].count, 28)
        self.assertEqual(len(resp.context["rows"]), 25)
        self.assertTrue(resp.context["is_paginated"])
        self.assertContains(resp, "28 leave applications")

        resp = self.client.get(
            reverse("leave_list"), {"tab": "all", "per_page": 100},
        )
        self.assertEqual(len(resp.context["rows"]), 28)
        self.assertFalse(resp.context["is_paginated"])

    def test_pagination_carries_the_active_filters(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "start_date": "2026-08-01", "per_page": 50, "page": 1},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("start_date=2026-08-01", resp.context["query_suffix"])
        self.assertIn("per_page=50", resp.context["query_suffix"])

    def test_leave_date_filter_survives_tab_and_calendar_links(self):
        self.client.force_login(self.hr)
        resp = self.client.get(
            reverse("leave_list"),
            {"tab": "all", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("start_date=2026-08-01", resp.context["query_suffix"])
        self.assertIn("end_date=2026-08-31", resp.context["query_suffix"])
        self.assertTrue(resp.context["filters_active"])
