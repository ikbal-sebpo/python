"""Salary loan balances and the instalment rule (docs/SALARY_LOAN_PLAN.md).

Pure reads and arithmetic, no writes — engine.py's loan stage persists the
PayrollRunLine, the views render what these functions return.

The balance is derived, never cached:

    outstanding = principal − opening_recovered − Σ LOAN_REPAYMENT lines in
                  CALCULATED runs − Σ adjustments.delta

Every term is an encrypted column, so the sums happen in Python over
``values_list`` rows — same discipline as the tax true-up's "TDS already
deducted". Loans are rare (≈100 live at cutover), so this is cheap.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from payroll.models import PayrollRun, PayrollRunLine, SalaryLoan, SalaryLoanAdjustment

LOAN_COMPONENT_CODE = "LOAN_REPAYMENT"
LOAN_SOURCE_KIND = "SalaryLoan"

ZERO = Decimal("0")


def loan_source_ref(loan_pk) -> str:
    return f"{LOAN_SOURCE_KIND}:{loan_pk}"


def _loan_pk_from_ref(source_ref: str):
    prefix = LOAN_SOURCE_KIND + ":"
    if not source_ref or not source_ref.startswith(prefix):
        return None
    try:
        return int(source_ref[len(prefix):])
    except ValueError:
        return None


def repayment_lines(loan_pks, *, exclude_run=None):
    """Every LOAN_REPAYMENT line for these loans in a CALCULATED run — the
    run being (re)calculated is excluded so its own old rows (already
    deleted inside the transaction anyway) can never count."""
    if not loan_pks:
        return PayrollRunLine.objects.none()
    qs = PayrollRunLine.objects.filter(
        component_code=LOAN_COMPONENT_CODE, source_kind=LOAN_SOURCE_KIND,
        source_ref__in=[loan_source_ref(pk) for pk in loan_pks],
        run_employee__run__status=PayrollRun.Status.CALCULATED,
    )
    if exclude_run is not None:
        qs = qs.exclude(run_employee__run=exclude_run)
    return qs


def recovered_via_payroll(loan_pks, *, exclude_run=None) -> tuple[dict, dict]:
    """(amount recovered, instalment count) per loan pk."""
    amounts: dict = defaultdict(lambda: ZERO)
    counts: dict = defaultdict(int)
    for source_ref, amount in repayment_lines(loan_pks, exclude_run=exclude_run).values_list("source_ref", "amount"):
        pk = _loan_pk_from_ref(source_ref)
        if pk is None:
            continue
        amounts[pk] += amount or ZERO
        counts[pk] += 1
    return amounts, counts


def adjustments_total(loan_pks) -> dict:
    totals: dict = defaultdict(lambda: ZERO)
    if not loan_pks:
        return totals
    for loan_id, delta in SalaryLoanAdjustment.objects.filter(loan_id__in=loan_pks).values_list("loan_id", "delta"):
        totals[loan_id] += delta or ZERO
    return totals


@dataclass
class LoanBalance:
    loan: SalaryLoan
    recovered_in_orbis: Decimal = ZERO
    instalments_in_orbis: int = 0
    adjustments: Decimal = ZERO

    @property
    def opening(self) -> Decimal:
        return self.loan.opening_recovered or ZERO

    @property
    def recovered(self) -> Decimal:
        """Everything that has reduced the balance, from every source."""
        return self.opening + self.recovered_in_orbis + self.adjustments

    @property
    def outstanding(self) -> Decimal:
        return max(ZERO, (self.loan.principal or ZERO) - self.recovered)

    @property
    def is_settled(self) -> bool:
        return ((self.loan.principal or ZERO) - self.recovered) <= 0

    @property
    def instalments_deducted(self) -> int:
        """Instalments so far, counting the pre-Orbis ones by amount — the
        "4 of 10" on the register and the profile card."""
        amount = self.loan.installment_amount or ZERO
        before = int(self.opening // amount) if amount > 0 else 0
        return before + self.instalments_in_orbis

    @property
    def progress_pct(self) -> int:
        principal = self.loan.principal or ZERO
        if principal <= 0:
            return 0
        return int(min(Decimal("100"), self.recovered / principal * 100))

    @property
    def next_instalment(self) -> Decimal:
        if self.is_settled:
            return ZERO
        return min(self.loan.installment_amount or ZERO, self.outstanding)


def loan_balances(loans, *, exclude_run=None) -> dict:
    """{loan.pk: LoanBalance} for an iterable of SalaryLoan rows."""
    loans = list(loans)
    pks = [loan.pk for loan in loans]
    amounts, counts = recovered_via_payroll(pks, exclude_run=exclude_run)
    adjustments = adjustments_total(pks)
    return {
        loan.pk: LoanBalance(
            loan=loan, recovered_in_orbis=amounts.get(loan.pk, ZERO),
            instalments_in_orbis=counts.get(loan.pk, 0), adjustments=adjustments.get(loan.pk, ZERO),
        )
        for loan in loans
    }


@dataclass(frozen=True)
class InstalmentDecision:
    amount: Decimal
    warning_code: str = ""
    message: str = ""
    full_balance: bool = False
    shortfall: Decimal = ZERO


def decide_instalment(
    *, installment_amount: Decimal, outstanding: Decimal, available_net: Decimal,
    leaving: bool = False, skipped: bool = False,
) -> InstalmentDecision:
    """The one rule the engine applies (plan §4, Q68/Q69):

    * skipped (SalaryLoanSkip for this period) → 0, LOAN_INSTALMENT_SKIPPED
    * leaving (last working date inside the period) → the whole balance
    * otherwise min(agreed instalment, outstanding)
    * never more than the net pay left after every other deduction and tax;
      the shortfall rolls forward with LOAN_INSTALMENT_REDUCED (or
      LOAN_OUTSTANDING_AT_SEPARATION when leaving).
    """
    outstanding = max(ZERO, Decimal(outstanding))
    if outstanding <= 0:
        return InstalmentDecision(ZERO)
    if skipped:
        return InstalmentDecision(
            ZERO, "LOAN_INSTALMENT_SKIPPED",
            "Loan instalment skipped this period on HR's approval; the balance is unchanged.",
        )
    wanted = outstanding if leaving else min(Decimal(installment_amount or ZERO), outstanding)
    available = max(ZERO, Decimal(available_net))
    if wanted <= available:
        return InstalmentDecision(wanted, full_balance=leaving)
    shortfall = wanted - available
    if leaving:
        return InstalmentDecision(
            available, "LOAN_OUTSTANDING_AT_SEPARATION",
            f"Final payroll could not cover the loan balance — {shortfall:.0f} still outstanding after this "
            "run; recover it in the final settlement.",
            full_balance=True, shortfall=shortfall,
        )
    return InstalmentDecision(
        available, "LOAN_INSTALMENT_REDUCED",
        f"Loan instalment reduced to what net pay could cover — {shortfall:.0f} short this period, "
        "carried forward.",
        shortfall=shortfall,
    )


@dataclass
class LoanHistoryEntry:
    kind: str                 # "instalment" | "adjustment" | "skip" | "opening"
    occurred_on: object       # date
    label: str
    amount: Decimal
    note: str = ""
    run_pk: int | None = None
    extra: dict = field(default_factory=dict)


def loan_history(loan) -> list[LoanHistoryEntry]:
    """Newest first: instalment lines (with their run), adjustments, skips,
    and the cutover opening as a single row at the top of the past."""
    entries: list[LoanHistoryEntry] = []
    lines = (
        repayment_lines([loan.pk])
        .select_related("run_employee__run__period")
        .order_by("run_employee__run__period__start_date")
    )
    for line in lines:
        run = line.run_employee.run
        entries.append(LoanHistoryEntry(
            kind="instalment", occurred_on=run.period.end_date,
            label=f"Instalment · {run.period}", amount=line.amount, note=line.calc_note, run_pk=run.pk,
        ))
    for adj in loan.adjustments.select_related("created_by"):
        entries.append(LoanHistoryEntry(
            kind="adjustment", occurred_on=adj.occurred_on, label=adj.get_reason_display(),
            amount=adj.delta, note=adj.note,
            extra={"by": adj.created_by.display_name if adj.created_by else ""},
        ))
    for skip in loan.skips.select_related("period", "approved_by_hr"):
        entries.append(LoanHistoryEntry(
            kind="skip", occurred_on=skip.period.end_date, label=f"Skipped · {skip.period}", amount=ZERO,
            note=skip.reason, extra={"by": skip.approved_by_hr.display_name},
        ))
    if loan.opening_recovered:
        entries.append(LoanHistoryEntry(
            kind="opening", occurred_on=loan.disbursed_on or loan.approved_on,
            label="Recovered before Orbis", amount=loan.opening_recovered,
        ))
    entries.sort(key=lambda e: (e.occurred_on, e.kind != "opening"), reverse=True)
    return entries
