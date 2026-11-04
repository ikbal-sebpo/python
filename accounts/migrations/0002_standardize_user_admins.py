"""Retire the empty legacy "Admins" group; standardize on "User Admins".

The whole permission layer historically checked a group literally named
"Admins", but the real admins live in "User Admins". The "Admins" group had
zero members and only existed because ``seed_orbis`` created it. This migration
re-points every Group M2M reference (dynamic-menu ACLs + protected views) from
"Admins" to "User Admins", moves any stray memberships, then deletes "Admins".
"""

from __future__ import annotations

from django.db import migrations


OLD_NAME = "Admins"
NEW_NAME = "User Admins"

# Every M2M from a model to auth.Group that may reference the old group.
# (app_label, model_name, m2m_field_name)
GROUP_M2M_FIELDS = [
    ("dynamic_menu", "MainMenu", "groups"),
    ("dynamic_menu", "SubMenu", "groups"),
    ("dynamic_menu", "ProtectedView", "allowed_groups"),
]


def forwards(apps, schema_editor):
    Group = apps.get_model("auth", "Group")

    try:
        old = Group.objects.get(name=OLD_NAME)
    except Group.DoesNotExist:
        return  # Nothing to reconcile.

    new, _ = Group.objects.get_or_create(name=NEW_NAME)
    if new.pk == old.pk:
        return

    # 1. Re-point every M2M link that pointed at the old group.
    for app_label, model_name, field_name in GROUP_M2M_FIELDS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        for obj in model.objects.filter(**{f"{field_name}__pk": old.pk}):
            manager = getattr(obj, field_name)
            manager.add(new)
            manager.remove(old)

    # 2. Move any user memberships (0 today, but be safe).
    for user in old.user_set.all():
        user.groups.add(new)
        user.groups.remove(old)

    # 3. Drop the now-orphaned legacy group.
    old.delete()


def backwards(apps, schema_editor):
    # One-way reconciliation; recreating an empty "Admins" group is a no-op.
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name=OLD_NAME)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("dynamic_menu", "0001_initial"),
        ("policies", "0002_seed_group_and_nav"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
