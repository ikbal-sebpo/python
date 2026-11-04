from django.apps import AppConfig


class DynamicMenuConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dynamic_menu'

    def ready(self):
        from dynamic_menu import signals  # noqa: F401 — cache invalidation
