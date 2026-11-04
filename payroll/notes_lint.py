"""Reconcile free-text payroll notes against the structured inputs that
should have replaced them.

Phases B–D moved Data Solutions' three recurring pay events out of the
Supervisor Note column and into real models. This is what stops the column
quietly filling up again: at calculation time, any note stating a **money
amount** that no structured row accounts for raises a WARNING control check.

**It detects the money, not the phrase.** An earlier draft proposed matching
known sentences ("Recommended for the variable part", "Go live date"), which
would rot the moment the uploaders shipped — supervisors stop writing those
sentences once they have a form. A taka figure keeps working, and it catches
categories nobody has thought of yet.

The deliberate limitation: **a note that implies money without stating a
figure is not caught.** September 2026's "13 days Test Salary: From August 26
to September 07" names no amount, so nothing here flags it. That is the
honest trade for not maintaining a phrase list, and it is much less of a gap
now that Test Salary has its own uploader — a window filed as a note instead
simply never appears on the variable-pay queue.

Dates are the obvious false-positive risk ("09/09/2026", "30 August 2026"),
which is why a bare number never matches: an amount only counts when it sits
next to a currency token.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# "3000 Taka", "TK. 18,000", "৳400", "BDT 250", "250tk" — a number adjacent to
# a currency token, in either order. Never a bare number.
_CURRENCY = r"(?:tk|taka|tka|bdt|৳)"
# Grouped, with the comma form FIRST and requiring at least one comma group.
# An earlier unparenthesised alternation let `\d{1,3}` win outright on a
# comma-less figure with no trailing context to force backtracking, so
# "Tk 11000" read as 110 — truncating exactly the go-live amounts this is
# meant to reconcile.
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
MONEY_RE = re.compile(
    rf"(?:{_CURRENCY}\s*\.?\s*(?P<before>{_NUMBER}))|(?:(?P<after>{_NUMBER})\s*{_CURRENCY}\b)",
    re.IGNORECASE,
)

# Below this, a "figure" is almost certainly a day count or a headcount that
# happens to sit next to the word Taka, not a payment worth reconciling.
MIN_INTERESTING_AMOUNT = Decimal("100")


def extract_amounts(text: str) -> list[Decimal]:
    """Every currency-marked amount in `text`, de-duplicated, in order."""
    if not text:
        return []
    found: list[Decimal] = []
    for match in MONEY_RE.finditer(text):
        raw = match.group("before") or match.group("after")
        try:
            amount = Decimal(raw.replace(",", ""))
        except InvalidOperation:  # pragma: no cover — regex shouldn't allow it
            continue
        if amount >= MIN_INTERESTING_AMOUNT and amount not in found:
            found.append(amount)
    return found


def lint_run_notes(run) -> list[dict]:
    """Amounts named in this run's notes that no structured input accounts for.

    Returns one dict per unaccounted amount:
    ``{employee, role, amount, note}``.

    An amount is accounted for when it equals either:

    * a ``VariablePayEntry.amount`` for that employee in this period that
      hasn't been rejected — SLA pay, a Test Salary deduction, a bonus; or
    * a ``SalaryStructure.gross_monthly`` in force at any point in the period
      — which is what a go-live note ("the new package for MARS (TK. 18,000)")
      is quoting.

    Matching on the **amount** rather than merely "does this person have any
    input at all" is what makes it useful: an employee with approved overtime
    and an unstructured 3,000 SLA note still gets flagged.

    Rejected variable pay deliberately does not count — a note describing a
    payment that was turned down is exactly the kind of thing worth surfacing.
    """
    from django.db.models import Q

    from payroll.models import PayrollRunEmployeeNote, SalaryStructure, VariablePayEntry

    notes = list(
        PayrollRunEmployeeNote.objects
        .filter(run=run)
        .exclude(note="")
        .select_related("employee")
    )
    if not notes:
        return []

    period = run.period
    employee_ids = {note.employee_id for note in notes}

    accounted: dict = {}
    for employee_id, amount in (
        VariablePayEntry.objects
        .filter(employee_id__in=employee_ids, period=period)
        .exclude(status=VariablePayEntry.Status.REJECTED)
        .values_list("employee_id", "amount")
    ):
        accounted.setdefault(employee_id, set()).add(amount)
    for employee_id, gross in (
        SalaryStructure.objects
        .filter(
            employee_id__in=employee_ids,
            status__in=(SalaryStructure.Status.APPROVED, SalaryStructure.Status.SUPERSEDED),
            effective_from__lte=period.end_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period.start_date))
        .values_list("employee_id", "gross_monthly")
    ):
        accounted.setdefault(employee_id, set()).add(gross)

    findings: list[dict] = []
    for note in notes:
        known = accounted.get(note.employee_id, set())
        for amount in extract_amounts(note.note):
            if amount in known:
                continue
            findings.append({
                "employee": note.employee,
                "role": note.get_role_display(),
                "amount": amount,
                "note": note.note,
            })
    return findings
