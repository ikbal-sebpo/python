"""Salary adjustment — the pay still owed for earlier months once an
employee's salary is changed *retroactively* (docs/SALARY_ADJUSTMENT_PLAN.md).

SEBPO's increment takes effect 1 January but is first paid in August, and
the January–July difference is then paid in instalments; a later revision
of the same increment (say 120,817 → 130,000, still from January) widens
the difference for every month since, including the ones already settled.
So the balance is never a typed figure: for every payroll month from the
current structure's effective date up to the month before the run,

    owed(month) = current gross (× that month's proration) − gross actually paid

read from the CALCULATED Orbis run, else the imported pre-Orbis month,
else — for a month Orbis has no record of — assumed paid at the *previous*
structure's rate (flagged). The Payroll Admin decides per run how many
more **months** to settle; the engine pays

    Σ owed(first K + N months) − adjustments already paid

where K is the months earlier instalments settled (``PayrollRunEmployee.
adjustment_months`` / ``LegacyPayrollMonth.adjustment_months``). A revision
therefore tops up the already-settled months automatically in the next
instalment, and "remaining" is always current. Derived, never cached — the
salary-loan and tax-true-up posture; ``exclude_run`` keeps the run being
rebuilt out of its own inputs. Money columns are encrypted, so every sum
is Python over ``values_list`` rows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.money import format_taka as _tk
from payroll.models import (
    LegacyPayrollMonth, PayrollPeriod, PayrollRun, PayrollRunEmployee, PayrollRunLine, SalaryStructure,
)
from payroll.periods import period_bounds, period_for_date

ADJUSTMENT_COMPONENT_CODE = "SALARY_ADJUSTMENT"
GROSS_CODE = "GROSS_SALARY"

SOURCE_RUN = "RUN"            # gross paid read from the calculated Orbis run
SOURCE_LEGACY = "LEGACY"      # …from the imported pre-Orbis month
SOURCE_ASSUMED = "ASSUMED"    # no record: assumed paid at the previous structure's rate
SOURCE_NONE = "NONE"          # no record and no previous structure: nothing can be owed

_TWO = Decimal("0.01")


def _q2(value) -> Decimal:
    return Decimal(value).quantize(_TWO, rounding=ROUND_HALF_UP)


@dataclass
class OwedMonth:
    year: int
    month: int
    rate: Decimal                 # the current structure's gross
    paid: Decimal                 # gross actually paid that month
    factor: Decimal               # the paid month's proration (1 when unknown)
    source: str

    @property
    def label(self) -> str:
        return f"{date(self.year, self.month, 1):%b %Y}"

    @property
    def owed(self) -> Decimal:
        return max(Decimal("0"), _q2(self.rate * self.factor) - self.paid)


@dataclass
class AdjustmentLedger:
    employee_id: object
    structure: SalaryStructure | None
    months: list[OwedMonth] = field(default_factory=list)
    paid_total: Decimal = Decimal("0")      # SALARY_ADJUSTMENT already paid inside the window
    months_settled: int = 0                 # K — months earlier instalments covered

    @property
    def owed_total(self) -> Decimal:
        return sum((m.owed for m in self.months), Decimal("0"))

    @property
    def balance(self) -> Decimal:
        return self.owed_total - self.paid_total

    @property
    def remaining_months(self) -> int:
        return max(0, len(self.months) - self.months_settled)

    @property
    def assumed_months(self) -> list[OwedMonth]:
        return [m for m in self.months if m.source == SOURCE_ASSUMED]

    @property
    def has_pay_record(self) -> bool:
        """At least one month in the window was read from a real record (an
        Orbis run or an imported month). Without one the whole balance would
        rest on the previous-rate assumption — including "nothing paid yet"
        — so the engine refuses to pay rather than guess."""
        return any(m.source in (SOURCE_RUN, SOURCE_LEGACY) for m in self.months)

    def instalment(self, months: int | None, settle_all: bool) -> tuple[int, Decimal]:
        """(months settled by this run, taka to pay). The taka covers the
        newly settled months *and* any shortfall on months settled earlier
        at a lower rate (a revision), and is never negative."""
        if settle_all:
            months_now = self.remaining_months
        else:
            months_now = min(int(months or 0), self.remaining_months)
        upto = self.months_settled + months_now
        owed_upto = sum((m.owed for m in self.months[:upto]), Decimal("0"))
        return months_now, max(Decimal("0"), owed_upto - self.paid_total)

    # -- the explanation HR and Finance read ---------------------------------

    @staticmethod
    def _span(months: list["OwedMonth"]) -> str:
        if not months:
            return ""
        first, last = months[0], months[-1]
        if len(months) == 1:
            return first.label
        if first.year == last.year:
            return f"{date(first.year, first.month, 1):%b}–{last.label}"
        return f"{first.label}–{last.label}"

    @staticmethod
    def _groups(months: list["OwedMonth"]) -> list[list["OwedMonth"]]:
        """Consecutive months paid at the same rate, so 'Jan–Jul paid at
        109,834, short 20,166 × 7' reads as one line."""
        groups: list[list[OwedMonth]] = []
        for m in months:
            if groups and (groups[-1][-1].paid, groups[-1][-1].owed) == (m.paid, m.owed):
                groups[-1].append(m)
            else:
                groups.append([m])
        return groups

    def _group_text(self, months: list["OwedMonth"], *, with_paid: bool = True) -> str:
        parts = []
        for g in self._groups(months):
            total = sum((m.owed for m in g), Decimal("0"))
            paid = f" paid at {_tk(g[0].paid)}," if with_paid else ""
            if len(g) == 1:
                parts.append(f"{self._span(g)}{paid} short {_tk(g[0].owed)}")
            else:
                parts.append(f"{self._span(g)}{paid} short {_tk(g[0].owed)} × {len(g)} = {_tk(total)}")
        return "; ".join(parts)

    def explain(self, months_now: int, amount: Decimal) -> list[str]:
        """The breakdown behind this run's figure, one sentence per step —
        owed to date, what earlier instalments covered (and the top-up a
        revision created), this run's pieces, what remains."""
        rate = _tk(self.structure.gross_monthly) if self.structure else "?"
        K = self.months_settled
        settled_before = self.months[:K]
        settled_now = self.months[K:K + months_now]
        remaining = self.months[K + months_now:]
        lines = [
            f"Owed to date at {rate}: {self._group_text(self.months)}; total {_tk(self.owed_total)}.",
        ]
        worth_before = sum((m.owed for m in settled_before), Decimal("0"))
        topup = worth_before - self.paid_total
        if K:
            s = (
                f"Paid before: {_tk(self.paid_total)} for {K} month{'s' if K != 1 else ''} ({self._span(settled_before)}), "
                f"worth {_tk(worth_before)} at {rate}"
            )
            s += f" → top-up {_tk(topup)}." if topup > 0 else "."
            lines.append(s)
        elif self.paid_total:
            lines.append(f"Paid before: {_tk(self.paid_total)} (months not recorded).")
        if settled_now:
            now_total = sum((m.owed for m in settled_now), Decimal("0"))
            pieces = self._group_text(settled_now, with_paid=False)
            if topup > 0:
                lines.append(f"This run: {pieces}; new months {_tk(now_total)} + top-up {_tk(topup)} = {_tk(amount)}.")
            elif len(self._groups(settled_now)) > 1:
                lines.append(f"This run: {pieces}; total {_tk(amount)}.")
            else:
                lines.append(f"This run: {pieces}.")
        elif amount > 0:
            lines.append(f"This run: top-up only, {_tk(amount)} — nothing new settled.")
        else:
            lines.append("This run: nothing to pay.")
        if remaining:
            rem_total = sum((m.owed for m in remaining), Decimal("0"))
            lines.append(
                f"Remaining after this run: {self._group_text(remaining, with_paid=False)}; "
                f"total {_tk(rem_total)} ({len(remaining)} month{'s' if len(remaining) != 1 else ''})."
            )
        else:
            lines.append("Remaining after this run: nothing — the adjustment is complete.")
        return lines

    def summary(self, months_now: int, amount: Decimal) -> str:
        """The compact one-liner that heads the pay line's note."""
        rate = _tk(self.structure.gross_monthly) if self.structure else "?"
        K = self.months_settled
        settled_now = self.months[K:K + months_now]
        remaining = self.months[K + months_now:]
        now_total = sum((m.owed for m in settled_now), Decimal("0"))
        topup = amount - now_total
        rem_total = sum((m.owed for m in remaining), Decimal("0"))
        head = (
            f"{months_now} month{'s' if months_now != 1 else ''} ({self._span(settled_now)}) at {rate}"
            if settled_now else "Top-up only"
        )
        if topup > 0 and settled_now:
            head += f": {_tk(now_total)} + top-up {_tk(topup)}"
        return (
            f"{head} = {_tk(amount)} · owed to date {_tk(self.owed_total)} · paid before {_tk(self.paid_total)}"
            f" ({K} mo) · remaining {_tk(rem_total)} ({len(remaining)} mo)"
        )

    def note(self, months_now: int, amount: Decimal) -> str:
        """What the pay line stores: the summary, then the breakdown, one
        step per line — self-contained for the admin, a payslip or an export."""
        return "\n".join([self.summary(months_now, amount)] + self.explain(months_now, amount))

    def as_json(self, months_now: int, amount: Decimal) -> dict:
        return {
            "explanation": self.explain(months_now, amount),
            "structure": str(self.structure.pk) if self.structure else None,
            "rate": str(self.structure.gross_monthly) if self.structure else None,
            "months": [
                {"month": f"{m.year}-{m.month:02d}", "paid": str(m.paid), "factor": str(m.factor),
                 "owed": str(m.owed), "source": m.source}
                for m in self.months
            ],
            "months_settled_before": self.months_settled,
            "paid_before": str(self.paid_total),
            "months_settled_now": months_now,
            "amount": str(amount),
            "remaining_after": max(0, self.remaining_months - months_now),
        }


def _window(structure, employee, upto_period) -> list[tuple[int, int]]:
    """The payroll months a structure can leave owed: from the period its
    effective date falls in (the period *named* for the month, SEBPO's
    full-month practice — not the engine's day-level proration) up to the
    month before ``upto_period``, skipping months before the employee
    joined or after they left."""
    y, m = period_for_date(structure.effective_from)
    out: list[tuple[int, int]] = []
    while (y, m) < (upto_period.year, upto_period.month):
        start, end = period_bounds(y, m)
        if not (employee.joining_date and employee.joining_date > end) and not (
            employee.last_working_date and employee.last_working_date < start
        ):
            out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def adjustment_ledgers(employees, upto_period, *, exclude_run=None) -> dict:
    """``{employee_pk: AdjustmentLedger}`` for every employee with a current
    structure, in a handful of queries for the whole population."""
    employees = list(employees)
    ids = [e.pk for e in employees]
    current: dict = {}
    for s in SalaryStructure.objects.filter(
        employee_id__in=ids, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
    ):
        current[s.employee_id] = s
    previous: dict = {}
    for s in SalaryStructure.objects.filter(employee_id__in=ids, status=SalaryStructure.Status.SUPERSEDED):
        cur = current.get(s.employee_id)
        if cur is not None and s.effective_to == cur.effective_from - timedelta(days=1):
            previous[s.employee_id] = s

    windows: dict = {}
    all_months: set[tuple[int, int]] = set()
    for emp in employees:
        s = current.get(emp.pk)
        if s is None:
            continue
        windows[emp.pk] = _window(s, emp, upto_period)
        all_months.update(windows[emp.pk])
    ledgers = {pk: AdjustmentLedger(employee_id=pk, structure=current.get(pk)) for pk in windows}
    if not all_months:
        return ledgers

    # --- what Orbis paid: the newest CALCULATED regular run per month -----
    years = {y for y, _ in all_months}
    runs: dict[tuple[int, int], PayrollRun] = {}
    run_qs = PayrollRun.objects.filter(
        period__period_type=PayrollPeriod.PeriodType.STANDARD, period__year__in=years,
        run_type=PayrollRun.RunType.REGULAR, status=PayrollRun.Status.CALCULATED,
    ).select_related("period").order_by("-calculated_at", "-pk")
    if exclude_run is not None:
        run_qs = run_qs.exclude(pk=exclude_run.pk)
    for r in run_qs:
        key = (r.period.year, r.period.month)
        if key in all_months:
            runs.setdefault(key, r)
    month_of_run = {r.pk: key for key, r in runs.items()}
    run_emp: dict = {}          # (emp, (y, m)) → (factor, adjustment_months)
    run_emp_pk_key: dict = {}   # run_employee pk → (emp, (y, m))
    if runs:
        for pk, emp_id, run_id, factor, adj_months in PayrollRunEmployee.objects.filter(
            run_id__in=list(month_of_run), employee_id__in=ids, status=PayrollRunEmployee.Status.INCLUDED,
        ).values_list("pk", "employee_id", "run_id", "proration_factor", "adjustment_months"):
            key = (emp_id, month_of_run[run_id])
            run_emp[key] = (Decimal(factor or 0), adj_months or 0)
            run_emp_pk_key[pk] = key
    run_gross: dict = defaultdict(lambda: Decimal("0"))
    run_adjust: dict = defaultdict(lambda: Decimal("0"))
    if run_emp_pk_key:
        for re_pk, code, amount in PayrollRunLine.objects.filter(
            run_employee_id__in=list(run_emp_pk_key), component_code__in=(GROSS_CODE, ADJUSTMENT_COMPONENT_CODE),
        ).values_list("run_employee_id", "component_code", "amount"):
            target = run_gross if code == GROSS_CODE else run_adjust
            target[run_emp_pk_key[re_pk]] += amount or Decimal("0")

    # --- what the pre-Orbis import says ------------------------------------
    legacy: dict = {}
    for lm in LegacyPayrollMonth.objects.filter(employee_id__in=ids, year__in=years):
        if (lm.year, lm.month) in all_months:
            lines = lm.lines or {}
            legacy[(lm.employee_id, (lm.year, lm.month))] = (
                Decimal(str(lines.get(GROSS_CODE, 0) or 0)) if GROSS_CODE in lines else None,
                Decimal(str(lines.get(ADJUSTMENT_COMPONENT_CODE, 0) or 0)),
                lm.adjustment_months or 0,
            )

    for emp in employees:
        ledger = ledgers.get(emp.pk)
        if ledger is None:
            continue
        rate = ledger.structure.gross_monthly
        prev = previous.get(emp.pk)
        candidates: list[OwedMonth] = []
        for ym in windows[emp.pk]:
            key = (emp.pk, ym)
            if key in run_emp:
                factor, adj_months = run_emp[key]
                candidates.append(OwedMonth(ym[0], ym[1], rate, run_gross[key], factor, SOURCE_RUN))
                ledger.paid_total += run_adjust[key]
                ledger.months_settled += adj_months
            elif key in legacy:
                gross, adjust, adj_months = legacy[key]
                if gross is None:
                    gross = prev.gross_monthly if prev is not None else rate
                    source = SOURCE_ASSUMED if prev is not None else SOURCE_NONE
                else:
                    source = SOURCE_LEGACY
                candidates.append(OwedMonth(ym[0], ym[1], rate, gross, Decimal("1"), source))
                ledger.paid_total += adjust
                ledger.months_settled += adj_months
            elif prev is not None:
                candidates.append(OwedMonth(ym[0], ym[1], rate, prev.gross_monthly, Decimal("1"), SOURCE_ASSUMED))
            else:
                candidates.append(OwedMonth(ym[0], ym[1], rate, rate, Decimal("1"), SOURCE_NONE))
        # Only months with a shortfall are "owed months" — a month already
        # paid at the current rate neither counts towards "remaining" nor
        # takes an instalment slot. Chronological, so the first K are the
        # ones earlier instalments settled.
        ledger.months = [m for m in candidates if m.owed > 0]
    return ledgers
