from django.apps import AppConfig


class OffboardingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "offboarding"
    verbose_name = "Offboarding"

    def ready(self):
        from offboarding import signals  # noqa: F401
