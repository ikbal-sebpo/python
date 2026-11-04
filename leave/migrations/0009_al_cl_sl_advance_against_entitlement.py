"""Turn on `allow_advance_against_entitlement` for AL/CL/SL (2026-09-10):
an application may exceed the live/accrued-so-far balance, so long as it
still fits within max_days_per_year + opening + carried_forward − used −
encashed for this cycle/year. Below that ceiling it's the supervisor's
call at Approve; above it, still hard-blocked as arithmetically impossible.
See docs/LEAVE_POLICY_IMPLEMENTATION.md.

Also re-applies AL's 2026-09-01 carry-forward flip (1.00 / no auto-encash,
see 0004_al_full_carry_forward_no_encashment.py) as a belt-and-suspenders
correction: leave/management/commands/seed_leave_policies.py's hardcoded
kwargs had drifted back to the pre-0004 values (0.50 / True) and would
have silently reverted a real AL policy row to the wrong numbers on a
routine re-run of that command. Fixed here and in the command itself in
the same change.

Reversible: reverse_code restores the pre-change values.
"""

from decimal import Decimal

from django.db import migrations


def flip_forward(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(shift_type__name__in=["AL", "CL", "SL"]).update(
        allow_advance_against_entitlement=True,
    )
    LeavePolicy.objects.filter(shift_type__name="AL").update(
        carry_forward_pct=Decimal("1.00"),
        encash_carry_forward_excess=False,
    )


def flip_backward(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(shift_type__name__in=["AL", "CL", "SL"]).update(
        allow_advance_against_entitlement=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0008_leaveapplication_approved_over_balance_and_more"),
    ]

    operations = [
        migrations.RunPython(flip_forward, flip_backward),
    ]
