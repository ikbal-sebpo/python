# Salary adjustment — the balance owed after a retroactive salary change, settled N months at a time

**Status:** BUILT 2026-10-09 (user's request the same day, after the first pre-Orbis upload).
Code: `payroll/adjustments.py`, the engine hook after variable pay in `payroll/engine.py`,
`PayrollRun.adjustment_months` / `adjustment_settle_all`, `PayrollRunEmployee.adjustment_months`,
`LegacyPayrollMonth.adjustment_months` (+ the months sheet's "Salary adjustment months" column),
the same-date correction branch in `services.supersede_salary_structure`, the "Salary adjustment
this run" control on the run page (`PayrollRunDetailView.post`, action `adjustment`),
`payroll/tests_salary_adjustment.py`. Migration `payroll/0033`. Supersedes
`PRE_ORBIS_PAYROLL_IMPORT_PLAN.md` §5.2's "deferred follow-up".

## 1. The fact pattern

SEBPO's yearly increment takes effect 1 January but is first *paid* in the August payroll; the
January–July difference is then paid as a "salary adjustment" in instalments the company decides
payroll by payroll — in 2026, three months' worth on 3 October, three more on 1 November, the last
one on 1 December. Meanwhile employees may ask for a *revision* of the same increment (say 120,817
→ 130,000, still effective 1 January) after an instalment has already been paid; the difference
then widens for every month since January, including the ones already settled.

The user's question (2026-10-09): *how do we calculate the next payroll's adjustment after such a
revision?* — and the decision: **the Payroll Admin picks how many months each run settles; Orbis
derives the taka.**

## 2. The rule — one running balance, derived, never typed

For every employee with a current (APPROVED, open-ended) `SalaryStructure`, and every payroll
month from the period the structure's `effective_from` falls in (`periods.period_for_date` — the
period *named* for the month, SEBPO's full-month practice, not the engine's day-level proration)
up to the month before the run being calculated:

```
owed(month) = current gross × that month's proration − gross actually paid that month
```

where "gross actually paid" is read, in order, from the CALCULATED regular Orbis run for the month
(its `GROSS_SALARY` lines, proration = `PayrollRunEmployee.proration_factor`), else the imported
`LegacyPayrollMonth` gross line, else — **for a month Orbis has no record of** — assumed paid at
the *previous* structure's rate (the SUPERSEDED row ending the day before the current one
starts), flagged by a `SALARY_ADJUSTMENT_ASSUMED_PAID` warning on the run. Months with no
shortfall are not "owed months". Then:

```
K      = months earlier instalments settled   (Σ PayrollRunEmployee.adjustment_months of earlier
                                               CALCULATED runs + Σ LegacyPayrollMonth.adjustment_months)
paid   = Σ SALARY_ADJUSTMENT already paid       (the same rows' lines)
N      = this run's instruction (months, or "all remaining")
amount = Σ owed(first K + N owed months) − paid        (never negative)
```

The chronological order is what makes a revision work: the next instalment settles the next N
months *at the current rate* **and** tops up the first K months, because they are inside the sum
and `paid` is what they actually got. "Remaining" is always `len(owed months) − K`, recomputed from
scratch — the salary-loan / tax-true-up posture ("derive, don't cache"), with the run being rebuilt
excluded from its own inputs so Recalculate can't double-count.

### Worked example (the user's own figures)

| | |
|---|---|
| 2025 structure | 109,834 (SUPERSEDED 31 Dec 2025) |
| 2026 structure | 120,817 effective 1 Jan 2026 |
| Paid Jan–Jul | 109,834 (no Orbis record → assumed at the previous rate) |
| Paid Aug, Sep | 120,817 (imported months) |
| Sep payroll (pre-Orbis) | Salary adjustment 32,949, **3 months** (sheet column) |

Without a revision the owed months are Jan–Jul, 7 × 10,983 = 76,881, K = 3, remaining 4. October
"3 months" → Apr–Jun = **32,949**; November "all remaining" → Jul = **10,983**; December → nothing.
Exactly Finance's 3 + 3 + 1 schedule.

With a same-date revision to 130,000 before the October run: owed months become Jan–Sep (nine —
Aug and Sep now short by 9,183 each), total 159,528, K still 3, remaining 6. October "3 months" →
owed(Jan–Jun) 120,996 − 32,949 paid = **88,047** (Apr–Jun at 20,166 plus the Jan–Mar top-up);
November "all remaining" → 159,528 − 120,996 = **38,532**. If the revision lands *after* October
paid 32,949 at the old difference, November "3 months" settles Jul–Sep and tops up Jan–Jun:
159,528 − 65,898 = 93,630, with October itself (paid at 120,817) the one month left.

## 3. Same-date correction of a structure

`supersede_salary_structure` used to close the current row at `effective_from − 1 day`; a revision
*from the same date* would have closed the 120,817 row on 31 December 2025, before it opened — an
inverted row the engine's date filter still matched. Now a same-date supersede marks the old row
**CANCELLED** with `effective_to = effective_from` and a note; the row before it stays SUPERSEDED,
which is exactly the "rate those months were actually paid at" the ledger reads. The bulk salary
uploader, the go-live path and the gross-salary page's **"Revise salary"** drawer (same day,
`PAYROLL_PLAN.md` 2026-10-09) all go through this function — the drawer, prefilled with the
current effective date, is the one-employee way to record a revision; `approve_salary_structure`
got the same branch.

## 4. The admin's control

On the run page (Payroll Admins, run INPUTS_LOCKED or CALCULATED): a **"Salary adjustment"** header
button beside Calculate/Recalculate, its badge the current instruction, opening the right-hand
drawer (`PayrollRunAdjustmentDrawerView`, `_drawer_salary_adjustment.html` — moved out of an inline
card the same day, it took too much room) with **None / 1–12 months / All remaining months** → saved on the run (`adjustment_months`,
`adjustment_settle_all`) and the run is recalculated at once. Company-wide — one instruction per
run; a per-employee override is a follow-up if ever needed. Per employee the engine writes one
`SALARY_ADJUSTMENT` earning line (taxable, so it feeds the TDS true-up as additional pay; `quantity`
= months, `calc_note` = "Settles 3 month(s) (Apr 2026–Jun 2026) at 120,817: owed … , paid before …;
N month(s) remain"), stores the months on `PayrollRunEmployee.adjustment_months`, and the whole
ledger under `inputs_json["salary_adjustment"]` — including **`explanation`**, the four-step
breakdown HR and Finance read in the register drawer (user's request 2026-10-09: "owed to date …;
paid before … → top-up …; this run … = …; remaining …"), and the line's `calc_note` — a text field since the same day (`payroll/0034`, was 255 chars and
silently truncated) — carries the one-liner (`AdjustmentLedger.summary`) followed by the same four
steps (`AdjustmentLedger.note`), so the line is self-contained for the admin, a payslip or an export. An instruction with nothing owed pays nothing,
silently; months marked settled with nothing to pay (over-paid earlier) get a `calc_warnings` note.
No instruction = no line, even if a top-up is pending — the adjustment is always an explicit act.

## 5. Cutover

The pre-Orbis months sheet has an optional **"Salary adjustment months"** column: on a row paying
a Salary adjustment, how many months that instalment settled (September 2026 = 3). Blank on such a
row is a warning (the next instalment would start from month 1); set on a row paying no adjustment
is an error. The export carries the column; for an Orbis month it is
`PayrollRunEmployee.adjustment_months`.

## 6. Decisions and limits worth stating

- **Positive only.** `SALARY_ADJUSTMENT` is an earning; a month paid *above* the current rate
  (a demotion, a correction downwards) contributes nothing — recovering an overpayment is a
  deduction, out of scope here.
- **Full-month basis for the window**, day-level proration only where the paid month itself was
  prorated. A mid-month effective date starts the window at the period named for the *next* month.
- **Months before Orbis and before the import are assumed paid at the previous structure.** True
  by SEBPO's practice (the increment is never paid before August). One run-level
  `SALARY_ADJUSTMENT_ASSUMED_PAID` warning names the employees; each register row's warning names
  the months (on the first runs after go-live this is everyone, so a check per employee only
  buried the Exceptions tab — changed the same day).
- **No pay record at all → no adjustment** (`SALARY_ADJUSTMENT_NO_RECORD`, per employee). Found
  on the first local run (2026-10-09): only one employee's pre-Orbis months had been imported, and
  every other employee with a 2026 structure was paid three months of adjustment on the
  assumption that nothing had ever been paid to them. The engine now requires at least one month
  of the window to come from a real record (an Orbis run or an imported month) before it pays;
  the fix for a skipped employee is to import their pre-Orbis months.
- **One instruction per run, company-wide.** Matches how the company decides it.
- Finance question **Q78** (`TAX_CERTIFICATE_PLAN.md` §9 numbering): when an increment is revised
  mid-schedule, does the company re-spread the new balance or keep the original month plan? Orbis
  supports either — it is only a different number in the dropdown.
