# Modules

| Module | What it does | Spec |
|---|---|---|
| **Accounts** | SSO, users, scope-based permissions, bulk new-joiner onboarding, Directory API | `docs/PLAN.md`, `docs/DIRECTORY_API_PLAN.md` |
| **Employees** | 14 profile sections, bulk uploaders, Service Book (Form-7), night-shift consent, org chart | `docs/LEGACY_60_IMPORT_PLAN.md`, `docs/NIGHT_SHIFT_CONSENT_PLAN.md` |
| **Roster** | Shift grid, uploads and exports, TimeLy/dsndd APIs, weekly team email, shift change requests, dedicated night shifts | `docs/SHIFT_CHANGE_REQUEST_PLAN.md`, `docs/TIMELY_API.md` |
| **Leave** | SEBPO Leave Policy v2.0, balance ledger, anniversary-cycle AL, CpL, shift summary | `docs/LEAVE_POLICY_IMPLEMENTATION.md` |
| **Timesheet** | TimeLy presence, nightly punch sync, corrections with approval, range modes, exports | `docs/PLAN.md` |
| **Documents** | HR letters from DB-stored templates, PDF with QR verification, approval chain | `docs/VERIDOC_IMPLEMENTATION_PLAN.md` |
| **Offboarding** | Resignation → 8-step clearance, exit interview, asset return, automatic deactivation | `docs/PLAN.md` |
| **Policies** | PDF policy distribution and employee sign-off (first GRC slice) | `docs/POLICY_MODULE_PLAN.md` |
| **Payroll** | Compensation, inputs, calculation engine, register, in-house TDS true-up, loans, pre-Orbis imports, overtime approval chain | `docs/PAYROLL_PLAN.md`, `docs/PAYROLL_PROCESS.md` |
| **Scheduler** | celery-beat schedule in the admin, with run history | `docs/SCHEDULER_ADMIN_PLAN.md` |
| **Access log** | Who accessed which data or page | `docs/ACCESS_LOG_PLAN.md` |
| **Media store** | Encrypted media on Google Drive | `docs/MEDIA_GOOGLE_DRIVE_PLAN.md` |
| **Dashboard, menu, theme** | Personal and team dashboard, group-based menus, icon-rail UI, notification bell | `docs/PLAN.md` |

Each module is an Epic in Work packages. Filter by **Category** to see one module's features and open items.
