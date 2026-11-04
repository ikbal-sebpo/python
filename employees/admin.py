import io

from django.contrib import admin
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import CharWidget, ForeignKeyWidget
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from accounts.models import CustomUser
from employees.models import (
    Address,
    City,
    Client,
    Contact,
    Country,
    DedicatedNightShiftAssignment,
    DedicatedNightShiftAssignmentEvent,
    Division,
    Dose,
    Education,
    Email,
    FamilyInfo,
    IdentificationNumber,
    Image,
    JobInfo,
    LegalIssue,
    NightShiftConsent,
    NightShiftConsentEvent,
    Nominee,
    PersonalInfo,
    ProfessionalCertification,
    Reference,
    Relation,
    Religion,
    State,
    UnionWard,
    Vaccination,
    Vaccine,
    TeamAssignment,
)
from employees.services import record_dedicated_night_shift_event


# --- Taxonomy -------------------------------------------------------------

@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("name", "iso_code", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "iso_code")


@admin.register(Division)
class DivisionAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "is_active")
    list_filter = ("is_active", "country")
    search_fields = ("name",)


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "division", "country", "source_code", "is_active")
    list_filter = ("kind", "is_active", "country", "division")
    search_fields = ("name", "source_code")


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "state", "source_code", "is_active")
    list_filter = ("kind", "is_active", "state")
    search_fields = ("name", "source_code", "state__name")


@admin.register(UnionWard)
class UnionWardAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "city", "state", "source_code", "is_active")
    list_filter = ("kind", "is_active", "city__state")
    search_fields = ("name", "source_code", "city__name", "city__state__name")

    @admin.display(description="State")
    def state(self, obj):
        return obj.city.state


class _SimpleTaxonomyAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Religion)
class ReligionAdmin(_SimpleTaxonomyAdmin):
    pass


@admin.register(Relation)
class RelationAdmin(_SimpleTaxonomyAdmin):
    pass


@admin.register(Vaccine)
class VaccineAdmin(_SimpleTaxonomyAdmin):
    pass


@admin.register(Dose)
class DoseAdmin(_SimpleTaxonomyAdmin):
    pass


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


# --- Profile --------------------------------------------------------------

class _EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee", "updated_at")
    search_fields = ("employee__email", "employee__first_name", "employee__last_name")
    autocomplete_fields = ("employee",)
    readonly_fields = ("added_by", "added_at", "updated_by", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(JobInfo)
class JobInfoAdmin(_EmployeeAdmin):
    list_display = ("employee", "resource_status", "billing_status", "reports_to")
    list_filter = ("resource_status", "billing_status")
    autocomplete_fields = ("employee", "reports_to")


@admin.register(PersonalInfo)
class PersonalInfoAdmin(_EmployeeAdmin):
    list_display = ("employee", "date_of_birth", "gender", "religion", "blood_group")
    list_filter = ("gender", "blood_group", "religion")


@admin.register(FamilyInfo)
class FamilyInfoAdmin(_EmployeeAdmin):
    list_display = ("employee", "marital_status", "spouse_name", "total_number_of_children")
    list_filter = ("marital_status",)


@admin.register(LegalIssue)
class LegalIssueAdmin(_EmployeeAdmin):
    list_display = ("employee", "has_history_of_drug_use", "is_convicted")
    list_filter = ("has_history_of_drug_use", "is_convicted")


@admin.register(Address)
class AddressAdmin(_EmployeeAdmin):
    list_display = (
        "employee", "address_type", "union_ward", "city", "state", "country",
    )
    list_filter = ("address_type", "country", "state")


@admin.register(Education)
class EducationAdmin(_EmployeeAdmin):
    list_display = ("employee", "degree_level", "academic_institution", "passing_year")
    list_filter = ("degree_level", "institution_category")
    search_fields = (
        "employee__email", "employee__first_name", "employee__last_name",
        "academic_institution", "degree_name",
    )


@admin.register(Contact)
class ContactAdmin(_EmployeeAdmin):
    list_display = ("employee", "contact_type", "contact_number", "emergency_contact_name")
    list_filter = ("contact_type",)


@admin.register(Nominee)
class NomineeAdmin(_EmployeeAdmin):
    list_display = ("employee", "nominee_name", "relation_with_nominee", "nominee_contact_number")
    list_filter = ("relation_with_nominee",)


@admin.register(Email)
class EmailAdmin(_EmployeeAdmin):
    list_display = ("employee", "email_type", "email_address")
    list_filter = ("email_type",)


@admin.register(IdentificationNumber)
class IdentificationNumberAdmin(_EmployeeAdmin):
    list_display = ("employee", "id_type", "id_number")
    list_filter = ("id_type",)


@admin.register(Reference)
class ReferenceAdmin(_EmployeeAdmin):
    list_display = ("employee", "reference_type", "reference_name", "reference_organization")
    list_filter = ("reference_type",)


@admin.register(ProfessionalCertification)
class ProfessionalCertificationAdmin(_EmployeeAdmin):
    list_display = ("employee", "certificate_name", "certificate_institution", "certificate_year")


@admin.register(Vaccination)
class VaccinationAdmin(_EmployeeAdmin):
    list_display = ("employee", "vaccine", "dose", "taken_date")
    list_filter = ("vaccine", "dose")


@admin.register(Image)
class ImageAdmin(_EmployeeAdmin):
    list_display = ("employee", "caption", "added_at")


class DedicatedNightShiftAssignmentResource(resources.ModelResource):
    """Bulk import/export for Dedicated Night Shift assignments.

    `employee`/`approved_by` are matched by `employee_id` — the same
    natural business key `accounts.resources.TeamResource` already uses
    for CustomUser, and portable across environments (unlike the PK).

    `import_id_fields = ("employee", "start_date")`, not the row's own id:
    following roster/admin.py's and accounts/resources.py's convention of
    matching by natural key rather than the auto-incrementing PK (which
    differs between dev and prod). An employee can legitimately have
    several assignments over their tenure (docs/PAYROLL_PLAN.md — they
    cycle in and out), so `employee` alone isn't unique; pairing it with
    `start_date` is, in practice, and lets re-importing the same file
    update an assignment (e.g. extend `end_date`, flip `status`) instead
    of duplicating it, while a genuinely new start_date still creates a
    new row.

    `clean_model_instances = True` — deliberately not the sibling
    resources' default. Unlike ShiftType/Holiday/etc., this model's
    `clean()` enforces a real payroll invariant (no two APPROVED windows
    for the same employee may overlap — see the model docstring); a bulk
    import is exactly where a copy-paste date-range mistake would slip
    through unnoticed otherwise, so invalid rows are rejected with a
    clear error instead of silently corrupting who's earning the
    Dedicated Night Shift rate.

    Audit fields are *mostly* excluded, same as every other resource in
    this codebase (see roster/admin.py's header comment) — but
    `added_by`/`added_at` are importable as of 2026-09-15, a deliberate
    deviation for the same reason `approved_by`/`approved_at` already are.
    The 2026-09-10 bulk load left 116 of 161 rows with a null `added_by`,
    which the list page rendered as a blank "Requested by" and everyone
    read as missing data. They're optional — leave the columns out and the
    old behaviour is unchanged — but when the source spreadsheet does name
    a requester, it should be able to land here instead of being dropped.
    `updated_by`/`updated_at` stay excluded: those describe edits made in
    Orbis, and an import isn't one.

    Every imported row also gets an IMPORTED row in its audit ledger (see
    after_save_instance below), so a spreadsheet-loaded assignment has the
    same provenance trail as one filed through the UI.

    `approved_by`/`approved_at` ARE importable (unlike
    the plain admin form, where they're read-only) so a batch of
    already-decided assignments — e.g. migrating a quarter's worth of
    dedicated-night-shift approvals from an existing spreadsheet — can be
    brought in pre-approved without walking each one through the
    request/approve UI. This bypasses can_approve_dedicated_night_shift's
    app-level gate, same as every other Django admin bulk action here —
    only staff with model import permission can do it.
    """

    employee = fields.Field(
        column_name="employee_id",
        attribute="employee",
        widget=ForeignKeyWidget(CustomUser, field="employee_id"),
    )
    approved_by = fields.Field(
        column_name="approved_by_employee_id",
        attribute="approved_by",
        widget=ForeignKeyWidget(CustomUser, field="employee_id"),
    )
    added_by = fields.Field(
        column_name="added_by_employee_id",
        attribute="added_by",
        widget=ForeignKeyWidget(CustomUser, field="employee_id"),
    )
    # Explicit CharWidget(allow_blank=True) for both — found 2026-09-08 after
    # a real import failed with MySQL "Column 'decline_reason' cannot be
    # null". django-import-export's own WIDGETS_MAP only maps CharField to
    # CharWidget (which turns a blank cell into "" instead of None); it has
    # no TextField entry, and Django's TextField doesn't subclass CharField,
    # so decline_reason/note were silently falling through to the bare base
    # Widget (no blank handling) and importing a blank cell as None — which
    # the DB then rejects, since blank=True (form-level) was never null=True
    # (column-level) on these fields, the correct Django convention. Fixing
    # it here, not by adding null=True to the model, keeps that convention
    # intact and fixes every TextField/blank=True import, not just these two.
    decline_reason = fields.Field(
        column_name="decline_reason",
        attribute="decline_reason",
        widget=CharWidget(allow_blank=True),
    )
    note = fields.Field(
        column_name="note",
        attribute="note",
        widget=CharWidget(allow_blank=True),
    )

    class Meta:
        model = DedicatedNightShiftAssignment
        import_id_fields = ("employee", "start_date")
        fields = (
            "employee", "start_date", "end_date", "status",
            "approved_by", "approved_at", "added_by", "added_at",
            "decline_reason", "note",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True

    def after_save_instance(self, instance, row, **kwargs):
        """Give every imported row an audit-ledger entry, so a
        spreadsheet-loaded assignment has the same provenance trail as one
        filed through the UI — the 2026-09-10 bulk load predates the ledger
        and left 116 rows with no history at all (backfilled once by
        employees/migrations/0011).

        IMPORTED rather than REQUESTED even when the file names an
        `added_by`: the row arrived through a bulk load, and the ledger
        says how a record actually got here, not how it would have looked
        if someone had filed it. Skipped on dry_run, which is the
        admin's preview pass — writing events there would double-count
        every real import.
        """
        if kwargs.get("dry_run"):
            return
        record_dedicated_night_shift_event(
            instance,
            DedicatedNightShiftAssignmentEvent.Event.IMPORTED,
            instance.added_by,
            note="Created by bulk import.",
        )


def _add_status_dropdown(xlsx_bytes: bytes) -> bytes:
    """Post-process an exported .xlsx: turn the Status column into a real
    Excel dropdown restricted to DedicatedNightShiftAssignment.Status's
    actual values, so a value typed by hand while preparing a bulk
    re-import can't silently be a typo. django-import-export's own XLSX
    export has no concept of cell-level validation — this runs after the
    fact, same idea as payroll/upload.py::build_overtime_xlsx_template's
    Overtime-type dropdown, but simpler: Status is a small, fixed
    TextChoices enum, so an inline literal list (`"A,B,C"`) is enough —
    no separate Lookups sheet needed the way a growing OvertimeType
    catalog needs one.

    Also extends the dropdown 200 rows past whatever was exported, so the
    same file doubles as a template for *appending* new assignments below
    the existing ones before re-importing, not just editing what's there.
    """
    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.active

    header = [cell.value for cell in ws[1]]
    if "status" not in header:
        return xlsx_bytes  # column renamed/removed — don't guess, leave the file alone
    status_col = header.index("status") + 1

    choices = list(DedicatedNightShiftAssignment.Status.values)
    dv = DataValidation(type="list", formula1='"{}"'.format(",".join(choices)), allow_blank=True)
    dv.error = "Pick one of: {}.".format(", ".join(choices))
    dv.errorTitle = "Invalid status"
    ws.add_data_validation(dv)
    col_letter = get_column_letter(status_col)
    last_row = ws.max_row + 200
    dv.add(f"{col_letter}2:{col_letter}{last_row}")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class DedicatedNightShiftAssignmentEventInline(admin.TabularInline):
    """Read-only: the ledger is append-only, written by
    employees.services.record_dedicated_night_shift_event. Editing a row
    here would be editing history."""

    model = DedicatedNightShiftAssignmentEvent
    extra = 0
    can_delete = False
    fields = (
        "event", "occurred_at", "actor",
        "old_start_date", "old_end_date", "new_start_date", "new_end_date", "note",
    )
    readonly_fields = fields
    ordering = ("occurred_at", "pk")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DedicatedNightShiftAssignment)
class DedicatedNightShiftAssignmentAdmin(ImportExportModelAdmin, _EmployeeAdmin):
    resource_classes = [DedicatedNightShiftAssignmentResource]
    inlines = [DedicatedNightShiftAssignmentEventInline]
    list_display = (
        "employee", "employee_hr_id", "employee_team", "start_date", "end_date",
        "status", "approved_by", "approved_at",
    )
    list_filter = ("status", "employee__department", "employee__team")
    list_select_related = ("employee", "employee__team", "employee__department", "approved_by")
    autocomplete_fields = ("employee", "approved_by")
    readonly_fields = ("added_by", "added_at", "updated_by", "updated_at", "approved_by", "approved_at")

    @admin.display(description="Employee ID", ordering="employee__employee_id")
    def employee_hr_id(self, obj):
        # Not named `employee_id` — that collides with the FK column Django
        # auto-generates for the `employee` FK (DedicatedNightShiftAssignment
        # .employee_id, the raw related-object pk), which silently wins over
        # a same-named ModelAdmin method.
        return obj.employee.employee_id

    @admin.display(description="Team", ordering="employee__team")
    def employee_team(self, obj):
        return obj.employee.team

    def get_export_data(self, file_format, request, queryset, **kwargs):
        data = super().get_export_data(file_format, request, queryset, **kwargs)
        if file_format.get_extension() == "xlsx":
            data = _add_status_dropdown(data)
        return data


class NightShiftConsentEventInline(admin.TabularInline):
    model = NightShiftConsentEvent
    extra = 0
    readonly_fields = ("event", "occurred_at", "actor", "note")
    can_delete = False


@admin.register(NightShiftConsent)
class NightShiftConsentAdmin(_EmployeeAdmin):
    list_display = ("employee", "period", "consents", "source", "decided_by", "decided_at")
    list_filter = ("consents", "source", "period")
    autocomplete_fields = ("employee", "decided_by")
    readonly_fields = ("added_by", "added_at", "updated_by", "updated_at", "decided_at")
    inlines = [NightShiftConsentEventInline]
    date_hierarchy = "period"


@admin.register(TeamAssignment)
class TeamAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "department", "team", "resource_status",
        "effective_from", "effective_to", "reason",
    )
    list_filter = ("reason", "resource_status", "department")
    search_fields = ("employee__first_name", "employee__last_name", "employee__employee_id")
    autocomplete_fields = ("employee", "department", "team")
    date_hierarchy = "effective_from"
