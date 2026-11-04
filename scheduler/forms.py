"""Admin forms for the periodic-task screens.

These are Django-admin forms, so they use admin widgets and NOT the Timely
`form-input` / Flatpickr conventions from CLAUDE.md — those govern the
first-party app UI, which the admin doesn't share a stylesheet with.
"""

from __future__ import annotations

from django import forms
from django_celery_beat.admin import TaskSelectWidget
from django_celery_beat.models import CrontabSchedule, PeriodicTask


class PeriodicTaskForm(forms.ModelForm):
    """Registered-task dropdown plus compact JSON fields.

    django_celery_beat's own admin form does the same TaskSelectWidget swap;
    declaring it here keeps it when we override the rest of the form.
    """

    class Meta:
        model = PeriodicTask
        fields = "__all__"
        widgets = {
            "task": TaskSelectWidget,
            "args": forms.Textarea(attrs={"rows": 2, "cols": 80}),
            "kwargs": forms.Textarea(attrs={"rows": 2, "cols": 80}),
            "description": forms.Textarea(attrs={"rows": 3, "cols": 80}),
        }
        help_texts = {
            "args": (
                'JSON list of positional arguments. For an Orbis nightly this '
                'is the management command: ["sync_attendance_punches"].'
            ),
            "enabled": "Untick to pause this job. Beat picks the change up within ~5s.",
        }


class HourFirstCrontabForm(forms.ModelForm):
    """Hour before minute — the order people actually think in."""

    class Meta:
        model = CrontabSchedule
        fields = (
            "hour",
            "minute",
            "day_of_month",
            "month_of_year",
            "day_of_week",
            "timezone",
        )
        help_texts = {
            "hour": "Cron hours to run. '*' for every hour (e.g. '0', '8,20').",
            "minute": "Cron minutes to run. '*' for every minute (e.g. '0', '15,45').",
            "day_of_week": "0 or 7 = Sunday … 5 = Friday. '*' for every day.",
            "timezone": "Orbis schedules run in Asia/Dhaka.",
        }


class RunWithArgumentsForm(forms.Form):
    """Extra command-line arguments for one ad-hoc run of a periodic task.

    Appended to the row's own ``args`` for that one dispatch only — the row
    itself is never edited, so the next scheduled run is unaffected.
    """

    extra_args = forms.CharField(
        label="Extra arguments",
        widget=forms.TextInput(attrs={"size": 60, "autofocus": True}),
        help_text=(
            "Typed as on the command line, e.g. <code>--date 2026-10-21</code> "
            "or <code>--from 2026-10-21 --to 2026-10-22</code>."
        ),
    )

    def __init__(self, *args, command_args: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.command_args = command_args

    def clean_extra_args(self) -> list[str]:
        import shlex

        from django.core.management import get_commands, load_command_class
        from django.core.management.base import CommandError

        try:
            extra = shlex.split(self.cleaned_data["extra_args"])
        except ValueError as exc:
            raise forms.ValidationError(f"Couldn't read the arguments: {exc}.")
        if not extra:
            raise forms.ValidationError("Enter at least one argument.")

        # Parse against the command's own argparse definition so a typo'd flag
        # fails here, not as a FAILURE row in the run log a minute later.
        name, *base = self.command_args
        command = load_command_class(get_commands()[name], name)
        parser = command.create_parser("manage.py", name)
        try:
            parser.parse_args([*base, *extra])
        except CommandError as exc:
            raise forms.ValidationError(str(exc))
        return extra
