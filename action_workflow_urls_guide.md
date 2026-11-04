# Action Workflow Porting Guide

This guide explains how to implement the same two action workflow screens in another Django project that uses Tailwind CSS.

Target URLs:

- `/doc/actions/`
- `/doc/actions/case/<id>/`

The goal is to reproduce the same feature set, permissions, and workflow behavior, but with Tailwind-friendly UI instead of Bootstrap.

## 1. What The Screens Do

### `/doc/actions/`

This is the action queue.

It shows:

- employee name and ID
- document type
- overall workflow status
- progress bar
- next pending step
- waiting-on-me highlight
- filters
- pagination

### `/doc/actions/case/<id>/`

This is the case detail screen.

It shows:

- case summary
- requestor details
- request metadata
- progress bar
- step timeline
- step history
- uploaded attachments
- step update form
- action authorization based on group membership

## 2. Source Code Map In The Current Project

Use these files as the reference implementation:

- [`veridoc/urls.py`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/urls.py)
- [`veridoc/views.py`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/views.py)
- [`veridoc/models.py`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/models.py)
- [`veridoc/forms.py`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/forms.py)
- [`veridoc/templates/veridoc/action_steps_queue.html`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/templates/veridoc/action_steps_queue.html)
- [`veridoc/templates/veridoc/action_case_detail.html`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/templates/veridoc/action_case_detail.html)
- [`veridoc/templates/veridoc/action_step_action.html`](/home/iftesamul/django_project/veridoc/veridoc_04/veridoc/veridoc/templates/veridoc/action_step_action.html)

## 3. URL Routing

Create these routes in the new project:

```python
path("doc/actions/", views.action_steps_queue, name="action_steps_queue")
path("doc/actions/case/<int:case_id>/", views.action_case_detail, name="action_case_detail")
path("doc/actions/step/<int:step_id>/", views.action_step_action, name="action_step_action")
```

Keep the names the same if you want easy template portability.

## 4. Required Data Models

Port these models first.

### Workflow templates

#### `DocumentActionWorkflowTemplate`

Fields:

- `document_type`
- `code`
- `name`
- `description`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

Purpose:

- defines which document types have a post-request workflow
- acts as the workflow container for step templates

#### `DocumentActionStepTemplate`

Fields:

- `workflow`
- `step_code`
- `step_name`
- `order`
- `assigned_group`
- `is_required`
- `is_active`
- `instruction`

Purpose:

- defines the reusable step blueprint
- controls ordering and group assignment
- drives the runtime step creation

#### `DocumentActionStepFieldTemplate`

Fields:

- `step_template`
- `field_key`
- `field_label`
- `sort_order`
- `is_active`

Purpose:

- lets you define step-specific structured inputs
- useful when a step needs extra metadata in the UI

### Runtime records

#### `DocumentActionCase`

Fields:

- `request`
- `workflow_code`
- `status`
- `current_step_order`
- `summary_json`
- `started_at`
- `completed_at`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

Purpose:

- one record per document request workflow instance
- stores overall workflow status and progress

#### `DocumentActionStep`

Fields:

- `action_case`
- `template`
- `step_code`
- `step_name`
- `order`
- `assigned_group`
- `status`
- `is_required`
- `remarks`
- `data_json`
- `acted_by`
- `acted_at`
- `added_at`
- `updated_at`

Purpose:

- stores the live status of each workflow step
- stores action history and step data

#### `DocumentActionStepAttachment`

Fields:

- `step`
- `file`
- `original_name`
- `uploaded_by`
- `uploaded_at`
- `is_active`
- `file_size`
- `content_type`

Purpose:

- supports multiple files per step
- keeps attachments visible inside the case detail page

## 5. Status Design

### Case statuses

- `NOT_STARTED`
- `IN_PROGRESS`
- `COMPLETED`
- `CANCELLED`

### Step statuses

- `PENDING`
- `IN_PROGRESS`
- `DONE`
- `SKIPPED`
- `REJECTED`

## 6. Views You Need

### Queue view

`action_steps_queue`

Behavior:

- loads cases visible to the current user
- filters by group membership
- supports `mine_only=1`
- supports search and advanced filters
- calculates progress per case
- finds the next pending step
- marks cases waiting on the user

### Detail view

`action_case_detail`

Behavior:

- loads a single case with all steps and attachments
- prepares UI-only fields for timeline display
- supports step update on POST
- handles attachment uploads
- refreshes case status after every update

### Step action view

`action_step_action`

Behavior:

- a smaller alternate screen for updating one step
- only accessible to users in the assigned group

## 7. Forms You Need

### `ActionStepFilterForm`

Used in the queue page.

Fields:

- `q`
- `employee_id`
- `document_type`
- `step`
- `step_status`
- `case_status`
- `assigned_group`
- `date_from`
- `date_to`

### `DocumentActionStepActionForm`

Used when a user updates a step.

Fields:

- `status`
- `remarks`

Validation:

- remarks required if status is `SKIPPED`
- remarks required if status is `REJECTED`

## 8. Business Logic To Reproduce

### Case refresh

Recalculate the case after any step update:

- if any step is rejected, set case to `CANCELLED`
- if all required steps are done or skipped, set case to `COMPLETED`
- if some steps are already done or in progress, set case to `IN_PROGRESS`
- otherwise keep it `NOT_STARTED`
- update `current_step_order`
- set `started_at` and `completed_at` when appropriate

### Authorization

Use group membership as the action gate:

- only users in the assigned group can act on a step
- unauthorized requests must return `HttpResponseForbidden`

### Attachments

Reproduce the current upload behavior:

- multiple files per step
- validate extension before save
- validate size before save
- store file metadata
- show attachments on the case detail page

Allowed file types:

- `pdf`
- `jpg`
- `jpeg`
- `png`
- `doc`
- `docx`
- `xls`
- `xlsx`
- `csv`

Maximum file size:

- 10 MB per file

## 9. Tailwind UI Mapping

The current project uses Bootstrap cards, tables, badges, and progress bars. In the new project, map those to Tailwind components.

### Queue page Tailwind structure

Use:

- `max-w-7xl mx-auto px-4 sm:px-6 lg:px-8`
- a top header with title and helper text
- a filter panel inside a `rounded-2xl border bg-white shadow-sm`
- a responsive grid for filters
- a table wrapper with `overflow-x-auto`
- badge pills for case status
- a thin progress bar using `div` width percentage
- row highlighting for `waiting_on_me`
- pagination controls with button-like links

Suggested Tailwind patterns:

```html
<div class="rounded-2xl border border-slate-200 bg-white shadow-sm">
  <div class="flex items-center justify-between border-b border-slate-200 px-6 py-4">
    <div>
      <h1 class="text-xl font-semibold text-slate-900">Workflow Queue</h1>
      <p class="mt-1 text-sm text-slate-500">Manage multi-step document actions and approvals</p>
    </div>
  </div>
</div>
```

### Detail page Tailwind structure

Use:

- a summary card at the top
- a progress section with a horizontal bar
- a vertical timeline for steps
- each step as a card with state-based border colors
- attachment chips or file rows
- a compact form panel for status updates

Suggested Tailwind patterns:

```html
<div class="rounded-2xl border border-slate-200 bg-white shadow-sm">
  <div class="px-6 py-5">
    <div class="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
      <div>
        <p class="text-sm font-medium text-slate-500">Requestor</p>
        <h2 class="text-2xl font-semibold text-slate-900">{{ doc_request.employee.get_full_name }}</h2>
      </div>
      <div class="w-full md:w-1/3">
        <div class="h-2 rounded-full bg-slate-200">
          <div class="h-2 rounded-full bg-emerald-500" style="width: {{ progress_pct }}%"></div>
        </div>
      </div>
    </div>
  </div>
</div>
```

### Tailwind status colors

Use a fixed mapping so the UI stays consistent:

- `PENDING` -> slate/gray
- `IN_PROGRESS` -> blue
- `DONE` -> green
- `SKIPPED` -> amber
- `REJECTED` -> red
- `COMPLETED` case -> green
- `CANCELLED` case -> red

## 10. Page Composition In The New Project

### Queue page sections

1. Page title and subtitle
2. Search and filter area
3. Active filter chips
4. Table of cases
5. Pagination footer

### Detail page sections

1. Breadcrumb or back link
2. Case summary card
3. Request metadata panel
4. Expanded request data section
5. Vertical step timeline
6. Step history and actor information
7. Attachments section
8. Step action form

## 11. Copy-Paste Checklist

Use this order when implementing in the new project.

### Phase 1: Data layer

- add the workflow models
- add the status enums
- add the attachment model
- create and run migrations

### Phase 2: Seed/configuration

- create document types that need action workflows
- create workflow templates
- create step templates
- assign groups to each step template

### Phase 3: Business logic

- implement case refresh logic
- implement attachment validation
- implement group-based authorization
- implement queue filtering and progress calculation

### Phase 4: Views and forms

- build the queue view
- build the case detail view
- build the step action view
- add the filter form
- add the step action form

### Phase 5: Tailwind templates

- build the queue page layout in Tailwind
- build the case detail page layout in Tailwind
- build the step action panel in Tailwind
- add responsive behavior for mobile

### Phase 6: UX polish

- add state badges
- add loading/empty states
- add attachment previews or file rows
- add inline validation messages
- add a clear progress bar in both screens

## 12. Migration Sequence

If this is a fresh project, follow this sequence.

### Step 1

Create the database tables for:

- workflow template
- step template
- step field template
- action case
- action step
- step attachment

### Step 2

Add indexes and uniqueness constraints:

- unique step code per workflow
- unique order per workflow
- unique step per case by code
- unique order per case

### Step 3

Add or seed reference data:

- groups
- document types
- workflow templates
- step templates

### Step 4

Wire document request creation to create an action case when the document type requires it.

### Step 5

Render the queue and detail screens.

### Step 6

Test these flows:

- user sees queue items based on group membership
- user can open a case detail page
- assigned user can update a step
- attachments save successfully
- case progress updates correctly
- rejected step cancels the workflow

## 13. Tailwind Implementation Notes

When converting the UI:

- do not copy Bootstrap classes directly
- use Tailwind utility classes for spacing, borders, typography, and layout
- keep the same information hierarchy
- preserve the same interactions:
  - progress bar
  - step timeline
  - waiting-on-me badge
  - filters
  - attachments
  - action form
- keep the case detail page visually denser than the queue page

## 14. What To Recreate Exactly

The most important behaviors to preserve are:

- group-based access control
- progress calculation from step completion
- timeline-style step history
- per-step attachment upload
- per-step status updates
- case refresh after every update
- queue filters and pagination
- quick scan usability for operators

## 15. Recommended Data Flow

### Queue page

1. Load cases visible to the user.
2. Prefetch steps and attachments.
3. Compute next pending step.
4. Compute progress percentage.
5. Render the queue table.

### Case detail page

1. Load one case with all steps.
2. Mark current step visually.
3. Display history and attachments.
4. Allow the authorized group to submit an action.
5. Save attachments and step data.
6. Refresh case status.
7. Redirect back to the same page.

## 16. Suggested Files In The New Project

You will likely want these files:

- `workflow/models.py`
- `workflow/forms.py`
- `workflow/views.py`
- `workflow/urls.py`
- `templates/workflow/action_queue.html`
- `templates/workflow/action_case_detail.html`
- `templates/workflow/action_step_action.html`

## 17. Final Build Order

If you want the safest implementation path, do it in this order:

1. models and migrations
2. seed data for templates and groups
3. views and forms
4. Tailwind templates
5. attachment handling
6. progress refresh logic
7. permissions and edge-case tests

