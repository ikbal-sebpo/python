from django.contrib import admin
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import CharWidget, ForeignKeyWidget

from accounts.models import AccessLevel, Department, Grade, SubProject, Team, WorkLocation
from core.admin_mixins import PayrollMoneyAdminMixin, money_display
from payroll.models import (
    Bank,
    BankBranch,
    EmployeeBankAccount,
    EmployeeTaxDeclaration,
    MealCount,
    MealDeductionTier,
    OvertimeEntry,
    OvertimeType,
    PayComponent,
    PayrollControlCheck,
    PayrollInputBatch,
    PayrollPeriod,
    PayrollRun,
    PayrollRunAudit,
    PayrollRunEmployee,
    PayrollRunLine,
    PayrollSettings,
    PayrollVariance,
    PayRate,
    PayslipCounter,
    SalaryLoan,
    SalaryLoanAdjustment,
    SalaryLoanSkip,
    LegacyPayrollMonth,
    TaxChallan, TaxChallanAllocation,
    SalaryStructure,
    TaxCategory,
    TaxComputation,
    TaxExemptionThreshold,
    TaxSlab,
    TaxYear,
    TDSEntry,
    VariablePayEntry,
    TeamSalaryPackage,
)


@admin.register(PayrollSettings)
class PayrollSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not PayrollSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PayslipCounter)
class PayslipCounterAdmin(admin.ModelAdmin):
    list_display = ("id", "last_value")

    def has_add_permission(self, request):
        return not PayslipCounter.objects.exists()


@admin.register(PayComponent)
class PayComponentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "entry_mode", "is_taxable", "sequence", "is_active")
    list_filter = ("kind", "entry_mode", "is_taxable", "is_active")
    search_fields = ("code", "name")
    ordering = ("sequence", "code")


@admin.register(PayrollPeriod)
class PayrollPeriodAdmin(admin.ModelAdmin):
    list_display = ("__str__", "year", "month", "period_type", "status", "days_in_period")
    list_filter = ("status", "period_type", "year")
    search_fields = ("year", "month")
    ordering = ("-year", "-month")


# Every admin below that shows a per-employee amount carries
# PayrollMoneyAdminMixin (docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.6): the
# figures render for a superuser or a Payroll Admin only, other staff keep
# the row minus its money columns. Rate cards (PayRate, MealDeductionTier,
# tax tables) are policy, not personal money, and stay fully visible.


@admin.register(SalaryStructure)
class SalaryStructureAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("gross_monthly",)
    list_display = ("employee", money_display("gross_monthly"), "effective_from", "effective_to", "status", "reason")
    list_filter = ("status", "reason")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "processed_by")
    ordering = ("-effective_from",)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(OvertimeType)
class OvertimeTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "basis", "component", "is_active")
    list_filter = ("basis", "is_active")
    search_fields = ("code", "name")
    autocomplete_fields = ("component",)


class PayRateResource(resources.ModelResource):
    """Bulk import/export for payroll rate rows.

    Unlike every other resourced model in this codebase (ShiftType.name,
    Holiday.date, Team.name, ...), PayRate has no single natural unique
    field — it's a scope table where a row is identified by *which
    combination* of dimensions it applies to (see resolve_rate() in
    payroll/rates.py). `import_id_fields` is therefore the full set of
    scope dimensions rather than a short key: on the 2026-09-16 dev data,
    a narrower key of just (rate_type, overtime_type, department, team)
    already collided on 8 of 30 rows — e.g. two NIGHT_REGULAR rows for
    the same department differing only by resource_status ("" vs
    "TRAINEE"), and two differing only by a min_gross/max_gross band.
    Every field below is optional in the spreadsheet (a blank cell
    matches NULL via Django's ORM), so a row that only sets 2-3
    dimensions behaves exactly like a short-key import would; it's only
    rows that share those 2-3 dimensions but differ elsewhere that need
    the rest of the tuple to disambiguate.

    FK fields are matched by the natural key that's actually unique at
    the DB level: `code` for OvertimeType/PayComponent (unique
    SlugFields), `name` for the six accounts scope models (all `_Named`,
    unique CharField). `resource_status` gets an explicit
    CharWidget(allow_blank=True) — it's a CharField(blank=True), and
    import-export's WIDGETS_MAP has no TextField/blank-CharField
    handling of its own, so a blank cell would otherwise import as None
    and MySQL would reject it (same fix as decline_reason/note on
    employees.admin.DedicatedNightShiftAssignmentResource).

    clean_model_instances=True — PayRate.clean() enforces real payroll
    invariants (overtime_type required iff rate_type=OVERTIME, the
    GROSS_30TH_PER_DAY multiplier bounds); a bad bulk row here directly
    mispays overtime/night rates, so invalid rows should be rejected
    with a clear error rather than silently saved.
    """

    overtime_type = fields.Field(
        column_name="overtime_type",
        attribute="overtime_type",
        widget=ForeignKeyWidget(OvertimeType, field="code"),
    )
    component = fields.Field(
        column_name="component",
        attribute="component",
        widget=ForeignKeyWidget(PayComponent, field="code"),
    )
    department = fields.Field(
        column_name="department",
        attribute="department",
        widget=ForeignKeyWidget(Department, field="name"),
    )
    team = fields.Field(
        column_name="team",
        attribute="team",
        widget=ForeignKeyWidget(Team, field="name"),
    )
    sub_project = fields.Field(
        column_name="sub_project",
        attribute="sub_project",
        widget=ForeignKeyWidget(SubProject, field="name"),
    )
    work_location = fields.Field(
        column_name="work_location",
        attribute="work_location",
        widget=ForeignKeyWidget(WorkLocation, field="name"),
    )
    grade = fields.Field(
        column_name="grade",
        attribute="grade",
        widget=ForeignKeyWidget(Grade, field="name"),
    )
    min_access_level = fields.Field(
        column_name="min_access_level",
        attribute="min_access_level",
        widget=ForeignKeyWidget(AccessLevel, field="name"),
    )
    resource_status = fields.Field(
        column_name="resource_status",
        attribute="resource_status",
        widget=CharWidget(allow_blank=True),
    )

    class Meta:
        model = PayRate
        import_id_fields = (
            "rate_type", "overtime_type", "department", "team",
            "sub_project", "work_location", "grade",
            "min_gross", "max_gross", "resource_status", "min_access_level",
            "effective_from",
        )
        fields = (
            "rate_type", "overtime_type", "component",
            "department", "team", "sub_project", "work_location", "grade",
            "min_gross", "max_gross", "resource_status", "min_access_level",
            "basis", "amount", "effective_from", "effective_to", "is_active",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True


@admin.register(PayRate)
class PayRateAdmin(ImportExportModelAdmin):
    resource_classes = [PayRateResource]
    list_display = (
        "rate_type", "overtime_type", "department", "team", "sub_project",
        "work_location", "grade", "min_access_level", "resource_status",
        money_display("min_gross"), money_display("max_gross"), "basis", money_display("amount"),
        "effective_from", "effective_to", "is_active",
    )
    list_filter = ("rate_type", "basis", "is_active", "department", "resource_status", "min_access_level")
    autocomplete_fields = (
        "overtime_type", "component", "department", "team", "sub_project",
        "work_location", "grade", "min_access_level",
    )
    ordering = ("rate_type", "-effective_from")


@admin.register(MealDeductionTier)
class MealDeductionTierAdmin(admin.ModelAdmin):
    list_display = (money_display("min_gross"), money_display("max_gross"), money_display("amount_per_meal"), "is_active")
    ordering = ("min_gross",)


@admin.register(PayrollInputBatch)
class PayrollInputBatchAdmin(admin.ModelAdmin):
    list_display = ("input_kind", "source", "period", "status", "row_count", "error_count", "added_at")
    list_filter = ("input_kind", "source", "status", "period")
    search_fields = ("input_kind",)
    ordering = ("-added_at",)


@admin.register(OvertimeEntry)
class OvertimeEntryAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "work_date", "worked_in", "overtime_type", "seconds", "status",
        "reviewed_by", "processed_by", "period",
    )
    list_filter = ("status", "overtime_type", "worked_in", "period")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "overtime_type", "worked_in", "period", "batch", "reviewed_by", "processed_by")
    ordering = ("-work_date",)


@admin.register(VariablePayEntry)
class VariablePayEntryAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("amount",)
    list_display = ("employee", "component", money_display("amount"), "days", "status", "period", "added_at")
    list_filter = ("status", "component", "period")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "reason")
    autocomplete_fields = ("employee", "component", "period", "batch", "processed_by")
    ordering = ("-added_at",)


@admin.register(TDSEntry)
class TDSEntryAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("amount",)
    list_display = ("employee", "period", money_display("amount"))
    list_filter = ("period",)
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")
    autocomplete_fields = ("employee", "period", "batch")
    ordering = ("-period__year", "-period__month")


@admin.register(MealCount)
class MealCountAdmin(admin.ModelAdmin):
    list_display = ("employee", "period", "total_meals", "batch")
    list_filter = ("period",)
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "period", "batch")
    ordering = ("-period__year", "-period__month")


class TaxExemptionThresholdInline(admin.TabularInline):
    model = TaxExemptionThreshold
    extra = 0
    autocomplete_fields = ("category",)


class TaxSlabInline(admin.TabularInline):
    model = TaxSlab
    extra = 0
    ordering = ("sequence",)


@admin.register(TaxYear)
class TaxYearAdmin(admin.ModelAdmin):
    list_display = ("code", "start_date", "end_date", "calculation_method", money_display("exemption_cap"), money_display("minimum_tax"), "declaration_deadline", "orbis_live_from", "is_active")
    autocomplete_fields = ("orbis_live_from",)
    list_filter = ("calculation_method", "is_active")
    search_fields = ("code",)
    inlines = [TaxExemptionThresholdInline, TaxSlabInline]


@admin.register(TaxCategory)
class TaxCategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "sequence")
    search_fields = ("code", "name")
    ordering = ("sequence", "code")


@admin.register(EmployeeTaxDeclaration)
class EmployeeTaxDeclarationAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = (
        "allowable_investment", "actual_investment", "source_tax", "ait_car_advance_tax",
        "opening_tds_deducted", "opening_additional_pay",
    )
    list_display = (
        "employee", "tax_year", "category", money_display("allowable_investment"),
        money_display("actual_investment"), money_display("ait_car_advance_tax"),
        "submission_source", "submitted_at",
    )
    list_filter = ("tax_year", "category", "submission_source")
    # Clearing submitted_at here is the one sanctioned way to unlock a
    # confirmed declaration (see EmployeeTaxDeclaration.is_locked).
    fieldsets = (
        (None, {"fields": ("employee", "tax_year", "category", "note")}),
        ("Investment", {"fields": ("allowable_investment", "actual_investment")}),
        ("Advance tax", {"fields": ("source_tax", "ait_car_advance_tax", "ait_document")}),
        ("Cutover openings (months before TaxYear.orbis_live_from)",
         {"fields": ("opening_tds_deducted", "opening_additional_pay")}),
        ("Confirmation", {"fields": ("submitted_at", "submitted_by", "submission_source")}),
    )
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "tax_year", "category")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TaxComputation)
class TaxComputationAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = (
        "gross_annual_projected", "exempted_income", "taxable_income", "tax_before_rebate",
        "rebate_amount", "net_tax_payable", "source_tax", "ait_car_advance_tax", "annual_tds",
        "monthly_tds", "additional_pay_ytd", "tds_already_deducted", "tax_remaining", "breakdown_json",
    )
    list_display = ("employee", "run", "category_name", money_display("taxable_income"), money_display("net_tax_payable"), money_display("monthly_tds"))
    list_filter = ("run", "tax_year")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Bank)
class BankAdmin(admin.ModelAdmin):
    list_display = ("name", "short_code", "is_active")
    search_fields = ("name", "short_code")


@admin.register(BankBranch)
class BankBranchAdmin(admin.ModelAdmin):
    list_display = ("bank", "name", "routing_number", "is_active")
    list_filter = ("bank", "is_active")
    search_fields = ("name", "routing_number")
    autocomplete_fields = ("bank",)


@admin.register(EmployeeBankAccount)
class EmployeeBankAccountAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("account_number", "routing_number")
    list_display = ("employee", "account_type", "account_number", "bank", "is_primary", "is_verified")
    list_filter = ("account_type", "is_primary", "is_verified")
    # account_number is encrypted at rest — the database can't search it.
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")
    autocomplete_fields = ("employee", "bank", "branch", "verified_by")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


class PayrollRunLineInline(PayrollMoneyAdminMixin, admin.TabularInline):
    MONEY_FIELDS = ("amount", "full_amount", "rate")
    model = PayrollRunLine
    extra = 0
    fields = ("component_code", "amount", "full_amount", "quantity", "rate", "unit", "source_kind", "calc_note")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PayrollRunEmployee)
class PayrollRunEmployeeAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = (
        "gross_earnings", "total_additions", "total_deductions", "taxable_gross",
        "tax_deducted", "net_pay", "bank_account_number",
    )
    list_display = (
        "employee_code", "full_name", "run", "status", "payable_days",
        money_display("gross_earnings"), money_display("total_deductions"), money_display("tax_deducted"), money_display("net_pay"),
    )
    list_filter = ("status", "run")
    search_fields = ("employee_code", "full_name")
    inlines = [PayrollRunLineInline]

    def has_add_permission(self, request):
        return False


@admin.register(PayrollControlCheck)
class PayrollControlCheckAdmin(admin.ModelAdmin):
    list_display = ("check_code", "severity", "status", "run", "employee", "message")
    list_filter = ("severity", "status", "check_code", "run")
    search_fields = ("message", "employee__email", "employee__first_name", "employee__last_name")
    autocomplete_fields = ("employee", "resolved_by")


@admin.register(PayrollVariance)
class PayrollVarianceAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("prev_net", "curr_net", "delta", "delta_pct")
    list_display = ("run", "employee", money_display("prev_net"), money_display("curr_net"), money_display("delta"), "delta_pct", "flagged")
    list_filter = ("flagged", "run")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")


@admin.register(PayrollRunAudit)
class PayrollRunAuditAdmin(admin.ModelAdmin):
    list_display = ("event", "run", "from_status", "to_status", "actor", "occurred_at")
    list_filter = ("event", "run")
    ordering = ("-occurred_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PayrollRun)
class PayrollRunAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("gross_total", "total_additions", "total_deductions", "tax_total", "net_total")
    list_display = (
        "__str__", "period", "run_type", "status", "employee_count",
        money_display("gross_total"), money_display("net_total"), "calculated_at",
    )
    list_filter = ("status", "run_type", "period")
    autocomplete_fields = ("period",)
    readonly_fields = ("engine_version", "config_snapshot", "calculated_at", "rejection_history")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TeamSalaryPackage)
class TeamSalaryPackageAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("gross_monthly",)
    list_display = (
        "department", "team", "designation", "resource_status",
        money_display("gross_monthly"), "effective_from", "effective_to", "is_active",
    )
    list_filter = ("department", "is_active", "resource_status")
    search_fields = ("department__name", "team__name", "note")
    autocomplete_fields = ("department", "team", "designation")


# --- Salary loans (docs/SALARY_LOAN_PLAN.md) --------------------------------
# Rescue path only: the list at /payroll/loans/ is the working surface.

class SalaryLoanAdjustmentInline(admin.TabularInline):
    model = SalaryLoanAdjustment
    extra = 0
    fields = ("occurred_on", "reason", "delta", "note", "created_by", "created_at")
    readonly_fields = ("created_at",)


class SalaryLoanSkipInline(admin.TabularInline):
    model = SalaryLoanSkip
    extra = 0
    fields = ("period", "reason", "approved_by_hr", "created_by", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("period", "approved_by_hr")


@admin.register(SalaryLoan)
class SalaryLoanAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("principal", "installment_amount", "opening_recovered")
    list_display = (
        "file_reference", "employee", "approved_on", money_display("principal"),
        "installment_count", money_display("installment_amount"), "first_deduction_period", "status",
        "recorded_source",
    )
    list_filter = ("status", "recorded_source")
    search_fields = ("file_reference", "employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "first_deduction_period")
    fieldsets = (
        (None, {"fields": ("employee", "file_reference", "approved_on", "approval_document", "recorded_source", "note")}),
        ("Terms", {"fields": ("principal", "installment_count", "installment_amount", "first_deduction_period", "opening_recovered")}),
        ("State", {"fields": ("status", "disbursed_on", "disbursed_by", "disbursement_note", "cancelled_reason")}),
    )
    inlines = (SalaryLoanAdjustmentInline, SalaryLoanSkipInline)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ---------------------------------------------------------------------------
# Pre-Orbis payroll months + tax challans (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md).
# Rescue path only: the Upload Center card is the working surface.

@admin.register(TaxChallan)
class TaxChallanAdmin(admin.ModelAdmin):
    # total_amount is the company's deposit, not personal pay — plaintext and
    # shown to every admin user (the PayRate posture).
    list_display = ("challan_number", "tax_year", "deposit_date", "bank", "total_amount", "month_label", "run", "source")
    list_filter = ("tax_year", "bank", "source")
    search_fields = ("challan_number", "note")
    autocomplete_fields = ("tax_year", "bank")
    raw_id_fields = ("run", "batch")
    date_hierarchy = "deposit_date"

    @admin.display(description="Payroll month")
    def month_label(self, obj):
        return obj.month_label or "—"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TaxChallanAllocation)
class TaxChallanAllocationAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    """Per-employee share of a deposit — personal tax, so the amount is
    encrypted and hidden from admins who may not see payroll money."""

    MONEY_FIELDS = ("amount",)
    list_display = ("challan", "employee", money_display("amount"), "source", "batch")
    list_filter = ("challan__tax_year", "source")
    search_fields = ("challan__challan_number", "employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee",)
    raw_id_fields = ("challan", "batch")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(LegacyPayrollMonth)
class LegacyPayrollMonthAdmin(PayrollMoneyAdminMixin, admin.ModelAdmin):
    MONEY_FIELDS = ("lines", "tds_deducted", "net_pay")
    list_display = ("employee", "tax_year", "year", "month", money_display("tds_deducted"), money_display("net_pay"), "batch")
    list_filter = ("tax_year", "year", "month")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "tax_year")
    raw_id_fields = ("batch",)
    fieldsets = (
        (None, {"fields": ("employee", "tax_year", "year", "month", "batch")}),
        ("Amounts (as Finance supplied them)", {"fields": ("lines", "tds_deducted", "net_pay")}),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
