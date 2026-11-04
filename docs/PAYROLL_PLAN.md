# Payroll — Technical Implementation Plan

**Companion to [`PAYROLL_PROCESS.md`](PAYROLL_PROCESS.md)** (the business-facing process design awaiting HR/Finance sign-off).
**Status:** design complete. **Phase 1 (compensation master), Phase 2 (rate tables + input pipeline), Phase 3 (calculation engine + register — register-only, no approval chain) and Phase 4 (in-house TDS calculation) implementation started 2026-08-20/2026-08-24, ahead of formal sign-off, by explicit decision** — Phase 5+ (the approval chain, payslips/bank advice, loans/settlement, meals/Eid) still waits on sign-off and on the remaining open questions in `PAYROLL_PROCESS.md` §12. **The 2026-09-07 AdOps night-shift/OT/transport-allowance design (dated section below) is built** — `PayRate.resource_status`/`min_access_level`, `employees.DedicatedNightShiftAssignment` (+ request/approve views, profile card), the engine's Dedicated/Custom/Regular night split + real OT/transport line generation (previously nonexistent — see the dated section), the shared `payroll_inputs.classify_night_shifts()`/`collect_overtime()` used by both the engine and `/payroll/report/`'s five new columns, and updated AdOps seed rates — migrated, tested (8 new engine tests + permission + model tests), all 262+ payroll/employees/accounts tests passing. It's a within-Phase-2/3 correction and extension, not a new phase. **Not yet done:** a `dynamic_menu` nav entry for the assignment list/approval queue (reachable today via direct URL and the employee profile card, just not in the sidebar).
**Date:** 2026-08-06 (updated 2026-08-13 after reviewing HR's and Finance's actual working templates; updated 2026-08-20 after reviewing July 2026's actual filled-in master compilation and building Phase 1-3; updated 2026-08-24 after Finance provided their actual tax computation template and building Phase 4; updated 2026-09-07 after AdOps (Digital Advertising) supplied a rate workbook and a new Dedicated Night Shift concept was designed — see callouts below and `PAYROLL_PROCESS.md` §12 "Reopened" / "Newly identified")

---

### Digital Advertising night allowance starts at confirmation, not at go-live — 2026-11-03

**Reverses the 2026-09-07 rule** ("Trainee resource status is excluded from all three night-duty rate types", below). Digital Advertising asked that night counts appear in Support Info only once an employee is permanent, and confirmed that **the night allowance starts on the confirmation date**. Before this change the rule keyed on `JobInfo.resource_status = TRAINEE` *at calculation time*, for the whole period. That over-paid someone who went live mid-period, under-paid someone confirmed mid-period, and Support Info still showed trainee nights that were never paid.

- **Rule, per night shift date** (`payroll/inputs.py::classify_night_shifts`): a night dated before the employee's confirmation goes to `NIGHT_PRE_CONFIRMATION` instead of Regular/Custom/Dedicated. Transport days still count before confirmation, unchanged.
- **The confirmation date** (`payroll/inputs.py::confirmed_from`) is worked out in this order:
  1. `CustomUser.confirmation_date`;
  2. else `joining_date` + `probation_month` months;
  3. else unknown. An unknown date counts as confirmed unless the employee is currently `TRAINEE`, whose nights stay unpaid that period. This matches the old rule, so no established employee loses pay.

  HR is filling in every Digital Advertising confirmation date before the next payroll (only 19 of 850 had one on 2026-11-03), so options 2 and 3 are a bridge.

  **HR fills them in bulk at `/employees/bulk-upload/`** (added the same day). There are two HR-only columns after Joining Date, "Probation Months" and "Confirmation Date", and the template comes pre-filled with current values.
  - A blank cell leaves the value unchanged.
  - A typed confirmation date always wins.
  - Probation alone is turned into joining date + probation (the file's Joining Date if given, else the one on file), and the preview marks it "(calculated)". This is the same rule as the create-user form.
  - A typed date that disagrees with joining date + probation is a warning, never an error, because an extended probation is legitimate.
  - A confirmation date before the joining date is an error, as is probation outside 0–24.
  - The preview counts how many employees' confirmation date changes, with a night-allowance note.
  - Line managers' uploads containing either column are refused, like the other HR-only columns.
- **The confirmation date wins over the status:** someone confirmed but still marked Trainee is paid from that date.
- **Which departments it applies to comes from the data**, not a hard-coded name (`night_allowance_unpaid_before_confirmation`). A department whose active night-rate `PayRate` rows include a `resource_status=TRAINEE` row at 0.00 pays nothing before confirmation. Today that is Digital Advertising only. Those rows now mean "the rate before confirmation". Every other department, Data Solutions included, is unchanged.
- **The engine resolves night rates without the TRAINEE status.** Pre-confirmation nights are already out of the counts, so a confirmed Trainee gets the ordinary rate, not the 0.00 row. Each employee with unpaid nights gets a `calc_warnings` line ("N night shift(s) before confirmation — not paid").
- **Support Info, `/payroll/report/` and both Excel exports** gain a greyed-out **"Trainee nights (unpaid)"** column (`counts["NIGHT_UNPAID"]`). Because it comes from the same function the engine uses, the paid columns always equal what a run pays.
- **Already-calculated runs** change only on Recalculate. Tests: `payroll/tests_engine.py::AdOpsPolicyEngineTests` (confirmation section).

### Overtime gets a two-step review chain — Q4 fully reversed, Q56 superseded, 2026-10-17

**This reverses a decision this doc recorded as settled, twice over.** Q4 (2026-08-11, *"no approval gate — a supervisor's upload is final"*) survived the 2026-09-15 Q55 change for open periods; it no longer survives at all. Q56's *"HR-Payroll (HR ∧ Payroll) plus Payroll Admins approve"* is superseded by a per-employee two-step chain in which that pair is the fallback approver, not the only one. Design + decisions: `docs/OVERTIME_APPROVAL_CHAIN_PLAN.md`; business record: `PAYROLL_PROCESS.md` §12 questions 73–77.

**What now happens.** Every `OvertimeEntry` write — the uploader, the drawer, the GH auto-generator — lands `PENDING` (relabelled "Pending review"; the value is kept so existing rows and `?status=PENDING` links keep working). Then:

- **`REVIEWED`** by `accounts.permissions.can_review_overtime`: Manager tier (`OVERTIME_REVIEW_MIN_ACCESS_LEVEL_NAME = "Manager"` — the APM/PM/Deputy PM/Manager designations; its own constant so it can diverge from the night-shift tier), employee in managed scope (separated-inclusive), never your own row, no HR bypass. Stamped `reviewed_by`/`reviewed_at` (new columns, `payroll/0035`).
- **`APPROVED`** by `can_approve_overtime`: the employee's department head or a `Department.additional_managers` member (`overtime_approver_department_ids`, one cached query), HR-Payroll, Payroll Admins. Never your own row; a head's own entry falls to the head's `reports_to`, the `can_act_on_variable_pay` rule. **May approve straight from `PENDING`** — the review stamp is then the approver's.
- **`VOID`** on reject from either stage (a reviewer only from `PENDING`), `processed_by`/`processed_at` recording who. Same end state as a later void, deliberately no `REJECTED`.
- **`OvertimeEntry.reset_to_pending()`** whenever a `DECISION_FIELDS` value (seconds, type, team, date) changes on a signed-off row — the drawer compares a pre-save snapshot, `apply_upload` compares the existing row's figures and skips identical re-uploads into `ApplyReport.unchanged` (otherwise `resubmitted`). Void clears both stamps too; Restore goes to `PENDING`. The form's `status` field is gone.
- **`OvertimeWindow` is a pure date gate again**: `WindowDecision.status` is always `PENDING`. `can_manage_locked_overtime` narrowed a second time — it now only decides who may edit/void/delete a settled (`APPROVED`/`VOID`) row in a locked period, and `apply_upload`'s `skipped_locked` refusal keys off that settled state rather than the window status.
- **Engine unchanged.** `collect_overtime` still reads `APPROVED`. `OvertimeEntry.UNAPPROVED_STATUSES = (PENDING, REVIEWED)` replaces every `status=PENDING` count (`PayrollRun.pending_overtime_count`, the run-list column, the `PENDING_OVERTIME` warning check), so a reviewed-but-unapproved row never drops out of a warning while unpaid.
- **Bell:** `core/approvals.py` has "Overtime to review" (`overtime_review_inbox_pks` — the night-shift nearest-tier walk, generalised into `_compute_nearest_tier_inbox(user, tier_check)`) and "Overtime to approve" (`overtime_approve_inbox_pks` — department members for heads/additional managers; **the HR/Payroll-Admin fallback deliberately has no section**). Both link to the list's `?awaiting=me` (`overtime_awaiting_q`), and the decide views send `orbis:approvals-changed`.
- **Views/templates:** `_decorate_overtime_rows` computes `can_review`/`can_approve`/`can_reject` per row; `OvertimeEntryDecideView` takes `review|approve|reject` from the URL; the bulk view re-checks each id through the shared `_review_entry`/`_approve_entry`/`_reject_entry`, which re-check the per-row permission themselves. The `_OvertimeApproverRequired` global gate became `_OvertimeDeciderRequired` ("can act on anybody's") — the per-row check is what actually decides.
- **Readiness:** `manage.py check_overtime_approvers` (exit 1 while an overtime-filing department has no head and no additional managers; advisory on teams with no Manager-tier reviewer above them and on unmapped designations). On the dev copy three of five overtime-filing departments had nobody — the user is fixing prod data before the deploy.

**Not done:** an email to the reviewer on upload (bell only, v1 like every approval type), a per-decision reason on reject, an event ledger of re-submissions.


### Financial data protection — field-level encryption + money-visibility tightening, 2026-10-05 (Phase 1 deployed the same day)

Every monetary column in this app is plaintext in MySQL, and developers hold database
credentials. Decided 2026-10-05, spec in [`PAYROLL_DATA_PROTECTION_PLAN.md`](PAYROLL_DATA_PROTECTION_PLAN.md):
application-level Fernet encryption (`core/fields.py::EncryptedDecimalField` & co., key in
`PAYROLL_FIELD_KEYS`, separate from `SECRET_KEY`) on every per-employee money column —
`SalaryStructure`/`TeamSalaryPackage.gross_monthly`, the `PayrollRun`/`PayrollRunEmployee`/
`PayrollRunLine`/`PayrollVariance` amounts, `TaxComputation`, `EmployeeTaxDeclaration`,
`VariablePayEntry`/`TDSEntry.amount`, bank account numbers, and `documents.DocumentRequest.input_data`
(salary certificates). **Explicit exception, recorded here so it isn't mistaken for an
omission: `PayRate`, `MealDeductionTier` and the `TaxYear`/`TaxSlab`/`TaxExemptionThreshold`
tables stay plaintext** — they are rate cards and public law, not personal data, and
`payroll/rates.py` + the engine's meal-tier lookup filter/sort on them in SQL.

Two access-rule reversals ride along: **`payroll_salary` (gross salary list/upload/pending/decide)
moves from Finance-only to Payroll Admins only**, and the Service Book salary history +
bulk-onboarding workbook narrow from `can_edit_all` (HR + User Admins) to the HR group only
(the Users sidebar section opens to HR so they can still reach bulk create). Django admin
hides money fields from everyone but superusers and Payroll Admins. No data migration: there
is no real financial data yet, so the conversion migrations purge the converted tables under
a `PAYROLL_PURGE_ON_MIGRATE=1` guard. Register sort/filter/`Sum()` on money columns move
from SQL to Python (§4.4 of the spec); `calculate_run` gains a `PayrollRunEmployee.has_pay`
boolean for the "calculated rows" filter.

**Built the same day (Phase 1 + the `__str__` part of Phase 2):** `core/crypto.py`,
`core/fields.py`, `core/migration_utils.py::guarded_purge`, `core/admin_mixins.py`,
`manage.py generate_payroll_key` / `rotate_payroll_keys` (under `payroll/management/`),
migrations `payroll/0021`–`0025` + `documents/0006`, `core/tests_encryption.py`. Three
deviations from the spec, recorded in its Phase 1 progress notes: `DocumentRequest.input_data`
is **backfilled** in place rather than purged (letters are real HR records); the no-amount
`__str__` change was pulled forward because the admin changelist renders `str(obj)` in markup
the money mixin can't reach; and `order_by()`/`Avg()`/`F()` on an encrypted column are caught
by a source-scan test rather than at runtime, since Django never asks the field about them.
Deploy procedure: `deploy/README.md` "Payroll field encryption".

### Gross salary upload rejected separated employees outright, 2026-09-21 — real bug, not a restriction to relax

Same bug class as the overtime uploader's 2026-09-08 fix (dated section below), found the same
way: real data (a 1,500-row gross salary file with a July 1 backdated batch) hit it directly.
11 employees who resigned between the effective date and the upload date were rejected as "No
active employee with ID X," because `parse_salary_upload`'s employee lookup filtered
`is_active=True` — wrong for a backdated correction, since `deactivate_past_lwd` deactivates
an employee the day after their last working date regardless of what date a later correction
is *for*.

**Fixed identically to the overtime uploader**: dropped the `is_active=True` filter from the
lookup and added the existing, already-public `employed_on(employee, effective_from)` check
per row instead (`payroll/upload.py:231`, unchanged — this reuses the exact helper built for
the 2026-09-08 fix, not a new one). A genuinely wrong employee ID/date is still caught; only a
resigned employee's legitimate pre-separation correction now goes through. Unlike the overtime
fix, no second scope-check fix was needed — `apply_salary_upload` has no per-row
`visible_employee_pks` filtering at all (gross salary is Finance-only, company-wide, gated
purely by `ProtectedView`, not managed scope). Tests:
`payroll/tests.py::SalaryUploadTests` (+2).

### Two upload-form tweaks after real-data testing — 2026-09-21

Both raised by the user after preparing real September data for the three new uploaders to
test against.

**SLA achievement drops its "Met SLA" Y/N column.** A supervisor only ever lists people who
achieved it, so a row's presence in the file already means "met" — the column served no
purpose. `parse_sla_upload` is now 2 columns (`Employee ID | SLA month`); a blank month is an
untouched line, same convention as every other uploader here. **One real risk this created**:
the template still lists every managed employee for convenience, and it used to pre-fill the
SLA month for all of them (harmless before, since the Y/N column defaulted to "not met"). With
that gate gone, pre-filling the month would mean an unedited download-then-upload pays
everyone by accident — so `build_sla_xlsx_template` now leaves the month cell blank for every
row instead, moving the safety net there.

**Go Live's column is "Team," not "New Team."** Purely a label change — supervisors read "New
Team" as needing to know the *old* team too, which the sheet was never asking for. Renamed the
column header, the `RowError.raw` display key, and the section's docs; the internal
`ParsedGoLiveRow.new_team` field name is unchanged (not user-facing).

### Go Live bulk upload — a structured front end for the 2026-09-19 go-live machinery — 2026-09-21

Before this, a Data Solutions trainee's go-live was still one-employee-at-a-time via the HR
profile-edit drawer — the exact free-text-note problem `employees.TeamAssignment` +
`payroll.TeamSalaryPackage` (2026-09-19) were built to close, just not yet reachable in bulk.
Supervisors kept writing "Go live date, Salary as per the new package for MARS (TK. 18,000):
Date: September 07, 2026" into the Supervisor Note column instead — 19 of them in September's
real file — because there was nowhere else to put it.

**One service-layer extension made this possible.** `employees.signals.sync_team_assignment`
always stamped `effective_from = today`, which is wrong for a go-live uploaded a few days
after the fact (the model's own docstring: "HR usually updates a few days after the real
go-live"). It now takes an optional `effective_from` kwarg — used by the new uploader, left
untouched (defaults to today) for the two existing signal call sites, so this is purely
additive.

**`payroll/upload.py`'s Go Live section** (`Employee ID | New Team | Go-live Date`, 3
columns): per row, `CustomUser.team`/`JobInfo.resource_status` are updated via queryset
`.update()` (bypassing the ordinary post_save signal so the row isn't dated "today" before it
can be corrected), then `sync_team_assignment(employee, reason=GO_LIVE,
effective_from=go_live_date)` is called directly — closing the Trainee-team row at
`go_live_date - 1 day`, opening the new one at the real date, and firing the existing
`propose_salary_for_assignment` with that date. **The amount is never typed** — it's resolved
from `TeamSalaryPackage` (the 2026-09-21 MARS-vs-everyone-else fix above), so an upload can
never disagree with the seeded policy.

**Trainee-to-Live only, deliberately.** An employee not currently `TRAINEE` is rejected — this
is the specific go-live transition, not a general transfer tool; a correction goes through the
profile drawer or admin, same as any other `TeamAssignment` correction today. This also makes
re-uploading the same row after a successful run a safe no-op: the employee is no longer
`TRAINEE`, so it's skipped rather than reprocessed.

**The org record and the salary proposal are reported as two separate guarantees, not one.**
`propose_salary_for_assignment` returns `None` — silently, by design — when the employee has
no current `APPROVED` `SalaryStructure` to revise at all (a brand-new joiner's starting salary
comes from the compensation master, not from here). The uploader still applies the team/status
change in that case, but the report says plainly that no proposal was created, rather than
implying success on the pay side too.

**Access is narrower than the other three managed-scope uploaders next to it** (Overtime/SLA/
Test Salary all grant bare `"Supervisors"`) — this one mutates `CustomUser`/`JobInfo` directly,
not just a payroll input row, so its `ProtectedView` group list is `payroll_ops` only
(HR/Finance/Payroll/Payroll Admins/User Admins). `ManagedScopeRequiredMixin` is still the real
per-request gate underneath, same mechanism as the other three.

Joins the Upload Center (below) as its fifth card. Tests: `payroll/tests_go_live.py` (18,
including a full org-record + proposal end-to-end and the `sync_team_assignment` kwarg itself).

### DSDD go-live package confirmed: MARS is the only team-specific rate — 2026-09-21

Resolves `DSDD_PAYROLL_AUTOMATION_PLAN.md` §5 Q2, open since 2026-09-19: *"Is the go-live
package table (Trainee 11,000 / MARS 18,000 / Item Review 16,000) the complete DSDD set? The
notes only ever mention two destination teams, but DSDD has 25."*

**Confirmed by the user 2026-09-21: MARS pays 18,000; every other DSDD team pays 16,000.**
"Item Review" in the original seed was never a special case of its own — it just happened to
be the only other team anyone had gone live to yet in September's real notes (8 MARS, 11
Item Review, per the DSDD plan doc's table). The other 23 teams (BVD, Alpaca, Kompany, Entity
Consolidation, …) were previously unconfigured, which would have made a go-live onto any of
them resolve to **no package at all** — a silent gap the uploader work surfaced before it
could produce a wrong or missing salary proposal in practice.

**Fixed in `seed_payroll_components.py`**: the second `TeamSalaryPackage` row changed from
`team="Item Review"` to `team=None` (department-wide) at 16,000 — a fallback for every DSDD
team except MARS, not a second single-team rate. No model or resolution-logic change was
needed; `rates.py::resolve_team_salary_package`'s existing most-specific-wins scoring already
does the right thing once the row itself is scoped correctly — MARS's team-scoped row
(`team_id` weight 4) outranks the department-wide fallback (weight 1) whenever both would
otherwise match, and the Trainee row's `resource_status` weight (8, dominant) still beats
both for anyone not yet live, on any team. Verified directly: MARS/LIVE → 18,000, BVD/LIVE →
16,000, Item Review/LIVE → 16,000, Trainee-on-MARS (not yet live) → 11,000.

Not yet deployed anywhere (`TeamSalaryPackage` had zero rows in dev before this session), so
this is a straight fix to the seed source, not a data migration — nothing needs correcting
in an already-seeded environment.

### Upload Center: one Payroll submenu for every bulk uploader — 2026-09-21

Before this, each bulk uploader either got its own sidebar submenu ("Upload salary", rank
25) or none at all — SLA and Test Salary were reachable only via a button on the Variable
Pay list, with no standing menu entry. Adding a fifth uploader would have meant deciding,
again, whether it earned a submenu row of its own.

**Replaced the standalone "Upload salary" submenu with a single "Upload Center" page**
(`payroll_upload_center`, `/payroll/uploads/`) listing every uploader as a card: Overtime,
SLA achievement, Test Salary, and Gross Salary. `payroll/upload_views.py::UPLOAD_CENTER_ENTRIES`
is a plain list of `UploadCenterEntry` — a future uploader is one entry there, not a new
`ORBIS_SUB_MENUS` row.

**Per-card visibility mirrors each destination's own real gate, not the broad
`ProtectedView` group list every uploader shares under `dynamic_allowed_users`.** That list
is deliberately wide (it also covers HR/Finance/Payroll-ops oversight), so checking only
group membership would show, say, a plain "Supervisors" member with zero reportees the
Overtime/SLA/Test Salary cards and then 403 them on click. Overtime/SLA/Test Salary reuse
`has_managed_scope` (the actual gate `ManagedScopeRequiredMixin` enforces); Gross Salary has
no equivalent function of its own — `UploadSalaryView` is plain `LoginRequiredMixin`, gated
purely by `ProtectedView.allowed_groups` (Finance, company-wide) — so its card re-checks
that same `ProtectedView` row directly instead.

**The existing in-context upload buttons are untouched** (Overtime list's "Upload overtime",
Variable Pay list's "Upload SLA achievement"/"Upload test salary", Gross Salary list's
"Upload salary") — those stay as shortcuts when you're already on the relevant page.
Payroll run notes upload also stays where it is (a button on a specific run's detail page)
rather than joining the hub — it's inherently per-run, not a standalone destination.

Removing "Upload salary" from `ORBIS_SUB_MENUS` doesn't delete its old DB row —
`update_or_create` only ever creates/updates rows still present in the source table, never
prunes ones that drop out (the same lesson as the Payroll > Runs/Payrolls duplicate fixed
the same day). `manage.py remove_payroll_upload_salary_menu --apply` does that cleanup once
per environment.

### Test Salary: day count is entered directly, not derived from a date window — 2026-09-21

**Revises the 2026-09-19 build below** (not yet in production — no migration had shipped,
so this is a straight edit of that migration rather than a follow-up one).

The original build read a From/To window per upload row and derived the day count from the
dates, specifically so a hand-typed count couldn't be wrong the way one of September's 15
notes was ("6 days" for 07–13 Sep, which is 7). That held up until the next real month's
data: an employee can fall under test across **several disjoint stretches of a single
period**, and a single From/To window can't represent that — supervisors were always going
to have to hand-sum multiple windows into one number before they could even fill in the
sheet. Deriving the count from dates only mattered if the dates were the ground truth; once
they can't represent the real shape of the data, they stop earning their place.

**What changed:**

| | Was | Now |
|---|---|---|
| Upload columns | `Employee ID \| From \| To` | `Employee ID \| Days` |
| `VariablePayEntry` fields | `window_start`, `window_end` (nullable `DateField`s) | `days` (nullable `PositiveSmallIntegerField`) |
| Rows per employee per period | One per window (could be several) | Exactly one — `source_key` is now the constant `"TEST"`, not a window-derived string |
| Re-upload semantics | Recomputed that window in place, other windows untouched | **Replaces** the employee's day count for the period outright |
| Already-unpaid (LWP) day handling | Auto-detected and excluded via `payroll.inputs.collect_lwp_days_by_date` | **Not automatic** — there's no date range left to check the roster against, so the uploader is expected to net out unpaid days before entering the number |
| Overlap detection | Rejected a second window overlapping an existing one | Doesn't apply — one row per employee per period, nothing to overlap |
| Sanity bound | Window had to fall inside the period being uploaded for | Day count must be a whole number from 1–31 |

Cap logic is unchanged in shape: `min(days × 200, 6000)`, per employee per period — it's
simpler now since there's only ever one row to cap rather than a shared pool across windows.
Trainee rejection, no-rate handling, scope, and the PENDING → department-head-approval flow
are all unchanged.

Migration `0020` was edited in place (added `days`, dropped `window_start`/`window_end`)
rather than layered under a new one, since it had not been applied anywhere outside this
session's local development. `payroll/tests_test_salary.py` was rewritten around it.

### New-table migrations must carry the collation CONVERT step — 2026-09-19

Noting this because it was missed once in this very session and cost a debugging detour.

**The rule:** any migration creating a table with a FK to `accounts.CustomUser` needs the
`_match_existing_collation` `RunPython` step and `atomic = False`. `CustomUser`'s pk is a
UUID, which MySQL stores as `char(32)` — a *string*, so it carries a collation, and MySQL
refuses a FK whose collations differ (`OperationalError 3780`). `CREATE TABLE` takes its
collation from the **database default**, not from the table it is about to reference, so a
schema whose default has drifted from its own tables produces a table that cannot be
FK'd. A `bigint` FK is never affected.

The step is not new — `employees/0008_designationhistory.py`, `attendance/0002`,
`leave/0010_cpl_grant.py`, `payroll/0002` and others all carry it, and `deploy/README.md`
documents the matching `ALTER DATABASE` pin. `employees/0013_teamassignment.py` was written
by mirroring `DesignationHistory`'s *model* without its *migration*, and duly failed on a
local database whose default had drifted to `utf8mb4_unicode_ci` while all 132 of its tables
were `utf8mb4_0900_ai_ci`. Fixed, and added to `payroll/0019_teamsalarypackage.py` too —
that one has no UUID FK today, but the house converts every new table so a later FK cannot
silently reintroduce the problem.

`atomic = False` matters: the step issues DDL, which Django forbids inside the transaction
an atomic migration would wrap it in. FK creation is deferred to schema-editor exit, which
is what makes the convert-then-FK ordering work.

**Verified, not assumed.** A throwaway database was built with the correct default, migrated
fully, rolled back to before these migrations, drifted to `utf8mb4_unicode_ci`, and
re-migrated: all four migrations applied, and `employees_teamassignment` /
`payroll_teamsalarypackage` both came out `utf8mb4_0900_ai_ci` with their three FKs to
`accounts_customuser` intact — while the database default was still wrong.

**Production was checked the same day and is clean** — database default, all 132 tables,
`accounts_customuser.id` and the MySQL server default are all `utf8mb4_0900_ai_ci`. There is
nothing to do before deploying, and no reason to re-run the `ALTER DATABASE` in
`deploy/README.md` against prod. It was only ever the *dev* database that had drifted.

**Still true for a new environment, and the mechanism is the reverse of what you'd guess:**
`accounts/0001_initial` has **no** CONVERT step, so `accounts_customuser.id` just follows
the database default — while the apps that *do* carry the step (policies, attendance, leave,
payroll, employees) force their own tables to `utf8mb4_0900_ai_ci`. On a fresh
`utf8mb4_unicode_ci` database those two disagree and the build dies at `policies_policy`'s
FK to `accounts_customuser` (verified 2026-09-19 on a throwaway database:
`accounts_customuser.id` came out `utf8mb4_unicode_ci`). **So the per-migration CONVERT step
is not a substitute for pinning the default on a new database** — it is only correct because
the pin is expected. `deploy/README.md`'s "MySQL collation" section is the single place this
is written down.

### Notes reconciled against structured inputs — the note column stops refilling, 2026-09-19

Phases B–D moved Data Solutions' three recurring pay events out of the Supervisor Note
column. `payroll/notes_lint.py` is what stops it quietly filling back up: at calculation
time, any note stating a **money amount** that no structured row accounts for raises an
`UNSTRUCTURED_NOTE_AMOUNT` WARNING check against that employee.

**It detects the money, not the phrase.** An earlier draft of the DSDD plan proposed
matching known sentences ("Recommended for the variable part", "Go live date"). That would
have rotted the moment the uploaders shipped — supervisors stop writing those sentences
once they have a form. A taka figure keeps working and catches categories nobody has
thought of yet.

**An amount counts as accounted for** when it equals a non-rejected `VariablePayEntry.amount`
for that employee in the period, or a `SalaryStructure.gross_monthly` in force during it
(what a go-live note quotes). Matching the **amount** rather than "does this person have any
input at all" is the point: someone with approved overtime and an unstructured 3,000 SLA
note is still flagged. Rejected variable pay deliberately doesn't count — a note describing
a payment that was turned down is worth surfacing.

**Validated against all 125 real September notes**: 108 detected (89 × 3,000 SLA, 8 × 18,000,
11 × 16,000), **zero false positives** on the dates those notes are full of ("09/09/2026",
"30 August 2026"). A bare number never matches — an amount only counts next to a currency
token.

**Known limitation, accepted:** a note implying money without naming a figure is not caught.
The 13 Test Salary notes say "13 days Test Salary: From August 26 to September 07" and name
no amount. That is the honest trade for not maintaining a phrase list, and it matters less
now Test Salary has its own uploader — a window filed as a note simply never appears on the
variable-pay queue.

**Bug found by the tests, not the data:** the amount regex used an unparenthesised
alternation, so `\d{1,3}` won outright on a comma-less figure with no trailing context to
force backtracking — **"Tk 11000" read as 110**. The real September notes escaped it only
because they happened to use a comma ("TK. 18,000") or a trailing word ("3000 Taka"). Fixed
by grouping the alternation and requiring the comma form to contain a comma; there is a
regression case for it.

**`apply_notes_upload` no longer refuses a locked run** (same change). The old posture closed
bulk note uploads at input lock while the per-employee drawer beside it had no such gate — so
the same edit was allowed one at a time and refused in bulk, which mostly taught supervisors
to email Finance instead. Notes never feed the engine; there is nothing about a locked run a
note can corrupt. What pushes back on a late note now is the linter above, not a refusal.
`NoteApplyReport.skipped_locked` is retained (always empty) so the report shape and template
stay stable.

Tests: `payroll/tests_notes_lint.py` (23), including every note category verbatim from
September's file.

### "Test Salary" is a deduction, not a pay category — Q34 answered and built, 2026-09-19

**The day-count mechanism described below (derived from a From/To window) was revised
2026-09-21 — see that dated section above.** The deduction rules on this page (200 Tk/day,
capped at 6,000, trainees excluded, DSDD only) are unchanged; only how the day count reaches
the system changed.

**`PAYROLL_PROCESS.md` question 34 is resolved**, and the answer is that the question was
wrong. It had been open since 2026-08-13 asking what *"a short trial-period daily rate on
11 new hires"* was and whether it was *"a probationary tier that precedes Trainee"*. It
survived the 08-20, 08-24 and 09-07 follow-up rounds unanswered because none of that
describes the thing: September 2026's 15 rows had **six months' to five years' tenure**,
**none were Trainees**, and **all already drew a full gross** of 25,000–44,500.

**What it actually is.** Data Solutions employees carry a **6,000 Tk variable portion**
inside their (already-stored) gross. A day under test forfeits one thirtieth of it —
**200 Tk/day**. "13 days Test Salary" means 13 × 200 = **2,600 deducted**. Update Q34 in
`PAYROLL_PROCESS.md` to this rather than leaving the misleading question standing.

Confirmed rules, each of which the free-text note could not express:

| Rule | Why |
|---|---|
| Day count derived from the dates, **never** read from the sheet | One of September's 15 notes was miscounted ("6 days" for 07–13 Sep, which is 7) |
| Weekly offs and holidays inside a window **are** charged | The rate is already 6,000 ÷ 30 *calendar* days, and supervisors' own counts were inclusive |
| An already-unpaid (LWP) day is **not** charged; a half-day costs half | That day's whole pay is gone already — charging 200 on top penalises it twice |
| Capped at 30 × the daily rate per employee per period, across all windows | The whole variable portion is all there is to forfeit |
| Trainees rejected at entry | They are on a flat 11,000 until go-live and never carry it |
| DSDD only — no global fallback rate | Another department surfaces as "no rate configured", never a silent wrong deduction |

**Built entirely on Phase C's machinery** — `TEST_SALARY_DEDUCTION` `PayComponent`, a
`PayRate(rate_type=TEST_SALARY_DAY, basis=PER_DAY, 200)` scoped to the department, and a
sibling parse/apply pair in `payroll/upload.py` writing the same `VariablePayEntry` rows
into the same queue with the same department-head gate. **No new model**, no new approval
path, no engine change. The cap is derived as `rate.amount × 30` rather than stored
separately, because 200 *is* 6,000/30 — the two move together by definition.

`VariablePayEntry` gained nullable `window_start`/`window_end` (migration `0020`). Those
earn their place on the shared table: detecting that two windows **overlap** — so the same
day is never deducted twice — needs real dates, not a parsed `source_key` string.

**Amounts are computed at apply time**, like the SLA uploader's. If the roster changes
afterwards (an LWP day added inside a window), re-uploading the same window recomputes it —
idempotent on `source_key`, and the report says "updated".

Applied to September's real 15 windows: **31,800 Tk** across the department, with `54614`'s
hand-counted off-by-one corrected from 1,200 to 1,400.

Tests: `payroll/tests_test_salary.py` (28).

### Team assignment history + team salary packages — the go-live becomes data, 2026-09-19

Phase A taught the engine to split a period at a salary change; this supplies the date and
the amount. Before it, Data Solutions' whole go-live policy (Trainee 11,000 / MARS 18,000 /
Item Review 16,000) existed only in free-text Supervisor Notes, and `CustomUser.team` /
`JobInfo.resource_status` are single mutable values that record *what* someone is, never
*when* they became it. See `DSNDD_PAYROLL_AUTOMATION_PLAN.md` Phase B.

**`employees.TeamAssignment`** — effective-dated (department, team, resource_status), one
open row per employee, same shape and auto-sync signal as `DesignationHistory`. HR's
ordinary "move to MARS, set Live" profile edit *is* the dated record. Two refinements over
DesignationHistory's signal, both forced by real behaviour:

- The pair spans two models (`CustomUser.team`, `JobInfo.resource_status`) and a go-live
  changes both, so one edit would otherwise leave a one-day orphan row. A change on a day
  whose open row already starts today **amends in place**.
- JobInfo is often created after CustomUser, so an assignment whose `resource_status` is
  still blank is **completed**, not superseded — learning someone's status is not a
  transfer. Deliberately *not* "amend whenever the row is still INITIAL": a genuine
  transfer months after hire must still stack, or the history is lost.

**`payroll.TeamSalaryPackage`** — what a (department, team, resource_status, designation)
is paid. This does **not** reopen the gross-only decision (locked 2026-08-11):
`SalaryStructure` remains the sole record of what a person is actually paid, and the
package is the *policy* it should match. An individually negotiated salary simply doesn't
match its package, which is information, not an error.

**Most-specific-wins is shared with `PayRate`, but the weights are not**, and that
distinction is load-bearing. For a PayRate, `resource_status` is a carve-out from an
otherwise-applicable rate (AdOps Trainees earn no night allowance) — a +1 tiebreak. For a
salary package it is the **dominant** axis, weighted above team: a Data Solutions trainee
is on 11,000 whatever team they have been parked on, and only becomes team-priced at
go-live. Weighting team higher put a trainee sitting on MARS on 18,000 *before* their
go-live date — caught by a test, and the exact overpayment Phase B exists to prevent.
`rates.py::_score_scope` is shared; `_SPECIFICITY_WEIGHTS` and `_PACKAGE_WEIGHTS` are not.

**The glue proposes, never applies.** `services.propose_salary_for_assignment` resolves the
package for a new assignment and writes a **PENDING** `SalaryStructure`; the employee keeps
being paid their current figure until Finance approves it at `/payroll/salary/pending/`.
Automating the arithmetic of a go-live is the point; automating the decision to change
someone's pay is not. `SalaryStructure.Status.PENDING` already existed with no flow using
it. It returns None when the employee has **no current structure at all** — a new joiner's
starting salary comes from the HR file via the bulk uploader, and proposing a package for
everyone without one would put a row in Finance's queue for every new hire.

**`services.supersede_salary_structure` is now the single writer** of "close the current row,
open the next": `apply_salary_upload` and the go-live approval both call it, so they cannot
drift on revision numbering or on what `effective_to` the closed row gets — the same reason
attendance's `_approve_correction` is shared.

**`SALARY_PACKAGE_MISMATCH`** — a WARNING control check when a paid gross doesn't match the
resolved package. Catches both a go-live whose proposal was never approved and a stale
package. Never BLOCKING. Resolved from one pre-loaded list per run
(`rates.active_team_salary_packages`), not a query per employee — the same in-memory path is
tested to agree with the querying path.

**Deploy step:** `manage.py backfill_team_assignments` seeds an INITIAL row for the ~1,527
employees who predate the model. It deliberately uses `bulk_create` (bypassing the signal)
so it does **not** propose salaries — backfilling history is a records exercise, not a pay
review.

Tests: `payroll/tests_team_package.py` (21), including an end-to-end where HR moves a
trainee to MARS and payroll ends up paying 22 days at 11,000 + 9 at 18,000 with nobody
typing a figure.

### Proration divisor confirmed: always 30 — `payable_days_basis` finally wired, 2026-09-19

**Confirmed with HR and Finance:** the proration divisor is **always 30**. That is what
`PayrollSettings.payable_days_basis` has defaulted to (`FIXED_30`) since Phase 1 while
being **read nowhere** — the engine divided by the calendar length of the period instead.
It also matches the Data Solutions "Test Salary" deduction, priced at 6,000 ÷ 30 = 200/day.

**This is not "divide by 30".** The 16→15 cycle runs **28 to 31 days** (16 Feb – 15 Mar is
28; six periods in 2026 are 31), so a naive `eligible_days / 30` would dock a fully-worked
March by 6.7% and overpay a fully-worked September by 3.3%. The rule that actually holds:

| Case | Paid |
|---|---|
| Employed the whole period | exactly 30 days' pay, whatever the calendar says |
| Employed part of the period | one 1/30 per day employed |
| Unpaid leave | costs one 1/30 per day |

Implemented by scaling a full period's segment spans onto the 30-day basis
(`span_scale = 30 / eligible_days` when employed throughout, else `1`) and dividing every
segment by `basis_days`. Under `CALENDAR` the scale is always 1 and the whole thing reduces
exactly to the pre-2026-09-19 behaviour, so the setting is now real rather than decorative.

**What changed in money terms.** Periods of exactly 30 days are unaffected. Elsewhere:

* a full period now pays exactly one month's gross in **every** month (previously February's
  28-day period underpaid and the 31-day periods overpaid — both invisible because the
  divisor tracked the calendar);
* an unpaid day now costs gross ÷ 30 rather than gross ÷ 31 in a long month;
* a joiner or leaver is paid `days employed × gross / 30`, so **the same 15 days pay the
  same in March as in September** — previously 15/28 vs 15/31.

Tests: `payroll/tests_engine.py::ProrationBasisTests` (9), covering all three period
lengths, LWP, half-days, and joiners in both a short and a long period. Three expectations
in `SalarySegmentTests` moved with the divisor and are re-derived in their comments.

### Variable pay gets a bulk uploader and its own approval gate — 2026-09-19

**The gap:** `VariablePayEntry` had no bulk path at all. The only way in was
`PayrollRunEmployeeAdjustView`'s per-employee drawer — 52 SLA bonuses in Data Solutions'
September 2026 file meant 52 drawer visits. Rows were written straight as `APPROVED` with
`reason` defaulting to the literal `"Manual test adjustment"`, so there was no supervisor
attribution and no approval step; `PayrollInputBatch.InputKind.VARIABLE_PAY` had existed
since Phase 2 with **nothing ever writing it**; and with no unique constraint nothing
stopped the same bonus being entered twice. Overtime got all of this in the 2026-09-15/16
work. Variable pay never did.

**Built (docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase C):**

- **`/payroll/variable-pay/`** — one generic queue over every `VariablePayEntry` (SLA, the
  coming Test Salary deduction, bonus, incentive, disciplinary) with single and bulk
  approve/reject, the same `form=`-attribute bulk-select shape as the overtime list.
- **`/payroll/variable-pay/upload-sla/`** — an SLA-specific intake. Columns are
  `Employee ID | SLA month | Met SLA`; **there is deliberately no Amount column.** The
  supervisor attests to the achievement and the system prices the row from a
  `PayRate(rate_type=SLA_INCENTIVE)` at apply time, so the ৳3,000 stays effective-dated
  data that can be carved out per team later. A row with no resolvable rate is skipped and
  reported, never written as zero — a 0.00 row is indistinguishable from a paid one on the
  register.
- **`VariablePayEntry.source_key`** (e.g. `"SLA:2026-08"`) + a partial unique on
  `(employee, period, component, source_key)`. This carries **the event the row pays for,
  which is not the period it is paid in** — "Meet SLA-August" is paid in the 16 Aug–15 Sep
  run, and a missed month can be claimed retroactively alongside the current one (employee
  3503 claimed July *and* August in one September note). Two rows, same period, different
  source_keys: a retro claim is now distinguishable from a double-payment, which the
  free-text note never was. Manual entries leave it blank and stay unconstrained.

**The approval gate is the department head, not HR** (`accounts/permissions.py::can_act_on_variable_pay`,
confirmed with the business 2026-09-19). HR reviews and Finance calculates and pays, but
neither approves. Deliberately **not** managed scope — that would let any team leader sign
off their own team's money. Nobody approves their own row; a department head's own row (they
aren't entitled to SLA pay, so this shouldn't arise) falls to their `reports_to`, not to HR.
Superusers are break-glass for a vacant headship.

**So the two payroll gates split by input table, not by component:** `OvertimeEntry` keeps
Q55's HR∧Payroll gate unchanged; `VariablePayEntry` uses the department head. An earlier
draft proposed a per-component `approver_rule` field for this — **not built**, because
authority turned out not to vary by component.

**No engine change was needed.** `calculate_run` already filtered variable pay to
`status=APPROVED`, which is exactly what makes an unapproved row unpayable by construction —
the same property that makes overtime's PENDING state safe.

**Scope note for whoever picks up Test Salary:** the *intake* is SLA-specific because the
pricing rule is; the *queue* is generic. Test Salary's uploader (`Employee ID | From | To`,
priced per day) should be a sibling parse/apply pair in `payroll/upload.py` sharing the same
write path, the way `gh_overtime.py::_apply_candidates` lets two presence sources share one
set of write rules — not a second queue.

Tests: `payroll/tests_variable_pay.py` (24). Migration `0018`. Seeded by
`seed_payroll_components` (component + ৳3,000 DSDD rate) and `seed_orbis` (nav +
`ProtectedView` rows).

### Mid-period salary changes now pay per-segment — a live overpayment bug, fixed 2026-09-19

**Found while scoping Data Solutions' trainee go-live automation (`DSNDD_PAYROLL_AUTOMATION_PLAN.md`), not introduced by it.** The engine could not pay a salary change that lands mid-period: it paid the **new** rate for the whole period.

`engine.py` bucketed at most one `SalaryStructure` per employee (`{s.employee_id: s for s in ...}`) filtered to `status=APPROVED`, then multiplied that one `gross_monthly` by a single period-wide `proration_factor`. But `upload.py::apply_salary_upload` closes the previous row as **`SUPERSEDED`** — so the `APPROVED` filter discarded the old rate entirely and the engine only ever saw the new one. Verified by running the real engine against a 2026-09-07 go-live (11,000 → 18,000, period 16 Aug – 15 Sep): it paid **18,000.00** for all 31 days where the correct figure is **13,032.26** — 4,967.74 over, per employee, on 19 employees in one department in one period.

This was never DSDD-specific: every mid-period increment, promotion, demotion or transfer company-wide was mispriced the same way. It was invisible only because no `SalaryStructure` rows existed yet — the first upload creates revision 1 with nothing to supersede, so **the bug bites on the second upload**. It also meant design principle 1 ("backdated promotions produce arrears arithmetically") was not actually true.

**Fix — stage 3 became segmented.** `engine.py::_salary_segments()` splits the employee's slice of the period into one segment per structure in force, and stage 3 emits **one `GROSS_SALARY` line per segment**, each with its own `full_amount`, its own proration and the arithmetic in `calc_note`. Supporting changes:

- The structure query now takes `status__in=(APPROVED, SUPERSEDED)`. **A superseded row is still what was true for its own dates** — excluding it is exactly what lost the old rate. `DRAFT`/`PENDING`/`CANCELLED` are not history and stay out.
- `inputs.py::collect_lwp_days_by_date()` (new) returns per-date weights so an unpaid day is charged against the segment it fell in, rather than the whole period's single rate. Same one query — `date` added to the existing `.values()`. `collect_lwp_days()` remains, now a sum over it, so the whole-period figure on `PayrollRunEmployee.lwp_days` and the reports can't disagree with it.
- Everything downstream needing one gross figure (night/OT rate banding, meal tier, TDS) uses the structure in force at the **end** of the employee's window — those are properties of the current contractual rate, not of a given day. Identical to the old behaviour whenever there's one structure.
- Overlapping structures (impossible through the uploader, but MySQL can't enforce the partial unique — Risk 4) are **clipped in favour of the later revision and warned about**, never double-paid. Structures that don't cover the whole employed window warn too, rather than silently paying the uncovered days at some other segment's rate.

**The regression guard is the equivalence property:** an employee with one structure produces exactly one segment and byte-identical output to the pre-segment engine. Asserted directly in `tests_engine.py::SalarySegmentTests`, alongside the 22/9 split, LWP landing in the correct segment, a mid-period joiner who also changes rate, and DRAFT rows being ignored. No downstream change was needed — `views.py::_attach_earning_breakdown` and `exports.py::_earning_breakdown` both already `Sum()` by component code (OT already emits several lines of one component).

**`PayrollSettings.payable_days_basis` deliberately left alone.** It defaults to `FIXED_30` and is still read nowhere; the engine still divides by the calendar length of the period. Switching it would move every payslip in a 31-day period by ~3% and needs a defined cap rule first (a full 31-day period would otherwise prorate to 31/30 = 103% of gross). That is its own decision, not a rider on a bug fix — see `DSNDD_PAYROLL_AUTOMATION_PLAN.md` §2.4.

### Template review, 2026-08-13

Reading HR's `HR Payroll File Template.xlsx` and Finance's `Finance Monthly_Salary_Template.xlsm` / `Monthly_Salary_Template_Reporting.xlsm` directly — formulas, not just column names — surfaced detail the 2026-08-11 meeting didn't cover. Nothing here overturns a locked decision outright, but several locked decisions turned out to be narrower than the actual formulas; those are marked **narrows** below. Full business framing is in `PAYROLL_PROCESS.md` §12 (questions 24–33). Technical impact is called out inline at each affected model/stage below.

### Master sheet review, 2026-08-20

Reading `Master Sheet_July_2026.xlsx` — the real HR master file used to run July 2026's payroll (1,544 employee rows across five sheets: a working master, department-specific OT breakdowns for AdOps and for Software/Creative/FlashTalking, and a pivot summary) — rather than a blank template. This confirms the 2026-08-13 figures with real production numbers and adds detail no blank template could show: exact flat OT/night rates (`PAYROLL_PROCESS.md` §12 questions 37–39), plus 248 free-text `Team Comments` entries revealing how HR currently tracks resignations, LWP reasons, confirmations and ad hoc corrections by hand — and three pay-related labels not documented anywhere else, raised as new open questions (§12 questions 34–36) rather than designed speculatively here:

- **"Test Salary"** — a short trial-period daily rate on 11 new hires this period (e.g. *"6 days Test Salary, From June 25 to June 30, 2026"*), distinct from the existing Trainee resource status. No model change proposed until HR confirms the rate/rule (question 34).
- **A dormant third AdOps OT column**, `Adops regular special 300 rate OT`, present in the live template structure but unused in July — unclear whether it's a real third rate tier alongside the 200/250 Tk bands below, or vestigial (question 35). *(This originally read "vestigial like question 27's dead formula" — that analogy no longer holds: Q27's formula was confirmed live and reversed 2026-09-09.)*
- **"Custom Shift"** — a manual flag on 62 of AdOps's 895 employees (~7%) whose schedule was handled outside the standard lookup this period (question 36; also tracked as Risk 13, §12).

### Phase 3 build, 2026-08-20 — built to actually see a computed July 2026 register

Building the engine surfaced two corrections to Phase 1/2 work already shipped, both caught before real code depended on them, and one real environment gap. All three are recorded here rather than silently fixed, per this doc's own posture on tracking decisions.

**`roster.ShiftType.is_night_shift` (added in Phase 2) has been reverted.** It shipped, migrated, admin-registered — and sat at `False` on every `ShiftType` including `NIG`, because nothing ever set it. Night-shift detection is derived instead from data the model already carries: `Shift.shift_attribute.start_time`/`.end_time` (falling back to `ShiftType.shift_start_time`/`.shift_end_time`), where a shift crosses midnight (`end_time < start_time`) iff it's a night shift — verified against real dev data (all `NIG` variants and `CUST` cross midnight; `EVE`/`MOR` never do). §4/§5/§6 below describe the derived rule, not the reverted field.

**No new field was added for half-day leave either.** `HDL` ("Half Day Leave") is already its own canonical `ShiftType`, deliberately seeded by exact code in `roster/management/commands/seed_shifts.py` — not a property to detect on other leave types. `LeavePolicy.is_paid` is `False` for both `HDL` and `LWP`; which specific unpaid type it is (to weight it 0.5 vs 1.0 payable day) is resolved by looking up the canonical `HDL` code directly, the same way `LWP`/`ABS` already are elsewhere in the codebase.

**Real environment gap: Orbis's `accounts.Department` taxonomy doesn't match HR's informal labels.** Neither "Digital Advertising" (AdOps) nor "Creative Development" exist as seeded departments in Orbis — checked directly. Software/Creative staff are organized under a single department, "BD - Technical Services" (confirmed against the real employees used for the Phase 3 test run below, by name cross-reference against the master sheet — not an ID collision). Since `PayRate` has no designation-level scope, the Software-band night rate (350/450) is applied to that department uniformly rather than split by designation — a deliberate simplification, not a fabricated number. AdOps's own rates (OT, Night Quarterly/Special, transport) are seeded but not exercised by anything, since "Digital Advertising" doesn't exist as an `accounts.Department` to scope them against.

**Test run, for real:** `Master Sheet_July_2026.xlsx`'s "Salary" column (the one used for the Phase 1/2 walk-back — §12 has the full story) turned out to be a 30000/100000 threshold indicator, not real pay. Of its 125 employee IDs, 22 match a real `CustomUser.employee_id` in Orbis. `SalaryStructure`/`TDSEntry` were seeded for those 22 with explicit `PLACEHOLDER` notes (surfaced as `calc_warnings` in the register, not hidden). A July 2026 `PayrollRun` was created, locked and calculated end to end: 947 employees populated (the full unfiltered population predicate), 22 calculated with real numbers (gross ≈ placeholder salary, prorated; night duty $0 for all 22 since none had a night-crossing shift logged in Orbis's real July roster data), 925 flagged `MISSING_SALARY_STRUCTURE` and still visible in the register rather than vanishing. Recalculating is idempotent (verified). This is the actual state of Orbis's data today, not a preview of July's real payout.

### Tax calculation review + Phase 4 build, 2026-08-24

Finance provided `Tax Calculation.xlsx` — their actual per-employee working template for computing salary income tax under the Income Tax Act 2023. Read formula-by-formula and hand-verified against a worked example (matched to the taka — see `PAYROLL_PROCESS.md` §12 question 1 for the walkthrough). This resolves questions 1 and 2 differently than locked on 2026-08-11 ("Deferred — Finance uploads monthly") and closes out the entire "Deferred to a future phase" block this doc previously carried for tax (§4 below, superseded).

**The confirmed formula, in order:**
1. Annualised gross = (monthly gross − a tiered, non-taxable mobile/internet reimbursement) × 12.
2. Plus one year's festival bonus, assumed at exactly one month's gross (2 Eids × 50% each — matches the festival-bonus decision already locked 2026-08-11; not read from actual `VariablePayEntry` data, since Finance's own template doesn't either).
3. Minus a statutory exemption of `min(annual gross ÷ 3, 500,000 Tk)`. **Corrects an error already in this doc** — §12 Risk 11 previously said "the flat one-third-or-450k rule"; the real cap confirmed by the template is **500,000 Tk (5 lac)**, not 450k. Fixed inline at Risk 11.
4. Progressive tax on the remaining taxable income: 0% up to a **category-specific** threshold (Men 400,000 / Women & Senior Citizen 450,000 / Disabled & 3rd gender 525,000 / Freedom Fighter 550,000 / Parents of a disabled child 450,000), then a **fixed 5-bracket table applying identically to every category**: next 300,000 @ 10%, next 400,000 @ 15%, next 500,000 @ 20%, next 2,000,000 @ 25%, remainder @ 30%.
5. Minus an investment rebate — the *lowest* of: 3% of taxable income, 10% of the employee's actual declared annual investment (savings certificate, life insurance premium, DPS, share market, retirement fund contribution), or a flat 750,000 Tk cap.
6. Floored at a 5,000 Tk minimum tax (matches ITA-2023 §165's Dhaka City Corporation rate — flagged as question 42 since other areas are lower and the figure is built configurable, not hardcoded).
7. Minus any known source tax / other TDS adjustment (rare, manual, defaults to 0).
8. ÷ 12 for the monthly deduction.

**Two judgment calls made explicit rather than silently decided:**
- **Investment data — asked and answered 2026-08-24.** Finance's own template hardcodes a large "Assumed Maximum" investment figure (1,232,400 in the example) instead of tracking real per-employee numbers — which only stays harmless while 3% of taxable income is the binding term in step 5; for high enough earners the 10%-of-investment term becomes binding and the assumption would silently understate the rebate. Confirmed: Orbis builds a real `EmployeeTaxDeclaration` per employee per tax year instead of replicating the shortcut — defaults to zero investment (no rebate beyond the income-based term) until an employee/Finance actually declares one.
- **No YTD true-up.** Finance's own template doesn't do one either — it recomputes the full annual projection from current monthly salary and divides by 12 every time an input changes, rather than tracking year-to-date collected tax and adjusting the remaining months. Orbis matches that exactly. This is why the `opening_ytd_taxable`/`opening_ytd_tax` fields floated in the original deferred design (§4, superseded below) turned out not to be needed — question 2 resolved as moot, not built.
- **Incentives / Special Allowance / Other Allowances default to 0**, matching Finance's own worked example — not summed from actual `VariablePayEntry` INCENTIVE data. Mixing a real year-to-date incentive total with an assumed-flat annual salary projection would be methodologically inconsistent (see "no YTD true-up" above); if Finance wants incentives factored in, that's a separate explicit decision, not guessed here.

**TDSEntry stays, as an override, not the primary path.** The manual entry drawer built for Phase 3 testing (`payroll/views.py::PayrollRunEmployeeAdjustView`) already writes to `TDSEntry` — that stays available for corrections and edge cases. The engine now checks it *first*; if a `TDSEntry` row exists for the employee/period, it wins (unchanged behaviour). Only when none exists does the engine fall through to calculating from `EmployeeTaxDeclaration` — this is the reversal the user asked for ("we don't need to upload TDS from Excel anymore"): calculation is now the default, upload/manual entry is the exception.

### "Revise salary" on the gross-salary page — 2026-10-09

The per-employee gross-salary page (`/payroll/salary/<uuid>/`) was read-only; a salary changed only through the bulk upload or the go-live proposal → approve flow. Now a **"Revise salary"** button opens a right-hand drawer (`SalaryStructureReviseView`, `SalaryRevisionForm`, `_salary_revise_drawer.html`): gross, effective from, reason, note — prefilled with the current structure so a same-date correction is two keystrokes. **Saved as APPROVED at once, no maker-checker** — the user's decision that the payroll-HR pair update *and* approve, as Payroll Admins do. Writes go through `services.supersede_salary_structure`, the upload's path, so a same-date revision CANCELs the old row and a later date closes it the day before. Validation mirrors the upload's (not future, not before joining, not more than a year back) plus "not earlier than the current structure" — older history is the salary-history uploader's job.

**Access (`accounts.permissions.can_edit_gross_salary` = `_is_payroll_admin or _is_payroll_hr`):** the detail page and drawer moved from the `payroll_salary` ProtectedView row to this code gate, because "in both HR and Payroll" is not a group (the loans views' posture). The Gross salary *list* and its sidebar entry stay Payroll Admins only; the pair reach the page from the employee profile's Service Book card ("Gross salary & revisions"), and its back button sends them to the profile rather than the list. This narrows the 2026-10-05 money-visibility line "gross salary list → Payroll Admins only" to the list; one employee's page is now the pair's too.

**Also fixed:** `approve_salary_structure` closed the old row "the day before" even for a same-date proposal, producing the inverted row `supersede_salary_structure` stopped making the same day — both now cancel.

### Salary adjustment settled N months at a time, derived from the compensation master — 2026-10-09

Built from the user's question "after a revision of the January increment, how do we calculate the next payroll's adjustment?" and the decision that **the Payroll Admin picks how many months each run settles** — full design and worked example in `SALARY_ADJUSTMENT_PLAN.md`. Closes `PRE_ORBIS_PAYROLL_IMPORT_PLAN.md` §5.2's deferred follow-up.

- **One derived balance per employee** (`payroll/adjustments.py`): for every payroll month since the current structure's effective date, owed = current gross − gross actually paid (Orbis run → imported month → assumed at the previous structure, flagged). The engine pays Σ owed(first K + N owed months) − adjustments already paid, K from `PayrollRunEmployee.adjustment_months` / `LegacyPayrollMonth.adjustment_months`, so a revision tops up the settled months in the next instalment automatically. Never cached; the run being rebuilt is excluded from its own inputs.
- **`PayrollRun.adjustment_months` / `adjustment_settle_all`** — set from the run page ("Salary adjustment this run"), recalculates at once; Payroll Admins only. No instruction = no line.
- **Same-date correction** of a `SalaryStructure` now CANCELs the old row (`effective_to = effective_from`) instead of closing it the day before it opened.
- The months sheet gains an optional **"Salary adjustment months"** column (the September 2026 instalment = 3).
- **The register explains it (same day, user's question "where can we see my 218,047 gross?").** The Calculated tab and its export now grow **one dynamic column per extra earning component present** (Salary adjustment, Festival/Performance bonus, Incentive, SLA…) between Transport and Gross — `views._extra_earning_components`, the deductions' "one column per type present" pattern; until then every non-fixed earning was folded into Gross with no column at all. And the **Adjust-inputs drawer** (click an employee ID on the Calculated tab) opens with a read-only **"Calculated lines"** table — every line of the row with its `calc_note`, so "Settles 3 month(s) (Apr–Jun 2026) at 130,000: owed …, paid before …; N remain" is one click from the figure.

### TDS by challan is its own per-employee record; the months sheet is pay only — 2026-10-09

The user's review of the live cutover page (`PRE_ORBIS_PAYROLL_IMPORT_PLAN.md` Revision 2) reversed two of the previous day's columns:

- **Mobile & Internet reimbursement is never typed.** It is the policy tier of the gross (`tax.py::mobile_internet_reimburse`), identical for a pre-Orbis and an Orbis month, and the stored `LegacyPayrollMonth.mobile_internet_reimbursement` had one reader (the export). Field dropped (`payroll/0031`); the export still carries the column, derived from the `SalaryStructure` in force at period end (fallback: the gross line); the months parser ignores the column on a round-trip.
- **Which challan carried how much of an employee's tax is `TaxChallanAllocation`** — (challan, employee, **stored** encrypted `amount`), uploaded monthly by Payroll Admins from the new "TDS by challan (per employee)" Upload Center card, template prefilled from the month's TDS. `LegacyPayrollMonth.challan` dropped; `TaxChallan` gained `period_year`/`period_month` (the typed payroll month was parsed but forgotten for a pre-Orbis challan). **This reverses the certificate plan's "the amount is never stored"**: a deposit is a historical fact, so a later recalculate should surface as a render-time warning that the run's tax no longer equals its allocations, not silently move the certificate figure. The certificate plan's `TaxChallanMember`/`extra_amounts` are superseded; Q70's remainder is moot.
- **Arrear schedule corrected** (user): 2026's January–July difference is paid 3 + 3 + 1 months across the September (pre-Orbis, 3 Oct payout), October (first Orbis run, 1 Nov) and November (1 Dec) periods — the plan had the third instalment in October.
- **`ARREAR` → `SALARY_ADJUSTMENT` ("Salary adjustment")**, user's call after the first test upload: one taxable earning for *pay that belongs to another period, paid in this one* — the increment arrear, or a payment held back a month and released later — instead of a code per reason. Positive only (an overpayment recovery is a deduction). Re-keyed by `payroll/0032` (renames the component row and the `ARREAR` key inside `LegacyPayrollMonth.lines` / `PayrollRunLine.component_code`) because the seed's `update_or_create` can't follow a changed lookup key — the Special OT precedent. The deferred "derive from the salary structures" follow-up now covers only the increment sub-case.

### Pre-Orbis payroll months imported per component; FESTIVAL_BONUS split from BONUS; ARREAR seeded — 2026-10-08

Orbis's first real payroll is the October 2026 period; July–September 2026 of FY2026-27 ran in Finance's workbooks. Everything annual (the TDS true-up, the coming tax certificate) needs those months **per employee, per month, per pay component**, not as totals. Built per `PRE_ORBIS_PAYROLL_IMPORT_PLAN.md`:

- **`LegacyPayrollMonth`** (employee × payroll month, `lines={component_code: amount}` encrypted JSON for **every** pay component — earnings and deductions — plus reimbursement, TDS, optional `net_pay`, `challan` FK) and **`TaxChallan`** (the company's treasury deposit — number, date, bank, `total_amount` **plaintext** by the `PayRate` exception, nullable `run`). Migrations `payroll/0029` (collation CONVERT step) and `0030`.
- **Two uploaders** (revised the same evening on the user's review): **"Tax challans"** is permanent and monthly — Finance + Payroll Admins record each deposit, tax year from the deposit date, optional payroll month linking it to that month's calculated run — and **"Pre-Orbis payroll months (cutover)"** is the one-time, single-sheet, full payroll record that references challans by number.
- **The monthly payroll ledger** (`payroll/tax_ledger.py`) is one shape in and out: the import template's columns are generated from `PayComponent` (earnings then deductions, display order; TDS and net pay fixed columns), and `monthly_ledger_rows()` reads an Orbis month from its CALCULATED run in the same shape, so the Upload Center page's "Export month" is both Finance's reconciliation sheet and the certificate's future input (which reads only the taxable earnings — `taxable_earning_codes`). Only months before `TaxYear.orbis_live_from` may be imported; apply rewrites the declaration's two openings from all of the employee's month rows with the engine's own exclusion rule (deductions never count) — the "Tax-year openings" totals uploader (2026-10-07, below) is therefore superseded for FY2026-27 and kept for a future year that only has totals.
- **`TAX_EXCLUDED_EARNING_CODES` reversal.** The 2026-10-07 section below excluded `BONUS` from additional pay "because the festival bonus is already assumed at one month's gross". SEBPO pays **both** a festival bonus and a performance bonus, and `BONUS` has always been seeded as "Performance bonus" — so a new **`FESTIVAL_BONUS`** component is the excluded one and `BONUS` counts like an incentive. No real money had gone through a run, so no historical computation changes; `tests_tax_trueup.py` pins both.
- **`ARREAR`** ("Salary arrear", EARNING, taxable, INPUT, `affects_gross`). The yearly increment is effective 1 January but first paid in August, with January–July's difference in three instalments from the 16 Aug–15 Sep payroll; the third falls in the first Orbis run. Finance files each instalment through the variable-pay uploader. Deriving the arrear from `SalaryStructure.effective_from` vs. what each run paid (balance derived like loans, `arrear_instalments` on the structure) is a deferred follow-up for the January 2027 increment — `PRE_ORBIS_PAYROLL_IMPORT_PLAN.md` §5.2, Finance questions Q75–Q76 in `TAX_CERTIFICATE_PLAN.md`.

### Salary loans — HR records, Finance disburses, the engine recovers, 2026-10-08

Built from Finance's approved file note (HR/21092026/01: BDT 50,000, 10 × 5,000 by payroll deduction) — full design in `SALARY_LOAN_PLAN.md`, decisions in `PAYROLL_PROCESS.md` §12 questions 67–72. **This supersedes §4 "Loans" below in three ways:**

1. **No instalment ledger, no cached totals.** `SalaryLoanTransaction(reason=INSTALLMENT)` and `outstanding_principal`/`total_recovered`/`installments_paid` assumed a `PAID` run state to post on. There is none yet, and `calculate_run()` is delete-and-rebuild, so a ledger row written at calculate time would be duplicated by every Recalculate. Instead **recovered-so-far is read from the `LOAN_REPAYMENT` lines of CALCULATED runs** (`payroll/loans.py::loan_balances`, the same source the TDS true-up reads "already deducted" from) plus the cutover `opening_recovered` and the append-only `SalaryLoanAdjustment` rows (prepayment / waiver / correction — the non-payroll events only). Money columns are encrypted, so the sums are Python over `values_list`.
2. **"Settled" is derived** (`outstanding <= 0`), not a status. `SalaryLoan.status` is `PENDING_DISBURSEMENT → ACTIVE`, plus `CANCELLED` (from pending only) — every value reachable, nothing for the engine to flip.
3. **Partial instalment, not postponement** (Q68): when net can't cover the instalment the engine deducts what fits, warns `LOAN_INSTALMENT_REDUCED`, and the rest rolls forward; a deliberate pause is a recorded `SalaryLoanSkip` (HR approver named) → `LOAN_INSTALMENT_SKIPPED`. A **leaver pays the whole balance in the LWD period** (Q69), `LOAN_OUTSTANDING_AT_SEPARATION` if net can't cover it. Stage 8 of the §6 engine table is therefore built; the never-negative cap holds (`NEGATIVE_NET_PAY` can't be caused by a loan).

Other decisions: Payroll Admins disburse (Q67); interest-free, no tax effect, deducted after TDS (Q70); first deduction defaults to the payroll named for the month *after* the disbursement month — 3 Mar → Mar 16–Apr 15, 16 Sep → Sep 16–Oct 15 (`periods.py::first_loan_deduction_month`, refined on the first dev test) — unless HR picked a month (Q71); ~100 live loans at cutover come in through the Upload Center's "Salary loans (cutover)" card with Finance's recovered-to-date figure, document optional on those rows (Q72); salary advances are not practised, so there is no `loan_type`. The register and export grew a "Loan" column with no view change (`_extra_deduction_components`).

### Tax calculation v2 — additional pay in the projection + year-to-date true-up, Q2 reversed, 2026-10-07

**This reverses the 2026-08-24 "no YTD true-up, matching Finance" decision (question 2) and the "incentives default to 0" call.** Finance's updated sheet does both, and the user confirmed the six open points the same evening (`PAYROLL_PROCESS.md` §12 questions 61–66; full design in `TAX_CALCULATION_TRUEUP_PLAN.md`). Built the same day:

- **`TaxYear.calculation_method = ITA_2023_SALARY_TRUEUP`** — the branch key this doc reserved for a formula-shape change. `tax.py::compute_tax_for_employee` takes `additional_pay_ytd`, `tds_already_deducted`, `months_remaining`; under the old flat method it ignores them and reproduces every historical run exactly (`tests_tax.py` pins that branch). `seed_tax_year` sets 2026-27 to the new method.
- **Additional pay** = every `PayrollRunLine` with `kind=EARNING` and `component.is_taxable` except `GROSS_SALARY` (annualised separately) and `BONUS` (already assumed at one month's gross) — `tax.py::TAX_EXCLUDED_EARNING_CODES`. Year-to-date = the declaration's `opening_additional_pay` + the sums from every earlier CALCULATED regular run of the tax year + this period's own lines (already built before the Tax stage). Enters `project_taxable_income()` before the exemption.
- **True-up** = `(net_tax_payable − source_tax − AIT − tds_already_deducted) ÷ months_remaining`, whole taka half-up (Q6). `tds_already_deducted` = `opening_tds_deducted` + Σ earlier runs' `PayrollRunEmployee.tax_deducted` (so a `TDSEntry` override in month N counts as deducted in N+1). `months_remaining_for(tax_year, period)` is calendar-based (July 12 … June 1); a missed month never stretches the rest. Negative remainder → 0 + warning (Q5).
- **Cutover**: `TaxYear.orbis_live_from` (FK `PayrollPeriod`) marks the first period Orbis is authoritative for; `EmployeeTaxDeclaration.opening_tds_deducted` / `opening_additional_pay` (encrypted, outside the confirm-and-lock) carry Finance's figures for the months before it, loaded by the Upload Center's **"Tax-year openings"** card (`UploadTaxOpeningView`, Payroll Admins via the `payroll_salary` ProtectedView group, batch kind `TAX_OPENING`, re-upload overwrites; extra-pay column optional — Q4). From FY2027-28 both are blank.
- **Checks** (both WARNING): `TAX_YTD_GAP` (a calendar month of the year at/after live-from with no CALCULATED regular run — counted as 0), `MISSING_TAX_OPENING` (live-from is mid-year, the employee is taxed this period, and both openings are 0).
- **Snapshot**: `TaxComputation` gains `additional_pay_ytd`, `tds_already_deducted`, `tax_remaining`, `months_remaining`; `breakdown_json["ytd"]` lists the prior periods used and each sum. The TDS line's `calc_note` carries the same four figures, since the register has no per-employee tax drawer yet (follow-up).
- **Reference**: Finance's October example (gross 120,817, incentives 5,000, AIT 25,000, 1,943 × 3 deducted) → 1,942.84 in the sheet, **1,943** in Orbis; pinned end to end in `tests_tax_trueup.py`.

### Tax declaration becomes a once-a-year commitment — Q41 reversed, AIT renamed, 2026-10-07

**This reverses a decision this doc recorded as settled.** On 2026-08-24 Orbis chose "real per-employee declarations, not Finance's assumed maximum" (question 41) and built six itemised `investment_*` fields defaulting to zero. A 2026-10-07 meeting with Finance, with their updated `Tax Calculation_ - Copy.xlsx` in hand, settled that Finance's rule is the company's rule: **every employee is assumed to invest the full "Allowable Investment" (30% of projected taxable income), confirms that once a year, and may only lower it.** `PAYROLL_PROCESS.md` §12 questions 58–60 carry the business decisions; this is the technical record.

**Why the assumption is safe arithmetic, not a shortcut.** The rebate is `min(3% × taxable, 10% × investment, 7.5 lac)`. With investment = 30% × taxable, the second term is `10% × 30% × taxable = 3% × taxable` — identical to the first. So "assume the maximum" simply means "the rebate is 3% of taxable income", which is what Finance's template produces (`H29 = C23 × 3%`, `H30 = C25 × 10%`, `H32 = MIN(...)`). The earlier worry that a hardcoded assumption could *overstate* the rebate for high earners is moot because the allowable figure is derived from each employee's own taxable income, not a fixed number; the cap term only binds above Tk 25,000,000 of taxable income.

**What changed:**

- `EmployeeTaxDeclaration` loses the six `investment_*` columns (every production row was zero, verified against the 2026-10-06 dev restore) and gains `allowable_investment` + `actual_investment`, both encrypted. `allowable_investment` is a **snapshot**: `tax.py::allowable_investment(tax_year, gross)` = `floor(project_taxable_income(...).taxable_income × TaxYear.allowable_investment_pct)` from the salary in force on the issue date (`services.gross_in_force` — current row, else the first later row for a joiner), set once by `seed_tax_declarations` or `save()`, never recomputed. Whole taka, rounded **down**, so the 10%-of-investment term can't exceed the 3% term by a paisa; matches Finance typing 308,286 from 308,286.30. `actual_investment` defaults to it; `clean()` refuses a figure above it. `declared_investment` (actual → allowable → 0) is what `tax.py` reads.
- `other_tds_adjustment` → **`ait_car_advance_tax`** ("AIT (Car & Adv. Tax)" — advance income tax already paid, e.g. on a car) via `RenameField` (the column already held Fernet tokens, so no purge), with `ait_document` (FileField, PDF/JPG/PNG ≤ 5 MB) **required when AIT > 0** — both the form and `clean()` enforce it. `TaxComputation.other_adjustment` renamed alongside. The upload path is `payroll/tax_declarations/<employee uuid>/<uuid4>.<ext>` — a random name because production nginx still serves `/media/` with no session check (`deploy/README.md`); the only link to the file is `TaxDeclarationDocumentView` (`can_view_tax_declaration_document`: the employee, HR / managed scope, Payroll-HR, Payroll Admins).
- **Confirm-and-lock.** `submitted_at` / `submitted_by` / `submission_source` (`IN_ORBIS` | `OUTSIDE_ORBIS`). `is_locked` = submitted **or** past `TaxYear.declaration_deadline` (new nullable date — leave blank for a year with no cut-off). A locked row: no "Review & confirm" button, no Edit/Delete in HR's generic profile drawer (`EmployeeTaxDeclarationForm.clean` refuses, `SectionDeleteView` 403s on any row with `is_locked`), 403 from the review view. **The Django admin is the only way back** (`EmployeeTaxDeclarationAdmin`'s "Confirmation" fieldset — clear `submitted_at`), and that admin's money fields are already behind `PayrollMoneyAdminMixin`.
- **The employee's own step** is `payroll/views.py::TaxDeclarationReviewView` (`/payroll/tax-declarations/<pk>/review/`): a right-hand drawer from the profile's "Tax declarations" card (htmx) or a full page (direct link from the dashboard "My" tab card, `core/dashboard.py::me_context["pending_tax_declaration"]`). `TaxDeclarationReviewForm` = `actual_investment` (pre-filled with the allowable figure), `ait_car_advance_tax`, `ait_document`, and a required "I confirm … can't be changed afterwards" checkbox. Saving stamps `IN_ORBIS` and redirects to the profile. Permission `can_submit_tax_declaration`: the employee themself — **deliberately independent of `profile_self_service_enabled`**, every employee must be able to confirm their own tax position — or Payroll-HR (HR ∧ Payroll) / Payroll Admins on their behalf (`submitted_on_behalf` shows who). HR's generic drawer still edits figures and category without locking.
- **Yearly runbook (each July):** `seed_tax_year` with the new year's rates (and `--tax-year` when the previous one must stay for its last run) → `seed_tax_declarations` (creates every row with its snapshot; `--as-of` picks the salary date, default today) → employees confirm → optionally set `declaration_deadline`. Not a beat job: the `TaxYear` row needs Finance's confirmed figures first, and the engine's existing `MISSING_TAX_YEAR_CONFIG` blocking check is the reminder.
- **FY2026-27 backfill:** Finance collected this year's agreement before Orbis held declarations, so on production run `seed_tax_declarations --tax-year 2026-27 --fill-allowable --mark-agreed-outside` once (after `migrate` and `seed_tax_year`). It snapshots every existing row from the salary in force today, stamps them `OUTSIDE_ORBIS` dated 2026-07-01, and employees see "Agreed outside Orbis" with nothing left open. `--fill-allowable` also covers a row whose salary structure arrived after the row did (fills NULL snapshots only; never overwrites one).

**What did NOT change — the per-run calculation itself.** Finance's updated sheet also differs from `tax.py` in two ways that are *not* declaration matters and were not built here: (1) it has an **Incentives** line (`C14`, 5,000 in the example) and Special/Other Allowance lines added to annual gross — Orbis still projects from gross salary + one month's bonus only (the 2026-08-24 "incentives default to 0" call stands until Finance asks); (2) it derives the monthly deduction as **(tax payable − AIT − TDS already deducted this year) ÷ months remaining** (`H36 = H26 − H34 − H35`, `H38 = H36 / 9`), i.e. a year-to-date true-up, whereas Orbis divides the annual figure by 12 with no true-up (question 2's 2026-08-24 "no YTD true-up, matching Finance" is therefore no longer an accurate description of Finance's own sheet). Both are recorded as open follow-ups in §13 rather than changed silently.

### Overtime management screens + input window — partially reverses "no period restriction", 2026-09-15

**Built:** `/payroll/overtime/` — a scoped list of `OvertimeEntry` rows with add / edit / void / delete, replacing "ask someone with Django admin access" as the only way to correct an uploaded entry. The right-hand drawer (`payroll/forms.py::OvertimeEntryForm`) is the add/edit surface; the Payroll section's sidebar item now lands here and the uploader is a button on the page. Access is unchanged from the uploader it sits beside: `ManagedScopeRequiredMixin` for the page, `payroll_visible_employee_pks` for the rows (separated employees included).

**This partially reverses a decision recorded here as settled.** On 2026-09-07 the uploader's period restriction was removed outright — `parse_upload`'s docstring records *"No period restriction… Any working date is accepted"* — because the old rule bound a whole file to one upload-time period and kept rejecting legitimate historical data. What comes back is **not** that period binding, which stays removed: a file may still span periods and each row still derives its own `PayrollPeriod` from its own `work_date`. What comes back is a **date window**, `payroll/services.py::OvertimeWindow`:

- **Floor** — the earliest `start_date` among periods whose run is still `INPUTS_OPEN`/`DRAFT`, falling back to the calendar-current period when no run is open. The two converge, so there is no gap between them. Worked example (the user's own): on **Sep 18**, with the Aug 16 – Sep 15 run still being compiled, the floor is **Aug 16** — and **Sep 17 is accepted**, filing into the next period. A supervisor is never blocked from the period they are currently working in just because the previous one is still open.
- **Lock** — once the run covering a date reaches `INPUTS_LOCKED`/`CALCULATING`/`CALCULATED`, only `accounts.permissions::can_manage_locked_overtime` may write: `_is_payroll_hr` (the curated "Payroll" ∧ "HR" pair) plus `_is_payroll_admin`. **Deliberately not the whole "Payroll" group** — a plain supervisor is in it too, which is the exact hole `can_write_payroll_note` had to close on 2026-09-10. Payroll Admins are included because they can already `reopen_inputs()`, edit and re-lock; refusing them a direct edit would buy a detour, not a control. `reopen_inputs()` hands the period back to supervisors automatically, so no extra state was added.

Two consequences worth naming:

- **The floor does not apply to `can_manage_locked_overtime` holders.** A locked period is below the floor by definition — its run stops contributing to the floor the moment it locks — so applying the floor to HR-Payroll as well would make "only HR may write after lock" dead code, blocked one check earlier. They stay bounded by `MAX_BACKDATE_DAYS` (365) and by the employee's own employment window (`employed_on`). This is a derivation from the two rules as stated, not a third rule.
- **`"status"` joined `apply_upload`'s `update_fields`.** It wasn't there, so re-uploading a voided employee/date/type left the row `VOID` while the report said *"updated"* — the overtime silently never got paid. Now a re-upload revives it. Safe specifically because of the lock rule above: a deliberate HR void happens in a locked period, where a supervisor's re-upload is refused outright.

**Also:** overtime is now rejected for **future work dates** (`REJECT_FUTURE_WORK_DATES`) — it is filed after the work is done, and the old ±365-day guard let a mistyped year through as a forward date. Window rejections are **per-row skips** in the upload report (`skipped_window` / `skipped_locked`), not whole-file aborts, matching how out-of-scope rows already behaved, and they surface in Preview before anything is written. Void keeps the row and drops it from the engine (`collect_overtime` already filters `status=APPROVED`); delete is a hard delete for genuine mistakes, the same posture as `PayrollRunVariablePayDeleteView`.

**Menu:** the `Payroll > Upload overtime` submenu becomes `Payroll > Overtime` pointing at `payroll_overtime_list`. `SubMenu.objects.update_or_create` keys on `(main_menu, title)`, so re-seeding would create a duplicate row rather than rename — `manage.py rename_overtime_menu --apply` must run once first. Same trap as the "Report" → "Support Info" rename.

### Overtime after input lock lands PENDING — Q4 partially reversed, 2026-09-15

**This partially reverses a decision this doc recorded as settled.** Q4 (2026-08-11) established *"no approval gate — a supervisor's upload is final; status exists only so HR can void/correct"*, and `OvertimeEntry.status` carried that as a comment. **That still holds while a period's inputs are OPEN**, which is the normal path and is untouched: a supervisor's upload goes straight to `APPROVED` and is paid. What changed is what happens *after* `INPUTS_LOCKED`.

**The problem.** Until now a locked period refused every non-HR write outright (the 2026-09-15 morning build). That loses real data — a team leader who files Sep 10's overtime on Sep 20 is simply told no, and the overtime is never captured anywhere. The user's own framing: *"if we lock the input and start the calculations and someone from the department uploads overtime but we don't recalculate then this overtime will not be paid."* Both halves are failure modes: silently rejected, or silently accepted into a run nobody recalculates.

**What now happens.** A locked period **accepts** the write and forces it to a new `OvertimeEntry.Status.PENDING`:

- `collect_overtime()` already filters `status=APPROVED`, so a pending entry is invisible to the engine with **no engine change** — it cannot be paid by accident.
- `can_manage_locked_overtime` (HR∧Payroll + Payroll Admins) approves or rejects it, recording `processed_by`/`processed_at`. Approve → `APPROVED`; reject → `VOID`.
- `calculate_run()` already accepts a `CALCULATED` run, so the loop is simply **approve → Recalculate**; no reopen needed.

**`PENDING` is deliberately not reused `VOID`**, which the user's first wording suggested. `VOID` means "HR deliberately cancelled this"; collapsing the two would lose the difference between *needs review* and *already rejected*, and make the Void/Restore toggle ambiguous. The three-state shape (+ `processed_by`/`processed_at`) copies `VariablePayEntry`, which has carried it since Phase 2.

**Nobody uploads as approved into a locked period — HR included.** `can_manage_locked_overtime` therefore *narrowed* in meaning: it used to be "may still write after lock", it is now "may approve what was filed after lock". HR filing an entry then approving it is two recorded steps rather than one implicit one, which is the whole point of the audit trail. HR loses nothing, since they can do both.

**Only the most recently locked period accepts late filing** (`OvertimeWindow.pending_floor`). An older locked period has been paid; reopening all history would restore the backfill hole the floor was built to close. Everyone remains bound by `MAX_BACKDATE_DAYS`.

**The anti-silence machinery.** A `PayrollControlCheck` alone is not enough — `calculate_run()` regenerates checks, so one only ever describes the state *at the last calculation*, and the dangerous case is an approval that happens after it. So:

- `PayrollRun.overtime_changed_since_calculation` and `.pending_overtime_count` are **computed live at render** (max `OvertimeEntry.updated_at` for the period vs `run.calculated_at`), driving two banners on the run detail page.
- A `PENDING_OVERTIME` **WARNING** check is still emitted at calculate time for the Exceptions tab and audit trail. Warning, not blocking: pending overtime is normal for a period still being worked, and `lock_inputs()` deliberately doesn't gate on checks.

**Re-upload can no longer disturb a settled entry.** `apply_upload` now reads existing rows' *status*, not just their keys: in a locked period it may create a new `PENDING` row or amend one still `PENDING`, but a row already `APPROVED` or `VOID` there is refused into `skipped_locked`. Without that, `bulk_create(update_conflicts=True)` — which cannot express "set status only on insert" — would either downgrade approved overtime to pending (dropping it from pay) or revive something HR had killed.

### Software / Creative Development overtime — Q27 reversed, 2026-09-09

**This reverses a decision this doc previously recorded as settled.** On 2026-08-13 Finance was asked about a `gross ÷ 30 × days × 1.5` formula spotted in the July workbook and answered that it was a dead leftover; §12 question 27 recorded *"Not active anymore… no third `PayRate.basis` value needed"*, and §4 above repeated that as a reason no schema change was required. **Finance confirmed in writing on 2026-09-09 that the identical formula is the current, active overtime policy for Software Development and Creative Development.** Recorded here as a reversal rather than silently fixed, per this doc's posture (the reverted `ShiftType.is_night_shift` field and the corrected AdOps OT basis above are the precedents).

Worth noting for whoever reads question 35 later: it explicitly asks whether a dormant AdOps OT column is *"dormant like the gross÷30×days×1.5 formula settled at question 27."* That comparison no longer supports the conclusion it was making — Q27's formula turned out to be live. Question 35 should be re-asked on its own merits, not by analogy.

**The confirmed policy:**

- **Formula:** the employee's own gross ÷ 30 × 1.5, **per overtime day**. Not per hour.
- **Scope:** Software Development (dept 7, 20 active) and Creative Development (dept 5, 86 active) only.
- **`GH OT` only.** These two departments earn overtime *solely* for working a government holiday, and they work a full shift when they do — which is why per-day is the natural unit rather than a simplification. **They have no Regular OT at all**, so no `regular_ot` row is seeded for them. Special OT may exist; Finance has not supplied that figure yet.
- **Nothing was mispaid in the meantime.** Both departments had zero `OVERTIME` `PayRate` rows and zero approved `OvertimeEntry` rows when this landed (checked directly 2026-09-09), so the gap had never been exercised.

**Seeded alongside it: General BPO and IT & Cybersecurity flat rates** (§12 Q50) — General BPO 350 Tk/night regular, 500 Tk/night custom, 400 Tk/day transport; IT 200 Tk/night regular. These are anchored at the seed's shared `EFFECTIVE_FROM` (2026-01-01) on the user's instruction, rather than the 2026-09-01 date they were first hand-entered with — so the rate table carries one start-date convention instead of two. `resolve_rate()` keys off the run's `period.end_date`, so this makes the rates resolve for any 2026 period, not only those closing after September. Two details are deliberate and were verified rather than assumed: **no `work_location` scope on the transport row** (the engine never passes one to `resolve_rate()`, and `inputs.classify_night_shifts` already excludes WFH days from the `TRANSPORT` count — scoping the rate as well makes it unresolvable, not stricter), and **no Trainee carve-out or `NIGHT_QUARTERLY` row for either department**, unlike AdOps.

**Modelled as a third `PayRate.Basis` value, `GROSS_30TH_PER_DAY`** — not a field on `CustomUser`/`JobInfo`, which was considered and rejected. A per-employee column would store one department policy 106 times; it would denormalise `SalaryStructure.gross_monthly` into a second, un-effective-dated place that goes stale silently at the next increment; and it would sit outside the rate table entirely, so nothing would explain a historical run. `PayRate` already carries department policy as effective-dated, most-specific-wins data — and already has the `resource_status` / `min_access_level` precedent for "a department carve-out is data, not code."

*(Correction, 2026-09-09: an earlier draft of this paragraph described `PayRate` rows as "snapshotted" into `run.config_snapshot`. They are not. The field exists and is migrated, but nothing writes it — see the note below. The argument against a `JobInfo` column stands on the other two grounds.)*

**Gap worth naming while it's in view: `run.config_snapshot` is declared but never populated.** §5's immutability layer 4 calls for it; today the only references in the codebase are the model field, its migration, and an `admin.py` readonly listing. Because `calculate_run()` is delete-and-rebuild and `resolve_rate()` reads the live table on every run, **recalculating an old run re-resolves today's rates, not the ones it was originally calculated with.** That makes `is_active=False` a retroactive edit to history rather than an end-date, and an in-place `amount` change a retroactive repricing. Until layer 4 is built, closing a rate out must be done with `effective_to`, and a changed figure must be a new row — see §12 Q49's Special OT note.

Two deliberate choices inside that:

- **`amount` holds the multiplier (1.50), not taka**, for this basis only. It is an overload, but `amount` is already read as taka-per-hour / per-night / per-meal depending on `basis`, so it follows the existing pattern instead of adding a column used by one basis. `PayRate.clean()` rejects the obvious mistake — a taka figure pasted in out of habit — by requiring `0 < amount <= 10`, and restricts the basis to `rate_type=OVERTIME`. Finance changing 1.5 to 2.0 stays a data edit.
- **The 30 is fixed by the policy, deliberately *not* `PayrollSettings.payable_days_basis`.** That setting governs proration, and its `CALENDAR` option would silently swing overtime to ÷31 or ÷28. If Finance ever wants ÷26 (as is still open for encashment, question 22), that is a deliberate second basis value, not an accident of an unrelated setting.

**Gross is the un-prorated `SalaryStructure.gross_monthly`**, since overtime is never prorated (§5 stage 4). A mid-period joiner's salary line is cut but their OT day is not — covered by a test.

`OvertimeType.basis` needs no third value: it is only a fallback default and this basis is only ever set on a `PayRate` row (locked 2026-08-11, §4). `resolve_rate()` needs no change either — it never inspects `basis`. `build_payroll_report` reports OT *hours*, not money, so the pre-payroll report is unaffected.

### GH OT auto-generated from attendance, 2026-09-17

Since these two departments never file overtime through the normal upload — the only overtime they can earn is `holiday_ot` for actually working a government holiday — a new `payroll/management/commands/generate_gh_overtime.py` beat job (the `Generate GH overtime` periodic task, 10:20 Asia/Dhaka, a few minutes after `sync-attendance-punches`) auto-files it instead of waiting on a supervisor upload. Logic: for each target date (default yesterday) that is an active `roster.Holiday`, find Software/Creative Development employees with an `AttendancePunch.check_in` and `time_in_office >= 4h` that date, and create one `OvertimeEntry(overtime_type=holiday_ot, seconds=8*3600, worked_in=employee.team, status=<OvertimeWindow.resolve(date)>)` per qualifying employee not already holding a `holiday_ot` entry for that date. Additive-only and idempotent — an existing entry (PENDING, APPROVED, or a deliberate VOID) is never touched, which is also what makes the command safe as a manual fail-safe (`--date`/`--from`/`--to`, `--dry-run`) when the scheduled run fails or `AttendancePunch` gets corrected after the fact. The 4h presence floor and the 8h grant are both command flags (`--min-hours`, `--hours`), not hardcoded.

**TODO, not built yet:** no auto-reversal. If an `AttendanceCorrection` later removes the punch that qualified someone (e.g. the "present" reading turns out to have been a mistake), the `OvertimeEntry` this command created isn't auto-voided — HR has to void/delete it by hand from `/payroll/overtime/`. Revisit once there's a concrete case to design against.

**UI trigger added same day.** The write logic was pulled out into `payroll/gh_overtime.py::generate_gh_overtime_for_date()` so the CLI command and a new superuser-only page (`/payroll/overtime/generate-gh/`, `payroll/views.py::GenerateGHOvertimeView`, a "Generate GH OT" button on the overtime list visible only to `request.user.is_superuser`) share one implementation — this is the first web-triggered management-command-style action in Orbis (no other nightly job has one), scoped to superusers only rather than any broader payroll group on the user's explicit instruction, and deliberately does **not** run `sync_attendance_punches` itself — the operator runs that first by hand for any date that isn't already synced. Same preview-then-apply shape as the bulk uploaders: a POST with `action=preview` computes the report with no writes, `action=apply` writes. Intended as a stopgap until Celery-beat jobs are manageable from Django admin (not yet built).

### General BPO's GH OT, sourced from TTMS instead of AttendancePunch — 2026-09-17

General BPO also earns overtime solely for working a government holiday, confirmed by the user the same way Software/Creative Development's scope was — but its presence source is different: General BPO already has `AttendancePunch`/TimeLy rows (182 existed for it when this was built), yet the user specifically wants this sourced from **ttms.sebpo.net**'s ops daily-hours API instead, presumably because it reflects actual production/billable hours for an ops floor more accurately than office badge presence. `POST /api/v1/operations/daily-hours/` (`{"department_name", "start_date", "end_date"}`, `team_name` omitted so one call covers every team in the department) returns one row per employee per date with `work_hour` already in seconds — **real worked hours, not a flat flag** (a non-holiday sample came back with partial hours like 23040/19200 seconds, not a uniform number). Confirmed live 2026-09-17 against the sample in `/home/smrashel/Downloads/TTMS_ops_daily_hours_api.txt`, which only documented the request shape, not the response.

**GH OT for General BPO pays the exact TTMS-reported hours, not a flat day grant** — the user's explicit choice, and it matches the `PayRate` they added the same day: `PayRate(department=General BPO, rate_type=OVERTIME, overtime_type=holiday_ot, basis=PER_HOUR, amount=300.00, team=None)`. This is why the client (`payroll/ttms.py`) and the generator (`payroll/gh_overtime.py::generate_bpo_gh_overtime_for_date`, `manage.py generate_gh_overtime_bpo`) are a parallel path rather than a parameter on the Software/Creative flow — the write rules (holiday gate, idempotency, `OvertimeWindow`, `employed_on`) are shared via one internal helper (`_apply_candidates`) so the two can't drift on those, but the presence source and the seconds-per-entry logic are genuinely different.

**`payroll/ttms.py` deliberately does NOT copy `attendance/timely.py`'s "never raises" shape** — it follows `payroll/meals.py`'s instead: not configured, or an actual fetch/parse failure, both raise `TTMSUnavailable` rather than degrading to an empty result. A silent empty result here is indistinguishable from "nobody in General BPO worked that holiday," which would be a wrong default on a payroll write (same reasoning `payroll/meals.py`'s module docstring gives for why it diverges from timely.py). The management command turns that into a clean `CommandError` rather than a raw traceback; on the scheduled beat run it surfaces as a failed Celery task in `logs/celery.err.log`, not a swallowed no-op.

**Scheduled at 10:25 Asia/Dhaka** (the `Generate GH overtime (General BPO)` periodic task), 5 minutes after the Software/Creative job at 10:20, per the user's "right after" instruction. Unlike that job, this one has **no local sync dependency** — TTMS is called live at generation time, so there's nothing to run first (simpler than the AttendancePunch/TimeLy flow, which needs `sync_attendance_punches` to have already populated that day's rows).

**No UI trigger built for this one** — CLI-only (`--date`/`--from`/`--to`/`--dry-run`, same shape as `generate_gh_overtime`) per what was actually asked; add a UI page if it's wanted later, mirroring `GenerateGHOvertimeView`.

**TODO, same class as the Software/Creative flow:** no auto-reversal if TTMS's reported hours for an already-generated date later change.

### AdOps night-shift / overtime / transport-allowance policy, 2026-09-07

AdOps (Digital Advertising) supplied `AdOps Payroll Rates.xlsx` — a two-tab workbook (a designation-by-rate table, and a per-`roster.ShiftAttribute` rate table for the `NIG`/`CUST` variants) — plus a plain-language description of a **Dedicated Night Shift** concept not previously modelled anywhere. Read cell-by-cell (both tabs) and cross-checked against the real dev database, not assumed from the sheet alone.

**Corrects a stale fact from the 2026-08-20 Phase 3 build note above: `accounts.Department` "Digital Advertising" already exists** (892 employees, checked directly 2026-09-07). It was genuinely missing when that note was written; it isn't anymore. `seed_payroll_components.py`'s existing `Department.objects.filter(name="Digital Advertising")` lookup needs no change — running that command today would actually seed AdOps's rows instead of hitting the warning branch. Also checked: **all 9 real `roster.ShiftAttribute` rows the workbook references already exist** (`NIG 7-4` … `NIG 11-8`, `CUST 7-4`, `CUST 8-5`, matching start/end times exactly) — the roster layer needs no changes for this work, only `payroll`.

**Overtime basis — narrows/corrects the 2026-08-11 locked decision for AdOps specifically, confirmed 2026-09-07.** The locked decision (§1 table, "OT / night / Eid rates") says OT basis "varies by department, configured per department, not globally" — that framing was always correct, but this doc's own Phase 2 seeding (`seed_payroll_components.py`) put AdOps on `FLAT_PER_OCCURRENCE` (flat per day) alongside Software Development and Creative Development, and question 37 below described AdOps's 200/250 Tk figures as "Tk/day." **That was wrong for AdOps.** Confirmed 2026-09-07: **Software Development and Creative Development stay `FLAT_PER_OCCURRENCE`** (unchanged, still locked) but **AdOps, and every other department without an explicit override, is `PER_HOUR`** — an actual rate × `OvertimeEntry.seconds/3600` calculation, using the OT hours data already collected. Recorded here as a correction rather than silently fixed, per this doc's own posture (see the reverted `ShiftType.is_night_shift` note above). The user flagged Software/Creative may move to hourly too in the future — not decided, no action needed now.

**Access-level tiers for the new approval gates — verified against real seeded data, not invented.** AdOps's real designations map onto the existing `accounts.AccessLevel` ladder cleanly: Executive / Senior Executive → `AccessLevel="Employee"` (rank 10); Team Leader → `AccessLevel="Supervisor"` (rank 20) — the same tier `accounts.permissions.SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME` already uses for "team-leader-and-above"; Assistant Program Manager / Program Manager / Operation Manager → `AccessLevel="Manager"` (rank 30). So "TL or above" (§ below) is the existing Supervisor tier, reused as-is; "APM or above" is a new **Manager** tier threshold — the first thing in `payroll`/`accounts` to gate on it.

**The three 500-Tk-and-350-Tk night rates, disambiguated.** `PayRate.RateType` already has `NIGHT_REGULAR` / `NIGHT_QUARTERLY` / `NIGHT_SPECIAL`, and the 2026-08-20 master-sheet review (question 38 below) already knew AdOps needed three separate night figures — but at the time it could only describe them by amount ("Quarterly/Special Shift: flat 500 Tk/day, the same figure across every other department"), not by *why* there were two rate types both worth 500 Tk. That's resolved now:

- **`NIGHT_REGULAR`** (350 Tk/shift, unbanded — AdOps has no salary band, unlike Software/Creative) — any `NIG`-category shift, employee has no approved Dedicated Night Shift assignment covering that date.
- **`NIGHT_QUARTERLY`** (500 Tk/shift) — **Dedicated Night Shift employees**, only while an approved assignment window covers the shift date, regardless of whether the shift itself is `NIG` or `CUST` (confirmed 2026-09-07: dedicated status wins over shift type — see the new model below).
- **`NIGHT_SPECIAL`, renamed `NIGHT_CUSTOM`** (500 Tk/shift) — **confirmed 2026-09-07, renamed for clarity**: any employee (dedicated or not) working a `CUST`-category shift, when not already covered by an active Dedicated assignment. `RateType.NIGHT_SPECIAL` becomes `RateType.NIGHT_CUSTOM` at implementation time — the enum value has never actually been seeded anywhere (checked directly: zero `PayRate` rows exist in dev today), so this is a plain rename, not a data migration.

**Trainee resource status is excluded from all three night-duty rate types, but not from transport.** *(Superseded 2026-11-03: the cut-off is now the confirmation date, per day — see the dated section at the top.)* Confirmed 2026-09-07: `JobInfo.resource_status = TRAINEE` (73 real employees in AdOps today) earns nothing for `NIGHT_REGULAR`/`NIGHT_QUARTERLY`/`NIGHT_CUSTOM`, but still earns `TRANSPORT_SPECIAL_SHIFT` (400 Tk) when working a `CUST` shift from office. Modelled as a new `resource_status` scope dimension on `PayRate` (§4 below) rather than an engine-level special case — keeps the exclusion data-driven and available to any other department with a similar carve-out, per design principle 3 ("a new allowance is a data change, not a code change").

**New model: `employees.DedicatedNightShiftAssignment`.** Tracks an employee's dedicated-night-duty window (commonly a quarter; an employee can cycle in and out repeatedly over their tenure, so this is a repeatable dated assignment, not a single flag). A Team-Leader-or-above manager (existing `SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME = "Supervisor"` tier) may request one for someone in their managed scope; only an APM-or-above manager (new `"Manager"`-tier threshold) may approve it; only APPROVED assignments make `NIGHT_QUARTERLY` resolve. Lives in `employees`, not `payroll` — this is an HR-managed org fact approved through the reporting hierarchy (the same shape as `leave.LeaveApplication`/`attendance.AttendanceCorrection`), not a payroll-gated financial record like `EmployeeBankAccount`; `payroll`'s engine reads it the same way it already reads `employees.JobInfo`, never owns a second copy. Full field list in §4 below.

**Reopens and closes question 36 below ("Custom Shift" flag).** The 62-employee "Custom Shift" tag Risk 13 flagged from July's compilation is exactly `roster.ShiftType(name="CUST")` usage — an ordinary roster shift type, already fully seeded with the right `ShiftAttribute` variants (`CUST 7-4`, `CUST 8-5`). No manual override path is needed; `NIGHT_CUSTOM` + `TRANSPORT_SPECIAL_SHIFT` resolve it entirely through the standard `PayRate` mechanism. Risk 13 is resolved below.

**`/payroll/report/` (`build_payroll_report`) needs new columns.** This pre-payroll cross-check report (§12 Risk 2) currently buckets `NIG` and `CUST` shift-days into two flat counts with no rate resolution at all. Requested 2026-09-07: split into **three night-count columns** (Regular / Custom / Dedicated — the same three-way classification `NIGHT_REGULAR`/`NIGHT_CUSTOM`/`NIGHT_QUARTERLY` resolve against, so this report and the engine's Stage 4 must share one classification function rather than growing two independent implementations of the same rule — see §6), a **Transport Allowance column** (count of `CUST` shift-days where the resolved work location isn't WFH, via the existing `_LocatedRosterCell` chain), and an **Overtime column** (total OT hours in the window, from `OvertimeEntry.seconds/3600` — the report's first column sourced from something other than `roster.Shift`). Consistent with the rest of this report, these are **day/hour counts, not Tk amounts** — the report's role is a manual cross-check tool, not a payout preview; if Tk amounts are wanted instead, that's a different, explicit ask.

**"Holiday OT" renamed to "GH OT", 2026-09-07 — answered by AdOps's real upload file, same posture as question 24's bank formats.** AdOps supplied `AdOps overtime-upload.xlsx` for actual upload; its `Overtime type` column uses **"GH OT"** (General Holiday — the same "GH" label `roster.ShiftType`/the holiday category already use), not "Holiday OT". `OvertimeType(code="holiday_ot")`'s `name` is renamed to `"GH OT"`, with `"Holiday OT"` (and the existing `"HOT"`) kept as aliases so nothing that already matched the old name breaks — `code` is unchanged. **Root cause of the upload not working at all: `seed_payroll_components` had never actually been run against this dev database** — `OvertimeType`/`PayComponent`/`PayRate` were all empty tables, which is also why the overtime-upload template's "Overtime type" dropdown (`upload.py::build_overtime_xlsx_template`, already wired with a `Lookups`-sheet `DataValidation` list) rendered with no choices: `if ot_type_names:` guards the whole dropdown, and there were zero active `OvertimeType` rows to populate it. The dropdown mechanism itself didn't need building — it already existed and needed no code change, only real seed data. Re-run `seed_payroll_components` whenever a fresh dev database needs this fixture data.

**"Eid Special OT" renamed to "Special OT", 2026-09-07 — deliberate broadening, not a data correction.** Requested so this same `OvertimeType` covers any special-occasion overtime, not only Eid. First pass kept `code="eid_special_ot"` (the GH OT-style safe rename, only changing `name`) — but confirmed unused (zero `OvertimeEntry`/`PayRate` rows referenced it, checked directly), so on the same-day follow-up `code` was fully re-keyed to `"special_ot"` too, `name` is `"Special OT"`, and `"Eid Special OT"`/`"ESOT"` are kept as aliases. Since `update_or_create` looks up by `code`, re-keying it can't "follow" the old row on its own — the existing row (pk unchanged) was renamed in place via a one-off `OvertimeType.objects.get(code="eid_special_ot"); .code = "special_ot"; .save()` before re-running `seed_payroll_components`, so there's exactly one row, not an orphaned duplicate.

**Overtime upload's period restriction removed, 2026-09-07 — deliberate, while the feature set is still being built; revisit later, not an oversight.** `parse_upload()` used to reject any row whose `work_date` fell outside a single upload-time `period` (defaulted to "today's" period, no picker) — in practice this rejected legitimate rows from AdOps's real file (`AdOps overtime-upload.xlsx`, dated 2026-08-05, uploaded on 2026-09-07 when "today's period" was Aug16–Sep15, not the Jul16–Aug15 period the file's dates actually fall in). Removed: each row's `PayrollPeriod` is now derived from its own `work_date` (`period_for_date()`, cached per year/month so a file spanning a couple of periods costs one or two extra `get_or_create`s, not one per row) — which also matches how the engine already reads `OvertimeEntry` (`payroll_inputs.collect_overtime` filters by `work_date` range, never by the `period` FK), so the stored `period` was already just descriptive metadata, not a source of truth. `parse_upload`/`apply_upload`/`build_overtime_xlsx_template` all dropped their `period` parameter; `PayrollInputBatch.period` is left `None` on overtime uploads (nullable specifically for this — a batch can now legitimately span more than one period). The template's date-validation hint no longer claims a period restriction either. `PayrollPeriod` rows are still created lazily (`get_or_create`) exactly as before — nothing about period bookkeeping itself changed, only the acceptance gate.

**Overtime upload rejected resigned employees outright, 2026-09-08 — real bug, not a restriction to relax.** Two employees in AdOps's real file, both worked and left during the Jul16–Aug15 period, were rejected as "No active employee with ID X". Root cause was two independent `is_active=True` filters, both wrong for payroll specifically (same reasoning as `payroll.engine._population_queryset`'s deliberate non-filtering of `is_active` — a person deactivated by `deactivate_past_lwd` after resigning must still be payable for days actually worked):

1. `parse_upload`'s employee lookup itself required `is_active=True`, so a resigned employee's ID resolved to nothing at all. Fixed by dropping the filter and instead validating the actual employment window per row — a new `_employed_on(employee, work_date)` (day-granularity version of the engine's own "employed at any point in the period" predicate: `joining_date <= work_date` and, if separated, `last_working_date >= work_date`) — so a genuinely wrong employee ID/date (hired later, long gone) is still caught, just not every leaver.
2. Even with that fixed, `apply_upload`'s scope check (`row.employee.pk in viewer_visible_pks`) would still have silently dropped the row into `skipped_scope` — `accounts.permissions.visible_employee_pks` (and everything it's built on: `get_reportee_user_ids`, `department_grant_pks`, `team_grant_pks`, `managed_scope_pks`) filters `is_active=True` at every level of the reports-to BFS and every grant query, so a separated employee had already dropped out of every manager's or admin's visible scope, `can_see_all` included. Fixed with a new `include_separated: bool = False` parameter threaded through that whole chain (default preserves existing behaviour everywhere — leave/roster/attendance/general employee lists all keep excluding separated employees from "who's on my team" scope on purpose) and a new `payroll_visible_employee_pks()` that calls it with `include_separated=True`. `payroll/upload_views.py::UploadOvertimeView` now calls this instead of `visible_employee_pks`.

Any future payroll input scoping (variable pay, TDS, a future bonus/incentive uploader) should use `payroll_visible_employee_pks`, not `visible_employee_pks`, for the same reason.

**`/payroll/report/` OT column(s) made dynamic per overtime type, 2026-09-08.** A single summed "OT (hrs)" column hid the fact that different `OvertimeType`s carry different pay rates (`payroll.PayRate.RateType.OVERTIME` is scoped per `overtime_type` — AdOps alone has two, banded by access level). `build_payroll_report()` now returns `ot_types` — the distinct `OvertimeType`s actually present among `payroll_inputs.collect_overtime()`'s results for the report's filtered employees/window (not the full system catalog: a scope with only Regular OT and GH OT entries shows exactly those 2 columns; a Special OT or Eid Package entry appearing anywhere in scope adds a 3rd) — alphabetically ordered, plus each row's `ot_by_type`, a list of hour values in that same order. The list-alignment (not a dict keyed by type id) is deliberate: Django templates can't index a dict by a loop variable without a custom filter, but two independently-ordered `{% for %}` loops built from the same server-side ordering line up for free. Both the HTML table and the XLSX export (`payroll.views.PayrollReportView._export_excel`, header `"OT: {name}"`) consume it the same way.

**`employees.DedicatedNightShiftAssignment` gets django-import-export, 2026-09-08.** `/admin/employees/dedicatednightshiftassignment/` now has Import/Export, matching the existing `accounts.resources.TeamResource`/`DesignationResource` and `roster/admin.py`'s `ShiftTypeResource`/`HolidayResource` pattern: `employee`/`approved_by` matched by `employee_id` (portable across dev/prod, unlike the PK); `import_id_fields = ("employee", "start_date")` since an employee can have several assignments over their tenure (no single-field natural key), so re-importing the same employee+start_date updates that assignment (e.g. extends `end_date`, flips `status`) rather than duplicating it, while a new start_date creates a new row. Deliberately deviates from the sibling resources on one point: `clean_model_instances = True`, so a bad date range or an overlapping-`APPROVED`-window mistake in a bulk file is rejected per-row (django-import-export 4.x surfaces this via `result.invalid_rows`/`has_validation_errors()`, not `has_errors()`) instead of silently corrupting who earns the Dedicated rate — the other resources don't have a comparable money-relevant invariant to protect. `approved_by`/`approved_at` are importable (read-only on the plain admin form) so an already-decided batch — e.g. migrating a quarter's dedicated-night-shift approvals from an existing spreadsheet — can be brought in pre-approved without walking each one through the request/approve UI; this bypasses `can_approve_dedicated_night_shift`'s app-level gate the same way every Django admin bulk action here already can, restricted only by admin model-import permission.

**Export's Status column got an Excel dropdown, same day, tested locally before deploying.** `_add_status_dropdown()` post-processes the exported `.xlsx` (django-import-export's own export has no concept of cell-level validation) to restrict the Status column to a real dropdown of the 4 valid values, extended 200 rows past the exported data so the file also works as an append-new-rows template.

**Real import then failed — `(1048, "Column 'decline_reason' cannot be null")` — a genuine django-import-export gap, not a data problem.** `WIDGETS_MAP` (the table `ModelResource` uses to pick a field's import/export widget from its Django field type) has an entry for `CharField` → `CharWidget` (which turns a blank cell into `""`), but **no entry for `TextField`** — and Django's `TextField` doesn't subclass `CharField`, so `decline_reason`/`note` (both `TextField(blank=True)`, the correct Django convention — `blank=True` alone, no `null=True`) fell through to the bare base `Widget`, which has no such handling and passes a blank cell's `None` straight through. MySQL then rejects the `NOT NULL` column. Fixed by explicitly declaring both fields on the resource with `widget=CharWidget(allow_blank=True)` — the same widget a real `CharField` would have gotten automatically. **Verified through the actual XLSX-reading code path** (`import_export.formats.base_formats.XLSX().create_dataset()`, not just a hand-built `tablib.Dataset`) before considering it fixed, since the bug only manifests on a genuinely blank Excel cell (openpyxl reads that as `None`), not an empty string. Any other `TextField(blank=True)` this resource (or a future one) exposes for import needs the same explicit widget — it's a django-import-export library gap, not specific to this one field.

**Single-record editing added on the profile page, 2026-09-08 — "update it, same as any other section."** The profile card (`templates/employees/sections/_dedicated_night_shifts.html`) now has a per-row Edit pencil, opening the same drawer scaffold every other section uses. Deliberately *not* wired through the generic `SectionSpec`/`SectionEditView` machinery (same reason the Request action wasn't, back in the original build) — the permission gate is two-tier, not the generic single-permission CRUD shape: a new `accounts.permissions.can_edit_dedicated_night_shift()` requires the lower TL+ bar (`can_request_dedicated_night_shift`) while a row is `PENDING`, but the higher APM+ bar (`can_approve_dedicated_night_shift`) once it's `APPROVED` — because at that point the window directly drives `NIGHT_QUARTERLY` pay, so changing it needs the same authority that approved it, not the bar that only sufficed to request it. `DECLINED`/`CANCELLED` rows aren't editable here at all (terminal — file a new request). Status itself is never touched by this edit path; only `start_date`/`end_date`/`note` — approve/decline stays on the existing dedicated actions. New view `employees.views.DedicatedNightShiftEditView`, same drawer-or-full-page-fallback shape as the Request view and `attendance.views.AttendanceCorrectionEditView`; on success, htmx gets a bare `204` + `HX-Refresh: true` (a full reload) rather than a hand-built OOB swap, since this card isn't a `SectionSpec` with the swap machinery the others get for free.

**Audit ledger added 2026-09-15 — `DedicatedNightShiftAssignmentEvent`.** Reported immediately after the tier drop above: a TL edited an approved window and the Pending queue's "Requested by" column showed blank, with nothing anywhere saying who had changed it. Three separate causes, only one of them new:

1. **The blank wasn't the edit's doing.** "Requested by" renders `added_by`, and `DedicatedNightShiftAssignmentResource` never imported that field — so all 116 rows from the 2026-09-10 bulk load have `added_by = NULL` (**116 of 161 rows, 72% of the table**). The demotion just pulled one of them into the Pending tab where it got noticed. Fixed two ways: `added_by`/`added_at` are now importable (a deliberate deviation from the "audit fields are never importable" convention, for the same reason `approved_by`/`approved_at` already deviate), and the list renders a muted **"Imported"** chip instead of a bare em-dash when `added_by` is null — it says where the row came from rather than looking like missing data. The 116 existing rows are **not** back-attributed to whoever ran the import; we don't know who requested them, and an audit trail that guesses is worse than one that admits the gap.
2. **The edit *was* recorded — on `updated_by`/`updated_at` — and the table had no column for it.** Added a "Last updated" column (actor + timestamp) to `/employees/dedicated-night-shifts/`.
3. **The real gap: `_Auditable` is last-writer-wins.** One `updated_by`/`updated_at` pair, so a second edit silently overwrote the first, and nothing recorded *what* changed. Tolerable while only Manager-tier could edit an approved window; not once a Supervisor can, since that window drives `NIGHT_QUARTERLY` pay.

So this model finally got the ledger half of the split it should have shipped with in 2026-09-07 — the same denormalised-state + append-only-event shape as `NightShiftConsent`/`NightShiftConsentEvent` and `policies.PolicyAcknowledgement`/`...Event`. `DedicatedNightShiftAssignmentEvent` records one immutable row per transition (`REQUESTED` / `EDITED` / `DEMOTED` / `APPROVED` / `DECLINED` / `IMPORTED`) with actor, timestamp, and — for events that move the window — the old and new dates. Every write path funnels through `employees.services.record_dedicated_night_shift_event`, including `_approve_night_shift`/`_decline_night_shift`, which are already shared by the single-row and bulk views, so the bulk path records without knowing it does. A null `actor` means system-or-import, never "unknown person", and the display layer renders it as "Imported". Surfaced as a read-only timeline drawer (`DedicatedNightShiftHistoryView`, gated by `can_view_employee` — nothing there is actionable, so a narrower gate would only hide provenance from people already looking at the record) linked from both the profile card and the list page, plus a read-only admin inline.

`employees/migrations/0011` backfills the ledger for the 161 existing rows, strictly from fields those rows already carry: creation (`added_by`/`added_at`, becoming `IMPORTED` when null), approval (`approved_by`/`approved_at`), and — where `updated_at` is meaningfully later than `added_at` and isn't just the approve write — one `EDITED` event that says *someone touched this then* and explicitly admits that what changed wasn't recorded before the ledger existed. No window before/after is invented for those. Result: 321 events across 161 assignments.

**Deployment note — MySQL collation, hit on dev while applying 0010.** The `CreateModel` failed with `(3780, "Referencing column 'actor_id' and referenced column 'id' in foreign key constraint ... are incompatible")`. Not a model problem: the dev database's *default* collation was `utf8mb4_unicode_ci` while all 130 existing tables (loaded from a dump, which writes `COLLATE` explicitly per table) are `utf8mb4_0900_ai_ci`, so every newly created table got the minority collation and couldn't FK to `accounts_customuser.id`. Fixed with `ALTER DATABASE <name> CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci`, which only changes the default for new tables and touches no existing data. MySQL DDL isn't transactional, so the half-created table had to be dropped before retrying. ~~**Check the prod database's default collation before deploying this migration.**~~ **Done — prod was checked 2026-09-19 and is clean** (default, all 132 tables, `accounts_customuser.id` and the MySQL server default are all `utf8mb4_0900_ai_ci`); only the dev database had drifted. No prod action was or is needed. The standing rule this left behind — every new model with a `CustomUser` FK needs the CONVERT step in its own migration — is now written up in `deploy/README.md`'s "MySQL collation" section and in the 2026-09-19 dated section at the top of this file.

**Reversed 2026-09-15 — the APPROVED edit bar drops to TL+, and the pay control moves into the view.** The two-tier rule above (`PENDING` → TL+, `APPROVED` → Manager+) turned out to block the people who actually maintain these windows. Reported against a real account: a Team Leader (`Supervisor`, rank 20) with an employee in his managed scope had no Edit pencil on that employee's *approved* assignment at all, because `can_edit_dedicated_night_shift`'s `APPROVED` branch delegated to `can_approve_dedicated_night_shift` (`Manager`, rank 30). Supervisors are the ones moving a from/to date as an employee cycles in and out of dedicated duty; APM/PM approves. The original rule let them do neither.

Rather than dropping the tier unguarded — which would let a TL extend a paid dedicated-night window with no approver in the loop — **the control moved from "may you open the editor" to "what does your save cost the row"**, the same shape as `payroll.services.OvertimeWindow`'s "may this be written, and *with what status*" answer from 2026-09-15. Concretely:

- `can_edit_dedicated_night_shift` now delegates to `can_request_dedicated_night_shift` (TL+) for **both** `PENDING` and `APPROVED`. It is a pure "may they open the editor" check again; `DECLINED`/`CANCELLED` stay terminal.
- `employees.views.DedicatedNightShiftEditView` captures `was_approved` + `can_approve` in `dispatch()` (before any form binds to the instance and overwrites what it would compare against) and exposes `demotes_on_save`. On POST, an editor who could **not** have approved the row has their save demote it to `PENDING` and clear `approved_by`/`approved_at`. An editor who could approve it saves in place, still `APPROVED`, stamp intact.
- A demoted row therefore drops straight out of `payroll/inputs.py`'s `status=APPROVED` collection — it earns no `NIGHT_QUARTERLY` until re-approved — and reappears in the APM's "Pending my approval" tab with no extra wiring, since `pending_qs` is just `status=PENDING` within managed scope.
- The editor is told, twice: `demotes_on_save` renders an amber "saving sends this back for approval" banner in the drawer *before* the save, and the POST emits a `messages.warning()` (not `success`) naming the consequence. The message is raised before the htmx `204` + `HX-Refresh: true` so it survives the reload in the session and lands as a toast.

Two consequences worth knowing. **The overlap check still fires on a demotion, and the ordering that makes that true is load-bearing.** `DedicatedNightShiftAssignment.clean()` only guards `APPROVED` rows, so the first draft of this assumed a demoted row would skip it — wrong, and the test caught it. The demotion runs *after* `form.is_valid()`, which is where `ModelForm._post_clean()` calls `instance.full_clean()`; `status` isn't a form field, so the instance still carries `APPROVED` at that moment and the clash is rejected with the error on the form. That's the behaviour we want — better than saving a `PENDING` row nobody could ever approve — but it means **moving the demotion above the `is_valid()` call would silently disable the overlap guard**. `test_tl_edit_of_approved_into_an_overlap_is_rejected_not_demoted` exists to catch exactly that. **Self-editing is unaffected** — the 2026-09-08 confirmation below still holds, because `can_request_dedicated_night_shift`'s `_in_managed_scope` never includes `target == user` for a non-admin, which is now the only path in.

Still open: there is no `PayrollRun` staleness signal for this model. If a run is already `CALCULATED` and a window is then edited (by anyone — this predates the change), the register goes quietly stale, exactly the problem `PayrollRun.overtime_changed_since_calculation` / `pending_overtime_count` solve for overtime. A `dedicated_night_changed_since_calculation` property in the same live-computed-at-render shape is the obvious fix; deferred, not done.

**Confirmed 2026-09-08 — self-request/self-edit stays fully blocked, TL/APM tier included.** Raised directly: should a Team-Leader-or-above be able to request/edit their *own* Dedicated Night Shift assignment? Confirmed no — `can_request_dedicated_night_shift`/`can_edit_dedicated_night_shift` already excluded self unconditionally (`_in_managed_scope` never includes self for non-admins, and `can_approve_dedicated_night_shift` has an explicit `assignment.employee_id == user.pk` check), so no code change was needed — only docstring notes and explicit regression tests (`accounts/tests.py::DedicatedNightShiftPermissionTests`) locking the behavior in so it doesn't get "fixed" into a self-service path by accident later. A TL who wants to become dedicated needs their own manager (or an admin) to act on it, same as anyone else.

### Bulk note upload + "Payroll" run-page access reversed back to Supervisors, 2026-09-16

**Built:** `/payroll/runs/<pk>/notes/upload/` — bulk-file Supervisor/HR/Finance notes for a run's employees from a `.xlsx`/`.csv` file (`Employee ID | Note`), mirroring the overtime uploader's shape (`payroll/upload.py`'s new notes-upload section, `payroll/upload_views.py::UploadPayrollNotesView`/`PayrollNotesUploadTemplateView`). One upload writes exactly one role's notes, chosen on the page from whichever roles `accounts.permissions::writable_payroll_note_roles` grants the viewer — not a per-row column, since a single upload is realistically one person (a supervisor, or the curated HR/Finance pair) filing their own note type for a whole team. A blank Note cell is a no-op, never a clear (`report.skipped_blank`) — clearing an existing note still requires the per-employee drawer. The whole upload is refused (`report.skipped_locked`) once the run's inputs are locked (`payroll.services.LOCKED_RUN_STATUSES`) — unlike the per-employee drawer, which has no such gate, since a single HR correction after lock is a different risk than a few-hundred-row bulk rewrite.

**This reverses the 2026-09-10 access decision above, for the run list/detail pages specifically.** That decision deliberately restricted `payroll_run_list`/`payroll_run_detail`/`payroll_run_employee_note_edit` to the curated "Payroll" group (a small hand-picked set that happens to include some supervisors), reasoning that only that group needed to see the register. Building the bulk uploader exposed that as too narrow: a plain supervisor by designation — not personally added to "Payroll" — could already write their own reportees' Supervisor notes one at a time via `can_write_payroll_note`, but couldn't reach the page to do it, bulk or otherwise. Confirmed 2026-09-16: **`"Supervisors"` (the broad designation-based group) is added back** to `payroll_run_list`, `payroll_run_detail`, `payroll_run_employee_note_edit`, and the two new upload URLs. **Money is unaffected** — `payroll_run_export`/`employee_adjust`/`variable_*` stay Payroll-Admins-only, and `can_see_payroll_money` still gates the money columns inside `payroll_run_detail` itself, exactly as before. Recorded here as a reversal rather than silently widened, per this doc's own posture (see the reverted `ShiftType.is_night_shift` field above).

### `payroll.PayRate` gets django-import-export, 2026-09-16

`/admin/payroll/payrate/` now has Import/Export — the first `payroll` model to get it (`payroll/admin.py::PayRateResource`/`PayRateAdmin`), matching the existing pattern in `accounts.resources`/`roster/admin.py`/`employees.admin.DedicatedNightShiftAssignmentResource`. FK fields match by natural key (`code` for `OvertimeType`/`PayComponent`, `name` for the six `accounts` scope models), and `resource_status` gets the same `CharWidget(allow_blank=True)` treatment `decline_reason`/`note` needed on the Dedicated Night Shift resource (blank `CharField(blank=True)` cell → `None` → MySQL `NOT NULL` failure otherwise).

**Deviates from every prior resource on `import_id_fields`.** `PayRate` has no natural unique field at all — it's a pure scope table (`resolve_rate()`'s most-specific-match-wins), so there's no single/short key the way `ShiftType.name`/`Holiday.date`/`(employee, start_date)` give the others. Checked against the real dev data before picking a key: a narrower 4-field key of `(rate_type, overtime_type, department, team)` was proposed first, but **already collided on 8 of the then-30 rows** — e.g. two `NIGHT_REGULAR` rows for the same department differing only by `resource_status` (`""` vs `"TRAINEE"`, the AdOps trainee carve-out), and two differing only by a `min_gross`/`max_gross` band. `import_id_fields` is therefore the full set of scope dimensions `resolve_rate()` actually reads: `rate_type, overtime_type, department, team, sub_project, work_location, grade, min_gross, max_gross, resource_status, min_access_level, effective_from`. Every field in it is optional in the spreadsheet — a blank cell matches `NULL` via Django's ORM — so a row using only 2-3 dimensions behaves exactly like a short-key import would; only rows sharing those dimensions but differing elsewhere need the rest of the tuple to disambiguate. `clean_model_instances = True`, same rationale as the Dedicated Night Shift resource: `PayRate.clean()` enforces real payroll invariants (OT/`GROSS_30TH_PER_DAY` rules), and a bad bulk row here directly mispays overtime/night rates. Tests: `payroll/tests_resources.py`.

### `OvertimeEntry.worked_in` + team-scoped overtime rate resolution actually wired up, 2026-09-16

**Found while scoping this feature, not introduced by it: team-scoped `PayRate` rows were dead data.** `PayRate.team` and `resolve_rate(team=...)` have existed since Phase 2/3, and Data Solutions & Due Diligence already has 6 team-scoped `OVERTIME` rows (MARS/MARS QC/AQM Content Source/Alpaca/BVD/Kompany, all ৳250/hr vs. the department default ৳200/hr) — but `engine.py`'s OT rate-resolution call never passed `team=` into `resolve_rate()`. Every OT hour for those teams was being priced at the department fallback. This is fixed as part of the same change, not filed separately, since the new field is what makes a team actually resolvable per entry.

**`OvertimeEntry.worked_in`** (FK to `accounts.Team`, nullable) records which team an entry's overtime was actually worked for — not necessarily the employee's current department/team, since `PayRate` scope can differ by team within a department and the two can drift (a transfer, a temporary loan to another team, etc). Blank on both the bulk uploader (`payroll/upload.py`, new "Worked In" column, position C in the 5-column layout) and the manual drawer (`OvertimeEntryForm`) **snapshots the employee's current `CustomUser.team` at write time** — not re-derived later, so a subsequent team transfer doesn't retroactively change an already-filed entry's rate. Stays blank if the employee has no team on file.

`payroll_inputs.collect_overtime()` now groups by `(overtime_type_id, worked_in_id)` instead of `overtime_type_id` alone, and `engine.py`'s OT stage loops over that pair, passing `team=` into `resolve_rate()` and emitting one `PayrollRunLine` per (type, team) combination actually logged — so an employee who worked OT for two different teams within one period gets two separately-rated lines rather than one blended one. `reports.py::build_payroll_report`'s per-type OT columns sum back across team locally (that report doesn't break OT out by team) — the shared `collect_overtime()` call didn't need a second function. `build_shift_summary` doesn't touch overtime at all, so it's unaffected.

**Backfill (`0017_backfill_overtimeentry_worked_in.py`) is deliberately scoped**: only entries whose period has no `PayrollRun` that reached `CALCULATING`/`CALCULATED` get `worked_in` set from the employee's team. Entries under an already-calculated run are left `NULL`, so a future recalculation of an old run can't silently start applying a team-scoped OT rate that was never part of what was actually paid — a decision, not an oversight, if a genuine historical restatement is ever needed.

## 1. Context

SEBPO runs payroll through two disconnected Excel workbooks — an HR Operations compilation master and a Finance salary workbook — with the Finance SOP's steps 2–6 consisting largely of *copying HR's file across and verifying it*. Orbis already holds the authoritative employee, roster, leave and offboarding data but is not in the loop, so the same information exists in three places and is reconciled by hand every month.

**Outcome:** one payroll run object inside Orbis that reads employees / roster / leave / offboarding directly, accepts only the inputs the system genuinely cannot derive, calculates to net pay, and emits payslips and the bank advice.

### Confirmed decisions

| Decision | Choice |
|---|---|
| Salary structure | **Gross salary only** — a single scalar amount per employee, effective-dated. No component breakdown (Basic/House Rent/Medical/Conveyance are not stored as separate figures). Locked 2026-08-11. |
| OT / night / Eid rates | **Flat BDT amounts**, one effective-dated table, most-specific-wins resolution. **OT basis (exact-second vs. flat-per-day) varies by department** — configured per department, not globally per overtime type. Locked 2026-08-11. |
| Meal deduction | **Tiered by the employee's own gross salary**, deducted from the employee: ≤25,000 → 20 BDT/meal; 25,000–50,000 → 30 BDT/meal; >50,000 → 50 BDT/meal. Locked 2026-08-11. |
| TDS | **Calculated in-system every period** (§4 "Run & results", confirmed 2026-08-24 from Finance's actual tax template) from gross salary + a per-employee tax category + an annual investment declaration. A manual `TDSEntry` override still exists for corrections/edge cases and wins if present. Originally locked 2026-08-11 as upload-only — superseded. |
| Provident Fund / Gratuity | **Neither exists at SEBPO.** No PF, no gratuity modelling. Locked 2026-08-11. |
| Festival bonus | **2 Eids per year, 50% of gross salary each.** No grade-banding, no fixed amount. Locked 2026-08-11. **Narrowed and confirmed 2026-08-13:** eligibility also requires **>180 days' tenure measured against the Eid date** — an employee joining fewer than ~6 months before a given Eid gets nothing for that Eid (`PAYROLL_PROCESS.md` §12 Q25). |
| Scope | End to end — gross → additions → deductions → loans → TDS (uploaded) → net → register → approval → payslip → bank advice |
| Department Excel submissions | **Removed entirely** |
| Overtime input | Supervisor XLSX: `working date, employee id, overtime_type, OT hours (hh:mm:ss)`; types Regular OT / GH OT (renamed 2026-09-07 from Holiday OT — see the dated section below) / Eid Package / Special OT (renamed 2026-09-07 from Eid Special OT, broadened to any special occasion); stored as **integer seconds** |
| OT approval gate | **None** — supervisor's upload is final; HR can correct during the compilation window |
| Meal count | Pulled from **meals.sebpo.net**. Confirmed 2026-09-09 with the meals dev: the API's `total_meals` already **excludes fully company-subsidized/complimentary meals** (special-occasion catering — Eid, New Year, etc.) — this applies even to employees whose regular meals are normally deducted from salary, since a complimentary meal on those occasions isn't one of "their" deductible meals. Orbis does no filtering of its own; the count it reads is already deduction-eligible only. |
| Eid coverage | **Derived** from roster shifts + holiday calendar over approved windows, WFO vs WFH rates |
| Bonus / incentive / disciplinary | Excel upload **or** single entry by HR / dept head / allowed managers |
| Run approval chain | **Fixed 3-stage chain** (locked 2026-08-11): Department Review (own department) → HR Approval (org-wide) → Finance Approval (final). Management's disbursement sign-off is a signature on the printed advice, not a digital step. |
| Salary visibility | **Downline managers** see their reporting line's salary; **HR and Finance see all employees'.** Locked 2026-08-11. |
| Historical salary data | **Not backfilled.** Only changes made in Orbis going forward carry effective-dated history. Locked 2026-08-11. |
| Late input cutoff | Bank-detail edits, loan changes and backdated adjustments **may be applied to the current, not-yet-locked run** rather than forced into a supplementary run. Locked 2026-08-11. |
| Statutory outputs | **Deferred to phase 2+.** Not required for initial go-live. Locked 2026-08-11. |

### Current state of the `payroll` app

A stub. `payroll/models.py` is 3 lines; **zero migrations**. The only real code is `payroll/reports.py` (`build_payroll_report` counting roster shift-days into AL/CL/SL/MTL/FL/CPL/HDL/LWP/NIG/CUST buckets, plus `build_shift_summary`) and `payroll/views.py` (`PayrollReportView` + XLSX export).

**No salary, bank, loan, tax, grade, cost-centre or GL field exists anywhere in the project.** Compensation modelling is entirely greenfield.

Verified against the production dump (2026-08-06): a `Payroll` `MainMenu` exists (icon `hand-coins`, rank 60, url `payroll_report`) with **zero submenus**. `auth_group` holds only `All Employees`, `Finance`, `HR`, `IT`, `Policy Managers`, `Senior Leadership`, `User Admins` — **there is no payroll group**.

### Data already owned by Orbis (never re-enter)

| SOP input | Orbis source |
|---|---|
| New joiners | `CustomUser.joining_date` |
| Leavers / LWD / final settlement | `CustomUser.last_working_date`, `offboarding.Resignation`, `OffboardingCase` |
| Transfers | `CustomUser.department` / `.team` / `.subteam` |
| Confirmation | `CustomUser.confirmation_date`, `.probation_month` |
| Leave incl. LWP | `roster.Shift` by `ShiftType.category`; LWP via `leave.LeavePolicy.is_paid=False` |
| Night shift count | `roster.Shift` where `shift_type` is night-flagged (`NIG`, 22:00–07:00) |
| Special/custom shift | `roster.Shift` where `shift_type = CUST` (19:00–04:00) |
| Holidays | `roster.Holiday` |
| Billable / Live-Bench | `employees.JobInfo.billing_status` / `.resource_status` |
| Workstation / WFO-WFH | `accounts.JobLocation` / `accounts.WorkLocation` |
| Leave encashment | `leave.EncashmentRequest` |

---

## 2. Design principles adopted

1. **Effective-dated compensation master** — salary is a series of dated records, never a mutable number; backdated promotions produce arrears arithmetically. *Kills HR SOP §7.*
2. **The run as an immutable snapshot** — the run copies every input it used into its own result rows, so a payslip re-rendered in 2029 shows what was true in 2026. *Kills HR SOP §2 and Finance SOP §2–5.*
3. **Ordered component evaluation** — a new allowance is a data change, not a code change. *Kills the hardcoded per-department handling in HR SOP §5.*
4. **A gated state machine** — nothing disbursable until approved, nothing editable after close. *Models Finance SOP §13.*
5. **Append-only ledgers for balances** — already the house pattern in `leave.LeaveBalanceTransaction`.
6. **Correction by reversal, never by editing** — a locked run is negated by a reversal run and fixed by a supplementary run.
7. **A variance report** — month-over-month per-employee delta with a threshold, replacing HR SOP §5's 26-point human eyeball check.
8. **Controls are product features** — maker-checker approval, permission separation, cutoff enforcement, checklist status and export logging are first-class requirements, not admin habits outside the system.
9. **Exception review replaces spreadsheet review** — the UI must present pre-payroll and post-payroll control checks so HR/Finance can clear, hold or explain issues before payment.

> **Guiding rule:** Orbis already owns the employee master. Payroll *reads* and *snapshots* it. Payroll never stores a second copy of anything the rest of the app knows.

---

## 3. Module layout — `payroll/`

| File | Responsibility |
|---|---|
| `models.py` | all models (one file, matching `leave`/`documents`/`offboarding`) |
| `periods.py` | 16→15 period math, December→January rollover |
| `rates.py` | effective-dated rate resolution + per-run `RateCache` |
| `inputs.py` | one collector per input source |
| `engine.py` | orchestrator + `ENGINE_VERSION` |
| `tax.py` | `compute_tax_for_employee()` — the in-house ITA-2023 slab/rebate engine, confirmed 2026-08-24 from Finance's actual template. Checks a manual `TDSEntry` override first (unchanged from the original design); calculates from `EmployeeTaxDeclaration` + current gross salary otherwise. |
| `upload.py` / `upload_views.py` | OT + variable-pay importers (mirror `roster/upload.py`) |
| `meals.py` | meals.sebpo.net client |
| `payslip.py` | `PAYSLIP_CONTEXT_SCHEMA` + WeasyPrint render |
| `bank.py` | bank-advice format registry |
| `exports.py` | register + GL/cost-centre XLSX |
| `services.py` | run lifecycle transitions, lock, reverse, settlement |
| `signals.py` | approval chain (port of `documents/signals.py`) |
| `permissions.py` | compensation visibility — **deliberately narrower than `accounts.permissions`** |
| `reports.py` | existing shift-count report, retained (see Risk 2) |

---

## 4. Models

All extend the house `_Auditable` mixin (`leave/models.py:21`).

### Configuration

`PayrollSettings` — singleton `pk=1`, mirrors `documents.CompanyBranding` (`load()` classmethod, `save()` forces id, delete blocked). Holds period start day, payable-days basis, net rounding mode, OT rounding minutes, encashment daily basis, payslip template + delivery mode, notice-pay basis. **No gratuity field — locked 2026-08-11: no gratuity scheme.**

`PayslipCounter` — mirrors `policies.PolicyCounter`; bumped under `select_for_update`.

`PayComponent` — the catalogue. `code` SlugField unique, `name`, `short_name`, `kind` (`EARNING`/`DEDUCTION`/`EMPLOYER_COST`/`INFORMATIONAL`), `entry_mode` (`MASTER` enterable on a structure / `INPUT` from a period table / `COMPUTED` engine-only), `is_taxable`, `is_prorated`, `affects_gross`, `sequence`, `gl_code`, `show_on_payslip`, `is_active`. Mirrors `roster.ShiftType` as a flat catalogue of everything that can appear as a line.

`OvertimeType` — `code`, `name`, `aliases` JSONField (so "Regular OT" / "regular_ot" / "ROT" all resolve via a lowercased alias cache, the `roster.upload._build_shift_cache` idiom), `basis` (`PER_HOUR`/`FLAT_PER_OCCURRENCE`), `component` FK, `requires_holiday`.

**`PayRate` — one table for OT, night allowance, Eid coverage and meals.** `rate_type`, optional `overtime_type` FK, `component` FK, nullable scope FKs (`department`, `team`, `sub_project`, `work_location`, `grade`), `basis` (`PER_HOUR`/`PER_NIGHT`/`PER_DAY`/`PER_MEAL`/`PER_OCCURRENCE`/`GROSS_30TH_PER_DAY`), `amount`, `effective_from`/`effective_to`. **`amount` is taka for every basis except `GROSS_30TH_PER_DAY`, where it is a multiplier** (added 2026-09-09 — see the dated section).

`rates.py::resolve_rate()` filters by date validity then orders by a **specificity score** (sub_project 8 > team 4 > work_location 2 > department 1 > grade 1 > global 0), tie-breaking on latest `effective_from`. A `RateCache` preloads every active rate once per run. This one model covers the SOP's 1–4 night-rate tiers, 1–3 OT tiers and WFO/WFH Eid rates without a schema change per department.

**Two more scope dimensions, confirmed 2026-09-07 (AdOps night/OT/transport policy, see the dated section above) — same additive pattern as `min_gross`/`max_gross` below, not a redesign:**
- **`resource_status`** (blank = wildcard, choices from `employees.JobInfo.ResourceStatus`) — lets one department's `PayRate` row zero out a specific resource status (AdOps seeds `resource_status=TRAINEE, amount=0` rows for `NIGHT_REGULAR`/`NIGHT_QUARTERLY`/`NIGHT_CUSTOM`) without an engine-level special case. Adds the usual `+1` specificity weight when set, same as a gross band.
- **`min_access_level`** FK → `accounts.AccessLevel`, nullable — an open-ended floor ("this access-level rank or above"), resolved against the employee's effective access level (`Designation.access_level`, or `CustomUser.access_level` when manually overridden — same resolution `accounts.permissions._has_min_access_level`/`_has_own_min_access_level` already do for permissions). One-sided by design: every real case so far ("Team Leader and above get 250") is a floor, never a closed band, so this doesn't need a `max_access_level` twin. Also adds a `+1` specificity weight when set. This is what lets AdOps's OT be two rows instead of duplicating the same amount across every designation: no floor → 200 Tk (Employee tier), `min_access_level=Supervisor` → 250 Tk (Supervisor/Manager/Administrator all inherit it).

**Template review 2026-08-13, confirmed by Finance same day (`PAYROLL_PROCESS.md` §12 Q26).** Night-shift and OT rates are banded by the *employee's own gross salary*, but **only in Software Development and Creative Development** (e.g. Software: 350 Tk below a 40,000 Tk threshold vs. 450 Tk above; Creative: 250 vs. 400) — every other department uses a single flat per-hour rate, as already listed in HR's compilation file. `MealDeductionTier` already solves this exact shape for meals; add an optional `min_gross`/`max_gross` band to `PayRate`, included in the specificity score only when populated. Left null (the common case, every department but these two), a `PayRate` row behaves exactly as already designed — this is a pure additive field, not a redesign. ~~**Q27 is resolved and needs no change:** the `gross ÷ 30 × days × 1.5` per-day formula spotted in the workbook is a dead leftover, not active — `PayRate.basis` stays at its two locked values (`PER_HOUR`/`FLAT_PER_OCCURRENCE`).~~ **Reversed 2026-09-09 — see the dated section below.** Finance confirmed in writing that this formula is the *current active* overtime policy for Software Development and Creative Development. A third basis value was added.

**Master sheet review 2026-08-20, confirmed against July 2026's actual production data (`PAYROLL_PROCESS.md` §12 Q37–38).** The salary-banded Software/Creative figures above match production exactly. Two further concrete figures, both seedable at Phase 2 without a schema change: ~~**AdOps's flat OT rate (`FLAT_PER_OCCURRENCE`) resolves to 200 Tk/day Below Team Leader, 250 Tk/day Team Leader and above** — banded by the existing `grade` scope FK, not salary.~~ **Corrected 2026-09-07 — see the dated section above.** AdOps's OT is `PER_HOUR`, not `FLAT_PER_OCCURRENCE` (that basis stays correct only for Software Development/Creative Development); 200/250 Tk are hourly rates. And the banding dimension is `min_access_level` (Employee tier vs. Supervisor-tier-and-above), not `grade` — AdOps has no `accounts.Grade` data to band against, but its designations already map cleanly onto the existing `AccessLevel` ladder. And night duty is not one rate per department — HR's file resolves it as **three separate `PayRate.rate_type` rows**: `NIGHT_REGULAR` (the salary-banded Software/Creative figures above, and — confirmed 2026-09-07 — a flat unbanded 350 Tk/shift for AdOps), and `NIGHT_QUARTERLY`/`NIGHT_CUSTOM` (renamed 2026-09-07 from `NIGHT_SPECIAL`; flat 500 Tk/shift, the same figure across every other department — AdOps, Creative, BPO — regardless of salary, but gated by different eligibility rules — see the dated section above). `rate_type` already exists on `PayRate`; this confirms it needs at least these three night values, not one, when Phase 2 seeds real rates.

**Master sheet review 2026-08-20 — one new confirmed `PayComponent`.** HR's file tracks `Special Shift Transport` as its own column, independent of the night-duty allowance: a flat **400 Tk/day** paid to AdOps staff on Quarterly/Special-shift days. Evidenced by several `Team Comments` rows requesting a missed-transport top-up from a prior payroll, always in exact multiples of 400 Tk/day — real-world confirmation that a missed variable-earning line is exactly the `SUPPLEMENTARY`-run scenario §6 already models, not a new correction mechanism. Add `PayComponent(code="SPECIAL_SHIFT_TRANSPORT")` and a matching `PayRate(rate_type=TRANSPORT_SPECIAL_SHIFT, basis=PER_DAY, amount=400)` at Phase 2 seeding, resolved the same way as the night-duty rates.

**Locked 2026-08-11 — OT basis is resolved per department, not fixed on the type.** The same `OvertimeType` (e.g. Regular OT) pays `PER_HOUR` (to the exact second) in some departments and `FLAT_PER_OCCURRENCE` (a fixed amount per day regardless of hours) in others. `PayRate.basis`, resolved per department via `resolve_rate()`, is the source of truth at calculation time; `OvertimeType.basis` is only a fallback default when no department-specific rate exists.

**Locked 2026-08-11 — meal deduction is not a `PayRate` row.** It does not use department/team/location specificity at all; it is resolved from the employee's own current gross salary against a banded table: new `MealDeductionTier` (`min_gross`, `max_gross`, `amount_per_meal`) — ≤25,000 → 20 BDT, 25,000–50,000 → 30 BDT, >50,000 → 50 BDT. It is a deduction from the employee, never an employer subsidy. **Boundary fix 2026-09-28:** `max_gross` is inclusive (`engine._resolve_meal_tier`), so exactly 25,000 → 20 and exactly 50,000 → 30, matching the ≤/> wording above — the engine had used an exclusive upper bound, charging those two exact figures the next tier up.

### Dedicated Night Shift assignment — new, 2026-09-07, lives in `employees` not `payroll`

`employees.DedicatedNightShiftAssignment` — the only model in this section that isn't a `payroll` model; called out here because `payroll`'s engine is its only real consumer. Rationale for the app placement is in the dated section above (HR-managed org fact approved through the reporting hierarchy, same shape as `leave.LeaveApplication`/`attendance.AttendanceCorrection`, not a payroll-gated financial record). Fields:

| Field | Notes |
|---|---|
| `employee` | FK `CustomUser` |
| `start_date` / `end_date` | `DateField` — the assignment window, commonly one quarter |
| `status` | `PENDING` / `APPROVED` / `DECLINED` / `CANCELLED` |
| `requested_by` | FK `CustomUser` — must pass `can_request_dedicated_night_shift` |
| `approved_by` / `approved_at` | FK `CustomUser` (null) / `DateTimeField` (null) |
| `decline_reason` | `TextField`, blank |
| `note` | `TextField`, blank |

An employee can cycle in and out of dedicated duty repeatedly over their tenure (§ dated section above: "may be moved to any other shift for a month or a quarter... later may be moved to the dedicated night shift again"), so this is a repeatable dated row, not a single boolean/date-pair on `JobInfo`. `clean()` rejects overlapping `APPROVED` windows for the same employee. Only an `APPROVED` row with a window covering the shift's date makes `PayRate.RateType.NIGHT_QUARTERLY` resolve for that employee/date in the engine (§6) — a `PENDING`/`DECLINED`/`CANCELLED` row pays nothing extra.

New permissions in `accounts/permissions.py`, mirroring the existing `can_request_attendance_correction`/`can_act_on_attendance_correction` shape (same `_in_managed_scope` pattern, nobody approves their own):
- `can_request_dedicated_night_shift(user, target)` — `_has_min_access_level(user, "Supervisor")` (reuses `SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME`, no new constant) **and** `target` in `user`'s managed scope, or `can_edit_all`.
- `can_approve_dedicated_night_shift(user, assignment)` — a new `DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME = "Manager"` constant **and** the assignment's employee in `user`'s managed scope, or `can_edit_all`.

Registered as an `employees.SectionSpec` so it renders on the profile drawer (add form gated by `can_request_dedicated_night_shift`), plus an APM-facing approval queue mirroring the attendance-corrections list/approve UI. Requesting one against a `resource_status=TRAINEE` employee shows a soft `messages.warning()`, not a hard block (matches the existing payroll soft-gate posture) — the rate resolution already zeroes `NIGHT_QUARTERLY` for Trainees regardless via the `resource_status` scope on `PayRate` above, so this is a courtesy nudge, not a safety-critical check.

**Managed from the Roster sidebar — 2026-11-03.** The list page (`/employees/dedicated-night-shifts/`, URL unchanged) is now the Roster section's third submenu, "Dedicated Night Shift" (`seed_orbis --menus-only` on prod), open to All Employees + User Admins like Shift Changes, because the TL+/APM+ bars are designation tiers a group can't express; the page scopes itself. It previously had no sidebar entry and a Team Leader couldn't find a decided row again: "Mine" is the viewer's own windows and "All" is HR-only. Added: a **"My team"** tab (`accounts.permissions::dedicated_night_shift_team_pks` — exactly who `can_request_dedicated_night_shift` lets the viewer file for, every status, the default tab whenever shown); a **"New request"** button whose drawer carries an employee picker (`DedicatedNightShiftRequestForm`, candidates from `dedicated_night_shift_candidates`, the chosen employee re-checked with `can_request_dedicated_night_shift`, so the self-request block of 2026-09-08 still holds); the standard q/department/team filters plus status and "active on"; per-row Edit (the existing drawer, demotion rule unchanged); and **Cancel** (`can_cancel_dedicated_night_shift`, `DedicatedNightShiftAssignmentEvent.Event.CANCELLED`, reason on the ledger). Cancel rules: a PENDING row by whoever may edit it; an APPROVED row only at the approval bar **and only before its window starts** — once nights have been worked at the dedicated rate, ending it early is an `end_date` edit, which keeps the worked part paid, where a cancel would un-pay it on the next recalculation. HR/Admins (`can_edit_all`) may cancel a started window, for an approval made by mistake.

**Department head owns the window's roster — 2026-11-03.** Inside an APPROVED window, shift change requests are decided by the employee's department head only (not HR), and direct roster writes other than NIG/CUST/non-working types need the head; approving a window over existing day shifts warns but doesn't block. Full rule: `docs/SHIFT_CHANGE_REQUEST_PLAN.md` §13, `roster/night_window.py`.

`Bank`, `BankBranch` (`routing_number` unique), `PayrollApprovalConfig` (`run_type`, `approver_group`, `approval_order`, `min_net_total` for threshold escalation, `block_preparer_approval` — mirrors `documents.DocumentSignatoryConfig` but adds payroll's segregation-of-duties rule).

**Template review 2026-08-13 — resolves an open question.** `bank.py`'s format registry (§8) needed a list of actual banks; Finance's workbook has three named advice formats seeded and ready: **Bank Asia PLC (BAL)**, **Eastern Bank PLC (EBL)**, **Modhumoti Bank Limited**. Seed `Bank` rows for these three at Phase 5 rather than leaving the registry generic.

`PayrollCalendarEvent` — optional per-period schedule rows (`event_type`, `planned_at`, `actual_at`, `owner_group`, `status`) for period open, input lock, HR review, Finance review, approval deadline, bank release and paid confirmation. This is useful operationally but not required for the calculation engine; Phase 1 can seed default offsets from `PayrollSettings`.

### Compensation master

`SalaryStructure` — UUID pk, `employee` PROTECT, `effective_from`/`effective_to`, `revision_no`, `reason` (`NEW_HIRE`/`CONFIRMATION`/`ANNUAL_INCREMENT`/`PROMOTION`/`TRANSFER`/`CORRECTION`/`DEMOTION`), **`gross_monthly` — the only stored figure**, `status` (`DRAFT`/`PENDING`/`APPROVED`/`SUPERSEDED`/`CANCELLED`), leave-style approval quintet.

**Locked 2026-08-11 — no `SalaryStructureLine`.** Finance confirmed only gross salary is stored per employee, effective-dated; Basic/House Rent/Medical/Conveyance are not tracked as separate figures. The calculation engine's "structure earnings" stage (§6, Stage 3) now produces a single `PayrollRunLine` (a `GROSS_SALARY` component) rather than one line per structure component. Consequence: any Basic-denominated formula has no input to read — leave-encashment rate (open question 22 in `PAYROLL_PROCESS.md` §12) must be answered in terms of gross.

`EmployeeBankAccount` — `bank`, `branch`, `account_name`, `account_number`, `account_type`, `routing_number` (denormalised from branch for snapshot safety), `is_primary`, `is_verified`, `verified_by`/`verified_at`. Registered as an `employees.SectionSpec` so it renders in the profile drawer, but **lives in `payroll`** so `payroll/permissions.py` gates it independently of `can_view_employee`.

**Template review 2026-08-13, confirmed by Finance same day (`PAYROLL_PROCESS.md` §12 Q30/Q31) — now a firm requirement, not an open question.** Cash, Cheque and MFS wallets (**bKash and Nagad**, specifically) must all be supported alongside bank transfer at go-live. Two consequences: (1) `EmployeeBankAccount.account_type` gains an MFS choice per provider (`MFS_BKASH`/`MFS_NAGAD`), with `branch`/`routing_number` optional and `account_number` doubling as the wallet's registered phone number; (2) `BankAdvice` is bank-transfer-specific by construction and can't represent cash or cheque — Phase 5 needs a parallel `CashChequeDisbursement`(+`Line`) pair (cheque-number sequence, cash-paid flag) so every included employee, whatever their payment method, lands in exactly one disbursement record and one `PayrollExportAudit` entry, not just the bank-paid ones.

### Period inputs

`PayrollPeriod` — `year`, `month` (the naming month), `start_date`, `end_date`, `days_in_period`, `status`. `periods.py::period_bounds(y, m)` returns `(date(y, m-1, 16), date(y, m, 15))` with December→January rollover; `period_for_date(d)` is its inverse.

**`period_type` (`STANDARD`/`CUSTOM`) added 2026-09-08 — Create Run now takes an explicit from/to range, not just "today's period".** HR/Finance needed to (a) open a run for a *past* standard cycle (e.g. testing against Jul16–Aug15 while the calendar was already in Aug16–Sep15) and (b) run a shorter, non-16-15-aligned cycle for special occasions — the concrete example given was a ~20-day Eid-month payroll. `run_list.html`'s Create Run form now posts `start_date`/`end_date` (defaulting to the current standard cycle); `PayrollPeriod.for_range()` reuses/creates the matching `STANDARD` row when the dates are an exact 16th→15th cycle, otherwise creates a `CUSTOM` row keyed by its own dates (the `UniqueConstraint(year, month)` was narrowed to `condition=Q(period_type="STANDARD")` so a custom range can share a nominal month with a standard period — mirrors `PayrollRun.clean()`'s existing "MySQL doesn't enforce the partial-unique constraint" pattern). This is **not** the §6 off-cycle-run epic (festival bonus with its own calc rules, tenure gate) — it's the same Phase 3 REGULAR engine, just run over a different date span. It works with no engine changes because `payroll/inputs.py`'s LWP/night/overtime collectors already read `period.start_date`/`end_date` directly rather than the `period` FK (see the overtime-upload note above), and `TDSEntry`/`VariablePayEntry` — the two input types that *are* period-FK-scoped — are only ever created against `run.period` from the adjust drawer, never against a separately-chosen period.

`PayrollInputBatch` — the upload/entry envelope: `input_kind`, `source`, `uploaded_file`, `status` (`DRAFT`/`VALIDATED`/`APPLIED`/`FAILED`/`REVERSED`), `row_count`/`error_count`, `report_json` (the serialised `ApplyReport`). This is the audit trail `roster/upload.py` currently lacks.

`OvertimeEntry` — `employee`, `period` (denormalised from `work_date`, validated in `clean()`, indexed), `work_date`, `overtime_type`, **`seconds` PositiveIntegerField**, `batch`, `status` (defaults **APPROVED** per the confirmed decision; retained only so HR can void or correct), `note`. `UniqueConstraint(employee, work_date, overtime_type)` so re-upload upserts rather than duplicating.

`MealCount` — `employee`, `period`, `meal_count`, `unit_amount`, `source`, `fetched_at`. Persisted at pull time so calculation never depends on a live external call.

`EidCoverageWindow` (`start_date`, `end_date`, `component`, optional `department`, approval fields) + `EidCoverageEntry` (derived per employee, `work_mode` WFO/WFH resolved via the `_LocatedRosterCell` fallback chain, reviewable before the run).

**Template review 2026-08-13, confirmed by Finance same day (`PAYROLL_PROCESS.md` §12 Q28) — simplifies this design.** There is no continuous rate × days calculation. "Eid Package" is a fixed 3-day / 24-hour coverage window paying a flat amount per employee — WFH 12,000 Tk, WFO 15,000 Tk — not a per-day rate. Hours worked *beyond* that window are paid as ordinary overtime through the existing `OvertimeEntry`/`OvertimeType` path, not a separate Eid rate. So `EidCoverageEntry` is a per-employee **eligibility flag** (did they cover the package window, and in what work mode) driving one flat `PayRate` lookup (`basis=FLAT_PER_OCCURRENCE`, scoped by work_location for WFO/WFH), not a per-day accumulator — materially simpler than originally scoped. This also resolves `PAYROLL_PROCESS.md` §12 Q7: "Eid Package" *is* this flat package, confirming it's a flat payment per occasion, not an hourly OT type.

`VariablePayEntry` — **one table for performance bonus, incentive and disciplinary adjustment**; the component's `kind` decides sign. `amount` always a positive magnitude, `reason` mandatory, `attachment` optional, leave-style approval fields. Deliberately **no unique constraint** — one employee can legitimately have two incentives in a period.

`ArrearEntry` — `employee`, `component`, `source_period`, `target_period`, `structure`, `old_amount`/`new_amount`/`amount`, `applied_in_run`.

### Loans

**Superseded 2026-10-08 — see the dated section "Salary loans" above and `SALARY_LOAN_PLAN.md`; kept for history.** The built model has a BigAuto pk, no `loan_type`, no cached totals and no instalment ledger.

`SalaryLoan` — UUID pk, `loan_type`, `principal`, `installment_amount`, `installment_count`, `first_deduction_period`, cached `outstanding_principal` / `total_recovered` / `installments_paid`, `status`. Structurally `leave.LeaveBalance`, with an `outstanding` property in the `LeaveBalance.available` idiom.

`SalaryLoanTransaction` — **append-only, a direct structural copy of `leave.LeaveBalanceTransaction`**: `loan` CASCADE, `occurred_at`, signed `delta`, `reason` (`DISBURSEMENT`/`INSTALLMENT`/`PREPAYMENT`/`WAIVER`/`ADJUSTMENT`/`SETTLEMENT`/`REVERSAL`), nullable `payroll_run` FK (the analogue of `LeaveBalanceTransaction.application`), `created_by`, `note`.

**Invariant:** totals are never edited without a matching transaction row in the same `transaction.atomic()`.

### Run & results

`PayrollRun` — UUID pk, `period`, `run_type` (`REGULAR`/`OFF_CYCLE`/`SUPPLEMENTARY`/`FINAL_SETTLEMENT`/`ARREAR`/`REVERSAL`/`SIMULATION`), `sequence`, `name`, `status`, `scope_filter_json`, `payment_date`, `employee_count`, five money totals, calculation/lock/paid timestamps, `engine_version`, `config_snapshot` JSON, `rejection_history` JSON + `append_rejection()` (copied verbatim from `documents.DocumentRequest`), `reverses` self-FK, leave-style approval quintet, **`disbursement_signoff_file`, `disbursement_signoff_uploaded_by`, `disbursement_signoff_uploaded_at`** (scanned copy of management's signed printed disbursement authorisation — confirmed 2026-08-13, `PAYROLL_PROCESS.md` §12 Q23; uploaded by Finance at the Paid-confirmation step, same `FileSystemStorage` posture as payslips).

Constraints: `UniqueConstraint(period, run_type, sequence)`; partial unique "one non-cancelled REGULAR run per period" **plus a `clean()` mirror** (MySQL ignores partial-unique conditions — the posture `offboarding.Resignation.clean()` documents).

`PayrollRunEmployee` — the register row and payslip header. **Every display field is a snapshot column**; FKs exist for traceability only and are never read for rendering.

| Group | Fields |
|---|---|
| org snapshot | `employee_code`, `full_name`, `department_name`, `team_name`, `sub_team_name`, `sub_project_name`, `designation_name`, `grade_code`, `employment_type_code`, `cost_center_code`, `gl_code`, `job_location_name`, `work_location_name` |
| lifecycle | `joining_date`, `confirmation_date`, `last_working_date`, `resource_status`, `billing_status` |
| person | `gender`, `date_of_birth`, `tin` (read from `employees.IdentificationNumber` type TIN) |
| bank | `bank_name`, `bank_branch_name`, `bank_account_number`, `bank_routing_number`, `bank_account_name` |
| attendance | `period_days`, `eligible_days`, `paid_leave_days`, `lwp_days`, `unpaid_absence_days`, `holiday_days`, `worked_days`, `off_days`, `payable_days`, `proration_factor` |
| variable counts | `night_shift_count`, `ot_seconds_total`, `meal_count`, `eid_coverage_days` |
| money | `gross_earnings`, `total_additions`, `total_deductions`, `taxable_gross`, `tax_deducted`, `loan_recovered`, `net_pay`, `employer_cost` |
| control | `status` (`INCLUDED`/`EXCLUDED`/`HELD`/`ERROR`), `exclusion_reason`, `calc_warnings` JSON, `inputs_json` |
| payslip | `payslip_number` unique, `payslip_file`, `payslip_generated_at`, `payslip_emailed_at`, `payslip_error` |

`PayrollRunLine` — payslip line items. `component` FK + `component_code`/`component_name`/`component_kind` snapshots, `sequence`, `amount` (always a positive magnitude; `component_kind` decides add/subtract), `full_amount` (un-prorated, so the payslip can print "36,000 × 20/31"), `quantity`, `rate`, `unit`, `is_taxable`/`is_prorated` snapshots, `source_kind`, `source_ref` (e.g. `"OvertimeEntry:1234"`), explicit `loan` and `variable_entry` FKs for reversal, `calc_note`. **No unique constraint** — arrears legitimately produce a second BASIC line.

`PayrollVariance` — `run`, `employee`, `prev_net`, `curr_net`, `delta`, `delta_pct`, `flagged`, `explanation`.

`PayrollControlCheck` — one row per generated checklist item (`run`, `check_code`, `severity`, `status`, `employee` nullable, `message`, `detail_json`, `resolved_by`, `resolved_at`, `resolution_note`). Generated after populate/calculate and displayed as the pre-payroll and post-payroll checklist. Blocking checks prevent submit/paid transitions; warning checks require explanation.

`PayrollRunApproval` — one-for-one with `documents.RequestApproval`, including `UniqueConstraint(run, approver_group)`. **Locked 2026-08-11:** exactly two ordered `PayrollApprovalConfig`/`PayrollRunApproval` steps — HR (order 1, org-wide) then Finance (order 2, final) — no threshold escalation.

`PayrollRunDepartmentReview` — **new, locked 2026-08-11**. One row per `(run, department)`: `reviewed_by`, `reviewed_at`, `note`. This is the department manager's "Reviewed" sign-off, scoped to their own department's rows only — structurally distinct from `PayrollRunApproval`, which is an org-wide gate. It sits **before** the two `PayrollRunApproval` steps in the state machine (§7): department review happens first, then HR approves org-wide, then Finance approves. Whether a missing department review blocks HR's approval outright or only raises a `PayrollControlCheck` warning is open question 19 (preparer/approver, §12) territory and should be confirmed with HR/Finance.

`PayrollRunAudit` — append-only event log (`CREATED`/`POPULATED`/`INPUTS_LOCKED`/`CALCULATED`/`SUBMITTED`/`APPROVED`/`REJECTED`/`LOCKED`/`PAID`/`REVERSED`/…), `from_status`/`to_status`, `actor`, `detail_json`. Mirrors `roster.ShiftChangeLog`'s SET_NULL-everywhere posture so rows outlive their subject.

`PayrollExportAudit` — append-only export log (`run`, `export_type` REGISTER/BANK_ADVICE/GL_SUMMARY/PAYSLIP_ZIP/TAX_SUMMARY, `actor`, `created_at`, `file_name`, `row_count`, `total_amount`, `filters_json`, `checksum`). This is not for preventing downloads; it gives Finance/IT a permanent record of who exported sensitive payroll data.

### Disbursement & statutory

`BankAdvice` (`run`, `bank`, `advice_format`, `file`, `value_date`, `total_amount`, `record_count`, `status`) + `BankAdviceLine` (fully snapshotted — a later account edit must not retro-alter a sent file).

GL / cost-centre summary is **derived at export time** by aggregating `PayrollRunLine` grouped by `cost_center_code` × `component.gl_code`, not persisted.

**Template review 2026-08-13 — deferred by Finance (`PAYROLL_PROCESS.md` §12 Q29).** Finance's actual JV needs the single stored `GROSS_SALARY` figure split into Basic (50%), Housing (25%), Medical (5%), Conveyance (10%), Other Allowance (remainder), and a salary-tiered Mobile/Internet allowance — each against its own GL code (51001/51002/51007/52009). This is a pure export-time calculation (no new stored structure, so it doesn't reopen the "gross-only" decision) — but Finance confirmed it's **not needed for initial go-live**. The formula above is captured here for whenever it's picked back up; do not build it into `exports.py` before then.

`TDSEntry` — `employee`, `period`, `amount`, `note`, `batch` FK → `PayrollInputBatch`, unique per `(employee, period)` (re-upload upserts, same idiom as `OvertimeEntry`). **Originally the only Tax stage input (locked 2026-08-11); now a manual override the engine checks first** — see the calculated path below. Built in Phase 2/3; unchanged by Phase 4.

**Phase 4, confirmed 2026-08-24 from Finance's actual `Tax Calculation.xlsx` — in-house TDS calculation.** See the dated section above for the full formula walkthrough. Models:

`TaxYear` — `code` (e.g. `"2026-27"`), `start_date`/`end_date` (BD tax year runs 1 July – 30 June), `calculation_method` (`TextChoices`, one value today: `ITA_2023_SALARY` — exists so a future Finance Act that changes the formula's *shape*, not just its numbers, lands as a new `tax.py` branch keyed by this field, never an edit to a branch historical runs depend on — this is what Risk 11 already flagged), `exemption_divisor` (default `3` — **built 2026-08-24 as a divisor, not a stored fraction**: dividing by an exact `3` avoids the repeating-decimal precision loss of storing `0.3333...`; `annual_gross / exemption_divisor` is numerically identical to `annual_gross × 1/3`), `exemption_cap` (default `500,000`), `minimum_tax` (default `5,000` — configurable per `PAYROLL_PROCESS.md` §12 Q42, not hardcoded), `rebate_income_pct` (default `3%`), `rebate_investment_pct` (default `10%`), `rebate_cap` (default `750,000`), **`allowable_investment_pct`** (default `30%`, added 2026-10-07 — Finance's "Allowable Investment" line), **`declaration_deadline`** (nullable date, added 2026-10-07 — after it every unconfirmed declaration for the year locks on its defaults), `is_active`.

`TaxCategory` — `code` (`MEN`, `WOMEN_SENIOR`, `DISABLED_THIRD_GENDER`, `FREEDOM_FIGHTER`, `PARENTS_OF_DISABLED_CHILD`), `name`, `sequence`. Stable across years — the *threshold amount* per category is what varies year to year, hence it lives on a separate model rather than as a field here.

`TaxExemptionThreshold` — `tax_year` FK, `category` FK, `threshold_amount` (the 0%-band ceiling — 400,000/450,000/525,000/550,000/450,000 for FY2026-27). `UniqueConstraint(tax_year, category)`.

`TaxSlab` — `tax_year` FK, `sequence`, `width` (nullable — null means "remaining," the last bracket), `rate`. **Applies identically across every category** — only the 0%-band width (`TaxExemptionThreshold`, above) varies by category; the progressive brackets above it don't. FY2026-27 seed: `(1, 300000, 10%)`, `(2, 400000, 15%)`, `(3, 500000, 20%)`, `(4, 2000000, 25%)`, `(5, null, 30%)`.

`EmployeeTaxDeclaration` — `employee` FK, `tax_year` FK, `category` FK, **`allowable_investment`** (encrypted snapshot — `TaxYear.allowable_investment_pct` (30%) × projected taxable income from the salary in force when the row was issued; never recomputed), **`actual_investment`** (encrypted — the employee's once-a-year commitment, defaults to the allowable figure, may only be lowered), `source_tax` (default `0` — tax already deducted elsewhere, e.g. bank interest), **`ait_car_advance_tax`** (default `0` — Finance's "AIT (Car & Adv. Tax)" line, advance income tax already paid; renamed from `other_tds_adjustment` 2026-10-07) + **`ait_document`** (FileField, required when AIT > 0, random filename, streamed by a permission-checked view), `note`, and the confirmation stamp **`submitted_at` / `submitted_by` / `submission_source`** (`IN_ORBIS` | `OUTSIDE_ORBIS`). `UniqueConstraint(employee, tax_year)`. `is_locked` = confirmed, or the year's `TaxYear.declaration_deadline` has passed — no UI path changes a locked row (Django admin is the escape hatch). **Revised 2026-10-07 with Finance (reverses question 41's itemised declarations — see the same-dated section above):** every employee is assumed to invest the full allowable amount unless they lower it, once, before the row locks. **Question 40 reopened 2026-08-24:** `category` auto-defaults from `employees.PersonalInfo.gender` when left blank on save (Male → Men, Female → Women & Senior Citizen) — a default, not a lock, since HR can still override it, and must for the three categories gender alone can't determine (Disabled & 3rd gender, Freedom Fighter, Parents of a disabled child — none of which Orbis tracks yet) and for a senior male citizen (65+), who also qualifies for the Women & Senior Citizen threshold. See `payroll.models.EmployeeTaxDeclaration.clean()`.

`TaxComputation` — the calculated breakdown, **snapshotted per run** (same "snapshot everything" posture as `PayrollRunEmployee`/`Line` — deleted and rebuilt every recalculate, per §6 Stage 4's existing "delete all `PayrollRunLine` + `TaxComputation` for the run" line, which already anticipated this model before it was built). `run` FK, `employee` FK, `tax_year` FK, `category_name` snapshot, `gross_annual_projected`, `exempted_income`, `taxable_income`, `tax_before_rebate`, `rebate_amount`, `net_tax_payable` (post minimum-tax floor), `source_tax`, `other_adjustment`, `annual_tds`, `monthly_tds` (this is what feeds the `PayrollRunLine`), `breakdown_json` (full slab-by-slab detail, mirrors `offboarding.FinalSettlement.breakdown_json`'s transparency posture).

`tax.py::compute_tax_for_employee(employee, tax_year, gross_monthly, declaration) -> TaxBreakdown` — a pure dataclass-returning function, no DB writes; `engine.py`'s Tax stage is what persists a `TaxComputation` row from it. Order of operations, all confirmed against the hand-verified example:

```
mobile_internet_reimburse = tiered lookup on gross_monthly (0/1000/2000/3000/4000/5000/6000
                             at the ≤20k/35k/80k/110k/150k/200k/above breakpoints — non-taxable,
                             carved out before annualising, never paid as an actual line item)
annual_gross   = (gross_monthly - mobile_internet_reimburse) * 12
               + gross_monthly                    # 1 month's gross = 2 Eids × 50% each, locked 2026-08-11
exempted       = min(annual_gross / exemption_divisor, exemption_cap)
taxable_income = annual_gross - exempted
tax_before_rebate = Σ over TaxSlab rows, first band width = category's TaxExemptionThreshold (at 0%),
                    remaining bands from TaxSlab in sequence
allowable_investment = floor(taxable_income * allowable_investment_pct)   # 30%; snapshotted once a year on the declaration
declared_investment  = declaration.actual_investment (defaults to allowable_investment; 0 if no salary existed)
rebate         = min(taxable_income * rebate_income_pct,
                      declared_investment * rebate_investment_pct,
                      rebate_cap)
net_tax_payable   = max(tax_before_rebate - rebate, minimum_tax)
annual_tds        = net_tax_payable - source_tax - ait_car_advance_tax
# flat method (ITA_2023_SALARY, historical runs):
monthly_tds       = annual_tds / 12
# true-up method (ITA_2023_SALARY_TRUEUP, 2026-10-07 — annual_gross above also includes additional_pay_ytd):
tds_already_deducted = opening_tds_deducted + Σ earlier calculated periods' tax_deducted (this tax year)
tax_remaining        = annual_tds - tds_already_deducted
monthly_tds          = round(max(tax_remaining, 0) / months_remaining)      # July 12 … June 1; whole taka
```

~~**No YTD true-up** (question 2, resolved 2026-08-24).~~ **Reversed 2026-10-07** (see the dated section above): under the TRUEUP method the year's taxable extra pay to date enters `annual_gross` and the monthly deduction is the remaining tax over the months left. What stays: base salary is the *current* `gross_monthly` × 12, recomputed every run — a mid-year raise re-projects the whole year and the true-up spreads the correction.

**Engine integration (§6, Stage 7, superseding "reads `TDSEntry.amount` directly"):** for each employee, check `TDSEntry` for `(employee, period)` first — if present, use it unchanged (manual override wins, e.g. for corrections). Otherwise look up `EmployeeTaxDeclaration` for `(employee, current TaxYear)`; if missing (specifically, no `category` set), write a blocking `PayrollControlCheck` (`MISSING_TAX_DECLARATION`, replacing `MISSING_TDS`) and the line stays at Tk 0, same "stays in the register, doesn't vanish" posture as a missing `SalaryStructure`. Otherwise call `tax.py::compute_tax_for_employee()`, persist the `TaxComputation` row, and use `.monthly_tds` as the `PayrollRunLine` amount.

`FinalSettlement` — `case` O2O to `offboarding.OffboardingCase`, `settlement_run`, `clearance_step`, and the component amounts (notice pay, encashment, pending salary, bonus, loan/advance recovery, tax adjustment, `net_settlement`), `breakdown_json`, status + approval fields. **No gratuity line — locked 2026-08-11: SEBPO has no gratuity scheme and no provident fund.**

**Template review 2026-08-13 (`PAYROLL_PROCESS.md` §12 Q32, Q33).** (1) **Confirmed by Finance:** the "Service benefit" long-service award (15/7/1 days' pay at tenure milestones, GL code 52010) is a real scheme, paid at separation as part of final settlement, not monthly payroll — so it belongs as a `FinalSettlement` component amount, not a `PayComponent`/`PayrollRunLine`. Modelling is **deferred to a later phase**, same posture as the in-house TDS engine (§4) — don't build it before then. (2) **Confirmed 2026-08-13 (question 33): a one-time legacy leave-balance import is likely needed.** Both the encashment and final-settlement sheets split leave balances "up to 2024" vs. "from 2025" and reference a legacy "Oracle" balance; `leave.LeaveBalance` does not currently carry this pre-cutover history. Exact scope and source (an Oracle export vs. an old workbook) still need to be worked out with HR/Finance before cutover — treat as a one-time data-migration task, not a code path, and don't conflate it with the "no salary-history backfill" decision, which was about compensation, not leave.

---

## 5. Fields to add to existing models

| Model | Field | Rationale |
|---|---|---|
| ~~`roster.ShiftType`~~ | ~~`is_night_shift = BooleanField(default=False)`~~ | **Reverted 2026-08-20.** Shipped in Phase 2, sat unpopulated (`False` on every `ShiftType` including `NIG`) and unread. Night-shift detection is derived instead from `Shift.shift_attribute`/`ShiftType`'s own `start_time`/`end_time` (crosses midnight = night shift) — see the Phase 3 build note above. No schema addition needed. |
| `leave.EncashmentRequest` | `paid_in_run` FK → `PayrollRun` | Its docstring says *"the actual money movement happens in payroll, outside Orbis"* — that stops being true. An indexed FK prevents double payment and lets payroll flip `PAID` idempotently. Same rationale as `roster.Shift.leave_application`. |
| `offboarding.ClearanceStepTemplate` | `opens_final_settlement = BooleanField(default=False)` | Uses the existing `opens_exit_interview` / `opens_asset_return` / `triggers_letters` behaviour-flag pattern as designed. **Do not touch** the seeded `salary_adjustment_done` / `loan_balance_settled` / `advance_settled` / `gratuity_calculated` field definitions — payroll auto-ticks them in `data_json` when the settlement is PAID. **Locked 2026-08-11:** since SEBPO has no gratuity scheme, payroll ticks `gratuity_calculated` trivially true (nothing to calculate) rather than leaving it perpetually false. |
| `accounts.CustomUser` | `grade` FK, `employment_type` FK | Same class of fact as the existing `designation`/`department`/`team` FKs; needed on every list filter, dropdown scope and report. |
| `employees.JobInfo` | `cost_center` FK, `sub_project` FK | `JobInfo` is the documented home for HR-managed org facts and is already excluded from `SELF_EDITABLE_SECTIONS` — the right access posture for cost allocation. |
| `employees.JobInfo.ResourceStatus` | add `TRAINEE` | The SOP's Live/Bench/Trainee; extend existing choices rather than add a field. |
| `accounts` (new `_Named` tables) | `Grade` (+`rank`, like `AccessLevel`), `EmploymentType`, `CostCenter` (+`code`, `gl_code`), `SubProject` | `accounts._Named` is the established home for org reference tables. Putting them in `payroll` would force `roster`/`employees` to import from `payroll` for plain org facts — an inverted dependency. |
| `employees/sections.py` | one `SectionSpec` for `EmployeeBankAccount` | The registry's docstring: *"Add a section = 1 entry here + 1 form + 1 partial."* |
| `core/settings.py` | `MEALS_API_BASE_URL`, `MEALS_API_TOKEN`, beat entries | Matches the existing `TIMELY_API_*` and `apply-monthly-accrual` patterns. |
| `payroll.PayRate` | `resource_status` (blank=wildcard), `min_access_level` FK → `accounts.AccessLevel` | **New 2026-09-07** — see the dated section above and the `PayRate` description in this section. Lets a department carve out a resource-status exception (Trainee) or an access-level-floor band (TL and above) purely as data. |
| `payroll.PayRate.RateType` | rename `NIGHT_SPECIAL` → `NIGHT_CUSTOM` | **New 2026-09-07.** Clarifies the type's actual meaning (any `CUST`-shift employee, not covered by a Dedicated assignment) now that it's disambiguated from `NIGHT_QUARTERLY`. Plain rename, no data migration — zero `PayRate` rows exist in dev today. |
| new: `employees.DedicatedNightShiftAssignment` | whole new model | **New 2026-09-07** — see the dedicated subsection above. Not an addition to an existing model; listed here because it's part of the same field-change batch. |
| `employees/sections.py` | one `SectionSpec` for `DedicatedNightShiftAssignment` | Same registry pattern as `EmployeeBankAccount` above. |
| `accounts/permissions.py` | `DEDICATED_NIGHT_APPROVE_MIN_ACCESS_LEVEL_NAME = "Manager"`, `can_request_dedicated_night_shift()`, `can_approve_dedicated_night_shift()` | **New 2026-09-07.** The add-gate reuses the existing `SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME = "Supervisor"` constant rather than adding a second one for the same tier. |
| `accounts/permissions.py`, `employees/views.py::DedicatedNightShiftListView` | `has_dedicated_night_shift_approval_scope()` — gap fix, 2026-09-11 | `DedicatedNightShiftListView.show_pending_tab` was wired to `has_managed_scope` (the lower, TL-tier bar shared with leave/attendance), not the `Manager`-tier bar `can_approve_dedicated_night_shift` actually requires — so a TL/APM-tier manager could see the "Pending" tab and its count populated with assignments they had no button to act on. Found while building the dashboard's unified pending-approvals widget (which needed a per-user "is this type even relevant to me" check with no specific assignment in hand). Fixed by adding `has_dedicated_night_shift_approval_scope()` (Manager-tier + non-empty `managed_scope_pks`, `can_edit_all` bypass) and swapping it in on both the real list view and the new widget, so the two agree. |

**Deliberately not added:** TIN (already `employees.IdentificationNumber.IdType.TIN` — read and snapshot it; duplicating a government identifier creates two sources of truth); bank details on `CustomUser` or in `employees` (they belong in `payroll` for access gating); any scalar gross/net on `CustomUser` (compensation is effective-dated by definition, so a scalar has no history and is wrong the day after an increment); any payroll-side copy of night-shift, leave-type or holiday data.

---

## 6. Calculation engine

`engine.py::calculate_run(run_pk, actor_pk)` runs on Celery via `core.tasks.defer`:

1. **Lease** — `select_for_update(nowait=True)`, assert status, flip to `CALCULATING`, commit. A second concurrent Calculate fails fast.
2. **Config snapshot** — every `PayRate` (incl. per-department OT basis) and `MealDeductionTier` valid in the window, every `Holiday` in the window, the chosen `SalaryStructure` per employee, the uploaded `TDSEntry` per employee; serialise to `run.config_snapshot`; stamp `ENGINE_VERSION`. (Resolving an active `TaxYear`/slab set only applies once the in-house TDS engine, §4, is built.)
3. **Prefetch once** — one query per input source, bucketed into dicts by employee pk. A 1,300-employee run should be ~25 queries, not 25,000 — mirroring `roster.upload.parse_upload`'s "~6 queries regardless of file size" discipline.
4. **Rebuild** — in one `transaction.atomic()`: delete all `PayrollRunLine` + `TaxComputation` for the run, recompute, `bulk_create`, roll up totals, flip to `CALCULATED`, write `PayrollRunAudit`.

**Population.** Reuse the predicate already proven in `build_shift_summary`:

```python
CustomUser.objects.filter(
    Q(joining_date__isnull=True) | Q(joining_date__lte=period.end_date),
    Q(last_working_date__isnull=True) | Q(last_working_date__gte=period.start_date),
)
```

Deliberately **not** `is_active=True` — separated employees are deactivated by the nightly `deactivate_past_lwd` job but must still be paid for their final period.

**Payable days.**

```
period_days   = FIXED_30 ? 30 : period.days_in_period
eligible_from = max(period.start_date, joining_date)
eligible_to   = min(period.end_date,   last_working_date)
eligible_days = max(0, (eligible_to - eligible_from).days + 1)

lwp_days      = Σ roster.Shift in window joined ShiftType → LeavePolicy where is_paid is False
                (half-day types count 0.5)

payable_days     = max(0, eligible_days - lwp_days - unpaid_absence_days)
proration_factor = payable_days / period_days      # 6 dp
```

Replaces HR SOP §4's "Added Days", "Minus Days" and manual leaver adjustments. **LWP is resolved through the `ShiftType → LeavePolicy.is_paid` FK, never by name matching** (Risk 2).

Proration applies **per line**, not to gross — prorating gross then splitting breaks the invariant `Σ lines == gross`:

```python
line.full_amount = structure_line.amount
line.amount = (full_amount * factor).quantize(Decimal("0.01"), ROUND_HALF_UP) \
              if component.is_prorated else full_amount
```

Edge cases each write a `calc_warnings` entry: joiner **and** LWP in one period (both subtract, floored at 0); LWD before period start (`EXCLUDED`, reason recorded, still in the register); full-period LWP (`payable_days=0`, zero gross, row retained). Zero-day employees never silently vanish.

**Stage order** — code, not data. `PayComponent.sequence` only orders lines *within* a stage for display.

| # | Stage | Produces |
|---|---|---|
| 0 | Population | the `PayrollRunEmployee` set |
| 1 | Snapshot | org / person / bank / structure columns |
| 2 | Attendance | `payable_days`, `proration_factor`, night count, `inputs_json` |
| 3 | Structure earnings | **one `GROSS_SALARY` line per salary segment** from `SalaryStructure.gross_monthly`, each prorated over its own days (locked 2026-08-11 — no per-component breakdown; segmented 2026-09-19, dated section above — a single structure still yields exactly one line) |
| 4 | Variable earnings | OT (per-department basis: `seconds/3600 × rate` or a flat per-day amount), night, special shift, Eid, bonus/incentive, encashment, arrears — **never prorated**, already quantity-based |
| 5 | **Gross** | `gross_earnings` = Σ EARNING; `taxable_gross` = Σ EARNING where `is_taxable` |
| 6 | Non-statutory deductions | disciplinary, meal deduction (tiered by gross, §4), other recoveries |
| 7 | **Tax** | TDS line read from `TDSEntry` for the period — **uploaded by Finance, not calculated** (locked 2026-08-11) |
| 8 | Loan recovery | **built 2026-10-08** — one `LOAN_REPAYMENT` line per active `SalaryLoan`, min(instalment, outstanding, available net); leaver takes the whole balance; see the dated section |
| 9 | Net + rounding | `net_pay` + a `NET_ROUNDING` line so the register foots exactly |
| 10 | Roll-up | line totals → employee totals → run totals |

Tax sits after non-statutory deductions because a disciplinary deduction reduces taxable pay while a loan repayment does not — this ordering still holds even though the TDS figure itself is now uploaded rather than computed. Loans go last because the never-negative cap needs the final TDS figure; when capped the shortfall is **not** silently dropped — a `calc_warnings` entry is written and `installments_paid` is not incremented. The rounding line exists because `Σ quantize(x) ≠ quantize(Σ x)`; making the drift explicit makes it auditable instead of a mystery paisa.

**Immutability — five layers.**

1. **Snapshot everything.** No result row joins live for display.
2. **Write guards.** `PayrollRunEmployee.save()` / `PayrollRunLine.save()` raise when the parent run is terminal, unless `_allow_locked_write` is set by `services.py`. A `CheckConstraint` cannot reference a parent's status, so this is a model-layer guard.
3. **Delete-and-rebuild, never patch.** Recalculating wipes and regenerates all lines in one atomic block.
4. **`config_snapshot` + `ENGINE_VERSION`** — with a reproducibility test that re-runs a locked run into a `SIMULATION` run and asserts byte-identical lines.
5. **Reversal, never editing.** `services.py::reverse_run(run, reason, actor)` creates a `REVERSAL` run whose lines negate the original, marks the original `REVERSED`, and posts `SalaryLoanTransaction(reason=REVERSAL)` rows — exactly how `LeaveBalanceTransaction.Reason.REVERSED` undoes a `USED`. Corrections then go into a `SUPPLEMENTARY` run. **This is the most important rule in the design.**

**Cross-year note.** The 16 Dec → 15 Jan period straddles calendar years. `build_shift_summary` raises `CrossYearSummaryError` on exactly that input — the engine must **not** reuse that path. `build_payroll_report` is safe (it only caps at 31 days).

**Midnight-crossing shifts, confirmed 2026-08-13 (`PAYROLL_PROCESS.md` §12 Q5).** A shift is attributed to the payroll period containing its **start date**, never its end date. A 22:00–06:00 shift starting 15 July belongs to the "16 Jun – 15 Jul" July period even though it ends on the 16th. `roster.Shift` is already keyed by its start date, so this needs no schema change — `periods.py::period_for_date()` should be called against the shift's start date wherever night-shift count, OT seconds or payable-days logic reads a shift that might cross midnight.

**Night duty, OT and transport — what's actually built vs. what's still needed, corrected 2026-09-07.** Reading `engine.py` and `inputs.py` directly (not the docstrings) found the real state is narrower than "wired but contributes nothing until real data exists" suggested: **OT and transport have zero line-generation code today** — `ot_seconds_total` is captured onto the snapshot row and never turned into a `PayrollRunLine`; there is no transport stage at all. Night duty only resolves `NIGHT_REGULAR` today, from `payroll_inputs.collect_night_shift_counts()`, which returns one lumped count per employee — it doesn't distinguish `NIG` from `CUST`, isn't date-granular, and doesn't know work location. All three need real Stage-4 work:

- **Night duty** — replace the lumped count with a per-employee, per-shift-day classification (still one query pass, keeping the "~25 queries not 25,000" discipline), resolved in this order per shift-day: (1) an `APPROVED` `DedicatedNightShiftAssignment` covers the date → `NIGHT_QUARTERLY`; (2) else the shift is `CUST`-category → `NIGHT_CUSTOM`; (3) else (plain night-crossing shift) → `NIGHT_REGULAR`. Trainee zeroing needs no branch here — it falls out of the `resource_status` scope on `PayRate` automatically.
- **Overtime** — resolve `PayRate` by `(department, min_access_level)`; if `basis=PER_HOUR`, `amount × seconds/3600`; if `PER_OCCURRENCE`, `amount × count of distinct work-dates with an approved OvertimeEntry`; if `GROSS_30TH_PER_DAY` (added 2026-09-09), `(structure.gross_monthly / 30 × amount) × that same day count`, with the arithmetic written into `PayrollRunLine.calc_note`.
- **Transport allowance** — new stage: for each `CUST` shift-day where the resolved work location isn't WFH (via the `_LocatedRosterCell` chain already used for Eid coverage), add `TRANSPORT_SPECIAL_SHIFT`. Applies to Trainees too — only night pay excludes them.

**This same per-shift-day classification must be shared with `/payroll/report/` (`build_payroll_report`, §12 Risk 2), not reimplemented.** That report needs the identical Regular/Custom/Dedicated split plus a transport-eligible-day count (requested 2026-09-07 — see the dated section above). Extract one classification helper (e.g. `payroll/inputs.py::classify_night_shifts()`) that both the engine's Stage 4 and `reports.py::build_payroll_report` call, so the report a human eyeballs before running payroll can never silently drift from what the engine actually pays. The report's new Overtime column pulls from the same `OvertimeEntry` data as the engine's OT stage, shown as hours (not Tk) to stay consistent with the report's existing day-count character — it's a manual cross-check tool, not a payout preview.

---

## 7. Run state machine

```
DRAFT → INPUTS_OPEN → INPUTS_LOCKED → CALCULATING → CALCULATED → PENDING_APPROVAL
            ▲                                            │              │
            └──────────── reopen ────────────────────────┘     (dept review, then HR, then Finance)
                                                                        │
   REJECTED ──ack──► CALCULATED                                     APPROVED
                                                                        │
                                                REVERSED ◄── PAID ◄── LOCKED
```

| Transition | Who |
|---|---|
| create / open / lock inputs / calculate / recalculate / reopen / submit | `Payroll Admins` |
| attach OT batch | supervisors, **scoped by `accounts.permissions.visible_employees`** |
| enter bonus / disciplinary | HR, dept head, `Department.additional_managers` |
| upload TDS batch | Finance |
| review (own department) | department approving manager — writes `PayrollRunDepartmentReview` |
| approve org-wide | HR authorised users — `PayrollRunApproval` order 1 |
| approve final | Finance — `PayrollRunApproval` order 2 |
| confirm disbursement | `Finance` |
| reverse | superuser + `Payroll Admins` |

**Locked 2026-08-11 — the approval chain is a fixed 3 stages**, not the originally-generic N-step config: Department Review (own department, via `PayrollRunDepartmentReview`) → HR Approval (org-wide, `PayrollApprovalConfig` order 1) → Finance Approval (final, order 2). No threshold escalation (`min_net_total`) is used. **Resolved 2026-08-13 (question 19): `block_preparer_approval` stays off.** There is no separate "approver" role in Orbis's payroll chain distinct from finalisation — Finance's approval is the run's final finalisation step regardless of who entered any upstream input, so no preparer/approver segregation is enforced. The field remains on `PayrollApprovalConfig` (`services.py` already supports checking `PayrollRunAudit`, `PayrollInputBatch`, salary-structure approval fields and bank-account verification fields before accepting an approval from the same user) purely so it can be turned on later without a model change, not because it's expected to be used at go-live. Management's disbursement authorisation is a signature on the printed bank advice, outside Orbis — **confirmed 2026-08-13 (question 23): Orbis stores a scanned copy**, via `PayrollRun.disbursement_signoff_file` above.

**Cutoff enforcement.** `services.py::lock_inputs(run, actor)` refuses to lock while blocking `PayrollControlCheck` rows exist, while a required input batch is `FAILED`, or while late period inputs exist in `DRAFT` / `VALIDATED`. Reopening inputs creates a `PayrollRunAudit(REOPENED)` row and invalidates prior control checks. **Locked 2026-08-11 — narrower exception:** bank-detail edits, loan changes and backdated adjustments are allowed to be applied directly to the current run at any point before it reaches `LOCKED` (including during `PENDING_APPROVAL`/partially-approved states), triggering a recalculation that resets the approval chain back to `CALCULATED`. This is deliberately narrower than reopening all inputs — OT, bonus and disciplinary entries are not included in this exception. Once a run reaches `LOCKED` (after Finance's final approval), the general immutability rule still applies in full: any further correction requires a `SUPPLEMENTARY`, `ARREAR`, `FINAL_SETTLEMENT` or `REVERSAL` run.

`signals.py::update_run_status_from_approvals` is a near-verbatim port of `documents/signals.py::update_request_status_from_approvals` — same `PARTIALLY_APPROVED` intermediate, same `append_rejection(stage, step_order, reason, by)`, same `transaction.on_commit(lambda: defer(_post_approval, run.pk))`. `_post_approval` locks the run, posts loan transactions, flips `EncashmentRequest → PAID`, chunk-defers payslip generation, then notifies — idempotent by design, like its documents counterpart. `services.py::refresh_run_status()` updates via `.filter(pk=…).update(…)` to avoid re-firing `post_save`, the `offboarding.services.refresh_case_status` trick.

**Individual holds.** `PayrollRunEmployee.status = HELD` lets an approver exclude one person without rejecting the whole run (HR SOP §9 "Salary Hold") — borrowed from `offboarding.ClearanceStep.Status.SKIPPED`, which has no analogue in documents. Held employees drop out of totals and the bank advice and roll into the next `SUPPLEMENTARY` run.

---

## 8. Payslips, uploads, integrations

**Payslips** reuse the `documents` pipeline wholesale rather than building a second one: `TemplateDesign` + `MessageTemplate` (CKEditor, bleach-sanitised, versioned via `TemplateRevision`), the two-pass render in `documents/pdf.py`, WeasyPrint, `CompanyBranding.load()`, base64 data-URI image embedding. Add a `PAYSLIP_CONTEXT_SCHEMA` alongside `PDF_CONTEXT_SCHEMA` so template variables are validated at save time. One payslip bound one-to-one to one `PayrollRunEmployee` makes Finance SOP §15's *"no employee receives another's payslip"* structural rather than procedural. The PDF itself is generated and stored regardless, for in-app viewing (§10 "payslip history in Orbis") — but **confirmed 2026-08-13 (`PAYROLL_PROCESS.md` §12 Q13): the delivery email carries neither the PDF attachment nor a link**, only the numeric summary rendered as plain text in the email body via a second, simpler `MessageTemplate`. Email via `core.notifications.build_user_email` (renders without sending, so one SMTP connection serves the fan-out) + `core.tasks.defer` inside `transaction.on_commit`.

**OT upload** — `payroll/upload.py` structurally copies `roster/upload.py`: `ParsedRow` / `RowError(line_no, col_letter, raw, message)` / `ApplyReport` dataclasses; read-only `parse_upload()` → dry-run report → `apply_upload()` in one `transaction.atomic()` only when there are zero parse errors; up-front lookup caches; `bulk_create(update_conflicts=True)` behind the `connection.features.supports_update_conflicts_with_target` MySQL guard; scope enforced via `accounts.permissions.visible_employees`. **`hh:mm:ss` parsing must accept `str`, `datetime.time` *and* `timedelta`** — openpyxl returns different types depending on cell formatting. The same importer shape is reused for `VariablePayEntry`.

**meals.sebpo.net** — `payroll/meals.py` keeps `attendance/timely.py`'s caching / latch / normalisation structure but **must not copy its "never raises, return `{}`" contract**. For a dashboard overlay `{}` is fine; for payroll it silently means *everyone gets zero*. The client raises a typed `MealsUnavailable`, `pull_meal_counts` sets `PayrollInputBatch.status=FAILED`, and that **blocks** the `INPUTS_LOCKED` transition. Requires an aggregate `?start=&end=` endpoint keyed by `employee_id`; the contract must be agreed before Phase 7.

**2026-09-09 — `payroll/meals.py` client built early, report-column only, not Phase 7.** The confirmed live endpoint (`GET https://meals.sebpo.net/api/v1/external/meal-counts/`, `Authorization: Token`, `?start_date=&end_date=`, response shape matches the contract above) is now wired into the **payroll report** (`/payroll/report/`) as a live "Meal" column, appended after the dynamically generated OT columns — a pre-payroll cross-check like the existing AL/CL/night/OT columns, same as this file's Risk 2 note on `build_payroll_report`. It reuses the `MealsUnavailable`-raises contract decided above (never a silent `{}` masquerading as zero meals), but for an ambiguous payroll control point the default is warn-not-block, so the report **soft-warns** rather than hard-failing: `build_payroll_report` catches `MealsUnavailable`, renders the column as 0 for everyone, and the page shows a non-blocking amber banner.

**2026-09-09, later same day — the `MealCount` persistence + engine slice of Phase 7 pulled forward.** A user ran a real payroll (Aug 16 – Sep 15, 2026) and found `Deductions: 0.00` on every row despite the report showing live meal counts for the same window — `payroll/engine.py` had `meal_count = 0` hardcoded (the Phase 7 stub noted above). Rather than patch the engine to call the live API directly (which would make a locked run's meal deduction non-reproducible on Recalculate — `run.config_snapshot` is meant to freeze every input), built the contract this section originally specified: new `MealCount(_Auditable)` model (`employee`, `period`, `total_meals`, `batch` FK, unique per employee+period); `payroll.meals.pull_meal_counts(period, actor)` fetches and persists it under a `PayrollInputBatch(input_kind=MEAL_COUNT, source=API)` envelope, leaving a `FAILED` batch and raising `ValidationError` on `MealsUnavailable`; `payroll.services.lock_inputs` calls it and lets that error block the `INPUTS_LOCKED` transition — the one payroll input source that hard-blocks rather than soft-warns, unlike the report's use of the same client above. `payroll.inputs.collect_meal_counts` reads the persisted rows for the engine (`payroll/engine.py`'s meal-deduction stage), never the live API. Migration `payroll/migrations/0012_...py` includes the usual MySQL collation-conversion `RunPython` for the new `payroll_mealcount` table (see the collation note on `0001_initial.py`). **`EidCoverageWindow(+Entry)` is still unbuilt** — out of scope for this slice, not needed for the meal-deduction fix.

**TimeLy** — no change. Attendance stays a live dashboard overlay; payroll reads roster shifts, not punches.

**Export controls** — every export view calls `payroll.permissions.can_export_payroll(user, export_type, run)` before streaming a file and records `PayrollExportAudit` after generation. Bank advice export is only available for locked/approved runs and must assert `sum(BankAdviceLine.amount) == PayrollRun.net_payable_total_for_included_rows` before returning the file. Payslip ZIP export is finance/admin-only; ordinary employees access only their own payslip file through a scoped detail view.

---

## 9. Phased delivery

| Phase | Ships | Value on its own |
|---|---|---|
| **0** | `docs/PAYROLL_PROCESS.md`, `docs/PAYROLL_PLAN.md`, `docs/payroll_process.html` | **Sign-off gate for Phase 3+.** Phase 1/2 started 2026-08-20 ahead of sign-off — see status line above. |
| **1** ✅ *(models shipped 2026-08-20)* | `PayComponent`, `PayrollSettings`, `PayslipCounter`, `PayrollPeriod`, `SalaryStructure` (gross-only; no `SalaryStructureLine`), `Bank`/`BankBranch`, `EmployeeBankAccount`; `accounts.Grade`/`EmploymentType`/`CostCenter`/`SubProject`; `CustomUser`/`JobInfo` FKs — all built, migrated, admin-registered, tested. `EmployeeBankAccount` renders in the profile drawer via `employees.SectionSpec`. **Not yet built:** the bulk salary-structure importer (only single-record admin entry exists so far) | Compensation master leaves Excel; `documents` salary certificates read real numbers |
| **2** ✅ *(models + OT importer shipped 2026-08-20; rate data seeded 2026-08-20)* | `OvertimeType`, `PayRate` (+ `rates.py::resolve_rate()`), `MealDeductionTier`, `OvertimeEntry`, `VariablePayEntry`, `TDSEntry`, `PayrollInputBatch` — all built, migrated, admin-registered, tested. `payroll/upload.py` + `upload_views.py` (dry-run/apply, HTMX) built for **overtime only**, at `/payroll/overtime/upload/`. `seed_payroll_components` now loads the confirmed 2026-08-20 `PayComponent`/`OvertimeType`/`PayRate`/`MealDeductionTier`/`Bank` figures (§12 Q37-39) — real for Software Development's night-duty band; AdOps's rows were seeded too but weren't exercised at the time (the Phase 3 build note's "Digital Advertising isn't a real `accounts.Department`" is now stale — see the 2026-09-07 dated section above: it exists today, 892 employees, and the seeded AdOps figures themselves need correcting per that section — OT is per-hour not `FLAT_PER_OCCURRENCE`, and the night/transport split needs the `resource_status`/`min_access_level` scope fields and the `DedicatedNightShiftAssignment` model before re-seeding). `ShiftType.is_night_shift` **shipped then reverted** — see the Phase 3 build note above; it's not part of this phase's deliverable anymore. **Not yet built:** the TDS and variable-pay (bonus/incentive/disciplinary) importers — same shape as OT, not yet instantiated. **Not yet configured:** `Payroll`'s `dynamic_menu.ProtectedView` group grants (deny-by-default, matching `payroll_report`'s existing unconfigured state — needs an explicit admin decision on who can upload/view) | **Department spreadsheets-by-email stop** — months before the calculator exists. TDS upload ships here too, since it's the same importer shape as OT. |
| **3** ✅ *(shipped 2026-08-20, register-only)* | `PayrollRun`, `PayrollRunEmployee`, `PayrollRunLine`, `PayrollRunAudit`, `PayrollControlCheck`, `PayrollVariance`, `inputs.py`/`engine.py`/`exports.py` (`periods.py`/`rates.py` already existed from Phase 1/2) — all built, migrated, admin-registered, tested (17 engine tests + a real end-to-end July 2026 run). A minimal register UI ships too (`/payroll/runs/`, list + detail + register export), gated the same deny-by-default way as `payroll_report`. **State machine subset only:** DRAFT → INPUTS_OPEN → INPUTS_LOCKED → CALCULATING → CALCULATED. **Not built:** the approval chain, lock/paid/reversal — those need `PayrollRunApproval`/`PayrollRunDepartmentReview` (Phase 5, still gated on sign-off). Loan recovery is wired structurally but no-ops (no `SalaryLoan` yet — Phase 6) | Parallel-run against the manual process and diff — **the trust-building milestone**. Net pay is complete from this phase since TDS is read from the Phase 2 upload. |
| **4** ✅ *(built 2026-08-24)* | In-house TDS calculation engine — `TaxYear`, `TaxCategory`, `TaxExemptionThreshold`, `TaxSlab`, `EmployeeTaxDeclaration`, `TaxComputation`, `tax.py` — built from Finance's actual `Tax Calculation.xlsx`. `TDSEntry` upload/manual-entry stays as an override, checked first | TDS calculated automatically every period from salary + declared tax category + investments; Finance no longer maintains a monthly upload |
| **5** | `PayrollApprovalConfig`, `PayrollRunApproval`, `PayrollRunDepartmentReview`, `PayrollExportAudit`, `signals.py`, lock/reverse, `payslip.py`, `BankAdvice(+Line)`, `bank.py` (seeded with Bank Asia / EBL / Modhumoti), `CashChequeDisbursement(+Line)`, MFS account type (bKash/Nagad), `PayrollRun.disbursement_signoff_file` upload — scope confirmed 2026-08-13, see §4/§12 | Finance's workbook retired; every payment method Finance actually uses is covered, not just bank transfer |
| **6** | `SalaryLoan(+Transaction)`, `ArrearEntry`, `FinalSettlement` (no gratuity line; "Service benefit" long-service award confirmed real but deferred — see §4), `EncashmentRequest.paid_in_run`, `opens_final_settlement`, off-cycle runs (incl. the 2-Eid festival bonus, >180-day tenure gate) | Loans, settlement, retro, festival bonus |
| **7** ◐ *(meal deduction shipped 2026-09-09; `EidCoverageWindow` still unbuilt)* | `MealCount`, `EidCoverageWindow(+Entry)`. *(`payroll/meals.py` client shipped early, 2026-09-09, as a payroll-report column only; the `MealCount` persistence + `pull_meal_counts`/`INPUTS_LOCKED` gating + engine deduction landed later the same day — see the two dated sections in §8. `EidCoverageWindow` is the one piece of this phase still not built.)* | **Last on purpose** — lowest value, highest external dependency, and both have a manual fallback via `VariablePayEntry` from Phase 2. Do not let an API on another deployment schedule gate the engine. |

---

## 10. Files

**New:** `payroll/{models,periods,rates,inputs,engine,tax,upload,upload_views,meals,payslip,bank,exports,services,signals,permissions,forms,admin}.py`; `payroll/views.py` (rewrite), `payroll/urls.py` (extend); `payroll/management/commands/{seed_payroll_components,seed_payroll_periods,seed_meal_deduction_tiers,sync_meal_counts}.py` (`seed_tax_year` deferred with the in-house TDS engine, §4); `templates/payroll/**`.

**Modified:** `accounts/models.py`, `accounts/permissions.py`, `employees/models.py`, `employees/sections.py`, `roster/models.py`, `leave/models.py`, `offboarding/models.py`, `offboarding/services.py`, `core/settings.py`, `docs/PLAN.md` (payroll is no longer "Phase 4 — deferred").

**Reused as-is:** `core/pagination.py`, `core/forms.py::StyledMixin`, `core/exports.py::escape_spreadsheet`, `core/tasks.py::defer`, `core/notifications.py`, `accounts/filters.py`, `documents/pdf.py`, `documents/sanitize.py`.

Every new payroll view needs a `dynamic_menu.ProtectedView` row seeded — `dynamic_allowed_users` **denies by default** when none exists. `SubMenu` rows hang off the existing `Payroll` main menu. Phase 1 creates the `Payroll` and `Payroll Admin` groups. List pages follow the `CLAUDE.md` table convention (`.page-fill` → `card p-0 table-scroll` → `table-simple table-sticky` → `_pagination.html`).

---

## 11. Verification

1. **Unit** — `hh:mm:ss` parsing across `str`/`time`/`timedelta`; payable days for mid-period joiner, leaver, both, and both-plus-LWP; specificity-based rate resolution incl. per-department OT basis; meal-tier boundary resolution; loan instalment capped at outstanding; the 16 Dec → 15 Jan cross-year period.
2. **Snapshot integrity** — calculate, transfer the employee to another department, re-render the payslip, assert the original department still shows.
3. **Idempotence** — calculate twice, assert identical lines and no duplicates.
4. **Immutability** — assert editing a `LOCKED` run raises; assert `reverse_run` produces exactly negating lines and unwinds loan transactions.
5. **Footing** — assert `Σ lines == net_pay` to the paisa across a 1,000-employee fixture.
6. **Reconciliation (the real test)** — load one historical month from Finance's workbook, run it through the engine, diff every employee's net pay. Target zero unexplained variance, over **two consecutive months**, before cutover.
7. **End to end** — seed components → enter a salary → upload an OT sheet (dry-run shows errors, then applies) → create run → lock → calculate → check register + variance → approve → bank advice → payslip → verify the employee can download theirs and nobody else can.
8. **Scope** — a supervisor cannot upload OT outside their downline; a non-payroll user gets 403 on every payroll URL.
9. **Control checks** — missing salary, missing bank, excessive OT, zero/negative net and variance thresholds produce `PayrollControlCheck` rows; blocking checks prevent submit/paid transitions until resolved or held.
10. **Cutoff** — late inputs after `INPUTS_LOCKED` cannot mutate the regular run; reopening writes an audit event; after `LOCKED`, corrections require a supplementary/reversal/final-settlement run.
11. **Export audit** — register, bank advice, GL and payslip ZIP downloads create `PayrollExportAudit`; unauthorised users cannot download sensitive exports; bank advice cannot export unless totals reconcile.

---

## 12. Risks

1. ~~**Period boundary vs. calendar-year leave.**~~ **Resolved 2026-08-13 (`PAYROLL_PROCESS.md` §12 Q17) — not actually a risk.** January's payable-days calculation only consumes LWP-flagged shift-days pulled directly from the roster (already resolved via `ShiftType → LeavePolicy.is_paid`, per Risk 2 below); it never touches annual leave balance, accrual or carry-forward. So the **16 Dec – 15 Jan** period's calendar-year boundary has no effect on payroll math, and the engine correctly does not reuse `build_shift_summary`'s cross-year-raising path (§6 "Cross-year note").
2. **`payroll/reports.py::_bucket_for_shift` is a latent overpayment bug.** It resolves LWP by fuzzy-matching `ShiftType.name` against `LEAVE_BUCKET_ALIASES`. Rename a shift type and LWP silently stops being counted — everyone is overpaid, with no error. The engine must join `ShiftType → LeavePolicy.is_paid`. Deprecate the alias map once the engine ships and add a test asserting every LEAVE `ShiftType` has a `LeavePolicy`. **`WORK_BUCKET_ALIASES`'s `NIG`/`CUST` buckets have the same fragility** and, as of 2026-09-07, are also simply the wrong shape — they need to become the Regular/Custom/Dedicated split described in §6's night-duty stage, sharing one classification function with the engine rather than growing a second copy of the same rule (see the dated section above).
3. ~~**`ABS` (Abscent) has no `LeavePolicy`.**~~ **Resolved 2026-08-13 (`PAYROLL_PROCESS.md` §12 Q16) — confirmed intentional, not a bug.** `seed_leave_policies.py` seeds AL, CL, SL, MTL, CPL, LWP, HDL only — LWP and HDL are the sole `is_paid=False` rows. `ABS` is a LEAVE `ShiftType` with no policy, so the payable-days formula treats unauthorised absence as fully paid — **HR/Finance confirmed this is the intended policy.** No `ABS` `LeavePolicy` row is needed; the engine should treat a policy-less leave shift as paid by design, not fail loudly.
4. **MySQL does not enforce partial unique indexes.** Every partial-unique here (one REGULAR run per period, one active structure per employee+date, one primary bank account) needs a `clean()` mirror plus `select_for_update` in the service — the posture `offboarding.Resignation.clean()` documents.
5. **Concurrent calculate.** Mitigated by `select_for_update(nowait=True)` + `CALCULATING` as a lease, plus a stale-lease timeout for a worker that dies mid-run.
6. **WeasyPrint at scale.** `CELERY_TASK_TIME_LIMIT = 300` (soft 240). ~1,300 payslips at 0.3–1 s each is 6–20 minutes — a monolithic task **will** be killed. Chunk at ~50/task. 1,300 PDFs/month × 12 on single-VPS `FileSystemStorage` also needs a retention/archive plan.
7. **Salary visibility ≠ profile visibility.** `accounts.permissions.can_see_all` gives HR organisation-wide profile access; that must not auto-grant salary access. Separately, `Department.additional_managers` is documented as existing partly *"for payroll review"* — whether that grant extends to money needs an explicit decision, or every department manager quietly gains access to every salary in their department.
8. **Export leakage.** Register, bank advice and payslip ZIP files are more sensitive than ordinary HR reports. Permission checks plus `PayrollExportAudit` reduce risk but do not replace operational discipline around email, local downloads and shared folders.
9. **Preparer/approver conflicts.** If the approver pool is too small, strict segregation of duties may block emergency payroll. Keep `block_preparer_approval` configurable and expose conflicts before submission.
10. **No effective-dating on department/team/grade.** A mid-period transfer loses history; the snapshot captures whatever is current at calculation time. Acceptable now, wrong for cost-centre allocation later. `PayrollRunAudit` records the snapshot; an effective-dated `DepartmentAssignment` is the eventual fix.
11. **Finance Acts change the *shape* of BD tax rules, not just the numbers** — the 2023 Act replaced per-component exemptions with a flat one-third-or-cap exemption rule. **Correction 2026-08-24:** this doc previously said the cap was 450k — Finance's actual template confirms it's **500,000 Tk (5 lac)**, now fixed throughout §4. **No longer dormant — the in-house engine is built** (§4, Phase 4). `TaxYear.calculation_method` exists exactly so a future Finance Act that changes the formula's shape lands as a new `tax.py` branch keyed by that value, never an edit to a branch historical runs depend on.
12. **Dead code.** `leave/views.py:894-951` holds a second, unrouted `PayrollReportView` with weaker scoping, plus `templates/leave/payroll_report.html` (a one-line extend of `payroll/report.html`). Delete both in Phase 3 rather than leaving a divergent copy.
13. ~~**"Custom Shift" long tail.**~~ **Resolved 2026-09-07 (`PAYROLL_PROCESS.md` §12 Q36/Q44).** 62 of AdOps's 895 employees (~7%) were flagged "Custom Shift" in July 2026's actual compilation — schedules HR currently handles outside the standard night/OT lookup. Confirmed: this is exactly `roster.ShiftType(name="CUST")` usage, an ordinary, already-fully-seeded roster shift type (`CUST 7-4`/`CUST 8-5` `ShiftAttribute` rows). No manual override path is needed — `NIGHT_CUSTOM` + `TRANSPORT_SPECIAL_SHIFT` resolve it entirely through the standard `PayRate` mechanism (see the 2026-09-07 dated section above).

## 13. Open questions

Tracked in [`PAYROLL_PROCESS.md` §12](PAYROLL_PROCESS.md). Most were locked at the 2026-08-11 HR/Finance meeting; the 2026-08-13 review of HR's and Finance's actual templates reopened a few, and Finance/HR answered every reopened or newly-identified item across two same-day follow-ups. The 2026-08-20 review of July 2026's actual filled-in master compilation confirmed those template-review figures with real production numbers (questions 37–39) and surfaced three further questions. The 2026-08-24 review of Finance's actual tax computation template superseded questions 1–2 with a real in-house calculation and confirmed two more (questions 40–41). The 2026-09-07 AdOps night-shift/OT/transport-allowance policy review (dated section above) confirmed four more (questions 43–46), closing question 36 in the process. The 2026-09-19 Test Salary review (dated section above) answered question 34. The 2026-10-07 Finance meeting on tax declarations (dated section above) reversed question 41 and settled questions 58–60 — **three questions remain open (#22, #35, #42), the two tax-engine follow-ups raised that morning (incentives in the projection, year-to-date true-up) were confirmed and built the same evening as questions 61–66.**

**Resolved:** TDS methodology (**calculated in-system from salary + tax category + investment declaration, superseding the 2026-08-11 upload-only decision** — question 1), mid-year cutover (**reversed 2026-10-07 — year-to-date true-up with a one-time openings upload for FY2026-27**, questions 2/63/64), approval chain composition (Department Review → HR → Finance), OT basis-per-department, meal deduction (tiered by gross, deducted from employee), gratuity (none), PF/WPPF (none), festival bonus (2 Eids, 50% of gross, >180-day tenure gate at the Eid date), salary visibility (downline managers + HR/Finance see all), historical effective dates (not backfilled), cut-off exceptions (bank/loan/backdated may apply to the current run), payroll export access (Finance, every download logged), statutory outputs (deferred to phase 2+), bank advice format(s) (Bank Asia / EBL / Modhumoti, question 24), management sign-off record (Orbis stores a scanned copy, question 23), "Eid Package" basis (flat 3-day/24hr package, question 7), salary-banded night-shift/OT rates (Software + Creative Development only, question 26), the third OT basis (**reversed 2026-09-09** — it *is* active for Software/Creative Development; a `GROSS_30TH_PER_DAY` basis was added, questions 27/47/48), Eid coverage shape (flat package, not rate × days, question 28), disbursement modes (Cash/Cheque/MFS required, questions 30–31), the "Service benefit" long-service award (real, paid at separation, modelling deferred, question 32), OT approval gate (no gate, question 4), midnight-crossing night shifts (attributed by shift start date, question 5), payslip delivery (numbers in the email body, no attachment or link, question 13), ABS pay treatment (confirmed fully paid by design, question 16), the cross-year leave rule (moot — payroll only reads LWP shift-days, not annual balances, question 17), preparer/approver separation (no blocking — Finance's approval is the only finalisation gate, question 19), the legacy Oracle leave-balance cutover (a one-time import is likely needed, scope TBD, question 33), AdOps's exact flat OT figures (200/250 Tk, question 37), the three-way night-duty rate split (Regular/Quarterly/Special, question 38), the new "Special Shift Transport" component (400 Tk/day, question 39), tax category source (**reopened 2026-08-24** — auto-defaults from gender, still overridable by HR, question 40), investment declaration data source (**reversed 2026-10-07** — every employee is assumed to invest the full 30% allowable figure, confirms once a year, may only lower it; question 41 → 58–60), AdOps's OT basis (**narrows the 2026-08-11 department-level decision** — per-hour, not flat-per-day like Software/Creative, question 43), AdOps's department identity ("Digital Advertising" already exists in Orbis, 892 employees, question 44), the Dedicated Night Shift concept and its TL-requests/APM-approves gate (question 45), the three-way night-rate naming/eligibility split — `NIGHT_REGULAR`/`NIGHT_CUSTOM` (renamed from `NIGHT_SPECIAL`)/`NIGHT_QUARTERLY` (question 46, also closes question 36/Risk 13), and "Test Salary" (**it is a deduction, not a probationary pay category** — 200 Tk/day off Data Solutions' existing gross, capped at 6,000, question 34, day-count entry revised 2026-09-21).

**Deferred (confirmed real, but not needed for initial go-live):** the GL-split formula for the statutory salary breakdown (question 29); the "Service benefit" long-service award's implementation (question 32).

**Still open:** leave encashment rate — Gross ÷ 30 or Gross ÷ 26 (question 22); whether the dormant third AdOps OT column is a live rate tier (question 35); and whether the 5,000 Tk minimum-tax floor (Dhaka's rate) needs to be lower for any non-Dhaka employees (question 42).
