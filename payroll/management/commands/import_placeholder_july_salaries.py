"""One-time import: placeholder SalaryStructure + TDSEntry for July 2026,
for the 22 employee IDs from Master Sheet_July_2026.xlsx's "ON - Software,
Creative & FT" sub-sheet that match a real accounts.CustomUser.employee_id
in Orbis (checked directly against the dev DB — 22 of that sub-sheet's 125
distinct IDs).

IMPORTANT — these are NOT real gross salaries. That sub-sheet's "Salary"
column has exactly two values (30000 / 100000) across all 125 rows; it's a
rounded stand-in HR used purely to pick which side of the 40,000 Tk
night-shift-rate threshold each employee falls on, not individual
compensation. Every row created here carries an explicit PLACEHOLDER note
so this is visible in the register and the admin, not just in this file.

Usage:
    python manage.py import_placeholder_july_salaries
    python manage.py import_placeholder_july_salaries --dry-run
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import CustomUser
from payroll.models import PayrollPeriod, SalaryStructure, TDSEntry

# (employee_id, placeholder gross) — see module docstring.
PLACEHOLDER_SALARIES = [
    ("539", Decimal("100000")),
    ("656", Decimal("100000")),
    ("917", Decimal("100000")),
    ("1086", Decimal("100000")),
    ("1124", Decimal("100000")),
    ("1844", Decimal("100000")),
    ("1892", Decimal("100000")),
    ("2604", Decimal("100000")),
    ("3839", Decimal("100000")),
    ("4612", Decimal("30000")),
    ("4754", Decimal("100000")),
    ("4782", Decimal("100000")),
    ("5086", Decimal("30000")),
    ("5089", Decimal("30000")),
    ("5130", Decimal("30000")),
    ("5416", Decimal("30000")),
    ("5571", Decimal("100000")),
    ("5657", Decimal("30000")),
    ("51761", Decimal("30000")),
    ("53770", Decimal("100000")),
    ("53981", Decimal("30000")),
    ("53983", Decimal("30000")),
]

SALARY_NOTE = "PLACEHOLDER — HR's night-shift threshold indicator (30000/100000), not real gross salary."
TDS_NOTE = "Placeholder — Finance has not uploaded July TDS."


class Command(BaseCommand):
    help = "Import placeholder July 2026 SalaryStructure/TDSEntry for the 22 real employee-ID matches."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        try:
            with transaction.atomic():
                self._import(self.stdout)
                if dry_run:
                    raise _DryRun()
        except _DryRun:
            self.stdout.write(self.style.WARNING("Dry run — rolled back."))
            return
        self.stdout.write(self.style.SUCCESS("import_placeholder_july_salaries complete"))

    def _import(self, out):
        period = PayrollPeriod.for_month(2026, 7)
        found = 0
        for employee_id, gross in PLACEHOLDER_SALARIES:
            employee = CustomUser.objects.filter(employee_id=employee_id).first()
            if employee is None:
                out.write(self.style.WARNING(f"employee_id {employee_id} not found — skipping"))
                continue
            found += 1
            # update_or_create can't take effective_to__isnull as a match kwarg
            # (it isn't a real field name, so the create path would crash) —
            # look up the current structure manually instead.
            structure = SalaryStructure.objects.filter(
                employee=employee, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True,
            ).first()
            created = structure is None
            if structure is None:
                structure = SalaryStructure(employee=employee, effective_from=date(2026, 1, 1), revision_no=1)
            structure.reason = SalaryStructure.Reason.CORRECTION
            structure.gross_monthly = gross
            structure.status = SalaryStructure.Status.APPROVED
            structure.note = SALARY_NOTE
            structure.save()
            out.write(f"salary structure: {employee.employee_id} ({employee.get_full_name()}) -> {gross} "
                       f"{'created' if created else 'updated'}")
            _, tds_created = TDSEntry.objects.update_or_create(
                employee=employee, period=period, defaults=dict(amount=Decimal("0"), note=TDS_NOTE),
            )
            out.write(f"tds entry: {employee.employee_id} -> 0 (placeholder) {'created' if tds_created else 'updated'}")
        out.write(f"{found}/{len(PLACEHOLDER_SALARIES)} employee IDs matched a real Orbis employee.")


class _DryRun(Exception):
    pass
