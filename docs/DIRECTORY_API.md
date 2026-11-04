# Orbis Directory API — integration reference

**Audience:** engineers of applications that read SEBPO employee data (TimeLy first).
**Owner:** Orbis team (S. M. Rashel).
**Status:** built 2026-10-14; production rollout to be announced. Design notes:
`docs/DIRECTORY_API_PLAN.md`.
New to the Orbis API? Start with `docs/ORBIS_API_GUIDE.md`; this page is the full reference.

Orbis is SEBPO's system of record for employees. Microsoft Entra (Azure AD) is still the
way people **sign in**, but Entra's profile data (department, title, manager, office) is
no longer maintained — read it from Orbis instead.

The typical flow:

1. The user signs in to your application through Entra SSO, as today.
2. Your application calls **`employees/lookup/`** with the identifiers SSO gave it, and
   gets back the person's department, team, designation, office, supervisor and direct
   reports.
3. Optionally, a nightly job calls **`employees/?updated_since=`** to keep a local copy in
   sync.

---

## Quick start

```bash
TOKEN="<your application's Orbis token>"
BASE="https://orbis.sebpo.net/api/v1/directory"

# Who just signed in?  Send every identifier you have.
curl -sS -H "Authorization: Token $TOKEN" \
  "$BASE/employees/lookup/?oid=3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f&email=first.last@sebpo.com"

# Everyone active, 500 per page
curl -sS -H "Authorization: Token $TOKEN" "$BASE/employees/"

# Only what changed since your last sync
curl -sS -H "Authorization: Token $TOKEN" "$BASE/employees/?status=all&updated_since=2026-10-14T02:00:00+06:00"
```

---

## Authentication

```
Authorization: Token orb_…
```

- Each application gets **its own token**, issued by the Orbis team and shown to them
  once. Store it in your environment / secrets manager; never commit it.
- A token carries **scopes**. Every endpoint on this page needs `directory:read`.
- Missing, unknown or revoked token → **401**. Valid token without the scope → **403**.
- To rotate, ask the Orbis team; the old token stops working the moment a new one is
  issued, so swap it in promptly.
- Every call is recorded in Orbis's access log under your application's name.

---

## Endpoints

All under `https://orbis.sebpo.net/api/v1/directory/`, `GET` only, JSON.

| Path | Returns |
|---|---|
| `employees/lookup/?oid=&email=&employee_id=` | One person (detail record + `matched_by`) |
| `employees/?status=&department=&team=&updated_since=&page=&page_size=` | Paged employee records |
| `employees/<employee_id>/` | One person (detail record) |
| `employees/<employee_id>/direct-reports/?status=` | Array of employee records |
| `employees/<employee_id>/scope/?status=&page=&page_size=` | Paged employee records of everyone this person manages, each with `scope` |
| `departments/` | Array of departments |
| `designations/` | Array of designations |
| `teams/` | Array of teams (with their department) |
| `offices/` | Array of offices |

### Employee lookup — after sign-in

```
GET employees/lookup/?oid=<Entra object id>&email=<email>&employee_id=<employee id>
```

Supply **any combination, at least one — and send every key you have.** Orbis tries them in
this order of trust and answers with the first that matches:

1. `oid` — the Entra **object id** (`oid` claim in the id token, `id` from Graph `/me`).
   Never changes, even if the person's email does.
2. `email` — case-insensitive.
3. `employee_id`.

Orbis is still collecting object ids (it records each person's at their next Orbis sign-in,
and will import the rest from Entra), so an `oid` on its own may not match yet. Sending
`oid` **and** `email` finds the person by email today and moves to `oid` automatically once
Orbis has it — no change on your side.

| Status | Meaning |
|---|---|
| 200 | The detail record (below) plus `"matched_by": "oid" \| "email" \| "employee_id"` |
| 400 | No key supplied, or `oid` is not a UUID |
| 404 | No employee matches any key |
| 409 | The keys identify **different** employees, e.g. the email is one person's and the employee id another's. Body: `{"detail": …, "matches": {"email": "539", "employee_id": "656"}}`. Orbis never guesses — treat this as a data problem to report. |

**Inactive employees are returned** (`"status": "inactive"`). Whether a former employee may
use your application is your decision.

### Employee list

```
GET employees/
```

| Parameter | Default | Notes |
|---|---|---|
| `status` | `active` | `active`, `inactive` or `all` |
| `department` | — | department id (from `departments/`) |
| `team` | — | team id (from `teams/`) |
| `updated_since` | — | ISO 8601 datetime (`2026-10-14T02:00:00+06:00`) or date (`2026-10-14`). Without an offset it is read as Asia/Dhaka time. Returns people whose record changed **after** that moment. |
| `page` | 1 | |
| `page_size` | 500 | maximum 1000 |

Ordered by `employee_id`. Response:

```json
{
  "count": 1472,
  "next": "https://orbis.sebpo.net/api/v1/directory/employees/?page=2",
  "previous": null,
  "as_of": "2026-10-14T02:00:00.123456+06:00",
  "results": [ { …employee record… } ]
}
```

Follow `next` until it is `null`.

#### Keeping a local copy in sync

1. First run: fetch `employees/?status=all` (every page). Store `as_of` from the first page.
2. Each later run: fetch `employees/?status=all&updated_since=<stored as_of>` (every page),
   upsert the results by `employee_id`, store the new `as_of`.
3. Re-fetch the four reference lists every run — they are small, and a rename doesn't mark
   employees as changed.

Use `status=all`: a person leaving shows up as a change to `"status": "inactive"`, and
`status=active` would hide it.

Two things change **without** the affected person's record being marked as changed:

- a manager's `direct_reports_count`, when someone moves into or out of their team;
- the nested `supervisor` name/email, when the supervisor's own details change.

So build reporting lines from each record's `supervisor.employee_id` against your synced
set, rather than trusting those nested copies.

### Employee detail

```
GET employees/<employee_id>/
```

The employee record plus `direct_reports`: the people who report to this employee and are
still active, `[{employee_id, email, full_name, designation, status}]`. Works for inactive
employees too. 404 if unknown.

### Direct reports

```
GET employees/<employee_id>/direct-reports/?status=active|inactive|all
```

Full employee records of the people who report to this employee (default: active only).
Unpaginated. 404 if the manager is unknown.

### Managed scope — everyone a manager is responsible for

```
GET employees/<employee_id>/scope/?status=active|inactive|all&page=&page_size=
```

Everyone this employee manages **in Orbis's own terms** — the same people Orbis lets them
see and approve for (leave, roster, timesheets):

- their whole reports-to downline, at any depth (their reports, their reports' reports, …);
- everyone in a department they head or co-manage;
- everyone in a team they were granted.

The employee themselves is not included. Paged like `employees/` (with `as_of`); 404 if the
employee is unknown, an empty `results` if they manage nobody.

Each record carries one extra field:

```json
"scope": {"via": ["reports_to", "department"], "depth": 2}
```

| Field | Notes |
|---|---|
| `via` | Every reason the person is in scope, in this order: `reports_to`, `department`, `team`. |
| `depth` | Distance in the reporting chain — `1` is a direct report, `2` their report, … `null` when the person is in scope only through a department or team grant. |

`status` defaults to `active`, which is exactly Orbis's scope. `inactive`/`all` also include
people who have left, following the reporting chain through them.

This list is computed from the current org structure on every call; it has no
`updated_since` — re-fetch it when you need it.

### Reference lists

Unpaginated arrays, ordered by name. Inactive rows are included (so an old employee's
department still resolves); add `?is_active=true` for current ones only.

```json
// departments/
{"id": 17, "name": "General BPO", "is_active": true,
 "head_of_department": {"employee_id": "539", "email": "…", "full_name": "…"}}   // or null

// designations/
{"id": 18, "name": "Assistant Program Manager", "is_active": true}

// teams/
{"id": 184, "name": "AdOps Managers", "is_active": true, "department_id": 8, "department_name": "Digital Advertising"}

// offices/
{"id": 3, "name": "RAOWA", "code": "RAOWA", "is_active": true}
```

An office's `code` is one of `MBD`, `MHK`, `RAOWA` or `OTHER` — use it rather than the
name, which may be a full street address.

---

## The employee record

```json
{
  "employee_id": "917",
  "entra_object_id": "3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f",
  "email": "first.last@sebpo.com",
  "full_name": "First Last",
  "first_name": "First",
  "last_name": "Last",
  "status": "active",
  "joining_date": "2014-10-18",
  "last_working_date": null,
  "department": {"id": 8, "name": "Digital Advertising"},
  "team": {"id": 184, "name": "AdOps Managers", "department_id": 8},
  "designation": {"id": 18, "name": "Assistant Program Manager"},
  "office": {"id": 3, "code": "RAOWA", "name": "RAOWA"},
  "work_location": "WFO",
  "supervisor": {"employee_id": "398", "email": "…", "full_name": "…", "status": "active"},
  "direct_reports_count": 13,
  "updated_at": "2026-10-14T16:26:44.074586+06:00"
}
```

| Field | Notes |
|---|---|
| `employee_id` | The stable HR employee id. Every record has one. |
| `entra_object_id` | `null` until Orbis has recorded it. |
| `status` | `active` or `inactive`. Someone serving notice is `active`, with `last_working_date` set in the future; they become `inactive` the day after it. |
| `department`, `team`, `designation`, `office` | `null` when not set. For an inactive employee: as on their last day. |
| `work_location` | `WFO`, `WFH`, `HYB` or `null`. |
| `supervisor` | Who this person reports to, or `null`. May itself be `inactive` if HR hasn't reassigned them yet. |
| `direct_reports_count` | Active people reporting to this person. |
| `updated_at` | When this record last changed — the value `updated_since` compares against. |

Dates are `YYYY-MM-DD`; timestamps are ISO 8601 with offset. The record carries directory
data only — no salary, personal, contact or identity-document fields. Ask the Orbis team if
your application needs another field.

---

## Errors

| Status | Body | When |
|---|---|---|
| 400 | `{"<param>": "<message>"}` | Bad parameter, or one the endpoint doesn't accept. Unknown parameters are rejected, never silently ignored — e.g. `employees/?email=…` is a 400 pointing you to `employees/lookup/?email=…`. `format` is accepted everywhere. |
| 401 | `{"detail": …}` | Missing / unknown / revoked token |
| 403 | `{"detail": …}` | Token lacks `directory:read` |
| 404 | `{"detail": …}` | Unknown employee |
| 409 | `{"detail": …, "matches": {…}}` | Lookup keys name different employees |
