"""Django-admin money visibility — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.6.

The admin is reachable by any ``is_staff`` user with model permissions, which
is wider than the people allowed to see pay. ``PayrollMoneyAdminMixin``
keeps the rows usable for everyone (fixing a status, re-linking a batch)
while the amounts themselves show only for a superuser or a Payroll Admin —
the same ``can_see_payroll_money`` rule the register uses.
"""

from __future__ import annotations

from accounts.permissions import can_see_payroll_money


def money_display(field_name: str, *, short_description: str | None = None):
    """``list_display`` entry that renders a money column accounting-style
    (``450,000``) instead of the raw Decimal. Encrypted columns can't be
    ordered in SQL, so no ``ordering`` is attached.

        list_display = ("employee", money_display("gross_monthly"))
    """
    from django.contrib import admin

    from core.money import format_taka

    @admin.display(description=short_description or field_name.replace("_", " ").capitalize())
    def _display(obj):
        return format_taka(getattr(obj, field_name))

    _display.__name__ = f"{field_name}_display"
    _display.money_field = field_name
    return _display


class PayrollMoneyAdminMixin:
    """Drop ``MONEY_FIELDS`` from every admin surface unless the viewer may
    see payroll money.

    Works on ``ModelAdmin`` and ``InlineModelAdmin`` alike. Two of the
    overrides apply to *everyone*, because the columns are encrypted at
    rest and the database can't evaluate them: ``search_fields`` (an
    ``icontains`` on ciphertext is refused by the field) and
    ``sortable_by`` (ordering on ciphertext is refused too).
    """

    MONEY_FIELDS: tuple[str, ...] = ()

    def can_see_money(self, request) -> bool:
        return can_see_payroll_money(request.user, request=request)

    def _without_money(self, request, names):
        if names is None or self.can_see_money(request):
            return names
        # A money_display() callable carries the field it renders, so it is
        # stripped exactly like the bare field name would be.
        return tuple(
            name for name in names
            if getattr(name, "money_field", name) not in self.MONEY_FIELDS
        )

    def _strip_fieldset_fields(self, fields):
        kept = []
        for item in fields:
            if isinstance(item, (list, tuple)):
                group = tuple(name for name in item if name not in self.MONEY_FIELDS)
                if group:
                    kept.append(group)
            elif item not in self.MONEY_FIELDS:
                kept.append(item)
        return tuple(kept)

    # --- changelist ------------------------------------------------------

    def get_list_display(self, request):
        return self._without_money(request, super().get_list_display(request))

    def get_search_fields(self, request):
        return tuple(
            name for name in super().get_search_fields(request)
            if name.lstrip("^=@").split("__")[0] not in self.MONEY_FIELDS
        )

    def get_sortable_by(self, request):
        return tuple(name for name in super().get_sortable_by(request) if name not in self.MONEY_FIELDS)

    # --- change form -----------------------------------------------------

    def get_exclude(self, request, obj=None):
        excluded = super().get_exclude(request, obj)
        if self.can_see_money(request):
            return excluded
        return tuple(excluded or ()) + tuple(self.MONEY_FIELDS)

    def get_fields(self, request, obj=None):
        return self._without_money(request, super().get_fields(request, obj))

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if self.can_see_money(request):
            return fieldsets
        return [
            (name, {**options, "fields": self._strip_fieldset_fields(options.get("fields", ()))})
            for name, options in fieldsets
        ]

    def get_readonly_fields(self, request, obj=None):
        return self._without_money(request, super().get_readonly_fields(request, obj))

    def has_add_permission(self, request, *args, **kwargs):
        """A row whose required fields include an amount can't be added by
        someone who isn't allowed to type one."""
        allowed = super().has_add_permission(request, *args, **kwargs)
        if not allowed or self.can_see_money(request):
            return allowed
        return not self._money_is_required()

    def _money_is_required(self) -> bool:
        for field in self.model._meta.concrete_fields:
            if (
                field.name in self.MONEY_FIELDS and field.editable
                and not field.blank and not field.null and not field.has_default()
            ):
                return True
        return False
