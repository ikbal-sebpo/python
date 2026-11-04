"""The monthly payroll ledger — one shape for every payroll month of a tax
year (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §1, Revision 2).

A row is one employee's one payroll month: every pay component paid or
deducted (by component code — earnings and deductions alike), the TDS
deducted, the net paid, plus two **derived** columns the export carries but
no sheet types — the mobile & internet reimbursement (the policy tier of the
gross the month was paid at, ``payroll.tax.mobile_internet_reimburse``) and the challan(s) the
month's TDS was deposited under (``TaxChallanAllocation``). An **Orbis
month** is read from its CALCULATED regular run; a **pre-Orbis month**
(before ``TaxYear.orbis_live_from``) from the ``LegacyPayrollMonth`` rows
Finance imported once at cutover. Callers never need to know which — the
cutover import's template/parser, the "Export month" workbook, the "TDS by
challan" uploader's prefill and checks, and (later) the tax certificate all
go through ``monthly_ledger_rows``.

The column list is generated from ``PayComponent`` at call time, so a
component seeded later appears in the template, the parser and the export
without a code change. The certificate and the TDS true-up read only the
taxable earnings out of a row (``taxable_earning_codes``).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q
from openpyxl import Workbook

from core.exports import escape_spreadsheet, money_value, set_money_columns
from payroll.models import (
    LegacyPayrollMonth, PayComponent, PayrollPeriod, PayrollRun, PayrollRunEmployee, PayrollRunLine, SalaryStructure,
    TaxChallan, TaxChallanAllocation,
)
from payroll.tax import mobile_internet_reimburse

SOURCE_ORBIS = "ORBIS"
SOURCE_LEGACY = "LEGACY"

# Fixed (non-component) columns, in sheet order around the component block.
HEADER_EMPLOYEE_ID = "Employee ID"
HEADER_MONTH = "Payroll month (YYYY-MM)"
HEADER_MOBILE = "Mobile & Internet reimbursement"
HEADER_TDS = "TDS deducted"
HEADER_NET = "Net pay"
HEADER_ADJ_MONTHS = "Salary adjustment months"
HEADER_CHALLAN = "Challan No"
HEADER_NAME = "Employee Name"
HEADER_AMOUNT = "Amount"

MONTHS_SHEET = "Employee months"
CHALLAN_SHEET = "Challans"
ALLOCATION_SHEET = "TDS by challan"
# The challan uploader's own sheet (payroll/upload.py::parse_tax_challan_upload).
CHALLAN_HEADERS = ("Challan No", "Date", "Bank", "Total Challan Amount", "Payroll month (YYYY-MM, optional)", "Note")
# The per-employee allocation sheet (payroll/upload.py::parse_challan_allocation_upload).
ALLOCATION_HEADERS = (HEADER_EMPLOYEE_ID, HEADER_CHALLAN, HEADER_AMOUNT, HEADER_NAME)

# TDS is a PayComponent too, but the ledger keeps it as a fixed column
# (PayrollRunEmployee.tax_deducted is the figure, not a line sum).
_NON_COMPONENT_CODES = {"TDS"}

# Normalised header → fixed key. Aliases cover the ways Finance is likely to
# retype a header; a component is matched by its name, short name or code.
_FIXED_KEYS = {
    "employee id": "employee_id", "employee_id": "employee_id", "eid": "employee_id", "emp id": "employee_id",
    "payroll month": "month", "month": "month", "period": "month", "payroll period": "month",
    "tds deducted": "tds", "tds": "tds", "tax deducted": "tds", "tax deducted at source": "tds",
    "net pay": "net", "net": "net", "net take home": "net", "net salary": "net", "take home": "net",
    "salary adjustment months": "adj_months", "adjustment months": "adj_months", "months settled": "adj_months",
}
# Reference-only columns the months parser skips without complaint — the
# export's two derived columns among them, so an exported month imports
# back unchanged (Revision 2: neither is typed; both are computed).
_IGNORED_KEYS = {
    "employee name", "name", "note", "notes", "remarks", "remark", "department", "team", "designation",
    "mobile internet reimbursement", "mobile internet", "mobile internet bill", "mobile internet bill reimburse",
    "challan no", "challan number", "challan",
}
FIXED_KEYS = ("employee_id", "month", "tds", "net", "adj_months")

# The allocation sheet's own header aliases.
_ALLOCATION_KEYS = {
    "employee id": "employee_id", "employee_id": "employee_id", "eid": "employee_id", "emp id": "employee_id",
    "challan no": "challan", "challan number": "challan", "challan": "challan",
    "amount": "amount", "tds": "amount", "tds deducted": "amount", "tax": "amount", "tax deducted": "amount",
    "tds amount": "amount",
}


def header_key(text) -> str:
    """Normalise a header cell: lowercase, drop anything in parentheses,
    collapse punctuation/whitespace to single spaces, drop 'and'."""
    if text is None:
        return ""
    s = re.sub(r"\(.*?\)", " ", str(text)).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\band\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def ledger_components() -> list[PayComponent]:
    """The component block: every active earning and deduction in display
    order (earnings first), except TDS which is a fixed column."""
    comps = list(
        PayComponent.objects.filter(
            kind__in=(PayComponent.Kind.EARNING, PayComponent.Kind.DEDUCTION), is_active=True,
        ).exclude(code__in=_NON_COMPONENT_CODES).order_by("sequence", "code")
    )
    return [c for c in comps if c.kind == PayComponent.Kind.EARNING] + [
        c for c in comps if c.kind == PayComponent.Kind.DEDUCTION
    ]


def taxable_earning_codes() -> set[str]:
    """The codes the certificate / true-up read out of a row."""
    return set(
        PayComponent.objects.filter(kind=PayComponent.Kind.EARNING, is_taxable=True).values_list("code", flat=True)
    )


def ledger_headers(components: list[PayComponent] | None = None, *, template: bool = False) -> list[str]:
    """The export's columns; ``template=True`` is the cutover sheet Finance
    fills — the same minus the two derived columns (reimbursement, challan)."""
    components = ledger_components() if components is None else components
    head = [HEADER_EMPLOYEE_ID, HEADER_MONTH] + [c.name for c in components]
    if template:
        return head + [HEADER_TDS, HEADER_NET, HEADER_ADJ_MONTHS, HEADER_NAME]
    return head + [HEADER_MOBILE, HEADER_TDS, HEADER_NET, HEADER_ADJ_MONTHS, HEADER_CHALLAN, HEADER_NAME]


def resolve_ledger_headers(header_row, components: list[PayComponent]) -> tuple[dict[int, str], list[str]]:
    """Map each column index of an uploaded months header row to a key: a
    fixed key (``FIXED_KEYS``), a component code, or nothing for a reference
    column. Returns the map and the error messages (unknown header,
    duplicate column, missing required column)."""
    by_key: dict[str, str] = {}
    for c in components:
        for label in (c.name, c.short_name, c.code, c.code.replace("_", " ")):
            k = header_key(label)
            if k and k not in _FIXED_KEYS and k not in _IGNORED_KEYS:
                by_key.setdefault(k, c.code)
    mapping: dict[int, str] = {}
    errors: list[str] = []
    seen: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        k = header_key(raw)
        if not k or k in _IGNORED_KEYS:
            continue
        key = _FIXED_KEYS.get(k) or by_key.get(k)
        if key is None:
            errors.append(
                f"Unknown column '{raw}' — not a fixed column and not a pay component Orbis knows. "
                f"Seed the component first, or remove the column."
            )
            continue
        if key in seen:
            errors.append(f"Column '{raw}' duplicates column {seen[key] + 1}.")
            continue
        seen[key] = idx
        mapping[idx] = key
    for required, label in (("employee_id", HEADER_EMPLOYEE_ID), ("month", HEADER_MONTH), ("tds", HEADER_TDS)):
        if required not in seen:
            errors.append(f"Required column '{label}' is missing.")
    return mapping, errors


def resolve_allocation_headers(header_row) -> tuple[dict[int, str], list[str]]:
    """Same idea for the allocation sheet: Employee ID, Challan No and
    Amount are required; anything else is reference and skipped."""
    mapping: dict[int, str] = {}
    errors: list[str] = []
    seen: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        key = _ALLOCATION_KEYS.get(header_key(raw))
        if key is None:
            continue
        if key in seen:
            errors.append(f"Column '{raw}' duplicates column {seen[key] + 1}.")
            continue
        seen[key] = idx
        mapping[idx] = key
    for required, label in (("employee_id", HEADER_EMPLOYEE_ID), ("challan", HEADER_CHALLAN), ("amount", HEADER_AMOUNT)):
        if required not in seen:
            errors.append(f"Required column '{label}' is missing.")
    return mapping, errors


# ---------------------------------------------------------------------------
# Months of a tax year
# ---------------------------------------------------------------------------

def tax_year_months(tax_year) -> list[tuple[int, int]]:
    """Calendar (year, month) pairs from the tax year's first to last month —
    the payroll periods named for them (July = 16 Jun – 15 Jul)."""
    months: list[tuple[int, int]] = []
    y, m = tax_year.start_date.year, tax_year.start_date.month
    end = (tax_year.end_date.year, tax_year.end_date.month)
    while (y, m) <= end:
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def legacy_months(tax_year) -> list[tuple[int, int]]:
    """The tax year's months before ``orbis_live_from`` — the only months a
    cutover sheet may carry. Empty when live-from is blank (Orbis ran every
    month) or unset."""
    live = tax_year.orbis_live_from
    if live is None:
        return []
    return [(y, m) for (y, m) in tax_year_months(tax_year) if (y, m) < (live.year, live.month)]


def is_legacy_month(tax_year, year: int, month: int) -> bool:
    return (year, month) in set(legacy_months(tax_year))


def month_label(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def period_start(year: int, month: int) -> date:
    from payroll.periods import period_bounds
    return period_bounds(year, month)[0]


def period_end(year: int, month: int) -> date:
    from payroll.periods import period_bounds
    return period_bounds(year, month)[1]


def tax_year_for_date(d: date):
    from payroll.models import TaxYear
    return TaxYear.objects.filter(start_date__lte=d, end_date__gte=d).order_by("-start_date").first()


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

@dataclass
class LedgerRow:
    employee: object                     # CustomUser
    employee_id: str
    full_name: str
    year: int
    month: int
    lines: dict[str, Decimal] = field(default_factory=dict)
    mobile_internet_reimbursement: Decimal = Decimal("0")   # derived (policy tier), never typed
    tds_deducted: Decimal = Decimal("0")
    net_pay: Decimal | None = None
    adjustment_months: int | None = None                     # months the SALARY_ADJUSTMENT line settled
    challan_number: str = ""                                 # derived from TaxChallanAllocation; ", "-joined
    source: str = SOURCE_ORBIS

    def amount(self, code: str) -> Decimal:
        return self.lines.get(code, Decimal("0"))

    @property
    def month_label(self) -> str:
        return month_label(self.year, self.month)


def structure_gross_on(employee_ids, on_date: date) -> dict:
    """``{employee_pk: gross_monthly}`` from the APPROVED/SUPERSEDED salary
    structure in force on ``on_date`` — the latest ``effective_from`` that
    covers it. Encrypted column, so the pick is done in Python."""
    best: dict = {}
    qs = (
        SalaryStructure.objects.filter(
            employee_id__in=list(employee_ids),
            status__in=(SalaryStructure.Status.APPROVED, SalaryStructure.Status.SUPERSEDED),
            effective_from__lte=on_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .order_by("effective_from", "revision_no")
    )
    for s in qs:
        best[s.employee_id] = s.gross_monthly      # later rows override: latest effective_from wins
    return best


def challan_numbers_for_month(tax_year, year: int, month: int, run=None) -> dict:
    """``{employee_pk: [challan numbers]}`` for one payroll month — every
    allocation under a challan tied to that month (by typed month, or by the
    run when the month was run in Orbis)."""
    cond = Q(challan__tax_year=tax_year, challan__period_year=year, challan__period_month=month)
    if run is not None:
        cond |= Q(challan__run=run)
    out: dict = {}
    for emp_id, number in (
        TaxChallanAllocation.objects.filter(cond)
        .order_by("challan__deposit_date", "challan__challan_number")
        .values_list("employee_id", "challan__challan_number")
    ):
        out.setdefault(emp_id, []).append(number)
    return out


def _legacy_rows(tax_year, year: int, month: int) -> list[LedgerRow]:
    qs = list(
        LegacyPayrollMonth.objects.filter(tax_year=tax_year, year=year, month=month)
        .select_related("employee")
        .order_by("employee__employee_id")
    )
    gross_on = structure_gross_on([lm.employee_id for lm in qs], period_end(year, month))
    challans = challan_numbers_for_month(tax_year, year, month)
    rows: list[LedgerRow] = []
    for lm in qs:
        lines = {k: Decimal(str(v or 0)) for k, v in (lm.lines or {}).items()}
        # Policy tier of the gross the month was actually paid at — the
        # same basis as an Orbis month's full_amount, and what Finance's
        # register carved out that month (a January increment first paid
        # in August leaves July at the old rate). The structure in force
        # at period end is the fallback for a row with no gross line.
        gross = lines.get("GROSS_SALARY") or gross_on.get(lm.employee_id) or Decimal("0")
        rows.append(LedgerRow(
            employee=lm.employee, employee_id=lm.employee.employee_id or "", full_name=lm.employee.get_full_name(),
            year=year, month=month, lines=lines,
            mobile_internet_reimbursement=mobile_internet_reimburse(gross) if gross else Decimal("0"),
            tds_deducted=lm.tds_deducted or Decimal("0"),
            net_pay=lm.net_pay,
            adjustment_months=lm.adjustment_months,
            challan_number=", ".join(challans.get(lm.employee_id, [])),
            source=SOURCE_LEGACY,
        ))
    return rows


def orbis_run_for_month(year: int, month: int):
    """The CALCULATED regular run of the standard period named (year, month),
    newest first — the same run the true-up engine treats as that month's
    authority. None when the month was never run."""
    return (
        PayrollRun.objects.filter(
            period__period_type=PayrollPeriod.PeriodType.STANDARD, period__year=year, period__month=month,
            run_type=PayrollRun.RunType.REGULAR, status=PayrollRun.Status.CALCULATED,
        )
        .order_by("-calculated_at", "-pk")
        .first()
    )


def _orbis_rows(tax_year, year: int, month: int) -> list[LedgerRow]:
    run = orbis_run_for_month(year, month)
    if run is None:
        return []
    challans = challan_numbers_for_month(tax_year, year, month, run=run)
    employees = {
        run_emp.pk: run_emp
        for run_emp in run.run_employees.filter(status=PayrollRunEmployee.Status.INCLUDED).select_related("employee")
    }
    lines_by_emp: dict[int, dict[str, Decimal]] = {pk: {} for pk in employees}
    gross_full: dict[int, Decimal] = {}
    for re_id, code, amount, full_amount in PayrollRunLine.objects.filter(
        run_employee__run=run,
        component_kind__in=(PayComponent.Kind.EARNING, PayComponent.Kind.DEDUCTION),
    ).exclude(component_code__in=_NON_COMPONENT_CODES).values_list(
        "run_employee_id", "component_code", "amount", "full_amount",
    ):
        if re_id not in lines_by_emp:
            continue
        lines_by_emp[re_id][code] = lines_by_emp[re_id].get(code, Decimal("0")) + (amount or Decimal("0"))
        if code == "GROSS_SALARY":
            # The un-prorated monthly gross the segment was priced at; a
            # mid-month change leaves two segments — the later (higher
            # sequence) one is the salary in force at period end.
            gross_full[re_id] = full_amount or Decimal("0")
    rows: list[LedgerRow] = []
    for re_id, run_emp in sorted(employees.items(), key=lambda kv: (kv[1].employee.employee_id or "")):
        lines = lines_by_emp.get(re_id, {})
        tds = run_emp.tax_deducted or Decimal("0")
        if not lines and not tds:
            continue
        mobile = mobile_internet_reimburse(gross_full[re_id]) if gross_full.get(re_id) else Decimal("0")
        rows.append(LedgerRow(
            employee=run_emp.employee, employee_id=run_emp.employee.employee_id or "",
            full_name=run_emp.employee.get_full_name(),
            year=year, month=month, lines=lines, mobile_internet_reimbursement=mobile, tds_deducted=tds,
            net_pay=run_emp.net_pay, adjustment_months=run_emp.adjustment_months or None,
            challan_number=", ".join(challans.get(run_emp.employee_id, [])),
            source=SOURCE_ORBIS,
        ))
    return rows


def monthly_ledger_rows(tax_year, year: int, month: int) -> list[LedgerRow]:
    """Every employee's ledger row for one payroll month of ``tax_year`` —
    legacy rows for a pre-Orbis month, run-derived rows otherwise."""
    if (year, month) not in set(tax_year_months(tax_year)):
        return []
    if is_legacy_month(tax_year, year, month):
        return _legacy_rows(tax_year, year, month)
    return _orbis_rows(tax_year, year, month)


def month_tds_by_employee(tax_year, year: int, month: int) -> dict:
    """``{employee_pk: LedgerRow}`` for the employees with TDS > 0 that
    month — the allocation uploader's prefill and its reconciliation base."""
    return {r.employee.pk: r for r in monthly_ledger_rows(tax_year, year, month) if r.tds_deducted > 0}


def legacy_month_summary(tax_year) -> list[dict]:
    """For the uploader page: each pre-Orbis month with how many employee
    rows are in, and the Orbis months of the year with whether a run exists."""
    counts: dict[tuple[int, int], int] = {}
    for y, m in LegacyPayrollMonth.objects.filter(tax_year=tax_year).values_list("year", "month"):
        counts[(y, m)] = counts.get((y, m), 0) + 1
    out = []
    legacy = set(legacy_months(tax_year))
    for y, m in tax_year_months(tax_year):
        if (y, m) in legacy:
            out.append({"year": y, "month": m, "label": month_label(y, m), "legacy": True, "rows": counts.get((y, m), 0)})
        else:
            out.append({
                "year": y, "month": m, "label": month_label(y, m), "legacy": False,
                "rows": None, "has_run": orbis_run_for_month(y, m) is not None,
            })
    return out


def challan_allocation_summary(tax_year) -> list[dict]:
    """For the allocation uploader page: every challan of the year with how
    many employees are allocated to it, their Σ amount and the difference
    from the typed total — computed in Python (``amount`` is encrypted)."""
    challans = list(TaxChallan.objects.filter(tax_year=tax_year).select_related("bank", "run__period"))
    totals: dict[int, list] = {c.pk: [0, Decimal("0")] for c in challans}
    for challan_id, amount in TaxChallanAllocation.objects.filter(challan__tax_year=tax_year).values_list("challan_id", "amount"):
        totals[challan_id][0] += 1
        totals[challan_id][1] += amount or Decimal("0")
    out = []
    for c in challans:
        count, allocated = totals[c.pk]
        out.append({
            "challan": c, "month_label": c.month_label, "count": count, "allocated": allocated,
            "difference": c.total_amount - allocated,
        })
    return out


# ---------------------------------------------------------------------------
# Workbook (the export — the same shape the cutover import reads)
# ---------------------------------------------------------------------------

def write_months_sheet(ws, rows: list[LedgerRow], components: list[PayComponent], *, template: bool = False) -> None:
    """``template=True`` writes the cutover sheet Finance fills in — the
    derived columns left out and every money cell empty; the export writes
    every column with the figures."""
    ws.append(ledger_headers(components, template=template))

    def money(v):
        return None if template or v is None else money_value(v)

    for r in rows:
        adj_months = None if template else r.adjustment_months
        tail = [money(r.tds_deducted), money(r.net_pay), adj_months, escape_spreadsheet(r.full_name)]
        if not template:
            tail = [money(r.mobile_internet_reimbursement)] + tail[:3] + [r.challan_number, tail[3]]
        ws.append([r.employee_id, r.month_label] + [money(r.amount(c.code)) for c in components] + tail)
    if rows:
        money_cols = len(components) + (2 if template else 3)
        set_money_columns(ws, list(range(3, 3 + money_cols)))
    for row_idx in range(2, len(rows) + 2):
        ws.cell(row=row_idx, column=1).number_format = "@"
        ws.cell(row=row_idx, column=2).number_format = "@"
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 16
    ws.freeze_panes = "C2"


def write_challans_sheet(ws, challans) -> None:
    ws.append(list(CHALLAN_HEADERS))
    for ch in challans:
        period_label = ch.month_label
        if not period_label and ch.run_id and ch.run.period_id:
            period_label = month_label(ch.run.period.year, ch.run.period.month)
        ws.append([ch.challan_number, ch.deposit_date, ch.bank.name, money_value(ch.total_amount), period_label, ch.note])
        ws.cell(row=ws.max_row, column=2).number_format = "yyyy-mm-dd"
    if challans:
        set_money_columns(ws, [4])
    for col, width in {"A": 22, "B": 14, "C": 28, "D": 22, "E": 30, "F": 30}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"


def write_allocation_sheet(ws, rows) -> None:
    """``rows``: (employee_id, challan_number, amount or None, full_name)."""
    ws.append(list(ALLOCATION_HEADERS))
    for employee_id, number, amount, name in rows:
        ws.append([employee_id, number or None, None if amount is None else money_value(amount), escape_spreadsheet(name)])
    if rows:
        set_money_columns(ws, [3])
    for row_idx in range(2, len(rows) + 2):
        ws.cell(row=row_idx, column=1).number_format = "@"
        ws.cell(row=row_idx, column=2).number_format = "@"
    for col, width in {"A": 14, "B": 24, "C": 14, "D": 32}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"


def build_month_ledger_xlsx(tax_year, year: int, month: int) -> bytes:
    """The "Export month" workbook: the Employee months sheet for that month,
    plus a reference Challans tab listing the challans the rows point at."""
    components = ledger_components()
    rows = monthly_ledger_rows(tax_year, year, month)
    numbers = {n for r in rows if r.challan_number for n in r.challan_number.split(", ")}
    challans = list(
        TaxChallan.objects.filter(tax_year=tax_year, challan_number__in=numbers).select_related("bank", "run__period")
    )
    wb = Workbook()
    ws_m = wb.active
    ws_m.title = MONTHS_SHEET
    write_months_sheet(ws_m, rows, components)
    ws_ch = wb.create_sheet(CHALLAN_SHEET)
    write_challans_sheet(ws_ch, challans)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()
