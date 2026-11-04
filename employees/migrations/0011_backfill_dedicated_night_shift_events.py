from datetime import timedelta

from django.db import migrations

# Marker on every row this migration writes, so a reader can tell a
# reconstructed event from one the app actually observed.
BACKFILL_NOTE = "Backfilled from the record's own audit fields"


def backfill(apps, schema_editor):
    """Seed the ledger from what each existing row already stores.

    Deliberately conservative: only facts the row itself carries are
    written, never a guess. Three derivable events per assignment —

      1. The row's creation (added_by/added_at). A null added_by means the
         2026-09-10 django-import-export bulk load, which never populated
         it, so that becomes IMPORTED rather than REQUESTED-by-nobody.
      2. Its approval, when approved_at is set (approved_by/approved_at).
      3. A later edit, when updated_at is meaningfully after added_at and
         isn't just the approve/decline write above. We know *someone*
         touched it then; we don't know what changed, because nothing
         recorded that before this ledger existed. The event says exactly
         that and leaves old_*/new_* null rather than inventing a window.

    Nothing is inferred about rows whose updated_at matches their
    added_at — those were written once and never edited.
    """
    Assignment = apps.get_model("employees", "DedicatedNightShiftAssignment")
    Event = apps.get_model("employees", "DedicatedNightShiftAssignmentEvent")

    if Event.objects.exists():
        return

    rows = []
    for a in Assignment.objects.all().iterator():
        rows.append(Event(
            assignment_id=a.pk,
            event="IMPORTED" if a.added_by_id is None else "REQUESTED",
            occurred_at=a.added_at,
            actor_id=a.added_by_id,
            new_start_date=a.start_date,
            new_end_date=a.end_date,
            note=(
                f"{BACKFILL_NOTE}. Created before the audit ledger existed; "
                "the original requester wasn't captured by the bulk import."
                if a.added_by_id is None else BACKFILL_NOTE
            ),
        ))

        if a.approved_at:
            rows.append(Event(
                assignment_id=a.pk,
                event="APPROVED",
                occurred_at=a.approved_at,
                actor_id=a.approved_by_id,
                new_start_date=a.start_date,
                new_end_date=a.end_date,
                note=BACKFILL_NOTE,
            ))

        # A separate later write we can prove happened, but can't describe.
        edited_later = (
            a.updated_at
            and a.updated_by_id
            and a.updated_at - a.added_at > timedelta(seconds=1)
            and not (a.approved_at and abs((a.updated_at - a.approved_at).total_seconds()) <= 2)
        )
        if edited_later:
            rows.append(Event(
                assignment_id=a.pk,
                event="DECLINED" if a.status == "DECLINED" else "EDITED",
                occurred_at=a.updated_at,
                actor_id=a.updated_by_id,
                new_start_date=a.start_date,
                new_end_date=a.end_date,
                note=(
                    f"{BACKFILL_NOTE}. The record was updated at this time; "
                    "what changed wasn't recorded before the ledger existed."
                ),
            ))

    Event.objects.bulk_create(rows, batch_size=500)


def unbackfill(apps, schema_editor):
    Event = apps.get_model("employees", "DedicatedNightShiftAssignmentEvent")
    Event.objects.filter(note__startswith=BACKFILL_NOTE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0010_dedicatednightshiftassignmentevent"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
