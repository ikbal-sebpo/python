"""Refinement to AL's worked-day-ratio accrual (2026-09-02) — see
docs/LEAVE_POLICY_IMPLEMENTATION.md §5.7: sets AL's `max_days_per_year` to
12, now read by `leave/services.py::live_accrued_al` as a per-cycle accrual
cap (a new use of this field for a WORKED_DAY_RATIO policy — it's otherwise
only consumed by `prorate_entitlement()` for FIXED_ANNUAL policies like
CL/SL).

Originally landed as an interim step pending CHRO sign-off on a full switch
to a flat fixed grant — decided the same day that this refinement already
covers both goals (paid leave doesn't cost AL; nobody exceeds 12/cycle), so
the FIXED_ANNUAL switch is shelved, not merely deferred. This is the settled
rule, not interim.

The other half of this refinement (worked days now also counting paid-leave
shifts, via the already-correct `LeavePolicy.is_paid` flag) needed no data
change — it reads `is_paid` values that were already correct.

Reversible: reverse_code restores the pre-2026-09-02 value (0, uncapped).
"""

from django.db import migrations


def flip_forward(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(shift_type__name="AL").update(max_days_per_year=12)


def flip_backward(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(shift_type__name="AL").update(max_days_per_year=0)


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0004_al_full_carry_forward_no_encashment"),
    ]

    operations = [
        migrations.RunPython(flip_forward, flip_backward),
    ]
