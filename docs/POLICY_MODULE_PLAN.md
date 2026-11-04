# Policy Acknowledgement Module — Implementation Plan

> **Status:** ✅ Implemented 2026-06-24 (ahead of the 2026-07-01 deadline). The `policies` app
> is live in the codebase: 5 models + services + signals + forms + views + templates + admin +
> `send_policy_reminders` command, 18 passing tests (`python manage.py test policies
> --settings=core.settings_test`), migrations applied to the dev DB, `Policy Managers` group +
> nav seeded, and the `EMAIL_USE_SSL` config bug fixed. Remaining before org-wide first publish:
> the manual staging smoke (managers re-upload the July-1 policies) and a real end-to-end mail
> send on staging. **Target ship date: 2026-07-01.**
> First in-house GRC slice for Orbis (replacing the policy/sign-off part of the commercial
> TeamMate / StandardFusion tenant).

---

## Context

SEBPO currently runs policy distribution and employee sign-off inside a commercial GRC tool
(TeamMate / StandardFusion — see `TeamMate_StandardFusion_Risk_Compliance_Platform.pdf`). The
larger goal is to bring that GRC capability in-house into **Orbis**. The full platform
(Organization/Controls, Risk register, Compliance/Standards, Vendors, Tasks, 15 reports) is a
multi-month effort and is **explicitly deferred ~6 months**.

The **immediate, hard-deadline need (2026-07-01)** is the **Policy** slice only, and in its
simplest defensible form:

1. A policy manager uploads a **PDF** policy document, with **versioning**.
2. They **map which employees must sign** each policy (the "adherents").
3. Those employees receive an **email asking them to sign**.
4. When an employee signs, the **acknowledgement is recorded** (who, when, which version, audit).
5. Employees who have **not yet signed are re-emailed** (reminders).

This plan covers exactly that. It deliberately leaves out rich-text policy authoring, multi-step
approval chains, and every non-Policy GRC module.

---

## Goals / Non-goals

**Goals (in scope for July 1):**
- Upload + version PDF policies (Draft → Active → Retired lifecycle).
- Policy/adherent management is granted through a single dedicated **`Policy Managers`** group
  (plus Admins/superusers). Both **HR and NSA** staff (NSA is part of the broader IT org) who do
  this work get added to that group — decoupling policy power from the HR group entirely.
  Enforced via `POLICY_MANAGER_GROUPS = {"Admins","Policy Managers"}`, NOT by reusing the
  HR/Admins-only `accounts.can_edit_all` (managers must get policy power *without* gaining
  employee-profile write scope).
- Target adherents by `auth.Group` with an **"All Employees" default** (every active employee
  signs unless a policy is deliberately narrowed), **plus** optional **custom individual
  adherents** via a multiselect; final adherent set = (active users in target groups) ∪
  (custom adherents). Materialise per-employee sign requests.
- The custom-adherent multiselect is **filtered to active employees not already adherents/signers
  of that policy** — it surfaces only people still missing coverage (typically new joiners) and
  prevents duplicate/re-add of someone who already signed.
- Email each adherent a signing request on publish.
- Authenticated, audited click-to-acknowledge ("I have read and accept") capturing user, UTC
  timestamp, policy version, IP, user-agent.
- Manager acceptance dashboard: signed vs outstanding counts + outstanding list.
- Manual "Send reminders" action **and** a scheduled reminder management command for outstanding
  signers.
- New policy version re-opens acknowledgement (configurable per version).

**Non-goals (deferred ~6 months / later phases):**
- Rich-text (CKEditor) policy authoring — we upload PDFs, not compose them.
- Multi-step review/approval chain before publish (managers publish directly for now).
- Drawn/typed signature capture and per-acknowledgement PDF receipts (defer; the `documents`
  app already has the WeasyPrint+QR pattern to graft on later).
- Risk, Compliance/Standards, Controls, Exceptions, Vendors, Questionnaires, Tasks, Reports
  beyond the single acceptance summary.
- Public (unauthenticated) signing — all signing happens behind Orbis login/SSO.

---

## Reuse map (do not reinvent)

This module is mostly an assembly of patterns already proven in Orbis:

| Need | Reuse from | Specifics |
|---|---|---|
| Audit fields | each app defines `_Auditable` (`documents/models.py`, `offboarding/models.py`, `leave/models.py`) | `added_by/added_at/updated_by/updated_at`; define one in `policies/models.py` to match convention |
| Append-only acknowledgement ledger | `leave` `LeaveBalanceTransaction` (`leave/models.py`, `leave/services.py`) | one row per state change; denormalised status on the parent for fast reads |
| Email-on-event | `offboarding/signals.py`, `documents/signals.py` | `post_save` + `transaction.on_commit(...)` notify pattern; reuse the existing notification/email helper these call |
| File upload + media | `documents` `DocumentRequest.generated_file` FileField + `MEDIA_ROOT` config in `core/settings.py` | store PDFs under `media/policies/<policy>/v<n>.pdf` with an upload_to callable |
| Scheduled job | `offboarding` `deactivate_past_lwd` mgmt command + `leave` monthly accrual | `policies/management/commands/send_policy_reminders.py` |
| Navigation | `dynamic_menu` (`MainMenu`/`SubMenu` M2M to `auth.Group`, `dynamic_menus` context processor, `sync_protected_views`) | seed nav rows via data migration/fixture; `ProtectedView` for non-sidebar views |
| Create/edit UX | htmx-drawer `SectionEditView` pattern (`employees/views.py` + `templates/employees/_drawer.html`) | branch on `request.htmx` for fragment vs full page |
| Form styling | `core.forms.StyledMixin` + `employees/forms.py::_Base` | `form-input` on every widget incl. `<select>`; `accounts/_field.html` partial |
| Scope/permissions | `accounts/permissions.py` | reuse `get_visible_employees` for an employee's own "to sign" list; define a **new** `policies.services.can_manage_policies(user)` checking `POLICY_MANAGER_GROUPS = {"Admins","Policy Managers"}` (do **not** reuse `can_edit_all`, which is HR/Admins-only and tied to profile scope) |
| App wiring | `core/settings.py` INSTALLED_APPS, `core/urls.py`, `documents/apps.py` `ready()` signal import | add `policies.apps.PoliciesConfig`; `path("policies/", include("policies.urls"))` |

---

## Domain model (`policies/models.py`)

```
Policy
  id            UUID pk
  key           CharField unique        # "P-1", "P-2" — auto-assigned (see Key assignment)
  title         CharField
  category      CharField blank         # free text or FK to a small PolicyCategory table (optional)
  description   TextField blank         # short summary (plain text; the real content is the PDF)
  owner         FK CustomUser           # accountable policy owner
  status        Draft | Active | Retired
  target_groups M2M auth.Group          # "adherents from group"; "All Employees" default
  custom_adherents M2M CustomUser        # individuals added on top of groups (form queryset filtered to non-signers)
  due_days      PositiveInt null        # acknowledge-by window after publish/assignment
  _Auditable

PolicyVersion                            # versioning = append a row
  id            UUID pk
  policy        FK Policy -> versions
  version       PositiveInt             # 1,2,3...; unique per policy
  pdf           FileField(upload_to=policy_pdf_path)   # the uploaded PDF
  change_note   CharField blank
  requires_reack BooleanField default True   # new version re-opens signing
  is_current    BooleanField            # exactly one current per policy
  published_at  DateTimeField null
  _Auditable
  unique_together (policy, version)

PolicyAcknowledgement                     # one per (employee, policy) — current sign state
  id            UUID pk
  policy        FK Policy -> acknowledgements
  employee      FK CustomUser
  version       FK PolicyVersion null     # the version they signed (null until signed)
  status        PENDING | SIGNED | SUPERSEDED   # SUPERSEDED when a new requires_reack version lands
  signed_at     DateTimeField null
  signed_ip     GenericIPAddress null
  signed_user_agent CharField blank
  notified_at   DateTimeField null        # last email sent
  reminder_count PositiveInt default 0
  _Auditable
  unique_together (policy, employee)

PolicyAcknowledgementEvent                # append-only audit ledger (leave-ledger pattern)
  id            UUID pk
  acknowledgement FK PolicyAcknowledgement -> events
  event         ASSIGNED | NOTIFIED | REMINDED | SIGNED | SUPERSEDED | REVOKED
  version       FK PolicyVersion null
  occurred_at   DateTimeField
  actor         FK CustomUser null        # who triggered (system/manager/the employee)
  ip            GenericIPAddress null
  note          CharField blank
```

**Key assignment ("P-1"…):** no global sequence app exists in Orbis. Mirror the documents-app
approach with a tiny per-model counter: on `Policy` create, assign
`key = "P-%d" % (next)` inside a `transaction.atomic()` + `select_for_update`
on a 1-row `PolicyCounter` singleton (avoids race + gaps). Keep it isolated in `services.py`.

**Why a denormalised `PolicyAcknowledgement` + append-only `...Event`:** the dashboard needs fast
"X of Y signed / who's outstanding" reads (denormalised `status`), while compliance needs an
immutable trail of every assignment, notification, reminder, and signature (the Event ledger) —
exactly the split `leave` uses between `LeaveBalance` and `LeaveBalanceTransaction`.

---

## Workflow

**Authoring & publish (policy manager):**
1. Create a `Policy` (Draft) and upload the first `PolicyVersion` (v1) — htmx drawer.
2. A policy manager (anyone in the **Policy Managers** group — HR/NSA staff — or an Admin) sets
   `target_groups` (defaults to "All Employees") and optionally adds `custom_adherents`
   (multiselect filtered to active employees not yet adherents/signers). **Since 2026-10-28
   `target_groups` is a checkbox list, not a native `<select multiple>`** — a plain click on a
   multi-select can only replace the selection, never empty it, so a manager who added one
   custom adherent and believed no group was chosen still circulated to all ~1,500 employees.
   The resolver's rule is unchanged: no groups ticked means nobody via groups, only the named
   individuals. The detail page now has a **"Who must sign"** panel (groups, named individuals,
   and the live headcount from `services.target_adherent_ids`) and the Circulate confirm names
   that headcount, so the reach is visible *before* the button is pressed.
3. A manager clicks **Publish** → `Policy.status = Active`, `PolicyVersion.is_current = True`,
   `published_at = now`. A service `assign_adherents(policy)` materialises
   `PolicyAcknowledgement` rows (PENDING) for the union of (active users in target groups) and
   (custom adherents) (idempotent: skips existing rows), writing an `ASSIGNED` + `NOTIFIED` event
   and sending the signing email on `transaction.on_commit`. This same call can be re-run after
   adding custom adherents later to top up coverage for new joiners.

**Retire / reset (2026-10-28):** retiring a policy stops reminders, hides it from every
employee's "To sign" list (`MyPoliciesView` filters outstanding rows to Active policies — the
sign page already refused a retired policy) and refuses new assignments, but **leaves the
acknowledgement rows in place**, so the detail page keeps counting them as outstanding and the
custom-adherent picker keeps hiding everyone already covered. A **"Reset signing data"** button
on a Retired policy (`PolicyResetView`, Policy Managers, same department scoping as every other
action) runs `services.reset_signing_data` for that one policy — the UI counterpart of
`manage.py reset_policy_signing_data` — deleting its acknowledgements and events, clearing
`published_at`, and returning it to Draft with title and PDFs intact so it can be re-targeted
and circulated again. The Retire confirm names the pending count and points at this button.

**Signing (employee):**
4. Email links to `policies/<id>/sign/` (login/SSO required). The page renders the policy
   metadata + an **embedded PDF viewer / download** of the current version + an
   "I have read and accept" checkbox and **Sign** button.
5. On POST: set `status=SIGNED`, `signed_at`, `version=current`, capture `signed_ip` +
   `signed_user_agent` from the request, append a `SIGNED` event. Guard against double-sign and
   signing a superseded version.

**Reminders (outstanding signers):**
6. "Send reminders" button → `remind_outstanding(policy)` emails all PENDING adherents,
   bumps `reminder_count`, sets `notified_at`, appends `REMINDED` events.
7. `python manage.py send_policy_reminders` (scheduled nightly) does the same across all Active
   policies for adherents PENDING beyond a configurable interval — reuses the offboarding
   management-command pattern.

**New version / re-acknowledgement:**
8. A manager uploads a new `PolicyVersion` (v2) with `requires_reack=True` → it becomes current;
   all `SIGNED` acknowledgements for that policy flip to `SUPERSEDED` (PENDING), a `SUPERSEDED`
   event is appended, and a fresh signing email goes out. (If `requires_reack=False`, prior
   signatures stand.) This matches the TeamMate "N acceptances due for this policy" behaviour.

**Retire:** a manager sets `status=Retired` → policy stops generating new assignments/reminders;
historical acknowledgements remain for audit.

---

## Files / work breakdown

New app `policies/` (follow `documents/` layout — single `models.py`, `services.py`, `signals.py`,
`views.py`, `forms.py`, `urls.py`, `admin.py`, `apps.py`, `management/commands/`, templates under
`templates/policies/`):

- `policies/models.py` — the 5 models above + `_Auditable` + `policy_pdf_path` upload_to.
- `policies/services.py` — `assign_adherents`, `publish_policy`, `sign`, `remind_outstanding`,
  `supersede_on_new_version`, `next_policy_key`, `can_manage_policies`. All business logic here
  (not in views/signals).
- `policies/signals.py` — `post_save` hooks that fire emails via `on_commit` (reuse existing
  notify helper); imported from `PoliciesConfig.ready()`.
- `policies/forms.py` — `PolicyForm` (incl. `target_groups` + `custom_adherents`;
  `custom_adherents` widget queryset filtered in `__init__` to active employees not already
  adherents/signers of this policy), `PolicyVersionUploadForm`, `SignForm`
  (StyledMixin; PDF-only `FileField` with content-type + size validation).
- `policies/views.py` — list, detail (with acceptance dashboard), create/edit drawers
  (htmx `SectionEditView` pattern), publish, upload-version, sign, send-reminders,
  "My policies to sign" for employees.
- `policies/urls.py` — wire under `policies/`; add `path("policies/", include("policies.urls"))`
  to `core/urls.py`.
- `policies/admin.py` — Policy with PolicyVersion + Acknowledgement inlines; read-only Event log.
- `policies/management/commands/send_policy_reminders.py` — nightly reminder job.
- `templates/policies/` — `policy_list.html`, `policy_detail.html` (Overview + Acceptance tabs),
  `_drawer.html` (create/edit + version upload), `sign.html`, `my_policies.html`.
- Nav + groups: data migration ensuring the **`Policy Managers`** group exists (alongside existing
  `Admins`/`HR`/`All Employees`), and adding a `MainMenu` "Policies" (Lucide `file-check` icon) +
  submenus — management submenus ("All Policies") scoped to **Policy Managers + Admins**, and a
  "To Sign" submenu scoped to **All Employees**; run `sync_protected_views` for non-sidebar views
  (sign, publish, upload-version, reminders), gating manager-only views via `can_manage_policies`.
- `core/settings.py` — add `policies.apps.PoliciesConfig` to INSTALLED_APPS; **fix email config**
  (add `EMAIL_USE_SSL = _env_bool("EMAIL_USE_SSL", False)`; reconcile TLS-vs-SSL for port 465 so
  mail actually sends) and verify with a `send_mail` localhost smoke test before building reminders.
- `docs/PLAN.md` — register the new `policies` app in the app list + a short section (CLAUDE.md
  requires PLAN.md stay the source of truth when a new app is added).
- `policies/tests.py` — see Verification.

**Email templates:** plain Django templates for "Please sign", "Reminder", "New version — please
re-sign", rendered through the existing notification/email helper used by `offboarding`/`documents`.

---

## Verification

Run with the SQLite test settings (`python manage.py test policies --settings=core.settings_test`).

Automated tests:
- `assign_adherents` materialises exactly the active users in target groups; idempotent on re-run.
- Publish sends one email per pending adherent; `on_commit` fires.
- Sign sets status/version/ip/ua, appends a `SIGNED` event, blocks double-sign.
- New `requires_reack` version supersedes prior signatures and re-notifies; `requires_reack=False`
  leaves them SIGNED.
- `send_policy_reminders` only emails PENDING adherents past the interval and bumps counters.
- Permissions: a member of **Policy Managers** (HR/NSA staff) can create/publish/upload/manage
  adherents and see acceptance; a plain employee (not in the group) cannot, and sees only their
  own "to sign" list; Policy Managers membership does **not** grant employee-profile write scope
  (`can_edit_all` unaffected).
- Custom-adherent multiselect excludes already-signed/already-adherent employees; adding a new
  joiner as a custom adherent and re-running `assign_adherents` creates exactly one new PENDING
  row + signing email.

Manual smoke (on `orbis.sebpo.net` staging before July 1):
1. A manager creates a policy, uploads a PDF, targets "All Employees", publishes.
2. A test employee receives the email, opens the sign page, views the PDF, accepts → the dashboard
   shows them as signed with timestamp + version.
3. A second employee ignores it → "Send reminders" re-emails only them.
4. A manager uploads v2 (requires_reack) → previously-signed employee shows as due again.

---

## Timeline to 2026-07-01 (~1 week)

| Day | Work |
|---|---|
| D1 | App scaffold, models + migrations, `_Auditable`, key counter, admin |
| D2 | `services.py` (assign/publish/sign/supersede) + unit tests |
| D3 | Views + forms + templates: list, detail/acceptance, create/upload drawers |
| D4 | Sign flow (page, PDF embed, audit capture) + "My policies to sign" |
| D5 | Signals + email templates + `send_policy_reminders` command + reminders button |
| D6 | Nav wiring, permissions, full test pass, PLAN.md update |
| D7 | Deploy to `orbis.sebpo.net`, manual smoke with managers, buffer |

---

## Decisions / resolved items (from review)

- **Email / SMTP** — configured in `.env` for both local and prod; final prod address + credentials
  get swapped in later. **Pre-launch fix required (found during localhost check):** `.env` uses
  `EMAIL_PORT=465` + `EMAIL_USE_SSL=True`, but `core/settings.py` (lines 262–268) only wires
  `EMAIL_USE_TLS` and never reads `EMAIL_USE_SSL` → Django attempts STARTTLS on a port that needs
  implicit SSL and the send fails. **Fix:** add `EMAIL_USE_SSL = _env_bool("EMAIL_USE_SSL", False)`
  to settings and ensure TLS/SSL aren't both enabled. First implementation step is a `send_mail`
  smoke test against localhost to confirm delivery. (This path is also used by the `documents`
  app, so the fix likely helps it too.)
- **Adherent volume** — ~1,785 adherents. For **July 1**, publish/remind batch-write events with
  `bulk_create` and send mail in chunks **synchronously** inside the request/management command —
  acceptable at this scale. **Async at scale (Celery + Redis + celery-beat) will be added to the
  project later** (already anticipated in `docs/PLAN.md`); the reminder command is written so it
  can later be wrapped as a Celery task with minimal change.
- **"All Employees" group freshness** — **resolved**: every user is added to the `All Employees`
  group at creation, so new hires automatically become adherents of org-wide policies.
- **Legacy policies** — **not importing** existing SEBPO policies for now; Policy Managers will
  re-upload the handful needed for July 1. Bulk import remains a later nice-to-have.
- **PDF rendering in-browser** — use the **browser's native PDF viewer** (`<iframe>`/`<object>`)
  plus a download link; no JS PDF library bundled.

## Remaining risks

- **Live mail delivery** — Gmail SMTP (app password) can rate-limit / be flagged for bulk sends;
  validate a real end-to-end send on staging before the org-wide first publish.
- **Synchronous send latency** — a single org-wide publish/reminder to ~1,785 recipients runs in
  one request/command; chunk sends and, if it's slow pre-Celery, trigger the big sends via the
  management command (off the web request) until Celery lands.

---

## Deferred (future GRC phases, ~6 months)

Controls, Exceptions, Risk (analyses/risks/scoring/assets/threats/vendors/questionnaires),
Compliance (standards library, programs, audits, requirement↔control mapping), Tasks, the 15
report types, approval chains, rich-text authoring, drawn-signature + PDF receipts, public verify
URLs. Each will be its own Orbis app, reusing the same patterns catalogued above.
