# Project Features and Functionality

This document summarizes the main features implemented in the current Django project based on the source code, templates, URLs, models, forms, and settings.

## 0. Quick App Map

- `accounts`: company, employee, profile, authentication, backup, and documentation utilities
- `dynamic_menu`: dynamic navigation and protected view management
- `veridoc`: document request lifecycle, review/approval, PDF generation, action workflows, and reporting

## 1. Project Overview

This is a multi-module Django application built around:

- Document request and approval workflows
- Role-based access control
- Dynamic navigation menus
- Microsoft SSO / social login support
- User and profile management
- Admin and operational dashboards

The project is split into three main apps:

- `accounts` for authentication, employee management, and admin utilities
- `dynamic_menu` for configurable menus and protected views
- `veridoc` for document generation, review, approval, verification, and action workflows

## 2. Model Reference

This section lists the primary models and the most important fields/relationships.

### `accounts.models`

#### `Company`

- `name`
- `contact_number`
- `email_address`
- `email_domain`
- `address`
- `joining_date`
- `company_logo`
- `favicon`
- `meal_cutoff_time`
- `meal_request_cutoff_time`
- `is_active`
- `is_paid`
- `updated_by`
- `updated_date`

Notes:
- `email_domain` is indexed and used for SSO/company matching
- `sanitized_name()` returns a lowercase identifier-safe name

#### `Location`

- `company` -> `Company`
- `name`
- `address`
- `is_active`
- `can_exchange_meal`
- `updated_by`
- `updated_at`

Constraints:
- unique on `(name, company)`

#### `Department`

- `name`
- `department_local`
- `description`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

Behavior:
- `department_local` defaults to `name` if not provided

#### `Designation`

- `name`
- `is_active`
- `added_at`
- `updated_at`

#### `CustomUser`

- `company` -> `Company`
- `contact_number`
- `username`
- `employee_id`
- `profile_picture`
- `default_location` -> `Location`
- `joining_date`
- `signature`
- `designation` -> `Designation`
- `is_guest`
- `department` -> `Department`

Notes:
- subclasses Django `AbstractUser`
- `search_by_text()` searches full name, email, username, contact number, and employee ID
- `save()` prevents guest users from having an `employee_id`

### `dynamic_menu.models`

#### `MainMenu`

- `title`
- `icon_class`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

#### `SubMenu`

- `main_menu` -> `MainMenu`
- `title`
- `url`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

Notes:
- `get_absolute_url()` resolves the stored URL name with `reverse()`

#### `GroupMainMenu`

- `group` -> Django `Group`
- `main_menu` -> `MainMenu`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

#### `GroupSubMenu`

- `group` -> Django `Group`
- `sub_menu` -> `SubMenu`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

#### `RegisteredApp`

- `name`

Notes:
- `update_registered_apps()` refreshes rows from installed Django app configs

#### `ProtectedView`

- `registered_app` -> `RegisteredApp`
- `view_name`
- `view_description`
- `allowed_groups` -> many-to-many with Django `Group`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

### `veridoc.models`

#### Enums / Choices

- `ActionWorkflowCode`: `NONE`, `RESIGNATION`, `NOC_CLEARANCE`, `ASSET_RETURN`, `EXPERIENCE_PROCESS`
- `ActionCaseStatus`: `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, `CANCELLED`
- `ActionStepStatus`: `PENDING`, `IN_PROGRESS`, `DONE`, `SKIPPED`, `REJECTED`

#### `TemplateDesign`

- `id` UUID primary key
- `key`
- `name`
- `html`
- `is_active`
- `created_at`
- `updated_at`

Notes:
- stores the full page layout
- should contain `{{ body_html|safe }}`

#### `MessageTemplate`

- `id` UUID primary key
- `key`
- `name`
- `body_html`
- `is_active`
- `created_at`
- `updated_at`

Notes:
- stores document body content only

#### `DocumentType`

- `name`
- `display_name`
- `template_file`
- `template_design` -> `TemplateDesign`
- `message_template` -> `MessageTemplate`
- `has_action_workflow`
- `action_workflow_code`
- `is_exportable`
- `is_active`
- `requires_approval`
- `created_at`
- `updated_at`

#### `DocumentSignatoryConfig`

- `document_type` -> `DocumentType`
- `signatory_group` -> Django `Group`
- `signature_order`

Constraint:
- unique `(document_type, signatory_group)`

#### `DocumentReviewConfig`

- `document_type` -> `DocumentType`
- `reviewer_group` -> Django `Group`
- `review_order`

Constraint:
- unique `(document_type, reviewer_group)`

#### `RequestReview`

- `request` -> `DocumentRequest`
- `reviewer_group` -> Django `Group`
- `reviewer` -> `CustomUser`
- `status`
- `remarks`
- `order`
- `reviewed_at`
- `updated_at`

Methods:
- `mark_reviewed(user, remarks="")`

#### `DocumentRequest`

- `id` UUID primary key
- `employee` -> `CustomUser`
- `document_type` -> `DocumentType`
- `status`
- `generated_file`
- `rejection_reason`
- `input_data`
- `created_at`
- `updated_at`

Indexes:
- `(employee, status)`
- `(status)`

Properties:
- `is_fully_approved`
- `is_fully_reviewed`

#### `RequestApproval`

- `request` -> `DocumentRequest`
- `approver_group` -> Django `Group`
- `signatory` -> `CustomUser`
- `status`
- `remarks`
- `order`
- `signed_at`
- `updated_at`

Constraint:
- unique `(request, approver_group)`

#### `DocumentExtraFieldType`

- `document_type` -> `DocumentType`
- `field_key`
- `field_name`
- `kind`
- `is_required`
- `is_active`
- `choices`
- `help_text`
- `sort_order`
- `created_at`
- `updated_at`

Field kinds:
- `text`
- `textarea`
- `number`
- `date`
- `bool`
- `choice`

Constraint:
- unique `(document_type, field_key)`

#### `DocumentExtraFieldValue`

- `request` -> `DocumentRequest`
- `field_type` -> `DocumentExtraFieldType`
- `value`
- `is_active`
- `submitted_by` -> `CustomUser`
- `submitted_at`

Constraint:
- unique `(request, field_type)`

Validation:
- `clean()` ensures the field belongs to the same `DocumentType` as the request

#### `Project`

- `name`
- `created_at`

#### `ProjectFile`

- `project` -> `Project`
- `title`
- `file`
- `file_type`
- `uploaded_at`

File types:
- `html`
- `pdf`
- `image`
- `doc`
- `other`

Helpers:
- `preview_url`
- `download_url`

Validation:
- `clean()` validates file extension for HTML/PDF

#### `DocumentActionWorkflowTemplate`

- `document_type` -> one-to-one with `DocumentType`
- `code`
- `name`
- `description`
- `is_active`
- `added_by`
- `added_at`
- `updated_by`
- `updated_at`

#### `DocumentActionStepTemplate`

- `workflow` -> `DocumentActionWorkflowTemplate`
- `step_code`
- `step_name`
- `order`
- `assigned_group` -> Django `Group`
- `is_required`
- `is_active`
- `instruction`

Constraints:
- unique `(workflow, step_code)`
- unique `(workflow, order)`

#### `DocumentActionStepFieldTemplate`

- `step_template` -> `DocumentActionStepTemplate`
- `field_key`
- `field_label`
- `sort_order`
- `is_active`

#### `DocumentActionCase`

- `request` -> one-to-one `DocumentRequest`
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

Property:
- `current_step`

#### `DocumentActionStep`

- `action_case` -> `DocumentActionCase`
- `template` -> `DocumentActionStepTemplate`
- `step_code`
- `step_name`
- `order`
- `assigned_group` -> Django `Group`
- `status`
- `is_required`
- `remarks`
- `data_json`
- `acted_by` -> `CustomUser`
- `acted_at`
- `added_at`
- `updated_at`

Constraints:
- unique `(action_case, step_code)`
- unique `(action_case, order)`

#### `DocumentActionStepAttachment`

- `step` -> `DocumentActionStep`
- `file`
- `original_name`
- `uploaded_by` -> `CustomUser`
- `uploaded_at`
- `is_active`
- `file_size`
- `content_type`

## 3. Document Workflow Inputs

### `veridoc.forms.DocumentRequestForm`

This is a dynamic form driven by `DocumentExtraFieldType`.

Behavior:
- loads only active field definitions for the selected document type
- creates fields dynamically at runtime
- supports text, textarea, number, date, boolean, and choice inputs
- enforces required dynamic fields in `clean()`
- saves request metadata in `DocumentRequest.input_data`
- persists each dynamic answer into `DocumentExtraFieldValue`

### `veridoc.forms.ApprovalActionForm`

- fields: `status`, `remarks`
- used for approval/rejection actions
- requires remarks when status is `REJECTED`

### `veridoc.forms.ReviewActionForm`

- fields: `status`, `remarks`
- used for review/reject actions
- requires remarks when status is `REJECTED`

### `veridoc.forms.DocumentActionStepActionForm`

- used by assigned staff/team members to update an action step
- fields: `status`, `remarks`

### `veridoc.forms.ActionStepFilterForm`

- filter form for action queue views
- used to narrow steps by status, workflow, or other queue criteria

### `veridoc.forms.SeparationDataFilterForm`

- filter form for separation reporting screens
- supports reporting/date-based filtering

### `accounts.forms`

#### `CustomUserCreationForm`

- `first_name`
- `last_name`
- `email`
- `username`
- `contact_number`
- `groups`

#### `CustomUserChangeForm`

- `first_name`
- `last_name`
- `employee_id`
- `email`
- `username`
- `contact_number`
- `department`
- `default_location`
- `groups`
- `is_active`

#### `UserProfileForm`

- `default_location`
- `profile_picture`

#### `SelfProfileForm`

- `joining_date`
- `profile_picture`
- `signature`

#### `AdminProfileForm`

- `first_name`
- `last_name`
- `email`
- `username`
- `employee_id`
- `contact_number`
- `department`
- `designation`
- `company`
- `default_location`
- `joining_date`
- `profile_picture`
- `signature`
- `is_active`
- `is_guest`

#### `JoiningDateOnlyForm`

- `joining_date`

## 4. Core Routes

## 2. Authentication and Access

### Login and Logout

- Username/password login
- Vendor login page variant
- Logout endpoint
- Login redirect handling

### Single Sign-On

- Microsoft SSO integration through Django Allauth
- Auto-signup for social login users
- Login-on-get flow for smoother SSO handling
- Social account token storage

### Password Management

- Password reset request
- Password reset done page
- Password reset confirmation
- Password reset completion
- Change password flow for logged-in users

### Access Control

- Group-based access decorators
- Admin-only protection for selected views
- Dynamic protected views controlled from the database
- Menu visibility controlled by user groups

## 3. Dynamic Menu System

The `dynamic_menu` app provides database-driven navigation.

### Menu Features

- Main menus
- Submenus under each main menu
- Group-to-menu mapping
- Active/inactive menu toggles
- URL resolution from submenu records

### Protected View Features

- Registered app tracking
- Protected view registration per app
- Allowed group assignment per protected view
- Dynamic access checking based on database configuration

### Maintenance

- Management command to refresh the list of registered Django apps in the database

## 4. User and Employee Management

The `accounts` app provides employee-centric user management.

### User Profile Data

- First name and last name
- Username
- Email
- Employee ID
- Contact number
- Company association
- Department
- Designation
- Default location
- Joining date
- Profile picture
- Signature image
- Guest-user flag
- Active/inactive status

### Employee Admin Operations

- Create users
- Update users
- Delete users
- View users list
- Bulk upload users from Excel
- Search and filter users
- Paginated user listing
- Export filtered user data to Excel

### Personal Profile Features

- Self-service profile update
- Joining date update
- Profile picture upload
- Signature upload
- Password change

### Organizational Data Models

- Company
- Location
- Department
- Designation

### Admin Utilities

- Database backup generation
- Secure database backup download
- Documentation pages for different roles

## 5. Document Workflow System

The `veridoc` app is the core document workflow engine.

### Document Type Configuration

- Document type definition
- Display name for user-facing screens
- Template file upload
- Optional template design assignment
- Optional message template assignment
- Active/inactive document types
- Approval requirement toggle
- Action workflow toggle
- Exportable flag for workflow results

### Template System

- Full-page template designs
- Message/body templates
- Rich text editing support
- Dynamic variables inside templates

### Dynamic Request Fields

- Per-document custom fields
- Field kinds:
  - text
  - textarea
  - number
  - date
  - boolean
  - choice
- Required/optional field control
- Help text for each field
- Field ordering
- Stored field values per request
- Validation to ensure field values match the document type

### Request Creation

- Start request flow
- Single-page request form flow
- Document-type-specific request form
- Save request input data in JSON
- Support for dynamic extra fields attached to each request

### Request Statuses

- Pending
- In review
- Partially approved
- Approved and generated
- Rejected

## 6. Review and Approval Workflow

The document workflow supports staged review and approval.

### Review Stage

- Review configurations per document type
- Multiple reviewers per document type
- Ordered review steps
- Assign review tasks to groups
- Assign final action to the specific reviewer user
- Review comments / remarks
- Reviewed, pending, and rejected states

### Approval Stage

- Signatory configurations per document type
- Multiple approvers per document type
- Ordered approval steps
- Assign approval tasks to groups
- Assign final action to the specific approver user
- Approval comments / remarks
- Approved, pending, and rejected states

### Request Tracking

- Track whether all reviews are complete
- Track whether all approvals are complete
- Request rejection reason
- Latest updated timestamps
- Review and approval ordering

## 7. Document Generation and Download

### Output Handling

- Generate final PDF files for approved requests
- Store generated files on disk
- Download generated PDFs by request ID
- Public PDF preview for shared links
- Public document preview pages

### Verification

- Public verification page by UUID
- Success and failure verification pages
- Public access to verification without login

## 8. Document Request Dashboards

### User Views

- My documents page
- Document request dashboard
- Request start page
- Request detail form page

### Admin / Operations Views

- Pending requests overview
- All documents admin view
- Review request page
- Approval request page
- Dashboard page for document operations

## 9. Action Workflow System

Some document types require post-request operational steps.

### Action Workflow Features

- Workflow code assignment per document type
- Action case tracking
- Action step queue
- Action step detail view
- Action step status updates
- Action step remarks
- Action attachments
- Action case status tracking
- Predefined field handling for specific steps
- Attachment validation for action steps

### Action Workflow Statuses

- Not started
- In progress
- Completed
- Cancelled

### Step Statuses

- Pending
- In progress
- Done
- Skipped
- Rejected

## 10. Separation Data Reporting

The project includes separation reporting utilities for workflow and HR operations.

- Separation data listing
- Separation data filtering
- Separation data export
- Dynamic report columns
- Combined review and approval flow reporting

## 11. Project File Management

The project includes a file registry for project assets.

- Project model
- Project file upload
- File type classification
- File preview routing
- Download URL support
- Basic file validation by extension

## 12. Templates and UI

The UI is built with reusable templates and static assets.

### Shared Layout

- Base layout template
- Header, sidebar, and footer includes
- Authentication templates
- Role-based dashboard templates

### Frontend Assets

- Bootstrap
- KaiAdmin admin theme
- Font Awesome icons
- Datepicker support
- DataTables
- Select2
- Summernote
- SweetAlert
- Chart and calendar plugins
- HTMX support

## 13. Background Jobs and Infrastructure

### Task Queue

- Celery integration
- Redis broker and backend
- Celery beat schedule file present

### File and Media Handling

- Media upload support
- CKEditor uploads
- Static file serving in development

### Email

- SMTP email configuration
- Password reset email support

## 14. Database and Configuration

### Database

- MySQL database backend

### Settings

- Time zone configured to Asia/Dhaka
- Site framework enabled
- Environment-based loading with `.env`
- Public base URL configured

## 15. Notes on the Codebase

- The repository includes several generated assets and vendor libraries under `static/`
- Some older README text describes a starter project, but the actual codebase now includes a larger document workflow and employee management system
- Several views and forms support both standard admin operations and workflow-specific operations
- Some form files reference fields that are not visibly defined in the current model excerpts, so those may be legacy leftovers or be defined elsewhere in unlisted migrations/admin logic

## 16. Main User Flows

### Employee Flow

1. Sign in with Microsoft or username/password
2. Update profile data
3. Request a document
4. Fill document-specific custom fields
5. Track request status
6. Download the final PDF when approved

### Reviewer / Approver Flow

1. Open the pending review or approval queue
2. Review or approve the assigned request
3. Add remarks if rejecting
4. Progress the request through ordered workflow steps

### Admin Flow

1. Manage users, departments, designations, companies, and locations
2. Manage document types, templates, and workflow configs
3. Configure menus and protected views
4. Export reports and manage backups

---

If you want, I can also turn this into a shorter executive summary or a more technical module-by-module architecture document.
