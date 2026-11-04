"""In-house ITA-2023 salary tax calculation (Phase 4).

Pure calculation, no DB writes — engine.py's Tax stage is what persists a
TaxComputation row from ``compute_tax_for_employee()``'s result. Confirmed
2026-08-24 against Finance's actual Tax Calculation.xlsx — see
docs/PAYROLL_PLAN.md's "Tax calculation review + Phase 4 build, 2026-08-24"
section for the full formula walkthrough this reproduces.

``slabs``/``threshold_amount`` are passed in rather than queried here so the
engine can resolve them once per run (not once per employee) — this module
stays free of any hidden per-call query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")

# gross_monthly ceiling -> non-taxable mobile/internet reimbursement, carved
# out before annualising. Above the last ceiling, the flat Tk 6,000 applies.
_MOBILE_INTERNET_TIERS = (
    (Decimal("20000"), Decimal("0")),
    (Decimal("35000"), Decimal("1000")),
    (Decimal("80000"), Decimal("2000")),
    (Decimal("110000"), Decimal("3000")),
    (Decimal("150000"), Decimal("4000")),
    (Decimal("200000"), Decimal("5000")),
)
_MOBILE_INTERNET_ABOVE = Decimal("6000")


def _q2(value) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _mobile_internet_reimburse(gross_monthly: Decimal) -> Decimal:
    for ceiling, amount in _MOBILE_INTERNET_TIERS:
        if gross_monthly <= ceiling:
            return amount
    return _MOBILE_INTERNET_ABOVE


def mobile_internet_reimburse(gross_monthly) -> Decimal:
    """Public name for the tier lookup — the monthly tax ledger
    (payroll/tax_ledger.py) reports an Orbis month's reimbursement with the
    exact figure the projection carves out, never a stored one."""
    return _mobile_internet_reimburse(Decimal(gross_monthly or 0))


# Earning components that are NOT "additional pay" for the tax projection:
# base salary is annualised separately (×12) and the festival bonus is already
# assumed at one month's gross, so counting the real FESTIVAL_BONUS lines too
# would double-count (Q2, 2026-10-07). Everything else that is kind=EARNING and
# is_taxable=True counts — OT, night, special-shift transport, incentives,
# SLA, the performance bonus (BONUS), salary adjustments (arrears), and any future component
# seeded taxable.
#
# Until 2026-10-08 this set held "BONUS", on the assumption that BONUS *was*
# the festival bonus. SEBPO pays both, so the festival bonus became its own
# code (FESTIVAL_BONUS, seed_payroll_components) and BONUS — a performance
# bonus — counts like an incentive. docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §5.1.
TAX_EXCLUDED_EARNING_CODES = frozenset({"GROSS_SALARY", "FESTIVAL_BONUS"})
TAX_YEAR_MONTHS = 12


@dataclass
class TaxBreakdown:
    mobile_internet_reimburse: Decimal
    gross_annual_projected: Decimal
    exempted_income: Decimal
    taxable_income: Decimal
    tax_before_rebate: Decimal
    total_declared_investment: Decimal
    rebate_amount: Decimal
    net_tax_payable: Decimal
    source_tax: Decimal
    ait_car_advance_tax: Decimal
    annual_tds: Decimal
    monthly_tds: Decimal
    additional_pay_ytd: Decimal = Decimal("0")
    tds_already_deducted: Decimal = Decimal("0")
    tax_remaining: Decimal = Decimal("0")
    months_remaining: int = TAX_YEAR_MONTHS
    warnings: list[str] = field(default_factory=list)
    slab_breakdown: list[dict] = field(default_factory=list)


def is_trueup(tax_year) -> bool:
    return tax_year.calculation_method == "ITA_2023_SALARY_TRUEUP"


def months_remaining_for(tax_year, period) -> int:
    """Months of the tax year still to be paid, counting the given period as
    one of them: the July period (end_date in July) → 12 … June → 1. Finance's
    sheet divides by 9 in October after three months deducted — Oct is the
    4th month, 12 − 3 = 9. Calendar-based, never a count of runs, so a
    missed month doesn't stretch the remaining ones.
    """
    start = tax_year.start_date
    elapsed = (period.year - start.year) * 12 + (period.month - start.month)
    return max(1, min(TAX_YEAR_MONTHS, TAX_YEAR_MONTHS - elapsed))


@dataclass(frozen=True)
class TaxableIncomeProjection:
    mobile_internet_reimburse: Decimal
    annual_gross: Decimal
    exempted_income: Decimal
    taxable_income: Decimal


def project_taxable_income(
    tax_year, gross_monthly: Decimal, additional_pay: Decimal = Decimal("0"),
) -> TaxableIncomeProjection:
    """Steps 1-3 of the confirmed formula — annualise the current gross (less
    the non-taxable mobile/internet tier, plus one month's festival bonus),
    add the year's taxable extra pay to date (Finance's "Incentives" line —
    0 under the flat method and for the allowable-investment snapshot), then
    take off the statutory exemption. Shared by the per-run tax calculation
    and the once-a-year allowable-investment snapshot so the two can never
    disagree about what "taxable income" means.
    """
    gross_monthly = Decimal(gross_monthly)
    mobile_internet = _mobile_internet_reimburse(gross_monthly)
    annual_gross = (gross_monthly - mobile_internet) * 12 + gross_monthly + Decimal(additional_pay)
    exempted_income = min(annual_gross / tax_year.exemption_divisor, tax_year.exemption_cap)
    return TaxableIncomeProjection(
        mobile_internet_reimburse=mobile_internet,
        annual_gross=annual_gross,
        exempted_income=exempted_income,
        taxable_income=annual_gross - exempted_income,
    )


def allowable_investment(tax_year, gross_monthly: Decimal) -> Decimal:
    """Finance's "Allowable Investment" line — ``allowable_investment_pct``
    (30%) of projected taxable income, in whole taka rounded **down** so the
    10%-of-investment rebate term can never exceed the 3%-of-income term by
    a rounding paisa. Matches the figure Finance types into their template
    (308,286 from 308,286.30 in the 2026-10-07 example).
    """
    taxable = project_taxable_income(tax_year, gross_monthly).taxable_income
    return (taxable * tax_year.allowable_investment_pct).quantize(Decimal("1"), rounding=ROUND_DOWN)


def compute_tax_for_employee(
    employee, tax_year, gross_monthly: Decimal, declaration, *, slabs, threshold_amount: Decimal,
    additional_pay_ytd: Decimal = Decimal("0"), tds_already_deducted: Decimal = Decimal("0"),
    months_remaining: int = TAX_YEAR_MONTHS,
) -> TaxBreakdown:
    """``slabs`` — an ordered (by sequence) list/queryset of TaxSlab rows for
    ``tax_year``. ``threshold_amount`` — the resolved TaxExemptionThreshold for
    ``declaration.category`` in ``tax_year`` (0 if none configured).

    The three keyword inputs drive the year-to-date true-up and are only
    read when ``tax_year.calculation_method`` is the TRUEUP method
    (docs/TAX_CALCULATION_TRUEUP_PLAN.md); the flat method ignores them so
    historical runs reproduce exactly. ``additional_pay_ytd`` already
    includes the current period's own extra lines (engine.py sums them
    before the Tax stage). ``tds_already_deducted`` is the opening figure
    plus every earlier period's actual TDS line (manual overrides included).
    """
    trueup = is_trueup(tax_year)
    additional_pay_ytd = Decimal(additional_pay_ytd) if trueup else Decimal("0")
    projection = project_taxable_income(tax_year, gross_monthly, additional_pay_ytd)
    mobile_internet = projection.mobile_internet_reimburse
    annual_gross = projection.annual_gross
    exempted_income = projection.exempted_income
    taxable_income = projection.taxable_income

    slab_breakdown: list[dict] = []
    remaining = taxable_income
    tax_before_rebate = Decimal("0")

    band_amount = max(Decimal("0"), min(remaining, threshold_amount))
    slab_breakdown.append({
        "band": "exemption_threshold", "width": str(threshold_amount),
        "rate": "0.0000", "taxed_amount": str(band_amount), "tax": "0.00",
    })
    remaining -= band_amount

    for slab in slabs:
        if remaining <= 0:
            break
        width = remaining if slab.width is None else min(remaining, slab.width)
        band_tax = width * slab.rate
        slab_breakdown.append({
            "band": slab.sequence, "width": str(width), "rate": str(slab.rate),
            "taxed_amount": str(width), "tax": str(_q2(band_tax)),
        })
        tax_before_rebate += band_tax
        remaining -= width

    tax_before_rebate = _q2(tax_before_rebate)

    # The employee's once-a-year commitment (defaults to the allowable
    # snapshot) — revised 2026-10-07, replacing the six itemised fields.
    total_investment = declaration.declared_investment
    rebate_amount = _q2(max(Decimal("0"), min(
        taxable_income * tax_year.rebate_income_pct,
        total_investment * tax_year.rebate_investment_pct,
        tax_year.rebate_cap,
    )))

    net_tax_payable = max(tax_before_rebate - rebate_amount, tax_year.minimum_tax)
    ait = declaration.ait_car_advance_tax or Decimal("0")
    annual_tds = net_tax_payable - declaration.source_tax - ait

    warnings: list[str] = []
    if trueup:
        # Finance's sheet, rows 34–38: what is still owed for the year,
        # spread over the months left — whole taka (Q6), so the deducted
        # line equals what the payslip prints and the YTD sum stays exact.
        tds_already_deducted = Decimal(tds_already_deducted)
        months_remaining = max(1, int(months_remaining))
        tax_remaining = annual_tds - tds_already_deducted
        if tax_remaining < 0:
            warnings.append(
                f"Tax already deducted this year exceeds what is owed by {-tax_remaining:.2f} — "
                "over-deducted (late AIT or a lower projection). Monthly TDS clamped to 0; "
                "the refund is a return-filing matter."
            )
        monthly_tds = (max(tax_remaining, Decimal("0")) / months_remaining).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        )
    else:
        tds_already_deducted = Decimal("0")
        months_remaining = TAX_YEAR_MONTHS
        tax_remaining = annual_tds
        if annual_tds < 0:
            warnings.append(
                f"Calculated annual TDS is negative ({annual_tds}) — declared source tax / AIT "
                "exceeds net tax payable. Monthly TDS line clamped to 0."
            )
        monthly_tds = _q2(max(annual_tds, Decimal("0")) / TAX_YEAR_MONTHS)

    return TaxBreakdown(
        mobile_internet_reimburse=mobile_internet,
        gross_annual_projected=_q2(annual_gross),
        exempted_income=_q2(exempted_income),
        taxable_income=_q2(taxable_income),
        tax_before_rebate=tax_before_rebate,
        total_declared_investment=total_investment,
        rebate_amount=rebate_amount,
        net_tax_payable=_q2(net_tax_payable),
        source_tax=declaration.source_tax,
        ait_car_advance_tax=ait,
        annual_tds=_q2(annual_tds),
        monthly_tds=monthly_tds,
        additional_pay_ytd=_q2(additional_pay_ytd),
        tds_already_deducted=_q2(tds_already_deducted),
        tax_remaining=_q2(tax_remaining),
        months_remaining=months_remaining,
        warnings=warnings,
        slab_breakdown=slab_breakdown,
    )
