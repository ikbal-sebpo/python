# Payroll — HR Meeting Prep

**Purpose:** Talking points and questions for the HR sync on 2026-08-11. Pulled from the "Still open" items in [`PAYROLL_PROCESS.md`](PAYROLL_PROCESS.md) §12 that are HR-owned, plus one design call worth sanity-checking with HR directly. The Finance-owned items from the earlier meeting (TDS, approval chain, meal deduction, PF/gratuity, festival bonus, etc. — see [`PAYROLL_FINANCE_MEETING_PREP.md`](PAYROLL_FINANCE_MEETING_PREP.md)) are already locked and don't need airtime here.

---

## 1. HR-owned open questions

- **Overtime approval gate.** Currently designed so a supervisor's upload is final — there's no department-head approval step before HR reviews. Does HR want to add one?
- **Night shift crossing midnight.** A shift like 22:00–06:00 on the 15th spans two payroll months (period boundary is the 15th/16th). Which month does it count toward? This needs to be fixed before supervisors start uploading overtime.
- **"Eid Package" OT type.** Is it an hourly rate like the other overtime types, or a flat payment per occasion?
- **Payslip delivery.** PDF attached to the email, or a secure link into Orbis? **We recommend the link** — a misdirected attachment is an unrecoverable disclosure.
- **Unauthorised absence (ABS).** There's currently no pay rule attached to this category, so it would default to fully paid. Should it be unpaid?
- **Cross-year leave period.** The 16 Dec – 15 Jan payroll period spans two leave years. We need a written rule for how leave entitlement is treated in the January run.

## 2. Joint with Finance — worth raising HR's view now

- **Preparer vs. approver separation.** Should someone who entered salary, bank, or variable-pay data be blocked from approving that same run at any of the three chain stages (Department Review → HR → Finance)?

## 3. Design call to sanity-check with HR

The department-review step is built so a **missing** department review raises a warning on HR's org-wide approval rather than hard-blocking it — HR can still approve past it. Worth confirming this is what HR wants operationally, or whether some departments (e.g. ones with active pay disputes) should be a hard stop instead.

---

## Already locked — for context only, no action needed

TDS is uploaded monthly by Finance via Excel (not calculated in-system); the approval chain is Department Review → HR Approval → Finance Approval, with management's disbursement sign-off happening offline on the printed advice; salary is stored as gross-only with no Basic/HRA/Medical breakdown; meal deduction is tiered by gross salary; there's no provident fund or gratuity scheme; festival bonus is 50% of gross for each of 2 Eids; salary visibility extends to downline managers plus full access for HR and Finance; historical salary data will not be backfilled. Full detail in `PAYROLL_PROCESS.md` §12 "Resolved."
