"""Issue the year's EmployeeTaxDeclaration rows — and, for FY2026-27, stamp
them as agreed outside Orbis.

engine.py raises a per-employee BLOCKING ``MISSING_TAX_DECLARATION`` control
check for anyone in the run population without a declared tax category, so a
run cannot complete until every employee has one. Each July this command is
how the new year's rows come into being (``docs/PAYROLL_PLAN.md``, "Tax
declaration becomes a once-a-year commitment, 2026-10-07"):

1. **Create** a row for every employee in the year's run population who has
   none. Category follows EmployeeTaxDeclaration.clean()'s own rule —
   PersonalInfo gender F -> WOMEN_SENIOR, M -> MEN — falling back to
   ``--fallback`` (default MEN) where gender is blank. The three categories
   Orbis has no data source for are never auto-assigned; HR overrides them.
   Each new row gets its **allowable investment snapshot** (30% of projected
   taxable income from the salary in force on ``--as-of``, default today) and
   ``actual_investment`` defaulting to it — Finance's "assume the maximum"
   rule. Employees without a salary structure get a NULL snapshot.
2. ``--fill-allowable`` — give existing rows that still have a NULL snapshot
   one (a salary structure arrived after the row did). Never touches a row
   that already has a figure: it is a snapshot.
3. ``--mark-agreed-outside`` — stamp every still-unconfirmed row for the year
   as ``OUTSIDE_ORBIS`` on ``--agreed-on`` (default: the tax year's start).
   This is the FY2026-27 backfill: Finance collected that year's agreement
   before Orbis held declarations, so employees see "Agreed outside Orbis"
   and nothing is left open for them to change. **Not for a normal July** —
   in a normal year rows stay open until the employee confirms or the
   year's ``declaration_deadline`` passes.

SAFE TO RE-RUN: creation only adds missing rows, fill only touches NULLs,
mark only touches unconfirmed rows. HR's hand-set categories survive.

Usage:
    python manage.py seed_tax_declarations --dry-run
    python manage.py seed_tax_declarations                       # a normal July
    python manage.py seed_tax_declarations --tax-year 2026-27 --fill-allowable --mark-agreed-outside
    python manage.py seed_tax_declarations --as-of 2026-07-01 --fallback MEN
"""

from __future__ import annotations

from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.models import CustomUser
from employees.models import PersonalInfo
from payroll.models import EmployeeTaxDeclaration, TaxCategory, TaxYear
from payroll.services import gross_in_force
from payroll.tax import allowable_investment

# PersonalInfo.gender -> TaxCategory.code. String literals rather than an
# import of employees' Gender choices, matching the model's own posture.
GENDER_CATEGORY = {"F": "WOMEN_SENIOR", "M": "MEN"}


class Command(BaseCommand):
    help = "Issue the tax year's EmployeeTaxDeclaration rows (gender-defaulted category, allowable-investment snapshot)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tax-year", dest="tax_year_code", default=None,
            help="TaxYear.code, e.g. '2026-27'. Defaults to the active year.",
        )
        parser.add_argument(
            "--fallback", default="MEN",
            help="TaxCategory.code for employees whose gender is unknown (default: MEN).",
        )
        parser.add_argument(
            "--as-of", dest="as_of", default=None,
            help="Date whose salary-in-force the allowable investment is projected from (default: today).",
        )
        parser.add_argument(
            "--fill-allowable", action="store_true",
            help="Also give existing rows with no allowable-investment snapshot one.",
        )
        parser.add_argument(
            "--mark-agreed-outside", action="store_true",
            help="Stamp every unconfirmed row for the year as agreed outside Orbis (FY2026-27 backfill).",
        )
        parser.add_argument(
            "--agreed-on", default=None,
            help="Date for --mark-agreed-outside (default: the tax year's start date).",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        tax_year = self._resolve_tax_year(options["tax_year_code"])
        fallback = TaxCategory.objects.filter(code=options["fallback"]).first()
        if fallback is None:
            raise CommandError(
                f"No TaxCategory with code '{options['fallback']}' — run seed_tax_year first."
            )
        as_of = self._parse_date(options["as_of"], "--as-of") or timezone.localdate()
        agreed_on = self._parse_date(options["agreed_on"], "--agreed-on") or tax_year.start_date
        categories = {c.code: c for c in TaxCategory.objects.all()}
        out = self.stdout

        # The payroll run population for the whole tax year, so every period in
        # it resolves. Mirrors engine._population_queryset's predicate (which is
        # deliberately NOT is_active=True — separated employees must still be
        # paid for their final period), widened from one period to the year.
        population = CustomUser.objects.filter(
            Q(joining_date__isnull=True) | Q(joining_date__lte=tax_year.end_date),
            Q(last_working_date__isnull=True) | Q(last_working_date__gte=tax_year.start_date),
        )
        existing = list(
            EmployeeTaxDeclaration.objects.filter(tax_year=tax_year, employee__in=population)
        )
        already = {d.employee_id for d in existing}
        missing = [e for e in population if e.pk not in already]

        gender_by_employee = dict(
            PersonalInfo.objects.filter(employee__in=missing).values_list("employee_id", "gender")
        )
        needs_gross = [e.pk for e in missing]
        unfilled = [d for d in existing if d.allowable_investment is None] if options["fill_allowable"] else []
        needs_gross += [d.employee_id for d in unfilled]
        gross_map = gross_in_force(needs_gross, as_of) if needs_gross else {}

        rows, tally, no_salary = [], {"WOMEN_SENIOR": 0, "MEN": 0}, 0
        for employee in missing:
            code = GENDER_CATEGORY.get(gender_by_employee.get(employee.pk) or "")
            category = categories.get(code) if code else fallback
            if category is None:
                category = fallback
            gross = gross_map.get(employee.pk)
            allowable = allowable_investment(tax_year, gross) if gross is not None else None
            if allowable is None:
                no_salary += 1
            rows.append(EmployeeTaxDeclaration(
                employee=employee, tax_year=tax_year, category=category,
                allowable_investment=allowable, actual_investment=allowable,
                note="Bulk default — category from gender; investment assumed at the allowable maximum.",
            ))
            tally[category.code] = tally.get(category.code, 0) + 1

        filled = []
        for declaration in unfilled:
            gross = gross_map.get(declaration.employee_id)
            if gross is None:
                continue
            declaration.allowable_investment = allowable_investment(tax_year, gross)
            if declaration.actual_investment is None:
                declaration.actual_investment = declaration.allowable_investment
            filled.append(declaration)

        to_mark = []
        if options["mark_agreed_outside"]:
            to_mark = [d for d in existing if d.submitted_at is None]

        out.write(f"tax year          : {tax_year.code} ({tax_year.start_date} -> {tax_year.end_date})")
        out.write(f"salary as of      : {as_of}")
        out.write(f"run population    : {population.count()}")
        out.write(f"already declared  : {len(already)} (left untouched)")
        out.write(f"to create         : {len(rows)}  ({no_salary} without a salary structure -> no allowable figure)")
        for code, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            if count:
                threshold = tax_year.exemption_thresholds.filter(category__code=code).first()
                amount = f"{threshold.threshold_amount:,.0f}" if threshold else "no threshold configured!"
                out.write(f"  {code:<26} {count:>5}  (threshold {amount})")
        out.write(f"  from gender data          {sum(1 for e in missing if gender_by_employee.get(e.pk) in GENDER_CATEGORY):>5}")
        out.write(f"  fell back to {fallback.code:<13} {sum(1 for e in missing if gender_by_employee.get(e.pk) not in GENDER_CATEGORY):>5}")
        if options["fill_allowable"]:
            out.write(f"allowable to fill : {len(filled)} of {len(unfilled)} existing rows without a snapshot")
        if options["mark_agreed_outside"]:
            out.write(f"to mark agreed    : {len(to_mark)} existing unconfirmed rows -> OUTSIDE_ORBIS on {agreed_on}")
            out.write("                    (new rows created in this same run are stamped too)")

        if options["dry_run"]:
            out.write(self.style.WARNING("Dry run — nothing written."))
            return

        agreed_at = timezone.make_aware(datetime.combine(agreed_on, time(9, 0)))
        # bulk_create bypasses save()/full_clean(), which is intentional here:
        # category and the allowable snapshot are resolved above rather than
        # left to save()'s per-row queries. 1,500+ individual save()s would be
        # 4,500+ queries for no added validation.
        with transaction.atomic():
            if options["mark_agreed_outside"]:
                for row in rows:
                    row.submitted_at = agreed_at
                    row.submission_source = EmployeeTaxDeclaration.SubmissionSource.OUTSIDE_ORBIS
            EmployeeTaxDeclaration.objects.bulk_create(rows, batch_size=500)
            if filled:
                EmployeeTaxDeclaration.objects.bulk_update(
                    filled, ["allowable_investment", "actual_investment"], batch_size=500,
                )
            if to_mark:
                for declaration in to_mark:
                    declaration.submitted_at = agreed_at
                    declaration.submission_source = EmployeeTaxDeclaration.SubmissionSource.OUTSIDE_ORBIS
                EmployeeTaxDeclaration.objects.bulk_update(
                    to_mark, ["submitted_at", "submission_source"], batch_size=500,
                )
        out.write(self.style.SUCCESS(
            f"seed_tax_declarations complete — {len(rows)} created, {len(filled)} snapshots filled, "
            f"{len(to_mark) + (len(rows) if options['mark_agreed_outside'] else 0)} marked agreed outside Orbis"
        ))

    def _resolve_tax_year(self, code: str | None) -> TaxYear:
        if code:
            tax_year = TaxYear.objects.filter(code=code).first()
            if tax_year is None:
                raise CommandError(f"No TaxYear with code '{code}'.")
            return tax_year
        tax_year = TaxYear.objects.filter(is_active=True).order_by("-start_date").first()
        if tax_year is None:
            raise CommandError("No active TaxYear — run seed_tax_year first.")
        return tax_year

    @staticmethod
    def _parse_date(raw, flag):
        if not raw:
            return None
        parsed = parse_date(raw)
        if parsed is None:
            raise CommandError(f"{flag} must be YYYY-MM-DD, got {raw!r}.")
        return parsed
