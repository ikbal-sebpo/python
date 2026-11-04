# Architecture

- **Stack:** Django 5.2 LTS on Python 3.12, MySQL, a single `core/settings.py` with `.env`, Tailwind (django-tailwind), htmx and Alpine.
- **Hosting:** a single VPS running gunicorn under supervisor, behind nginx with TLS.
- **Background and scheduled work:** Celery workers with celery-beat. The schedule is stored in the database and managed from the Django admin; there is no cron.
- **Identity:** `CustomUser` (UUID pk, email as username). Microsoft Entra SSO with just-in-time onboarding for sebpo.com accounts. Access is scope-based: the reporting line (`JobInfo.reports_to`) plus department and team grants.
- **Integrations:**
  - **TimeLy:** Orbis pushes the roster; TimeLy supplies office presence and punches.
  - **dsndd:** roster read/write API.
  - **Directory API:** Orbis replaces Entra/Oracle as the source of employee data for other apps.
- **Data protection:**
  - Payroll money is encrypted at rest (Fernet) and decrypted only in Python.
  - Media files live on Google Drive as encrypted blobs and are served only through a permission-checked view.
  - An append-only access log records who read what.
- **Unified roster cell:** a single `ShiftType` table covers work, off and leave. Leave rules hang off a `LeavePolicy` that is one-to-one with its `ShiftType`, so roster entries and leave share one cell model.

Source of truth: `docs/PLAN.md` in the repository.
