"""16th -> 15th payroll period math.

A period is named after the month it *closes* in: "July 2026" covers
16 June - 15 July 2026. December rolls the start year back one.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Q


def period_bounds(year: int, month: int) -> tuple[date, date]:
    """Return (start_date, end_date) for the payroll period named (year, month)."""
    if month == 1:
        start_year, start_month = year - 1, 12
    else:
        start_year, start_month = year, month - 1
    return date(start_year, start_month, 16), date(year, month, 15)


def employed_during_q(start: date, end: date) -> Q:
    """Everyone employed at any point in start..end, active or not.

    The payroll population: a leaver is deactivated by deactivate_past_lwd
    the day after their LWD but is still paid for their final period, and a
    joiner whose joining_date falls after `end` isn't in it yet. Shared by
    the engine and the payroll report (Support Info) so the two can't count
    different people.

    An account without an employee_id (service accounts like "External
    Integrations Service Account", test logins like intern.hr01) is never
    an employee on the payroll, so it is out of the population too.
    """
    return (
        (Q(employee_id__isnull=False) & ~Q(employee_id=""))
        & (Q(joining_date__isnull=True) | Q(joining_date__lte=end))
        & (Q(last_working_date__isnull=True) | Q(last_working_date__gte=start))
    )


def period_for_date(d: date) -> tuple[int, int]:
    """Inverse of period_bounds: which (year, month) period contains `d`."""
    if d.day >= 16:
        month = d.month + 1
        year = d.year
        if month == 13:
            month = 1
            year += 1
        return year, month
    return d.year, d.month


def first_loan_deduction_month(disbursed_on: date) -> tuple[int, int]:
    """Salary-loan rule (docs/SALARY_LOAN_PLAN.md Q71, refined 2026-10-08):
    the payroll named for the calendar month AFTER the disbursement month.
    Month M's payroll (16/M-1 → 15/M) is the salary already being paid out
    when the money goes out in M, so the first instalment comes from M+1's:
    3 Mar → Mar 16–Apr 15 (April); 16 Sep → Sep 16–Oct 15 (October);
    14 Sep → the same October payroll, never Aug 16–Sep 15.
    """
    year, month = disbursed_on.year, disbursed_on.month + 1
    if month == 13:
        year, month = year + 1, 1
    return year, month
