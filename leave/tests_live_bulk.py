"""The batch AL helpers (`get_or_create_balances`, `live_accrued_and_used_bulk`)
must return exactly what the per-employee functions do — the shift summary
export relies on it. Every branch of `live_accrued_al` / `live_used` /
`get_or_create_balance` gets a case here; add one when you add a branch.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from leave.models import LeaveBalance, LeavePolicy
from leave.services import (
    anniversary_cycle_window,
    get_or_create_balance,
    get_or_create_balances,
    live_accrued_al,
    live_accrued_and_used_bulk,
    live_used,
)
from roster.models import Shift, ShiftType


User = get_user_model()


class LiveBulkMatchesSingleTests(TestCase):
    def setUp(self):
        self.today = date.today()
        self.work = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        self.off = ShiftType.objects.create(name="OFF", category=ShiftType.Category.OFF)
        leave = {
            code: ShiftType.objects.create(name=code, category=ShiftType.Category.LEAVE)
            for code in ("AL", "CL", "LWP")
        }
        self.al_type = leave["AL"]
        self.al = LeavePolicy.objects.create(
            shift_type=leave["AL"],
            cycle=LeavePolicy.Cycle.ANNIVERSARY,
            accrual_method=LeavePolicy.AccrualMethod.WORKED_DAY_RATIO,
            accrual_ratio_denominator=18,
            max_days_per_year=12,
            is_paid=True,
        )
        LeavePolicy.objects.create(shift_type=leave["CL"], max_days_per_year=10, is_paid=True)
        LeavePolicy.objects.create(shift_type=leave["LWP"], is_paid=False)
        self.cl_type, self.lwp_type = leave["CL"], leave["LWP"]

        # Joined ~2y3m ago → the open cycle started ~90 days ago.
        self.joining = self.today.replace(year=self.today.year - 2) - timedelta(days=90)
        self.cycle_start, self.cycle_end = anniversary_cycle_window(self.joining, self.today)
        self.n = 0

    def _emp(self):
        self.n += 1
        return User.objects.create_user(
            email=f"e{self.n}@example.com", username=f"e{self.n}@example.com",
            password="x-1234!", employee_id=str(self.n), joining_date=self.joining,
        )

    def _shifts(self, emp, shift_type, count, offset=0):
        # bulk_create bypasses the ledger signal, so AL rows stay unreconciled
        # ("pending") — the case live_used exists for.
        Shift.objects.bulk_create(
            Shift(employee=emp, date=self.cycle_start + timedelta(days=offset + i), shift_type=shift_type)
            for i in range(count)
        )

    def _assert_bulk_matches_single(self, employees, upto):
        balances = get_or_create_balances(employees, self.al, upto)
        bulk = live_accrued_and_used_bulk(balances.values(), self.al, upto=upto)
        for emp in employees:
            single_bal = get_or_create_balance(emp, self.al, upto)
            bal = balances[emp.pk]
            self.assertEqual(bal.pk, single_bal.pk, emp.email)
            self.assertEqual(
                bulk[bal.pk],
                (live_accrued_al(emp, self.al, single_bal, upto=upto),
                 live_used(emp, self.al, single_bal, upto=upto)),
                emp.email,
            )
        return balances, bulk

    def _scenarios(self):
        open_cycle = self._emp()          # worked + paid + unpaid + pending AL
        self._shifts(open_cycle, self.work, 40)
        self._shifts(open_cycle, self.cl_type, 5, offset=40)
        self._shifts(open_cycle, self.lwp_type, 3, offset=45)
        self._shifts(open_cycle, self.al_type, 2, offset=48)
        self._shifts(open_cycle, self.off, 4, offset=50)
        get_or_create_balance(open_cycle, self.al, self.today)

        missing = self._emp()             # no balance row yet → created
        self._shifts(missing, self.work, 20)

        legacy = self._emp()              # pre-rework row, NULL cycle → healed
        self._shifts(legacy, self.work, 30)
        LeaveBalance.objects.create(employee=legacy, leave_policy=self.al, year=self.cycle_start.year)

        closed = self._emp()              # closed cycle → stored numbers
        self._shifts(closed, self.work, 50)
        LeaveBalance.objects.create(
            employee=closed, leave_policy=self.al, year=self.cycle_start.year,
            cycle_start=self.cycle_start, cycle_end=self.cycle_end,
            cycle_closed=True, accrued=Decimal("7"), used=Decimal("2"),
        )

        frozen = self._emp()              # frozen → stored accrual, live used
        self._shifts(frozen, self.work, 50)
        self._shifts(frozen, self.al_type, 1, offset=50)
        LeaveBalance.objects.create(
            employee=frozen, leave_policy=self.al, year=self.cycle_start.year,
            cycle_start=self.cycle_start, cycle_end=self.cycle_end,
            entitlement_frozen=True, accrued=Decimal("3"), used=Decimal("1"),
        )

        no_end = self._emp()              # cycle_start without cycle_end → fallback
        self._shifts(no_end, self.work, 25)
        LeaveBalance.objects.create(
            employee=no_end, leave_policy=self.al, year=self.cycle_start.year,
            cycle_start=self.cycle_start,
        )
        return [open_cycle, missing, legacy, closed, frozen, no_end]

    def test_matches_single_functions_today(self):
        employees = self._scenarios()

        balances, bulk = self._assert_bulk_matches_single(employees, self.today)

        open_cycle, missing, legacy = employees[:3]
        # 40 worked + 5 CL + 2 AL (paid) = 47 → 47/18 ≈ 2.61 → 2.5; 2 pending AL.
        self.assertEqual(bulk[balances[open_cycle.pk].pk], (Decimal("2.5"), Decimal("2")))
        self.assertTrue(LeaveBalance.objects.filter(employee=missing).exists())
        balances[legacy.pk].refresh_from_db()
        self.assertEqual(balances[legacy.pk].cycle_start, self.cycle_start)

    def test_matches_mid_cycle_and_before_cycle_start(self):
        employees = self._scenarios()

        self._assert_bulk_matches_single(employees, self.cycle_start + timedelta(days=20))
        self._assert_bulk_matches_single(employees, self.cycle_start - timedelta(days=1))

    def test_matches_when_capped_at_max_days_per_year(self):
        self.al.accrual_ratio_denominator = 1
        self.al.save()
        employees = self._scenarios()

        balances, bulk = self._assert_bulk_matches_single(employees, self.today)

        self.assertEqual(bulk[balances[employees[0].pk].pk][0], Decimal("12"))

    def test_query_count_does_not_grow_with_headcount(self):
        employees = [self._emp() for _ in range(8)]
        for emp in employees:
            self._shifts(emp, self.work, 10)
            get_or_create_balance(emp, self.al, self.today)

        # balances + paid leave types + one annotated count query.
        with self.assertNumQueries(3):
            balances = get_or_create_balances(employees, self.al, self.today)
            live_accrued_and_used_bulk(balances.values(), self.al, upto=self.today)
