"""``taka`` template filter — registered as a template builtin in
core/settings.py, so it works in every template and in the DB-stored PDF
designs without ``{% load money %}``. See core/money.py."""

from django import template

from core.money import format_taka

register = template.Library()


@register.filter(name="taka")
def taka(value, decimals=0):
    """``{{ amount|taka }}`` -> ``450,000``; ``{{ amount|taka:2 }}`` keeps paise."""
    try:
        decimals = int(decimals)
    except (TypeError, ValueError):
        decimals = 0
    return format_taka(value, decimals=decimals)
