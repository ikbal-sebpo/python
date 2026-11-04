"""ModelForms for every editable section. `employee` is excluded — views inject it."""

from django import forms

from accounts.models import CustomUser
from core.forms import StyledMixin, use_date_picker
from employees.models import (
    Address,
    City,
    Contact,
    Country,
    DedicatedNightShiftAssignment,
    DesignationHistory,
    Education,
    Email,
    FamilyInfo,
    IdentificationNumber,
    Image,
    JobInfo,
    LegalIssue,
    Nominee,
    PersonalInfo,
    ProfessionalCertification,
    Reference,
    State,
    UnionWard,
    Vaccination,
)
from employees.services import reports_to_candidates
from employees.widgets import EmployeeSearchWidget


class _Base(forms.ModelForm, StyledMixin):
    """Every section form excludes `employee` + applies the theme styling."""

    _date_fields: tuple[str, ...] = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        use_date_picker(self, *self._date_fields)
        self.style_widgets()


# --- singletons ----------------------------------------------------------

class JobInfoForm(_Base):
    class Meta:
        model = JobInfo
        fields = (
            "reports_to", "resource_status", "billing_status",
            "cost_center", "sub_project", "note",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        target = self.instance.employee if self.instance and self.instance.employee_id else None
        self.fields["reports_to"].queryset = reports_to_candidates(target)
        self.fields["reports_to"].widget = EmployeeSearchWidget(
            for_employee_pk=target.pk if target else None,
        )


class PersonalInfoForm(_Base):
    _date_fields = ("date_of_birth",)

    class Meta:
        model = PersonalInfo
        fields = (
            "date_of_birth", "gender", "religion",
            "height_in_inch", "weight_in_kg", "blood_group",
            "nationality", "identification_mark",
            "father_name", "mother_name", "note",
        )


class FamilyInfoForm(_Base):
    _date_fields = ("marriage_date",)

    class Meta:
        model = FamilyInfo
        fields = (
            "marital_status", "spouse_name", "marriage_date",
            "total_number_of_children", "note",
        )


class LegalIssueForm(_Base):
    class Meta:
        model = LegalIssue
        fields = ("has_history_of_drug_use", "is_convicted", "case_document", "note")


# --- repeating ----------------------------------------------------------

class AddressForm(_Base):
    class Meta:
        model = Address
        fields = (
            "address_type", "country", "state", "city", "union_ward",
            "union_name", "village_area", "holding_no", "flat_no", "post_code",
        )
        labels = {
            "state": "District / City Corporation",
            "city": "Upazila / Thana / Paurashava",
            "union_ward": "Union / Ward No",
            "union_name": "Union / Ward No (if not listed)",
            "village_area": "Address Line 1 / Village or Area",
            "holding_no": "Holding / House No",
            "flat_no": "Flat No",
            "post_code": "Post Code",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not (self.instance and self.instance.pk):
            default_country_id = self._default_country_id()
            if default_country_id and not self.initial.get("country"):
                self.initial["country"] = default_country_id

        country_id = self._bound_or_instance_value("country")
        state_id = self._bound_or_instance_value("state")
        city_id = self._bound_or_instance_value("city")

        self.fields["state"].queryset = (
            State.objects.filter(country_id=country_id, is_active=True).order_by("name")
            if country_id
            else State.objects.none()
        )
        self.fields["city"].queryset = (
            City.objects.filter(state_id=state_id, is_active=True).order_by("name")
            if state_id
            else City.objects.none()
        )
        self.fields["union_ward"].queryset = (
            UnionWard.objects.filter(city_id=city_id, is_active=True).order_by("name")
            if city_id
            else UnionWard.objects.none()
        )
        self.fields["state"].required = True
        self.fields["city"].required = True
        self.fields["village_area"].required = True
        self.fields["union_name"].required = False

    def _default_country_id(self):
        country = (
            Country.objects.filter(iso_code="BD", is_active=True).first()
            or Country.objects.filter(name__iexact="Bangladesh", is_active=True).first()
        )
        return country.pk if country else None

    def _bound_or_instance_value(self, field_name: str):
        if self.is_bound:
            return self.data.get(self.add_prefix(field_name)) or None
        if self.instance and self.instance.pk:
            return getattr(self.instance, f"{field_name}_id", None)
        value = self.initial.get(field_name)
        if value is not None:
            return getattr(value, "pk", value)
        return None

    def clean(self):
        cleaned = super().clean()
        state = cleaned.get("state")
        city = cleaned.get("city")
        union_ward = cleaned.get("union_ward")

        if state and city and city.state_id != state.pk:
            self.add_error("city", "Select an upazila/thana under the selected state.")
        if city and union_ward and union_ward.city_id != city.pk:
            self.add_error(
                "union_ward",
                "Select a union/ward under the selected upazila/thana/paurashava.",
            )
        return cleaned


class EducationForm(_Base):
    class Meta:
        model = Education
        fields = (
            "degree_level", "degree_name", "major",
            "result", "result_details", "passing_year",
            "academic_institution", "institution_category", "note",
        )


class ContactForm(_Base):
    class Meta:
        model = Contact
        fields = (
            "contact_type", "contact_number",
            "emergency_contact_name", "relation_with_emergency_contact",
            "emergency_contact_address", "note",
        )


class NomineeForm(_Base):
    class Meta:
        model = Nominee
        fields = (
            "nominee_name", "relation_with_nominee",
            "nominee_occupation", "nominee_contact_number", "note",
        )


class EmailForm(_Base):
    class Meta:
        model = Email
        fields = ("email_type", "email_address")


class IdentificationNumberForm(_Base):
    class Meta:
        model = IdentificationNumber
        fields = ("id_type", "id_number")


class ReferenceForm(_Base):
    class Meta:
        model = Reference
        fields = (
            "reference_type", "reference_name", "reference_contact_number",
            "relation_with_reference",
            "reference_organization", "reference_designation",
            "reference_email_address",
        )


class ProfessionalCertificationForm(_Base):
    class Meta:
        model = ProfessionalCertification
        fields = ("certificate_name", "certificate_institution", "certificate_year")


class VaccinationForm(_Base):
    _date_fields = ("taken_date",)

    class Meta:
        model = Vaccination
        fields = ("vaccine", "dose", "taken_date")


class DesignationHistoryForm(_Base):
    """Backfills a historical row (see DesignationHistory's docstring —
    forward-going changes are captured automatically by employees/signals.py,
    this drawer is for filling in genuine pre-Orbis promotion history)."""

    _date_fields = ("effective_from", "effective_to")

    class Meta:
        model = DesignationHistory
        fields = ("designation", "effective_from", "effective_to", "reason", "note")


class ImageForm(_Base):
    class Meta:
        model = Image
        fields = ("image", "caption")


class AvatarForm(forms.ModelForm):
    """Sets CustomUser.profile_picture — the avatar shown in the topbar,
    employee cards, and the profile hero. Distinct from the Image section,
    which is a general photo gallery and doesn't drive the avatar."""

    MAX_SIZE_MB = 5

    class Meta:
        model = CustomUser
        fields = ("profile_picture",)

    def clean_profile_picture(self):
        picture = self.cleaned_data.get("profile_picture")
        if picture and picture.size > self.MAX_SIZE_MB * 1024 * 1024:
            raise forms.ValidationError(
                f"Image must be under {self.MAX_SIZE_MB} MB."
            )
        return picture


class DedicatedNightShiftAssignmentForm(_Base):
    """employee/status/approved_by/approved_at are set by the view, not the
    form — a TL+ manager requests a window, an APM+ manager approves it
    separately (employees/views.py's request/approve views)."""

    _date_fields = ("start_date", "end_date")

    class Meta:
        model = DedicatedNightShiftAssignment
        fields = ("start_date", "end_date", "note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["note"].widget = forms.Textarea(attrs={"rows": 3, "class": "form-input"})
        self.fields["note"].required = False

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        if start_date and end_date and end_date < start_date:
            raise forms.ValidationError({"end_date": "End date must be on or after the start date."})
        return cleaned


class DedicatedNightShiftRequestForm(DedicatedNightShiftAssignmentForm):
    """The request form opened from the list page's "New request" button
    (added 2026-11-03), where no `?for=` names the employee yet — so it
    carries a picker. The queryset is the requester's own candidate set
    (accounts.permissions.dedicated_night_shift_team_pks, or every active
    employee for HR/Admins); the view still re-checks
    can_request_dedicated_night_shift on the chosen employee.
    """

    employee = forms.ModelChoiceField(
        queryset=CustomUser.objects.none(),
        label="Employee",
        widget=EmployeeSearchWidget(search_url_name="dedicated_night_shift_candidates"),
        error_messages={"invalid_choice": "Pick someone from your team."},
    )

    field_order = ("employee", "start_date", "end_date", "note")

    def __init__(self, *args, candidates=None, **kwargs):
        super().__init__(*args, **kwargs)
        if candidates is not None:
            self.fields["employee"].queryset = candidates
