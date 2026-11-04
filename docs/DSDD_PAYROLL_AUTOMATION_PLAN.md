# Data Solutions & Due Diligence — payroll automation plan

**Status:** **All five phases (A–E) are built** (2026-09-19). Written 2026-09-19 from the
September 2026
support-info export (`payroll-run-2026-09-support-info.xlsx`, 379 DSDD rows) and a
read of the `payroll` app as it stands at commit `113f2a4`.

**Problem in one line:** DSDD's three recurring pay events — trainee go-live, SLA
achievement pay, and Test Salary — reach payroll only as English sentences in the
Supervisor Note column, so a Finance payroll admin re-derives and re-keys every
number by hand each month.

Companion docs: `PAYROLL_PLAN.md` (technical, the source of truth for decisions —
anything here that lands gets folded back into it), `PAYROLL_PROCESS.md` §12 (open
business questions, where Q34 already tracks Test Salary).

---

## 1. What the notes actually contain

125 of 379 rows carry a Supervisor Note. Every one falls into six categories:

| Category | Rows | Shape | Owner today |
|---|---:|---|---|
| **SLA achievement** | 52 | *"Meet SLA-August, 2026: Recommended for the variable part (3000 Taka) for achieving SLA targets."* | nothing — Finance types a `VariablePayEntry` by hand |
| **Trainee go-live** | 19 | *"Go live date, Salary as per the new package for MARS (TK. 18,000): Date: September 07, 2026"* | nothing — Finance edits the salary master and prorates by hand |
| **Test Salary** | 15 | *"13 days Test Salary: From August 26 to September 07, 2026"* | nothing — a **200 Tk/day deduction** (Q34, answered 2026-09-19); Finance deducts it by hand |
| Resignation / LWD | 3 | *"Date of Resignation: 09/09/2026, Last Working Day: 10/12/2026"* | `offboarding.Resignation` already owns this |
| Maternity leave | 1 | *"She started her maternity leave on 16 July 2026."* | `leave` already owns this (MTL shift rows) |
| Temporary suspension | 1 | *"Placed on temporary suspension on 30 August 2026…"* | nothing, and no obvious owner |

Two details the note format hides, both visible in this month's data:

* **SLA pay lags the period and can be claimed retroactively.** "Meet SLA-**August**"
  is paid in the 16 Aug – 15 Sep run, and employee `3503` claims *both* July (flagged
  "Missed in the Previous Payroll") and August in the same note. Without a recorded
  SLA month you cannot tell a retro claim from a double-payment, or answer "did we
  ever pay August?".
* **The notes can contradict Orbis and nothing notices.** Employee `53251`'s note
  reads "Date of Resignation: 09/11/2026, Last Working Day: 07/21/2026" — an LWD
  two months *before* the resignation date. A free-text cell cannot be wrong; a model
  field can.

### The go-live package table, recovered from the notes

All 19 trainees joined 2026-06-08, sit on team `Trainee` with
`JobInfo.resource_status = TRAINEE`, and go live 2026-09-07:

| Destination team | Gross | Count | Employee IDs |
|---|---:|---:|---|
| MARS | 18,000 | 8 | 55043, 55047, 55049, 55063, 55069, 55071, 55079, 55091 |
| Item Review | 16,000 | 11 | 55045, 55051, 55055, 55059, 55067, 55073, 55075, 55077, 55081, 55083, 55089 |

Trainee tier is 11,000 across both. **That three-row table is the entire policy**, and
it is currently encoded nowhere except in 19 English sentences.

---

## 2. Three defects, all verified against the code

### 2.1 The engine cannot pay a mid-period salary change — it pays the new rate for the whole period

`engine.py:119-124` selects **at most one** `SalaryStructure` per employee:

```python
structures = {
    s.employee_id: s for s in SalaryStructure.objects.filter(
        employee_id__in=employee_ids, status=SalaryStructure.Status.APPROVED,
        effective_from__lte=period.end_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period.start_date))
}
```

and stage 3 multiplies that one `gross_monthly` by a single period-wide
`proration_factor` built only from employment days minus LWP.

`upload.py::apply_salary_upload` closes the old row as `SUPERSEDED`. The
`status=APPROVED` filter above then **discards it** — so after a 2026-09-07 go-live the
engine sees only the 18,000 row and prices all 31 days at 18,000.

Confirmed by running the real engine against exactly this shape:

```
period          : 2026-08-16 .. 2026-09-15 (31 days)
eligible days   : 31.00
proration       : 1.000000
full_amount     : 18000.00
PAID            : 18000.00
```

Correct is 22 days @ 11,000 + 9 days @ 18,000 = **13,032.26** on the engine's own
÷31 divisor. **Overpaid by 4,967.74 per trainee — ~94,400 Tk across the 19, in one
period.**

This is not DSDD-specific. Every mid-period increment, promotion, demotion or transfer
company-wide is paid at the new rate from day one of the period. It is invisible today
only because there are no `SalaryStructure` rows yet — the very first upload creates
revision 1 with nothing to supersede. **The bug bites on the second upload**, which is
the go-live one.

It also means design principle 1 in `PAYROLL_PLAN.md` — *"backdated promotions produce
arrears arithmetically"* — is not true today.

### 2.2 Team and resource-status changes leave no dated record

`CustomUser.team` and `JobInfo.resource_status` are mutable single values. When HR flips
a trainee to MARS/LIVE, nothing records *when*. So even a correct engine could not derive
the 22/9 split — the go-live date exists only in the note.

`PAYROLL_PLAN.md` Risk 10 already logs this ("No effective-dating on department/team/grade…
acceptable now, wrong for cost-centre allocation later"). DSDD promotes it from a
*reporting* problem to a *gross pay* problem. A second consequence: `PayRate` is scoped by
team and resolved once at `period.end_date`, so a mid-period team move silently reprices
the whole period's overtime too (MARS is ৳250/hr, the DSDD default is ৳200/hr).

The fix has two working precedents in this codebase already — `employees.DesignationHistory`
(effective-dated, auto-synced by `employees/signals.py` whenever `CustomUser.designation`
changes) and `employees.DedicatedNightShiftAssignment` (dated window + approval gate).

### 2.3 `VariablePayEntry` has no bulk path, no approval gate, and no idempotency

* The only way in is `PayrollRunEmployeeAdjustView`'s per-employee drawer — **52 SLA
  bonuses = 52 drawer visits**.
* It is written straight as `APPROVED`, with `reason` defaulting to the literal string
  `"Manual test adjustment"`. There is no supervisor attribution and no approval step.
* `PayrollInputBatch.InputKind.VARIABLE_PAY` has existed since Phase 2 and **nothing
  ever writes it** — only `OVERTIME`, `SALARY` and `MEAL_COUNT` are used. Variable pay
  has no audit envelope.
* `VariablePayEntry` has no unique constraint (deliberately — one employee can have two
  legitimate incentives), so nothing stops the same SLA bonus being entered twice.

Overtime got all of this in the 2026-09-15/16 work (window rules, `PENDING` on late
filing, HR∧Payroll approval, batch envelope, re-upload that reads existing status).
Variable pay never did.

### 2.4 Secondary: `PayrollSettings.payable_days_basis` is dead — ✅ **RESOLVED 2026-09-19**

It defaulted to `FIXED_30` and was read nowhere; the engine always used
`(end_date - start_date).days + 1` — 31 for Aug 16 – Sep 15. **HR and Finance confirmed the
divisor is always 30**, and the setting is now wired.

It is not a one-character change, because the 16→15 cycle runs **28 to 31 days** (16 Feb –
15 Mar is 28). `eligible_days / 30` would dock a fully-worked March 6.7%. The implemented
rule: employed throughout → exactly 30 days' pay whatever the calendar says; employed
partly → one 1/30 per day employed; unpaid leave → 1/30 each. Full write-up in
`PAYROLL_PLAN.md`'s 2026-09-19 section; 9 tests in `ProrationBasisTests`.

---

## 3. Plan

Phases are ordered by dependency. **A is a prerequisite for B** — capturing the go-live
date perfectly still produces a wrong number until the engine can split a period.

### Phase A — segmented proration ✅ **BUILT 2026-09-19**

Landed as described below. `engine.py::_salary_segments()` + per-segment `GROSS_SALARY`
lines, `inputs.py::collect_lwp_days_by_date()`, 7 new tests in
`tests_engine.py::SalarySegmentTests` including the single-structure equivalence guard.
Full write-up folded into `PAYROLL_PLAN.md`'s dated section for 2026-09-19. No downstream
change was needed — the register and export breakdowns already `Sum()` by component code.
§2.4 (`payable_days_basis`) deliberately untouched; see below.

Change stage 3 from *one structure × one factor* to *one line per salary segment*.

1. Widen the structure query to `status__in=(APPROVED, SUPERSEDED)` and bucket to a
   **list** per employee. A superseded row is still what was true then; the current
   filter is precisely what throws history away. `DRAFT`/`PENDING`/`CANCELLED` stay out.
2. Per employee, intersect each structure's `[effective_from, effective_to or ∞]` with
   the period and with `[joining_date, last_working_date]` to get date segments.
3. Make `inputs.collect_lwp_days` return per-date weights (`dict[emp_id, dict[date, Decimal]]`)
   instead of one total, so LWP lands in the segment it actually occurred in. The query
   already reads one row per shift — this is adding `date` to the `.values()` call, not a
   new query.
4. Emit one `GROSS_SALARY` `PayrollRunLine` per segment: `full_amount` = that segment's
   gross, `quantity` = segment payable days ÷ `period_days`, and the arithmetic written
   into the existing `calc_note` field.
5. Keep `PayrollRunEmployee.proration_factor` as the whole-period aggregate so the
   register, exports and variance report are unaffected.

**The safety property worth a test:** an employee with one structure produces exactly one
segment and byte-identical output to today. That, plus a test asserting the 22/9 split,
is the whole regression surface.

**§2.4 was deliberately left alone by Phase A** and settled separately the same day, once
HR/Finance confirmed the divisor is always 30. See §2.4.

*Size: M. Touches `engine.py` stage 3, `inputs.py::collect_lwp_days`, tests.*

### Phase B — the go-live as a structured, dated event ✅ **BUILT 2026-09-19**

Landed as described below, plus a pending-proposal queue at `/payroll/salary/pending/`
(B3 needed somewhere for Finance to act; the existing salary screens were read-only) and
`manage.py backfill_team_assignments` for the ~1,527 employees who predate the model.

Two things the build changed from this spec:

* **`_PACKAGE_WEIGHTS` is not `_SPECIFICITY_WEIGHTS`.** Sharing the *scoring function* was
  right; sharing the *weights* was wrong. Resource status has to outrank team for packages,
  or a trainee parked on MARS resolves to 18,000 before go-live. Caught by a test.
* **No proposal when the employee has no current salary structure.** Otherwise every new
  joiner lands a row in Finance's queue; their starting salary comes from the HR file.

Full write-up in `PAYROLL_PLAN.md`'s 2026-09-19 section. 21 tests in
`payroll/tests_team_package.py`.

**B1. `employees.TeamAssignment`** — effective-dated team + resource status, modelled
directly on `DesignationHistory`:

* `employee`, `team` (nullable), `resource_status`, `effective_from`, `effective_to`,
  `reason` (`INITIAL`/`GO_LIVE`/`TRANSFER`/`BENCH`/`CORRECTION`), `note`
* one open-ended row per employee — partial unique + `clean()` mirror (MySQL posture,
  `PAYROLL_PLAN.md` Risk 4)
* **auto-synced by a signal** on `CustomUser.team` / `JobInfo.resource_status`, exactly
  like `_sync_designation_history`. HR's ordinary "move to MARS, set Live" edit *is* the
  dated record — no second step to forget. The profile drawer exists only to backdate,
  since HR typically updates a few days after the real go-live.

**B2. `payroll.TeamSalaryPackage`** — the policy table that is currently only in English:

* scope `department` + optional `team` / `resource_status` / `designation`,
  plus `gross_monthly`, `effective_from`/`effective_to`, `is_active`
* resolved most-specific-wins. **Extract** `rates.py`'s specificity scoring into a shared
  helper rather than copying it — Risk 2's lesson is that a second copy of a resolution
  rule is how the engine and the report drift apart.
* seed: DSDD/`TRAINEE` → 11,000, DSDD/MARS → 18,000, DSDD/*(every other team)* → 16,000
  — confirmed 2026-09-21 that MARS is the only team with its own rate (§5 Q2)

**B3. The glue.** When a `TeamAssignment` is created, resolve `TeamSalaryPackage` for the
new (team, resource_status) as of `effective_from`. If it resolves and differs from the
employee's current gross, **propose** a `SalaryStructure` with `status=PENDING`,
`reason=TRANSFER`, `effective_from` = the assignment date. Finance approves it on the
existing salary screen.

Deliberately *propose*, not auto-approve: money keeps a human gate, and
`SalaryStructure.Status.PENDING` already exists with no flow using it — this gives the
existing state a purpose instead of inventing one.

**B4. Extract the supersede-and-create.** `apply_salary_upload` already knows how to close
the current row and open the next. Pull it into
`payroll/services.py::supersede_salary_structure(...)` and have both the uploader and the
go-live approval call it — the same reason `attendance`'s `_approve_correction` is a shared
module-level function rather than duplicated in the single-row and bulk views.

**B5. A control check.** `PayrollControlCheck(check_code="SALARY_PACKAGE_MISMATCH", WARNING)`
when a segment's paid gross doesn't match the resolved `TeamSalaryPackage`. Catches both a
missed go-live and a stale package.

*Size: L. New model + migration in `employees`, new model + migration in `payroll`, a
signal, a shared resolver, a seed command, a control check.*

### Phase C — SLA achievement as a real input ✅ **BUILT 2026-09-19**

Landed as described below, with two scope decisions worth recording:

* **The intake is SLA-specific, the queue is generic.** An uploader that priced any
  component from a typed Amount column would serve neither SLA nor Test Salary well — both
  want the *system* to price. So `/payroll/variable-pay/upload-sla/` is an SLA intake, and
  `/payroll/variable-pay/` is one generic queue over every `VariablePayEntry` (SLA, Test
  Salary, bonus, incentive, disciplinary). Phase D's Test Salary intake becomes a sibling
  parse/apply pair in the same module, sharing the write path — the `gh_overtime.py::_apply_candidates`
  pattern, where two sources feed one set of write rules.
* **The payment period is a form-level choice, not derived per row.** Overtime derives its
  period from the work date; SLA cannot, because the SLA month is *not* the payment month.
  The uploader targets the open period and the SLA month rides on `source_key`.

Shipped: `payroll/upload.py` (`parse_sla_upload`/`apply_sla_upload`/`build_sla_xlsx_template`),
`payroll/upload_views.py` (`UploadSlaView`, `SlaUploadTemplateView`), `payroll/views.py`
(`VariablePayEntryListView` + single/bulk decide), `accounts/permissions.py::can_act_on_variable_pay`,
`VariablePayEntry.source_key` + its partial unique (migration `0018`), a `SLA_INCENTIVE`
`PayComponent`, `PayRate.RateType.SLA_INCENTIVE` seeded at ৳3,000 for DSDD, three templates,
and nav/`ProtectedView` rows in `seed_orbis.py`. 24 tests in `payroll/tests_variable_pay.py`.

**No engine change was needed** — `calculate_run` already filtered variable pay to
`status=APPROVED`, which is exactly what makes a pending row unpayable by construction.


**C1. A variable-pay bulk uploader** — the generally-missing piece, worth building
irrespective of DSDD. Mirror the overtime uploader exactly (`parse_*`/`apply_*`/
`build_*_xlsx_template` + preview/apply/template views, same shape as `roster`/`payroll`/
`employees`):

* an `SLA_INCENTIVE` `PayComponent`, and the 3,000 figure as a **`PayRate` row**
  (new `RateType.SLA_INCENTIVE`, `PER_OCCURRENCE`, scoped to DSDD) so it is effective-dated
  data that can differ per team later — not a constant
* template columns: `Employee ID | SLA month | Met SLA (Y/N) | Note`, pre-filled with the
  supervisor's own managed scope (`build_notes_xlsx_template`'s pattern). **The supervisor
  ticks names; the system prices the row.** That is the actual division of labour — the
  supervisor is attesting to an achievement, not quoting a rate.
* wrapped in a `PayrollInputBatch(input_kind=VARIABLE_PAY, source=UPLOAD)` — finally
  populating the choice that has been dead since Phase 2
* **lands as `PENDING`**, promoted by the **department head** (confirmed 2026-09-19 — see
  "One gate" below). `engine.py` already filters variable pay to `status=APPROVED`, so a
  pending row cannot be paid by accident **with no engine change**.

**C2. Idempotency + the retro case.** Add a nullable `source_key` to `VariablePayEntry`
(e.g. `"SLA:2026-08"`) with a unique constraint on
`(employee, period, component, source_key)` where `source_key != ""`. Re-uploading the
same SLA month becomes an update, not a duplicate, while free-form manual entries stay
unconstrained. This is also what makes employee `3503`'s July-claimed-in-September case
representable and auditable, rather than indistinguishable from a double-entry.

*Size: M — the uploader is a close copy of an existing one; `source_key` is a small
migration.*

### Phase D — Test Salary ✅ **BUILT 2026-09-19, revised 2026-09-21** — Q34 answered: it is a deduction, 200 Tk/day

Built exactly as specced below, on Phase C's machinery: no new model, no new approval path,
no engine change. Priced against September's real 15 windows: **31,800 Tk**. Full write-up
in `PAYROLL_PLAN.md`'s 2026-09-19 section; tests in `payroll/tests_test_salary.py`.

**Revised 2026-09-21, before this shipped to production: the day count is entered directly,
not derived from a From/To date window.** The original build (below, as originally written)
gave `VariablePayEntry` nullable `window_start`/`window_end` so the day count could be
derived from the dates and so two windows could be checked for overlap. That broke down on
the next real month's data — an employee can fall under test across several disjoint
stretches of one period, which a single window can't represent — so the uploader now reads
one `Employee ID | Days` row per employee per period instead of `Employee ID | From | To`.
`VariablePayEntry.days` (nullable `PositiveSmallIntegerField`) replaces the two date fields;
`source_key` is now the constant `"TEST"` (one row per employee/period, re-upload replaces
it) rather than a window-derived string; the automatic already-unpaid-day exclusion and the
overlap check are both gone, since there's no date range left to check the roster against or
compare between rows. See `PAYROLL_PLAN.md`'s 2026-09-21 section for the full before/after.
The rest of this section is preserved as originally written for the historical record; where
it describes the From/To mechanics, treat it as superseded by the above.

> **"Test Salary" is a salary *deduction*, not a pay category.** Data Solutions employees
> carry a **6,000 Tk variable portion over a 30-day month = 200 Tk/day**. A note reading
> "3 days Test Salary" means **3 × 200 = 600 Tk deducted**. — user, 2026-09-19

This supersedes `PAYROLL_PROCESS.md` **Q34** as written. Q34 (raised 2026-08-13 off July's
file, owner HR, unanswered through the 08-20/08-24/09-07 rounds) described *"11 new hires…
a short trial-period daily rate… a separate probationary tier that precedes Trainee"* —
**every part of that premise was wrong**, which is why it never resolved. September's 15
rows joined between 2021-11-29 and 2026-03-04, none are `TRAINEE` (13 `Live`, 2 `Bench`),
and all carry a full gross of 25,000–44,500. It was never a probation tier and never a
rate paid *to* anyone. Update Q34 in `PAYROLL_PROCESS.md` to the resolved wording above
rather than leaving the misleading question standing.

**Applied to September's real data:** 15 employees, 4–27 days each, **31,800 Tk** deducted
in total. Largest single case is `52979` at 27 days = 5,400.

#### Build shape — it collapses into Phase C, it is not a subsystem

| Piece | What |
|---|---|
| `PayComponent` | `TEST_SALARY_DEDUCTION`, `kind=DEDUCTION`, `is_prorated=False` |
| Rate | `PayRate.RateType.TEST_SALARY_DAY`, `basis=PER_DAY`, `amount=200`, scoped to DSDD, effective-dated — **data, exactly like the SLA 3,000, never a constant** |
| Input | the Phase C1 uploader, one row per window: `Employee ID | From | To | Reason` |
| Pricing | **derive the day count from the dates, never accept a typed count** — `54614`'s note says "6 days" for 07–13 Sep, which is 7. The supervisor supplies the window; the system does the arithmetic |

`VariablePayEntry` already carries deductions (*"One table for performance bonus, incentive
and disciplinary adjustment — `component.kind` decides the sign"*), so this needs **no new
model**, only the C1 uploader plus a dated window on the entry so the count is derivable
and auditable.

#### Two consequences worth knowing

* **No tax interaction.** `engine.py:457` computes TDS from `structure.gross_monthly` — the
  contractual monthly gross — not from `gross_earnings` net of deductions. A Test Salary
  deduction correctly leaves TDS untouched. (This also means the deduction-vs-reduced-earning
  modelling choice has no tax consequence today.)
* **It agrees with §2.4, now settled.** 200 Tk/day is 6,000 ÷ **30**, and HR/Finance
  confirmed the same day that gross proration also always divides by 30. The two bases
  match, so a 31-day period no longer prices the variable portion one way and the gross
  another. See §2.4.

#### Confirmed 2026-09-19

| | Answer | Consequence for the build |
|---|---|---|
| Is the 6,000 uniform across every DSDD package? | **Yes** | **One** `PayRate` row at 200/day scoped to the department. It does *not* belong on `TeamSalaryPackage`, and Phase B and D stay independent. |
| Inside the stored gross, or on top? | **Inside** | The compensation master stays gross-only (locked 2026-08-11). Nothing about this reopens that decision. |
| Does it cap at 6,000? | **Yes** | `min(days × 200, 6000)`. |
| Deduct on an already-unpaid day? | **No** | Eligible days = window days **minus** any day already unpaid. Never double-penalised. |
| Who approves? | **The department head.** HR reviews, Finance calculates and pays. | `Department.head_of_department`, resolved exactly as `offboarding/services.py::_resolve_assignee`'s `DEPT_HEAD` branch already does. Same gate as SLA — see "One gate" below. |
| Do new joiners get it? | **No** — they are paid 11,000 until go-live or end of probation | Moot's the cap-proration question (1f, withdrawn). Also means a window filed against a `TRAINEE` is an error, not an edge case — **validate and reject it**. |
| Any department but DSDD? | **No, DSDD only** | Confirms the seed-one-row, no-global-fallback decision below. |

#### Rules this implies, being built as stated rather than re-asked

* **The cap is per period, across all windows** — the 6,000 is a monthly variable portion,
  so two windows in one period share one cap rather than getting 6,000 each.
* **Weekly offs and holidays inside a window *do* attract the 200.** The basis is 6,000 ÷ 30
  calendar days, and the supervisor's own counts are inclusive calendar days ("13 days
  Test Salary: From August 26 to September 07" = 13). Excluding offs would make the note
  and the arithmetic disagree.
* **A half-day (`HDL`) deducts 100**, matching `inputs.collect_lwp_days`'s existing 0.5
  weighting. Zero occurrences in September's 15 rows — stated so the code has a defined
  answer, not because it has come up.
* **Seed DSDD only, with no global fallback row.** An unconfigured department then raises
  a warning instead of silently deducting 200, the same posture the engine already takes
  for a missing night-duty rate.
* **A window filed against a `TRAINEE` is rejected at entry.** New joiners are paid 11,000
  flat until go-live or end of probation and never carry this deduction — so a trainee
  window is a mistake, and the cheapest place to catch it is the form.
* **Late filing follows Q55**: a window filed after inputs lock lands `PENDING`, is promoted
  by an explicit approval, and the run must be recalculated.

#### One gate for all variable pay — confirmed 2026-09-19

**SLA (an earning) and Test Salary (a deduction) are both approved by the department head.
HR is not in the path for either.** That is simpler than the two-gate design sketched
earlier in this doc, and it deletes a field: there is **no `PayComponent.approver_rule`**,
because authority does not vary by component.

The gate splits cleanly by input table, not by component:

| Input | Files | Approves |
|---|---|---|
| `OvertimeEntry` | supervisor | **HR∧Payroll** + Payroll Admins (Q55, 2026-09-15 — unchanged) |
| `VariablePayEntry` (SLA, Test Salary, future bonus/incentive/disciplinary) | supervisor | **Department head** (`Department.head_of_department`) |

So this is one new helper — `can_act_on_variable_pay(user, employee)` — in
`accounts/permissions.py`, next to `can_act_on_leave` and
`can_act_on_attendance_correction`. No new field, no per-component branch.

Two rules carried over from the house pattern rather than invented here:

* **Nobody approves their own row.** Every other approval gate in this codebase enforces
  it (`can_act_on_leave`, `can_act_on_attendance_correction`, Q56's "supervisors can file
  but never approve"). **Confirmed 2026-09-19:** department heads are not entitled to the
  3,000 SLA at all, so the case should never arise — and if it ever does, the approver
  falls back to the head's own `JobInfo.reports_to`, **not** HR. HR stays out of the
  variable-pay path entirely.
* **Post-lock filing follows Q55's mechanism with the new approver.** A row filed after
  inputs lock lands `PENDING`, the department head promotes it explicitly, and the run must
  be recalculated. Only the authority changes, not the machinery.

**"HR reviews" is deliberately not built as a workflow step.** It is oversight — HR sees
the pending list and the run — not a state anything transitions through. Adding a status
nothing moves through is exactly what the Run-&-results section of `PAYROLL_PLAN.md` warns
against, and it has bitten this app once already.

#### One thing for HR to look at, not a build blocker

At the cap, this is **6,000 off a gross that can be 11,000** — 55% of a month's pay for a
trainee, ~22% for an analyst. Whether that is characterised as *a deduction from wages* or
as *variable pay that was not earned* is a real distinction under the Bangladesh Labour
Act's rules on deductions, and it is HR/legal's call, not an engineering one. It changes
nothing about the arithmetic; it changes how the payslip line should be **labelled** and
whether a cap lower than 6,000 applies in some cases. Worth raising because this project
already tracks BLA obligations elsewhere (s.109 night-shift consent, Form-7 service book),
and because "Test Salary" as a payslip label reads like an earning when it is the opposite.

### Phase E — retire the note column ✅ **BUILT 2026-09-19**

`payroll/notes_lint.py` + an `UNSTRUCTURED_NOTE_AMOUNT` WARNING on every calculation, and
`apply_notes_upload` no longer refuses a locked run. Validated on all 125 real September
notes: 108 amounts detected, zero false positives on dates. Full write-up in
`PAYROLL_PLAN.md`'s 2026-09-19 section; 23 tests.

Add a **note linter** at `INPUTS_LOCKED` that emits a `PayrollControlCheck` WARNING for any
supervisor note **mentioning a money amount with no structured row behind it** for that
employee in that run.

Detect the *money*, not the phrase — a taka figure (`3000 Taka`, `TK. 18,000`, `৳400`),
then check whether that employee has a corresponding `VariablePayEntry`, `SalaryStructure`
segment or `OvertimeEntry` in the run. **Confirmed 2026-09-19:** a phrase list keyed to
"Recommended for the variable part" would rot the moment Phase C ships, because supervisors
stop writing that sentence once they have an uploader. A money-mention detector keeps
working, and it catches categories nobody has thought of yet.

This is how the migration happens without a flag day — notes stay legal, but the run tells
Finance *"this note says 3,000 Taka and there is nothing structured behind it."* It is
`PAYROLL_PLAN.md` design principle 9 ("exception review replaces spreadsheet review")
applied to the notes themselves, and it is cheap.

Related, and worth fixing while here: `apply_notes_upload` refuses every row once the run
hits `LOCKED_RUN_STATUSES`. Supervisors in practice send notes late — which is exactly why
Finance ends up keying things in by hand. Overtime already solved this shape in Q55
(accept into the most recently locked period, force `PENDING`, require an explicit
approval). Notes and variable pay should follow the same rule rather than hard-refusing.

---

## 4. What this plan deliberately does not do

* **No `SlaAchievement` model.** A dedicated component + a `PayRate` row + the C1 uploader
  covers every observed case and reuses effective-dated scoping that already exists. A new
  model would add status surface nothing transitions into — the thing
  `PAYROLL_PLAN.md`'s Run-&-results comment explicitly warns against.
* **No auto-approved salary changes.** B3 proposes; a human approves.
* **No DSDD-specific branch in `engine.py`.** Everything above lands as data
  (`TeamSalaryPackage`, `PayRate`) or as a general mechanism (segmented proration, a
  variable-pay uploader). Phase A and C1 fix company-wide gaps that DSDD merely exposed
  first — same posture as the AdOps work in 2026-09-07, which added `PayRate.resource_status`
  and `min_access_level` rather than an AdOps `if`.
* **No new model for Test Salary.** `VariablePayEntry` already carries deductions and
  decides the sign from `component.kind`; it needs the C1 uploader and a dated window, not
  a table of its own.

---

## 5. Questions to put to HR / Finance

1. ~~**Q34 (Test Salary)** and its follow-ups 1a–1g~~ — **all answered or withdrawn
   2026-09-19.** Phase D is fully specified; see its confirmation table. (1f — cap
   proration — is **withdrawn**: new joiners never carry this deduction, so a partial
   month cannot arise.)
2. ~~**Is the go-live package table (Trainee 11,000 / MARS 18,000 / Item Review 16,000) the
   complete DSDD set?**~~ **Resolved 2026-09-21 — MARS is the only team-specific rate.**
   Every other DSDD team (Item Review, BVD, Alpaca, Kompany, Entity Consolidation, …) pays
   16,000 on go-live. Seeded as a department-wide `TeamSalaryPackage` row (`team=None`)
   rather than a second single-team rate — MARS's team-scoped row outranks it via the
   `team_id` weight whenever both would otherwise match. See `PAYROLL_PLAN.md`'s 2026-09-21
   section.
3. ~~**Does go-live pay split mid-period?**~~ **Resolved 2026-09-19 — it splits.** Phase A's
   segmented proration is the correct fix (22 days @ 11,000 + 9 days @ 18,000), not a
   next-period step-up rule.
4. **Is SLA pay always 3,000 flat, for every DSDD team?** And is it strictly
   previous-calendar-month, or sometimes a quarter?
5. ~~**Who approves SLA pay?**~~ **Resolved 2026-09-19 — the department head, same as Test
   Salary; HR is not in the path.** One gate for all `VariablePayEntry` rows, and
   `PayComponent.approver_rule` is not needed. Department heads are not entitled to the
   3,000 themselves, so self-approval should never arise; if it ever does, the fallback
   approver is the head's own `reports_to`.
6. **Temporary suspension** — is it paid, unpaid, or partially paid? It has no owner
   anywhere in Orbis today. One row this month, but it is a real gap.

---

## 6. Suggested order

| | Phase | Why here | Size |
|---|---|---|---|
| 1 | ~~**A** — segmented proration~~ ✅ **built 2026-09-19** | Fixes a verified live overpayment and unblocks B. Valuable alone. | M |
| 2 | ~~**C1/C2** — variable-pay uploader~~ ✅ **built 2026-09-19** | Independent of A/B, removes 52 manual entries this month, fills a company-wide gap. | M |
| 3 | ~~**B** — `TeamAssignment` + `TeamSalaryPackage`~~ ✅ **built 2026-09-19** | Needs A to be worth anything. Largest piece. | L |
| 4 | ~~**D** — Test Salary deduction~~ ✅ **built 2026-09-19** | Q34 answered. Rides entirely on C1's uploader — a component, a `PayRate` row and a dated window. | S |
| 5 | ~~**E** — note linter~~ ✅ **built 2026-09-19** | Needs B, C and D to have something to reconcile against. | S |

Questions 1a–1d and 2–6 can be asked today and do not block starting Phase A. **1a and 1b
block Phase D only**; 1c and 1d are edge rules that can land after the main path.
