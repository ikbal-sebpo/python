from django.contrib import admin
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from leave.models import (
    CplGrant,
    EncashmentRequest,
    LeaveApplication,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
    LegacyAnnualLeaveRecord,
)
from leave.services import credit_manual_cpl_grant
from roster.models import ShiftType


class LeavePolicyResource(resources.ModelResource):
    """Sync LeavePolicy rows between dev and prod.

    Matched by `shift_type` (joined via name) so PK differences across
    environments don't break the import. Audit fields are excluded.
    """

    shift_type = fields.Field(
        column_name="shift_type",
        attribute="shift_type",
        widget=ForeignKeyWidget(ShiftType, field="name"),
    )

    class Meta:
        model = LeavePolicy
        import_id_fields = ("shift_type",)
        fields = (
            "shift_type",
            "max_days_per_year", "is_paid",
            "applies_to_gender", "min_service_months",
            "max_surviving_children", "prorate_on_join",
            "bridges_sandwich", "cycle",
            "accrual_method", "accrual_ratio_denominator",
            "carry_forward_pct", "max_carry_forward_days",
            "freezes_entitlement_at_cap", "encash_carry_forward_excess",
            "allow_advance_against_entitlement",
            "cert_required_min_days",
            "cpl_grant_on_holiday_work", "cpl_use_within_days",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True


@admin.register(LeavePolicy)
class LeavePolicyAdmin(ImportExportModelAdmin):
    resource_classes = [LeavePolicyResource]
    list_display = (
        "shift_type", "max_days_per_year", "is_paid",
        "applies_to_gender", "min_service_months",
        "cert_required_min_days", "carry_forward_pct",
        "accrual_method", "bridges_sandwich",
    )
    list_filter = (
        "is_paid", "applies_to_gender", "cycle", "accrual_method",
        "freezes_entitlement_at_cap", "encash_carry_forward_excess",
        "allow_advance_against_entitlement",
    )
    search_fields = ("shift_type__name",)
    autocomplete_fields = ("shift_type",)

    fieldsets = (
        ("Identity", {"fields": ("shift_type",)}),
        ("Entitlement", {"fields": ("max_days_per_year", "is_paid")}),
        ("Eligibility gates", {"fields": (
            "applies_to_gender", "min_service_months", "max_surviving_children",
        )}),
        ("Pro-rating + cycle", {"fields": ("prorate_on_join", "cycle")}),
        ("Accrual", {"fields": ("accrual_method", "accrual_ratio_denominator")}),
        ("Sandwich rule", {"fields": ("bridges_sandwich",)}),
        ("Carry-forward + encashment", {"fields": (
            "carry_forward_pct", "max_carry_forward_days",
            "freezes_entitlement_at_cap", "encash_carry_forward_excess",
        )}),
        ("Advance requests", {"fields": ("allow_advance_against_entitlement",)}),
        ("Documentation", {"fields": ("cert_required_min_days",)}),
        ("Compensatory", {"fields": ("cpl_grant_on_holiday_work", "cpl_use_within_days")}),
        ("Audit", {"fields": ("added_by", "added_at", "updated_by", "updated_at"),
                    "classes": ("collapse",)}),
    )
    readonly_fields = ("added_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(LeaveApplication)
class LeaveApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "leave_type", "start_date", "end_date",
        "effective_days", "is_half_day", "status", "applied_at", "processed_by",
        "approved_over_balance",
    )
    list_filter = ("status", "leave_type", "is_half_day", "start_date", "approved_over_balance")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
        "employee__employee_id", "note", "process_note",
    )
    autocomplete_fields = ("employee", "leave_type", "applied_by", "processed_by")
    date_hierarchy = "start_date"
    readonly_fields = (
        "applied_at", "processed_at", "added_at", "updated_at",
        "working_days", "sandwich_days", "effective_days",
        "approved_over_balance", "balance_shortfall_days",
    )

    fieldsets = (
        ("Request", {"fields": (
            "employee", "leave_type", "start_date", "end_date",
            "is_half_day", "half_day_period", "note", "attachment",
        )}),
        ("Billing breakdown (computed at approval)", {"fields": (
            "working_days", "sandwich_days", "effective_days",
        )}),
        ("Workflow", {"fields": (
            "status", "applied_by", "applied_at",
            "processed_by", "processed_at", "process_note",
            "approved_over_balance", "balance_shortfall_days",
        )}),
        ("Audit", {"fields": ("added_by", "added_at", "updated_by", "updated_at"),
                    "classes": ("collapse",)}),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
            if not obj.applied_by_id:
                obj.applied_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(LeaveBalance)
class LeaveBalanceAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "leave_policy", "year",
        "opening", "accrued", "carried_forward", "used", "encashed",
        "available_display", "entitlement_frozen",
    )
    list_filter = ("year", "leave_policy", "entitlement_frozen")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
    )
    autocomplete_fields = ("employee", "leave_policy")
    readonly_fields = ("added_at", "updated_at", "available_display")

    @admin.display(description="Available")
    def available_display(self, obj):
        return obj.available


@admin.register(LeaveBalanceTransaction)
class LeaveBalanceTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "occurred_at", "balance", "delta", "reason", "application", "created_by",
    )
    list_filter = ("reason",)
    search_fields = (
        "balance__employee__email", "balance__employee__first_name",
        "balance__employee__last_name", "note",
    )
    autocomplete_fields = ("balance", "application", "created_by")
    readonly_fields = ("occurred_at",)
    date_hierarchy = "occurred_at"


@admin.register(EncashmentRequest)
class EncashmentRequestAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "leave_policy", "year",
        "days_requested", "days_approved", "status",
        "requested_at", "processed_by",
    )
    list_filter = ("status", "leave_policy", "year")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
    )
    autocomplete_fields = ("employee", "leave_policy", "processed_by")
    date_hierarchy = "requested_at"
    readonly_fields = ("requested_at", "added_at", "updated_at")

    fieldsets = (
        ("Request", {"fields": ("employee", "leave_policy", "year", "days_requested")}),
        ("Workflow", {"fields": (
            "status", "days_approved", "requested_at", "processed_by", "processed_at", "note",
        )}),
        ("Audit", {"fields": ("added_by", "added_at", "updated_by", "updated_at"),
                    "classes": ("collapse",)}),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(CplGrant)
class CplGrantAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "department", "work_date", "work_context", "status", "source",
        "consumed_date", "added_at",
    )
    list_filter = ("status", "source", "work_context", "department")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
    )
    autocomplete_fields = (
        "employee", "department", "holiday", "work_shift", "consumption_shift",
        "accrual_transaction", "consumption_transaction",
    )
    date_hierarchy = "work_date"
    readonly_fields = ("added_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        # A grant created here has no Shift to key a signal off of (AUTO
        # grants credit via leave/signals.py; this is the manual/admin
        # path) — credit_manual_cpl_grant is idempotent, so it's safe to
        # call on every save, not just creation.
        credit_manual_cpl_grant(obj, created_by=request.user)


@admin.register(LegacyAnnualLeaveRecord)
class LegacyAnnualLeaveRecordAdmin(admin.ModelAdmin):
    """Pre-Orbis AL position as HR recorded it — Service Book only, never a
    ledger credit (docs/LEGACY_60_IMPORT_PLAN.md §4)."""

    list_display = (
        "employee", "as_of", "accrued_before_year", "taken_in_year",
        "balance_in_year", "capped_balance", "note", "updated_at",
    )
    list_filter = ("as_of",)
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee",)
    readonly_fields = ("added_by", "added_at", "updated_by", "updated_at", "source_file")
