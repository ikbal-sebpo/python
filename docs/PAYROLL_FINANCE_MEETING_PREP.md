# Payroll — Finance Meeting Prep

**Purpose:** Talking points and questions for the Finance sync on 2026-08-10. Pulled from the open decisions in [`PAYROLL_PROCESS.md`](PAYROLL_PROCESS.md) §12, filtered to what Finance owns or co-owns, and ordered by what actually blocks progress.

---

## 1. Hard blockers — nothing in the tax stage moves without these

- **TDS calculation method.** The Finance SOP just says "update latest TDS information." We need the **actual calculation sheet**: projected annual income basis, exemption treatment, investment rebate mechanics, thresholds used. We will not guess or approximate this — ask them to bring or send the working sheet, not just describe it verbally.
- **Mid-year cutover data.** If Orbis goes live mid-tax-year, we need each employee's **tax paid to date** and **taxable income to date** at cutover. Ask: where does this live today (their workbook, NBR filings, something else), and who can extract it per employee?

## 2. Needed early — shapes the approval & access model

- **Approval chain composition.** Who approves a payroll run, in what order? Their SOP step 13 says "Obtain Management Approval" — does that mean HR Head → Finance Head → Management, or something else? Is a **threshold escalation** wanted (extra approval step above a set total)?
- **Preparer vs. approver separation.** Should someone who entered salary, bank, or variable-pay data be blocked from approving that same run? This decides how strict segregation-of-duties is in the system.
- **Salary visibility.** HR currently has org-wide access to employee *profiles* — we're proposing that does **not** automatically extend to pay data, with a separate payroll permission. Does Finance agree? Relatedly: `Department.additional_managers` exists partly "for payroll review" today — should that role see salary, or just roster/leave?
- **Payroll export access.** Who can download the register, bank advice, GL/cost-centre summary, and payslip ZIP? Should every download be logged? (We're building the audit log regardless — this just decides who's allowed to trigger it.)

## 3. Calculation rules Finance needs to confirm

- **Leave encashment rate** — Gross ÷ 30, Basic ÷ 30, or Basic ÷ 26?
- **Meal amount** — is it a deduction from the employee or a company-paid subsidy? Changes which side of the payslip it lands on.
- **Gratuity formula** — months of basic per year of service, and the eligibility threshold (years of service)?
- **Provident Fund / WPPF** — does SEBPO operate a recognized provident fund? If yes, we need employee + employer contribution rules, since it also affects taxable income.
- **Festival bonus** — fixed amount, a multiple of basic, or grade-banded?
- **Overtime rounding** — pay to the exact second, or round to nearest 15/30/60 minutes? (Joint with HR, but Finance owns the cost impact.)

## 4. Data & historical gap

- **Historical salary data.** Finance's workbook holds *current* figures only, with no effective dates. That means **arrears cannot be calculated for any salary change dated before go-live** — only changes made in Orbis going forward will have history. Confirm this is acceptable, or find out if effective-dated history exists somewhere else (old workbooks, email approvals) that could be backfilled.

## 5. Governance / operational

- **Cut-off exceptions** — which late changes (bank detail edits, loan changes, backdated adjustments) are allowed to slip into the current run vs. must move to a supplementary run?
- **Statutory outputs** — which tax certificates, year-end summaries, or bank/government file formats are needed in phase 1 vs. can wait?
- **Bank advice format(s)** — which bank(s) does SEBPO disburse through, and do they have a fixed file format/template we need to match exactly?

---

## Good framing for the meeting

The whole point of the redesign (§1 of `PAYROLL_PROCESS.md`) is that Finance currently **re-keys HR's entire compiled file into a second workbook** just to add loans, bank details, TDS, and produce the bank advice — most of their SOP's steps 2–6 are "copy and re-verify," not new calculation. Worth stating plainly: once this ships, that whole re-entry step disappears and Finance instead reviews a system-generated register. That's the incentive for them to prioritize answering the tax/approval questions above — those are the only things actually blocking that outcome.

## Not for this meeting (HR-owned, flagged for completeness)

Overtime approval gate, night-shift-crossing-midnight month assignment, "Eid Package" flat-vs-hourly basis, unauthorised absence (ABS) pay treatment, cross-year leave period rule, payslip delivery mode (link vs. attachment — we recommend a link). Full list in `PAYROLL_PROCESS.md` §12.
