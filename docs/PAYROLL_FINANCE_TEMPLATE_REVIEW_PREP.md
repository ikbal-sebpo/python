# Payroll — Template Review Follow-up: Finance Meeting Prep

**Purpose:** Talking points for the Finance follow-up on the 2026-08-13 review of *HR Payroll File Template.xlsx*, *Finance Monthly_Salary_Template.xlsm* and *Monthly_Salary_Template_Reporting.xlsm* — the actual working files, not the SOP documents. Pulled from [`PAYROLL_PROCESS.md`](PAYROLL_PROCESS.md) §12 "Reopened" and "Newly identified" (questions 25–33).

**Framing for the meeting:** none of this overturns what was locked on 2026-08-11. Reading the live formulas in your own workbook turned up detail — bands, gates, extra columns — that the SOP descriptions didn't mention. We need a quick confirm-or-correct on each one before this goes to formal sign-off, not a re-litigation.

---

## 1. Confirm or correct — these narrow a decision already locked

- **Festival bonus tenure gate (reopens decision #12).** Your formula pays nothing to anyone with under ~180 days' tenure at the Eid date. The locked decision ("50% of gross, no grade-banding, no fixed amount") never mentioned this. Is the 180-day gate still current? What date is it measured against — the Eid date itself, or the period the bonus run covers?

- **Night-shift and OT rates are banded by the employee's own salary, not just department (reopens decision #6).** E.g. Software pays 350 Tk below a 40,000 Tk gross threshold vs. 450 Tk above; Creative pays 250 vs. 400. Are these salary bands still live? We need the current threshold and rate for every department that uses one — this is a new dimension our rate table doesn't have yet.

- **A third overtime basis shows up in the formula (reopens decision #6).** Beyond flat-per-hour and flat-per-day, one calculation is `gross ÷ 30 × days × 1.5` — a statutory-style per-day rate. Which departments or OT types actually use this today?

- **Eid coverage looks like flat lump-sum tiers, not a rate × days calculation.** WFH 12,000 Tk / WFO 15,000 Tk, plus extra 13,500 / 10,000 / 3,000 Tk bands tied to hours-worked windows. Can you walk us through how this is actually calculated today? (Low priority — this is Phase 7 — but worth capturing accurately while the template is fresh.)

- **The GL split formula for the salary breakdown.** Even though Orbis will store only gross salary, your JV needs it split into Basic (50%), Housing (25%), Medical (5%), Conveyance (10%), Other Allowance (remainder), and a salary-tiered Mobile/Internet allowance — each against its own GL code (51001 / 51002 / 51007 / 52009). Are these percentages and GL codes still correct? This is the one thing we need exactly right before it's hardcoded into the export.

## 2. New — not in either SOP, needs a decision

- **Disbursement isn't bank-transfer-only.** Your workbook has a separate Cash/Cheque sheet with its own cheque-number sequence. Does Orbis need to support cash and cheque payment at go-live, or can those employees be migrated to bank transfer before cutover? This changes the shape of the disbursement model either way, so we'd like an answer before Phase 5.

- **MFS wallet accounts** (bKash/Nagad-style, keyed by phone number, not branch/routing/account number) show up as a payment channel alongside real bank accounts. Do we need to support MFS as a distinct account type, and if so, which providers?

- **"Service benefit" long-service award.** Your workbook has a scheme paying 15 / 7 / 1 days' pay at specific tenure milestones (GL code 52010), separate from leave encashment. Is this a live scheme we need to model, or a legacy column no longer in use?

- **Legacy leave-balance cutover.** Both the encashment and final-settlement sheets split balances "up to 2024" vs. "from 2025" and reference a balance "of Oracle" (a prior system). Does this need a one-time import into Orbis, or does the leave module already have everything it needs for final settlements going forward? *(This one is joint with HR — worth looping them in, since it's about leave data, not compensation.)*

## 3. Already answered — for context only, no action needed

Your own templates settled one open question for us: **bank advice format.** Three named formats — Bank Asia PLC (BAL), Eastern Bank PLC (EBL), Modhumoti Bank Limited — each with its own letter convention. We'll seed these directly rather than asking again.

---

## Good framing to close with

Everything above came out of reading your actual formulas, not guesswork — the goal is to make sure the calculation engine reproduces exactly what you do today, including the parts that never made it into the SOP write-up. Answering these now is cheaper than finding the gap during parallel-run reconciliation (§13 of `PAYROLL_PROCESS.md`), where a missed salary band or tenure gate would show up as an unexplained variance on every affected employee, every month.
