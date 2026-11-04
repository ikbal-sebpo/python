from django import forms

from core.forms import StyledMixin, TakaField, use_date_picker
from employees.widgets import EmployeeSearchWidget
from payroll.models import (
    EmployeeBankAccount, EmployeeTaxDeclaration, OvertimeEntry, OvertimeType, PayComponent,
    PayrollPeriod, SalaryLoan, SalaryLoanAdjustment, SalaryLoanSkip, SalaryStructure,
)


class EmployeeBankAccountForm(forms.ModelForm, StyledMixin):
    """Registered as an employees.SectionSpec — `employee` is excluded, the view injects it."""

    class Meta:
        model = EmployeeBankAccount
        fields = (
            "bank", "branch", "account_type", "account_name",
            "account_number", "routing_number", "is_primary",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()


_AIT_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
_AIT_DOCUMENT_MAX_BYTES = 5 * 1024 * 1024


def _validate_ait_document(uploaded):
    """PDF or image, 5 MB cap. Content-type is browser-supplied so it is a
    hint, not the check — the extension is what we enforce, same as
    policies' PDF validator."""
    if not uploaded:
        return uploaded
    name = (getattr(uploaded, "name", "") or "").lower()
    if not any(name.endswith(ext) for ext in sorted(_AIT_DOCUMENT_EXTENSIONS)):
        raise forms.ValidationError("Upload a PDF, JPG or PNG.")
    if getattr(uploaded, "size", 0) and uploaded.size > _AIT_DOCUMENT_MAX_BYTES:
        raise forms.ValidationError(
            f"File too large ({uploaded.size // (1024 * 1024)} MB). Maximum is 5 MB."
        )
    return uploaded


class _LockedDeclarationMixin:
    """A confirmed (or past-deadline) declaration can't be changed from any
    form — the Django admin is the escape hatch (EmployeeTaxDeclaration)."""

    LOCKED_MESSAGE = (
        "This declaration is locked for the tax year — it has been confirmed and payroll "
        "calculates on it. Ask Payroll if it genuinely needs to change."
    )

    def _refuse_if_locked(self):
        if self.instance.pk and self.instance.is_locked:
            raise forms.ValidationError(self.LOCKED_MESSAGE)


class EmployeeTaxDeclarationForm(_LockedDeclarationMixin, forms.ModelForm, StyledMixin):
    """HR's generic profile-drawer form (registered as an employees.SectionSpec —
    `employee` is excluded, the view injects it). Edits the figures without
    confirming them; the employee's own confirm-and-lock step is
    TaxDeclarationReviewForm. ``allowable_investment`` is shown read-only:
    it's a snapshot the model issues on first save and never recomputes.
    """

    class Meta:
        model = EmployeeTaxDeclaration
        fields = (
            "tax_year", "category", "allowable_investment", "actual_investment",
            "source_tax", "ait_car_advance_tax", "ait_document", "note",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        allowable = self.fields["allowable_investment"]
        allowable.disabled = True
        allowable.required = False
        allowable.help_text = (
            "Issued automatically from the salary in force when this row was created "
            "(30% of projected taxable income). Blank until the employee has a salary structure."
        )
        self.fields["actual_investment"].help_text = (
            "Leave blank to default to the allowable figure. Can't exceed it."
        )
        self.style_widgets()

    def clean_ait_document(self):
        return _validate_ait_document(self.cleaned_data.get("ait_document"))

    def clean(self):
        cleaned = super().clean()
        self._refuse_if_locked()
        return cleaned


class TaxDeclarationReviewForm(_LockedDeclarationMixin, forms.ModelForm, StyledMixin):
    """The employee's own once-a-year confirmation (or Payroll-HR's on their
    behalf): commit to an investment figure, declare any advance tax paid
    with its document, tick the confirmation. Saving locks the row —
    TaxDeclarationReviewView stamps submitted_at/by.
    """

    confirm = forms.BooleanField(
        label="I confirm these figures for the tax year and understand they can't be changed afterwards.",
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
    )

    class Meta:
        model = EmployeeTaxDeclaration
        fields = ("actual_investment", "ait_car_advance_tax", "ait_document")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["actual_investment"].label = "Investment I will make this tax year"
        self.fields["actual_investment"].required = False
        allowable = self.instance.allowable_investment
        if allowable is not None:
            self.fields["actual_investment"].help_text = (
                f"Pre-filled with your allowable investment of Tk {allowable:,.0f}. "
                "Lower it if you will invest less; it can't be higher."
            )
        self.fields["ait_car_advance_tax"].required = False
        self.style_widgets()

    def clean_ait_document(self):
        return _validate_ait_document(self.cleaned_data.get("ait_document"))

    def clean(self):
        cleaned = super().clean()
        self._refuse_if_locked()
        if cleaned.get("actual_investment") is None:
            cleaned["actual_investment"] = self.instance.allowable_investment
        if cleaned.get("ait_car_advance_tax") is None:
            cleaned["ait_car_advance_tax"] = 0
        return cleaned


class PayrollRunEmployeeAdjustForm(forms.Form, StyledMixin):
    """Edits the engine's actual INPUT sources for one employee/period —
    TDSEntry and a new VariablePayEntry — never the computed
    PayrollRunEmployee/Line rows, which are deleted and rebuilt on every
    recalculate. Changes only show up in the register after Recalculate.
    """

    tds_amount = TakaField(label="TDS amount", max_digits=10, min_value=0, required=False)
    tds_note = forms.CharField(label="TDS note", max_length=255, required=False)
    variable_component = forms.ModelChoiceField(
        label="Add bonus / incentive / deduction",
        queryset=PayComponent.objects.filter(entry_mode=PayComponent.EntryMode.INPUT).exclude(code="TDS"),
        required=False,
    )
    variable_amount = TakaField(label="Amount", max_digits=10, min_value=0, required=False)
    variable_reason = forms.CharField(label="Reason", max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("variable_component") and not cleaned.get("variable_amount"):
            self.add_error("variable_amount", "Required when adding a bonus/incentive/deduction.")
        if cleaned.get("variable_amount") and not cleaned.get("variable_component"):
            self.add_error("variable_component", "Pick which component this amount is for.")
        return cleaned


class SalaryRevisionForm(forms.Form, StyledMixin):
    """"Revise salary" drawer on the gross-salary page (2026-10-09): one new
    APPROVED SalaryStructure through services.supersede_salary_structure —
    the same write path as the bulk upload, so a same-date revision cancels
    the old row and a later date closes it the day before. Validation
    mirrors the upload's: not in the future, not before joining, not more
    than a year back, and not earlier than the current structure (older
    history is corrected through the salary-history uploader)."""

    gross_monthly = TakaField(label="Gross monthly salary", max_digits=12, min_value=1)
    effective_from = forms.DateField(label="Effective from")
    reason = forms.ChoiceField(label="Reason", choices=SalaryStructure.Reason.choices)
    note = forms.CharField(label="Note", max_length=255, required=False)

    def __init__(self, *args, employee, current, today, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee, self.current, self.today = employee, current, today
        if current is not None and not self.is_bound:
            self.initial.update({
                "gross_monthly": current.gross_monthly, "effective_from": current.effective_from,
                "reason": SalaryStructure.Reason.CORRECTION,
            })
        elif not self.is_bound:
            self.initial["reason"] = SalaryStructure.Reason.NEW_HIRE
        use_date_picker(self, "effective_from")
        self.style_widgets()

    def clean_effective_from(self):
        from datetime import timedelta

        d = self.cleaned_data["effective_from"]
        if d > self.today:
            raise forms.ValidationError("Effective from cannot be in the future.")
        if self.current is not None and d < self.current.effective_from:
            raise forms.ValidationError(
                f"Effective from is before the current structure's {self.current.effective_from:%Y-%m-%d} — "
                f"only the current structure can be revised here; older history goes through the salary-history uploader."
            )
        if d < self.today - timedelta(days=366):
            raise forms.ValidationError("Effective from is more than a year back — use the salary-history uploader for older history.")
        if self.employee.joining_date and d < self.employee.joining_date:
            raise forms.ValidationError(f"Effective from is before the joining date ({self.employee.joining_date:%Y-%m-%d}).")
        if self.employee.last_working_date and d > self.employee.last_working_date:
            raise forms.ValidationError(f"Effective from is after the last working date ({self.employee.last_working_date:%Y-%m-%d}).")
        return d

    def clean(self):
        data = super().clean()
        if (
            self.current is not None and data.get("gross_monthly") is not None and data.get("effective_from")
            and data["gross_monthly"] == self.current.gross_monthly
            and data["effective_from"] == self.current.effective_from
        ):
            raise forms.ValidationError("Nothing changed — same figure, same effective date.")
        return data

    @property
    def is_same_date_revision(self) -> bool:
        return (
            self.current is not None and self.is_valid()
            and self.cleaned_data["effective_from"] == self.current.effective_from
        )


class SalaryUploadMetaForm(forms.Form, StyledMixin):
    """Batch-level fields for the gross-salary uploader.

    Effective date and reason are per-row in the file itself (see
    payroll/upload.py::parse_salary_upload) — a single January sweep can
    carry a mix of ANNUAL_INCREMENT rows and a few PROMOTION/CORRECTION
    rows in the same file, and the payroll engine picks a SalaryStructure
    by effective_from, so a batch-wide date would misdate anyone whose
    current salary took effect earlier. Only note is genuinely batch-level.
    """

    note = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()


class PayrollRunCreateForm(forms.Form, StyledMixin):
    """From/to for a new run's period. Defaults (set by the view) are the
    current standard 16th->15th cycle; editing either date creates/reuses a
    CUSTOM PayrollPeriod instead — see ``PayrollPeriod.for_range``."""

    start_date = forms.DateField(label="From")
    end_date = forms.DateField(label="To")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        use_date_picker(self, "start_date", "end_date")
        self.style_widgets()

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end <= start:
            self.add_error("end_date", "Must be after the from date.")
        return cleaned


class VariablePayEntryEditForm(forms.Form, StyledMixin):
    """Amount/reason for an existing VariablePayEntry — the component and
    employee/period don't change; delete + re-add for that instead."""

    amount = TakaField(max_digits=10, min_value=0)
    reason = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()


class OvertimeEntryForm(forms.ModelForm, StyledMixin):
    """Manual add/edit for one OvertimeEntry — the drawer behind
    /payroll/overtime/'s "Add entry" and per-row pencil.

    Enforces exactly the same rules as the bulk uploader, by calling the same
    code rather than restating it: payroll.services.OvertimeWindow for the
    date window/lock gate, payroll.upload.parse_ot_seconds for HH:MM:SS, and
    payroll.upload.employed_on for the employment-window sanity check. A rule
    the uploader enforced and this form didn't would be a hole, not a bug.

    `seconds` and `period` aren't form fields: hours are entered as HH:MM:SS
    (see ot_hours) and the period is always derived from work_date, never
    chosen — same posture as parse_upload, where a row's stored period is
    descriptive metadata rather than the source of truth.
    """

    ot_hours = forms.CharField(
        label="OT hours", max_length=12,
        help_text="HH:MM:SS, e.g. 02:30:00.",
    )

    class Meta:
        model = OvertimeEntry
        # No `status` field since the review chain (2026-10-17): a new entry
        # always starts PENDING, an edit that changes the figures sends it
        # back there (OvertimeEntryEditView), and Void/Restore/Review/
        # Approve are explicit recorded actions on the list — never a
        # dropdown anyone in scope could set to Approved.
        fields = ("employee", "work_date", "overtime_type", "worked_in", "note")

    def __init__(self, *args, viewer=None, window=None, employee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.viewer = viewer
        self.window = window

        if employee_queryset is not None:
            self.fields["employee"].queryset = employee_queryset
        self.fields["overtime_type"].queryset = OvertimeType.objects.filter(is_active=True)
        self.fields["worked_in"].required = False
        self.fields["worked_in"].help_text = "Which team this was worked for — blank defaults to the employee's own team."

        if self.instance.pk:
            # The employee is half the unique key and the whole audit trail —
            # moving an entry between people would silently rewrite history.
            # Delete and re-add instead (same call as VariablePayEntryEditForm).
            self.fields["employee"].disabled = True
            self.fields["ot_hours"].initial = _hhmmss(self.instance.seconds)
        else:
            # A plain <select> of every employee in scope is unusable for HR
            # and ambiguous for same-named people — same reasoning (and same
            # widget) as offboarding.forms' create-resignation picker.
            self.fields["employee"].widget = EmployeeSearchWidget(
                search_url_name="payroll_overtime_employee_search",
            )

        use_date_picker(self, "work_date")
        self.style_widgets()

    def clean_ot_hours(self):
        from payroll.upload import parse_ot_seconds

        try:
            return parse_ot_seconds(self.cleaned_data["ot_hours"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc))

    def clean(self):
        from payroll.models import PayrollPeriod
        from payroll.periods import period_for_date
        from payroll.upload import employed_on

        cleaned = super().clean()
        employee = cleaned.get("employee")
        work_date = cleaned.get("work_date")
        overtime_type = cleaned.get("overtime_type")

        if work_date and self.window is not None:
            decision = self.window.resolve(work_date)
            if not decision.allowed:
                self.add_error("work_date", decision.reason)
                work_date = None

        if employee and work_date and not employed_on(employee, work_date):
            self.add_error("work_date", (
                f"{employee.display_name} wasn't employed on {work_date.isoformat()}."
            ))
            work_date = None

        if employee and work_date and overtime_type:
            clash = OvertimeEntry.objects.filter(
                employee=employee, work_date=work_date, overtime_type=overtime_type,
            )
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error("overtime_type", (
                    "This employee already has an entry of this overtime type on "
                    f"{work_date.isoformat()} — edit that one instead."
                ))
            else:
                # Both are excluded from the form, so _post_clean's full_clean()
                # would otherwise validate the model against a stale/empty
                # period and trip OvertimeEntry.clean()'s range check the
                # moment a date is edited across the 15th/16th boundary.
                self.instance.period = PayrollPeriod.for_month(*period_for_date(work_date))
                self.instance.seconds = cleaned.get("ot_hours") or 0

        # Blank worked_in snapshots the employee's current team at write
        # time — same rule as the bulk uploader (payroll/upload.py) — so an
        # entry's recorded team doesn't retroactively shift if the employee
        # is later moved to a different one. Only applied on create: an
        # existing entry's employee field is disabled (can't change), so a
        # blank worked_in on edit means "clear it", not "re-default it".
        # Both cleaned_data and self.instance are set — _post_clean's
        # construct_instance() rebuilds the instance from cleaned_data, so
        # setting the instance alone would be silently discarded.
        if not self.instance.pk and not cleaned.get("worked_in") and employee:
            cleaned["worked_in"] = employee.team
            self.instance.worked_in = employee.team
        return cleaned


def _hhmmss(total_seconds) -> str:
    """Seconds -> HH:MM:SS, for the edit form's initial value. Same rendering
    as payroll.templatetags.payroll_extras.hhmmss, which templates use."""
    try:
        total_seconds = int(total_seconds)
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Salary loans (docs/SALARY_LOAN_PLAN.md, 2026-10-08)
# ---------------------------------------------------------------------------

def _month_choices(center=None, before: int = 6, after: int = 6):
    """("YYYY-MM", "October 2026 payroll (Sep 16 – Oct 15)") around today —
    a payroll period is named after the month it closes in."""
    from datetime import date as _date

    from payroll.periods import period_bounds, period_for_date

    center = center or _date.today()
    year, month = period_for_date(center)
    choices = []
    y, m = year, month
    for _ in range(before):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for _ in range(before + after + 1):
        start, end = period_bounds(y, m)
        choices.append((f"{y:04d}-{m:02d}", f"{_date(y, m, 1):%B %Y} payroll ({start:%b %d} – {end:%b %d})"))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return choices


def _period_from_month(value: str):
    year, month = (int(part) for part in value.split("-"))
    return PayrollPeriod.for_month(year, month)


class SalaryLoanForm(forms.ModelForm, StyledMixin):
    """Record / edit one management-approved loan (the HR file note).

    ``terms_locked`` — set by the view once an instalment has been deducted
    in a calculated run: principal, first deduction period and the cutover
    opening then go read-only, because changing them would reprice history.
    Instalment amount, document and note stay editable (Finance restructures
    forward). ``opening_recovered`` is shown to Payroll Admins only.
    """

    first_deduction_month = forms.ChoiceField(
        label="First deduction", required=False,
        help_text="Leave blank to default to the payroll after the disbursement month (disbursed in March → Mar 16 – Apr 15).",
    )

    class Meta:
        model = SalaryLoan
        fields = (
            "employee", "file_reference", "approved_on", "approval_document",
            "principal", "installment_count", "installment_amount", "opening_recovered", "note",
        )
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, viewer=None, can_set_opening=False, terms_locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.viewer = viewer
        self.terms_locked = terms_locked
        self.fields["first_deduction_month"].choices = [("", "— default from disbursement date —")] + _month_choices()
        if self.instance.pk and self.instance.first_deduction_period_id:
            fp = self.instance.first_deduction_period
            key = f"{fp.year:04d}-{fp.month:02d}"
            if key not in dict(self.fields["first_deduction_month"].choices):
                self.fields["first_deduction_month"].choices.append((key, str(fp)))
            self.fields["first_deduction_month"].initial = key
        self.fields["installment_amount"].required = False
        self.fields["installment_amount"].help_text = "Blank = principal ÷ instalments, rounded up to the taka."
        self.fields["approval_document"].required = (
            self.instance.recorded_source != SalaryLoan.RecordedSource.OUTSIDE_ORBIS
            and not (self.instance.pk and self.instance.approval_document)
        )
        if self.instance.pk:
            self.fields["employee"].disabled = True
        else:
            self.fields["employee"].widget = EmployeeSearchWidget(search_url_name="payroll_loan_employee_search")
        if not can_set_opening:
            self.fields.pop("opening_recovered")
        if terms_locked:
            for name in ("principal", "first_deduction_month", "opening_recovered"):
                if name in self.fields:
                    self.fields[name].disabled = True
                    self.fields[name].help_text = "Locked — an instalment has already been deducted on these terms."
        use_date_picker(self, "approved_on")
        self.style_widgets()

    def clean_approval_document(self):
        return _validate_ait_document(self.cleaned_data.get("approval_document"))

    def clean(self):
        cleaned = super().clean()
        principal = cleaned.get("principal")
        count = cleaned.get("installment_count")
        if principal is not None and count and not cleaned.get("installment_amount"):
            cleaned["installment_amount"] = SalaryLoan.default_installment(principal, count)
            self.instance.installment_amount = cleaned["installment_amount"]
        if not self.terms_locked:
            month = cleaned.get("first_deduction_month")
            self.instance.first_deduction_period = _period_from_month(month) if month else None
        return cleaned


class SalaryLoanDisburseForm(forms.Form, StyledMixin):
    disbursed_on = forms.DateField(label="Disbursed on")
    note = forms.CharField(label="Note", max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        use_date_picker(self, "disbursed_on")
        self.style_widgets()


class SalaryLoanCancelForm(forms.Form, StyledMixin):
    reason = forms.CharField(label="Reason", max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()


class SalaryLoanAdjustmentForm(forms.ModelForm, StyledMixin):
    """Repaid outside payroll, waived, or a correction. A positive delta
    reduces the balance and may not exceed it; a CORRECTION may be negative."""

    class Meta:
        model = SalaryLoanAdjustment
        fields = ("occurred_on", "reason", "delta", "note")

    def __init__(self, *args, outstanding=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.outstanding = outstanding
        self.fields["delta"].label = "Amount"
        self.fields["delta"].help_text = "Positive reduces what is owed. Only a correction may be negative."
        use_date_picker(self, "occurred_on")
        self.style_widgets()

    def clean(self):
        cleaned = super().clean()
        delta = cleaned.get("delta")
        reason = cleaned.get("reason")
        if delta is None:
            return cleaned
        if delta == 0:
            self.add_error("delta", "Enter a non-zero amount.")
        elif delta < 0 and reason != SalaryLoanAdjustment.Reason.CORRECTION:
            self.add_error("delta", "Only a correction can increase the balance.")
        elif delta > 0 and self.outstanding is not None and delta > self.outstanding:
            self.add_error("delta", f"Only {self.outstanding:,.0f} is outstanding.")
        return cleaned


class SalaryLoanSkipForm(forms.ModelForm, StyledMixin):
    """Skip one period's instalment on HR's approval (Q68)."""

    class Meta:
        model = SalaryLoanSkip
        fields = ("period", "approved_by_hr", "reason")

    def __init__(self, *args, loan=None, period_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.loan = loan
        if period_queryset is not None:
            self.fields["period"].queryset = period_queryset
        self.fields["period"].label = "Payroll to skip"
        self.fields["period"].help_text = "Only payrolls not yet calculated can be skipped."
        from accounts.models import CustomUser
        self.fields["approved_by_hr"].queryset = CustomUser.objects.filter(
            is_active=True, groups__name="HR",
        ).distinct().order_by("first_name", "last_name")
        self.fields["approved_by_hr"].label = "Approved by (HR)"
        self.style_widgets()

    def clean_period(self):
        period = self.cleaned_data["period"]
        if self.loan and SalaryLoanSkip.objects.filter(loan=self.loan, period=period).exists():
            raise forms.ValidationError("That payroll is already skipped for this loan.")
        return period
