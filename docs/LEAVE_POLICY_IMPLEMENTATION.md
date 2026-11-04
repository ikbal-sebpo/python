# Orbis — Leave Policy v2.0 Implementation Spec

This document is the canonical reference for SEBPO Leave Policy v2.0 enforcement in Orbis. The policy PDF (`SEBPO Leave Policy (V - 2.0).pdf`, signed off 2021-12-29, effective 2022-01-01) is the source of truth for the rules; this document is the source of truth for how those rules are realised in code.

> If a rule changes in code, update this doc in the same change. If the policy changes (HR releases v2.1+), update this doc to reflect the new rules and reconcile the schema.

## Why this exists

SEBPO's Leave Policy v2.0 defines eight leave types, accrual rules, carry-forward rules, eligibility gates, encashment rules, and — most critically for daily operations — a **sandwich-leave rule** that decides whether holidays/weekends "absorbed" by surrounding leave count against the employee's balance.

Before this work, Orbis had only the *frame* of a leave module: `LeavePolicy` and `LeaveApplication` models, `materialise_shifts_for_leave` writing LEAVE-category Shift rows on approval, and the hierarchy access layer (from the prior PR) routing approvals correctly. Every policy enforcement was missing — no balance ledger, no accrual job, no sandwich logic, no probation pro-rating, no eligibility checks. The only "balance check" was a date-overlap test.

This implementation closes that gap end-to-end.

## Section-by-section inventory of the policy

### §3 Attendance
| Provision | Status / Action |
|---|---|
| 8-hour shifts in three bands (A 7am–4pm / B 2pm–11pm / C 10pm–7am) | `ShiftAttribute` model already supports named time variants. Seed the three standard ShiftAttribute rows. |
| WFH prior approval + list to HR/Admin/IT | Out of scope for this work. |
| Habitual-late tracking | Out of scope; lives in the future attendance module. |

### §4 Government & Weekly Holidays
| Provision | Status |
|---|---|
| 2 weekly holiday days | Default roster pattern Sat/Sun = OFF; configurable per employee. ✅ Already correct. |
| Roster-duty employees per their roster | `Roster` weekly pattern per employee. ✅ Already correct. |
| Compensatory holiday within 3 working days when made to work on a holiday | Implemented as **CpL auto-grant**: signal on `Shift` save credits +1 CpL when a WORK-category Shift is saved on a Holiday-tagged date. The 3-working-day use-by deadline was enforced until 2026-10-01 and is now off (see §5.12). |

### §5.1 The eight leave types
**AL, CL, SL, MtL, FL, CpL, LWP, HdL.** All seeded as `ShiftType(category=LEAVE)` rows with matching `LeavePolicy` rows.

### §5.2 Approval
| Provision | Status |
|---|---|
| Department heads approve all types | Hierarchy permission helpers (`can_act_on_leave` in `accounts/permissions.py`) already route this. ✅ Done. |
| Department heads' own leave routes to COO per the policy text | **Operational override**: COO is unavailable for routine approvals. Dept-head leave is approvable by (a) their own `reports_to` chain ancestors and (b) HR / User Admins. No COO routing code. |

### §5.3 Carry-forward
- **AL**: ~~50% carry-forward. Remaining 50% encashed quarterly~~ **REVISED 2026-09-01** — see §5.7 below; the written v2.0 text is superseded by an explicit manager confirmation.
- **CpL**: 100% at end of year, never expires (revised 2026-10-01 — see §5.12).
- **Everything else**: zeroed at year-end.

### §5.4 Sandwich rule
> Weekly holidays or the government and company-approved holidays either prefixed or suffixed with any leave will not be considered as leave. But those holidays having the same suffixed or prefixed falling in-between the leave will make a sandwich of leaves.

**Translation**: A non-working day (weekend or holiday) is billed against the balance **iff** it falls strictly between two leave days. Edge non-working days are free. See [Sandwich algorithm](#sandwich-algorithm) below for the implementation.

### §5.5 Probation
All leaves except AL are pro-rated for new joiners and during probation. AL has its own 12-month service gate.

### §5.6 Unauthorized absence
Triggers salary deduction, recorded as LWP. Auto-creation of LWP for unmarked absences depends on the future attendance module — out of scope here.

### §5.7 Annual Leave (AL)

**REVISED 2026-09-01** — the rules below supersede the signed v2.0 PDF's §5.7 (50% carry-forward, quarterly encashment) and this doc's prior text. Reconciled against three sources: the user's own description, the signed policy PDF, and — after those two conflicted — a follow-up confirmation from the user's manager, which is the version implemented. Real practice had diverged from the written v2.0 document; this section records that reversal rather than silently diverging (see the project's own posture on this, e.g. the reverted `ShiftType.is_night_shift` field).

- **Eligibility**: ≥12 months service from joining date. Unchanged.
- **Cycle**: per-employee **anniversary year** (`joining_date` + N years), not calendar year — `LeavePolicy.cycle=ANNIVERSARY` now actually drives this (previously defined on the model but unused by any running code).
- **Accrual**: 1 day per 18 worked days (ratio 1:18) — unchanged, kept over an earlier "flat 12/year" description once directly confirmed. **Computed live, not monthly-batched, as of the same day** (a real employee review surfaced a wrong negative balance caused by the batch lag — see below). **Refined 2026-09-02 — see below**: "worked days" now also counts paid-leave days, and the result is capped at 12/cycle.
- **Carry-forward**: **all** unused AL carries forward every cycle — no 50% cut. The only ceiling is a **lifetime running cap of 60 days** (`LeaveBalance.carried_forward`, capped via `LeavePolicy.max_carry_forward_days`). Carried-forward AL **stays spendable** — it is not a separate non-spendable pool; an employee can draw on it for a real leave need (e.g. extended sick leave) same as freshly-accrued AL.
- **No ongoing encashment**: nothing is auto-cashed quarterly or annually (`LeavePolicy.encash_carry_forward_excess=False`) — unused beyond what carries (i.e. once the running total would exceed the 60-day cap) is simply forfeited.
- **60-day cap penalty**: once carried-forward AL reaches 60 days, the employee's AL entitlement is *frozen* for the rest of their service — a one-way door: they can still consume the existing balance, but earn no further AL even if the balance later drops below 60 through use. Implemented as `LeaveBalance.entitlement_frozen=True`.
- **Separation payout**: whatever AL balance remains (up to 60) is paid out at separation — currently a **manual HR step**, not automated (payroll settlement is Phase 5+, not built). ~~Historical long-tenured employees' pre-Orbis balance (tracked externally in an HR file) is seeded once via `leave/upload_bank.py` into `LeaveBalance.opening` on their current cycle.~~ **REVERSED 2026-10-05** (`docs/LEGACY_60_IMPORT_PLAN.md` decision 3): the pre-Orbis AL position is **not** credited to the ledger at all. It is stored as HR calculated it on `leave.LegacyAnnualLeaveRecord` (uploaded at `/leave/al-history/upload/`), shown only in the Service Book's Part 4, and the live ledger runs on 2026 roster data alone — so the balance an employee sees in Orbis excludes the pre-Orbis bank (up to 60 days); requests beyond the 12-day advance ceiling are a manual HR decision. `upload_bank.py` remains in the codebase but is not the path used for the legacy load. Employees with under 12 months' service at the as-of date (2025-12-31) get no record, per the eligibility gate.
- **Mechanics**: `leave/management/commands/close_al_cycles.py` runs nightly and rolls over whichever employees' cycles ended that day (anniversaries are scattered across the calendar, unlike CL/SL's shared Jan-1 close) — `apply_year_end_rollover.py` now excludes AL (`cycle=CALENDAR` filter) — since 2026-10-01 CpL (100% carry, no expiry) is its live customer, see §5.12. `leave/management/commands/reconcile_shift_leave_usage.py` runs nightly before it, so the carry-forward math reads accurate usage — see the risk note below. **A newly-eligible employee's *first* cycle row is also fully automatic** — `leave/management/commands/open_al_cycles.py` (renamed 2026-09-02 from the one-off `open_initial_al_cycles`) runs nightly too, right before `close_al_cycles`, as the `Open AL cycles` periodic task (a `django_celery_beat` row since 2026-09-29 — `docs/SCHEDULER_ADMIN_PLAN.md`; seeded from `scheduler/schedule.py::JOBS`). No page visit, application, or report needs to touch that employee first — the lazy `get_or_create_balance` call from those code paths still works, but nothing depends on it firing promptly anymore.

**Real-time, not batch — added 2026-09-01, same review that found the accrual gap below:**
- **Usage (`used`) debits instantly.** `leave/signals.py::sync_shift_leave_ledger` reacts synchronously the moment a `Shift` is saved as a LEAVE-category type with no `LeaveApplication` — same pattern this file's CpL-grant signal already used (`grant_cpl_on_holiday_work`). It also reverses the debit instantly if that Shift is later edited away from the leave type or deleted. `reconcile_shift_leave_usage` (nightly) is now only a catch-up for `bulk_create`-written rows (bulk roster uploads bypass Django signals on purpose, for performance) — most usage no longer waits for it.
- **AL's `accrued` computes live, not from a monthly batch.** `leave/services.py::live_accrued_al` counts worked days from the open cycle's start to today, ÷ 18, every time the balance is read (balance page, leave-application validation) — `apply_monthly_accrual.py` no longer processes AL at all (excluded via `cycle=ANNIVERSARY`; kept dormant for a hypothetical future CALENDAR+WORKED_DAY_RATIO policy, same treatment `apply_year_end_rollover.py` got). The live number is **frozen exactly once**, as the cycle's permanent historical value, at the moment `close_al_cycles.py` closes that cycle — a later correction to that closed window's Shift data does not retroactively change it (would need an explicit `ADJUSTMENT`, same posture as everything else post-close).
- **Real incident that drove this**: reviewing a real employee (Imran Ahmed, ID 623) the same day surfaced `available: -6.00` — `used=6` was correctly picked up by Shift reconciliation, but `accrued=0` because the monthly batch had never run for this employee's elapsed cycle months under the new anniversary system. He'd actually worked 117 days (should read `accrued≈6.5`). Backfilling the missing months fixed the display, but exposed the batch model's lag as the root cause — replaced with live computation the same session. **Testing the fix immediately caused a second incident**: `apply_monthly_accrual` had no idempotency guard, so re-running an already-processed month silently double-credited `accrued` for **1,020 employees** company-wide. Fixed with a note-matching dedup guard (same pattern the CpL signal already used) and reversed via an `ADJUSTMENT` transaction per affected balance — nothing was deleted, the ledger stayed append-only.

**Paid-leave credit + 12-day cap — added 2026-09-02:**
- The original motivation was a flat 12 AL days guaranteed per anniversary cycle (a genuine switch to `FIXED_ANNUAL`, like CL/SL) — a real reversal of the ratio-based model confirmed just the day before (2026-09-01, above), so it was treated as pending explicit CHRO confirmation. **Decided 2026-09-02: not needed** — the refinement below already achieves both goals (paid leave doesn't cost AL; nobody exceeds 12/cycle) without touching the accrual mechanism itself, so the `FIXED_ANNUAL` switch is shelved, not merely deferred.
- **The ratio mechanism stays, refined two ways**: (1) "worked days" now also counts any day under a *paid* `LeavePolicy` (`is_paid=True` — currently AL, CL, CPL, MtL, SL), so taking paid leave no longer reduces AL accrual the way it used to (only genuinely unpaid time — LWP, HdL, ABS — still reduces it); (2) the live-computed result is clamped to `LeavePolicy.max_days_per_year` when set above 0 — **12 for AL**, reusing the same field `FIXED_ANNUAL` policies (CL/SL/MtL) use for their flat entitlement (a new use of that field for a `WORKED_DAY_RATIO` policy; harmless, since AL never takes the `FIXED_ANNUAL` code path that field otherwise feeds). Both live in `leave/services.py::live_accrued_al`, and both changes apply to open/future cycles only — closed cycles remain frozen snapshots.
- **Net effect**: an employee attending fully (working or on paid leave every non-off, non-holiday day) now lands at exactly 12/cycle instead of ~11.5–13 depending on how much CL/SL they happened to use. Someone with real unpaid absence still accrues proportionally less — that part of the design is unchanged and intentional. This is now the settled AL accrual rule, not an interim step.

**Shift-summary report basis mismatch + a second, deeper incident — 2026-09-02:** reviewing the same employee (Imran Ahmed, ID 623) again — this time via `/leave/reports/shift-summary/` — surfaced two separate bugs:

1. **The report's AL "taken" was on a different basis than AL "allocated".** `payroll/reports.py::build_shift_summary` computed "taken" as a raw count of AL shifts inside the report's own start/end window (arbitrary, user-chosen), while "allocated" was already the live cycle balance (anniversary-cycle-based). For Imran the window happened to span his cycle boundary, so "taken" showed 12 (both his closed-cycle and open-cycle AL shifts) next to an "allocated" that only ever meant the open cycle — not comparable, and confusing for every employee whose report window crosses a cycle boundary. Fixed by adding `_al_ledger_snapshot` (replaces `_al_ledger_allocation`), which returns both allocated *and* taken (via `live_used`) for the same open cycle. CL/SL are unaffected — they stay on the report window / calendar-year pro-ration, which is correct for them.
   - **Reversed 2026-10-30.** Putting cycle-to-date usage in the report's plain "AL" column read as "AL taken in this date range" and wasn't: employee 4132 (cycle opened 2026-05-31) showed AL 4 for Oct 1–30 when only 2 AL days fell in that window (the other 2 were in July) — and "Taken" (5) then mixed a cycle figure into an otherwise window-based total. The AL column is now the window count again, like CL/SL, and the cycle basis gets its own columns: "AL allocated (cycle)", **"AL used (cycle)"** (`al_cycle_used`, the snapshot's `live_used`) and **"AL balance (cycle)"** (`al_cycle_balance` = allocated − used). "Taken" is the plain window total; "Remaining" = AL cycle balance + (CL allocated − CL taken) + (SL allocated − SL taken), each part on its own basis. `_al_ledger_snapshot` itself is unchanged.
2. **The real bug: his `allocated` was reading 1, not a live number.** His current-cycle `LeaveBalance` row (year=2026) had `cycle_start`/`cycle_end` = NULL. Root cause: `get_or_create_balance` keys its lookup by `(employee, policy, year)`. Before the 2026-09-01 rework, AL had one row per employee per *calendar* year; for any employee whose anniversary-cycle start happens to fall in a year that already had one of those old rows (Imran's: `[2026-02-01, 2027-02-01)`, year=2026 — collided with his pre-rework "2026" row from the very incident described above, which itself never got a real fix beyond a manual backfill of `accrued`), `get_or_create` silently returned the stale row instead of creating a fresh cycle-tracked one — `cycle_start`/`cycle_end` are only set via `defaults`, which `get_or_create` ignores on a match. With `cycle_start` NULL, `live_accrued_al` permanently fell back to the frozen legacy `accrued` value, and `close_al_cycles` could never select the row either (it requires `cycle_end` set) — a silent, permanent stall, not a one-time display glitch. **Scope**: audited company-wide — **1,036 of 1,887** AL `LeaveBalance` rows (~55%) had this same NULL `cycle_start`.
   - **Fix**: `get_or_create_balance` now self-heals — when it matches an existing row with `cycle_start IS NULL` for a still-open cycle (`cycle_end > today`; a historical `as_of` request must never resurrect an already-elapsed cycle as if it were live), it populates `cycle_start`/`cycle_end` in place and leaves every numeric field (`opening`/`accrued`/`used`/`carried_forward`) untouched — `live_accrued_al`/`live_used` take over correctly the moment the window is set. This makes the failure mode self-correcting for any future collision, not just a one-off patch.
   - **Remediation**: `leave/management/commands/heal_al_cycle_collisions.py` (one-off, `--dry-run` supported) walked every existing stale row and triggered the heal via the same fixed `get_or_create_balance`. Run 2026-09-02: **706 rows healed**, 325 correctly left alone (genuinely historical, predating the employee's current cycle), 5 skipped (employee no longer clears the 12-month eligibility gate). Imran's `al_allocated` went from `1` to `7.5` (135 worked days ÷ 18, live) with no change to `opening`/`used`/`carried_forward`.

**Dashboard tile had the same `year=`-vs-cycle-year bug as the payroll report, plus two more findings — 2026-09-03:** auditing employee 539's (S. M. Rashel) dashboard tile against `/leave/balance/` after a production backup restore surfaced three issues, all fixed the same day:

1. **`core/dashboard.py::_build_leave_balances`** had the exact same bug as `payroll/reports.py` did before its 2026-09-01 fix (above): it filtered `LeaveBalance.objects.filter(employee=user, year=year)` using the current calendar year, which is wrong for AL (ANNIVERSARY-cycle, keyed by the cycle's *start* year). For 539 (cycle 2025-11-01 → 2026-11-01, keyed `year=2025`) this missed the real row and grabbed a dead `year=2026` legacy row instead, showing "AL 2.00/2.00" against the balance page's correct "8.50 available". Fixed by splitting the lookup: CALENDAR-cycle policies keep the `year=` filter; AL is looked up by its open cycle window (`cycle_start__isnull=False, cycle_end__gt=today`) instead, without `get_or_create_balance`'s side effect of creating a missing row (the dashboard intentionally never auto-materialises). Confirmed company-wide before the fix: 387 employees had an open AL cycle keyed to a different year than the current calendar year, 321 of them with a same-employee decoy row making the tile actively wrong rather than blank.
2. **Bulk roster uploads can silently strand a leave-ledger debit.** `apply_upload`/`import_data_solutions_roster` write via `bulk_create` (bypasses `leave/signals.py::sync_shift_leave_ledger`, which normally reverses a debit the instant its `Shift` is retyped away from that leave policy). `reconcile_shift_leave_usage` only ever added missing debits, never reversed a stale one. Added `leave/services.py::reconcile_stale_shift_leave_reversals` (mirror of `reconcile_orphan_shift_usage`) and wired it into the same nightly command/Celery beat slot. Found and reversed 26 stale debits (18 CL, 8 SL) across 19 employees in production, including 539's SL (a shift reconciled 2026-09-01, then retyped to a worked shift by a later roster upload).
3. **324 dead pre-anniversary-rework AL `LeaveBalance` rows deleted.** These are exactly the "325 correctly left alone" rows from the 2026-09-02 remediation above — confirmed inert (`live_accrued_al`/`live_used`/`live_available` never read them while a cycle is open) but latent: `get_or_create_balance`'s self-heal matches purely on `(employee, policy, year)`, so each employee's *next* cycle will eventually key to the same year and silently reuse the dead row, carrying its stranded `accrued` into a live cycle (harmless to `available` — `close_al_cycles` overwrites rather than adds — but understates that cycle's closing ledger transaction by the stale amount). **Decision: delete rather than zero out** — the data is confirmed superseded by the rework, not real entitlement, and `LeaveBalanceTransaction.balance` is `on_delete=CASCADE` so this also clears their attached transactions. New one-off `leave/management/commands/purge_dead_al_legacy_rows.py` (`--dry-run` default, mirrors `heal_al_cycle_collisions.py`'s style) re-derives "dead" independently rather than trusting "whatever's left after healing" — a row can also be left with `cycle_start IS NULL` because the employee no longer clears the 12-month eligibility gate (5 such rows found, correctly left alone, not dead). A row with a real `used` debit was initially carved out and left for manual review (2 of the 324: employees 3484, 4612) rather than deleted alongside the other 322 — resolved by finding #4 below, then deleted in a second pass. Reproduce in production via `heal_al_cycle_collisions` then `purge_dead_al_legacy_rows`, dry-run first each time.
4. **291 leave-ledger transactions across 151 balances orphaned by hard-deleting test `LeaveApplication` rows.** While testing the sandwich-billing calculation, `LeaveApplication` rows were created (triggering `consume_balance`/`_reconcile_application` debits) and later deleted outright rather than cancelled through the normal workflow. `LeaveBalanceTransaction.application` is `on_delete=SET_NULL`, so the debits (and any matching refunds already written before deletion) survived with their `application` link nulled, indistinguishable from a real event except by `note` (`consume_balance` -> blank; `restore_balance` -> "Cancelled / declined"; `_reconcile_application` -> "Sandwich recompute (...)") — this includes 2 of the AL rows from finding #3, which is why they'd shown a real `used` debit despite being otherwise-dead legacy rows. New one-off `leave/management/commands/reverse_orphaned_application_transactions.py` (`--dry-run` default) nets these per balance rather than reversing each individually — a debit that was already properly cancelled-and-refunded before its application was deleted nets to zero and must be left alone, or it gets double-credited. Idempotent via a marker note on its own corrective `ADJUSTMENT` transactions. Run 2026-09-03: 108 of 151 affected balances had a real nonzero net (43 already netted to zero); credited back **+244.50 days total**. `manage.py recompute_leave_billing` (pre-existing, unrelated to this — reconciles sandwich billing drift for applications that still exist) was also dry-run to check for a related concern about historical Excel-uploaded shifts never triggering sandwich recompute: zero drift found, nothing to fix there.

**Advance against not-yet-accrued entitlement — added 2026-09-10:** Imran Ahmed (the same employee from the incidents above) hit the hard block at `/leave/apply/` with only 2 AL currently accrued, wanting to apply for 5 — a legitimate advance against days he'll accrue later this cycle, with his manager willing to approve knowing that. Balance enforcement had no middle ground: short of the *live* balance meant a flat reject, for the employee at form-submit time and for the manager again at Approve, with no way for a supervisor to exercise judgment.

- **New knob**: `LeavePolicy.allow_advance_against_entitlement` (bool, default False). `True` for AL, CL, and SL (`leave/migrations/0009_al_cl_sl_advance_against_entitlement.py`); `False` (unchanged hard-block behavior) for MtL, CpL, LWP, HdL. (CpL's separate `cpl_use_within_days` expiry block was still live when this was written; it was switched off 2026-10-01 — see §5.12 — but the advance knob stays `False` for CpL regardless.)
- **The rule**: when a request exceeds the live/currently-accrued balance on a policy with this flag set, it isn't necessarily rejected — it's checked against a second, harder **ceiling**: `max_days_per_year + opening + carried_forward − used − encashed`. That's "assume the employee earns everything they could possibly earn this cycle/year (the configured cap, not what's accrued/granted so far) — is the request even theoretically payable?" For AL, `max_days_per_year` is already the per-cycle accrual cap (12) reused from §5.7's 2026-09-02 refinement above, so the formula needs no AL-specific logic. For CL/SL (flat annual grant, no further accrual within the year) it reduces to "full annual quota minus what's already been used" — e.g. 5 CL already taken + a request for 6 more (11 > the 10/year cap) is still hard-blocked, arithmetically impossible regardless of supervisor discretion; a request for 4 more (9 ≤ 10) is allowed as an advance. Below the ceiling, the request is no longer blocked at either checkpoint (form submit or Approve) — the decision belongs to the supervisor, who sees the same shortfall figure in the balance drawer (amber "Advance request" styling, not the red "will be rejected" reserved for the genuinely-impossible case) before deciding.
- **`max_days_per_year == 0` is treated as "uncapped"** (its existing meaning elsewhere in this model, e.g. LWP/HdL) — there's no meaningful "everything you could ever earn" ceiling to advance against in that case, so `describe_balance_impact` falls back to the ordinary hard block rather than computing a nonsensical zero ceiling.
- **Audit trail**: `LeaveApplication.approved_over_balance` + `balance_shortfall_days` are set once, at the moment `LeaveApproveView` approves an application that was in the advance zone — frozen then, since the point-in-time shortfall can't be reconstructed later as the ledger moves on. Shown as a `⚠ Advance` badge on the leave list and detail page.
- **Known, accepted consequence — not treated as a bug**: if an employee approved into an advance doesn't end up accruing (AL) or the year doesn't otherwise cover it (CL/SL can't accrue further within the year at all), `close_al_cycles.py`'s existing `if unused < 0: unused = 0` (and the equivalent at CL/SL's year-end rollover) already forgives the shortfall rather than carrying it as a debt into the next cycle/year — same as it always has for any other negative-balance cause. No new debt-tracking was added; recovering a genuine over-draw, if HR wants to, stays the same **manual settlement step** separation payout already is (§5.7 above).
- **Belt-and-suspenders fix in the same change**: `leave/management/commands/seed_leave_policies.py`'s hardcoded AL kwargs had drifted back to the pre-2026-09-01 values (`carry_forward_pct=0.50`, `encash_carry_forward_excess=True`) — the script was never updated when the §5.7 carry-forward flip above landed as a data migration instead. A routine re-run of that command would have silently reverted a real AL policy row to the wrong numbers. Fixed in the script and re-applied via the same 0009 migration as a defensive correction (does not revert on migration rollback — only the new advance flag does).

### §5.8 Casual Leave (CL)
- **Entitlement**: 10 days/calendar year (Jan–Dec).
- **Pro-rated** for mid-year joiners.
- **Use cases**: personal/family/emergency.
- **No carry-forward**.
- **Advance requests**: allowed against the full 10/year cap, not just what pro-ration has granted so far — see §5.7's "Advance against not-yet-accrued entitlement" above.

### §5.9 Sick Leave (SL)
- **Entitlement**: 14 days/calendar year (Jan–Dec).
- **Pro-rated** for mid-year joiners.
- **Self only** (not for family illness).
- **Doctor's certificate**: required for **3+ day** applications. 1- or 2-day SL applications don't require an attachment. (The policy text says "generally required for all sick leave"; this is the operational threshold.)
- **No carry-forward**.
- **Advance requests**: allowed against the full 14/year cap, not just what pro-ration has granted so far — see §5.7's "Advance against not-yet-accrued entitlement" above.

### §5.10 Maternity Leave (MtL)
- **Eligibility**: female employee + ≥6 months service + <2 surviving children.
- **Duration**: 16 weeks total (8 weeks pre-delivery + 8 weeks post-delivery).
- **Documents required**: written notice + medical-practitioner certificate (Forms 18 / 18a / 19 in the policy annexures).
- **Failure modes** (insufficient service, ≥2 children, male employee): MtL is hidden from the leave-type dropdown; LWP is the fallback.

### §5.11 Festival Leave (FL)
- HR-circulated calendar; adjustable on moon-sight.
- **Operational decision**: everyone observes the same festival days, regardless of religion. Festival days are seeded as plain `Holiday` rows.
- Festival bonus is a payroll concern, not modeled in Orbis.

### §5.12 Compensatory Leave (CpL)
- **Operational decision**: 1 day CpL per holiday OR festival worked. (The policy text grants 2 for festivals; SEBPO standardises on flat 1.)
- **Grants do not expire** (revised 2026-10-01; manager decision). Until then `cpl_use_within_days = 3` rejected any CpL application that would consume a grant more than 3 working days old — `leave/services.py::cpl_unexpired_balance` still implements that FIFO deadline walk and still fires for any policy with the knob > 0, but CpL's is now `0`, so `describe_balance_impact` routes CpL through the ordinary paid-leave balance check instead (`leave/migrations/0012_cpl_no_expiry_full_carry.py` flips the live row; `seed_leave_policies` writes the same values).
- **The only gate is balance ≥ requested**: an employee may apply as long as at least one day is in balance, and no further. The "policy not seeded yet" bypass in `describe_balance_impact` (which lets CL/SL/AL/MtL through when opening + accrued + carried_forward is 0 so HR can configure entitlements ahead of use) is **skipped for grant-driven policies** (`cpl_grant_on_holiday_work=True`) — CpL has no entitlement to configure, its balance *is* its grants, so a zero there means "nothing earned" and hits the hard block. Without that guard, switching the expiry off would have let a never-granted employee apply for CpL.
- **Carries forward 100% across calendar years** (`carry_forward_pct = 1.00`, no cap, no freeze, no encashment): a day earned working Dec 16 or Dec 25 can be taken in January. CpL balances are still per-calendar-year rows, so the carry is a real ledger step — `apply_year_end_rollover` (the "Year-end rollover" periodic task, 01:00 Asia/Dhaka on Jan 1 — a `django_celery_beat` row, `docs/SCHEDULER_ADMIN_PLAN.md`) posts a `CARRIED_FORWARD` transaction onto the new year's row. That command was dormant until this change and is now **idempotent**: it tags each closing year's carry by note and re-posts only the difference, so the task's second firing on **Jan 15** (`day_of_month="1,15"`; `scheduler/migrations/0003_rollover_jan15_catchup.py` retimes the already-seeded prod row) and any manual re-run pick up December grants or usage posted after Jan 1 — a late roster upload for the last week of December is the expected case. `CplGrant` history consumption is unaffected: it already picks the oldest OPEN grant regardless of year.

### §5.13 Leave Without Pay (LWP)
- Primary use: extension of maternity.
- Secondary: discretionary regularization of unauthorized absences.
- Salary deduction handled in payroll (out of scope).

### §5.14 Half Day Leave (HdL)
- Half-basic-salary deduction.
- Manager + HR approval.
- No fixed entitlement (deduct-on-use).

### §6 Leave record
- Self-service balance dashboard (`/leave/balance/`) replaces the §6 email loop.
- Monthly HR report (`/leave/reports/monthly/`) exports CSV.

## Sandwich algorithm

This is the core technical design.

### Definitions
- **Working day for employee E on date D**: D is not a Holiday and the employee's `Roster` weekly pattern for D's weekday has a WORK-category ShiftType. (Equivalent to: would `prefill_shifts_from_pattern` produce a WORK Shift on D?)
- **Non-working day**: weekend (employee's roster says OFF for that weekday) or Holiday calendar entry.
- **Leave day**: any date covered by an APPROVED `LeaveApplication` for the employee, OR any date in the application currently being processed.

### Rule
For each calendar day D in `[application.start_date, application.end_date]`:
- **If D is a working day**: count as 1 leave day (deducts from balance).
- **If D is a non-working day**:
  - Look at the closest leave-day strictly before D and strictly after D *for this employee*. Both lookups can hit (a) other days in this same application or (b) any adjacent APPROVED `LeaveApplication`.
  - **Both exist** → D is sandwiched. Count as 1 leave day. Materialise a LEAVE Shift on D.
  - **Only one or neither** → D is at an edge. Count as 0. Skip materialising a LEAVE Shift; the roster grid renders the Holiday/OFF as it would normally.

### Worked examples (Sat/Sun off)
| Application range | Working | Sandwich | Edge | Billed |
|---|---|---|---|---|
| Mon only | Mon | — | — | 1 |
| Fri only | Fri | — | — | 1 |
| Fri–Mon (single app) | Fri, Mon | Sat, Sun | — | 4 |
| Sat–Mon (single app) | Mon | — | Sat, Sun (no leave before Sat) | 1 |
| Fri (app A), Mon (app B) — both approved | Fri, Mon | Sat, Sun (cross-app) | — | 4 (1 + 1 + 2 bridge) |
| Mon, Tue + Holiday Wed + Thu, Fri | Mon, Tue, Thu, Fri | Wed | — | 5 |
| Fri (Holiday), Mon | Mon | — | Fri (no leave before) | 1 |

### Cross-application sandwich
The policy diagram explicitly shows separate CL applications wrapping a weekend. When approving application B that's adjacent to a previously-approved application A (with only non-working days in between), the bridge days are now sandwiched and must be billed retroactively. Conversely, cancelling A "opens" a previous sandwich and reduces the bill on B.

**Implementation**: `recompute_sandwich(employee, around_date)` runs after every approval and cancel:
1. Inspects all approved `LeaveApplication`s for the employee within ±14 days of the changed app.
2. Walks each affected application's date range, applies the algorithm, materialises/de-materialises LEAVE Shifts on bridge days as needed.
3. Updates each application's `effective_days` / `working_days` / `sandwich_days` columns and reconciles the corresponding `LeaveBalanceTransaction` rows.

Idempotent — pure function of the current approved-leave state. Re-runnable, no race-condition risk.

### Materialisation onto the roster
- Working leave days → LEAVE Shift created (overrides pattern-fill).
- Sandwich days → LEAVE Shift created (overrides Holiday/OFF pattern).
- Edge non-working days → no LEAVE Shift. Roster grid renders the Holiday/OFF.

### Reverse logic
On cancel, `reverse_shifts_for_leave` deletes only Shifts tagged with this app's fingerprint (`note="Leave #{app.pk}"`). Combined with `recompute_sandwich`, adjacent applications regain their bridge days correctly.

## Schema additions

### `leave/models.py`

**Extend `LeavePolicy`** with the policy-driven knobs:
- `max_days_per_year` — PositiveIntegerField, default 0. For `FIXED_ANNUAL` policies (CL=10, SL=14, MtL=112), the flat annual entitlement. For `WORKED_DAY_RATIO` (AL), a separate use added 2026-09-02: the per-cycle cap on live-computed accrual — 12 for AL, 0 (unused) before then.
- `applies_to_gender` — CharField (ANY / FEMALE / MALE), default ANY. Gates Maternity to female employees.
- `min_service_months` — PositiveIntegerField, default 0. AL=12, MtL=6, others=0.
- `max_surviving_children` — PositiveIntegerField, nullable. MtL=1 (must have <2), others=null.
- `prorate_on_join` — Boolean, default True. False for AL.
- `bridges_sandwich` — Boolean, default True. False for LWP and HdL.
- `cycle` — CharField (CALENDAR / ANNIVERSARY), default CALENDAR. ANNIVERSARY only for AL accrual eligibility gate.
- `accrual_method` — CharField (NONE / FIXED_ANNUAL / WORKED_DAY_RATIO), default NONE. WORKED_DAY_RATIO for AL.
- `accrual_ratio_denominator` — PositiveIntegerField, default 0. 18 for AL. Since 2026-09-02 the numerator counts paid-leave days too (any `is_paid=True` policy), not just WORK-category shifts, and the ÷18 result is clamped to `max_days_per_year` above.
- `carry_forward_pct` — DecimalField, default 0. 0.50 for AL.
- `max_carry_forward_days` — PositiveIntegerField, nullable. 60 for AL.
- `freezes_entitlement_at_cap` — Boolean, default False. True for AL.
- `encash_carry_forward_excess` — Boolean, default False. True for AL.
- `cert_required_min_days` — PositiveIntegerField, default 0. 0 = never required; ≥1 = attachment required when `application.day_count >= cert_required_min_days`. SL=3, MtL=1, others=0.
- `cpl_grant_on_holiday_work` — Boolean, default False. True for CpL.
- `cpl_use_within_days` — PositiveIntegerField, nullable. 0 for CpL since 2026-10-01 (was 3); 0/NULL = never expires.

**Extend `LeaveApplication`**:
- `effective_days` — DecimalField, default 0. Billed count after sandwich resolution.
- `working_days` — DecimalField, default 0. Working days in the range.
- `sandwich_days` — DecimalField, default 0. Bridge days absorbed.
- `is_half_day` — Boolean, default False.
- `half_day_period` — CharField (AM / PM / null).

**New model `LeaveBalance`** — `(employee FK, leave_policy FK, year IntField)` with a unique-together constraint:
- `opening` — DecimalField (entitlement at year start, after pro-rating).
- `accrued` — DecimalField (AL accrual accumulator).
- `used` — DecimalField (debit total).
- `encashed` — DecimalField.
- `carried_forward` — DecimalField (received from previous year).
- `entitlement_frozen` — Boolean. Set when AL hits the 60-day cap.
- `available` — property: `opening + accrued + carried_forward − used − encashed`.

**New model `LeaveBalanceTransaction`** — append-only ledger:
- `balance` — FK to LeaveBalance.
- `occurred_at` — DateTimeField.
- `delta` — DecimalField (signed).
- `reason` — CharField, choices: ACCRUAL / USED / REVERSED / CARRIED_FORWARD / ENCASHED / FROZEN / ADJUSTMENT.
- `application` — FK, nullable.
- `created_by` — FK CustomUser, nullable.
- `note` — CharField.

**New model `EncashmentRequest`**:
- `employee` — FK CustomUser.
- `leave_policy` — FK.
- `year` — Integer.
- `days_requested` — DecimalField.
- `days_approved` — DecimalField, nullable.
- `status` — CharField (PENDING / APPROVED / REJECTED / PAID).
- `requested_at` — DateTimeField.
- `processed_by` — FK, nullable.
- `processed_at` — DateTimeField, nullable.
- `note` — CharField.

### `employees/models.py`
**Extend `JobInfo`**: add `surviving_children_count` (PositiveSmallIntegerField, default 0). Drives Maternity eligibility. Gender continues to read from `PersonalInfo.gender`.

## Service layer (`leave/services.py`)

- `compute_application_billable_days(application, *, include_pending=False)` → `(working_days, sandwich_days, total)` decimals. Walks the date range, applies the sandwich algorithm using `Roster` + `Holiday` + adjacent approved applications.
- `recompute_sandwich(employee, around_date)` — idempotent recompute over ±14 days. Updates `effective_days` etc., adjusts Shift materialisation, reconciles ledger.
- `materialise_shifts_for_leave(application, approver)` — sandwich-aware. Writes LEAVE Shifts only on billable days.
- `reverse_shifts_for_leave(application)` — unchanged contract; complements recompute.
- `consume_balance(application, created_by)` — debit transaction.
- `restore_balance(application, created_by)` — reverse transaction.
- `validate_application_against_balance(application)` — pre-approval check.
- `prorate_entitlement(policy, joining_date, year)` — for new joiners and probation: `entitlement * (months_remaining_in_year / 12)`, rounded to 0.5.

## Eligibility validators

- **AL service-length gate**: block AL applications when `joining_date` is < 12 months ago.
- **MtL gender**: hide MtL from the dropdown for non-female employees.
- **MtL service**: ≥6 months from `joining_date`.
- **MtL children**: `JobInfo.surviving_children_count < 2`.
- **Attachment threshold**: when `policy.cert_required_min_days > 0` and `application.day_count >= policy.cert_required_min_days`, require `application.attachment`. Form UI dynamically toggles the required indicator.
- **CpL expiry**: `cpl_use_within_days` working days from grant date; reject CpL applications that consume an expired grant. **Off since 2026-10-01** (knob = 0) — CpL is validated on plain balance ≥ requested instead, with the unseeded bypass disabled for grant-driven policies (§5.12).

## Forms + UI

- `LeaveApplicationForm`:
  - Eligibility-filtered leave-type queryset.
  - `is_half_day` + `half_day_period` fields.
  - Conditional `attachment` requirement.
  - Live AJAX preview pane → `/leave/preview/` returns `{working, sandwich, total, available_balance}`.
- `LeaveApplyView` template (`templates/leave/apply.html`): preview pane next to date pickers.
- `LeaveDetailView` template: working/sandwich/billed breakdown.
- **New** `LeaveBalanceView` (`/leave/balance/`): per-policy table (opening / accrued / carried / used / encashed / available) + transaction-history drawer per row.
  - **The drawer's leading column is the date the leave was *taken*, not `LeaveBalanceTransaction.occurred_at`** (revised 2026-09-06). `occurred_at` is when the ledger row was *written*, which for roster-direct leave is the reconcile batch's timestamp — so three CLs taken in January, March and April all read as one identical September datetime, which is what the drawer used to show. `LeaveBalanceTransactionsView` now coalesces `shift__date` → `application__start_date` → `occurred_at` for both the displayed date and the sort (the `[:200]` cap would otherwise slice an arbitrary 200 out of a same-timestamp batch), and returns a rendered fragment (`templates/leave/_balance_history.html`, htmx `hx-trigger="load"`) instead of JSON so it can also show source (linked back to the application, or to that week of the roster), applied by/at, approved by/at, and recorded at/by.
  - Roster-direct rows — most real usage, and all of the Jan–Aug 2026 pre-Orbis history — have no `LeaveApplication`, so applied/approved are legitimately blank; the drawer says so in a footnote rather than leaving the columns unexplained. Leave filed through `LeaveApplication` from here on carries the full trail.
- **"Direct reports only" on the Pending approvals and All leaves tabs** (added 2026-10-10). Both tabs open narrowed to the viewer's immediate reports (`accounts.permissions.direct_report_pks`, one level of `JobInfo.reports_to`) — the same default the roster grid has used since launch (`roster.views._resolve_grid_employees`). The switch is a checkbox in the tab row, deliberately *not* in the Filters popover: it is a scope switch rather than a filter, so it never lights the Filters button, survives the popover's "Clear" link, and is visible on page load, where a default that hides rows has to be. URL contract: `?direct=0` widens to the full approval scope; absent means on, so the default URL stays clean and the dashboard's `?tab=pending` links land on the direct-reports view (their count is the full managed scope, so the card number can exceed the rows shown — the checked box explains the gap). The flag rides through `_leave_filter_suffix` only when set to `0`, so it survives the list/calendar toggle, month navigation and paging. It narrows *within* `_visible_leave_qs`, never widens. A viewer with **no direct reports** (typically HR/Admins, for whom All leaves is the working list) never gets the switch: the checkbox is hidden and the full scope shows, because a default that can only ever show an empty tab is a trap — deliberately *unlike* the roster grid, which gives HR an empty default. Decided 2026-10-10 after first shipping the roster-style empty default the same day. One switch serves both tabs and carries across them via the tab links; "My leaves" has no switch (the viewer's own rows are never a direct report's).

- **Balance drawer on the leave list** (added 2026-09-06). The employee column of `/leave/` gets a wallet icon per row that opens the balance in the right-hand drawer, so a manager can decide without leaving the list — and because `LeaveDetailView` shows no balance either, this was previously unavailable anywhere in the approve/decline flow.
  - **It is `LeaveBalanceView` itself, not a second implementation** — the view returns `leave/_balance_drawer.html` when `request.htmx`, and both hosts render the same `leave/_balance_table.html`. A separate drawer view would have had to re-derive the AL anniversary-cycle row selection and the `live_accrued_al`/`live_used` calls, which is exactly the drift that produced findings #3 and #4 above.
  - `?application=<pk>` adds the decision arithmetic: requested days (working + sandwich), available, and available-if-approved. This comes from the new `leave/services.py::describe_balance_impact`, which `validate_application_against_balance` is now a thin wrapper over — so the shortfall a manager is shown is by construction the one Approve will raise, rather than two implementations that agree until they don't.
  - **Availability there is as of the leave's `start_date`, not today**, because that is the window the approve-time guard uses. For a future-dated leave it therefore disagrees with the table below it (as of today) by whatever accrues in between; the drawer states the as-of date rather than showing two unexplained "available" numbers.
  - Already-decided applications show status instead of an if-approved figure — the debit is already inside `available`, so the subtraction would double-count.
- **New** `EncashmentRequestView` + `EncashmentApproveView` (HR-only).
- **New** sidebar entries: "Leave balance" (All Employees), "Encashment" (HR / User Admins).

## Approval / cancel wiring (`leave/views.py`)

- `LeaveApproveView`: validate balance → save APPROVED → materialise (sandwich-aware) → consume_balance → recompute_sandwich.
- `LeaveCancelView`: restore_balance → reverse shifts → recompute_sandwich.
- `LeaveDeclineView`: status DECLINED, no shifts/balance changes.

Dept-head approval routing reuses `accounts.permissions.can_act_on_leave` from the prior PR. No new code.

## Automation

- **Management command `apply_monthly_accrual`** (the `Monthly leave accrual (dormant)` periodic task, 00:30 on the 1st):
  - For each (active employee, policy with `accrual_method=WORKED_DAY_RATIO`): count last month's WORK Shifts, divide by `accrual_ratio_denominator`, add to `LeaveBalance.accrued`, write ACCRUAL transaction.
  - Skip when `entitlement_frozen=True`.
  - `--dry-run` flag.
  - Dormant for AL (`cycle=ANNIVERSARY` excludes it — see §5.7's "Real-time, not batch"). Its WORK-only, uncapped counting was **not** updated to match `live_accrued_al`'s 2026-09-02 refinement (paid-leave credit + 12-day cap) since it can't currently run for any live policy — whoever revives this for a future CALENDAR+WORKED_DAY_RATIO policy should decide then whether that policy wants the same treatment.

- **Management command `apply_year_end_rollover`** (the "Year-end rollover" periodic task — 01:00 Asia/Dhaka on Jan 1 and again Jan 15, a `django_celery_beat` row per `docs/SCHEDULER_ADMIN_PLAN.md`; CALENDAR-cycle policies only — today that's CpL):
  - For each (employee, policy with `carry_forward_pct > 0`): take that fraction of unused balance, capped at `max_carry_forward_days`, push to next year's `carried_forward`. **Re-runnable**: the carry is tagged by closing year and only the difference vs. what's already posted is written (2026-10-01).
  - If new `carried_forward >= max_carry_forward_days` and `freezes_entitlement_at_cap=True`: set `entitlement_frozen=True`, emit FROZEN transaction.
  - For `encash_carry_forward_excess=True`: any unused beyond carry-forward auto-creates a PENDING `EncashmentRequest`.

- **Signal on `Shift` save**: when WORK Shift saved on a Holiday-tagged date, atomically credit +1 CpL and write an ACCRUAL transaction. Idempotent (checks for existing CpL grant tagged to that date).

- **Signal on `Holiday` save**: trigger `recompute_sandwich` for any approved applications spanning that date.

## Reports

- `/leave/reports/monthly/?month=YYYY-MM` → CSV export (HR / User Admins): employee, dept, leave_type, days, status, applied_at, processed_at.
- `/leave/employees/<pk>/register/` → per-employee Form-9 layout (accumulated leave, granted dates, encashment column, signature placeholders). HR-readable.

## Files to modify / create

| File | Change |
|---|---|
| `leave/models.py` | Extend `LeavePolicy` (15 new fields); extend `LeaveApplication` (5 fields); add `LeaveBalance`, `LeaveBalanceTransaction`, `EncashmentRequest` |
| `leave/services.py` | New billing + sandwich + ledger helpers; rewrite `materialise_shifts_for_leave` |
| `leave/forms.py` | Eligibility queryset + half-day + attachment validator + preview AJAX |
| `leave/views.py` | New views (Balance, Preview, Encashment, MonthlyReport); rewire approve/cancel |
| `leave/admin.py` | Register the three new models |
| `leave/management/commands/apply_monthly_accrual.py` | NEW |
| `leave/management/commands/apply_year_end_rollover.py` | NEW |
| `leave/migrations/000X_schema.py` | NEW — schema |
| `leave/migrations/000Y_seed_policies.py` | NEW — seed 8 leave types + policies |
| `leave/migrations/000Z_zero_balances.py` | NEW — backfill year-2026 LeaveBalance rows |
| `leave/signals.py` | NEW — Shift-on-Holiday → CpL grant; Holiday change → recompute |
| `leave/tests.py` | NEW — comprehensive matrix |
| `roster/signals.py` | Hook Shift save → CpL grant signal |
| `employees/models.py` | Add `JobInfo.surviving_children_count` |
| `templates/leave/apply.html` | Preview pane |
| `templates/leave/detail.html` | Breakdown |
| `templates/leave/balance.html` | NEW |
| `templates/leave/encashment_*.html` | NEW |
| `templates/leave/reports_monthly.html` | NEW |
| `templates/employees/sections/_jobinfo.html` | Show `surviving_children_count` |

## Verification

End-to-end against staging seed: 2 dept heads, 1 PM, 1 TL, 4 ICs incl. one female with 1 child, weekly Sat/Sun off, plus a Tuesday Holiday on the test month, plus a Festival Holiday.

### Sandwich algorithm
1. **Within-app sandwich**: TL applies Fri–Mon. Preview: "2 working + 2 sandwich = 4 billed". Approve → balance debits 4, roster shows LEAVE on Fri/Sat/Sun/Mon.
2. **Edge non-working**: TL applies Sat–Mon. Preview: "1 working + 0 sandwich = 1 billed". LEAVE only on Mon.
3. **Cross-app sandwich**: TL applies Fri (1 day), approved → debit 1. TL applies Mon (1 day) → on approving Mon, `recompute_sandwich` runs: total billed across both apps = 4 (1 + 1 + 2 bridge attributed to Mon). Cancel Mon → bridge releases, Fri's bill stays at 1.
4. **Holiday inside leave**: Mon–Fri with Tuesday Holiday. Preview: "4 working + 1 sandwich = 5 billed".
5. **Holiday adjacent**: Mon only with Tuesday Holiday outside the range. Bill 1.
6. **Long chain**: app A Mon, app B Wed (Tue Holiday between), app C Fri. Approve in order. Final billing reconciles.

### Eligibility
7. **AL service gate**: employee joined 7 months ago tries AL → form rejects.
8. **MtL gender**: male employee can't see MtL in the dropdown.
9. **MtL service**: female with `joining_date` 4 months ago sees MtL but submit is rejected.
10. **MtL children**: female with `surviving_children_count = 2` rejected.
11. **SL doctor cert threshold**:
    - 1-day SL no attachment → accepted.
    - 2-day SL no attachment → accepted.
    - 3-day SL no attachment → rejected.
    - 5-day SL with attachment → accepted.
12. **CpL expiry** (superseded 2026-10-01): a CpL grant from 5 working days ago → application consuming it → **accepted**; zero grants → rejected (not bypassed); Dec 16 grant → `apply_year_end_rollover` → applicable in January, and a re-run doesn't double-carry (`CplNoExpiryTests`).

### Balance + ledger
13. **Balance dashboard**: per-policy table + transaction-history drawer.
14. **Approve increments USED**: 3-day CL approve → one USED transaction with delta=-3.
15. **Cancel reverses**: cancel that approved CL → REVERSED transaction with delta=+3.
16. **Insufficient balance**: 1 CL day available, apply for 5 → rejected ("balance available: 1 day").

### Automation
17. **Monthly accrual**: for each active employee with WORK Shifts last month, AL.accrued += `count / 18` rounded to 0.5. `--dry-run` prints what would change.
18. **Year-end rollover**: 12 unused AL days → 6 carried, EncashmentRequest for the other 6 (PENDING). CL resets to 0.
19. **60-day freeze**: AL carry-forward reaching 60 → `entitlement_frozen=True`, accrual skips that employee, admin warning surfaces.
20. **CpL on holiday**: WORK Shift assigned on Holiday → +1 CpL with 3-working-day deadline.
21. **CpL on festival**: WORK Shift on Festival Holiday → +1 CpL (flat).

### Routing + policy
22. **Dept-head leave routing**: dept head submits leave → appears in `reports_to` ancestors' queue + HR / User Admins. Peer dept-head sees it in visibility list but approve button disabled.
23. **Universal festival observance**: festival Holiday appears as observed for every active employee in the roster grid.
24. **Half-day**: HdL with `is_half_day=True, half_day_period=AM` → balance debits 0.5; roster grid shows half-day attribute.

### Reports
25. **Per-employee Leave Register**: `/leave/employees/<pk>/register/` renders Form-9 layout. HR-readable.
26. **Monthly HR report**: `/leave/reports/monthly/?month=2026-04` returns CSV.

### Tests
27. `leave/tests.py` ~50 cases. Sandwich coverage especially: 10+ cases including 3-app chains, holidays-at-edge vs middle, weekend-pattern variants, half-days.
28. Accrual + rollover command tests with fixed dates.
29. Both suites pass under `python manage.py test --settings=core.settings_test` (SQLite) and under MySQL.

## Confirmed decisions

- **Sandwich scope**: cross-application. `recompute_sandwich(employee, around_date)` runs on every approve/cancel.
- **Eight leave types**: AL, CL, SL, MtL, FL, CpL, LWP, HdL.
- **Cycle**: calendar year for CL, SL, FL, CpL, LWP, HdL. Anniversary for AL — eligibility *and* accrual/carry-forward/close all key off the employee's own `joining_date` cycle now (`LeaveBalance.cycle_start`/`cycle_end`, revised 2026-09-01 — see §5.7).
- **AL carry-forward** (revised 2026-09-01): 100% carries forward every cycle, capped at a 60-day lifetime running total; no quarterly/annual encashment. See §5.7 for the full reconciliation against the written v2.0 PDF.
- **AL 60-day cap penalty**: `LeaveBalance.entitlement_frozen` flag. Auditable.
- **Initial balances**: zero-start every employee for the current year. Bulk import from legacy ttms is out of scope.
- **Half-day**: deduct 0.5 from balance; materialise via `ShiftAttribute` "Half Day (AM)" / "Half Day (PM)".
- **Encashment**: workflow shipped (request → HR approve → mark PAID). Money movement happens in payroll.
- **Dept-head routing**: no special-case code. `reports_to` chain + HR.
- **CpL grant**: flat 1 day per holiday-day worked, festival or otherwise.
- **Festival Leave**: universal. Festival days are plain `Holiday` rows.

## Risks worth calling out

- **PR size**: ~25 files modified, ~80 new tests, two cron-suitable management commands, three migrations. Stage local commits per Block so review can happen in chunks even though the merge is a single PR.
- **Cross-app sandwich recomputation** has subtle edge cases when an employee chains many short leaves. `recompute_sandwich` is the most likely source of bugs; the test matrix needs ≥10 cases covering chains of 3+ adjacent applications.
- **AL 60-day freeze** is a one-way door per the policy. Year-end rollover surfaces a "this employee is about to be frozen" warning in admin one year before it triggers, so HR has a chance to intervene.
- **Existing approved leaves**: the Block 2 backfill migration runs `recompute_sandwich` for every existing approved application, reconciling shifts and writing initial USED transactions for the ledger.
- **✅ RESOLVED 2026-09-01 — the balance ledger used to only see leave that went through `LeaveApplication`.** `consume_balance`/`restore_balance` still debit `LeaveBalance` off an approved `LeaveApplication` as before, but `leave/services.py::reconcile_single_shift` now also debits it for LEAVE-category `Shift` rows a supervisor set directly on the roster without the employee ever filing a `LeaveApplication` — confirmed by the user 2026-08-31 as the actual bulk of real leave usage (locally, 16,713 of 16,714 LEAVE-category `Shift` rows had no `leave_application` link). **Same-day follow-up (still 2026-09-01): made instant.** `leave/signals.py::sync_shift_leave_ledger` calls it synchronously on every `Shift` save (mirrors this file's own `grant_cpl_on_holiday_work` pattern), so the debit lands the moment a supervisor sets the shift — no more nightly lag for anything written through a normal `.save()`. `reconcile_orphan_shift_usage` (`manage.py reconcile_shift_leave_usage`, nightly) is now only a catch-up batch for rows written via `bulk_create` (bulk roster uploads bypass signals on purpose, for performance). Idempotent via `LeaveBalanceTransaction.shift` (a partial-unique FK mirroring `application`); `roster.Shift` edits/deletes reverse a stale reconciliation via the same `sync_shift_leave_ledger` signal (`post_save`) and `reverse_shift_ledger_on_delete` (`pre_delete`). There's a known gap: an orphan Shift has no half-day marker (only `LeaveApplication.is_half_day` expresses that), so every orphan LEAVE Shift is counted as a full day. **Cutover caveat**: the first real (non-dry-run) *batch* reconciliation on production should still be scoped with `--since <cutover date>` and its per-(policy, year) delta report reviewed with HR first (this only applies to the historical catch-up run, not the now-instant going-forward path) — some years may already carry manual `ADJUSTMENT` transactions HR used to patch around this exact gap, and blind unbounded reconciliation risks double-counting against those.
- **✅ RESOLVED 2026-09-01, then found incomplete and finished 2026-09-02** — `payroll/reports.py`'s AL allocation used to read `LeaveBalance.objects.filter(leave_policy=al_policy, year=year, ...)` assuming AL's `year` was a calendar year, which silently diverged from a payroll report's calendar year for anyone not hired in January. Fixed 2026-09-01 by looking up each employee's own row via `get_or_create_balance` instead (now `_al_ledger_snapshot`, since renamed 2026-09-02 to also return AL "taken" on the same cycle basis — see above). That fix assumed `get_or_create_balance` always returns a properly cycle-tracked row; it doesn't when a legacy pre-rework row collides on `year` — see the 2026-09-02 entry above for the real fix (self-healing `get_or_create_balance` + `heal_al_cycle_collisions` for the ~55% of rows already affected).
