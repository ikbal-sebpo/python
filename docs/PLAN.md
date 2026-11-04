# Orbis — Greenfield Django Project Plan

## Context

Orbis is a new Django project that will consolidate SEBPO's workforce-management systems (currently spread across ~8–10 legacy apps) into a single platform. Per the architecture document (`06_Architecture_Integration_Document_Orbis.docx`), the implementation is phased: (1) consolidate existing Django apps, (2) modernize the UI to the TimeLy look, (3) integrate with the external TimeLy (Laravel) attendance engine via a Roster API, (4) progressively activate remaining modules.

Key environment facts from the doc:
- Timeline: 2026-04-20 → 2026-05-15 (~3.5 weeks) for MVP.
- Deployment: VPS (Ubuntu 26.04 LTS), subdomain `orbis.sebpo.net`.
- Azure AD SSO, redirect URI `https://orbis.sebpo.net/auth/redirect` — but credentials are not yet available, so MVP ships with Django built-in auth and SSO slots in later.
- Database: MySQL.

UI direction: replicate the TimeLy product's look & feel (dark-teal `#0A4958` + bright-teal `#41B3B3` brand, Figtree/Roboto fonts, white cards on light-gray canvas, 240px white sidebar with teal active bar, `.5rem` radius, dark-mode support).

TimeLy itself has no roster — it aggregates attendance from UNIS turnstile hardware across SEBPO's locations. Orbis will push roster to TimeLy; TimeLy will compute late/duration/etc. and optionally expose a summary endpoint Orbis can pull.

## Locked decisions

1. **Django 5.2 LTS** (supported through April 2028). Python 3.12.
2. **Single `core/settings.py`** with `python-dotenv` reading `.env` from project root — mirrors `reicrm`'s pattern. No split `base/dev/prod` files.
3. **Apps split** — ttms/core becomes multiple small apps in Orbis. Single-company deployment (no multi-tenant `organization` app needed).
4. **Authentication — two-phase**:
   - **MVP (now → 2026-05-15)**: Django's built-in `ModelBackend` with **email + password**. Custom login page that leaves space (hidden/gated) for a "Sign in with Microsoft" button. Users are created by an admin via Django admin + a simple in-app user-create view (no JIT yet). This mirrors ttms's current dual login (username/password + SSO button) minus the SSO half for now.
   - **Post-MVP (when Azure AD tenant/client/secret arrive)**: add `mozilla-django-oidc` and a small `OrbisOIDCBackend` subclass (~40 lines) for delegated Microsoft Entra sign-in + per-login Graph enrichment of HR fields (`department`, `jobTitle`, `officeLocation`, `employeeId`, `mobilePhone`, `employeeHireDate`, `manager`). **No JIT** — admin still pre-creates users; the backend's `filter_users_by_claims` links the Entra identity to the existing `CustomUser` by email and `create_user` raises `PermissionDenied` so unknown accounts get a clear "no Orbis account" error. Mount at `/oidc/` (callback `https://orbis.sebpo.net/oidc/callback/`); the architecture doc's `/auth/redirect` URI is superseded by the library's natural callback path (registered in Azure as the new redirect URI on Orbis's own dedicated app registration). No data migration needed because email is already the identity key. (Library choice rationale: single IdP + no JIT means allauth's social-account framework is overkill — we'd own ~600 lines of adapter code, 6+ tables, and a multi-provider abstraction we don't use. mozilla-django-oidc fits the actual scope.)
   - `USERNAME_FIELD = 'email'` on `CustomUser` from day 1 so the SSO cutover is a pure config change.
5. **Custom user model**: clean hybrid — veridoc's `CustomUser` shape + ttms fields that are actually used (`employee_id`, `joining_date`, `probation_month`, `confirmation_date`, `last_working_date`, `timezone_name`, `shift_start_hour`, `signature`). UUID primary key.
6. **Data strategy**: fresh MySQL DB, clean `0001_initial` per app. No ttms migration history. Fix typos (`ProfessionalCertificatiion`, `has_projec`) while porting. Keep ttms's unified `ShiftType` pattern for roster cells (it's the right call — see "Roster + leave — unified cell model" section). Bulk import of existing ttms employee data is a later decision — not blocking MVP.
7. **Tailwind**: `django-tailwind` (npm-based) → `theme` app holds Tailwind source; build pipeline plugs into `collectstatic`.
8. **Extensibility**: design so ttms's `bpo`, `operations`, `timesheet` apps can be ported later without re-architecting.
9. **MVP for 2026-05-15**: email/password login + **full Employee Directory** (all 14 profile models wired up) + Roster (ShiftType/Shift/Roster CRUD + read API for TimeLy).

## Project layout

```
/home/smrashel/djangoprojects/orbis/
├── manage.py
├── requirements.txt
├── .env                       # not committed
├── .env.example               # committed; placeholder values
├── .gitignore
├── README.md
├── core/                      # project settings package
│   ├── __init__.py
│   ├── asgi.py
│   ├── wsgi.py
│   ├── urls.py                # root URL conf (+ /auth/redirect alias added in SSO phase)
│   └── settings.py            # single file, reads from .env
├── accounts/                  # CustomUser, auth views, org structure, SSO adapter
├── employees/                 # employee profile + HR taxonomy
├── roster/                    # shifts, roster, holiday + Roster API (DRF)
├── leave/                     # Leave Policy v2.0 (shipped post-MVP)
├── attendance/                # TimeLy office-presence overlay (shipped post-MVP)
├── documents/                 # HR-issued letters + QR verify (shipped post-MVP)
├── offboarding/               # resignation → 8-step clearance (shipped post-MVP)
├── policies/                  # PDF policy distribution + sign-off (shipped 2026-06-24)
├── payroll/                   # payroll runs, rates, register, TDS (Phases 1-4 shipped)
├── scheduler/                 # celery-beat schedule in the Django admin (added 2026-09-29)
├── audit/                     # access log — who accessed which data or page (added 2026-10-09)
├── dynamic_menu/              # ported from veridoc (cleaner than ttms's)
├── theme/                     # django-tailwind app (Tailwind source)
├── templates/
│   ├── base.html              # Timely-brand base with dark-mode toggle
│   ├── partials/              # topbar, sidebar, footer (pinned below <main> — see App-shell layout)
│   └── auth/                  # login page, error pages
├── static/                    # project-wide logo, favicon, fonts (+ vendored js)
├── media/                     # user uploads
├── docs/                      # PLAN.md + per-module specs
├── deploy/                    # nginx / supervisor / gunicorn config
└── logs/
```

### App responsibilities

**`accounts`** — authentication + identity + org structure (single-company, so org structure lives here because `CustomUser` FKs point at it):
- `CustomUser` (UUID pk, `AbstractUser` extension, `USERNAME_FIELD='email'`, FKs to Department/Designation/JobLocation/WorkLocation).
- `Department`, `Designation`, `AccessLevel`, `Team`, `SubTeam`, `JobLocation`, `WorkLocation`.
- **Auth**: SSO-only for regular users against Microsoft Entra, from the 2026-09-01 go-live. `OrbisOIDCBackend` (custom subclass of `mozilla_django_oidc.auth.OIDCAuthenticationBackend`) handles delegated Entra sign-in + **first-login-only** Graph enrichment (`department`, `jobTitle`, `officeLocation`, `employeeId`, `mobilePhone`, `employeeHireDate`, `manager`) — Orbis is the system of record after that; no ongoing AAD sync on later logins. Full JIT — anyone with a `sebpo.com` Entra account auto-onboards on first sign-in (`OIDC_CREATE_USER=True`); RBAC handles access. The regular `/login/` password form only renders when SSO isn't configured (local dev); `/admin/` password login (`ModelBackend`) remains as a break-glass path for staff/superuser accounts. See "SSO integration — shipped state" below for the full detail.
- Decorators: `@login_required` (Django built-in) + a small `@role_required(...)` helper.
- **Canonical admin group is `"User Admins"`.** The permission layer, `seed_orbis`, and menu ACLs all key off `accounts.permissions.ADMIN_GROUP = "User Admins"`. (A legacy empty `"Admins"` group — created by an earlier seed, matched by no user — was retired by `accounts/migrations/0002_standardize_user_admins`, which re-points menu/ProtectedView ACLs onto `"User Admins"` and deletes it. Admins previously only worked because they were also superusers.) Use `ADMIN_GROUP`, never the literal string.
- **Org-structure dropdown scope**: `permissions.can_see_all_departments` (`ALL_DEPARTMENTS_GROUPS = {User Admins, HR, Senior Leadership, Payroll Admins}` + superuser) governs who may browse every department/team. Everyone else is scoped via `effective_department_id` / `visible_departments` / `visible_teams`, on every dept/team dropdown (roster, employee list, payroll, shift-summary) and the user create/edit form (department→team cascade via the `load_teams` AJAX endpoint + `static/js/dept-team-cascade.js`). **`visible_teams` narrows past department to team, 2026-09-07** (an ordinary employee — reported live by an actual Vox Media CX Support agent seeing every other General BPO team in `/attendance/corrections/`'s filter popover): someone who manages the whole department (`head_of_department`/`Department.additional_managers`, via `_manages_any_department`) still sees every team in it; someone who manages specific teams (`Team.additional_managers`, the "acting TL" grant) sees those plus their own; everyone else sees only their own team, falling back to the whole department only if they have no team of their own (nothing narrower to show). **Corrected 2026-09-16**: this used to clamp every non-privileged user to their own single `CustomUser.department_id` row, which was silently wrong for a manager whose reports-to downline spans multiple departments (e.g. a C-level exec whose direct reports are themselves department heads of separate departments) — real case: a C-level account's dashboard showed 1359 people in scope (`visible_employee_pks`, the correct unbounded `reports_to` walk) but `/employees/` showed only 11, the headcount of their own department. `effective_department_id` / `visible_departments` / `visible_teams` now derive the allowed department set from a new `scope_department_ids()` helper — the departments actually represented in the viewer's own `visible_employee_pks` scope, not their own department row. No-op for the common case of a manager whose whole downline sits in one department; a requested out-of-scope `?department=` (or `visible_teams`' `department_id` arg) is still ignored, same anti-tamper behavior as before.

**`employees`** — HR profile data + HR taxonomy:
- `JobInfo` (O2O CustomUser), `PersonalInfo`, `FamilyInfo`.
- `Education`, `Contact`, `Nominee`, `Email`, `IdentificationNumber`, `Reference`, `ProfessionalCertification` *(typo fixed)*, `Vaccination`, `LegalIssue`, `Image`, `Address`.
  - **Relaxed 2026-10-05** (`docs/LEGACY_60_IMPORT_PLAN.md` §2.3): `Education` requires only `degree_level` (result, result details, passing year, institution and category optional) and `ProfessionalCertification` only `certificate_name` (institution and year optional). HR's legacy records carry just a level label / certificate name and must be on file for audit before the details can be completed; the drawers and the Service Book render "—" for the missing pieces.
- Taxonomy used only here: `Country`, `Division`, `State`, `City`, `Religion`, `Relation`, `Vaccine`, `Dose`, `Client`.
- Employee directory UI (list, detail, create, edit) with all profile sections.
- AJAX endpoints: `load_states`, `load_cities`, `load_teams`, `load_employees`.

**`roster`** — shifts, roster, holidays, Roster API:
- `ShiftType`, `ShiftAttribute`, `Shift`, `Roster` (one per employee per weekday), `Holiday`.
- **Per-cell work location** on both `Roster` and `Shift` (`_LocatedRosterCell` mixin → nullable `job_location` + `work_location` FKs). A single canonical dropdown per cell — MHK / MBD / RAOWA / WFH — backed by `accounts.locations` helpers; null falls back to the employee's contractual `CustomUser.job_location` / `work_location` so the common case needs zero per-row config. See "Roster + leave — unified cell model" for the model shape.
- UI: weekly roster grid, shift CRUD.
- **Roster upload** (`roster/upload.py`, `upload_views.py`): admin pastes/uploads a CSV or XLSX export from the legacy roster sheet → dry-run report (matched employees, unknown shift codes, conflicts) → apply. Endpoints: `roster/upload/` (form), `roster/upload/template/<fmt>/` (download blank template).
- DRF API (`/api/v1/roster/`): read-only endpoints consumed by TimeLy (Laravel). Token auth. `ShiftSerializer` exposes `job_location`, `job_location_code`, `work_location`, and `location_badge` so TimeLy sees the resolved per-day location.

**`leave`** — placeholder in MVP; full build in Phase 4:
- `LeaveApplication` (employee, leave_type FK, start/end dates, status, approver, notes).
- `LeavePolicy` — **OneToOne to `ShiftType`** (not a replacement for it). Hangs leave-specific business rules (`max_days_per_year`, `is_paid`, `requires_medical_cert`, `carry_forward`) off the leave-flavored `ShiftType` rows. See "Roster + leave — unified cell model" section below.
- `LeaveApplication.leave_type` → FK to `ShiftType` with `limit_choices_to={'category': 'LEAVE'}`.
- **Shift summary report** (`leave/reports.py::build_shift_summary`, view at `reports/shift-summary/`): per-employee counts of materialized `Shift` rows by shift category (CL/SL/AL/LWP/MTL/CPL/GH/working days) for a date range, filterable by department + employee. Mirrors the old TTMS roster-summary shape but reads Orbis' semantic ShiftType rows + leave ledger instead of hardcoded IDs.

**`attendance`** — TimeLy office-presence overlay + local attendance history, shipped post-MVP 2026-09-07:
- `attendance/timely.py::fetch_office_presence(date_obj)` — outbound client for TimeLy's `/api/attendance/office-presence` endpoint (current, pre-final API; a dedicated version is still coming). Never raises; 60s-cached per date. The dashboard's `working` bucket (`dashboard/status/?bucket=working`) calls this live, for **today only**.
- `AttendancePunch` (employee, date unique) — local, durable record of one day's office presence, either synced from TimeLy or written by an approved correction (`source`: `TIMELY_API` / `MANUAL_CORRECTION`): `check_in`/`check_out` (first/last punch), `time_in_office` (`DurationField`), `office_bucket` (via `accounts.locations.office_bucket`), `raw_payload` (exact TimeLy record, kept as insurance while the API is pre-final; stale/absent on a `MANUAL_CORRECTION` row). Keyed by `(employee, date)` rather than FK'd to `roster.Shift` — a punch can exist for a date with no concrete `Shift` row (covered by the weekly `Roster` pattern instead), and this is an observed fact from an external system, not a scheduling record; joined to `Shift`/`Roster` at query time by `(employee_id, date)` when a scheduled-vs-actual (on-time/late) comparison is needed — not built yet.
  - **Reversal, 2026-09-07**: `time_in_office` was originally left null pending a dedicated TimeLy duration field, "deliberately not derived from `check_out - check_in`... a future duration field may account for multiple sessions/breaks that simple subtraction can't". TimeLy's own developer confirmed their dashboard computes it exactly that way — `last_punch_at − first_punch_at`, no finer-grained punch data behind it — so `sync_attendance_punches.py` now computes and stores it the same way (null if either timestamp is missing or `check_out <= check_in`). Existing rows were backfilled in place.
  - **Reversal, 2026-09-16**: the 2026-09-07 subtraction turned out to be wrong for shifts crossing midnight — TimeLy was truncating `first_punch_at`/`last_punch_at` to the *queried calendar date*, so a night shift's post-midnight punch-out was silently dropped (a real ~9h shift stored as ~1h). At our request TimeLy's API now also returns `duration`/`duration_seconds` (and an as-yet-unused per-shift `shifts[]` breakdown) computed server-side, unaffected by that truncation. `sync_attendance_punches.py` now prefers `duration_seconds` for `time_in_office`, falling back to `check_out - check_in` only when the API omits it. Verified against ~4,000 employee-days that `duration_seconds` always equals a correct `last_punch_at − first_punch_at` — the bug was in the timestamps TimeLy handed back, not in the subtraction itself. All of `AttendancePunch`'s history (2026-09-01 onward, the app's full lifetime) was re-synced in place; a scan of night shifts (`payroll.inputs.is_night_shift`) with a suspiciously short (<4h) stored duration dropped from 641/3,354 to 34/4,038 after the resync.
- `manage.py sync_attendance_punches [--date YYYY-MM-DD] [--dry-run]` upserts one day's punches into `AttendancePunch`, skipping TimeLy records whose `employee_id` doesn't match a real `CustomUser.employee_id` (TimeLy returns unmapped rows, e.g. `employee_id: "-1"`). Runs nightly as the `Sync attendance punches` periodic task (a `django_celery_beat` row — `docs/SCHEDULER_ADMIN_PLAN.md`) at **10:00 Asia/Dhaka for the previous day** — not overnight, because TimeLy's punch data for a day isn't settled until ~7:00 AM the next morning. A row whose current `source` is `MANUAL_CORRECTION` has its `check_in`/`check_out`/`time_in_office`/`source` left untouched on any resync (office/punch_count/raw_payload still refresh) — TimeLy's own numbers for that date are the ones already known to be wrong.
- `/attendance/` (`AttendanceListView`) — one row per visible employee for a chosen working date (`?date=`, defaults to yesterday — the most recent date `sync_attendance_punches` has settled), filtered by the same `accounts.filters` q/department/team bar as `/employees/`. Nav entry: a section header labeled **"Timesheet"** (display-only rename, 2026-09-07 — the app/models/URLs all keep the `attendance`/`Attendance*` name) with two submenus, `Timesheet` (this list) and `Corrections` (below) — mirrors the `Documents`/`Offboarding` header pattern. Row-level scoping enforced by `visible_employees`/`apply_employee_filters` (locked users see only their own department/team), no `ProtectedView` gate. The dashboard's `working` bucket page links out to it ("View full history" — carrying an explicit `?date=` since the bare URL now lands on the viewer's own range, see below).
- **Three modes off one URL (two added 2026-09-11, the third 2026-10-01)** — `AttendanceListView.get` branches on whether `?q=` is an *exact* employee-ID match (`attendance/views.py::_resolve_range_employee`), so a manager/HR can pull one person's day-by-day log instead of only one day's whole team, using the same search box rather than a second control; failing that, on whether a department/team is selected together with a multi-day span:
  - **Single-employee date range** — `?q=<their exact employee_id>` (the human-readable ID, not the pk) + `?from=`/`?to=`: one `_DateRow` per date in the span, built as a plain Python list and paginated with `core.pagination.paginate_list`. Rows come from `_build_timesheet_rows([employee], dates)` — see the department/team mode below for the shared builder; punches, pending/approved corrections and the WORK-with-no-punch "Unaccounted" flag are all keyed by `(employee, date)`, and `?flagged=1` filters the whole span. The employee is resolved *through* `visible_employees`, so it's the same scope check as every other row — out of scope and not-found are indistinguishable by design (both just fail to match, no separate error message).
  - **Department/team date range (added 2026-10-01)** — `from != to`, no exact-ID match, and a department or team effectively selected (`accounts.filters.effective_filter_ids`): one `_DateRow` per employee per date, **ordered employee-then-date** so each person reads as a contiguous block, paginated with `paginate_list` like the single-employee mode. "Effectively selected" covers a department-locked viewer with no explicit pick (`effective_department_id` already clamps them) and a team pick on its own — `effective_filter_ids` backfills the team's own `department_id`, since the filter bar's cascade never lets anyone pick a team without a department and a hand-typed `?team=` used to filter by the team while the dropdown read "All departments" (the same backfill now applies on every `accounts.filters` consumer: `/employees/`, `/users/`, `/leave/`, the shift-summary report, `/attendance/corrections/`; `roster` keeps its own private helpers per the standing rule). A privileged viewer on "All departments" is still refused — one row per employee per date across the whole company is the unbounded case — and the `date_errors` message now says "Pick a department or team, or narrow your search…". **Reversed for `can_see_all` viewers (HR/Admins/superusers) on 2026-11-02:** HR needs the whole company's timesheet for a payroll period, and their "All departments" pick is an empty `department=` that can't otherwise unlock the range. Their range rows are the payroll population for the span (`payroll.periods.employed_during_q` over `visible_employees(include_separated=True)` — leavers inside the span in, no-employee-ID service/test accounts out), still capped at 31 days. To keep it affordable (~1,500 people × 30 days ≈ 45k rows), the page no longer builds every row: `_unaccounted_keys` resolves the live count and `?flagged=1` from bare `values_list` columns through the same `_resolve_matrix` precedence loop, the list pages over `(employee, date)` keys, and full `_DateRow`s are built for the page's employees only (dev, prod-sized data: 4.3s → 1.2s a page). The export still builds the full set (~10s for a payroll period). Everyone else is still refused. Capped at `TEAM_MAX_RANGE_DAYS = 31` (same value/reasoning as `roster.views.MAX_RANGE_DAYS`; the single-employee cap stays 186). Rows are built by `_build_timesheet_rows(employees, dates, user, request)` over `_resolve_shifts_matrix` (`_resolve_shifts` across employees × dates: one Shift query over the set and span, one Roster query over the set, same employment-window guard on the pattern fallback) plus one punch query and one corrections query — constant query count regardless of row count, with `can_request_attendance_correction` evaluated once per employee rather than per row. Each name links to `?q=<their id>&from=&to=` as the drill-in; a "View <date> only" link drops back to team mode.
  - **Export (`?export=xlsx`, added 2026-10-01)** — an "Export Excel" submit button in the same GET form, in every mode, same `?export=` convention as `/employees/` and `/payroll/overtime/`: `AttendanceListView.get` checks it before pagination and hands the *full* row list the active filters matched (flagged filter included) to `attendance/exports.py::build_timesheet_export`. Team mode's single date goes through the same builder with `dates == [date_to]`, so the three modes can't drift from one another or from the screen. Columns mirror the table (Date, Day, Emp ID, Name, Department, Team, Designation, Shift, Office, Punch in, Punch out, Time in office, Status — the on-screen badges flattened to `Unaccounted` / `Correction pending` / `Corrected`, `_DateRow.status_label`); filename `timesheet-<scope>-<from>-to-<to>.xlsx` where scope is the employee ID, else `department-team`, else the department (`_scope_label`, also the range-mode banner text). The openpyxl tail every exporter shared (column auto-width + download response) moved from `payroll/exports.py::_finalize_xlsx` to `core.exports.finalize_xlsx` in the same change; `employees/exports.py` had its own inline copy and now uses it too.
  - **Team** — `?q=` empty, or not an exact ID match (including a partial/substring ID, a name, an email, or a department/team narrowing to one person by coincidence): the historic shape, one row per visible employee for `to`, still a `ListView` + `PageSizeMixin`, still narrowed by the same fuzzy `accounts.filters` q/department/team search. Deliberately exact-match-only for the mode switch — a fuzzy match happening to narrow to one row would flip modes on a coincidence, not a deliberate ask. Each name links to `?q=<their id>` as the drill-in.
  - Defaults: a *bare* `/attendance/` (none of `q`/`department`/`team`/`date`/`from`/`to`/`flagged` present — `page`/`per_page` deliberately excluded so paging can't flip modes) searches for the viewer's own `employee_id` and lands on their own month-to-date, `from = today.replace(day=1)`, `to = yesterday`. A range with neither a resolved employee nor a department/team, and an inverted range, both report through `date_errors` and fall back to team mode on `to` — same surfacing mechanism as `leave.views::_leave_date_range`, not `messages`. `?date=` is still honoured as `from = to = date` so existing links/bookmarks are unchanged. The span is clamped to `SINGLE_EMP_MAX_RANGE_DAYS = 186`, same value and same hold-`from`/pull-`to`-back shape as `roster.views`. No separate "Employee ID" param — `partials/_employee_filter_controls.html` gained an optional `search_placeholder` override (default unchanged) so this page's box reads "Employee ID" while still being the same shared `q` search everywhere else. All three modes render `attendance/list.html`; everything from the Shift column rightwards is the shared `attendance/_timesheet_cells.html` partial, so the row shapes can't drift.
- **Shift column** — each row also shows the employee's resolved shift for that date (`attendance/views.py::_resolve_shifts`): a concrete `roster.Shift` row wins, else the employee's weekly `Roster` pattern — same resolution order as `core.dashboard.today_status_buckets`. Renders the `ShiftAttribute` name when set (e.g. `EVE 2-11` — SEBPO's attribute naming already bakes the start/end time into the label), else the plain `ShiftType` name. This is what makes the actual-vs-scheduled comparison (on time or not) visually obvious without a dedicated late/on-time computation yet.
- **Corrections** (`AttendanceCorrection`, shipped 2026-09-07) — an employee (or their manager/HR on their behalf) can request a fix to a wrong punch, or — for a date with no punch at all, e.g. forgot to punch in and out completely — add a timesheet from scratch: same `AttendanceCorrection` model, same drawer, same approval flow either way, only the button label ("Request correction" vs "Add timesheet", picked by whether a punch already exists) and the drawer's "Currently on file" box (or its absence) differ. Opened via `/attendance/corrections/request/?for=<employee_pk>&date=YYYY-MM-DD` (linked from each attendance-list row's action, plus a header-level "Add timesheet" button/"Timesheet pending" badge for the viewer's own record — computed independent of the table's current q/department/team filter, since that's about the viewer, not the filtered list). `AttendanceCorrectionRequestView` is a plain `View`, not a `CreateView`: an htmx GET renders `attendance/_correction_request_drawer.html` into the right-hand drawer, a non-htmx GET falls back to the full-page `correction_request.html` for direct navigation, and POST returns `204 + HX-Redirect` back to `/attendance/` (htmx, so pending badges refresh) or a normal redirect — same drawer-or-page/`_hx_redirect` split `policies` uses, and an invalid POST re-renders the same template with `status=400`. The employee's manager (via `JobInfo.reports_to` — `accounts.permissions::can_act_on_attendance_correction`, same `_in_managed_scope` rule as `can_act_on_leave`) or HR/Admins approve or decline it at `/attendance/corrections/`. Same request/decide shape as `leave.LeaveApplication` deliberately: `requested_by`/`requested_at` mirror `applied_by`/`applied_at`, `processed_by`/`processed_at`/`process_note` mirror the same fields there, and `Meta.ordering = ["-date", "-requested_at"]` mirrors the same reasoning as `LeaveApplication.Meta.ordering` — the list reads as "which working day was this about", so it's sorted on `date`, not the filing timestamp. The form also takes an optional `requested_time_in_office` (HH:MM:SS) override, separate from the two punch times: a first-punch/last-punch span can include time the employee wasn't actually in the building (e.g. punch in 07:00, out 23:00, but a long unrecorded break in between) — TimeLy has no data for that gap, only the two punches, so the employee states the real worked duration directly rather than it being re-derived from a span that's known to overcount. Left blank, it falls back to `check_out − check_in` same as the default sync (`AttendanceCorrection.effective_time_in_office`); the form and model both reject an override longer than the check-in/check-out span. Approval writes `requested_check_in`/`requested_check_out`/`effective_time_in_office` onto the target `AttendancePunch` with `source=MANUAL_CORRECTION`, creating the row if the date had no punch at all (the list's "Unaccounted" case). No approval-chain/audit-ledger split like `leave`/`policies` — a single review decision doesn't need one. While a request is still PENDING, the requester (or whoever could have filed it, per `can_request_attendance_correction`) can revise it in place via a right-hand drawer (`AttendanceCorrectionEditView`, `attendance/_correction_edit_drawer.html`) — the same edit icon appears on `/attendance/` next to "Correction pending" and on the "My requests" tab. It 403s once the request is no longer PENDING. `/attendance/corrections/` has its own q/department/team filter, styled as the same hover popover as `leave/list.html` (a `sliders-horizontal` button that highlights + shows the active query when a filter is set, `.menu-panel` dropdown with the cascading department/team selects) rather than an always-visible bar — a reload would close a plain popover mid-pick, so the panel is pure CSS `group-hover`/`group-focus-within`, no JS. Filtering itself goes through `accounts.filters::employee_filter_context` for the dropdown data, applied via `employee__department_id`/`employee__team_id` since the queryset is `AttendanceCorrection` not `CustomUser`; reused across all three tabs, and tab links carry the active filter forward via `{% qs_replace %}`. The popover also has an optional working-date range (`date_from`/`date_to` — distinct param names from `leave`'s `start_date`/`end_date` since a correction has one `date`, not a start/end pair), filtered via `attendance.views::_correction_date_range` — same inverted-range/malformed-date error handling as `leave.views::_leave_date_range`, reported back rather than silently dropped or applied wrong. **Bulk approve/decline** (shipped 2026-09-07): a checkbox per actionable row (the same PENDING+`can_act_on_attendance_correction` condition that shows the individual Approve/Decline buttons — `AttendanceCorrectionListView` precomputes `actionable_pks` for the current page so the header's "select all" checkbox and the row checkboxes agree on the same set) plus a toolbar ("Approve selected"/"Decline selected", only visible once ≥1 is checked, Alpine `x-show`/`x-cloak`) that submits to `AttendanceCorrectionBulkActionView` (`/attendance/corrections/bulk/`). The checkboxes live inside the `<table>` but the submit `<form>` doesn't wrap it — HTML5 `form="bulk-corrections-form"` on each checkbox and both toolbar buttons associates them with a separate, otherwise-empty `<form>` elsewhere in the DOM, since the table already has its own per-row single-action `<form>`s and forms can't nest. Every submitted id is still individually re-checked server-side (`is_pending` + `can_act_on_attendance_correction`) — a stale page, a tampered id, or a colleague having already acted on one of the selected rows just gets silently skipped rather than aborting the batch, and the result message reports both counts (`skipped = len(ids) - acted`, so even a nonexistent id is counted, not dropped). `_approve_correction`/`_decline_correction` module-level functions do the actual mutation, shared by the single-row and bulk views so the two can't drift apart.
- **Unaccounted flag** — a WORK-category resolved shift with no TimeLy punch at all (`check_in` null) for that date is flagged `⚠ Unaccounted` (`badge-warning`, gold — a real gap worth checking, not a hard error). LEAVE/OFF/HOLIDAY shifts and unscheduled days are never flagged; TimeLy folds WFH in as remote punches, so this isn't office-only — it's "scheduled to work, nothing recorded anywhere." A per-employee check, not a blanket weekday assumption, so 24/7 teams scheduled on a weekend/holiday are still checked normally while their teammates' regular off days are not flagged. `?flagged=1` filters the **entire** filtered/scoped employee set (not just the current page) down to just those rows, with a live count badge next to the toggle — doubles as a company-wide daily reconciliation check, not just a per-page spot check.

**`dynamic_menu`** — role-based navigation + view-level access control:
- Ported from **reicrm** (most mature of the three existing implementations — has `rank` ordering, direct URL support on `MainMenu`, and `get_absolute_url()` via `reverse()`).
- **Redesigned** to collapse the intermediate `GroupMainMenu` / `GroupSubMenu` tables into direct M2M fields on the menu models. See "Dynamic menu — model design" section below.
- Models: `MainMenu`, `SubMenu`, `ProtectedView`.
- Context processor injects deduplicated menu tree into every template.
- Decorator `@dynamic_allowed_users` enforces access on views that aren't in the sidebar.

**`theme`** — django-tailwind's generated app:
- `theme/static_src/` → Tailwind source (`input.css`, `tailwind.config.js` with Timely brand tokens).
- `theme/static/css/dist/styles.css` → compiled output.
- Project-level `templates/base.html` is the real base (not `theme/templates/base.html`).

**`policies`** — first in-house GRC slice (PDF policy distribution + employee sign-off; replaces the policy part of the commercial TeamMate / StandardFusion tenant). Spec: `docs/POLICY_MODULE_PLAN.md` (shipped 2026-06-24, ahead of the 2026-07-01 deadline).
- Models: `Policy` (UUID pk, auto `P-n` key via a `PolicyCounter` singleton + `select_for_update`, Draft→Active→Retired), `PolicyVersion` (append-a-row PDF versioning, `is_current`, `requires_reack`), `PolicyAcknowledgement` (denormalised current sign-state, one per (policy, employee)), `PolicyAcknowledgementEvent` (append-only audit ledger — ASSIGNED/NOTIFIED/REMINDED/SIGNED/SUPERSEDED/REVOKED). The denorm-state + event-ledger split mirrors `leave`'s `LeaveBalance` / `LeaveBalanceTransaction`.
- Adherent targeting: M2M `target_groups` (default **All Employees**) ∪ `custom_adherents` (picker filtered to active employees not already adherents/signers). `services.assign_adherents` materialises PENDING rows idempotently; publish/remind/supersede `bulk_create` events + fan out email synchronously on `transaction.on_commit` (Celery deferred — funcs written to wrap later).
- **Permissions are decoupled from HR**: `services.can_manage_policies` checks `POLICY_MANAGER_GROUPS = {"User Admins","Policy Managers"}` — deliberately NOT `accounts.can_edit_all`, so HR/NSA policy managers get policy power without employee-profile write scope. A data migration seeds the `Policy Managers` group + nav ("Policies" → "All Policies" for managers, "To Sign" for All Employees).
- Sign flow: authenticated click-to-acknowledge behind login/SSO, captures IP + user-agent + version. New `requires_reack` version supersedes prior signatures and re-notifies. Nightly `send_policy_reminders` management command + manual "Send reminders" button.
- **PDF viewer (sign page + manager preview)**: renders client-side onto `<canvas>` via `pdf.js` (`static/js/policy-pdf-viewer.js`) instead of the browser's native PDF plugin (`<iframe>`/direct link). A native plugin is a separate rendering surface page JS can't reach, so its own right-click "Save As" can't be suppressed; canvas is ordinary DOM, so `oncontextmenu` blocking on it actually works. Deterrent, not real prevention — the bytes still transit the network to render. Pages render at a capped comfortable width (840px, centered on a neutral backdrop) rather than stretched to the viewer's full width — matches native-viewer proportions instead of reading as "zoomed in". `pdf.js` (`pdfjs-dist` 6.2.108) is vendored at `static/vendor/pdfjs-6.2.108.min.js` + `...worker.min.js`, following the project's existing local-vendoring convention (alpine/htmx/flatpickr/lucide) rather than a CDN. Loaded via dynamic `import()` of a `{% static %}`-resolved URL (not a static top-level import) so it survives whitenoise's content-hashed filenames in production. **Vendored as `.js`, not pdf.js's native `.mjs`**: in production nginx serves `/static/` directly via `alias` (bypassing Django/whitenoise entirely — see "Configuration pattern" below), using its own `mime.types`, which has no `.mjs` entry and falls back to `application/octet-stream`; browsers strictly enforce MIME type for `<script type="module">` and dynamic `import()`, so `.mjs` silently failed to load in production (worked fine locally under `runserver`, which uses Python's `mimetypes`, masking the gap). `.js` has always mapped correctly in nginx's default `mime.types`, so renaming avoided touching the shared nginx config. The manager-facing `policy_detail.html` ("View current PDF" + per-version "Open" links) uses the same viewer via `PolicyPdfPreviewView` / `templates/policies/preview.html` rather than linking straight to `policies:pdf` — same no-download rationale, just without the sign page's accept bar.
- **Email config fix shipped with this app**: `core/settings.py` now wires `EMAIL_USE_SSL` (was missing — port 465 + SSL silently failed because Django defaulted to STARTTLS); TLS/SSL are now mutually exclusive.
- **MySQL collation gotcha**: this DB's schema default is `utf8mb4_unicode_ci` but existing tables (incl. `accounts_customuser.id` char(32) UUID PKs) are `utf8mb4_0900_ai_ci`. New tables inherited unicode_ci → cross-app UUID FKs were rejected as "incompatible". `policies/migrations/0001` is non-atomic and runs a MySQL-only `CONVERT TO ... COLLATE utf8mb4_0900_ai_ci` right after table creation (before any FK op) to match. Any future app with FKs to `CustomUser` needs the same step — **always**, not just "until the DB default is corrected". Checked 2026-09-19: **production's default has always been correct** (`utf8mb4_0900_ai_ci`, matching all 132 tables); it was the *dev* database that had drifted. The step is still required, because on a fresh database with the wrong default it is `accounts_customuser` that ends up mis-collated and the CONVERT steps then disagree with it — see `deploy/README.md`'s "MySQL collation" section.

**`payroll`** — replaces SEBPO's two disconnected Excel workbooks (the HR Operations compilation master and the Finance salary workbook) with one payroll run inside Orbis that reads employees / roster / leave / offboarding directly. Business process: `docs/PAYROLL_PROCESS.md` (awaiting formal HR/Finance sign-off). Technical spec: `docs/PAYROLL_PLAN.md`. **Phases 1–4 were built 2026-08-20/2026-08-24 ahead of sign-off, by explicit decision**; Phase 5+ still waits on it.
- **Period**: `PayrollPeriod` — one cycle named for the month it closes in, running the **16th of the previous month → the 15th**.
- **Phase 1, compensation master**: `SalaryStructure` (effective-dated, **gross only — no component breakdown**, locked 2026-08-11; SEBPO has no gratuity scheme, so `PayrollSettings` has no gratuity field). `Bank` / `BankBranch` / `EmployeeBankAccount` live here rather than in `employees` so salary + bank access is gated independently of `can_view_employee`.
- **Phase 2, rate tables + input pipeline**: `PayRate` (one table for OT, night-duty and transport), `MealDeductionTier` (tiered on the employee's own gross, not a `PayRate` row), `OvertimeType`, and the period inputs `OvertimeEntry` / `VariablePayEntry` / `TDSEntry` — all wrapped in a `PayrollInputBatch` upload envelope, the audit trail `roster/upload.py` lacks. Night-shift detection is **derived** from `Shift.shift_attribute.start_time`/`.end_time` crossing midnight; a `ShiftType.is_night_shift` field was added, shipped, then reverted because nothing ever set it.
- **Phase 3, engine + register**: `PayrollRun` → `PayrollRunEmployee` (snapshot fields) / `PayrollRunLine` / `PayrollRunAudit` (append-only, SET_NULL everywhere, mirroring `roster.ShiftChangeLog`) / `PayrollControlCheck` / `PayrollVariance`. Lifecycle is **only** DRAFT → INPUTS_OPEN → INPUTS_LOCKED → CALCULATING → CALCULATED (+ reopen/recalculate). **The engine is delete-and-rebuild, never patch** — every `calculate_run()` wipes this run's rows and regenerates them inside one transaction, so recalculation is idempotent. Employees missing a salary structure are flagged `MISSING_SALARY_STRUCTURE` and stay visible in the register rather than vanishing.
- **Phase 4, in-house TDS**: `TaxYear` (BD tax year, 1 Jul – 30 Jun) / `TaxCategory` / `TaxExemptionThreshold` / `TaxSlab` / `EmployeeTaxDeclaration`, computed in `payroll/tax.py` per ITA-2023 and hand-verified against Finance's own `Tax Calculation.xlsx`. This **reversed** the 2026-08-11 lock ("TDS is uploaded, never calculated"): calculation is now the default and a manual `TDSEntry` is an override the engine checks *first*. Orbis tracks a real `EmployeeTaxDeclaration` instead of Finance's hardcoded "Assumed Maximum" investment figure, and does **no YTD true-up** (matching Finance's own method — it reprojects annually and divides by 12).
- **Not built — Phase 5+, gated on sign-off**: approval chain (`PayrollRunApproval` / `PayrollRunDepartmentReview`), lock/paid/reversal states, payslips, bank advice, final settlement and Eid packages. **Salary loans shipped 2026-10-08** (`SalaryLoan`, `docs/SALARY_LOAN_PLAN.md`) — recovered on every calculated run; meal counts shipped earlier. **Don't add a status choice nothing transitions into** — an unused choice is exactly the silently-stale field this app has been burned by once already (see `PAYROLL_PLAN.md` Risk 2 and the `is_night_shift` revert).
- **Money display** (2026-10-05, `docs/MONEY_FORMAT_PLAN.md`): whole taka with thousands grouping everywhere — `|taka` template builtin, `core.exports.money_value`/`set_money_columns` in Excel, `core.money.format_taka` in Python, `money_display` in admin; a template-scan test enforces it.
- **Access control**: nav + `ProtectedView` gates are seeded in `accounts/management/commands/seed_orbis.py`. `payroll_ops` (report, overtime upload, runs) = `{User Admins, HR, Finance, Payroll, Payroll Admins}`; `payroll_salary` (the gross-salary register + upload) = **Payroll Admins only** (Finance until 2026-10-05). The salary views deliberately do **not** reuse `accounts.filters.effective_department_id` / `employee_filter_context`: those clamp anyone outside `ALL_DEPARTMENTS_GROUPS` to their own department, and Finance isn't in that set — gross-salary access is company-wide by design, so `payroll/views.py` keeps narrower local helpers that always treat the viewer as able to browse every department (text search is still shared). **Built 2026-10-05 (Phase 1 of `docs/PAYROLL_DATA_PROTECTION_PLAN.md`):** the Service Book salary history and bulk-onboarding workbook are HR-only (`can_view_salary_history`), every per-employee money column is a `core/fields.py` encrypted field (Fernet under `PAYROLL_FIELD_KEYS`), the Django admin hides money from everyone but superusers and Payroll Admins, and the register's money sort/filter/sum run in Python.


## Configuration pattern

Follows `reicrm` exactly: single `core/settings.py`, `python-dotenv` loaded in both `manage.py` and `settings.py` (`load_dotenv(BASE_DIR / ".env")`), `os.getenv("X")` / `os.getenv("X", default)` for reads. Static settings (`INSTALLED_APPS`, `MIDDLEWARE`, `TEMPLATES`) hardcoded in `settings.py`.

**Vars in `.env`** (grouped):
- Core: `SECRET_KEY`, `DJANGO_DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`.
- Database: `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`.
- Azure SSO (blank in MVP, filled when credentials arrive): `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`. (No `AZURE_REDIRECT_URI` env var needed — mozilla-django-oidc derives the callback from URL routing; Azure-side it's `https://orbis.sebpo.net/oidc/callback/` + `http://127.0.0.1:8000/oidc/callback/` for local dev.)
- TimeLy integration (Phase 3): `TIMELY_API_BASE_URL`, `TIMELY_API_TOKEN`.
- Static/media: optional overrides.

**Additions over reicrm's pattern** (small, worth including):
- Commit a `.env.example` (reicrm doesn't — painful for onboarding).
- Assert required vars loudly at settings import: raise `ImproperlyConfigured` if `SECRET_KEY` is missing. `AZURE_*` vars are not required in MVP — a conditional in `settings.py` enables the SSO code path only when `AZURE_CLIENT_ID` is present.

## Authentication — MVP details

1. `CustomUser` with `USERNAME_FIELD = 'email'`, `REQUIRED_FIELDS = ['username', 'first_name', 'last_name']`. Email is unique and the identity anchor.
2. Login view at `/login/` — custom form (email + password). Renders `templates/auth/login.html`:
   - Email field, password field, "Sign in" button (primary, teal).
   - Divider and a **"Sign in with Microsoft"** button behind `{% if sso_enabled %}` — template-gated, so it just doesn't render in MVP.
   - `{% if messages %}` for error display.
3. Logout view at `/logout/` — Django's built-in `LogoutView` (POST-only, CSRF-protected).
4. User CRUD for admins (a simple table + form) at `/users/`, `/users/create/`, `/users/<pk>/update/`, `/users/<pk>/delete/`, gated by `is_staff`. Mostly used now to edit existing users; SSO auto-creates them.
5. ~~`change_password` view~~ — removed once SSO became the only auth path. Users can't rotate Orbis passwords; they manage credentials in their Entra/Microsoft account.
6. Django admin at `/admin/` — superuser-only fallback. Surfaced in topbar dropdown only for `is_superuser`.
7. `LOGIN_URL = '/login/'`, `LOGIN_REDIRECT_URL = '/'`, `LOGOUT_REDIRECT_URL = '/login/'`.

### SSO phase (post-MVP, when credentials arrive)

1. Add `mozilla-django-oidc==4.*` to `requirements.txt`. No new DB tables / migrations.
2. In `core/settings.py`, add a conditional block (gated on `os.getenv('AZURE_CLIENT_ID')`) that:
   - Prepends `accounts.auth_backends.OrbisOIDCBackend` to `AUTHENTICATION_BACKENDS` (keep `ModelBackend` so password users still log in).
   - Sets `OIDC_RP_CLIENT_ID`, `OIDC_RP_CLIENT_SECRET`, `OIDC_RP_SIGN_ALGO='RS256'`, and the four `OIDC_OP_*` endpoints derived from `AZURE_TENANT_ID` (authorization, token, JWKS, user-info).
   - Sets `OIDC_RP_SCOPES = 'openid email profile User.Read User.Read.All'`. The `User.Read.All` scope (delegated, **admin-consent required**) is needed to read `employeeHireDate` and to expand the `manager` nav property — these are not available on the basic `User.Read` scope.
   - Optionally adds `mozilla_django_oidc.middleware.SessionRefresh` to `MIDDLEWARE`.
3. Create `accounts/auth_backends.py::OrbisOIDCBackend` (~40 lines): override `filter_users_by_claims` (link by email), `create_user` (raise `PermissionDenied` — no JIT), and `update_user` (sync names from id_token claims, then call Graph `/me?$select=employeeId,employeeHireDate,department,jobTitle,officeLocation,mobilePhone,companyName&$expand=manager($select=id,displayName,mail,jobTitle,userPrincipalName)` once and map results onto `JobInfo`).
4. In `core/urls.py`: `path('oidc/', include('mozilla_django_oidc.urls'))`. The library's natural callback path is used directly; no `/auth/redirect` alias.
5. In Azure: provision a **new dedicated Orbis app registration** (separate from veridoc's). Register both redirect URIs (`https://orbis.sebpo.net/oidc/callback/` and `http://127.0.0.1:8000/oidc/callback/`) under the Web platform. Grant Delegated `User.Read` and `User.Read.All` with admin consent.
6. Set `sso_enabled = True` in a template context processor → the login page's "Sign in with Microsoft" button appears.
7. **Pre-cutover validation gate**: before writing any of the above, prove that `employeeHireDate` and `manager` are populated for SEBPO accounts in Entra and reachable via delegated `User.Read.All`. Use standalone msal scripts against veridoc's existing app credentials — the data lives at the user/tenant level, not the app level, so any consented app sees the same shape. If validation fails (data not synced from Oracle Fusion, or admin consent withheld), reconsider the architecture before writing Orbis code.

**Local-bootstrap option** (test the full Orbis SSO code path before the new Orbis app registration is provisioned): Orbis can borrow veridoc's app credentials by mounting mozilla-django-oidc's views at `/accounts/microsoft/login/` and `/accounts/microsoft/login/callback/` — matching the localhost redirect URI veridoc already has registered (`http://127.0.0.1:8000/accounts/microsoft/login/callback/`). Set `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` in Orbis's `.env` to veridoc's values (live secret comes from veridoc's DB `SocialApp.objects.get(provider='microsoft').secret`, not the stale `settings.py` value). Stop veridoc's runserver to free port 8000. When the new Orbis app registration is ready, swap to `path('oidc/', include('mozilla_django_oidc.urls'))` and update `.env` — that's the whole cutover. Caveat: still requires Delegated `User.Read.All` admin-consented on veridoc's app to test the Graph enrichment path; if it's not, the login flow will fail at the consent step (same blocker as the new app would face — useful early signal).

No data migration or user-identity change is needed because email stays the `USERNAME_FIELD`.

## Tailwind + Timely brand tokens

`theme/static_src/tailwind.config.js` extends the default palette:

```js
brand: {
  DEFAULT: '#0A4958',   // dark teal
  light:   '#0F6070',
  accent:  '#41B3B3',   // bright teal
  navy:    '#0B333F',
  blue:    '#006F99',
  teal:    '#57B6B2',
  gold:    '#FFC638',
},
bg:   { main: '#F5F7F8', card: '#ffffff', soft: '#ECEFF1' },
text: { main: '#0A1A1A', soft: '#4B5C6B', mute: '#8A9AA6' },
```

Dark mode via `darkMode: 'class'` — toggles `class="dark"` on `<html>` from a topbar button, persisted in `localStorage`. Parallel dark palette via `dark:` variants.

Fonts: Figtree (400/500/600) + Roboto body fallback, loaded from Bunny Fonts (TimeLy's source, privacy-preserving Google Fonts proxy).

Base template (`templates/base.html`) has:
- Fixed 240px white sidebar with teal right-border active indicator.
- White topbar with shadow-sm, logo left, user menu + theme toggle right.
- Lucide icons via `lucide-static`.
- Blocks: `{% block title %}`, `{% block extra_head %}`, `{% block content %}`, `{% block extra_scripts %}`.

### App-shell layout (locked)

The shell is a **fixed-height frame**, not a scroll-the-whole-document page: `<body class="h-screen overflow-hidden">` → sidebar + a flex column whose only vertical scroll region is `<main class="flex-1 min-h-0 overflow-y-auto">`. Sidebar and topbar stay pinned, and so does a slim **footer** (`templates/partials/footer.html`) — a `shrink-0` sibling *after* `<main>`, outside the scroll region, carrying the copyright + "Designed and Developed by Software Development Department" line and a link to **Project Credits** (`/credits/`, data in `core/credits.py`, no model; also a last-in-the-rail "Project Credits" main menu, icon `award`, seeded by `seed_orbis`). **Reversed 2026-10-30:** the original rule was "no page footer (internal tool, no need for an org strip)"; management wanted the same copyright/credits treatment Procura has. Pinning it outside `<main>` (rather than at the end of the content) is what keeps `.page-fill` pages working unchanged — they just lose ~32px of table height; a footer inside `<main>` would overflow every `h-full` page and un-pin the pager.

Data-table list pages use a bounded inner-scroll pattern so the header pins and pagination sits at the bottom (Invelo-style):
- Content root = **`.page-fill`** (`flex flex-col gap-4 h-full`); filters/toolbars/tabs are `shrink-0`.
- Table wrapper = **`.card p-0 .table-scroll`** (`flex-1 min-h-0 overflow-auto`) — the bounded scroll container that makes `.table-sticky` (`thead th { position: sticky; top: 0 }`) actually pin. A plain `overflow-x-auto` wrapper does **not** work (unbounded scroll box). Multi-row headers pin only the first row.
- Pagination = shared **`templates/partials/_pagination.html`** — numbered range with ellipsis + First/Prev/Next/Last + a 25/50/100 size picker, preserving all GET params via the `qs_replace` tag in `dynamic_menu/templatetags/menu_extras.py`. A top-left total comes from **`templates/partials/_result_count.html`**.
- Page-size support: `ListView`s mix in **`core.pagination.PageSizeMixin`** (`paginate_by = 25`); custom report views (payroll, shift-summary) call **`core.pagination.paginate_list(request, rows)`**. Exports use the full unpaginated dataset.

See CLAUDE.md → "Page layout" for the full authoring rule.

## Dynamic menu — model design

Base comes from reicrm's `dynamic_menu` (the most feature-complete of reicrm/veridoc/ttms — has `rank`, `url` on `MainMenu`, and `get_absolute_url()`). Two changes during the port:

### 1. Collapse group-permission intermediate tables into direct M2M

**Problem today** (all three existing projects): `GroupMainMenu` and `GroupSubMenu` are intermediate tables with one row per `(group, menu)` pair. No `unique_together`. Context processor queries flat lists and doesn't call `.distinct()`. Result: a user in 3 groups that all grant the same submenu sees the link rendered 3 times. Admin UX: granting one URL to 5 groups means 5 separate admin rows across two admin pages with no inline widget.

**Redesign**: drop `GroupMainMenu` and `GroupSubMenu`; add a direct `groups = M2M(auth.Group)` field to `MainMenu` and to `SubMenu`.

```python
class MainMenu(models.Model):
    title = models.CharField(max_length=100)
    icon_class = models.CharField(max_length=100, blank=True)
    url = models.CharField(max_length=200, blank=True, help_text="Route name for main-menu-level links")
    rank = models.PositiveIntegerField(default=0)
    groups = models.ManyToManyField(Group, blank=True, related_name='main_menus')
    is_active = models.BooleanField(default=True)
    # audit fields: added_by, added_at, updated_by, updated_at
    class Meta:
        ordering = ['rank', 'title']

class SubMenu(models.Model):
    main_menu = models.ForeignKey(MainMenu, on_delete=models.CASCADE, related_name='submenus')
    title = models.CharField(max_length=100)
    url = models.CharField(max_length=200)
    rank = models.PositiveIntegerField(default=0)
    groups = models.ManyToManyField(Group, blank=True, related_name='sub_menus')
    is_active = models.BooleanField(default=True)
    # audit fields
    class Meta:
        ordering = ['main_menu__rank', 'rank', 'title']
```

Context processor (simple, deduplicated, prefetches to kill the N+1 template loop):

```python
def dynamic_menus(request):
    if not request.user.is_authenticated:
        return {'dynamic_main_menus': [], 'dynamic_sub_menus': []}
    groups = request.user.groups.all()
    main = (MainMenu.objects
            .filter(is_active=True, groups__in=groups)
            .distinct().order_by('rank', 'title')
            .prefetch_related('submenus'))
    subs = (SubMenu.objects
            .filter(is_active=True, main_menu__in=main, groups__in=groups)
            .distinct().order_by('main_menu__rank', 'rank', 'title'))
    return {'dynamic_main_menus': main, 'dynamic_sub_menus': subs}
```

Admin:
- Single `MainMenuAdmin` / `SubMenuAdmin` each with `filter_horizontal = ['groups']`.
- `SubMenu` shown as inline on `MainMenuAdmin` (`SubMenuInline`) so the whole menu tree edits on one screen.
- `GroupMainMenuAdmin` and `GroupSubMenuAdmin` are **deleted** — no more two-page shuffle.

Sidebar template (leverages prefetch, no N+1 re-filter):

```django
{% for main in dynamic_main_menus %}
  {% if main.url %}
    <li><a href="{% url main.url %}"><i class="{{ main.icon_class }}"></i>{{ main.title }}</a></li>
  {% else %}
    <li>
      <button data-collapse="#menu-{{ main.id }}"><i class="{{ main.icon_class }}"></i>{{ main.title }}</button>
      <ul id="menu-{{ main.id }}">
        {% for sub in main.submenus.all %}
          {% if sub in dynamic_sub_menus %}
            <li><a href="{% url sub.url %}">{{ sub.title }}</a></li>
          {% endif %}
        {% endfor %}
      </ul>
    </li>
  {% endif %}
{% endfor %}
```

### 2. Keep `ProtectedView` in scope

`ProtectedView` handles the URLs that aren't in the sidebar but still need access control — AJAX endpoints, action POST handlers, detail pages reached via links from other views. Ported from reicrm verbatim (only cosmetic cleanup):

```python
class ProtectedView(models.Model):
    registered_app = models.CharField(max_length=100)
    view_name = models.CharField(max_length=200, unique=True)
    view_description = models.CharField(max_length=300, blank=True)
    allowed_groups = models.ManyToManyField(Group, blank=True, related_name='protected_views')
    is_active = models.BooleanField(default=True)
    # audit fields
```

Decorator (ported from reicrm's `decorators.py`):

```python
def dynamic_allowed_users(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        view_name = view_func.__name__
        try:
            pv = ProtectedView.objects.get(view_name=view_name, is_active=True)
        except ProtectedView.DoesNotExist:
            # Unprotected views default to login-only (safer than defaulting to allow)
            return view_func(request, *args, **kwargs)
        if request.user.groups.filter(pk__in=pv.allowed_groups.values_list('pk', flat=True)).exists():
            return view_func(request, *args, **kwargs)
        return redirect('home')
    return wrapper
```

This gives non-dev admins the ability to grant/revoke view access at runtime without deploys — useful for managing the larger set of views once modules activate progressively.

**Seeding note**: a management command `python manage.py sync_protected_views` will scan `urls.py` across all apps and create `ProtectedView` rows for every view name it finds (leaving `allowed_groups` empty until admin fills it in). Otherwise the admin has to type view names by hand.

## Roster + leave — unified cell model

The roster grid needs to render both shifts and leave the same way — one cell per (employee, day). Example: employee 00001 → Monday 4/27 "Morning", Tuesday 4/28 "Casual Leave". Both values come from the same column in the template, so the underlying model needs a single source for the cell label.

**Decision**: keep ttms's pattern — one `ShiftType` table holds everything that can appear in a roster cell. Don't split out a separate `LeaveType` model.

```python
class ShiftType(models.Model):
    CATEGORY_CHOICES = [
        ('WORK',    'Working shift'),
        ('LEAVE',   'Leave'),
        ('HOLIDAY', 'Holiday'),
        ('OFF',     'Weekly off'),
    ]
    shift_type       = models.CharField(max_length=100, unique=True)  # "Morning", "Casual Leave", ...
    description      = models.CharField(max_length=255, blank=True)
    category         = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default='WORK')
    is_working_shift = models.BooleanField(default=True)  # derived from category; kept for payroll filter compatibility
    shift_timezone   = models.CharField(max_length=64, blank=True)
    shift_start_time = models.TimeField(null=True, blank=True)  # null for LEAVE / HOLIDAY / OFF
    shift_end_time   = models.TimeField(null=True, blank=True)
    has_attribute    = models.BooleanField(default=False)
    is_active        = models.BooleanField(default=True)
```

`Shift`, `Roster`, and `LeaveApplication.leave_type` all FK to `ShiftType`. The roster template renders `{{ cell.shift_type.shift_type }}` — no branching, no polymorphism, no nulls.

### Per-cell work location

Both `Roster` (the weekly pattern) and `Shift` (the materialized per-date row) inherit from a small abstract mixin `_LocatedRosterCell` that adds nullable `job_location` and `work_location` FKs plus a `location_badge` property. The badge resolves in this order: row's own `work_location` → row's `job_location` → employee's `CustomUser.work_location` → employee's `CustomUser.job_location` (via `accounts.locations.office_bucket`). So an employee whose contract says MHK gets `MHK` on every cell automatically; HR only sets the FKs when overriding ("Tue WFH" on an MHK employee).

The grid UI shows a single dropdown per cell with four canonical values (MHK / MBD / RAOWA / WFH); `accounts.locations.resolve_location_choice(code)` maps that to the underlying `(job_location, work_location)` FK pair and is idempotent (reuses existing bucketed rows, creates canonical rows only if missing). `prefill_shifts_from_pattern` carries locations from `Roster` → `Shift`, and `CellUpdateView` inherits the matching weekday's pattern locations when a cell-click creates a fresh `Shift`.

### Last-working-date cutoff (added 2026-09-11)

**Invariant: if an employee has a `CustomUser.last_working_date` (LWD), no `Shift` may exist for a date after it.** The LWD's own day is fine — the person worked through their last day, matching the "strictly past" convention offboarding's deactivation already uses (`last_working_date < today`).

Enforced on both sides:

- **Guards** (stop the rows appearing): `roster.services.prefill_shifts_from_pattern` (the week grid — without this, merely *viewing* the roster re-materialises a leaver's shifts from their weekly pattern forever), `roster.upload.apply_upload` (the bulk importer — reports these as `skipped_past_lwd`, a hard skip with no override), `roster.api.shift_writes` (the external batch write API — `conflict` / `past_last_working_date`; deleting a post-LWD row stays allowed), `roster.views.CellUpdateView` (the single-cell click-to-edit — a manual write, previously the one path with no LWD guard at all; added 2026-09-17 alongside the joining-date guard below, returns a `409` + `HX-Error-Message` toast, deleting a post-LWD row stays allowed same as the other guards), and `leave.services.materialise_shifts_for_application` (an approved leave whose range runs past a later-set LWD is clipped, not cancelled).
- **Purge** (`roster.services.purge_shifts_after_last_working_date`), wired into the three places an LWD gets set: the `Resignation` → ACCEPTED branch, a `CustomUser` save that changes the LWD (a new pre_save/post_save pair in `offboarding/signals.py`, which owns LWD semantics — covers the HR edit form and Django admin), and `offboarding.services.deactivate_past_lwd_users` as a nightly backstop. It must use a real queryset `.delete()`, never bulk SQL, so per-row `pre_delete` signals fire: the leave-ledger reversal, the CpL-grant reversal, and the `ShiftChangeLog` audit rows.

The weekly `Roster` pattern is deliberately **not** deleted — only its materialisation into `Shift` is gated, so a corrected or cleared LWD restores the shifts on the next grid view. `manage.py purge_shifts_past_lwd [--dry-run]` is an on-demand backfill for rows written before these guards existed; it is deliberately **not** on the celery-beat schedule, since the write paths above keep the data clean going forward.

### Joining-date cutoff (added 2026-09-17)

**Mirror-image invariant: no `Shift` may exist for a date before an employee's `CustomUser.joining_date`.** Found 2026-09-17 as a real-world bug — a new hire's roster week grid showed shift/location filled in on dates before she joined, because `prefill_shifts_from_pattern` only ever guarded the LWD (end) side, never the joining-date (start) side, so it happily bulk-created `Shift` rows from the new hire's auto-seeded weekly `Roster` pattern (`ensure_default_pattern` — EVE Mon–Fri / OFF Sat-Sun) onto pre-employment dates the moment anyone viewed a week spanning her join date. This had never been flagged before; there was no partial fix in place.

Guarded the same way, in the same write paths as the LWD guard above (`roster.services.shift_is_before_joining_date`, symmetric to `shift_is_after_last_working_date`): `roster.services.prefill_shifts_from_pattern` (the week grid), `roster.upload.apply_upload` (reports these as `skipped_before_joining`, also a hard skip with no override), `roster.api.shift_writes` (`conflict` / `before_joining_date`), and `roster.views.CellUpdateView` (`409` + `HX-Error-Message`, added alongside that view's first-ever LWD guard — see above). No purge command was written yet for pre-existing bad rows — see the follow-up DB-wide audit before assuming this is the last piece.

`core.dashboard.today_status_buckets` and `attendance.views._resolve_shifts`/`_resolve_shifts_matrix` have the identical read-only display gap (no persisted rows, but they'd still render a fabricated shift label for a pre-join date) — not yet fixed, lower urgency since they only ever resolve `today` or an explicitly-picked range rather than silently writing rows.

Out of scope, by decision: a `LeaveApplication` whose range extends past a later-set LWD is neither cancelled nor flagged — trimming it is a manual HR step, same as the separation payout.

**Read side (added 2026-09-11):** the pre-LWD `Shift` rows the guards above preserve must stay *reachable*, so `visible_employee_pks` / `get_visible_employees` / `visible_employees` take an `include_separated=False` kwarg (same opt-in shape `payroll_visible_employee_pks` already had, which is now a thin wrapper over it). It drops only the `is_active=True` clamp — permission scoping still runs through `managed_scope_pks`, so nobody sees anyone they couldn't see before. The first caller outside payroll was `roster.views._resolve_grid_employees`, which passes `include_separated=_search_active(request.GET)`: the default "my team" grid stays active-only, but a `?q=` / `?department=` / `?team=` / `?employee=` search surfaces leavers and their history (the export inherits this, since it reuses the same resolver). No UI toggle — search mode *is* the opt-in. Writes are unaffected: `CellUpdateView` still resolves the employee with `is_active=True`, so a separated employee's roster is readable but not editable. **The employee and user lists follow the same split (2026-10-01):** `EmployeeListView` (`/employees/`) and `UserListView` (`/users/`) pass `include_separated=accounts.filters.search_active(request)`, which is true only for a non-blank `?q=` — a department/team pick alone still shows the current headcount, but typing a name finds a deactivated person who was in the viewer's scope (a team leader still can't find a leaver from another department). Both templates already rendered an Active/Disabled badge per row, and the XLSX export reuses the same queryset. `can_view_employee` now reads the separated-inclusive scope too, so a manager who could see someone while they were active can still open their profile after deactivation; edit permissions keep their active-only checks.

### Where leave-specific rules live

`ShiftType` holds only what the roster needs. Leave-specific business rules (max days per year, paid/unpaid, certificate requirements, accrual, carry-forward) go on a separate `LeavePolicy` model that hangs off the leave-flavored `ShiftType` rows via OneToOne. This keeps working-shift rows clean (no irrelevant `max_days_per_year` field) while giving leave management everything it needs.

```python
class LeavePolicy(models.Model):
    shift_type             = models.OneToOneField(
        ShiftType, on_delete=models.CASCADE, related_name='leave_policy',
        limit_choices_to={'category': 'LEAVE'},
    )
    max_days_per_year      = models.PositiveIntegerField(default=0)
    is_paid                = models.BooleanField(default=True)
    requires_medical_cert  = models.BooleanField(default=False)
    carry_forward          = models.BooleanField(default=False)
    accrual_per_month      = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    # ...

class LeaveApplication(models.Model):
    STATUS = [('PENDING','Pending'),('APPROVED','Approved'),('DECLINED','Declined')]
    employee    = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name='leaves')
    leave_type  = models.ForeignKey(
        ShiftType, on_delete=models.PROTECT,
        limit_choices_to={'category': 'LEAVE', 'is_active': True},
    )
    start_date  = models.DateField()
    end_date    = models.DateField()
    note        = models.TextField(blank=True)
    status      = models.CharField(max_length=10, choices=STATUS, default='PENDING')
    applied_by  = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name='+')
    applied_at  = models.DateTimeField(auto_now_add=True)
    processed_by= models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    processed_at= models.DateTimeField(null=True, blank=True)
```

### Department-scoped shift attributes

`ShiftAttribute` (the named time-variant of a WORK `ShiftType`, e.g. "EVE 1-10") carries a `departments` M2M to `accounts.Department` (`blank=True`). Empty = visible in every department's roster dropdown (the default, and how every attribute behaves until someone curates it); non-empty = visible only to employees in one of those departments. Only WORK-category attributes are ever meaningfully scoped this way — LEAVE/HOLIDAY/OFF `ShiftType`s (and their attributes, if any) always show to everyone regardless of this field, since those aren't department-specific. `ShiftType` itself carries no such field — every WORK `ShiftType` today (CUST/EVE/MOR/NIG) has `has_attribute=True`, so the bare-type dropdown branch never fires for WORK; add the same M2M to `ShiftType` only if a WORK type without attributes is ever introduced.

`roster.services.visible_shift_types(department_id, include_attr_ids)` is the single filtering function (`roster/views.py` and `RosterPatternRowForm` both call it, replacing what used to be duplicated query logic). `include_attr_ids` is a required escape hatch, not an optional nicety: an employee's already-assigned attribute must always remain a selectable, correctly-`selected` option even once it's scoped out of new picks, or the weekly pattern editor's per-row `<select>` submits blank for an untouched row and silently deletes it (a real bug found while building this — see `roster/forms.py::RosterPatternRowForm.__init__`).

**"EVE 1-10" must never be scoped to specific departments.** `roster.services.ensure_default_pattern` hardcodes it as the Mon–Fri default for every new hire regardless of department; scoping it away from any department would mean new hires there land on an attribute invisible in their own dropdown from day one.

### Reporting queries stay clean

- Payroll (worked hours): `Shift.objects.filter(shift__category='WORK')`.
- Leave report: `LeaveApplication.objects.filter(status='APPROVED').select_related('leave_type__leave_policy')`.
- Holiday calendar: `Holiday` model is still separate (a holiday is a date, not a roster cell assignment; the ShiftType row 'Holiday' is what shows up in the cell when the `Roster` pattern falls on a holiday date).
- **OFF wins over GH when a holiday lands on an employee's weekly off.** A `Holiday`-calendar date only becomes a `GH` cell when it displaces an actual working shift. If the employee's `Roster` pattern already has that weekday as `OFF`, the `Shift` cell should stay `OFF` — that is correct, not a data error, and any GH/holiday-consistency audit (bulk-upload validation, payroll holiday pay, `CPL` auto-grant) must check the employee's weekday pattern before flagging a non-GH row on a holiday date. Confirmed 2026-08-31 while auditing the HR flat-roster bulk upload (`roster-upload-flat-HR-2026-08-30.xlsx`): 16 rows across 11 employees were `OFF` on a `Holiday` date purely because the holiday overlapped their scheduled day off — correct as-is.

### MVP impact

- In W4 (roster), we ship `ShiftType` with the `category` field but won't enable the `LEAVE` or `LeavePolicy` flows yet — just seed the WORK-category rows for SEBPO's actual shifts.
- In Phase 4 (leave module), we add LEAVE-category `ShiftType` rows, create matching `LeavePolicy` rows, and build the leave application + approval UI. Zero schema refactor needed at that point because the structure is already there.

## Weekly team roster email (added 2026-09-25)

Replaces the hand-built weekly schedule email managers send clients (e.g. Vox Media AP). One `roster.RosterEmailConfig` per team (To/CC, optional POC override — default the department head — intro/signature wording, and the "send automatically" switch), `RosterEmailMember` for per-person tweaks (a Role-column label like "Team Lead"/"Shift Lead" overriding `designation`, and `always_include`), and `RosterEmailLog` for each send attempt, which is the audit trail and the duplicate guard.

- **Who's listed** (`roster/emails.py::resolve_members`): the team's active members employed during the week, **minus trainees** (`JobInfo.resource_status == TRAINEE`, not live on the account yet). They drop off without anyone editing the email settings and reappear once HR sets them Live. **Bench stays in**, because bench covers sick calls. `always_include` adds someone from another team who covers this one (e.g. a night lead whose own team is CMX Creative), or forces a trainee on. They're listed with *their own* roster, and this deliberately isn't `Team.additional_managers`, since appearing on a client email must not grant roster/management scope. The drawer names the excluded trainees so a missing name is explained.

- **Rendering** (`roster/emails.py::build_context`) is read-only. Each cell resolves the Shift row first, then the weekly `Roster` pattern (same order as the grid), but it never calls `prefill_shifts_from_pattern`, because a scheduled job must not write roster rows. Days outside the joining date → last working date window render "—". Rows are grouped by each person's main WORK shift that week. Sat+Sun merge into one "Weekend" block only when every member is OFF on both days. Templates `templates/roster/email/weekly_roster.{html,txt}` use inline styles only, for Gmail and Outlook.
- **US time in the legend is DST-aware**: shift times are converted from the shift's timezone to `America/New_York` on the week's Monday and labelled with the real abbreviation (EDT/EST), never a fixed offset. Cell/legend text comes from `ShiftType.full_name` ("Night Shift" → cell "Night"), falling back to the code; `CELL_LABELS` overrides cell wording per code (NIG → "Overnight"). Cells never name the time variant ("NIG 9-6"), rows group by shift *type*, and the legend lists only the **team's own members'** shifts. An `always_include` person from another team shows in the grid, but their shift times aren't converted.
- **Sending as a team leader (2026-09-25, "option D")**: `RosterEmailConfig.sender` (optional) makes the email go out as `"Name (via Orbis)" <system address>`, with Reply-To and a CC to that person (CC skipped on TEST sends). **No per-user mailbox credentials are stored.** Storing leaders' Gmail app passwords was considered and rejected: full-mailbox scope, bypasses 2-step verification, and breaks on every password change. If a real personal From is ever needed, the upgrade path is Google Workspace SMTP relay (IP allow-listed, send as any @sebpo.com address; mail is Google Workspace despite Entra SSO) or Gmail API domain-wide delegation (lands in the sender's Sent folder, but adds a Google client library). The default signature signs as the sender.
- **Team copy** (`RosterEmailConfig.send_team_copy`, off by default): after each real client send (manual or scheduled), `roster/emails.py::send_roster` also sends **one separate email per person on the roster**, To: just them, with their row highlighted, trainees included and marked "(Trainee)". There are no client addresses on it, and that's why it's a separate send rather than BCC on the client email: a BCC'd teammate's Reply All would reach the client. Logged as `RosterEmailLog.kind = TEAM_COPY`, which never counts toward "this week was sent". A TEST send includes one sample copy to the tester only.
- **Sending**: from `DEFAULT_FROM_EMAIL` (under the sender's display name when set), Reply-To the sender or else the POC, via `core.tasks.defer(deliver_roster_email, log_pk)`, which marks the log SENT or FAILED. "Email roster" button on `/roster/` (visible when one team is selected and `accounts.permissions::can_send_roster_email` passes: HR/Admins, department head/additional managers, team additional managers, or anyone with a team member in their managed scope) opens a drawer with settings, a live preview, "Send test to me" and "Send to recipients". The drawer asks for confirmation before re-sending a week that already went out.
- **Schedule**: the `Send weekly team rosters` periodic task (a `django_celery_beat` row — retime or pause it in the admin), **Friday 22:00 Asia/Dhaka**, runs `manage.py send_weekly_rosters` for next week across active configs. It skips any team whose week already has a MANUAL or SCHEDULED log in QUEUED/SENT status. TEST sends never count, and a FAILED manual send doesn't block the schedule.

## Shift change requests (added 2026-10-01)

Spec: `docs/SHIFT_CHANGE_REQUEST_PLAN.md`. An employee (or HR / a manager on their behalf) requests a different shift **and/or work location** for one rostered date after the roster is published; the supervisor approves or declines. `roster.ShiftChangeRequest` copies `leave.LeaveApplication`'s request/decide shape, and the three permission helpers (`can_request_shift_change`, `can_act_on_shift_change`, `can_cancel_shift_change`) are one-line delegations to the leave ones, so the approval rules are the leave rules by construction.

- **One date per request.** `original_*` snapshot the cell at filing (Shift row, else weekly pattern — `original_was_concrete` says which) so the before/after diff is frozen. Locations are stored as the grid's four-value code (`MHK/MBD/RAOWA/WFH`), resolved to the two FKs only at apply time via `resolve_location_choice`, same as `CellUpdateView`.
- **Requestable types are WORK and OFF only.** LEAVE stays with `/leave/apply/`; a date already holding a LEAVE shift or a pending/approved application is refused ("cancel the leave first"). A request that changes neither shift nor location is refused; changing one of the two is fine.
- **Dropdown = the grid's dropdown.** `roster.services.shift_choice_groups` / `shift_ref_choices` (extracted from the cell view and the pattern form) build the options off `visible_shift_types(department_id)`, so department scoping is `ShiftAttribute.departments`, and `resolve_shift_ref` (moved from the views to services) re-validates the posted pk through the same filter.
- **Date window**: today or later for self-service; HR/Admins may backdate.
- **Approval** = `services.apply_shift_change`: re-runs the guards (leave approved since? attribute re-scoped?) then one `Shift.update_or_create` with the exact `defaults` set a grid edit writes — so `ShiftChangeLog`, the leave-ledger sync and the CpL signal all fire as usual. Nothing on `Shift` points back at the request, and **an approved change is never reversed from the request** (the cell may have been edited again since); only PENDING can be cancelled.
- **UI**: `/roster/change-requests/` (Mine / Pending / All tabs, `accounts.filters`), apply page with a live "currently on the roster" panel that pre-selects both dropdowns, detail page, htmx row actions. The sidebar's **Roster** entry became a section with two submenus (Roster, Shift Changes) — re-seed with `seed_orbis --menus-only` on prod. The dashboard's pending-approvals card has a row for it. On the grid, a cell with a pending request shows a "Change pending" badge, and a row the viewer can't edit now renders read-only text (with a "Request change" link on the viewer's own row) instead of a dropdown that 403'd on change.

## Roster API (for TimeLy)

Orbis is the source of truth; TimeLy pulls from Orbis.

**Endpoints** (all under `/api/v1/`, DRF, token-auth via `TIMELY_API_TOKEN` — a shared service-account token stored in Orbis DB + TimeLy `.env`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/roster/?from=YYYY-MM-DD&to=YYYY-MM-DD&location=...` | List roster entries in range, filterable by location/department/team |
| GET | `/api/v1/roster/{employee_id}/?from=...&to=...` | One employee's roster for date range |
| GET | `/api/v1/shift-types/` | List shift types + attributes (start/end times, flags) |
| GET | `/api/v1/holidays/?year=YYYY` | Holiday calendar |
| GET | `/api/v1/employees/?is_active=true` | Employee directory slice TimeLy needs (employee_id, email, name, department, location) |

**Optional reverse direction** (Phase 3, Orbis consumes TimeLy):

| Method | Path (on TimeLy) | Purpose |
|---|---|---|
| GET | `{TIMELY_API_BASE_URL}/api/attendance-summary/?date=...` | Pull processed attendance for display in Orbis reports |

Contract details (exact schemas, pagination, auth header) to be finalized with the TimeLy team before Phase 3 kickoff — not blocking MVP.

## Directory API (planned 2026-10-14)

Orbis replaces Entra (fed by the discontinued Oracle ERP) as the employee data source for other SEBPO applications: SSO stays the authentication layer, and after sign-in an app asks Orbis for the person's department, designation, team, office, supervisor and direct reports. New read-only endpoints under `/api/v1/directory/`; the TimeLy `/api/v1/employees/` shape is frozen. Introduces **per-consumer `ApiClient` tokens with scopes** (a new pattern, replacing one `.env` token per app) and stores the Entra object id on `CustomUser` (lookup order: object id → email → employee id). TimeLy is the first consumer; dsndd (served at rnc.sebpo.net) reads the roster and writes shifts. Roster history starts 2026-01-01. Developer docs are served in-app at `/developers/api/`, rendered from `docs/` Markdown with `markdown-it-py` (already a pinned requirement). **See `docs/DIRECTORY_API_PLAN.md`** for the spec.

## dsndd integration (shift/leave sync)

dsndd (a separate ~500-user team-scheduling app) is a client of Orbis, the same shape as TimeLy: dsndd pushes shift writes to a new token-authed Orbis batch endpoint and reuses the TimeLy read endpoints above (plus the shift-type catalog) for its own reporting — no local `ShiftType` model in dsndd, no webhook. **See `docs/DSNDD_INTEGRATION.md`** for the canonical spec (Orbis-side implementation status, dsndd-side plan, leave-ownership invariant, verification steps).

## What NOT to port from ttms/core

- Google OAuth receiver (`auth_receiver`) and related Google login code — we have no Google SSO plans.
- Password-reset URL patterns that reference missing templates — use Django's built-in `PasswordResetView` with our own templates.
- `download_mysql_backup` — security hole; use `mysqldump` via a proper backup cron outside the web tier.
- Hardcoded defaults (`SEBPO`, `DHK-MHK`, `WFO`, `country_id=1`) — seed via fixtures or admin, not `get_default_pk()`.
- The 34-migration history — start clean.
- `hours_to_seconds` templatetag bug (`* 360` should be `* 3600`) — fix on port.
- `qa_not_required`, `declaration` flags on `CustomUser` — appear unused; omit unless confirmed needed.
- `CoreShiftForm` filter `user_type='employee'` — field doesn't exist on current `CustomUser`; remove.
- `cron.py` with hardcoded `shift_id=5` for holidays — reparameterize via config.
- Integration with `operations.stored_procedures.spOpsDailyStats` — Orbis' own `payroll` engine reads roster/leave directly (see the `payroll` section above); the stored proc stays unported.

## Phased delivery — MVP by 2026-05-15

| Week | Dates | Scope | Exit criterion |
|---|---|---|---|
| **W1** | 04-20 → 04-26 | Scaffold: Django 5.2 project, `core/settings.py` + `.env`, MySQL connection, `django-tailwind` + `theme` app with Timely tokens, base template + topbar/sidebar partials, dark-mode toggle, `whitenoise`, `logs/` setup | App boots, `/` serves styled empty dashboard, Tailwind hot-reload works |
| **W2** | 04-27 → 05-03 | `accounts` app: `CustomUser` (UUID, email as username), org-structure models (Department/Designation/Team/SubTeam/AccessLevel/JobLocation/WorkLocation), **email/password login + logout + change-password**, admin user-CRUD views, `dynamic_menu` ported from veridoc, seed fixtures for departments/locations | Admin creates a user via `/users/create/`; that user logs in with email+password; nav renders based on their group |
| **W3** | 05-04 → 05-10 | `employees` app: **all** profile models (JobInfo, PersonalInfo, FamilyInfo, Education, Contact, Nominee, Email, IdentificationNumber, Reference, ProfessionalCertification, Vaccination, LegalIssue, Image, Address + Country/Division/State/City + Religion/Relation/Vaccine/Dose + Client), employee directory UI (list + detail + create + edit with all sections), AJAX load_states/load_cities/load_teams/load_employees | Browse all employees, open a profile, add/edit JobInfo + PersonalInfo + FamilyInfo + Education + Contact + Nominee + Address etc.; all forms persist correctly |
| **W4** | 05-11 → 05-15 | `roster` app: ShiftType/ShiftAttribute/Shift/Roster/Holiday models, weekly roster grid UI, shift CRUD, **Roster DRF API** (read-only endpoints for TimeLy), token auth, seed data (ShiftTypes for SEBPO's actual shifts), admin panels, deploy to `orbis.sebpo.net` | MVP demo: log in with email+password → browse employees → create/edit a full employee profile → see/edit roster → hit Roster API with a token and get JSON |

**MVP explicitly excludes** (deferred):
- Azure SSO (post-MVP phase — wired when credentials arrive).
- JIT provisioning from Graph (ditto).
- **Leave Policy v2.0 enforcement** — basic apply / approve / decline / cancel ships in Phase 4 (W4); the full SEBPO Leave Policy v2.0 (sandwich rule, balance ledger, 8 leave types with accrual + carry-forward, encashment, eligibility validators) is a separate post-MVP phase. **See `docs/LEAVE_POLICY_IMPLEMENTATION.md`** for the canonical spec.
- `attendance` TimeLy consumer (Phase 3). **(Shipped post-MVP, 2026-09-07: `AttendancePunch` local history + nightly sync + `/attendance/` list page — see the `attendance` section above.)**
- `payroll` (Phase 4). **(Phases 1–4 shipped post-MVP, 2026-08-20/2026-08-24:
  compensation master, rate tables + input pipeline, calculation engine +
  register, and in-house ITA-2023 TDS calculation — see the `payroll`
  section above and `docs/PAYROLL_PLAN.md`. Phase 5+ — approval chain,
  payslips, bank advice, loans, final settlement — still deferred, gated on
  HR/Finance sign-off of `docs/PAYROLL_PROCESS.md`.)**
- `onboarding`/`offboarding` (Phase 4).
- `reporting`/`dashboard` beyond a trivial landing page.
- Celery + Redis. **(Shipped post-MVP, 2026-07-21: Celery 5 + Redis —
  Redis backs the shared Django cache / cached_db sessions and brokers the
  Celery worker; celery-beat runs the periodic management commands.
  **Revised 2026-09-29: the schedule moved out of `CELERY_BEAT_SCHEDULE` in
  settings and into `django_celery_beat`'s tables, managed from the Django
  admin — new library + new `scheduler` app, see
  `docs/SCHEDULER_ADMIN_PLAN.md`. `CELERY_BEAT_SCHEDULE` must now stay `{}`;
  `DatabaseScheduler` overwrites the DB rows from it on every beat start.**
  All slow work — SMTP fan-outs via
  `core.notifications`, the chunked policy signing fan-out via
  `policies.tasks`, WeasyPrint renders via
  `documents.signals._post_approval`, holiday sandwich recomputes via
  `leave.tasks` — dispatches through `core.tasks.defer()`, which runs
  inline when `REDIS_URL` is unset (dev/test) or `TASK_QUEUE_ENABLED=0`.
  django-q2 was briefly wired first and swapped for Celery the same day,
  before ever reaching production.)
- Mobile-app hooks.
- Bulk import of existing ttms HR data (later decision).

## Requirements.txt (MVP target)

```
Django==5.2.*
python-dotenv==1.1.*
mysqlclient==2.2.*
djangorestframework==3.15.*
django-tailwind==3.*
django-htmx==1.23.*
whitenoise==6.*
pillow==11.*
requests==2.32.*
```

**Front-end libs loaded from CDN in `base.html`** (no Python package):
- `htmx` ~1.9 — partial-page HTML swaps for the employee-profile drawer UX and the roster grid.
- `alpine.js` ~3.14 — drawer open/close state + small interactivity bits.
- `lucide` icons — already in use.

The `employees` app (W3) uses a right-side drawer (slide-over) for every section edit — the modern HRIS pattern (Rippling / Deel / new Workday). Full-page URLs stay as a progressive-enhancement fallback, so every view works without JS and Django's form errors still render server-side. `django-htmx` provides the `request.htmx` attribute that views use to branch between "return full page" and "return fragment".

**Added in SSO phase** (post-MVP): `mozilla-django-oidc==4.*`. (`msal` is *not* needed for the login flow — added later only if/when we decide to do app-only background HR sync.)

**Added later, as modules activate**: `celery`, `redis`, `django-import-export`, `drf-spectacular`. `django-celery-beat` (+ `django-timezone-field`, `python-crontab`, `cron-descriptor`) landed 2026-09-29 with the `scheduler` app.

**Dev-only tooling (2026-11-03)**: `playwright` (in `tools/openproject/requirements.txt`, never on the server) drives the OpenProject sync. `tools/openproject/sync.py` and `timelog.py` keep op.sebpo.net/projects/orbis in step with `tools/openproject/inventory.yaml`, through a logged-in browser session because there is no API token. See `docs/OPENPROJECT.md`.

## Open items / risks

1. **Azure AD app registration**: credentials (tenant, client id, client secret) are not yet available. MVP is unblocked — uses Django built-in auth. Post-MVP phase begins the day credentials arrive.
2. **TimeLy (Laravel) API contract**: not needed for MVP (Roster API is Orbis-side and read-only), but needed before Phase 3. Schedule a short sync with the TimeLy team to agree on schemas.
3. **Existing employee data**: JIT would have provisioned users from Graph on SSO login — but we're on password auth now, so every new user must be manually created by an admin. For MVP demo, seed a handful of users via fixtures / admin. Bulk import from ttms DB is a later decision.
4. **`dynamic_menu` port**: resolved — porting from reicrm (most mature of the three existing implementations) and collapsing `GroupMainMenu`/`GroupSubMenu` into direct M2M on the menu models. `ProtectedView` kept for non-sidebar view access control. See "Dynamic menu — model design" section.
5. **W3 scope is ambitious**: 14 employee profile models + forms + templates in one week. User has accepted this risk and wants the full profile shipped; plan treats it as a hard requirement.
6. **Groups + permissions revisit (post-MVP)**: the current access model is "good enough for the demo" but has known rough edges. A dedicated pass is needed before prod rollout to more than a handful of users. Known issues and questions:
    - **`is_staff` is overloaded.** Today `is_staff=True` simultaneously grants: Django admin login, the user-CRUD pages in `accounts`, the "Shift types" CRUD, write access to roster cells via `StaffRequiredMixin`, and (via `roster.services.can_see_all`) visibility into every employee's roster + leave. The three are different concerns and should decouple.
    - **Group semantics need a rewrite.** Four groups exist today:
      - `User Admins` (pk 1) — sees all + can do user CRUD.
      - `HR` (pk 2) — sees all (via `can_see_all`).
      - `Roster Planner` (pk 3) — exists in the seed fixture, **unused in code**. Either wire it in (e.g. roster-cell edit for non-staff planners) or remove it.
      - `All Employees` (pk 4) — auto-populated, only used by the dynamic menu so Leave shows up for everyone. Fine as-is.
    - **`can_see_all` / `can_edit_pattern` / leave `_can_act_on` logic** lives in `roster/services.py` and is reused by `employees` and `leave`. Good — but the *policy* (who can see whose data) is encoded as Python `if`s, not config. Harder to audit + change for non-devs.
    - **`ProtectedView`** scaffold exists in `dynamic_menu` (including `dynamic_allowed_users` decorator + a `sync_protected_views` management command) but isn't actually used. Either seed it and gate non-sidebar views through it (the original intent), or remove the dead code.
    - **Fine-grained Django permissions** (`auth.Permission`) aren't used at all. Decision needed: stay with group-gated broad-strokes, or start using per-model permissions? The former is simpler; the latter is what the Django admin expects.
    - **Separation of duties on approvals**: today a `can_see_all` user can approve their own leave. Called out in the leave module PR but intentionally deferred — revisit once more than one admin exists in the org.
    - **Delegation**: no way for a manager to delegate their approval authority for a date range (when they're on leave themselves). Common HR pattern, worth planning.
    - **Permission tests**: none yet. Regression risk grows every time we touch the roster/services matrix.

## Verification (for MVP demo on 05-15)

1. `git clone && cp .env.example .env` → fill in DB creds + `SECRET_KEY` → `python manage.py migrate` → `python manage.py loaddata seed_departments seed_locations` → `python manage.py createsuperuser` → `python manage.py tailwind install && python manage.py tailwind build` → `python manage.py runserver`.
2. Visit `/` → redirected to `/login/` → log in with superuser email+password → land on dashboard. No "Sign in with Microsoft" button visible (gated off).
3. Go to `/users/create/` → create a regular user with email, password, department, designation → log out → log in as that user → dashboard renders with that user's menu.
4. Click "Employees" in sidebar → list of created users → click one → profile page renders with all sections → add JobInfo + PersonalInfo + FamilyInfo + an Education row + a Contact row + an Address; reload and verify persistence.
5. Click "Roster" → weekly grid loads with current user's shifts → create a roster entry for Mon-Fri → reload and verify.
6. `curl -H "Authorization: Token <t>" https://orbis.sebpo.net/api/v1/roster/?from=2026-05-11&to=2026-05-17` → returns JSON with expected employees/shifts.
7. Toggle dark mode in topbar → entire app repaints without flicker → reload page → dark mode persists.
8. Lighthouse a11y ≥ 90 on login + dashboard + employee list (sanity check, not hard requirement).

## Running tests

Tests use Django's built-in `TestCase`. Two ways to run:

- **MySQL (production-fidelity)** — requires `orbis_user` to have `CREATE`/`DROP` on `test_orbis_db`. One-time grant as MySQL root: `GRANT ALL PRIVILEGES ON test_orbis_db.* TO 'orbis_user'@'localhost'; FLUSH PRIVILEGES;`. Then `python manage.py test`.
- **SQLite (zero-infra)** — `python manage.py test --settings=core.settings_test`. The test-only settings module forces `DB_ENGINE=sqlite` and disables migration replay (Django builds the schema directly from current models). Necessary because `roster/migrations/0003_alter_shiftattribute_options_and_more.py` is hand-tuned for MySQL (`ALTER TABLE ... MODIFY COLUMN`, MySQL index quirks) and isn't SQLite-compatible.

Both paths run the same test code. Use SQLite for fast local feedback; use MySQL when validating engine-specific behavior or before merging schema changes.

## Media storage — Google Drive as an encrypted blob store (added 2026-10-10)

Media (every `FileField`/`ImageField`: profile pictures, signatures, leave and
offboarding attachments, legal-case documents, generated letters, policy PDFs,
payroll documents) must not live on the VPS, and IT cannot buy S3/GCS. The
`media_store` app (`docs/MEDIA_GOOGLE_DRIVE_PLAN.md`) makes the sebpo.com Google
Workspace Drive the backend, under three locked rules:

1. **Nothing on Drive is readable or identifiable outside Orbis.** Every blob is
   Fernet-encrypted on the VPS under `MEDIA_ENCRYPTION_KEYS` (separate from
   `PAYROLL_FIELD_KEYS`, same format) and stored flat in the app-created
   "Orbis Media" folder as `<32 hex>.bin`, `application/octet-stream`, with the
   real name/MIME/size only in the MySQL index (`media_store.DriveObject`) and,
   for recovery, encrypted in the blob's Drive `description`.
2. **Orbis is the only reader.** `/media/<name>` is served in every environment
   by `media_store.views.MediaServeView` (login + per-prefix authorisation in
   `media_store/rules.py`, each rule the owning page's own check; letters,
   policies and payroll documents stay 404 there because their own
   permission-checked views are the only path). nginx never serves `/media/`
   and the DEBUG `static()` helper is gone. Every fetch is an access-log row.
3. **Authenticated as the service mailbox, not a service account.** An OAuth
   refresh token (`manage.py media_drive_authorize`, scope `drive.file`) for
   `srv_orbis@sebpo.com` in production and `srv_meals@sebpo.com` for dev/test;
   no Shared Drive, no folder shared with anyone. Service accounts have had no
   Drive quota since 2025-04-15.

`MEDIA_BACKEND=filesystem` (default) keeps `FileSystemStorage` for dev, tests
and production until the Phase 2 flip; `drive` selects
`media_store.storage.GoogleDriveStorage`, which keeps a bounded **ciphertext**
read cache on local disk (`MEDIA_CACHE_ROOT`, LRU-pruned by the nightly
`prune_media_cache` beat job) and decrypts in memory after authorisation. The
Drive client is ~150 lines over `requests` (no `google-api-python-client`).
Uploads are synchronous in the request; `delete()` trashes (30-day undo);
`verify_media_drive` (weekly beat job) fails loudly on the first missing blob;
`rebuild_drive_index` regenerates the index from Drive alone.

## Org chart (added 2026-10-11)

`/employees/org-chart/` — who reports to whom, top to bottom, **built live
from `JobInfo.reports_to` on every request**. There is no stored chart, no
cache and no batch: a manager change saved in the Job Info drawer is on the
chart on the next page load. `employees/org_chart.py::build_org_tree` loads
every active employee and their `reports_to` pointer (two queries) and
assembles the forest in Python; `OrgChartView` picks a root, opens the first
tiers, and `templates/employees/_org_node.html` recurses. Deeper tiers are
fetched on demand by htmx (`employee_org_chart_node`, one manager's children
as a fragment) and toggled by Alpine, so the first paint is one leader plus
their direct reports however big the company is (`?levels=2|3|99` opens
more; a second tier of the 1,300-person tree is already ~9,000px wide).

Decisions:

- **It is a forest, not a tree.** Nobody above the C-level is recorded, so the
  page offers one chip per top-level leader (`?root=`), largest first, and an
  "Unlinked" chip for people with no manager and nobody under them. No
  synthetic company node is invented. If HR later records an MD above the
  leaders, the chips collapse to one by themselves.
- **Company-wide by design.** The cards carry name, designation, department
  and team only — the same information the sidebar's people list shows — so
  every logged-in employee sees the whole chart. A viewer's scope
  (`visible_employee_pks`) gates only the *profile links* on the cards, never
  the tree. No `ProtectedView` row; the sidebar entry is granted to
  `All Employees`.
- **Wide spans stay readable.** Children who manage people are laid out side
  by side under a horizontal bus; individual contributors stack in a single
  column off a left spine, twelve at a time with "Show all". Spans of 20–34
  direct reports exist in the real data.
- **Connectors are pure CSS** (`theme/static_src/src/styles.css`, `.org-*`):
  nested flex boxes with `::before`/`::after` rules, colour on `--org-conn`
  so htmx-loaded fragments inherit the theme. No chart library.
- **Data gaps are shown, not hidden.** A card whose manager has left renders
  dashed (`manager_inactive`); the HR group gets a collapsible "Data quality"
  strip listing unlinked people, those reporting to someone inactive, active
  users with no JobInfo row, and managers with 20+ direct reports. Each links
  to the profile (or focuses the chart) so it works as a fix-up queue.
- **Cycle-safe.** `reports_to` is free-form; a loop is cut at build time (the
  member nearest the loop becomes a root) rather than recursing forever.
- `?focus=<uuid>` opens the path to one person and highlights them ("Show my
  chain" is `?focus=<me>`); `?q=` searches name / designation / employee ID
  in the already-built tree — one hit focuses, several list as chips.
- Access log: `employee_` prefix → PROFILE for the page;
  `employee_org_chart_node` is an EXACT rule with `pk` as subject, and a
  focused person is noted as the page's subject.

Not built (deliberately): a static printable "leadership overview" in the
diagram-design editorial style (twelve boxes: the leaders and their units,
headcounts from the live tree). It would be a Django template rendered to
PDF through the existing WeasyPrint path; revisit when HR asks for a handout.

## Notifications — the topbar bell (v1 added 2026-10-16)

The bell lists **what is waiting on you**: pending leave applications,
timesheet corrections, shift change requests and dedicated night shift
assignments you can approve. It is **derived, not stored**
(`core/approvals.py::bell_summary`). Every refresh recomputes it from the
live pending rows, so an item disappears as soon as anyone acts on it. There
is no `Notification` table and no read/unread state that could drift.

Decisions:

- **Scope = direct reports first** (`approval_inbox_pks`). If you have any
  direct reports, the bell shows their requests. If you have none, it shows
  your managed scope (downline + department/team grants). This matches the
  default of the "Direct reports only" switch on all four list pages (leave
  had it from 2026-10-10; corrections, shift changes and dedicated night
  shift got it with the bell), so the bell's count equals the Pending tab it
  links to. The dashboard's pending-approvals rows use the same rule.
- **Dedicated night shift reaches past below-tier managers**
  (`night_shift_inbox_pks`). Only Manager tier and up can approve one, so a
  plain direct-reports rule would strand requests: in APM → TL → agent, the
  TL can't approve and the APM wouldn't see it by default. For night shifts,
  "direct" therefore means **you are the nearest Manager-tier person up the
  employee's `reports_to` chain**. That is your direct reports, plus anyone
  whose managers between you and them are below Manager tier. If no one in
  the chain qualifies, the request goes to everyone whose scope covers the
  employee. Below-tier viewers get no night-shift section at all
  (`has_dedicated_night_shift_approval_scope`).
  Org-wide visibility (HR's All tab) deliberately does **not** feed the bell:
  being able to see every request doesn't mean it is waiting on you. A
  skip-level manager isn't notified about their downline's requests, even
  though they may approve them (`can_act_on_leave`).
- **Real-time, v1 = htmx polling, no new infrastructure.**
  `partials/_notification_bell.html` polls `GET /notifications/bell/`:
  - on page load;
  - every 30s, only while the tab is visible;
  - when you come back to the tab;
  - on the `orbis:approvals-changed` event, which htmx action views send as
    `HX-Trigger` (leave and shift-change approve/decline/cancel).

  The response is the dropdown body plus an out-of-band badge, so an open
  dropdown stays open through a refresh. To reach other tabs, the fragment
  carries a signature of the pending set, and `base.html` writes it to
  `localStorage`; the `storage` event makes every other tab re-poll at once.
  This covers full-page POSTs (corrections approve/bulk) as well as htmx ones.
  The page title gets a `(n)` prefix.
- **Polling stays cheap.** The route is in `audit/registry.py::EXCLUDED_ROUTES`
  (otherwise every tab would write an access-log row every 30s). The scope pks
  are cached for 60s. A signed-out poll gets a bare 401 instead of a login
  redirect, so the login page never ends up inside the dropdown.
- **Adding a type** is one `BELL_TYPES` entry (with `eligible` when approval
  needs more than scope). Add it only once that type's list page applies the
  same scope rule; otherwise the bell and the page will disagree.

Later phases:

- **v2: WebSockets.** Push replaces polling, which needs an ASGI server
  (uvicorn workers or Channels with a Redis channel layer) and an nginx
  upgrade block. That is a deploy change, so it gets its own plan before
  anything is built. The fragment and endpoint stay the same; only the
  delivery changes.
- **Informational notifications** ("your leave was approved", a policy to
  sign) need a stored table with read state, written from the existing
  signals.
- **Escalation** (an item left untouched for N days moves up to the next
  manager) is a business rule that waits on HR.

## Post-MVP: Leave Policy v2.0

Full enforcement of SEBPO Leave Policy v2.0 (signed off 2021-12-29) is a dedicated post-MVP phase. It introduces a per-employee leave-balance ledger, the sandwich-leave rule (cross-application), monthly accrual + year-end carry-forward jobs, eligibility validators (gender, service length, surviving children for maternity), the encashment workflow, and a self-service balance dashboard.

**See `docs/LEAVE_POLICY_IMPLEMENTATION.md`** for the canonical spec — schema additions, sandwich algorithm, files to modify, verification matrix, and the operational decisions that override or refine the literal policy text (e.g., flat 1-day CpL per holiday-day worked instead of 2 for festivals; SL doctor-cert threshold of 3+ days; dept-head leave routes via `reports_to` chain + HR rather than COO).

## SSO integration — shipped state (supersedes the checklist that used to live here)

SSO shipped ahead of the original "post-MVP, when credentials arrive" phasing above (the two-phase auth plan in "Locked decisions" #4 and the "SSO phase" section below are historical — describing the *original* design, not what was actually built). Actual state, as of the **2026-09-01 SSO-only go-live**:

- `accounts/auth_backends.py::OrbisOIDCBackend` — full JIT (`OIDC_CREATE_USER=True`, default): any `sebpo.com` Entra account auto-provisions a `CustomUser` on first sign-in (`create_user`), enriched once from Graph (`_provision_from_claims`: names, employee_id, joining_date, department/designation/job_location, contact_number, manager → `JobInfo.reports_to`, including a manager "stub" account if the manager hasn't signed in yet).
- **Orbis is the system of record after that first login — there is no ongoing AAD sync.** `update_user` (called by mozilla-django-oidc on every *returning*-user login) is a deliberate no-op; it does not re-pull or re-write any field from Graph. An HR admin's edit in Orbis is never overwritten by a later SSO login. (Before 2026-09-01, `update_user` re-synced every field from Graph on every login, including overwriting admin edits — fixed for go-live.)
- `manage.py preprovision_sso_user` still exists for pre-seeding a `CustomUser` (email/name/employee_id/department) ahead of someone's first SSO login — e.g. Day 1 access for a new hire before Entra data is fully synced.
- **The one-time enrichment `get_or_create`s Department / Designation / JobLocation by the raw Entra string**, so a typo or an unabbreviated value in someone's Entra profile creates a duplicate row nobody added in Orbis (seen 2026-09-28: an intern's `department` "Human Resource" next to the real "Human Resources", and `officeLocation` "Monem Business District, Level-7 Dhaka 1205 BD" next to "MBD"). Fix the value in Entra, then fold the duplicate with `manage.py merge_reference <department|location|designation> --from <dup> --into <real> --apply` (dry-run without `--apply`; re-points every FK/M2M by reflection, collapses the fake `TeamAssignment`/`DesignationHistory` "transfer" the profile-form move left behind, refuses on a unique-constraint collision, then deletes the duplicate). The Django admin's delete bounces off `TeamAssignment.department` (PROTECT, hidden `related_name="+"`), which is why the command exists.
- **`/login/` keeps both options for now**: `templates/auth/login.html` always renders the email/password form; the "Sign in with Company SSO" button appears below it, gated on `sso_enabled`. A prior version of this change hid the password form whenever SSO was configured — reverted (2026-08-27) at the user's request to keep `ModelBackend` password login available on `/login/` alongside SSO, not just at `/admin/`. Revisit removing the password form for regular users as a separate decision, closer to or after the 2026-09-01 cutover.
- **`/admin/` password login (`ModelBackend`) is untouched and still live** for `is_staff`/superuser accounts — a break-glass path in case of an Entra outage.
- No background/Celery job pulls from AAD; all enrichment is synchronous, inline in the login request.
