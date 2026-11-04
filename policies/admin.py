"""Admin for the policies app.

Policy with version + acknowledgement inlines; the event ledger is exposed
read-only (it's an append-only audit trail — never hand-edited)."""

from __future__ import annotations

from django.contrib import admin

from policies.models import (
    Policy,
    PolicyAcknowledgement,
    PolicyAcknowledgementEvent,
    PolicyCounter,
    PolicyVersion,
)


class PolicyVersionInline(admin.TabularInline):
    model = PolicyVersion
    extra = 0
    fields = ("version", "pdf", "is_current", "requires_reack", "change_note", "published_at")
    readonly_fields = ("version", "published_at")
    ordering = ("-version",)


class PolicyAcknowledgementInline(admin.TabularInline):
    model = PolicyAcknowledgement
    extra = 0
    fields = ("employee", "status", "version", "signed_at", "reminder_count", "notified_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Policy)
class PolicyAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "status", "owner", "adherent_count", "signed_count", "published_at")
    list_filter = ("status", "category")
    search_fields = ("key", "title", "description", "owner__email")
    readonly_fields = ("key", "published_at", "added_by", "added_at", "updated_by", "updated_at")
    filter_horizontal = ("target_groups", "custom_adherents")
    inlines = (PolicyVersionInline, PolicyAcknowledgementInline)


@admin.register(PolicyAcknowledgement)
class PolicyAcknowledgementAdmin(admin.ModelAdmin):
    list_display = ("policy", "employee", "status", "version", "signed_at", "reminder_count")
    list_filter = ("status", "policy")
    search_fields = ("policy__key", "policy__title", "employee__email")
    readonly_fields = (
        "policy", "employee", "version", "status", "signed_at", "signed_ip",
        "signed_user_agent", "notified_at", "reminder_count",
        "added_by", "added_at", "updated_by", "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(PolicyAcknowledgementEvent)
class PolicyAcknowledgementEventAdmin(admin.ModelAdmin):
    list_display = ("acknowledgement", "event", "version", "occurred_at", "actor")
    list_filter = ("event",)
    search_fields = ("acknowledgement__policy__key", "acknowledgement__employee__email")
    readonly_fields = (
        "acknowledgement", "event", "version", "occurred_at", "actor", "ip", "note",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PolicyCounter)
class PolicyCounterAdmin(admin.ModelAdmin):
    list_display = ("id", "last_value")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
