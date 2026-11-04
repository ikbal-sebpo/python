from django.apps import AppConfig


class PoliciesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "policies"
    verbose_name = "Policies"

    def ready(self):
        from policies import signals  # noqa: F401
