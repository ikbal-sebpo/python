# Salary loans — HR records the approval, payroll recovers the instalments

**Status: IMPLEMENTED 2026-10-08** (questions answered the same day, §8; built as written with the deviations in §9). Source: the approved
"Request for Employee Financial Assistance" file note Finance shared
(HR/21092026/01, dated 21.09.2026): management approves a lump sum
(BDT 50,000) that the employee repays "in 10 monthly installments
(5,000 BDT / Month - Payroll deduction)". HR prepares and files the note,
top management approves it on paper, Finance pays the money out and then
deducts the instalment from every payroll until it is recovered.

Today Orbis has none of this. `payroll/engine.py` carries the comment
`# Loan recovery — no-op, no SalaryLoan model yet (Phase 6)` right after
the TDS line, `docs/PAYROLL_PLAN.md` §"Loans" sketched a `SalaryLoan` +
`SalaryLoanTransaction` pair in August, and `docs/PAYROLL_PROCESS.md`
already states the two business rules: a loan instalment is deducted
**after** tax (it does not reduce taxable pay) and it **never takes net pay
below zero**. This plan builds that slot, adjusted to how the engine
actually turned out (delete-and-rebuild, no paid state yet — §3.2).

## 1. What the form tells us

| On the file note | Becomes in Orbis |
|---|---|
| Employee name + ID | `SalaryLoan.employee` |
| "consolidated lump sum amount of BDT 50,000" | `principal` |
| "10 monthly installments (5000 BDT / Month)" | `installment_count` = 10, `installment_amount` = 5,000 |
| File Reference `HR/21092026/01`, Date 21.09.2026 | `file_reference`, `approved_on` |
| The signed note itself | `approval_document` (scan, required — Finance deducts nothing without it) |
| Reason ("medical expenses related to the birth of his child") | `note` |
| Finance pays the money out | `disbursed_on` / `disbursed_by`, set by Finance; **nothing is deducted before this** |
| "Payroll deduction" from the next payroll | `first_deduction_period`, picked by HR; the engine does the rest |

Not on the form, assumed unless Finance says otherwise: **no interest**, no
processing fee, equal instalments with any rounding remainder on the last
one, recovery runs until the outstanding balance is zero.

## 2. Who does what

| Step | Who | In Orbis |
|---|---|---|
| Record the approved loan | HR (payroll-HR pair) or Payroll Admins | `/payroll/loans/` → "Add loan" drawer |
| Pay the money out | Finance | "Mark disbursed" on the loan — Payroll Admins (Finance's payroll staff hold that group since 2026-10-05) |
| Deduct monthly | nobody | the engine, on every calculated run from `first_deduction_period` until settled |
| Change the instalment / pause / waive | Finance with HR | "Edit" (instalment amount, forward-looking) or an **adjustment** row (§3.3) |
| See their own balance | the employee | read-only card on their profile |

Permissions (`accounts/permissions.py`), following the existing tiers:

- `can_manage_salary_loans(user)` — create / edit / cancel: `_is_payroll_hr` or `_is_payroll_admin`. Same pair as `can_create_payroll_run`. A plain "Payroll" supervisor or a generic HR person never sees a loan.
- `can_disburse_salary_loans(user)` and adding adjustments — `_is_payroll_admin` only (it is money leaving the company; the same tier that sees money everywhere else in payroll).
- `can_view_salary_loan(user, loan)` — the employee themself, or anyone in the first rule. Drives the profile card and the document download.
- Sidebar: a **"Loans"** submenu under Payroll + `ProtectedView` rows `payroll_loan_*` seeded on `MENU_GROUPS["payroll_ops"]` (the view enforces the finer pair rule, as every payroll view does). `seed_orbis --menus-only` on prod.

## 3. Data model (`payroll/models.py`)

### 3.1 `SalaryLoan` (`_Auditable`)

| Field | Type | Notes |
|---|---|---|
| `employee` | FK `CustomUser`, PROTECT, `related_name="salary_loans"` | |
| `file_reference` | char(50) | `HR/21092026/01`; unique together with `employee`? No — unique on its own (one file note = one loan). |
| `approved_on` | date | 21.09.2026 |
| `recorded_source` | choices `IN_ORBIS`, `OUTSIDE_ORBIS` | `OUTSIDE_ORBIS` for the ~100 cutover rows loaded by the uploader (§5.5) — the `EmployeeTaxDeclaration.submission_source` posture. |
| `approval_document` | FileField, `upload_to=salary_loan_document_path` | **Required for `IN_ORBIS` loans**; optional for cutover rows (a bulk sheet can't carry 100 scans — the list flags "no document" in red, like a document-less AIT, and HR attaches them over time). pdf/jpg/jpeg/png ≤ 5 MB (`_validate_ait_document`'s rule, generalised). Random filename under `payroll/salary_loans/<employee uuid>/<uuid4>.ext`, streamed only by `SalaryLoanDocumentView` — same reason as the AIT document: prod nginx serves `/media/` with no session check. |
| `principal` | `EncryptedDecimalField(12,2)` | 50,000 |
| `installment_count` | PositiveSmallInteger | 10 |
| `installment_amount` | `EncryptedDecimalField(10,2)` | 5,000. Form default = `ceil(principal / count)` in whole taka; HR may overtype. Must be ≤ principal. |
| `first_deduction_period` | FK `PayrollPeriod`, PROTECT, **nullable until disbursement** | HR *may* pick a month when recording the loan. If still blank when Finance marks the loan disbursed, it defaults to **the payroll named for the month after the disbursement month** (`payroll/periods.py::first_loan_deduction_month`, get-or-create the STANDARD row): month M's payroll (16/M-1 → 15/M) is the salary already being paid out when the money goes out in M. Disbursed 3 Mar → Mar 16–Apr 15 (April); disbursed 14 or 16 Sep → Sep 16–Oct 15 (October), never Aug 16–Sep 15 (Q71, refined 2026-10-08 after the first dev test). The engine compares by `start_date`, so a CUSTOM period still orders correctly. |
| `opening_recovered` | `EncryptedDecimalField(12,2)`, default 0 | **Cutover only**: instalments Finance already deducted before Orbis for a loan that is mid-way at go-live. Same posture as `EmployeeTaxDeclaration.opening_tds_deducted`. Payroll Admins only on the form. |
| `status` | choices `PENDING_DISBURSEMENT` → `ACTIVE`; `CANCELLED` (only from PENDING) | Human transitions only, every value reachable (CLAUDE.md: no choice nothing transitions into). **"Settled" is derived, not a status** — see §3.2. |
| `disbursed_on`, `disbursed_by`, `disbursement_note` | date / FK / char | Set by "Mark disbursed"; clears on no path. |
| `note` | text | The reason from the note. |

`clean()`: `installment_amount ≤ principal`; `installment_count ≥ 1`;
`opening_recovered ≤ principal`; `approval_document` required when
`recorded_source == IN_ORBIS`; `ACTIVE` requires `disbursed_on` and
`first_deduction_period`; a second
non-cancelled loan for the same employee is **allowed** (two loans deduct
two lines) but raises a form warning so HR notices.

### 3.2 Repayments are read from the register, not from a ledger

The August sketch had `SalaryLoanTransaction(reason=INSTALLMENT)` rows and
cached `total_recovered`/`installments_paid`. That assumed a `PAID` run
state to post them on. There isn't one yet, and `calculate_run()` is
delete-and-rebuild: a ledger row written at calculate time would be
duplicated by every Recalculate and would have to be reversed by hand.

So, exactly as the TDS true-up does for "TDS already deducted this year":

> **recovered_via_payroll(loan) = Σ `PayrollRunLine.amount` where
> `component_code == "LOAN_REPAYMENT"`, `source_ref == f"SalaryLoan:{pk}"`
> and the line's run is `CALCULATED`.**

> **outstanding = principal − opening_recovered − recovered_via_payroll −
> Σ adjustments.delta** (§3.3), computed in Python because every term is
> encrypted (no `Sum()`). Loans are rare (a handful live at any time), so
> one `values_list` over the lines for the loans on screen is the whole
> cost.

A loan is **settled** when `outstanding ≤ 0`. The engine skips settled
loans, the list shows a "Settled" badge/tab, and nothing has to flip a
status. Recalculating an old run changes `outstanding` the same way it
changes the tax YTD — Q66's convention holds: don't recalculate a paid run.

### 3.3 `SalaryLoanAdjustment` (append-only)

For everything that is **not** a payroll instalment: the employee repays
early in cash (`PREPAYMENT`), management forgives the remainder
(`WAIVER`), a correction (`CORRECTION`, signed either way). Shape is
`leave.LeaveBalanceTransaction` minus the instalment reason: `loan`
CASCADE, `occurred_on`, signed `delta` (`EncryptedDecimalField`; positive =
reduces outstanding), `reason`, `note`, `created_by`, `created_at`. Never
edited or deleted from the UI — a mistake gets a second row.

### 3.4 `SalaryLoanSkip` — "skip this month" (Q68)

The default is to deduct what fits and carry the rest forward. Finance
also wants a deliberate pause: the employee asks, HR approves, a Payroll
Admin records it. One row per `(loan, period)`, unique: `loan` CASCADE,
`period` FK PROTECT, `reason` (required), `approved_by_hr` — the HR person
who approved, FK `CustomUser` — plus `_Auditable` for who recorded it and
when. The engine deducts **nothing** for that loan in that period and
writes the WARNING `LOAN_INSTALMENT_SKIPPED` so the register still shows
why the column is empty. Skipping never changes `outstanding`; the
schedule simply stretches by a month. Only periods not yet calculated (or
the current recalculable one) may be skipped; deleting a skip is allowed
while its period is still open, otherwise it stays as history.

### 3.5 `PayComponent` seed

`("LOAN_REPAYMENT", "Loan repayment", "Loan", DEDUCTION, COMPUTED,
is_taxable=False, is_prorated=False, affects_gross=False, sequence=97)`
added to `seed_payroll_components.py` (update-or-create, safe on prod).
Not taxable, so the true-up projection (which sums `EARNING` lines only) is
untouched by construction.

### 3.6 Migration

Three new tables with a `CustomUser` FK → copy `_match_existing_collation`
and set `atomic = False` (CLAUDE.md "MySQL collation" rule). No data
migration.

## 4. Engine (`payroll/engine.py`)

In the once-per-run block, next to the tax inputs:

1. `active_loans` = `SalaryLoan` rows for `employee_ids` with
   `status=ACTIVE` and `first_deduction_period__start_date ≤ period.start_date`,
   `select_related("first_deduction_period")`.
2. `recovered_by_loan` = one `values_list("source_ref", "amount")` over
   `PayrollRunLine` for those loans across every **other** `CALCULATED`
   run (any period, any tax year — a loan can straddle June/July), summed
   in Python. Plus each loan's adjustments, summed the same way.

Per employee, replacing the no-op comment (after `tax_deducted` is final,
because the cap needs it):

```
for loan in loans_for(employee):
    outstanding = loan.principal - loan.opening_recovered - recovered - adjustments
    if outstanding <= 0: continue                     # settled
    if (loan.pk, period.pk) in skips:                 # Q68 override
        warning LOAN_INSTALMENT_SKIPPED; continue
    leaving = employee.last_working_date and period.start_date <= lwd <= period.end_date
    instalment = outstanding if leaving else min(loan.installment_amount, outstanding)
    available = gross_earnings - total_deductions - tax_deducted
    if instalment > available:
        warning LOAN_INSTALMENT_REDUCED (LOAN_OUTSTANDING_AT_SEPARATION when leaving)
        instalment = max(available, 0)
    if instalment > 0:
        line LOAN_REPAYMENT, amount=instalment, full_amount=loan.installment_amount,
             source_kind="SalaryLoan", source_ref=f"SalaryLoan:{pk}",
             calc_note="instalment 4 of 10 · outstanding after 30,000"
                       / "final payroll · full balance 30,000 recovered"
        total_deductions += instalment
```

- Whole taka: `installment_amount` is already whole from `TakaField`; the
  last instalment is whatever remains (50,000 ÷ 12 → 11 × 4,167 + 4,163).
- **Partial instalment, not postponement (Q68).** Deduct what fits, warn,
  carry the rest forward; `outstanding` stays the single source of truth
  and `installment_count` is the agreed plan, not a hard stop. A deliberate
  pause is a `SalaryLoanSkip` row (§3.4), never an engine guess.
- `NEGATIVE_NET_PAY` can therefore never be caused by a loan.
- **Leaver (Q69): the whole balance comes out of the last payroll** — the
  period containing `last_working_date`. If the final net can't cover it,
  Orbis takes what it can and raises `LOAN_OUTSTANDING_AT_SEPARATION`
  (WARNING) with the shortfall, for HR to recover in the manual settlement.
- `PENDING_DISBURSEMENT` loans are ignored (money not yet paid out); a
  loan disbursed after the run was calculated shows up on Recalculate,
  the same "stale run" shape as overtime approvals. A WARNING
  `LOAN_DISBURSED_SINCE_CALCULATION` is **not** added — the loan list
  shows "disbursed, not yet deducted" instead, since control checks are
  regenerated by calculate anyway.
- `breakdown`/`inputs_json`: the loan ids and instalments go into
  `re.inputs_json["loans"]` so the row explains itself.

Register, export, totals: **no change.** `_extra_deduction_components`
already adds one named column per DEDUCTION component present, so a run
with any instalment grows a "Loan" column on screen and in the Excel.

## 5. Screens

### 5.1 `/payroll/loans/` — `SalaryLoanListView`

`page-fill` + `table-simple table-sticky` + pagination, the overtime list's
layout. Tabs: **Active** (default) · **Awaiting disbursement** · **Settled**
· **Cancelled**. Columns: Employee (ID, name, department), File ref ·
Approved on, Principal, Instalment (`5,000 × 10`), Recovered / Outstanding
(bar or `15,000 / 35,000`), First deduction, Next: "Oct 2026 payroll" or
"Settled 2027-07", Status badge, actions. Company-wide by design (the
`payroll_salary` posture — does not use `accounts.filters`); `?q=` matches
employee ID / name / file reference. Money columns need
`can_manage_salary_loans` (the view is already gated on it).

A row expands (htmx) to its **history**: every instalment line (period,
run, amount, note) and every adjustment, newest first — the audit trail
Finance asks for when an employee disputes a balance.

### 5.2 Drawers

- **Add / Edit** (`_loan_drawer.html`, `SalaryLoanForm`): employee
  (company-wide search endpoint — a loan-scoped twin of
  `OvertimeEmployeeSearchView`, since that one is managed-scope),
  file reference, approved on (Flatpickr), approval document, principal
  (`TakaField`), instalments, instalment amount (`TakaField`, defaulted
  client-side from principal ÷ count), first deduction month (a month
  select, 12 months around today), note, and for Payroll Admins "Recovered
  before Orbis". `hx-target="#drawer-body"` so validation errors render
  inside the drawer (the 2026-10-07 fix).
  After the first calculated instalment exists: `principal`,
  `first_deduction_period` and `opening_recovered` go read-only (changing
  them would reprice history); instalment amount, document and note stay
  editable (Finance restructures forward).
- **Mark disbursed**: date (default today) + note; sets `ACTIVE` and, when
  HR left it blank, `first_deduction_period` = the payroll after that
  date's month, shown back as "First instalment: October 2026 payroll (Sep 16 –
  Oct 15)" so Finance sees what they just committed to. Only from
  `PENDING_DISBURSEMENT`.
- **Skip a month**: period (next open/current only), HR approver, reason.
  Payroll Admins. Listed in the loan's history with a "skipped" marker.
- **Add adjustment**: reason, amount, occurred on, note. Payroll Admins.
- **Cancel**: only while `PENDING_DISBURSEMENT`, reason required. An
  active loan is never cancelled — it is waived (adjustment) or left to
  settle.

### 5.3 Employee profile — "Loans" card

Read-only (not a `SectionSpec`, which is form-driven): one line per
non-cancelled loan — `Loan 50,000 · approved 2026-09-21 · 3 of 10
instalments deducted · outstanding 35,000 · document`. Visible to the
employee and to `can_manage_salary_loans`. No edit here; HR edits from the
list. Document link → `payroll_loan_document` (streams with
`can_view_salary_loan`).

### 5.4 Django admin

`SalaryLoanAdmin` with `PayrollMoneyAdminMixin` (money fields visible to
superuser + Payroll Admins), adjustments as a read-only inline, status not
editable there except by superuser. Rescue path only.

### 5.5 Cutover uploader (Q72 — about 100 live loans)

New loans still arrive one at a time on paper and go in through the
drawer. The live set at go-live is far too big to type, so an Upload
Center card **"Salary loans (cutover)"**, Payroll Admins, same
preview/apply/template shape as "Tax-year openings"
(`payroll/upload.py::parse_salary_loan_upload` / `apply_salary_loan_upload`
/ `build_salary_loan_xlsx_template`, `PayrollInputBatch.InputKind.SALARY_LOAN`):

| Column | Required | Notes |
|---|---|---|
| Employee ID | yes | unknown ID = row error |
| File reference | yes | duplicate in sheet or already in Orbis = row error |
| Approved on | yes | date |
| Principal | yes | whole taka |
| Instalments | yes | count |
| Instalment amount | no | blank = `ceil(principal ÷ instalments)` |
| Disbursed on | yes | these are live loans — row lands `ACTIVE` |
| First deduction month | no | `YYYY-MM`; blank = the payroll after Disbursed on's month |
| Recovered before Orbis | yes | Finance's recovered-to-date figure; may equal 0 |
| Note | no | |

Rows land `recorded_source=OUTSIDE_ORBIS`, document optional (§3.1).
Re-uploading a file reference already in Orbis **corrects** `Recovered
before Orbis` and the note only (the SLA uploader's correct-not-duplicate
rule) — never principal/instalment once an Orbis instalment exists.

## 6. Tests

- **Formula** (`payroll/tests_loan.py`): default instalment rounds up;
  last instalment is the remainder; `outstanding` derivation with opening
  + adjustments; settled at exactly zero; two loans on one employee.
- **Engine**: 3-period sequence recovers 15,000 and the fourth run shows
  the right `calc_note`; recalculating a period does not double-count;
  pending-disbursement loan deducts nothing; `first_deduction_period`
  honoured, and defaulted correctly for a 14 Sep vs 16 Sep disbursement;
  instalment reduced to available net with the warning and the shortfall
  recovered next period; a `SalaryLoanSkip` deducts nothing and warns;
  leaver's full balance taken in the LWD period, shortfall warned; register
  grows a "Loan" column and the export matches.
- **Uploader**: template round-trips; unknown ID / duplicate reference /
  bad month are row errors; re-upload corrects opening only.
- **Guards**: `core/tests_encryption.py` registry (new encrypted columns,
  no `Sum()`), `core/tests_money.py` template scan (`|taka` on
  `principal`/`installment_amount`/`outstanding`), `TakaField` on the
  form.
- **Permissions/views**: supervisor in "Payroll" gets 302/403; HR-only
  user cannot reach the list; employee sees own card and document, not a
  colleague's; invalid form re-renders inside `#drawer-body` with 400.

## 7. Docs + rollout

- `docs/PAYROLL_PLAN.md`: dated section recording the deviation from
  §"Loans" (no instalment ledger, derived outstanding, no cached totals,
  three-state status); stage 8 of the engine table becomes "built".
- `docs/PAYROLL_PROCESS.md` §12: Q67–Q72 (§8 below) in a "Resolved
  (2026-10-xx, salary loans)" block once answered; §4 roles table row
  "Salary loans and advances" renamed "Salary loans" → HR records, Finance disburses, engine
  deducts.
- `docs/PLAN.md` "Not built" line: drop `loans (SalaryLoan)`.
- `CLAUDE.md` payroll bullet: one sentence on loans.
- Deploy: `update_orbis.sh` (migration), then `seed_payroll_components`
  and `seed_orbis --menus-only`; Finance fills the cutover sheet from their
  recovered-to-date figures and a Payroll Admin uploads it; Finance checks
  the next run's "Loan" column against their own sheet.

Order: model + migration + component seed → engine + tests → list +
drawers + document view → cutover uploader → profile card → admin → docs.
One branch, roughly two days.

## 8. Decisions (2026-10-08, answered by the product owner after Finance)

| # | Question | Decision |
|---|---|---|
| 67 | Who marks a loan as disbursed? | **Payroll Admins** — Finance's payroll staff hold that group. |
| 68 | Net pay can't cover the instalment | **Deduct what fits, warn, carry the rest forward.** A deliberate pause is a separate, recorded act: the employee requests, HR approves, a Payroll Admin records a `SalaryLoanSkip` for that period (§3.4). |
| 69 | Employee leaves with a balance | **Take the whole balance from the last payroll** (the period containing the last working date), capped at available net with a warning for any shortfall. |
| 70 | Interest / tax treatment | **Interest-free, no fee, no tax effect.** Repayment is a non-taxable deduction after TDS. |
| 71 | First deduction | **Default = the payroll named for the month after the disbursement month** — refined 2026-10-08 on the first dev test: disbursed 3 Mar → Mar 16–Apr 15, because Feb 16–Mar 15 is March's own salary; disbursed 16 Sep → Sep 16–Oct 15. HR may pick a different month, before or after disbursement. |
| 72 | Live loans at cutover | **About 100**, Finance has each recovered-to-date figure → cutover uploader (§5.5), document optional on those rows. |
| 72b | Salary advances | **Not practised at SEBPO** (confirmed 2026-10-08) — no `loan_type` field at all; every row is a management-approved loan. Finance's SOP wording "loans and advances" is historical. |

## 9. As built (2026-10-08)

- `payroll/models.py`: `SalaryLoan`, `SalaryLoanAdjustment`, `SalaryLoanSkip`; `PayrollInputBatch.InputKind.SALARY_LOAN`. Migration `0028_salary_loans` (collation CONVERT, `atomic = False`).
- `payroll/loans.py`: `loan_balances()` (derived, §3.2), `decide_instalment()` (the one rule, §4), `loan_history()`.
- `payroll/engine.py`: loans loaded once per run; the stage sits after the TDS line; `LOAN_REPAYMENT` lines with `source_ref="SalaryLoan:<pk>"`; `re.inputs_json["loans"]`; control checks `LOAN_INSTALMENT_REDUCED` / `LOAN_INSTALMENT_SKIPPED` / `LOAN_OUTSTANDING_AT_SEPARATION` (WARNING) and `MISSING_LOAN_COMPONENT` (BLOCKING, if the seed was never run).
- `payroll/loan_views.py` + `templates/payroll/loan_*.html`: list with four tabs, add/edit, disburse, cancel, adjust, skip (+ undo while the period is open), history drawer, document stream, company-wide employee search. `/payroll/loans/`, sidebar "Loans" under Payroll.
- `payroll/upload.py` / `upload_views.py`: cutover uploader (§5.5) + Upload Center card "Salary loans (cutover)".
- `employees/views.py` + `templates/employees/sections/_salary_loans.html`: read-only profile card.
- `accounts/permissions.py`: `can_manage_salary_loans`, `can_disburse_salary_loans`, `can_view_salary_loan`.
- `seed_payroll_components` (LOAN_REPAYMENT), `seed_orbis --menus-only` (submenu + `payroll_loan_*` ProtectedViews).
- Tests: `payroll/tests_loan.py` (32).

**Deviations from the text above.** The list opens the history in a drawer rather than an expandable row (same shell as every other payroll drawer). "Undo skip" lives in the history drawer. The run's register and export needed no change, as predicted.
