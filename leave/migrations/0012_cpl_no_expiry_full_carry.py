"""CpL policy change (2026-10-01): grants no longer expire after 3 working
days, and unused CpL carries forward 100% into the next calendar year.

No column change (the AlterField is help_text only), so no collation step. Keyed on
`cpl_grant_on_holiday_work=True` (the grant-driven policy) rather than a
ShiftType name, so it finds the row whatever the type is labelled.
`seed_leave_policies` writes the same values, so a re-seed is a no-op.
"""

from decimal import Decimal

from django.db import migrations, models


def forwards(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(cpl_grant_on_holiday_work=True).update(
        cpl_use_within_days=0,
        carry_forward_pct=Decimal("1.00"),
    )


def backwards(apps, schema_editor):
    LeavePolicy = apps.get_model("leave", "LeavePolicy")
    LeavePolicy.objects.filter(cpl_grant_on_holiday_work=True).update(
        cpl_use_within_days=3,
        carry_forward_pct=Decimal("0"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0011_cpl_grant_work_context"),
    ]

    operations = [
        # help_text only — no column change.
        migrations.AlterField(
            model_name="leavepolicy",
            name="cpl_use_within_days",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Working-day deadline to consume a granted CpL day. "
                          "0 or blank = grants never expire (CpL since 2026-10-01; "
                          "it was 3 before that).",
                null=True,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
