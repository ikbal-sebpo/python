"""Permission + visibility helpers for the documents app.

Mirror the leave module's pattern: thin wrappers around
``accounts.permissions`` so the access policy lives in one place.
"""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse

from accounts.permissions import (
    _in_managed_scope,
    can_edit_all,
    can_see_all,
    visible_employee_pks,
)


def absolute_url(path: str) -> str:
    """Build an absolute URL using settings.PUBLIC_BASE_URL. Used in email
    bodies where the request object isn't available."""
    from django.conf import settings
    base = getattr(settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base}{path}"


def request_download_url(doc_request) -> str:
    return absolute_url(reverse("documents:download_pdf", args=[doc_request.id]))


def request_verify_url(doc_request) -> str:
    return absolute_url(reverse("documents_public:verify_document", args=[doc_request.id]))


def can_act_on_document_request(user, doc_request, request=None) -> bool:
    """Mirror of ``leave.services.can_act_on_leave``: admins act on anyone
    except themselves; managers act on downline; self can cancel-while-pending
    only (handled separately).
    """
    if not user or not user.is_authenticated or not doc_request:
        return False
    if can_edit_all(user, request):
        return True
    if doc_request.employee_id == user.pk:
        return False
    return _in_managed_scope(user, doc_request.employee_id, request)


def can_review_step(user, review) -> bool:
    from documents.models import DocumentRequest, RequestReview

    if not user or not user.is_authenticated:
        return False
    # A finished step must stay finished — without these gates a reviewer
    # could re-action a completed step (e.g. flip a REJECTED request back
    # through the pipeline).
    if review.status != RequestReview.Status.PENDING:
        return False
    if review.request.status != DocumentRequest.Status.IN_REVIEW:
        return False
    if review.request.employee_id == user.pk:
        return False  # self-review forbidden even if you're in the group
    return user.groups.filter(pk=review.reviewer_group_id).exists()


def can_approve_step(user, approval) -> bool:
    from documents.models import DocumentRequest, RequestApproval

    if not user or not user.is_authenticated:
        return False
    if approval.status != RequestApproval.Status.PENDING:
        return False
    if approval.request.status not in (
        DocumentRequest.Status.PENDING,
        DocumentRequest.Status.PARTIALLY_APPROVED,
    ):
        return False
    if approval.request.employee_id == user.pk:
        return False
    return user.groups.filter(pk=approval.approver_group_id).exists()


def can_cancel_request(user, doc_request) -> bool:
    """Applicants cancel while still pending; admins always can."""
    from documents.models import DocumentRequest
    if not user or not user.is_authenticated:
        return False
    if can_edit_all(user):
        return True
    return (
        doc_request.employee_id == user.pk
        and doc_request.status in (
            DocumentRequest.Status.IN_REVIEW,
            DocumentRequest.Status.PENDING,
        )
    )


def visible_document_requests(user, request=None):
    """All DocumentRequest rows ``user`` is allowed to see."""
    from documents.models import DocumentRequest

    qs = DocumentRequest.objects.select_related("employee", "document_type")
    if can_see_all(user, request):
        return qs
    return qs.filter(
        Q(employee=user)
        | Q(employee_id__in=visible_employee_pks(user, request))
        | Q(reviews__reviewer_group__in=user.groups.all())
        | Q(approvals__approver_group__in=user.groups.all())
    ).distinct()
