"""Signal receivers for the roster app.

Hooked up via `RosterConfig.ready()`.
"""

from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from roster.models import Shift, ShiftChangeLog
from roster.services import ensure_default_pattern


@receiver(user_logged_in)
def ensure_default_pattern_on_login(sender, user, request, **kwargs):
    """Every employee should have a default weekly pattern.

    On first login (or any login where they still have no pattern), create:
        Mon–Fri → default working shift (Morning if present)
        Sat, Sun → Weekly Off
    """
    ensure_default_pattern(user)


# ---------------------------------------------------------------------------
# Shift change audit log
#
# Every write path (roster grid CellUpdateView, bulk upload, leave
# materialisation, the external write API) already sets added_by/updated_by
# on Shift, so these three receivers capture all of them with no changes to
# any of those call sites — mirrors dsndd's own ShiftChangeLog mechanism
# (accounts/signals.py in that project), widened to also cover location
# changes, which dsndd's version misses.
# ---------------------------------------------------------------------------

_WATCHED_FIELDS = ("shift_type_id", "shift_attribute_id", "job_location_id", "work_location_id", "note")


@receiver(pre_save, sender=Shift)
def _cache_previous_shift_state(sender, instance, **kwargs):
    if instance.pk:
        instance._previous_state = Shift.objects.filter(pk=instance.pk).select_related(
            "shift_type", "shift_attribute", "job_location", "work_location",
        ).first()
    else:
        instance._previous_state = None


@receiver(post_save, sender=Shift)
def _log_shift_create_update(sender, instance, created, **kwargs):
    changed_by = instance.updated_by or instance.added_by

    if created:
        ShiftChangeLog.objects.create(
            shift=instance, employee=instance.employee, date=instance.date,
            action_type=ShiftChangeLog.ActionType.CREATED,
            new_shift_type=instance.shift_type, new_shift_attribute=instance.shift_attribute,
            new_job_location=instance.job_location, new_work_location=instance.work_location,
            new_note=instance.note,
            changed_by=changed_by,
        )
        return

    previous = getattr(instance, "_previous_state", None)
    if previous is None:
        # Cache miss (e.g. row created and saved again within the same
        # pre_save/post_save cycle) — nothing to diff against.
        return
    if all(getattr(previous, f) == getattr(instance, f) for f in _WATCHED_FIELDS):
        return

    ShiftChangeLog.objects.create(
        shift=instance, employee=instance.employee, date=instance.date,
        action_type=ShiftChangeLog.ActionType.UPDATED,
        previous_shift_type=previous.shift_type, new_shift_type=instance.shift_type,
        previous_shift_attribute=previous.shift_attribute, new_shift_attribute=instance.shift_attribute,
        previous_job_location=previous.job_location, new_job_location=instance.job_location,
        previous_work_location=previous.work_location, new_work_location=instance.work_location,
        previous_note=previous.note, new_note=instance.note,
        changed_by=changed_by,
    )


@receiver(pre_delete, sender=Shift)
def _log_shift_delete(sender, instance, **kwargs):
    changed_by = instance.updated_by or instance.added_by
    ShiftChangeLog.objects.create(
        shift=None, employee=instance.employee, date=instance.date,
        action_type=ShiftChangeLog.ActionType.DELETED,
        previous_shift_type=instance.shift_type, previous_shift_attribute=instance.shift_attribute,
        previous_job_location=instance.job_location, previous_work_location=instance.work_location,
        previous_note=instance.note,
        changed_by=changed_by,
    )
