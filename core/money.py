"""One money format for the whole app (docs/MONEY_FORMAT_PLAN.md).

Finance's decision (2026-10-05): **whole taka everywhere**, standard thousands
grouping, half-up rounding — including computed payroll figures, accepting a
one-taka drift between a column and its printed total. Stored values are
never rounded; this is display only.

- Templates: the ``taka`` filter (a template builtin, so DB-stored PDF
  designs can use it without ``{% load %}``) — ``{{ x.net_pay|taka }}``.
- Excel: ``core.exports.money_cell`` / ``set_money_columns`` keep cells
  numeric with a ``#,##0`` number format so Finance can still sum them.
- Python: ``format_taka`` for anything else (letters, Service Book context).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

EMPTY = "—"
EXCEL_NUMBER_FORMAT = "#,##0"
EXCEL_NUMBER_FORMAT_2DP = "#,##0.00"


def to_whole_taka(amount) -> int | None:
    """Round to the nearest whole taka (half-up). None/blank -> None."""
    if amount is None or amount == "":
        return None
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_taka(amount, decimals: int = 0) -> str:
    """``450000.00`` -> ``450,000``; ``-1234.5`` -> ``-1,235``; None -> ``—``.

    ``decimals=2`` keeps paise (``1,234,567.50``) for the rare caller that
    must show them (nothing in the UI does, by Finance's decision).
    """
    if amount is None or amount == "":
        return EMPTY
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return str(amount)
    if decimals == 0:
        return f"{to_whole_taka(value):,}"
    quantum = Decimal(1).scaleb(-decimals)
    return f"{value.quantize(quantum, rounding=ROUND_HALF_UP):,.{decimals}f}"
