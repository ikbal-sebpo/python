"""Signal receivers for the accounts app.

Auto-membership source of truth for `All Employees`:

- Newly-created users are always added to `All Employees`.

Hooked up via `AccountsConfig.ready()`.
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.db.models.signals import post_save
from django.dispatch import receiver


ALL_EMPLOYEES_GROUP = "All Employees"


# --- All Employees ---------------------------------------------------------

@receiver(post_save, sender="accounts.CustomUser")
def add_to_all_employees_group(sender, instance, created, **kwargs):
    """Every new user lands in the `All Employees` group.

    Used by dynamic_menu to show company-wide entries (Leave, Dashboard, …)
    without per-user permission management.
    """
    if not created:
        return
    try:
        group = Group.objects.get(name=ALL_EMPLOYEES_GROUP)
    except Group.DoesNotExist:
        return
    instance.groups.add(group)
