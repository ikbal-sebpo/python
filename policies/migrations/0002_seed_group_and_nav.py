"""Seed the 'Policy Managers' group and the Policies navigation rows.

Idempotent: uses get_or_create keyed on stable names/route-names, so re-running
(or running after the rows were hand-created) touches nothing.

Navigation shape (per docs/POLICY_MODULE_PLAN.md):
  · MainMenu "Policies" (Lucide ``file-check``) — a section (blank url) visible
    to Admins / Policy Managers / All Employees.
  · SubMenu "All Policies" → ``policies:list``     scoped to Admins + Policy Managers.
  · SubMenu "To Sign"      → ``policies:my_policies`` scoped to All Employees.
"""

from django.db import migrations


def seed(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    MainMenu = apps.get_model("dynamic_menu", "MainMenu")
    SubMenu = apps.get_model("dynamic_menu", "SubMenu")

    admins, _ = Group.objects.get_or_create(name="Admins")
    all_emp, _ = Group.objects.get_or_create(name="All Employees")
    managers, _ = Group.objects.get_or_create(name="Policy Managers")

    main, _ = MainMenu.objects.get_or_create(
        title="Policies",
        defaults={"icon_class": "file-check", "url": "", "rank": 70, "is_active": True},
    )
    main.groups.add(admins, managers, all_emp)

    all_policies, _ = SubMenu.objects.get_or_create(
        main_menu=main, url="policies:list",
        defaults={"title": "All Policies", "rank": 10, "is_active": True},
    )
    all_policies.groups.add(admins, managers)

    to_sign, _ = SubMenu.objects.get_or_create(
        main_menu=main, url="policies:my_policies",
        defaults={"title": "To Sign", "rank": 20, "is_active": True},
    )
    to_sign.groups.add(all_emp)


def unseed(apps, schema_editor):
    MainMenu = apps.get_model("dynamic_menu", "MainMenu")
    SubMenu = apps.get_model("dynamic_menu", "SubMenu")
    SubMenu.objects.filter(url__in=["policies:list", "policies:my_policies"]).delete()
    MainMenu.objects.filter(title="Policies").delete()
    # Leave the 'Policy Managers' group in place — it may have members.


class Migration(migrations.Migration):

    dependencies = [
        ("policies", "0001_initial"),
        ("dynamic_menu", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
