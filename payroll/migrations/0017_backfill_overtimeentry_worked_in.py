"""Backfill OvertimeEntry.worked_in from the employee's current team —
2026-09-16, docs/PAYROLL_PLAN.md.

Deliberately scoped: only entries whose period has NO run that has reached
CALCULATING/CALCULATED are touched. An entry under an already-calculated run
is left NULL, so a future recalculation of that run can't silently start
applying a team-scoped OT rate nobody explicitly asked to re-touch — see the
"Backfill scope" decision recorded in docs/PAYROLL_PLAN.md.
"""

from django.db import migrations

_SETTLED_STATUSES = ("CALCULATING", "CALCULATED")


def backfill_worked_in(apps, schema_editor):
    OvertimeEntry = apps.get_model("payroll", "OvertimeEntry")
    PayrollRun = apps.get_model("payroll", "PayrollRun")

    settled_period_ids = set(
        PayrollRun.objects.filter(status__in=_SETTLED_STATUSES).values_list("period_id", flat=True)
    )
    entries = list(
        OvertimeEntry.objects
        .filter(worked_in__isnull=True)
        .exclude(period_id__in=settled_period_ids)
        .exclude(employee__team__isnull=True)
        .select_related("employee")
    )
    for entry in entries:
        entry.worked_in_id = entry.employee.team_id
    OvertimeEntry.objects.bulk_update(entries, ["worked_in"], batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0016_overtimeentry_worked_in"),
    ]

    operations = [
        migrations.RunPython(backfill_worked_in, noop),
    ]
