"""Django admin for the documents app.

CompanyBranding is locked to a single row. TemplateDesign / MessageTemplate
admins surface bleach sanitisation warnings via ``messages.warning(...)``.
DocumentType has a "Test render PDF" action that streams a sample PDF for
the current admin user.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import format_html

from core.admin_mixins import PayrollMoneyAdminMixin

from documents.models import (
    CompanyBranding,
    DocumentExtraFieldType,
    DocumentExtraFieldValue,
    DocumentRequest,
    DocumentReviewConfig,
    DocumentSignatoryConfig,
    DocumentType,
    MessageTemplate,
    RequestApproval,
    RequestReview,
    TemplateDesign,
    TemplateRevision,
)


# ---------------------------------------------------------------------------
# Inlines for DocumentType
# ---------------------------------------------------------------------------

class ReviewConfigInline(admin.TabularInline):
    model = DocumentReviewConfig
    extra = 0
    autocomplete_fields = ["reviewer_group"]


class SignatoryConfigInline(admin.TabularInline):
    model = DocumentSignatoryConfig
    extra = 0
    autocomplete_fields = ["signatory_group"]


class ExtraFieldTypeInline(admin.TabularInline):
    model = DocumentExtraFieldType
    extra = 0
    fields = ("sort_order", "field_key", "field_name", "kind",
              "fillable_by", "is_required", "is_active", "help_text")


# ---------------------------------------------------------------------------
# CompanyBranding (singleton)
# ---------------------------------------------------------------------------

@admin.register(CompanyBranding)
class CompanyBrandingAdmin(admin.ModelAdmin):
    list_display = ("company_name", "footer_company", "website", "updated_at")
    fields = (
        "company_name", "footer_company", "logo",
        "head_office_address", "branch_office_address", "jashore_office_address",
        "website", "default_signatory", "default_ref_no_prefix",
    )
    autocomplete_fields = ["default_signatory"]

    def has_add_permission(self, request):
        return not CompanyBranding.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

class TemplateRevisionInline(admin.TabularInline):
    model = TemplateRevision
    fields = ("version", "changed_at", "changed_by", "change_note")
    readonly_fields = fields
    extra = 0
    can_delete = False
    max_num = 0
    show_change_link = True


def _surface_template_warnings(request, obj):
    for w in getattr(obj, "_template_warnings", []):
        messages.warning(request, w)


@admin.register(TemplateDesign)
class TemplateDesignAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "version", "is_active", "updated_by", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "key")
    readonly_fields = ("version", "added_at", "added_by", "updated_at")
    inlines = [TemplateRevisionInline]

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        _surface_template_warnings(request, obj)


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "version", "is_active", "updated_by", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "key")
    readonly_fields = ("version", "added_at", "added_by", "updated_at")
    inlines = [TemplateRevisionInline]

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        _surface_template_warnings(request, obj)


@admin.register(TemplateRevision)
class TemplateRevisionAdmin(admin.ModelAdmin):
    list_display = ("changed_at", "_target", "version", "changed_by", "change_note")
    list_filter = ("template_design", "message_template", "changed_at")
    search_fields = ("change_note",)
    readonly_fields = ("template_design", "message_template", "version",
                       "html_snapshot", "changed_by", "changed_at", "change_note")
    ordering = ("-changed_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Template")
    def _target(self, obj):
        return obj.template_design or obj.message_template


# ---------------------------------------------------------------------------
# DocumentType
# ---------------------------------------------------------------------------

@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = (
        "display_name", "code", "name", "is_active",
        "requires_approval", "auto_approve",
        "template_design", "message_template",
        "_reviewer_count", "_signatory_count",
    )
    list_filter = ("is_active", "requires_approval", "auto_approve")
    search_fields = ("code", "name", "display_name")
    autocomplete_fields = ("template_design", "message_template")
    inlines = [ReviewConfigInline, SignatoryConfigInline, ExtraFieldTypeInline]
    actions = ["test_render_pdf"]

    fieldsets = (
        (None, {
            "fields": ("code", "name", "display_name", "description"),
        }),
        ("Rendering", {
            "fields": ("template_design", "message_template"),
        }),
        ("Workflow", {
            "fields": ("requires_approval", "auto_approve", "auto_approve_reason", "is_active"),
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Reviewers")
    def _reviewer_count(self, obj):
        return obj.review_configs.count()

    @admin.display(description="Signatories")
    def _signatory_count(self, obj):
        return obj.signatory_configs.count()

    @admin.action(description="Test render PDF (sample data)")
    def test_render_pdf(self, request, queryset):
        from documents.pdf import test_render_pdf
        if queryset.count() != 1:
            self.message_user(
                request, "Select exactly one document type to test-render.",
                level=messages.WARNING,
            )
            return
        doc_type = queryset.first()
        try:
            pdf_bytes = test_render_pdf(doc_type, employee=request.user)
        except Exception as exc:
            self.message_user(request, f"Render failed: {exc}", level=messages.ERROR)
            return
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'inline; filename="test_{doc_type.code}.pdf"'
        )
        return response


# ---------------------------------------------------------------------------
# DocumentRequest + inlines
# ---------------------------------------------------------------------------

class RequestReviewInline(admin.TabularInline):
    model = RequestReview
    extra = 0
    can_delete = False
    fields = ("reviewer_group", "reviewer", "status", "remarks", "order", "reviewed_at")
    readonly_fields = ("reviewer_group", "reviewer", "status", "order", "reviewed_at")


class RequestApprovalInline(admin.TabularInline):
    model = RequestApproval
    extra = 0
    can_delete = False
    fields = ("approver_group", "signatory", "status", "remarks", "order", "signed_at")
    readonly_fields = ("approver_group", "signatory", "status", "order", "signed_at")


class ExtraFieldValueInline(admin.TabularInline):
    model = DocumentExtraFieldValue
    extra = 0
    can_delete = False
    fields = ("field_type", "value", "submitted_by", "submitted_at")
    readonly_fields = fields


_STATUS_COLORS = {
    "REVIEW":   "#e0f2fe",
    "PENDING":  "#fff7ed",
    "PARTIAL":  "#fef9c3",
    "APPROVED": "#dcfce7",
    "REJECTED": "#fee2e2",
    "CANCELLED":"#e5e7eb",
}


@admin.register(DocumentRequest)
class DocumentRequestAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    # input_data is the salary certificate's three months of gross/bonus/net
    # (encrypted at rest) — shown here to a superuser or Payroll Admin only,
    # same rule as every payroll amount (PAYROLL_DATA_PROTECTION_PLAN.md §4.6).
    MONEY_FIELDS = ("input_data",)
    list_display = (
        "_id_short", "employee", "document_type", "_status_badge",
        "applied_at", "processed_at", "processed_by",
    )
    # Avoid MySQL timezone conversion on admin date bucketing when the
    # server lacks loaded timezone tables. ``applied_at`` still shows in the
    # table, but the changelist filters stay on fields that don't require
    # timezone extraction.
    list_filter = ("status", "document_type")
    search_fields = (
        "id", "employee__email", "employee__first_name",
        "employee__last_name", "employee__employee_id",
    )
    autocomplete_fields = ("employee", "applied_by", "processed_by")
    readonly_fields = (
        "id", "generated_file", "applied_at", "processed_at",
        "added_at", "updated_at", "input_data", "rejection_history",
    )
    inlines = [RequestReviewInline, RequestApprovalInline, ExtraFieldValueInline]

    @admin.display(description="ID")
    def _id_short(self, obj):
        return str(obj.id)[:8]

    @admin.display(description="Status")
    def _status_badge(self, obj):
        color = _STATUS_COLORS.get(obj.status, "#e5e7eb")
        return format_html(
            '<span style="padding:2px 8px; border-radius:8px; background:{}; '
            'color:#111; font-size:0.8em">{}</span>',
            color, obj.get_status_display(),
        )


# ---------------------------------------------------------------------------
# Smaller config admins (mostly editable via inline on DocumentTypeAdmin,
# but keep stand-alone admins for power-user lookups)
# ---------------------------------------------------------------------------

@admin.register(DocumentExtraFieldType)
class DocumentExtraFieldTypeAdmin(admin.ModelAdmin):
    list_display = ("field_name", "field_key", "document_type", "kind",
                    "fillable_by", "is_required", "is_active", "sort_order")
    list_filter = ("document_type", "kind", "fillable_by", "is_active")
    search_fields = ("field_name", "field_key")
    list_editable = ("sort_order", "is_active")


@admin.register(DocumentExtraFieldValue)
class DocumentExtraFieldValueAdmin(admin.ModelAdmin):
    list_display = ("request", "field_type", "value", "submitted_by", "submitted_at")
    list_filter = ("is_active", "field_type__document_type")
    search_fields = ("value", "request__employee__email")
    readonly_fields = ("submitted_at",)


@admin.register(DocumentSignatoryConfig)
class DocumentSignatoryConfigAdmin(admin.ModelAdmin):
    list_display = ("document_type", "signatory_group", "signature_order")
    list_filter = ("document_type",)
    autocomplete_fields = ("signatory_group",)


@admin.register(DocumentReviewConfig)
class DocumentReviewConfigAdmin(admin.ModelAdmin):
    list_display = ("document_type", "reviewer_group", "review_order")
    list_filter = ("document_type",)
    autocomplete_fields = ("reviewer_group",)
