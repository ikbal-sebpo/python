from django import forms

from attendance.models import AttendanceCorrection
from core.forms import StyledMixin, use_date_picker


class AttendanceCorrectionForm(forms.ModelForm, StyledMixin):
    """employee/date/punch/original_* are set by the view, not the form —
    they come from the row the correction was requested against, not from
    user input."""

    class Meta:
        model = AttendanceCorrection
        fields = (
            "requested_check_in", "requested_check_out",
            "requested_time_in_office", "reason",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        use_date_picker(self, "requested_check_in", "requested_check_out")
        self.fields["requested_time_in_office"].widget.attrs["placeholder"] = "HH:MM:SS"
        self.fields["reason"].widget = forms.Textarea(attrs={"rows": 3})
        self.fields["reason"].help_text = "Why the current punch is wrong."
        self.style_widgets()

    def clean(self):
        cleaned = super().clean()
        check_in = cleaned.get("requested_check_in")
        check_out = cleaned.get("requested_check_out")
        time_in_office = cleaned.get("requested_time_in_office")
        if check_in and check_out:
            if check_out <= check_in:
                raise forms.ValidationError({"requested_check_out": "Check-out must be after check-in."})
            if time_in_office is not None and time_in_office > (check_out - check_in):
                raise forms.ValidationError({
                    "requested_time_in_office":
                        "Time in office can't be longer than the span between check-in and check-out.",
                })
        return cleaned
