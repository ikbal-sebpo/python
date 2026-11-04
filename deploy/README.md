# Orbis deployment notes

Server-side setup that cannot be expressed in the repo. The actual deploy
scripts (`deploy_orbis.sh` / `update_orbis.sh`) are gitignored because they
embed secrets — this file is the checklist they must satisfy.

## Day-to-day deploy (push a merged `main` to production)

SSH access is `orbis-prod` (an alias in `~/.ssh/config` on the machine doing
the deploy — `ubuntu@<host>:2221`, key-based). The whole flow:

```sh
git push origin main
ssh orbis-prod "cd ~/orbis && ./update_orbis.sh"
```

`update_orbis.sh` (present only on the server, not in the repo) does, in
order: `git fetch` + `git reset --hard origin/main` (so any local drift on
the server is discarded — the server is never the source of truth),
`pip install -r requirements.txt`, `manage.py migrate`, `manage.py
tailwind build`, `manage.py collectstatic`, then `sudo supervisorctl
restart orbis_gunicorn` and waits for it to report `RUNNING`. Takes under a
minute when there's nothing new to install.

**Updated 2026-09-01 — it now also restarts the Celery worker + beat**,
via `sudo -n supervisorctl restart celery_orbis celery_beat_orbis` (the
`-n` makes it non-interactive: it fails fast instead of hanging on a
password prompt if the sudoers rule is ever missing). This is gated to
not abort the rest of the script (`update_orbis.sh` has `set -e`) — if it
ever fails, it prints a warning and the manual fallback command instead of
breaking the deploy.

**The actual supervisor program names are `celery_orbis` / `celery_beat_
orbis`** (from `/etc/supervisor/conf.d/celery_orbis.conf` and `celery_
beat_orbis.conf`) — **not** `orbis-celery`/`orbis-celery-beat`, which is
what this doc, `update_orbis.sh`, and the sudoers rule all incorrectly
assumed until this was caught while wiring up the restart (2026-09-01).
Both `update_orbis.sh` and the sudoers rule below are now fixed to match
what's actually configured.

**Sudoers NOPASSWD rule — added 2026-09-01**, matching the existing
`orbis_gunicorn` rule's format in `/etc/sudoers.d/orbis-deploy` (edit via
`sudo visudo -f /etc/sudoers.d/orbis-deploy`, never a plain text editor —
a syntax error there can lock out all sudo access):

```
ubuntu ALL=(root) NOPASSWD: /usr/bin/supervisorctl restart orbis_gunicorn, /usr/bin/supervisorctl status orbis_gunicorn
ubuntu ALL=(root) NOPASSWD: /usr/bin/supervisorctl restart celery_orbis celery_beat_orbis, /usr/bin/supervisorctl status celery_orbis celery_beat_orbis
```

Verified working: `sudo -n supervisorctl restart celery_orbis celery_beat_orbis` restarts both with no password prompt. Manual fallback, if the rule is ever missing again:

```sh
ssh orbis-prod "sudo supervisorctl restart celery_orbis celery_beat_orbis"
```

**Verify after deploying** — curling the public hostname *from the server
itself* hangs (no hairpin NAT back to its own public IP); go through nginx
on localhost with an explicit Host header instead:

```sh
ssh orbis-prod "curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: orbis.sebpo.net' https://127.0.0.1/login/"
```

A `502`/hang after restart means gunicorn didn't come back up — check
`~/orbis/logs/gunicorn-error.log` and `sudo supervisorctl status`.

No migration was needed for the bulk-onboarding feature (2026-08-31) — pure
new views/templates on existing models, so `update_orbis.sh`'s `migrate`
step was a no-op. When a change *does* add a migration, there's no separate
approval gate here — `update_orbis.sh` runs `migrate --noinput`
unconditionally as part of the normal flow.

**Git remote — SSH deploy key (fixed 2026-08-31).** The server used to clone
over `https://` with an account-wide personal access token embedded in
plaintext in the remote URL. Replaced with a dedicated, **read-only**
GitHub deploy key registered on just this repo:

```sh
ssh-keygen -t ed25519 -N '' -C 'orbis deploy key (read-only)' -f ~/.ssh/orbis_github_deploy
# Register the .pub as a read-only deploy key on smrashel/orbis, e.g. from a
# machine with `gh` authenticated:
gh repo deploy-key add ~/.ssh/orbis_github_deploy.pub --repo smrashel/orbis \
    --title "orbis-prod (read-only, update_orbis.sh)"
```

then an `~/.ssh/config` entry on the server:

```
Host github-orbis-deploy
    HostName github.com
    User git
    IdentityFile ~/.ssh/orbis_github_deploy
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
```

and `git -C ~/orbis remote set-url origin git@github-orbis-deploy:smrashel/orbis.git`.
`update_orbis.sh` never pushes, only fetches — a read-only key is enough,
and `deploy_orbis.sh` (the fresh-VPS provisioning script) was updated the
same way so a new box doesn't reintroduce the plaintext token. **Every VPS
needs its own keypair** — never copy the private key between servers; see
`deploy_orbis.sh`'s header comment for the from-scratch steps.

The old PAT was NOT revoked as part of this — it's an account-wide token
also embedded (as of 2026-08-31) in `veridoc/.git/config` and
`resumeintel/.git/config` on the same server, and once in `.bash_history`.
Revoking it needs those migrated to their own deploy keys first, or it
breaks their deploys too — tracked as a follow-up, not done here.

## Redis (cache + Celery broker)

```sh
apt install redis-server        # binds 127.0.0.1 by default — keep it that way
```

Set `REDIS_URL=redis://127.0.0.1:6379/1` in `.env`. This enables the shared
Django cache, cached_db sessions, and the Celery broker (see
`core/settings.py`).

## Celery worker + beat

Email fan-outs (policy publishes to All Employees included), WeasyPrint PDF
renders, and holiday recomputes run through `core.tasks.defer()` on the
Celery worker. Periodic jobs (leave accrual, AL cycle close, shift-usage
reconciliation, year-end rollover, policy reminders, LWD deactivation,
attendance sync, weekly roster email, `clearsessions`) are scheduled by
celery-beat — without beat running, they silently never fire.

**Since 2026-09-29 the schedule lives in the database**, not in
`core/settings.py`: `CELERY_BEAT_SCHEDULER` points at
`django_celery_beat.schedulers:DatabaseScheduler`, and the jobs are rows under
`/admin/django_celery_beat/periodictask/`. Consequences for ops:

- **Pausing or retiming a job is an admin edit, not a deploy.** Beat re-reads
  the database within ~5s; no restart is needed. The admin's "Run selected
  tasks" action fires an ad-hoc run.
- **`CELERY_BEAT_SCHEDULE` must stay empty.** Any entry there is written over
  the database row on every beat start, silently reverting admin edits.
- **`celerybeat-schedule` (the shelve file) is no longer used.** Beat keeps its
  sync state in `django_celery_beat_periodictasks`. A leftover file on disk is
  harmless; delete it if it confuses anyone.
- **No supervisor change was needed** — `CELERY_BEAT_SCHEDULER` is a settings
  key, so the `celery -A core beat` command line below is unchanged (no `-S`
  flag).
- **Fresh install / first deploy**: `manage.py migrate` creates the tables and
  seeds the 16 jobs (`scheduler/migrations/0002_seed_beat_schedule.py`).
  `manage.py seed_beat_schedule` is the re-runnable equivalent and is safe on
  every deploy — it is get-or-create only and never overwrites an existing row.
- **"Did the nightly run?"** is now `/admin/scheduler/scheduledtaskrun/`, which
  logs status, duration and the command's captured output per run. The logs
  below are the second stop.

Full detail: `docs/SCHEDULER_ADMIN_PLAN.md`.
Actual supervisor programs (`/etc/supervisor/conf.d/celery_orbis.conf` /
`celery_beat_orbis.conf` — verified 2026-09-01; program names are
`celery_orbis` / `celery_beat_orbis`, not the `orbis-celery*` this doc used
to say):

```ini
[program:celery_orbis]
directory=/home/ubuntu/orbis
command=/home/ubuntu/orbis/venv/bin/celery -A core worker --loglevel=INFO --concurrency=2
user=ubuntu
autostart=true
autorestart=true
startsecs=10
stopwaitsecs=310          ; > CELERY_TASK_TIME_LIMIT so tasks finish on restart
stderr_logfile=/home/ubuntu/orbis/logs/celery.err.log
stdout_logfile=/home/ubuntu/orbis/logs/celery.out.log
environment=HOME="/home/ubuntu",USER="ubuntu"

[program:celery_beat_orbis]
directory=/home/ubuntu/orbis
command=/home/ubuntu/orbis/venv/bin/celery -A core beat --loglevel=INFO
user=ubuntu
autostart=true
autorestart=true
startsecs=10
stderr_logfile=/home/ubuntu/orbis/logs/celery_beat.err.log
stdout_logfile=/home/ubuntu/orbis/logs/celery_beat.out.log
environment=HOME="/home/ubuntu",USER="ubuntu"
```

**`update_orbis.sh` now restarts both `celery_orbis` and `celery_beat_orbis`
after `orbis_gunicorn`** (fixed 2026-09-01, see above) — any deploy that
changes task code or anything the workers import now picks up cleanly with no
manual step. (Schedule changes no longer need a restart at all; they are
database rows.)

If Redis/worker are down, `defer()` logs and
falls back to inline execution — slow requests, but nothing is dropped.
`TASK_QUEUE_ENABLED=0` in `.env` forces inline mode without removing Redis
caching. The scheduled commands remain ordinary management commands and can
always be run by hand via `manage.py`.

## TLS — DONE (nginx + Django)

**Completed 2026-08-29.** nginx terminates TLS, the redirect is in place, and
the Django half was finished the same day (commit b395964).

nginx:
- `sites-available/orbis` serves `listen 443 ssl` with the SEBPO wildcard
  cert (`/home/noc/ssl_cert_new/`), and port 80 `return 301 https://...`.
  Verified: `Host: orbis.sebpo.net` on :80 → 301, on :443 → 302.
- `BEHIND_TLS_PROXY=1` is set in the production `.env`.

Django: the `BEHIND_TLS_PROXY` block in `core/settings.py` was commented out
from the cert-procurement window until 2026-08-29, so the `.env` flag was a
no-op for months. Uncommenting it fixed that. Measured in production after
the deploy:

    SECURE_PROXY_SSL_HEADER  ('HTTP_X_FORWARDED_PROTO','https')
    SESSION_COOKIE_SECURE    True      CSRF_COOKIE_SECURE  True
    USE_X_FORWARDED_HOST     True      SECURE_HSTS_SECONDS 0

`check --deploy` went from four warnings to two: W012 and W016 cleared. W004
is HSTS, deliberately still off (below). W008 is `SECURE_SSL_REDIRECT`, which
stays False on purpose — nginx does the redirect, so Django doing it too
would just add a wasted round trip. Verified afterwards that `/login/`
returns 200 and `Set-Cookie` carries `Secure`.

What that fixed (the state to avoid regressing to):
- **Session and CSRF cookies are not marked Secure.** A browser will send
  them on the initial plaintext request to `http://orbis.sebpo.net` *before*
  nginx's 301 redirect, so the session id is recoverable from the wire — the
  very thing the redirect was meant to stop.
- **Django does not know it is behind TLS.** With no
  `SECURE_PROXY_SSL_HEADER`, `request.is_secure()` is False, so anything
  building an absolute URL server-side can emit `http://` — including the
  `documents` app's public QR-verify links.

**HSTS is still off and is the one remaining step.** `SECURE_HSTS_SECONDS`
reads from `.env` and defaults to 0, so nothing sets it today. To enable,
add `SECURE_HSTS_SECONDS=3600` to the production `.env` and restart — no code
change needed. Raise it in stages (an hour, then a day, then a year): browsers
cache HSTS, so an over-long value is very hard to walk back if anything on
the domain ever needs plain HTTP. Only add `includeSubDomains`/`preload` once
every host under sebpo.net is known to be HTTPS-only.

**Still owed regardless: rotate `TIMELY_API_TOKEN`.** It travelled in
cleartext for the whole pre-TLS period. Whether this was ever done is
unrecorded — assume not.

## /media/ — served by Orbis, never by nginx (media_store, 2026-10-10)

**History.** Until 2026-10-10 the live config had a bare
`location /media/ { alias /home/ubuntu/orbis/media/; }` and Django only served
`/media/` under DEBUG, so every upload (staff photos with guessable
`<employee_id>_<name>` filenames, payroll workbooks, letters) was fetchable by
anyone with no session — verified 2026-08-29 and re-confirmed 2026-10-05
(`docs/PAYROLL_DATA_PROTECTION_PLAN.md` §2.2). Earlier notes said the per-prefix
`deny all` blocks were never applied; in fact three were live
(`/media/payroll/`, `/media/documents/`, `/media/legal/`), so payroll and letter
files were blocked but profile pictures, employee images, leave and offboarding
attachments were not. The Phase 1 deploy left the alias in place too.

**Applied 2026-10-16:** all four blocks (the three denies + the alias) removed
from `/etc/nginx/sites-enabled/orbis`, nginx reloaded; an anonymous
`/media/profile_pictures/x.jpg` now returns `302` to login. Backup of the old
config: `~/nginx-orbis.bak-2026-10-16` on the VPS.

**Now.** `core/urls.py` routes `media/<path:name>` to
`media_store.views.MediaServeView` in every environment: login required, the
file is resolved to its owning row and the owning page's own permission check
applied (`media_store/rules.py`), every fetch is an access-log row, and
`payroll/`, `documents/generated/`, `policies/` are 404 there because their
dedicated views are the only path. This holds on the filesystem backend today
and on Google Drive after the flip (`docs/MEDIA_GOOGLE_DRIVE_PLAN.md`).

**nginx change (do at the Phase 1 deploy):** delete the `/media/` alias block
from `/etc/nginx/sites-enabled/orbis` so `/media/` proxies to gunicorn like
every other path, then:

```
sudo nginx -t && sudo systemctl reload nginx
curl -sI https://orbis.sebpo.net/media/profile_pictures/x.jpg | head -1   # 302 to /login/, not 200/403/404-from-nginx
```

Do **not** add an `alias` or `deny` block back — a deny would also block the
Django view.

Note: prod's server block has `client_max_body_size 50M` (checked 2026-10-29),
matching `MEDIA_MAX_UPLOAD_BYTES` (50 MiB) — policy PDFs use that cap too since
2026-10-29 (previously a fixed 20 MB). A file within a few KB of 50 MiB can hit
nginx's bare 413 page (multipart overhead) instead of the form error; if
`MEDIA_MAX_UPLOAD_BYTES` is ever raised, raise this with headroom.

## Media on Google Drive (media_store) — rollout (DONE 2026-10-16)

`docs/MEDIA_GOOGLE_DRIVE_PLAN.md` §5. Dev verified end-to-end on 2026-10-10
(`srv_meals`). **Production applied 2026-10-16** in the order below and is on
`MEDIA_BACKEND=drive`; what happened is recorded in the plan's "Production
rollout (2026-10-16)". **Still owed: step 5 on 2026-10-30** — delete
`~/orbis/media` and `/home/ubuntu/media-pre-drive-2026-10-16.tgz`. The
secrets live in `/home/ubuntu/orbis-secrets/` (`0700`): `oauth-client.json`
and `drive-credentials.json`. The steps stay below as the reference for a
rebuild or a re-authorisation.

Running `migrate_media_to_drive` takes ~10 minutes for ~1,500 files and prints
nothing per successful file — watch progress with
`python manage.py shell -c "from media_store.models import DriveObject; print(DriveObject.objects.count())"`.

**Before anything else — backups:**

1. `/home/ubuntu/mysql_backup/mysql-backup.sh` by hand (encrypted dump →
   Drive, same as the nightly) so a restore point exists from minutes before
   the migration, not from 00:01.
2. `tar czf /home/ubuntu/media-pre-drive-$(date +%F).tgz -C /home/ubuntu/orbis media`
   and record `du -sh /home/ubuntu/orbis/media` + `find … -type f | wc -l`.
   This tarball is the plaintext rollback copy; it stays on the VPS until the
   14-day retention in Phase 2 step 5 ends, then is deleted with the `media/`
   directory.

Phase 1 (code) deploys behaviour-neutral:
`MEDIA_BACKEND` defaults to `filesystem` and **needs no new `.env` entry**
(2026-10-11: the code went to production ahead of the Drive work, so the key
is required only once `MEDIA_BACKEND=drive` — settings refuse to load *then*
without it, and every Drive command refuses on its own). Add the key as the
first step of Phase 0, before `media_drive_authorize`:

```
MEDIA_ENCRYPTION_KEYS=<manage.py generate_payroll_key>     # separate from PAYROLL_FIELD_KEYS; same offline-copy rule
```

Production's value never goes in git, email or chat; add it to the CTO's
printed sheet next to the payroll key. Losing it loses every blob on Drive.

Phase 1 deploy, in order: normal `update_orbis.sh` (runs `migrate` — one plain
table, no collation step); `python manage.py seed_beat_schedule` (adds "Prune
media cache" 03:30 and "Verify media on Drive" Sunday 04:00, the latter a no-op
until the flip — seed never overwrites existing rows); the nginx change above.

Phase 0 on production (one-time, as the Orbis maintainer, signed in to the
browser as **`srv_orbis@sebpo.com`** in a private window — `srv_meals` is the
dev/test account):

1. Google Cloud console, project `meals` (org `sebpo.com`): Google Auth
   Platform → Clients → create a second **Desktop app** client named
   "Orbis media (prod)"; download its JSON to
   `/home/ubuntu/orbis-secrets/oauth-client.json` (`0600`, owner `ubuntu`).
2. `manage.py media_drive_authorize --client /home/ubuntu/orbis-secrets/oauth-client.json --print-url`
   → open as `srv_orbis`, approve, copy the `code` from the
   `http://localhost:8765/?code=…` address bar (the page itself fails to load —
   expected).
3. `manage.py media_drive_authorize --client … --code '4/0A…' --out /home/ubuntu/orbis-secrets/drive-credentials.json`
4. Set `GOOGLE_DRIVE_CREDENTIALS_FILE=/home/ubuntu/orbis-secrets/drive-credentials.json`
   in `.env`, then `manage.py media_drive_authorize --mkroot` → prints the id
   of the app-created "Orbis Media" folder (a hand-made folder is invisible
   under the `drive.file` scope) → `GOOGLE_DRIVE_ROOT_FOLDER_ID=…` in `.env`.
5. `manage.py media_drive_probe` → signed in as srv_orbis, round-trip OK.

Phase 2 (the flip), after Phase 0:

1. `du -sh ~/orbis/media` for the record. `manage.py migrate_media_to_drive --dry-run`,
   then for real, **while still on `MEDIA_BACKEND=filesystem`** (pure copy;
   idempotent — re-run as often as needed, it skips indexed names).
2. `manage.py verify_media_drive --force` → `OK`. Re-run step 1 immediately
   before the flip to catch late uploads.
3. `.env`: `MEDIA_BACKEND=drive`. Restart gunicorn **and** the Celery worker
   (`sudo supervisorctl restart orbis_gunicorn celery_orbis` — letters are generated in
   the worker). Smoke: topbar avatar, employee list photos, open a letter PDF,
   a policy PDF, a tax-declaration document; upload a profile picture;
   generate a letter; Service Book PDF.
4. Rollback at any time: `MEDIA_BACKEND=filesystem`, restart; files uploaded
   while on Drive come back with `manage.py migrate_media_to_drive --reverse`.
5. After 14 quiet days: `rm -rf ~/orbis/media`. `~/orbis/media_cache` (≤ 2 GiB,
   ciphertext, disposable) is the only media-related disk use left.

Dependency note for IT: `srv_orbis@sebpo.com` is now a production dependency
for media as well as outbound mail. Suspending it, deleting it or resetting
its password stops media reads/writes until `media_drive_authorize` is re-run
(deletion also deletes the blobs unless Drive ownership is transferred first).
"Verify media on Drive" (Sunday 04:00) fails visibly in
`/admin/scheduler/scheduledtaskrun/` the first week anything is missing.

## Access log (audit app) — rollout

`docs/ACCESS_LOG_PLAN.md`. One new table (`audit_accesslog`), one middleware, one beat job,
one group, one menu section. Deploy steps, in order:

1. Normal deploy (`update_orbis.sh` runs `migrate` — the migration is non-atomic with the
   usual collation CONVERT step, like `employees/0008`).
2. `python manage.py seed_orbis --menus-only` — creates the **"Audit Viewers"** group, the
   "Audit → Access log" menu, and the `audit_access_log` ProtectedView grant (User Admins +
   Audit Viewers). Assign Audit Viewers to individuals in the admin on request.
3. `python manage.py seed_beat_schedule` — adds the "Prune access log" row (03:15 Dhaka).
4. **Optional — make the table append-only at the database too.** Decided 2026-10-09:
   NOT applied; the requirement is "keep a record", and the model/admin already refuse
   updates and per-row deletes. Keep this step here so it can be run later if the
   requirement becomes "tamper-resistant against someone with the DB password or a shell
   on the box". The app's MySQL user has full DML, and its grant is schema-level
   (`ALL ON orbis_db.*`), so a table-level `REVOKE UPDATE` fails with error 1147 ("no such
   grant") — confirmed 2026-10-09. Use a trigger instead. Run once as root (`sudo mysql`):

   ```sql
   DELIMITER //
   CREATE TRIGGER audit_accesslog_append_only
   BEFORE UPDATE ON orbis_db.audit_accesslog
   FOR EACH ROW
   BEGIN
     SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_accesslog is append-only';
   END//
   DELIMITER ;
   ```

   **Do NOT also `REVOKE TRIGGER ON orbis_db.*` from the app user**: `mysql-backup.sh`
   runs `mysqldump --triggers` as `orbis_user`, which needs the TRIGGER privilege, so the
   revoke would break or silently thin the nightly backup. Keeping it means the trigger
   definition is in every dump and a restore recreates it. The app user could in theory
   `DROP TRIGGER`, but that is a deliberate DDL act the application never performs.
   DELETE stays granted — the nightly `prune_access_log` needs it. Re-create the trigger
   if the table is ever dropped and re-migrated.
5. **Give Django the client IP.** Confirm the nginx `location /` block sets
   `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` (and `X-Real-IP`);
   without it every row's IP is `127.0.0.1`. `sudo nginx -t && sudo systemctl reload nginx`.
6. Verify from the admin (`/admin/audit/accesslog/`): open one profile, one salary page, one
   export, log out and back in by SSO — rows with classes PROFILE, SALARY, an EXPORT action,
   LOGOUT and LOGIN (`backend: sso`) should appear within seconds.

Operations: `ACCESS_LOG_ENABLED=0` in `.env` + restart gunicorn is the kill switch;
`ACCESS_LOG_RETENTION_DAYS` (default 730) is read by the prune job at run time, so changing
it needs no re-seed. The job's captured output (`/admin/scheduler/scheduledtaskrun/`) prints
the last-24h row count every night — a zero there means the middleware stopped writing.

## Payroll field encryption — Phase 1 rollout (APPLIED on production 2026-10-05)

`docs/PAYROLL_DATA_PROTECTION_PLAN.md` Phase 1: every per-employee money column
becomes a Fernet token in MySQL, keyed by `PAYROLL_FIELD_KEYS` in `.env`.
Applied 2026-10-05 exactly as below: 46 letter requests re-encrypted in place,
3 team packages / 255 test runs / 1,525 generated tax declarations purged (the
salary, bank, variable-pay and TDS tables were already empty), tax declarations
regenerated with `seed_tax_declarations` (1,528), admin log had no payroll rows
to scrub, `SELECT input_data … NOT LIKE 'gAAAA%'` → 0. Kept for the next
environment and for key rotation. Two things make this deploy different from
a normal `./update_orbis.sh`:

1. **Settings refuse to load without `PAYROLL_FIELD_KEYS`** (same as a missing
   `SECRET_KEY`), so the key must be in the production `.env` *before* the code
   lands — otherwise `update_orbis.sh` dies at `manage.py migrate` and gunicorn
   would not come back up on the next restart.
2. **The conversion migrations delete the converted tables' rows** (there is no
   backfill — decision 2026-10-05, no real money in the database yet) and
   **refuse to run while those tables hold data** unless
   `PAYROLL_PURGE_ON_MIGRATE=1` is exported for that one `migrate`. The plain
   `./update_orbis.sh` will therefore stop at its migrate step with a message
   naming the non-empty tables. That is expected; the steps below handle it.

**What gets wiped on production** (re-import afterwards from the source
workbooks — the gross salary file, team packages, tax declarations, bank
accounts, any period inputs): `payroll_salarystructure`,
`payroll_teamsalarypackage`, every `payroll_payrollrun*` row (employees, lines,
variances, control checks, audit log), `payroll_taxcomputation`,
`payroll_employeetaxdeclaration`, `payroll_variablepayentry`,
`payroll_tdsentry`, `payroll_employeebankaccount`. `documents_documentrequest`
is **not** wiped — its `input_data` is re-encrypted in place.

```sh
# 0. (once, before anything) take a backup with the new script — the dump
#    is encrypted with the age key, so it is safe to keep. Dumps taken before
#    this point hold plaintext salaries and must be treated as sensitive.

# 1. Generate the key ON THE SERVER (never paste a key through chat/email):
ssh orbis-prod
cd ~/orbis && git fetch && git reset --hard origin/main   # new code, not yet running
venv/bin/python manage.py generate_payroll_key            # prints one line

# 2. Add it to .env (mode stays 0600, owner ubuntu):
printf 'PAYROLL_FIELD_KEYS=%s\n' '<the key>' >> .env
# Print the key for the CTO's sealed offline copy NOW, before the migrate —
# losing every copy loses every encrypted value. Clear the shell history line:
history -d $(history 1 | awk '{print $1}')

# 3. Convert, with the purge guard lifted for this one command only:
PAYROLL_PURGE_ON_MIGRATE=1 venv/bin/python manage.py migrate
#    It prints "purged <table>: N row(s)" per table and
#    "re-encrypted documents_documentrequest.input_data: N of N row(s)".

# 4. New sidebar/ACL rules (gross salary → Payroll Admins, Users menu → +HR):
venv/bin/python manage.py seed_orbis --menus-only

# 5. Finish the normal deploy (pip, tailwind, collectstatic, restart gunicorn +
#    Celery worker + beat — all three read the key from .env on start):
./update_orbis.sh
```

Verify from the MySQL side — this is the whole point:

```sh
sudo mysql -e "SELECT gross_monthly FROM orbis_db.payroll_salarystructure LIMIT 3;"
# → gAAAAAB… tokens, never a number. Same for payroll_payrollrunemployee.net_pay
#   once a run has been calculated.
```

Then re-import the gross salary workbook (Payroll section → Gross salary →
upload, now Payroll Admins only), open the register / Service Book / a salary
certificate as the relevant role, and confirm the Django admin shows money
columns only to a superuser or Payroll Admin.

**Scrub the admin log (Phase 0 step 4) in the same window.** Until this deploy
`str(obj)` embedded the amount, so `django_admin_log.object_repr` holds
historical figures:

```sql
UPDATE orbis_db.django_admin_log
   SET object_repr = '[scrubbed 2026-10-05 — carried a salary figure]'
 WHERE content_type_id IN (
   SELECT id FROM orbis_db.django_content_type
    WHERE app_label = 'payroll'
      AND model IN ('salarystructure', 'teamsalarypackage', 'variablepayentry',
                    'tdsentry', 'payrollrunline', 'payrollvariance',
                    'taxcomputation', 'employeebankaccount'));
```

**Key rotation, later:** `generate_payroll_key`, prepend the new key to
`PAYROLL_FIELD_KEYS` (comma-separated, newest first), restart gunicorn +
Celery, `venv/bin/python manage.py rotate_payroll_keys`, drop the old key, restart
again, replace the CTO's printed copy. Never put `PAYROLL_PURGE_ON_MIGRATE` in
`.env`.

**Local dev** goes through the same steps against the dev database
(`PAYROLL_FIELD_KEYS` is already in the local `.env`; `.env.example` ships a
throwaway key). Until the dev database is migrated, any page that reads a
money column fails with `DecryptionError` — the model expects tokens and finds
decimals.

## Staging snapshot for IT (`prepare_staging_snapshot`, 2026-11-04)

Gives IT a database + `.env` they can run a staging copy from, without
handing over the production payroll key or anyone's real pay. Production is
never touched and **its key is not rotated** — only the copy is re-keyed.

```bash
# 1. A scratch database whose name contains "staging" (the command refuses anything else).
sudo mysql -e "CREATE DATABASE orbis_staging_prep CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
  GRANT ALL ON orbis_staging_prep.* TO 'orbis_user'@'localhost';"
# 2. Restore a fresh production dump into it (a decrypted mysql-backup.sh file, or a dump taken on prod).
mysql orbis_staging_prep < orbis_db-<date>.sql
# 3. Run against it with the production key it was written under (inline vars win over .env).
DB_NAME=orbis_staging_prep PAYROLL_FIELD_KEYS=<production key> \
  python manage.py prepare_staging_snapshot --host <staging host> --output-dir ~/staging-handover
# 4. Send the .7z and, through a different channel, the password it prints. Then:
sudo mysql -e "DROP DATABASE orbis_staging_prep;"
```

What it does (`core/staging_snapshot.py`, counts only, never values):

- **Preflight, before any write.** Every encrypted value must open under the
  given key. It refuses if a run holds calculated figures (run output can't be
  scaled to match its inputs, so a run has to be recalculated), and it refuses
  if an encrypted model is one it doesn't know how to mock.
- **Re-key** to a newly generated staging key (`rotate_payroll_keys`). The
  rest runs with the staging key alone, and the final check reads every token
  from SQL: each must open under the staging key and none under prod's.
- **Mock money.** One random factor per employee (0.85–0.90, `SystemRandom`,
  no seed), applied to all of that employee's salary, variable pay, TDS,
  declaration, loan, challan-allocation and pre-Orbis-month rows, in whole
  taka. Team packages and challan totals get a fresh factor per row. Bank and
  routing numbers become random digits. Salary-certificate inputs get the same
  treatment: money keys scaled, bank number faked.
- **Strip production state.** All beat jobs disabled (the weekly roster email
  reaches clients). Sessions, API client tokens, the access log, the beat run
  log, `django_admin_log`, payroll control checks and the Drive media index are
  cleared. One staging password is set on every account, because staging has
  no SSO (`--keep-passwords` opts out).
- **Package.** `staging.env` holds fresh secrets: `SECRET_KEY`, DB password,
  payroll key and `EXTERNAL_API_TOKEN` (inbound, so never prod's). SSO and the
  TimeLy/meals/TTMS tokens are copied from the `.env` the command runs with,
  because staging can't be tested without them (`--no-integrations` blanks
  them). SSO reuses the production Entra app, so IT must add
  `https://<staging host>/oidc/callback/` as a redirect URI on it (or register
  a separate staging app). With SSO on, password login is superuser-only. Email
  goes to the console; `--include-smtp` copies the real mailbox, and staging
  then mails real employees. Redis and media-on-Drive are off, and there are
  no `MEDIA_ENCRYPTION_KEYS`.
  `HANDOVER.txt` has IT's setup steps and the login password. Both go into a
  `mysqldump`-backed 7z archive (AES-256, `-mhe=on`) under `--output-dir`, which
  must be outside the repo. The staging key is written to disk before the first
  write, so a packaging failure never leaves an unreadable database.

Uploaded files are not part of the snapshot, so links to them 404 on staging.
Names, NIDs and addresses stay real (accepted for IT, 2026-11-04). Run notes
(`PayrollRunEmployeeNote`) are kept; they quote TA allowance amounts, which are
rate-card figures, not salaries. Once real payroll has been run, extend
`DERIVED` handling to clear and recalculate runs. The preflight refuses until
someone does.

## MySQL backups

**State found 2026-10-05:** `/home/ubuntu/mysql_backup/mysql-backup.sh` (June 2026,
not in this repo) ran from `ubuntu`'s crontab at 00:01 and 15:05 and (a) embedded
the `orbis_user` password in the script and passed it on the `mysqldump` command
line, (b) dumped `orbis_db` as a plain, uncompressed ~195 MB `.sql`
(`rw-rw-r--`, world-readable), (c) uploaded it **unencrypted** to a Google Drive
folder via the `gdrive` CLI and then deleted every previous Drive file, so
exactly one copy existed off-box, (d) kept ~2 days locally, (e) had no
`set -e`/`pipefail` and no artefact check, so a failed `mysqldump` would upload an
empty file and delete the good one, and (f) used no `--single-transaction`, so a
dump mid-write was not a consistent snapshot. The script also had `mysql-backup.sh.save`
/ `.resumeintel` siblings and a `resumeintel_db.sql` beside it.

**Replacement: [`deploy/mysql-backup.sh`](mysql-backup.sh)** — credential-free
and committed. Dump (`--single-transaction --quick --routines --triggers --events
--hex-blob --no-tablespaces --set-gtid-purged=OFF`) → `gzip -t` + "Dump completed"
trailer + size check → **`age` public-key encryption** → upload → prune to the
newest 30 on Drive and 7 days locally, pruning only after a confirmed upload.
Plaintext exists on disk for the seconds between dump and encrypt only. The
private key is **not on the server**: the VPS can write backups it cannot read.

A backup is an OS-level job and stays in cron — the "Celery beat only" rule
(CLAUDE.md) is about `manage.py` commands, and a DB backup must not depend on
the app it backs up.

One-time setup (needs MySQL root, which is `sudo`-gated on this host):

```bash
sudo apt install age
# 1. A least-privilege MySQL user for the dump — the app's orbis_user is no longer involved.
sudo mysql -e "CREATE USER 'orbis_backup'@'localhost' IDENTIFIED BY '<generate one>';
  GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON orbis_db.* TO 'orbis_backup'@'localhost';
  GRANT SHOW_ROUTINE ON *.* TO 'orbis_backup'@'localhost'; FLUSH PRIVILEGES;"
# 2. Credentials file, mode 0600 (the script refuses anything looser).
printf '[client]\nuser=orbis_backup\npassword=<the same>\n' > /home/ubuntu/mysql_backup/backup.conf
chmod 600 /home/ubuntu/mysql_backup/backup.conf
# 3. Recipient = the age PUBLIC key. Generate the pair OFFLINE (age-keygen -o backup-key.txt),
#    keep backup-key.txt with the payroll field key (printed copy with the CTO), paste only
#    the "public key:" line here.
echo 'age1...' > /home/ubuntu/mysql_backup/backup-recipient.txt
# 4. Install + schedule (once daily is enough; add the 15:05 line back if HR wants midday too).
sudo install -m 0755 deploy/mysql-backup.sh /home/ubuntu/mysql_backup/mysql-backup.sh
crontab -e   # 1 0 * * * /home/ubuntu/mysql_backup/mysql-backup.sh >> /home/ubuntu/mysql_backup/backup.log 2>&1
# 5. Remove the old plaintext artefacts and the password-bearing siblings.
shred -u /home/ubuntu/mysql_backup/mysql-backup.sh.save /home/ubuntu/mysql_backup/mysql-backup.sh.resumeintel
rm /home/ubuntu/mysql_backup/backup/*.sql           # the new script also does this on its first run
# 6. Rotate orbis_user's password (it was in a 0775 script for four months) — .env + `update_orbis.sh`.
# 7. Delete the unencrypted dumps already sitting in the Drive folder.
```

Restore drill (do one before trusting this): `age -d -i backup-key.txt <file>.sql.gz.age | gunzip | mysql --defaults-extra-file=... orbis_db`
into a scratch database, then `manage.py check` against it.

Retention policy: 30 daily encrypted copies on Drive, 7 on the VPS. Dumps taken
before the payroll field-encryption migration (`docs/PAYROLL_DATA_PROTECTION_PLAN.md`
Phase 1) contain plaintext money — test data only today, but delete them when
Phase 1 lands rather than letting retention age them out.

## MySQL developer account

Developers hold database credentials but no shell on the VPS (confirmed
2026-10-05), so the database is the exposure to close. [`deploy/mysql-dev-grants.sql`](mysql-dev-grants.sql)
creates `orbis_dev` with **`SELECT` on every table except** the per-employee money
tables, `payroll_payrollcontrolcheck`/`payroll_payrollrunaudit`/
`payroll_payrollrunemployeenote` (messages and notes can embed figures),
`django_admin_log` (`object_repr` holds historical amounts), `documents_documentrequest`
(salary-certificate inputs), `django_session`, and `accounts_customuser.password`
(a column-level grant covers the other 32 columns). MySQL has no "revoke a subset
of a DB-wide grant", hence one `GRANT` per table — 127 of them, generated from the
Django model registry, so **regenerate the file after adding a model** (the
snippet is in the file header's referenced `manage.py shell` block in
`docs/PAYROLL_DATA_PROTECTION_PLAN.md` Phase 0). Apply with
`sudo mysql < deploy/mysql-dev-grants.sql` after replacing the password, then
rotate `orbis_user`'s password so the old shared credential dies. The account is
read-only on purpose; a write grant for a specific table is a deliberate
per-case addition, not the default.

## MySQL timezone tables — ⚠️ NOT LOADED (silent data bug)

`USE_TZ=True` with `TIME_ZONE='Asia/Dhaka'` means Django compiles every
`__date` lookup on a **DateTimeField** into `CONVERT_TZ(col,'UTC','Asia/Dhaka')`.
`CONVERT_TZ` with *named* zones needs MySQL's `mysql.time_zone*` tables
populated. On this server they are not:

    SELECT CONVERT_TZ('2026-08-29 00:00:00','UTC','Asia/Dhaka');  -- NULL
    SELECT CONVERT_TZ('2026-08-29 00:00:00','+00:00','+06:00');   -- works

It fails **silently**: the expression yields NULL, so `filter(ts__date=...)`
and `TruncDate('ts')` match nothing and group everything into a single NULL
bucket. No error is raised. This was caught on 2026-08-29 when a
`TruncDate` over `roster_shift.added_at` collapsed 168k rows into one `None`
group and a `added_at__date__gte` filter reported 0 rows where 114 existed.

DateField columns (`Shift.date`, `joining_date`) need no conversion and are
unaffected — but any report, dashboard or payroll window filtering a
*timestamp* by date is quietly returning empty results.

Load the tables (needs MySQL root; `sudo` on this host is password-gated, so
run it interactively):

```sh
mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql
# "Unable to load .../leap-seconds.list" warnings are harmless.
sudo systemctl restart mysql        # drop the server's cached tz set
```

Then verify as the app user — it must return a timestamp, not NULL:

```sh
cd ~/orbis && venv/bin/python manage.py shell -c "
from django.db import connection
c=connection.cursor()
c.execute(\"SELECT CONVERT_TZ('2026-08-29 00:00:00','UTC','Asia/Dhaka')\")
print(c.fetchone()[0])"
```

`orbis_user` needs no grant on the `mysql.time_zone*` tables for `CONVERT_TZ`
itself — the server reads them internally. Grant `SELECT` on them only if you
want to query the tables directly for diagnostics (it currently cannot:
`SELECT command denied ... for table 'time_zone_name'`).

## MySQL collation

**Production is already pinned — verified 2026-09-19, no action needed.** The
database default, all 132 tables, `accounts_customuser.id` and the MySQL server
default are all `utf8mb4_0900_ai_ci`. Don't re-run the `ALTER` below on prod;
it is recorded here for **new environments**.

Pin a new database's default so migration-created tables always match
`accounts_customuser`'s collation (its pk is a UUID, stored as `char(32)` — a
string, so it carries a collation, and MySQL refuses a FK whose collations
differ):

```sql
ALTER DATABASE orbis CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
```

Run it **before the first `migrate`** on a fresh environment — this is
mandatory, not hygiene. `accounts/0001_initial` has no CONVERT step, so
`accounts_customuser.id` simply follows the database default; the apps that
*do* carry the step then force their own tables to `utf8mb4_0900_ai_ci`, and
the two disagree. Verified 2026-09-19 on a throwaway `utf8mb4_unicode_ci`
database: `accounts_customuser.id` came out `utf8mb4_unicode_ci` and the build
died at `policies_policy`'s FK to it. **The per-migration CONVERT step is
therefore not a substitute for this pin on a new database** — it is only
correct because the pin is expected.

The statement is default-only — it changes what future objects inherit and
touches no existing table, column or row — so it is safe to run on an
established database too, just unnecessary on prod.

**New-table migrations that FK CustomUser must still carry the explicit CONVERT
step**, whatever the database default says — `employees/0008_designationhistory.py`
is the reference, and `attendance/0002`, `leave/0010_cpl_grant.py` and
`payroll/0002` all follow it:

```python
def _match_existing_collation(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    schema_editor.execute(
        "ALTER TABLE `<new_table>` CONVERT TO CHARACTER SET utf8mb4 "
        "COLLATE utf8mb4_0900_ai_ci"
    )
```

with `atomic = False` on the Migration (the step issues DDL, which Django
forbids inside an atomic migration's transaction; FK creation is deferred to
schema-editor exit, so convert-then-FK still ordered correctly). Skipping it is
an easy mistake — it happened on 2026-09-19 writing
`employees/0013_teamassignment.py`, which mirrored `DesignationHistory`'s model
without its migration and failed with `OperationalError 3780` on a dev database
whose default had drifted.

## Git history note

`2026-06-08_07_34_59-orbis_db.sql` (a production dump with employee PII) was
removed from tracking on 2026-07-21 but still exists in git history. Purging
requires a coordinated `git filter-repo` + force-push and re-clones by every
checkout holder. Until then, treat repo access as PII access.
