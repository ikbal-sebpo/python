# Orbis API — developer guide

- **For:** developers of SEBPO applications that need employee or roster data.
- **Owner:** Orbis team — S. M. Rashel (`smrashel@sebpo.com`).
- **Base URL:** `https://orbis.sebpo.net/api/v1/`
- **Read it online:** `https://orbis.sebpo.net/developers/api/` (sign in with your SEBPO account).

This guide is the starting point. Two references carry the full detail:
`docs/DIRECTORY_API.md` (employee data) and `docs/TIMELY_API.md` (roster).

---

## 1. How it fits together

Orbis is SEBPO's system of record for employees and rosters. HR onboards and maintains
everyone in Orbis.

- **Sign-in stays on Microsoft Entra (Azure AD) SSO.** Keep authenticating users exactly
  as you do today.
- **Employee data comes from Orbis, not from Entra.** Entra's profile fields (department,
  job title, manager, office) are still fed by the retired Oracle ERP and are out of date.
  After a user signs in, ask Orbis who they are.

```
User ──sign in──▶ Entra SSO ──id token (oid, email)──▶ Your app ──lookup──▶ Orbis
```

---

## 2. Getting access

Ask the Orbis team for an API client. Tell us:

1. your application's name;
2. which data it needs — employees, roster, or both;
3. whether it writes roster shifts (rare; only dsndd does today).

You receive **one token for your application**, shown to us once and sent to you over a
secure channel. It looks like `orb_` followed by 43 characters.

- Store it in your server's environment or secrets manager. Never commit it, never send it
  to a browser or mobile app — call Orbis from your backend only.
- Send it on every request:

  ```
  Authorization: Token orb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  ```

- To rotate it, ask us. The old token stops working the moment the new one is issued.
- If it leaks, tell us at once; we can revoke it in seconds.
- Every call is recorded in Orbis's access log under your application's name.

### Scopes

Your token carries only the scopes you need.

| Scope | Lets you call |
|---|---|
| `directory:read` | Everything under `/api/v1/directory/`, and the older `/api/v1/employees/` |
| `roster:read` | `/api/v1/roster/…`, `/api/v1/shift-types/`, `/api/v1/holidays/`, `/api/v1/external/report-shifts/` |
| `roster:write` | `/api/v1/external/shifts/batch/` |

A missing, unknown or revoked token returns **401**. A valid token without the scope an
endpoint needs returns **403**.

---

## 3. Quick start

```bash
TOKEN="orb_…"
ORBIS="https://orbis.sebpo.net/api/v1"

# Who is this? (after SSO sign-in)
curl -sS -H "Authorization: Token $TOKEN" \
  "$ORBIS/directory/employees/lookup/?oid=3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f&email=first.last@sebpo.com"

# Everyone, 500 per page
curl -sS -H "Authorization: Token $TOKEN" "$ORBIS/directory/employees/?status=all"

# Today's roster
curl -sS -H "Authorization: Token $TOKEN" "$ORBIS/roster/?date=2026-10-14"
```

---

## 4. Common tasks

### 4.1 Identify the user who just signed in

```
GET /api/v1/directory/employees/lookup/?oid=<object id>&email=<email>&employee_id=<employee id>
```

Send **every identifier you have**. Orbis tries them in this order and answers with the
first that matches:

| Order | Key | Where you get it |
|---|---|---|
| 1 | `oid` | The `oid` claim in the Entra id token (or `id` from Graph `/me`). Never changes, even when an email does. |
| 2 | `email` | The `email` / `preferred_username` claim. Case-insensitive. |
| 3 | `employee_id` | If your app stores it. |

Orbis is still collecting object ids — it records each person's at their next Orbis
sign-in and will import the rest from Entra — so `oid` alone may not match yet. With `oid`
**and** `email` the lookup works today by email and moves to `oid` by itself later.

The response is the full employee record (section 6), their active direct reports, and
`matched_by` (`oid`, `email` or `employee_id`).

| Status | Meaning | What to do |
|---|---|---|
| 200 | Found | Use the record. `status` may be `inactive` — deciding whether a former employee may use your app is up to you. |
| 404 | Not in Orbis | Treat as unknown; tell the Orbis team if it's a real employee. |
| 409 | The keys point to **different** employees | Don't guess. Log it and report it to the Orbis team. The body lists which key matched whom. |
| 400 | No key, or `oid` isn't a UUID | Fix the request. |

**Python**

```python
import os, requests

ORBIS = "https://orbis.sebpo.net/api/v1"
HEADERS = {"Authorization": f"Token {os.environ['ORBIS_API_TOKEN']}"}

def orbis_person(claims: dict) -> dict | None:
    params = {"oid": claims.get("oid"), "email": claims.get("email") or claims.get("preferred_username")}
    r = requests.get(f"{ORBIS}/directory/employees/lookup/",
                     params={k: v for k, v in params.items() if v}, headers=HEADERS, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()  # 409 lands here — report it, don't guess
    return r.json()
```

**PHP (Laravel)**

```php
use Illuminate\Support\Facades\Http;

$response = Http::withToken(config('services.orbis.token'), 'Token')
    ->timeout(10)
    ->get('https://orbis.sebpo.net/api/v1/directory/employees/lookup/', array_filter([
        'oid'   => $claims['oid'] ?? null,
        'email' => $claims['email'] ?? $claims['preferred_username'] ?? null,
    ]));

$person = $response->status() === 404 ? null : $response->throw()->json();
```

### 4.2 Keep a local copy of employees (nightly sync)

```
GET /api/v1/directory/employees/?status=all&updated_since=<as_of>&page=<n>
```

1. **First run:** fetch `employees/?status=all`, following `next` until it is `null`.
   Save the `as_of` value from the first page.
2. **Every later run:** fetch `employees/?status=all&updated_since=<saved as_of>`, every
   page. Upsert each record by `employee_id`. Save the new `as_of`.
3. **Every run:** re-fetch the four reference lists (section 4.5) — they're small, and a
   rename doesn't mark employees as changed.

Use `status=all` so that someone leaving arrives as a change to `"status": "inactive"`.

Two things can change without the affected person's record being marked as changed: a
manager's `direct_reports_count`, and the name/email inside a `supervisor` object. Build
reporting lines from each record's `supervisor.employee_id` against your own synced copy
instead of trusting those nested values.

```python
def sync_employees(since: str | None) -> str:
    url = f"{ORBIS}/directory/employees/"
    params = {"status": "all", "page_size": 1000}
    if since:
        params["updated_since"] = since
    as_of = None
    while url:
        page = requests.get(url, params=params, headers=HEADERS, timeout=60).json()
        as_of = as_of or page["as_of"]
        for record in page["results"]:
            upsert_employee(record)       # keyed by record["employee_id"]
        url, params = page["next"], None  # `next` already carries the query
    return as_of                          # store it for the next run
```

### 4.3 Show a manager's team

| You want | Call |
|---|---|
| The people who report directly to them | `GET /api/v1/directory/employees/<employee_id>/direct-reports/` |
| Everyone they are responsible for in Orbis | `GET /api/v1/directory/employees/<employee_id>/scope/` |

**Scope** is exactly who Orbis lets that manager see and approve for (leave, roster,
timesheets): their whole reporting line at any depth, plus every department they head or
co-manage, plus any team they were granted. Each record carries how the person got there:

```json
"scope": {"via": ["reports_to", "department"], "depth": 2}
```

`depth` is the distance down the reporting line (1 = direct report); it is `null` when the
person is in scope only through a department or team grant. Scope is paged like the
employee list and has no `updated_since` — fetch it when you need it.

### 4.4 Read the roster

| You want | Call |
|---|---|
| Everyone, one day | `GET /api/v1/roster/?date=2026-10-14` — a plain array |
| Everyone, a date range | `GET /api/v1/roster/?start_date=2026-10-01&end_date=2026-10-31` — paged, at most 31 days |
| One employee, a date range | `GET /api/v1/roster/<employee_id>/?start_date=…&end_date=…` — a plain array, at most 92 days |
| Shift types and their times | `GET /api/v1/shift-types/` |
| Holidays | `GET /api/v1/holidays/?year=2026` |

Use the range form instead of one call per day. It pages 1000 rows by default
(`page_size` up to 5000), ordered by date, then employee id. A full month for the whole
company is about 42,000 rows.

Good to know:

- Only **concrete** shifts are returned. A day covered only by someone's weekly pattern
  doesn't appear until the roster is prepared.
- Only active employees with an employee id appear.
- Times are Asia/Dhaka wall-clock, `HH:MM`. When `shift_end_time` is earlier than
  `shift_start_time`, the shift crosses midnight.
- Roster history starts on **2026-01-01**.

### 4.5 Reference lists

```
GET /api/v1/directory/departments/
GET /api/v1/directory/designations/
GET /api/v1/directory/teams/
GET /api/v1/directory/offices/
```

Plain arrays ordered by name, including inactive rows so an old employee's department
still resolves. Add `?is_active=true` for current rows only. Teams carry `department_id`;
departments carry their `head_of_department`. An office's `code` is `MBD`, `MHK`, `RAOWA`
or `OTHER` — match on it rather than the name, which can be a street address.

---

## 5. Endpoint reference

All `GET` unless noted. Paged responses are
`{"count", "next", "previous", "results"}` (the employee list and scope also return
`as_of`); follow `next` until it is `null`.

| Endpoint | Scope | Parameters | Returns |
|---|---|---|---|
| `directory/employees/lookup/` | directory:read | `oid`, `email`, `employee_id` (at least one) | Employee + `direct_reports` + `matched_by` |
| `directory/employees/` | directory:read | `status` (`active`\|`inactive`\|`all`, default `active`), `department`, `team`, `updated_since`, `page`, `page_size` (≤ 1000, default 500) | Paged employees |
| `directory/employees/<employee_id>/` | directory:read | — | Employee + `direct_reports` |
| `directory/employees/<employee_id>/direct-reports/` | directory:read | `status` | Array of employees |
| `directory/employees/<employee_id>/scope/` | directory:read | `status`, `page`, `page_size` | Paged employees + `scope` |
| `directory/departments/`, `designations/`, `teams/`, `offices/` | directory:read | `is_active` | Array |
| `roster/` | roster:read | `date`, **or** `start_date` + `end_date` (≤ 31 days), `page`, `page_size` (≤ 5000) | Array (one day) or paged shifts (range) |
| `roster/<employee_id>/` | roster:read | `start_date`, `end_date` (≤ 92 days) | Array of shifts |
| `shift-types/` | roster:read | — | Shift types with their attributes |
| `holidays/` | roster:read | `year` | Holidays |
| `employees/` | directory:read | `is_active` | Older flat employee list — new apps use `directory/employees/` |
| `external/report-shifts/` | roster:read | `start_date`, `end_date` (≤ 92 days), `employee_id`/`employee_ids`, `include_inactive`, `page`, `page_size` | Paged report shifts — see `docs/TIMELY_API.md` §5 |
| `POST external/shifts/batch/` | roster:write | JSON body: `actor_employee_id`, `items` (≤ 2000) | Per-item results — see `docs/TIMELY_API.md` §6 |

`updated_since` takes an ISO 8601 datetime (`2026-10-14T02:00:00+06:00`) or a date
(`2026-10-14`); without an offset it is read as Asia/Dhaka time.

---

## 6. Records

### Employee

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
| `employee_id` | Stable HR id. Use it as your key. |
| `entra_object_id` | `null` until Orbis has recorded it. |
| `status` | `active` or `inactive`. Someone serving notice is `active` with a future `last_working_date`, and becomes `inactive` the day after it. |
| `department`, `team`, `designation`, `office` | `null` when not set. For a former employee, as on their last day. |
| `work_location` | `WFO`, `WFH`, `HYB` or `null`. |
| `supervisor` | Who they report to, or `null`. Can be `inactive` if HR hasn't reassigned them yet. |
| `direct_reports_count` | Active people who report to them. |
| `updated_at` | When the record last changed; what `updated_since` compares. |

Employee records hold directory data only — no salary, personal, contact or ID-document
fields. If your app needs another field, ask us.

### Shift

```json
{
  "working_day": "2026-10-14",
  "employee_id": "917",
  "name": "First Last",
  "shift_attribute": "EVE 1-10",
  "shift_start_time": "13:00",
  "shift_end_time": "22:00",
  "office_location": "MHK",
  "work_location": "WFO",
  "leave_locked": false
}
```

| Field | Notes |
|---|---|
| `shift_attribute` | The shift's variant (`EVE 1-10`, `MOR 7-4`), or the type code for leave, off and holiday rows: `OFF`, `AL`, `CL`, `SL`, `MTL`, `CPL`, `LWP`, `HDL`, `ABS`, `FL`, `GH`. |
| `shift_start_time`, `shift_end_time` | `HH:MM`, Asia/Dhaka. `null` on leave, off and holiday rows. |
| `office_location` | `MBD`, `MHK`, `RAOWA`, or `null` (non-working shift, or unknown office). |
| `work_location` | `WFO`, `WFH`, `HYB`, or `null`. |
| `leave_locked` | `true` when the row comes from an approved leave application in Orbis. |

---

## 7. Errors

| Status | Body | When |
|---|---|---|
| 400 | `{"<param>": "<message>"}` | A parameter is missing, malformed, or **not one the endpoint accepts** (directory endpoints) |
| 401 | `{"detail": "…"}` | Missing, unknown or revoked token |
| 403 | `{"detail": "…"}` | Your token lacks the scope this endpoint needs |
| 404 | `{"detail": "…"}` | Unknown employee |
| 409 | `{"detail": "…", "matches": {…}}` | Lookup keys identify different employees |

---

## 8. Ground rules

- **Call from your server only.** The token is a secret.
- **Directory endpoints reject parameters they don't know** with a 400 naming the parameter, rather than ignoring it and returning everyone. To find one person by email, use `directory/employees/lookup/?email=`; there is no `email` filter on the list.
- **Ignore fields you don't know.** We may add fields without notice; breaking changes are
  announced at least 7 days ahead.
- **Be gentle.** There's no hard rate limit. A nightly sync plus a lookup per sign-in is
  the expected load; cache what you can.
- **Times** are Asia/Dhaka. Timestamps carry their offset; roster times don't.
- **Questions, new fields, a token, or a 409 to report:** S. M. Rashel, `smrashel@sebpo.com`.
