from django.contrib import admin
from django.db.models import F
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from roster.models import (
    Holiday, Roster, RosterEmailConfig, RosterEmailLog, RosterEmailMember,
    Shift, ShiftAttribute, ShiftChangeLog, ShiftChangeRequest, ShiftType,
)


# ---------------------------------------------------------------------------
# Import-export resources
#
# All resources use natural keys (e.g., ShiftType.name) instead of the
# auto-incrementing PK for `import_id_fields`. PKs differ between local and
# prod, so matching by name lets a CSV exported on a dev box import cleanly
# into production. Audit fields (added_by/added_at/updated_by/updated_at)
# are excluded — they auto-fill on save.
# ---------------------------------------------------------------------------

class ShiftTypeResource(resources.ModelResource):
    class Meta:
        model = ShiftType
        import_id_fields = ("name",)
        fields = (
            "name", "description", "category", "is_working_shift",
            "shift_timezone", "shift_start_time", "shift_end_time", "sort_order",
            "color", "has_attribute", "is_active",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True


class ShiftAttributeResource(resources.ModelResource):
    shift_type = fields.Field(
        column_name="shift_type",
        attribute="shift_type",
        widget=ForeignKeyWidget(ShiftType, field="name"),
    )

    class Meta:
        model = ShiftAttribute
        import_id_fields = ("shift_type", "name")
        fields = (
            "shift_type", "name", "start_time", "end_time", "description", "sort_order", "is_active",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True


class HolidayResource(resources.ModelResource):
    class Meta:
        model = Holiday
        import_id_fields = ("date",)
        fields = ("date", "name", "is_active")
        export_order = fields
        skip_unchanged = True
        report_skipped = True


# ---------------------------------------------------------------------------
# Admin registrations
# ---------------------------------------------------------------------------

class ShiftAttributeInline(admin.TabularInline):
    model = ShiftAttribute
    extra = 0
    fields = ("name", "start_time", "end_time", "sort_order", "description", "is_active")


@admin.register(ShiftType)
class ShiftTypeAdmin(ImportExportModelAdmin):
    resource_classes = [ShiftTypeResource]
    list_display = (
        "id", "name", "category", "shift_start_time", "shift_end_time",
        "sort_order", "is_working_shift", "is_active",
    )
    list_filter = ("category", "is_active", "is_working_shift")
    search_fields = ("name", "description")
    ordering = (F("sort_order").asc(nulls_last=True), "name")
    inlines = [ShiftAttributeInline]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ShiftAttribute)
class ShiftAttributeAdmin(ImportExportModelAdmin):
    resource_classes = [ShiftAttributeResource]
    list_display = ("name", "shift_type", "start_time", "end_time", "sort_order", "is_active")
    list_filter = ("shift_type", "is_active", "departments")
    search_fields = ("name", "shift_type__name")
    filter_horizontal = ("departments",)
    ordering = (
        F("shift_type__sort_order").asc(nulls_last=True),
        "shift_type__name",
        F("sort_order").asc(nulls_last=True),
        "start_time",
        "name",
    )
    actions = ("activate_shift_attributes", "deactivate_shift_attributes")

    @admin.action(description="Mark selected shift attributes as active")
    def activate_shift_attributes(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(
            request,
            f"{updated} shift attribute{'s' if updated != 1 else ''} marked active.",
        )

    @admin.action(description="Mark selected shift attributes as inactive")
    def deactivate_shift_attributes(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(
            request,
            f"{updated} shift attribute{'s' if updated != 1 else ''} marked inactive.",
        )


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "date", "shift_type", "shift_attribute",
        "job_location", "work_location", "note",
    )
    list_filter = ("shift_type", "job_location", "work_location", "date")
    search_fields = (
        "employee__email", "employee__first_name",
        "employee__last_name", "employee__employee_id",
    )
    autocomplete_fields = (
        "employee", "shift_type", "shift_attribute", "job_location", "work_location",
    )
    date_hierarchy = "date"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Roster)
class RosterAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "weekday", "shift_type", "shift_attribute",
        "job_location", "work_location",
    )
    list_filter = ("weekday", "shift_type", "job_location", "work_location")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")
    autocomplete_fields = (
        "employee", "shift_type", "shift_attribute", "job_location", "work_location",
    )


@admin.register(ShiftChangeLog)
class ShiftChangeLogAdmin(admin.ModelAdmin):
    """Read-only audit trail — unlike dsndd's bare `register(ShiftChangeLog)`
    with no list_display/filters, this gives HR/admins real search/filter
    access immediately."""

    list_display = ("employee", "date", "action_type", "changed_by", "changed_at")
    list_filter = ("action_type", "changed_at")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
        "changed_by__email", "changed_by__first_name", "changed_by__last_name",
    )
    autocomplete_fields = (
        "employee", "changed_by", "shift",
        "previous_shift_type", "new_shift_type",
        "previous_shift_attribute", "new_shift_attribute",
        "previous_job_location", "new_job_location",
        "previous_work_location", "new_work_location",
    )
    date_hierarchy = "changed_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ShiftChangeRequest)
class ShiftChangeRequestAdmin(admin.ModelAdmin):
    """Read-mostly: the request/decide flow lives at /roster/change-requests/.
    Status is editable here only as an escape hatch (e.g. HR cancelling a
    stale pending row on someone's behalf); editing it does NOT write the
    roster — approvals must go through the app so `apply_shift_change` runs."""

    list_display = (
        "employee", "date", "original_label", "original_location",
        "requested_label", "requested_location", "status", "applied_by", "applied_at",
    )
    list_filter = ("status", "requested_location", "date")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
        "employee__employee_id",
    )
    autocomplete_fields = ("employee", "applied_by", "processed_by")
    readonly_fields = (
        "original_shift_type", "original_shift_attribute", "original_was_concrete",
        "original_location", "requested_shift_type", "requested_shift_attribute",
        "requested_location", "reason", "applied_by", "applied_at",
        "added_by", "added_at", "updated_by", "updated_at",
    )
    date_hierarchy = "date"

    def has_add_permission(self, request):
        return False


@admin.register(Holiday)
class HolidayAdmin(ImportExportModelAdmin):
    resource_classes = [HolidayResource]
    list_display = ("date", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    date_hierarchy = "date"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ---------------------------------------------------------------------------
# Weekly roster email
# ---------------------------------------------------------------------------

class RosterEmailMemberInline(admin.TabularInline):
    model = RosterEmailMember
    extra = 0
    autocomplete_fields = ("employee",)


@admin.register(RosterEmailConfig)
class RosterEmailConfigAdmin(admin.ModelAdmin):
    list_display = ("team", "is_active", "send_team_copy", "sender", "poc", "to_emails", "updated_at", "updated_by")
    list_filter = ("is_active",)
    search_fields = ("team__name", "to_emails", "cc_emails")
    autocomplete_fields = ("team", "poc")
    readonly_fields = ("updated_at", "updated_by")
    inlines = [RosterEmailMemberInline]


@admin.register(RosterEmailLog)
class RosterEmailLogAdmin(admin.ModelAdmin):
    list_display = ("team", "week_start", "kind", "trigger", "status", "to_emails", "sent_by", "created_at", "sent_at")
    list_filter = ("kind", "trigger", "status")
    search_fields = ("team__name", "to_emails", "subject")
    date_hierarchy = "week_start"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
