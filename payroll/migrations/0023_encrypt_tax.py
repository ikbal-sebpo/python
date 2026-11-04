"""Encrypt money at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5.

Tax: TaxComputation (already emptied with the runs by 0022) +
EmployeeTaxDeclaration.

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
        ("payroll", "0022_encrypt_payroll_run"),
    ]

    operations = [
        migrations.RunPython(
            guarded_purge(
                "payroll.TaxComputation",
                "payroll.EmployeeTaxDeclaration",
            ),
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='investment_dps',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='investment_life_insurance',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='investment_others',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='investment_retirement_fund',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='investment_savings_certificate',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='investment_share_market',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='other_tds_adjustment',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, help_text="Matches the template's 'TDS Car & Adv. Tax' line.", max_digits=12),
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='source_tax',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, help_text='Tax already deducted elsewhere, e.g. bank interest.', max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='annual_tds',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='breakdown_json',
            field=core.fields.EncryptedJSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='exempted_income',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='gross_annual_projected',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='monthly_tds',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='net_tax_payable',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='other_adjustment',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='rebate_amount',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='source_tax',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='tax_before_rebate',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='taxcomputation',
            name='taxable_income',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
