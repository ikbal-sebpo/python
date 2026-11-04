"""Code-level record of Orbis's periodic jobs.

Until 2026-09-29 this lived in ``CELERY_BEAT_SCHEDULE`` in ``core/settings.py``
and beat read it directly. It is now the *seed* for django_celery_beat's
tables: ``manage.py seed_beat_schedule`` (and the data migration that calls it)
creates any missing ``PeriodicTask`` row, and from then on the Django admin is
the source of truth — the seed never overwrites an existing row unless asked.

So this module is documentation plus fresh-install bootstrap, NOT the live
schedule. Retiming a job in production is an admin edit; changing it here only
affects databases that don't have the row yet. See
docs/SCHEDULER_ADMIN_PLAN.md.

Every job dispatches ``core.tasks.run_management_command``, so the work stays a
plain ``manage.py`` command that can be run by hand.

``rank`` orders the admin changelist by time of day rather than alphabetically,
which is what makes the real dependency chain legible: reconcile-cpl-grants
(00:40) and reconcile-shift-leave-usage (00:45) must both precede
close-al-cycles (01:15).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The dispatcher every scheduled job goes through.
BEAT_TASK = "core.tasks.run_management_command"

#: Timezone every seeded crontab is stored with. Matches settings.CELERY_TIMEZONE
#: (which is also django_celery_beat's own default for the field), stated
#: explicitly so a seeded row is never ambiguous.
SCHEDULE_TIMEZONE = "Asia/Dhaka"


@dataclass(frozen=True)
class Job:
    """One seeded periodic job."""

    name: str                       # PeriodicTask.name — the admin label
    command: str                    # manage.py command name
    hour: str
    minute: str
    day_of_week: str = "*"          # 0/7 = Sunday, 5 = Friday
    day_of_month: str = "*"
    month_of_year: str = "*"
    description: str = ""
    args: tuple[str, ...] = ()      # extra argv after the command name
    enabled: bool = True

    @property
    def crontab_kwargs(self) -> dict[str, str]:
        return {
            "minute": self.minute,
            "hour": self.hour,
            "day_of_week": self.day_of_week,
            "day_of_month": self.day_of_month,
            "month_of_year": self.month_of_year,
            "timezone": SCHEDULE_TIMEZONE,
        }

    @property
    def task_args(self) -> list[str]:
        return [self.command, *self.args]


#: Ordered by time of day; ``rank`` is assigned from this order.
JOBS: tuple[Job, ...] = (
    Job(
        name="Deactivate past LWD",
        command="deactivate_past_lwd",
        hour="0", minute="15",
        description=(
            "Deactivate accounts whose last_working_date has passed. "
            "LastWorkingDateMiddleware is the fast path on request; this is "
            "the backstop."
        ),
    ),
    Job(
        name="Monthly leave accrual (dormant)",
        command="apply_monthly_accrual",
        hour="0", minute="30", day_of_month="1",
        description=(
            "Monthly accrual for CALENDAR-cycle worked-day-ratio policies. "
            "Dormant since 2026-09-01: AL's accrued is computed live "
            "(leave.services.live_accrued_al) and AL was the only such "
            "policy. Kept scheduled for a hypothetical future "
            "CALENDAR + WORKED_DAY_RATIO policy."
        ),
    ),
    Job(
        name="Reconcile CpL grants",
        command="reconcile_cpl_grants",
        hour="0", minute="40",
        description=(
            "Grant CpL for WORK-on-Holiday Shift rows written via bulk_create "
            "(bulk roster uploads bypass leave/signals.py on purpose). "
            "AUTO-treatment departments/teams only. Runs just before the "
            "usage job so a same-night bulk import's grants and debits both "
            "land."
        ),
    ),
    Job(
        name="Reconcile shift leave usage",
        command="reconcile_shift_leave_usage",
        hour="0", minute="45",
        description=(
            "Catch-up debit of LeaveBalance for LEAVE-category Shift rows set "
            "directly on the roster. Most are reconciled instantly by "
            "leave/signals.py::sync_shift_leave_ledger; this catches "
            "bulk_create rows. MUST run before close-al-cycles so AL's "
            "carry-forward math reads an accurate `used`."
        ),
    ),
    Job(
        name="Open AL cycles",
        command="open_al_cycles",
        hour="1", minute="0",
        description=(
            "Open each employee's new anniversary AL cycle on their joining "
            "date anniversary."
        ),
    ),
    Job(
        name="Year-end rollover",
        command="apply_year_end_rollover",
        hour="1", minute="0", day_of_month="1,15", month_of_year="1",
        description=(
            "Calendar-year carry-forward for CALENDAR-cycle policies with "
            "carry_forward_pct > 0 — CpL (100%, no expiry) since 2026-10-01. "
            "Excludes AL, which is anniversary-cycle (see close-al-cycles). "
            "Fires Jan 1 and again Jan 15: the command is idempotent (posts "
            "only the delta vs. what's already carried), so the second run "
            "picks up December grants/usage posted after Jan 1, e.g. a late "
            "roster upload."
        ),
    ),
    Job(
        name="Close AL cycles",
        command="close_al_cycles",
        hour="1", minute="15",
        description=(
            "Close each employee's ended anniversary AL cycle: freeze the "
            "live-computed accrued into a permanent number and carry 100% of "
            "unused AL forward, capped at the 60-day lifetime total. Depends "
            "on the two reconcile jobs above having run first."
        ),
    ),
    Job(
        name="Generate night-shift consents",
        command="generate_night_shift_consents",
        hour="1", minute="30", day_of_month="1",
        description=(
            "Bulk-materialise the month's NightShiftConsent rows so HR's "
            "audit list at /employees/night-shift-consent/ is complete "
            "(Bangladesh Labour Act 2006 s.109)."
        ),
    ),
    Job(
        name="Sync line managers",
        command="sync_line_managers",
        hour="2", minute="0",
        description=(
            "Reconcile the derived 'Line Managers' group. employees/signals.py "
            "keeps it live; this catches bulk writes that bypass signals. "
            "Early, so a manager promoted overnight has the menu before the "
            "working day starts."
        ),
    ),
    Job(
        name="Sync supervisors",
        command="sync_supervisors",
        hour="2", minute="10",
        description=(
            "Reconcile the derived 'Supervisors' group from designation "
            "seniority. No instant signal — a promotion lands at the next run."
        ),
    ),
    Job(
        name="Clear expired sessions",
        command="clearsessions",
        hour="3", minute="0",
        description="Django's own django_session cleanup.",
    ),
    Job(
        name="Prune access log",
        command="prune_access_log",
        hour="3", minute="15",
        description=(
            "Delete access-log rows older than ACCESS_LOG_RETENTION_DAYS "
            "(default 730) in batches; the only code path that removes them. "
            "docs/ACCESS_LOG_PLAN.md §3.6."
        ),
    ),
    Job(
        name="Prune media cache",
        command="prune_media_cache",
        hour="3", minute="30",
        description=(
            "LRU-trim the local ciphertext cache of Drive media blobs to "
            "MEDIA_CACHE_MAX_BYTES. Harmless on the filesystem backend. "
            "docs/MEDIA_GOOGLE_DRIVE_PLAN.md §4.3."
        ),
    ),
    Job(
        name="Verify media on Drive",
        command="verify_media_drive",
        hour="4", minute="0", day_of_week="0",
        description=(
            "Sunday 04:00: every file the database references must be in the "
            "DriveObject index and still present, un-trashed, on Google Drive. "
            "Fails (visible here) on the first missing blob. A no-op success "
            "while MEDIA_BACKEND=filesystem. docs/MEDIA_GOOGLE_DRIVE_PLAN.md §7."
        ),
    ),
    Job(
        name="Send policy reminders",
        command="send_policy_reminders",
        hour="9", minute="0",
        description=(
            "Remind employees with outstanding policy acknowledgements. The "
            "command has its own de-duplication."
        ),
    ),
    Job(
        name="Sync attendance punches",
        command="sync_attendance_punches",
        hour="10", minute="0",
        description=(
            "Pull the PREVIOUS day's office presence from TimeLy into "
            "AttendancePunch. 10:00, not overnight: TimeLy's punch data isn't "
            "settled until ~07:00 the next morning. Leaves "
            "MANUAL_CORRECTION rows' timestamps alone."
        ),
    ),
    Job(
        name="Generate GH overtime",
        command="generate_gh_overtime",
        hour="10", minute="20",
        description="Government-holiday overtime entries for eligible teams.",
    ),
    Job(
        name="Generate GH overtime (General BPO)",
        command="generate_gh_overtime_bpo",
        hour="10", minute="25",
        description=(
            "Government-holiday overtime for General BPO. Note the command is "
            "generate_gh_overtime_bpo — the old settings key said "
            "'general-bpo', which never matched."
        ),
    ),
    Job(
        name="Send weekly team rosters",
        command="send_weekly_rosters",
        hour="22", minute="0", day_of_week="5",
        description=(
            "Friday 22:00: next week's roster to every team with an active "
            "RosterEmailConfig. Skips any team whose week was already sent by "
            "hand from /roster/ (RosterEmailLog is the guard)."
        ),
    ),
)

#: django_celery_beat's DatabaseScheduler creates this row itself because
#: ``result_expires`` defaults to 1 day. Orbis runs with
#: CELERY_TASK_IGNORE_RESULT and no result backend, so there is nothing to
#: clean up — seed it disabled rather than leaving a live mystery job in the
#: admin list.
BACKEND_CLEANUP_TASK = "celery.backend_cleanup"


def jobs_by_command() -> dict[str, Job]:
    return {job.command: job for job in JOBS}
