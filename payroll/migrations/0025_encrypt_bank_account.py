"""Encrypt money at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5.

EmployeeBankAccount.account_number + routing_number.

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
        ("payroll", "0024_encrypt_variable_pay_tds"),
    ]

    operations = [
        migrations.RunPython(
            guarded_purge(
                "payroll.EmployeeBankAccount",
            ),
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='employeebankaccount',
            name='account_number',
            field=core.fields.EncryptedCharField(help_text='For MFS wallets, this is the registered wallet phone number.', max_length=50),
        ),
        migrations.AlterField(
            model_name='employeebankaccount',
            name='routing_number',
            field=core.fields.EncryptedCharField(blank=True, help_text='Denormalised from branch, snapshotted for record safety.', max_length=20),
        ),
    ]
