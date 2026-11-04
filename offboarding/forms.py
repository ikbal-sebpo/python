"""Forms for offboarding views.

* ``ResignationSubmitForm`` — employee-facing submission
* ``ResignationDecideForm`` — HR accept / reject
* ``ResignationHREditForm`` — HR fix-typos before deciding
* ``ClearanceStepActionForm`` — dynamic, generates fields from
  ``ClearanceStepFieldDefinition`` rows for the step's template
* ``ExitInterviewForm`` / ``AssetReturnForm`` / ``OffboardingNoteForm``
"""

from __future__ import annotations

from datetime import date, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Q

from core.forms import date_widget, datetime_widget
from employees.widgets import EmployeeSearchWidget
from offboarding.services import offboarding_candidates
from offboarding.models import (
    AssetReturn,
    ClearanceStep,
    ClearanceStepAttachment,
    ClearanceStepFieldDefinition,
    OffboardingCase,
    ExitInterview,
    OffboardingNote,
    Resignation,
)


# ---------------------------------------------------------------------------
# Resignation
# ---------------------------------------------------------------------------

class ResignationSubmitForm(forms.ModelForm):
    """The form an employee fills out to start their offboarding."""

    class Meta:
        model = Resignation
        fields = [
            "separation_type", "reason", "notice_period_days",
            "notice_start_date", "last_working_date",
        ]
        widgets = {
            "separation_type": forms.Select(attrs={"class": "form-input"}),
            "reason": forms.Textarea(attrs={"rows": 4, "class": "form-input"}),
            "notice_period_days": forms.NumberInput(attrs={"class": "form-input"}),
            "notice_start_date": date_widget("form-input"),
            "last_working_date": date_widget("form-input"),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Sensible defaults if not pre-filled.
        if not self.is_bound and not self.instance.pk:
            today = date.today()
            self.fields["notice_start_date"].initial = today
            self.fields["last_working_date"].initial = today + timedelta(days=30)
            # Default the required notice to this employee's own contracted
            # term (set at hire) rather than the model's flat 30-day default,
            # so the shortfall check compares against what was actually
            # agreed with them. HR/the employee can still override it.
            months = getattr(employee, "notice_period_months", None)
            if months:
                self.fields["notice_period_days"].initial = months * 30


class ResignationDecideForm(forms.ModelForm):
    """HR-only: accept, reject, or override fields before accepting."""

    decision = forms.ChoiceField(
        widget=forms.Select(attrs={"class": "form-input"}),
        choices=[
            (Resignation.Status.ACCEPTED, "Accept"),
            (Resignation.Status.REJECTED, "Reject"),
        ],
    )
    process_note = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-input"}),
        help_text="Short reason shown to the employee.",
    )

    class Meta:
        model = Resignation
        fields = ["notice_period_days", "last_working_date", "hr_notes"]
        widgets = {
            "notice_period_days": forms.NumberInput(attrs={"class": "form-input"}),
            "last_working_date": date_widget("form-input"),
            "hr_notes": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
        }

    def clean(self):
        cd = super().clean()
        if cd.get("decision") == Resignation.Status.REJECTED:
            if not (cd.get("process_note") or "").strip():
                self.add_error("process_note", "Reason required when rejecting.")
        return cd


class ResignationHREditForm(forms.ModelForm):
    class Meta:
        model = Resignation
        fields = [
            "separation_type", "reason", "notice_period_days",
            "notice_start_date", "last_working_date", "hr_notes",
        ]
        widgets = ResignationSubmitForm.Meta.widgets | {
            "hr_notes": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
        }


class HRCreateResignationForm(forms.ModelForm):
    """HR-facing form: pick the employee, fill the fields, optionally
    auto-accept on save (skips the separate decide step).

    Sensible separation-type-driven defaults are pre-filled when the form
    isn't bound — INVOLUNTARY / ABSCONDING default to 0-day notice + LWD
    today; the rest default to 30-day notice from today.
    """

    employee = forms.ModelChoiceField(
        queryset=get_user_model().objects.none(),
        widget=forms.Select(attrs={"class": "form-input"}),
        help_text="The employee being separated.",
    )
    auto_accept = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
        label="Accept immediately",
        help_text="Skip the separate decide step and start clearance now.",
        initial=True,
    )

    class Meta:
        model = Resignation
        fields = [
            "employee", "separation_type", "reason", "notice_period_days",
            "notice_start_date", "last_working_date", "hr_notes",
        ]
        widgets = {
            "separation_type": forms.Select(attrs={"class": "form-input"}),
            "reason": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            "notice_period_days": forms.NumberInput(attrs={"class": "form-input"}),
            "notice_start_date": date_widget("form-input"),
            "last_working_date": date_widget("form-input"),
            "hr_notes": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
        }

    def __init__(self, *args, viewer=None, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        user = viewer
        if user is None and hasattr(self, "request"):
            user = getattr(self.request, "user", None)
        # Candidate set lives in services so this form and the search
        # endpoint behind the picker can never drift apart.
        self.fields["employee"].queryset = (
            offboarding_candidates(user, request=request)
            .order_by("first_name", "last_name")
        )
        # A plain <select> of 1500+ people is unusable and, worse, ambiguous:
        # same-named employees are indistinguishable. Swap in the same
        # searchable picker JobInfo.reports_to uses, which shows employee ID
        # + email alongside the name. Pointed at offboarding's own endpoint
        # because the employees one is hardwired to manager candidates.
        self.fields["employee"].widget = EmployeeSearchWidget(
            search_url_name="offboarding:ajax_offboarding_employees",
        )
        if not self.is_bound and not self.instance.pk:
            today = date.today()
            self.fields["notice_start_date"].initial = today
            self.fields["last_working_date"].initial = today + timedelta(days=30)
            self.fields["notice_period_days"].initial = 30

    def clean(self):
        cd = super().clean()
        emp = cd.get("employee")
        if emp:
            conflict = Resignation.objects.filter(
                employee=emp,
                status__in=[Resignation.Status.PENDING,
                            Resignation.Status.ACCEPTED],
            ).first()
            if conflict:
                self.add_error("employee",
                    f"{emp.display_name} already has an active resignation "
                    f"({conflict.get_status_display()}).")
        return cd


# ---------------------------------------------------------------------------
# Clearance step — dynamic
# ---------------------------------------------------------------------------

class ClearanceStepActionForm(forms.ModelForm):
    """Single source of truth for "complete a clearance step".

    Pulls dynamic field definitions from
    ``step.template.field_definitions`` and generates form fields for them.
    Their cleaned values land in ``step.data_json`` so reports can query
    them without schema migrations."""

    class Meta:
        model = ClearanceStep
        fields = ["status", "remarks"]
        widgets = {
            "status": forms.Select(attrs={"class": "form-input"}),
            "remarks": forms.Textarea(attrs={"rows": 3, "maxlength": 1000,
                                              "class": "form-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].widget = forms.Select(
            attrs={"class": "form-input"},
            choices=[
                (ClearanceStep.Status.IN_PROGRESS, "Mark in progress"),
                (ClearanceStep.Status.DONE, "Mark done"),
                (ClearanceStep.Status.SKIPPED, "Skip (with reason)"),
                (ClearanceStep.Status.REJECTED, "Block / reject (with reason)"),
            ],
        )

        # Dynamic fields from the step template — empty if template was
        # deleted or doesn't define any.
        self._field_defs: list[ClearanceStepFieldDefinition] = []
        if self.instance and self.instance.template_id:
            self._field_defs = list(
                self.instance.template.field_definitions
                .filter(is_active=True).order_by("sort_order")
            )
        existing_data = self.instance.data_json or {}
        for fd in self._field_defs:
            self.fields[fd.field_key] = self._build_dynamic(fd, existing_data.get(fd.field_key))

    def _build_dynamic(self, fd: ClearanceStepFieldDefinition, initial):
        common = {
            "label": fd.field_label,
            "required": fd.is_required,
            "help_text": fd.help_text or "",
            "initial": initial,
        }
        ft = fd.field_type
        if ft == ClearanceStepFieldDefinition.FieldType.TEXTAREA:
            return forms.CharField(
                widget=forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
                **common,
            )
        if ft == ClearanceStepFieldDefinition.FieldType.BOOLEAN:
            common["required"] = False
            return forms.BooleanField(
                widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
                **{**common, "initial": initial in (True, "true", "True", "1")},
            )
        if ft == ClearanceStepFieldDefinition.FieldType.DATE:
            return forms.DateField(
                widget=date_widget("form-input"),
                **common,
            )
        # default: TEXT
        return forms.CharField(
            widget=forms.TextInput(attrs={"class": "form-input"}),
            max_length=300, **common,
        )

    def clean(self):
        cd = super().clean()
        rejecting_or_skipping = cd.get("status") in (
            ClearanceStep.Status.SKIPPED, ClearanceStep.Status.REJECTED,
        )
        if rejecting_or_skipping and not (cd.get("remarks") or "").strip():
            self.add_error("remarks", "Remarks required when skipping or rejecting.")

        # Required-dynamic check (BooleanField is_required is enforced via
        # field-level rules; we re-check the textual ones here for clarity).
        for fd in self._field_defs:
            if fd.is_required and fd.field_type != ClearanceStepFieldDefinition.FieldType.BOOLEAN:
                if not cd.get(fd.field_key):
                    self.add_error(fd.field_key, "This field is required.")
        return cd

    def save(self, commit=True, acted_by=None):
        obj = super().save(commit=False)
        data_json = dict(obj.data_json or {})
        for fd in self._field_defs:
            raw = self.cleaned_data.get(fd.field_key)
            if raw is None or raw == "":
                continue
            if fd.field_type == ClearanceStepFieldDefinition.FieldType.BOOLEAN:
                data_json[fd.field_key] = bool(raw)
            elif fd.field_type == ClearanceStepFieldDefinition.FieldType.DATE:
                data_json[fd.field_key] = raw.isoformat() if hasattr(raw, "isoformat") else str(raw)
            else:
                data_json[fd.field_key] = str(raw)
        obj.data_json = data_json
        if acted_by is not None:
            obj.acted_by = acted_by
            from django.utils import timezone
            obj.acted_at = timezone.now()
        if commit:
            obj.save()
        return obj


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

    def value_from_datadict(self, data, files, name):
        if hasattr(files, "getlist"):
            return files.getlist(name)
        return files.get(name)


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if not data:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]
        clean_one = super().clean
        return [clean_one(item, initial) for item in data]


class StepAttachmentForm(forms.Form):
    attachments = MultipleFileField(
        required=False,
        widget=MultipleFileInput(attrs={"class": "form-input", "multiple": True}),
    )

    def save(self, step, user):
        created = []
        for upload in self.cleaned_data.get("attachments") or []:
            created.append(ClearanceStepAttachment.objects.create(
                step=step,
                file=upload,
                original_name=getattr(upload, "name", "attachment"),
                uploaded_by=user,
                file_size=getattr(upload, "size", 0) or 0,
                content_type=getattr(upload, "content_type", ""),
            ))
        return created


# ---------------------------------------------------------------------------
# Queue filters
# ---------------------------------------------------------------------------

class ClearanceQueueFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "Employee, ID, step"}),
    )
    employee_id = forms.CharField(
        required=False,
        label="Employee ID",
        widget=forms.TextInput(attrs={"class": "form-input"}),
    )
    separation_type = forms.ChoiceField(
        required=False,
        label="Separation type",
        choices=[("", "All types")] + list(Resignation.SeparationType.choices),
        widget=forms.Select(attrs={"class": "form-input"}),
    )
    step_status = forms.ChoiceField(
        required=False,
        label="Step status",
        choices=[("", "All statuses")] + list(ClearanceStep.Status.choices),
        widget=forms.Select(attrs={"class": "form-input"}),
    )
    case_status = forms.ChoiceField(
        required=False,
        label="Case status",
        choices=[("", "All statuses")] + list(OffboardingCase.Status.choices),
        widget=forms.Select(attrs={"class": "form-input"}),
    )


# ---------------------------------------------------------------------------
# Exit interview, asset return, notes
# ---------------------------------------------------------------------------

class ExitInterviewForm(forms.ModelForm):
    class Meta:
        model = ExitInterview
        fields = [
            "reason_for_leaving", "would_recommend_sebpo", "would_return_if_offered",
            "what_did_well", "what_could_improve", "new_employer", "final_remarks",
        ]
        widgets = {
            "reason_for_leaving": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            "would_recommend_sebpo": forms.Select(
                attrs={"class": "form-input"},
                choices=[(i, f"{i} — {l}") for i, l in (
                    (1, "would not recommend"),
                    (2, "probably not"),
                    (3, "neutral"),
                    (4, "would recommend"),
                    (5, "would strongly recommend"),
                )],
            ),
            "would_return_if_offered": forms.Select(
                attrs={"class": "form-input"},
                choices=[(True, "Yes"), (False, "No")],
            ),
            "what_did_well": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            "what_could_improve": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            "new_employer": forms.TextInput(attrs={"class": "form-input"}),
            "final_remarks": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
        }


class AssetReturnForm(forms.ModelForm):
    class Meta:
        model = AssetReturn
        fields = ["asset_type", "asset_identifier", "returned",
                  "returned_at", "condition", "receiver", "note"]
        widgets = {
            "asset_type": forms.Select(attrs={"class": "form-input"}),
            "asset_identifier": forms.TextInput(attrs={"class": "form-input"}),
            "returned": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
            "returned_at": datetime_widget("form-input"),
            "condition": forms.TextInput(attrs={"class": "form-input"}),
            "note": forms.Textarea(attrs={"rows": 2, "class": "form-input"}),
        }


class OffboardingNoteForm(forms.ModelForm):
    class Meta:
        model = OffboardingNote
        fields = ["body", "is_internal"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 4, "class": "form-input"}),
            "is_internal": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
        }
