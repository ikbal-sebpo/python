"""Seed the payroll master/rate data confirmed 2026-08-20 from July 2026's
actual HR payroll compilation (docs/PAYROLL_PROCESS.md §12 Q37-39), the
AdOps night-shift/OT/transport-allowance policy confirmed 2026-09-07
(docs/PAYROLL_PLAN.md's dated section, §12 Q43-46), and the Creative
Development night-shift + Data Solutions/Due Diligence OT team-level
exceptions confirmed 2026-09-08 (same doc), and the General BPO / IT flat
night rates plus the Software/Creative gross-derived overtime formula
confirmed 2026-09-09 (§12 Q47-48, Q50).

All rows are anchored at EFFECTIVE_FROM (2026-01-01), same as the rest of
this seed — including the 2026-09-09 rates, on the user's instruction, so
every rate in the table shares one start date rather than the seed carrying
two conventions.

Idempotent — update_or_create-s by stable keys. The "Digital Advertising"
department lookup below used to warn-and-skip (it didn't exist in Orbis as
of the 2026-08-20 Phase 3 build) — it exists now (892 employees, verified
2026-09-07), so this actually seeds AdOps's rows today. The warning branch
is kept for whichever department name genuinely isn't seeded yet, not
removed.

Usage:
    python manage.py seed_payroll_components
    python manage.py seed_payroll_components --dry-run
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import AccessLevel, Department, Team
from employees.models import JobInfo
from payroll.models import Bank, MealDeductionTier, OvertimeType, PayComponent, PayRate

EFFECTIVE_FROM = date(2026, 1, 1)

# Flat night/transport rates confirmed by Finance 2026-09-09 (PAYROLL_PROCESS.md
# §12 Q50). Neither department has a Trainee carve-out or a Dedicated/Quarterly
# night rate — deliberately absent, confirmed, not an omission.
#   department name -> [(rate_type, basis, component code, amount), ...]
FLAT_DEPARTMENT_RATES = {
    "General BPO": [
        (PayRate.RateType.NIGHT_REGULAR, PayRate.Basis.PER_NIGHT, "NIGHT_ALLOWANCE", Decimal("350")),
        (PayRate.RateType.NIGHT_CUSTOM, PayRate.Basis.PER_NIGHT, "NIGHT_ALLOWANCE", Decimal("500")),
        (PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, PayRate.Basis.PER_DAY, "SPECIAL_SHIFT_TRANSPORT", Decimal("400")),
    ],
    "Information Technology & Cybersecurity": [
        (PayRate.RateType.NIGHT_REGULAR, PayRate.Basis.PER_NIGHT, "NIGHT_ALLOWANCE", Decimal("200")),
    ],
}

PAY_COMPONENTS = [
    # code, name, short_name, kind, entry_mode, is_taxable, is_prorated, affects_gross, sequence, gl_code
    ("GROSS_SALARY", "Gross salary", "Gross", PayComponent.Kind.EARNING, PayComponent.EntryMode.MASTER, True, True, True, 10, ""),
    # Salary adjustment (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §5.2, seeded
    # 2026-10-08 as ARREAR, generalised and re-keyed 2026-10-09 — payroll/0032
    # renames an existing row and its stored line keys): pay that belongs to
    # another period, paid in this one — the increment arrear (effective
    # 1 January, first paid in August, the difference in instalments) or a
    # payment held back one month and released in a later payroll. Always a
    # positive earning; recovering an overpayment is a deduction. INPUT —
    # Finance files it as a VariablePayEntry; deriving the increment arrear
    # from the salary structures is a deferred follow-up. Taxable and not in
    # tax.py::TAX_EXCLUDED_EARNING_CODES, so the true-up counts it.
    ("SALARY_ADJUSTMENT", "Salary adjustment", "Adjustment", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, True, False, True, 15, ""),
    ("NIGHT_ALLOWANCE", "Night duty allowance", "Night", PayComponent.Kind.EARNING, PayComponent.EntryMode.COMPUTED, True, False, True, 20, ""),
    ("OT_ALLOWANCE", "Overtime allowance", "OT", PayComponent.Kind.EARNING, PayComponent.EntryMode.COMPUTED, True, False, True, 30, ""),
    ("SPECIAL_SHIFT_TRANSPORT", "Special shift transport", "Transport", PayComponent.Kind.EARNING, PayComponent.EntryMode.COMPUTED, True, False, True, 40, ""),
    ("MEAL_DEDUCTION", "Meal deduction", "Meal", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.COMPUTED, False, False, False, 50, ""),
    ("TDS", "Tax deducted at source", "TDS", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.INPUT, False, False, False, 60, ""),
    # Festival bonus (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §5.1, 2026-10-08):
    # SEBPO pays BOTH a festival (Eid) bonus and a performance bonus, so
    # BONUS keeps its "Performance bonus" meaning and the festival bonus is
    # its own code. INPUT until a later phase generates the 2 Eids × 50% of
    # gross automatically. This is the one bonus code the tax projection
    # excludes from additional pay (it already assumes one month's gross).
    ("FESTIVAL_BONUS", "Festival bonus", "Festival", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, True, False, True, 68, ""),
    ("BONUS", "Performance bonus", "Bonus", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, True, False, True, 70, ""),
    ("INCENTIVE", "Incentive", "Incentive", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, True, False, True, 80, ""),
    ("DISCIPLINARY", "Disciplinary deduction", "Disciplinary", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.INPUT, False, False, False, 90, ""),
    # SLA achievement pay — Data Solutions, 3,000 Tk/month for meeting SLA
    # targets. EntryMode.INPUT (it arrives as a VariablePayEntry from the
    # supervisor upload), never COMPUTED: the engine does not decide who
    # met SLA, it only pays the approved rows. The amount lives on a
    # PayRate row below, not here.
    ("SLA_INCENTIVE", "SLA achievement incentive", "SLA", PayComponent.Kind.EARNING, PayComponent.EntryMode.INPUT, True, False, True, 85, ""),
    # Data Solutions "Test Salary" — a deduction of the daily slice of the
    # employee's variable pay portion. INPUT (it arrives as a
    # VariablePayEntry from the supervisor upload), not_prorated (the
    # uploader has already counted the exact days), and NOT taxable-relevant
    # since TDS is computed from contractual gross, not from earnings net of
    # deductions.
    ("TEST_SALARY_DEDUCTION", "Test salary deduction", "Test salary", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.INPUT, False, False, False, 95, ""),
    # Salary loan instalment (docs/SALARY_LOAN_PLAN.md, 2026-10-08). COMPUTED:
    # the engine derives the instalment from the SalaryLoan row and the
    # balance recovered so far; nobody enters it per period. Not taxable
    # (repaying a loan is not income), deducted after TDS.
    ("LOAN_REPAYMENT", "Loan repayment", "Loan", PayComponent.Kind.DEDUCTION, PayComponent.EntryMode.COMPUTED, False, False, False, 97, ""),
]

OVERTIME_TYPES = [
    # code, name, aliases, basis, component_code
    ("regular_ot", "Regular OT", ["ROT"], OvertimeType.Basis.PER_HOUR, "OT_ALLOWANCE"),
    # Renamed 2026-09-07 from "Holiday OT" to match AdOps's actual upload
    # terminology ("GH OT" — General Holiday, same "GH" ShiftType/holiday
    # naming already used elsewhere) — confirmed by AdOps's real
    # AdOps overtime-upload.xlsx. "Holiday OT"/"HOT" kept as aliases so any
    # existing reference to the old name still resolves.
    ("holiday_ot", "GH OT", ["Holiday OT", "HOT"], OvertimeType.Basis.PER_HOUR, "OT_ALLOWANCE"),
    # Renamed 2026-09-07 from "Eid Special OT" — broadened to cover any
    # special-occasion OT (not just Eid), on request. Confirmed unused
    # (zero OvertimeEntry/PayRate rows referenced it), so `code` is fully
    # re-keyed to "special_ot" too, not just the display name — see the
    # dated section in PAYROLL_PLAN.md for the one-off row rename this
    # required (update_or_create can't follow a changed lookup key).
    # "Eid Special OT"/"ESOT" kept as aliases.
    ("special_ot", "Special OT", ["Eid Special OT", "ESOT"], OvertimeType.Basis.PER_HOUR, "OT_ALLOWANCE"),
    # "Eid Package" (PAYROLL_PROCESS.md §7/§12 Q7/Q28) is deliberately NOT an
    # OvertimeType — it's a flat 3-day/24hr coverage payment, not hours-based,
    # and belongs to the not-yet-built EidCoverageEntry + PayRate(EID_COVERAGE)
    # path (PAYROLL_PLAN.md ~line 203). Don't add it here.
]

BANKS = [
    ("Bank Asia PLC", "BAL"),
    ("Eastern Bank PLC", "EBL"),
    ("Modhumoti Bank Limited", "MDB"),
]

MEAL_TIERS = [
    # min_gross, max_gross, amount_per_meal
    (Decimal("0"), Decimal("25000"), Decimal("20")),
    (Decimal("25000"), Decimal("50000"), Decimal("30")),
    (Decimal("50000"), None, Decimal("50")),
]


class Command(BaseCommand):
    help = "Seed PayComponent/OvertimeType/PayRate/MealDeductionTier/Bank with the 2026-08-20 confirmed figures."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        try:
            with transaction.atomic():
                self._seed(self.stdout)
                if dry_run:
                    raise _DryRun()
        except _DryRun:
            self.stdout.write(self.style.WARNING("Dry run — rolled back."))
            return
        self.stdout.write(self.style.SUCCESS("seed_payroll_components complete"))

    def _seed(self, out):
        components = {}
        for code, name, short_name, kind, entry_mode, is_taxable, is_prorated, affects_gross, sequence, gl_code in PAY_COMPONENTS:
            obj, created = PayComponent.objects.update_or_create(
                code=code,
                defaults=dict(
                    name=name, short_name=short_name, kind=kind, entry_mode=entry_mode,
                    is_taxable=is_taxable, is_prorated=is_prorated, affects_gross=affects_gross,
                    sequence=sequence, gl_code=gl_code,
                ),
            )
            components[code] = obj
            out.write(f"component: {code} {'created' if created else 'updated'}")

        ot_types = {}
        for code, name, aliases, basis, component_code in OVERTIME_TYPES:
            obj, created = OvertimeType.objects.update_or_create(
                code=code,
                defaults=dict(name=name, aliases=aliases, basis=basis, component=components[component_code]),
            )
            ot_types[code] = obj
            out.write(f"overtime type: {code} {'created' if created else 'updated'}")

        for name, short_code in BANKS:
            _, created = Bank.objects.update_or_create(name=name, defaults={"short_code": short_code})
            out.write(f"bank: {name} {'created' if created else 'updated'}")

        for min_gross, max_gross, amount in MEAL_TIERS:
            _, created = MealDeductionTier.objects.update_or_create(
                min_gross=min_gross, max_gross=max_gross, defaults={"amount_per_meal": amount},
            )
            out.write(f"meal tier: {min_gross}-{max_gross} -> {amount} {'created' if created else 'updated'}")

        software = Department.objects.filter(name="Software Development").first()
        if software is None:
            out.write(self.style.WARNING("Department 'Software Development' not found — skipping banded night rate."))
        else:
            for max_gross, min_gross, amount in [(Decimal("40000"), None, Decimal("350")), (None, Decimal("40000"), Decimal("450"))]:
                _, created = PayRate.objects.update_or_create(
                    rate_type=PayRate.RateType.NIGHT_REGULAR, department=software,
                    min_gross=min_gross, max_gross=max_gross,
                    defaults=dict(
                        component=components["NIGHT_ALLOWANCE"], basis=PayRate.Basis.PER_NIGHT,
                        amount=amount, effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(f"pay rate: NIGHT_REGULAR Software Development band<{max_gross}>{min_gross} -> {amount} "
                           f"{'created' if created else 'updated'}")

        # Creative Development, confirmed 2026-09-08: department-wide 250/400
        # (below/at-or-above 40K gross), except the FlashTalking team, which
        # is paid at Software Development's 350/450. FlashTalking is seeded
        # as a team-scoped row rather than touching Software Development's
        # rows directly — resolve_rate()'s specificity scoring (team=4 beats
        # department=1) makes it win for FlashTalking employees automatically,
        # with every other Creative Development team falling through to the
        # department-level row. No need to seed the department's other teams.
        creative = Department.objects.filter(name="Creative Development").first()
        if creative is None:
            out.write(self.style.WARNING("Department 'Creative Development' not found — skipping night rates."))
        else:
            for max_gross, min_gross, amount in [(Decimal("40000"), None, Decimal("250")), (None, Decimal("40000"), Decimal("400"))]:
                _, created = PayRate.objects.update_or_create(
                    rate_type=PayRate.RateType.NIGHT_REGULAR, department=creative, team=None,
                    min_gross=min_gross, max_gross=max_gross,
                    defaults=dict(
                        component=components["NIGHT_ALLOWANCE"], basis=PayRate.Basis.PER_NIGHT,
                        amount=amount, effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(f"pay rate: NIGHT_REGULAR Creative Development band<{max_gross}>{min_gross} -> {amount} "
                           f"{'created' if created else 'updated'}")

            flashtalking = Team.objects.filter(name="FlashTalking", department=creative).first()
            if flashtalking is None:
                out.write(self.style.WARNING("Team 'FlashTalking' not found under Creative Development — skipping override."))
            else:
                for max_gross, min_gross, amount in [(Decimal("40000"), None, Decimal("350")), (None, Decimal("40000"), Decimal("450"))]:
                    _, created = PayRate.objects.update_or_create(
                        rate_type=PayRate.RateType.NIGHT_REGULAR, department=creative, team=flashtalking,
                        min_gross=min_gross, max_gross=max_gross,
                        defaults=dict(
                            component=components["NIGHT_ALLOWANCE"], basis=PayRate.Basis.PER_NIGHT,
                            amount=amount, effective_from=EFFECTIVE_FROM,
                        ),
                    )
                    out.write(f"pay rate: NIGHT_REGULAR Creative Development/FlashTalking band<{max_gross}>{min_gross} -> {amount} "
                               f"{'created' if created else 'updated'}")

        # Flat night-duty / transport rates, confirmed by Finance 2026-09-09.
        # No work_location scope on the transport row: the engine never passes
        # one to resolve_rate(), and inputs.classify_night_shifts already
        # excludes WFH days from the TRANSPORT count — scoping the rate too
        # would make it unresolvable rather than stricter.
        for dept_name, rate_rows in FLAT_DEPARTMENT_RATES.items():
            dept = Department.objects.filter(name=dept_name).first()
            if dept is None:
                out.write(self.style.WARNING(
                    f"Department '{dept_name}' not found — skipping its night/transport rates."
                ))
                continue
            for rate_type, basis, component_code, amount in rate_rows:
                _, created = PayRate.objects.update_or_create(
                    rate_type=rate_type, department=dept, team=None, overtime_type=None,
                    work_location=None, resource_status="", min_access_level=None,
                    defaults=dict(
                        component=components[component_code], basis=basis,
                        amount=amount, effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(
                    f"pay rate: {rate_type} {dept_name} -> {amount} "
                    f"{'created' if created else 'updated'}"
                )

        # Software Development / Creative Development overtime, confirmed in
        # writing by Finance 2026-09-09: gross / 30 * 1.5, per OT day. This
        # REOPENS PAYROLL_PROCESS.md Q27, which had recorded the identical
        # formula as a dead workbook leftover on 2026-08-13 — see the dated
        # section in PAYROLL_PLAN.md.
        #
        # GH OT only. These two departments earn overtime solely for working a
        # government holiday, and they work a full shift when they do — hence
        # per-day, not per-hour. They have no Regular OT at all. Special OT may
        # exist; Finance has not supplied that figure yet, so no row is seeded
        # for it (the engine warns rather than paying an invented rate).
        for dept_name in ("Software Development", "Creative Development"):
            dept = Department.objects.filter(name=dept_name).first()
            if dept is None:
                out.write(self.style.WARNING(
                    f"Department '{dept_name}' not found — skipping its gross-derived GH OT rate."
                ))
                continue
            _, created = PayRate.objects.update_or_create(
                rate_type=PayRate.RateType.OVERTIME, overtime_type=ot_types["holiday_ot"],
                department=dept, team=None, min_access_level=None,
                defaults=dict(
                    component=components["OT_ALLOWANCE"],
                    basis=PayRate.Basis.GROSS_30TH_PER_DAY,
                    amount=Decimal("1.5"), effective_from=EFFECTIVE_FROM,
                ),
            )
            out.write(
                f"pay rate: OVERTIME {dept_name} GH OT -> gross/30 x 1.5 per day "
                f"{'created' if created else 'updated'}"
            )

        adops = Department.objects.filter(name="Digital Advertising").first()
        if adops is None:
            out.write(self.style.WARNING(
                "Department 'Digital Advertising' not found in Orbis — skipping AdOps OT/night/transport "
                "rates entirely. These are confirmed real figures (PAYROLL_PROCESS.md §12 Q37-39, Q43-46); "
                "if this warns, the department was renamed or removed since 2026-09-07."
            ))
        else:
            supervisor_level = AccessLevel.objects.filter(name="Supervisor").first()
            if supervisor_level is None:
                out.write(self.style.WARNING(
                    "AccessLevel 'Supervisor' not found — skipping AdOps's Team-Leader-and-above OT band "
                    "(250 Tk/hr). Only the below-Supervisor rate (200 Tk/hr) will be seeded."
                ))

            # Per-hour, not flat-per-day — corrected 2026-09-07 (docs/PAYROLL_PLAN.md).
            # Software Development / Creative Development stay PER_OCCURRENCE (flat
            # per day), unchanged; AdOps and every other department without an
            # explicit override are PER_HOUR. Banded by min_access_level (AdOps has
            # no accounts.Grade data, but its designations map onto the existing
            # AccessLevel ladder: Executive/Sr. Executive = Employee, Team
            # Leader/APM/PM/OM = Supervisor or above).
            # Both OT types, same two bands: AdOps prices GH OT identically to
            # Regular OT (question 51, confirmed 2026-09-09). That makes AdOps
            # the opposite shape to Software/Creative, whose GH OT is a
            # gross-derived per-day formula and who have no Regular OT at all
            # (question 47/48) — so the two are seeded per department, never
            # defaulted from the overtime type. GH OT was missing here until
            # 2026-09-09: a real run flagged 596 unpriced AdOps GH OT entries
            # against 98 Regular OT ones, i.e. the *dominant* type was the
            # unseeded one.
            for ot_code, ot_label in (("regular_ot", "Regular OT"), ("holiday_ot", "GH OT")):
                _, created = PayRate.objects.update_or_create(
                    rate_type=PayRate.RateType.OVERTIME, overtime_type=ot_types[ot_code], department=adops,
                    min_access_level=None,
                    defaults=dict(
                        component=components["OT_ALLOWANCE"], basis=PayRate.Basis.PER_HOUR,
                        amount=Decimal("200"), effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(f"pay rate: OVERTIME AdOps {ot_label} (below Supervisor) -> 200 Tk/hr {'created' if created else 'updated'}")
                if supervisor_level is not None:
                    _, created = PayRate.objects.update_or_create(
                        rate_type=PayRate.RateType.OVERTIME, overtime_type=ot_types[ot_code], department=adops,
                        min_access_level=supervisor_level,
                        defaults=dict(
                            component=components["OT_ALLOWANCE"], basis=PayRate.Basis.PER_HOUR,
                            amount=Decimal("250"), effective_from=EFFECTIVE_FROM,
                        ),
                    )
                    out.write(f"pay rate: OVERTIME AdOps {ot_label} (Supervisor+) -> 250 Tk/hr {'created' if created else 'updated'}")

            # Three-way night split, confirmed 2026-09-07 — see PAYROLL_PLAN.md.
            # NIGHT_REGULAR: ordinary night-crossing shift, no active Dedicated
            # assignment. NIGHT_CUSTOM (renamed from NIGHT_SPECIAL): any employee on
            # a CUST shift, not already covered by an active Dedicated assignment.
            # NIGHT_QUARTERLY: an approved Dedicated Night Shift assignment covers
            # the date — wins over shift type.
            for rate_type, amount, label in (
                (PayRate.RateType.NIGHT_REGULAR, Decimal("350"), "NIGHT_REGULAR"),
                (PayRate.RateType.NIGHT_CUSTOM, Decimal("500"), "NIGHT_CUSTOM"),
                (PayRate.RateType.NIGHT_QUARTERLY, Decimal("500"), "NIGHT_QUARTERLY"),
            ):
                _, created = PayRate.objects.update_or_create(
                    rate_type=rate_type, department=adops, resource_status="",
                    defaults=dict(
                        component=components["NIGHT_ALLOWANCE"], basis=PayRate.Basis.PER_NIGHT,
                        amount=amount, effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(f"pay rate: {label} AdOps -> {amount} {'created' if created else 'updated'}")
                # Trainee resource status earns none of the three — a specific
                # zero-amount override row outranks the generic one above via
                # resolve_rate()'s specificity scoring, rather than an engine-level
                # special case (PayRate.resource_status, added 2026-09-07).
                _, created = PayRate.objects.update_or_create(
                    rate_type=rate_type, department=adops, resource_status=JobInfo.ResourceStatus.TRAINEE,
                    defaults=dict(
                        component=components["NIGHT_ALLOWANCE"], basis=PayRate.Basis.PER_NIGHT,
                        amount=Decimal("0"), effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(f"pay rate: {label} AdOps (Trainee) -> 0 {'created' if created else 'updated'}")

            # Transport allowance — flat, applies to Trainees too (unlike the
            # three night rates above), so no resource_status override needed.
            _, created = PayRate.objects.update_or_create(
                rate_type=PayRate.RateType.TRANSPORT_SPECIAL_SHIFT, department=adops,
                defaults=dict(
                    component=components["SPECIAL_SHIFT_TRANSPORT"], basis=PayRate.Basis.PER_DAY,
                    amount=Decimal("400"), effective_from=EFFECTIVE_FROM,
                ),
            )
            out.write(f"pay rate: TRANSPORT_SPECIAL_SHIFT AdOps -> 400 {'created' if created else 'updated'}")

        # Data Solutions and Due Diligence, confirmed 2026-09-08: department-wide
        # 200/hr for both Regular OT and GH OT, except MARS and MARS QC, which
        # are paid 250/hr on both. Same team-overrides-department pattern as
        # Creative Development/FlashTalking above — MARS/MARS QC are seeded as
        # team-scoped rows and every other team in the department falls through
        # to the department-level row via resolve_rate()'s specificity scoring.
        dsdd = Department.objects.filter(name="Data solution and Due Diligence").first()
        if dsdd is None:
            out.write(self.style.WARNING("Department 'Data solution and Due Diligence' not found — skipping OT rates."))
        else:
            for ot_code, label in (("regular_ot", "Regular OT"), ("holiday_ot", "GH OT")):
                _, created = PayRate.objects.update_or_create(
                    rate_type=PayRate.RateType.OVERTIME, overtime_type=ot_types[ot_code],
                    department=dsdd, team=None,
                    defaults=dict(
                        component=components["OT_ALLOWANCE"], basis=PayRate.Basis.PER_HOUR,
                        amount=Decimal("200"), effective_from=EFFECTIVE_FROM,
                    ),
                )
                out.write(f"pay rate: OVERTIME Data Solutions/Due Diligence ({label}) -> 200 Tk/hr "
                           f"{'created' if created else 'updated'}")

            for team_name in ("MARS", "MARS QC"):
                team = Team.objects.filter(name=team_name, department=dsdd).first()
                if team is None:
                    out.write(self.style.WARNING(f"Team '{team_name}' not found under Data solution and Due "
                                                   f"Diligence — skipping override."))
                    continue
                for ot_code, label in (("regular_ot", "Regular OT"), ("holiday_ot", "GH OT")):
                    _, created = PayRate.objects.update_or_create(
                        rate_type=PayRate.RateType.OVERTIME, overtime_type=ot_types[ot_code],
                        department=dsdd, team=team,
                        defaults=dict(
                            component=components["OT_ALLOWANCE"], basis=PayRate.Basis.PER_HOUR,
                            amount=Decimal("250"), effective_from=EFFECTIVE_FROM,
                        ),
                    )
                    out.write(f"pay rate: OVERTIME {team_name} ({label}) -> 250 Tk/hr {'created' if created else 'updated'}")

            # SLA achievement incentive — 3,000 Tk flat, department-wide
            # (docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase C). Held as a
            # PayRate rather than a constant so it is effective-dated and can
            # be carved out per team later, exactly like the OT rates above.
            # Resolved by payroll/upload.py::apply_sla_upload at upload time,
            # not by the engine — the engine only ever sees the resulting
            # VariablePayEntry amount.
            _, created = PayRate.objects.update_or_create(
                rate_type=PayRate.RateType.SLA_INCENTIVE, overtime_type=None,
                department=dsdd, team=None,
                defaults=dict(
                    component=components["SLA_INCENTIVE"], basis=PayRate.Basis.PER_OCCURRENCE,
                    amount=Decimal("3000"), effective_from=EFFECTIVE_FROM,
                ),
            )
            out.write(f"pay rate: SLA_INCENTIVE Data Solutions/Due Diligence -> 3000 Tk "
                      f"{'created' if created else 'updated'}")

            # Data Solutions go-live packages — the entire policy recovered
            # from September 2026's Supervisor Notes (Trainee 11,000; MARS
            # 18,000), plus the rule confirmed 2026-09-21 for
            # DSDD_PAYROLL_AUTOMATION_PLAN.md §5 Q2 ("is MARS/Item Review the
            # complete set?"): MARS is the only team with its own rate — every
            # other DSDD team pays 16,000 on go-live. Held as data so a
            # go-live can propose the right figure instead of Finance
            # retyping it, and so the engine can flag anyone paid off-package.
            #
            # The Trainee row is department-wide with resource_status set, so
            # it beats every other row for anyone still a trainee no matter
            # which team they already sit on (dominant-axis weighting, see
            # rates.py::_PACKAGE_WEIGHTS). The 16,000 row is ALSO
            # department-wide (team=None) rather than pinned to "Item
            # Review" — that's what makes it the fallback for every DSDD
            # team except MARS, not a second single-team rate. MARS's
            # team-scoped row outranks it via the team_id weight whenever
            # both would otherwise match.
            from payroll.models import TeamSalaryPackage

            package_rows = [
                (None, "TRAINEE", Decimal("11000")),
                ("MARS", "", Decimal("18000")),
                (None, "", Decimal("16000")),
            ]
            for team_name, resource_status, gross in package_rows:
                team = Team.objects.filter(name=team_name, department=dsdd).first() if team_name else None
                if team_name and team is None:
                    out.write(self.style.WARNING(
                        f"Team '{team_name}' not found under Data solution and Due Diligence "
                        f"— skipping salary package."
                    ))
                    continue
                _, created = TeamSalaryPackage.objects.update_or_create(
                    department=dsdd, team=team, designation=None,
                    resource_status=resource_status,
                    defaults=dict(gross_monthly=gross, effective_from=EFFECTIVE_FROM),
                )
                label = team_name or (f"{resource_status.title()}" if resource_status else "Every other team") \
                    + " (department-wide)"
                out.write(f"salary package: {label} -> {gross} {'created' if created else 'updated'}")

            # Test salary — 200 Tk/day, i.e. one thirtieth of the 6,000 Tk
            # variable portion every Data Solutions employee carries. The
            # per-period cap is derived as 30 x this figure rather than
            # stored separately, because the two are the same number by
            # definition (see payroll/upload.py's Test Salary section).
            _, created = PayRate.objects.update_or_create(
                rate_type=PayRate.RateType.TEST_SALARY_DAY, overtime_type=None,
                department=dsdd, team=None,
                defaults=dict(
                    component=components["TEST_SALARY_DEDUCTION"], basis=PayRate.Basis.PER_DAY,
                    amount=Decimal("200"), effective_from=EFFECTIVE_FROM,
                ),
            )
            out.write(f"pay rate: TEST_SALARY_DAY Data Solutions/Due Diligence -> 200 Tk/day "
                      f"{'created' if created else 'updated'}")


class _DryRun(Exception):
    pass
