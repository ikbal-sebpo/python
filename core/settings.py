"""
Django settings for Orbis (core project).

Single-file configuration; environment-specific values come from `.env`
loaded via python-dotenv. Mirrors the reicrm pattern.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("true", "1", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY is not set. Populate it in the .env file.")

# Field-level encryption keys for money at rest (docs/PAYROLL_DATA_PROTECTION_PLAN.md
# §4.1): comma-separated Fernet keys, newest first. Every per-employee money
# column (core.fields.Encrypted*Field) is written under the first key and can
# be read under any of them, so a rotation is "prepend the new key, restart,
# `manage.py rotate_payroll_keys`, drop the old key". Deliberately separate
# from SECRET_KEY. Losing every key loses the data — keep the offline copy.
PAYROLL_FIELD_KEYS = _env_list("PAYROLL_FIELD_KEYS")
if not PAYROLL_FIELD_KEYS:
    raise ImproperlyConfigured(
        "PAYROLL_FIELD_KEYS is not set. Run `manage.py generate_payroll_key` and populate it in the .env file."
    )

# Whole-file encryption keys for media on Google Drive (docs/MEDIA_GOOGLE_DRIVE_PLAN.md
# §4.4): same format and rotation story as PAYROLL_FIELD_KEYS, but a separate
# key so rotating one never forces re-encrypting the other's data. Every media
# blob leaves the VPS as a Fernet token under the first key. Required only when
# MEDIA_BACKEND=drive (checked below, next to the other Drive settings) — the
# filesystem backend never touches it, and requiring it there would make a
# plain code deploy depend on a secret the deploy doesn't use (2026-10-11
# reversal of the 2026-10-10 "required in every .env" rule; media_store.crypto
# still raises its own ImproperlyConfigured the moment a Drive command needs
# the key and finds it unset).
MEDIA_ENCRYPTION_KEYS = _env_list("MEDIA_ENCRYPTION_KEYS")

DEBUG = _env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS")


INSTALLED_APPS = [
    "core.apps.OrbisAdminConfig",  # replaces django.contrib.admin (custom admin site)
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Required so FORM_RENDERER = TemplatesSetting (below) can still find
    # Django's own built-in widget templates (django/forms/templates/...)
    # via APP_DIRS, since TemplatesSetting doesn't get the form renderer's
    # normal extra template dir.
    "django.forms",

    "rest_framework",
    "tailwind",
    "theme",
    "django_htmx",
    "import_export",
    "django_ckeditor_5",
    # Database-backed celery-beat schedule, managed from the Django admin.
    # See docs/SCHEDULER_ADMIN_PLAN.md and the `scheduler` app below.
    "django_celery_beat",

    "accounts.apps.AccountsConfig",
    "dynamic_menu.apps.DynamicMenuConfig",
    "employees.apps.EmployeesConfig",
    "roster.apps.RosterConfig",
    "leave.apps.LeaveConfig",
    "attendance.apps.AttendanceConfig",
    "documents.apps.DocumentsConfig",
    "offboarding.apps.OffboardingConfig",
    "payroll.apps.PayrollConfig",
    "policies.apps.PoliciesConfig",
    "scheduler.apps.SchedulerConfig",
    # Access log — who accessed which data or page. docs/ACCESS_LOG_PLAN.md.
    "audit.apps.AuditConfig",
    # Media files on Google Drive as encrypted blobs + the /media/ serve view.
    # docs/MEDIA_GOOGLE_DRIVE_PLAN.md.
    "media_store.apps.MediaStoreConfig",
]

AUTH_USER_MODEL = "accounts.CustomUser"

# MySQL doesn't support unique constraints with a WHERE condition, so Django
# flags offboarding.Resignation and offboarding.ClearanceWorkflowTemplate's
# partial UniqueConstraints (W036). Both models enforce the same invariant
# at the model layer instead (Resignation.clean, ClearanceWorkflowTemplate.save).
SILENCED_SYSTEM_CHECKS = ["models.W036"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "offboarding.middleware.LastWorkingDateMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    # Last on purpose: request.user is final and the view's transaction is
    # over by the time it writes its one row. docs/ACCESS_LOG_PLAN.md §3.2.
    "audit.middleware.AccessLogMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.sso_flags",
                "dynamic_menu.context_processors.dynamic_menus",
            ],
            # Money filter available everywhere without {% load %} — including
            # the DB-stored PDF designs rendered via engine.from_string
            # (docs/MONEY_FORMAT_PLAN.md).
            "builtins": ["core.templatetags.money"],
        },
    },
]

# Default FORM_RENDERER only looks in each installed app's own templates/
# directory. This project keeps every template under one project-level
# templates/ dir (TEMPLATES[0]["DIRS"] above), so widget-level template_name
# lookups (e.g. employees.widgets.EmployeeSearchWidget) need the renderer
# that respects the full TEMPLATES setting instead.
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"


# Database — MySQL per PLAN.md. Falls back to SQLite when DB_ENGINE=sqlite
# is set in .env — useful for local bootstrap before MySQL is provisioned.
if os.getenv("DB_ENGINE", "mysql").lower() == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME"),
            "USER": os.getenv("DB_USER"),
            "PASSWORD": os.getenv("DB_PASSWORD"),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "use_unicode": True,
                "sql_mode": "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION",
            },
            # Persistent connections: without this every request pays a fresh
            # MySQL connect+auth round-trip. Health checks make a recycled
            # connection that the server dropped get replaced transparently.
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }


# --- Cache ---
# Shared cache backend (dynamic menu, dashboard snapshots, TimeLy presence
# probe). Redis when REDIS_URL is set (production — also the broker for the
# background-task worker); per-process LocMem fallback otherwise so local dev
# needs no extra services.
_REDIS_URL = os.getenv("REDIS_URL", "")
if _REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _REDIS_URL,
        }
    }
    # Session reads served from cache, writes persisted to the DB — keeps
    # logins durable across Redis restarts while skipping the per-request
    # django_session SELECT.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
else:
    CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }

# Application-cache TTLs (seconds). 0 disables the individual cache — used by
# settings_test for deterministic tests, and available as an ops kill switch.
DYNAMIC_MENU_CACHE_SECONDS = int(os.getenv("DYNAMIC_MENU_CACHE_SECONDS", "3600"))
DASHBOARD_ORG_CACHE_SECONDS = int(os.getenv("DASHBOARD_ORG_CACHE_SECONDS", "180"))
LEAVE_HOLIDAY_CACHE_SECONDS = int(os.getenv("LEAVE_HOLIDAY_CACHE_SECONDS", "3600"))

# --- Offboarding notifications kill switch ---
# When False, every offboarding notification helper becomes a no-op: the
# resignation-submitted / HR-initiated mail to the employee + manager, the
# step-assigned mail to IT / Finance / HR, and the case-completed mail.
# Paused in prod while the module is still being rolled out — HR works the
# clearance queue by hand rather than being pushed to it. NOTE the trade-off:
# with this off, assignees get no signal that a step is waiting, so the queue
# is pull-only. Turn it back on (drop the env var) before real volume.
OFFBOARDING_NOTIFICATIONS_ENABLED = _env_bool("OFFBOARDING_NOTIFICATIONS_ENABLED", True)

# How far back the unbacked-separation detector looks. An employee whose LWD
# is older than this is treated as historical and left out of the "Needs
# offboarding" queue — back-filling a long-closed separation creates
# permanent queue noise nobody will action.
OFFBOARDING_BACKFILL_CUTOFF_DAYS = int(os.getenv("OFFBOARDING_BACKFILL_CUTOFF_DAYS", "90"))


# --- Background tasks (Celery + Redis broker) ---
# Slow work — SMTP fan-outs, WeasyPrint PDF renders, holiday recomputes — is
# dispatched through core.tasks.defer(). With Redis configured, the Celery
# worker (supervisor: `celery -A core worker`) executes it off the request
# path; without Redis, defer() runs the function inline (dev/test behavior,
# identical to pre-queue semantics). Periodic jobs run via celery-beat
# (supervisor: `celery -A core beat`), reading their schedule from the
# database — see the "Periodic schedule" block below.
TASK_QUEUE_ENABLED = _env_bool("TASK_QUEUE_ENABLED", bool(_REDIS_URL))
CELERY_BROKER_URL = _REDIS_URL or "redis://127.0.0.1:6379/1"
CELERY_TASK_IGNORE_RESULT = True          # fire-and-forget; no result backend
# No result backend means nothing to expire. Stating it also stops
# DatabaseScheduler.install_default_entries() from creating an enabled
# `celery.backend_cleanup` periodic task on first start (it only adds that
# entry when result_expires is truthy) — a daily 04:00 no-op against
# DisabledBackend that would otherwise sit unexplained in the admin task list
# and add a row to the run log every night.
CELERY_RESULT_EXPIRES = None
CELERY_TASK_TIME_LIMIT = 300              # hard per-task cap (WeasyPrint worst case)
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TIMEZONE = os.getenv("TIME_ZONE", "Asia/Dhaka")  # mirrors TIME_ZONE below
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Bound how long an enqueue attempt blocks when Redis is down before
# core.tasks.defer falls back to inline execution (~1s worst case).
CELERY_TASK_PUBLISH_RETRY_POLICY = {
    "max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5,
}
CELERY_BROKER_TRANSPORT_OPTIONS = {"max_retries": 2}

# --- Periodic (nightly/weekly) schedule: DATABASE-BACKED ---
# The schedule lives in django_celery_beat's tables and is managed from the
# Django admin (/admin/django_celery_beat/periodictask/), not here. Each row
# fires core.tasks.run_management_command, so every job stays a hand-runnable
# manage.py command. scheduler/schedule.py::JOBS is the code-level record and
# `manage.py seed_beat_schedule` materialises it on a fresh install.
#
# CELERY_BEAT_SCHEDULE MUST STAY EMPTY while the database scheduler is active.
# DatabaseScheduler.setup_schedule() pushes every entry here through
# PeriodicTask.update_or_create() on each beat startup, so a single entry
# silently reverts that task's admin-edited schedule/args/enabled flag on the
# next deploy. scheduler/tests.py asserts this stays {}.
# See docs/SCHEDULER_ADMIN_PLAN.md.
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
DJANGO_CELERY_BEAT_TZ_AWARE = True      # read each crontab in its own timezone
CELERY_BEAT_SCHEDULE = {}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = ("django.contrib.auth.backends.ModelBackend",)
# Replaced below when SSO_ENABLED (OIDC + superuser-only password backend).

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Dhaka")
USE_I18N = True
USE_TZ = True

# Display every date/time in ISO (YYYY-MM-DD) across templates, admin, forms.
FORMAT_MODULE_PATH = ["core.formats"]


STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# --- Media storage backend (docs/MEDIA_GOOGLE_DRIVE_PLAN.md) ---
# "filesystem" (default; dev/tests, and production until the Phase 2 flip)
# keeps uploads under MEDIA_ROOT. "drive" stores every upload as an encrypted
# blob in the service mailbox's Google Drive (media_store.storage), with a
# bounded ciphertext cache on local disk. Either way /media/<name> is served
# only by media_store.views.MediaServeView (login + per-prefix authorisation),
# never by nginx or the DEBUG static() helper.
MEDIA_BACKEND = os.getenv("MEDIA_BACKEND", "filesystem").strip().lower()
if MEDIA_BACKEND not in ("filesystem", "drive"):
    raise ImproperlyConfigured("MEDIA_BACKEND must be 'filesystem' or 'drive'.")
MEDIA_CACHE_ROOT = Path(os.getenv("MEDIA_CACHE_ROOT") or (BASE_DIR / "media_cache"))
MEDIA_CACHE_MAX_BYTES = int(os.getenv("MEDIA_CACHE_MAX_BYTES", str(2 * 1024**3)))   # 2 GiB
MEDIA_MAX_UPLOAD_BYTES = int(os.getenv("MEDIA_MAX_UPLOAD_BYTES", str(50 * 1024**2)))  # 50 MiB
# Refresh-token credentials written by `manage.py media_drive_authorize` and
# the id of the app-created "Orbis Media" root folder (`--mkroot`).
GOOGLE_DRIVE_CREDENTIALS_FILE = os.getenv("GOOGLE_DRIVE_CREDENTIALS_FILE", "").strip()
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "").strip()
if MEDIA_BACKEND == "drive":
    if not MEDIA_ENCRYPTION_KEYS:
        raise ImproperlyConfigured(
            "MEDIA_BACKEND=drive needs MEDIA_ENCRYPTION_KEYS. Run `manage.py generate_payroll_key` "
            "and populate it in the .env file."
        )
    if not (GOOGLE_DRIVE_CREDENTIALS_FILE and GOOGLE_DRIVE_ROOT_FOLDER_ID):
        raise ImproperlyConfigured(
            "MEDIA_BACKEND=drive needs GOOGLE_DRIVE_CREDENTIALS_FILE and GOOGLE_DRIVE_ROOT_FOLDER_ID."
        )
    STORAGES["default"] = {"BACKEND": "media_store.storage.GoogleDriveStorage"}

LOGS_DIR = BASE_DIR / "logs"

# --- Access log (audit app) ---
# Kill switch for the per-request access log; retention is enforced by the
# nightly `prune_access_log` beat job. docs/ACCESS_LOG_PLAN.md.
ACCESS_LOG_ENABLED = _env_bool("ACCESS_LOG_ENABLED", True)
ACCESS_LOG_RETENTION_DAYS = int(os.getenv("ACCESS_LOG_RETENTION_DAYS", "730"))
LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOGS_DIR / "orbis.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"


# --- Tailwind ---
TAILWIND_APP_NAME = "theme"
INTERNAL_IPS = ["127.0.0.1"]
NPM_BIN_PATH = os.getenv("NPM_BIN_PATH", "/usr/bin/npm")


# --- Azure SSO (Microsoft Entra via mozilla-django-oidc) ---
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
SSO_ENABLED = bool(AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and AZURE_TENANT_ID)

if SSO_ENABLED:
    # SSO is the only way in for everyone but superusers: /login/ shows just
    # the SSO button, and password login survives only as the superusers'
    # break-glass via /admin/login/ (accounts.auth_backends.SuperuserPasswordBackend).
    AUTHENTICATION_BACKENDS = (
        "accounts.auth_backends.OrbisOIDCBackend",
        "accounts.auth_backends.SuperuserPasswordBackend",
    )

    _AZURE_AUTHORITY = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"

    OIDC_RP_CLIENT_ID = AZURE_CLIENT_ID
    OIDC_RP_CLIENT_SECRET = AZURE_CLIENT_SECRET
    OIDC_RP_SIGN_ALGO = "RS256"
    # Use Azure's `.default` scope so Entra issues a token covering whatever
    # Delegated Graph permissions are admin-consented on the app registration —
    # no per-user "Approval required" prompts. Mirrors how the legacy ttms project
    # talks to Graph. Mix with the standard OIDC scopes (openid/email/profile);
    # don't combine with other Graph scopes for the same resource.
    OIDC_RP_SCOPES = "openid email profile https://graph.microsoft.com/.default"

    OIDC_OP_AUTHORIZATION_ENDPOINT = f"{_AZURE_AUTHORITY}/oauth2/v2.0/authorize"
    OIDC_OP_TOKEN_ENDPOINT = f"{_AZURE_AUTHORITY}/oauth2/v2.0/token"
    OIDC_OP_JWKS_ENDPOINT = f"{_AZURE_AUTHORITY}/discovery/v2.0/keys"

    # Graph /me with $select for HR fields + $expand=manager so the manager
    # sub-object comes back in one round-trip. Default get_userinfo uses this URL.
    OIDC_OP_USER_ENDPOINT = (
        "https://graph.microsoft.com/v1.0/me"
        "?$select=id,displayName,givenName,surname,mail,userPrincipalName,"
        "jobTitle,department,officeLocation,employeeId,employeeHireDate,"
        "mobilePhone,companyName"
        "&$expand=manager($select=id,displayName,mail,userPrincipalName,jobTitle)"
    )

    # Full JIT by default: anyone with a sebpo.com Entra account auto-onboards
    # to Orbis on first SSO login. Set OIDC_AUTO_CREATE_USERS=0 in `.env` to
    # fall back to admin-pre-creates-only. The single-tenant app registration
    # is the first gate; OIDC_ALLOWED_EMAIL_DOMAINS below is the in-app
    # defense-in-depth gate so an Entra-side misconfiguration (multi-tenant
    # flip, borrowed test app) can't auto-provision outside identities.
    OIDC_CREATE_USER = _env_bool("OIDC_AUTO_CREATE_USERS", True)

    # On auth failure (unknown email, Graph denied, token invalid) bounce back
    # to the login page rather than the default `/`.
    LOGIN_REDIRECT_URL_FAILURE = LOGIN_URL

# Domains whose identities may sign in / auto-provision via SSO. Checked in
# OrbisOIDCBackend.verify_claims regardless of the Entra app-reg tenancy.
# Comma-separated; an explicitly EMPTY value disables the check (not
# recommended outside local experiments).
OIDC_ALLOWED_EMAIL_DOMAINS = [
    d.strip().lower().lstrip("@")
    for d in os.getenv("OIDC_ALLOWED_EMAIL_DOMAINS", "sebpo.com").split(",")
    if d.strip()
]


# --- TimeLy integration (Phase 3) ---
TIMELY_API_BASE_URL = os.getenv("TIMELY_API_BASE_URL", "")
TIMELY_API_TOKEN = os.getenv("TIMELY_API_TOKEN", "")

# --- External write API — Orbis is authoritative, the caller is a client ---
# Shared token for external systems writing shifts (dsndd is the first
# consumer; see docs/DSNDD_INTEGRATION.md).
EXTERNAL_API_TOKEN = os.getenv("EXTERNAL_API_TOKEN", "")

# --- Meal counts integration ---
# Read-only client (payroll/meals.py) for the meals system's employee
# meal-count API, backing the payroll report's Meal column. Base URL only —
# the client appends the fixed /api/v1/external/meal-counts/ path itself.
MEAL_COUNTS_API_BASE_URL = os.getenv("MEAL_COUNTS_API_BASE_URL", "https://meals.sebpo.net")
MEAL_COUNTS_API_TOKEN = os.getenv("MEAL_COUNTS_API_TOKEN", "")

# --- TTMS daily-hours integration ---
# Read-only client (payroll/ttms.py) for ttms.sebpo.net's ops daily-hours
# API, backing General BPO's government-holiday overtime generation
# (generate_gh_overtime_bpo). Base URL only — the client appends the fixed
# /api/v1/operations/daily-hours/ path itself.
TTMS_API_BASE_URL = os.getenv("TTMS_API_BASE_URL", "https://ttms.sebpo.net")
TTMS_API_TOKEN = os.getenv("TTMS_API_TOKEN", "")


# --- Email (synchronous send_mail; Celery deferred) ---
# Dev tip: set EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
# in .env to print outgoing mail to stdout instead of needing real SMTP.
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
# TLS (STARTTLS, port 587) and SSL (implicit, port 465) are mutually exclusive
# in Django — setting both raises ImproperlyConfigured. Our .env uses port 465
# + EMAIL_USE_SSL=True, so EMAIL_USE_SSL must be wired here (it previously was
# not, which silently broke every send: Django defaulted EMAIL_USE_TLS=True and
# attempted STARTTLS on a port that needs implicit SSL). EMAIL_USE_TLS now
# defaults OFF whenever SSL is on, and SSL wins if both are somehow enabled.
EMAIL_USE_SSL = _env_bool("EMAIL_USE_SSL", False)
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", not EMAIL_USE_SSL) and not EMAIL_USE_SSL
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@sebpo.com")
HR_NOTIFICATION_EMAILS = _env_list("HR_NOTIFICATION_EMAILS")


# --- Public base URL (used by documents app for QR-code verify links) ---
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# Optional absolute path to a fallback logo PNG used by the PDF pipeline when
# the CompanyBranding singleton has no logo uploaded yet. Leave blank in prod
# once CompanyBranding is populated.
DOCUMENTS_FALLBACK_LOGO = os.getenv("DOCUMENTS_FALLBACK_LOGO", "")


# --- CKEditor 5 (rich-text fields on documents TemplateDesign + MessageTemplate) ---
# django-ckeditor-5 replaced django-ckeditor (2026-07: bundled CKEditor 4 is
# EOL with unfixed security issues — Django check ckeditor.W001). The bundled
# CKEditor 5 build ships from our own static files (no CDN). Toolbar mirrors
# the old config; the CKEditor 4 "Image" button is intentionally dropped —
# letter imagery (logo/signatures) comes from CompanyBranding in the PDF
# pipeline, and no upload endpoint is exposed.
# The upload endpoint is mounted (the widget requires the URL to resolve)
# but no toolbar button uses it; staff-only as defence in depth.
CKEDITOR_5_FILE_UPLOAD_PERMISSION = "staff"
CKEDITOR_5_CONFIGS = {
    "default": {
        "toolbar": [
            "heading", "|",
            "bold", "italic", "underline", "|",
            "numberedList", "bulletedList", "outdent", "indent", "|",
            "alignment", "|",
            "link", "insertTable", "horizontalLine", "|",
            "fontSize", "fontColor", "fontBackgroundColor", "|",
            "removeFormat", "sourceEditing",
        ],
    },
}


# --- Security (relaxed in DEBUG; tightened only when actually behind TLS) ---
# Decoupled from DEBUG so production can run on plaintext HTTP during a
# cert-procurement window without the Secure-cookie flags blocking login.
# Set BEHIND_TLS_PROXY=1 in `.env` once HTTPS is in front of gunicorn.
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True

if not DEBUG and os.getenv("BEHIND_TLS_PROXY", "0") == "1":
    # nginx terminates TLS and sets `X-Forwarded-Proto $scheme`, so this is
    # the header to trust. Without it request.is_secure() stays False and
    # server-built absolute URLs (documents' QR-verify links) emit http://.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # nginx does not set X-Forwarded-Host, so this falls through to Host.
    # A client-supplied X-Forwarded-Host is still checked against
    # ALLOWED_HOSTS, which is why this is safe to enable here.
    USE_X_FORWARDED_HOST = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Off (0) unless SECURE_HSTS_SECONDS is set in .env. Raise it in stages --
    # browsers cache HSTS, so an over-long value is hard to walk back.
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
