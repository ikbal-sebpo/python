"""Build the staging handover from a restored copy of the production database.

    # 1. restore a fresh prod dump into a scratch database (name must contain "staging")
    mysql -e "CREATE DATABASE orbis_staging_prep CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    mysql orbis_staging_prep < orbis_db-<date>.sql
    # 2. point this process at it, with the production key it was written under
    DB_NAME=orbis_staging_prep PAYROLL_FIELD_KEYS=<prod key> \\
        python manage.py prepare_staging_snapshot --host staging.example.com --output-dir ~/handover

What it does is in ``core/staging_snapshot.py``: re-key every encrypted
column to a new staging key, cut every employee's money by a random 10–15%,
disable beat jobs and clear sessions/tokens/logs. Then it writes
``staging.env`` (fresh secrets; SSO and the TimeLy/meals/TTMS tokens copied
from this .env unless ``--no-integrations``; email on the console unless
``--include-smtp``), ``HANDOVER.txt``
and a ``mysqldump``, and packs the three into one AES-256 7z archive whose
password is printed once. Send the password to IT separately from the file.

The scratch database is left in its staging state; drop it afterwards.
Production is never touched, and its key is not rotated.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import get_random_secret_key
from django.db import transaction
from django.utils import timezone

from core import staging_snapshot as snap
from core.crypto import generate_key


class Command(BaseCommand):
    help = "Re-key, mock salaries and package a restored prod database as a staging snapshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host", required=True, help="Staging hostname (https assumed) or full URL, e.g. orbis-staging.sebpo.net or http://localhost:8000",
        )
        parser.add_argument("--output-dir", required=True, help="Where the .7z goes (must be outside the repo).")
        parser.add_argument("--target-db-name", default="orbis_staging", help="DB_NAME written to staging.env.")
        parser.add_argument("--target-db-user", default="orbis_staging", help="DB_USER written to staging.env.")
        parser.add_argument(
            "--keep-passwords", action="store_true",
            help="Leave account passwords as in production instead of setting one staging password.",
        )
        parser.add_argument(
            "--no-integrations", action="store_true",
            help="Leave SSO and the TimeLy/meals/TTMS tokens blank instead of copying them from this .env.",
        )
        parser.add_argument(
            "--include-smtp", action="store_true",
            help="Copy this .env's SMTP settings. Staging will then email REAL employees.",
        )
        parser.add_argument("--keep-plain", action="store_true", help="Keep the unencrypted files next to the archive.")
        parser.add_argument("--noinput", action="store_true", help="Skip the type-the-database-name confirmation.")

    def handle(self, *args, **opts):
        try:
            snap.site(opts["host"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        db = settings.DATABASES["default"]
        db_name = str(db["NAME"])
        self._guard(db, db_name)

        output_dir = Path(opts["output_dir"]).expanduser().resolve()
        if output_dir.is_relative_to(Path(settings.BASE_DIR).resolve()):
            raise CommandError("--output-dir must be outside the repository, so the snapshot can never be committed.")
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        archive = output_dir / f"orbis-staging-{stamp}.7z"
        if archive.exists():
            raise CommandError(f"{archive} already exists.")
        for tool in ("mysqldump", "7z"):
            if not shutil.which(tool):
                raise CommandError(f"`{tool}` is not on PATH.")

        prod_keys = [k.strip() for k in settings.PAYROLL_FIELD_KEYS if k.strip()]
        try:
            found = snap.preflight(prod_keys)
        except snap.SnapshotError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Preflight: {found} encrypted value(s) open under the current key. Nothing changed yet.")

        if not opts["noinput"]:
            typed = input(f"This rewrites database '{db_name}' in place. Type its name to continue: ")
            if typed.strip() != db_name:
                raise CommandError("Name did not match — nothing changed.")

        staging_key = generate_key()
        user_password = None if opts["keep_passwords"] else secrets.token_urlsafe(12)
        archive_password = secrets.token_urlsafe(18)

        # The staging key goes to disk before the first write: if anything
        # after the rotation fails, the database is still readable.
        workdir = Path(tempfile.mkdtemp(prefix="orbis-staging-", dir=output_dir))
        os.chmod(workdir, 0o700)
        env_path = workdir / "staging.env"
        integrations = not opts["no_integrations"]
        copied = {"EXTERNAL_API_TOKEN": secrets.token_urlsafe(32)}  # inbound: never prod's
        if integrations:
            copied.update(snap.copied_settings(snap.INTEGRATION_SETTINGS))
        if opts["include_smtp"]:
            copied.update(snap.copied_settings(snap.SMTP_SETTINGS))
        sso = bool(copied.get("AZURE_TENANT_ID") and copied.get("AZURE_CLIENT_ID") and copied.get("AZURE_CLIENT_SECRET"))
        env_path.touch(mode=0o600)
        env_path.write_text(snap.with_values(snap.staging_env(
            host=opts["host"], staging_key=staging_key, secret_key=get_random_secret_key(),
            db_name=opts["target_db_name"], db_user=opts["target_db_user"],
            db_password=secrets.token_urlsafe(24), time_zone=settings.TIME_ZONE,
        ), copied))

        try:
            with transaction.atomic():
                report = snap.rekey_and_mock(
                    prod_keys=prod_keys, staging_key=staging_key,
                    user_password=user_password, stdout=self.stdout,
                )
        except Exception as exc:
            # Rolled back, so the key just generated protects nothing.
            shutil.rmtree(workdir, ignore_errors=True)
            if isinstance(exc, snap.SnapshotError):
                raise CommandError(f"{exc} Rolled back — the database is unchanged.") from None
            raise
        finally:
            snap.set_keys(prod_keys)
        self.stdout.write(f"Verified: {report.tokens_verified} value(s) open under the staging key and not the production one.")

        try:
            handover = workdir / "HANDOVER.txt"
            handover.touch(mode=0o600)
            handover.write_text(snap.handover_notes(
                host=opts["host"], db_name=opts["target_db_name"], db_user=opts["target_db_user"],
                source_db=db_name, taken_at=timezone.localtime().strftime("%Y-%m-%d %H:%M %Z"),
                report=report, user_password=user_password, sso=sso,
                integrations=integrations, smtp=opts["include_smtp"],
            ))
            self._dump(db, workdir / "orbis_staging.sql")
            self._archive(workdir, archive, archive_password)
        except Exception as exc:
            raise CommandError(
                f"Packaging failed ({exc}). The database IS already converted; its staging key is in {env_path}."
            ) from None

        if opts["keep_plain"]:
            self.stdout.write(f"Plain files kept in {workdir}")
        else:
            shutil.rmtree(workdir)

        self.stdout.write("")
        for name, n in {**report.scaled, **report.faked}.items():
            self.stdout.write(f"  mocked  {name}: {n}")
        for name, n in report.cleared.items():
            self.stdout.write(f"  cleared {name}: {n}")
        self.stdout.write(f"  beat jobs disabled: {report.beat_disabled}, API clients revoked: {report.api_clients_revoked}, "
                          f"passwords reset: {report.passwords_reset}")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Archive: {archive}"))
        self.stdout.write(self.style.WARNING(f"Archive password (shown once — send it separately): {archive_password}"))
        if sso:
            self.stdout.write(self.style.WARNING(
                f"SSO is on: {snap.site(opts['host'])[0]}/oidc/callback/ must be a redirect URI "
                "on the Entra app registration."
            ))
        if opts["include_smtp"]:
            self.stdout.write(self.style.WARNING("Real SMTP is in staging.env — staging will email real employees."))
        self.stdout.write(f"Now drop the scratch database: DROP DATABASE {db_name};")

    # -- guards / packaging -------------------------------------------------

    def _guard(self, db, db_name):
        if db["ENGINE"] != "django.db.backends.mysql":
            raise CommandError("The snapshot is a mysqldump — run against the MySQL scratch database.")
        if "staging" not in db_name.lower():
            raise CommandError(
                f"Refusing to rewrite '{db_name}'. Restore the prod dump into a scratch database whose "
                "name contains 'staging' (e.g. orbis_staging_prep) and run with DB_NAME=<that name>."
            )

    def _dump(self, db, out: Path) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as cnf:
            os.chmod(cnf.name, 0o600)
            cnf.write("[client]\n")
            cnf.write(f"user={db['USER']}\npassword={db['PASSWORD'] or ''}\n")
            cnf.write(f"host={db.get('HOST') or 'localhost'}\nport={db.get('PORT') or 3306}\n")
        try:
            subprocess.run(
                [
                    "mysqldump", f"--defaults-extra-file={cnf.name}",
                    "--single-transaction", "--routines", "--triggers", "--no-tablespaces",
                    "--set-gtid-purged=OFF", "--default-character-set=utf8mb4",
                    f"--result-file={out}", str(db["NAME"]),
                ],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"mysqldump: {exc.stderr.strip()}") from None
        finally:
            os.unlink(cnf.name)
        os.chmod(out, 0o600)

    def _archive(self, workdir: Path, archive: Path, password: str) -> None:
        # -mhe=on encrypts the file names too. The password is briefly on the
        # 7z command line (visible to `ps` on this machine only).
        try:
            subprocess.run(
                ["7z", "a", "-t7z", "-mhe=on", "-mx=9", f"-p{password}", "-bso0", "-bsp0",
                 str(archive), "orbis_staging.sql", "staging.env", "HANDOVER.txt"],
                cwd=workdir, check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"7z: {(exc.stderr or exc.stdout).strip()}") from None
        os.chmod(archive, 0o600)
