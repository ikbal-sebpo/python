"""Documents app — DocumentType, request workflow, PDF generation.

Single source of truth for HR-issued letters (NOC, salary certificate,
experience certificate, employment verification, etc.). The model shape
ports veridoc's design with several improvements:

* ``rejection_history`` JSONField replaces a single overwritten reason
* ``DocumentType.code`` stable SlugField for cross-app references
* ``auto_approve`` paired with ``auto_approve_reason`` for audit clarity
* ``CompanyBranding`` singleton replaces hardcoded paths in pdf_generator.py
* ``TemplateRevision`` audit log for HR-edited templates
* Approval shape mirrors ``leave.LeaveApplication`` so Orbis admins see a
  consistent ``applied_by`` / ``processed_by`` / ``process_note`` shape

The action-workflow models (clearance steps, exit interview, asset return)
live in the ``offboarding`` app — not here. ``DocumentRequest.offboarding_case``
is added by a follow-up migration once that app exists.
"""

from __future__ import annotations

import uuid

from django_ckeditor_5.fields import CKEditor5Field
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.fields import EncryptedJSONField


# ---------------------------------------------------------------------------
# Local abstract (mirrors leave/models.py::_Auditable)
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
# CompanyBranding (singleton, pk=1)
# ---------------------------------------------------------------------------

class CompanyBranding(models.Model):
    """One row. Holds the logo, addresses, default signatory used by the PDF
    pipeline. Replaces every hardcoded constant in veridoc's pdf_generator.py.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    company_name = models.CharField(max_length=200, default="SEBPO")
    footer_company = models.CharField(max_length=200, blank=True, default="ServicEngine Ltd.")
    logo = models.ImageField(upload_to="documents/branding/", blank=True, null=True)

    head_office_address = models.TextField(blank=True)
    branch_office_address = models.TextField(blank=True)
    jashore_office_address = models.TextField(blank=True)
    website = models.CharField(max_length=200, blank=True, default="sebpo.com")

    default_signatory = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text=_("Used on letters that don't have a signed approval row yet."),
    )
    default_ref_no_prefix = models.CharField(
        max_length=50, blank=True, default="SE/HRD",
        help_text=_("Prepended to letter reference numbers when not overridden in input_data."),
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = _("Company branding")
        verbose_name_plural = _("Company branding")

    def save(self, *args, **kwargs):
        self.id = 1  # enforce singleton
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover — defensive
        raise ValidationError("CompanyBranding is a singleton; it cannot be deleted.")

    @classmethod
    def load(cls) -> "CompanyBranding":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return self.company_name


# ---------------------------------------------------------------------------
# Template machinery (DB-stored RichText with sanitisation + revisions)
# ---------------------------------------------------------------------------

class _VersionedRichTemplate(_Auditable):
    """Shared shape for TemplateDesign + MessageTemplate.

    Subclasses define the RichText field name via ``_HTML_FIELD``. Save flow:
    1. bleach-clean the HTML in place
    2. validate ``{{ var }}`` tokens against ``PDF_CONTEXT_SCHEMA`` (warnings,
       not errors — HR may use dynamic input_data keys we can't predict)
    3. detect change vs DB; bump ``version`` and queue a TemplateRevision row
    4. super().save()
    5. on commit, write the TemplateRevision

    Warnings collected during save land on ``self._template_warnings`` for the
    admin to surface via ``messages.warning(...)``.
    """

    _HTML_FIELD: str = ""  # subclass override
    _SANITIZE: bool = True  # subclass may set False (TemplateDesign needs full <html>/<style>)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    # `added_at`/`added_by` from _Auditable serve as the create timestamp;
    # `updated_at`/`updated_by` track the last edit. No extra fields needed.

    class Meta:
        abstract = True
        ordering = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Snapshot the original HTML so save() can detect changes.
        self._original_html = getattr(self, self._HTML_FIELD, "") or ""
        self._template_warnings: list[str] = []
        self._pending_revision: bool = False

    def __str__(self) -> str:
        return f"{self.name} (v{self.version})"

    def save(self, *args, **kwargs):
        from documents.sanitize import clean_template_html
        from documents.pdf import (
            PDF_CONTEXT_SCHEMA,
            validate_template_against_context,
        )

        html = getattr(self, self._HTML_FIELD, "") or ""
        if self._SANITIZE:
            cleaned, was_modified = clean_template_html(html)
            if was_modified:
                self._template_warnings.append(
                    "Some HTML was removed during sanitisation (disallowed tags, "
                    "scripts, or unsafe CSS). The cleaned version was saved."
                )
                setattr(self, self._HTML_FIELD, cleaned)
                html = cleaned

        self._template_warnings.extend(
            validate_template_against_context(html, PDF_CONTEXT_SCHEMA)
        )

        # Detect change: bump version + queue a revision row.
        # Use _state.adding (not `not self.pk`) — UUID-pk models have pk
        # assigned in __init__ via default=uuid.uuid4 long before save().
        if self._state.adding:
            self.version = 1
            self._pending_revision = True
        elif html != (self._original_html or ""):
            self.version = (self.version or 1) + 1
            self._pending_revision = True

        super().save(*args, **kwargs)

        if self._pending_revision:
            TemplateRevision.objects.create(
                **self._revision_kwargs(),
                version=self.version,
                html_snapshot=html,
                changed_by=self.updated_by or self.added_by,
            )
            self._pending_revision = False
            self._original_html = html

    def _revision_kwargs(self) -> dict:  # subclass override
        raise NotImplementedError


class TemplateDesign(_VersionedRichTemplate):
    """Outer page layout / letterhead wrapper.

    HTML must include ``{{ body_html|safe }}`` somewhere — that's where the
    selected MessageTemplate body is injected at render time.

    Sanitisation is OFF: designs need <html>/<head>/<style>/<body> and are
    only editable by Admins and HR. The output is fed to WeasyPrint server-side,
    so XSS doesn't apply. (Variable validation still runs.)
    """

    _HTML_FIELD = "html"
    _SANITIZE = False

    html = CKEditor5Field(
        blank=True,
        help_text=_("Full HTML layout. Must contain {{ body_html|safe }}."),
    )

    class Meta(_VersionedRichTemplate.Meta):
        verbose_name = _("Template design")
        verbose_name_plural = _("Template designs")

    def _revision_kwargs(self) -> dict:
        return {"template_design": self}


class MessageTemplate(_VersionedRichTemplate):
    """Inner body content (the actual letter wording).

    Rendered with the per-request context built by ``documents.pdf``.
    """

    _HTML_FIELD = "body_html"

    body_html = CKEditor5Field(
        blank=True,
        help_text=_("Letter body. Django template syntax — {{ employee.get_full_name }} etc."),
    )

    class Meta(_VersionedRichTemplate.Meta):
        verbose_name = _("Message template")
        verbose_name_plural = _("Message templates")

    def _revision_kwargs(self) -> dict:
        return {"message_template": self}


class TemplateRevision(models.Model):
    """Append-only audit log. One row per save of a template that changed
    the HTML. Visibility only — no rollback in MVP.
    """

    template_design = models.ForeignKey(
        TemplateDesign, on_delete=models.CASCADE, null=True, blank=True,
        related_name="revisions",
    )
    message_template = models.ForeignKey(
        MessageTemplate, on_delete=models.CASCADE, null=True, blank=True,
        related_name="revisions",
    )
    version = models.PositiveIntegerField()
    html_snapshot = models.TextField()
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now)
    change_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-changed_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(template_design__isnull=False)
                    | models.Q(message_template__isnull=False)
                ),
                name="documents_revision_targets_one_template",
            ),
        ]
        indexes = [
            models.Index(fields=["template_design", "-version"]),
            models.Index(fields=["message_template", "-version"]),
        ]

    def __str__(self) -> str:
        target = self.template_design or self.message_template
        return f"{target} → v{self.version}"


# ---------------------------------------------------------------------------
# DocumentType + per-type configuration
# ---------------------------------------------------------------------------

class DocumentType(_Auditable):
    """Catalogue of letters HR can issue.

    Each row binds a renderable template (design + body) to a review +
    approval chain configured via ``DocumentReviewConfig`` and
    ``DocumentSignatoryConfig`` inlines. Dynamic per-type form fields live
    in ``DocumentExtraFieldType``.
    """

    code = models.SlugField(
        max_length=64, unique=True,
        help_text=_("Stable machine key, e.g. 'NOC', 'EXP_LETTER'. Used for "
                    "cross-app references (offboarding looks up letters by code)."),
    )
    name = models.CharField(
        max_length=100, unique=True,
        help_text=_("Internal label, e.g. 'noc_visa'."),
    )
    display_name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    template_design = models.ForeignKey(
        TemplateDesign, on_delete=models.PROTECT,
        null=True, blank=True, related_name="document_types",
    )
    message_template = models.ForeignKey(
        MessageTemplate, on_delete=models.PROTECT,
        null=True, blank=True, related_name="document_types",
    )

    requires_approval = models.BooleanField(default=True)
    auto_approve = models.BooleanField(
        default=False,
        help_text=_("Skip review + approval entirely. Requires "
                    "requires_approval=False AND auto_approve_reason."),
    )
    auto_approve_reason = models.CharField(
        max_length=255, blank=True,
        help_text=_("Audit trail for why no human review is needed."),
    )

    is_active = models.BooleanField(default=True)
    # `added_at`/`updated_at` come from _Auditable.

    class Meta:
        ordering = ["display_name"]
        verbose_name = _("Document type")
        verbose_name_plural = _("Document types")

    def __str__(self) -> str:
        return self.display_name

    def clean(self):
        if self.auto_approve and not self.auto_approve_reason:
            raise ValidationError({
                "auto_approve_reason": _("Required when auto-approve is enabled."),
            })
        if self.auto_approve and self.requires_approval:
            raise ValidationError(
                _("Auto-approve only makes sense when requires_approval is False."),
            )


class DocumentSignatoryConfig(models.Model):
    """One row per (DocumentType, Group) — the groups that must sign.
    Ordering controls who signs first."""

    document_type = models.ForeignKey(
        DocumentType, on_delete=models.CASCADE, related_name="signatory_configs",
    )
    signatory_group = models.ForeignKey(Group, on_delete=models.PROTECT)
    signature_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["document_type", "signature_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "signatory_group"],
                name="documents_unique_group_per_doc_signatory",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_type} · {self.signatory_group} (order {self.signature_order})"


class DocumentReviewConfig(models.Model):
    """One row per (DocumentType, Group) — the groups that must review
    before approval. Ordering controls who reviews first."""

    document_type = models.ForeignKey(
        DocumentType, on_delete=models.CASCADE, related_name="review_configs",
    )
    reviewer_group = models.ForeignKey(Group, on_delete=models.PROTECT)
    review_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["document_type", "review_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "reviewer_group"],
                name="documents_unique_group_per_doc_review",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_type} · {self.reviewer_group} (order {self.review_order})"


class DocumentExtraFieldType(models.Model):
    """Per-DocumentType dynamic form field definition.

    The request form generates these on the fly; values land in
    ``DocumentExtraFieldValue``.
    """

    class FieldKind(models.TextChoices):
        TEXT = "TEXT", _("Single-line text")
        TEXTAREA = "TEXTAREA", _("Multi-line text")
        NUMBER = "NUMBER", _("Number")
        DATE = "DATE", _("Date")
        BOOLEAN = "BOOLEAN", _("Yes / No")
        CHOICE = "CHOICE", _("Dropdown choice")

    class FillableBy(models.TextChoices):
        REQUESTER = "REQUESTER", _("Requester")
        APPROVER = "APPROVER", _("Approver")

    document_type = models.ForeignKey(
        DocumentType, on_delete=models.CASCADE, related_name="extra_field_types",
    )
    field_key = models.SlugField(
        max_length=120,
        help_text=_("Template variable name, e.g. 'destination_country'."),
    )
    field_name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=FieldKind.choices, default=FieldKind.TEXT)
    fillable_by = models.CharField(
        max_length=20,
        choices=FillableBy.choices,
        default=FillableBy.REQUESTER,
        help_text=_("Who fills this value in the document workflow."),
    )
    is_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    choices = models.JSONField(
        default=list, blank=True,
        help_text=_("For CHOICE kind: list of strings."),
    )
    help_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "field_key"],
                name="documents_unique_extra_field_key_per_doc",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_type} · {self.field_name}"


# ---------------------------------------------------------------------------
# DocumentRequest + Review + Approval (the workflow rows)
# ---------------------------------------------------------------------------

def _generated_upload_to(instance, filename):
    return f"documents/generated/{instance.document_type.code}_{instance.id}.pdf"


class DocumentRequest(_Auditable):
    """An employee's request for a letter.

    Lifecycle:
      created → IN_REVIEW (if any DocumentReviewConfig) → PENDING (after all
      reviews pass) → APPROVED + PDF attached.
    Or:
      created → APPROVED if requires_approval=False AND auto_approve=True.
    Or:
      created → REJECTED at any review/approval step.

    Approval shape mirrors ``leave.LeaveApplication`` so the admin / queue
    UI is consistent across modules.
    """

    class Status(models.TextChoices):
        IN_REVIEW = "REVIEW", _("In review")
        PENDING = "PENDING", _("Pending approval")
        PARTIALLY_APPROVED = "PARTIAL", _("Partially approved")
        APPROVED = "APPROVED", _("Approved & generated")
        REJECTED = "REJECTED", _("Rejected")
        CANCELLED = "CANCELLED", _("Cancelled by applicant")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="document_requests",
    )
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )

    input_data = EncryptedJSONField(
        default=dict, blank=True,
        help_text=_("Raw form data — both standard fields (reason, letter_date) "
                    "and dynamic ones merged in. Flattened into the PDF context."),
    )
    generated_file = models.FileField(
        upload_to=_generated_upload_to, null=True, blank=True,
    )

    rejection_history = models.JSONField(
        default=list, blank=True,
        help_text=_("Append-only list of {at, by, by_pk, stage, step_order, "
                    "reason} dicts. Replaces veridoc's overwritten field."),
    )

    # Cross-app link to offboarding. Populated by ``auto_issue_letters`` when
    # a clearance case completes; null for all other requests. String FK
    # avoids a circular import between documents and offboarding.
    offboarding_case = models.ForeignKey(
        "offboarding.OffboardingCase", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="letters",
    )

    # Leave-style approval shape — usually applied_by == employee, but HR may
    # apply on someone's behalf (e.g. for a departing employee).
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
    )
    applied_at = models.DateTimeField(default=timezone.now)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    process_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-applied_at"]
        verbose_name = _("Document request")
        verbose_name_plural = _("Document requests")
        indexes = [
            models.Index(fields=["employee", "status"], name="docreq_emp_status_idx"),
            models.Index(fields=["status"], name="docreq_status_idx"),
            models.Index(fields=["document_type", "status"], name="docreq_dt_status_idx"),
            # Matches Meta.ordering — avoids a full-table filesort on list views.
            models.Index(fields=["-applied_at"], name="docreq_applied_at_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.document_type} ({self.get_status_display()})"

    # ----- convenience accessors used by templates + admin -----

    @property
    def is_in_review(self) -> bool:
        return self.status == self.Status.IN_REVIEW

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == self.Status.APPROVED

    @property
    def is_rejected(self) -> bool:
        return self.status == self.Status.REJECTED

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    @property
    def is_fully_approved(self) -> bool:
        return not self.approvals.exclude(status=RequestApproval.Status.APPROVED).exists()

    @property
    def is_fully_reviewed(self) -> bool:
        return not self.reviews.exclude(status=RequestReview.Status.REVIEWED).exists()

    @property
    def current_review_step(self):
        return self.reviews.filter(status=RequestReview.Status.PENDING).order_by("order").first()

    @property
    def current_approval_step(self):
        return self.approvals.filter(status=RequestApproval.Status.PENDING).order_by("order").first()

    @property
    def latest_rejection(self):
        return self.rejection_history[-1] if self.rejection_history else None

    def append_rejection(self, *, stage: str, step_order: int, reason: str, by) -> None:
        """Append to rejection_history and save in one go."""
        entry = {
            "at": timezone.now().isoformat(),
            "stage": stage,
            "step_order": step_order,
            "reason": reason or "",
            "by_pk": str(by.pk) if by else None,
            "by_email": getattr(by, "email", "") if by else "",
        }
        history = list(self.rejection_history or [])
        history.append(entry)
        self.rejection_history = history
        self.save(update_fields=["rejection_history", "updated_at"])


class RequestReview(models.Model):
    """One row per review step on a DocumentRequest. Generated by the
    ``create_review_chain`` signal from ``DocumentReviewConfig``."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        REVIEWED = "REVIEWED", _("Reviewed")
        REJECTED = "REJECTED", _("Rejected")

    request = models.ForeignKey(
        DocumentRequest, on_delete=models.CASCADE, related_name="reviews",
    )
    reviewer_group = models.ForeignKey(Group, on_delete=models.PROTECT)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    remarks = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=1)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "reviewed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "reviewer_group"],
                name="documents_unique_review_group_per_request",
            ),
        ]

    def __str__(self) -> str:
        actor = self.reviewer or self.reviewer_group
        return f"Review step {self.order}: {actor} ({self.get_status_display()})"

    def mark_reviewed(self, user, remarks: str = "") -> None:
        self.reviewer = user
        self.status = self.Status.REVIEWED
        self.remarks = remarks or ""
        self.reviewed_at = timezone.now()
        self.save()


class RequestApproval(models.Model):
    """One row per approval / signing step. Generated when the review chain
    completes (or immediately if there's no review chain)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")

    request = models.ForeignKey(
        DocumentRequest, on_delete=models.CASCADE, related_name="approvals",
    )
    approver_group = models.ForeignKey(Group, on_delete=models.PROTECT)
    signatory = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    remarks = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=1)
    signed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "signed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "approver_group"],
                name="documents_unique_approval_group_per_request",
            ),
        ]

    def __str__(self) -> str:
        actor = self.signatory or self.approver_group
        return f"Approval step {self.order}: {actor} ({self.get_status_display()})"


class DocumentExtraFieldValue(models.Model):
    """A submitted value for a ``DocumentExtraFieldType``. One row per
    (request, field_type). All values stored as strings; the request form
    handles type coercion when reading back."""

    request = models.ForeignKey(
        DocumentRequest, on_delete=models.CASCADE, related_name="extra_field_values",
    )
    field_type = models.ForeignKey(
        DocumentExtraFieldType, on_delete=models.PROTECT, related_name="values",
    )
    value = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="+",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "field_type"],
                name="documents_unique_extra_value_per_request_field",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.field_type.field_name}: {self.value[:50]}"

    def clean(self):
        if self.field_type and self.request \
                and self.field_type.document_type_id != self.request.document_type_id:
            raise ValidationError(
                _("Field type does not belong to this request's document type."),
            )
