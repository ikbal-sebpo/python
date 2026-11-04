"""Tax declaration becomes a once-a-year commitment — revised with Finance
2026-10-07 (docs/PAYROLL_PLAN.md, same-dated section; reverses PAYROLL_PROCESS
question 41).

* The six itemised ``investment_*`` columns go (every production row held 0),
  replaced by the ``allowable_investment`` snapshot + the employee's
  ``actual_investment`` commitment.
* ``other_tds_adjustment`` is RENAMED (not dropped) to ``ait_car_advance_tax``
  — Finance's "AIT (Car & Adv. Tax)" line — with ``ait_document`` beside it;
  ``TaxComputation.other_adjustment`` follows. The column already holds
  Fernet tokens, so a rename carries any value across unchanged and no purge
  guard is needed.
* ``submitted_at`` / ``submitted_by`` / ``submission_source`` record the
  confirmation that locks the row; ``TaxYear`` gains the 30%
  ``allowable_investment_pct`` and an optional ``declaration_deadline``.

``submitted_by`` is a new FK to CustomUser on an EXISTING table (created with
the collation CONVERT step in 0002), so the column inherits the table's
utf8mb4_0900_ai_ci and needs no CONVERT of its own — ``added_by`` on the same
table is the precedent.
"""

import core.fields
import django.db.models.deletion
import payroll.models
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0025_encrypt_bank_account'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # -- EmployeeTaxDeclaration -------------------------------------------
        migrations.RemoveField(model_name='employeetaxdeclaration', name='investment_dps'),
        migrations.RemoveField(model_name='employeetaxdeclaration', name='investment_life_insurance'),
        migrations.RemoveField(model_name='employeetaxdeclaration', name='investment_others'),
        migrations.RemoveField(model_name='employeetaxdeclaration', name='investment_retirement_fund'),
        migrations.RemoveField(model_name='employeetaxdeclaration', name='investment_savings_certificate'),
        migrations.RemoveField(model_name='employeetaxdeclaration', name='investment_share_market'),
        migrations.RenameField(
            model_name='employeetaxdeclaration',
            old_name='other_tds_adjustment',
            new_name='ait_car_advance_tax',
        ),
        migrations.AlterField(
            model_name='employeetaxdeclaration',
            name='ait_car_advance_tax',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, default=0, help_text='Advance income tax already paid this year (e.g. on a car). Reduces the TDS Orbis deducts; needs the payment document.', max_digits=12, verbose_name='AIT (Car & Adv. Tax)'),
        ),
        migrations.AddField(
            model_name='employeetaxdeclaration',
            name='allowable_investment',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, help_text='Snapshot of allowable_investment_pct × projected taxable income, taken when the row was issued. Blank only when no salary structure existed at that point.', max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='employeetaxdeclaration',
            name='actual_investment',
            field=core.fields.EncryptedDecimalField(blank=True, decimal_places=2, help_text='What the employee commits to invest this tax year. Defaults to the allowable figure; may be lowered, never raised above it.', max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='employeetaxdeclaration',
            name='ait_document',
            field=models.FileField(blank=True, help_text='Evidence of the advance tax payment (PDF or image). Required when AIT is above zero.', null=True, upload_to=payroll.models.tax_declaration_document_path, verbose_name='AIT document'),
        ),
        migrations.AddField(
            model_name='employeetaxdeclaration',
            name='submitted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='employeetaxdeclaration',
            name='submitted_by',
            field=models.ForeignKey(blank=True, help_text='The employee, or the Payroll-HR person who confirmed on their behalf.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='employeetaxdeclaration',
            name='submission_source',
            field=models.CharField(blank=True, choices=[('IN_ORBIS', 'Confirmed in Orbis'), ('OUTSIDE_ORBIS', 'Agreed outside Orbis')], default='', max_length=20),
        ),
        # -- TaxComputation ------------------------------------------------------
        migrations.RenameField(
            model_name='taxcomputation',
            old_name='other_adjustment',
            new_name='ait_car_advance_tax',
        ),
        # -- TaxYear ---------------------------------------------------------------
        migrations.AddField(
            model_name='taxyear',
            name='allowable_investment_pct',
            field=models.DecimalField(decimal_places=4, default=Decimal('0.3000'), help_text="Fraction of taxable income an employee may count as rebate-eligible investment (Finance's 'Allowable Investment' line — 0.3000 = 30%).", max_digits=6),
        ),
        migrations.AddField(
            model_name='taxyear',
            name='declaration_deadline',
            field=models.DateField(blank=True, help_text="After this date every unconfirmed declaration for the year locks on its defaults, so the rebate basis can't move once payroll is running. Blank = no cut-off.", null=True),
        ),
    ]
