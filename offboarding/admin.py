"""Django admin for the offboarding app."""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from offboarding.models import (
    AssetReturn,
    ClearanceStep,
    ClearanceStepFieldDefinition,
    ClearanceStepTemplate,
    ClearanceWorkflowTemplate,
    ExitInterview,
    OffboardingCase,
    OffboardingNote,
    Resignation,
)


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------

class ClearanceStepFieldDefinitionInline(admin.TabularInline):
    model = ClearanceStepFieldDefinition
    extra = 0
    fields = ("sort_order", "field_key", "field_label", "field_type",
              "is_required", "is_active", "help_text")


class ClearanceStepTemplateInline(admin.TabularInline):
    model = ClearanceStepTemplate
    extra = 0
    fields = ("order", "step_code", "step_name", "assigned_kind",
              "assigned_group", "is_required", "is_active",
              "opens_exit_interview", "opens_asset_return", "triggers_letters")


class ClearanceStepInline(admin.TabularInline):
    model = ClearanceStep
    extra = 0
    can_delete = False
    fields = ("order", "step_code", "step_name", "status",
              "assigned_group", "assigned_user", "acted_by", "acted_at")
    readonly_fields = ("order", "step_code", "step_name", "assigned_group",
                       "assigned_user", "acted_by", "acted_at")


# ---------------------------------------------------------------------------
# Workflow templates
# ---------------------------------------------------------------------------

_CASE_STATUS_COLORS = {
    "NOT_STARTED": "#e5e7eb",
    "IN_PROGRESS": "#fff7ed",
    "COMPLETED": "#dcfce7",
    "CANCELLED": "#fee2e2",
}

_STEP_STATUS_COLORS = {
    "PENDING": "#e0f2fe",
    "IN_PROGRESS": "#fff7ed",
    "DONE": "#dcfce7",
    "SKIPPED": "#e5e7eb",
    "REJECTED": "#fee2e2",
}


@admin.register(ClearanceWorkflowTemplate)
class ClearanceWorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_default", "is_active",
                    "separation_type", "_step_count", "updated_at")
    list_filter = ("is_default", "is_active", "separation_type")
    search_fields = ("name", "code")
    inlines = [ClearanceStepTemplateInline]

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Steps")
    def _step_count(self, obj):
        return obj.step_templates.count()


@admin.register(ClearanceStepTemplate)
class ClearanceStepTemplateAdmin(admin.ModelAdmin):
    list_display = ("workflow", "order", "step_code", "step_name",
                    "assigned_kind", "assigned_group", "is_required", "is_active",
                    "opens_exit_interview", "opens_asset_return", "triggers_letters")
    list_filter = ("workflow", "assigned_kind", "is_required", "is_active",
                   "opens_exit_interview", "opens_asset_return", "triggers_letters")
    search_fields = ("step_code", "step_name")
    autocomplete_fields = ("workflow", "assigned_group")
    inlines = [ClearanceStepFieldDefinitionInline]


@admin.register(ClearanceStepFieldDefinition)
class ClearanceStepFieldDefinitionAdmin(admin.ModelAdmin):
    list_display = ("step_template", "sort_order", "field_key", "field_label",
                    "field_type", "is_required", "is_active")
    list_filter = ("field_type", "is_required", "is_active")
    search_fields = ("field_key", "field_label")


# ---------------------------------------------------------------------------
# Runtime models
# ---------------------------------------------------------------------------

@admin.register(Resignation)
class ResignationAdmin(admin.ModelAdmin):
    list_display = ("employee", "separation_type", "_status_badge",
                    "notice_start_date", "last_working_date",
                    "applied_at", "processed_by")
    # Avoid MySQL timezone conversion in the admin changelist when the
    # server does not have timezone tables installed.
    list_filter = ("status", "separation_type")
    search_fields = ("employee__email", "employee__first_name",
                     "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "applied_by", "processed_by")
    readonly_fields = ("applied_at", "processed_at", "added_at", "updated_at")

    @admin.display(description="Status")
    def _status_badge(self, obj):
        return format_html(
            '<span style="padding:2px 8px; border-radius:8px; '
            'background:#e0f2fe; color:#111; font-size:0.8em">{}</span>',
            obj.get_status_display(),
        )


@admin.register(OffboardingCase)
class OffboardingCaseAdmin(admin.ModelAdmin):
    list_display = ("_short_id", "_employee", "_separation_type", "_status_badge",
                    "current_step_order", "started_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("resignation__employee__email",
                     "resignation__employee__first_name",
                     "resignation__employee__last_name",
                     "resignation__employee__employee_id")
    autocomplete_fields = ("resignation", "workflow_template", "primary_letter")
    readonly_fields = ("started_at", "completed_at", "added_at", "updated_at")
    inlines = [ClearanceStepInline]

    @admin.display(description="ID")
    def _short_id(self, obj):
        return str(obj.pk)

    @admin.display(description="Employee")
    def _employee(self, obj):
        return obj.resignation.employee

    @admin.display(description="Type")
    def _separation_type(self, obj):
        return obj.resignation.get_separation_type_display()

    @admin.display(description="Status")
    def _status_badge(self, obj):
        color = _CASE_STATUS_COLORS.get(obj.status, "#e5e7eb")
        return format_html(
            '<span style="padding:2px 8px; border-radius:8px; '
            'background:{}; color:#111; font-size:0.8em">{}</span>',
            color, obj.get_status_display(),
        )


@admin.register(ClearanceStep)
class ClearanceStepAdmin(admin.ModelAdmin):
    list_display = ("case", "order", "step_name", "_status_badge",
                    "assigned_group", "assigned_user", "acted_by", "acted_at")
    list_filter = ("status", "assigned_group")
    search_fields = ("step_code", "step_name",
                     "case__resignation__employee__email")
    autocomplete_fields = ("case", "template", "assigned_group",
                           "assigned_user", "acted_by")
    readonly_fields = ("added_at", "updated_at", "acted_at")

    @admin.display(description="Status")
    def _status_badge(self, obj):
        color = _STEP_STATUS_COLORS.get(obj.status, "#e5e7eb")
        return format_html(
            '<span style="padding:2px 8px; border-radius:8px; '
            'background:{}; color:#111; font-size:0.8em">{}</span>',
            color, obj.get_status_display(),
        )


@admin.register(ExitInterview)
class ExitInterviewAdmin(admin.ModelAdmin):
    list_display = ("case", "conducted_by", "conducted_at",
                    "would_recommend_sebpo", "would_return_if_offered")
    list_filter = ("would_return_if_offered", "would_recommend_sebpo")
    search_fields = ("case__resignation__employee__email",)
    autocomplete_fields = ("case", "conducted_by")


@admin.register(AssetReturn)
class AssetReturnAdmin(admin.ModelAdmin):
    list_display = ("case", "asset_type", "asset_identifier",
                    "returned", "returned_at", "receiver")
    list_filter = ("asset_type", "returned")
    search_fields = ("asset_identifier", "case__resignation__employee__email")
    autocomplete_fields = ("case", "receiver")


@admin.register(OffboardingNote)
class OffboardingNoteAdmin(admin.ModelAdmin):
    list_display = ("case", "author", "created_at", "is_internal", "_body_preview")
    list_filter = ("is_internal",)
    search_fields = ("body", "case__resignation__employee__email")
    autocomplete_fields = ("case", "author")
    readonly_fields = ("created_at",)

    @admin.display(description="Note")
    def _body_preview(self, obj):
        return (obj.body[:80] + "…") if len(obj.body) > 80 else obj.body
