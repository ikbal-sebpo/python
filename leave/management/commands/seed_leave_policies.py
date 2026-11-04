"""(Re)seed LeavePolicy rows linked to existing LEAVE-category ShiftTypes.

The original migration ``0004_seed_policy_v2_data`` created policies via
``ShiftType.objects.get_or_create(name="Casual Leave")`` — works fine on
dev where ShiftTypes are named with the long form, but on production the
ShiftTypes use short codes (``CL``, ``AL``, ``SL``…). The migration ended
up creating *phantom* "Casual Leave" / "Sick Leave" rows separate from the
real "CL" / "SL" rows; when the phantoms were later cleaned up, every
``LeavePolicy`` cascade-deleted with them and the leave-application
dropdown went silent.

This command resolves the parent ShiftType by trying each alias in turn
(short code first, long form second) so it works across environments. It
``update_or_create``-s keyed on ``shift_type`` — re-running is a no-op
when nothing has changed.

Usage::

    manage.py seed_leave_policies --dry-run     # report only
    manage.py seed_leave_policies               # apply

Doesn't touch ABS / FL / GH / OFF / working ShiftTypes — those don't
have policies (Festival days run from Holiday rows, ABS was retired).
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from leave.models import LeavePolicy
from roster.models import ShiftType


# Each entry: short-code alias (production), long-name alias (dev), policy kwargs.
# Values mirror leave/migrations/0004_seed_policy_v2_data.py — do not drift.
LEAVE_POLICIES = [
    {
        "aliases": ("AL", "Annual Leave"),
        "description": (
            "Earned at 1 day per 18 worked days (paid leave — CL/SL/CPL/MtL/AL "
            "itself — counts as worked, so it doesn't cost AL accrual), capped "
            "at 12 days per cycle. 100% carries forward (revised 2026-09-01, "
            "supersedes the signed v2.0 PDF's 50%/quarterly-encash clauses — "
            "see leave/migrations/0004_al_full_carry_forward_no_encashment.py "
            "and docs/LEAVE_POLICY_IMPLEMENTATION.md), nothing auto-encashed. "
            "60-day carry-forward cap freezes the entitlement. A request may "
            "advance against this cycle's not-yet-accrued days (revised "
            "2026-09-10, see docs/LEAVE_POLICY_IMPLEMENTATION.md)."
        ),
        "kwargs": {
            "max_days_per_year": 12,
            "is_paid": True,
            "applies_to_gender": "ANY",
            "min_service_months": 12,
            "max_surviving_children": None,
            "prorate_on_join": False,
            "bridges_sandwich": True,
            "cycle": "ANNIVERSARY",
            "accrual_method": "WORKED_DAY_RATIO",
            "accrual_ratio_denominator": 18,
            "carry_forward_pct": Decimal("1.00"),
            "max_carry_forward_days": 60,
            "freezes_entitlement_at_cap": True,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 0,
            "cpl_grant_on_holiday_work": False,
            "cpl_use_within_days": None,
            "allow_advance_against_entitlement": True,
        },
    },
    {
        "aliases": ("CL", "Casual Leave"),
        "description": (
            "10 days per calendar year for personal/family/emergency reasons. "
            "Pro-rated for mid-year joiners. No carry-forward. A request may "
            "advance against the full annual entitlement, not just what's "
            "already been granted (revised 2026-09-10, see "
            "docs/LEAVE_POLICY_IMPLEMENTATION.md)."
        ),
        "kwargs": {
            "max_days_per_year": 10,
            "is_paid": True,
            "applies_to_gender": "ANY",
            "min_service_months": 0,
            "max_surviving_children": None,
            "prorate_on_join": True,
            "bridges_sandwich": True,
            "cycle": "CALENDAR",
            "accrual_method": "FIXED_ANNUAL",
            "accrual_ratio_denominator": 0,
            "carry_forward_pct": Decimal("0"),
            "max_carry_forward_days": None,
            "freezes_entitlement_at_cap": False,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 0,
            "cpl_grant_on_holiday_work": False,
            "cpl_use_within_days": None,
            "allow_advance_against_entitlement": True,
        },
    },
    {
        "aliases": ("SL", "Sick Leave"),
        "description": (
            "14 days per calendar year for own illness. Pro-rated for mid-year "
            "joiners. Doctor's certificate required for 3+ day applications. "
            "A request may advance against the full annual entitlement, not "
            "just what's already been granted (revised 2026-09-10, see "
            "docs/LEAVE_POLICY_IMPLEMENTATION.md)."
        ),
        "kwargs": {
            "max_days_per_year": 14,
            "is_paid": True,
            "applies_to_gender": "ANY",
            "min_service_months": 0,
            "max_surviving_children": None,
            "prorate_on_join": True,
            "bridges_sandwich": True,
            "cycle": "CALENDAR",
            "accrual_method": "FIXED_ANNUAL",
            "accrual_ratio_denominator": 0,
            "carry_forward_pct": Decimal("0"),
            "max_carry_forward_days": None,
            "freezes_entitlement_at_cap": False,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 3,
            "cpl_grant_on_holiday_work": False,
            "cpl_use_within_days": None,
            "allow_advance_against_entitlement": True,
        },
    },
    {
        "aliases": ("MTL", "Maternity Leave"),
        "description": (
            "16 weeks (8 pre + 8 post delivery) for female employees with >=6 "
            "months service and <2 surviving children. Medical cert required."
        ),
        "kwargs": {
            "max_days_per_year": 112,
            "is_paid": True,
            "applies_to_gender": "FEMALE",
            "min_service_months": 6,
            "max_surviving_children": 1,
            "prorate_on_join": False,
            "bridges_sandwich": True,
            "cycle": "CALENDAR",
            "accrual_method": "NONE",
            "accrual_ratio_denominator": 0,
            "carry_forward_pct": Decimal("0"),
            "max_carry_forward_days": None,
            "freezes_entitlement_at_cap": False,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 1,
            "cpl_grant_on_holiday_work": False,
            "cpl_use_within_days": None,
            "allow_advance_against_entitlement": False,
        },
    },
    {
        "aliases": ("CPL", "Compensatory Leave"),
        "description": (
            "Auto-granted +1 day when an employee works on a Holiday-tagged "
            "date. Never expires and carries forward 100% across calendar "
            "years (2026-10-01; previously had to be used within 3 working days)."
        ),
        "kwargs": {
            "max_days_per_year": 0,
            "is_paid": True,
            "applies_to_gender": "ANY",
            "min_service_months": 0,
            "max_surviving_children": None,
            "prorate_on_join": True,
            "bridges_sandwich": True,
            "cycle": "CALENDAR",
            "accrual_method": "NONE",
            "accrual_ratio_denominator": 0,
            "carry_forward_pct": Decimal("1.00"),
            "max_carry_forward_days": None,
            "freezes_entitlement_at_cap": False,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 0,
            "cpl_grant_on_holiday_work": True,
            "cpl_use_within_days": 0,
            "allow_advance_against_entitlement": False,
        },
    },
    {
        "aliases": ("LWP", "Leave Without Pay"),
        "description": (
            "Unpaid leave — extension of maternity, or discretionary "
            "regularisation of unauthorised absence. Doesn't bridge sandwich."
        ),
        "kwargs": {
            "max_days_per_year": 0,
            "is_paid": False,
            "applies_to_gender": "ANY",
            "min_service_months": 0,
            "max_surviving_children": None,
            "prorate_on_join": True,
            "bridges_sandwich": False,
            "cycle": "CALENDAR",
            "accrual_method": "NONE",
            "accrual_ratio_denominator": 0,
            "carry_forward_pct": Decimal("0"),
            "max_carry_forward_days": None,
            "freezes_entitlement_at_cap": False,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 0,
            "cpl_grant_on_holiday_work": False,
            "cpl_use_within_days": None,
            "allow_advance_against_entitlement": False,
        },
    },
    {
        "aliases": ("HDL", "Half Day Leave", "HdL"),
        "description": (
            "Half-day off with half-basic-salary deduction. Single date, AM "
            "or PM. Doesn't bridge sandwich."
        ),
        "kwargs": {
            "max_days_per_year": 0,
            "is_paid": False,
            "applies_to_gender": "ANY",
            "min_service_months": 0,
            "max_surviving_children": None,
            "prorate_on_join": True,
            "bridges_sandwich": False,
            "cycle": "CALENDAR",
            "accrual_method": "NONE",
            "accrual_ratio_denominator": 0,
            "carry_forward_pct": Decimal("0"),
            "max_carry_forward_days": None,
            "freezes_entitlement_at_cap": False,
            "encash_carry_forward_excess": False,
            "cert_required_min_days": 0,
            "cpl_grant_on_holiday_work": False,
            "cpl_use_within_days": None,
            "allow_advance_against_entitlement": False,
        },
    },
]


class Command(BaseCommand):
    help = "(Re)seed LeavePolicy rows for the standard SEBPO leave types."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change; don't write anything.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        # Build name → ShiftType lookup once (case-insensitive).
        by_name = {
            st.name.upper(): st
            for st in ShiftType.objects.filter(
                is_active=True, category=ShiftType.Category.LEAVE,
            )
        }

        created = updated = skipped = 0
        details: list[str] = []

        for entry in LEAVE_POLICIES:
            aliases = entry["aliases"]
            kwargs = entry["kwargs"]
            description = entry["description"]

            # First alias that resolves wins.
            shift_type = None
            for alias in aliases:
                shift_type = by_name.get(alias.upper())
                if shift_type:
                    break

            if shift_type is None:
                self.stdout.write(self.style.WARNING(
                    f"  skip   {aliases[0]:6s}  — no matching ShiftType "
                    f"(tried: {', '.join(aliases)})"
                ))
                skipped += 1
                continue

            existing = LeavePolicy.objects.filter(shift_type=shift_type).first()
            if existing:
                # Compare to detect a real diff.
                changed = any(
                    getattr(existing, k) != v for k, v in kwargs.items()
                )
                if changed:
                    self.stdout.write(self.style.SUCCESS(
                        f"  update {aliases[0]:6s}  → ShiftType {shift_type.name!r} (pk={shift_type.pk})"
                    ))
                    updated += 1
                else:
                    self.stdout.write(
                        f"  same   {aliases[0]:6s}  → ShiftType {shift_type.name!r} (pk={shift_type.pk})"
                    )
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  create {aliases[0]:6s}  → ShiftType {shift_type.name!r} (pk={shift_type.pk})"
                ))
                created += 1

            if not dry:
                LeavePolicy.objects.update_or_create(
                    shift_type=shift_type,
                    defaults=kwargs,
                )
                # Keep ShiftType description in sync if it's blank.
                if not (shift_type.description or "").strip():
                    shift_type.description = description
                    shift_type.save(update_fields=["description"])

        self.stdout.write("")
        verb = "would" if dry else "did"
        self.stdout.write(self.style.SUCCESS(
            f"Done. {verb} create {created}, update {updated}, skip {skipped}."
        ))
        if dry:
            self.stdout.write(self.style.WARNING(
                "--dry-run: nothing written. Re-run without --dry-run to apply."
            ))
