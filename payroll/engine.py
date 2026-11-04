"""Calculation engine (Phase 3, tax stage rebuilt Phase 4, night/OT/transport
rebuilt 2026-09-07 for the AdOps policy — docs/PAYROLL_PLAN.md).

Stages actually wired, in order: population -> attendance -> structure
earnings -> variable earnings (night duty — Dedicated/Custom/Regular via
payroll_inputs.classify_night_shifts; overtime — per-department PER_HOUR/
PER_OCCURRENCE basis via payroll_inputs.collect_overtime, one line per
overtime_type; transport allowance — CUST shift-days worked from office;
bonus/incentive/disciplinary wired, no data yet) -> gross -> meal deduction
(Phase 7, 2026-09-09: tiered by MealDeductionTier against gross, quantity
from persisted MealCount rows pulled from meals.sebpo.net at lock_inputs()
time — payroll_inputs.collect_meal_counts, never a live API call here) -> tax
(TDSEntry manual override checked first; otherwise calculated in-system from
EmployeeTaxDeclaration + tax.py — see docs/PAYROLL_PLAN.md's 2026-08-24 tax
build note) -> net + rounding -> roll-up. Loan recovery is wired
structurally but no-ops (no SalaryLoan model yet — Phase 6).

Immutability: delete-and-rebuild, never patch (design principle 6) — every
call to calculate_run() wipes this run's PayrollRunEmployee/Line/ControlCheck
rows and regenerates them from scratch inside one transaction.

Only reaches CALCULATED. No approval chain, no lock/paid/reversal — Phase 5,
still gated on HR/Finance sign-off same as before.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.permissions import effective_access_level
from employees.models import JobInfo
from payroll import inputs as payroll_inputs
from payroll.models import (
    ENGINE_VERSION,
    EmployeeBankAccount,
    EmployeeTaxDeclaration,
    OvertimeEntry,
    OvertimeType,
    MealDeductionTier,
    PayComponent,
    PayRate,
    PayrollControlCheck,
    PayrollPeriod,
    PayrollRun,
    PayrollRunAudit,
    PayrollRunEmployee,
    PayrollRunLine,
    PayrollSettings,
    PayrollVariance,
    SalaryLoan,
    SalaryLoanSkip,
    SalaryStructure,
    TaxComputation,
    TaxExemptionThreshold,
    TaxYear,
    TDSEntry,
    VariablePayEntry,
)
from payroll.adjustments import ADJUSTMENT_COMPONENT_CODE, adjustment_ledgers
from payroll.loans import LOAN_COMPONENT_CODE, decide_instalment, loan_balances, loan_source_ref
from payroll.notes_lint import lint_run_notes
from payroll.periods import employed_during_q
from payroll.rates import active_team_salary_packages, resolve_rate, resolve_team_salary_package
from payroll.tax import (
    TAX_EXCLUDED_EARNING_CODES, compute_tax_for_employee, is_trueup, months_remaining_for,
)

User = get_user_model()

TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


def _q2(value) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _population_queryset(period):
    """Everyone employed at any point in the period — never is_active=True.
    Separated employees are deactivated by deactivate_past_lwd but must
    still be paid for their final period. Copied from
    payroll/reports.py::build_shift_summary's predicate.
    """
    return User.objects.select_related(
        "department", "team", "designation", "designation__access_level", "access_level",
    ).filter(employed_during_q(period.start_date, period.end_date))


def _resolve_meal_tier(gross: Decimal, tiers: list[MealDeductionTier]) -> MealDeductionTier | None:
    """`tiers` must be sorted by min_gross. The upper bound is inclusive —
    the locked wording is "≤25,000 → 20; 25,000–50,000 → 30; >50,000 → 50",
    so exactly 25,000 is 20/meal and exactly 50,000 is 30/meal. Adjacent
    tiers share a boundary (0–25,000, 25,000–50,000), so the lowest matching
    tier wins it."""
    for tier in tiers:
        if gross >= tier.min_gross and (tier.max_gross is None or gross <= tier.max_gross):
            return tier
    return None


def _salary_segments(structures, period, employee) -> tuple[list[tuple], list[str]]:
    """Split the employee's slice of `period` into one segment per salary
    structure in force: ``[(start_date, end_date, structure), ...]`` sorted
    by start, plus any warnings.

    This is what lets a mid-period salary change (a Data Solutions trainee
    going live on the 7th, a backdated promotion, a demotion) be paid at the
    rate that was actually in force on each day, instead of pricing the whole
    period at whichever single structure the engine happened to pick. See
    docs/DSDD_PAYROLL_AUTOMATION_PLAN.md §2.1 — before this, a 2026-09-07
    go-live from 11,000 to 18,000 paid all 31 days at 18,000.

    `structures` must already be filtered to rows overlapping the period, and
    must include ``SUPERSEDED`` rows: a superseded row is still what was true
    for its own dates, and excluding it is precisely what discarded the old
    rate. ``DRAFT``/``PENDING``/``CANCELLED`` are not history and stay out.

    Overlapping structures shouldn't exist — ``apply_salary_upload``
    supersedes the previous row at ``effective_from - 1 day`` and
    ``SalaryStructure.clean()`` forbids two current approved rows — but MySQL
    can't enforce the partial unique (PAYROLL_PLAN.md Risk 4), so an overlap
    is clipped in favour of the later revision rather than double-paid, and
    warned about.
    """
    warnings: list[str] = []
    emp_start = max(period.start_date, employee.joining_date or period.start_date)
    emp_end = min(period.end_date, employee.last_working_date or period.end_date)
    if emp_start > emp_end:
        return [], warnings

    spans: list[tuple] = []
    for structure in sorted(structures, key=lambda s: (s.effective_from, s.revision_no)):
        start = max(emp_start, structure.effective_from)
        end = emp_end if structure.effective_to is None else min(emp_end, structure.effective_to)
        if start <= end:
            spans.append([start, end, structure])

    # Later revision wins on any overlap.
    for earlier, later in zip(spans, spans[1:]):
        if earlier[1] >= later[0]:
            warnings.append(
                f"Overlapping salary structures ({earlier[2].pk} and {later[2].pk}) — "
                "the later revision was applied for the overlapping days."
            )
            earlier[1] = later[0] - timedelta(days=1)
    segments = [(s, e, st) for s, e, st in spans if s <= e]

    covered = sum((e - s).days + 1 for s, e, _ in segments)
    expected = (emp_end - emp_start).days + 1
    if segments and covered < expected:
        warnings.append(
            f"Salary structures cover only {covered} of {expected} employed day(s) "
            "this period — the uncovered days are unpaid."
        )
    return segments, warnings


def calculate_run(run_pk, actor_pk=None) -> PayrollRun:
    run = PayrollRun.objects.select_related("period").get(pk=run_pk)
    if run.status not in (PayrollRun.Status.INPUTS_LOCKED, PayrollRun.Status.CALCULATED):
        raise ValueError(f"Cannot calculate a run in status {run.status}.")
    actor = User.objects.filter(pk=actor_pk).first() if actor_pk else None
    period = run.period

    with transaction.atomic():
        from_status = run.status
        run.status = PayrollRun.Status.CALCULATING
        run.save(update_fields=["status"])

        # Delete-and-rebuild, never patch.
        PayrollRunEmployee.objects.filter(run=run).delete()
        PayrollControlCheck.objects.filter(run=run).delete()
        PayrollVariance.objects.filter(run=run).delete()
        TaxComputation.objects.filter(run=run).delete()

        employees = list(_population_queryset(period))
        employee_ids = [e.pk for e in employees]
        job_info_map = {ji.employee_id: ji for ji in JobInfo.objects.filter(employee_id__in=employee_ids)}
        bank_map = {
            a.employee_id: a for a in
            EmployeeBankAccount.objects.filter(employee_id__in=employee_ids, is_primary=True).select_related("bank")
        }

        lwp_by_date_map = payroll_inputs.collect_lwp_days_by_date(period, employee_ids)
        lwp_map = {
            employee_id: sum(by_date.values(), Decimal("0"))
            for employee_id, by_date in lwp_by_date_map.items()
        }
        night_map = payroll_inputs.classify_night_shifts(period, employee_ids)
        # SUPERSEDED is included deliberately: a superseded row is still what
        # was true for its own dates, and filtering it out is what made a
        # mid-period salary change pay the new rate for the whole period
        # (docs/DSDD_PAYROLL_AUTOMATION_PLAN.md §2.1). Bucketed to a LIST per
        # employee — _salary_segments turns it into dated segments.
        structures: dict[str, list] = defaultdict(list)
        for s in SalaryStructure.objects.filter(
            employee_id__in=employee_ids,
            status__in=(SalaryStructure.Status.APPROVED, SalaryStructure.Status.SUPERSEDED),
            effective_from__lte=period.end_date,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period.start_date)):
            structures[s.employee_id].append(s)
        tds_map = {t.employee_id: t for t in TDSEntry.objects.filter(employee_id__in=employee_ids, period=period)}

        # Calculated-TDS inputs (Phase 4) — resolved once per run, never per
        # employee, to keep the "~25 queries, not 25,000" discipline.
        tax_year = (
            TaxYear.objects.filter(
                is_active=True, start_date__lte=period.end_date, end_date__gte=period.end_date,
            ).first()
        )
        tax_slabs = list(tax_year.slabs.order_by("sequence")) if tax_year else []
        tax_threshold_map = (
            {t.category_id: t.threshold_amount for t in TaxExemptionThreshold.objects.filter(tax_year=tax_year)}
            if tax_year else {}
        )
        tax_declaration_map = (
            {
                d.employee_id: d for d in
                EmployeeTaxDeclaration.objects.filter(employee_id__in=employee_ids, tax_year=tax_year)
                .select_related("category")
            }
            if tax_year else {}
        )
        tax_year_warned = False
        # Year-to-date true-up inputs (TRUEUP method, 2026-10-07 — see
        # docs/TAX_CALCULATION_TRUEUP_PLAN.md): every earlier period of this
        # tax year that Orbis is authoritative for, its CALCULATED regular
        # run, and from those runs each employee's actual TDS line and taxable
        # extra earnings. Resolved once per run; money columns are encrypted,
        # so the two sums happen in Python over loaded rows.
        tax_trueup = bool(tax_year) and is_trueup(tax_year)
        tax_months_remaining = months_remaining_for(tax_year, period) if tax_year else 12
        tax_prior_tds: dict = defaultdict(lambda: Decimal("0"))
        tax_prior_extra: dict = defaultdict(lambda: Decimal("0"))
        tax_prior_period_labels: list[str] = []
        tax_live_from_mid_year = False
        tax_gap_months: list[str] = []
        if tax_trueup:
            # Expected months are CALENDAR months of the tax year up to this
            # period (a PayrollPeriod row only exists once someone created a
            # run for it, so a month nobody ran has no row to find).
            first_year, first_month = tax_year.start_date.year, tax_year.start_date.month
            if tax_year.orbis_live_from_id:
                live_from = tax_year.orbis_live_from
                first_year, first_month = live_from.year, live_from.month
                tax_live_from_mid_year = (live_from.year, live_from.month) != (
                    tax_year.start_date.year, tax_year.start_date.month,
                )
            expected_months: list[tuple[int, int]] = []
            y, m = first_year, first_month
            while (y, m) < (period.year, period.month):
                expected_months.append((y, m))
                m += 1
                if m > 12:
                    y, m = y + 1, 1
            prior_runs: dict[tuple[int, int], PayrollRun] = {}
            if expected_months:
                for r in PayrollRun.objects.filter(
                    period__period_type=PayrollPeriod.PeriodType.STANDARD,
                    period__year__gte=first_year, period__end_date__lt=period.end_date,
                    run_type=PayrollRun.RunType.REGULAR, status=PayrollRun.Status.CALCULATED,
                ).select_related("period"):
                    key = (r.period.year, r.period.month)
                    if key in expected_months:
                        prior_runs[key] = r
            # control_checks is created further down; the warning is appended there.
            tax_gap_months = [f"{date(y, m, 1):%b %Y}" for (y, m) in expected_months if (y, m) not in prior_runs]
            tax_prior_period_labels = [str(prior_runs[k].period) for k in expected_months if k in prior_runs]
            if prior_runs:
                for emp_id, tax_deducted in PayrollRunEmployee.objects.filter(
                    run__in=prior_runs.values(),
                ).values_list("employee_id", "tax_deducted"):
                    tax_prior_tds[emp_id] += tax_deducted or Decimal("0")
                for emp_id, amount in PayrollRunLine.objects.filter(
                    run_employee__run__in=prior_runs.values(),
                    component_kind=PayComponent.Kind.EARNING, component__is_taxable=True,
                ).exclude(component_code__in=TAX_EXCLUDED_EARNING_CODES).values_list(
                    "run_employee__employee_id", "amount",
                ):
                    tax_prior_extra[emp_id] += amount or Decimal("0")
        ot_map = payroll_inputs.collect_overtime(period, employee_ids)
        ot_type_names = dict(OvertimeType.objects.values_list("pk", "name"))
        variable_entries = {}
        for entry in VariablePayEntry.objects.filter(
            employee_id__in=employee_ids, period=period, status=VariablePayEntry.Status.APPROVED,
        ).select_related("component"):
            variable_entries.setdefault(entry.employee_id, []).append(entry)

        meal_tiers = list(MealDeductionTier.objects.filter(is_active=True).order_by("min_gross"))
        # One query for every package in force, resolved per employee in
        # memory below — never a lookup per employee.
        salary_packages = active_team_salary_packages(period.end_date)
        meal_counts = payroll_inputs.collect_meal_counts(period, employee_ids)
        components = {c.code: c for c in PayComponent.objects.all()}

        # Salary loans (docs/SALARY_LOAN_PLAN.md) — ACTIVE loans whose first
        # deduction period has arrived, their derived balances (principal −
        # opening − LOAN_REPAYMENT lines in OTHER calculated runs −
        # adjustments; payroll/loans.py), and any HR-approved skip for this
        # period. Resolved once per run; money is encrypted, so the sums are
        # in Python. A PENDING_DISBURSEMENT loan is not here: nothing is
        # recovered before Finance has paid the money out.
        active_loans = list(
            SalaryLoan.objects.filter(
                employee_id__in=employee_ids, status=SalaryLoan.Status.ACTIVE,
                first_deduction_period__start_date__lte=period.start_date,
            ).select_related("first_deduction_period")
        )
        loans_by_employee: dict = defaultdict(list)
        for loan in active_loans:
            loans_by_employee[loan.employee_id].append(loan)
        loan_balance_map = loan_balances(active_loans, exclude_run=run)
        loan_skips = set(
            SalaryLoanSkip.objects.filter(
                loan__in=active_loans, period=period,
            ).values_list("loan_id", flat=True)
        )
        loan_component = components.get(LOAN_COMPONENT_CODE)
        loan_component_warned = False

        # Salary adjustment (docs/SALARY_ADJUSTMENT_PLAN.md) — only when the
        # Payroll Admin set an instruction on this run. The per-employee
        # ledger (owed per month vs paid, months settled so far) is derived
        # once for the population, this run excluded from its own inputs.
        adjustment_active = bool(run.adjustment_settle_all or run.adjustment_months)
        adjustment_ledger_map = adjustment_ledgers(employees, period, exclude_run=run) if adjustment_active else {}
        adjustment_component = components.get(ADJUSTMENT_COMPONENT_CODE)
        adjustment_component_warned = False
        adjustment_assumed: list[str] = []      # employees whose early months were assumed (one summary check)
        adjustment_no_record: list[str] = []    # employees skipped for having no pay record at all

        control_checks: list[PayrollControlCheck] = []

        if tax_gap_months:

            control_checks.append(PayrollControlCheck(

                run=run, check_code="TAX_YTD_GAP", severity=PayrollControlCheck.Severity.WARNING,

                message=(

                    "No calculated regular run for " + ", ".join(tax_gap_months)

                    + " — that month's TDS and extra pay count as 0 in the year-to-date tax true-up."

                ),

            ))
        run_employees: list[PayrollRunEmployee] = []
        pending_lines: list[tuple[PayrollRunEmployee, list[PayrollRunLine]]] = []
        tax_computations: list[TaxComputation] = []
        period_days = (period.end_date - period.start_date).days + 1
        # Proration divisor. Confirmed with HR/Finance 2026-09-19: **always
        # 30**, which is what PayrollSettings.payable_days_basis has defaulted
        # to since Phase 1 while being read nowhere (docs/DSNDD_PAYROLL_
        # AUTOMATION_PLAN.md §2.4). It also matches the Test Salary deduction,
        # priced at 6,000 / 30 = 200 a day.
        #
        # A fixed divisor cannot be applied by simply dividing by 30: the
        # 16->15 cycle runs 28 to 31 days (Feb 16 - Mar 15 is 28), so
        # eligible_days / 30 would underpay a full March by 6.7% and overpay a
        # full September by 3.3%. The rule that actually holds:
        #
        #   * employed the whole period      -> exactly 30 days' pay, whatever
        #                                       the calendar says
        #   * employed part of the period    -> one 1/30 for each day employed
        #   * unpaid leave                   -> costs one 1/30 per day
        #
        # implemented by scaling a full period's spans onto the 30-day basis
        # and leaving a partial period's spans alone. Under CALENDAR the scale
        # is always 1 and this reduces exactly to the previous behaviour.
        settings_row = PayrollSettings.load()
        fixed_basis = settings_row.payable_days_basis == PayrollSettings.PayableDaysBasis.FIXED_30
        basis_days = Decimal("30") if fixed_basis else Decimal(period_days)

        run_gross = run_additions = run_deductions = run_tax = run_net = Decimal("0")
        run_night_regular = run_night_custom = run_night_dedicated = run_transport = 0
        run_ot_seconds_by_type: dict[str, int] = defaultdict(int)

        for employee in employees:
            elig_days = payroll_inputs.eligible_days(employee, period)
            lwp_days = lwp_map.get(employee.pk, Decimal("0"))
            lwp_by_date = lwp_by_date_map.get(employee.pk, {})
            # One segment per salary structure in force. An employee whose
            # gross didn't change this period gets exactly one segment, and
            # the arithmetic below is then identical to the pre-segment
            # engine — that equivalence is asserted in tests_engine.py.
            segments, segment_warnings = _salary_segments(
                structures.get(employee.pk, []), period, employee,
            )
            # Employed for every day of the period -> the period counts as a
            # whole month (basis_days), however many calendar days it held.
            # Otherwise each employed day is worth one basis day. See the
            # basis_days comment above.
            span_scale = Decimal("1")
            if elig_days and elig_days == period_days:
                span_scale = basis_days / Decimal(elig_days)

            segment_days = []
            for seg_start, seg_end, seg_structure in segments:
                seg_lwp = sum(
                    (weight for day, weight in lwp_by_date.items() if seg_start <= day <= seg_end),
                    Decimal("0"),
                )
                seg_span = Decimal((seg_end - seg_start).days + 1) * span_scale
                segment_days.append((seg_start, seg_end, seg_structure, max(Decimal("0"), seg_span - seg_lwp)))
            if segments:
                payable_days = sum((d for _, _, _, d in segment_days), Decimal("0"))
            else:
                payable_days = max(Decimal("0"), Decimal(elig_days) * span_scale - lwp_days)
            proration = (payable_days / basis_days) if basis_days else Decimal("0")
            job_info = job_info_map.get(employee.pk)
            bank_account = bank_map.get(employee.pk)
            resource_status = job_info.resource_status if job_info else ""
            access_level = effective_access_level(employee)
            night_counts = night_map.get(employee.pk, {})
            employee_ot = ot_map.get(employee.pk, {})
            # employee_ot is keyed by (overtime_type_id, worked_in_id) —
            # payroll_inputs.collect_overtime's docstring — but this display
            # summary collapses back to one figure per type across teams.
            ot_seconds_by_type: dict[str, int] = defaultdict(int)
            for (ot_type_id, _team_id), d in employee_ot.items():
                ot_seconds_by_type[ot_type_names.get(ot_type_id, str(ot_type_id))] += d["seconds"]

            re = PayrollRunEmployee(
                run=run, employee=employee,
                employee_code=employee.employee_id or "",
                full_name=employee.get_full_name() or employee.email,
                department_name=employee.department.name if employee.department_id else "",
                team_name=employee.team.name if employee.team_id else "",
                designation_name=employee.designation.name if employee.designation_id else "",
                joining_date=employee.joining_date,
                last_working_date=employee.last_working_date,
                resource_status=job_info.resource_status if job_info else "",
                billing_status=job_info.billing_status if job_info else "",
                bank_name=str(bank_account.bank) if bank_account and bank_account.bank else "",
                bank_account_number=bank_account.account_number if bank_account else "",
                period_days=period_days,
                eligible_days=elig_days,
                lwp_days=lwp_days,
                payable_days=payable_days,
                proration_factor=proration.quantize(SIX_PLACES),
                night_shift_count=sum(
                    night_counts.get(k, 0)
                    for k in (payroll_inputs.NIGHT_REGULAR, payroll_inputs.NIGHT_CUSTOM, payroll_inputs.NIGHT_DEDICATED)
                ),
                night_regular_count=night_counts.get(payroll_inputs.NIGHT_REGULAR, 0),
                night_custom_count=night_counts.get(payroll_inputs.NIGHT_CUSTOM, 0),
                night_dedicated_count=night_counts.get(payroll_inputs.NIGHT_DEDICATED, 0),
                transport_count=night_counts.get("TRANSPORT", 0),
                ot_seconds_total=sum(d["seconds"] for d in employee_ot.values()),
                ot_seconds_by_type=ot_seconds_by_type,
            )
            run_night_regular += re.night_regular_count
            run_night_custom += re.night_custom_count
            run_night_dedicated += re.night_dedicated_count
            run_transport += re.transport_count
            for name, secs in ot_seconds_by_type.items():
                run_ot_seconds_by_type[name] += secs
            calc_warnings: list[str] = []
            lines: list[PayrollRunLine] = []
            sequence = 0
            gross_earnings = Decimal("0")
            total_deductions = Decimal("0")
            tax_deducted = Decimal("0")

            # The structure in force at the end of the employee's window —
            # the "current" one. Everything downstream that needs a single
            # gross figure (night/OT rate banding, meal tier, TDS) uses this,
            # not a per-segment figure: those are all properties of the
            # employee's current contractual rate, not of a given day.
            structure = segment_days[-1][2] if segment_days else None
            if structure is None:
                # Distinguish "no structure on file" from "structures exist
                # but none of them cover a day this employee was employed"
                # — the second is a joining/last-working-date problem, and
                # reporting it as a missing salary structure sends whoever
                # clears it looking in the wrong place.
                if structures.get(employee.pk):
                    control_checks.append(PayrollControlCheck(
                        run=run, check_code="SALARY_STRUCTURE_NOT_IN_FORCE",
                        severity=PayrollControlCheck.Severity.BLOCKING, employee=employee,
                        message=(
                            "Salary structure(s) on file, but none in force on any day employed "
                            "this period — check the joining / last working dates."
                        ),
                    ))
                else:
                    control_checks.append(PayrollControlCheck(
                        run=run, check_code="MISSING_SALARY_STRUCTURE",
                        severity=PayrollControlCheck.Severity.BLOCKING, employee=employee,
                        message="No current approved salary structure.",
                    ))
            else:
                calc_warnings.extend(segment_warnings)
                component = components.get("GROSS_SALARY")
                # One line per segment. With a single structure this is one
                # line whose amount equals the old full_amount * proration.
                for seg_start, seg_end, seg_structure, seg_payable in segment_days:
                    seg_proration = (seg_payable / basis_days) if basis_days else Decimal("0")
                    full_amount = seg_structure.gross_monthly
                    line_amount = _q2(full_amount * seg_proration)
                    if component:
                        calc_note = ""
                        if len(segment_days) > 1:
                            calc_note = (
                                f"{seg_start:%d %b} – {seg_end:%d %b}: {seg_payable} payable day(s) "
                                f"/ {period_days} × {full_amount}"
                            )
                        lines.append(PayrollRunLine(
                            component=component, component_code=component.code, component_name=component.name,
                            component_kind=component.kind, sequence=sequence, amount=line_amount,
                            full_amount=full_amount, quantity=seg_proration, unit="proration",
                            source_kind="SalaryStructure", source_ref=f"SalaryStructure:{seg_structure.pk}",
                            calc_note=calc_note,
                        ))
                        sequence += 1
                    gross_earnings += line_amount
                if any(
                    seg_structure.note and "PLACEHOLDER" in seg_structure.note.upper()
                    for _, _, seg_structure, _ in segment_days
                ):
                    calc_warnings.append(
                        "Gross salary is a placeholder value (HR's night-shift threshold indicator, not real pay)."
                    )

                # Does what we're paying match the package their team/status
                # implies? (Phase B, 2026-09-19.) WARNING, never BLOCKING:
                # an individually negotiated salary legitimately differs
                # from its package, so this is information for the
                # exception review, not an error. It catches the two real
                # failures — a go-live whose proposal was never approved,
                # and a package that has gone stale.
                package = resolve_team_salary_package(
                    on_date=period.end_date, department=employee.department,
                    team=employee.team, designation=employee.designation,
                    resource_status=resource_status, packages=salary_packages,
                )
                if package is not None and package.gross_monthly != structure.gross_monthly:
                    control_checks.append(PayrollControlCheck(
                        run=run, check_code="SALARY_PACKAGE_MISMATCH",
                        severity=PayrollControlCheck.Severity.WARNING, employee=employee,
                        message=(
                            f"Paid {structure.gross_monthly} but the {package} package "
                            f"is {package.gross_monthly}."
                        ),
                    ))

                # Night duty — three-way split (Dedicated/Custom/Regular),
                # confirmed 2026-09-07 (docs/PAYROLL_PLAN.md). Dedicated wins
                # over shift type. Since 2026-11-03 the "no night allowance
                # yet" rule is per day, keyed on the confirmation date:
                # classify_night_shifts already keeps pre-confirmation nights
                # out of these counts (NIGHT_PRE_CONFIRMATION), so the rate is
                # resolved *without* the TRAINEE status — a confirmed
                # employee still marked Trainee must get the ordinary rate,
                # not the department's TRAINEE 0.00 row.
                night_resource_status = (
                    "" if resource_status == payroll_inputs.TRAINEE_STATUS else resource_status
                )
                pre_confirmation = night_counts.get(payroll_inputs.NIGHT_PRE_CONFIRMATION, 0)
                if pre_confirmation:
                    calc_warnings.append(
                        f"{pre_confirmation} night shift(s) before confirmation — not paid "
                        "(no night allowance until confirmed)."
                    )
                for rate_type, count_key, unit_label in (
                    (PayRate.RateType.NIGHT_QUARTERLY, payroll_inputs.NIGHT_DEDICATED, "dedicated night"),
                    (PayRate.RateType.NIGHT_CUSTOM, payroll_inputs.NIGHT_CUSTOM, "custom night"),
                    (PayRate.RateType.NIGHT_REGULAR, payroll_inputs.NIGHT_REGULAR, "night"),
                ):
                    count = night_counts.get(count_key, 0)
                    if not count:
                        continue
                    rate = resolve_rate(
                        rate_type=rate_type, on_date=period.end_date,
                        department=employee.department, gross=structure.gross_monthly,
                        resource_status=night_resource_status, access_level=access_level,
                    )
                    if rate is None:
                        calc_warnings.append(
                            f"{count} {unit_label} shift(s) logged but no confirmed rate for this department."
                        )
                        continue
                    line_amount = _q2(rate.amount * count)
                    if not line_amount:
                        continue
                    lines.append(PayrollRunLine(
                        component=rate.component, component_code=rate.component.code,
                        component_name=rate.component.name, component_kind=rate.component.kind,
                        sequence=sequence, amount=line_amount, full_amount=line_amount,
                        quantity=count, rate=rate.amount, unit=unit_label,
                        source_kind="NightDuty", source_ref=rate_type,
                    ))
                    sequence += 1
                    gross_earnings += line_amount

                # Transport allowance — new 2026-09-07. CUST shift-days
                # worked from office (roster.Shift.location_badge != "WFH");
                # applies to Trainees too, unlike the three night rates above.
                transport_count = night_counts.get("TRANSPORT", 0)
                if transport_count:
                    rate = resolve_rate(
                        rate_type=PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, on_date=period.end_date,
                        department=employee.department, gross=structure.gross_monthly,
                        resource_status=resource_status, access_level=access_level,
                    )
                    if rate is None:
                        calc_warnings.append(
                            f"{transport_count} custom-shift office day(s) logged but no confirmed "
                            "transport rate for this department."
                        )
                    else:
                        line_amount = _q2(rate.amount * transport_count)
                        if line_amount:
                            lines.append(PayrollRunLine(
                                component=rate.component, component_code=rate.component.code,
                                component_name=rate.component.name, component_kind=rate.component.kind,
                                sequence=sequence, amount=line_amount, full_amount=line_amount,
                                quantity=transport_count, rate=rate.amount, unit="day",
                                source_kind="Transport", source_ref="",
                            ))
                            sequence += 1
                            gross_earnings += line_amount

                # Overtime — new 2026-09-07. Basis (PER_HOUR to the exact
                # second, PER_OCCURRENCE flat-per-day, or GROSS_30TH_PER_DAY
                # gross-derived-per-day added 2026-09-09) and any
                # min_access_level band are resolved per department via
                # PayRate, never fixed on the type (locked 2026-08-11,
                # narrowed 2026-09-07 for AdOps specifically — see
                # docs/PAYROLL_PLAN.md). One line per (overtime_type, team)
                # actually logged — team added 2026-09-16 so a department's
                # team-scoped PayRate rows (e.g. Data Solutions' MARS) are
                # actually reachable; an employee who worked OT for two
                # different teams in one period gets two separately-rated
                # lines rather than one blended one.
                for (overtime_type_id, team_id), ot_data in employee_ot.items():
                    rate = resolve_rate(
                        rate_type=PayRate.RateType.OVERTIME, on_date=period.end_date,
                        department=employee.department, team=team_id, overtime_type=overtime_type_id,
                        gross=structure.gross_monthly, resource_status=resource_status,
                        access_level=access_level,
                    )
                    if rate is None:
                        calc_warnings.append(
                            "Overtime logged but no confirmed rate for this department/team/type."
                        )
                        continue
                    calc_note = ""
                    if rate.basis == PayRate.Basis.GROSS_30TH_PER_DAY:
                        # Software Development / Creative Development, confirmed in
                        # writing by Finance 2026-09-09: OT is the employee's own
                        # gross / 30 * a multiplier, per OT day. `rate.amount` is
                        # that multiplier, not a taka figure. Reopens
                        # PAYROLL_PROCESS.md Q27, which had recorded this exact
                        # formula as a dead workbook leftover.
                        #
                        # Gross is the un-prorated structure figure: OT is never
                        # prorated (PAYROLL_PLAN.md section 5, stage 4). The 30 is
                        # fixed by the policy, deliberately NOT PayrollSettings.
                        # payable_days_basis -- that setting governs proration, and
                        # its CALENDAR option would silently swing this to /31.
                        effective_rate = _q2(structure.gross_monthly / Decimal(30) * rate.amount)
                        quantity, unit = Decimal(ot_data["days"]), "day"
                        calc_note = (
                            f"gross {structure.gross_monthly} / 30 x {rate.amount} "
                            f"= {effective_rate}/day"
                        )
                    elif rate.basis == PayRate.Basis.PER_OCCURRENCE:
                        effective_rate = rate.amount
                        quantity, unit = Decimal(ot_data["days"]), "day"
                    else:
                        effective_rate = rate.amount
                        quantity = (Decimal(ot_data["seconds"]) / Decimal(3600)).quantize(SIX_PLACES)
                        unit = "hour"
                    if not quantity:
                        continue
                    line_amount = _q2(effective_rate * quantity)
                    lines.append(PayrollRunLine(
                        component=rate.component, component_code=rate.component.code,
                        component_name=rate.component.name, component_kind=rate.component.kind,
                        sequence=sequence, amount=line_amount, full_amount=line_amount,
                        quantity=quantity, rate=effective_rate, unit=unit,
                        source_kind="OvertimeEntry",
                        source_ref=f"OvertimeType:{overtime_type_id}:Team:{team_id or ''}",
                        calc_note=calc_note,
                    ))
                    sequence += 1
                    gross_earnings += line_amount

                # Bonus / incentive / disciplinary — wired, no data yet.
                for entry in variable_entries.get(employee.pk, []):
                    line_amount = _q2(entry.amount)
                    lines.append(PayrollRunLine(
                        component=entry.component, component_code=entry.component.code,
                        component_name=entry.component.name, component_kind=entry.component.kind,
                        sequence=sequence, amount=line_amount, full_amount=line_amount,
                        source_kind="VariablePayEntry", source_ref=f"VariablePayEntry:{entry.pk}",
                    ))
                    sequence += 1
                    if entry.component.kind == PayComponent.Kind.EARNING:
                        gross_earnings += line_amount
                    else:
                        total_deductions += line_amount

                # Salary adjustment — taxable earnings, so before the Tax
                # stage (the true-up reads `lines`). Settles the next N
                # months of the employee's owed balance, topping up months
                # settled earlier at a lower rate.
                ledger = adjustment_ledger_map.get(employee.pk) if adjustment_active else None
                if ledger is not None and ledger.months:
                    if adjustment_component is None:
                        if not adjustment_component_warned:
                            control_checks.append(PayrollControlCheck(
                                run=run, check_code="MISSING_ADJUSTMENT_COMPONENT",
                                severity=PayrollControlCheck.Severity.BLOCKING,
                                message="No SALARY_ADJUSTMENT pay component — run seed_payroll_components; no adjustment was paid.",
                            ))
                            adjustment_component_warned = True
                    elif not ledger.has_pay_record:
                        # Every month since the structure's effective date is
                        # unknown to Orbis — the balance (and "nothing paid
                        # yet") would be pure assumption. Skip, and say so.
                        adjustment_no_record.append(employee.employee_id or employee.email)
                        control_checks.append(PayrollControlCheck(
                            run=run, check_code="SALARY_ADJUSTMENT_NO_RECORD",
                            severity=PayrollControlCheck.Severity.WARNING, employee=employee,
                            message=(
                                f"Salary adjustment not paid: Orbis has no pay record for any month since the "
                                f"salary took effect ({ledger.months[0].label}–{ledger.months[-1].label}). "
                                f"Import their pre-Orbis months first."
                            ),
                        ))
                        calc_warnings.append("Salary adjustment skipped — no pay record for any month since the salary took effect.")
                    else:
                        months_now, adj_amount = ledger.instalment(run.adjustment_months, run.adjustment_settle_all)
                        re.adjustment_months = months_now
                        re.inputs_json = {**(re.inputs_json or {}), "salary_adjustment": ledger.as_json(months_now, adj_amount)}
                        if ledger.assumed_months:
                            assumed = ledger.assumed_months
                            adjustment_assumed.append(employee.employee_id or employee.email)
                            calc_warnings.append(
                                f"Salary adjustment: no pay record for {assumed[0].label}–{assumed[-1].label} "
                                f"({len(assumed)} month(s)) — assumed paid at the previous salary {assumed[0].paid:.0f}."
                            )
                        if adj_amount > 0:
                            lines.append(PayrollRunLine(
                                component=adjustment_component, component_code=adjustment_component.code,
                                component_name=adjustment_component.name, component_kind=adjustment_component.kind,
                                sequence=sequence, amount=adj_amount, full_amount=adj_amount,
                                quantity=Decimal(months_now), unit="month",
                                source_kind="SalaryStructure", source_ref=f"SalaryStructure:{ledger.structure.pk}",
                                # Summary line + the four-step breakdown; the
                                # same steps sit structured in
                                # inputs_json["salary_adjustment"]["explanation"]
                                # for the register drawer's numbered list.
                                calc_note=ledger.note(months_now, adj_amount),
                            ))
                            sequence += 1
                            gross_earnings += adj_amount
                        elif months_now:
                            calc_warnings.append(
                                f"Salary adjustment: {months_now} month(s) marked settled, nothing to pay "
                                f"(owed {ledger.owed_total:.0f}, already paid {ledger.paid_total:.0f})."
                            )

                # Meal deduction — Phase 7 (docs/PAYROLL_PLAN.md's meals.sebpo.net
                # note): meal_counts comes from persisted MealCount rows
                # (payroll_inputs.collect_meal_counts), pulled and frozen at
                # lock_inputs() time — never a live API call at calculate time.
                tier = _resolve_meal_tier(structure.gross_monthly, meal_tiers)
                meal_count = meal_counts.get(employee.pk, 0)
                if tier and meal_count:
                    line_amount = _q2(tier.amount_per_meal * meal_count)
                    component = components.get("MEAL_DEDUCTION")
                    if component:
                        lines.append(PayrollRunLine(
                            component=component, component_code=component.code, component_name=component.name,
                            component_kind=component.kind, sequence=sequence, amount=line_amount,
                            full_amount=line_amount, quantity=meal_count, rate=tier.amount_per_meal, unit="meal",
                            source_kind="MealDeductionTier", source_ref=f"MealDeductionTier:{tier.pk}",
                        ))
                        sequence += 1
                    total_deductions += line_amount

                # Tax — TDSEntry manual override checked first (unchanged
                # behaviour); otherwise calculated in-system from
                # EmployeeTaxDeclaration via tax.py (Phase 4, 2026-08-24).
                tds_entry = tds_map.get(employee.pk)
                if tds_entry is not None:
                    tax_deducted = tds_entry.amount
                    if tax_deducted:
                        component = components.get("TDS")
                        if component:
                            lines.append(PayrollRunLine(
                                component=component, component_code=component.code, component_name=component.name,
                                component_kind=component.kind, sequence=sequence, amount=tax_deducted,
                                full_amount=tax_deducted, source_kind="TDSEntry", source_ref=f"TDSEntry:{tds_entry.pk}",
                            ))
                            sequence += 1
                    if tds_entry.note and "PLACEHOLDER" in tds_entry.note.upper():
                        calc_warnings.append(
                            "TDS is a placeholder (Tk 0) — Finance has not uploaded July TDS."
                        )
                elif tax_year is None:
                    if not tax_year_warned:
                        control_checks.append(PayrollControlCheck(
                            run=run, check_code="MISSING_TAX_YEAR_CONFIG", severity=PayrollControlCheck.Severity.BLOCKING,
                            message="No active TaxYear configured for this period — TDS cannot be calculated for anyone.",
                        ))
                        tax_year_warned = True
                else:
                    declaration = tax_declaration_map.get(employee.pk)
                    if declaration is None or declaration.category_id is None:
                        control_checks.append(PayrollControlCheck(
                            run=run, check_code="MISSING_TAX_DECLARATION", severity=PayrollControlCheck.Severity.BLOCKING,
                            employee=employee, message="No tax category declared — TDS cannot be calculated.",
                        ))
                    else:
                        threshold_amount = tax_threshold_map.get(declaration.category_id, Decimal("0"))
                        # This period's own taxable extra lines are already in
                        # `lines` (earnings are built before the Tax stage).
                        current_extra = sum(
                            (
                                line.amount for line in lines
                                if line.component_kind == PayComponent.Kind.EARNING
                                and line.component_code not in TAX_EXCLUDED_EARNING_CODES
                                and getattr(line.component, "is_taxable", True)
                            ),
                            Decimal("0"),
                        ) if tax_trueup else Decimal("0")
                        additional_pay_ytd = (
                            (declaration.opening_additional_pay or Decimal("0"))
                            + tax_prior_extra[employee.pk] + current_extra
                        ) if tax_trueup else Decimal("0")
                        tds_already_deducted = (
                            (declaration.opening_tds_deducted or Decimal("0")) + tax_prior_tds[employee.pk]
                        ) if tax_trueup else Decimal("0")
                        breakdown = compute_tax_for_employee(
                            employee, tax_year, structure.gross_monthly, declaration,
                            slabs=tax_slabs, threshold_amount=threshold_amount,
                            additional_pay_ytd=additional_pay_ytd,
                            tds_already_deducted=tds_already_deducted,
                            months_remaining=tax_months_remaining,
                        )
                        if (
                            tax_trueup and tax_live_from_mid_year and breakdown.monthly_tds > 0
                            and not (declaration.opening_tds_deducted or declaration.opening_additional_pay)
                        ):
                            control_checks.append(PayrollControlCheck(
                                run=run, check_code="MISSING_TAX_OPENING",
                                severity=PayrollControlCheck.Severity.WARNING, employee=employee,
                                message=(
                                    "Taxed this period but no cutover opening figures (TDS deducted / extra pay "
                                    f"before {tax_year.orbis_live_from}) — the year-to-date true-up assumes 0 "
                                    "for the pre-Orbis months. Upload the tax-year openings."
                                ),
                            ))
                        tax_computations.append(TaxComputation(
                            run=run, employee=employee, tax_year=tax_year,
                            category_name=declaration.category.name,
                            gross_annual_projected=breakdown.gross_annual_projected,
                            exempted_income=breakdown.exempted_income,
                            taxable_income=breakdown.taxable_income,
                            tax_before_rebate=breakdown.tax_before_rebate,
                            rebate_amount=breakdown.rebate_amount,
                            net_tax_payable=breakdown.net_tax_payable,
                            source_tax=breakdown.source_tax,
                            ait_car_advance_tax=breakdown.ait_car_advance_tax,
                            annual_tds=breakdown.annual_tds,
                            monthly_tds=breakdown.monthly_tds,
                            additional_pay_ytd=breakdown.additional_pay_ytd,
                            tds_already_deducted=breakdown.tds_already_deducted,
                            tax_remaining=breakdown.tax_remaining,
                            months_remaining=breakdown.months_remaining,
                            breakdown_json={
                                "slabs": breakdown.slab_breakdown,
                                "warnings": breakdown.warnings,
                                "method": tax_year.calculation_method,
                                "ytd": {
                                    "prior_periods": tax_prior_period_labels,
                                    "opening_tds_deducted": str(declaration.opening_tds_deducted),
                                    "opening_additional_pay": str(declaration.opening_additional_pay),
                                    "prior_tds_in_orbis": str(tax_prior_tds[employee.pk]),
                                    "prior_extra_in_orbis": str(tax_prior_extra[employee.pk]),
                                    "current_period_extra": str(current_extra),
                                } if tax_trueup else None,
                                # The rebate basis as it stood at this run —
                                # the declaration itself may be unlocked
                                # (changeable) for part of the year.
                                "allowable_investment": str(declaration.allowable_investment),
                                "declared_investment": str(breakdown.total_declared_investment),
                                "declaration_locked": declaration.is_locked,
                            },
                        ))
                        calc_warnings.extend(breakdown.warnings)
                        if breakdown.monthly_tds > 0:
                            tax_deducted = breakdown.monthly_tds
                            component = components.get("TDS")
                            if component:
                                lines.append(PayrollRunLine(
                                    component=component, component_code=component.code, component_name=component.name,
                                    component_kind=component.kind, sequence=sequence, amount=tax_deducted,
                                    full_amount=tax_deducted, source_kind="TaxComputation",
                                    source_ref=f"TaxComputation:{run.pk}:{employee.pk}",
                                    calc_note=(
                                        f"Calculated: category={declaration.category.name}, "
                                        f"taxable={breakdown.taxable_income}"
                                        + (
                                            f", extras YTD={breakdown.additional_pay_ytd}, "
                                            f"deducted YTD={breakdown.tds_already_deducted}, "
                                            f"remaining={breakdown.tax_remaining} over "
                                            f"{breakdown.months_remaining} month(s)"
                                            if tax_trueup else ""
                                        )
                                    ),
                                ))
                                sequence += 1

                # Loan recovery (docs/SALARY_LOAN_PLAN.md §4) — after tax,
                # because the never-negative cap needs the final TDS figure;
                # not taxable, so it never feeds the true-up projection. One
                # line per active loan; the balance is derived from earlier
                # calculated runs, so Recalculate can never double-count.
                employee_loans = loans_by_employee.get(employee.pk, [])
                if employee_loans and loan_component is None and not loan_component_warned:
                    control_checks.append(PayrollControlCheck(
                        run=run, check_code="MISSING_LOAN_COMPONENT", severity=PayrollControlCheck.Severity.BLOCKING,
                        message="No LOAN_REPAYMENT pay component — run seed_payroll_components; loan instalments were not deducted.",
                    ))
                    loan_component_warned = True
                leaving_this_period = bool(
                    employee.last_working_date
                    and period.start_date <= employee.last_working_date <= period.end_date
                )
                loan_inputs = []
                for loan in employee_loans:
                    if loan_component is None:
                        break
                    balance = loan_balance_map[loan.pk]
                    if balance.is_settled:
                        continue
                    decision = decide_instalment(
                        installment_amount=loan.installment_amount, outstanding=balance.outstanding,
                        available_net=gross_earnings - total_deductions - tax_deducted,
                        leaving=leaving_this_period, skipped=loan.pk in loan_skips,
                    )
                    if decision.warning_code:
                        control_checks.append(PayrollControlCheck(
                            run=run, check_code=decision.warning_code,
                            severity=PayrollControlCheck.Severity.WARNING, employee=employee,
                            message=f"Loan {loan.file_reference}: {decision.message}",
                        ))
                        calc_warnings.append(f"Loan {loan.file_reference}: {decision.message}")
                    outstanding_after = balance.outstanding - decision.amount
                    loan_inputs.append({
                        "loan": loan.pk, "file_reference": loan.file_reference,
                        "instalment": str(decision.amount), "outstanding_after": str(outstanding_after),
                        "skipped": decision.warning_code == "LOAN_INSTALMENT_SKIPPED",
                    })
                    if decision.amount <= 0:
                        continue
                    if decision.full_balance:
                        calc_note = (
                            f"Final payroll · balance {balance.outstanding:.0f} recovered"
                            + (f", {decision.shortfall:.0f} short" if decision.shortfall else "")
                        )
                    else:
                        calc_note = (
                            f"Instalment {balance.instalments_deducted + 1} of {loan.installment_count}"
                            f" · outstanding after {outstanding_after:.0f}"
                            + (f" · reduced, {decision.shortfall:.0f} short" if decision.shortfall else "")
                        )
                    lines.append(PayrollRunLine(
                        component=loan_component, component_code=loan_component.code,
                        component_name=loan_component.name, component_kind=loan_component.kind,
                        sequence=sequence, amount=decision.amount, full_amount=loan.installment_amount,
                        source_kind="SalaryLoan", source_ref=loan_source_ref(loan.pk), calc_note=calc_note,
                    ))
                    sequence += 1
                    total_deductions += decision.amount
                if loan_inputs:
                    re.inputs_json = {**(re.inputs_json or {}), "loans": loan_inputs}

            net_pay = gross_earnings - total_deductions - tax_deducted
            if structure is not None and payable_days == 0:
                calc_warnings.append("Zero payable days this period.")
            if net_pay < 0:
                control_checks.append(PayrollControlCheck(
                    run=run, check_code="NEGATIVE_NET_PAY", severity=PayrollControlCheck.Severity.BLOCKING,
                    employee=employee, message=f"Net pay is negative ({net_pay}).",
                ))

            re.gross_earnings = gross_earnings
            re.has_pay = gross_earnings > 0
            re.total_additions = gross_earnings
            re.total_deductions = total_deductions
            re.taxable_gross = gross_earnings
            re.tax_deducted = tax_deducted
            re.net_pay = net_pay
            re.calc_warnings = calc_warnings

            run_employees.append(re)
            pending_lines.append((re, lines))

            run_gross += gross_earnings
            run_additions += gross_earnings
            run_deductions += total_deductions
            run_tax += tax_deducted
            run_net += net_pay

        PayrollRunEmployee.objects.bulk_create(run_employees)

        # MySQL's bulk_create doesn't populate pks on the passed-in objects
        # (connection.features.can_return_rows_from_bulk_insert is False),
        # so re-fetch the (employee_id -> pk) mapping in one query — same
        # idiom as roster/upload.py's post-bulk_create ShiftChangeLog lookup.
        run_employee_pks = dict(
            PayrollRunEmployee.objects.filter(run=run).values_list("employee_id", "id")
        )
        all_lines: list[PayrollRunLine] = []
        for re, lines in pending_lines:
            run_employee_pk = run_employee_pks[re.employee_id]
            for line in lines:
                line.run_employee_id = run_employee_pk
                all_lines.append(line)
        PayrollRunLine.objects.bulk_create(all_lines)

        # Overtime filed into this period but never approved is NOT in the
        # figures above (collect_overtime takes status=APPROVED only), so say
        # so on the run itself rather than leaving it to be noticed on the
        # overtime page. WARNING, not BLOCKING: pending overtime is a normal
        # state for a period still being worked, and lock_inputs deliberately
        # doesn't gate on checks anyway.
        #
        # This only describes the moment of calculation. An entry approved
        # AFTER this runs needs PayrollRun.overtime_changed_since_calculation,
        # which is computed live at render — see that property.
        pending_ot = OvertimeEntry.objects.filter(
            period=period, status__in=OvertimeEntry.UNAPPROVED_STATUSES,
        ).count()
        if pending_ot:
            control_checks.append(PayrollControlCheck(
                run=run, check_code="PENDING_OVERTIME", severity=PayrollControlCheck.Severity.WARNING,
                message=(
                    f"{pending_ot} overtime entr{'y' if pending_ot == 1 else 'ies'} filed for this "
                    "period are still awaiting review or approval and are NOT included in these "
                    "figures. Approve them and recalculate if they should be paid."
                ),
            ))

        # Free-text notes naming money that nothing structured accounts for
        # (Phase E, 2026-09-19). Phases B-D moved Data Solutions' recurring
        # pay events out of the Supervisor Note column; this is what stops it
        # quietly filling back up. WARNING, never BLOCKING — a note can
        # legitimately quote a figure that is not a payment.
        for finding in lint_run_notes(run):
            control_checks.append(PayrollControlCheck(
                run=run, check_code="UNSTRUCTURED_NOTE_AMOUNT",
                severity=PayrollControlCheck.Severity.WARNING, employee=finding["employee"],
                message=(
                    f"{finding['role']} note mentions {finding['amount']:g} Tk, but no "
                    f"variable pay or salary on file for this period matches it: "
                    f"\u201c{finding['note'][:120]}\u201d"
                ),
            ))

        # One run-level summary for the previous-rate assumption rather than
        # a row per employee: on the first runs after go-live it applies to
        # nearly everyone (the months before the import), and the per-row
        # detail is in calc_warnings / inputs_json["salary_adjustment"].
        if adjustment_assumed:
            shown = ", ".join(adjustment_assumed[:12]) + (f" … and {len(adjustment_assumed) - 12} more" if len(adjustment_assumed) > 12 else "")
            control_checks.append(PayrollControlCheck(
                run=run, check_code="SALARY_ADJUSTMENT_ASSUMED_PAID", severity=PayrollControlCheck.Severity.WARNING,
                message=(
                    f"Salary adjustment for {len(adjustment_assumed)} employee(s) counts months Orbis has no pay "
                    f"record for (before the pre-Orbis import) as paid at their previous salary — the normal case "
                    f"for the months before go-live; each register row's warning names the months: {shown}."
                ),
            ))

        PayrollControlCheck.objects.bulk_create(control_checks)
        TaxComputation.objects.bulk_create(tax_computations)

        # Variance — first-ever run has no baseline to diff against; every
        # row still gets created (prev_net=None) so the model's shape is
        # proven before a second month exists.
        if period.month == 1:
            prev_year, prev_month = period.year - 1, 12
        else:
            prev_year, prev_month = period.year, period.month - 1
        prev_run = (
            PayrollRun.objects
            .filter(period__year=prev_year, period__month=prev_month, run_type=PayrollRun.RunType.REGULAR,
                    status=PayrollRun.Status.CALCULATED)
            .order_by("-added_at").first()
        )
        prev_net_map = {}
        if prev_run:
            prev_net_map = dict(
                PayrollRunEmployee.objects.filter(run=prev_run, employee_id__in=employee_ids)
                .values_list("employee_id", "net_pay")
            )
        variances = []
        for re in run_employees:
            prev_net = prev_net_map.get(re.employee_id)
            delta = (re.net_pay - prev_net) if prev_net is not None else None
            delta_pct = _q2((delta / prev_net) * 100) if (prev_net and delta is not None) else None
            variances.append(PayrollVariance(
                run=run, employee_id=re.employee_id, prev_net=prev_net, curr_net=re.net_pay,
                delta=delta, delta_pct=delta_pct, flagged=bool(delta_pct is not None and abs(delta_pct) >= 10),
            ))
        PayrollVariance.objects.bulk_create(variances)

        run.employee_count = len(run_employees)
        run.night_regular_total = run_night_regular
        run.night_custom_total = run_night_custom
        run.night_dedicated_total = run_night_dedicated
        run.transport_total = run_transport
        run.ot_seconds_by_type_total = dict(run_ot_seconds_by_type)
        run.gross_total = run_gross
        run.total_additions = run_additions
        run.total_deductions = run_deductions
        run.tax_total = run_tax
        run.net_total = run_net
        run.engine_version = ENGINE_VERSION
        run.calculated_at = timezone.now()
        run.status = PayrollRun.Status.CALCULATED
        run.save()

        PayrollRunAudit.objects.create(
            run=run, event="CALCULATED", from_status=from_status, to_status=run.status,
            actor=actor, detail_json={"employee_count": run.employee_count, "net_total": str(run.net_total)},
        )

    return run
