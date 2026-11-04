from django.apps import AppConfig


class SchedulerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scheduler"
    verbose_name = "Scheduler"

    def ready(self):
        # Connects the Celery task_prerun/task_postrun handlers that write
        # ScheduledTaskRun rows. Import-only side effect, same shape as
        # policies/employees signal wiring.
        from scheduler import monitoring  # noqa: F401
