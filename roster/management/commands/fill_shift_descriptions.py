"""Fill blank ShiftType and ShiftAttribute descriptions.

Usage:
    python manage.py fill_shift_descriptions --dry-run
    python manage.py fill_shift_descriptions
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from roster.models import ShiftAttribute, ShiftType


SHIFT_TYPE_DESCRIPTIONS = {
    "ABS": "Absence marker for an employee who was not present and does not have an approved leave or scheduled off day.",
    "AL": "Annual Leave. Earned leave used for planned time off; follows the annual leave balance and carry-forward rules.",
    "CL": "Casual Leave. Short planned or emergency leave for personal or family needs within the yearly casual leave entitlement.",
    "CPL": "Compensatory Leave. Time off granted against approved work on a holiday or weekly off, used within policy limits.",
    "CUST": "Custom working shift. Use when the employee's planned duty window does not match a standard morning, evening, or night shift.",
    "EVE": "Evening working shift. Use a specific EVE shift attribute when exact start and end time matters.",
    "FL": "Festival Leave. Organization-declared festival holiday observed according to the approved company calendar.",
    "GH": "Government Holiday. Public holiday roster status for dates declared as government holidays.",
    "HDL": "Half Day Leave. Half-day approved absence for either first half or second half according to leave policy.",
    "LWP": "Leave Without Pay. Unpaid approved absence used when paid leave balance or eligibility is not available.",
    "MOR": "Morning working shift. Use a specific MOR shift attribute when exact start and end time matters.",
    "MTL": "Maternity Leave. Approved maternity absence governed by eligibility, duration, and documentation rules.",
    "NIG": "Night working shift. Use a specific NIG shift attribute when exact overnight timing matters.",
    "OFF": "Scheduled weekly off or rest day. Use when the employee is not expected to work.",
    "SL": "Sick Leave. Medical leave for the employee's illness, governed by sick leave entitlement and certificate rules.",
}


def _fmt_time(value) -> str:
    return value.strftime("%H:%M") if value else ""


def _type_description(shift_type: ShiftType) -> str:
    explicit = SHIFT_TYPE_DESCRIPTIONS.get(shift_type.name.upper())
    if explicit:
        return explicit
    if shift_type.category == ShiftType.Category.WORK:
        if shift_type.shift_start_time and shift_type.shift_end_time:
            return (
                f"Working shift from {_fmt_time(shift_type.shift_start_time)} "
                f"to {_fmt_time(shift_type.shift_end_time)}. Use when this is the employee's planned duty window."
            )
        return "Working shift. Use when the employee is scheduled to work for this roster date."
    if shift_type.category == ShiftType.Category.OFF:
        return "Scheduled off day. Use when the employee is not expected to work."
    if shift_type.category == ShiftType.Category.LEAVE:
        return "Leave roster status. Use for approved absence according to leave policy."
    if shift_type.category == ShiftType.Category.HOLIDAY:
        return "Holiday roster status. Use for approved company or public holidays."
    return "Roster status available for assignment."


def _attribute_description(attr: ShiftAttribute) -> str:
    parent = attr.shift_type.name if attr.shift_type_id else "shift"
    if attr.start_time and attr.end_time:
        return (
            f"{parent} variant from {_fmt_time(attr.start_time)} to {_fmt_time(attr.end_time)}. "
            "Use when the employee needs this exact roster timing."
        )
    if attr.name.upper() == parent.upper():
        return f"{parent} status option. Use when the roster should carry this exact status value."
    return f"Specific option under {parent}. Use when this value best matches the planned roster status."


class Command(BaseCommand):
    help = "Fill blank descriptions for ShiftType and ShiftAttribute rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without saving.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        type_updates = []
        attr_updates = []

        for shift_type in ShiftType.objects.all().order_by(F("sort_order").asc(nulls_last=True), "name"):
            if (shift_type.description or "").strip():
                continue
            shift_type.description = _type_description(shift_type)
            type_updates.append(shift_type)

        for attr in (
            ShiftAttribute.objects
            .select_related("shift_type")
            .all()
            .order_by(
                F("shift_type__sort_order").asc(nulls_last=True),
                "shift_type__name",
                F("sort_order").asc(nulls_last=True),
                "name",
            )
        ):
            if (attr.description or "").strip():
                continue
            attr.description = _attribute_description(attr)
            attr_updates.append(attr)

        self.stdout.write(f"ShiftType descriptions to fill: {len(type_updates)}")
        self.stdout.write(f"ShiftAttribute descriptions to fill: {len(attr_updates)}")

        for obj in type_updates[:20]:
            self.stdout.write(f"  ShiftType {obj.name}: {obj.description}")
        if len(type_updates) > 20:
            self.stdout.write(f"  ... {len(type_updates) - 20} more ShiftType row(s)")

        for obj in attr_updates[:20]:
            self.stdout.write(f"  ShiftAttribute {obj.name}: {obj.description}")
        if len(attr_updates) > 20:
            self.stdout.write(f"  ... {len(attr_updates) - 20} more ShiftAttribute row(s)")

        if dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] no changes saved"))
            return

        with transaction.atomic():
            if type_updates:
                ShiftType.objects.bulk_update(type_updates, ["description"])
            if attr_updates:
                ShiftAttribute.objects.bulk_update(attr_updates, ["description"])

        self.stdout.write(self.style.SUCCESS("Shift descriptions filled."))
