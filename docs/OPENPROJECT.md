# Orbis in OpenProject — how it's kept up to date

Orbis is tracked at **https://op.sebpo.net/projects/orbis**: work packages arranged in a hierarchy, Gantt dates, a wiki, and spent time. OpenProject is a *view* of the repo, not the place to edit. Edit these three files and run a script:

| What | Source of truth | Script |
|---|---|---|
| Work packages: epics, features, tasks, milestones | `tools/openproject/inventory.yaml` | `sync.py` |
| Wiki pages | `tools/openproject/wiki/*.md` | `sync.py --wiki` |
| Spent time | Roster flat export + TimeLy attendance CSV, both kept outside the repo | `timelog.py` |

Nobody has an OpenProject API token, so both scripts drive a real logged-in browser session through Playwright. They sign in once, save the session, and from then on call OpenProject's own `/api/v3` (the JSON API its web frontend uses) with that cookie and the page's CSRF token. The wiki has no write API, so wiki pages are saved by posting the edit form exactly as the browser would.

Added 2026-11-03.

## 1. Structure

- **Epics.** There is one epic per module: accounts, employees, roster, leave, attendance (shown as "Timesheet"), core (dashboard), dynamic_menu, theme, documents, offboarding, policies, scheduler, audit, media_store, payroll, and infra. Three more epics cut across modules: *Open business questions (HR/Finance)*, *Tech debt & known test failures* and *Future features*.
- **Sub-epics.** These group open work under a parent epic:
  - Payroll Phase 5
  - Payroll Phase 6
  - Tax certificate
  - Payroll data protection (later phases)
  - Production follow-ups
  - Directory API switch-over
- **Features (Closed).** These are shipped work. Each one lists its commits, and its Gantt bar runs from its first commit to its last. A commit made before 04:00 counts toward the previous evening's shift.
- **Tasks and Features (New / In progress / On hold).** These are open work. Anything waiting on someone else is **On hold** and carries a **Blocked on:** line.
- **Milestones.** Project start (04-24), MVP (05-15), Celery (07-21), Payroll 1–4 (08-24), HTTPS (08-29), payroll encryption live (10-05), Directory API (10-14), media on Drive (10-16), AI Summit (11-05).
- **Categories.** Each module maps to one category, so filtering by category shows one module's work.
- **Footer.** Every description ends with `orbis-key: <key>`. That footer is how the scripts recognise a work package. Never edit or remove it.

## 2. One-time setup

**In OpenProject** (needs a project admin):
1. *Project settings → Modules:* enable **Work packages**, **Gantt charts**, **Wiki** and **Time and costs**.
2. *Project settings → Work package types:* enable **Epic, Feature, Task, Milestone, Bug**.
3. **Categories:** run `sync.py --create-categories --apply`. It creates the names under `categories:` in `config.yaml` through the project's category form. *(Done 2026-11-03.)*
4. **Statuses:** op.sebpo.net uses its own status set, so `config.yaml` maps the inventory's four statuses: **Closed → Resolved** (the instance's closed status), New → New, In progress → In progress, On hold → On hold. New work packages always start in **Backlog**, the instance default.
5. **Workflow (needs an OpenProject instance admin):** the *Project admin* role must be allowed to move work packages out of Backlog. Without that, every status change is refused with a 422 "no valid transition". Grant the transitions under *Administration → Work packages → Workflows*, for the Project admin role and the types Epic, Feature, Task, Milestone and Bug: Backlog → New / In progress / On hold / Resolved, and between those four. Until then, `sync.py` creates everything in Backlog, warns, and retries the status on every later run.
6. Confirm a time-entry activity named **Development** exists (*Administration → Time and costs*), or change `timelog.activity`.

**On your machine:**
```bash
venv/bin/pip install -r tools/openproject/requirements.txt
venv/bin/playwright install chromium
venv/bin/python tools/openproject/sync.py --login   # a browser opens — sign in, it closes by itself
venv/bin/python tools/openproject/sync.py --check   # verifies session, types, statuses, categories, modules
```
The session is saved in `~/.config/orbis-openproject/auth.json` (mode 600, outside the repo). When it expires, any command will tell you to run `--login` again.

## 3. Work packages

```bash
venv/bin/python tools/openproject/sync.py                    # dry-run (default): create / update / drift / conflict
venv/bin/python tools/openproject/sync.py --apply --only theme   # one epic and its children (parents pulled in automatically)
venv/bin/python tools/openproject/sync.py --apply            # everything
```

- **Idempotent.** `tools/openproject/state.json` (committed) maps each key to its work-package id. It also stores a hash of the last version pushed and a snapshot of what OpenProject returned. A re-run touches only what changed.
- **Drift.** If someone edits a synced field in OpenProject, the dry-run reports **drift** and the field is left alone. Copy the change into the inventory, or force the inventory's version with `--overwrite`. If the same entry changed in both places, it is reported as a **conflict** and always skipped.
- **Never deletes.** An entry removed from the inventory shows up as an *orphan*. Close it by hand in OpenProject.
- **Lost state.** If `state.json` is lost, `sync.py --rebuild-state` rebuilds it from the `orbis-key:` footers.
- **Commit check.** `sync.py --check-commits` checks that every non-merge commit belongs to exactly one entry. `--check` runs the same test.

Comments, attachments, watchers and assignees added in OpenProject are never touched. Only subject, description, type, status, category, parent and dates are synced.

## 4. Spent time

Rule decided 2026-11-03 (see `timelog:` and `people:` in `config.yaml`):

| Roster shift | TimeLy punch | Hours logged |
|---|---|---|
| Working shift | yes | actual Duration, up to and including `actual_until` (2026-11-03). **After it: max(9h, actual)** |
| Working shift | none (worked from home, forgot the remote punch) | **9h** |
| OFF / GH / leave | yes | actual Duration |
| OFF / GH / leave | none | nothing |

**Whose commits count.** Each person's hours follow **their own commits only**, matched by the git author emails under `people:` in `config.yaml`. Teammates' commits are logged as *their* hours, from their own exports, once they have OpenProject accounts. Pass `--person <key>` and fill in that person's `employee_id` and `op_login`. Writing entries for someone else needs OpenProject's "Log time for other users" permission; otherwise they run it from their own `--login`.

**Which features each day goes to.** Work happens on every working day, commit or not. A commit made before 04:00 counts toward the previous evening's shift. Each logged day is split by the first rule that applies:

1. **The person committed that day:** the hours go to those features, in proportion to commit count.
2. **No commit, but inside one of their short features** (first → last commit ≤ 14 days apart, so active work rather than a later follow-up): split equally across those features.
3. **Inside a `gap_windows` range** set for that person in `config.yaml`: split across their commits in the window's `commits_from`→`commits_to` range, by commit count. Use this for work done over several days and committed later in one batch. Set 2026-11-03: Jul 3–15 → the Jul 16–21 batch.
4. **Otherwise:** the hours go to the features of their **next** commit day, since that is the work in progress during the gap.
5. **After their last commit:** the hours go to **Planning, support & operations** (`infra.planning-support`).

Every time entry carries the comment `orbis-timelog:<date>:<key>`, and the dry-run table shows which rule applied to each day.

### Daily, automatically (since 2026-11-03)

The systemd user timer `orbis-timelog.timer` runs every day at 11:00, after Orbis's 10:00 punch sync. It calls `daily_timelog.sh`, which runs:

```bash
venv/bin/python tools/openproject/timelog.py --from-orbis --apply --quiet
```

- **No exports needed.** `--from-orbis` pipes the read-only `orbis_source.py` into `manage.py shell` on `orbis-prod` over SSH. It uses the Timesheet's own shift resolver, and a day counts as working when its ShiftType category is WORK. Checked 2026-11-03 against the exports for Sep 1 – Nov 2: identical, 39 days and 368.9h.
- **It always re-reads the last 7 days through yesterday.** A late punch sync or an approved timesheet correction corrects that day in place. Today is never logged, because its punches aren't synced until 10:00 tomorrow.
- **It never reads before 2026-09-01** (`orbis_punches_from`). Orbis has no punch history before then, so earlier dates would wrongly log 9h. Use the exports below for those.
- **Missed runs:** if the machine was off or logged out at 11:00, it runs at the next login (`Persistent=true`).
- **Output:** written to `~/.config/orbis-openproject/daily.log`.
- **Expired OpenProject session:** you get a desktop notification. Run `sync.py --login`, then `systemctl --user start orbis-timelog`.
- **Install on another machine:** `cp tools/openproject/systemd/orbis-timelog.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now orbis-timelog.timer`. The unit expects the repo at `~/projects/orbis`.
- **Check it:** `systemctl --user list-timers orbis-timelog.timer`, and `journalctl --user -u orbis-timelog` for the run history.

### From exports (history before September, or teammates without SSH)

```bash
R=~/Downloads/roster-flat-YYYY-MM-DD.xlsx         # Orbis → Roster → flat view → Export
A=~/Downloads/attendance_539_....csv               # timely.sebpo.net attendance export
venv/bin/python tools/openproject/timelog.py --roster $R --attendance $A            # dry-run: per-day table + totals
venv/bin/python tools/openproject/timelog.py --roster $R --attendance $A --apply --from 2026-11-04
```

- **Before writing.** Work packages must exist first, so run `sync.py --apply` before the first time log.
- **What is personal.** The exports and `~/.config/orbis-openproject/timelog_state.<person>.json` hold personal attendance data and never go in the repo.
- **Re-runs.** Days already logged are skipped, and a day whose hours changed is corrected in place. If a day's hours moved to different features, for example after adding a gap window, the old feature's entry is reported. Re-run that date range with `--rebalance` to delete it. `--rebalance` only ever deletes entries this tool created, and only within the run's date range (all dates if you pass no `--from`/`--to`).
- **Lost state.** `timelog.py --rebuild-state` recovers the state file from your time entries' comments.
- **First load.** The baseline at first load was 2026-04-24 → 2026-11-03: 119 working shifts, 6 of them with no punch (Jun 4; Jul 10, 13, 16; Aug 7, 17), plus 3 OFF and 2 SL days worked, for **1,106.6h**.

## 5. Routine

- **When a feature ships:**
  1. In the same commit, add or extend its entry in `inventory.yaml`: put the new hash under `commits:` and move `finish`.
  2. Run `sync.py --apply`.
  3. Commit `state.json`.

  If the work closes an open item, set its `status: Closed` and add `start`/`finish`.
- **When an open item changes** (unblocked, decided, deferred): edit its `status` and `blocked_on`, then sync.
- **When something new comes up:** add an entry with a new, permanent `key` (`<epic>.<slug>`). Never rename or reuse a key; the footer and state depend on it.
- **Time log:** automatic, every day at 11:00 (§4). If you see a desktop notification, act on it.
- **Wiki:** edit `tools/openproject/wiki/*.md`, then run `sync.py --wiki --apply`.
- **Commit messages** may mention `OP#<id>`. It's optional; the inventory is the real link.
