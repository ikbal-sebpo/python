"""Flip AL's LeavePolicy to the manager-confirmed rules (2026-09-01):
all unused AL carries forward every cycle (no 50% cut, only the 60-day
lifetime cap), and nothing is auto-encashed. Supersedes the signed v2.0
policy PDF's "only 50%" + "cashed quarterly" clauses — see
docs/LEAVE_POLICY_IMPLEMENTATION.md, "Risks worth calling out".

Reversible: reverse_code restores the pre-2026-09-01 values (0.50 / True)
in case this needs to be rolled back before the rollout (open_initial_al_
cycles / reconcile_shift_leave_usage / close_al_cycles) actually runs.
"""

from decimal import Decimal

from django.db import migrations


def flip_forward(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(shift_type__name="AL").update(
        carry_forward_pct=Decimal("1.00"),
        encash_carry_forward_excess=False,
    )


def flip_backward(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(shift_type__name="AL").update(
        carry_forward_pct=Decimal("0.50"),
        encash_carry_forward_excess=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0003_leavebalance_cycle_closed_leavebalance_cycle_end_and_more"),
    ]

    operations = [
        migrations.RunPython(flip_forward, flip_backward),
    ]
