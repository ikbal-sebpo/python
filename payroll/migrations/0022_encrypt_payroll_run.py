"""Encrypt money at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5.

Register: PayrollRun totals, PayrollRunEmployee amounts + bank snapshot (+
has_pay), PayrollRunLine amounts, PayrollVariance deltas. Every run-derived
table is purged with the runs (TaxComputation and the audit log too — the
latter's detail_json carried net_total).

One-way: the DECIMAL/VARCHAR/JSON columns become TEXT holding Fernet tokens,
and because an ALTER would merely cast the existing values to plaintext
strings, the rows are DELETED first. The purge refuses on any non-empty table
unless PAYROLL_PURGE_ON_MIGRATE=1 is exported for this one `migrate` — there
is no real money in any Orbis database yet (decision 2026-10-05), and the
guard makes sure nobody repeats this against real data later.
"""

import core.fields
from django.db import migrations, models

from core.migration_utils import guarded_purge


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0021_encrypt_salary_structure"),
    ]

    operations = [
        migrations.RunPython(
            guarded_purge(
                "payroll.PayrollRunLine",
                "payroll.PayrollVariance",
                "payroll.TaxComputation",
                "payroll.PayrollRunEmployee",
                "payroll.PayrollControlCheck",
                "payroll.PayrollRunAudit",
                "payroll.PayrollRun",
            ),
            migrations.RunPython.noop,
        ),
        migrations.AddField(
            model_name='payrollrunemployee',
            name='has_pay',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='payrollrun',
            name='gross_total',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name='payrollrun',
            name='net_total',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name='payrollrun',
            name='tax_total',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name='payrollrun',
            name='total_additions',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name='payrollrun',
            name='total_deductions',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='bank_account_number',
            field=core.fields.EncryptedCharField(blank=True, max_length=50),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='gross_earnings',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='net_pay',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='tax_deducted',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='taxable_gross',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='total_additions',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunemployee',
            name='total_deductions',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunline',
            name='amount',
            field=core.fields.EncryptedDecimalField(decimal_places=2, help_text='Always a positive magnitude.', max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunline',
            name='full_amount',
            field=core.fields.EncryptedDecimalField(decimal_places=2, help_text='Un-prorated amount.', max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollrunline',
            name='rate',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AlterField(
            model_name='payrollvariance',
            name='curr_net',
            field=core.fields.EncryptedDecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterField(
            model_name='payrollvariance',
            name='delta',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AlterField(
            model_name='payrollvariance',
            name='delta_pct',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AlterField(
            model_name='payrollvariance',
            name='prev_net',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
