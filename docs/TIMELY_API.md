# Orbis Roster API — integration reference

- **Audience:** engineers of applications that read (or, for dsndd, write) the Orbis roster.
  Written first for TimeLy, which is why some notes mention it.
- **Owner:** Orbis team (S. M. Rashel).
- **Status:** live at `https://orbis.sebpo.net`.

This document describes the roster HTTP API: who is scheduled to work, when, and where,
plus the shift-type and holiday catalogs and the batch write endpoint.

---

## Quick start

```bash
TOKEN="<the API token your team was issued — Authorization header below>"

# All shifts on a given day
curl -sS -H "Authorization: Token $TOKEN" \
  "https://orbis.sebpo.net/api/v1/roster/?date=2026-05-06"

# One employee's shifts in a date range
curl -sS -H "Authorization: Token $TOKEN" \
  "https://orbis.sebpo.net/api/v1/roster/539/?start_date=2026-04-30&end_date=2026-05-06"
```

Both endpoints return JSON arrays of shift records (see schema below).

---

## Authentication

Every request must include this header:

```
Authorization: Token <api-token>
```

- **TimeLy now has its own token** (it starts with `orb_`), issued by the Orbis
  team. It replaces the older shared token, which keeps working until TimeLy
  has switched and is then removed — swap the value in TimeLy's environment;
  nothing else changes (same header, same endpoints, same responses).
- The new token carries scopes: `roster:read` for the roster, shift-type and
  holiday endpoints, `directory:read` for `/api/v1/employees/` and the
  Directory API (`docs/DIRECTORY_API.md`). A valid token without the needed
  scope returns **HTTP 403**.
- **Don't** commit it to TimeLy's repo — store it in TimeLy's environment /
  secrets manager. Contact the Orbis team to request a rotation; the old token
  stops working the moment a new one is issued.
- A missing, invalid or revoked token returns **HTTP 401**.

---

## Endpoints

All under `https://orbis.sebpo.net/api/v1/`.

| Method | Path | Scope | Section |
|---|---|---|---|
| GET | `roster/?date=` | `roster:read` | 1 |
| GET | `roster/?start_date=&end_date=` | `roster:read` | 1b |
| GET | `roster/<employee_id>/?start_date=&end_date=` | `roster:read` | 2 |
| GET | `shift-types/` | `roster:read` | 3 |
| GET | `holidays/?year=` | `roster:read` | 4 |
| GET | `external/report-shifts/?start_date=&end_date=` | `roster:read` | 5 |
| POST | `external/shifts/batch/` | `roster:write` | 6 |
| GET | `employees/?is_active=` | `directory:read` | 7 |

### 1. Daily roster — all employees on a given date

```
GET /api/v1/roster/?date=YYYY-MM-DD
```

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `date` | query | `YYYY-MM-DD` | yes | The day to fetch. |

**Response:** `200 OK` — JSON array of `Shift` objects (see schema). Empty
array `[]` if there are no scheduled shifts on that day.

**Example:**

```bash
curl -sS -H "Authorization: Token $TOKEN" \
  "https://orbis.sebpo.net/api/v1/roster/?date=2026-05-06"
```

```json
[
  {
    "working_day": "2026-05-06",
    "employee_id": "539",
    "name": "S. M. Rashel",
    "shift_attribute": "EVE 1-10",
    "shift_start_time": "13:00",
    "shift_end_time": "22:00",
    "office_location": "MHK",
    "work_location": "WFO",
    "leave_locked": false
  }
]
```

### 1b. Roster for a date range — all employees

```
GET /api/v1/roster/?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&page=&page_size=
```

The daily endpoint for many days at once — use it instead of calling `?date=`
once per day.

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `start_date` | query | `YYYY-MM-DD` | yes | Range start, inclusive. |
| `end_date`   | query | `YYYY-MM-DD` | yes | Range end, inclusive. At most **31 days** including both ends. |
| `page`       | query | int | no | Default 1. |
| `page_size`  | query | int | no | Default 1000, maximum 5000. |

Don't send `date` together with `start_date`/`end_date` (HTTP 400).

**Response:** `200 OK` — a **paged object**, unlike the bare array of the
single-day form. The same `Shift` objects, for the same employees (active,
with an employee id), ordered by `working_day` then `employee_id`. Follow
`next` until it is `null`.

```json
{
  "count": 45210,
  "next": "https://orbis.sebpo.net/api/v1/roster/?start_date=2026-05-01&end_date=2026-05-31&page=2",
  "previous": null,
  "results": [ { …Shift… }, … ]
}
```

Roster history in Orbis starts on **2026-01-01**; earlier dates return
(almost) nothing.

### 2. Per-employee roster — one employee, date range

```
GET /api/v1/roster/<employee_id>/?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `employee_id` | path | string | yes | The human employee code (e.g. `"539"`), **not** a UUID. Same value TimeLy uses internally. |
| `start_date` | query | `YYYY-MM-DD` | yes | Range start, inclusive. |
| `end_date`   | query | `YYYY-MM-DD` | yes | Range end, inclusive. Max 92 days from `start_date`. |

**Response:** `200 OK` — JSON array of `Shift` objects, sorted ascending by
`working_day`.

**Example:**

```bash
curl -sS -H "Authorization: Token $TOKEN" \
  "https://orbis.sebpo.net/api/v1/roster/539/?start_date=2026-04-30&end_date=2026-05-06"
```

---

## Shift object schema

Every record returned by both endpoints has the same shape:

| Field | Type | Nullable | Description |
|---|---|---|---|
| `working_day`      | `YYYY-MM-DD` string  | no  | The shift's calendar date in Asia/Dhaka. |
| `employee_id`      | string               | no  | The Orbis employee code (matches TimeLy's). |
| `name`             | string               | no  | Display name (full name with fallback to username/email). |
| `shift_attribute`  | string               | no  | Attribute name (e.g. `"EVE 1-10"`, `"MOR 7-4"`). For LEAVE/OFF/HOLIDAY rows where there is no per-variant attribute, this is the shift-type code (`"OFF"`, `"AL"`, `"FL"`, `"GH"`, etc.). |
| `shift_start_time` | `HH:MM` string       | yes | 24-hour local start time. **Null** for LEAVE/OFF/HOLIDAY shifts. |
| `shift_end_time`   | `HH:MM` string       | yes | 24-hour local end time. **Null** for LEAVE/OFF/HOLIDAY shifts. Note: when end < start, the shift crosses midnight (e.g. NIG 11-7 starts 23:00, ends 07:00 next day). |
| `office_location`  | enum string          | yes | One of `"MBD"`, `"MHK"`, `"RAOWA"`. **Null** when the shift is non-WORK or the employee's location can't be canonicalized to one of those three. |
| `work_location`    | enum string          | yes | One of `"WFO"` (in office), `"WFH"` (remote), `"HYB"` (hybrid). **Null** when the shift is non-WORK or no work-location is recorded. |
| `leave_locked`     | boolean              | no  | `true` when this row was materialised from an approved Orbis `LeaveApplication`. Added for external write-API consumers (dsndd is the first) so their reconciliation sweeps know never to overwrite these rows; safe for TimeLy to ignore. |

### Shift-type codes you'll see in `shift_attribute`

| Code | Category | Meaning |
|---|---|---|
| `EVE`, `MOR`, `NIG`, `CUST` | WORK | Working shifts (usually appear as their attribute names like `EVE 1-10`). |
| `OFF` | OFF | Weekly day off. |
| `AL` | LEAVE | Annual leave. |
| `CL` | LEAVE | Casual leave. |
| `SL` | LEAVE | Sick leave. |
| `MTL` | LEAVE | Maternity leave. |
| `CPL` | LEAVE | Compensatory leave. |
| `LWP` | LEAVE | Leave without pay. |
| `HDL` | LEAVE | Half-day leave. |
| `ABS` | LEAVE | Absence (legacy). |
| `FL` | HOLIDAY | Festival / company holiday. |
| `GH` | HOLIDAY | Government holiday. |

---

## Error responses

| Status | When | Body |
|---|---|---|
| `400 Bad Request` | Required query param missing, malformed date, end before start, range too long (31 days for 1b, 92 for 2 and 5). | `{"date": "Required, format YYYY-MM-DD"}` etc. — DRF field-keyed validation errors. |
| `401 Unauthorized` | Missing, wrong or revoked token. | `{"detail": "Invalid token."}` |
| `403 Forbidden` | Valid token without the scope the endpoint needs (see the table under Endpoints). | `{"detail": "This API client is not allowed to use this endpoint."}` |
| `404 Not Found` | Per-employee endpoint with an `employee_id` that doesn't match any active employee. | `{"detail": "No active employee with employee_id=<x>"}` |

There is no `5xx` response that is part of the contract — if you see one,
contact the Orbis team.

---

## Caveats and behaviour notes

These are not bugs; please make sure your client handles them.

1. **Sparse data — only concrete shifts are returned.**
   Orbis stores both a *weekly pattern* (per employee per weekday) and
   *concrete shift rows* (per employee per date). This API returns **only
   concrete rows.** Days where an employee has only a pattern (no
   materialised shift) do **not** appear in the response.

   In practice this means: if you query a future date that hasn't been
   prepared yet, you may get `[]` even though the employee has a default
   pattern that says they'd work that day. The Orbis team materialises the
   roster ahead of time; please coordinate the look-ahead window with us
   if you need shifts further out than what's currently materialised.

2. **Filters: inactive employees and missing `employee_id` values are excluded.**
   - Inactive Orbis users (`is_active = false`) never appear.
   - Users whose `employee_id` field is null/blank in Orbis never appear,
     because there's no way to map them to TimeLy.

3. **Time zone is Asia/Dhaka.**
   `shift_start_time` and `shift_end_time` are wall-clock times in
   Asia/Dhaka with no offset suffix. TimeLy is on the same clock so no
   conversion is needed.

4. **Cross-midnight shifts.**
   Some shifts (e.g. `NIG 11-7`, `CUST`) start late and end early next
   morning. Detect this when `shift_end_time < shift_start_time`.

5. **Pagination only in the date-range form; no rate limiting.**
   The single-day and per-employee responses are plain arrays — the biggest
   single day is roughly 1 500 rows (~300 KB). The all-employee date-range
   form (1b) is paged; a full month is about 42 000 rows.

6. **`office_location` may be null even for a WORK shift.**
   When an employee's `JobLocation` is recorded as a free-text Azure-AD
   address that doesn't match the canonical `MBD` / `MHK` / `RAOWA`
   buckets, this field returns null. The shift still happens; only the
   bucket is unknown.

7. **The path uses the human `employee_id`, not a UUID.**
   `/api/v1/roster/539/` is correct. `/api/v1/roster/00000000-...-...`
   would 404. This was a deliberate change in May 2026.

---

## Polling guidance

A reasonable poll cadence:

- **Live dashboards (today's roster):** every 60–120 seconds.
- **Look-ahead sync (tomorrow / week-ahead):** every 5–10 minutes.
- **Bulk historical pull:** use the date-range form (1b) in windows of up to
  31 days, starting from 2026-01-01.

The API is stateless and idempotent; identical requests return identical
responses.

---

## Catalogs, reports and writes

### 3. Shift types — the catalog

```
GET /api/v1/shift-types/
```

Every active shift type, ordered as Orbis shows them, each with its active
time variants ("attributes"). Use it to resolve the codes you see in
`shift_attribute`, and to get the ids the batch write (6) needs.

```json
[
  {
    "id": 3,
    "name": "EVE",
    "description": "Evening",
    "category": "WORK",
    "is_working_shift": true,
    "shift_timezone": "Asia/Dhaka",
    "shift_start_time": "14:00:00",
    "shift_end_time": "23:00:00",
    "color": "#3B82F6",
    "sort_order": 2,
    "has_attribute": true,
    "is_active": true,
    "attributes": [
      {"id": 11, "name": "EVE 1-10", "start_time": "13:00:00", "end_time": "22:00:00", "description": "", "sort_order": 1}
    ]
  }
]
```

| Field | Notes |
|---|---|
| `name` | The code (`EVE`, `OFF`, `AL`, …) — see the code table above. |
| `category` | `WORK`, `LEAVE`, `HOLIDAY` or `OFF`. `is_working_shift` is `true` exactly for `WORK`. |
| `shift_start_time`, `shift_end_time` | The type's default times, `HH:MM:SS`; `null` for non-working types. An attribute's own times win when a shift uses one. |
| `attributes` | The time variants. Their `id` is what the batch write calls `orbis_shift_attribute_id`. |

### 4. Holidays

```
GET /api/v1/holidays/?year=2026
```

Active holidays ordered by date; `year` is optional (all years without it).

```json
[{"id": 7, "date": "2026-12-16", "name": "Victory Day", "is_active": true}]
```

### 5. Report shifts — a richer range report

```
GET /api/v1/external/report-shifts/?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

The same concrete shifts as 1b in a fuller shape, built for reporting (dsndd
uses it). Paged like 1b.

| Parameter | Required | Notes |
|---|---|---|
| `start_date`, `end_date` | yes | Inclusive; at most 92 days apart. |
| `employee_id` / `employee_ids` | no | Repeat `employee_id`, or give `employee_ids` as a comma-separated list (up to 1000). Named employees are included even if inactive, for historical reports. An unknown id is a 400. |
| `include_inactive` | no | `true` adds inactive employees when no ids are named. |
| `page`, `page_size` | no | Default 1000, maximum 5000. |

Ordered by date, then employee id. Response:

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "meta": {"start_date": "2026-10-01", "end_date": "2026-10-31", "range_days": 31,
           "employee_scope": "specified", "employee_ids": ["917"], "include_inactive": true},
  "results": [
    {
      "orbis_shift_id": 123456,
      "employee_id": "917",
      "employee_name": "First Last",
      "date": "2026-10-01",
      "shift_type_id": 3,
      "shift_code": "EVE",
      "shift_full_name": "Evening",
      "shift_attribute_id": 11,
      "shift_label": "EVE 1-10",
      "category": "WORK",
      "is_working_shift": true,
      "start_time": "13:00",
      "end_time": "22:00",
      "office_location": "MHK",
      "work_location": "WFO",
      "note": "",
      "leave_locked": false,
      "updated_at": "2026-09-28T10:15:02+06:00"
    }
  ]
}
```

### 6. Batch shift writes (`roster:write`)

```
POST /api/v1/external/shifts/batch/
Content-Type: application/json
```

Creates, updates or deletes roster cells — up to **2000 items** per request.
Only applications granted `roster:write` may call it (today: dsndd).

```json
{
  "actor_employee_id": "539",
  "items": [
    {"client_ref": "row-1", "employee_id": "917", "date": "2026-10-20",
     "orbis_shift_type_id": 3, "orbis_shift_attribute_id": 11, "location": "MHK", "note": ""},
    {"client_ref": "row-2", "employee_id": "918", "date": "2026-10-20",
     "orbis_shift_type_id": null}
  ]
}
```

| Field | Notes |
|---|---|
| `actor_employee_id` | **Required.** The real person making the change (e.g. the supervisor who edited the cell); recorded as the editor of every write. Unknown → the whole request is a 400. |
| `client_ref` | Your own reference, echoed back in the result. Defaults to `item-<n>`. |
| `employee_id`, `date` | The cell. `date` is `YYYY-MM-DD`. |
| `orbis_shift_type_id` | A shift type id from 3. `null` or omitted **deletes** the cell. |
| `orbis_shift_attribute_id` | Optional time variant; must belong to the shift type. |
| `location` | Optional `MHK`, `MBD`, `RAOWA` or `WFH`. Every write replaces the cell's location; omitted means "use the employee's default". |
| `note` | Optional text. |

The response is always **200** with one result per item, unless the request
itself is broken (not JSON, no `items`, more than 2000 items, bad
`actor_employee_id`) — a bad row never fails the batch:

```json
{
  "results": [
    {"client_ref": "row-1", "status": "ok", "action": "created"},
    {"client_ref": "row-2", "status": "conflict", "action": "skipped", "reason": "leave_application_owned"}
  ],
  "summary": {"ok": 1, "conflict": 1, "error": 0}
}
```

| `status` | `action` / `reason` |
|---|---|
| `ok` | `created`, `updated`, `deleted`, or `noop` (nothing changed) |
| `conflict` | `skipped`, because of `leave_application_owned` (the cell belongs to an approved leave — change the leave instead), `past_last_working_date`, `before_joining_date` or `dedicated_night_shift_window` (the employee is on an approved dedicated night shift window that date — only `NIG`/`CUST` or a non-working type can be written; any other working shift needs their department head in Orbis) |
| `error` | `reason` explains: unknown employee or shift type, attribute of another type, bad date or location, … |

### 7. Employee list (older)

```
GET /api/v1/employees/?is_active=true
```

Needs `directory:read`. A flat list kept for existing consumers:
`id`, `employee_id`, `email`, `full_name`, `first_name`, `last_name`,
`department`, `designation`, `team`, `job_location`, `work_location` (names, not
ids) and `is_active`. **New integrations use the Directory API instead**
(`docs/DIRECTORY_API.md`): ids, supervisors, direct reports, lookup and
change tracking.

---

## Contact / change management

- **Orbis team:** S. M. Rashel (`smrashel@sebpo.com`).
- **Breaking changes** to the schema will be announced at least 7 days
  ahead of deploy. Additive changes (new optional fields) may ship without
  notice; please write your client to ignore unknown fields.
