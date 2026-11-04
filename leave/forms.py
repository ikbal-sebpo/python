from __future__ import annotations

from datetime import date

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import F
from django.utils import timezone

from core.forms import StyledMixin, use_date_picker
from leave.models import CplGrant, LeaveApplication
from leave.services import (
    eligible_leave_policies,
    validate_application_against_balance,
    validate_application_attachment,
    validate_application_eligibility,
)
from roster.models import ShiftType


class LeaveApplicationForm(forms.ModelForm, StyledMixin):
    class Meta:
        model = LeaveApplication
        fields = (
            "leave_type", "start_date", "end_date",
            "is_half_day", "half_day_period",
            "note", "attachment",
        )

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee

        # Filter the leave-type dropdown to policies this employee is eligible
        # for (gender / service / surviving children gates).
        if employee is not None:
            eligible_policies = eligible_leave_policies(employee)
            eligible_st_pks = list(eligible_policies.values_list("shift_type_id", flat=True))
            self.fields["leave_type"].queryset = (
                ShiftType.objects
                .filter(
                    pk__in=eligible_st_pks,
                    is_active=True,
                    category=ShiftType.Category.LEAVE,
                )
                .order_by(F("sort_order").asc(nulls_last=True), "name")
            )
        else:
            self.fields["leave_type"].queryset = (
                ShiftType.objects
                .filter(is_active=True, category=ShiftType.Category.LEAVE)
                .order_by(F("sort_order").asc(nulls_last=True), "name")
            )
        self.fields["leave_type"].label_from_instance = (
            lambda st: f"{st.name} — {st.full_name}" if st.full_name else st.name
        )

        use_date_picker(self, "start_date", "end_date")
        self.fields["note"].widget.attrs["rows"] = 3
        self.fields["half_day_period"].required = False
        self.style_widgets()

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        leave_type = cleaned.get("leave_type")
        is_half_day = cleaned.get("is_half_day")
        half_day_period = cleaned.get("half_day_period")

        if start and end and end < start:
            self.add_error("end_date", "End date must be on or after start date.")
            return cleaned

        if is_half_day:
            if start and end and start != end:
                self.add_error("end_date", "Half-day leave must start and end on the same date.")
            if not half_day_period:
                self.add_error("half_day_period", "Pick AM or PM for a half-day leave.")

        # Overlap check — block apps that collide with another pending/approved
        # leave on the same employee.
        if self.employee and start and end:
            overlap = LeaveApplication.objects.filter(
                employee=self.employee,
                status__in=[
                    LeaveApplication.Status.PENDING,
                    LeaveApplication.Status.APPROVED,
                ],
                start_date__lte=end,
                end_date__gte=start,
            )
            if self.instance.pk:
                overlap = overlap.exclude(pk=self.instance.pk)
            if overlap.exists():
                raise ValidationError(
                    "You already have a pending or approved leave request "
                    "covering one or more of these dates."
                )

        # Build a transient application for the policy validators.
        if self.employee and leave_type and start and end and not self.errors:
            probe = LeaveApplication(
                employee=self.employee,
                leave_type=leave_type,
                start_date=start,
                end_date=end,
                is_half_day=bool(is_half_day),
                half_day_period=half_day_period or "",
                attachment=cleaned.get("attachment"),
                applied_by=self.employee,
            )
            try:
                validate_application_eligibility(probe)
            except ValidationError as e:
                self._merge(e)
            try:
                validate_application_attachment(probe)
            except ValidationError as e:
                self._merge(e)
            try:
                validate_application_against_balance(probe)
            except ValidationError as e:
                self._merge(e)

        return cleaned

    def _merge(self, exc: ValidationError) -> None:
        """Merge a ValidationError raised by a service-layer validator into
        the form's errors, attaching messages to the right fields where the
        exception used a dict, and to non-field otherwise."""
        if hasattr(exc, "error_dict"):
            for field, errs in exc.error_dict.items():
                for err in errs:
                    self.add_error(field if field in self.fields else None, err)
        else:
            for err in exc.error_list:
                self.add_error(None, err)


class CplGrantManualForm(forms.ModelForm, StyledMixin):
    """HR/manager manual entry for CpL earned outside the auto-detected
    Holiday path — today, always a worked scheduled weekly off day or an
    other ad-hoc reason (see leave.models.CplGrant.WorkContext). A
    HOLIDAY-context grant always goes through the AUTO signal or the
    Data Solutions upload instead, since only those pin a real `holiday`
    FK — so it's deliberately not offered here."""

    class Meta:
        model = CplGrant
        fields = ("work_date", "work_context", "note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["work_context"].choices = [
            c for c in CplGrant.WorkContext.choices
            if c[0] != CplGrant.WorkContext.HOLIDAY
        ]
        self.fields["work_context"].initial = CplGrant.WorkContext.WEEKLY_OFF
        self.fields["note"].required = True
        self.fields["note"].widget.attrs["rows"] = 3
        self.fields["note"].help_text = (
            "Why this day earns CpL, e.g. \"Audit prep for labour "
            "department visit — worked scheduled weekly off day.\""
        )
        use_date_picker(self, "work_date")
        self.style_widgets()

    def clean_work_date(self):
        work_date = self.cleaned_data["work_date"]
        if work_date > timezone.localdate():
            raise ValidationError("Can't record a CpL grant for a date in the future.")
        return work_date


class ALHistoryUploadForm(forms.Form):
    """As-of date for the pre-Orbis AL record import (leave/upload_al_history.py)."""

    as_of = forms.DateField(
        label="Figures stated as of",
        help_text="Orbis's own leave ledger takes over the day after this date.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        use_date_picker(self, "as_of", css_class="form-input")

    def clean_as_of(self):
        value = self.cleaned_data["as_of"]
        if value > date.today():
            raise forms.ValidationError("The as-of date can't be in the future.")
        # HR's figures are year-end snapshots ("Till 2024", "2025 AL Taken",
        # "60 Cap Till 2025") and the Service Book rolls them forward one
        # calendar year at a time, so anything but a 31 December is a
        # mistake — on 2026-10-05 the first prod upload went in dated "today"
        # and vanished from the Service Book.
        if (value.month, value.day) != (12, 31):
            raise forms.ValidationError(
                "The as-of date must be a 31 December — the year-end HR's figures are stated at "
                "(e.g. 2025-12-31)."
            )
        return value
