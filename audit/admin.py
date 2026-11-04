from django.contrib import admin

from audit.models import AccessLog


@admin.register(AccessLog)
class AccessLogAdmin(admin.ModelAdmin):
    """Read-only. Rows are written by audit.middleware / audit.signals and
    removed only by prune_access_log — same posture as ScheduledTaskRunAdmin."""

    list_display = (
        "occurred_at", "actor_label", "action", "data_class", "subject_label",
        "route", "method", "status_code", "ip_address", "is_htmx",
    )
    list_filter = ("data_class", "action", "status_code", "is_htmx", "occurred_at")
    search_fields = ("actor_label", "subject_label", "route", "path", "object_type", "object_id", "ip_address")
    date_hierarchy = "occurred_at"
    list_select_related = ("actor", "subject")
    fields = (
        "occurred_at", "actor", "actor_label", "session", "ip_address", "user_agent",
        "method", "route", "path", "query", "status_code", "duration_ms", "is_htmx",
        "data_class", "action", "subject", "subject_label", "object_type", "object_id", "detail",
    )

    def get_readonly_fields(self, request, obj=None):
        return self.fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
