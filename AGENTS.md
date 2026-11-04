# Orbis - Codex Agent Guide

This guide is for developers using Codex in this repository. It should give an agent enough project context to make scoped, consistent changes without rediscovering the architecture every session.

## Read First

- Read `docs/PLAN.md` before any substantive implementation. It is the architecture source of truth for app boundaries, locked decisions, and business rules.
- Use `CLAUDE.md` as historical project context, but prefer the current code when it disagrees with older phase notes.
- Check `README.md` for the quick-start. This file also includes the local development workflow because the README is intentionally brief and some status text there is stale.
- Do not hardcode secrets or environment-specific values. `manage.py` and `core/settings.py` load `.env` with `python-dotenv`.

## Project Snapshot

Orbis is a Django 5.2 LTS workforce-management platform for SEBPO. It consolidates employee profiles, roster, leave, attendance overlays, dynamic navigation, HR documents, and offboarding into one app.

Current first-party apps:

- `accounts`: custom UUID user model, email identity, org structure, Microsoft Entra SSO via `mozilla-django-oidc`, hierarchy and permission helpers.
- `employees`: HR profile sections and taxonomy; profile edits use the htmx drawer pattern.
- `roster`: shift types, shifts, weekly roster, holiday records, upload importer, and TimeLy-facing DRF API under `/api/v1/roster/`.
- `leave`: SEBPO Leave Policy v2.0, balance ledger, sandwich-leave logic, accrual and rollover commands, shift-summary report.
- `attendance`: TimeLy office-presence client used by dashboard status overlays.
- `dynamic_menu`: group-based sidebar menus and `ProtectedView` access gating.
- `theme`: `django-tailwind` app and Tailwind build source.
- `documents`: HR letters, CKEditor-backed templates, bleach sanitisation, PDF generation, QR verification, approval flow.
- `offboarding`: resignation/termination flow, clearance steps, exit interviews, asset return, LWD deactivation, letter issuance.

Deployment context: production runs on a VPS with gunicorn, supervisor, nginx, and MySQL at `orbis.sebpo.net`.

## Local Development

Use Python 3.12.

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

For fast local bootstrap, edit `.env`:

```dotenv
SECRET_KEY=dev-only-secret-key
DJANGO_DEBUG=True
DB_ENGINE=sqlite
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
```

Then initialize and run:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

The app is available at `http://127.0.0.1:8000/`; admin is at `/admin/`.

For a MySQL-like local setup, leave `DB_ENGINE=mysql` and fill `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT` in `.env`.

If a developer has a production-like MySQL dump, follow `docs/LOCAL_DEVELOPMENT.md` -> "Restore A MySQL Backup". Never commit dumps or real `.env` files.

### Tailwind CSS

Templates load compiled Tailwind through `{% tailwind_css %}`. If
`theme/static/css/dist/styles.css` is missing or stale, localhost will render
without the project styling.

```bash
cd theme/static_src
npm install
npm run dev
```

Keep `npm run dev` running while changing templates or Tailwind classes.

For a production-style build:

```bash
cd theme/static_src
npm run build
```

`core/settings.py` reads `NPM_BIN_PATH` for `django-tailwind` commands, but the package scripts in `theme/static_src/package.json` are the most direct way to work on CSS.

### Useful Seed And Maintenance Commands

Run only the commands needed for the feature you are touching.

```bash
python manage.py seed_orbis
python manage.py sync_protected_views
python manage.py seed_shift_attributes
python manage.py seed_shifts
python manage.py seed_leave_policies
python manage.py seed_document_types
python manage.py seed_offboarding_workflow
```

Operational commands:

```bash
python manage.py apply_monthly_accrual
python manage.py apply_year_end_rollover
python manage.py deactivate_past_lwd
python manage.py timely_probe
```

## Tests And Verification

There is no custom test runner. Use Django's test command.

```bash
python manage.py test
```

Focused examples:

```bash
python manage.py test accounts.tests
python manage.py test dynamic_menu.tests
python manage.py test roster.tests roster.tests_upload
python manage.py test leave.tests leave.tests_shift_summary
```

For view/template-heavy work, also smoke-test the relevant page locally because much of the UX uses server-rendered templates, htmx fragments, Alpine state, and Tailwind classes.

For docs-only changes, tests are usually unnecessary; still check formatting and links manually.

## Coding Conventions

- Prefer existing app-local patterns over new abstractions.
- Keep settings in the single `core/settings.py`; do not introduce split settings modules.
- Read env vars with `os.getenv()` and safe defaults. Required secrets should fail loudly.
- Keep migrations clean and scoped. This project intentionally started with clean initial migrations rather than legacy migration history.
- Use Django ORM, forms, class-based/function-based view patterns already present in each app.
- For cross-app business logic, prefer service modules such as `leave/services.py`, `documents/services.py`, `offboarding/services.py`, and `roster/services.py`.
- Keep comments sparse and useful. Add comments only where they reduce real parsing effort.

## UI And Template Rules

- Preserve the TimeLy-inspired Orbis UI language: dense operational screens, restrained styling, teal brand accents, light canvas, dark-mode support.
- Form widgets should use `form-input` for text inputs, selects, textareas, dates, and numeric fields.
- Do not use `form-select`; it is not defined and causes unreadable native select options in dark mode.
- Use `form-checkbox` for checkboxes.
- For ModelForms, prefer `core.forms.StyledMixin`. If adding dynamic fields in `__init__`, call `self.style_widgets()` after adding them.
- Project-level templates live under `templates/`; app templates are also used where already established.
- The employee profile edit pattern is `SectionEditView` plus `templates/employees/_drawer.html`; preserve that htmx drawer workflow.

## Permissions And Navigation

- `accounts/permissions.py` is the main permission matrix. Expand tests there when changing visibility, edit, roster, or leave authorization logic.
- Dynamic menus are group-based via direct M2M on `MainMenu` and `SubMenu`; do not reintroduce intermediate `GroupMainMenu` or `GroupSubMenu` tables.
- Use `ProtectedView` for non-sidebar endpoints that still need access control, especially detail pages, action POSTs, and AJAX endpoints.
- After adding protected routes or changing route names, run:

```bash
python manage.py sync_protected_views
```

## App-Specific Notes

### Accounts

- `AUTH_USER_MODEL` is `accounts.CustomUser` with UUID primary keys and email as the identity anchor.
- SSO is enabled only when `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET` are present.
- The OIDC backend enriches users from Microsoft Graph; keep local-password and SSO behavior aligned with current templates and settings.
- Hierarchy comes from `employees.JobInfo.reports_to` and `Department.head_of_department`.

### Employees

- The profile has many section models. Before adding a new profile field, confirm whether it belongs on `CustomUser`, `JobInfo`, or a section model.
- AJAX taxonomy endpoints include states, cities, teams, and employees. Preserve the dependent-dropdown behavior when changing taxonomy models or forms.

### Roster

- `ShiftType` is the semantic center for work, off, leave, and holiday-like cells.
- `Shift` materializes dated roster cells; `Roster` stores weekday patterns.
- Per-cell location uses nullable `job_location` and `work_location`, falling back to the employee's contractual locations.
- The TimeLy API contract is covered by `roster/tests.py`; keep response fields stable unless the integration contract changes deliberately.
- Upload behavior lives in `roster/upload.py` and `roster/upload_views.py`.

### Leave

- Leave types are `ShiftType` rows with `category=LEAVE`; `LeavePolicy` is OneToOne with `ShiftType`.
- Balance changes must go through the append-only ledger model and service helpers.
- Be careful around sandwich leave, half-days, cancellation, approval, and year-end rollover. Add focused tests for any rule change.

### Attendance

- TimeLy integration should degrade cleanly when `TIMELY_API_BASE_URL` or `TIMELY_API_TOKEN` is blank.
- Dashboard presence display is heuristic because TimeLy does not expose every punch-direction detail.

### Documents

- Rich-text templates must be sanitised with bleach before rendering.
- Preserve context-variable validation and approval ordering.
- PDF generation and public verification are part of the user-facing contract; test both when changing issued-letter behavior.

### Offboarding

- `Resignation` is the workflow entry point for employee-submitted and HR-on-behalf cases.
- The final HR step can issue documents. Coordinate changes with `documents` services.
- `LastWorkingDateMiddleware` and `deactivate_past_lwd` enforce LWD-driven deactivation; do not bypass them casually.

## Git And Collaboration

- The worktree may contain user changes. Do not revert unrelated edits.
- Keep commits small and focused when asked to commit.
- Use present-tense imperative commit messages.
- If Codex authors the commit, use the co-author line requested by the project owner when applicable.

## Reference Projects

These sibling projects are read-only references. Do not modify them while working on Orbis.

- `/home/smrashel/djangoprojects/reicrm/`: dynamic menu, settings/env pattern, related Django conventions.
- `/home/smrashel/djangoprojects/veridoc/`: SSO adapter and custom-user reference.
- `/home/smrashel/djangoprojects/ttms/`: legacy workforce business logic reference.
