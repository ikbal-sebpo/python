from django.apps import AppConfig


class LeaveConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'leave'
    verbose_name = 'Leave'

    def ready(self):
        from leave import signals  # noqa: F401
