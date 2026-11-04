# Overtime review → approve chain

**Date:** 2026-10-17. **Status:** built the same day (see "What was built").
**Companion to** `PAYROLL_PROCESS.md` §12 (questions 73–77) and `PAYROLL_PLAN.md`
("Overtime gets a two-step review chain — Q4 fully reversed, Q56 superseded, 2026-10-17").

## 1. The ask

> "Currently when a TL or supervisor uploads a record it's auto approved / pending.
> I want to introduce another level like **Reviewed**, which should be done by an
> APM or PM. And **Approved** should be done by a department head or department
> additional managers."

Decisions taken with the user the same day:

| # | Decision |
|---|---|
| 1 | **Approvers** are the employee's department head, the department's additional managers, **and** HR-Payroll (HR ∧ Payroll) **and** Payroll Admins — the Q56 pair stays as the fallback. |
| 2 | **Auto-generated GH overtime** (the two `generate_gh_overtime*` beat jobs and the superuser page) enters the chain like everything else: Pending review, a manager reviews, the head approves. |
| 3 | **A reviewer's own upload still lands Pending review.** No auto-advance; a sign-off is always an explicit recorded act (the Q56 posture). The list has bulk "Review selected" for exactly this. |
| 4 | **Existing PENDING rows on prod** (post-lock entries waiting on HR-Payroll under Q55) are left alone — they simply need the new chain. No migration-time approvals. |
| 5 | **Department data** (heads / additional managers on every department that files overtime) is the user's to fix before the deploy; `manage.py check_overtime_approvers` says where the gaps are. |

## 2. What changed

**Before** (Q4 + Q55): a write into an *open* period landed `APPROVED` — the
supervisor's upload was final. A write into the most recently *locked* period
landed `PENDING`, and only HR-Payroll / Payroll Admins could promote it.

**Now:** **every** write lands `PENDING` ("Pending review"), whoever makes it
and whether or not the period is locked. Two recorded steps follow:

```
PENDING ──review──▶ REVIEWED ──approve──▶ APPROVED   (the only status the engine pays)
   │                   │
   └──────reject───────┴──────▶ VOID
```

- **Review** — `accounts.permissions.can_review_overtime`: Manager tier
  (`Designation.access_level` rank ≥ "Manager", or the per-user
  `CustomUser.access_level` override — the APM / PM / Deputy PM / Manager
  titles), the employee in the reviewer's managed scope
  (separated-inclusive), never your own row. No HR/admin bypass.
- **Approve** — `can_approve_overtime`: the employee's department head or a
  department additional manager, HR-Payroll, Payroll Admins (superuser via
  the latter). Never your own row; a head's own overtime falls to whoever
  the head reports to (the `can_act_on_variable_pay` rule).
- **An approver may approve straight from PENDING.** The review stamp is then
  the approver's. Without this, a department whose head is also the nearest
  manager would click twice, and a team with no Manager-tier person between
  the TL and the head would stall.
- **Reject** from either stage → `VOID` (same end state as a later void;
  `processed_by` says who). A reviewer may reject only a PENDING row; an
  approver may reject either.
- **Stamps:** `reviewed_by` / `reviewed_at` (new) and `processed_by` /
  `processed_at` (the Q55 decision stamp, kept rather than renamed).
- **A sign-off covers the figures that were signed.** Changing the date,
  type, team or hours of a REVIEWED/APPROVED row — in the drawer or by
  re-upload — calls `OvertimeEntry.reset_to_pending()`: back to PENDING,
  both stamps cleared. A re-upload with the *same* figures leaves the row
  (and its sign-off) untouched (`ApplyReport.unchanged`); different figures
  are reported as `resubmitted`. Note-only edits keep the stamps. Void also
  clears both stamps so Restore can't carry a stale sign-off along; Restore
  returns the row to PENDING.
- **The window** (`payroll/services.py::OvertimeWindow`) is unchanged as a
  *date* gate (floor, most-recently-locked rule, future/backdate limits).
  `WindowDecision.status` is now always PENDING. A locked period still
  refuses re-uploads over an APPROVED/VOID row (`skipped_locked`), still
  only HR-Payroll / Payroll Admins may edit/void/delete those
  (`can_manage_locked_overtime` — **no longer an approval right**).
- **The engine is untouched.** `collect_overtime` reads `status=APPROVED`.
  `PayrollRun.pending_overtime_count`, the run-list column, the
  `PENDING_OVERTIME` warning check and the stale-inputs banner all count
  `OvertimeEntry.UNAPPROVED_STATUSES = (PENDING, REVIEWED)`. The
  approve-then-recalculate loop for a locked period is exactly as Q55 left it.
- **The form** lost its `status` dropdown — nothing in scope can file as
  Approved any more; Void/Restore/Review/Approve are list actions.

## 3. Where it waits on you

`core/approvals.py` gained two bell sections, because the stages wait on
different people and one person may hold both:

- **Overtime to review** — `overtime_review_inbox_pks`: the nearest
  Manager-tier person up the employee's reporting chain (the same walk as
  dedicated night shifts, generalised into `_compute_nearest_tier_inbox`).
  A PM above the APM may still review, but isn't nagged while the APM exists.
- **Overtime to approve** — `overtime_approve_inbox_pks`: members of the
  departments the viewer heads or is an additional manager of. **HR-Payroll
  and Payroll Admins get no bell section on purpose** — they are the
  fallback, not the owner, and org-wide visibility isn't "waiting on you".

Both link to `/payroll/overtime/?awaiting=me`, an "Awaiting me" checkbox in
the filter bar that applies `overtime_awaiting_q` — the same sets the bell
counts (for a global approver: everything unapproved). Decide actions send
`HX-Trigger: orbis:approvals-changed` so the bell refreshes.

## 4. The list page

`/payroll/overtime/`: per-row Review (eye) / Approve (check) / Reject (x)
computed per row by `_decorate_overtime_rows` (`can_review`, `can_approve`,
`can_reject`; one cached scope query and one cached approver-department
query per request, no per-row queries except a head's own row). Bulk
"Review selected" / "Approve selected" / "Reject selected" re-check every
id server-side and skip what the viewer can't act on. Ticking the header
checkbox offers a Django-admin style **"Select all N matching entries"**
link (added 2026-10-17 after the first dev test — a head approving a month
is hundreds of rows over many pages): it posts `select_all=1`, and the bulk
view re-runs the list's own filters (`_filtered_overtime_queryset`, shared
with the page and the Excel export; the filters ride on the form's action
querystring) over every unapproved row across all pages, inside one
transaction, still re-checking each row's permission. Three badges:
Pending review (amber), Reviewed (blue, tooltip names the reviewer),
Approved (green, tooltip names approver and reviewer). The decide views
(`OvertimeEntryDecideView`, `OvertimeEntryBulkDecideView`) share the
module-level `_review_entry` / `_approve_entry` / `_reject_entry`, each of
which re-checks the per-row permission itself.

## 5. Rollout

1. Fix department data on prod: `head_of_department` and/or
   `additional_managers` on every department that files overtime, and map
   the three unmapped designations (CS Officer, Customer Service Executive,
   Office Assistant) to an AccessLevel.
2. `python manage.py check_overtime_approvers` — exits 1 while any
   overtime-filing department has nobody to approve. Advisory lines list
   teams with no Manager-tier reviewer above them (the head approves those
   straight from Pending) and unmapped designations.
3. Deploy; `migrate` adds `reviewed_by`/`reviewed_at` and the new choices.
   No data migration — rows keep their status; Approved stays Approved.
4. Tell supervisors: uploads now say "Pending review"; tell APMs/PMs and
   heads about the bell sections and the "Awaiting me" switch.

## 6. Not done / later

- No email on upload to the reviewer — the bell is v1, as for every other
  approval type (`docs/PLAN.md` "Notifications").
- No per-row decision reason on reject (the entry's `note` is the entry's,
  not the decision's). Add a `decision_note` if HR asks.
- No event ledger — stamps on the row are the audit trail, as for
  `VariablePayEntry`. A full re-submission history would need an
  `OvertimeEntryEvent` table in the `policies`/`NightShiftConsent` shape.
