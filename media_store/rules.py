"""Who may fetch ``/media/<name>`` — authorisation by top-level prefix.

Every rule resolves the file back to its owning row and applies the same
check the page that links to it already applies, so the serve view can never
be more permissive than the page. Prefixes whose files already have a
dedicated permission-checked view (letters, policies, payroll documents) are
*hidden* here — 404 for everyone, superusers included — so those views stay
the only path. Spec: docs/MEDIA_GOOGLE_DRIVE_PLAN.md §4.5.
"""

from __future__ import annotations

from dataclasses import dataclass

from audit.models import DataClass

from accounts.permissions import can_see_all, can_view_employee, visible_employee_pks


@dataclass
class Resolution:
    allowed: bool
    hidden: bool = False          # True → 404 rather than 403
    subject: object = None        # CustomUser the file is about (audit)
    obj: object = None            # owning row (audit)
    data_class: str = DataClass.DOCUMENT


def _exact(qs, field: str, name: str):
    """The row whose ``field`` holds exactly ``name``, or None.

    The FileField columns carry MySQL's case- and accent-insensitive default
    collation, so ``filter(field=name)`` can also return the row for
    ``Photo.jpg`` when asked for ``photo.jpg`` — real files on a case-sensitive
    filesystem (2026-10-16 migration). Authorising against that row would apply
    the wrong person's permission, so re-compare in Python.
    """
    for row in qs.filter(**{field: name}):
        if getattr(row, field).name == name:
            return row
    return None


def _deny_hidden(name, user, request):
    return Resolution(allowed=False, hidden=True)


def _profile_picture(name, user, request):
    from accounts.models import CustomUser
    owner = _exact(CustomUser.objects.all(), "profile_picture", name)
    return Resolution(allowed=True, subject=owner, data_class=DataClass.PROFILE)


def _employee_image(name, user, request):
    from employees.models import Image
    row = _exact(Image.objects.select_related("employee"), "image", name)
    if row is None:
        return Resolution(allowed=False, hidden=True)
    return Resolution(
        allowed=can_view_employee(user, row.employee, request),
        subject=row.employee, obj=row, data_class=DataClass.PROFILE,
    )


def _branding(name, user, request):
    return Resolution(allowed=True, data_class=DataClass.DOCUMENT)


def _signature(name, user, request):
    from accounts.models import CustomUser
    owner = _exact(CustomUser.objects.all(), "signature", name)
    if owner is None:
        return Resolution(allowed=False, hidden=True)
    allowed = owner.pk == user.pk or can_see_all(user, request)
    return Resolution(allowed=allowed, subject=owner, data_class=DataClass.PROFILE)


def _legal(name, user, request):
    from employees.models import LegalIssue
    row = _exact(LegalIssue.objects.select_related("employee"), "case_document", name)
    if row is None:
        return Resolution(allowed=False, hidden=True)
    return Resolution(
        allowed=can_view_employee(user, row.employee, request),
        subject=row.employee, obj=row, data_class=DataClass.PROFILE,
    )


def _leave(name, user, request):
    from leave.models import LeaveApplication
    app = _exact(LeaveApplication.objects.select_related("employee"), "attachment", name)
    if app is None:
        return Resolution(allowed=False, hidden=True)
    # Same rule as leave.views.LeaveDetailView.get_object.
    allowed = (
        app.employee_id == user.pk
        or app.employee_id in visible_employee_pks(user, request=request)
        or can_see_all(user, request=request)
    )
    return Resolution(allowed=allowed, subject=app.employee, obj=app, data_class=DataClass.LEAVE)


def _offboarding(name, user, request):
    from offboarding.models import ClearanceStepAttachment
    from offboarding.permissions import can_view_case
    att = _exact(
        ClearanceStepAttachment.objects.select_related("step__case__resignation__employee"), "file", name,
    )
    if att is None:
        return Resolution(allowed=False, hidden=True)
    case = att.step.case
    return Resolution(
        allowed=can_view_case(user, case, request=request),
        subject=case.resignation.employee, obj=att, data_class=DataClass.OFFBOARDING,
    )


def _ckeditor_root_upload(name, user, request):
    # django_ckeditor_5 saves straight at the storage root (no prefix); its
    # upload endpoint is staff-only, so is reading the result.
    return Resolution(allowed=bool(user.is_staff), data_class=DataClass.DOCUMENT)


#: Longest matching prefix wins. Order here is readability only.
RULES: tuple[tuple[str, object], ...] = (
    ("profile_pictures/", _profile_picture),
    ("employees/", _employee_image),
    ("documents/branding/", _branding),
    ("signatures/", _signature),
    ("legal/", _legal),
    ("leave/", _leave),
    ("offboarding/", _offboarding),
    # Served only by their own permission-checked views:
    ("payroll/", _deny_hidden),             # payroll_tax_declaration_document, payroll_salary_loan_document
    ("documents/generated/", _deny_hidden), # documents DownloadPdfView + public verify
    ("policies/", _deny_hidden),            # policies:pdf
)


def resolve(name: str, user, request=None) -> Resolution:
    if not name or name.startswith("/") or ".." in name.split("/"):
        return Resolution(allowed=False, hidden=True)
    if "/" not in name:
        return _ckeditor_root_upload(name, user, request)
    best = None
    best_len = -1
    for prefix, func in RULES:
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = func, len(prefix)
    if best is None:
        return Resolution(allowed=False, hidden=True)
    return best(name, user, request)
