"""Seed ShiftAttribute rows for production.

Recreates the time-variant attributes (MOR 7-4, EVE 1-10, NIG 9-7, etc.)
from SEBPO's existing roster system on top of the new production ShiftTypes
(MOR, EVE, NIG, CUST + leave/holiday/off types).

ShiftType lookups are by **name**, not PK — so this works the same against
dev, staging, or production regardless of seed-row IDs.

Idempotent: existing (shift_type, name) pairs are left alone. Missing ones
are created. Use ``--dry-run`` to preview.

Usage::

    manage.py seed_shift_attributes --dry-run
    manage.py seed_shift_attributes
"""

from __future__ import annotations

from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from roster.models import ShiftAttribute, ShiftType


# (parent_shift_type_name, attribute_name, start_time, end_time)
# `None`/`None` for start/end means a non-time variant (leave/off mirror).
SEED: list[tuple[str, str, time | None, time | None]] = [
    # ── Working shifts: time variants ──
    ("MOR",  "MOR 7-4",   time(7, 0),   time(16, 0)),
    ("MOR",  "MOR 7-5",   time(7, 0),   time(17, 0)),

    ("EVE",  "EVE 1-10",  time(13, 0),  time(22, 0)),
    ("EVE",  "EVE 1-11",  time(13, 0),  time(23, 0)),
    ("EVE",  "EVE 2-10",  time(14, 0),  time(22, 0)),
    ("EVE",  "EVE 2-11",  time(14, 0),  time(23, 0)),
    ("EVE",  "EVE 3-10",  time(15, 0),  time(22, 0)),
    ("EVE",  "EVE 3-11",  time(15, 0),  time(23, 0)),
    ("EVE",  "EVE 4-11",  time(16, 0),  time(23, 0)),

    ("NIG",  "NIG 7-4",   time(19, 0),  time(4, 0)),
    ("NIG",  "NIG 8-7",   time(20, 0),  time(7, 0)),
    ("NIG",  "NIG 9-7",   time(21, 0),  time(7, 0)),
    ("NIG",  "NIG 10-7",  time(22, 0),  time(7, 0)),
    ("NIG",  "NIG 11-7",  time(23, 0),  time(7, 0)),

    ("CUST", "Custom",    time(19, 0),  time(4, 0)),

    # ── Leave / off: 1:1 mirrors (no time variants) ──
    # Kept so the roster-cell UI can offer a single dropdown across all
    # ShiftAttributes regardless of category. Drop any line you don't need.
    ("AL",   "AL",        None,         None),
    ("CL",   "CL",        None,         None),
    ("CPL",  "CPL",       None,         None),
    ("LWP",  "LWP",       None,         None),
    ("MTL",  "MTL",       None,         None),
    ("SL",   "SL",        None,         None),
    ("OFF",  "OFF",       None,         None),
]


class Command(BaseCommand):
    help = "Seed ShiftAttribute rows by ShiftType name. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without writing.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        # Cache ShiftTypes by name (case-insensitive lookup, exact-stored).
        shift_types = {st.name.upper(): st for st in ShiftType.objects.all()}

        created = matched = missing_parent = 0
        rows_to_create: list[ShiftAttribute] = []

        for parent_name, attr_name, start, end in SEED:
            parent = shift_types.get(parent_name.upper())
            if parent is None:
                self.stdout.write(self.style.WARNING(
                    f"  skip   {attr_name:12s} — ShiftType {parent_name!r} not found"
                ))
                missing_parent += 1
                continue

            existing = ShiftAttribute.objects.filter(
                shift_type=parent, name=attr_name,
            ).first()
            if existing:
                self.stdout.write(
                    f"  exists {attr_name:12s} → {parent.name}  (pk={existing.pk})"
                )
                matched += 1
                continue

            self.stdout.write(self.style.SUCCESS(
                f"  create {attr_name:12s} → {parent.name}"
                + (f"  ({start}–{end})" if start and end else "")
            ))
            rows_to_create.append(ShiftAttribute(
                shift_type=parent,
                name=attr_name,
                start_time=start,
                end_time=end,
                is_active=True,
            ))
            created += 1

        if dry:
            self.stdout.write(self.style.WARNING(
                f"\n--dry-run: would create {created}, "
                f"matched {matched}, missing {missing_parent}. Nothing written."
            ))
            return

        if rows_to_create:
            with transaction.atomic():
                # Save one-by-one so ShiftAttribute.save() runs the
                # has_attribute sync on each parent ShiftType.
                for row in rows_to_create:
                    row.save()

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {created} created, {matched} already existed, "
            f"{missing_parent} skipped (parent ShiftType missing)."
        ))
