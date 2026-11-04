# dsndd ↔ Orbis shift/leave sync

**Audience:** Orbis + dsndd engineers (currently the same person).
**Status:** Phase 1 (Orbis side) implemented. Phase 2 (dsndd side) not started.

dsndd (`/home/smrashel/projects/dsndd`) is a separate ~500-user Django app
where team leads do day-to-day shift scheduling. Orbis is SEBPO's central
HRIS and the system of record for HR data org-wide — any shift/leave
assignment made in dsndd needs to also exist in Orbis's `roster.Shift`.

dsndd is the **client**; Orbis is the **server**. No webhook, no dual-write —
dsndd calls Orbis's API synchronously on write, and reuses Orbis's read API
for reporting and for its shift-type catalog (dsndd has no local `ShiftType`
model). A dsndd-side reconciliation sweep guards against transient failures.
This doc is now the sole living record of the design — the original planning
session's scratch file has since been reused for unrelated planning work
(per this project's plan-file convention, that file is a scratchpad, not a
permanent record) so it no longer holds the dsndd design history. Anything
from that original discussion worth keeping has been folded into this doc;
if you find a gap, add it here rather than looking for it elsewhere.

## Key invariant

**Leave stays Orbis-owned.** Real leave-balance-affecting leave only ever
comes from an actual `leave.LeaveApplication`, approved through Orbis's own
workflow (`leave/services.py::materialise_shifts_for_application`). A dsndd
supervisor marking a cell "CL" is a schedule/planning marking only — it
writes a `roster.Shift` row via the API but never touches
`LeaveApplication`/`LeaveBalance`. Enforced server-side: **the batch endpoint
never overwrites or deletes a `Shift` row whose `leave_application_id` is
set** — it reports `conflict` and moves on.

## Orbis side (Phase 1 — implemented)

The Orbis-side implementation is deliberately **not named after dsndd** —
dsndd is the first consumer, but the endpoint, auth class, and settings are
generic so any other system needing to push shifts into Orbis can reuse the
same mechanism without it looking dsndd-specific. It's still a single shared
token/service account for now; if a second real consumer shows up, revisit
whether each caller needs its own token + principal.

- **`POST /api/v1/external/shifts/batch/`** (`roster/api/shift_writes.py`) —
  batch create/update/delete of `Shift` rows. Request: `{"actor_employee_id",
  "items": [{"client_ref", "employee_id", "date", "orbis_shift_type_id",
  "orbis_shift_attribute_id", "location", "note"}, ...]}` (max 2000
  items/request; `orbis_shift_type_id: null` deletes the cell). Writes are
  bulked (`bulk_create(update_conflicts=True)` for creates/updates, one
  OR-chained delete for removals — same technique `roster/upload.py` uses,
  including manually building `ShiftChangeLog` rows since `bulk_create`
  bypasses signals) rather than one query per item — benchmarked at 5,600
  items (800 employees × 7 days, a full department's weekly publish) in
  1.44s using 8 queries, down from ~48,000 queries with the original
  per-item `update_or_create` loop. This is also why the cap moved from 500
  to 2000: write cost is now ~flat regardless of batch size, so the cap
  only bounds payload size, not performance — an 800×7 publish now chunks
  into 3 requests instead of 12.
  `orbis_shift_attribute_id` is optional — pins the shift to a specific
  time-variant under the shift type (e.g. "EVE 1-10" 13:00–22:00 vs
  "EVE 3-11" 15:00–23:00) instead of the type's own default start/end time,
  which matters for attendance lateness checks whenever a shift type has
  more than one variant. If given, it must belong to the given shift type
  (validated) and `orbis_shift_type_id` must also be given. When omitted,
  the row falls back to `ShiftType`'s own default times via
  `Shift.display_times`. `location` is optional — one of `"MHK"`/`"MBD"`/
  `"RAOWA"`/`"WFH"` (the same codes the read endpoints already return as
  `office_location`/`work_location`), resolved via
  `accounts.locations.resolve_location_choice` — the same helper the native
  roster grid and `roster/upload.py` already use. **Every write fully
  replaces the location, not just when given** — omitting it blanks both
  `job_location`/`work_location` (which means "inherit from the employee's
  profile"), even on an update that previously had one set; an unrecognised
  code is a per-item error, not silently dropped. Always returns `200` with
  a per-item `ok`/`conflict`/`error` status — mirrors the partial-success
  pattern in `roster/upload.py`. Only a structurally broken request (not
  JSON, no `items` list, empty, over the cap, or a missing/unresolvable
  `actor_employee_id`) fails with 4xx.
- **`actor_employee_id`** (required, top-level, not per-item) — the real
  dsndd user performing the action, distinct from each item's `employee_id`
  (whose shift it is). One request = one person's action (a single cell
  edit, or one bulk import/publish run attributed to whoever triggered it),
  so it's top-level rather than repeated per item. Resolved once to a real
  `CustomUser` via `employee_id`; if missing or unresolvable, the **whole
  request** fails with 400 — you can't attribute any part of a batch to an
  unknown person. The resolved actor becomes `Shift.added_by`/`updated_by`
  on every write in the batch (see below).
- **Auth** — `ExternalTokenAuthentication` (`roster/api/shift_writes.py`), shared
  secret via `Authorization: Token <EXTERNAL_API_TOKEN>`. Resolves to a real,
  persisted `CustomUser` — the shared external-integrations service account,
  created by `manage.py create_external_service_account` (idempotent; email
  `external-integrations@sebpo.internal`, `employee_id=None`, `is_active=False`
  so it never appears in employee-scoped listings or the TimeLy roster
  endpoints). **This account only proves the calling system is legitimate —
  it is no longer what ends up on `Shift.added_by`/`updated_by`.** That's
  `actor_employee_id`'s job (above); the two are checked independently, and
  both are required.
- **Per-app token (2026-10-14, `docs/DIRECTORY_API_PLAN.md` §4.4)** — dsndd
  (served at rnc.sebpo.net) moves to its own `accounts.ApiClient` token with
  scopes `roster:read`, `roster:write` and `directory:read` (the last for
  `/api/v1/employees/`), its `service_account` set to the same
  `external-integrations@sebpo.internal` user — so writes behave exactly as
  today. Every endpoint above accepts it alongside `EXTERNAL_API_TOKEN`; the
  env token and `ExternalTokenAuthentication` are removed once dsndd has
  switched (confirm in the access log first).
- **Shift change audit log** — `roster.ShiftChangeLog` (`roster/models.py`),
  an append-only before/after record of every `Shift` create/update/delete,
  populated by signals in `roster/signals.py` (not by this endpoint — the
  same mechanism also covers native roster-grid edits, bulk CSV upload, and
  leave materialisation, since all of them already set `added_by`/
  `updated_by` correctly). Modeled on dsndd's own `ShiftChangeLog`
  (`dsndd/accounts/models.py`), widened to also diff location fields (dsndd's
  version misses those) and given a real admin (`list_display`/`list_filter`/
  `search_fields`) and a manager-facing "History" toggle on the roster grid
  — dsndd's own version has neither, just a bare admin registration.
- **Reads** — dsndd reuses the existing TimeLy-facing endpoints
  (`ShiftTypeListAPI`, `HolidayListAPI`, `EmployeeListAPI`, `DailyRosterAPI`,
  `EmployeeRangeRosterAPI` in `roster/api/timely.py`) for its own reporting and for
  its shift-type catalog cache. `_BaseAPI.authentication_classes` now tries
  `TimelyTokenAuthentication` then `ExternalTokenAuthentication` — either
  token works on these read-only endpoints.
- **`leave_locked` field** — added to `TimelyShiftSerializer` (additive,
  documented in `docs/TIMELY_API.md`). `true` when a `Shift` row was
  materialised from an approved `LeaveApplication`; dsndd's reconciliation
  sweep (Phase 2) uses this to know when to back off rather than re-push.
- **Settings**: `EXTERNAL_API_TOKEN` in `core/settings.py` / `.env.example`.
- **Tests**: `roster/api/tests_shift_writes.py`.

## dsndd side (Phase 2 — not started)

Not yet implemented. Summarized from the plan for when this phase starts:

- Drop dsndd's local `ShiftType` model entirely; `Shift`/`RosterShift` get
  denormalized fields (`orbis_shift_type_id`, `shift_code`,
  `shift_category`, `is_working_shift`) resolved from a Django-cache-backed
  read of Orbis's `/api/v1/shift-types/`. No backfill of historical rows —
  dsndd is still under active development; old rows keep resolving via the
  legacy local FK indefinitely, new rows use the new fields. Existing
  queries/templates that assume the FK (~30 call sites, mostly
  `accounts/views.py`) need dual-shape handling (old-FK OR new-field).
- New `accounts/orbis_client.py::OrbisClient` — thin wrapper over the batch
  endpoint and the read endpoints above.
- Synchronous push from the specific live-schedule-edit views/tasks (not a
  blanket signal — drafts in `RosterShift` are never pushed, only published
  `Shift` rows). Local save always succeeds regardless of Orbis reachability;
  a failed push surfaces as a visible "not yet synced" warning, never a
  rollback.
- `accounts/reconcile.py::reconcile_shifts_with_orbis` — celery-beat task
  that diffs recently-touched local rows against Orbis and re-pushes drift,
  skipping anything Orbis reports as `leave_locked`.

**Exact trigger points for the push** (verified against dsndd's
`accounts/urls.py` — deliberately not a blanket `post_save` signal, since
that can't distinguish a draft edit from a live one the way this table
does). dsndd's `accounts/views.py` has duplicate function definitions for
two of these names — Python keeps the later one, and only the later copy is
live per `urls.py`; implementation must edit the live copy, not the
shadowed earlier one:

| Route | Live function | Writes | Orbis push |
|---|---|---|---|
| `roster/cell/update/` | `roster_cell_update` — line 4363 (line 2974 is dead/shadowed) | live `Shift` | synchronous, inline, right after local commit |
| `shift-details/<pk>/update/` | `shift_row_update` (line 820) | live `Shift` | synchronous, inline |
| `shift-details/<pk>/delete/` | `shift_row_delete` (line 847) | live `Shift` | synchronous, inline |
| CSV bulk import (`upload_shifts_big` view, line 1534) → `process_shift_csv_big` Celery task (`accounts/tasks.py:575`) | live `Shift`, bulk | chunked batch calls from inside the task, after local writes commit |
| `roster_shift_publish_current` (line 2196) → `publish_roster_shift_matrix` Celery task (`accounts/tasks.py:926`) | `RosterShift` → live `Shift` | chunked batch calls, same as CSV path — this is the moment a draft becomes the real schedule |
| `roster-shift-cell-update/` | `roster_shift_cell_update` — line 4431 (line 2795 dead/shadowed) | `RosterShift` only (draft, unpublished) | **no push** — not yet "the schedule," same reason Orbis's own `Roster` weekly pattern isn't pushed anywhere until a concrete `Shift` exists |

## Verification

**Orbis (Phase 1 — can run today):**
1. Batch endpoint: one request mixing create/update/delete/unknown-employee/unknown-or-inactive-shift-type/leave-locked all resolve to correct per-item statuses; good rows still commit.
2. Write a `LeaveApplication`-linked `Shift`, then POST a batch targeting that same (employee, date) → confirm it comes back `conflict` and the row is untouched.
3. `create_external_service_account` is idempotent (run twice → same row).
4. `_BaseAPI` accepts either `TIMELY_API_TOKEN` or `EXTERNAL_API_TOKEN`; rejects neither matching.
5. `TimelyShiftSerializer.leave_locked` appears in existing TimeLy responses without breaking them.
6. Missing or unresolvable `actor_employee_id` fails the whole request with 400; a valid one is what ends up on `Shift.added_by`/`updated_by`, not the service account.
7. Create/update/delete each write a matching `ShiftChangeLog` row with the correct `action_type`/before-after fields/`changed_by`; a delete is attributed to whoever called the delete, not whoever last saved the row.

**dsndd (Phase 2 — once started):**
1. Denormalized fields present and nullable; old rows untouched and still readable via the legacy `ShiftType` FK; new rows created post-cutover populate the new fields correctly.
2. Every call site touching the legacy FK (~30 sites, see above) handles both old and new rows correctly — spot-check OT calc totals for a month spanning the cutover date, schedule grid rendering, and roster CSV export for both an old and a new row.
3. Shift-type dropdown renders from cache; cold-cache fallback fetch works; cache refresh picks up a newly-added Orbis `ShiftType`.
4. Edit a live cell via `roster_cell_update` (4363) → confirm synchronous Orbis push; simulate an Orbis 500 → local save still succeeds, warning badge shows, `orbis_synced_at` stays null.
5. `roster_shift_cell_update` (4431, draft-only) does **not** call Orbis.
6. Bulk CSV import and publish-matrix tasks batch-push and correctly handle per-item conflicts/errors without failing the whole task.
7. `audit_employee_id_mapping` lists all null-`employee_id` active users before rollout.
8. Reconciliation task: seed a drifted unsynced row → detects and re-pushes it; seed a case where Orbis reports `leave_locked: true` differing from dsndd's local value → task skips and logs it, doesn't touch either side.

**End-to-end**: mark a test employee's cell "CL" in dsndd → Orbis shows a `Shift` with `category=LEAVE`, `leave_application_id` null. File and approve a real Orbis `LeaveApplication` for the same employee/date → `materialise_shifts_for_application` overwrites the row, `leave_application_id` now set → dsndd's next reconciliation run leaves that date alone.

Phase 1 smoke test from a terminal:

```bash
python manage.py create_external_service_account
TOKEN="<EXTERNAL_API_TOKEN from .env>"
curl -sS -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"actor_employee_id": "<employee_id of the person making this change>", "items": [{"client_ref": "t1", "employee_id": "<existing employee_id>", "date": "2026-08-01", "orbis_shift_type_id": <existing active ShiftType id>, "location": "MHK"}]}' \
  http://127.0.0.1:8000/api/v1/external/shifts/batch/
```
