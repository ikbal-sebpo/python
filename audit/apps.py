from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audit"
    verbose_name = "Audit"

    def ready(self):
        # Register the auth-event receivers (imported for the side effect).
        from audit import signals  # noqa: F401
