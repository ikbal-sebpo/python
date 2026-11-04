"""Detect and fix ShiftAttributes whose parent ShiftType doesn't match their name.

A ShiftAttribute named ``MOR 8-5`` should belong to the ``MOR`` ShiftType.
If somebody created it under ``EVE`` by mistake (admin UI, a typo, whatever),
every Shift / Roster picking that attribute via the cell-update view stores
``shift_type=EVE`` — which silently ruins the daily / per-row counts on the
roster grid even though the cell still *renders* as "MOR 8-5".

This command:

1. Walks every ShiftAttribute with a non-blank name.
2. Splits the name on whitespace; the first token (case-insensitive) should
   match the parent ShiftType's name.
3. Reports any mismatch. With ``--yes``, reparents the attribute to the
   correct ShiftType (matched by name) and re-saves every Shift / Roster
   row that referenced it — the existing ``Shift.save`` / ``Roster.save``
   override automatically copies ``shift_attribute.shift_type`` onto
   ``self.shift_type``, so the broken classification is fixed in one shot.

Idempotent: a clean DB shows "0 mismatches found." Safe to re-run after
each manual edit.

Usage::

    manage.py fix_shift_attribute_parents --dry-run
    manage.py fix_shift_attribute_parents --yes
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from roster.models import Roster, Shift, ShiftAttribute, ShiftType


def _expected_parent_name(attr_name: str) -> str | None:
    """First whitespace-delimited token of the attribute name, uppercased.

    ``"MOR 8-5"`` → ``"MOR"``. Returns None for empty / whitespace-only.
    """
    if not attr_name:
        return None
    head = attr_name.strip().split()
    if not head:
        return None
    return head[0].upper()


class Command(BaseCommand):
    help = "Fix ShiftAttributes whose parent ShiftType doesn't match their name prefix."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report mismatches; don't write anything.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Required for non-dry-run: confirms you intend to repair the data.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        confirmed = opts["yes"]

        # Build a name → ShiftType lookup once.
        types_by_name = {
            st.name.upper(): st
            for st in ShiftType.objects.filter(is_active=True)
        }

        attrs = ShiftAttribute.objects.select_related("shift_type").order_by(
            "shift_type__name", "name",
        )
        mismatches: list[tuple[ShiftAttribute, ShiftType]] = []
        unknown_prefixes: list[ShiftAttribute] = []

        for attr in attrs:
            expected_name = _expected_parent_name(attr.name)
            if not expected_name:
                continue
            expected_st = types_by_name.get(expected_name)
            if expected_st is None:
                # The name prefix doesn't match any known ShiftType — could
                # be intentional (e.g. an attribute named "Custom" under
                # CUST). Report so a human can decide; never auto-fix.
                if attr.shift_type and not (attr.shift_type.name.upper().startswith(expected_name)
                                            or expected_name.startswith(attr.shift_type.name.upper())):
                    unknown_prefixes.append(attr)
                continue
            if attr.shift_type_id != expected_st.pk:
                mismatches.append((attr, expected_st))

        if not mismatches and not unknown_prefixes:
            self.stdout.write(self.style.SUCCESS(
                "0 mismatches found. ShiftAttribute parents look correct."
            ))
            return

        if mismatches:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{len(mismatches)} mismatched attribute(s):"
            ))
            for attr, expected in mismatches:
                current = attr.shift_type.name if attr.shift_type else "<none>"
                shift_count = Shift.objects.filter(shift_attribute=attr).count()
                roster_count = Roster.objects.filter(shift_attribute=attr).count()
                self.stdout.write(
                    f"  · {attr.name!r:18s} parent={current!r:8s}  "
                    f"→ should be {expected.name!r:8s}  "
                    f"(used by {shift_count} Shift, {roster_count} Roster row{'s' if roster_count != 1 else ''})"
                )

        if unknown_prefixes:
            self.stdout.write(self.style.WARNING(
                f"\n{len(unknown_prefixes)} attribute(s) with an unrecognised "
                f"name prefix (review manually):"
            ))
            for attr in unknown_prefixes:
                self.stdout.write(
                    f"  ? {attr.name!r:18s} parent={attr.shift_type.name!r:8s}"
                )

        if dry:
            self.stdout.write(self.style.WARNING("\n--dry-run: nothing written."))
            return

        if not confirmed:
            raise CommandError(
                "Refusing to write changes without --yes. Pass --yes once you "
                "have reviewed the mismatch list above."
            )

        if not mismatches:
            self.stdout.write(
                "No clear mismatches to fix; manual-review items left alone."
            )
            return

        with transaction.atomic():
            shift_resaved = roster_resaved = 0
            for attr, expected in mismatches:
                attr.shift_type = expected
                attr.save(update_fields=["shift_type"])
                # Re-save dependent rows so their `shift_type` field is
                # corrected by the model's save-time override.
                for s in Shift.objects.filter(shift_attribute=attr).select_related("shift_attribute"):
                    s.save()
                    shift_resaved += 1
                for r in Roster.objects.filter(shift_attribute=attr).select_related("shift_attribute"):
                    r.save()
                    roster_resaved += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nFixed {len(mismatches)} attribute(s). "
            f"Re-saved {shift_resaved} Shift and {roster_resaved} Roster row(s)."
        ))
