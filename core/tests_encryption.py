"""Field-level encryption of money at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §6.

What a database-credential holder sees after Phase 1 is a Fernet token, never
a number: the raw-SQL assertions below are the point of the whole plan. The
rest pins the guard rails (no SQL on ciphertext, guarded purge, key
rotation), the register's Python relocation, the admin money mixin and the
tightened money-visibility matrix (§3).
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.db import connection, models
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department
from core import crypto
from core.fields import EncryptedJSONField, encrypted_fields
from core.migration_utils import PURGE_FLAG, guarded_purge
from documents.models import DocumentRequest, DocumentType
from dynamic_menu.models import MainMenu, ProtectedView
from employees.models import JobInfo
from payroll.models import (
    EmployeeBankAccount, PayrollPeriod, PayrollRun, PayrollRunEmployee, PayrollVariance, SalaryStructure,
)

User = get_user_model()

TEST_KEY = settings.PAYROLL_FIELD_KEYS[0]


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
        employee_id=employee_id, **extra,
    )


def _group(user, *names):
    for name in names:
        group, _ = Group.objects.get_or_create(name=name)
        user.groups.add(group)


def _structure(employee, gross, **extra):
    return SalaryStructure.objects.create(
        employee=employee, effective_from=date(2026, 1, 1), reason=SalaryStructure.Reason.NEW_HIRE,
        gross_monthly=Decimal(gross), status=extra.pop("status", SalaryStructure.Status.APPROVED),
        revision_no=1, **extra,
    )


def _raw(model, column, pk):
    """The column exactly as the database holds it — what a DB client sees."""
    db_pk = model._meta.pk.get_db_prep_value(pk, connection)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {model._meta.db_table} WHERE id = %s", [db_pk])
        return cursor.fetchone()[0]


class FieldRoundTripTests(TestCase):
    def setUp(self):
        self.employee = _user("alice@example.com", "001")

    def test_decimal_round_trips_but_the_column_holds_a_token(self):
        row = _structure(self.employee, "12345.67")
        row.refresh_from_db()
        self.assertEqual(row.gross_monthly, Decimal("12345.67"))
        self.assertIsInstance(row.gross_monthly, Decimal)

        stored = _raw(SalaryStructure, "gross_monthly", row.pk)
        self.assertTrue(stored.startswith("gAAAA"), stored[:12])
        self.assertNotIn("12345", stored)
        self.assertNotIn(".67", stored)
        self.assertEqual(crypto.decrypt(stored), "12345.67")

    def test_same_value_encrypts_differently_each_time(self):
        a = _structure(self.employee, "50000")
        b = _structure(_user("bob@example.com", "002"), "50000")
        token_a = _raw(SalaryStructure, "gross_monthly", a.pk)
        token_b = _raw(SalaryStructure, "gross_monthly", b.pk)
        self.assertNotEqual(token_a, token_b)

    def test_char_field_round_trips_as_a_token(self):
        account = EmployeeBankAccount.objects.create(
            employee=self.employee, account_type=EmployeeBankAccount.AccountType.MFS_BKASH,
            account_name="Alice", account_number="01711223344",
        )
        account.refresh_from_db()
        self.assertEqual(account.account_number, "01711223344")
        self.assertEqual(account.routing_number, "")
        stored = _raw(EmployeeBankAccount, "account_number", account.pk)
        self.assertTrue(stored.startswith("gAAAA"))
        self.assertNotIn("0171", stored)

    def test_json_field_round_trips_as_a_token(self):
        doc_type = DocumentType.objects.create(code="SAL", name="sal", display_name="Salary certificate")
        request = DocumentRequest.objects.create(
            employee=self.employee, document_type=doc_type, applied_by=self.employee,
            input_data={"salary_month_1_gross_salary": "92500", "reason": "visa"},
        )
        request.refresh_from_db()
        self.assertEqual(request.input_data["salary_month_1_gross_salary"], "92500")
        stored = _raw(DocumentRequest, "input_data", request.pk)
        self.assertTrue(stored.startswith("gAAAA"))
        self.assertNotIn("92500", stored)

        field = EncryptedJSONField()
        self.assertEqual(field.from_db_value(field.get_db_prep_value({"a": [1, 2]}, connection), None, connection), {"a": [1, 2]})

    def test_values_list_only_and_update_still_work(self):
        row = _structure(self.employee, "70000")
        self.assertEqual(list(SalaryStructure.objects.values_list("gross_monthly", flat=True)), [Decimal("70000.00")])
        self.assertEqual(SalaryStructure.objects.only("gross_monthly").get(pk=row.pk).gross_monthly, Decimal("70000.00"))
        SalaryStructure.objects.filter(pk=row.pk).update(gross_monthly=Decimal("71000"))
        row.refresh_from_db()
        self.assertEqual(row.gross_monthly, Decimal("71000.00"))
        self.assertEqual(SalaryStructure.objects.filter(gross_monthly__isnull=False).count(), 1)

    def test_none_stays_null(self):
        period = PayrollPeriod.for_month(2026, 7)
        run = PayrollRun.objects.create(period=period, status=PayrollRun.Status.INPUTS_OPEN)
        variance = PayrollVariance.objects.create(run=run, employee=self.employee, prev_net=None, curr_net=Decimal("10"))
        variance.refresh_from_db()
        self.assertIsNone(variance.prev_net)
        self.assertIsNone(_raw(PayrollVariance, "prev_net", variance.pk))

    def test_model_form_field_is_a_whole_taka_decimal_input(self):
        from django import forms

        from core.forms import TakaField
        formfield = SalaryStructure._meta.get_field("gross_monthly").formfield()
        self.assertIsInstance(formfield, forms.DecimalField)
        self.assertIsInstance(formfield, TakaField)  # whole taka in every form/admin input
        self.assertEqual(formfield.max_digits, 12)
        self.assertEqual(formfield.decimal_places, 0)
        self.assertIsInstance(EmployeeBankAccount._meta.get_field("account_number").formfield(), forms.CharField)


class StrHasNoAmountTests(TestCase):
    """§5: str(obj) feeds django_admin_log.object_repr and admin markup."""

    def test_money_models_str_carry_no_amount(self):
        employee = _user("alice@example.com", "001")
        structure = _structure(employee, "123456.78")
        account = EmployeeBankAccount.objects.create(
            employee=employee, account_type=EmployeeBankAccount.AccountType.MFS_BKASH,
            account_name="Alice", account_number="01711223344",
        )
        period = PayrollPeriod.for_month(2026, 7)
        run = PayrollRun.objects.create(period=period, status=PayrollRun.Status.INPUTS_OPEN)
        variance = PayrollVariance(run=run, employee=employee, prev_net=Decimal("123456"), curr_net=Decimal("234567"), delta=Decimal("111111"))
        for obj in (structure, account, variance):
            # Dates and years are fine; amounts are not.
            text = re.sub(r"\d{4}-\d{2}-\d{2}|\b(19|20)\d{2}\b", "", str(obj)).replace("3344", "")
            self.assertIsNone(re.search(r"\d{4,}", text), str(obj))
        self.assertIn("····3344", str(account))


class NoSqlOnCiphertextTests(TestCase):
    def setUp(self):
        self.employee = _user("alice@example.com", "001")
        _structure(self.employee, "50000")

    def test_filter_lookups_other_than_isnull_are_refused(self):
        with self.assertRaisesRegex(TypeError, "cannot be filtered with 'gt'"):
            SalaryStructure.objects.filter(gross_monthly__gt=1)
        with self.assertRaisesRegex(TypeError, "cannot be filtered with 'exact'"):
            SalaryStructure.objects.filter(gross_monthly=Decimal("50000"))
        with self.assertRaises(TypeError):
            list(SalaryStructure.objects.exclude(gross_monthly__in=[1]))

    def test_order_by_is_refused(self):
        with self.assertRaisesRegex(TypeError, "cannot be ordered or used in an expression"):
            list(SalaryStructure.objects.order_by("-gross_monthly"))

    def test_sum_fails_loudly(self):
        with self.assertRaises(TypeError):
            SalaryStructure.objects.aggregate(total=Sum("gross_monthly"))

    def test_wrong_key_fails_loudly(self):
        with override_settings(PAYROLL_FIELD_KEYS=[Fernet.generate_key().decode()]):
            with self.assertRaises(crypto.DecryptionError):
                SalaryStructure.objects.get(employee=self.employee)


class KeyRotationTests(TestCase):
    def setUp(self):
        self.employee = _user("alice@example.com", "001")
        self.row = _structure(self.employee, "65000")
        self.new_key = Fernet.generate_key().decode()

    def test_value_written_under_an_older_key_is_readable_with_both_listed(self):
        with override_settings(PAYROLL_FIELD_KEYS=[self.new_key, TEST_KEY]):
            self.assertEqual(SalaryStructure.objects.get(pk=self.row.pk).gross_monthly, Decimal("65000.00"))

    def test_rotate_payroll_keys_rewrites_every_row_under_the_newest_key(self):
        with override_settings(PAYROLL_FIELD_KEYS=[self.new_key, TEST_KEY]):
            out = StringIO()
            call_command("rotate_payroll_keys", stdout=out)
            self.assertIn("payroll.SalaryStructure: 1 row(s) re-encrypted", out.getvalue())
            self.assertNotIn("65000", out.getvalue())
        # Old key gone: still readable, so the rewrite really happened.
        with override_settings(PAYROLL_FIELD_KEYS=[self.new_key]):
            self.assertEqual(SalaryStructure.objects.get(pk=self.row.pk).gross_monthly, Decimal("65000.00"))
        # And the original key alone can no longer open it.
        with self.assertRaises(crypto.DecryptionError):
            SalaryStructure.objects.get(pk=self.row.pk)

    def test_generate_payroll_key_prints_a_usable_key(self):
        out = StringIO()
        call_command("generate_payroll_key", stdout=out)
        Fernet(out.getvalue().strip().encode())


class GuardedPurgeTests(TestCase):
    def setUp(self):
        self.employee = _user("alice@example.com", "001")
        _structure(self.employee, "50000")
        self.purge = guarded_purge("payroll.SalaryStructure")

    def test_refuses_on_a_non_empty_table_without_the_flag(self):
        with patch.dict(os.environ, {PURGE_FLAG: ""}):
            with self.assertRaisesRegex(RuntimeError, "payroll_salarystructure \\(1 rows\\)"):
                self.purge(apps, None)
        self.assertEqual(SalaryStructure.objects.count(), 1)

    def test_runs_with_the_flag(self):
        with patch.dict(os.environ, {PURGE_FLAG: "1"}), patch("builtins.print"):
            self.purge(apps, None)
        self.assertEqual(SalaryStructure.objects.count(), 0)

    def test_empty_tables_need_no_flag(self):
        SalaryStructure.objects.all().delete()
        with patch.dict(os.environ, {PURGE_FLAG: ""}):
            self.purge(apps, None)


class RegistryGuardTests(TestCase):
    """Plan §8 risk 2: a DecimalField added to a payroll model later lands
    plaintext by default. Every plaintext decimal must be on this list —
    which is exactly the rate-card / quantity exception of §4.3."""

    PLAINTEXT_DECIMALS = {
        "PayRate.min_gross", "PayRate.max_gross", "PayRate.amount",
        "MealDeductionTier.min_gross", "MealDeductionTier.max_gross", "MealDeductionTier.amount_per_meal",
        "TaxYear.exemption_divisor", "TaxYear.exemption_cap", "TaxYear.minimum_tax",
        "TaxYear.rebate_income_pct", "TaxYear.rebate_investment_pct", "TaxYear.rebate_cap",
        "TaxYear.allowable_investment_pct",
        "TaxExemptionThreshold.threshold_amount", "TaxSlab.width", "TaxSlab.rate",
        # the company's treasury deposit — not any one person's pay
        "TaxChallan.total_amount",
        # attendance arithmetic, not money
        "PayrollRunEmployee.eligible_days", "PayrollRunEmployee.lwp_days",
        "PayrollRunEmployee.payable_days", "PayrollRunEmployee.proration_factor",
        "PayrollRunLine.quantity",
    }

    def test_every_payroll_decimal_is_encrypted_or_explicitly_allowed(self):
        plaintext = {
            f"{model.__name__}.{field.name}"
            for model in apps.get_app_config("payroll").get_models()
            for field in model._meta.concrete_fields
            if isinstance(field, models.DecimalField) and not getattr(field, "encrypted_at_rest", False)
        }
        self.assertEqual(plaintext, self.PLAINTEXT_DECIMALS)

    def test_no_sql_ordering_or_aggregate_on_an_encrypted_column(self):
        """order_by()/Avg()/F() on ciphertext can't all be caught at runtime
        (Avg's output field isn't ours), so the source is scanned."""
        names = sorted({f.name for model in apps.get_models() for f in encrypted_fields(model)})
        alternation = "|".join(names)
        pattern = re.compile(
            rf"""order_by\([^)]*["']-?({alternation})["']|\b(Sum|Avg|Max|Min|F)\(["']({alternation})["']"""
        )
        root = pathlib.Path(settings.BASE_DIR)
        hits = []
        for app in ("payroll", "documents", "accounts", "employees", "core"):
            for path in (root / app).rglob("*.py"):
                if "migrations" in path.parts or path.name.startswith("tests"):
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        self.assertEqual(hits, [])


class RegisterViewTests(TestCase):
    """§4.4: filter/sort/sum on the register moved to Python; has_pay
    replaces gross_earnings > 0."""

    def setUp(self):
        self.admin = _user("padmin@example.com", "900")
        _group(self.admin, "Payroll Admins")
        for view_name in ("payroll_run_detail",):
            pv, _ = ProtectedView.objects.get_or_create(view_name=view_name, defaults={"registered_app": "payroll"})
            pv.allowed_groups.set([Group.objects.get(name="Payroll Admins")])
        self.period = PayrollPeriod.for_month(2026, 7)
        self.run = PayrollRun.objects.create(period=self.period, status=PayrollRun.Status.INPUTS_LOCKED)
        rows = [
            ("a@example.com", "0003", "300", "250", True),
            ("b@example.com", "0001", "900", "800", True),
            ("c@example.com", "0002", "500", "400", True),
            ("z@example.com", "0004", "0", "0", False),
        ]
        for email, code, gross, net, has_pay in rows:
            PayrollRunEmployee.objects.create(
                run=self.run, employee=_user(email, code), employee_code=code, full_name=email,
                gross_earnings=Decimal(gross), net_pay=Decimal(net), total_deductions=Decimal("10"),
                tax_deducted=Decimal("5"), has_pay=has_pay,
            )
        PayrollRun.objects.filter(pk=self.run.pk).update(status=PayrollRun.Status.CALCULATED)
        self.client.force_login(self.admin)

    def test_calculated_tab_sorts_by_net_pay_and_skips_unpaid_rows(self):
        resp = self.client.get(reverse("payroll_run_detail", args=[self.run.pk]) + "?tab=calculated")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r.employee_code for r in resp.context["rows"]], ["0001", "0002", "0003"])
        self.assertEqual(resp.context["calculated_total"], 3)
        self.assertEqual(resp.context["filtered_gross_total"], Decimal("1700"))
        self.assertEqual(resp.context["filtered_net_total"], Decimal("1450"))
        self.assertEqual(resp.context["filtered_deductions_total"], Decimal("30"))
        self.assertEqual(resp.context["filtered_tax_total"], Decimal("15"))
        self.assertIn("3 calculated", resp.context["page_subtitle"])

    def test_register_rows_are_tokens_in_the_database(self):
        row = PayrollRunEmployee.objects.get(employee_code="0001")
        for column in ("gross_earnings", "net_pay", "total_deductions", "tax_deducted"):
            stored = _raw(PayrollRunEmployee, column, row.pk)
            self.assertTrue(stored.startswith("gAAAA"), column)
        self.assertNotIn("900", _raw(PayrollRunEmployee, "gross_earnings", row.pk))


def _money_fields(list_display):
    """Field names a changelist renders, unwrapping money_display() callables."""
    return [getattr(entry, "money_field", entry) for entry in list_display]


class AdminMoneyVisibilityTests(TestCase):
    """§4.6: staff outside Payroll Admins see the row, not the amount."""

    def setUp(self):
        self.employee = _user("alice@example.com", "001")
        self.row = _structure(self.employee, "123456.78")
        perms = Permission.objects.filter(content_type__app_label="payroll", codename__endswith="salarystructure")
        self.ops = _user("ops@example.com", "800", is_staff=True)
        self.ops.user_permissions.set(perms)
        self.payroll_admin = _user("padmin@example.com", "900", is_staff=True)
        self.payroll_admin.user_permissions.set(perms)
        _group(self.payroll_admin, "Payroll Admins")
        self.changelist = reverse("admin:payroll_salarystructure_changelist")
        self.change = reverse("admin:payroll_salarystructure_change", args=[self.row.pk])
        self.add = reverse("admin:payroll_salarystructure_add")

    def test_staff_outside_payroll_admins_never_see_the_amount(self):
        self.client.force_login(self.ops)
        resp = self.client.get(self.changelist)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "123456")
        self.assertNotContains(resp, "123,457")
        self.assertNotIn("gross_monthly", _money_fields(resp.context["cl"].list_display))
        resp = self.client.get(self.change)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "123456")
        self.assertNotContains(resp, 'name="gross_monthly"')
        # A required amount they can't type → no add form either.
        self.assertEqual(self.client.get(self.add).status_code, 403)

    def test_payroll_admin_sees_it(self):
        self.client.force_login(self.payroll_admin)
        resp = self.client.get(self.changelist)
        # Rendered accounting-style via core.admin_mixins.money_display
        # (docs/MONEY_FORMAT_PLAN.md), so the changelist shows "123,457" (123456.78 rounded).
        self.assertContains(resp, "123,457")
        self.assertIn("gross_monthly", _money_fields(resp.context["cl"].list_display))
        resp = self.client.get(self.change)
        self.assertContains(resp, 'name="gross_monthly"')
        self.assertEqual(self.client.get(self.add).status_code, 200)

    def test_ciphertext_columns_are_never_searchable_or_sortable(self):
        from django.contrib import admin as django_admin
        from payroll.admin import EmployeeBankAccountAdmin, SalaryStructureAdmin
        from django.test import RequestFactory
        request = RequestFactory().get("/")
        request.user = self.payroll_admin
        model_admin = SalaryStructureAdmin(SalaryStructure, django_admin.site)
        self.assertNotIn("gross_monthly", model_admin.get_sortable_by(request))
        bank_admin = EmployeeBankAccountAdmin(EmployeeBankAccount, django_admin.site)
        self.assertNotIn("account_number", bank_admin.get_search_fields(request))


class MoneyVisibilityMatrixTests(TestCase):
    """§3: Service Book + bulk onboarding are HR-only; gross salary screens are
    Payroll Admins only; the Users menu opens to HR."""

    def setUp(self):
        department = Department.objects.create(name="Ops")
        self.employee = _user("alice@example.com", "001", department=department, joining_date=date(2025, 1, 1))
        JobInfo.objects.create(employee=self.employee)
        self.user_admin = _user("uadmin@example.com", "700")
        _group(self.user_admin, "User Admins")
        self.hr = _user("hr@example.com", "701")
        _group(self.hr, "HR")
        self.finance = _user("fin@example.com", "702")
        _group(self.finance, "Finance")
        self.payroll_admin = _user("padmin@example.com", "703")
        _group(self.payroll_admin, "Payroll Admins")

    def test_service_book_is_hr_only(self):
        url = reverse("employee_service_book", args=[self.employee.pk])
        self.client.force_login(self.user_admin)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.hr)
        self.assertEqual(self.client.get(url).status_code, 200)
        # The employee's own self-view was removed 2026-10-05
        # (docs/LEGACY_60_IMPORT_PLAN.md decision 4): Part 4 now also carries
        # HR's pre-Orbis leave record, and HR wants the whole book HR-only.
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_bulk_onboarding_is_hr_only(self):
        url = reverse("user_bulk_create")
        self.client.force_login(self.user_admin)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("user_bulk_create_template")).status_code, 403)
        self.client.force_login(self.hr)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(reverse("user_bulk_create_template")).status_code, 200)

    def test_seeded_acls_put_gross_salary_behind_payroll_admins_and_open_users_to_hr(self):
        from accounts.management.commands.seed_orbis import MENU_GROUPS, ORBIS_MAIN_MENUS
        self.assertEqual(MENU_GROUPS["payroll_salary"], ["Payroll Admins"])
        self.assertEqual(MENU_GROUPS["admins"], ["User Admins", "HR"])
        users_menu = next(row for row in ORBIS_MAIN_MENUS if row[0] == "Users")
        self.assertIn("HR", users_menu[4])

    def test_gross_salary_list_follows_the_seeded_acl(self):
        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_salary_list", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([Group.objects.get(name="Payroll Admins")])
        url = reverse("payroll_salary_list")
        self.client.force_login(self.finance)
        self.assertRedirects(self.client.get(url), "/")
        self.client.force_login(self.payroll_admin)
        self.assertEqual(self.client.get(url).status_code, 200)
