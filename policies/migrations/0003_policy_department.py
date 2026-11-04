"""Add Policy.department (required).

Added nullable first, backfilled from each policy's owner department (falling
back to the first active department if the owner has none), then made
non-nullable — the standard 3-step pattern for a required FK on a non-empty
table.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def backfill_department(apps, schema_editor):
    Policy = apps.get_model("policies", "Policy")
    Department = apps.get_model("accounts", "Department")

    fallback = None

    for policy in Policy.objects.select_related("owner").filter(department__isnull=True):
        department_id = policy.owner.department_id if policy.owner_id else None
        if department_id is None:
            if fallback is None:
                fallback = Department.objects.filter(is_active=True).order_by("name").first()
            department_id = fallback.pk if fallback else None
        policy.department_id = department_id
        policy.save(update_fields=["department"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_department_additional_managers_and_more"),
        ("policies", "0002_seed_group_and_nav"),
    ]

    operations = [
        migrations.AddField(
            model_name="policy",
            name="department",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="policies",
                to="accounts.department",
                help_text="Which department this policy is issued from.",
            ),
        ),
        migrations.RunPython(backfill_department, noop),
        migrations.AlterField(
            model_name="policy",
            name="department",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="policies",
                to="accounts.department",
                help_text="Which department this policy is issued from.",
            ),
        ),
    ]
