"""Forms for the policies app.

All ModelForms use ``core.forms.StyledMixin`` so every widget (incl. the
``<select>`` / multiselects) gets the theme's ``.form-input`` class — see
CLAUDE.md "Working conventions".
"""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from accounts.permissions import visible_departments
from core.forms import StyledMixin
from policies.models import Policy, PolicyVersion
from policies.services import eligible_custom_adherents

User = get_user_model()


def pdf_max_bytes() -> int:
    """Cap on uploaded policy PDFs — the media store's own upload limit.

    Read at call time so ``MEDIA_MAX_UPLOAD_BYTES`` (50 MiB default) is the one
    knob; the old separate 20 MB policy cap only turned away PDFs the storage
    would have accepted.
    """
    return int(settings.MEDIA_MAX_UPLOAD_BYTES)

_PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}


def _validate_pdf(uploaded):
    """Shared PDF validation: extension, content-type, magic header, size."""
    if not uploaded:
        return uploaded
    name = (uploaded.name or "").lower()
    if not name.endswith(".pdf"):
        raise forms.ValidationError("Upload a PDF file (.pdf).")
    content_type = getattr(uploaded, "content_type", "") or ""
    if content_type and content_type not in _PDF_CONTENT_TYPES:
        raise forms.ValidationError("That doesn't look like a PDF.")
    max_bytes = pdf_max_bytes()
    if uploaded.size and uploaded.size > max_bytes:
        raise forms.ValidationError(
            f"PDF too large ({uploaded.size // (1024 * 1024)} MB). "
            f"Max {max_bytes // (1024 * 1024)} MB."
        )
    # Cheap magic-byte sniff — PDFs start with "%PDF-".
    try:
        head = uploaded.read(5)
        uploaded.seek(0)
    except Exception:  # pragma: no cover — defensive
        head = b""
    if head and not head.startswith(b"%PDF"):
        raise forms.ValidationError("That doesn't look like a PDF.")
    return uploaded


class PolicyForm(StyledMixin, forms.ModelForm):
    """Create / edit a policy's metadata + adherent targeting.

    ``department`` defaults to the caller's own department on a new policy;
    non-privileged managers are locked to it (see ``accounts.permissions.
    visible_departments`` — same rule as every other department dropdown in
    Orbis). ``owner`` is scoped to active employees of whichever department is
    currently selected, and re-scoped client-side via htmx when the manager
    changes the department (see ``PolicyOwnerOptionsView``).

    ``target_groups`` defaults to the 'All Employees' group on new policies.
    ``custom_adherents`` is filtered to active employees not already
    adherents/signers of this policy (surfaces only people missing coverage).
    """

    class Meta:
        model = Policy
        fields = (
            "title", "category", "description", "department", "owner",
            "target_groups", "custom_adherents", "due_days",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            # A checkbox list, not a native <select multiple>: a plain click
            # on a multi-select can only *replace* the selection, never empty
            # it (deselecting needs Ctrl+click), so managers who thought they
            # had cleared the "All Employees" default were still circulating
            # to everyone (2026-10-28). Same widget the user form uses for
            # groups.
            "target_groups": forms.CheckboxSelectMultiple(attrs={"class": "form-checkbox"}),
            "custom_adherents": forms.SelectMultiple(attrs={"size": 8}),
        }

    def __init__(self, *args, user=None, request=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["department"].queryset = visible_departments(user, request)
        self.fields["department"].widget.attrs.update({
            "hx-get": reverse("policies:owners_by_department"),
            "hx-target": "#id_owner",
            "hx-trigger": "change",
            "hx-swap": "innerHTML",
            "hx-include": "this",
        })

        # Default a brand-new policy to the creator's own department. NOTE:
        # can't use "not self.instance.pk" to detect new-vs-existing here —
        # Policy.id is a UUIDField(default=uuid.uuid4), so an unsaved
        # instance already has a truthy pk the moment it's constructed.
        # ``_state.adding`` is the correct "not yet saved" check.
        is_new = self.instance._state.adding
        if is_new and not self.data and getattr(user, "department_id", None):
            self.initial.setdefault("department", user.department_id)

        # Scope 'owner' to whichever department is currently selected:
        # submitted value (bound form), the instance's (edit), or the
        # just-applied default (new policy) — every active employee if none
        # of those resolve to anything.
        if self.is_bound:
            department_id = self.data.get("department")
        else:
            department_id = self.initial.get("department") or getattr(self.instance, "department_id", None)
        owner_qs = User.objects.filter(is_active=True)
        if department_id:
            owner_qs = owner_qs.filter(department_id=department_id)
        self.fields["owner"].queryset = owner_qs.order_by("first_name", "last_name")

        self.fields["custom_adherents"].queryset = eligible_custom_adherents(self.instance)
        self.fields["custom_adherents"].required = False
        self.fields["target_groups"].required = False

        # Default targeting to "All Employees" on a brand-new policy.
        if is_new and not self.data:
            all_emp = Group.objects.filter(name="All Employees").first()
            if all_emp:
                self.initial.setdefault("target_groups", [all_emp.pk])

        self.style_widgets()


class PolicyVersionUploadForm(StyledMixin, forms.ModelForm):
    """Upload a PDF as a new version of an existing policy."""

    class Meta:
        model = PolicyVersion
        fields = ("pdf", "change_note", "requires_reack")
        widgets = {
            "change_note": forms.TextInput(
                attrs={"placeholder": "What changed in this version?"},
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pdf"].help_text = f"PDF, up to {pdf_max_bytes() // (1024 * 1024)} MB."
        self.fields["requires_reack"].help_text = (
            "Re-open signing for everyone who already signed an earlier version."
        )
        self.style_widgets()

    def clean_pdf(self):
        return _validate_pdf(self.cleaned_data.get("pdf"))


class SignForm(StyledMixin, forms.Form):
    """The 'I have read and accept' acknowledgement form.

    ``accept`` defaults to checked and renders hidden — clicking "Accept" is
    itself the affirmative act; the field stays around only because the sign
    view's audit trail (ip/user-agent/timestamp — see ``services.sign``) is
    keyed off a validated form submission, not because we still want the
    reader to tick a separate box.
    """

    accept = forms.BooleanField(
        required=True,
        initial=True,
        label="I have read and accept this policy.",
        error_messages={"required": "You must confirm you have read and accept the policy."},
        widget=forms.CheckboxInput(attrs={"class": "hidden"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_widgets()
