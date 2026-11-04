"""Policies app — PDF policy distribution + employee acknowledgement.

First in-house GRC slice for Orbis (replacing the policy/sign-off part of the
commercial TeamMate / StandardFusion tenant). See ``docs/POLICY_MODULE_PLAN.md``.

The shape mirrors patterns already proven elsewhere in Orbis:

* ``_Auditable`` matches ``documents``/``offboarding``/``leave`` (added_by/at,
  updated_by/at).
* ``PolicyAcknowledgement`` is a denormalised "current sign state" row (fast
  dashboard reads), backed by an append-only ``PolicyAcknowledgementEvent``
  ledger (immutable compliance trail) — exactly the split ``leave`` uses
  between ``LeaveBalance`` and ``LeaveBalanceTransaction``.
* PDFs are stored under ``media/policies/<policy>/v<n>.pdf`` via an
  ``upload_to`` callable, mirroring ``documents``.

All business logic (key assignment, adherent materialisation, publish, sign,
supersede, remind) lives in ``policies/services.py`` — not here.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# Local abstract (mirrors documents/models.py::_Auditable)
# ---------------------------------------------------------------------------

class _Auditable(models.Model):
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Key counter (singleton, pk=1) — mirrors documents' CompanyBranding singleton
# ---------------------------------------------------------------------------

class PolicyCounter(models.Model):
    """One-row counter backing the ``P-1``…``P-n`` key sequence.

    No global sequence app exists in Orbis, so we keep a tiny per-model counter
    and bump it under ``select_for_update`` inside a transaction (see
    ``policies.services.next_policy_key``) to avoid races and gaps.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("Policy counter")
        verbose_name_plural = _("Policy counter")

    def save(self, *args, **kwargs):
        self.id = 1  # enforce singleton
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"PolicyCounter(last_value={self.last_value})"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class Policy(_Auditable):
    """A policy document employees must read and acknowledge.

    The actual content lives in the attached PDF (``PolicyVersion``). This row
    holds metadata, lifecycle status, and the adherent targeting rules.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        ACTIVE = "ACTIVE", _("Active")
        RETIRED = "RETIRED", _("Retired")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(
        max_length=20, unique=True, editable=False,
        help_text=_("Auto-assigned, e.g. 'P-1', 'P-2'."),
    )
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=120, blank=True)
    description = models.TextField(
        blank=True,
        help_text=_("Short plain-text summary. The real content is the PDF."),
    )
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.PROTECT,
        related_name="policies",
        help_text=_("Which department this policy is issued from."),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="owned_policies",
        help_text=_("Accountable policy owner."),
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT,
    )
    target_groups = models.ManyToManyField(
        Group, blank=True, related_name="target_policies",
        help_text=_("Active members of these groups must sign. Defaults to "
                    "'All Employees' (every active employee)."),
    )
    custom_adherents = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="custom_policies",
        help_text=_("Individuals added on top of the target groups."),
    )
    due_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=_("Acknowledge-by window (days) after publish/assignment."),
    )
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-added_at"]
        verbose_name = _("Policy")
        verbose_name_plural = _("Policies")
        indexes = [
            models.Index(fields=["status"], name="policy_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.key} · {self.title}"

    # ----- convenience accessors used by templates/views -----

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def is_retired(self) -> bool:
        return self.status == self.Status.RETIRED

    @property
    def is_circulated(self) -> bool:
        """True once employees have actually been notified to sign.

        Distinguishes "Active, finalized but not yet sent out" from "Active
        and adherents are signing" without a separate status value —
        ``published_at`` is already stamped exactly once, at circulation
        time (see ``policies.services.circulate_policy``).
        """
        return self.is_active and self.published_at is not None

    @property
    def current_version(self):
        return self.versions.filter(is_current=True).first()

    @property
    def signed_count(self) -> int:
        return self.acknowledgements.filter(
            status=PolicyAcknowledgement.Status.SIGNED,
        ).count()

    @property
    def outstanding_count(self) -> int:
        return self.acknowledgements.exclude(
            status=PolicyAcknowledgement.Status.SIGNED,
        ).count()

    @property
    def adherent_count(self) -> int:
        return self.acknowledgements.count()


def policy_pdf_path(instance, filename):
    """Store PDFs under media/policies/<policy_id>/v<n>.pdf."""
    return f"policies/{instance.policy_id}/v{instance.version}.pdf"


class PolicyVersion(_Auditable):
    """One uploaded PDF revision of a policy. Versioning = append a row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        Policy, on_delete=models.CASCADE, related_name="versions",
    )
    version = models.PositiveIntegerField(help_text=_("1, 2, 3… unique per policy."))
    pdf = models.FileField(upload_to=policy_pdf_path)
    change_note = models.CharField(max_length=255, blank=True)
    requires_reack = models.BooleanField(
        default=True,
        help_text=_("New version re-opens signing for everyone who already signed."),
    )
    is_current = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["policy", "-version"]
        verbose_name = _("Policy version")
        verbose_name_plural = _("Policy versions")
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "version"],
                name="policies_unique_version_per_policy",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.policy.key} v{self.version}"


class PolicyAcknowledgement(_Auditable):
    """Denormalised current sign state — one row per (policy, employee).

    Drives the fast "X of Y signed / who's outstanding" dashboard. Every state
    change is also appended to ``PolicyAcknowledgementEvent`` for the immutable
    compliance trail.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SIGNED = "SIGNED", _("Signed")
        SUPERSEDED = "SUPERSEDED", _("Superseded — re-sign required")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        Policy, on_delete=models.CASCADE, related_name="acknowledgements",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="policy_acknowledgements",
    )
    version = models.ForeignKey(
        PolicyVersion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="signatures",
        help_text=_("The version the employee signed (null until signed)."),
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING,
    )
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_user_agent = models.CharField(max_length=400, blank=True)
    notified_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When the last email was sent."),
    )
    reminder_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["policy", "employee"]
        verbose_name = _("Policy acknowledgement")
        verbose_name_plural = _("Policy acknowledgements")
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "employee"],
                name="policies_unique_ack_per_policy_employee",
            ),
        ]
        indexes = [
            models.Index(fields=["policy", "status"], name="policy_ack_status_idx"),
            models.Index(fields=["employee", "status"], name="policy_ack_emp_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.policy.key} ({self.get_status_display()})"

    @property
    def is_signed(self) -> bool:
        return self.status == self.Status.SIGNED

    @property
    def is_outstanding(self) -> bool:
        return self.status != self.Status.SIGNED


class PolicyAcknowledgementEvent(models.Model):
    """Append-only audit ledger — one row per state change.

    Mirrors ``leave.LeaveBalanceTransaction``: immutable, never updated, and
    the source of truth for "who was assigned / notified / reminded / signed /
    superseded / revoked, and when".
    """

    class Event(models.TextChoices):
        ASSIGNED = "ASSIGNED", _("Assigned")
        NOTIFIED = "NOTIFIED", _("Notified")
        REMINDED = "REMINDED", _("Reminded")
        SIGNED = "SIGNED", _("Signed")
        SUPERSEDED = "SUPERSEDED", _("Superseded")
        REVOKED = "REVOKED", _("Revoked")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    acknowledgement = models.ForeignKey(
        PolicyAcknowledgement, on_delete=models.CASCADE, related_name="events",
    )
    event = models.CharField(max_length=12, choices=Event.choices)
    version = models.ForeignKey(
        PolicyVersion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    occurred_at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text=_("Who triggered it (system=null, a manager, or the employee)."),
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["occurred_at"]
        verbose_name = _("Acknowledgement event")
        verbose_name_plural = _("Acknowledgement events")
        indexes = [
            models.Index(fields=["acknowledgement", "occurred_at"],
                         name="policy_ack_event_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.acknowledgement_id} · {self.get_event_display()} @ {self.occurred_at:%Y-%m-%d %H:%M}"
