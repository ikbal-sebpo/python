# Certificate of deduction of tax from salary — challan membership, issue list, PDF

**Status:** PROPOSED 2026-10-08, **waiting on Finance** (§9, answers expected the
week of 2026-10-12). Builds on `PRE_ORBIS_PAYROLL_IMPORT_PLAN.md` (built 2026-10-08): the monthly tax ledger (`monthly_ledger_rows`), `TaxChallan`,
`LegacyPayrollMonth`, the `FESTIVAL_BONUS`/`SALARY_ADJUSTMENT` (né `ARREAR`) components. Nothing in this file
is on the October 2026 run's critical path.
Source: Finance's sample `Salary Certificate_EID-539.pdf` (income year 2025-26,
assessment year 2026-27) and the user's reviews of 2026-10-08.

---

## 1. What the certificate is

Each July, Finance hands every employee a one-page letter the employee attaches
to their income-tax return. The sample has three parts:

1. **Header** — reference number `SEL/F&A/SC/IY-2025-2026/539/315` (company /
   department / "Salary Certificate" / income year / Employee ID / running serial),
   issue date, employee name, Employee ID, e-TIN, designation, income year,
   assessment year.
2. **Income table** — Basic / House Rent / Medical / Conveyance / Other Allowances
   summing to **Gross Salary**, then **Mobile & Internet Bill (Reimburse)** with a
   6th-Schedule remark, **Festival Bonus**, **Incentives**, **Other Benefits**, and
   a **Total Salary** line.
3. **Challan table** — every treasury deposit the company made in the income year
   (challan number, date, bank, *total* challan amount) with the employee's own
   share in a "Related Tax Amount" column, zero where that deposit carried nothing
   for them, and the employee's annual TDS as the total.

Two facts in the sample shape the design:

- **The component split is arithmetic, not data.** Basic = 50% of gross, House
  Rent 25%, Medical 5%, Conveyance 10%, Other 10% — exactly, to the taka. Orbis
  stores gross only (locked 2026-08-11) and that is enough: the split is applied
  to the annual gross at certificate time from a configurable percentage table.
- **Challans are company-level facts, allocated per employee.** 14 challans for
  12 payrolls (January has three, 30 June carries a year-end deposit), rows with
  `000`. Finance confirmed 2026-10-08: a run's TDS may be deposited under several
  challans, even at different banks, but **each employee's tax for that run is in
  exactly one of them.**

Everything the certificate prints is a read over the monthly tax ledger (import
plan §1) plus the challan register — there is no certificate-specific money
anywhere except the frozen copy of what was issued (§3.3).

---

## 2. Scope

| Piece | Needed by |
|---|---|
| A. Challan membership for Orbis runs — "Record challans" drawer, Tax Challans page (§4) | the first Orbis run's deposit, ~mid-November 2026 |
| B. Certificate build + PDF + issue list + self-service download (§5) | before the first mid-year leaver asks; in any case before July 2027 |

---

## 3. Data model (additions to the import plan's)

### 3.1 `TaxChallanMember` — which challan carried an employee's tax

> **Superseded (built 2026-10-09):** `PRE_ORBIS_PAYROLL_IMPORT_PLAN.md` Revision 2 §R2.3 replaces this table with **`TaxChallanAllocation`** — (challan, employee, **stored amount**), uploaded per month by Payroll Admins from a new "TDS by challan" Upload Center card, for pre-Orbis months and Orbis runs alike. The "amount is never stored" rule below is reversed there (a deposit is a historical fact; a later recalculate surfaces as a render-time warning), which also answers Q70's remainder without `extra_amounts`. Piece A's membership sheet (§4) *is* that card; the "Record challans" drawer shrinks to a convenience.

`challan` FK, `employee` FK, unique together; `run` denormalised from the challan
so the DB enforces **unique `(run, employee)`** — an employee can't be in two
challans of the same run. The *amount* is never stored: for an Orbis run it **is**
`PayrollRunEmployee.tax_deducted` on `challan.run`, read at render — the
"derive, don't cache" posture of `loans.py::loan_balances` and the true-up's
`tds_already_deducted`. A recalculate after the deposit can't leave a stale amount
behind; it can only make a challan's Σ of members' tax differ from its typed total,
which §4 shows as a warning. Legacy months need no membership row — their
`LegacyPayrollMonth.challan` FK already says it.

Open (Q70, remainder): a deposit tied to **no** run (the sample's 30 June row, if it
was one) would need per-member amounts — a `TaxChallan.extra_amounts` encrypted
JSON (employee → amount) filled by the same membership sheet with an optional
`Amount` column. Not built unless Finance says such deposits exist.

### 3.2 `PayrollSettings` additions

Five percentage fields (`cert_basic_pct` 50, `cert_house_rent_pct` 25,
`cert_medical_pct` 5, `cert_conveyance_pct` 10, `cert_other_allowance_pct` 10;
must sum to 100, validated) and `tax_certificate_signatory` (FK `CustomUser`, the
Finance manager). Company policy, not tax law, so it lives beside
`payable_days_basis` rather than on `TaxYear`. Pending Q67.

### 3.3 `TaxCertificateIssue` — the issued letter

| Field | Notes |
|---|---|
| `tax_year` FK, `employee` FK | unique together among non-superseded rows |
| `serial` | running number per tax year (the `/315`), `PayslipCounter`-style, allocated under `select_for_update` on first issue |
| `reference_number` | rendered once, stored, never recomputed (format per Q72) |
| `issued_at`, `issued_by`, `signatory` FK + `snapshot_json` (name/title/phone/email at issue) | |
| `figures_json` (encrypted) | the exact income table + challan rows the PDF was built from, incl. the split percentages used |
| `pdf` FileField | `documents/tax_certificates/<random>.pdf`, streamed only through a view — prod nginx serves `/media/` openly (same reason `ait_document` has `payroll_tax_declaration_document`) |
| `superseded_by` FK self (nullable) | a correction is a new serial, never an edit |

Built fresh for preview, **frozen on issue**: the Service Book precedent (always
recomputed) plus the salary-certificate precedent (what was handed out is kept),
because this document is quoted to the NBR and must be reproducible.

---

## 4. Piece A — challan membership for Orbis runs

- **Already built by the import plan (2026-10-08):** the permanent "Tax challans"
  Upload Center card records each deposit with an optional payroll month that links
  it to the month's calculated run (`TaxChallan.run`). What remains here is the
  **page** `/payroll/challans/?tax_year=` under the Payroll section (new submenu
  "Tax Challans"; `payroll_ops` group gate, writes narrowed by a new
  `accounts.permissions::can_record_tax_challan` = Payroll Admins + Finance) listing
  every challan of the year: bank, total, linked run, member count, Σ members' tax,
  difference — and the membership below.
- **"Record challans" drawer on a CALCULATED run** (register header, next to the
  stale-overtime warning): one or more rows of challan number, date, bank, total;
  `run` set automatically. Shows the run's `tax_total` beside Σ typed totals and
  warns on a difference — not a block.
- **Membership:** one challan → auto-assign every employee with `tax_deducted > 0`.
  Several → an `Employee ID | Challan No` sheet (template pre-filled with every
  taxed employee and a blank challan column) or the two columns pasted into a
  textarea. Preview per challan: members, Σ tax, typed total, difference (warning);
  taxed employees in no challan are listed — a blocker for *the certificate*, not
  for saving (Finance may record the deposits before finishing the split).
- **Recalculate after challans are linked** stays allowed (Phase 5's approval chain
  is what will freeze a run); the run detail shows a `badge-warning` "Challans
  recorded on <date> — TDS has changed since" when `tax_total` ≠ Σ challan totals,
  and the membership list flags anyone whose tax went to 0 or who newly has tax and
  no challan. Computed at render, like `overtime_changed_since_calculation`.
- `PayrollRun` gets no new column; the link lives on `TaxChallan.run`.

---

## 5. Piece B — the certificate

### 5.1 `payroll/tax_certificate.py::build_certificate_context(employee, tax_year)`

Pure function, no writes, same shape as
`employees/services.py::build_service_book_context`. Income rows are sums over the
year's monthly ledger rows (`tax_ledger.monthly_ledger_rows`, one per payroll month,
Orbis or legacy alike), bucketed by component code through one dict —
`CERTIFICATE_ROWS = {"FESTIVAL_BONUS": {"FESTIVAL_BONUS"}, "INCENTIVES": {...},
"OTHER_BENEFITS": {...}}` — the single place Q71's answer lands, changeable later
without touching any imported data.

| Certificate line | Source |
|---|---|
| Name, Employee ID, e-TIN | `CustomUser`, `IdentificationNumber(TIN)` — missing TIN is a listed blocker (§5.3) |
| "as *Manager*" | the `DesignationHistory` row covering the tax year's end (or LWD if earlier); fallback `CustomUser.designation` |
| his/her | `PersonalInfo.gender`; "their" when blank |
| Income year / assessment year | `TaxYear.code` and the following year |
| **Gross Salary** | Σ `GROSS_SALARY` (+ Σ `SALARY_ADJUSTMENT`, pending Q75), **minus** Σ Mobile & Internet reimbursement only if Q68 says the reimbursement is inside the stored gross |
| Basic / House Rent / Medical / Conveyance / Other | §3.2 percentages on that annual gross; rounding remainder to Basic so the five rows add to the printed gross |
| Mobile & Internet (Reimburse) | Σ the ledger's reimbursement column |
| Festival Bonus | Σ `FESTIVAL_BONUS` |
| Incentives / Other Benefits | per `CERTIFICATE_ROWS`, seeded from Finance's own sheet (Q71, Option C); any taxable component not named anywhere defaults to Other Benefits |
| Total Salary | sum of the rows above |
| Challan rows | every `TaxChallan` of the tax year, chronological; share = `tax_deducted` on `challan.run` when a `TaxChallanMember` row exists, else the legacy month rows pointing at it, else 0 |
| Total | Σ shares; **must equal** Σ `tax_deducted` over the year's runs + Σ legacy `tds_deducted` — a mismatch is a blocker, never silently printed |

Money through `|taka` in the template (`core/tests_money.py` scans it) and
`format_taka` in Python.

### 5.2 PDF

DB-stored `TemplateDesign` + `MessageTemplate` seeded by
`documents/management/commands/seed_tax_certificate_template.py`, the Service Book
arrangement (a `DocumentType` never used to create a `DocumentRequest`; rendered via
the in-memory unsaved-request technique `documents.pdf.test_render_pdf` uses).
Letterhead **on** — a company letter, unlike the Service Book. Signatory block from
`snapshot_json`. No QR verify code in v1; the `/verify/<uuid>/` plumbing exists if
Finance wants it later.

### 5.3 Pages and permissions

- **`/payroll/tax-certificates/?tax_year=`** — Payroll Admins + the HR∧Payroll
  pair (new `can_issue_tax_certificate`, same audience as
  `can_manage_locked_overtime`). One row per employee active at any point in the
  year: TIN present?, months covered (runs + legacy) vs. expected, challan coverage,
  issued serial/date. Actions: **Preview** (fresh HTML, nothing stored), **Issue**
  (freezes §3.3 row + PDF; bulk-selectable like the attendance-corrections
  toolbar), **Re-issue**. A **blockers** column (no TIN, a run month with no
  challan membership, a missing legacy month, share total ≠ TDS total) disables
  Issue while any exists — the one hard gate, because the output is a statutory
  document.
- **Employee self-service** (pending Q73): a "Tax certificate" card on the
  profile's payroll tab beside the tax declaration card, listing issued years with
  a download; never a preview. A leaver can download through their last day
  (`LastWorkingDateMiddleware` posture); after that HR emails it from the list
  page's "Send" action via `core.tasks.defer()`.
- **Leavers** (pending Q74): Issue is available per employee at any time, so a
  November leaver gets a certificate covering July–LWD; the serial continues the
  same per-year counter.

---

## 6. Consequences worth stating up front

- **The component split is a presentation rule.** Nothing in payroll starts
  computing Basic/House Rent; the 2026-08-11 gross-only decision stands. If Finance
  ever needs per-employee splits, that is a `SalaryStructure` change recorded as a
  reversal in `PAYROLL_PLAN.md`, not a certificate tweak.
- **A challan is recorded once, when it happens.** Finance's current practice of
  reconstructing 14 challans per employee every July disappears; the certificate
  is a read of the register.
- **Issued certificates are immutable.** Correcting one is a new serial — stricter
  than the rest of payroll (delete-and-rebuild) because the document leaves the
  company.

---

## 7. Build order and estimate (after the import plan has shipped)

1. `TaxChallanMember`, `PayrollSettings` fields, `TaxCertificateIssue` + migrations
   (collation step) + admin — half a day.
2. Piece A: challans page, run drawer with multi-challan membership sheet, stale
   badge, `can_record_tax_challan`, tests — one day.
3. Piece B: context builder with a reference test against the sample's figures
   (1,322,008 → 661,004 / 330,502 / 66,100 / 132,201 / 132,201; 36,000; total
   1,467,842; TDS 39,364 across 14 challans), template seed, list page, bulk issue,
   self-service card, tests — one and a half days.
4. Docs: this file's status line, `PAYROLL_PLAN.md` pointer, `PAYROLL_PROCESS.md`
   §12 Q67–Q76 answers, CLAUDE.md payroll paragraph.

---

## 8. Alternatives considered

- **Importing the three legacy months as `PayrollRun` rows of a new run type.**
  One code path everywhere, and the register would show them — but every
  run-shaped invariant (control checks, variances, `loan_balances`, the true-up's
  `prior_runs` filter) would have to learn the new type, for three months that never
  recur. The dedicated `LegacyPayrollMonth` table behind the same
  `monthly_ledger_rows` function gives the one-code-path benefit without the blast
  radius.
- **Storing the per-employee challan amount.** Rejected for Orbis runs: it would
  need re-syncing on every recalculate, which is exactly the duplication the loan
  and true-up designs avoid.

---

## 9. Questions for Finance (Q67–Q76, continuing `PAYROLL_PROCESS.md` §12)

| # | Question | Recommended answer |
|---|---|---|
| 67 | Is the Basic 50 / House Rent 25 / Medical 5 / Conveyance 10 / Other 10 split fixed for every employee, every year? | Yes, company-wide, held in `PayrollSettings`; snapshotted per issued certificate. |
| 68 | **How does the certificate's "Gross Salary" relate to the gross Orbis stores, and where does the Mobile & Internet figure come from?** Orbis stores one gross per employee (the figure the festival bonus is 50% of). The engine, copying Finance's tax sheet, treats a tier amount of that gross (3,000 for 80k–110k) as non-taxable reimbursement and projects tax on the remainder. On the sample, Gross Salary 1,322,008 ≈ 12 × the 2025 stored gross (1,318,008) and the 36,000 is listed *on top* — reading as "stored gross, reimbursement shown additionally", not "stored gross minus reimbursement". Whichever it is, Total Salary − the exempt Mobile row must equal the taxable income the tax sheet used, or the certificate and the TDS disagree by the reimbursement. **The three months Finance fills in the import plan's sheet answer this by construction** — the per-month Gross salary and Mobile cells *are* the definition. | Read it off the uploaded months; subtract the reimbursement from Gross Salary only if Finance says it is inside the stored gross. |
| 69 | ~~Festival vs performance bonus~~ **Answered 2026-10-08** — both exist; `FESTIVAL_BONUS` is seeded and the true-up exclusion moves to it (import plan §5.1). `BONUS` (performance) is bucketed per Q71. | — |
| 70 | ~~One challan per run?~~ **Answered 2026-10-08** — several per run, each employee in exactly one (§3.1). ~~Still open: the 30 June 2026 deposit~~ **Moot since 2026-10-09:** `TaxChallanAllocation` stores the amount, so a deposit tied to no month is just allocations under a challan with no payroll month — no `extra_amounts` path needed either way. | — |
| 71 | **Which components go to "Incentives" and which to "Other Benefits"?** Both rows are fully taxable, so the split only changes how the letter reads. Example month — OT 4,500, night duty 3,500, special-shift transport 1,200, SLA incentive 2,000, performance bonus 5,000: Option A: Incentives = performance-linked (SLA + performance bonus) 7,000, Other Benefits = working-condition allowances 9,200; Option B: everything variable under Incentives 16,200; **Option C (user's pick 2026-10-08): copy whatever Finance's current sheet does.** Needed: one real certificate or working sheet where those rows are non-zero — the sample has `000` in both. | Option C; seed `CERTIFICATE_ROWS` from Finance's sheet, unlisted taxable components → Other Benefits. |
| 72 | Reference number: is the trailing serial a running count of certificates issued in that income year, company-wide? Who signs — always the Manager (Finance & Accounts)? | Per-year counter; signatory configured in `PayrollSettings`, snapshotted on issue. |
| 73 | May employees download their own issued certificate from Orbis, or does Finance hand it out? | Self-service download once issued; Finance still controls *when* by issuing. |
| 74 | A leaver mid-year: issued at separation (part of the clearance checklist) or at year end? | At separation, by the HR final clearance step — add it to the offboarding auto-issue list next to the release letter. |
| 75 | **Arrears on the certificate:** is a salary arrear (import plan §5.2) part of "Gross Salary" (and therefore split 50/25/5/10/10), or its own line / "Other Benefits"? | Gross Salary — an arrear is salary for earlier months paid late; taxed in the year received either way. |
| 76 | **Arrear instalments:** is "3 instalments from the first payroll after the increment is approved" the standing rule, or decided per year? Do arrears follow proration (a mid-January joiner gets a part-month January arrear)? | Standing default of 3, overridable per increment batch; prorated exactly as the engine would have paid the month. |
