# Deployment and Operations

- **Production:** https://orbis.sebpo.net. One VPS runs gunicorn, Celery and celery-beat under supervisor, with nginx in front for TLS and MySQL behind.
- **Deploying:** pull with a read-only deploy key, then run the update script (migrate, collectstatic, restart). Seeds such as `seed_orbis --menus-only` are a manual follow-up step. The runbook is `deploy/README.md`.
- **Scheduled jobs:** these are `PeriodicTask` rows in the Django admin. Run history is at `/admin/scheduler/scheduledtaskrun/`.
- **Secrets:** settings come from `.env` only. The payroll and media encryption keys are kept separately from `SECRET_KEY`.
- **Backups and rollback:** see `deploy/README.md`, which covers the payroll data protection and media-on-Drive sections.

The open production follow-ups are listed under the **Infrastructure & deploy → Production follow-ups** epic.
