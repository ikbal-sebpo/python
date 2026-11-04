"""Route → data-class registry for the access log.

Keyed on the resolver's route name (``request.resolver_match.view_name``) so
it costs no DB work and no view edits. Order of resolution in ``classify``:

1. ``EXCLUDED_ROUTES`` — reference-data endpoints that reveal nothing about
   a person (country/state/team dropdown feeds), plus the topbar bell's
   background poll. Never logged.
2. ``EXACT`` — routes with a specific subject kwarg, object type or action.
3. ``PREFIXES`` — everything else by route-name prefix (``payroll_`` → SALARY).
4. ``admin:<app>_<model>_<action>`` — Django admin, refined by model so a
   superuser opening a salary structure in the admin files under SALARY.
5. Anything left is a plain PAGE.

A route under a sensitive prefix that falls through to PAGE fails
``audit.tests.RegistryGuardTests`` — that is how a new payroll or profile
view cannot land unclassified. Spec: docs/ACCESS_LOG_PLAN.md §3.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from audit.models import Action, DataClass

#: Logged with no authenticated user (the public verify pages, auth flow).
ANONYMOUS_OK_PREFIXES = ("documents_public:", "login", "logout", "oidc_")

EXCLUDED_ROUTES = frozenset({
    "ajax_load_states", "ajax_load_cities", "ajax_load_union_wards",
    "ajax_load_teams", "ajax_load_subteams",
    "policies:owners_by_department",
    "admin:jsi18n",
    # The topbar bell polls every 30s from every open tab — logging it would
    # make it the access log's biggest writer. The pages it links to are
    # logged as usual when opened.
    "notification_bell",
})

#: Path prefixes the middleware never looks at (nginx serves them in prod).
#: /media/ is NOT here: media_store.views.MediaServeView serves it and notes
#: the subject, so every file fetch is a logged access.
EXCLUDED_PATH_PREFIXES = ("/static/", "/favicon")


@dataclass(frozen=True)
class Rule:
    data_class: str
    subject_kwarg: str | None = None   # URL kwarg holding the employee's UUID
    object_type: str = ""              # model name for the ``pk``/object kwarg
    object_kwarg: str = "pk"
    action: str | None = None          # forces EXPORT / DOWNLOAD


_P, _S, _T, _L, _V, _TS, _R, _D, _PO, _O, _U, _API, _AU, _AD = (
    DataClass.PROFILE, DataClass.SALARY, DataClass.TAX, DataClass.LOAN,
    DataClass.LEAVE, DataClass.TIMESHEET, DataClass.ROSTER, DataClass.DOCUMENT,
    DataClass.POLICY, DataClass.OFFBOARDING, DataClass.USER_ADMIN, DataClass.API,
    DataClass.AUTH, DataClass.AUDIT,
)

EXACT: dict[str, Rule] = {
    # Employee profile — the UUID is the subject.
    "employee_detail": Rule(_P, "pk"),
    "employee_avatar": Rule(_P, "pk"),
    "section_add": Rule(_P, "pk"),
    "section_edit": Rule(_P, "pk"),
    "section_edit_row": Rule(_P, "pk"),
    "section_delete_row": Rule(_P, "pk"),
    "employee_service_book": Rule(_P, "pk"),
    "employee_service_book_pdf": Rule(_P, "pk", action=Action.DOWNLOAD),
    "profile_self_service_toggle": Rule(_P, "pk"),
    "team_downline": Rule(_P, "pk"),
    "employee_org_chart_node": Rule(_P, "pk"),
    "bank_account_verify": Rule(_P, object_type="BankAccount"),
    "dedicated_night_shift_approve": Rule(_P, object_type="DedicatedNightShiftAssignment"),
    "dedicated_night_shift_decline": Rule(_P, object_type="DedicatedNightShiftAssignment"),
    "dedicated_night_shift_edit": Rule(_P, object_type="DedicatedNightShiftAssignment"),
    "dedicated_night_shift_history": Rule(_P, object_type="DedicatedNightShiftAssignment"),
    "dedicated_night_shift_cancel": Rule(_P, object_type="DedicatedNightShiftAssignment"),
    # User administration.
    "user_update": Rule(_U, "pk"),
    # Payroll money.
    "payroll_salary_detail": Rule(_S, "employee_pk"),
    "payroll_salary_revise": Rule(_S, "employee_pk"),
    "payroll_salary_decide": Rule(_S, object_type="SalaryStructure"),
    "payroll_run_detail": Rule(_S, object_type="PayrollRun"),
    "payroll_run_export": Rule(_S, object_type="PayrollRun", action=Action.EXPORT),
    "payroll_run_adjustment": Rule(_S, object_type="PayrollRun"),
    "payroll_run_employee_adjust": Rule(_S, "employee_pk", object_type="PayrollRun"),
    "payroll_run_employee_note_edit": Rule(_S, "employee_pk", object_type="PayrollRun"),
    "payroll_run_variable_edit": Rule(_S, "employee_pk", object_type="PayrollRun"),
    "payroll_run_variable_delete": Rule(_S, "employee_pk", object_type="PayrollRun"),
    "payroll_run_notes_upload": Rule(_S, object_type="PayrollRun"),
    "payroll_run_notes_upload_template": Rule(_S, object_type="PayrollRun"),
    "payroll_legacy_payroll_export": Rule(_S, action=Action.EXPORT),
    "payroll_overtime_decide": Rule(_S, object_type="OvertimeEntry"),
    "payroll_overtime_edit": Rule(_S, object_type="OvertimeEntry"),
    "payroll_overtime_void": Rule(_S, object_type="OvertimeEntry"),
    "payroll_overtime_delete": Rule(_S, object_type="OvertimeEntry"),
    "payroll_variable_pay_decide": Rule(_S, object_type="VariablePayEntry"),
    # Tax.
    "payroll_tax_declaration_review": Rule(_T, object_type="EmployeeTaxDeclaration"),
    "payroll_tax_declaration_document": Rule(_T, object_type="EmployeeTaxDeclaration", action=Action.DOWNLOAD),
    # Loans.
    "payroll_loan_document": Rule(_L, object_type="SalaryLoan", action=Action.DOWNLOAD),
    # Leave.
    "employee_leave_register": Rule(_V, "pk"),
    "leave_detail": Rule(_V, object_type="LeaveApplication"),
    "leave_edit": Rule(_V, object_type="LeaveApplication"),
    "leave_approve": Rule(_V, object_type="LeaveApplication"),
    "leave_decline": Rule(_V, object_type="LeaveApplication"),
    "leave_cancel": Rule(_V, object_type="LeaveApplication"),
    "leave_balance_transactions": Rule(_V, object_type="LeaveBalance"),
    "encashment_decide": Rule(_V, object_type="EncashmentRequest"),
    # Timesheet.
    "attendance_correction_approve": Rule(_TS, object_type="AttendanceCorrection"),
    "attendance_correction_decline": Rule(_TS, object_type="AttendanceCorrection"),
    "attendance_correction_cancel": Rule(_TS, object_type="AttendanceCorrection"),
    "attendance_correction_edit": Rule(_TS, object_type="AttendanceCorrection"),
    # Roster.
    "roster_export": Rule(_R, action=Action.EXPORT),
    "shift_change_detail": Rule(_R, object_type="ShiftChangeRequest"),
    "shift_change_approve": Rule(_R, object_type="ShiftChangeRequest"),
    "shift_change_decline": Rule(_R, object_type="ShiftChangeRequest"),
    "shift_change_cancel": Rule(_R, object_type="ShiftChangeRequest"),
    "shift_type_update": Rule(_R, object_type="ShiftType"),
    "shift_type_delete": Rule(_R, object_type="ShiftType"),
    # Documents.
    "documents:download_pdf": Rule(_D, object_type="DocumentRequest", object_kwarg="request_id", action=Action.DOWNLOAD),
    "documents:request_detail": Rule(_D, object_type="DocumentRequest", object_kwarg="request_id"),
    "documents:request_cancel": Rule(_D, object_type="DocumentRequest", object_kwarg="request_id"),
    "documents:review_action": Rule(_D, object_type="DocumentReviewStep", object_kwarg="step_id"),
    "documents:approval_action": Rule(_D, object_type="DocumentApprovalStep", object_kwarg="step_id"),
    "documents_public:verify_document": Rule(_D, object_type="DocumentRequest", object_kwarg="uid"),
    "documents_public:public_preview": Rule(_D, object_type="DocumentRequest", object_kwarg="uid"),
    "documents_public:public_pdf": Rule(_D, object_type="DocumentRequest", object_kwarg="uid", action=Action.DOWNLOAD),
    # Policies.
    "policies:pdf": Rule(_PO, object_type="Policy", action=Action.DOWNLOAD),
    "policies:preview": Rule(_PO, object_type="Policy"),
    "policies:detail": Rule(_PO, object_type="Policy"),
    "policies:edit": Rule(_PO, object_type="Policy"),
    "policies:sign": Rule(_PO, object_type="Policy"),
    # Offboarding.
    "offboarding:case_detail": Rule(_O, object_type="OffboardingCase"),
    "offboarding:case_cancel": Rule(_O, object_type="OffboardingCase"),
    "offboarding:case_issue_letters": Rule(_O, object_type="OffboardingCase"),
    "offboarding:case_note_add": Rule(_O, object_type="OffboardingCase"),
    "offboarding:exit_interview": Rule(_O, object_type="OffboardingCase"),
    "offboarding:asset_return_add": Rule(_O, object_type="OffboardingCase"),
    "offboarding:asset_return_edit": Rule(_O, object_type="OffboardingCase"),
    "offboarding:asset_return_delete": Rule(_O, object_type="OffboardingCase"),
    "offboarding:resignation_decide": Rule(_O, object_type="Resignation"),
    "offboarding:resignation_hr_edit": Rule(_O, object_type="Resignation"),
    "offboarding:resignation_withdraw": Rule(_O, object_type="Resignation"),
    "offboarding:clearance_step_action": Rule(_O, object_type="ClearanceStep"),
    # Audit log itself.
    "audit_access_log": Rule(_AD),
    # Media files — the view refines data_class per prefix via note().
    "media_serve": Rule(_D),
}

#: Longest prefix wins. Keep the specific payroll_tax_/payroll_loan_ entries
#: ahead of the broad payroll_ one by length, not by position.
PREFIXES: tuple[tuple[str, Rule], ...] = (
    ("employee_", Rule(_P)),
    ("profile_self_service", Rule(_P)),
    ("night_shift_consent", Rule(_P)),
    ("dedicated_night_shift", Rule(_P)),
    ("ajax_load_employees", Rule(_P)),
    ("team_reports", Rule(_P)),
    ("user_", Rule(_U)),
    ("ajax_check_nid", Rule(_U)),
    ("ajax_suggest_identity", Rule(_U)),
    ("payroll_tax_", Rule(_T)),
    ("payroll_loan", Rule(_L)),
    ("payroll_", Rule(_S)),
    ("leave_", Rule(_V)),
    ("cpl_grant", Rule(_V)),
    ("encashment", Rule(_V)),
    ("attendance_", Rule(_TS)),
    ("roster_", Rule(_R)),
    ("shift_change", Rule(_R)),
    ("shift_type", Rule(_R)),
    ("documents:", Rule(_D)),
    ("documents_public:", Rule(_D)),
    ("policies:", Rule(_PO)),
    ("offboarding:", Rule(_O)),
    ("api_", Rule(_API)),
    ("login", Rule(_AU)),
    ("logout", Rule(_AU)),
    ("oidc_", Rule(_AU)),
)

#: Django-admin models that belong to a more specific class than ADMIN.
ADMIN_MODEL_CLASS: dict[str, str] = {
    "payroll_salarystructure": _S, "payroll_teamsalarypackage": _S,
    "payroll_payrollrun": _S, "payroll_payrollrunemployee": _S,
    "payroll_payrollrunaudit": _S, "payroll_payrollvariance": _S,
    "payroll_payrollcontrolcheck": _S, "payroll_overtimeentry": _S,
    "payroll_variablepayentry": _S, "payroll_tdsentry": _S,
    "payroll_legacypayrollmonth": _S, "payroll_employeebankaccount": _S,
    "payroll_payrollinputbatch": _S, "payroll_mealcount": _S,
    "payroll_employeetaxdeclaration": _T, "payroll_taxcomputation": _T,
    "payroll_taxchallan": _T, "payroll_taxchallanallocation": _T,
    "payroll_salaryloan": _L,
    "accounts_customuser": _P,
    "accounts_apiclient": _U,
    "employees_jobinfo": _P, "employees_personalinfo": _P, "employees_familyinfo": _P,
    "employees_education": _P, "employees_contact": _P, "employees_nominee": _P,
    "employees_email": _P, "employees_identificationnumber": _P, "employees_reference": _P,
    "employees_professionalcertification": _P, "employees_vaccination": _P,
    "employees_legalissue": _P, "employees_image": _P, "employees_address": _P,
    "employees_teamassignment": _P, "employees_nightshiftconsent": _P,
    "employees_dedicatednightshiftassignment": _P,
    "leave_leaveapplication": _V, "leave_leavebalance": _V,
    "leave_leavebalancetransaction": _V, "leave_cplgrant": _V,
    "leave_encashmentrequest": _V, "leave_legacyannualleaverecord": _V,
    "attendance_attendancepunch": _TS, "attendance_attendancecorrection": _TS,
    "documents_documentrequest": _D,
    "offboarding_resignation": _O, "offboarding_offboardingcase": _O,
    "offboarding_exitinterview": _O, "offboarding_assetreturn": _O,
    "offboarding_clearancestep": _O, "offboarding_offboardingnote": _O,
    "roster_shift": _R, "roster_roster": _R, "roster_shiftchangelog": _R,
    "roster_shiftchangerequest": _R,
    "audit_accesslog": _AD,
}

_ADMIN_ACTIONS = ("_changelist", "_change", "_add", "_delete", "_history")

#: Route-name prefixes that must never classify as PAGE (guard test).
SENSITIVE_PREFIXES = (
    "payroll_", "employee_", "section_", "user_", "documents:", "documents_public:",
    "offboarding:", "attendance_", "leave_", "cpl_grant", "encashment", "api_",
    "policies:", "roster_", "shift_change", "team_downline", "media_serve",
)


def anonymous_ok(route: str) -> bool:
    return bool(route) and route.startswith(ANONYMOUS_OK_PREFIXES)


def _admin_rule(route: str) -> Rule:
    tail = route.removeprefix("admin:")
    for suffix in _ADMIN_ACTIONS:
        if tail.endswith(suffix):
            model = tail[: -len(suffix)]
            return Rule(
                ADMIN_MODEL_CLASS.get(model, DataClass.ADMIN),
                object_type=model, object_kwarg="object_id",
            )
    return Rule(DataClass.ADMIN)


def rule_for(route: str) -> Rule | None:
    """``None`` means "never log this route"."""
    if route in EXCLUDED_ROUTES:
        return None
    if not route:
        return Rule(DataClass.PAGE)
    if route in EXACT:
        return EXACT[route]
    if route.startswith("admin:"):
        return _admin_rule(route)
    best: Rule | None = None
    best_len = -1
    for prefix, rule in PREFIXES:
        if route.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rule, len(prefix)
    return best or Rule(DataClass.PAGE)
