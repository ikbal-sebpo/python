# Local Development

This is the developer setup path for running Orbis locally.

## Requirements

- Python 3.12
- pip and venv
- Node.js and npm, only when working on Tailwind/CSS
- MySQL, unless using the SQLite bootstrap option

## Python Environment

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

For quick local development, edit `.env` and use SQLite:

```dotenv
SECRET_KEY=dev-only-secret-key
DJANGO_DEBUG=True
DB_ENGINE=sqlite
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
```

For MySQL, keep `DB_ENGINE=mysql` and set:

```dotenv
DB_NAME=orbis
DB_USER=orbis
DB_PASSWORD=
DB_HOST=localhost
DB_PORT=3306
```

## Restore A MySQL Backup

Do not commit database backups or real `.env` files to GitHub. Share backups
through a private channel.

These commands assume a SQL dump file such as `orbis_backup.sql` or
`orbis_backup.sql.gz`.

Create a local database and user:

```bash
mysql -u root -p
```

```sql
CREATE DATABASE orbis CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'orbis'@'localhost' IDENTIFIED BY 'replace-with-local-password';
GRANT ALL PRIVILEGES ON orbis.* TO 'orbis'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

Restore an uncompressed dump:

```bash
mysql -u orbis -p orbis < /path/to/orbis_backup.sql
```

Restore a gzipped dump:

```bash
gunzip -c /path/to/orbis_backup.sql.gz | mysql -u orbis -p orbis
```

Then update `.env`:

```dotenv
DB_ENGINE=mysql
DB_NAME=orbis
DB_USER=orbis
DB_PASSWORD=replace-with-local-password
DB_HOST=localhost
DB_PORT=3306
```

After restoring, run migrations in case the checked-out code is ahead of the
backup:

```bash
python manage.py migrate
```

If the backup came from production, avoid running destructive test or seed
commands against it unless you intentionally want to modify that local copy.

## Database And Server

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. Django admin is at `http://127.0.0.1:8000/admin/`.

## Tailwind CSS

The site templates load compiled Tailwind through `{% tailwind_css %}`. If
`theme/static/css/dist/styles.css` is missing or stale, localhost will render
without the project styling.

Install the Tailwind toolchain and start the watcher:

```bash
cd theme/static_src
npm install
npm run dev
```

Keep `npm run dev` running in a separate terminal while changing templates or
Tailwind classes. It writes the compiled CSS to
`theme/static/css/dist/styles.css`.

For a production-style CSS build:

```bash
cd theme/static_src
npm run build
```

## Optional Seed Commands

Run only the seeds needed for the area you are testing.

```bash
python manage.py seed_orbis
python manage.py sync_protected_views
python manage.py seed_shift_attributes
python manage.py seed_shifts
python manage.py seed_leave_policies
python manage.py seed_document_types
python manage.py seed_offboarding_workflow
```

## Tests

```bash
python manage.py test
```

Focused suites:

```bash
python manage.py test accounts.tests
python manage.py test dynamic_menu.tests
python manage.py test roster.tests roster.tests_upload
python manage.py test leave.tests leave.tests_shift_summary
```

## External Integrations

Leave these blank unless you are explicitly testing the integration:

- `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` for Microsoft Entra SSO.
- `TIMELY_API_BASE_URL`, `TIMELY_API_TOKEN` for TimeLy attendance overlays.

When the TimeLy variables are blank, the dashboard presence overlay should degrade without blocking local development.
