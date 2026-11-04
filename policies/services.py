"""Business logic for the policies app.

Everything that mutates policy/acknowledgement state lives here — views,
signals, forms, and the management command are thin callers. Mirrors the
``leave``/``documents`` convention of keeping the state machine out of views.

Scale note (~1,785 adherents): publish/remind/supersede batch-write rows with
``bulk_create``/``update`` and schedule the email fan-out on
``transaction.on_commit`` so a rolled-back transaction never sends mail. Mail
is sent synchronously (one ``send_mail`` per recipient via ``core.notifications``)
which is acceptable at this scale; the same functions can later be wrapped as a
Celery task with no signature change (see ``docs/PLAN.md`` — Celery deferred).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from accounts.filters import employee_search_q
from accounts.permissions import can_see_all_departments, user_groups
from policies.models import (
    Policy,
    PolicyAcknowledgement,
    PolicyAcknowledgementEvent,
    PolicyCounter,
    PolicyVersion,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# Policy-management power is decoupled from HR/Admins profile-write scope.
# Both HR and NSA (IT) staff who run policy work get added to "Policy Managers".
# Do NOT reuse accounts.can_edit_all here — that grants employee-profile writes.
POLICY_MANAGER_GROUPS = {"User Admins", "Policy Managers"}


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def can_manage_policies(user, request=None) -> bool:
    """True for superusers and members of POLICY_MANAGER_GROUPS.

    Deliberately independent of ``accounts.can_edit_all`` so a Policy Manager
    gets policy power *without* gaining employee-profile write scope.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(user_groups(user, request) & POLICY_MANAGER_GROUPS)


def can_access_policy(user, policy, request=None) -> bool:
    """True if a manager may view/manage this specific policy.

    Privileged roles (``accounts.permissions.can_see_all_departments`` —
    Admins/HR/Senior Leadership) can access every policy; every other Policy
    Manager is locked to their own department's policies, same rule as the
    Department picker on the create/edit form. Callers must separately check
    ``can_manage_policies`` first — this only decides *which* policies, not
    *whether* the user manages policies at all.
    """
    if can_see_all_departments(user, request):
        return True
    return policy.department_id is not None and policy.department_id == getattr(user, "department_id", None)


def visible_policies(user, request=None):
    """Policies ``user`` may see: department-scoped for managers (every
    department if privileged — see ``can_access_policy``), only the ones an
    employee is an adherent of otherwise."""
    qs = Policy.objects.select_related("owner", "department")
    if can_manage_policies(user, request):
        if can_see_all_departments(user, request):
            return qs
        return qs.filter(department_id=getattr(user, "department_id", None))
    return qs.filter(acknowledgements__employee=user).distinct()


def my_acknowledgements(user):
    """An employee's own acknowledgement rows (their 'to sign' list)."""
    return (
        PolicyAcknowledgement.objects
        .filter(employee=user)
        .select_related("policy", "version")
        .order_by("status", "policy__title")
    )


# ---------------------------------------------------------------------------
# URLs (for email bodies — no request object available)
# ---------------------------------------------------------------------------

def absolute_url(path: str) -> str:
    base = getattr(settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}{path}"


def policy_sign_url(policy: Policy) -> str:
    return absolute_url(reverse("policies:sign", args=[policy.id]))


# ---------------------------------------------------------------------------
# Key assignment ("P-1", "P-2", …)
# ---------------------------------------------------------------------------

def next_policy_key() -> str:
    """Atomically bump the singleton counter and return the next key.

    ``select_for_update`` on the 1-row ``PolicyCounter`` serialises concurrent
    creates so keys never race or gap. Must run inside a transaction.
    """
    counter, _created = PolicyCounter.objects.select_for_update().get_or_create(pk=1)
    counter.last_value = (counter.last_value or 0) + 1
    counter.save(update_fields=["last_value"])
    return f"P-{counter.last_value}"


# ---------------------------------------------------------------------------
# Email fan-out helpers
# ---------------------------------------------------------------------------

# Each kind: (subject.txt, plain body.txt fallback, branded body.html).
_EMAIL_KINDS = {
    "assigned": (
        "policies/notifications/assigned_subject.txt",
        "policies/notifications/assigned_body.txt",
        "policies/notifications/assigned_body.html",
    ),
    "reminder": (
        "policies/notifications/reminder_subject.txt",
        "policies/notifications/reminder_body.txt",
        "policies/notifications/reminder_body.html",
    ),
    "new_version": (
        "policies/notifications/new_version_subject.txt",
        "policies/notifications/new_version_body.txt",
        "policies/notifications/new_version_body.html",
    ),
}


# Acks per fan-out task. Small enough that a retried chunk re-renders little,
# large enough that a company-wide publish is a handful of queue messages.
_FANOUT_CHUNK = 100


def _email_acknowledgements(acks, kind: str) -> None:
    """Fan the signing emails out to the task queue in chunks. Called inside
    ``on_commit`` so a rolled-back transaction never enqueues mail.

    The request does O(len(acks)/chunk) work — just enqueues pk lists.
    Rendering and SMTP both happen on the worker
    (policies.tasks.send_acknowledgement_emails), so publishing to
    "All Employees" can't time the request out no matter the headcount."""
    from core.tasks import defer
    from policies.tasks import send_acknowledgement_emails

    pks = [str(ack.pk) for ack in acks]
    for i in range(0, len(pks), _FANOUT_CHUNK):
        defer(send_acknowledgement_emails, pks[i:i + _FANOUT_CHUNK], kind)


# ---------------------------------------------------------------------------
# Adherent targeting + materialisation
# ---------------------------------------------------------------------------

def target_adherent_ids(policy: Policy) -> set:
    """Final adherent set = (active users in target groups) ∪ (custom adherents)."""
    group_ids = set(
        User.objects.filter(
            is_active=True, groups__in=policy.target_groups.all(),
        ).values_list("pk", flat=True)
    )
    custom_ids = set(
        policy.custom_adherents.filter(is_active=True).values_list("pk", flat=True)
    )
    return group_ids | custom_ids


def eligible_custom_adherents(policy: Policy | None):
    """Queryset for the custom-adherent picker: active employees who are
    NOT already adherents/signers of this policy (surfaces only people still
    missing coverage — typically new joiners)."""
    qs = User.objects.filter(is_active=True)
    if policy is not None and policy.pk:
        existing = policy.acknowledgements.values_list("employee_id", flat=True)
        qs = qs.exclude(pk__in=existing)
    return qs.order_by("first_name", "last_name")


def search_eligible_custom_adherents(policy: Policy | None, q: str, limit: int = 25):
    """Search results for the custom-adherent picker's search box.

    With several thousand employees, dumping the full eligible list isn't
    useful — the picker only shows matches once the manager types a name,
    employee ID, or email, capped to ``limit`` rows.
    """
    q = (q or "").strip()
    if not q:
        return []
    qs = eligible_custom_adherents(policy).filter(employee_search_q(q))
    return list(qs[:limit])


@transaction.atomic
def assign_adherents(policy: Policy, *, actor=None, notify: bool = True) -> list:
    """Materialise PENDING ``PolicyAcknowledgement`` rows for every targeted
    adherent who doesn't already have one. Idempotent — re-running only tops up
    coverage for new joiners / newly-added custom adherents.

    Writes ASSIGNED (+ NOTIFIED when ``notify``) events and schedules the
    signing email on commit. Returns the list of newly created acknowledgements.
    """
    target_ids = target_adherent_ids(policy)
    existing_ids = set(policy.acknowledgements.values_list("employee_id", flat=True))
    new_ids = target_ids - existing_ids
    if not new_ids:
        return []

    current = policy.current_version
    now = timezone.now()

    # UUID pks are generated in Python at instantiation, so these objects carry
    # valid pks before bulk_create — safe to build child Event rows from them.
    acks = [
        PolicyAcknowledgement(
            policy=policy, employee_id=uid,
            status=PolicyAcknowledgement.Status.PENDING,
            notified_at=now if notify else None,
            added_by=actor, updated_by=actor,
        )
        for uid in new_ids
    ]
    PolicyAcknowledgement.objects.bulk_create(acks, batch_size=500)

    events = []
    for ack in acks:
        events.append(PolicyAcknowledgementEvent(
            acknowledgement=ack, event=PolicyAcknowledgementEvent.Event.ASSIGNED,
            version=current, actor=actor,
        ))
        if notify:
            events.append(PolicyAcknowledgementEvent(
                acknowledgement=ack, event=PolicyAcknowledgementEvent.Event.NOTIFIED,
                version=current, actor=actor,
            ))
    PolicyAcknowledgementEvent.objects.bulk_create(events, batch_size=500)

    if notify:
        transaction.on_commit(lambda: _email_acknowledgements(acks, "assigned"))
    return acks


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

@transaction.atomic
def add_policy_version(policy: Policy, *, pdf, change_note: str = "",
                       requires_reack: bool = True, actor=None) -> PolicyVersion:
    """Append a new PolicyVersion, make it current, and (when the policy is
    already Active and re-ack is required) supersede prior signatures + notify."""
    last = policy.versions.order_by("-version").first()
    next_v = (last.version + 1) if last else 1

    version = PolicyVersion.objects.create(
        policy=policy, version=next_v, pdf=pdf,
        change_note=change_note, requires_reack=requires_reack,
        is_current=True,
        published_at=timezone.now() if policy.is_circulated else None,
        added_by=actor, updated_by=actor,
    )
    policy.versions.exclude(pk=version.pk).update(is_current=False)

    if next_v > 1 and requires_reack and policy.is_circulated:
        supersede_on_new_version(version, actor=actor)
    return version


@transaction.atomic
def supersede_on_new_version(version: PolicyVersion, *, actor=None) -> list:
    """Flip every SIGNED acknowledgement for this policy to SUPERSEDED, append a
    SUPERSEDED event, and re-send the signing email. No-op for already-pending
    rows (they're still outstanding against the new current version)."""
    policy = version.policy
    signed = list(policy.acknowledgements.filter(
        status=PolicyAcknowledgement.Status.SIGNED,
    ))
    if not signed:
        return []

    now = timezone.now()
    ids = [a.pk for a in signed]
    PolicyAcknowledgement.objects.filter(pk__in=ids).update(
        status=PolicyAcknowledgement.Status.SUPERSEDED,
        notified_at=now, updated_by=actor,
    )
    PolicyAcknowledgementEvent.objects.bulk_create([
        PolicyAcknowledgementEvent(
            acknowledgement=a, event=PolicyAcknowledgementEvent.Event.SUPERSEDED,
            version=version, actor=actor,
        )
        for a in signed
    ], batch_size=500)
    transaction.on_commit(lambda: _email_acknowledgements(signed, "new_version"))
    return signed


# ---------------------------------------------------------------------------
# Activate / circulate
# ---------------------------------------------------------------------------

@transaction.atomic
def activate_policy(policy: Policy, *, actor=None) -> None:
    """Move a Draft policy to Active. Finalizes the policy as "ready" but
    does NOT notify anyone — no acknowledgement rows are created and no
    email goes out. That's a separate, deliberate act (``circulate_policy``).

    Only a Draft policy may be activated — the "Activate" button is already
    hidden in the UI for Active/Retired, but that alone doesn't stop a stale
    tab, browser back/resubmit, or a direct POST from hitting this endpoint,
    so the state check has to live here, not just in the template.
    """
    if policy.status != Policy.Status.DRAFT:
        raise ValidationError(
            f"Only a Draft policy can be activated (this policy is "
            f"{policy.get_status_display()})."
        )
    if policy.current_version is None:
        raise ValidationError("Upload a PDF version before activating.")

    policy.status = Policy.Status.ACTIVE
    policy.updated_by = actor
    policy.save(update_fields=["status", "updated_by", "updated_at"])


@transaction.atomic
def circulate_policy(policy: Policy, *, actor=None) -> list:
    """Stamp ``published_at``, ensure the current version is published, and
    materialise/notify adherents. Returns the newly created acknowledgements.

    Only an Active, not-yet-circulated policy may be circulated — this one
    check rejects Draft, Retired, and "already circulated" alike. Same
    defense-in-depth reasoning as ``activate_policy``: the button is hidden
    in the UI, but the state check has to live here too.
    """
    if not policy.is_active or policy.is_circulated:
        raise ValidationError(
            f"Only an Active, not-yet-circulated policy can be circulated "
            f"(this policy is {policy.get_status_display()}"
            f"{', already circulated' if policy.is_circulated else ''})."
        )
    current = policy.current_version
    if current is None:
        raise ValidationError("Upload a PDF version before circulating.")

    now = timezone.now()
    policy.published_at = now
    policy.updated_by = actor
    policy.save(update_fields=["published_at", "updated_by", "updated_at"])

    if not current.published_at:
        current.published_at = now
        current.save(update_fields=["published_at"])

    return assign_adherents(policy, actor=actor)


@transaction.atomic
def reset_signing_data(policies) -> tuple[int, int]:
    """Wipe acknowledgement/event data for ``policies`` and reset each to
    Draft (no ``published_at``, current version's ``published_at`` cleared).
    Policy content (title, PDFs, target_groups, custom_adherents) untouched.

    Used by ``manage.py reset_policy_signing_data`` — see that command's
    docstring for why this exists. Returns ``(acks_deleted, events_deleted)``.
    """
    events = PolicyAcknowledgementEvent.objects.filter(acknowledgement__policy__in=policies)
    acks = PolicyAcknowledgement.objects.filter(policy__in=policies)
    event_count = events.count()
    ack_count = acks.count()

    events.delete()
    acks.delete()
    PolicyVersion.objects.filter(policy__in=policies).update(published_at=None)
    policies.update(status=Policy.Status.DRAFT, published_at=None)

    return ack_count, event_count


@transaction.atomic
def retire_policy(policy: Policy, *, actor=None) -> None:
    policy.status = Policy.Status.RETIRED
    policy.updated_by = actor
    policy.save(update_fields=["status", "updated_by", "updated_at"])


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

@transaction.atomic
def sign(acknowledgement: PolicyAcknowledgement, *, version: PolicyVersion,
         ip=None, user_agent: str = "", actor=None) -> PolicyAcknowledgement:
    """Record a signature. Idempotent: a re-POST on an already-SIGNED row for
    the same current version is a no-op (guards double-sign)."""
    ack = acknowledgement
    if ack.is_signed and ack.version_id == version.pk:
        return ack

    now = timezone.now()
    ack.status = PolicyAcknowledgement.Status.SIGNED
    ack.version = version
    ack.signed_at = now
    ack.signed_ip = ip
    ack.signed_user_agent = (user_agent or "")[:400]
    ack.updated_by = actor
    ack.save(update_fields=[
        "status", "version", "signed_at", "signed_ip",
        "signed_user_agent", "updated_by", "updated_at",
    ])
    PolicyAcknowledgementEvent.objects.create(
        acknowledgement=ack, event=PolicyAcknowledgementEvent.Event.SIGNED,
        version=version, actor=actor, ip=ip,
    )
    return ack


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

@transaction.atomic
def remind_outstanding(policy: Policy, *, actor=None) -> list:
    """Manual 'Send reminders' action: email every outstanding adherent of one
    policy, bump ``reminder_count``, set ``notified_at``, append REMINDED
    events. Returns the reminded acknowledgements."""
    outstanding = list(policy.acknowledgements.exclude(
        status=PolicyAcknowledgement.Status.SIGNED,
    ))
    return _remind(outstanding, actor=actor)


def _remind(acks: list, *, actor=None) -> list:
    if not acks:
        return []
    now = timezone.now()
    ids = [a.pk for a in acks]
    PolicyAcknowledgement.objects.filter(pk__in=ids).update(
        reminder_count=F("reminder_count") + 1, notified_at=now,
    )
    PolicyAcknowledgementEvent.objects.bulk_create([
        PolicyAcknowledgementEvent(
            acknowledgement=a, event=PolicyAcknowledgementEvent.Event.REMINDED,
            version=a.policy.current_version, actor=actor,
        )
        for a in acks
    ], batch_size=500)
    transaction.on_commit(lambda: _email_acknowledgements(acks, "reminder"))
    return acks


def send_scheduled_reminders(*, interval_days: int = 3, actor=None) -> int:
    """Scheduled nightly job: remind outstanding adherents across all Active
    policies whose last notification is older than ``interval_days`` (or who
    were never notified). Returns the number of acknowledgements reminded.

    Each policy is reminded in its own transaction so one bad policy can't roll
    back the whole run.
    """
    cutoff = timezone.now() - timezone.timedelta(days=interval_days)
    total = 0
    active_policies = Policy.objects.filter(status=Policy.Status.ACTIVE)
    for policy in active_policies.iterator():
        with transaction.atomic():
            due = list(
                policy.acknowledgements
                .exclude(status=PolicyAcknowledgement.Status.SIGNED)
                .filter(models.Q(notified_at__isnull=True) | models.Q(notified_at__lt=cutoff))
            )
            reminded = _remind(due, actor=actor)
            total += len(reminded)
    return total
