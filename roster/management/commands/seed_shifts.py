"""Seed canonical ShiftType + ShiftAttribute rows for production.

Data is embedded inline (sourced from the SEBPO export
ShiftType-2026-05-06.csv / ShiftAttribute-2026-05-06.csv). The command is
idempotent — re-running update_or_create-s by stable keys.

Replaces the old fixture pair:
    roster/fixtures/seed_shift_types.json
    roster/fixtures/seed_shift_attributes.json
plus the old ``seed_shift_attributes`` management command.

Usage:
    python manage.py seed_shifts             # apply
    python manage.py seed_shifts --dry-run   # report only
"""

from __future__ import annotations

from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from roster.models import ShiftAttribute, ShiftType


# Each row: (name, description, category, shift_timezone, start, end, color)
# Booleans (is_working_shift, has_attribute) are managed by the model's
# save() overrides — don't set them here.
SHIFT_TYPES = [
    ("FL",   "HR-circulated festival days. Universal — everyone observes the same calendar.", "HOLIDAY", "", None, None, ""),
    ("GH",   "Government Holiday", "HOLIDAY", "", None, None, "brand-blue"),
    ("ABS",  "Abscent", "LEAVE", "", None, None, ""),
    ("AL",   "Earned at 1 day per 18 worked days; 50% carries forward, 50% encashable. 60-day carry-forward cap freezes the entitlement.", "LEAVE", "", None, None, ""),
    ("CL",   "10 days per calendar year for personal/family/emergency reasons. Pro-rated for mid-year joiners. No carry-forward.", "LEAVE", "", None, None, ""),
    ("CPL",  "Auto-granted +1 day when an employee works on a Holiday-tagged date. Must be consumed within 3 working days of grant.", "LEAVE", "", None, None, ""),
    ("HDL",  "Half-day off with half-basic-salary deduction. Single date, AM or PM. Doesn't bridge sandwich.", "LEAVE", "", None, None, ""),
    ("LWP",  "Unpaid leave — extension of maternity, or discretionary regularization of unauthorized absence. Doesn't bridge sandwich.", "LEAVE", "", None, None, ""),
    ("MTL",  "16 weeks (8 pre + 8 post delivery) for female employees with >=6 months service and <2 surviving children. Medical certificate required.", "LEAVE", "", None, None, ""),
    ("SL",   "14 days per calendar year for own illness. Pro-rated for mid-year joiners. Doctor's certificate required for 3+ day applications.", "LEAVE", "", None, None, ""),
    ("OFF",  "Day Off", "OFF", "", None, None, "text-mute"),
    ("CUST", "Custom", "WORK", "Asia/Dhaka", time(19, 0), time(4, 0), "brand-teal"),
    ("EVE",  "14:00 – 23:00", "WORK", "Asia/Dhaka", time(14, 0), time(23, 0), "brand-blue"),
    ("MOR",  "07:00 – 14:00", "WORK", "Asia/Dhaka", time(7, 0), time(16, 0), "brand-accent"),
    ("NIG",  "22:00 – 07:00 shift", "WORK", "Asia/Dhaka", time(22, 0), time(7, 0), "brand-navy"),
]

# Each row: (parent_shift_type_name, attribute_name, start_time, end_time)
SHIFT_ATTRIBUTES = [
    ("AL",   "AL",       None,        None),
    ("CL",   "CL",       None,        None),
    ("CPL",  "CPL",      None,        None),
    ("LWP",  "LWP",      None,        None),
    ("MTL",  "MTL",      None,        None),
    ("SL",   "SL",       None,        None),
    ("OFF",  "OFF",      None,        None),
    ("CUST", "Custom",   time(19, 0), time(4, 0)),
    ("EVE",  "EVE 1-10", time(13, 0), time(22, 0)),
    ("EVE",  "EVE 1-11", time(13, 0), time(23, 0)),
    ("EVE",  "EVE 2-10", time(14, 0), time(22, 0)),
    ("EVE",  "EVE 2-11", time(14, 0), time(23, 0)),
    ("EVE",  "EVE 3-10", time(15, 0), time(22, 0)),
    ("EVE",  "EVE 3-11", time(15, 0), time(23, 0)),
    ("EVE",  "EVE 4-11", time(16, 0), time(23, 0)),
    ("MOR",  "MOR 7-4",  time(7, 0),  time(16, 0)),
    ("MOR",  "MOR 7-5",  time(7, 0),  time(17, 0)),
    ("NIG",  "NIG 7-4",  time(19, 0), time(4, 0)),
    ("NIG",  "NIG 8-7",  time(20, 0), time(7, 0)),
    ("NIG",  "NIG 9-7",  time(21, 0), time(7, 0)),
    ("NIG",  "NIG 10-7", time(22, 0), time(7, 0)),
    ("NIG",  "NIG 11-7", time(23, 0), time(7, 0)),
]


class _DryRun(Exception):
    """Sentinel: rolls back the outer atomic block on --dry-run."""


class Command(BaseCommand):
    help = "Seed canonical ShiftType + ShiftAttribute rows from the production export."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would change without writing anything.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        try:
            with transaction.atomic():
                self._seed_types()
                self._seed_attributes()
                if dry:
                    self.stdout.write(self.style.WARNING("[dry-run] rolling back"))
                    raise _DryRun()
        except _DryRun:
            return
        self.stdout.write(self.style.SUCCESS("✅ seed_shifts complete"))

    def _seed_types(self):
        created = updated = 0
        for name, description, category, tz, start, end, color in SHIFT_TYPES:
            _, was_created = ShiftType.objects.update_or_create(
                name=name,
                defaults={
                    "description": description,
                    "category": category,
                    "shift_timezone": tz,
                    "shift_start_time": start,
                    "shift_end_time": end,
                    "color": color,
                    "is_active": True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(
            f"ShiftTypes: {created} created, {updated} updated."
        )

    def _seed_attributes(self):
        type_by_name = {st.name: st for st in ShiftType.objects.all()}
        created = updated = skipped = 0
        for parent_name, attr_name, start, end in SHIFT_ATTRIBUTES:
            parent = type_by_name.get(parent_name)
            if parent is None:
                self.stdout.write(self.style.WARNING(
                    f"  skip {attr_name} — parent ShiftType '{parent_name}' missing"
                ))
                skipped += 1
                continue
            _, was_created = ShiftAttribute.objects.update_or_create(
                shift_type=parent,
                name=attr_name,
                defaults={
                    "start_time": start,
                    "end_time": end,
                    "description": "",
                    "is_active": True,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(
            f"ShiftAttributes: {created} created, {updated} updated, "
            f"{skipped} skipped."
        )
