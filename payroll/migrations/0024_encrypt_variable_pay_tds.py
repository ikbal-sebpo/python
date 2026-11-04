"""Encrypt money at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5.

Period inputs: VariablePayEntry.amount + TDSEntry.amount.

One-way: the DECIMAL/VARCHAR/JSON columns become TEXT holding Fernet tokens,
and because an ALTER would merely cast the existing values to plaintext
strings, the rows are DELETED first. The purge refuses on any non-empty table
unless PAYROLL_PURGE_ON_MIGRATE=1 is exported for this one `migrate` — there
is no real money in any Orbis database yet (decision 2026-10-05), and the
guard makes sure nobody repeats this against real data later.
"""

import core.fields
from django.db import migrations

from core.migration_utils import guarded_purge


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0023_encrypt_tax"),
    ]

    operations = [
        migrations.RunPython(
            guarded_purge(
                "payroll.VariablePayEntry",
                "payroll.TDSEntry",
            ),
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='tdsentry',
            name='amount',
            field=core.fields.EncryptedDecimalField(decimal_places=2, max_digits=10),
        ),
        migrations.AlterField(
            model_name='variablepayentry',
            name='amount',
            field=core.fields.EncryptedDecimalField(decimal_places=2, help_text='Always a positive magnitude.', max_digits=10),
        ),
    ]
