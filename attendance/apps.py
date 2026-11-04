from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "attendance"
    # Display-only rename (2026-09-07): the app/models/URLs keep the
    # `attendance` name, but everything user-facing — sidebar, page titles,
    # and now the Django admin app list — reads "Timesheet".
    verbose_name = "Timesheet"
