# Orbis

Unified workforce-management platform for SEBPO. Consolidates employee directory, roster, leave, attendance, and reporting into a single Django application.

## Architecture & roadmap

See [`docs/PLAN.md`](docs/PLAN.md) for the full architecture plan, phased delivery schedule, and design decisions.

## Tech stack

- Django 5.2 LTS (Python 3.12)
- MySQL
- Tailwind CSS via `django-tailwind`
- Django REST Framework (for the Roster API consumed by TimeLy)
- Microsoft Entra SSO via `mozilla-django-oidc`

## Status

Post-MVP iteration. Core employee directory, roster, leave, attendance overlay,
dynamic menu, documents, and offboarding modules are present. See
[`docs/PLAN.md`](docs/PLAN.md) for the architecture source of truth.

## Getting started

For the full local development workflow, including SQLite bootstrap, Tailwind,
seed commands, and test commands, see
[`docs/LOCAL_DEVELOPMENT.md`](docs/LOCAL_DEVELOPMENT.md).

```bash
git clone <repo>
cd orbis
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # fill in SECRET_KEY + DB creds
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

**Tailwind build:** templates load compiled CSS from the `theme` Tailwind app.
Run this when `theme/static/css/dist/styles.css` is missing or when changing
styles/templates:

```bash
cd theme/static_src
npm install
npm run dev        # or: npm run build
```

**Database:** defaults to MySQL per `PLAN.md`. For a quick local smoke test
without MySQL, set `DB_ENGINE=sqlite` in `.env`.

## Project layout

```
orbis/
├── manage.py
├── core/           # Django project settings
├── accounts/       # auth, CustomUser, org structure
├── employees/      # HR profile, taxonomy
├── roster/         # shifts, roster, Roster API
├── leave/          # leave policy, balances, approval flow
├── attendance/     # TimeLy presence consumer
├── dynamic_menu/   # role-based nav + view access
├── documents/      # HR letters, templates, PDF verification
├── offboarding/    # resignation and clearance workflow
├── theme/          # Tailwind source + build output
├── templates/      # project-wide templates
├── static/         # project-wide static assets
└── docs/
    ├── PLAN.md
    └── LOCAL_DEVELOPMENT.md
```
