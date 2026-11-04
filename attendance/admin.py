from django.contrib import admin

from attendance.models import AttendanceCorrection, AttendancePunch


@admin.register(AttendancePunch)
class AttendancePunchAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "date", "check_in", "check_out", "time_in_office",
        "office_bucket", "punch_count", "source", "synced_at",
    )
    list_filter = ("office_bucket", "source", "date")
    search_fields = (
        "employee__email", "employee__first_name",
        "employee__last_name", "employee__employee_id",
    )
    autocomplete_fields = ("employee",)
    date_hierarchy = "date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AttendanceCorrection)
class AttendanceCorrectionAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "date", "status", "requested_check_in", "requested_check_out",
        "requested_by", "processed_by", "processed_at",
    )
    list_filter = ("status", "date")
    search_fields = (
        "employee__email", "employee__first_name",
        "employee__last_name", "employee__employee_id",
    )
    autocomplete_fields = ("employee", "punch", "requested_by", "processed_by")
    date_hierarchy = "date"
