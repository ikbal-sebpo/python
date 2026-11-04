# Pre-Orbis payroll months — import of the three FY2026-27 payrolls already paid

**Status:** BUILT 2026-10-08 (same day as proposed), **revised the same evening after the
user reviewed the downloaded template** — see "Revision" below. **Revision 2 (2026-10-09, built the same day) separates the per-employee challan allocation from the months sheet and drops the typed reimbursement — see "Revision 2" below; §1–§3 describe the 2026-10-08 state and are corrected by it. Code: `payroll/0031`, `TaxChallanAllocation`, `TaxChallan.period_year/month`, the `..._challan_allocation_*` trio in `payroll/upload.py`, `UploadChallanAllocationView` + template, `ChallanAllocationUploadTests`.** Code: `payroll/0029`–`0030`,
`payroll/tax_ledger.py`, the `parse_/apply_/build_tax_challan_*` and `..._legacy_payroll_*` trios
in `payroll/upload.py`, `UploadTaxChallanView` / `UploadLegacyPayrollView` + template + export
views in `payroll/upload_views.py`, `payroll/tests_legacy_payroll.py`. Not yet deployed — §4 is
the production runbook.

**Revision (2026-10-08 evening, user's review of the template):**

1. **Two uploaders, not one workbook.** Challans are needed *every month* from now on (Finance
   deposits TDS after each payout), so **"Tax challans"** is its own permanent Upload Center card
   (`UploadTaxChallanView`, Finance + Payroll Admins + User Admins — company-level figures, not
   per-employee pay): Challan No · Date · Bank · Total · **Payroll month (optional)** · Note. The
   tax year is the one the deposit date falls in; the payroll month links the challan to that
   month's CALCULATED regular run when one exists (`TaxChallan.run`, source `RUN`), warns and
   records it unlinked when the run isn't calculated yet (re-upload links), and is recorded only
   for a pre-Orbis month. Re-upload corrects by number; the run link is never cleared from here.
   The **months sheet is single-tab** and references challans by number — a number not yet in
   Orbis is an error pointing at the challan uploader. CSV works for both.
2. **The months sheet carries every pay component**, not only taxable earnings: the column block
   is every active EARNING then DEDUCTION component (`ledger_components`), TDS stays a fixed
   column, and **Net pay** is a new optional reference column (`LegacyPayrollMonth.net_pay`).
   It is Finance's one-time full payroll record for the pre-Orbis months. The certificate and
   the true-up still read only the taxable earnings out of a row (`taxable_earning_codes`;
   `LegacyPayrollMonth.taxable_additional_pay` ignores deduction lines). The Orbis-month export
   now carries deduction lines and net pay from the run too. Loan recoveries in the months sheet
   are informational — the figure the engine uses stays the loan cutover sheet's "Recovered
   before Orbis".
**Revision 2 (2026-10-09, user's review of the live page at `/payroll/legacy-months/upload/`) —
BUILT the same day.** Three findings; the third changes the data model.

### R2.1 The missing "Salary arrear" column is a seed, not a gap — and it is now "Salary adjustment"

`ARREAR` and `FESTIVAL_BONUS` are in `seed_payroll_components` (commit `09b5e95`) but the local
database was never re-seeded, so `PayComponent` still ends at `LOAN_REPAYMENT` and the header —
generated from the component list at request time — has no arrear column. `manage.py
seed_payroll_components` adds both; §4 step 1 already lists it for production. Nothing in the
uploader changes.

**Renamed after the first test upload (user, 2026-10-09): `ARREAR` → `SALARY_ADJUSTMENT`,
"Salary adjustment".** One taxable earning for *pay that belongs to another period, paid in this
one* — the increment arrear, or a payment held back one month for any reason and released in a
later payroll — rather than a code per reason. Positive only: recovering an overpayment is a
deduction. `payroll/0032` renames an already-seeded `ARREAR` row and rewrites the key inside
stored `LegacyPayrollMonth.lines` (and any `PayrollRunLine.component_code`), so a database that
had the first seed converts in place. "Salary arrear" below means this component.

**The 2026 arrear schedule (user, 2026-10-09, correcting §5.2):** the January–July 2026
increment difference (seven months) is paid in **three instalments of 3 + 3 + 1 months**:

| Paid on | Payroll period | Sheet month | Instalment |
|---|---|---|---|
| 3 Oct 2026 | 16 Aug – 15 Sep | `2026-09` (pre-Orbis — this import) | 1st, three months' arrear |
| 1 Nov 2026 | 16 Sep – 15 Oct | `2026-10` (first Orbis run) | 2nd, three months' arrear — Finance files it as a `VariablePayEntry` on `ARREAR` |
| 1 Dec 2026 | 16 Oct – 15 Nov | `2026-11` (Orbis run) | 3rd, one month's arrear — same; the arrear is then complete |

So the September row's "Salary adjustment" cell carries the first instalment and nothing else;
without it the row can't match Finance's register. §5.2's "January–July's difference in three
instalments from the 16 Aug – 15 Sep payroll" stands; its "the third falls in the October run"
is wrong — the **second** does, and the third is November.

### R2.2 Mobile & Internet reimbursement is derived from policy, never typed

The reimbursement is a *tier of the monthly gross* (`tax.py::mobile_internet_reimburse`), the
same for a pre-Orbis month as for an Orbis one. The stored
`LegacyPayrollMonth.mobile_internet_reimbursement` has exactly one reader — the ledger export —
and the true-up/certificate already derive it. So:

- **Drop the column from the months sheet and the field from the model** (`payroll/0031`; no
  rows exist in any database — the uploader was never deployed, confirm with §4 step 0).
- **The ledger still carries the column, derived:** for an Orbis month unchanged
  (`full_amount` of the gross line); for a legacy month the tier of the row's own `GROSS_SALARY`
  line — the gross the month was *paid* at, which is what Finance's register carved out (user's
  check 2026-10-09: July 2026 was paid at the pre-increment 109,834 → 3,000, though the structure
  in force said 120,817 → 4,000) — falling back to the `SalaryStructure` gross in force on the
  period's end date only for a row with no gross line. `structure_gross_on` in `tax_ledger.py`.
- **The parser ignores a "Mobile & Internet reimbursement" column** if Finance round-trips an
  export back in (`_IGNORED_KEYS`, not `_FIXED_KEYS`) — never an "unknown column" error.
- **Q68 consequence (certificate plan):** the reimbursement is no longer "answered by the data
  Finance types". Orbis asserts the policy tier for every month; Finance verifies it against
  their register from the "Export month" sheet (§4 step 4). If a month disagrees, that is a
  finding about the tier table, fixed there — not a per-row override.

### R2.3 Which challan carried how much of an employee's tax is its own record

The user's requirement: *"a separate record, uploaded by Payroll Admins, of in which challan
exactly how much of an employee's tax was included."* The current design answers it two
different ways — a `LegacyPayrollMonth.challan` FK (one challan per employee-month, amount
implied by the month's TDS) for pre-Orbis months, and the certificate plan's amount-less
`TaxChallanMember` for Orbis runs. Replace both with one table:

**`TaxChallanAllocation`** — one row per (challan, employee), with the amount.

| Field | Notes |
|---|---|
| `challan` FK `TaxChallan` (PROTECT), `employee` FK (PROTECT) | unique together |
| `amount` `EncryptedDecimalField` | the taka of this employee's tax inside this deposit — **stored, not derived** (see "Reversal") |
| `source` (`IMPORTED` / `RUN`), `batch` FK `PayrollInputBatch` (new `Kind.TAX_CHALLAN_ALLOCATION`), `_Auditable` | |

Migration with the `_match_existing_collation` CONVERT step and `atomic = False` (CLAUDE.md
"MySQL collation"); `PayrollMoneyAdminMixin` on `amount`.

**`TaxChallan` gains `period_year` / `period_month`** (nullable). The challan uploader already
parses the optional "Payroll month" cell but only keeps it as `run` when a calculated run exists,
so a pre-Orbis challan currently forgets its month. Store the month whenever one is typed;
`run` stays the link to the calculated run. The allocation checks below need the month.

**`LegacyPayrollMonth.challan` is dropped** (same migration as R2.2) and "Challan No" leaves the
months sheet; the parser ignores the column on a round-trip. The months sheet is now exactly
what the user asked for: *what was paid to each employee on each payroll* —
`Employee ID | Payroll month | <every active component, earnings then deductions> | TDS deducted
| Net pay (optional) | Employee Name (reference)`. The §3.5 openings write is unchanged.

**New permanent Upload Center card — "TDS by challan (per employee)"** (built: `/payroll/tax-challans/allocations/upload/`). Payroll Admins only
(`payroll_salary` gate: per-employee tax figures, same posture as the months). Same
preview → apply → template trio in `payroll/upload.py` (`parse_/apply_/build_tax_challan_allocation_*`),
view + template + URL names `payroll_tax_challan_allocation_upload` / `..._template`, ACL rows in
`seed_orbis.py`, CSV accepted. Monthly from now on: Finance records the deposit on the "Tax
challans" card, then Payroll Admins upload who was in it and for how much.

Sheet (single tab + a read-only "Challans" reference tab listing the year's challans with number,
date, bank, month, total):

| Employee ID | Challan No | Amount | Employee Name (reference) |
|---|---|---|---|

Template: one row per employee per payroll month that has at least one challan recorded —
pre-Orbis months from `LegacyPayrollMonth.tds_deducted`, Orbis months from the run's
`tax_deducted` — **amount prefilled with the month's TDS**, and the Challan No prefilled only
when that month has exactly one challan (unambiguous), blank otherwise for the admin to fill.
Employees with zero tax that month are left out.

Rules (errors abort the row): challan number must exist for the tax year (message points at
the Tax challans card); Employee ID must resolve (leavers included); amount > 0; the same
(employee, challan) twice in one file. Re-upload overwrites (facts, not a ledger — the
openings/months posture).

Soft checks (warnings, the payroll soft-gate preference):

- Per challan: Σ allocations vs `TaxChallan.total_amount`, with the difference — a deposit can
  include people outside the file, and a company may split one run across banks.
- Per employee per month (via the challan's month): Σ allocations vs the month's TDS (legacy row
  or run) — mismatch, and "taxed that month but in no challan". The latter is the certificate's
  blocker, not this upload's.
- An employee allocated to two challans of the same month — Finance said exactly one
  (2026-10-08, certificate plan §1) — warning, not refused: the amounts still sum.

**Reversal of the certificate plan's §3.1 "the amount is never stored".** That rule exists so a
recalculated run can't leave a stale figure behind. But a challan is a *deposit that already
happened*: if the run is recalculated after the money went to the treasury, the truthful
certificate figure is what was deposited, and the thing that should surface is a warning that
the run's `tax_deducted` no longer equals its allocation — computed at render on the run and the
challans page, exactly like `overtime_changed_since_calculation`. Storing the amount is also what
makes a deposit tied to **no** run (the sample's 30 June year-end row, Q70's open remainder)
representable with no extra path: an allocation to a challan with no month. Consequences for
`TAX_CERTIFICATE_PLAN.md`: `TaxChallanMember` and `extra_amounts` are superseded by
`TaxChallanAllocation`; Piece A's membership sheet *is* this card; the "Record challans" drawer on
a run shrinks to a convenience that creates the challan and, when it is the run's only one,
auto-allocates every taxed employee at their `tax_deducted`; §5.1's challan-row rule becomes one
read — Σ `TaxChallanAllocation.amount` per challan for the employee.

**Ledger / export.** `LedgerRow.challan_number` is read from the allocations for that
employee-month (numbers joined by ", " when there are several), for legacy and Orbis months
alike — replacing `_orbis_rows`'s "one challan on the run else blank" shortcut.
`build_month_ledger_xlsx`'s Challans tab keeps listing the challans the rows point at.

### R2.4 Rollout order for FY2026-27 (replaces §4 steps 3–4)

0. Before migrating, confirm `LegacyPayrollMonth` and `TaxChallan` are empty in production
   (the two fields being dropped carry no data anyone typed).
1. Deploy; `migrate payroll`; `seed_payroll_components`; `seed_orbis --menus-only`; set
   `orbis_live_from` to October 2026.
2. Payroll Admins upload the **Pre-Orbis payroll months** (September's row carries the first
   adjustment instalment and its "Salary adjustment months" = 3). **This is the only upload
   payroll processing depends on** — the TDS true-up and the salary adjustment ledger read the
   month rows; neither reads a challan. Recalculate the open October run right after.
3. Finance records each **Tax challan** as it is deposited — the three for July–September
   whenever convenient, every later one monthly. Nothing waits on them except the certificate
   (due next July) and step 4.
4. Once a month's challans are in, Payroll Admins download the **TDS by challan** template —
   amounts prefilled from the month rows, challan number filled in where the month has exactly
   one — and upload it. (User, 2026-10-09: the earlier "challans first" wording here was only
   so that this template prefills; it was never a dependency of the payroll itself.)
5. "Export month" for one legacy month back to Finance to eyeball (reimbursement now derived —
   R2.2); recalculate the open October run and confirm the `TAX_YTD_GAP` /
   `MISSING_TAX_OPENING` warnings clear.

### R2.5 Build order and estimate

1. `TaxChallanAllocation` + `TaxChallan.period_year/month` + drop the two `LegacyPayrollMonth`
   fields — one migration, collation step, `atomic = False`; admin — half a day.
2. Months uploader: remove the two columns, ignore them on round-trip, derived reimbursement in
   `_legacy_rows`, page copy and Upload Center description; challan uploader stores the month —
   half a day.
3. Allocation uploader (parse / apply / template / page / card / ACL) + tests mirroring
   `tests_legacy_payroll.py` (round-trip, prefill, the three soft checks) — one day.
4. Ledger challan column from allocations; `TAX_CERTIFICATE_PLAN.md` §3.1/§4/§5.1 edits;
   `PAYROLL_PLAN.md` reversal note; CLAUDE.md payroll paragraph — half a day.

### R2.6 New question for Finance (continuing `TAX_CERTIFICATE_PLAN.md` §9)

| # | Question | Recommended answer |
|---|---|---|
| 77 | For July–September 2026, was the Mobile & Internet reimbursement in your register exactly the tier of each employee's gross (as the tax sheet applies it), or did any month differ? | Expect "the tier"; verify from the "Export month" sheet rather than typing it. |

(The arrear question drafted here was answered by the user before it was asked — see R2.1.)

Split out of the first draft of `TAX_CERTIFICATE_PLAN.md` the same day: that plan
(the certificate PDF, challan membership, issue list) needs Finance's answers next
week; this one is the data it will read, and the October 2026 run needs two of its
pieces (§5) regardless.

**The situation.** Orbis runs its first real payroll for the **October 2026 period
(16 Sep – 15 Oct 2026)**. The tax year is July–June, and the July, August and
September 2026 payrolls of FY2026-27 were processed in Finance's workbooks. Every
later per-employee annual figure — the tax certificate, the year-to-date TDS
true-up, any "what did we pay this person this year" report — needs those three
months in Orbis, **per month and per pay component**, not as a total.

What exists: `TaxYear.orbis_live_from` + `EmployeeTaxDeclaration.opening_tds_deducted`
/ `opening_additional_pay` and the "Tax-year openings" uploader
(`TAX_CALCULATION_TRUEUP_PLAN.md` §4/§6) — year-to-date **totals** only.

---

## 1. The monthly tax ledger — one shape, in and out

The user's framing (2026-10-08): a pre-Orbis month row should carry **every monetary
amount the payroll process computes for an employee**, so that Orbis can later export
the very same columns for any month it ran itself. The import format is therefore
defined as the export format:

| Column | Source for an Orbis month | Source for a pre-Orbis month |
|---|---|---|
| Employee ID, Payroll month (`YYYY-MM`, `periods.py` naming: "July 2026" = 16 Jun – 15 Jul) | run / period | typed |
| One column per **taxable EARNING `PayComponent`**, in `sequence` order — today: Gross salary, Salary arrear (§5), Festival bonus (§5), Performance bonus, Incentive, SLA achievement incentive, Overtime allowance, Night duty allowance, Special shift transport; anything seeded later appears automatically | `PayrollRunLine.amount` by `component_code` | typed |
| Mobile & Internet reimbursement | `tax.py::_mobile_internet_reimburse(gross_monthly)` — the tier the engine carves out of gross; never a line | typed (Finance's figure for that month) |
| TDS deducted | `PayrollRunEmployee.tax_deducted` | typed |
| Challan No | `TaxChallan` linked to the run (certificate plan) | typed, optional |

Deductions other than TDS (meal, loan, disciplinary, test salary) and net pay are
**not** in the ledger: they are not income and no annual document reports them.

`payroll/tax_ledger.py::monthly_ledger_rows(tax_year, year, month)` builds the rows
for either kind of month and is the one function behind the export (§3.4), the
import preview's comparison, and later the certificate.

---

## 2. Data model

New per-employee money columns are `core.fields.Encrypted*Field` (the registry guard
in `core/tests_encryption.py` enforces it). New tables with a `CustomUser` FK get the
`_match_existing_collation` CONVERT step and `atomic = False` (CLAUDE.md "MySQL
collation").

### 2.1 `TaxChallan` — the company's treasury deposit (minimal here)

| Field | Notes |
|---|---|
| `tax_year` FK `TaxYear` | |
| `challan_number` | e.g. `2526-00042394161`; unique per tax year |
| `deposit_date` | the date on the challan |
| `bank` FK `payroll.Bank` (PROTECT) | the two banks on Finance's sample (Bank Asia PLC, Modhumoti Bank Limited) are already seeded |
| `total_amount` | **plaintext** `DecimalField` — a company-level figure, not personal data; same exception class as `PayRate` (add to the registry allow-list) |
| `run` FK `PayrollRun` (nullable, PROTECT) | blank for everything this plan imports; set by the certificate plan's run drawer |
| `note`, `source` (`IMPORTED` / `RUN`), `_Auditable` | |

Only the table and admin land here. Per-employee membership for Orbis runs
(`TaxChallanMember`), the "Record challans" drawer and the Tax Challans page are the
certificate plan's §3.2/§5 — they need Finance's answer on the 30 June deposit.

### 2.2 `LegacyPayrollMonth` — one row per employee per pre-Orbis payroll month

Named after `leave.LegacyAnnualLeaveRecord`: stored exactly as Finance supplied it,
never recomputed.

| Field | Notes |
|---|---|
| `tax_year` FK, `employee` FK, `year`, `month` | unique together (employee, year, month) |
| `lines` `EncryptedJSONField` | `{component_code: amount}` for the taxable EARNING components paid that month; keys validated against `PayComponent` on import |
| `mobile_internet_reimbursement` `EncryptedDecimalField` | |
| `tds_deducted` `EncryptedDecimalField` | |
| `challan` FK `TaxChallan` (nullable, PROTECT) | the deposit this month's TDS went under — one per employee-month (Finance 2026-10-08: an employee's tax for a run is in exactly one challan, even when the company splits the deposit) |
| `batch` FK `PayrollInputBatch` | audit envelope, new `Kind.LEGACY_PAYROLL_MONTH` |
| `_Auditable` | |

Re-upload overwrites (facts, not a ledger) — the openings uploader's posture.

---

## 3. The uploader

### 3.1 Who, where

Upload Center card **"Pre-Orbis payroll months (cutover)"**, Payroll Admins only
(`payroll_salary` gate — per-employee pay and tax, same posture as the openings, salary
history and loan cutover uploads). Same preview → apply → template shape as every
other `payroll/upload.py` importer (`parse_legacy_payroll_upload` /
`apply_legacy_payroll_upload` / `build_legacy_payroll_xlsx_template`), views in
`upload_views.py`, URL names `payroll_legacy_payroll_upload` +
`..._template`, ACL rows in `seed_orbis.py` beside the tax-opening ones.

### 3.2 The workbook — two sheets

**Sheet "Challans"** (company-level, a handful of rows; may be left empty):

| Challan No | Date | Bank | Total Challan Amount | Note |
|---|---|---|---|---|

**Sheet "Employee months"** — the §1 ledger columns, generated from `PayComponent`
at template-download time so the header is always the live component list.

Rules:
- `Payroll month` must be a STANDARD period **before** `TaxYear.orbis_live_from`
  and inside the tax year (FY2026-27: `2026-07`, `2026-08`, `2026-09`). Anything
  else is a row error — an Orbis month comes from its run, never from a sheet.
  `orbis_live_from` unset = file error with the instruction to set it first.
- Unknown column header = file error (a component Orbis doesn't know — seed it
  first). Blank cell = 0. Money parses through `_parse_money_or_zero`.
- `Employee ID` must resolve (active or not — leavers since July have months too).
- `Challan No` must appear on the Challans sheet or already exist for the tax year.
  **Blank is allowed** (warning when `TDS deducted` > 0) so the import never blocks
  on a challan number Finance hasn't looked up yet; a re-upload fills it later.
- Bank matched by name or `short_code` against `payroll.Bank`, case-insensitive;
  unknown bank = row error, not auto-create.
- Template notes on the two columns Finance may read differently: "Gross salary:
  the gross paid for the month, as in your register, before TDS" and "Mobile &
  Internet reimbursement: the month's figure if your records carry it separately;
  otherwise leave blank". Whatever Finance types *is* the definition — the
  certificate plan's Q68 is answered by this data, not by this uploader.

### 3.3 Preview checks (soft — the payroll soft-gate preference)

- Per challan: Σ `TDS deducted` of the rows pointing at it vs. `Total Challan
  Amount` — warning with the difference (a deposit can include people outside the
  file).
- Per employee: months present vs. months expected (joined before each period's
  end, not a leaver before its start) — warning on a gap.
- Per employee: Σ `TDS deducted` vs. the `opening_tds_deducted` already on their
  2026-27 declaration if the totals uploader ran first — warning on mismatch
  (then §3.5 overwrites).
- Rows for an employee with no `EmployeeTaxDeclaration` for the year: info — apply
  creates it, as the openings uploader does.

### 3.4 "Export month" — the same workbook back out

On the uploader page (it lists the months already imported, with row counts, per
tax year): a month picker + **Export** producing the §1 workbook for **any** payroll
month of the tax year — legacy months from `LegacyPayrollMonth`, Orbis months from
run lines — via `monthly_ledger_rows`. Same ACL as the upload. This is Finance's
monthly reconciliation sheet against their own records, and the proof that an
imported month and an Orbis month are the same shape. Money cells via
`core.exports.money_value` + `set_money_columns`.

### 3.5 Apply also writes the true-up openings

For each employee in the file, after upserting the rows:

```
opening_tds_deducted    = Σ tds_deducted over their legacy months
opening_additional_pay  = Σ lines[code] over their legacy months,
                          for every code NOT in tax.py::TAX_EXCLUDED_EARNING_CODES
```

— the identical rule the engine applies to an Orbis month's lines, so a legacy
month and an Orbis month feed the projection on the same basis, and the engine's
"already deducted" can never disagree with the month rows. The existing
"Tax-year openings" card stays for a future year where Finance only has totals; its
description says this import supersedes it.

---

## 4. Rollout for FY2026-27

1. Deploy; `migrate payroll`; `seed_payroll_components` (§5); `seed_orbis --menus-only`.
2. In the admin, set `orbis_live_from` on the 2026-27 `TaxYear` to the **October
   2026** period (the true-up rollout step — this import validates against it, so it
   goes first).
3. Finance fills the two-sheet template for July, August, September 2026 (challan
   numbers if at hand, blank otherwise); Payroll Admins upload, read the warnings,
   apply.
4. Download "Export month" for one legacy month and hand it back to Finance to
   eyeball — the same file they filled, so differences are theirs to explain.
5. Recalculate the open October run; its `TAX_YTD_GAP` / `MISSING_TAX_OPENING`
   warnings should clear for every uploaded employee.

---

## 5. Two seed changes the October run needs anyway

Confirmed by the user 2026-10-08; both are in this plan because they shape the §1
columns and the first Orbis run can't wait for the certificate.

### 5.1 `FESTIVAL_BONUS` — SEBPO pays both a festival and a performance bonus

`BONUS` ("Performance bonus") keeps its name. Seed **`FESTIVAL_BONUS`** ("Festival
bonus", `EARNING`, taxable, not prorated, `EntryMode.INPUT` until a later phase
generates the 2 Eids × 50% automatically). Then
`tax.py::TAX_EXCLUDED_EARNING_CODES` becomes `{"GROSS_SALARY", "FESTIVAL_BONUS"}`:
today's exclusion of `BONUS` was written on the assumption that it *was* the festival
bonus (which the projection already assumes at one month's gross); a performance
bonus is additional pay and must count in the true-up like `INCENTIVE`. No real money
has gone through a run yet (the encryption migrations purged the tables), so the
reinterpretation is safe; the exclusion assertions in `tests_tax_trueup.py` move to
the new code. **Record as a reversal in `PAYROLL_PLAN.md`.**

### 5.2 `ARREAR` — the yearly increment is paid late

The fact: the annual increment takes effect 1 January but is first *paid* in August;
January–July's difference is paid as arrears in three instalments starting with the
16 Aug – 15 Sep payroll. Almost every year. **For 2026 the instalments are 3 + 3 + 1
months across the September, October and November periods (R2.1) — the second one
falls in the October 2026 period, Orbis's first real run, and the third in November.**

Seed **`ARREAR`** ("Salary arrear", `EARNING`, taxable, not prorated,
`EntryMode.INPUT`, `affects_gross`). Finance files the instalment as a
`VariablePayEntry` through the existing bonus/incentive uploader, exactly like an
incentive. Nothing else changes: the engine pays it as an ordinary line; the true-up
counts it as additional pay automatically (taxable, not excluded), so October's TDS
reflects it; the §1 ledger grows a "Salary arrear" column by itself, so the August
and September instalments ride in on this import.

**Follow-up — BUILT 2026-10-09 as `SALARY_ADJUSTMENT_PLAN.md`** (the Payroll Admin picks
how many months each run settles; the taka is derived from the structures vs what each
month paid, with a revision topping up the settled months automatically). The original
sketch, kept for the record: The compensation master already tells the
truth (`SalaryStructure.effective_from` = 1 Jan; `engine.py::_salary_segments` pays
each day at the rate in force), so the delta is computable: for every CALCULATED run
since `effective_from` and before the first run paid at the new rate, (what the
current structures would have paid, same proration) − (`GROSS_SALARY` actually
paid); minus `ARREAR` lines already paid for that structure; paid over
`SalaryStructure.arrear_instalments` (default from `PayrollSettings`, 3). Balance
derived, never cached — the salary-loan posture. It can't serve the 2026 raise
(January–July 2026 were never run in Orbis), which is why §5.2's manual entry exists.
Guard to add with it: warn on a CALCULATED run whose structures changed after
`calculated_at`, since recalculating a paid month at the new rate would silently
shrink the arrear (closed properly by Phase 5's approval chain). Finance questions on
it are Q75–Q76 in `TAX_CERTIFICATE_PLAN.md` §9.

---

## 6. Build order and estimate

1. Models + migration (collation step, `atomic = False`) + admin
   (`PayrollMoneyAdminMixin` on the money fields) + registry allow-list for
   `TaxChallan.total_amount` — half a day.
2. `seed_payroll_components`: `FESTIVAL_BONUS`, `ARREAR`; `TAX_EXCLUDED_EARNING_CODES`
   change + test moves + `PAYROLL_PLAN.md` reversal note — an hour.
3. `tax_ledger.py::monthly_ledger_rows` + the export workbook — half a day.
4. Uploader (parse / apply / template, two sheets, component-generated header,
   preview warnings, openings write) + views + Upload Center card + `seed_orbis`
   ACL + tests mirroring `tests_upload_center.py` (a round-trip test: import a
   month, export it, compare) — one day.
5. Docs: CLAUDE.md payroll paragraph, this file's status line,
   `TAX_CALCULATION_TRUEUP_PLAN.md` §6 pointer ("superseded for FY2026-27 by the
   month-level import").

Pause for the user's local check before any commit/deploy.
