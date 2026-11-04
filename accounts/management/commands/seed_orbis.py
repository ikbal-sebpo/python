"""Idempotent orchestrator for first-run / re-seedable Orbis data.

Wraps every initial-population step in one transactional command. Safe to
re-run: groups, menus, JobLocations are ``get_or_create``-d by stable keys,
fixtures use explicit pks (`loaddata` is upsert-by-pk), and the per-app
``seed_*`` commands are themselves idempotent.

Replaces the seeding work previously split across the deploy script's
multi-loaddata loop **plus** the data migrations
(``accounts/0003_seed_hierarchy_groups`` and
``leave/0002_all_employees_group_and_menu``) that were dropped during the
production migration squash.

Order matters — fixtures depend on groups (e.g. menu rows reference auth
groups by pk), and ``seed_leave_policies`` depends on the seeded ShiftTypes.

**`seed_org` deliberately contains no Departments** (2026-09-19). It used to
ship four demo ones — Engineering / Operations / Human Resources / Finance at
pks 1-4 — left over from early development. Because `loaddata` is
upsert-by-pk and those pks are long gone from any real deployment, re-running
this command against real org data either created phantom departments
("Engineering", "Operations") or died outright on the unique-name index when a
real department happened to share a placeholder's name — which is exactly what
happened here: SEBPO's real "Human Resources" lives at pk=14, so loading pk=3
with the same name raised IntegrityError and rolled the whole seed back.

Departments are customer org data, not bootstrap taxonomy. The remaining
`seed_org` rows (AccessLevel, Designation, WorkLocation, Grade) are genuinely
canonical and verified to match production at those pks, so they stay. The old
demo departments moved to `seed_demo_departments.json`, loaded only with
``--with-demo-departments`` for a genuinely empty dev database.

**`seed_taxonomy` lost its State/City rows for the same reason** (2026-09-19).
It carried a 12-district / 10-city sample at pks 1-12 and 1-10, superseded by
`manage.py import_bangladesh_locations` (see docs/BANGLADESH_LOCATION_DATA.md),
whose real output lives at pks 89-164 and 906-1800. Re-seeding therefore hit
the `(country, name)` unique on "Dhaka" and rolled everything back. The other
taxonomy rows — Country, Division, Relation, Religion, Vaccine, Dose, Client —
were each verified pk-for-pk against the real database and stay. The sample
moved to `employees/fixtures/seed_demo_locations.json`, loaded with
``--with-demo-departments`` too, since both exist for the same empty-database
case.
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction


# Stable ordering: groups Orbis relies on. Re-running creates nothing new.
ORBIS_GROUPS = (
    "User Admins",
    "HR",
    "All Employees",
    "Senior Leadership",
    "IT",
    "Finance",
    "Payroll",
    "Payroll Admins",
    # Derived membership — populated by accounts.services.sync_line_managers
    # from JobInfo.reports_to, never hand-edited. Exists only so dynamic_menu
    # (which gates on groups and nothing else) can show manager-facing items.
    "Line Managers",
    # Also derived — accounts.services.sync_supervisors, from
    # Designation.access_level.rank >= Supervisor.
    "Supervisors",
    # Readers of the access log (audit app). Assigned by hand on demand under
    # company policy; User Admins and superusers can read it regardless.
    # HR deliberately not — the log shows HR's own reads. 2026-10-09.
    "Audit Viewers",
)

# Canonical menu ACL groups, in order.
MENU_GROUPS = {
    # "HR" added 2026-10-05: the bulk new-joiner onboarding page under Users
    # became HR-only (its sheet carries gross salary), and HR already passed
    # the user list/create views' can_edit_all gate — only the menu hid it.
    "admins": ["User Admins", "HR"],
    "people_ops": ["User Admins", "HR"],
    "people_ops_leaves": ["User Admins", "HR", "All Employees", "Senior Leadership"],
    "reports": ["User Admins", "HR", "IT", "Finance"],
    "roster_ops": ["User Admins", "HR"],
    "documents": ["User Admins", "HR", "Senior Leadership", "All Employees"],
    "offboarding_ops": ["User Admins", "HR"],
    "offboarding_people": ["User Admins", "HR", "All Employees", "Senior Leadership"],
    "offboarding_queue": ["User Admins", "HR", "IT", "Finance"],
    # MANAGER_ACK steps are assigned to the employee's JobInfo.reports_to, so
    # the people who must action step 1 have to see this. "Line Managers" is
    # derived from that same field, so the grant matches the assignment.
    "offboarding_acks": ["User Admins", "HR", "Line Managers"],
    # Main-menu visibility policy: every section except Users is open to all
    # staff, and what you can actually DO lives on the submenu grants below.
    # accounts.signals auto-adds every new user to "All Employees", so this
    # reliably covers everyone including new joiners.
    "all_staff": ["All Employees"],
    "payroll_ops": ["User Admins", "HR", "Finance", "Payroll", "Payroll Admins"],
    # Gross salary is sensitive comp data — deliberately narrower than
    # payroll_ops. Payroll Admins only since 2026-10-05 (was Finance), the
    # same group that sees money on the register
    # (accounts.permissions.can_see_payroll_money) — one visibility rule for
    # every per-employee amount. See docs/PAYROLL_DATA_PROTECTION_PLAN.md §3.
    "payroll_salary": ["Payroll Admins"],
    # Access log readers — see ORBIS_GROUPS "Audit Viewers".
    "audit": ["User Admins", "Audit Viewers"],
}

# ProtectedView ACLs we assert rather than leave to hand-editing.
# ``sync_protected_views`` creates missing rows with NO allowed_groups, which
# denies everyone but superusers — a silent lockout. Views listed here get
# their groups set on every seed run; views NOT listed keep whatever they
# have, so this is additive and safe to re-run.
PROTECTED_VIEW_GROUPS = {
    # Access log viewer — the same two groups as the menu (superusers bypass).
    "audit_access_log": MENU_GROUPS["audit"],
    # Overtime upload: Supervisor-and-above by designation, plus payroll ops.
    # Broad by design — the view itself is the real gate. It additionally
    # requires has_managed_scope, and rows outside the uploader's managed
    # scope are rejected into report.skipped_scope, so a supervisor can only
    # ever file overtime for their own people.
    "payroll_overtime_upload": ["Supervisors", "User Admins", "HR", "Finance",
                                "Payroll", "Payroll Admins"],
    # The overtime list/add/edit/void/delete screens — same grant as the
    # uploader they replace as the section's landing page. Same two real
    # gates behind it too: has_managed_scope to open it, and
    # payroll.services.OvertimeWindow for which work dates may still be
    # written (a locked period is HR-Payroll only).
    "payroll_overtime_list": ["Supervisors", "User Admins", "HR", "Finance",
                              "Payroll", "Payroll Admins"],
    "payroll_overtime_create": ["Supervisors", "User Admins", "HR", "Finance",
                                "Payroll", "Payroll Admins"],
    "payroll_overtime_edit": ["Supervisors", "User Admins", "HR", "Finance",
                              "Payroll", "Payroll Admins"],
    "payroll_overtime_void": ["Supervisors", "User Admins", "HR", "Finance",
                              "Payroll", "Payroll Admins"],
    "payroll_overtime_delete": ["Supervisors", "User Admins", "HR", "Finance",
                                "Payroll", "Payroll Admins"],
    # Variable pay (SLA achievement, Test Salary, bonus, incentive,
    # disciplinary). Same "who may reach the page" tier as overtime — a
    # supervisor uploads and sees their own scope. Who may APPROVE is a
    # narrower, per-row question the page itself asks via
    # accounts.permissions::can_act_on_variable_pay (the employee's own
    # department head, deliberately NOT HR — confirmed 2026-09-19), so it
    # is not expressible as a group list here.
    # Pending salary proposals — same Payroll-Admins-only, company-wide
    # posture as the rest of the gross-salary screens (payroll_salary).
    "payroll_salary_pending": MENU_GROUPS["payroll_salary"],
    "payroll_salary_decide": MENU_GROUPS["payroll_salary"],
    # The list/detail/upload/template views were previously granted by hand
    # (Finance). Asserted here since 2026-10-05 so the Payroll-Admins-only
    # rule actually lands on a re-seed — the "Gross salary" submenu only
    # controls sidebar visibility, dynamic_allowed_users reads these rows.
    "payroll_salary_list": MENU_GROUPS["payroll_salary"],
    "payroll_salary_detail": MENU_GROUPS["payroll_salary"],
    "payroll_salary_upload": MENU_GROUPS["payroll_salary"],
    "payroll_salary_upload_template": MENU_GROUPS["payroll_salary"],
    # Salary HISTORY import (2026-10-05, docs/LEGACY_60_IMPORT_PLAN.md §3) —
    # same file sensitivity, same gate.
    "payroll_salary_upload_history": MENU_GROUPS["payroll_salary"],
    "payroll_salary_upload_history_template": MENU_GROUPS["payroll_salary"],
    # Tax-year openings for the YTD TDS true-up (2026-10-07,
    # docs/TAX_CALCULATION_TRUEUP_PLAN.md §6) — per-employee tax, same gate.
    "payroll_tax_opening_upload": MENU_GROUPS["payroll_salary"],
    "payroll_tax_opening_upload_template": MENU_GROUPS["payroll_salary"],
    # Pre-Orbis payroll months (2026-10-08, docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md
    # §3) — per-employee pay and tax per month, same gate; the month export
    # reads the same figures back out.
    "payroll_legacy_payroll_upload": MENU_GROUPS["payroll_salary"],
    "payroll_legacy_payroll_upload_template": MENU_GROUPS["payroll_salary"],
    "payroll_legacy_payroll_export": MENU_GROUPS["payroll_salary"],
    # Tax challans (2026-10-08, same plan §3) — the company's treasury
    # deposits, not per-employee pay, so Finance records them alongside
    # Payroll Admins. Permanent, monthly.
    "payroll_tax_challan_upload": ["User Admins", "Finance", "Payroll Admins"],
    "payroll_tax_challan_upload_template": ["User Admins", "Finance", "Payroll Admins"],
    # TDS by challan, per employee (2026-10-09, same plan Revision 2 §R2.3)
    # — how much of each employee's tax a deposit carried: personal tax, so
    # the per-employee-money gate. Permanent, monthly.
    "payroll_tax_challan_allocation_upload": MENU_GROUPS["payroll_salary"],
    "payroll_tax_challan_allocation_upload_template": MENU_GROUPS["payroll_salary"],
    # Salary loans (2026-10-08, docs/SALARY_LOAN_PLAN.md §2) — the group gate
    # is payroll_ops; can_manage_salary_loans narrows it in the views. The
    # cutover uploader is Payroll Admins only, like every per-employee money
    # sheet.
    "payroll_loan_list": MENU_GROUPS["payroll_ops"],
    "payroll_loan_create": MENU_GROUPS["payroll_ops"],
    "payroll_loan_edit": MENU_GROUPS["payroll_ops"],
    "payroll_loan_disburse": MENU_GROUPS["payroll_ops"],
    "payroll_loan_cancel": MENU_GROUPS["payroll_ops"],
    "payroll_loan_adjust": MENU_GROUPS["payroll_ops"],
    "payroll_loan_skip": MENU_GROUPS["payroll_ops"],
    "payroll_loan_skip_delete": MENU_GROUPS["payroll_ops"],
    "payroll_loan_history": MENU_GROUPS["payroll_ops"],
    "payroll_loan_employee_search": MENU_GROUPS["payroll_ops"],
    "payroll_loan_upload": MENU_GROUPS["payroll_salary"],
    "payroll_loan_upload_template": MENU_GROUPS["payroll_salary"],
    # Pre-Orbis AL record import (2026-10-05, docs/LEGACY_60_IMPORT_PLAN.md
    # §4) — HR/Admins, reached from the Upload Center, no sidebar entry.
    "leave_al_history_upload": ["User Admins", "HR"],
    "leave_al_history_upload_template": ["User Admins", "HR"],
    "payroll_variable_pay_list": ["Supervisors", "User Admins", "HR", "Finance",
                                  "Payroll", "Payroll Admins"],
    "payroll_variable_pay_decide": ["Supervisors", "User Admins", "HR", "Finance",
                                    "Payroll", "Payroll Admins"],
    "payroll_variable_pay_bulk_decide": ["Supervisors", "User Admins", "HR", "Finance",
                                         "Payroll", "Payroll Admins"],
    "payroll_test_salary_upload": ["Supervisors", "User Admins", "HR", "Finance",
                                   "Payroll", "Payroll Admins"],
    "payroll_test_salary_upload_template": ["Supervisors", "User Admins", "HR", "Finance",
                                            "Payroll", "Payroll Admins"],
    "payroll_sla_upload": ["Supervisors", "User Admins", "HR", "Finance",
                           "Payroll", "Payroll Admins"],
    "payroll_sla_upload_template": ["Supervisors", "User Admins", "HR", "Finance",
                                    "Payroll", "Payroll Admins"],
    "payroll_overtime_employee_search": ["Supervisors", "User Admins", "HR", "Finance",
                                         "Payroll", "Payroll Admins"],
    # Approving/rejecting overtime filed into a locked period. Seeded to the
    # same groups as the rest of the section so the nav grant stays uniform;
    # the real gate is accounts.permissions::can_manage_locked_overtime
    # (HR-Payroll + Payroll Admins), re-checked inside the view — a
    # supervisor reaching this URL still gets a 403.
    "payroll_overtime_decide": ["Supervisors", "User Admins", "HR", "Finance",
                                "Payroll", "Payroll Admins"],
    "payroll_overtime_bulk_decide": ["Supervisors", "User Admins", "HR", "Finance",
                                     "Payroll", "Payroll Admins"],
    "payroll_overtime_upload_template": ["Supervisors", "User Admins", "HR",
                                          "Finance", "Payroll", "Payroll Admins"],
    # Upload Center (added 2026-09-21) — one page listing every bulk
    # uploader the viewer can use; per-card visibility inside the page is
    # finer than this (see payroll/upload_views.py::UPLOAD_CENTER_ENTRIES),
    # so this just needs to be at least as broad as the widest uploader it
    # links to (which it already is — identical to payroll_overtime_upload).
    "payroll_upload_center": ["Supervisors", "User Admins", "HR", "Finance",
                              "Payroll", "Payroll Admins"],
    # Go Live (added 2026-09-21) — deliberately NARROWER than the other
    # managed-scope uploaders next to it (Overtime/SLA/Test Salary all
    # include "Supervisors"). This one changes CustomUser.team /
    # JobInfo.resource_status directly, not just a payroll input row, so a
    # plain supervisor by designation should not be able to reach it — only
    # payroll_ops (HR/Finance/Payroll/Payroll Admins/User Admins). Real
    # enforcement is still has_managed_scope (ManagedScopeRequiredMixin)
    # inside the view, same mechanism as the other three, just without the
    # blanket "Supervisors" grant at the door.
    "payroll_go_live_upload": MENU_GROUPS["payroll_ops"],
    "payroll_go_live_upload_template": MENU_GROUPS["payroll_ops"],
    # Payroll runs — regraded 2026-09-10 to mirror the real manual payroll
    # workflow, then widened again 2026-09-16. Superusers bypass the
    # decorator outright (see dynamic_menu/decorators.py), so they need no
    # group.
    #
    # The register/detail page (list + detail; the "Support Info" tab and
    # its per-employee notes drawer live inline on payroll_run_detail, no
    # separate view) is open to "Supervisors" (every supervisor by
    # designation — widened 2026-09-16, see below), "Payroll" (the small
    # curated payroll-HR team, who review it and write their own HR notes —
    # a plain supervisor is also typically in this group, but membership is
    # no longer what gates the page) AND "Payroll Admins" (who also see the
    # actual money columns — accounts.permissions::can_see_payroll_money
    # gates those specifically, inside the page).
    #
    # REVERSAL (2026-09-16): the 2026-09-10 decision deliberately restricted
    # this page to the curated "Payroll" group, not the broad "Supervisors"
    # designation group, on the theory that only a small set of managers
    # needed to see it. Building the notes bulk-uploader (a supervisor
    # filing Supervisor notes for a department of hundreds) exposed that as
    # too narrow: a supervisor who isn't in "Payroll" couldn't reach the
    # notes UI at all, even though accounts.permissions::can_write_payroll_note
    # already let them write their own reportees' Supervisor notes. "Supervisors"
    # was added back to run list/detail/the note drawer/the new bulk uploader.
    # This does NOT reopen money: payroll_run_export/employee_adjust/
    # variable_* stay Payroll-Admins-only below, and can_see_payroll_money
    # still gates the money columns inside payroll_run_detail itself.
    "payroll_run_list": ["Supervisors", "Payroll", "Payroll Admins"],
    "payroll_run_detail": ["Supervisors", "Payroll", "Payroll Admins"],
    "payroll_run_employee_note_edit": ["Supervisors", "Payroll", "Payroll Admins"],
    # Bulk note upload — broad by design, same posture as the overtime
    # uploader: the view itself is the real gate
    # (accounts.permissions::can_bulk_upload_payroll_notes to open it,
    # can_write_payroll_note re-checked per row/role at apply time), so
    # rows outside the uploader's role/scope land in report.skipped_scope
    # rather than being silently accepted.
    "payroll_run_notes_upload": ["Supervisors", "User Admins", "HR", "Finance",
                                 "Payroll", "Payroll Admins"],
    "payroll_run_notes_upload_template": ["Supervisors", "User Admins", "HR",
                                          "Finance", "Payroll", "Payroll Admins"],
    "payroll_run_export": ["Payroll Admins"],
    "payroll_run_employee_adjust": ["Payroll Admins"],
    "payroll_run_variable_delete": ["Payroll Admins"],
    "payroll_run_variable_edit": ["Payroll Admins"],
    # Night shift consent audit list — HR/Admins only (accounts.permissions
    # ::can_manage_night_shift_consent is the same can_edit_all bar; the
    # menu item and this ProtectedView row must agree).
    "night_shift_consent_list": ["User Admins", "HR"],
    # Profile self-service audit list (added 2026-09-17) — HR/Admins only,
    # same can_edit_all bar as accounts.permissions::can_manage_profile_self_service.
    # No sidebar menu entry yet (reached from a button on the employee list
    # page instead, same as employee_bulk_upload) — this row exists purely
    # so a direct hit on the URL is still gated.
    "profile_self_service_list": ["User Admins", "HR"],
}

# Each entry: ``(title, icon_class, route_name, rank, [group_names])``.
# `route_name` must match a `name=...` in the corresponding app's `urls.py`.
ORBIS_MAIN_MENUS = (
    # Users is the one restricted section — it exposes account administration.
    ("Users",       "users",          "user_list",                  10, MENU_GROUPS["admins"]),
    # Section header (blank URL) since 2026-10-11 so "Org Chart" can sit
    # under it next to the people list — same move Roster made on
    # 2026-10-01. Re-seeding (`seed_orbis --menus-only`) blanks the existing
    # row's url in place (update_or_create keys on title).
    ("Employees",   "user-round",     "",                           20, MENU_GROUPS["all_staff"]),
    # Leave is a section header (blank URL) so its submenus render below.
    ("Leave",       "calendar-check", "",                           25, MENU_GROUPS["all_staff"]),
    # Section header (blank URL) since 2026-10-01 so "Shift Changes" can sit
    # under it — a main menu WITH a url renders as a direct link and hides
    # its submenus (templates/partials/sidebar.html). The grid itself is now
    # the "Roster" submenu below. Re-seeding updates the existing row's url
    # in place (update_or_create keys on title).
    ("Roster",      "calendar-days",  "",                           30, MENU_GROUPS["all_staff"]),
    # Section header (blank URL) so its submenus (list + corrections queue)
    # render below — same reason as Documents/Offboarding. Labeled
    # "Timesheet" (the `attendance` app/URLs/models keep their original
    # name — this is a display-only rename, 2026-09-07).
    ("Timesheet",   "clock-4",        "",                            32, MENU_GROUPS["all_staff"]),
    # Documents + Offboarding are section headers (blank URL) so the sidebar
    # template renders them as expandable sections whose submenus show below.
    # Giving them a URL would make them direct links and suppress submenus —
    # see templates/partials/sidebar.html.
    ("Documents",   "file-text",      "",                            35,
        MENU_GROUPS["all_staff"]),
    ("Offboarding", "door-open",      "",                            40,
        MENU_GROUPS["all_staff"]),
    # Section header (blank URL) — submenus below give access to the
    # report, overtime upload and payroll runs, same pattern as Documents/Offboarding.
    ("Payroll",     "hand-coins",     "",                            60, MENU_GROUPS["all_staff"]),
    # Section header for the access log (audit app, 2026-10-09). Restricted
    # at the main-menu level, unlike the other sections, because it has no
    # self-service content for ordinary staff.
    ("Audit",       "shield-check",   "",                            70, MENU_GROUPS["audit"]),
    # Direct link (not a section) to the credits page, last in the rail
    # (2026-10-30). Also linked from the footer.
    ("Project Credits", "award",      "project_credits",            100, MENU_GROUPS["all_staff"]),
)

# Each entry: ``(main_title, sub_title, route_name, rank, [group_names])``.
# ``main_title`` must match an entry in ``ORBIS_MAIN_MENUS``; if the parent
# main menu is missing the row is skipped with a warning.
ORBIS_SUB_MENUS = (
    # Employees — the people list plus the org chart (docs/PLAN.md "Org
    # chart"). Both open to everyone: the list scopes its rows to
    # visible_employees, the chart is company-wide by design (names/titles
    # only) with profile links gated per card.
    ("Employees", "Employees", "employee_list",      10, MENU_GROUPS["all_staff"]),
    ("Employees", "Org Chart", "employee_org_chart", 20, MENU_GROUPS["all_staff"]),
    # Leave. Titles MUST match the rows already in the database — this table
    # is keyed on (main_menu, title), so renaming an entry here creates a
    # second menu row pointing at the same route rather than updating the
    # existing one. "Leave list"/"Shift summary" did exactly that and were
    # removed as duplicates of "Leaves"/"Summary".
    ("Leave", "Leaves",  "leave_list",          0, ["All Employees", "User Admins"]),
    ("Leave", "Summary", "leave_shift_summary", 0, ["All Employees", "HR", "User Admins"]),
    # Everyone sees their own grants (CplGrantListView scopes to
    # visible_employee_pks, same as the balance page); the Upload/Add
    # buttons inside the page are separately gated
    # (can_edit_all/has_managed_scope, can_manage_cpl_grants) so nothing
    # extra is needed here.
    ("Leave", "Compensatory Leave", "cpl_grant_list", 10, ["All Employees", "User Admins"]),
    # Night shift consent (Bangladesh Labour Act 2006 s.109, added
    # 2026-09-13) — HR/Admin audit list; the self-service consent button
    # itself lives on the dashboard's My tab, not the sidebar.
    ("Leave", "Night Shift Consent", "night_shift_consent_list", 20, MENU_GROUPS["people_ops"]),
    # No entry for leave_al_bank_upload or cpl_grant_upload: both are
    # one-off/manager-driven imports (see
    # docs/LEAVE_POLICY_IMPLEMENTATION.md) reached from within the
    # balance/register pages rather than given standing sidebar space of
    # their own.
    # Roster — the grid plus the shift-change request flow
    # (docs/SHIFT_CHANGE_REQUEST_PLAN.md). Both open to everyone: the grid
    # scopes itself to self + managed scope, and the request list scopes to
    # self / managed scope / org-wide for HR-Admins, same as leave.
    ("Roster", "Roster",        "roster_week",       10, MENU_GROUPS["all_staff"]),
    ("Roster", "Shift Changes", "shift_change_list", 20, ["All Employees", "User Admins"]),
    # Dedicated night shift windows (added 2026-11-03). Open to everyone for
    # the same reason as Shift Changes: the menu is group-gated but the
    # request/approve bars are designation tiers (TL+/APM+), which a group
    # can't express — the page scopes itself (an employee sees their own
    # windows, a TL+ their team, HR all).
    ("Roster", "Dedicated Night Shift", "dedicated_night_shift_list", 30,
        ["All Employees", "User Admins"]),
    # Timesheet (the `attendance` app underneath keeps its original name)
    ("Timesheet", "Timesheet",  "attendance_list",             10,
        MENU_GROUPS["all_staff"]),
    # Same access posture as the list: open to everyone, scoped in the view
    # to self + managed scope (can_act_on_attendance_correction) / org-wide
    # for HR-Admins — no ProtectedView gate.
    ("Timesheet", "Corrections", "attendance_correction_list",  20,
        ["All Employees", "User Admins"]),
    # Documents
    ("Documents", "Dashboard",         "documents:dashboard",            5,
        MENU_GROUPS["documents"]),
    ("Documents", "My documents",      "documents:my_documents",        10,
        MENU_GROUPS["documents"]),
    ("Documents", "Request document",  "documents:request_start",       20,
        MENU_GROUPS["documents"]),
    ("Documents", "Pending queue",     "documents:all_pending_queue",   30,
        ["User Admins", "HR", "Senior Leadership"]),
    ("Documents", "All documents",     "documents:all_documents",       40,
        ["User Admins", "HR", "Senior Leadership"]),
    # Offboarding
    ("Offboarding", "Create case (HR)", "offboarding:hr_create_resignation", 5,
        MENU_GROUPS["offboarding_ops"]),
    ("Offboarding", "My offboarding",  "offboarding:my_offboarding",    10,
        MENU_GROUPS["offboarding_people"]),
    ("Offboarding", "Pending acks",    "offboarding:manager_ack_queue", 20,
        MENU_GROUPS["offboarding_acks"]),
    ("Offboarding", "Clearance queue", "offboarding:clearance_queue",   30,
        MENU_GROUPS["offboarding_queue"]),
    ("Offboarding", "All cases",       "offboarding:case_list",         40,
        ["User Admins", "HR", "Senior Leadership"]),
    # Payroll
    # Open to everyone: build_payroll_report clamps rows to
    # visible_employees(viewer) — an IC sees only themselves, a manager their
    # downline, HR/Admins org-wide. The payroll_report ProtectedView already
    # grants All Employees; this makes the menu agree with it.
    #
    # NOTE (2026-09-10): this row's route (payroll_report) and internal name
    # are unchanged — only the user-facing label moved from "Report" to
    # "Support Info". SubMenu.objects.update_or_create is keyed on
    # (main_menu, title), NOT on route/url — re-running this seed with the
    # title changed creates a SECOND "Support Info" row alongside the old
    # untouched "Report" row rather than renaming it in place (same trap the
    # "Leave list"/"Shift summary" rename hit, see the Leave section above).
    # The existing "Report" row must be renamed directly once, e.g.:
    #   SubMenu.objects.filter(main_menu__title="Payroll", title="Report").update(title="Support Info")
    # BEFORE re-running this command, so update_or_create then matches and
    # updates that same row instead of creating a duplicate.
    ("Payroll", "Support Info",        "payroll_report",                10,
        MENU_GROUPS["payroll_ops"] + MENU_GROUPS["all_staff"]),
    # Renamed 2026-09-15: the section now lands on the overtime list (which
    # carries its own "Upload overtime" button) instead of the uploader.
    # SubMenu.objects.update_or_create keys on (main_menu, title), so this
    # seed would create a SECOND row next to the untouched "Upload overtime"
    # one — the same trap the "Report"/"Support Info" rename hit above. Run
    # `manage.py rename_overtime_menu` once BEFORE re-seeding; it renames the
    # existing row in place so update_or_create then matches it.
    ("Payroll", "Overtime",            "payroll_overtime_list",         20,
        MENU_GROUPS["payroll_ops"] + ["Supervisors"]),
    ("Payroll", "Variable pay",        "payroll_variable_pay_list",     22,
        MENU_GROUPS["payroll_ops"] + ["Supervisors"]),
    ("Payroll", "Gross salary",        "payroll_salary_list",           24,
        MENU_GROUPS["payroll_salary"]),
    # Salary loans (2026-10-08, docs/SALARY_LOAN_PLAN.md). The sidebar row is
    # payroll_ops-wide like the rest of the section; the views themselves
    # enforce the finer rule (accounts.permissions.can_manage_salary_loans —
    # the payroll-HR pair + Payroll Admins), as every payroll view does.
    ("Payroll", "Loans",               "payroll_loan_list",             26,
        MENU_GROUPS["payroll_ops"]),
    # Replaces the standalone "Upload salary" row (removed 2026-09-21): every
    # bulk uploader (overtime, SLA, test salary, gross salary) now lands on
    # one Upload Center page instead of needing its own submenu entry — see
    # payroll/upload_views.py::UPLOAD_CENTER_ENTRIES, which is where a future
    # uploader gets added. The old "Upload salary" DB row is NOT renamed here
    # (its url changes too, from payroll_salary_upload to
    # payroll_upload_center) — run `manage.py remove_payroll_upload_salary_menu
    # --apply` once on any environment that still has it, to delete the
    # orphaned row (update_or_create never prunes rows that drop out of this
    # table, so leaving it would just create the exact "Runs"/"Payrolls"
    # duplicate class of bug fixed 2026-09-21 all over again).
    ("Payroll", "Upload Center",       "payroll_upload_center",         25,
        MENU_GROUPS["payroll_ops"] + ["Supervisors"]),
    ("Payroll", "Pending salary",      "payroll_salary_pending",        26,
        MENU_GROUPS["payroll_salary"]),

    # Must match PROTECTED_VIEW_GROUPS["payroll_run_list"] — a menu item you
    # can see but not open just redirects you home. Superusers bypass the
    # view gate but NOT the menu filter (which is group-only), so they reach
    # runs by URL.
    #
    # Groups redefined 2026-09-10: "Payroll" (supervisors + the small
    # payroll-HR team) and "Payroll Admins" (the person(s) who actually run
    # payroll and send bank advice) replace the former bare "HR"/"Finance"
    # grant here — see accounts/permissions.py::can_see_payroll_money /
    # can_write_payroll_note and payroll/models.py::PayrollRunEmployeeNote.
    # "Supervisors" added back 2026-09-16 — see the REVERSAL note on
    # PROTECTED_VIEW_GROUPS["payroll_run_list"] above.
    #
    # Renamed 2026-09-21: the label is "Payrolls", not "Runs" — the seed
    # source had said "Runs" since this row was first added, and someone had
    # already renamed the live row to "Payrolls" via Django admin without
    # updating this file, which is exactly the trap "Report"->"Support Info"
    # and "Upload overtime"->"Overtime" hit above: SubMenu.objects.
    # update_or_create keys on (main_menu, title), NOT url, so a reseed with
    # the titles out of sync created a second, duplicate row instead of
    # updating the existing one. Confirmed as the intended label (not an
    # accident) 2026-09-21. `manage.py fix_payroll_runs_menu --apply`
    # consolidates any environment still carrying the old "Runs" row (or a
    # leftover duplicate of either title) down to one row titled "Payrolls" —
    # run it once before reseeding anywhere this hasn't landed yet.
    ("Payroll", "Payrolls",            "payroll_run_list",              30,
        ["Supervisors", "Payroll", "Payroll Admins"]),
    # Audit — who accessed which data or page (docs/ACCESS_LOG_PLAN.md).
    ("Audit", "Access log", "audit_access_log", 0, MENU_GROUPS["audit"]),
)


def main_menu_groups(title: str, base_groups) -> list:
    """Base grant UNION every group granted to this menu's submenus.

    dynamic_menu only queries submenus whose parent the viewer can already
    see (``main_menu__in=main``), so a group granted a submenu but not its
    parent renders for nobody — a silent, easily-missed ACL hole. Deriving
    the parent grant here means the two can't drift: add a submenu for a new
    group and the section opens up for them automatically.
    """
    groups = set(base_groups)
    for main_title, _sub_title, _url, _rank, sub_groups in ORBIS_SUB_MENUS:
        if main_title == title:
            groups |= set(sub_groups)
    return sorted(groups)


# Canonical office codes (see accounts/locations.py).
CANONICAL_OFFICE_CODES = ("MBD", "MHK", "RAOWA")


class Command(BaseCommand):
    help = "Seed Orbis groups, menus, locations, shifts, leave policies, and taxonomy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print the steps without writing anything (rolls back the transaction).",
        )
        parser.add_argument(
            "--skip-taxonomy", action="store_true",
            help="Skip the Country/Division/State/City fixture (faster on re-runs).",
        )
        parser.add_argument(
            "--skip-holidays", action="store_true",
            help="Skip the 2026 holidays fixture.",
        )
        parser.add_argument(
            "--with-demo-departments", action="store_true",
            help=("Also load the four placeholder departments (Engineering / Operations / "
                  "Human Resources / Finance) for an empty dev database. NEVER use this on a "
                  "deployment that already has real departments — see this module's docstring."),
        )
        parser.add_argument(
            "--menus-only", action="store_true",
            help=("Only ensure groups + re-apply menu ACLs and protected views. "
                  "Touches no fixtures, no org data, no leave/roster/document "
                  "seeds -- but it OVERWRITES menu ACLs (groups.set), so any "
                  "grant edited by hand in admin is replaced by the values in "
                  "this file. Run --dry-run and diff first."),
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        skip_taxonomy = opts["skip_taxonomy"]
        skip_holidays = opts["skip_holidays"]
        with_demo_departments = opts["with_demo_departments"]

        if opts["menus_only"]:
            try:
                with transaction.atomic():
                    self._seed_groups()
                    self._seed_main_menus()
                    self._seed_submenus()
                    self._call("sync_protected_views")
                    self._seed_protected_view_groups()
                    if dry_run:
                        self.stdout.write(self.style.WARNING("[dry-run] rolling back"))
                        raise _DryRun()
            except _DryRun:
                return
            self.stdout.write(self.style.SUCCESS("✅ seed_orbis (menus only) complete"))
            return

        try:
            with transaction.atomic():
                self._seed_groups()
                self._loaddata("seed_org")
                if with_demo_departments:
                    self._loaddata("seed_demo_departments")
                self._seed_canonical_joblocations()
                if skip_taxonomy:
                    self.stdout.write(self.style.WARNING("[skip] employees taxonomy"))
                else:
                    self._loaddata("seed_taxonomy")
                    if with_demo_departments:
                        self._loaddata("seed_demo_locations")
                self._call("seed_shifts")
                self._call("seed_leave_policies")
                self._call("seed_document_types")
                self._call("seed_offboarding_workflow")
                self._seed_main_menus()
                self._seed_submenus()
                self._call("sync_protected_views")
                self._seed_protected_view_groups()
                if skip_holidays:
                    self.stdout.write(self.style.WARNING("[skip] 2026 holidays"))
                else:
                    self._loaddata("seed_holidays_2026")

                if dry_run:
                    self.stdout.write(self.style.WARNING("[dry-run] rolling back"))
                    raise _DryRun()
        except _DryRun:
            return

        self.stdout.write(self.style.SUCCESS("✅ seed_orbis complete"))

    # ----- step helpers ---------------------------------------------------

    def _seed_groups(self):
        created = 0
        for name in ORBIS_GROUPS:
            _, made = Group.objects.get_or_create(name=name)
            created += int(made)
        self.stdout.write(f"groups: {len(ORBIS_GROUPS)} ensured ({created} new)")

    def _seed_canonical_joblocations(self):
        from accounts.locations import get_canonical_office_jobloc

        for code in CANONICAL_OFFICE_CODES:
            jl = get_canonical_office_jobloc(code)
            self.stdout.write(f"jobloc: {code} → {jl}")

    def _seed_protected_view_groups(self):
        from dynamic_menu.models import ProtectedView

        for view_name, group_names in PROTECTED_VIEW_GROUPS.items():
            pv = ProtectedView.objects.filter(view_name=view_name).first()
            if pv is None:
                self.stdout.write(self.style.WARNING(
                    f"  protected view {view_name!r} missing — run "
                    f"sync_protected_views first; skipped."
                ))
                continue
            groups = list(Group.objects.filter(name__in=group_names))
            pv.allowed_groups.set(groups)
            self.stdout.write(
                f"protected view: {view_name} → {sorted(g.name for g in groups)}"
            )

    def _seed_main_menus(self):
        from dynamic_menu.models import MainMenu

        for title, icon, url, rank, base_groups in ORBIS_MAIN_MENUS:
            group_names = main_menu_groups(title, base_groups)
            mm, _ = MainMenu.objects.update_or_create(
                title=title,
                defaults={
                    "icon_class": icon,
                    "url": url,
                    "rank": rank,
                    "is_active": True,
                },
            )
            groups = list(Group.objects.filter(name__in=group_names))
            mm.groups.set(groups)
            self.stdout.write(
                f"menu: {title} (rank {rank}) → {[g.name for g in groups]}"
            )

    def _seed_submenus(self):
        from dynamic_menu.models import MainMenu, SubMenu

        if not ORBIS_SUB_MENUS:
            self.stdout.write("submenus: (none defined yet)")
            return

        for main_title, sub_title, url, rank, group_names in ORBIS_SUB_MENUS:
            try:
                main = MainMenu.objects.get(title=main_title)
            except MainMenu.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"submenu skip: parent menu '{main_title}' not found"
                ))
                continue
            sm, _ = SubMenu.objects.update_or_create(
                main_menu=main,
                title=sub_title,
                defaults={
                    "url": url,
                    "rank": rank,
                    "is_active": True,
                },
            )
            groups = list(Group.objects.filter(name__in=group_names))
            sm.groups.set(groups)
            self.stdout.write(
                f"submenu: {main_title} → {sub_title} (rank {rank}) → "
                f"{[g.name for g in groups]}"
            )

    # ----- thin wrappers --------------------------------------------------

    def _loaddata(self, fixture: str):
        self.stdout.write(f"loaddata: {fixture}")
        call_command("loaddata", fixture, verbosity=0)

    def _call(self, command: str, *args, **kwargs):
        self.stdout.write(f"call: {command}")
        call_command(command, *args, verbosity=0, **kwargs)


class _DryRun(Exception):
    """Sentinel: rolls back the outer atomic block on --dry-run."""
