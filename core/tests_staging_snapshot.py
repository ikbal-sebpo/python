"""prepare_staging_snapshot — re-key, mock money, strip production state.

The mutation is exercised through ``core.staging_snapshot`` directly; the
command itself only adds the MySQL-only guards and packaging, which are
checked for their refusals.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from accounts.models import ApiClient, Department
from audit.models import AccessLog
from core import crypto
from core import staging_snapshot as snap
from payroll.management.commands.prepare_staging_snapshot import Command
from documents.models import DocumentRequest, DocumentType
from payroll.models import EmployeeBankAccount, PayrollPeriod, PayrollRun, SalaryStructure, TeamSalaryPackage

User = get_user_model()
PROD_KEY = settings.PAYROLL_FIELD_KEYS[0]


def _user(email, employee_id):
    return User.objects.create_user(
        email=email, username=email, password="prod-pass-1!", first_name="T", last_name="User",
        employee_id=employee_id,
    )


def _raw(model, column):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {model._meta.db_table}")
        return [r[0] for r in cursor.fetchall()]


class StagingSnapshotTests(TestCase):
    def setUp(self):
        self.alice = _user("alice@example.com", "001")
        self.bob = _user("bob@example.com", "002")
        SalaryStructure.objects.create(
            employee=self.alice, effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31),
            reason=SalaryStructure.Reason.NEW_HIRE, gross_monthly=Decimal("40000"),
            status=SalaryStructure.Status.APPROVED, revision_no=1,
        )
        SalaryStructure.objects.create(
            employee=self.alice, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("80000"), status=SalaryStructure.Status.APPROVED, revision_no=2,
        )
        SalaryStructure.objects.create(
            employee=self.bob, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
            gross_monthly=Decimal("55555"), status=SalaryStructure.Status.APPROVED, revision_no=1,
        )
        TeamSalaryPackage.objects.create(
            department=Department.objects.create(name="Ops"), gross_monthly=Decimal("20000"),
            effective_from=date(2026, 1, 1),
        )
        EmployeeBankAccount.objects.create(
            employee=self.bob, account_type=EmployeeBankAccount.AccountType.MFS_BKASH,
            account_name="Bob", account_number="01711223344",
        )
        doc_type = DocumentType.objects.create(code="SAL", name="sal", display_name="Salary certificate")
        DocumentRequest.objects.create(
            employee=self.bob, document_type=doc_type, applied_by=self.bob,
            input_data={
                "salary_month_1_gross_salary": "50,000", "salary_month_1_name": "July 2026",
                "bank_account_no": "1234-5678", "reason": "visa", "letter_date": "2026-10-01",
            },
        )
        every = IntervalSchedule.objects.create(every=1, period=IntervalSchedule.DAYS)
        PeriodicTask.objects.create(name="weekly roster", task="x", interval=every, enabled=True)
        ApiClient.objects.create(name="timely", token_prefix="abcd", token_hash="f" * 64, scopes=["directory:read"])
        AccessLog.objects.create(method="GET", path="/x/", actor_label="a@b")
        Session.objects.create(session_key="s" * 32, session_data="x", expire_date=timezone.now())
        self.staging_key = crypto.generate_key()

    def tearDown(self):
        snap.set_keys([PROD_KEY])

    def _run(self, password="staging-pw"):
        snap.preflight([PROD_KEY])
        return snap.rekey_and_mock(
            prod_keys=[PROD_KEY], staging_key=self.staging_key, user_password=password, stdout=StringIO(),
        )

    def test_every_token_moves_to_the_staging_key(self):
        report = self._run()
        self.assertGreater(report.tokens_verified, 0)
        prod, staging = Fernet(PROD_KEY.encode()), Fernet(self.staging_key.encode())
        for token in _raw(SalaryStructure, "gross_monthly"):
            staging.decrypt(token.encode())
            with self.assertRaises(InvalidToken):
                prod.decrypt(token.encode())

    def test_salaries_drop_10_to_15_percent_with_one_factor_per_employee(self):
        self._run()
        snap.set_keys([self.staging_key])
        alice = list(SalaryStructure.objects.filter(employee=self.alice).order_by("effective_from")
                     .values_list("gross_monthly", flat=True))
        (bob,) = SalaryStructure.objects.filter(employee=self.bob).values_list("gross_monthly", flat=True)
        for new, old in ((alice[0], 40000), (alice[1], 80000), (bob, 55555)):
            self.assertGreaterEqual(new, Decimal(old) * Decimal("0.85") - 1)
            self.assertLessEqual(new, Decimal(old) * Decimal("0.90") + 1)
            self.assertEqual(new, new.to_integral_value())
        # Same factor across Alice's history: the 2x increment survives (±1 for rounding).
        self.assertLessEqual(abs(alice[1] - 2 * alice[0]), 1)
        (package,) = TeamSalaryPackage.objects.values_list("gross_monthly", flat=True)
        self.assertTrue(17000 <= package <= 18000)

    def test_bank_numbers_and_letter_figures_are_mocked(self):
        self._run()
        snap.set_keys([self.staging_key])
        account = EmployeeBankAccount.objects.get()
        self.assertEqual(len(account.account_number), 11)
        self.assertTrue(account.account_number.isdigit())
        data = DocumentRequest.objects.get().input_data
        self.assertNotEqual(data["salary_month_1_gross_salary"], "50,000")
        self.assertTrue(42_500 <= int(data["salary_month_1_gross_salary"].replace(",", "")) <= 45_000)
        self.assertRegex(data["bank_account_no"], r"^\d{4}-\d{4}$")
        self.assertEqual(data["salary_month_1_name"], "July 2026")
        self.assertEqual(data["letter_date"], "2026-10-01")
        self.assertEqual(data["reason"], "visa")

    def test_production_state_is_stripped(self):
        report = self._run()
        self.assertFalse(PeriodicTask.objects.filter(enabled=True).exists())
        client = ApiClient.objects.get()
        self.assertFalse(client.is_active)
        self.assertIsNone(client.token_hash)
        self.assertFalse(AccessLog.objects.exists())
        self.assertFalse(Session.objects.exists())
        self.assertEqual(report.passwords_reset, User.objects.count())
        self.assertTrue(User.objects.get(pk=self.alice.pk).check_password("staging-pw"))

    def test_keep_passwords_leaves_hashes_alone(self):
        self._run(password=None)
        self.assertTrue(User.objects.get(pk=self.alice.pk).check_password("prod-pass-1!"))

    def test_preflight_refuses_calculated_runs(self):
        run = PayrollRun.objects.create(period=PayrollPeriod.for_month(2026, 7), status=PayrollRun.Status.INPUTS_OPEN)
        snap.preflight([PROD_KEY])  # an empty run is fine
        PayrollRun.objects.filter(pk=run.pk).update(net_total=Decimal("123"))
        with self.assertRaisesMessage(snap.SnapshotError, "calculated figures"):
            snap.preflight([PROD_KEY])

    def test_preflight_refuses_a_foreign_key(self):
        with self.assertRaisesMessage(snap.SnapshotError, "PAYROLL_FIELD_KEYS=<production key>"):
            snap.preflight([crypto.generate_key()])


class CommandGuardTests(TestCase):
    def test_refuses_sqlite(self):
        with self.assertRaisesMessage(CommandError, "MySQL"):
            call_command("prepare_staging_snapshot", host="s.example", output_dir="/tmp/x", noinput=True)

    def test_refuses_a_database_not_named_staging(self):
        with self.assertRaisesMessage(CommandError, "Refusing to rewrite 'orbis_db'"):
            Command()._guard({"ENGINE": "django.db.backends.mysql"}, "orbis_db")
        Command()._guard({"ENGINE": "django.db.backends.mysql"}, "orbis_staging_prep")

    def test_staging_env_carries_no_production_secret(self):
        env = snap.staging_env(
            host="s.example", staging_key="K", secret_key="S", db_name="d", db_user="u",
            db_password="p", time_zone="Asia/Dhaka",
        )
        self.assertIn("PAYROLL_FIELD_KEYS=K\n", env)
        self.assertIn("EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend", env)
        self.assertNotIn(PROD_KEY, env)
        for line in env.splitlines():
            if line.startswith(("AZURE_CLIENT_SECRET=", "TIMELY_API_TOKEN=", "EMAIL_HOST_PASSWORD=",
                                "EXTERNAL_API_TOKEN=", "MEDIA_ENCRYPTION_KEYS=")):
                self.assertTrue(line.endswith("="), line)

    def test_copied_values_replace_the_blank_lines(self):
        env = snap.staging_env(
            host="s.example", staging_key="K", secret_key="S", db_name="d", db_user="u",
            db_password="p", time_zone="Asia/Dhaka",
        )
        out = snap.with_values(env, {"AZURE_CLIENT_SECRET": "sec", "EMAIL_USE_SSL": True, "EMAIL_PORT": 465})
        self.assertIn("AZURE_CLIENT_SECRET=sec\n", out)
        self.assertEqual(out.count("AZURE_CLIENT_SECRET="), 1)
        self.assertIn("EMAIL_USE_SSL=1\n", out)
        self.assertIn("EMAIL_PORT=465\n", out)

    def test_handover_warns_about_the_redirect_uri_and_real_email(self):
        notes = snap.handover_notes(
            host="s.example", db_name="d", db_user="u", source_db="x", taken_at="now",
            report=snap.Report(), user_password="pw", sso=True, integrations=True, smtp=True,
        )
        self.assertIn("https://s.example/oidc/callback/", notes)
        self.assertIn("REAL EMAIL IS ON", notes)

    def test_localhost_url_keeps_http_and_port(self):
        self.assertEqual(snap.site("http://localhost:8000/"), ("http://localhost:8000", "localhost,127.0.0.1"))
        self.assertEqual(snap.site("orbis-staging.sebpo.net"), ("https://orbis-staging.sebpo.net", "orbis-staging.sebpo.net"))
        env = snap.staging_env(
            host="http://localhost:8000", staging_key="K", secret_key="S", db_name="d", db_user="u",
            db_password="p", time_zone="Asia/Dhaka",
        )
        self.assertIn("ALLOWED_HOSTS=localhost,127.0.0.1\n", env)
        self.assertIn("CSRF_TRUSTED_ORIGINS=http://localhost:8000\n", env)
        self.assertIn("PUBLIC_BASE_URL=http://localhost:8000\n", env)
