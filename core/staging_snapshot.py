"""Turn a restored production database into a staging snapshot for IT.

Driven by ``manage.py prepare_staging_snapshot`` (deploy/README.md "Staging
snapshot"). Everything here mutates the database it runs against, which is
why the command only accepts a database whose name contains ``staging`` —
it is meant for a throwaway copy restored from a prod dump, never prod and
never your dev database.

Three jobs, in order:

1. **Re-key** every ``core.fields.Encrypted*Field`` column from the
   production ``PAYROLL_FIELD_KEYS`` to a freshly generated staging key
   (``rotate_payroll_keys`` under ``[staging, *prod]``), then continue with
   the staging key alone so every later read proves the rotation.
2. **Mock the money.** One random factor per employee in 0.85–0.90 (a
   10–15% cut) applied to *all* of that employee's money rows, so their
   salary history keeps its shape (an increment is still an increment).
   Rounded to whole taka. ``SystemRandom`` and no seed, so the factor can't
   be regenerated to reverse it. Bank/account numbers are replaced with
   random digits of the same length. Run *output* tables (register, tax
   computations, variances) can't be scaled consistently — they need a
   recalculate — so the snapshot refuses to run while they hold figures.
3. **Strip production-only state**: every beat job disabled (the weekly
   roster email would otherwise reach real clients), sessions, API tokens,
   access log, beat run log, admin change log, payroll control checks and
   the Drive media index cleared, and (by default) one staging password set on every account,
   since staging has no SSO.

Prints counts, never a value.
"""

from __future__ import annotations

import random
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.apps import apps
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.db import connection

from core import crypto
from core.fields import EncryptedDecimalField, EncryptedJSONField, encrypted_fields

_rng = random.SystemRandom()

FACTOR_MIN = 0.85
FACTOR_MAX = 0.90

#: Encrypted models whose money is scaled by the row's employee factor,
#: mapped to the path from the row to the employee's pk.
SCALED_BY_EMPLOYEE = {
    "payroll.SalaryStructure": "employee_id",
    "payroll.VariablePayEntry": "employee_id",
    "payroll.TDSEntry": "employee_id",
    "payroll.EmployeeTaxDeclaration": "employee_id",
    "payroll.SalaryLoan": "employee_id",
    "payroll.SalaryLoanAdjustment": "loan__employee_id",
    "payroll.TaxChallanAllocation": "employee_id",
    "payroll.LegacyPayrollMonth": "employee_id",
}
#: Not tied to one employee — a fresh factor per row.
SCALED_PER_ROW = {"payroll.TeamSalaryPackage"}
#: Account numbers — replaced with random digits.
FAKED_NUMBERS = {"payroll.EmployeeBankAccount": ("account_number", "routing_number")}
#: Letter input data — money keys scaled, bank number faked (see ``_scrub_letter``).
LETTER_MODEL = "documents.DocumentRequest"
#: Calculated output. Must hold no figures: scaling it would not match the
#: scaled inputs, and a recalculate on staging is the honest way to get it.
DERIVED = {
    "payroll.PayrollRun",
    "payroll.PayrollRunEmployee",
    "payroll.PayrollRunLine",
    "payroll.PayrollVariance",
    "payroll.TaxComputation",
}
#: Plaintext company-level money (``TaxChallan.total_amount`` is a deliberate
#: plaintext column) — a fresh factor per row.
PLAINTEXT_PER_ROW = {"payroll.TaxChallan": ("total_amount",)}

#: Tables emptied outright. Raw DELETE: AccessLog refuses per-row deletes by
#: design, and none of these need signals.
CLEARED_TABLES = (
    "sessions.Session",
    "audit.AccessLog",
    "scheduler.ScheduledTaskRun",
    "media_store.DriveObject",
    "admin.LogEntry",  # object_repr held amounts before __str__ dropped them
    "payroll.PayrollControlCheck",  # run output; messages can quote figures, regenerated on calculate
)

_LETTER_MONEY_KEY = re.compile(r"(salary|gross|net|tax|amount|bonus|allowance|incentive|payment|adj|tada)", re.I)
_LETTER_BANK_KEY = re.compile(r"(bank|account)", re.I)


class SnapshotError(Exception):
    """Refuse to continue; nothing has been changed yet unless stated."""


@dataclass
class Report:
    rotated: int = 0
    scaled: dict[str, int] = field(default_factory=dict)
    faked: dict[str, int] = field(default_factory=dict)
    cleared: dict[str, int] = field(default_factory=dict)
    beat_disabled: int = 0
    api_clients_revoked: int = 0
    passwords_reset: int = 0
    tokens_verified: int = 0


# -- helpers ----------------------------------------------------------------


def label(model) -> str:
    return model._meta.label


def new_factor() -> Decimal:
    return Decimal(str(round(_rng.uniform(FACTOR_MIN, FACTOR_MAX), 4)))


def scale(value, factor: Decimal):
    """Decimal × factor in whole taka; None stays None, zero stays zero."""
    if value is None:
        return None
    return (Decimal(value) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _scale_json_number(value, factor: Decimal):
    """Scale a number that may be stored as int/float/str, keeping its type."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return int(scale(Decimal(str(value)), factor))
    if isinstance(value, str):
        raw = value.strip()
        try:
            number = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            return value
        scaled = scale(number, factor)
        if "," in raw:
            return f"{scaled:,}"
        if "." in raw:
            return f"{scaled:.2f}"
        return str(scaled)
    return value


def fake_digits(value: str | None) -> str | None:
    """Random digits, same length as the original; non-digits kept in place."""
    if not value:
        return value
    return "".join(str(_rng.randrange(10)) if ch.isdigit() else ch for ch in value)


def encrypted_models():
    return [(m, encrypted_fields(m)) for m in apps.get_models() if encrypted_fields(m)]


def set_keys(keys: list[str]) -> None:
    settings.PAYROLL_FIELD_KEYS = list(keys)
    crypto.reset_fernet_cache()


def _multifernet(keys) -> MultiFernet:
    return MultiFernet([Fernet(k.encode("ascii")) for k in keys])


def check_tokens(*, readable_with: list[str], unreadable_with: list[str] | None = None) -> int:
    """Read every stored token straight from SQL (no ORM decryption).

    Each must open under ``readable_with``; if ``unreadable_with`` is given,
    none may open under it. Returns the token count; raises ``SnapshotError``
    naming the first bad column.
    """
    ok = _multifernet(readable_with)
    bad = _multifernet(unreadable_with) if unreadable_with else None
    qn = connection.ops.quote_name
    count = 0
    with connection.cursor() as cursor:
        for model, fields in encrypted_models():
            table = qn(model._meta.db_table)
            for f in fields:
                col = qn(f.column)
                cursor.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL")
                for (token,) in cursor.fetchall():
                    if token == "":
                        continue
                    raw = token.encode("ascii")
                    try:
                        ok.decrypt(raw)
                    except InvalidToken:
                        raise SnapshotError(
                            f"{label(model)}.{f.name}: a value does not open with the expected key."
                        ) from None
                    if bad is not None:
                        try:
                            bad.decrypt(raw)
                        except InvalidToken:
                            pass
                        else:
                            raise SnapshotError(
                                f"{label(model)}.{f.name}: a value still opens with the production key."
                            )
                    count += 1
    return count


# -- preflight --------------------------------------------------------------


def preflight(prod_keys: list[str]) -> int:
    """Refuse before any write. Returns the number of encrypted values found."""
    known = set(SCALED_BY_EMPLOYEE) | SCALED_PER_ROW | set(FAKED_NUMBERS) | DERIVED | {LETTER_MODEL}
    unknown = sorted(label(m) for m, _ in encrypted_models() if label(m) not in known)
    if unknown:
        raise SnapshotError(
            "Encrypted model(s) this command doesn't know how to mock: "
            + ", ".join(unknown)
            + ". Add them to core/staging_snapshot.py before taking a snapshot."
        )

    for name in sorted(DERIVED):
        model = apps.get_model(name)
        fields = encrypted_fields(model)
        for obj in model._default_manager.only("pk", *[f.attname for f in fields]).iterator():
            for f in fields:
                value = getattr(obj, f.attname)
                if value not in (None, "", 0, {}, []) and not (isinstance(value, Decimal) and value == 0):
                    raise SnapshotError(
                        f"{name} holds calculated figures (pk={obj.pk}). Real pay has been run, so "
                        "the run output can't simply be scaled — extend this command to clear and "
                        "recalculate runs first."
                    )

    try:
        return check_tokens(readable_with=prod_keys)
    except SnapshotError as exc:
        raise SnapshotError(
            f"{exc} The database was not written under the PAYROLL_FIELD_KEYS this process has — "
            "run with PAYROLL_FIELD_KEYS=<production key> set inline."
        ) from None


# -- the mutation -----------------------------------------------------------


class _Factors(dict):
    def __missing__(self, key):
        self[key] = value = new_factor()
        return value


def _scale_row_changes(obj, fields, factor: Decimal) -> dict:
    changes = {}
    for f in fields:
        value = getattr(obj, f.attname)
        if isinstance(f, EncryptedDecimalField):
            changes[f.attname] = scale(value, factor)
        elif isinstance(f, EncryptedJSONField):
            if isinstance(value, dict):
                changes[f.attname] = {k: _scale_json_number(v, factor) for k, v in value.items()}
        else:  # EncryptedCharField holding a number
            changes[f.attname] = _scale_json_number(value, factor) if value else value
    return changes


def _resolve(obj, path: str):
    for part in path.split("__"):
        obj = getattr(obj, part)
        if obj is None:
            return None
    return obj


def mock_money(report: Report) -> None:
    factors = _Factors()

    for name, path in SCALED_BY_EMPLOYEE.items():
        model = apps.get_model(name)
        fields = encrypted_fields(model)
        qs = model._default_manager.all()
        if "__" in path:
            qs = qs.select_related(path.rsplit("__", 1)[0])
        n = 0
        for obj in qs.order_by("pk").iterator(chunk_size=500):
            changes = _scale_row_changes(obj, fields, factors[_resolve(obj, path)])
            model._default_manager.filter(pk=obj.pk).update(**changes)
            n += 1
        report.scaled[name] = n

    for name in SCALED_PER_ROW:
        model = apps.get_model(name)
        fields = encrypted_fields(model)
        n = 0
        for obj in model._default_manager.order_by("pk").iterator():
            model._default_manager.filter(pk=obj.pk).update(**_scale_row_changes(obj, fields, new_factor()))
            n += 1
        report.scaled[name] = n

    for name, cols in PLAINTEXT_PER_ROW.items():
        model = apps.get_model(name)
        n = 0
        for obj in model._default_manager.order_by("pk").iterator():
            factor = new_factor()
            model._default_manager.filter(pk=obj.pk).update(
                **{c: scale(getattr(obj, c), factor) for c in cols}
            )
            n += 1
        report.scaled[name] = n

    for name, cols in FAKED_NUMBERS.items():
        model = apps.get_model(name)
        n = 0
        for obj in model._default_manager.order_by("pk").iterator():
            model._default_manager.filter(pk=obj.pk).update(**{c: fake_digits(getattr(obj, c)) for c in cols})
            n += 1
        report.faked[name] = n

    model = apps.get_model(LETTER_MODEL)
    n = 0
    for obj in model._default_manager.order_by("pk").iterator():
        data = obj.input_data
        if not isinstance(data, dict) or not data:
            continue
        scrubbed = scrub_letter(data, factors[obj.employee_id])
        if scrubbed != data:
            model._default_manager.filter(pk=obj.pk).update(input_data=scrubbed)
            n += 1
    report.scaled[LETTER_MODEL] = n


def scrub_letter(data: dict, factor: Decimal) -> dict:
    """Letter inputs: money-looking numeric values scaled, bank numbers faked."""
    out = {}
    for key, value in data.items():
        if _LETTER_BANK_KEY.search(key) and isinstance(value, str):
            out[key] = fake_digits(value)
        elif _LETTER_MONEY_KEY.search(key) and not key.endswith("_name"):
            out[key] = _scale_json_number(value, factor)
        else:
            out[key] = value
    return out


def strip_production_state(report: Report, *, user_password: str | None) -> None:
    from django_celery_beat.models import PeriodicTask, PeriodicTasks

    report.beat_disabled = PeriodicTask.objects.filter(enabled=True).update(enabled=False)
    PeriodicTasks.update_changed()

    ApiClient = apps.get_model("accounts.ApiClient")
    report.api_clients_revoked = ApiClient.objects.update(
        is_active=False, token_hash=None, token_prefix="", token_issued_at=None,
    )

    if user_password:
        User = apps.get_model(settings.AUTH_USER_MODEL)
        report.passwords_reset = User.objects.update(password=make_password(user_password))

    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        for name in CLEARED_TABLES:
            model = apps.get_model(name)
            cursor.execute(f"DELETE FROM {qn(model._meta.db_table)}")
            report.cleared[name] = cursor.rowcount


def rekey_and_mock(*, prod_keys: list[str], staging_key: str, user_password: str | None, stdout=None) -> Report:
    """Steps 1–3. Caller owns the transaction and restores the keys after."""
    report = Report()
    set_keys([staging_key, *prod_keys])
    call_command("rotate_payroll_keys", stdout=stdout)
    # From here on only the staging key is loaded, so every read below
    # proves the rotation reached that row.
    set_keys([staging_key])
    mock_money(report)
    strip_production_state(report, user_password=user_password)
    report.tokens_verified = check_tokens(readable_with=[staging_key], unreadable_with=prod_keys)
    return report


# -- handover files ---------------------------------------------------------


#: Copied from the running settings into staging.env (default; ``--no-integrations``
#: leaves them blank). SSO + the read-only pulls from TimeLy / meals / TTMS.
INTEGRATION_SETTINGS = (
    "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
    "TIMELY_API_BASE_URL", "TIMELY_API_TOKEN",
    "MEAL_COUNTS_API_BASE_URL", "MEAL_COUNTS_API_TOKEN",
    "TTMS_API_BASE_URL", "TTMS_API_TOKEN",
)
#: Copied only with ``--include-smtp``: staging would then mail real people.
SMTP_SETTINGS = (
    "EMAIL_BACKEND", "EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_SSL", "EMAIL_USE_TLS",
    "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD", "DEFAULT_FROM_EMAIL",
)


def site(host_or_url: str) -> tuple[str, str]:
    """``--host`` → (origin, ALLOWED_HOSTS). A bare hostname means https; a full
    URL (``http://localhost:8000``) keeps its scheme and port. Local runs allow
    both loopback names."""
    value = host_or_url.strip().rstrip("/")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.hostname:
        raise ValueError(f"Not a hostname or URL: {host_or_url!r}")
    hosts = [parsed.hostname]
    if parsed.hostname in ("localhost", "127.0.0.1"):
        hosts = ["localhost", "127.0.0.1"]
    return f"{parsed.scheme}://{parsed.netloc}", ",".join(hosts)


def copied_settings(names) -> dict:
    return {name: getattr(settings, name, "") for name in names}


def with_values(env_text: str, values: dict) -> str:
    """Set ``KEY=value`` lines in a generated env file (replace, else append)."""
    lines = env_text.splitlines()
    for key, value in values.items():
        text = "1" if value is True else "0" if value is False else ("" if value is None else str(value))
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={text}"
                break
        else:
            lines.append(f"{key}={text}")
    return "\n".join(lines) + "\n"


def staging_env(*, host: str, staging_key: str, secret_key: str, db_name: str, db_user: str,
                db_password: str, time_zone: str) -> str:
    origin, allowed_hosts = site(host)
    return f"""\
# Orbis STAGING environment — generated by `manage.py prepare_staging_snapshot`.
# Every secret here is new and staging-only; none of them work against
# production, and production's values must never be copied in.

# --- Core Django ---
SECRET_KEY={secret_key}
DJANGO_DEBUG=False
ALLOWED_HOSTS={allowed_hosts}
CSRF_TRUSTED_ORIGINS={origin}
TIME_ZONE={time_zone}
PUBLIC_BASE_URL={origin}
# 1 once nginx terminates TLS in front of gunicorn (Secure cookies).
BEHIND_TLS_PROXY=0
SECURE_HSTS_SECONDS=0

# --- Payroll field encryption (staging-only key; the snapshot is encrypted under it) ---
PAYROLL_FIELD_KEYS={staging_key}

# --- Media: local disk. Production's files are not part of the snapshot. ---
MEDIA_BACKEND=filesystem

# --- Database (create it utf8mb4 / utf8mb4_0900_ai_ci and import orbis_staging.sql) ---
DB_ENGINE=mysql
DB_NAME={db_name}
DB_USER={db_user}
DB_PASSWORD={db_password}
DB_HOST=localhost
DB_PORT=3306

# --- Cache / background tasks: blank = in-process cache, tasks run inline. ---
REDIS_URL=
TASK_QUEUE_ENABLED=0

# --- SSO off: register a separate Entra app for staging if SSO is needed. ---
AZURE_TENANT_ID=
AZURE_CLIENT_ID=
AZURE_CLIENT_SECRET=
OIDC_ALLOWED_EMAIL_DOMAINS=sebpo.com

# --- Integrations off (blank tokens degrade silently; TTMS raises if its job is run). ---
TIMELY_API_BASE_URL=
TIMELY_API_TOKEN=
EXTERNAL_API_TOKEN=
MEAL_COUNTS_API_BASE_URL=
MEAL_COUNTS_API_TOKEN=
TTMS_API_BASE_URL=
TTMS_API_TOKEN=

# --- Email goes to the console log, never to a real inbox. ---
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
EMAIL_HOST=
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
DEFAULT_FROM_EMAIL=Orbis Staging <no-reply@localhost>
HR_NOTIFICATION_EMAILS=
OFFBOARDING_NOTIFICATIONS_ENABLED=0
"""


def handover_notes(*, host: str, db_name: str, db_user: str, source_db: str, taken_at: str,
                   report: Report, user_password: str | None, sso: bool = False,
                   integrations: bool = False, smtp: bool = False) -> str:
    origin, _ = site(host)
    password = (
        f"Every account's local password is set to:\n\n    {user_password}\n"
        if user_password else
        "Account passwords were left as they were in production.\n"
    )
    if sso:
        login = (
            "SSO (Microsoft Entra) is configured with the same app registration as\n"
            "production. This exact redirect URI must be registered on that app\n"
            "(scheme, host AND port must match the address you browse to, so\n"
            "localhost and 127.0.0.1 are different URIs):\n\n"
            f"    {origin}/oidc/callback/\n\n"
            "Staff sign in with their own Microsoft account; it is matched to their\n"
            "staging user by email. While SSO is on, password login works for\n"
            "superuser accounts only.\n\n" + password
        )
    else:
        login = password + "\nSSO is not configured: sign in with any employee's email address."
    outside = []
    if integrations:
        outside.append(
            "- TimeLy, meals and TTMS tokens are the production ones. Those calls only\n"
            "  READ from the live systems (dashboard presence, meal counts, daily hours).\n"
            "  The jobs that pull them nightly are disabled; run them by hand if needed."
        )
    outside.append(
        "- EXTERNAL_API_TOKEN is new and staging-only. A system that pushes shifts\n"
        "  into staging (e.g. dsndd's test instance) must be given this value."
    )
    if smtp:
        outside.append(
            "- REAL EMAIL IS ON. staging.env carries the production SMTP mailbox and\n"
            "  the users are real employees: leave approvals, letters, policy sign-off\n"
            "  and offboarding mails WILL reach their inboxes. Set\n"
            "  EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend to stop it."
        )
    else:
        outside.append("- Email goes to the console log only; nobody receives anything.")
    login += "\n\nTalking to other systems\n------------------------\n" + "\n".join(outside)
    return f"""\
Orbis staging snapshot
======================

Taken {taken_at} from a restored copy of the production database ({source_db}).

What was changed from production
--------------------------------
- Payroll money columns are re-encrypted under a new staging-only key
  (PAYROLL_FIELD_KEYS in staging.env). The production key is not included.
- Every salary and money figure is MOCK DATA: each employee's figures were cut
  by a random 10-15%, so nothing in this database is anyone's real pay.
  Bank/routing numbers are random digits.
- All scheduled (celery beat) jobs are DISABLED. Leave them disabled unless
  you know what a job does. The weekly roster email in particular goes to
  external clients.
- Sessions, API client tokens, the access log, the beat run log, the admin
  change log, payroll control checks and the Google Drive media index are
  empty.
- Uploaded files (photos, scans, generated PDFs) are NOT included. Pages that
  link to a file show "not found". That is expected.

Setting it up
-------------
1. Create the database:
     CREATE DATABASE {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
     CREATE USER '{db_user}'@'localhost' IDENTIFIED BY '<DB_PASSWORD from staging.env>';
     GRANT ALL ON {db_name}.* TO '{db_user}'@'localhost';
2. Import:  mysql {db_name} < orbis_staging.sql
3. Check out the same Orbis commit as production, copy staging.env to .env
   and adjust ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS / PUBLIC_BASE_URL if the
   address is not {origin}.
4. python manage.py migrate        (should report nothing to apply)
   python manage.py collectstatic --noinput
5. Do NOT run `seed_beat_schedule --force`. It would re-enable the jobs.

Signing in
----------
{login}

Counts
------
rotated values      : {report.tokens_verified}
scaled              : {", ".join(f"{k.split('.')[-1]} {v}" for k, v in report.scaled.items())}
faked               : {", ".join(f"{k.split('.')[-1]} {v}" for k, v in report.faked.items())}
beat jobs disabled  : {report.beat_disabled}
API clients revoked : {report.api_clients_revoked}
cleared             : {", ".join(f"{k.split('.')[-1]} {v}" for k, v in report.cleared.items())}
"""
