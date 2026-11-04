# Scheduler Admin — django-celery-beat in the Django admin

**Status:** implemented 2026-09-29.
**Supersedes:** the `CELERY_BEAT_SCHEDULE` dict in `core/settings.py` as the
source of truth for periodic jobs.

## Why

Before this change the 16 periodic jobs lived in a Python dict
(`CELERY_BEAT_SCHEDULE` in `core/settings.py`). That meant:

- **Pausing a job needed a deploy.** A misbehaving nightly (say
  `sync_attendance_punches` during a TimeLy outage) could only be stopped by
  editing settings, pushing, and restarting `celery_beat_orbis`.
- **Retiming a job needed a deploy**, for the same reason.
- **"Did it run?" was a log grep.** `CLAUDE.md` literally instructed grepping
  both `logs/celery.err.log` and `logs/celery_beat.err.log` when a nightly was
  suspected of not firing. Nothing in the app recorded a run.
- **Ad-hoc re-runs needed SSH.** `manage.py <command>` on the VPS.

Moving the schedule into the database (`django_celery_beat`) fixes all four:
HR/ops-visible rows in the Django admin, `enabled` as a checkbox, crontab edits
picked up by beat within ~5s with no restart, a "Run selected tasks" action for
ad-hoc dispatch, and a run-history table.

The design is ported from the `scheduler` app in the reference project
`/home/smrashel/djangoprojects/smsbot/` (read-only reference), with the
deviations listed under "What we did not port".

## The load-bearing gotcha

`DatabaseScheduler.setup_schedule()` calls
`update_from_dict(app.conf.beat_schedule)`, which routes every settings entry
through `PeriodicTask.from_entry()` →
**`PeriodicTask.objects.update_or_create(name=..., defaults=...)`**
(`django_celery_beat/schedulers.py:194,259`).

So a non-empty `CELERY_BEAT_SCHEDULE` **overwrites the database rows on every
beat startup** — schedule, `args`, and `enabled` all reset. Since
`update_orbis.sh` restarts `celery_beat_orbis` on every deploy, admin edits
would appear to work and then silently revert at the next deploy.

**Therefore `CELERY_BEAT_SCHEDULE` must stay empty for as long as the database
scheduler is active.** `scheduler/tests.py::BeatScheduleSourceOfTruthTests`
asserts this, so re-adding an entry fails the test suite rather than quietly
clobbering production.

Adding a periodic job is now: add a `PeriodicTask` row in the admin, and — if
the job must exist on a fresh install — add it to `scheduler/schedule.py::JOBS`
so `manage.py seed_beat_schedule` creates it.

## Timezone

Dhaka comes for free. `CrontabSchedule.timezone` defaults to
`crontab_schedule_celery_timezone()`, which reads `settings.CELERY_TIMEZONE`
(`django_celery_beat/models.py:302`) — already `Asia/Dhaka` in Orbis. The seed
still writes `timezone="Asia/Dhaka"` explicitly so a row is never ambiguous,
and `DJANGO_CELERY_BEAT_TZ_AWARE = True` keeps beat interpreting each crontab
in its own stored zone.

No schedule shifted in the migration: the old `_crontab()` entries were already
interpreted in `CELERY_TIMEZONE`, so every wall-clock time is unchanged.
(smsbot hardcoded `America/New_York` in its seed migration; we do not.)

## What shipped

### Phase 1 — install the database scheduler

- `requirements.txt`: `django-celery-beat==2.8.1` plus its dependency chain
  (`django-timezone-field`, `python-crontab`, `cron-descriptor`).
  2.8.1 requires `celery>=5.2.3,<6` — our 5.6.3 is in range, and Django 5.2 is
  supported.
- `core/settings.py`: `"django_celery_beat"` in `INSTALLED_APPS`,
  `CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"`,
  `DJANGO_CELERY_BEAT_TZ_AWARE = True`, and `CELERY_BEAT_SCHEDULE = {}` with a
  comment pointing at `scheduler/schedule.py`. The
  `from celery.schedules import crontab as _crontab` import is gone.
- **No MySQL collation `CONVERT` step.** The `CLAUDE.md` rule applies to a new
  table with a `CustomUser` FK; none of the `django_celery_beat_*` tables have
  one, and `scheduler`'s own FKs point at `PeriodicTask` (bigint pk).

One upstream side effect had to be shut off: `install_default_entries` creates
an **enabled** `celery.backend_cleanup` task whenever `result_expires` is truthy
(it defaults to 1 day). Orbis has no result backend, so that job is a daily
04:00 no-op against `DisabledBackend` — but it would sit unexplained in the
admin list and write a run-log row every night. Worse, beat creates it on
*first start*, i.e. after the seed has already run, so a "disable it during
seeding" approach can never win that race on a fresh install. The fix is
`CELERY_RESULT_EXPIRES = None` in settings, which is also simply true: nothing
is stored, so nothing expires. `scheduler/services._disable_backend_cleanup`
stays as a one-shot cleanup for databases where the row already exists.

### Phase 2 — the `scheduler` app (admin UX)

`scheduler/` replaces the stock `PeriodicTask` / `CrontabSchedule` admins:

- **`TaskOrder`** (`OneToOne` → `PeriodicTask`, `rank`) as a stacked inline, so
  the changelist reads in execution order rather than alphabetically. Ranks are
  seeded by time-of-day, which makes the real dependency chain visible —
  `reconcile_cpl_grants` (00:40) and `reconcile_shift_leave_usage` (00:45) must
  precede `close_al_cycles` (01:15), something the old settings dict hid.
- **`format_schedule`** column: `10:00 PM   Fri` for numeric hour/minute,
  falling back to the raw cron string for expressions like `*/5`.
- **`PeriodicTaskForm`**: restores the `TaskSelectWidget` dropdown (registered
  task names, not a free-text field) and compacts `args`/`kwargs` to 2-row
  textareas.
- **`HourFirstCrontabAdmin`**: hour before minute (the order people think in),
  with `human_readable` as a readonly preview.
- Fieldsets are restricted to `crontab` — no `interval` / `solar` / `clocked`,
  since all 16 Orbis jobs are cron-shaped. Add those back deliberately if a
  job ever needs them.
- `admin_custom.css` makes the **Enabled** checkbox label bold red, because
  that checkbox is the one with production consequences.

### Phase 3 — seed all 16 jobs

`scheduler/schedule.py::JOBS` is the code-level record of the jobs, and
`manage.py seed_beat_schedule` materialises it. It is **`get_or_create`-shaped
on purpose**: an existing row is left completely alone, so the command can run
on every deploy without ever overwriting an admin edit. `--force` re-syncs a
row back to the code default, `--only <name>` narrows to one job, and `--dry-run`
prints what would change.

`scheduler/migrations/0002_seed_beat_schedule.py` runs the same seed so a fresh
install (and CI) gets the rows automatically — same command-plus-data-migration
split as `documents`' `seed_service_book_template`.

| # | Task name | Command | Crontab (Asia/Dhaka) |
|---|---|---|---|
| 1 | Deactivate past LWD | `deactivate_past_lwd` | 00:15 daily |
| 2 | Monthly leave accrual (dormant) | `apply_monthly_accrual` | 00:30 on the 1st |
| 3 | Reconcile CpL grants | `reconcile_cpl_grants` | 00:40 daily |
| 4 | Reconcile shift leave usage | `reconcile_shift_leave_usage` | 00:45 daily |
| 5 | Open AL cycles | `open_al_cycles` | 01:00 daily |
| 6 | Year-end rollover | `apply_year_end_rollover` | 01:00 on Jan 1 and Jan 15 (retimed 2026-10-01 for CpL's 100% carry — the Jan 15 firing is an idempotent catch-up; `scheduler/migrations/0003_rollover_jan15_catchup.py` moved the seeded row) |
| 7 | Close AL cycles | `close_al_cycles` | 01:15 daily |
| 8 | Generate night-shift consents | `generate_night_shift_consents` | 01:30 on the 1st |
| 9 | Sync line managers | `sync_line_managers` | 02:00 daily |
| 10 | Sync supervisors | `sync_supervisors` | 02:10 daily |
| 11 | Clear expired sessions | `clearsessions` | 03:00 daily |
| 12 | Send policy reminders | `send_policy_reminders` | 09:00 daily |
| 13 | Sync attendance punches | `sync_attendance_punches` | 10:00 daily |
| 14 | Generate GH overtime | `generate_gh_overtime` | 10:20 daily |
| 15 | Generate GH overtime (General BPO) | `generate_gh_overtime_bpo` | 10:25 daily |
| 16 | Send weekly team rosters | `send_weekly_rosters` | 22:00 Friday |

`day_of_week` is seeded numerically (`"5"` = Friday) rather than `"fri"`, to
stay inside `python-crontab`'s validator regardless of version.

Two quirks are preserved verbatim rather than "fixed" — they are intentional:

- **#2 is dormant.** AL's accrual is computed live (`live_accrued_al`);
  `apply_monthly_accrual` is kept for a hypothetical future
  CALENDAR + WORKED_DAY_RATIO policy. It stays scheduled and enabled, exactly
  as before, so its behaviour does not change as part of this migration.
- **#15's old settings key** (`generate-gh-overtime-general-bpo`) did not match
  its command (`generate_gh_overtime_bpo`). The command is what matters; the
  display name follows the command.

### Phase 4 — run history

- **`ScheduledTaskRun`** — one row per beat-dispatched run:
  `periodic_task_name`, `task`, `celery_task_id`, `status`
  (success / partial failure / failure), `started_at` / `finished_at`,
  `total_records` / `failed_records`, `summary` (JSON), `output`,
  `error_message`.
- `scheduler/monitoring.py` hooks Celery's `task_prerun` / `task_postrun` and
  keys off the `periodic_task_name` message header that
  `django_celery_beat` injects (`schedulers.py:89`). Only beat-dispatched tasks
  are recorded — `core.tasks.defer()` traffic (SMTP fan-outs, PDF renders) is
  deliberately not logged here, it would be pure noise.
- De-duplicated on `celery_task_id`, so a retry storm cannot fan out rows.
- Read-only Django admin changelist with `list_filter` on name / status / date.
  Deliberately **not** an Orbis-themed sidebar page yet: this is an ops/audit
  surface, and a first-party page would need the full
  `page-fill` / `table-scroll` / `PageSizeMixin` treatment from `CLAUDE.md` for
  no current user demand. Revisit if HR ever needs it.
- **`core.tasks.run_management_command` now returns a summary dict** and
  captures the command's stdout/stderr into it, so the run log has something to
  show instead of a bare SUCCESS. Return value only — the command itself is
  unchanged and still hand-runnable via `manage.py`.

This retires the "grep both celery logs" workaround in `CLAUDE.md`.

### Phase 5 — docs + deploy

- `deploy/README.md` — the schedule is in the database; the `celerybeat-schedule`
  shelve file is no longer used (DatabaseScheduler keeps its sync state in
  `django_celery_beat_periodictasks`); no supervisor change is needed because
  `CELERY_BEAT_SCHEDULER` is a settings key, not a `-S` CLI flag.
- `CLAUDE.md` — the "adding a nightly job = adding a beat entry" convention is
  rewritten, and the empty-`CELERY_BEAT_SCHEDULE` invariant recorded.
- `docs/PLAN.md` — new library + new app, logged per the plan-drift rule.

**Deploy order matters.** Between the settings change and the seed there is a
window with no schedule at all, so do it in one deploy:

```
pip install -r requirements.txt
python manage.py migrate            # creates the tables and seeds the 16 rows
python manage.py seed_beat_schedule # no-op if the migration already ran
sudo -n supervisorctl restart celery_orbis celery_beat_orbis
```

### Run once with arguments (added 2026-10-30)

"Run selected tasks" dispatches a row exactly as saved, so a resync of a
specific date used to mean SSH — editing the row's `args` to add `--date` would
have pinned the nightly to that date until someone noticed. The admin now has
a **"Run once with arguments…"** action (and a button on the change page,
`<pk>/run-with-arguments/`): extra arguments typed as on the command line are
appended to the row's own `args` for that one `apply_async`, with the same
`periodic_task_name` header, so the run is logged in `ScheduledTaskRun` and its
recorded command line shows the extra arguments. The row is never saved. The
arguments are parsed against the command's own argparse definition before
dispatch, so an unknown flag fails on the form rather than as a FAILURE row.
Only `run_management_command` rows naming a real command are eligible; change
permission on the task is required.

The first use is `sync_attendance_punches`, which gained `--from`/`--to`
(inclusive, `--to` defaults to yesterday, at most 31 days so it fits inside
`CELERY_TASK_TIME_LIMIT`; today and later are refused — never settled).

## What we did not port from smsbot

| smsbot | Decision |
|---|---|
| `apps.ready()` bulk-disabling every enabled task when `DEBUG` | **Skipped.** It writes to whatever database is configured; if `DEBUG` were ever true against prod it would silently stop every nightly. Local dev can toggle `enabled` in the admin. |
| `apps.ready()` calling `setup_gmail_renewal()` to seed a task | **Skipped.** Seeding belongs in a management command + data migration, not an import-time side effect. |
| `services/task_reports.py` (794 lines) | **Mostly skipped** — it is foreclosure/probate domain logic and depends on `opslogs.EmailNotificationConfig`. Only the generic recorder half (~150 lines) is ported, as `scheduler/monitoring.py`. |
| The daily emailed task report | **Skipped for now.** Worth revisiting once there is run history to report on; it needs a recipient policy decision first. |
| `/scheduler/task-runs/` bespoke page (390-line template, Bootstrap-flavoured, inline `<style>`) | **Skipped** in favour of the read-only admin changelist. It would not match the Timely theme, and `CLAUDE.md`'s list-page contract would mean rebuilding it from scratch anyway. |
| `from .forms import *` | Explicit imports. |

## Verification

`scheduler/tests.py` covers:

- `CELERY_BEAT_SCHEDULE` is empty while the DB scheduler is active (the
  clobbering invariant).
- Every `JOBS` entry names a real management command.
- Seed idempotency: running twice creates no duplicates and mutates no fields;
  `--force` does re-sync.
- Every seeded crontab's **next fire time matches the pre-migration
  `_crontab()` schedule** — the proof that nothing shifted.
- All rows land on `Asia/Dhaka`.
- A real `DatabaseScheduler.setup_schedule()` leaves exactly the 16 rows — the
  regression test for the `backend_cleanup` behaviour above.
- `format_schedule` / `_dow_to_names` / `_format_time_12h` against numeric,
  `*/5`, and day-range inputs.
- `ScheduledTaskRun` recording: success, failure, `celery_task_id`
  de-duplication, and that non-beat tasks are ignored.

Manual smoke test after deploy:

```
python manage.py shell -c "from django_celery_beat.models import PeriodicTask; print(PeriodicTask.objects.count())"   # 16
celery -A core beat -l debug     # expect "DatabaseScheduler: Fetching database schedule"
```

**Do not expect beat's debug log to list all 16 entries.** django-celery-beat
2.8.1 only holds the tasks whose hour falls within ±2 hours of the current
server hour (`DatabaseScheduler._get_crontab_exclude_query`) and re-syncs at
least every 5 minutes (`SCHEDULE_SYNC_MAX_INTERVAL`), so a healthy scheduler
typically shows a handful. `PeriodicTask.objects.enabled().count()` is the
number to check; the scheduler's own view of the world is a moving window, not
the schedule.

Then toggle `enabled` on any task in the admin and confirm beat picks it up
without a restart.

## Rollback

Clean and fast: restore the `CELERY_BEAT_SCHEDULE` dict, drop
`CELERY_BEAT_SCHEDULER`, restart `celery_beat_orbis`. The database rows become
inert; nothing is lost. The `scheduler` tables can stay in place.

## Open risks (not yet addressed)

1. **Silent clobbering** if `CELERY_BEAT_SCHEDULE` is ever repopulated. The
   test guards the repo, but nothing guards a hand-edit on the VPS.
2. **Double-run** if beat is started twice (two supervisor programs, or a
   stray `celery -A core beat` in a shell), or if a stale `celerybeat-schedule`
   file is picked up by an old process. One scheduler, always.
3. **A deleted `PeriodicTask` row is invisible.** With the schedule in
   settings, deleting a job needed a code review. In the admin, anyone with
   permission can delete a nightly and nothing will report its absence. A
   "seeded jobs still present" health check would close this.
4. **Admin write access.** The stock `django_celery_beat` admin permissions are
   the only gate; there is no Orbis-specific group check on who may retime a
   payroll-adjacent nightly.
