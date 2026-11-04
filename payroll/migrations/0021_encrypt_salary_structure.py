"""Encrypt money at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5.

Compensation master: SalaryStructure.gross_monthly +
TeamSalaryPackage.gross_monthly.

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
        ("payroll", "0020_variablepayentry_days_alter_payrate_rate_type"),
    ]

    operations = [
        migrations.RunPython(
            guarded_purge(
                "payroll.SalaryStructure",
                "payroll.TeamSalaryPackage",
            ),
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='salarystructure',
            name='gross_monthly',
            field=core.fields.EncryptedDecimalField(decimal_places=2, max_digits=12),
        ),
        migrations.AlterField(
            model_name='teamsalarypackage',
            name='gross_monthly',
            field=core.fields.EncryptedDecimalField(decimal_places=2, max_digits=12),
        ),
    ]
