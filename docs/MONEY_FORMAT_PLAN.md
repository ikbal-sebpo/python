# Money display — one accounting format everywhere

**Status: BUILT 2026-10-05 (Finance chose whole taka everywhere — see §4).** Every monetary amount Orbis
shows, prints or exports should read the same way: comma thousands grouping,
right-aligned, tabular figures. Today the only places that do are the Service
Book wage table (`employees/services.py::format_taka`, added 2026-10-05) and
the HR letters (`documents/pdf.py::_fmt_salary_amount`). Everything else
prints the raw `Decimal` (`450000.00`), or `floatformat:2`, or a bare
`float()` cell in Excel.

## 1. The convention

- **One helper, `core/money.py::format_taka(amount, decimals=None)`**:
  comma grouping; `decimals=None` means "whole taka when the value is
  integral, else two places" (the letters' rule); `decimals=0`/`2` force it.
  `None` → `"—"`. Rounds half-up. Negative values render with a leading minus
  (not accounting parentheses — the UI already colours deductions).
- **Template filter `|taka`** in a new `core/templatetags/money.py`,
  registered as a **template builtin** in `core/settings.py`
  (`TEMPLATES[0]["OPTIONS"]["builtins"]`) so no `{% load %}` is needed and the
  DB-stored PDF designs (`TemplateDesign.html`, rendered via
  `engine.from_string`) can use it too. `|taka:2` forces two places.
- **Excel: numbers stay numbers.** Money cells are written as `float` with
  `cell.number_format = "#,##0"` (or `"#,##0.00"`), via a
  `core/exports.py::money_cell(ws, row, col, value, decimals)` helper and a
  per-column convenience, so Finance can still sum them. Never write a
  pre-formatted string.
- **Which precision where** (the one decision to confirm):
  - **Whole taka (`#,##0`)**: master data that is whole by nature — salary
    structures, team packages, pay rates, meal tiers, tax thresholds/caps,
    tax declarations, Service Book, letters.
  - **Two places (`#,##0.00`)**: computed run figures — the register, line
    items, totals, tax computations, variances — because proration and tax
    produce paise and the columns must still add up on screen. Alternative:
    round to whole taka in the engine and show whole everywhere, which is a
    calculation change, not a display one, and is out of scope here.
- `tabular-nums text-right` on every money `<td>` (most already have it).
- Lakh grouping (`4,50,000`) is **not** used — standard thousands throughout,
  as decided for the Service Book.

## 2. Inventory — where amounts are rendered today

### 2.1 Payroll pages (`templates/payroll/`)

| Page | Values | Today | Target |
|---|---|---|---|
| `salary_structure_list.html` (`/payroll/salary/`) | `structure.gross_monthly` | raw | `\|taka` |
| `salary_structure_detail.html` (`/payroll/salary/<uuid>/`) | hero `current.gross_monthly`, history `s.gross_monthly` | raw | `\|taka` |
| `salary_pending_list.html` | `s.current_gross`, `s.gross_monthly`, `s.delta` (×2) | `floatformat:2` | `\|taka` (delta keeps its sign) |
| `run_detail.html` (register) | totals row: `salary_amount`, `night_amount`, `ot_amount`, `transport_amount`, `filtered_gross_total`, `meal_amount`, per-component `amount`, `filtered_deductions_total`, `filtered_tax_total`, `filtered_net_total`; per employee: `re.salary_amount`, `re.night_amount`, `re.ot_amount`, `re.transport_amount`, `re.gross_earnings`, `re.meal_amount`, extra-deduction `amount`, `re.total_deductions`, `re.tax_deducted`, `re.net_pay` | raw | `\|taka:2` |
| `_drawer_adjust.html` | `calculated_tax` in the help text | raw | `\|taka:2` |
| `variable_pay_list.html`, `_variable_entry_row.html` | `entry.amount` | `floatformat:2` / raw | `\|taka` |
| `_salary_upload_report.html`, `_salary_history_upload_report.html` | `row.gross_monthly`, `h.current.gross_monthly` | raw | `\|taka` |
| `_sla_upload_report.html`, `_test_salary_upload_report.html` | `row.amount` | `floatformat:2` | `\|taka` |
| `_go_live_upload_report.html` | `row.package_amount` | `floatformat:2` | `\|taka` |
| `run_list.html`, `report.html`, `overtime_list.html`, `_overtime_*` | counts/hours only | — | no change |

### 2.2 Employee profile + Service Book

| Place | Values | Target |
|---|---|---|
| `employees/sections/_tax_declaration.html` | `row.total_investment`, `row.source_tax`, `row.other_tds_adjustment` | `\|taka` |
| Service Book (HTML + PDF design) | `row.gross_display` | switch to `\|taka` and delete `employees/services.py::format_taka` once the shared helper exists (keep behaviour identical) |

### 2.3 Letters (`documents`)

`documents/pdf.py::_fmt_salary_amount` already does comma grouping with the
"whole when integral" rule and parenthesised negatives (HR types the
figures). Keep it, but have it call `core.money.format_taka` so the rule
lives in one place; keep its parentheses convention for letters only.

### 2.4 Excel exports

| Builder | Money columns | Today | Target |
|---|---|---|---|
| `payroll/exports.py::build_register_export` | 5 component amounts, gross, deductions, tax, net | `float()` no format | `#,##0.00` |
| `payroll/exports.py::build_calculated_export` | salary/night/OT/transport/gross/meal, extra deductions, deductions/tax/net | `float()` no format | `#,##0.00` |
| `payroll/exports.py::build_support_info_export`, `build_overtime_export`, `build_exceptions_export` | counts / hours / text | — | no change |
| `payroll/views.py::_export_excel` (support info) | counts only | — | no change |
| `payroll/upload.py::build_salary_xlsx_template` | "Current gross (reference only)" | `float()` | `#,##0` |
| `payroll/upload.py::build_salary_history_xlsx_template`, `leave/upload_*` templates | no prefilled money | — | no change |
| `employees/exports.py`, `attendance/exports.py`, `leave/views.py` CSV | no money | — | no change |

### 2.5 Django admin

`list_display` money columns on `SalaryStructure`, `TeamSalaryPackage`,
`PayRate`, `MealDeductionTier`, `TaxExemptionThreshold`/`TaxYear` caps,
`PayrollRun` totals, `PayrollRunEmployee`, `VariablePayEntry`, `TDSEntry`,
`TaxComputation`, `EmployeeTaxDeclaration` show raw Decimals. Add a
`money_display(field, decimals)` factory in `core/admin_mixins.py` (an
`@admin.display(ordering=…)` wrapper returning `format_taka`) and use it in
`payroll/admin.py`. Edit forms stay plain inputs. Lowest priority — admin is
superuser + Payroll Admins only.

### 2.6 Already fine / out of scope

- Dashboard tabs, policies, offboarding, leave: the "totals" there are counts
  (people, days, steps), not money.
- Flash messages and upload reports only print counts.
- Stored values are untouched; encrypted columns are formatted in Python
  anyway, so nothing here touches a query.

## 3. Work plan

1. **Core** — `core/money.py`, `core/templatetags/money.py` (`taka` filter),
   settings builtin, `core/exports.py::money_cell` + a `set_money_columns(ws,
   first_row, cols, decimals)` helper; unit tests (`core/tests_money.py`):
   grouping, half-up rounding, integral vs paise, None, negatives, the filter
   with/without an argument, Excel number formats.
2. **Guard test** — same posture as `core/tests_encryption.py`'s source scan:
   fail on any `{{ … }}` in `templates/` whose last attribute is a known money
   name (`gross_monthly`, `gross_earnings`, `net_pay`, `tax_deducted`,
   `total_deductions`, `salary_amount`, `night_amount`, `ot_amount`,
   `transport_amount`, `meal_amount`, `package_amount`, `current_gross`,
   `delta`, `calculated_tax`, `total_investment`, `source_tax`,
   `other_tds_adjustment`, `amount` inside `templates/payroll/`) without a
   `|taka` filter, and on `floatformat` applied to any of them. Keeps the
   convention from drifting back.
3. **Payroll templates** (§2.1) and the profile tax section (§2.2) — mechanical
   filter edits; `floatformat:2` → `|taka`.
4. **Exports** (§2.4) — number formats on the register and calculated
   exports and the salary template's reference column.
5. **Service Book + letters** — point both at `core.money.format_taka`;
   re-run `seed_service_book_template` after deploy (the design will use
   `|taka` directly, which is why the filter is a builtin).
6. **Admin** (§2.5).
7. **Docs** — `docs/PLAN.md` "Money display" convention + a CLAUDE.md working
   convention line: *"Money — never render a Decimal raw or with
   `floatformat`; use `|taka` (`|taka:2` for computed run figures) in
   templates and `money_cell` in exports."*

Order 1 → 2 → 3/4 (parallel) → 5 → 6 → 7. One branch, one deploy, one
`seed_service_book_template` on prod. Roughly a day's work; the guard test
is what makes it stick.

## 4. Decision (2026-10-05)

Finance prefers **whole taka everywhere**, including the register and line
totals, rounded at display time, accepting that a column may be off by a
taka from its printed total. So `|taka` (no argument) is the rule on every
page and `#,##0` in every export; `|taka:2` exists but nothing uses it.

## 5. Inputs follow suit (2026-10-07)

Finance confirmed the same for anything a person *types*: no paise in a
money input. `core.forms.TakaField` is the form field — it renders the
bound value as an integer (`306786`, never `306786.00`), asks the browser
for `step="1"`, and rounds a typed decimal half-up to the nearest taka
rather than rejecting it. `core.fields.EncryptedDecimalField.formfield()`
returns it by default, so every ModelForm and Django-admin form over an
encrypted money column complies without naming it; the three hand-built
money inputs in `payroll/forms.py` (TDS amount, variable-pay amount, edit
amount) name it directly. Columns keep `decimal_places=2` — this is an
input/display rule, not a storage change. Noticed on the tax declaration
drawer, where the allowable/actual investment showed `306786.00`.
