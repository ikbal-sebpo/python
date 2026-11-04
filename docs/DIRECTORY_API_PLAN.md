# Directory API — Orbis as the employee data source for other applications

Status: **phases 1–3 built 2026-10-14** (not yet deployed). Phase 1 (`b1e9015`): API
clients + scopes, the four reference lists, `CustomUser.entra_object_id` captured at SSO
login, the `CustomUser.directory_updated_at` change-feed column. Phase 2 (`01c39ca`): the
employee list (+ `updated_since`), lookup, detail, direct-reports and managed-scope
endpoints, and the consumer reference `docs/DIRECTORY_API.md`. Phase 3: every roster
endpoint accepts `ApiClient` tokens by scope (the `.env` tokens still work), the
all-employee date-range roster mode, and `import_entra_object_ids`. The token switch-over
itself (§12) and phase 4 remain. Decisions in
§3 were taken with the user the same day; the open items in §11 were answered the same day.

## 1. Why

SEBPO's applications authenticate with Microsoft Entra (Azure AD) SSO and, after sign-in,
read employee data (department, title, manager, office) from Entra. Entra was fed by the
Oracle ERP. Oracle has been discontinued and Orbis replaces it — HR onboards and maintains
every employee in Orbis now — but **Entra is still syncing from the frozen Oracle data**, so
anything reading Entra sees stale titles, departments and managers.

The fix: **SSO stays the authentication layer for every app; employee data comes from
Orbis.** After an app authenticates a user through Entra, it asks Orbis who that person is.

Required resources:

1. Employee list, active and inactive — with **supervisor** (who I report to) and
   **direct reports** (who reports to me)
2. Department list
3. Designation list
4. Office list
5. Team list with department mapping
6. Roster list

## 2. What exists today (survey, 2026-10-14)

| Piece | Where | State |
|---|---|---|
| Roster — one day, everyone | `GET /api/v1/roster/?date=` (`roster/api/timely.py::DailyRosterAPI`) | Live, used by TimeLy and rnc.sebpo.net (= dsndd) |
| Roster — one employee, range ≤ 92 days | `GET /api/v1/roster/<employee_id>/?start_date=&end_date=` | Live |
| Shift types, holidays | `GET /api/v1/shift-types/`, `/api/v1/holidays/?year=` | Live |
| Employee list | `GET /api/v1/employees/?is_active=` (`roster/api/timely.py::EmployeeListAPI`) | Live; callable with either token, so TimeLy and possibly dsndd. Department/team/designation as **name strings, no ids**; no supervisor / direct reports; unpaginated; no change feed |
| Shift write API | `POST /api/v1/external/shifts/batch/` (dsndd, served at rnc.sebpo.net) | Live |
| Auth | `TimelyTokenAuthentication` (`TIMELY_API_TOKEN`), `ExternalTokenAuthentication` (`EXTERNAL_API_TOKEN`) | Two shared secrets in `.env`. A new consumer = an env edit + deploy; a leak can't be revoked per app; the access log can't tell consumers apart |
| Access log | `audit` — `api_` routes are `DataClass.API` | Logs API calls already; a non-`CustomUser` principal becomes a label only (`audit/capture.py::actor_fields`) |
| Supervisor | `employees.JobInfo.reports_to` (reverse: `direct_reports`) | Present |
| Org reference tables | `accounts.Department`, `Designation`, `Team` (FK `department`), `JobLocation`, `WorkLocation` | Present, all with `is_active` |
| Entra object id | Graph `/me` already `$select`s `id` (`core/settings.py::OIDC_OP_USER_ENDPOINT`) | **Not stored** — discarded on every login |
| Change tracking | `JobInfo.updated_at` exists | **`CustomUser` has no `updated_at`** |

Data quality found in the survey (dev DB = prod restore 2026-10-06):

- 1,473 active / 56 inactive users. 56 looks low — pre-Orbis leavers were never loaded.
- 31 active users with no `reports_to`; 4 reporting to an inactive manager (all General
  BPO, manager left 2026-09-25).
- `JobLocation` has a duplicate MBD: `MBD` and `Monem Business District, Level-7 Dhaka 1205
  BD` (the latter an Entra free-text import).
- 1 active user with no `employee_id`.

**Orbis's own SSO login does not re-pull from Entra.** `accounts/auth_backends.py` enriches
from Graph once, on JIT creation only; `update_user` is a no-op. Bulk-onboarded users link by
email and are never enriched. So Orbis is not itself contaminated by the stale Entra data —
except for a JIT-created user nobody pre-created (an edge case HR's onboarding flow avoids).

## 3. Decisions (2026-10-14)

| # | Decision |
|---|---|
| D1 | **Lookup key order: Entra object id → email → employee id.** A consumer sends every key it has; Orbis returns the first match. |
| D2 | Until the object-id backfill (§6.2) runs, lookups fall back to email / employee id. The backfill is **not a launch blocker**. |
| D3 | An inactive employee keeps the department, team, designation and supervisor they had on their last day (Orbis never clears these at deactivation — locked in by a test). |
| D4 | **TimeLy is the first consumer.** Its main mode is a nightly sync, so the change feed (`updated_since`) is phase 1, not later. |
| D5 | **rnc.sebpo.net is dsndd** — one app, not two. There are exactly two consumers today and **each already has its own key**: TimeLy → `TIMELY_API_TOKEN`, dsndd/rnc → `EXTERNAL_API_TOKEN`. No token is shared between apps, so retiring either env token depends on one team only. (Corrected 2026-10-14 — an earlier answer had rnc on TimeLy's token.) |
| D6 | **Per-consumer API clients** in the database (§4) replace adding one `.env` token per app. The two existing env tokens keep working until their consumers migrate. |
| D7 | The existing `/api/v1/employees/` is **frozen, not changed** — TimeLy depends on its shape. New endpoints live under `/api/v1/directory/`. |
| D8 | **Directory data only.** No salary, tax, NID, personal, family, contact, address, bank or leave-balance fields. A field is added only when a named consumer asks for it, and recorded in §11. |
| D9 | **Roster range endpoint is confirmed**: TimeLy and dsndd/rnc both call `roster/?date=` once per day today. |
| D10 | **Object ids fill in at SSO login from phase 1**, independently of IT's Entra export; the export backfill (§6.2) runs whenever IT delivers it. |
| D11 | **TimeLy needs no fields beyond §5.1** — the record shape is final for phase 2. |
| D12 | **dsndd/rnc moves to an `ApiClient` too**, with `roster:read` + `roster:write` scopes; `EXTERNAL_API_TOKEN` is retired once it has moved (§4.4). |
| D13 | **Roster history in Orbis starts 2026-01-01** (rosters were backfilled from that date). The roster APIs don't refuse earlier dates, they just have almost nothing there (3 stray rows from the week of 2025-12-29 on the 2026-10-06 prod restore); consumers backfilling their own history start at 2026-01-01. |

## 4. API clients and scopes

### 4.1 Model — `accounts.ApiClient`

| Field | Notes |
|---|---|
| `name` | unique, e.g. `timely`, `dsndd` |
| `token_prefix` | first 8 chars of the token, shown in the admin so a leaked token can be identified |
| `token_hash` | SHA-256 hex of the full token, unique + indexed. Tokens are 32+ random bytes, so a plain hash is sufficient (no per-row salt needed; lookup stays an indexed equality) |
| `scopes` | JSON list, from `directory:read`, `roster:read`, `roster:write` |
| `is_active` | revoke without deleting |
| `last_used_at` | updated at most once per minute per client (avoid a write per request) |
| `service_account` | optional FK `CustomUser`, PROTECT — the persisted principal a **write** client acts as (§4.4). Null for read-only clients |
| `created_by` | FK `CustomUser`, SET_NULL → **migration needs the collation CONVERT step** (`employees/migrations/0008_designationhistory.py::_match_existing_collation`, `atomic = False`) |
| `created_at`, `note` | |

Token format: `orb_<43 url-safe chars>` (`secrets.token_urlsafe(32)`). The plaintext exists
only in the response of the admin "create / rotate token" action — shown once, never stored
— the same one-time-secret posture as bulk onboarding's local passwords.

### 4.2 Authentication

`accounts/api/auth.py::ApiClientTokenAuthentication` — `Authorization: Token <token>`, same
header as today. Hashes the presented token, looks up an active `ApiClient`, returns
`(ApiClientPrincipal(client), client)`. Like the two existing authenticators it returns
`None` (not an error) on a mismatch, so all three compose on shared endpoints.
`ApiClientPrincipal.__str__` is `api:<name>`, which `audit.capture.actor_fields` records as
the access-log actor label — so `/audit/access-log/` shows which app made each call. A client
with a `service_account` returns that `CustomUser` instead (§4.4); every `ApiClient` request
also gets `detail.api_client = <name>` via `audit.capture.note`, so the app is visible either
way.

### 4.3 Authorization

`HasScope("directory:read")` / `HasScope("roster:read")` DRF permissions. The existing
TimeLy and external principals are treated as holding `directory:read` + `roster:read` on
the endpoints they already use, so nothing breaks before migration.

### 4.4 dsndd (write client) — D12

The shift write endpoints (`roster/api/shift_writes.py::ExternalShiftBatchAPI`,
`report_shifts.py::ExternalReportShiftAPI`) need `request.user` to be a real `CustomUser`
because `Shift.added_by`/`updated_by` are FKs to it (attribution itself comes from the
request's `actor_employee_id`). So dsndd's `ApiClient` row links `service_account` to the
existing `external-integrations@sebpo.internal` user (`create_external_service_account`), and
`ApiClientTokenAuthentication` returns that user for it. The write views accept
`ApiClientTokenAuthentication` alongside `ExternalTokenAuthentication` and require
`roster:write`; a client with `roster:write` but no `service_account` is refused at
authentication (misconfiguration, 401 with a clear message — same posture as today's missing
service account).

### 4.5 Admin

Django admin page for `ApiClient`: list (name, prefix, scopes, active, last used), "Generate
new token" action (rotates; old token dies immediately), `is_active` toggle. Superusers and
User Admins only.

## 5. Endpoints

All under `/api/v1/directory/`, read-only, JSON, `directory:read` scope. Code in a new
`accounts/api/` package (`auth.py`, `serializers.py`, `views.py`, `urls.py`, `tests.py`),
mirroring `roster/api/`.

### 5.1 Employee record

```json
{
  "employee_id": "917",
  "entra_object_id": "3f2b…-…",
  "email": "first.last@sebpo.com",
  "full_name": "First Last",
  "first_name": "First",
  "last_name": "Last",
  "status": "active",
  "joining_date": "2024-02-01",
  "last_working_date": null,
  "department": {"id": 3, "name": "General BPO"},
  "team": {"id": 41, "name": "…", "department_id": 3},
  "designation": {"id": 12, "name": "Senior Executive"},
  "office": {"id": 2, "code": "MHK", "name": "MHK"},
  "work_location": "WFO",
  "supervisor": {"employee_id": "677", "email": "…", "full_name": "…", "status": "active"},
  "direct_reports_count": 9,
  "updated_at": "2026-10-14T09:12:00+06:00"
}
```

- `status`: `active` = `is_active=True` (includes someone serving notice — `last_working_date`
  in the future tells the consumer when they leave); `inactive` = `is_active=False`.
- `office.code` uses `accounts.locations.office_bucket` (`MBD` / `MHK` / `RAOWA` / `OTHER`).
- `work_location`: `WFO` / `WFH` / `HYB` / null — same mapping as the TimeLy serializer.
- `supervisor` is `JobInfo.reports_to`, null when unset. For an inactive employee it is the
  supervisor on their last day (D3), and `supervisor.status` may itself be `inactive`.
- `direct_reports_count` counts **active** direct reports.
- Users with no `employee_id` (service accounts, e.g. the external-integrations account)
  are never returned.

### 5.2 Endpoint list

| Method | Path | Purpose |
|---|---|---|
| GET | `employees/` | Paged list. Filters: `status=active\|inactive\|all` (default `active`), `department=<id>`, `team=<id>`, `updated_since=<ISO datetime>`. Ordered by `employee_id`. |
| GET | `employees/lookup/?oid=&email=&employee_id=` | Resolve one person — the call made after SSO login (§5.3). |
| GET | `employees/<employee_id>/` | One record plus a `direct_reports` array (each `{employee_id, email, full_name, designation, status}`). |
| GET | `employees/<employee_id>/direct-reports/?status=` | Direct reports only (default active), as **full employee records**, unpaginated. |
| GET | `employees/<employee_id>/scope/?status=` | **Added 2026-10-14 at the user's request.** Everyone in the employee's Orbis managed scope — reports-to downline at any depth ∪ departments they head/co-manage ∪ teams granted to them (`accounts.permissions.managed_scope_pks`, verified equal on dev for three managers, up to 859 people). Paged; each record adds `scope: {via: [...], depth}`. The BFS moved into `accounts.permissions.reportee_depths` so the endpoint and Orbis's permission checks share one traversal. `status=active` (default) is Orbis's active-only scope; `inactive`/`all` use the `include_separated` traversal. |
| GET | `departments/` | `{id, name, is_active, head_of_department: {employee_id, email, full_name} \| null}` |
| GET | `designations/` | `{id, name, is_active}` |
| GET | `teams/` | `{id, name, is_active, department_id, department_name}` |
| GET | `offices/` | `{id, name, code, is_active}` from `JobLocation` |

**Unknown query parameters are a 400 (added 2026-10-14 after a real mix-up):** `employees/?email=x/scope/` was silently treated as an unfiltered list and returned the whole company. Each directory view now declares `allowed_params`; anything else is rejected naming the parameter (a lookup key gets a pointer to `employees/lookup/`). Checked after authentication. The roster endpoints keep their lenient legacy behaviour so existing consumers' extra parameters can't start failing.

Reference lists are small (16 / 63 / 95 / 4 rows), unpaginated, and include inactive rows
with `is_active` so a consumer can resolve an old employee's department. `?is_active=true`
narrows.

Pagination on `employees/`: page-number, default 500, `?page_size=` up to 1000, response
`{count, next, previous, results, as_of}`. One nightly full sync is 3 requests at today's
size (measured on the dev copy of prod: 1,000 records in ~0.2 s, 4 queries regardless of
page size). **`as_of` was added during the build:** the server time taken before the query
runs, which the consumer stores and sends back as its next `updated_since` — so a change
landing mid-sync is picked up next time instead of falling between two clocks.

### 5.3 Lookup semantics

`GET employees/lookup/?oid=…&email=…&employee_id=…` — any combination, at least one.

1. Try each supplied key in order **oid → email (case-insensitive) → employee_id**.
2. Return the first match with `"matched_by": "oid" | "email" | "employee_id"` added to the
   **detail** record (the §5.1 record plus `direct_reports`) — decided during the build, so
   the post-login call is one request rather than lookup + detail.
3. If two supplied keys resolve to **different** people → `409 Conflict` naming the keys.
   Never pick one silently.
4. No match on any key → `404`.
5. Inactive employees are returned (with `status: "inactive"`) — the consumer decides
   whether to let them in. Orbis does not make the access decision for another app.

Consumers should **send every key they have**. Sending only `oid` before the backfill (§6.2)
would 404 for most people; `oid` + `email` finds them by email today and switches to `oid`
automatically once Orbis has it, with no consumer change. `matched_by` (and the access log)
show how many lookups still fall back.

Consumers **cannot write** object ids. Only SSO login and the Entra import set them (§6), so
a misbehaving consumer can't mislink a person.

### 5.4 Change feed (`updated_since`)

- New `CustomUser.directory_updated_at` (indexed; existing rows backfilled to the
  migration time, so the first `updated_since` sync after deploy is a full one).
  **Built as a tracked-field stamp, not `auto_now` (revised during the phase 1 build):**
  `auto_now` would move on every login (`last_login` is a `CustomUser` column, so nearly
  every employee would "change" daily) and is silently skipped by
  `save(update_fields=[...])` unless the caller lists it. Instead `CustomUser.save()`
  compares `CustomUser.DIRECTORY_FIELDS` against the values loaded from the database and
  bumps the stamp — adding it to `update_fields` itself — only when one of them changed.
  Hence the name: it means "a field this API exposes changed", not "the row was saved".
- `employees/?updated_since=T` returns users where `CustomUser.directory_updated_at > T`
  **or** `JobInfo.updated_at > T` (a supervisor change lives on `JobInfo`). The record's
  `updated_at` is the greater of the two.
- Known gaps, documented for consumers rather than engineered around:
  - A manager's `direct_reports_count` changes when a report moves, but the manager's row
    isn't touched. A syncing consumer should build direct reports from the synced
    `supervisor` fields, not from the count.
  - `QuerySet.update()` / `bulk_update()` bypass `save()`. Every existing path that
    bulk-writes a directory field sets the stamp via `accounts.models.directory_stamp()`
    (audited in phase 1): `offboarding/services.py::deactivate_past_lwd_users`,
    `offboarding/middleware.py` (LWD fast path), `offboarding/signals.py` (LWD on accept),
    `payroll/upload.py` (team go-live), `employees/upload.py` (legacy bulk upload),
    `merge_reference`, `consolidate_job_locations`, `set_department_office`,
    `import_employee_joining_dates`. **A new bulk write to a directory field must do the
    same.** `JobInfo.updated_at` is `auto_now`; its only queryset `update()`
    (`payroll/upload.py`, `resource_status`) isn't a directory field.
  - Likewise a supervisor's own name/email change doesn't touch their reports' rows, so the
    nested `supervisor` object can lag; resolve it by `supervisor.employee_id` from the
    synced set.
  - Reference-list renames don't touch employees; consumers re-fetch the four small lists
    on every sync.

### 5.5 Roster

No change to the existing roster endpoints' shape (`docs/TIMELY_API.md`). They gain
`ApiClientTokenAuthentication` + `roster:read` alongside the current authenticators, so
TimeLy and dsndd can each move to their `ApiClient` token without any other change.

**Range endpoint (D9):** TimeLy and dsndd/rnc both loop `roster/?date=` once per day. Built
as `GET /api/v1/roster/?start_date=&end_date=` (all employees, at most 31 days inclusive,
paged 1000 by default / 5000 max, ordered by date then employee id, same
`TimelyShiftSerializer` record) — **`start_date`/`end_date`, not the `from`/`to` this plan
first named**, to match the per-employee range endpoint and dsndd's report-shifts, which
already use those names. `?date=` keeps its bare-array response; supplying `date` together
with `start_date`/`end_date` is a 400. dsndd also has `/api/v1/external/report-shifts/`, an
older all-employee range report in its own shape. History starts 2026-01-01 (D13) — a consumer's
first sync walks forward from there in 31-day windows.

## 6. Entra object id

### 6.1 Capture at SSO login

- New `CustomUser.entra_object_id` — `CharField(36)`, `null=True`, `unique=True` (MySQL
  allows many NULLs under a unique index).
- `accounts/auth_backends.py`: on **every** SSO login (`update_user` as well as
  `create_user`), set `entra_object_id` from Graph `id` (or the id_token `oid` claim) when
  it is empty. This is an identifier, not HR data — it does not reopen the "Orbis is the
  system of record" rule `update_user` exists to protect.
- If the incoming id already belongs to a **different** user: don't write it, log a
  warning, carry on with the login.
- If the user already has a **different** id: don't overwrite, log a warning (an Entra
  account was deleted and recreated — a human decides).
- `filter_users_by_claims` matches by `entra_object_id` first, then email. This also makes
  Orbis login survive an email rename in Entra.

### 6.2 Backfill from an Entra export

`manage.py import_entra_object_ids <users.csv> [--apply]` — the CSV IT downloads from the
Entra admin centre (Users → Download users; columns include Object ID, User principal name,
Mail, Employee ID).

- Matches each row to Orbis by `mail`, then UPN (case-insensitive). **Not** by Entra's
  `employeeId` — that is the stale Oracle field.
- Fills only empty `entra_object_id`; never overwrites.
- Dry-run by default; prints matched / unmatched / already-set / conflicts. Where Orbis's
  `employee_id` disagrees with Entra's `employeeId`, the row is reported, not acted on.
- Idempotent; safe to re-run with a newer export.

No new Entra permission is needed. Ask IT for the export now so it's ready at deploy.

### 6.3 Optional later: nightly Graph sync

A beat job reading Graph `/users` with app-only credentials. Needs an **Application**
`User.Read.All` permission with admin consent (Orbis has only delegated permissions today).
Only worth it if new joiners regularly hit other apps before their first Orbis login —
6.1 + bulk onboarding probably cover that. Not planned.

## 7. Access log and privacy

- New routes are named `api_directory_*`, which the registry's `api_` prefix already
  classifies as `DataClass.API` — the registry guard test stays green.
- Views call `audit.capture.note(request, rows=<n>)` for lists and
  `note(request, subject=<user>)` for single-employee reads, so `/audit/access-log/` can
  answer "which app read this person's record".
- The record (§5.1) has no field the access log's money guard would trip on.

## 8. Phasing

| Phase | Scope | Consumer outcome |
|---|---|---|
| **1** | `ApiClient` model + admin + authenticator + scopes; `CustomUser.directory_updated_at` and `entra_object_id` (one migration, `accounts/0014`); SSO capture (§6.1); the four reference lists; `docs/PLAN.md` pointer | `ApiClient` tokens issued to TimeLy and dsndd (old env tokens keep working); reference lists usable |
| **2** | Employee list (+ `updated_since`), lookup, detail, direct reports; `docs/DIRECTORY_API.md` consumer reference (like `docs/TIMELY_API.md`) | TimeLy moves its nightly sync and post-login lookup to `/api/v1/directory/` |
| **3** | Roster gains `roster:read` via `ApiClient`; range roster endpoint (D9); TimeLy and dsndd switch to their `ApiClient` tokens (§11); `import_entra_object_ids`, run on prod when IT's export arrives (not blocking — login capture runs from phase 1) | TimeLy and dsndd fetch a range in one call; lookups increasingly match on `oid`; `TIMELY_API_TOKEN` retired once TimeLy has moved, `EXTERNAL_API_TOKEN` once dsndd has |
| **4** | Data hygiene: merge the duplicate MBD `JobLocation` (`manage.py merge_reference location`) and the duplicate "Human Resource" / "Human Resources" `Department` (`merge_reference department`); HR fills in the missing `head_of_department`s (most departments have none); HR list of the 31 unlinked + 4 inactive-manager employees; an offboarding warning listing a leaver's still-active direct reports | Cleaner supervisor / office data for every consumer |

Later, separate decision: Entra keeps serving Oracle data to things Orbis doesn't control
(Outlook address book, Teams org chart). The durable fix is IT stopping the Oracle → Entra
sync, and eventually Orbis writing title / department / manager back to Entra through Graph
(needs `User.ReadWrite.All` application permission).

## 9. Tests

- Auth: valid / revoked / unknown token; scope enforcement; the three authenticators
  compose (TimeLy, external and `ApiClient` tokens all work on roster endpoints).
- Lookup: each key alone; key order; `matched_by`; conflicting keys → 409; inactive
  returned; case-insensitive email.
- Records: supervisor + direct reports; inactive employee keeps last-day department and
  supervisor (D3); users without `employee_id` excluded; no sensitive field present
  (serializer field allow-list test).
- `updated_since`: a `JobInfo.reports_to` change surfaces the employee; a `CustomUser` org
  change surfaces; deactivation surfaces.
- SSO: object id stored on returning login; not overwritten; conflict logged, login still
  succeeds; login matches by object id after an email change.
- Import command: dry-run writes nothing; never overwrites; reports Entra `employeeId`
  mismatches.
- Query count constant in page size (`assertNumQueries`).
- Access log: every new route classified as `API`.

## 10. Files

| File | Change |
|---|---|
| `accounts/models.py` | `ApiClient`; `CustomUser.directory_updated_at`, `entra_object_id`; `directory_stamp()` |
| `accounts/migrations/00xx_…` | the above; collation CONVERT for `ApiClient`, `atomic = False` |
| `accounts/admin.py` | `ApiClient` admin + token action |
| `accounts/api/` (new) | `auth.py`, `permissions.py`, `serializers.py`, `views.py`, `urls.py`, `tests.py` |
| `accounts/auth_backends.py` | object-id capture + match-by-object-id |
| `accounts/management/commands/import_entra_object_ids.py` | backfill |
| `core/urls.py` | `path("api/v1/directory/", include("accounts.api.urls"))` |
| `roster/api/timely.py` | add `ApiClientTokenAuthentication` + scope check; range endpoint |
| `roster/api/shift_writes.py`, `report_shifts.py` | add `ApiClientTokenAuthentication` + `roster:write` |
| `docs/PLAN.md` | pointer to this plan |
| `docs/DIRECTORY_API.md` (phase 2) | consumer-facing reference |
| `docs/ORBIS_API_GUIDE.md` | developer guide for any consuming app (token, sign-in lookup, sync, roster) |
| `accounts/api/guide.py`, `templates/accounts/api_guide.html` | **`/developers/api/`** (added 2026-10-14): the guide plus the Directory and Roster references as tabs, rendered from the `docs/` Markdown on each request (cached by file mtime) with `markdown-it-py` — already pinned in `requirements.txt` (it came in with `rich`), now used directly; raw HTML in the Markdown is disabled. Any signed-in user can open it; no sidebar entry — the token-issued admin page links to it. |

No new library: DRF is already installed.

## 11. Open items

Answered 2026-10-14:

1. ~~Does rnc use `TIMELY_API_TOKEN`?~~ No — rnc.sebpo.net is dsndd, on `EXTERNAL_API_TOKEN` → D5.
2. ~~Do consumers loop the daily roster?~~ Yes, both → D9, range endpoint in phase 3.
3. ~~Entra export~~ The user is requesting it from IT; login capture fills ids meanwhile → D10.
4. ~~Extra TimeLy fields?~~ None → D11.
5. ~~dsndd?~~ Moves to an `ApiClient` with a write scope → D12, §4.4.
6. ~~Roster history start~~ 2026-01-01 → D13.

Still open:

- Token switch-over dates with the TimeLy and dsndd teams. Not a blocker: old and new tokens
  work side by side, and each env token belongs to one app, so each retires independently. Before
  removing one, confirm in the access log that nothing has called with it for a few days.
  Sequence per app: new token issued → they swap it into their `.env` → the env token is
  removed from Orbis's `.env` and its authenticator deleted.

## 12. Deploying phase 1

1. Deploy as usual — `accounts/0014_directory_api` adds two `CustomUser` columns and the
   `accounts_apiclient` table (non-atomic, with the collation CONVERT step; verified on the
   dev MySQL database). No `.env` change, nothing to seed.
2. Object ids start filling in at the next SSO login of each user — nothing to run.
3. When a consumer is ready: Django admin → Accounts → API clients → add `timely` with
   `directory:read`, save, then select it and run **Generate new token**. The token is
   shown once; send it over a secure channel. (Superusers and User Admins only.)
4. Nothing existing changes: the roster endpoints still take only the two `.env` tokens
   until phase 3.

## 13. Switching a consumer to its own token (phase 3)

Every roster endpoint now takes `ApiClientTokenAuthentication` first, with a scope check
(`accounts.api.auth.HasApiScopeOrEnvToken`): `roster:read` for roster / shift-types /
holidays / report-shifts, `roster:write` for the shift batch write, `directory:read` for the
frozen `/api/v1/employees/`. A request on an `.env` token keeps today's access. So each
consumer switches on its own schedule:

| Client | Scopes | Service account |
|---|---|---|
| `timely` | `roster:read`, `directory:read` | — |
| `dsndd` (rnc.sebpo.net) | `roster:read`, `roster:write`, `directory:read` | `external-integrations@sebpo.internal` |

1. In the production admin create the client with the scopes above and **Generate new
   token**; send it to the team securely.
2. They swap the value in their `.env` — no code change.
3. Check `/audit/access-log/`: their calls now show as `api:timely`, or with
   `api_client: dsndd` in the detail (dsndd acts as its service account).
4. When the old label (`timely-service-account`, or the external-integrations account
   *without* `api_client`) has been quiet for a few days, remove that token from `.env`,
   then delete `TimelyTokenAuthentication` / `ExternalTokenAuthentication` and the `.env`
   branch of `HasApiScopeOrEnvToken` in code. Each token belongs to one app (D5), so the two
   retire independently.

Object-id backfill, when IT delivers the Entra export:

    manage.py import_entra_object_ids exportUsers.csv --report problems.csv   # dry run
    manage.py import_entra_object_ids exportUsers.csv --apply

