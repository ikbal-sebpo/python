# Shift Change Request — implementation plan

Status: **shipped 2026-10-01** (all of §12; the optional grid badge and
read-only "Request change" link in §10 included). Companion to `docs/PLAN.md`
("Shift change requests"). Record reversals here, same posture as
`docs/PAYROLL_PLAN.md`. Tests: `roster/tests_shift_change.py`.

## 1. What we're building

An employee (or HR / a manager on their behalf) files a request to change
the shift **and the work location** they're rostered on for one date, *after*
the roster has been published. Their supervisor approves or declines. On
approval the request writes both values straight onto `roster.Shift` for
that date — creating the row if the day was only covered by the weekly
`Roster` pattern.

The request/decide flow is a deliberate copy of `leave.LeaveApplication`
(`/leave/apply/` → detail → approve/decline/cancel, Mine/Pending/All tabs),
and the "who may act" rules are **exactly** the leave rules — the three new
permission helpers are one-line delegations to the existing leave ones, not
re-implementations.

## 2. What already exists and gets reused

| Need | Already in the codebase | Used how |
|---|---|---|
| Who can file / act / cancel | `accounts/permissions.py::can_apply_leave_for`, `can_act_on_leave`, `can_cancel_leave` | New helpers delegate to these (§5). |
| Department-scoped shift dropdown | `roster/services.py::visible_shift_types(department_id, include_attr_ids)` + the optgroup builder in `roster/forms.py::CellForm.__init__` | Extracted into a shared helper (§6). |
| Tamper-proof server-side resolve of the posted option | `roster/views.py::_resolve_shift_ref(ref, department_id, include_attr_ids)` | Moved to `roster/services.py` so the form can call it. |
| "What is this person's shift on that date" | `attendance/views.py::_resolve_shifts` order: `Shift` row wins, else `Roster` pattern, with tenure cutoffs | Same order for the "current shift" snapshot + preview. |
| Writing the shift | `Shift.objects.update_or_create(...)` as in `CellUpdateView` and `leave.services.materialise_shifts_for_application` | Same call, plus the `added_by` backfill. |
| Location dropdown + its FK mapping | `accounts/locations.py::LOCATION_DROPDOWN_CHOICES` (`MHK/MBD/RAOWA/WFH`), `resolve_location_choice(code) -> (job_location, work_location)`, and `roster_tags.resolved_location_choice(shift, employee)` for the current value | Same four-value code the grid cell uses; FKs resolved only at apply time. |
| Audit trail of the actual roster change | `roster.ShiftChangeLog` (signal-driven on every `Shift` save) | Free — approval goes through a normal `save()`, `changed_by` = the approver. |
| Leave-ledger / CpL side effects | `leave/signals.py::sync_shift_leave_ledger`, `grant_cpl_on_holiday_work` | Free for the same reason — never `bulk_create` here. |
| Tenure guards | `roster.services.shift_is_after_last_working_date` / `shift_is_before_joining_date` | Checked at filing *and* re-checked at approval. |
| List page plumbing | `accounts.filters` (q/department/team), `core.pagination.paginate_list`, `.page-fill` / `.table-scroll` / `_pagination.html` | Same as `LeaveListView`. |
| Dashboard "Pending approvals" card | `core/dashboard.py::_APPROVAL_TYPES` | One new entry. |
| Date input | `core.forms.use_date_picker` (Flatpickr) | Same as `LeaveApplicationForm`. |

Nothing new is added to `Shift` itself. Leave needed a `leave_application`
FK on `Shift` because it has to find and delete its own rows on cancel;
a shift change is a one-shot overwrite, and `ShiftChangeLog` already records
before/after/who. (See §9 on why an approved change is not reversible from
the request.)

## 3. Decisions to confirm before building

These change the shape of the work. Defaults are what the plan below assumes.

1. **One date per request** (default) vs a start/end range. The request
   snapshots the *current* shift to show a before/after diff, which is
   per-date; a range would mean one row per date anyway. Start with one date;
   a range later is "loop and create N rows", nothing structural.
2. **Which shift types can be requested.** Default: `WORK` and `OFF`
   categories only. `LEAVE` types are owned by the leave module (balances,
   sandwich rule) and must go through `/leave/apply/`; `HOLIDAY` is a
   calendar fact, not a personal choice. A day that is *currently* a leave
   day (any LEAVE-category `Shift`, whether from an approved application or a
   roster-direct upload) is refused at filing: "cancel the leave first".
3. **"Department shift types" means `ShiftAttribute.departments`.** Today
   the department scoping lives on the attribute (e.g. "Evening / 2PM start"
   is limited to AdOps), not on `ShiftType` — `docs/PLAN.md` records that
   `ShiftType` carries no department field on purpose, since every WORK type
   has attributes. The request dropdown therefore shows exactly what the
   roster grid would show for that employee's department. If HR actually
   wants a *type-level* restriction, that's a data-model change to raise
   separately, not part of this feature.
4. **Date window.** Default: today or later for a self-service request;
   HR/Admins (`can_edit_all`) may file any date. A past-date shift change
   alters payroll inputs (night-shift allowance, OT basis, meal deductions)
   and the attendance "Unaccounted" check, so the ordinary path shouldn't
   rewrite history silently. Not a hard block for HR, matching the payroll
   "soft gate" posture.
5. **Location is part of the request (confirmed 2026-10-01).** Every
   request carries a `MHK/MBD/RAOWA/WFH` choice alongside the shift, both
   required, and approval writes both onto the `Shift` row. The location
   is stored as the four-value *code* the grid dropdown uses, not as the
   two FKs — `resolve_location_choice` maps it to `(job_location,
   work_location)` at apply time, the same way `CellUpdateView` does, so
   the canonical `JobLocation`/`WorkLocation` rows are looked up (or
   created) once, at the moment of writing. A request may change only the
   shift, only the location, or both; what's refused is a request that
   changes neither (§6, rule 5).
6. **No email notifications.** Leave sends none today; keep parity. The
   dashboard pending-approvals card is the manager's inbox.

## 4. Data model — `roster.ShiftChangeRequest`

Lives in `roster` (it's about `Shift`), next to `ShiftChangeLog`.

```python
class ShiftChangeRequest(_Auditable):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    employee = FK(AUTH_USER_MODEL, PROTECT, related_name="shift_change_requests")
    date = DateField()

    # Snapshot of what was on file when filed — the "from" half of the diff
    # shown for review, frozen so a later roster edit can't rewrite it.
    # Resolved Shift-row-else-pattern at filing time; both null = unscheduled.
    original_shift_type = FK(ShiftType, SET_NULL, null=True, related_name="+")
    original_shift_attribute = FK(ShiftAttribute, SET_NULL, null=True, related_name="+")
    original_was_concrete = BooleanField()   # True = a Shift row existed, False = pattern fallback
    # Resolved via roster_tags.resolved_location_choice (row override, else the
    # employee's contract location); "" = neither resolved to a tracked office/WFH.
    original_location = CharField(max_length=5, blank=True, default="")

    requested_shift_type = FK(ShiftType, PROTECT, related_name="+")
    requested_shift_attribute = FK(ShiftAttribute, SET_NULL, null=True, related_name="+")
    requested_location = CharField(max_length=5, choices=LOCATION_DROPDOWN_CHOICES)
    reason = TextField()

    status = CharField(choices=Status, default=PENDING)
    applied_by = FK(AUTH_USER_MODEL, PROTECT, related_name="+")
    applied_at = DateTimeField(default=timezone.now)
    processed_by = FK(AUTH_USER_MODEL, SET_NULL, null=True, related_name="+")
    processed_at = DateTimeField(null=True)
    process_note = CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-date", "-applied_at"]
        indexes = [
            Index(fields=["employee", "status"]),
            Index(fields=["status", "date"]),
            Index(fields=["employee", "date"]),
        ]
```

- `requested_shift_attribute` set ⇒ `requested_shift_type` is its parent
  (same invariant as `Shift.save()`; enforced in `clean()`).
- `clean()` also rejects `requested_shift_type.category` outside
  `{WORK, OFF}` (decision 2) and a request where *both* the shift pair and
  the location equal the originals (a no-op request). Changing just one of
  the two is a valid request.
- `requested_location` is validated against `LOCATION_DROPDOWN_VALUES`, so
  a tampered POST can't smuggle in an arbitrary string.
- "At most one PENDING per (employee, date)" is enforced in the form and
  re-checked inside the approve transaction — MySQL has no partial unique
  index, same situation as `LeaveApplication` overlap.
- **Migration:** new table with a `CustomUser` FK ⇒ copy
  `_match_existing_collation` from `employees/migrations/0008_designationhistory.py`,
  target `roster_shiftchangerequest`, `atomic = False`. (`deploy/README.md`,
  "MySQL collation".)

## 5. Permissions — `accounts/permissions.py`

Three helpers, each a delegation so the rules can't drift from leave:

```python
def can_request_shift_change(user, target, request=None) -> bool:
    """Same rule as can_apply_leave_for."""
    return can_apply_leave_for(user, target, request)

def can_act_on_shift_change(user, req, request=None) -> bool:
    """Same rule as can_act_on_leave: HR/Admins anyone incl. self; others
    their managed scope, never their own."""
    return can_act_on_leave(user, req, request)      # duck-typed on .employee_id

def can_cancel_shift_change(user, req, request=None) -> bool:
    """Applicant may cancel while PENDING; managers/admins per can_act_on_shift_change."""
    if user.is_authenticated and req.employee_id == user.pk and req.is_pending:
        return True
    return can_act_on_shift_change(user, req, request)
```

`can_act_on_leave` only reads `application.employee_id`, so passing a
`ShiftChangeRequest` works without changes. Visibility of rows mirrors
`leave/views.py::_visible_leave_qs` (`can_see_all` → all, else
`visible_employee_pks`).

## 6. Service layer — `roster/services.py`

Move + add, so views and forms share one implementation:

- **Move** `_resolve_shift_ref` from `roster/views.py` to
  `services.resolve_shift_ref` (views keep a thin import). It already does
  the department re-validation the dropdown enforces, which is what stops a
  tampered POST from requesting an out-of-department attribute.
- **Extract** the optgroup builder from `CellForm.__init__` into
  `services.shift_ref_choices(department_id, include_attr_ids=(), categories=None)`
  returning the `[(group_label, [(value, label), ...]), ...]` list. `CellForm`
  calls it with `categories=None` (today's behaviour); the request form calls
  it with `categories={WORK, OFF}`.
- **Add** `services.resolve_current_cell(employee, day) -> CurrentCell(shift_type, shift_attribute, location, is_concrete)`
  — `Shift` row else `Roster` pattern else empty, with the tenure cutoffs
  applied to the pattern branch exactly as `attendance/views.py::_resolve_shifts`
  does. `location` is the `MHK/MBD/RAOWA/WFH` code from
  `resolved_location_choice(row, employee)` (row override, else the
  employee's contract location, else `""`). Used for the snapshot at
  filing and the live preview.
- **Add** `services.apply_shift_change(req, approver)`:

  ```python
  @transaction.atomic
  def apply_shift_change(req, approver):
      # re-validate: still PENDING, tenure guards, no LEAVE shift on that date,
      # requested attribute still active + in scope for req.employee.department_id
      jl, wl = resolve_location_choice(req.requested_location)
      Shift.objects.update_or_create(
          employee=req.employee, date=req.date,
          defaults={
              "shift_type": req.requested_shift_type,
              "shift_attribute": req.requested_shift_attribute,
              "job_location": jl,
              "work_location": wl,
              "updated_by": approver,
              "note": f"Shift change #{req.pk}",
          },
      )
      Shift.objects.filter(employee=req.employee, date=req.date,
                           added_by__isnull=True).update(added_by=approver)
  ```

  This is the same `defaults` set `CellUpdateView` writes, so a request
  approval and a grid edit produce identical rows — and identical
  `ShiftChangeLog` entries, which already carry `previous_/new_job_location`
  and `previous_/new_work_location`. `note` is overwritten the same way leave
  materialisation overwrites it; `ShiftChangeLog.previous_note` keeps the
  old value.

Validation that runs in both the form and the approve view (one function,
`services.validate_shift_change(req)` raising `ValidationError`):

1. tenure: not before `joining_date`, not after `last_working_date`;
2. date window (decision 4) — bypassed when `applied_by` has `can_edit_all`;
3. the date isn't a leave day (existing `Shift` with `category == LEAVE`, or
   a PENDING/APPROVED `LeaveApplication` covering it);
4. no other PENDING request for the same `(employee, date)`;
5. at least one of (shift pair, location) differs from the current cell;
6. requested category in `{WORK, OFF}`;
7. `requested_location` is one of the four dropdown codes.

## 7. Forms — `roster/forms.py`

`ShiftChangeRequestForm(forms.ModelForm, StyledMixin)` — fields `date`,
`shift_ref` (a `ChoiceField`, choices from `shift_ref_choices(employee.department_id, categories={WORK, OFF})`),
`requested_location` (a `ChoiceField` over `LOCATION_DROPDOWN_CHOICES`,
required — same four options as the grid cell, no blank), `reason`.
`__init__(employee=...)` like `LeaveApplicationForm`;
`use_date_picker(self, "date")`; `clean()` resolves `shift_ref` through
`services.resolve_shift_ref(..., department_id=employee.department_id)`,
snapshots the original shift *and* location via `resolve_current_cell`,
builds a transient instance and runs `validate_shift_change`, merging
errors the way `LeaveApplicationForm._merge` does.

The apply page's preview (§8, `change-requests/current/`) pre-selects both
dropdowns to the current values once a date is picked, so the user only
touches what they want changed — the no-op rule then catches an untouched
submit with a clear message.

## 8. Views + URLs — `roster/views.py`, `roster/urls.py`

Under `/roster/change-requests/`, names prefixed `shift_change_`:

| URL | View | Mirrors |
|---|---|---|
| `change-requests/` | `ShiftChangeListView` — tabs `mine` / `pending` / `all`, `?q=`/department/team via `accounts.filters`, `paginate_list`, counts | `LeaveListView` (minus calendar mode) |
| `change-requests/apply/` | `ShiftChangeApplyView` (`CreateView`, `?for=<pk>` on-behalf-of via `can_request_shift_change`) | `LeaveApplyView` |
| `change-requests/current/` | `ShiftChangeCurrentView` — POST `for`+`date`, returns JSON `{current_label, current_shift_ref, current_location, is_concrete, warnings[]}` for the live "Current shift on this date" panel and to pre-select both dropdowns | `LeavePreviewView` |
| `change-requests/<pk>/` | `ShiftChangeDetailView` — before/after diff, `can_act`, `can_cancel` | `LeaveDetailView` |
| `change-requests/<pk>/approve/` | `ShiftChangeApproveView` — `can_act_on_shift_change` + PENDING, calls `apply_shift_change` inside the transaction, htmx → `_row.html` OOB swap | `LeaveApproveView` |
| `change-requests/<pk>/decline/` | `ShiftChangeDeclineView` — `process_note` required | `LeaveDeclineView` |
| `change-requests/<pk>/cancel/` | `ShiftChangeCancelView` — PENDING only (§9) | `LeaveCancelView` |

Templates under `templates/roster/change_requests/`: `list.html`
(`.page-fill` + `.table-scroll` + `table-simple table-sticky` + `_pagination.html`),
`apply.html` (two-column like `leave/apply.html`: form left, "Current shift on
this date" preview right), `detail.html`, `_row.html`, `_row_actions.html`.
Columns: Employee · ID, Team, Date, From (original shift + location badge),
To (requested shift + location badge), Status, Filed, Actions. The location
badge uses the same `WFH` blue / office accent colouring as `_cell.html`,
and the detail page's diff highlights only the half that actually changed.

## 9. Lifecycle

```
PENDING ──approve──▶ APPROVED   (Shift written; terminal)
   │
   ├────decline──▶ DECLINED    (note required; terminal)
   └────cancel───▶ CANCELLED   (applicant or manager; terminal)
```

**No "un-approve".** Leave can withdraw a future approved application
because it owns its `Shift` rows via the FK. A shift change overwrites a
cell that may since have been edited again on the grid, re-uploaded, or
re-materialised by leave — restoring `original_*` could clobber a newer
legitimate value. If an approved change was wrong, the fix is a new request
or a manager editing the roster, both of which land in `ShiftChangeLog`.

## 10. Navigation, dashboard, seeding

- **Sidebar.** "Roster" is currently a *direct-link* main menu
  (`route_name="roster_week"`), and a main menu with a URL suppresses
  submenus (`templates/partials/sidebar.html`). Convert it to a section
  header with two submenus, the same shape "Timesheet" uses:
  `("Roster", "Roster", "roster_week", 10, all_staff)` and
  `("Roster", "Shift Changes", "shift_change_list", 20, ["All Employees", "User Admins"])`.
  In `seed_orbis.py` that means blanking the main-menu URL and adding the
  two `ORBIS_SUB_MENUS` rows — mind the "titles are the key" gotcha noted
  above the leave rows, and on prod run `seed_orbis --menus-only` after
  deploy (bare `seed_orbis` is broken; see project memory).
- **Roster grid.** Two small additions to `roster/_cell.html`, both
  optional for v1: a `badge-info` "Change pending" marker on a cell with a
  PENDING request (one extra query per grid build, keyed by
  `(employee_id, date)`), and, for a viewer who can't edit the cell
  (`can_edit_roster_pattern` false), a "Request change" link to
  `shift_change_apply?for=<pk>&date=<d>` instead of the dead `<select>`.
- **Dashboard.** Add to `core/dashboard.py::_APPROVAL_TYPES`:
  `{"key": "shift_change", "label": "Shift change requests", "icon": "refresh-cw", "list_url_name": "shift_change_list", "pending_qs": _pending_shift_change_qs, "eligible": has_managed_scope}`.
- **Admin.** Read-mostly `ShiftChangeRequestAdmin` with status/date filters.

## 11. Tests — `roster/tests_shift_change.py`

- Permission matrix: self file ✓; manager files for reportee ✓; outsider ✗;
  self-approve ✗ unless HR; manager approves reportee ✓; HR approves anyone ✓;
  cancel: applicant while PENDING ✓, after APPROVED ✗.
- Dropdown scoping: an attribute limited to another department is absent
  from the rendered form *and* a tampered POST with its pk is rejected.
- Filing guards: leave day refused; duplicate pending refused; before
  joining / after LWD refused; past date refused for self, allowed for HR;
  no-op (both shift and location equal current) refused; shift-only and
  location-only requests accepted.
- Approval effects: `Shift` created when only the pattern existed, updated
  when a row existed; `ShiftChangeLog` row with `changed_by` = approver and
  the location before/after columns filled; `job_location`/`work_location`
  match `resolve_location_choice(requested_location)` (WFH ⇒ job_location
  None + WFH work_location; an office code ⇒ canonical office + WFO);
  a location-only request leaves the shift pair untouched and vice versa;
  a tampered `requested_location` outside the four codes is rejected;
  a WORK→OFF change on a date that held a
  roster-direct leave shift is impossible (guard), so no ledger movement;
  a change *onto* a holiday-dated WORK shift triggers the existing CpL
  grant signal.
- Approval re-validation: a leave approved for that date between filing and
  approving makes approve fail with a message, request stays PENDING.
- List tabs: Mine/Pending/All counts and visibility match the leave rules;
  dashboard summary counts the new type.
- `CELERY_BEAT_SCHEDULE` untouched (no new periodic job).

## 12. Delivery order

1. Services (`resolve_shift_ref` move, `shift_ref_choices` extract,
   `resolve_current_cell`, `validate_shift_change`, `apply_shift_change`) +
   `CellForm` switched to the shared helper — no behaviour change, tests
   green.
2. Model + collation-aware migration + admin.
3. Permissions + form + apply/current/detail/action views + templates.
4. List view + tabs + filters + pagination.
5. Sidebar seed change, dashboard entry, optional grid badge/link.
6. `docs/PLAN.md` "Roster" section: one paragraph pointing here; CLAUDE.md
   `roster` bullet updated.

Steps 1–4 are one deployable unit; 5 can follow. Nothing in the plan
touches the leave ledger code, `Shift` columns, the uploader, or the API.

## 13. Dedicated night shift windows — department head approves (2026-11-03)

**Reverses "exactly the leave rule" (§5) for one case.** On a date inside an employee's **APPROVED** `employees.DedicatedNightShiftAssignment` window (`start_date ≤ date ≤ end_date`), the date belongs to the employee's **department head** (`Department.head_of_department`). Decided with the user on 2026-11-03:

- **Shift change requests** for such a date are approved, declined or cancelled (as a manager) by the department head **only**. That excludes Team Leaders, APMs, `Department.additional_managers`, HR, Admins and superusers; there is no HR fallback. The employee may still withdraw their own pending request. `accounts.permissions::can_act_on_shift_change` branches on `shift_change_needs_department_head`; `can_cancel_shift_change` inherits it.
- **The department head's own window** falls to their line manager (`JobInfo.reports_to`), since nobody approves their own (`roster/night_window.py::night_window_approver_id`).
- **Only dates inside the window count.** A date after it ends, or a window that is only PENDING or CANCELLED, keeps the leave rule.
- **Direct roster writes** inside the window may set only `NIG`, `CUST` or a non-working type (leave/off/holiday). Any other working shift needs the department head (`night_window.may_set_shift`). This applies to every write path:
  - **grid cell** (`CellUpdateView`): 403 + `HX-Error-Message`. A location-only change, or re-picking the shift already there, stays allowed. Non-heads see the restricted options disabled and a "Night" badge on the cell.
  - **weekly pattern** (`RosterPatternView._night_window_ok`): while a window covers today or later, a *changed* slot to another working shift is a form error.
  - **bulk upload**: `ApplyReport.skipped_night_window`, a hard skip.
  - **external API**: `conflict` / `dedicated_night_shift_window`, because a machine client is never the head.
- **The window is read live** from the same APPROVED rows payroll classifies night pay from. It is never stamped onto a request or Shift, so approving or cancelling a window takes effect at once.
- **Bell and list:**
  - `core.approvals.pending_shift_change_qs` keeps window-date requests out of everyone's inbox but the approver's, and gives the head their whole department's, not only direct reports'.
  - The list's "Direct reports only" switch does the same for the head.
  - Rows carry `can_decide` (Approve/Decline shown only when the viewer can act) or a "Needs department head" badge.
  - The detail page and the post-submit message name the approver.
- **Approving a window over existing day shifts** (option (b)): the shifts stay as rostered, but the approve (single or bulk) shows a warning listing them, e.g. "Rahim has 2 non-night working shifts in this window: 10 Dec MOR, 11 Dec MOR — they stay as rostered; ask Nadia to confirm or change them." It never blocks, like the payroll soft checks. The Dedicated Night Shift list keeps an "N non-night shifts" badge on an approved, current or future window until they are fixed (`night_window.conflicts_by_assignment`, two queries per page). Weekly-pattern days count, since the grid materialises them. **Only today onward is checked** (follow-up the same day): prod windows started as early as 2024 and run up to ~800 days, and the first deploy counted 1,771 "conflicts" across 82 windows, mostly days already worked and paid before the rule existed. It also scanned years of Shift rows.
- **No migration, no new model.** Tests: `roster/tests_night_window.py`.
