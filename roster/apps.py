from django.apps import AppConfig


class RosterConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'roster'
    verbose_name = 'Roster'

    def ready(self):
        # Register signal receivers (imported for the side effect).
        from roster import signals  # noqa: F401
