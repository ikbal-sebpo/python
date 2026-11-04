from dateutil.relativedelta import relativedelta

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    UserCreationForm,
)

from accounts.models import CustomUser
from core.forms import StyledMixin as _StyledMixin, use_date_picker

_FLATPICKR_DATE_FIELDS = ("joining_date", "confirmation_date", "last_working_date")


def _apply_date_widgets(form):
    """Swap the given date fields (when present) to the flatpickr widget —
    same control on Create and Edit rather than a bare browser date picker on
    one and flatpickr on the other."""
    use_date_picker(form, *_FLATPICKR_DATE_FIELDS)


class EmailLoginForm(AuthenticationForm, _StyledMixin):
    """Login by email + password. Overrides the username field label/placeholder."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email",
                                        "placeholder": "you@sebpo.net"}),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password",
                                           "placeholder": "••••••••"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()


class _OrgCascadeMixin:
    """Keep department -> team -> subteam agreeing on the user create/edit form.

    The dropdowns cascade in the browser (static/js/dept-team-cascade.js), but
    that is UX only. These two hooks are what actually hold the line: the
    querysets are narrowed so the *initial* render already offers just the
    selected department's teams (a stale page otherwise lists every team in
    the company), and clean() rejects a mismatched pair that arrives anyway —
    a stale tab, a hand-rolled POST, or a browser with JS off.

    The employee's currently-saved values are always kept selectable, and a
    pair that was already mismatched on file is only reported when this edit
    actually touches it — editing someone's phone number must not be blocked
    by legacy org data nobody is changing.
    """

    def _selected_pk(self, name):
        """The pk currently chosen for ``name``: POSTed value, else the instance's."""
        if self.is_bound:
            raw = self.data.get(self.add_prefix(name))
            return int(raw) if str(raw).isdigit() else None
        value = self.initial.get(name) or getattr(self.instance, f"{name}_id", None)
        return int(value) if value else None

    def _narrow(self, name, parent_field, parent_pk):
        from django.db.models import Q

        field = self.fields.get(name)
        if field is None:
            return
        cond = Q(**{parent_field: parent_pk}) if parent_pk else Q(pk__in=())
        current = self._selected_pk(name)
        if current:
            cond |= Q(pk=current)
        field.queryset = field.queryset.filter(cond)

    def narrow_org_choices(self):
        """Clamp team to the picked department and subteam to the picked team."""
        self._narrow("team", "department_id", self._selected_pk("department"))
        self._narrow("subteam", "team_id", self._selected_pk("team"))

    def clean(self):
        cleaned = super().clean()
        pairs = (
            ("team", "department", "department_id", "department"),
            ("subteam", "team", "team_id", "team"),
        )
        for child_name, parent_name, fk_attr, parent_label in pairs:
            if child_name not in self.fields:
                continue
            if not ({child_name, parent_name} & set(self.changed_data)):
                continue
            child = cleaned.get(child_name)
            if not child:
                continue
            parent = cleaned.get(parent_name)
            if parent is None:
                self.add_error(child_name, f"Select a {parent_label} first.")
            elif getattr(child, fk_attr) != parent.pk:
                self.add_error(
                    child_name,
                    f"{child} doesn't belong to the selected {parent_label}.",
                )
        return cleaned


class CustomUserCreationForm(_OrgCascadeMixin, UserCreationForm, _StyledMixin):
    """last_working_date is deliberately absent — that field belongs to the
    offboarding flow (see offboarding/signals.py's write-through from
    Resignation.last_working_date on acceptance) and has no place at hire
    time. confirmation_date is also absent as an input: it's derived from
    Joining Date + Probation Month in save() below rather than typed by
    hand, since HR always wants it computed the same way. Username is
    likewise absent — this app authenticates by email (USERNAME_FIELD),
    Username is only still on the model because AbstractUser requires it,
    so it's silently set equal to the email in save() rather than asking
    HR to type the same value twice. nid_number is a plain (non-model)
    field — it isn't persisted on CustomUser, the view writes it to
    employees.IdentificationNumber(id_type=NID) instead, matching how
    payroll treats IdentificationNumber as the source of truth for
    government IDs rather than duplicating them."""

    _REQUIRED_EXEMPT_FIELDS = {"team", "is_active", "password1", "password2"}

    nid_number = forms.CharField(label="NID Number", max_length=100)

    class Meta:
        model = CustomUser
        fields = ("email", "first_name", "last_name",
                  "employee_id", "nid_number", "department", "designation", "grade",
                  "team", "job_location", "work_location",
                  "contact_number", "joining_date", "probation_month", "notice_period_months",
                  "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_date_widgets(self)
        self.style_widgets()
        if "probation_month" in self.fields:
            self.fields["probation_month"].help_text = (
                "Confirmation date is calculated automatically (Joining Date + Probation Month)."
            )
        for name, field in self.fields.items():
            if name not in self._REQUIRED_EXEMPT_FIELDS:
                field.required = True
        self.narrow_org_choices()

    def clean_nid_number(self):
        return self.cleaned_data["nid_number"].strip()

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        # username isn't a form field (see class docstring), so ModelForm's
        # usual unique-field check never runs for it — check by hand so a
        # collision with some existing user's username surfaces as a normal
        # form error instead of an IntegrityError out of save().
        if email and CustomUser.objects.filter(username=email).exists():
            self.add_error("email", "This email is already taken as another user's username.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = user.email
        if user.joining_date and user.probation_month:
            user.confirmation_date = user.joining_date + relativedelta(months=user.probation_month)
        if commit:
            user.save()
        return user


class CustomUserUpdateForm(_OrgCascadeMixin, forms.ModelForm, _StyledMixin):
    MANAGER_EXCLUDED_FIELDS = {"access_level", "is_active"}

    class Meta:
        model = CustomUser
        fields = ("email", "username", "first_name", "last_name",
                  "employee_id", "department", "designation", "grade", "access_level",
                  "team", "subteam", "job_location", "work_location",
                  "contact_number", "joining_date", "probation_month",
                  "confirmation_date", "last_working_date",
                  "is_active", "groups")

    def __init__(self, *args, limited_scope=False, editor=None,
                 editor_request=None, can_manage_groups=False, **kwargs):
        super().__init__(*args, **kwargs)
        if limited_scope:
            for field_name in self.MANAGER_EXCLUDED_FIELDS:
                self.fields.pop(field_name, None)
            self._limit_org_choices(editor, editor_request)
        if not can_manage_groups:
            self.fields.pop("groups", None)
        elif "groups" in self.fields:
            groups_field = self.fields["groups"]
            groups_field.widget = forms.CheckboxSelectMultiple(attrs={"class": "form-checkbox"})
            groups_field.widget.choices = groups_field.choices
        _apply_date_widgets(self)
        self.style_widgets()
        # After _limit_org_choices so the department cascade composes with the
        # editor's own scope rather than replacing it.
        self.narrow_org_choices()

    def _limit_org_choices(self, editor, request):
        """Clamp the department / team / subteam dropdowns to the editor's own
        department so a supervisor can't (accidentally or otherwise) move an
        employee into another org unit. Scoping the queryset also enforces this
        server-side: a tampered POST with an out-of-scope id fails validation.

        The employee's *current* department / team / subteam are always kept
        selectable — a downline can sit in a different department via the
        reports-to chain, and editing must never silently drop those values.
        """
        if editor is None:
            return
        from django.db.models import Q

        from accounts.models import Department, SubTeam, Team
        from accounts.permissions import visible_departments, visible_teams

        inst = getattr(self, "instance", None)

        if "department" in self.fields:
            dept_ids = set(
                visible_departments(editor, request=request).values_list("pk", flat=True)
            )
            if inst and inst.department_id:
                dept_ids.add(inst.department_id)
            self.fields["department"].queryset = (
                Department.objects.filter(pk__in=dept_ids).order_by("name")
            )

        team_ids = set(
            visible_teams(editor, request=request).values_list("pk", flat=True)
        )
        if inst and inst.team_id:
            team_ids.add(inst.team_id)
        if "team" in self.fields:
            self.fields["team"].queryset = (
                Team.objects.filter(pk__in=team_ids).order_by("name")
            )
        if "subteam" in self.fields:
            subteam_q = Q(team_id__in=team_ids)
            if inst and inst.subteam_id:
                subteam_q |= Q(pk=inst.subteam_id)
            self.fields["subteam"].queryset = (
                SubTeam.objects.filter(subteam_q).order_by("name")
            )
