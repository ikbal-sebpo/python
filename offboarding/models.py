"""Offboarding app — Resignation, clearance workflow, exit interview, asset return.

Owns the employee-lifecycle side of separation. Delegates letter generation
(experience certificate, NOC, release letter) to the ``documents`` app via
``OffboardingCase.primary_letter`` FK + ``auto_issue_letters`` service.

Account deactivation is driven by ``CustomUser.last_working_date``, NOT by
case completion — employees must keep access until and including LWD so
they can download payslips + letters. The middleware + the nightly
celery-beat job (``deactivate_past_lwd``) handle the actual flip from
``is_active=True → False``.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


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
# Resignation (entry point)
# ---------------------------------------------------------------------------

class Resignation(_Auditable):
    """Employee-submitted separation request.

    Accepting a Resignation triggers ``CustomUser.last_working_date`` to be
    written through AND creates the matching ``OffboardingCase`` via signal.
    Only one active (PENDING or ACCEPTED) resignation per employee at a time
    — enforced by partial unique index.
    """

    class SeparationType(models.TextChoices):
        VOLUNTARY = "VOLUNTARY", _("Voluntary resignation")
        INVOLUNTARY = "INVOLUNTARY", _("Involuntary termination")
        RETIREMENT = "RETIREMENT", _("Retirement")
        CONTRACT_END = "CONTRACT_END", _("Contract end")
        ABSCONDING = "ABSCONDING", _("Absconding")
        MUTUAL = "MUTUAL", _("Mutual separation")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending HR review")
        ACCEPTED = "ACCEPTED", _("Accepted")
        REJECTED = "REJECTED", _("Rejected")
        WITHDRAWN = "WITHDRAWN", _("Withdrawn")

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="resignations",
    )
    separation_type = models.CharField(
        max_length=20, choices=SeparationType.choices,
        default=SeparationType.VOLUNTARY,
    )
    reason = models.TextField()
    notice_period_days = models.PositiveIntegerField(
        default=30,
        help_text=_("From policy default. HR can override before accepting."),
    )
    notice_start_date = models.DateField()
    last_working_date = models.DateField(
        help_text=_("On ACCEPTED, written through to CustomUser.last_working_date."),
    )

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        help_text=_("Usually self; HR can apply on behalf in special cases."),
    )
    applied_at = models.DateTimeField(default=timezone.now)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    process_note = models.CharField(max_length=255, blank=True)
    hr_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-applied_at"]
        verbose_name = _("Resignation")
        verbose_name_plural = _("Resignations")
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "last_working_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(status__in=["PENDING", "ACCEPTED"]),
                name="offboarding_one_active_resignation_per_employee",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.get_separation_type_display()} · LWD {self.last_working_date}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def is_accepted(self) -> bool:
        return self.status == self.Status.ACCEPTED

    @property
    def notice_days_given(self) -> int:
        return (self.last_working_date - self.notice_start_date).days

    @property
    def notice_shortfall_days(self) -> int:
        return max(self.notice_period_days - self.notice_days_given, 0)

    @property
    def fell_short_of_notice(self) -> bool:
        """Computed live rather than persisted — notice_period_days and
        notice_start_date stay editable up to (and even at) the ACCEPT
        decision, so a stored snapshot would risk going stale."""
        return self.notice_shortfall_days > 0

    def clean(self):
        if (self.notice_start_date and self.last_working_date
                and self.last_working_date < self.notice_start_date):
            raise ValidationError({
                "last_working_date": _("LWD cannot precede notice start date."),
            })
        # MySQL doesn't enforce the partial-unique constraint, so we re-check
        # at the model layer. New row: must not duplicate an active resignation
        # for the same employee. Existing row: ignore self.
        if self.employee_id and self.status in (self.Status.PENDING, self.Status.ACCEPTED):
            conflict_qs = Resignation.objects.filter(
                employee_id=self.employee_id,
                status__in=[self.Status.PENDING, self.Status.ACCEPTED],
            )
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError(
                    _("This employee already has an active resignation."),
                )


# ---------------------------------------------------------------------------
# Workflow template + step template + field definition (admin-configured)
# ---------------------------------------------------------------------------

class ClearanceWorkflowTemplate(_Auditable):
    """One canonical workflow per separation_type, plus future variants.

    ``is_default=True`` rows are used at case-creation time. Exactly one
    default per separation_type is enforced via partial unique index.
    """

    code = models.SlugField(
        max_length=60, unique=True,
        help_text=_("Stable identifier, e.g. 'SEBPO_DEFAULT_2026'."),
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    separation_type = models.CharField(
        max_length=20, blank=True,
        choices=Resignation.SeparationType.choices,
        help_text=_("If set, only used for that separation type. Blank = all types."),
    )

    class Meta:
        ordering = ["-is_default", "code"]
        verbose_name = _("Clearance workflow template")
        constraints = [
            models.UniqueConstraint(
                fields=["separation_type"],
                condition=models.Q(is_default=True),
                name="offboarding_one_default_workflow_per_separation_type",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # MySQL doesn't enforce the partial-unique constraint (see Meta), so
        # we self-heal here: making a row the default atomically un-defaults
        # any other row for the same separation_type.
        if self.is_default:
            with transaction.atomic():
                super().save(*args, **kwargs)
                ClearanceWorkflowTemplate.objects.filter(
                    separation_type=self.separation_type, is_default=True,
                ).exclude(pk=self.pk).update(is_default=False)
        else:
            super().save(*args, **kwargs)


class ClearanceStepTemplate(models.Model):
    """Step in a workflow template — assigned to a group OR resolved
    dynamically at case-creation time (MANAGER, DEPT_HEAD, HR_HEAD)."""

    class AssignedKind(models.TextChoices):
        GROUP = "GROUP", _("Static group")
        MANAGER = "MANAGER", _("Employee's line manager")
        DEPT_HEAD = "DEPT_HEAD", _("Employee's department head")
        HR_HEAD = "HR_HEAD", _("HR (group lookup at runtime)")

    workflow = models.ForeignKey(
        ClearanceWorkflowTemplate, on_delete=models.CASCADE,
        related_name="step_templates",
    )
    step_code = models.CharField(
        max_length=60,
        help_text=_("Stable per workflow, e.g. MANAGER_ACK, HR_INIT, IT, FIN, "
                    "ASSET, EXIT, ERP, HR_FINAL."),
    )
    step_name = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=1)

    assigned_kind = models.CharField(
        max_length=20, choices=AssignedKind.choices, default=AssignedKind.GROUP,
    )
    assigned_group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="clearance_step_templates",
        help_text=_("Required when assigned_kind == GROUP. Optional fallback "
                    "for MANAGER/DEPT_HEAD/HR_HEAD if user resolution fails."),
    )
    is_required = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    instruction = models.TextField(blank=True)

    # Behavior flags — toggled by the step UI to wire side-views in.
    opens_exit_interview = models.BooleanField(default=False)
    opens_asset_return = models.BooleanField(default=False)
    triggers_letters = models.BooleanField(
        default=False,
        help_text=_("On step DONE, auto-create the standard departing-employee "
                    "letters via documents.DocumentRequest."),
    )

    class Meta:
        ordering = ["workflow", "order"]
        verbose_name = _("Clearance step template")
        unique_together = [
            ("workflow", "step_code"),
            ("workflow", "order"),
        ]

    def __str__(self) -> str:
        return f"{self.workflow.code}:{self.step_code} ({self.order})"


class ClearanceStepFieldDefinition(models.Model):
    """Per-step dynamic field definition.

    Replaces veridoc's hardcoded `_get_predefined_fields_for_step` map at
    veridoc/views.py:930-977. HR adds / removes fields per workflow without
    code changes.
    """

    class FieldType(models.TextChoices):
        TEXT = "TEXT", _("Single-line text")
        TEXTAREA = "TEXTAREA", _("Multi-line text")
        BOOLEAN = "BOOLEAN", _("Yes / No")
        DATE = "DATE", _("Date")

    step_template = models.ForeignKey(
        ClearanceStepTemplate, on_delete=models.CASCADE,
        related_name="field_definitions",
    )
    field_key = models.SlugField(max_length=120)
    field_label = models.CharField(max_length=150)
    field_type = models.CharField(
        max_length=10, choices=FieldType.choices, default=FieldType.TEXT,
    )
    is_required = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    help_text = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["step_template", "sort_order", "field_key"]
        unique_together = [("step_template", "field_key")]
        verbose_name = _("Clearance step field definition")

    def __str__(self) -> str:
        return f"{self.step_template.step_code}.{self.field_key}"


# ---------------------------------------------------------------------------
# OffboardingCase + runtime steps
# ---------------------------------------------------------------------------

class OffboardingCase(_Auditable):
    """Runtime instance — one per accepted Resignation. Holds the workflow
    state and links to the issued letters."""

    class Status(models.TextChoices):
        NOT_STARTED = "NOT_STARTED", _("Not started")
        IN_PROGRESS = "IN_PROGRESS", _("In progress")
        COMPLETED = "COMPLETED", _("Completed")
        CANCELLED = "CANCELLED", _("Cancelled")

    resignation = models.OneToOneField(
        Resignation, on_delete=models.PROTECT, related_name="case",
    )
    workflow_template = models.ForeignKey(
        ClearanceWorkflowTemplate, on_delete=models.PROTECT, related_name="cases",
    )
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.NOT_STARTED,
    )
    current_step_order = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Convenience pointer to the experience certificate request — the rest of
    # the issued letters are discoverable via DocumentRequest.offboarding_case
    # (added by documents/0002 migration).
    primary_letter = models.ForeignKey(
        "documents.DocumentRequest", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-added_at"]
        verbose_name = _("Offboarding case")
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["completed_at"]),
        ]

    def __str__(self) -> str:
        return f"Case for {self.resignation.employee} ({self.get_status_display()})"

    @property
    def employee(self):
        return self.resignation.employee

    @property
    def current_step(self):
        return self.steps.filter(order=self.current_step_order).first()

    @property
    def progress(self) -> tuple[int, int]:
        total = self.steps.count()
        done = self.steps.filter(
            status__in=[ClearanceStep.Status.DONE, ClearanceStep.Status.SKIPPED],
        ).count()
        return done, total


class ClearanceStep(models.Model):
    """Runtime step on an OffboardingCase. Has BOTH ``assigned_group`` and
    ``assigned_user`` — manager-flavoured steps materialise the actual user
    at case-creation time via ``_resolve_assignee``. The reverse manager on
    the group still lets a backup approver (e.g. another HR member) act on
    the step when needed."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        IN_PROGRESS = "IN_PROGRESS", _("In progress")
        DONE = "DONE", _("Done")
        SKIPPED = "SKIPPED", _("Skipped")
        REJECTED = "REJECTED", _("Blocked / rejected")

    case = models.ForeignKey(
        OffboardingCase, on_delete=models.CASCADE, related_name="steps",
    )
    template = models.ForeignKey(
        ClearanceStepTemplate, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="runtime_steps",
    )

    # Snapshot from template at creation time (kept stable even if template
    # changes mid-flight).
    step_code = models.CharField(max_length=60)
    step_name = models.CharField(max_length=255)
    order = models.PositiveIntegerField()
    assigned_kind = models.CharField(
        max_length=20, choices=ClearanceStepTemplate.AssignedKind.choices,
        default=ClearanceStepTemplate.AssignedKind.GROUP,
    )
    assigned_group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="clearance_steps",
    )
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="clearance_steps_assigned",
        help_text=_("Materialised at case-creation for MANAGER/DEPT_HEAD/HR_HEAD."),
    )

    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.PENDING,
    )
    is_required = models.BooleanField(default=True)
    remarks = models.TextField(blank=True)
    data_json = models.JSONField(
        default=dict, blank=True,
        help_text=_("Values for the step's ClearanceStepFieldDefinition rows."),
    )

    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    acted_at = models.DateTimeField(null=True, blank=True)
    added_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["case", "order"]
        verbose_name = _("Clearance step")
        unique_together = [
            ("case", "step_code"),
            ("case", "order"),
        ]
        indexes = [
            models.Index(fields=["status", "order"]),
            models.Index(fields=["assigned_group", "status"]),
            models.Index(fields=["assigned_user", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.case_id} step {self.order}: {self.step_name} ({self.get_status_display()})"


class ClearanceStepAttachment(models.Model):
    """Files uploaded against a clearance step."""

    step = models.ForeignKey(
        ClearanceStep, on_delete=models.CASCADE, related_name="attachments",
    )
    file = models.FileField(upload_to="offboarding/step_attachments/")
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
    )
    uploaded_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    file_size = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.original_name} @ step {self.step_id}"


def step_attachments_supported() -> bool:
    """Return False when the attachment table is not yet migrated."""
    from django.db import connection
    return ClearanceStepAttachment._meta.db_table in connection.introspection.table_names()


# ---------------------------------------------------------------------------
# Side-views (exit interview, asset return, notes)
# ---------------------------------------------------------------------------

class ExitInterview(_Auditable):
    """Structured exit interview — replaces veridoc's freeform JSON blob."""

    case = models.OneToOneField(
        OffboardingCase, on_delete=models.CASCADE, related_name="exit_interview",
    )
    conducted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
    )
    conducted_at = models.DateTimeField(default=timezone.now)
    reason_for_leaving = models.TextField()
    would_recommend_sebpo = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text=_("1 = would not recommend, 5 = would strongly recommend."),
    )
    would_return_if_offered = models.BooleanField()
    what_did_well = models.TextField(blank=True)
    what_could_improve = models.TextField(blank=True)
    new_employer = models.CharField(max_length=255, blank=True)
    final_remarks = models.TextField(blank=True)

    class Meta:
        verbose_name = _("Exit interview")

    def __str__(self) -> str:
        return f"Exit interview · {self.case.employee}"


class AssetReturn(_Auditable):
    """One row per asset returned during offboarding. Replaces the boolean-
    in-data_json approach veridoc used."""

    class AssetType(models.TextChoices):
        LAPTOP = "LAPTOP", _("Laptop")
        PHONE = "PHONE", _("Phone")
        SIM = "SIM", _("SIM card")
        ID_CARD = "ID_CARD", _("ID card")
        ACCESS_CARD = "ACCESS_CARD", _("Access / proximity card")
        UNIFORM = "UNIFORM", _("Uniform")
        OTHER = "OTHER", _("Other")

    case = models.ForeignKey(
        OffboardingCase, on_delete=models.CASCADE, related_name="asset_returns",
    )
    asset_type = models.CharField(max_length=20, choices=AssetType.choices)
    asset_identifier = models.CharField(
        max_length=120, blank=True,
        help_text=_("Serial / tag / IMEI etc."),
    )
    returned = models.BooleanField(default=False)
    returned_at = models.DateTimeField(null=True, blank=True)
    condition = models.CharField(max_length=255, blank=True)
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["case", "asset_type"]
        verbose_name = _("Asset return")
        indexes = [models.Index(fields=["case", "returned"])]

    def __str__(self) -> str:
        return f"{self.get_asset_type_display()}{(' #' + self.asset_identifier) if self.asset_identifier else ''}"


class OffboardingNote(models.Model):
    """Append-only HR timeline. Notes never edited, never deleted."""

    case = models.ForeignKey(
        OffboardingCase, on_delete=models.CASCADE, related_name="notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
    )
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    is_internal = models.BooleanField(
        default=True,
        help_text=_("False would expose the note on the employee's My Offboarding view."),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Offboarding note")
        indexes = [models.Index(fields=["case", "-created_at"])]

    def __str__(self) -> str:
        return f"Note by {self.author} on {self.created_at:%Y-%m-%d}"
