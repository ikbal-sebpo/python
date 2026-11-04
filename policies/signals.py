"""Signal handlers for the policies app.

Scope is deliberately small. The email fan-out for publish/remind/supersede is
driven from ``policies.services`` on ``transaction.on_commit`` rather than from
per-row ``post_save`` handlers — at ~1,785 adherents those paths use
``bulk_create`` (which does NOT emit ``post_save``) and send mail in chunks, so
a signal-per-row design would both miss the bulk rows and fan out unmanageably.

What lives here: assigning the human-readable ``P-n`` key on Policy create, so
every creation path (htmx drawer, admin, data migration, shell) gets a key
without each caller remembering to call the service.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import pre_save
from django.dispatch import receiver

from policies.models import Policy
from policies.services import next_policy_key


@receiver(pre_save, sender=Policy, dispatch_uid="policies.assign_key")
def assign_policy_key(sender, instance: Policy, **kwargs):
    """Assign ``key`` once, on first save. ``next_policy_key`` takes a
    ``select_for_update`` lock on the counter, so it must run inside a
    transaction — wrap defensively in case the caller isn't already atomic."""
    if instance.key:
        return
    with transaction.atomic():
        instance.key = next_policy_key()
