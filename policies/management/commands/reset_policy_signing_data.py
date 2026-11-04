"""Wipe a policy's circulation/sign-off state back to a clean,
pre-circulation baseline (back to Draft, no acknowledgements, no event
ledger, no published_at).

**The policies module is LIVE on production (since 2026-10-27).** Real
policies carry genuine employee signatures, so ``--all --apply`` there would
destroy them. Always scope with ``--policy KEY`` and dry-run first. The
per-policy "Reset signing data" button on a Retired policy's detail page
(``policies.views.PolicyResetView``, shipped 2026-10-28) is the normal path;
this command is the hand-run fallback for cleaning several at once.

History: written 2026-09-18, when the module was still in its pre-launch
testing period and a Retired policy had been accidentally circulated to all
1,482 employees (the fix lives in ``policies.services.activate_policy`` /
``circulate_policy``). Used again on 2026-10-28 to clear three mistaken mass
circulations (P-9, P-12, P-13), each named explicitly.

Deletes ``PolicyAcknowledgementEvent`` + ``PolicyAcknowledgement`` rows and
clears ``PolicyVersion.published_at`` / ``Policy.published_at``, resetting
``Policy.status`` to Draft. Does NOT touch policy content — title, PDFs,
``target_groups``/``custom_adherents``, or the version rows themselves are
left alone, only their circulation/sign state.

This is a one-off, hand-run command, not a scheduled job — do NOT give it a
PeriodicTask row (see docs/SCHEDULER_ADMIN_PLAN.md).

Dry-run by default (omit --apply to preview); refuses to run with no scope.
Always preview first::

    python manage.py reset_policy_signing_data --all
    python manage.py reset_policy_signing_data --all --apply
    python manage.py reset_policy_signing_data --policy P-5 --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from policies.models import Policy, PolicyAcknowledgementEvent
from policies.services import reset_signing_data


class Command(BaseCommand):
    help = (
        "Wipe all policy acknowledgement/event data and reset affected "
        "policies to Draft. Dry-run by default; scope is required."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--policy", action="append", default=[], dest="policy_keys",
            help="Policy key (e.g. P-5) to reset. Repeatable.",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Reset every policy, not just --policy.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete/reset (default: dry-run, prints what would happen).",
        )

    def handle(self, *args, **opts):
        policy_keys = opts["policy_keys"]
        sweep_all = opts["all"]
        apply_changes = opts["apply"]

        if not policy_keys and not sweep_all:
            raise CommandError(
                "Refusing to run with no scope — pass one or more --policy "
                "KEY, or --all to reset every policy."
            )

        policies = Policy.objects.all().order_by("key")
        if policy_keys:
            policies = policies.filter(key__in=policy_keys)
            found = set(policies.values_list("key", flat=True))
            missing = set(policy_keys) - found
            if missing:
                raise CommandError(f"No policy with key in {sorted(missing)}.")

        if not policies.exists():
            self.stdout.write("No policies in scope.")
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — no writes. Pass --apply to actually reset."
            ))

        total_acks = 0
        total_events = 0
        for policy in policies:
            ack_count = policy.acknowledgements.count()
            event_count = PolicyAcknowledgementEvent.objects.filter(
                acknowledgement__policy=policy,
            ).count()
            total_acks += ack_count
            total_events += event_count
            self.stdout.write(
                f"  · {policy.key} · {policy.title} — status={policy.status}, "
                f"published_at={policy.published_at or '—'}, "
                f"{ack_count} acknowledgement(s), {event_count} event(s)"
            )

        verb = "about to reset" if apply_changes else "would reset"
        self.stdout.write(self.style.WARNING(
            f"\n{policies.count()} polic(y/ies) in scope — {verb}: "
            f"delete {total_acks} acknowledgement(s), {total_events} event(s), "
            f"clear published_at, set status=Draft."
        ))

        if not apply_changes:
            return

        count = policies.count()
        deleted_acks, deleted_events = reset_signing_data(policies)

        self.stdout.write(self.style.SUCCESS(
            f"Reset {count} polic(y/ies) to Draft: deleted "
            f"{deleted_acks} acknowledgement(s), {deleted_events} event(s)."
        ))
