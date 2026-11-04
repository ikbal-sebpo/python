# Veridoc Document Process Implementation Plan

This plan tracks the remaining work to finish the document workflow in Orbis.
It intentionally excludes user-management and employee-profile work that is
already present in the codebase.

## Scope

In scope:
- document type configuration
- request creation and request detail flow
- review and approval workflow
- PDF generation and download
- public verification and preview
- action workflow for post-request operational steps
- queue and dashboard views
- admin/configuration screens
- seed data for starter document types
- tests for workflow rules and permissions

Out of scope:
- accounts/user CRUD
- employee profile sections
- roster/leave/offboarding implementation details unless they directly connect
  to document issuance

## Current State

The repository already contains a substantial `documents` app with:
- `DocumentType`, `DocumentRequest`, `RequestReview`, `RequestApproval`
- dynamic request fields
- PDF rendering helpers
- review/approval signals
- private document routes and templates
- seed command for starter document types

The remaining work is mostly:
- filling gaps between the current implementation and the full veridoc feature set
- tightening permissions and queue behavior
- finishing public verification/preview behavior
- adding coverage for edge cases

## Phase 1 Gap Audit

Legend:
- `done` = already implemented in the current `documents` app
- `partial` = present, but incomplete or not yet wired end to end
- `missing` = not present yet in the current codebase

### Document catalog and templates

- `DocumentType` catalog: `done`
- template design model: `done`
- message/body template model: `done`
- template versioning / revision log: `done`
- HTML sanitisation for stored templates: `done`
- admin surface for template editing: `done`
- template variable validation warnings: `done`

### Dynamic request form

- request form with runtime field generation: `done`
- support for text, textarea, number, date, boolean, and choice fields: `done`
- required-field validation: `done`
- saving request metadata to JSON: `done`
- saving extra field values to normalized rows: `done`
- document-type-specific field definitions: `done`
- typed coercion when storing values: `done`
- typed coercion when reading values back into the PDF context: `partial`
  - storage side exists
  - the audit should confirm the read side in `documents.pdf`

### Request lifecycle

- create request: `done`
- request detail page: `done`
- cancel request: `done`
- status model for pending / review / partial / approved / rejected / cancelled: `done`
- review chain creation: `done`
- approval chain creation: `done`
- rejection history: `done`
- current-step properties on request: `done`
- auto-approve path on document type: `partial`
  - model fields exist
  - the full branch needs verification in signals/services/PDF flow

### Review and approval UI

- review action form: `done`
- approval action form: `done`
- htmx drawer rendering: `done`
- full-page fallback rendering: `done`
- pending queue view: `done`
- HR/all-documents list: `done`
- queue filtering form: `done`
- remark-required-on-reject validation: `done`
- direct approval/review permission helpers: `done`

### PDF and public verification

- PDF rendering helper: `done`
- generated-file storage on request: `done`
- admin PDF test-render action: `done`
- employee download view: `done`
- public verification page: `done`
- public inline preview page: `done`
- public PDF stream endpoint: `done`
- QR/public verification URLs in helper layer: `done`
- approval-signature selection logic: `partial`
  - the current code exists, but the audit should verify fallbacks and ordering against the full veridoc contract

### Action workflow

- workflow code concept on document type: `missing`
- workflow template model: `missing`
- workflow step template model: `missing`
- workflow step field template model: `missing`
- workflow case model: `missing`
- workflow step instance model: `missing`
- workflow attachments: `missing`
- action queue views: `missing`
- action step action form: `missing`
- separation reporting based on action workflow: `missing`

### Public / external routes

- private document routes under `/documents/`: `done`
- public verification routes under `/verify/`: `done`
- no-auth public PDF preview route: `done`
- route protection via `ProtectedView`: `partial`
  - the infrastructure exists in `dynamic_menu`
  - this app’s routes still need to be checked against that access-control layer

### Admin and seed data

- `DocumentType` admin: `done`
- request admin with inlines: `done`
- extra field admin: `done`
- template revision admin: `done`
- branding singleton admin: `done`
- starter seed command: `done`
- starter NOC document type: `done`
- starter experience/release letter seeding: `done`
- group dependency on seed data: `done`

### Tests

- model tests for request/review/approval state transitions: `missing`
- form tests for dynamic request fields: `missing`
- view tests for queues / detail / cancel / download: `missing`
- public verification and preview tests: `missing`
- PDF generation regression tests: `missing`
- permission tests for reviewer/approver/admin visibility: `missing`

### Biggest Remaining Gaps

1. The action-workflow subsystem from the veridoc document is not present in the current `documents` app.
2. Automated tests for the document workflow are mostly missing.
3. The public verification / preview and PDF fallback behavior should be validated against the original contract.
4. Route protection should be checked against `ProtectedView` for any non-sidebar endpoints that need explicit database-driven access control.

## Phase 2 Findings: Workflow Core

### What is already working

- Request creation stores the base request row plus dynamic field values.
- `post_save` on `DocumentRequest` creates review rows when review configs exist.
- Review completion advances the request to approval or final approval depending on `requires_approval`.
- Approval completion advances the request to `APPROVED`.
- Rejections append to `rejection_history` instead of overwriting the prior reason.
- PDF generation is triggered on approval and saves the rendered file on the request.
- Notifications are deferred with `transaction.on_commit`, which avoids phantom emails on rollback.

### What is partial or needs explicit verification

- `DocumentType.auto_approve` exists, but the no-review/no-approval branch still needs a real seeded end-to-end check.
- The lifecycle is split across models, signals, and PDF helpers rather than a single service object.
- The PDF/signatory fallback path should be regression-tested with a request that has no signed approvals.

### What is still missing from this phase

- Automated regression tests for:
  - request creation with dynamic fields
  - review rejection
  - approval rejection
  - approval completion and PDF attachment
  - PDF generation failure handling
  - cancellation behavior
- A deliberate test for the `auto_approve` branch with starter seed data

### Phase 2 conclusion

The core lifecycle is substantially implemented. The remaining work for this phase is mostly verification and regression coverage, not a major feature rewrite.

## Phase 3 Findings: Document Configuration

### What is already working

- `DocumentType` has the expected configuration fields:
  - `code`
  - `name`
  - `display_name`
  - `description`
  - `template_design`
  - `message_template`
  - `requires_approval`
  - `auto_approve`
  - `auto_approve_reason`
  - `is_active`
- `DocumentType.clean()` enforces the auto-approve rule pairing.
- `CompanyBranding` exists as a singleton and is editable in admin.
- `TemplateDesign` and `MessageTemplate` are versioned and revision-tracked.
- HTML sanitisation is in place before save for rich templates.
- `DocumentExtraFieldType` supports all documented field kinds.
- `DocumentExtraFieldValue.clean()` checks the field belongs to the same document type.
- Admin screens exist for:
  - company branding
  - template design
  - message template
  - template revisions
  - document types
  - extra-field definitions
  - document requests
  - review configs
  - signatory configs
- The `seed_document_types` command seeds:
  - the branding singleton
  - the default A4 design
  - NOC message template
  - starter document types
  - review/signatory configs when the groups exist

### What is partial

- The admin `test_render_pdf` action exists and is wired, but the feature should still be exercised against seeded data to verify it renders all starter document types cleanly.
- Extra-field definitions are stored and validated, but the phase should confirm the admin UX is sufficient for HR to maintain them without code changes.
- Seed coverage exists for the starter document types, but the current command is still group-dependent and silently skips configs when HR / Senior Leadership are missing.

### What is missing

- There is no separate admin validation layer for enforcing document-type rules beyond the model `clean()` method.
- There is no dedicated configuration test suite yet.
- There is no explicit seeded example for the full auto-approve path.

### Phase 3 conclusion

The document-configuration layer is mostly complete. The main work left for this phase is QA-level verification, seed-data validation, and tests around admin/configuration behavior.

## Phase 4 Findings: Review And Approval UI

### What is already working

- Pending queue page exists and separates reviews and approvals into two lists.
- Request detail page shows:
  - request status
  - review chain
  - approval chain
  - submitted dynamic data
  - rejection history
  - cancel button when allowed
  - download/preview/verify links when approved
- Review action view exists with:
  - GET form rendering
  - POST save flow
  - htmx drawer response path
  - non-htmx full-page fallback
- Approval action view exists with the same GET/POST and htmx/full-page pattern.
- Remarks are required on rejection in both review and approval forms.
- Queue filters exist for HR/all-documents listing.
- Permission helpers block self-review/self-approval and limit queue visibility by group.

### What is partial

- The queue/detail pages are functional, but they should still be checked against the intended operator workflow for clarity and ergonomics.
- The htmx drawer templates reuse a shared fragment, which is good, but the UI should be smoke-tested to confirm error states render cleanly inside the drawer.
- The pending queue currently depends on group membership and request exclusion rules; the behavior is correct in code, but should be validated with real group data.

### What is missing

- There are no dedicated UI regression tests for:
  - queue visibility
  - drawer render paths
  - rejection remark validation
  - request detail visibility rules
  - cancel permission rules
- The approval/review pages do not yet have a lot of custom workflow-specific UX beyond the shared form pattern.

### Phase 4 conclusion

The reviewer and approver UI is already present and usable. The remaining work for this phase is mostly verification and test coverage rather than structural implementation.

## Phase 5 Findings: PDF And Public Views

### What is already working

- Approved requests generate a PDF and store it on `DocumentRequest.generated_file`.
- The PDF renderer builds a full context from:
  - the request
  - the employee
  - the document type
  - branding
  - extra field values
  - public verification URL
  - QR code data URI
- `CompanyBranding` supplies logo, footer text, office addresses, and default signatory fallback data.
- `RequestApproval` data is used to select the latest approved signatory for the rendered PDF.
- Download view exists for approved documents with a generated file.
- Public verification page exists and distinguishes:
  - approved / authentic documents
  - non-approved / unavailable documents
- Public preview page exists and embeds the PDF inline through a separate PDF stream route.
- Public verification and preview URLs are included in request-facing templates.
- The admin test-render action can generate a sample PDF from a `DocumentType`.

### What is partial

- The PDF/signatory selection path is implemented, but should be verified with both:
  - a request that has approved signers
  - a request that falls back to `CompanyBranding.default_signatory`
- The public preview flow exists, but the "unavailable" fallback should be smoke-tested for non-approved requests.
- The PDF output is generated via WeasyPrint and should be treated as a rendering contract that needs regression coverage.

### What is missing

- There are no automated tests for:
  - PDF generation after approval
  - PDF download permissions
  - public verification success
  - public verification failure
  - public preview success
  - public preview unavailable state
  - inline public PDF streaming
- There is no explicit test for `PUBLIC_BASE_URL` handling in generated QR/public links.
- There is no explicit regression check that the template context still matches the documented `PDF_CONTEXT_SCHEMA`.

### Phase 5 conclusion

The PDF and public verification subsystem is implemented end to end. The remaining work for this phase is primarily regression testing and a small amount of behavior verification around signatory fallback and unavailable-preview handling.

## Phase 6 Findings: Action Workflow

### What is already working

- The veridoc-style action workflow has been moved out of `documents` and into `offboarding`, which is the correct modular split for Orbis.
- `offboarding` already has:
  - workflow templates
  - step templates
  - step field definitions
  - runtime clearance cases
  - runtime clearance steps
  - exit interview support
  - asset return support
  - note support
  - queue and action views
- The final offboarding step can trigger letter issuance through `documents.DocumentRequest`.
- The `documents` app already stores `offboarding_case` on `DocumentRequest`, so issued letters remain linked back to the clearance case.

### What is partial

- The action workflow is implemented, but it is no longer in the `documents` app the way the original veridoc document described.
- Some of the veridoc concepts are now represented by different names and boundaries:
  - action workflow → offboarding clearance workflow
  - step template → `ClearanceStepTemplate`
  - step runtime → `ClearanceStep`
  - action attachments / step forms → offboarding side views and forms
- The documents plan should not try to reintroduce the old veridoc action models into `documents`; that would duplicate the existing offboarding subsystem.

### What is missing

- A dedicated bridge document that maps the original veridoc action-workflow concepts to the current `offboarding` implementation.
- Regression tests for the offboarding clearance workflow itself, especially:
  - step assignment resolution
  - final-step letter issuance
  - idempotency of `auto_issue_letters`
  - case status transitions

### Phase 6 conclusion

The original veridoc action-workflow feature is not missing from the project; it has been intentionally rehomed to `offboarding`. The remaining work for this phase is to document that mapping clearly and to test the offboarding workflow as the canonical implementation.

## Phase 7 Findings: Permissions And Navigation

### What is already working

- `dynamic_menu` supports database-driven `MainMenu`, `SubMenu`, and `ProtectedView` records.
- The sidebar renders menu items from group membership via direct M2M relationships.
- `sync_protected_views` exists to seed `ProtectedView` rows from named URLs.
- `ProtectedView` is admin-editable with group assignment.
- Core document and offboarding views already enforce access control via:
  - login gates
  - `can_see_all`
  - `can_act_on_*` helpers
  - group membership checks

### What is partial

- The infrastructure for `ProtectedView` exists, but the current audit did not find widespread use of the `dynamic_allowed_users` decorator on the document/offboarding views themselves.
- The menu and protected-view systems are parallel concepts and need to stay aligned whenever URLs or view names change.
- A route seed is available, but its output still needs to be curated by admin to map the right groups to the right hidden routes.

### What is missing

- A deliberate pass over all document/offboarding action endpoints to decide which ones should be:
  - sidebar-visible only
  - protected-view gated
  - login-only
- Regression tests for:
  - sidebar visibility by group
  - protected-view enforcement
  - route-name drift after URL changes
  - `sync_protected_views` idempotency

### Phase 7 conclusion

The navigation and access-control infrastructure exists, but the final policy pass is incomplete. The next work is to align route protection with the intended visibility model and add tests so URL changes do not silently bypass access control.

## Phase 8 Findings: Tests

### What is already working

- `accounts.tests` already provides strong coverage for permission helpers and hierarchy behavior.
- The workflow code itself is structured in a way that is testable:
  - `documents` has forms, signals, services, PDF helpers, public views, and admin actions separated enough to test individually.
  - `offboarding` has service helpers and queue/action views that can be tested in isolation.
  - `dynamic_menu` has a small protected-view surface that can be exercised with focused tests.

### What is partial

- There is currently very little dedicated test coverage in:
  - `documents`
  - `dynamic_menu`
  - `offboarding`
- The workflow areas we audited earlier are mostly verified by code inspection, not by automated regression tests.

### What is missing

- `documents` tests for:
  - dynamic request field creation and validation
  - request lifecycle transitions
  - review / approval rejection handling
  - queue visibility
  - request detail permissions
  - cancel/download/public verification / preview
  - PDF generation and signatory fallback
- `offboarding` tests for:
  - clearance workflow creation
  - step assignment resolution
  - step completion transitions
  - final letter issuance
  - idempotency of letter issuance
- `dynamic_menu` tests for:
  - sidebar visibility by group
  - protected-view gating
  - `sync_protected_views` idempotency
- End-to-end smoke tests for the document workflow and offboarding clearance workflow

### Phase 8 conclusion

The codebase has a solid permission-test foundation in `accounts`, but the document and offboarding workflows still need real regression coverage. This is the biggest remaining engineering gap across the phases we audited.

## Phase 9 Findings: Smoke Test

### What is confirmed

- `manage.py check` passes for the project.
- Core named routes resolve successfully:
  - `documents:dashboard`
  - `documents:request_start`
  - `documents:my_documents`
  - `documents:all_pending_queue`
  - `documents_public:verify_document`
  - `offboarding:my_offboarding`
  - `offboarding:case_list`
  - `home`

### What is still manual

- Browser-level smoke testing of:
  - htmx drawer forms
  - queue actions
  - PDF preview iframe
  - public verification pages
  - offboarding clearance actions
- End-to-end smoke test with real seeded data in a running dev server

### Phase 9 conclusion

The project is structurally sound enough to boot and route correctly. The remaining smoke-test step is live browser interaction, which is the last practical confirmation before treating the workflow as fully complete locally.

## Step-By-Step Tasks

### Phase 1: Gap Audit

- [ ] Compare the current `documents` app against `veridoc_functionality.md`
- [ ] Mark every feature as `done`, `partial`, or `missing`
- [ ] Identify any routes, forms, models, or admin actions that still need to be added
- [ ] Identify anything that should stay out of scope because Orbis already replaced it elsewhere

Deliverable:
- a short gap matrix in this file or a companion note

### Phase 2: Workflow Core

- [ ] Verify request creation persists dynamic fields correctly
- [ ] Verify review chain creation is idempotent
- [ ] Verify approval chain creation is idempotent
- [ ] Verify request status transitions are correct for:
  - pending
  - in review
  - partially approved
  - approved
  - rejected
- [ ] Verify rejection captures the latest reason/history behavior
- [ ] Verify cancellation behavior cannot bypass workflow rules
- [ ] Add regression tests for the core lifecycle paths above

Deliverable:
- stable request lifecycle with no duplicate review/approval rows

### Phase 3: Document Configuration

- [ ] Finish the document type admin experience
- [ ] Ensure template design and message template validation is consistent
- [ ] Ensure extra-field definitions validate against the correct document type
- [ ] Verify starter seed data exists for the main document types
- [ ] Confirm document-type flags behave correctly:
  - approval required
  - exportable
  - action-workflow enabled
  - active/inactive
- [ ] Add configuration tests for document types, templates, and extra fields

Deliverable:
- admin can configure all document type behavior without code changes

### Phase 4: Review And Approval UI

- [ ] Verify reviewer queue pages show only actionable items
- [ ] Verify approver queue pages show only actionable items
- [ ] Confirm drawer/full-page behavior works for both htmx and non-htmx requests
- [ ] Confirm remarks are required when rejecting
- [ ] Confirm the request detail page shows review and approval state cleanly
- [ ] Add UI/view tests for queues, drawer actions, and request detail permissions

Deliverable:
- reviewer and approver flows usable end to end

### Phase 5: PDF And Public Views

- [ ] Verify PDF generation uses the correct template design and message template
- [ ] Verify generated files are attached to the request record
- [ ] Verify download permissions are correct
- [ ] Verify public preview only exposes approved documents
- [ ] Verify public verification by UUID works for success and failure cases
- [ ] Add PDF/public-view regression tests, including the fallback preview and signatory cases

Deliverable:
- approved documents can be generated, downloaded, previewed, and verified

### Phase 6: Action Workflow

- [ ] Confirm action workflow models and status transitions are complete
- [ ] Confirm queue views expose the current action step correctly
- [ ] Confirm step updates support remarks and attachments
- [ ] Confirm action workflows can be tied to the document lifecycle
- [ ] Confirm separation/offboarding hooks only trigger when the document type requires them
- [ ] Add an explicit mapping note from veridoc action workflow to offboarding clearance workflow
- [ ] Add regression tests for offboarding clearance workflow transitions

Deliverable:
- post-approval operational steps work without breaking document issuance

### Phase 7: Permissions And Navigation

- [ ] Confirm private document routes are protected correctly
- [ ] Confirm menu entries appear only for the right groups
- [ ] Confirm non-sidebar routes use `ProtectedView` where needed
- [ ] Run `python manage.py sync_protected_views` after any route changes
- [ ] Add tests for sidebar visibility, protected views, and route-name drift

Deliverable:
- access control matches the intended group-based workflow

### Phase 8: Tests

- [ ] Add model tests for workflow state transitions
- [ ] Add form tests for dynamic request fields
- [ ] Add view tests for review, approval, cancel, download, and verification routes
- [ ] Add permission tests for employee/reviewer/approver/admin visibility
- [ ] Add regression tests for PDF generation and public verification
- [ ] Add offboarding workflow tests
- [ ] Add dynamic menu / protected-view tests

Deliverable:
- the workflow is protected by automated tests

### Phase 9: Smoke Test

- [ ] Seed starter document types
- [ ] Create a sample request
- [ ] Run the request through review and approval
- [ ] Generate a PDF
- [ ] Verify download and public preview
- [ ] Start the dev server and smoke-test the pages in a browser
- [ ] Validate browser-level htmx drawer flow and offboarding clearance flow

Deliverable:
- end-to-end document flow confirmed locally

## Execution Order

1. Gap audit
2. Workflow core
3. Document configuration
4. Review and approval UI
5. PDF and public views
6. Action workflow
7. Permissions and navigation
8. Tests
9. Smoke test

## Notes

- Keep changes inside the `documents` app unless a gap clearly belongs in
  `dynamic_menu` or `offboarding`.
- Do not revisit accounts/employee fields unless a document workflow screen
  explicitly depends on them.
- Prefer finishing one phase fully before starting the next.
- When a route or view name changes, update protected views and tests in the
  same change.
