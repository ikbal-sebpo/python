"""Seed the FY2026-27 in-house tax calculation config, confirmed 2026-08-24
from Finance's actual Tax Calculation.xlsx (docs/PAYROLL_PLAN.md §4).

Idempotent — update_or_create-s by stable keys.

Usage:
    python manage.py seed_tax_year
    python manage.py seed_tax_year --dry-run
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from payroll.models import TaxCategory, TaxExemptionThreshold, TaxSlab, TaxYear

TAX_CATEGORIES = [
    # code, name, sequence
    ("MEN", "Men", 10),
    ("WOMEN_SENIOR", "Women & senior citizen (65+)", 20),
    ("DISABLED_THIRD_GENDER", "Person with disability & third gender", 30),
    ("FREEDOM_FIGHTER", "Gazetted war-wounded freedom fighter", 40),
    ("PARENTS_OF_DISABLED_CHILD", "Parent/guardian of a person with disability", 50),
]

TAX_YEAR = dict(
    code="2026-27",
    start_date=date(2026, 7, 1),
    end_date=date(2027, 6, 30),
    # Year-to-date true-up since 2026-10-07 (docs/TAX_CALCULATION_TRUEUP_PLAN.md).
    calculation_method=TaxYear.CalculationMethod.ITA_2023_SALARY_TRUEUP,
    exemption_divisor=Decimal("3"),
    exemption_cap=Decimal("500000"),
    minimum_tax=Decimal("5000"),
    rebate_income_pct=Decimal("0.0300"),
    rebate_investment_pct=Decimal("0.1000"),
    rebate_cap=Decimal("750000"),
    allowable_investment_pct=Decimal("0.3000"),
    is_active=True,
)

EXEMPTION_THRESHOLDS = [
    # category code, threshold_amount
    ("MEN", Decimal("400000")),
    ("WOMEN_SENIOR", Decimal("450000")),
    ("DISABLED_THIRD_GENDER", Decimal("525000")),
    ("FREEDOM_FIGHTER", Decimal("550000")),
    ("PARENTS_OF_DISABLED_CHILD", Decimal("450000")),
]

TAX_SLABS = [
    # sequence, width (None = remaining), rate
    (1, Decimal("300000"), Decimal("0.1000")),
    (2, Decimal("400000"), Decimal("0.1500")),
    (3, Decimal("500000"), Decimal("0.2000")),
    (4, Decimal("2000000"), Decimal("0.2500")),
    (5, None, Decimal("0.3000")),
]


class Command(BaseCommand):
    help = "Seed TaxCategory/TaxYear/TaxExemptionThreshold/TaxSlab with the 2026-08-24 confirmed FY2026-27 figures."

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
        self.stdout.write(self.style.SUCCESS("seed_tax_year complete"))

    def _seed(self, out):
        categories = {}
        for code, name, sequence in TAX_CATEGORIES:
            obj, created = TaxCategory.objects.update_or_create(
                code=code, defaults={"name": name, "sequence": sequence},
            )
            categories[code] = obj
            out.write(f"tax category: {code} {'created' if created else 'updated'}")

        tax_year, created = TaxYear.objects.update_or_create(
            code=TAX_YEAR["code"], defaults={k: v for k, v in TAX_YEAR.items() if k != "code"},
        )
        out.write(f"tax year: {tax_year.code} {'created' if created else 'updated'}")

        for category_code, threshold_amount in EXEMPTION_THRESHOLDS:
            _, created = TaxExemptionThreshold.objects.update_or_create(
                tax_year=tax_year, category=categories[category_code],
                defaults={"threshold_amount": threshold_amount},
            )
            out.write(f"exemption threshold: {category_code} -> {threshold_amount} {'created' if created else 'updated'}")

        for sequence, width, rate in TAX_SLABS:
            _, created = TaxSlab.objects.update_or_create(
                tax_year=tax_year, sequence=sequence, defaults={"width": width, "rate": rate},
            )
            out.write(f"tax slab: #{sequence} width={width} rate={rate} {'created' if created else 'updated'}")


class _DryRun(Exception):
    pass
