"""Test-only settings.

Two things differ from `core.settings`:

1. **SQLite engine.** Avoids needing MySQL `CREATE` on `test_orbis_db` —
   useful in dev where `orbis_user` doesn't have that privilege.
2. **Skip migration replay.** The roster app's migration 0003 is hand-tuned
   for MySQL (raw `ALTER TABLE ... MODIFY COLUMN`, MySQL index quirks) and
   isn't compatible with SQLite. Disabling migrations makes Django create
   the schema directly from current model state, which is exactly what
   unit tests want.

Usage:

    DJANGO_SETTINGS_MODULE=core.settings_test python manage.py test
    # or:
    python manage.py test --settings=core.settings_test

For production-fidelity tests against MySQL, grant `CREATE` on
`test_orbis_db` to `orbis_user` and run with the default settings.
"""

import os

# Force the SQLite branch in core/settings.py before we import * from it.
os.environ["DB_ENGINE"] = "sqlite"
# core/settings.py refuses to load without a key; the real value is pinned
# below so a developer's .env can never change what the tests encrypt with.
os.environ.setdefault("PAYROLL_FIELD_KEYS", "VLJ7qrlDNi28Og-e2cSLIlOyfOi8qNv-M3rFZHL5dXc=")
os.environ.setdefault("MEDIA_ENCRYPTION_KEYS", "k9d7Zp1yQmX3vT0bN6cR4sW8uJ2hL5aE7gF1iK3oP9Y=")
os.environ["MEDIA_BACKEND"] = "filesystem"

from core.settings import *  # noqa: E402, F401, F403

# Feature flags that core/settings.py reads from .env are pinned here so a
# developer's local .env can never change test outcomes.
OFFBOARDING_NOTIFICATIONS_ENABLED = True
# With Azure creds in .env, SSO mode makes password login superuser-only —
# keep the plain password backend so tests can client.login() anyone.
# The SSO-only behaviour is exercised via override_settings
# (accounts/tests_sso_only_login.py).
AUTHENTICATION_BACKENDS = ("django.contrib.auth.backends.ModelBackend",)
# Fixed test-only key (never used anywhere else) so encrypted-field tests are
# deterministic across machines.
PAYROLL_FIELD_KEYS = ["VLJ7qrlDNi28Og-e2cSLIlOyfOi8qNv-M3rFZHL5dXc="]
# Same for the media blob key; tests that exercise the Drive backend inject a
# fake client via STORAGES OPTIONS (media_store/tests.py) and a temp cache.
MEDIA_ENCRYPTION_KEYS = ["k9d7Zp1yQmX3vT0bN6cR4sW8uJ2hL5aE7gF1iK3oP9Y="]
MEDIA_BACKEND = "filesystem"
MEDIA_CACHE_ROOT = BASE_DIR / "media_cache_test"


class _DisableMigrations:
    """Tells Django there's no migration module for any app, so the test
    runner builds the schema from `models.py` directly."""

    def __contains__(self, _item) -> bool:
        return True

    def __getitem__(self, _item):
        return None


MIGRATION_MODULES = _DisableMigrations()

# LocMemCache persists across tests in a run and pks restart per test — a
# cached menu tree / org snapshot / holiday set from one test would leak into
# the next. TTL 0 makes those caches pass-through; the attendance probe cache
# stays on (its tests manage isolation with cache.clear()).
DYNAMIC_MENU_CACHE_SECONDS = 0
DASHBOARD_ORG_CACHE_SECONDS = 0
LEAVE_HOLIDAY_CACHE_SECONDS = 0

# Plain static storage: the manifest backend raises for any file that hasn't
# been through `collectstatic`, which makes admin pages with a ModelAdmin
# Media CSS (scheduler's, for one) unrenderable in tests. Production keeps
# WhiteNoise's manifest storage.
STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
