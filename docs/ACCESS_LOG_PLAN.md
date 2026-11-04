# Access log — who accessed which data or page

Status: **confirmed and built, 2026-10-09.** Requested by the CTO; decisions in §8 were
taken the same day and all three phases shipped together (§5). Deployment steps are in
`deploy/README.md` "Access log".

## 1. What is being asked, and what exists today

The ask: a record of **who accessed which data or page, and when** — reads, not just
writes. Today Orbis has no such record anywhere.

What the project *does* have (all found during the 2026-10-09 survey):

| Layer | What it records | Gap for this ask |
|---|---|---|
| nginx `access.log` on the VPS | IP, path, status, bytes, user-agent | No user identity — every row is anonymous. Rotated by the OS; nobody reads it. |
| `logs/gunicorn-error.log`, `logs/orbis.log` | Errors and `INFO` application logging | Not per request, no actor. |
| Domain change ledgers — `ShiftChangeLog`, `LeaveBalanceTransaction`, `PolicyAcknowledgementEvent`, `PayrollRunAudit`, `ProfileSelfServiceEvent`, `NightShiftConsentEvent`, `DedicatedNightShiftAssignmentEvent`, `RosterEmailLog` | *Changes* to one domain's rows, with actor | Nothing about *viewing*. Each is its own shape and its own page. |
| Django admin `LogEntry` | Add/change/delete made through `/admin/` | Admin reads (changelist, change form) leave no trace. |
| `scheduler.ScheduledTaskRun` | Each beat-dispatched job run | Background, not users. |
| `_Auditable` (`added_by`/`updated_by`) on menu rows, `added_by`/`updated_by` on `Shift` | Last writer only | Overwritten on every save; no reads. |

`docs/PAYROLL_DATA_PROTECTION_PLAN.md` §7 Phase 3 already reserved a `PayrollAccessLog`
("who viewed/exported money, when"). The CTO's ask is the same requirement widened to the
whole application, so **this plan absorbs that item** — one table and one viewer, with
payroll as one data class among several, rather than a payroll-only log plus a general one.

### Scale (prod, 2026-10-09)

| | |
|---|---|
| User rows / active | 1,529 / 1,469 |
| Non-admin routes | ~275 |
| Admin routes | ~300 (generated) |
| Infra | single VPS, gunicorn + MySQL + Redis + Celery worker/beat |

Most employees touch Orbis a few times a week (dashboard, roster, leave). A working
estimate is 5–15k logged requests per day, under 300 bytes a row. That is roughly 1–2 GB a
year including indexes — fine for a plain MySQL table with a retention job.

## 2. Requirements (as understood — please correct)

1. Every **authenticated** request that reaches a Django view is recorded with actor,
   time, page (route name + path), HTTP method, status, and the resolved **subject**
   (which employee's data) and **object** (which run/policy/document/case) where the URL
   identifies one.
2. **Sensitive reads carry a data class** — profile, salary, tax, loan, leave, timesheet,
   document, policy, offboarding, roster, admin, API — so the question "who looked at
   salary data last month" is one filter, not a path regex.
3. **Exports and downloads** record what left the system: file name, row count, filters.
4. **Auth events**: login (password vs SSO), logout, failed login, and **denied** access
   (403) — a denied attempt on a salary page is more interesting than a granted one.
5. The log is **append-only**, readable by a small group, and kept for a fixed period.
6. **Zero impact on behaviour**: logging must never break or slow a request noticeably,
   and must never store money amounts, passwords, tokens or request bodies.
7. A **viewer page** inside Orbis (filter, search, export) so the CTO does not need SSH.

Out of scope for this plan: database-level query auditing (MySQL audit plugin), tamper-proof
remote shipping (SIEM), and alerting. Listed in §9 as follow-ups.

## 3. Design

### 3.1 One table — `audit.AccessLog`

A new first-party app **`audit`** (flagged as plan drift: `docs/PLAN.md`'s app list gains a
line in the same change). It is thin, like `scheduler`: one model, one middleware, three
signal receivers, one list view, one admin, one beat job.

```
AccessLog
  occurred_at     DateTimeField(db_index)        # request start, UTC
  actor           FK CustomUser SET_NULL null    # who
  actor_label     CharField(150)                 # email snapshot; "timely-service-account" for the API
  session         CharField(16, blank)           # sha256(session_key)[:16] — groups a sitting, never the key itself
  ip_address      GenericIPAddressField(null)    # X-Forwarded-For first hop (nginx sets it) else REMOTE_ADDR
  user_agent      CharField(300, blank)

  method          CharField(8)                   # GET / POST / …
  route           CharField(200, db_index)       # resolver route name, e.g. "payroll:salary_detail", "admin:payroll_salarystructure_change"
  path            CharField(500)                 # /payroll/salary/<uuid>/
  query           CharField(500, blank)          # query string with password/token/csrf keys stripped
  status_code     PositiveSmallIntegerField
  duration_ms     PositiveIntegerField
  is_htmx         BooleanField                   # drawer/fragment loads, so the viewer can collapse them

  data_class      CharField(20, db_index)        # PAGE | PROFILE | SALARY | TAX | LOAN | LEAVE | TIMESHEET | ROSTER | DOCUMENT | POLICY | OFFBOARDING | USER_ADMIN | ADMIN | API | AUTH
  action          CharField(16, db_index)        # VIEW | LIST | WRITE | EXPORT | DOWNLOAD | LOGIN | LOGOUT | LOGIN_FAILED | DENIED
  subject         FK CustomUser SET_NULL null    # whose data (the employee), when the URL/view names one
  subject_label   CharField(150, blank)          # "EMP-0421 · Jane Doe" snapshot so the row still reads after SET_NULL
  object_type     CharField(60, blank)           # "PayrollRun", "Policy", "DocumentRequest", "Resignation" …
  object_id       CharField(64, blank)
  detail          JSONField(default=dict)        # {"rows": 312, "file": "timesheet_2026-09.xlsx", "filters": {...}} — never an amount
```

Indexes: `(actor, occurred_at)`, `(subject, occurred_at)`, `(data_class, occurred_at)`,
`(route, occurred_at)`, `(action, occurred_at)`. Ordering `-occurred_at`.

Conventions this must follow (all from `CLAUDE.md`):

- Two `CustomUser` FKs → the migration needs `_match_existing_collation` (copy from
  `employees/migrations/0008`) and `atomic = False`.
- `save()` refuses an update (`if self.pk: raise`), `delete()` raises; the admin is read-only
  like `ScheduledTaskRunAdmin` (`has_add/change/delete_permission` → False). Deletion happens
  only in the retention job via `QuerySet.delete()`/raw SQL, never row-wise.
- `detail` is checked by a test that walks every `record_access` call site and refuses any
  key named `amount`, `gross`, `net`, `tds`, `salary` — same posture as `core/tests_money.py`.

### 3.2 Capture: one middleware, enriched by the view

**`audit.middleware.AccessLogMiddleware`**, placed after `AuthenticationMiddleware` and
`LastWorkingDateMiddleware` (so `request.user` is final) and before `HtmxMiddleware` is
irrelevant — it reads `HX-Request` directly.

- `process_view` — creates `request.access = AccessContext(...)` pre-filled from
  `request.resolver_match` (route name, kwargs) through the **registry** (§3.3). Unregistered
  routes get `data_class=PAGE, action=VIEW|WRITE` (by method).
- The view may enrich it: `request.access.subject = employee`,
  `request.access.detail["rows"] = len(rows)`, `request.access.action = "EXPORT"`. This is
  the whole "explicit" API — **no second insert path**. Views that already know their
  subject (`EmployeeDetailView`, `SalaryStructureDetailView`, `ServiceBookPDFView`,
  `DownloadPdfView`, `TaxDeclarationDocumentView`, `SalaryLoanDocumentView`,
  `PolicyPdfView`, every `?export=`/`Export` view, `PayrollRunDetailView`) add one line.
- `process_response` — writes **one synchronous INSERT** inside `try/except Exception:
  logger.exception(...)`, so a logging failure can never surface to the user. Runs after
  the view's transaction (`ATOMIC_REQUESTS` wraps the view only), so a rolled-back POST
  is still recorded with its status.
- Skipped entirely: `/static/`, `/media/` (nginx serves these in prod anyway), anonymous
  requests except the auth routes, HTTP 404 for unresolved paths (bot noise), and an
  explicit allow-list of reference-data endpoints — `employees/ajax/{states,cities,
  union-wards,teams,subteams}`, `policies/owners-by-department/` — that reveal nothing
  about a person. `employees/ajax/employees/` and the people-search endpoints **stay
  logged**: a search for a name is an access.
- 403 → `action=DENIED` regardless of what the registry said. 302 from
  `LoginRequiredMixin` is skipped (anonymous).

Why synchronous rather than `defer()`: the audit record is the point, and a Celery hop
adds a failure mode (Redis down → `defer()` already runs inline anyway) for ~1 ms of
same-host MySQL time. Measured after Phase 1; if it ever shows in p95, the write moves to
`defer()` behind a setting with no model change.

### 3.3 Classification registry — `audit/registry.py`

A plain dict from route name → `(data_class, subject_kwarg_or_None, object_type_or_None)`.
Seeded from the full URL map captured on 2026-10-09. Highlights:

| data_class | Routes (examples) | subject |
|---|---|---|
| PROFILE | `employees:detail`, `employees:section_edit/add/delete`, `employees:avatar`, `accounts:user_update`, `users/` list | `pk` |
| SALARY | `payroll:salary_list/detail/revise`, `payroll:run_detail/export/report`, `payroll:legacy_months_export`, go-live/salary/history uploads, pending salary decisions, admin `payroll_salarystructure_*`, `payroll_payrollrunemployee_*` | `employee_pk` where present, else `object=PayrollRun` |
| TAX | `payroll:tax_declaration_review/document`, tax-challan/allocation/opening uploads, admin `payroll_employeetaxdeclaration_*` | declaration's employee (view sets it) |
| LOAN | `payroll/loans/**` | loan's employee (view sets it) |
| TIMESHEET | `attendance/**` | `?q=` resolved by the view when it is one employee |
| LEAVE | `leave/**`, encashment, CpL grants, bank uploads, AL history | `pk` on register/detail; application's employee on detail/approve |
| ROSTER | `roster/**`, API `api/v1/**` (class API) | `employee_pk` |
| DOCUMENT | `documents/**`, `verify/**` (public, logged anonymously — these are *meant* to be opened by outsiders, so the log shows which letters are being verified) | request's employee |
| POLICY | `policies/**` | — |
| OFFBOARDING | `offboarding/**` | case's employee |
| USER_ADMIN | `users/create`, `users/bulk-create/**` (results page carries one-time passwords → always logged, detail = row count) | — |
| ADMIN | everything under `admin/` — `object_type` from the model, `object_id` from `object_id` kwarg | — |
| AUTH | `login/`, `logout/`, `oidc/**` | — |

**Guard test** (same style as `core/tests_encryption.py::RegistryGuardTests`): walks the URL
resolver and fails if any route under `payroll/`, `employees/<uuid>/`, `documents/`,
`offboarding/`, `attendance/`, `leave/` or `admin/<payroll|employees|accounts>` is missing
from the registry. New sensitive views then cannot land unclassified.

### 3.4 Auth events — `audit/signals.py`

`user_logged_in` → `LOGIN` with `detail={"backend": "oidc"|"password"}` (the backend name
`auth.login()` stamps on the session); `user_logged_out` → `LOGOUT`;
`user_login_failed` → `LOGIN_FAILED` with the attempted email in `actor_label` (never the
password), `ip_address`, user-agent. Mozilla-django-oidc calls `auth.login()`, so SSO
logins fire the same signal with no extra wiring.

### 3.5 Viewer — `/audit/access-log/`

A data-table list page per the layout convention (`.page-fill`, `.table-scroll`,
`table-simple table-sticky`, `PageSizeMixin`, `_pagination.html`, `_result_count.html`).

- Filters: actor (people search, same widget as the roster email people picker), subject,
  data class, action, route (contains), path (contains), date range (Flatpickr),
  "hide htmx fragments" toggle (default on — a drawer open is three rows otherwise).
- Columns: time (Asia/Dhaka), actor, action, data class, subject, page (route + path),
  status, IP, duration. Row expands to show `detail`.
- **Export Excel** via `core.exports` — itself logged as `data_class=ADMIN, action=EXPORT`.
- Access: a `ProtectedView` `access_log` seeded in `seed_orbis.py` → **User Admins +
  superusers**. New sidebar section "Audit" → "Access log" (`seed_orbis --menus-only` on
  prod). HR is deliberately *not* in: the log shows HR's own reads.
- Also registered in the Django admin, read-only, for ad-hoc filtering by superusers.

Phase 2 optional: an "Access history" tab on the employee profile (HR + User Admins) listing
who viewed *this* employee's data — the question HR actually gets asked.

### 3.6 Retention — `prune_access_log`

A management command + `PeriodicTask` row in `scheduler/schedule.py::JOBS` (nightly 01:00,
seeded by `seed_beat_schedule`). Deletes rows older than `ACCESS_LOG_RETENTION_DAYS`
(`.env`, default **730** = 24 months) in 10k-row batches so it never holds a long lock.
Optional `--archive` writes the pruned rows to `logs/access-log-YYYY-MM.jsonl.gz` first.

### 3.7 Immutability beyond the ORM

The app's MySQL user has full DML on every table. For this one table the deploy step runs
`REVOKE UPDATE ON orbis.audit_accesslog FROM 'orbis'@'localhost'` (DELETE stays, for the
retention job — or the job runs as a second DB user, decided in Phase 3). This is a
`deploy/README.md` step like the `/media/` deny block, not code.

### 3.8 What is deliberately *not* logged

- Request bodies, form values, uploaded file contents.
- Money. `detail` carries counts, filenames and filter values only (test-enforced).
- `password`, `token`, `csrfmiddlewaretoken`, `next` keys from query strings.
- The raw session key (hashed prefix only).
- Static/media hits — nginx already has those with IPs.

## 4. Relation to existing pieces

- **`PayrollAccessLog`** (data-protection plan Phase 3 §1) is superseded by `data_class=
  SALARY|TAX|LOAN` rows here. That doc gets a one-line pointer in the same change.
- **Domain change ledgers stay.** They answer "what changed" with before/after values;
  `AccessLog` answers "who looked / who tried". A `WRITE` row here links to them by
  `object_type/object_id`, nothing is duplicated.
- **Django admin `LogEntry` stays** as the admin write history; `ADMIN` rows cover reads.
- **nginx access log** stays as the raw transport log. Optionally add
  `$upstream_http_x_orbis_actor` to its `log_format` (middleware sets the header) so the
  two can be joined by IP + timestamp if ever needed — Phase 3, zero risk.

## 5. Delivery

| Phase | Content | Effort | Deploy |
|---|---|---|---|
| **1 — capture** | `audit` app, model + collation migration, middleware, registry (all current routes classified) + guard test, auth signals, read-only admin, `detail` money-key test, `docs/PLAN.md` app list line | ~2 days | `migrate`; one `MIDDLEWARE` entry; restart gunicorn |
| **2 — viewer** | `/audit/access-log/` list + filters + Excel export, `ProtectedView` + menu seed, `request.access` enrichment in the ~15 subject-aware/export/download views, employee-profile "Access history" tab (optional) | ~1.5 days | `seed_orbis --menus-only` |
| **3 — retention & hardening** | `prune_access_log` + beat row, `ACCESS_LOG_RETENTION_DAYS`, MySQL `REVOKE UPDATE`, nginx header (optional), `deploy/README.md` section, measure write cost in prod | ~0.5 day | `seed_beat_schedule`; one SQL grant |

Phase 1 alone already answers the CTO's question from the Django admin; Phases 2–3 make
it usable without a developer and bounded in size.

## 6. Verification

- Unit: middleware writes one row per request with the right actor/subject/class; skips
  static/anonymous/allow-listed; 403 → DENIED; API requests record the service account
  label with `actor=NULL`; a DB error in the audit write does not change the response.
- Registry guard test (§3.3) and `detail` money-key test (§3.1) run in the normal suite.
- Manual on prod after Phase 1: open one salary page, one profile, one export, one denied
  page, log out/in by SSO → five rows with the expected classes in the admin.
- Load: time 200 requests to `/employees/` before/after the middleware on the dev box;
  expect < 2 ms added at p95.

## 7. Risks

1. **Table growth** if the estimate is wrong (htmx polling, bots on `/verify/`). Mitigated by
   the skip rules, `is_htmx` flag and retention job; the admin shows the row count.
2. **Unclassified new views** silently landing as `PAGE`. Mitigated by the guard test on
   the sensitive URL prefixes.
3. **The log itself is sensitive** (it reveals who HR is looking at, and failed-login
   emails). Restricted to User Admins + superusers; never exported by beat.
4. **Write failure masking**: a broken audit insert is swallowed by design. The exception is
   logged to `orbis.log` at ERROR, and the daily `prune` job's `ScheduledTaskRun` output
   reports the last 24 h row count so a silent stop is visible.

## 8. Decisions (taken 2026-10-09)

1. **Scope — everything.** Every authenticated request that reaches a view, plus admin
   reads, plus the anonymous public verify pages and auth flow.
2. **Retention — 24 months, nightly beat job, MySQL `REVOKE UPDATE`.** Delete by default;
   `prune_access_log --archive` writes the pruned rows to `logs/` first for anyone who
   wants a cold copy.
3. **Readers — User Admins, superusers, and a new "Audit Viewers" group** (seeded by
   `seed_orbis`). The group exists only for this purpose and is assigned to individuals on
   demand under company policy. HR is not a reader.
4. **Admin reads included.**
5. **Employee-profile "Access history" card — shipped** (§3.5), visible to the same readers.

Two small deviations from §3 as first written, made while building:

- `action` has no separate `LIST` value — list versus detail is already visible from the
  route name, and one fewer value keeps the filter honest. VIEW covers both.
- Admin routes classify by *model* (`ADMIN_MODEL_CLASS` in `audit/registry.py`): a
  superuser opening a salary structure in the Django admin files under SALARY, not ADMIN,
  so a "who looked at salary data" filter cannot be sidestepped through `/admin/`.

## 9. Follow-ups, not in this plan

- Weekly digest email to the CTO: salary/tax reads by actor, denied attempts, failed logins.
- Anomaly rules (one actor opening > N profiles in an hour).
- Shipping rows off-box (SIEM / S3) for tamper evidence beyond the MySQL `REVOKE`.
- MySQL audit plugin for raw-SQL access by DB users, which this app-level log cannot see.
