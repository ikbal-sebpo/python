"""Cascade-delete ShiftType rows along with everything that points to them.

Django admin refuses to delete a ShiftType when there are PROTECT-bound
dependents (Roster patterns, Shifts, LeaveBalances, LeaveApplications,
EncashmentRequests). This command removes the ShiftTypes plus *every*
dependent in the correct order, so a stale set of seeded "test" ShiftTypes
can be wiped out without hand-typing 6 separate deletes in `manage.py shell`.

Usage::

    # Preview what gets deleted, no DB writes
    manage.py delete_shift_types "Morning" "Weekly Off" --dry-run

    # Actually wipe them
    manage.py delete_shift_types "Morning" "Weekly Off" "Casual Leave"

Because this is destructive, the command refuses to run without `--yes` once
you're past the dry-run preview — you have to opt in explicitly.

Cascade order (each step removes the PROTECT block that prevents the next):

    1. LeaveBalanceTransactions tied to balances of these ShiftTypes' policies
    2. EncashmentRequests on those policies
    3. LeaveBalances on those policies
    4. LeaveApplications whose `leave_type` is one of these ShiftTypes
    5. Shifts using these ShiftTypes
    6. Roster pattern rows using these ShiftTypes
    7. The ShiftTypes themselves (LeavePolicy + ShiftAttribute cascade
       automatically — both are CASCADE on ShiftType deletion)
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from leave.models import (
    EncashmentRequest,
    LeaveApplication,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
)
from roster.models import Roster, Shift, ShiftType


class Command(BaseCommand):
    help = "Cascade-delete ShiftType rows along with all PROTECT-bound dependents."

    def add_arguments(self, parser):
        parser.add_argument(
            "names",
            nargs="+",
            help='One or more ShiftType names to delete (case-sensitive). '
                 'Wrap in quotes if the name contains spaces.',
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show counts; don't write anything.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Required for non-dry-run: confirms you intend to delete data.",
        )

    def handle(self, *args, **opts):
        names = opts["names"]
        dry = opts["dry_run"]
        confirmed = opts["yes"]

        targets = ShiftType.objects.filter(name__in=names)
        found = set(targets.values_list("name", flat=True))
        missing = set(names) - found
        if missing:
            self.stdout.write(self.style.WARNING(
                f"Not found (skipped): {', '.join(sorted(missing))}"
            ))

        if not targets.exists():
            raise CommandError("No matching ShiftTypes — nothing to do.")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nTargeting {targets.count()} ShiftType(s):"
        ))
        for st in targets.order_by("name"):
            self.stdout.write(f"  · {st.name}  (category={st.category}, pk={st.pk})")

        # Build the dependency querysets up front. Done as querysets (not
        # materialized lists) so the counts are accurate at delete time too.
        policies = LeavePolicy.objects.filter(shift_type__in=targets)
        balances = LeaveBalance.objects.filter(leave_policy__in=policies)
        txns = LeaveBalanceTransaction.objects.filter(balance__in=balances)
        encashments = EncashmentRequest.objects.filter(leave_policy__in=policies)
        applications = LeaveApplication.objects.filter(leave_type__in=targets)
        shifts = Shift.objects.filter(shift_type__in=targets)
        rosters = Roster.objects.filter(shift_type__in=targets)

        self.stdout.write("\nDependents to be deleted:")
        rows = [
            ("LeaveBalanceTransactions", txns.count()),
            ("EncashmentRequests",       encashments.count()),
            ("LeaveBalances",            balances.count()),
            ("LeaveApplications",        applications.count()),
            ("Shifts",                   shifts.count()),
            ("Roster pattern rows",      rosters.count()),
            ("LeavePolicies (cascade)",  policies.count()),
            ("ShiftTypes (target)",      targets.count()),
        ]
        for label, n in rows:
            self.stdout.write(f"  {label:30s} {n}")

        if dry:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run: nothing written."
            ))
            return

        if not confirmed:
            raise CommandError(
                "Refusing to delete without --yes. Pass --yes once you've "
                "reviewed the dry-run counts above."
            )

        with transaction.atomic():
            txns.delete()
            encashments.delete()
            balances.delete()
            applications.delete()
            shifts.delete()
            rosters.delete()
            # ShiftType.delete() cascades to LeavePolicy and ShiftAttribute.
            count, _ = targets.delete()

        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {count} ShiftType(s) and all dependents."
        ))
