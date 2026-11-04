# Tax calculation v2 — additional pay in the projection + year-to-date true-up

**Status:** CONFIRMED by the user on behalf of Finance 2026-10-07 (all six §10 questions answered as recommended; Q4 with the nuance noted there) and **IMPLEMENTED the same day** — `payroll/migrations/0027_tax_trueup.py`, `payroll/tax.py` (TRUEUP branch), `payroll/engine.py`, the tax-year openings uploader, `payroll/tests_tax_trueup.py`. Production rollout: §9.
Source: Finance's updated `Tax Calculation_ - Copy.xlsx` (sheet "2027-28", income
year 2026-27) and the 2026-10-07 meeting. The declaration half of that meeting
(allowable investment, confirm-and-lock, AIT) already shipped — see
`PAYROLL_PLAN.md` "Tax declaration becomes a once-a-year commitment, 2026-10-07".
This plan covers the two **engine** differences that were deliberately left out
of that change.

---

## 1. What Finance's sheet does that Orbis doesn't

Orbis (`payroll/tax.py`, 2026-08-24) projects a flat year from the current salary
and divides the annual tax by 12 every run. Finance's sheet differs in two places:

| Sheet | What it is | Orbis today |
|---|---|---|
| `C14` **Incentives** (5,000 in the example, "just an example") | **Every taxable payment on top of base salary**, accumulated through the tax year: overtime, night-shift allowance, special-shift transport, performance bonus, SLA bonus, top-performer award, and anything else paid as an extra line. Added to gross annual income before the exemption. | Not included — projection is salary + one month's bonus only. |
| `H34`–`H38` **AIT − TDS Deducted → ÷ months remaining** | `Yearly Deduction = Tax payable with return − AIT − TDS already deducted this year`; `Monthly Deduction = Yearly Deduction ÷ remaining months` (÷ 9 in October, after 3 months deducted). A **year-to-date true-up**: whatever changed since last month (more incentives, a salary revision, an AIT entry) is absorbed over the months left, not ignored. | `annual_tds ÷ 12`, no memory of what was already deducted. |

Everything else in the sheet already matches Orbis to the paisa (mobile/internet
tier, ×12, one-month bonus, ⅓-or-5-lac exemption, category thresholds, slabs,
3%/10%/7.5-lac rebate, 5,000 floor, source tax, AIT).

Why it matters: an employee with heavy overtime in Q1 is under-taxed all year under
the flat projection, and Finance then has to correct it by hand in the last months
(or at return filing). With the true-up, Orbis reproduces Finance's sheet every
month and the correction happens automatically and gradually.

---

## 2. The v2 formula

```
projection (unchanged):
  mobile_internet       = tier(gross_monthly)
  base_annual           = (gross_monthly − mobile_internet) × 12 + gross_monthly   # ×12 + one month's festival bonus
NEW:
  additional_pay_ytd    = opening_additional_pay                                   # pre-Orbis months of this tax year
                        + Σ taxable extra earnings in every earlier Orbis period of this tax year
                        + taxable extra earnings in THIS period (computed earlier in the same engine pass)
  annual_gross          = base_annual + additional_pay_ytd
unchanged:
  exempted              = min(annual_gross ÷ 3, 500,000)
  taxable_income        = annual_gross − exempted
  tax_before_rebate     = slabs(taxable_income, category threshold)
  rebate                = min(3% × taxable_income, 10% × declared_investment, 750,000)
  net_tax_payable       = max(tax_before_rebate − rebate, 5,000)
  tax_with_return       = net_tax_payable − source_tax
NEW:
  tds_already_deducted  = opening_tds_deducted + Σ tax_deducted in every earlier Orbis period of this tax year
  tax_remaining         = tax_with_return − ait_car_advance_tax − tds_already_deducted
  months_remaining      = 12 − (number of tax-year months before this period)     # Jul=12 … Jun=1; Oct=9 ✓
  monthly_tds           = max(tax_remaining, 0) ÷ months_remaining                 # negative → 0 + warning (over-deducted)
```

Check against Finance's example (gross 120,817, incentives 5,000, Men, AIT 25,000,
TDS deducted 1,943 × 3, October): taxable 1,027,621 → tax 79,143.15 → rebate
30,828.60 → net 48,314.55 → remaining 48,314.55 − 25,000 − 5,829 = 17,485.55 →
÷ 9 = **1,942.84**, the sheet's `H38`. This becomes the reference test.

**Deliberately kept from the sheet, even though a purist would do otherwise:** base
salary is still *current gross × 12*, not YTD actual salary. A mid-year raise
therefore still re-projects the whole year at the new rate. That is what Finance's
sheet does, and the true-up spreads the resulting correction over the remaining
months anyway.

---

## 3. What counts as "additional pay"

`PayComponent` already carries `is_taxable`. Rule: **every `PayrollRunLine` whose
component is `kind=EARNING` and `is_taxable=True`, except two codes:**

| Component | In the Incentives line? | Why |
|---|---|---|
| `GROSS_SALARY` | No | It *is* the base — annualised separately (×12). |
| `BONUS` (festival) | **No — question Q2** | The sheet already assumes one month's gross as "Bonus (50% of Gross salary)". Counting the actual Eid lines too would double-count. |
| `NIGHT_ALLOWANCE`, `OT_ALLOWANCE`, `SPECIAL_SHIFT_TRANSPORT` | Yes | Computed earnings — exactly the "Overtime, Night Shift allowance, Transport" the user named. |
| `INCENTIVE`, `SLA_INCENTIVE` | Yes | Entered earnings — performance / SLA bonuses. |
| any future EARNING component (e.g. top performer) | Yes, automatically | Falls under the rule the moment it is seeded with `is_taxable=True`; no engine edit. |
| `MEAL_DEDUCTION`, `TDS`, `DISCIPLINARY`, `TEST_SALARY_DEDUCTION` | No | Deductions, not income. |

A component Finance wants left out of tax (if any) is `is_taxable=False` in the
admin — a data change, not code.

---

## 4. Data model

**`TaxYear`**
- `calculation_method` gains `ITA_2023_SALARY_TRUEUP`. `tax.py` branches on it, so
  every historical run that was computed under the flat method still reproduces —
  the doc already reserved this field for exactly this ("a future Finance Act that
  changes the formula's *shape* lands as a new branch"). `seed_tax_year` sets
  2026-27 to the new method.
- `orbis_live_from` (FK → `PayrollPeriod`, nullable): the first period in this tax
  year whose TDS Orbis is authoritative for. Months before it come from the opening
  figures below. Blank = Orbis has run every month of the year (the normal case from
  FY2027-28 on).

**`EmployeeTaxDeclaration`** (already per employee per tax year — the natural home)
- `opening_tds_deducted` (encrypted, default 0): TDS deducted outside Orbis for
  this tax year's months before `orbis_live_from`.
- `opening_additional_pay` (encrypted, default 0): taxable extra pay in those same
  months.
- Both are Finance-supplied cutover facts, never recomputed, editable by Payroll
  Admins only (admin + the uploader in §6). They are **not** part of what the
  employee confirms — the confirm-and-lock covers the investment/AIT fields only;
  openings stay editable after lock.

**`TaxComputation`** (snapshot, delete-and-rebuild) adds `additional_pay_ytd`,
`tds_already_deducted`, `tax_remaining`, `months_remaining`; `breakdown_json` lists
the per-period rows that fed the two sums (period code + amount) for the audit
trail. No new tables.

---

## 5. Engine changes (`payroll/engine.py`, `payroll/tax.py`)

Resolved **once per run**, never per employee (the "~25 queries, not 25,000" rule):

1. Prior periods = `PayrollPeriod` rows in the same tax year with `end_date` before
   this period's, at or after `orbis_live_from`. For each, the `REGULAR` run with
   `status=CALCULATED` (one per period by the `(period, run_type)` constraint).
2. Load every `PayrollRunEmployee` of those runs → `{employee_id: Σ tax_deducted}`;
   load their taxable EARNING `PayrollRunLine`s (component in the §3 set) →
   `{employee_id: Σ amount}`. Money columns are encrypted, so both sums are in
   Python over loaded rows — same discipline as the register.
3. In the per-employee loop the current period's own taxable extra lines are
   already in `lines` before the Tax stage runs, so they are summed there and
   added. `TDSEntry` overrides are untouched (still win for *this* period) and are
   counted as "deducted" for later periods because they are the `tax_deducted` on
   that run's `PayrollRunEmployee`.
4. `compute_tax_for_employee()` stays pure; it gains keyword inputs
   `additional_pay_ytd`, `tds_already_deducted`, `months_remaining` and returns the
   new fields on `TaxBreakdown`.

New **control checks** (both WARNING — the run still completes, the Exceptions tab
shows them):
- `TAX_YTD_GAP` — a prior tax-year period at/after `orbis_live_from` has no
  CALCULATED regular run; that month's TDS and extras are treated as 0.
- `MISSING_TAX_OPENING` — `orbis_live_from` is later than the tax year's first
  period and the employee's declaration has zero opening figures (most likely the
  cutover upload missed them). Per employee.

There is no per-employee tax drawer in the register today (checked 2026-10-07), so
the YTD figures surface in two existing places instead: the TDS line's `calc_note`
(visible wherever run lines are shown — now reads `…, extras YTD=…, deducted YTD=…,
remaining=… over N month(s)`) and the `TaxComputation` admin / `breakdown_json`
(`ytd` block: prior periods used, openings, Orbis-period sums, current-period extra).
A register drawer and export columns remain a follow-up, not built here.

---

## 6. Cutover for FY2026-27

> **Superseded for FY2026-27 on 2026-10-08** by the month-level import in
> `PRE_ORBIS_PAYROLL_IMPORT_PLAN.md`: the "Pre-Orbis payroll months (cutover)"
> card takes each pre-Orbis month per employee and per pay component and
> *derives* the two opening fields below from those rows (same exclusion rule
> as the engine). The totals uploader described here still exists for a future
> year where Finance only has totals. Also note `BONUS` → `FESTIVAL_BONUS` in
> `TAX_EXCLUDED_EARNING_CODES` the same day (`PAYROLL_PLAN.md` dated section).

Orbis has not deducted TDS for July–September 2026 (and won't for October if go-live
slips). Finance already has those figures per employee in their sheet (`H35`
"TDS Deducted", and the incentives they have accumulated). One uploader, Upload
Center, Payroll Admins only, same preview → apply → template shape as the salary
uploader: columns **Employee ID · TDS deducted to date · Additional pay to date**,
writing the two opening fields on the 2026-27 declaration. Re-upload overwrites
(these are facts, not a ledger), logged under a `PayrollInputBatch`.

Then `orbis_live_from` is set on the 2026-27 `TaxYear` in the admin (e.g. the
Oct 16 – Nov 15 period) and the current run is recalculated. From FY2027-28 on the
openings are zero and `orbis_live_from` blank — nothing to upload.

---

## 7. Consequences worth stating up front

- **Monthly TDS now moves month to month.** That is the point, but payslip readers
  will ask. The TDS line's note and the `TaxComputation` row show *why* (extras added, months left).
- **Recalculating an old period changes later periods' YTD inputs.** Today nothing
  stops a Payroll Admin recalculating a CALCULATED run. Until the Phase 5 approval
  chain exists, the convention is "don't recalculate a paid period"; the
  `TaxComputation` snapshot of every later run still records the figures it used.
- **Over-deduction** (negative remainder — e.g. a large AIT entered late) clamps
  the monthly line to 0 with the existing warning. Refunds happen at return filing,
  outside payroll — unchanged posture.
- **Leavers**: `months_remaining` is calendar-based; a resignation does not shorten
  it. Final-month true-up belongs to the Phase 5+ settlement, not this change.
- **Projection vs. actual for the extras**: Finance's sheet carries the *actual*
  accumulated figure and lets the true-up absorb it — it does not forecast the
  remaining months' overtime. Orbis will do the same (Q1 below confirms).

---

## 8. Tests

- Finance's example reproduced to the paisa (above) — the reference test.
- `months_remaining` for each of the 12 periods (Jul → 12 … Jun → 1).
- Three consecutive periods on one employee: flat months produce the same TDS as
  today's engine; a month with 30,000 overtime raises the following month's TDS by
  exactly the extra tax ÷ months left; a later AIT entry lowers it.
- `TDSEntry` override in period N is counted as deducted in period N+1.
- `TAX_YTD_GAP` and `MISSING_TAX_OPENING` fire and clear as described.
- Opening uploader: preview/apply/overwrite, unknown Employee ID skipped, Payroll
  Admins only.
- Historical method: a `TaxYear` left on `ITA_2023_SALARY` still computes exactly
  as the existing `tests_tax.py` expects (those tests pin the old branch).

---

## 9. Rollout

1. Deploy; `migrate payroll`; `seed_tax_year` (sets 2026-27 to the true-up method).
2. Finance fills the opening template for every employee; Payroll Admins upload it.
3. Set `orbis_live_from` on 2026-27 in the admin; recalculate the open run.
4. Compare the register's TDS column with Finance's sheet for a sample of
   employees before the first real payout — the reconciliation step
   `PAYROLL_PROCESS.md` §13 already requires.

Built 2026-10-07. One deviation from §5 worth knowing: `TAX_YTD_GAP` is detected on
**calendar months**, not `PayrollPeriod` rows — a month nobody ever ran has no period
row to find (`PayrollPeriod.for_month` creates rows on demand). One deviation from §2:
the allowable-investment snapshot stays salary-only (taken in July when extras are 0),
whereas Finance's October sheet had the 5,000 incentive inside the figure it typed —
the reference test pins the sheet's 308,286 explicitly to reproduce 1,943.

---

## 10. Questions confirmed with Finance (2026-10-07)

All six were answered as recommended. **Q4 nuance:** Finance will try to upload the exact July–September payments; if they can't assemble the full figures they will upload only the TDS deducted for those three months — so the uploader's "Additional pay to date" column is optional and blank counts as 0.

| # | Question | Recommended answer (built this way unless told otherwise) |
|---|---|---|
| Q1 | Is "Incentives" the **actual accumulated** extra pay so far this year (including the current month), or a forecast for the whole year? | Actual to date, including the current month — that is what the sheet carries and what the true-up is for. |
| Q2 | The sheet assumes one month's gross as festival **Bonus**. When the real Eid bonus is paid through payroll (`BONUS` component), is it excluded from Incentives to avoid double-counting — and does the assumption stay even for staff under the 180-day tenure gate who receive no bonus? | Exclude `BONUS` from Incentives; keep the one-month assumption for everyone, exactly as the sheet. |
| Q3 | Special-shift **transport** is taxable extra pay? (`is_taxable=True` today.) | Yes — "all additional pay". |
| Q4 | Finance will supply **per-employee TDS deducted and additional pay for July → go-live** (the `H35` figure and the incentives total) for the cutover upload. | Yes, one sheet, Employee ID keyed. |
| Q5 | If the remainder goes **negative** (over-deducted), the monthly deduction is 0 and the refund is handled at return filing, not through payroll. | Yes. |
| Q6 | Deduct the monthly figure in **whole taka** (the sheet shows 1,942.84; payslips show whole taka)? | Round half-up to whole taka in the engine so the deducted line equals what the payslip prints; the YTD sum then stays exact. |
