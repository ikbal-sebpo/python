# Backfill CPL treatment for the departments/teams classified in this
# session (see docs/LEAVE_POLICY_IMPLEMENTATION.md §5.12): Digital
# Advertising, Creative Development, Software Development and General BPO
# never use CPL (overtime only); Data Solutions & Due Diligence decides
# CPL vs overtime per-employee outside Orbis (manual upload only); the
# "Reporting" team under Corporate is overtime-only even though the rest
# of Corporate auto-grants. Everything else keeps the field's own AUTO
# default — no row needed here.
from django.db import migrations

NONE_DEPARTMENTS = [
    "Digital Advertising",
    "Creative Development",
    "Software Development",
    "General BPO",
]
MANUAL_DEPARTMENTS = [
    "Data solution and Due Diligence",
]
NONE_TEAMS = [
    # (team name, parent department name) — disambiguates from any other
    # team that might reuse the "Reporting" name elsewhere.
    ("Reporting", "Corporate"),
]


def backfill(apps, schema_editor):
    Department = apps.get_model("accounts", "Department")
    Team = apps.get_model("accounts", "Team")

    Department.objects.filter(name__in=NONE_DEPARTMENTS).update(cpl_treatment="NONE")
    Department.objects.filter(name__in=MANUAL_DEPARTMENTS).update(cpl_treatment="MANUAL")
    for team_name, dept_name in NONE_TEAMS:
        Team.objects.filter(
            name=team_name, department__name=dept_name,
        ).update(cpl_treatment_override="NONE")


def unbackfill(apps, schema_editor):
    Department = apps.get_model("accounts", "Department")
    Team = apps.get_model("accounts", "Team")

    Department.objects.filter(
        name__in=NONE_DEPARTMENTS + MANUAL_DEPARTMENTS,
    ).update(cpl_treatment="AUTO")
    for team_name, dept_name in NONE_TEAMS:
        Team.objects.filter(
            name=team_name, department__name=dept_name,
        ).update(cpl_treatment_override="")


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_cpl_treatment'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
