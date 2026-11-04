"""Backfill Shift.leave_application from the legacy note fingerprint.

Before the FK existed, leave materialisation tagged its Shift rows with
note="Leave #<pk>" and located them by scanning that string. Parse the tag
back into the FK so the services can filter on the indexed column.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Shift = apps.get_model("roster", "Shift")
    LeaveApplication = apps.get_model("leave", "LeaveApplication")

    existing_pks = set(LeaveApplication.objects.values_list("pk", flat=True))
    to_update = []
    tagged = Shift.objects.filter(
        note__startswith="Leave #", leave_application__isnull=True,
    ).only("pk", "note")
    for shift in tagged.iterator():
        try:
            app_pk = int(shift.note.removeprefix("Leave #").strip())
        except ValueError:
            continue  # note happens to start with the prefix but isn't a tag
        if app_pk in existing_pks:
            shift.leave_application_id = app_pk
            to_update.append(shift)
    Shift.objects.bulk_update(to_update, ["leave_application"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("roster", "0004_shift_leave_application"),
        ("leave", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
