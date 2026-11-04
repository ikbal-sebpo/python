"""Read-only admin over the Drive index — what is on Drive, under which blob.

Nothing here is editable: the index is written only by the storage backend
and ``rebuild_drive_index``. Shows no file content (there is none to show —
every blob is ciphertext).
"""

from django.contrib import admin

from media_store.models import DriveObject


@admin.register(DriveObject)
class DriveObjectAdmin(admin.ModelAdmin):
    list_display = ("name", "mime", "size", "drive_name", "key_fingerprint", "created_at")
    list_filter = ("mime", "key_fingerprint")
    search_fields = ("name", "drive_name", "file_id")
    ordering = ("-created_at",)
    readonly_fields = ("name", "file_id", "drive_name", "size", "md5", "mime", "key_fingerprint", "created_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
