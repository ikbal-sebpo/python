from django import forms
from django.core.exceptions import ValidationError

from accounts.locations import LOCATION_DROPDOWN_CHOICES
from core.forms import StyledMixin, use_date_picker
from roster import services
from roster.models import RosterEmailConfig, RosterEmailMember, ShiftChangeRequest, ShiftType


class ShiftTypeForm(forms.ModelForm, StyledMixin):
    class Meta:
        model = ShiftType
        fields = (
            "name", "description",
            "category", "shift_timezone",
            "shift_start_time", "shift_end_time",
            "color", "sort_order", "has_attribute", "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for time_field in ("shift_start_time", "shift_end_time"):
            self.fields[time_field].widget = forms.TimeInput(
                attrs={"type": "time", "placeholder": "HH:MM"},
                format="%H:%M",
            )
        self.style_widgets()


class RosterPatternRowForm(forms.Form, StyledMixin):
    """One row in the seven-weekday pattern formset.

    `weekday` is hidden. `shift_ref` encodes the selected option as either
    `type:<pk>` (shift type without variants) or `attr:<pk>` (specific
    attribute). Empty = clear the row.
    """

    weekday = forms.IntegerField(widget=forms.HiddenInput())
    shift_ref = forms.CharField(required=False, widget=forms.Select(), label="Shift")
    location = forms.ChoiceField(
        choices=[("", "— default —"), *LOCATION_DROPDOWN_CHOICES],
        required=False,
        label="Location",
    )

    def __init__(self, *args, employee=None, **kwargs):
        # A row's current value must always remain selectable even once it's
        # out of scope for new picks — otherwise an untouched row round-trips
        # as blank on save and silently deletes itself. See docs/PLAN.md,
        # "Roster + leave — unified cell model".
        current_ref = (kwargs.get("initial") or {}).get("shift_ref", "")
        include_attr_ids = set()
        if current_ref.startswith("attr:"):
            try:
                include_attr_ids.add(int(current_ref.split(":", 1)[1]))
            except ValueError:
                pass

        super().__init__(*args, **kwargs)
        # Widget choices are built dynamically so optgroups + attribute
        # variants appear together in one dropdown (shared builder with the
        # grid cell and the shift-change request form).
        choices = services.shift_ref_choices(
            employee.department_id if employee else None, include_attr_ids,
            attr_times=True,
        )
        self.fields["shift_ref"].widget.choices = choices
        self.style_widgets()


class RosterEmailConfigForm(forms.ModelForm, StyledMixin):
    """Drawer form on /roster/ — recipients, POC, wording, plus per-person
    fields: `role_<pk hex>` (blank = designation) for everyone listed, and
    `include_<pk hex>` for the people whose inclusion is a choice — trainees
    (off by default) and people from outside the team. `add_person` takes
    an email to always-include someone from another team."""

    class Meta:
        model = RosterEmailConfig
        fields = (
            "to_emails", "cc_emails", "poc", "sender", "intro_text", "signature",
            "send_team_copy", "is_active",
        )
        widgets = {
            "to_emails": forms.Textarea(attrs={"rows": 2}),
            "cc_emails": forms.Textarea(attrs={"rows": 2}),
            "intro_text": forms.Textarea(attrs={"rows": 5}),
            "signature": forms.Textarea(attrs={"rows": 4}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
            "send_team_copy": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
        }

    add_person = forms.ModelChoiceField(
        queryset=None, required=False, label="Add someone from another team",
        help_text="They're listed with their own roster, e.g. a lead who covers this team.",
    )

    def __init__(self, *args, team, members, excluded, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import CustomUser

        from employees.widgets import EmployeeSearchWidget

        self.team = team
        self.members = members
        self.excluded = excluded
        self.fields["add_person"].queryset = CustomUser.objects.filter(is_active=True)
        self.fields["add_person"].widget = EmployeeSearchWidget(
            search_url_name="roster_email_people_search",
        )
        # POC picker: people plausibly fronting the team — its members, their
        # line managers, and the department/team managers. Not the whole org.
        dept = team.department
        pks = {m.pk for m in members}
        pks |= set(
            CustomUser.objects.filter(team=team, is_active=True)
            .exclude(job_info__reports_to=None)
            .values_list("job_info__reports_to", flat=True)
        )
        pks |= set(team.additional_managers.values_list("pk", flat=True))
        if dept is not None:
            if dept.head_of_department_id:
                pks.add(dept.head_of_department_id)
            pks |= set(dept.additional_managers.values_list("pk", flat=True))
        for fk in ("poc_id", "sender_id"):
            if getattr(self.instance, fk):
                pks.add(getattr(self.instance, fk))
        people = CustomUser.objects.filter(pk__in=pks).order_by("first_name", "last_name")
        self.fields["poc"].queryset = people
        self.fields["sender"].queryset = people
        self.fields["sender"].empty_label = "System (no personal name)"
        head = dept.head_of_department if dept else None
        self.fields["poc"].empty_label = (
            f"Department head ({head.display_name})" if head else "Department head"
        )

        from employees.models import JobInfo

        existing = {m.employee_id: m for m in self.instance.members.all()} if self.instance.pk else {}
        trainee_pks = set(
            JobInfo.objects.filter(
                employee__in=list(members) + list(excluded),
                resource_status=JobInfo.ResourceStatus.TRAINEE,
            ).values_list("employee_id", flat=True)
        )
        self.people = []  # (employee, role BoundField, include BoundField-or-None, note)
        for emp in list(members) + list(excluded):
            row = existing.get(emp.pk)
            key = emp.pk.hex
            self.fields[f"role_{key}"] = forms.CharField(
                label=emp.display_name, required=False, max_length=100,
                initial=row.role_label if row else "",
                widget=forms.TextInput(attrs={
                    "placeholder": emp.designation.name if emp.designation_id else "Role",
                }),
            )
            outsider = emp.team_id != team.pk
            is_trainee = emp.pk in trainee_pks and not outsider
            if outsider or is_trainee:
                self.fields[f"include_{key}"] = forms.BooleanField(
                    required=False, label="Include",
                    initial=bool(row and row.always_include),
                    widget=forms.CheckboxInput(attrs={"class": "form-checkbox"}),
                )
            note = (
                f"From {emp.team.name if emp.team_id else 'no team'}" if outsider
                else "Trainee" if is_trainee else ""
            )
            self.people.append((emp, f"role_{key}", f"include_{key}" if (outsider or is_trainee) else None, note))
        self.style_widgets()

    @property
    def person_rows(self):
        return [
            {"employee": e, "role": self[r], "include": self[i] if i else None, "note": n}
            for e, r, i, n in self.people
        ]

    def _clean_emails(self, field):
        from django.core.validators import validate_email

        raw = self.cleaned_data.get(field, "")
        emails = RosterEmailConfig.split_emails(raw)
        bad = []
        for e in emails:
            try:
                validate_email(e)
            except forms.ValidationError:
                bad.append(e)
        if bad:
            raise forms.ValidationError(f"Not a valid email: {', '.join(bad)}")
        return ", ".join(emails)

    def clean_to_emails(self):
        return self._clean_emails("to_emails")

    def clean_cc_emails(self):
        return self._clean_emails("cc_emails")

    def save(self, commit=True):
        config = super().save(commit=commit)
        if not commit:
            return config
        existing = {m.employee_id: m for m in config.members.all()}
        for emp, role_name, include_name, _ in self.people:
            role = (self.cleaned_data.get(role_name) or "").strip()
            include = bool(include_name and self.cleaned_data.get(include_name))
            row = existing.get(emp.pk)
            if role or include:
                if row is None:
                    RosterEmailMember.objects.create(
                        config=config, employee=emp, role_label=role, always_include=include,
                    )
                elif (row.role_label, row.always_include) != (role, include):
                    row.role_label, row.always_include = role, include
                    row.save(update_fields=["role_label", "always_include"])
            elif row is not None:
                row.delete()
        person = self.cleaned_data.get("add_person")
        if person is not None:
            RosterEmailMember.objects.update_or_create(
                config=config, employee=person, defaults={"always_include": True},
            )
        return config


class ShiftChangeRequestForm(forms.ModelForm, StyledMixin):
    """`/roster/change-requests/apply/` — one date, the shift to move to and
    the location to work from, plus a reason. Same `employee=` kwarg shape
    as `leave.forms.LeaveApplicationForm`.

    `shift_ref` is the grid's `type:<pk>` / `attr:<pk>` encoding, with the
    choices scoped to the employee's department exactly as the roster cell
    is (`services.shift_ref_choices`), and limited to the categories an
    employee may request (`services.REQUESTABLE_SHIFT_CATEGORIES`). The
    posted value is re-resolved server-side through the same department
    filter, so a tampered pk from another department is rejected.
    """

    shift_ref = forms.ChoiceField(label="Shift", widget=forms.Select())
    # Blank placeholder first: the apply page pre-selects the current roster
    # location via JS, and without a blank the browser would default to the
    # first real option (MHK) — an accidental office request if that JS
    # ever doesn't run. `required` rejects the blank on submit.
    requested_location = forms.ChoiceField(
        label="Location", choices=[("", "— select —"), *LOCATION_DROPDOWN_CHOICES],
        widget=forms.Select(),
    )

    class Meta:
        model = ShiftChangeRequest
        fields = ("date", "requested_location", "reason")

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.fields["shift_ref"].choices = services.shift_ref_choices(
            employee.department_id if employee is not None else None,
            categories=services.REQUESTABLE_SHIFT_CATEGORIES,
            attr_times=True,
        )
        use_date_picker(self, "date")
        self.fields["reason"].widget.attrs["rows"] = 3
        self.fields["reason"].widget.attrs["placeholder"] = (
            "Why the change is needed — shown to your supervisor."
        )
        # The shift/location dropdowns are pre-selected to the current roster
        # values by the apply page once a date is picked (see
        # ShiftChangeCurrentView), so the user only touches what changes.
        self.style_widgets()

    def clean(self):
        cleaned = super().clean()
        day = cleaned.get("date")
        ref = cleaned.get("shift_ref")
        if self.employee is None or not day or not ref or self.errors:
            return cleaned

        try:
            st, attr = services.resolve_shift_ref(
                ref, department_id=self.employee.department_id,
            )
        except services.ShiftRefError as exc:
            self.add_error("shift_ref", exc)
            return cleaned

        current = services.resolve_current_cell(self.employee, day)
        inst = self.instance
        inst.employee = self.employee
        inst.date = day
        inst.original_shift_type = current.shift_type
        inst.original_shift_attribute = current.shift_attribute
        inst.original_was_concrete = current.is_concrete
        inst.original_location = current.location
        inst.requested_shift_type = st
        inst.requested_shift_attribute = attr
        inst.requested_location = cleaned.get("requested_location") or ""

        try:
            services.validate_shift_change(inst, actor=self.actor, stage="file")
        except ValidationError as exc:
            for field, errs in exc.error_dict.items():
                for err in errs:
                    self.add_error(field if field in self.fields else None, err)
        return cleaned

    # Set by the view before is_valid(): who is filing (matters for the
    # past-date rule — HR may backdate, an employee may not).
    actor = None
