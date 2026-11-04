"""Forms for the documents app.

* ``DocumentRequestForm`` — dynamic, generates fields per
  ``DocumentExtraFieldType`` row attached to the chosen DocumentType.
* ``ReviewActionForm`` / ``ApprovalActionForm`` — status + remarks (remarks
  required when rejecting).
* ``DocumentRequestFilterForm`` — HR queue filtering.
"""

from __future__ import annotations

from datetime import date

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from core.forms import date_widget
from documents.models import (
    DocumentExtraFieldType,
    DocumentExtraFieldValue,
    DocumentRequest,
    DocumentType,
    RequestApproval,
    RequestReview,
)


# ---------------------------------------------------------------------------
# Employee-facing
# ---------------------------------------------------------------------------

def _field_to_form_field(ft: DocumentExtraFieldType) -> forms.Field:
    kind = ft.kind
    common = {
        "required": ft.is_required,
        "label": ft.field_name,
        "help_text": ft.help_text or "",
    }
    if kind == DocumentExtraFieldType.FieldKind.TEXT:
        return forms.CharField(
            widget=forms.TextInput(attrs={"class": "form-input"}),
            max_length=300, **common,
        )
    if kind == DocumentExtraFieldType.FieldKind.TEXTAREA:
        return forms.CharField(
            widget=forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            **common,
        )
    if kind == DocumentExtraFieldType.FieldKind.NUMBER:
        return forms.DecimalField(
            widget=forms.NumberInput(attrs={"class": "form-input"}),
            max_digits=14, decimal_places=4, **common,
        )
    if kind == DocumentExtraFieldType.FieldKind.DATE:
        return forms.DateField(
            widget=date_widget("form-input"),
            **common,
        )
    if kind == DocumentExtraFieldType.FieldKind.BOOLEAN:
        common["required"] = False
        return forms.BooleanField(
            widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
            **common,
        )
    if kind == DocumentExtraFieldType.FieldKind.CHOICE:
        choices = [("", "—")] + [(c, c) for c in (ft.choices or [])]
        return forms.ChoiceField(
            widget=forms.Select(attrs={"class": "form-input"}),
            choices=choices, **common,
        )
    return forms.CharField(widget=forms.TextInput(attrs={"class": "form-input"}), **common)


def _serialise_extra_value(ft: DocumentExtraFieldType, raw) -> str:
    if ft.kind == DocumentExtraFieldType.FieldKind.BOOLEAN:
        return "true" if raw else "false"
    if ft.kind == DocumentExtraFieldType.FieldKind.DATE:
        return raw.isoformat() if isinstance(raw, date) else str(raw)
    return str(raw)


def _shift_month(month: date, offset: int) -> date:
    month_index = month.year * 12 + month.month - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def _month_choices(initial=""):
    today = date.today().replace(day=1)
    values = [_shift_month(today, offset).strftime("%b-%y") for offset in range(1, -25, -1)]
    initial = str(initial or "").strip()
    if initial and initial not in values:
        values.insert(0, initial)
    return [("", "—")] + [(value, value) for value in values]


class DocumentRequestForm(forms.ModelForm):
    """The request creation form.

    Built from the dynamic ``DocumentExtraFieldType`` rows for the chosen
    DocumentType. The ``employee`` and ``document_type`` come from the view,
    not the form, so they're never user-editable.
    """

    class Meta:
        model = DocumentRequest
        fields: list[str] = []  # everything is dynamic

    def __init__(self, *args, document_type: DocumentType = None,
                 user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if document_type is None:
            raise ValueError("DocumentRequestForm requires a document_type kwarg.")
        self._document_type = document_type
        self._user = user

        # Standard 'reason' field (always shown — used in the request detail page)
        self.fields["reason"] = forms.CharField(
            widget=forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            required=False,
            label="Reason / context",
            help_text="Optional. Helps HR understand the request.",
        )

        # Dynamic per-DocumentType fields
        self._extra_field_types = list(
            DocumentExtraFieldType.objects
            .filter(
                document_type=document_type,
                is_active=True,
                fillable_by=DocumentExtraFieldType.FillableBy.REQUESTER,
            )
            .order_by("sort_order")
        )
        for ft in self._extra_field_types:
            field = self._build_field(ft)
            self.fields[ft.field_key] = field

    def _build_field(self, ft: DocumentExtraFieldType) -> forms.Field:
        return _field_to_form_field(ft)

    def clean(self):
        data = super().clean()
        for ft in self._extra_field_types:
            if ft.is_required and ft.kind != DocumentExtraFieldType.FieldKind.BOOLEAN:
                if not data.get(ft.field_key):
                    self.add_error(ft.field_key, "This field is required.")
        return data

    @transaction.atomic
    def save(self, commit: bool = True, employee=None) -> DocumentRequest:
        if employee is None:
            raise ValueError("DocumentRequestForm.save requires an `employee` kwarg.")

        cleaned = {k: v for k, v in self.cleaned_data.items() if k}
        input_data: dict = {}

        if "reason" in cleaned:
            input_data["reason"] = cleaned["reason"] or ""

        # Persist dynamic field values to both input_data and the
        # DocumentExtraFieldValue rows (latter is what reports + the PDF
        # context map iterate; former is convenient for ad-hoc keys).
        extra_values: dict[str, str] = {}
        for ft in self._extra_field_types:
            raw = cleaned.get(ft.field_key)
            if raw is None or raw == "":
                continue
            str_val = _serialise_extra_value(ft, raw)
            extra_values[ft.field_key] = str_val
            input_data[ft.field_key] = str_val

        obj = self.instance
        obj.document_type = self._document_type
        obj.employee = employee
        obj.applied_by = self._user or employee
        obj.added_by = self._user
        obj.updated_by = self._user
        obj.input_data = input_data
        if commit:
            obj.save()
            for ft in self._extra_field_types:
                if ft.field_key in extra_values:
                    DocumentExtraFieldValue.objects.update_or_create(
                        request=obj, field_type=ft,
                        defaults={
                            "value": extra_values[ft.field_key],
                            "submitted_by": self._user,
                            "is_active": True,
                        },
                    )
        return obj


# ---------------------------------------------------------------------------
# Review + Approval action forms
# ---------------------------------------------------------------------------

class _ActionFormBase(forms.ModelForm):
    """Common pattern: status + remarks, with remarks required when rejecting."""

    _reject_value: str = ""  # subclass sets

    class Meta:
        fields = ["status", "remarks"]
        widgets = {
            "remarks": forms.Textarea(attrs={"rows": 3, "maxlength": 800, "class": "form-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].widget = forms.Select(
            attrs={"class": "form-input"}, choices=self._status_choices(),
        )
        self.fields["status"].help_text = "Pick one."

    def _status_choices(self):  # subclass override
        raise NotImplementedError

    def clean(self):
        data = super().clean()
        if data.get("status") == self._reject_value and not (data.get("remarks") or "").strip():
            self.add_error("remarks", "Remarks are required when rejecting.")
        return data


class ReviewActionForm(_ActionFormBase):
    _reject_value = RequestReview.Status.REJECTED

    class Meta(_ActionFormBase.Meta):
        model = RequestReview

    def _status_choices(self):
        return [
            (RequestReview.Status.REVIEWED, "Mark reviewed"),
            (RequestReview.Status.REJECTED, "Reject"),
        ]


class ApprovalActionForm(_ActionFormBase):
    _reject_value = RequestApproval.Status.REJECTED

    class Meta(_ActionFormBase.Meta):
        model = RequestApproval

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request_obj = self.instance.request
        existing_values = {
            value.field_type.field_key: value.value
            for value in request_obj.extra_field_values.filter(is_active=True).select_related("field_type")
        }
        self._extra_field_types = list(
            DocumentExtraFieldType.objects
            .filter(
                document_type=request_obj.document_type,
                is_active=True,
                fillable_by=DocumentExtraFieldType.FillableBy.APPROVER,
            )
            .exclude(
                document_type__code="SALARY_CERT",
                field_key="ref_no",
            )
            .order_by("sort_order")
        )
        for ft in self._extra_field_types:
            field = _field_to_form_field(ft)
            field.required = False
            field.initial = existing_values.get(
                ft.field_key,
                (request_obj.input_data or {}).get(ft.field_key, ""),
            )
            if request_obj.document_type.code == "SALARY_CERT" and ft.field_key.endswith("_name"):
                field = forms.ChoiceField(
                    choices=_month_choices(field.initial),
                    required=False,
                    label=ft.field_name,
                    help_text=ft.help_text or "",
                    widget=forms.Select(attrs={"class": "form-input"}),
                )
                field.initial = existing_values.get(
                    ft.field_key,
                    (request_obj.input_data or {}).get(ft.field_key, ""),
                )
            self.fields[ft.field_key] = field
        self._configure_salary_wizard(request_obj)

    def _configure_salary_wizard(self, request_obj):
        self.has_salary_month_wizard = request_obj.document_type.code == "SALARY_CERT"
        self.salary_common_fields = []
        self.salary_month_field_groups = []
        self.action_fields = [self["status"], self["remarks"]]
        if not self.has_salary_month_wizard:
            self.extra_action_fields = [self[ft.field_key] for ft in self._extra_field_types]
            return

        common_keys = ("ref_no", "letter_date", "bank_account_no")
        for key in common_keys:
            if key in self.fields:
                self.salary_common_fields.append(self[key])

        component_keys = (
            ("gross_salary", "Gross Salary"),
            ("incentive_lwp", "Incentive / LWP"),
            ("festival_bonus", "Festival Bonus"),
            ("gross_payment", "Gross Payment"),
            ("salary_due_adj", "Salary Due / Adjustment"),
            ("tada_others", "TA/DA & Others"),
            ("lunch_allowance", "Lunch Allowance"),
            ("tax", "Tax"),
            ("net_take_home", "Net Take-home Salary"),
        )
        for idx in (1, 2, 3):
            fields = []
            name_key = f"salary_month_{idx}_name"
            if name_key in self.fields:
                fields.append(self[name_key])
            for key, _label in component_keys:
                field_key = f"salary_month_{idx}_{key}"
                if field_key in self.fields:
                    fields.append(self[field_key])
            self.salary_month_field_groups.append({
                "number": idx,
                "title": f"Month {idx}",
                "fields": fields,
            })

        wizard_keys = set(common_keys)
        for idx in (1, 2, 3):
            wizard_keys.add(f"salary_month_{idx}_name")
            for key, _label in component_keys:
                wizard_keys.add(f"salary_month_{idx}_{key}")
        self.extra_action_fields = [
            self[ft.field_key]
            for ft in self._extra_field_types
            if ft.field_key not in wizard_keys
        ]

    def _status_choices(self):
        return [
            (RequestApproval.Status.APPROVED, "Approve"),
            (RequestApproval.Status.REJECTED, "Reject"),
        ]

    def clean(self):
        data = super().clean()
        if data.get("status") != RequestApproval.Status.APPROVED:
            return data
        for ft in self._extra_field_types:
            if ft.is_required and ft.kind != DocumentExtraFieldType.FieldKind.BOOLEAN:
                if not data.get(ft.field_key):
                    self.add_error(ft.field_key, "This field is required before approval.")
        return data

    @transaction.atomic
    def save_extra_values(self, *, submitted_by) -> None:
        request_obj = self.instance.request
        input_data = dict(request_obj.input_data or {})
        for ft in self._extra_field_types:
            raw = self.cleaned_data.get(ft.field_key)
            if raw is None or raw == "":
                continue
            str_val = _serialise_extra_value(ft, raw)
            input_data[ft.field_key] = str_val
            DocumentExtraFieldValue.objects.update_or_create(
                request=request_obj,
                field_type=ft,
                defaults={
                    "value": str_val,
                    "submitted_by": submitted_by,
                    "is_active": True,
                },
            )
        request_obj.input_data = input_data
        request_obj.updated_by = submitted_by
        request_obj.save(update_fields=["input_data", "updated_by", "updated_at"])


# ---------------------------------------------------------------------------
# Filter forms
# ---------------------------------------------------------------------------

class DocumentRequestFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Employee or email", "class": "form-input"}),
    )
    document_type = forms.ModelChoiceField(
        queryset=DocumentType.objects.filter(is_active=True),
        required=False, empty_label="All types",
        widget=forms.Select(attrs={"class": "form-input"}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses")] + list(DocumentRequest.Status.choices),
        widget=forms.Select(attrs={"class": "form-input"}),
    )
    mine_only = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
        label="Only requests waiting on me",
    )
    date_from = forms.DateField(
        required=False,
        widget=date_widget("form-input"),
    )
    date_to = forms.DateField(
        required=False,
        widget=date_widget("form-input"),
    )


class MyDocumentsFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Search", "class": "form-input"}),
    )
    document_type = forms.ModelChoiceField(
        queryset=DocumentType.objects.filter(is_active=True),
        required=False, empty_label="All types",
        widget=forms.Select(attrs={"class": "form-input"}),
    )
