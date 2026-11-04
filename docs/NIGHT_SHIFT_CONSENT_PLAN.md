# Night Shift Consent — Implementation Plan

> **Status:** ✅ Implemented 2026-09-13. Lives in the `employees` app: 2 models
> (`NightShiftConsent` + `NightShiftConsentEvent`) + service functions + 2 views +
> 1 management command + a scheduled periodic task + a dashboard card + an HR
> audit list page, 12 passing tests (`python manage.py test employees.tests.NightShiftConsentTests
> employees.tests.NightShiftConsentViewTests`), migration applied to the dev DB
> (with the MySQL collation-conversion step — see "Gotchas" below), menu/`ProtectedView`
> seeded via `seed_orbis.py --menus-only`.

## Context

Bangladesh Labour Act 2006 s.109 (as amended 2018) requires a female worker's written
consent to work night hours (10pm–6am), and she may withdraw that consent at any time.
Orbis had no representation of this — `employees.DedicatedNightShiftAssignment` covers
*pay eligibility* for a dedicated night-duty window, not the underlying legal consent
question, and nothing tracked it for the broader population of women who work an
occasional or roster-assigned night shift (not just dedicated-duty staff).

Requirements (from the original ask):
- A consent button on the employee's own dashboard ("My" tab), shown when she has a
  night shift coming up.
- She can consent or decline; declining doesn't remove her from the schedule — it just
  records her decision for HR/roster planning to act on.
- Re-asked **every calendar month**.
- Defaults to **consenting (`True`)** — an employee only has to act if she wants to
  decline. Since the feature shipped mid-September, September's rows also default `True`.
- Full history kept per month, for audit purposes.

## Design

**Eligibility** — a female employee (`employees.PersonalInfo.gender == FEMALE`) with at
least one WORK-category, midnight-crossing shift-day anywhere in the calendar month (a
concrete `roster.Shift` row wins, falling back to the `roster.Roster` weekly pattern —
same resolution order `core/dashboard.py::_build_week_strip` already uses). Covers **any**
night shift, office or WFH (confirmed with the requester — broader than just WFH).
"Crosses midnight" reuses the single canonical derivation already living in
`payroll/inputs.py::is_night_shift`/`effective_shift_times`, rather than re-deriving it —
see that module's own docstring for why a second independent implementation of this rule
is exactly what bit this codebase once already (the reverted `ShiftType.is_night_shift`
boolean, `docs/PAYROLL_PLAN.md`).

**Data model** (`employees/models.py`) — denormalised current state + append-only ledger,
the same split `policies.PolicyAcknowledgement`/`PolicyAcknowledgementEvent` and
`leave.LeaveBalance`/`LeaveBalanceTransaction` already use:

- `NightShiftConsent` — one row per `(employee, period)`, `period` always the 1st of a
  calendar month. `consents` (bool, default `True`), `source` (`AUTO_DEFAULT` /
  `EMPLOYEE` / `HR_ON_BEHALF`), `decided_at`/`decided_by`/`note`.
- `NightShiftConsentEvent` — immutable ledger: `GENERATED` / `CONFIRMED` / `DECLINED`,
  with `actor` and `occurred_at`.

**Self-healing creation** (`employees/services.py::get_or_create_night_shift_consent`) —
mirrors `leave.services.get_or_create_balance`. A row is created (defaulting `True`, with
a `GENERATED` event) the moment it's needed: lazily from the dashboard, lazily from the HR
audit list, or in bulk from the monthly beat job below. Never manufactures a row for
someone who isn't currently eligible — `qualifies_for_night_shift_consent` gates it.

**Monthly beat job** — `generate_night_shift_consents` management command, wired into
a `django_celery_beat` `PeriodicTask` row (seeded from `scheduler/schedule.py::JOBS`) for 01:30 Asia/Dhaka on the 1st of each month.
Belt-and-suspenders alongside the dashboard/audit-list lazy creation, so HR's audit trail
is complete even for an employee who never opens her dashboard that month.

**Dashboard card** (`templates/dashboard/_tab_me.html`, `core/dashboard.py::me_context`) —
directly below "This week," shown only when `night_shift_consent` is non-`None`. Two
buttons ("I consent" / "Decline") plus an optional note textarea, posting to
`NightShiftConsentUpdateView` (`employees/views.py`). Plain POST + redirect, not htmx — a
two-button dashboard-card form doesn't need a drawer.

**HR audit list** — `/employees/night-shift-consent/`
(`employees/views.py::NightShiftConsentListView`), gated to `can_manage_night_shift_consent`
(HR/Admins, `accounts/permissions.py`) both by an in-view check and a `ProtectedView`
row (seeded via `seed_orbis.py`, under the "Leave" sidebar section — a new "Compliance"
section wasn't warranted for one page). `?period=YYYY-MM-01` reads a past month's
already-recorded rows as-is (no lazy creation — a closed month's eligibility isn't
re-evaluated); the current month lazily materialises + displays every currently
qualifying employee, satisfying the audit-history requirement.

**Permissions** (`accounts/permissions.py`):
- `can_set_own_night_shift_consent` — self-service only.
- `can_manage_night_shift_consent` — HR/Admins (`can_edit_all`), for the audit list and
  recording a decision on an employee's behalf (e.g. captured verbally).

## Non-goals

- Declining does **not** automatically remove the employee from the roster or reassign
  the shift — that stays a manual roster/HR step, same "soft signal, not a hard block"
  posture this codebase already uses elsewhere (payroll gates, offboarding notice
  shortfall). The audit list is how HR/roster planners see who declined.
- No email/notification on decline yet — the dashboard badge + audit list are the only
  visibility today.
- No retroactive consent for a closed month — a decision always applies to the
  current-month row only.

## Gotchas hit during implementation

- **MySQL collation FK break** — the *dev* database's schema default collation
  (`utf8mb4_unicode_ci`; production's has always been `utf8mb4_0900_ai_ci`, confirmed
  2026-09-19) didn't match the existing tables' `utf8mb4_0900_ai_ci`
  (including `accounts_customuser.id`, a UUID pk), so the new tables' FKs to
  `CustomUser` failed at migrate time (`OperationalError 3780`). Fixed with the same
  `RunPython` collation-conversion step `policies/migrations/0001_initial.py` uses
  (`atomic = False` + `ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4 COLLATE
  utf8mb4_0900_ai_ci`, placed right after the `CreateModel`s and before the indexes/
  constraints) — see the `mysql-collation-fk-gotcha` note for the general rule (it
  applies to *any* migration creating a new table with a UUID FK, not just a brand-new
  app's `0001`).
