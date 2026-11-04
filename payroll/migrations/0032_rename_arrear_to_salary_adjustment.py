"""ARREAR → SALARY_ADJUSTMENT (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md R2.1,
2026-10-09). The seed's update_or_create keys on `code`, so an
already-seeded ARREAR row has to be re-keyed here — and the code is also a
*value* inside LegacyPayrollMonth.lines (encrypted JSON, so rewritten row
by row in Python) and PayrollRunLine.component_code.
"""

from django.db import migrations

OLD, NEW = "ARREAR", "SALARY_ADJUSTMENT"


def forwards(apps, schema_editor):
    PayComponent = apps.get_model("payroll", "PayComponent")
    LegacyPayrollMonth = apps.get_model("payroll", "LegacyPayrollMonth")
    PayrollRunLine = apps.get_model("payroll", "PayrollRunLine")
    if PayComponent.objects.filter(code=OLD).exists() and not PayComponent.objects.filter(code=NEW).exists():
        PayComponent.objects.filter(code=OLD).update(code=NEW, name="Salary adjustment", short_name="Adjustment")
    for lm in LegacyPayrollMonth.objects.all():
        lines = lm.lines or {}
        if OLD in lines:
            lines[NEW] = lines.pop(OLD)
            lm.lines = lines
            lm.save(update_fields=["lines"])
    PayrollRunLine.objects.filter(component_code=OLD).update(component_code=NEW)


def backwards(apps, schema_editor):
    PayComponent = apps.get_model("payroll", "PayComponent")
    LegacyPayrollMonth = apps.get_model("payroll", "LegacyPayrollMonth")
    PayrollRunLine = apps.get_model("payroll", "PayrollRunLine")
    PayComponent.objects.filter(code=NEW).update(code=OLD, name="Salary arrear", short_name="Arrear")
    for lm in LegacyPayrollMonth.objects.all():
        lines = lm.lines or {}
        if NEW in lines:
            lines[OLD] = lines.pop(NEW)
            lm.lines = lines
            lm.save(update_fields=["lines"])
    PayrollRunLine.objects.filter(component_code=NEW).update(component_code=OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0031_challan_allocations"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
