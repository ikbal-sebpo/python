"""Admin for the database-backed beat schedule.

Replaces django_celery_beat's stock PeriodicTask / CrontabSchedule admins with
versions tuned for how Orbis actually uses them: every job is a cron-shaped
``core.tasks.run_management_command`` dispatch, so the raw ``task`` column is
identical on every row and the interesting value (the management command) is
buried inside the JSON ``args``.

`admin.site` here is core.admin_site.OrbisAdminSite (wired via
core.apps.OrbisAdminConfig), so unregister/re-register lands on the custom site.

Ported from smsbot/scheduler/admin.py. See docs/SCHEDULER_ADMIN_PLAN.md.
"""

from __future__ import annotations

import datetime
import json
import re

from django.contrib import admin, messages
from django.contrib.admin.sites import NotRegistered
from django.core.exceptions import PermissionDenied
from django.core.management import get_commands
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django_celery_beat.admin import CrontabScheduleAdmin, PeriodicTaskAdmin
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)

from scheduler.forms import HourFirstCrontabForm, PeriodicTaskForm, RunWithArgumentsForm
from scheduler.models import ScheduledTaskRun, TaskOrder
from scheduler.schedule import BEAT_TASK

DAY_NAME = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
ALIAS_TO_NUM = {
    "sun": 7, "0": 7, "7": 7,
    "mon": 1, "1": 1,
    "tue": 2, "2": 2,
    "wed": 3, "3": 3,
    "thu": 4, "4": 4,
    "fri": 5, "5": 5,
    "sat": 6, "6": 6,
}


def _parse_day_token(token: str) -> list[int]:
    """Expand one day_of_week token ('1', 'mon', '1-5', 'mon-fri') to ints."""
    token = token.strip().lower()
    if not token:
        return []
    if "-" in token:
        start, _, end = token.partition("-")
        first = ALIAS_TO_NUM.get(start.strip())
        last = ALIAS_TO_NUM.get(end.strip())
        if first and last:
            # Inclusive range; wrap-around (fri-mon) isn't supported.
            return list(range(min(first, last), max(first, last) + 1))
        return []
    value = ALIAS_TO_NUM.get(token)
    return [value] if value else []


def _dow_to_names(day_of_week: str | None) -> str:
    """'1,2,3,4,5' → 'Mon–Fri'; '0,6' → 'Sat,Sun'; '*' → '*'."""
    if not day_of_week or day_of_week.strip() == "*":
        return "*"

    values: list[int] = []
    for part in day_of_week.split(","):
        values += _parse_day_token(part)

    unique = sorted(set(values))
    if not unique:
        return "*"

    # Compress consecutive runs so Mon..Fri reads as one range.
    groups: list[tuple[int, int]] = []
    start = previous = unique[0]
    for day in unique[1:]:
        if day == previous + 1:
            previous = day
        else:
            groups.append((start, previous))
            start = previous = day
    groups.append((start, previous))

    return ",".join(
        DAY_NAME[a] if a == b else f"{DAY_NAME[a]}–{DAY_NAME[b]}" for a, b in groups
    )


def _format_time_12h(hour: str, minute: str) -> str | None:
    """'0','15' → '12:15 AM'. None when either side isn't a plain number."""
    if not re.fullmatch(r"\d{1,2}", hour or "") or not re.fullmatch(r"\d{1,2}", minute or ""):
        return None
    try:
        moment = datetime.time(hour=int(hour), minute=int(minute))
    except ValueError:
        return None
    return moment.strftime("%I:%M %p").lstrip("0")


def _month_day_suffix(crontab: CrontabSchedule) -> str:
    """Narrow a daily-looking time to 'on the 1st' / 'on 1 Jan' when it isn't."""
    day = (crontab.day_of_month or "*").strip()
    month = (crontab.month_of_year or "*").strip()
    if day == "*" and month == "*":
        return ""
    if month == "*":
        return f"on day {day}"
    if re.fullmatch(r"\d{1,2}", month):
        name = datetime.date(2000, int(month), 1).strftime("%b")
        return f"on {day} {name}"
    return f"on day {day} of month {month}"


@admin.display(description="Schedule", ordering="crontab__hour")
def format_schedule(obj: PeriodicTask) -> str:
    """'10:00 PM   Fri', falling back to the raw cron for '*/5'-style values.

    The cadence half reads as words rather than cron: 'Daily' beats a bare '*',
    and a month/day narrowing replaces it ('on 1 Jan') instead of being tacked
    on after one.
    """
    crontab = obj.crontab
    if crontab:
        time_str = _format_time_12h(crontab.hour, crontab.minute)
        if time_str:
            days = _dow_to_names(crontab.day_of_week)
            suffix = _month_day_suffix(crontab)
            if days == "*":
                cadence = suffix or "Daily"
            else:
                cadence = f"{days}   {suffix}" if suffix else days
            return f"{time_str}   {cadence}"
        # Non-numeric cron — show it verbatim, minus the timezone suffix.
        return str(crontab).split(" (")[0]
    if obj.interval:
        return str(obj.interval)
    if obj.clocked:
        return str(obj.clocked)
    if obj.solar:
        return str(obj.solar)
    return "—"


@admin.display(description="Command", ordering="args")
def format_command(obj: PeriodicTask) -> str:
    """The manage.py command inside args — the only part that varies per row."""
    if obj.task != "core.tasks.run_management_command":
        return "—"
    try:
        args = json.loads(obj.args or "[]")
    except (TypeError, ValueError):
        return "(unparseable args)"
    if not isinstance(args, list) or not args:
        return "(no command)"
    return " ".join(str(a) for a in args)


def command_args(obj: PeriodicTask) -> list[str] | None:
    """The row's args as a command line, or None when it isn't a
    run_management_command dispatch of a command that exists."""
    if obj.task != BEAT_TASK:
        return None
    try:
        args = json.loads(obj.args or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(args, list) or not args or str(args[0]) not in get_commands():
        return None
    return [str(a) for a in args]


@admin.display(description="Rank", ordering="order_meta__rank")
def get_rank(obj: PeriodicTask):
    return getattr(getattr(obj, "order_meta", None), "rank", "")


class TaskOrderInline(admin.StackedInline):
    model = TaskOrder
    extra = 0
    verbose_name_plural = "Admin list position"


# --- Periodic tasks ------------------------------------------------------

try:
    admin.site.unregister(PeriodicTask)
except NotRegistered:
    pass


@admin.register(PeriodicTask)
class OrbisPeriodicTaskAdmin(PeriodicTaskAdmin):
    """Subclasses the stock admin to keep its "Run selected tasks" action.

    That action dispatches immediately with the right periodic_task_name
    header, which is how an ops re-run (e.g. sync_attendance_punches after a
    TimeLy outage) happens without SSH. "Run once with arguments" is the same
    dispatch with extra arguments appended for that one run (a resync of a
    specific date), so nobody edits ``args`` and pins the nightly to it.
    """

    form = PeriodicTaskForm
    list_display = (
        "name",
        format_command,
        "enabled",
        format_schedule,
        get_rank,
        "last_run_at",
        "total_run_count",
    )
    # The stock admin hangs a date_hierarchy off start_time, which Orbis never
    # sets — it would render a permanently empty drill-down.
    date_hierarchy = None
    list_filter = ("enabled", "crontab__hour")
    search_fields = ("name", "task", "args", "description")
    ordering = ("order_meta__rank", "name")
    inlines = [TaskOrderInline]
    list_select_related = ("crontab", "order_meta")

    fieldsets = (
        (
            None,
            {
                "fields": ("name", "task", ("enabled", "one_off"), "args", "kwargs"),
                "description": (
                    "Orbis jobs all use core.tasks.run_management_command; the "
                    "management command goes in <code>args</code> as a JSON "
                    "list. See docs/SCHEDULER_ADMIN_PLAN.md."
                ),
            },
        ),
        (
            "Schedule",
            {
                "fields": ("crontab",),
                "description": (
                    "Cron only — interval / solar / clocked schedules are "
                    "intentionally not exposed. Times are read in the "
                    "crontab's own timezone (Asia/Dhaka)."
                ),
            },
        ),
        ("Notes", {"fields": ("description",)}),
        (
            "Execution (read-only)",
            {
                "fields": ("last_run_at", "total_run_count", "date_changed"),
                "classes": ("collapse",),
            },
        ),
    )
    readonly_fields = ("last_run_at", "total_run_count", "date_changed")
    actions = [*PeriodicTaskAdmin.actions, "run_with_arguments"]
    change_form_template = "admin/scheduler/periodictask/change_form.html"

    def get_urls(self):
        return [
            path(
                "<int:pk>/run-with-arguments/",
                self.admin_site.admin_view(self.run_with_arguments_view),
                name="scheduler_periodictask_run_with_arguments",
            ),
            *super().get_urls(),
        ]

    @admin.action(description="Run once with arguments…", permissions=["change"])
    def run_with_arguments(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request, "Select exactly one task to run with arguments.", messages.WARNING,
            )
            return None
        return redirect("admin:scheduler_periodictask_run_with_arguments", queryset.get().pk)

    def run_with_arguments_view(self, request, pk):
        task = get_object_or_404(PeriodicTask, pk=pk)
        if not self.has_change_permission(request, task):
            raise PermissionDenied
        change_url = reverse("admin:django_celery_beat_periodictask_change", args=[task.pk])
        base = command_args(task)
        if base is None:
            self.message_user(
                request, f"“{task.name}” isn't a management-command task.", messages.ERROR,
            )
            return redirect(change_url)

        form = RunWithArgumentsForm(request.POST or None, command_args=base)
        if request.method == "POST" and form.is_valid():
            from core.tasks import run_management_command

            full = [*base, *form.cleaned_data["extra_args"]]
            # Same header the stock "Run selected tasks" action sets, so the
            # run lands in ScheduledTaskRun under this task's name — and the
            # recorded command line shows the extra arguments.
            run_management_command.apply_async(
                args=full,
                queue=task.queue or None,
                headers={"periodic_task_name": task.name},
            )
            self.message_user(
                request,
                f"Dispatched “{' '.join(full)}”. Its result appears in Scheduled task runs.",
                messages.SUCCESS,
            )
            return redirect(change_url)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "original": task,
            "title": f"Run “{task.name}” once with arguments",
            "command_line": " ".join(base),
            "form": form,
            "change_url": change_url,
        }
        return TemplateResponse(
            request, "admin/scheduler/periodictask/run_with_arguments.html", context,
        )

    class Media:
        css = {"all": ("scheduler/css/admin_custom.css",)}


# --- Crontab schedules ---------------------------------------------------

class OrbisCrontabScheduleAdmin(CrontabScheduleAdmin):
    form = HourFirstCrontabForm
    readonly_fields = ("human_readable",)
    # `fields`, not `fieldsets` — a fieldset containing a readonly non-model
    # field trips admin.E005 here.
    fields = (
        "human_readable",
        "hour",
        "minute",
        "day_of_month",
        "month_of_year",
        "day_of_week",
        "timezone",
    )

    @admin.display(description="Reads as")
    def human_readable(self, obj: CrontabSchedule) -> str:
        return str(obj) if obj and obj.pk else "—"


try:
    admin.site.unregister(CrontabSchedule)
except NotRegistered:
    pass
admin.site.register(CrontabSchedule, OrbisCrontabScheduleAdmin)

# Orbis exposes cron schedules only (see the PeriodicTask fieldsets), so the
# other three schedule models are noise in the admin index.
for _unused_model in (IntervalSchedule, SolarSchedule, ClockedSchedule):
    try:
        admin.site.unregister(_unused_model)
    except NotRegistered:
        pass


# --- Run history ---------------------------------------------------------

@admin.register(ScheduledTaskRun)
class ScheduledTaskRunAdmin(admin.ModelAdmin):
    """Read-only execution log. Rows are written by scheduler/monitoring.py."""

    list_display = (
        "periodic_task_name",
        "status_badge",
        "started_at",
        "duration_label",
        "total_records",
        "failed_records",
    )
    list_filter = ("status", "periodic_task_name", "started_at")
    search_fields = ("periodic_task_name", "task", "celery_task_id", "error_message", "output")
    date_hierarchy = "started_at"
    fields = (
        "periodic_task_name",
        "task",
        "celery_task_id",
        "status",
        "started_at",
        "finished_at",
        "duration_label",
        "total_records",
        "failed_records",
        "summary",
        "output",
        "error_message",
        "created_at",
    )

    def get_readonly_fields(self, request, obj=None):
        return self.fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Viewable, never editable — it's an audit log.
        return False

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj: ScheduledTaskRun) -> str:
        colors = {
            ScheduledTaskRun.STATUS_SUCCESS: "#1a7f37",
            ScheduledTaskRun.STATUS_PARTIAL_FAILURE: "#9a6700",
            ScheduledTaskRun.STATUS_FAILURE: "#cf222e",
        }
        return format_html(
            '<b style="color:{}">{}</b>',
            colors.get(obj.status, "#57606a"),
            obj.get_status_display(),
        )

    @admin.display(description="Duration")
    def duration_label(self, obj: ScheduledTaskRun) -> str:
        delta = obj.duration
        if delta is None or delta.total_seconds() < 0:
            return "—"
        total = int(delta.total_seconds())
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
