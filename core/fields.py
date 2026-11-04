"""Model fields that encrypt at rest — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.2.

Each field keeps its parent's Python-side behaviour (validation,
``formfield()``, admin display) but stores a Fernet token in a ``TEXT``
column, so a ``SELECT`` from any database client shows ``gAAAAA…``, never a
number.

Because the ciphertext is randomised, SQL can't compare, sort or sum these
columns. The guard is deliberate and loud:

- any lookup other than ``isnull`` raises ``TypeError`` at query-build time;
- ``order_by()`` on the column, or an ``F()`` reference to it inside an
  expression, raises ``TypeError`` when the ordering is resolved;
- an aggregate such as ``Sum()`` fails on the way back out, because the
  database returns a number where a token was expected.

Load the rows and do the arithmetic in Python (``payroll/views.py``'s
register is the reference). ``values()``/``values_list()``/``only()`` keep
working — they read the column and decrypt like any other select.
"""

from __future__ import annotations

import json
from decimal import Decimal

from django.db import models
from django.db.backends.utils import format_number
from django.db.models.expressions import Col
from django.db.models.lookups import IsNull
from django.utils.functional import cached_property

from core import crypto

class _EncryptedCol(Col):
    """The column reference ``get_col()`` hands to the ORM.

    ``resolve_expression`` runs when the column is wrapped in ``OrderBy`` or
    any other expression — never for a plain SELECT or ``values()`` — so it
    is the one place an ordering on ciphertext can be refused before SQL
    runs.
    """

    def resolve_expression(self, *args, **kwargs):
        raise TypeError(self.target.sql_refusal("ordered or used in an expression"))


class _IsNullOnPlainCol(IsNull):
    """``isnull`` — the one lookup the database can answer about a token.

    ``Lookup.process_lhs`` resolves its column at compile time, which would
    trip ``_EncryptedCol``'s refusal, so this lookup swaps in a plain ``Col``.
    """

    def __init__(self, lhs, rhs):
        if isinstance(lhs, _EncryptedCol):
            lhs = Col(lhs.alias, lhs.target, lhs.output_field)
        super().__init__(lhs, rhs)


class EncryptedFieldMixin:
    """Shared plumbing — subclasses provide ``_to_text`` / ``_from_text``."""

    encrypted_at_rest = True

    def sql_refusal(self, what: str) -> str:
        return (
            f"{self.model.__name__}.{self.name} is encrypted at rest and cannot be {what} "
            "in SQL — load the rows and do it in Python."
        )

    # --- storage ---------------------------------------------------------

    def get_internal_type(self):
        # Drives db_type() — a TEXT column on every backend.
        return "TextField"

    def get_db_prep_value(self, value, connection, prepared=False):
        if not prepared:
            value = self.get_prep_value(value)
        if value is None:
            return None
        return crypto.encrypt(self._to_text(value))

    def get_db_prep_save(self, value, connection):
        if hasattr(value, "as_sql"):
            return value
        return self.get_db_prep_value(value, connection=connection, prepared=False)

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(
                f"{self.sql_refusal('aggregated')} (the database returned {value!r} "
                "instead of a token)"
            )
        return self._from_text(crypto.decrypt(value))

    # --- no SQL on ciphertext --------------------------------------------

    def get_lookup(self, lookup_name):
        if lookup_name == "isnull":
            return _IsNullOnPlainCol
        raise TypeError(self.sql_refusal(f"filtered with '{lookup_name}'"))

    def get_transform(self, lookup_name):
        raise TypeError(self.sql_refusal(f"transformed with '{lookup_name}'"))

    def get_col(self, alias, output_field=None):
        if alias == self.model._meta.db_table and (output_field is None or output_field == self):
            return self.cached_col
        return _EncryptedCol(alias, self, output_field)

    @cached_property
    def cached_col(self):
        return _EncryptedCol(self.model._meta.db_table, self)


class EncryptedDecimalField(EncryptedFieldMixin, models.DecimalField):
    """A ``DecimalField`` whose column holds a token.

    ``max_digits``/``decimal_places`` still drive validation and the form
    field; the stored text is the same quantised string a plain
    ``DecimalField`` would have sent to a ``DECIMAL`` column.
    """

    description = "Decimal (encrypted at rest)"

    def formfield(self, **kwargs):
        # Every encrypted decimal is per-employee money, and money is typed
        # and shown in whole taka (core.forms.TakaField) — the column keeps
        # its two places, the input simply never shows or asks for them.
        from core.forms import TakaField

        return super().formfield(**{"form_class": TakaField, **kwargs})

    def _to_text(self, value) -> str:
        return format_number(self.to_python(value), self.max_digits, self.decimal_places)

    def _from_text(self, text: str) -> Decimal:
        return Decimal(text)


class EncryptedCharField(EncryptedFieldMixin, models.CharField):
    """A ``CharField`` (``max_length`` validated in Python) stored as a token."""

    description = "String (encrypted at rest)"

    def _to_text(self, value) -> str:
        return str(value)

    def _from_text(self, text: str) -> str:
        return text


class EncryptedJSONField(EncryptedFieldMixin, models.JSONField):
    """A ``JSONField`` serialised with ``json.dumps`` and stored as a token.

    Key transforms (``field__key``) are refused along with every other
    lookup — the whole document is opaque to the database.
    """

    description = "JSON (encrypted at rest)"

    def _to_text(self, value) -> str:
        return json.dumps(value, cls=self.encoder)

    def _from_text(self, text: str):
        return json.loads(text, cls=self.decoder)


def encrypted_fields(model) -> list:
    """Every encrypted field on ``model`` — used by ``rotate_payroll_keys``
    and the tests that walk the registry."""
    return [f for f in model._meta.concrete_fields if getattr(f, "encrypted_at_rest", False)]
