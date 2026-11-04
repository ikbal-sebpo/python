"""core.money + the ``taka`` filter + the template guard (docs/MONEY_FORMAT_PLAN.md)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase

from core.money import EXCEL_NUMBER_FORMAT, format_taka, to_whole_taka

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Attribute names that are money wherever they appear in a template.
MONEY_ATTRS = {
    "gross_monthly", "gross_earnings", "net_pay", "tax_deducted", "total_deductions",
    "salary_amount", "night_amount", "ot_amount", "transport_amount", "meal_amount",
    "package_amount", "current_gross", "calculated_tax", "source_tax",
    "allowable_investment", "actual_investment", "declared_investment", "ait_car_advance_tax",
    "filtered_gross_total", "filtered_deductions_total",
    "filtered_tax_total", "filtered_net_total", "gross_display",
    "principal", "installment_amount", "opening_recovered",
}
# ``amount``/``delta`` are money only inside the payroll templates; so are the
# loan-balance words (``outstanding`` is a count in policies/).
PAYROLL_ONLY_ATTRS = {"amount", "delta", "outstanding", "recovered", "next_instalment"}

_EXPR = re.compile(r"\{\{\s*([^}|]+?)\s*((?:\|[^}]*)?)\}\}")


class FormatTakaTests(SimpleTestCase):
    def test_whole_taka_grouping_and_rounding(self):
        self.assertEqual(format_taka(Decimal("450000.00")), "450,000")
        self.assertEqual(format_taka(Decimal("1234567.50")), "1,234,568")
        self.assertEqual(format_taka(Decimal("1234567.49")), "1,234,567")
        self.assertEqual(format_taka(Decimal("0.5")), "1")
        self.assertEqual(format_taka(0), "0")
        self.assertEqual(format_taka(-1234.5), "-1,235")
        self.assertEqual(format_taka("95000"), "95,000")

    def test_blank_and_garbage(self):
        self.assertEqual(format_taka(None), "—")
        self.assertEqual(format_taka(""), "—")
        self.assertEqual(format_taka("n/a"), "n/a")
        self.assertIsNone(to_whole_taka(None))
        self.assertEqual(to_whole_taka("12.5"), 13)

    def test_two_places_when_asked(self):
        self.assertEqual(format_taka(Decimal("1234567.5"), decimals=2), "1,234,567.50")

    def test_filter_is_a_builtin(self):
        out = Template("{{ a|taka }}|{{ b|taka:2 }}|{{ c|taka }}").render(
            Context({"a": Decimal("27419.35"), "b": Decimal("27419.35"), "c": None})
        )
        self.assertEqual(out, "27,419|27,419.35|—")

    def test_excel_number_format_is_accounting(self):
        self.assertEqual(EXCEL_NUMBER_FORMAT, "#,##0")


class TakaFieldTests(SimpleTestCase):
    """core.forms.TakaField — whole taka in, whole taka shown (2026-10-07)."""

    def _form(self, **kwargs):
        from django import forms

        from core.forms import TakaField

        class F(forms.Form):
            amount = TakaField(max_digits=12, required=False)

        return F(**kwargs)

    def test_initial_renders_without_paise_and_with_integer_step(self):
        html = self._form(initial={"amount": Decimal("306786.00")}).as_p()
        self.assertIn('value="306786"', html)
        self.assertNotIn("306786.00", html)
        self.assertIn('step="1"', html)

    def test_typed_decimals_round_half_up_instead_of_failing(self):
        form = self._form(data={"amount": "12345.67"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["amount"], Decimal("12346"))
        form = self._form(data={"amount": "12345.50"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["amount"], Decimal("12346"))

    def test_blank_stays_none_and_garbage_still_errors(self):
        blank = self._form(data={"amount": ""})
        self.assertTrue(blank.is_valid(), blank.errors)
        self.assertIsNone(blank.cleaned_data["amount"])
        self.assertFalse(self._form(data={"amount": "abc"}).is_valid())

    def test_bound_model_value_shows_whole_taka(self):
        from payroll.forms import TaxDeclarationReviewForm
        from payroll.models import EmployeeTaxDeclaration
        declaration = EmployeeTaxDeclaration(
            allowable_investment=Decimal("306786.00"), actual_investment=Decimal("306786.00"),
            ait_car_advance_tax=Decimal("0.00"),
        )
        html = TaxDeclarationReviewForm(instance=declaration).as_p()
        self.assertIn('value="306786"', html)
        self.assertNotIn("306786.00", html)
        self.assertNotIn('value="0.00"', html)


class TemplateMoneyGuardTests(SimpleTestCase):
    """Fail on any money attribute rendered raw or via floatformat, so the
    convention can't drift back (same posture as tests_encryption's source scan)."""

    def test_every_money_expression_uses_the_taka_filter(self):
        offenders = []
        for path in TEMPLATES_DIR.rglob("*.html"):
            rel = path.relative_to(TEMPLATES_DIR).as_posix()
            payroll = rel.startswith("payroll/")
            for m in _EXPR.finditer(path.read_text(encoding="utf-8")):
                expr, filters = m.group(1).strip(), m.group(2)
                attr = expr.split(".")[-1].strip()
                if attr in MONEY_ATTRS or (payroll and attr in PAYROLL_ONLY_ATTRS):
                    if "|taka" not in filters or "floatformat" in filters:
                        offenders.append(f"{rel}: {m.group(0)}")
        self.assertEqual(offenders, [], "Money rendered without |taka:\n" + "\n".join(offenders))
