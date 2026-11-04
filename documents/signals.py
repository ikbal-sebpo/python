"""Signal handlers driving the document-request workflow.

Six handlers ported from veridoc with two changes:

* rejection is appended to ``rejection_history`` (not overwritten)
* every notification is wrapped in ``transaction.on_commit`` so a rolled-back
  transaction never fires a phantom email

Order of fire on a brand-new APPROVED-on-create request (auto-approve):
  pre_save (stash prev=None) → create row → post_save:
    create_review_chain (no configs, requires_approval=False, auto_approve=True
                         → marks APPROVED + saves note)
    → second post_save (created=False from the inner .save) →
    generate_pdf_when_approved → on_commit(generate + email)
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from core.tasks import defer

from documents.models import (
    DocumentRequest,
    DocumentReviewConfig,
    DocumentSignatoryConfig,
    RequestApproval,
    RequestReview,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _create_approval_chain(req: DocumentRequest) -> bool:
    """Idempotent: create RequestApproval rows from DocumentSignatoryConfig.
    Returns True if any row was created (False if all already existed)."""
    configs = DocumentSignatoryConfig.objects.filter(
        document_type=req.document_type,
    ).order_by("signature_order")
    any_created = False
    for cfg in configs:
        _, made = RequestApproval.objects.get_or_create(
            request=req,
            approver_group=cfg.signatory_group,
            defaults={"order": cfg.signature_order},
        )
        any_created = any_created or made
    return any_created


def _notify_first_pending_reviewer(req: DocumentRequest) -> None:
    review = req.reviews.filter(status=RequestReview.Status.PENDING).order_by("order").first()
    if not review:
        return
    from core.notifications import notify_group
    notify_group(
        review.reviewer_group,
        subject_template_path="documents/notifications/review_assigned_subject.txt",
        body_template_path="documents/notifications/review_assigned_body.txt",
        context={"request_obj": req, "review": review},
    )


def _notify_first_pending_approver(req: DocumentRequest) -> None:
    approval = req.approvals.filter(
        status=RequestApproval.Status.PENDING,
    ).order_by("order").first()
    if not approval:
        return
    from core.notifications import notify_group
    notify_group(
        approval.approver_group,
        subject_template_path="documents/notifications/approval_assigned_subject.txt",
        body_template_path="documents/notifications/approval_assigned_body.txt",
        context={"request_obj": req, "approval": approval},
    )


def _notify_request_rejected(req: DocumentRequest) -> None:
    from core.notifications import notify_user
    notify_user(
        req.employee,
        subject_template_path="documents/notifications/request_rejected_subject.txt",
        body_template_path="documents/notifications/request_rejected_body.txt",
        context={"request_obj": req, "latest_rejection": req.latest_rejection},
    )


def _notify_request_approved(req: DocumentRequest) -> None:
    from core.notifications import notify_user
    from documents.services import request_download_url, request_verify_url
    notify_user(
        req.employee,
        subject_template_path="documents/notifications/request_approved_subject.txt",
        body_template_path="documents/notifications/request_approved_body.txt",
        context={
            "request_obj": req,
            "download_url": request_download_url(req),
            "verify_url": request_verify_url(req),
        },
    )


# ---------------------------------------------------------------------------
# Signal 1 — create the review/approval chain (or auto-approve) on create
# ---------------------------------------------------------------------------

@receiver(post_save, sender=DocumentRequest, dispatch_uid="documents.create_review_chain")
def create_review_chain(sender, instance: DocumentRequest, created: bool, **kwargs):
    if not created:
        return

    review_configs = list(DocumentReviewConfig.objects.filter(
        document_type=instance.document_type,
    ).order_by("review_order"))

    if review_configs:
        RequestReview.objects.bulk_create([
            RequestReview(
                request=instance,
                reviewer_group=cfg.reviewer_group,
                order=cfg.review_order,
            )
            for cfg in review_configs
        ])
        DocumentRequest.objects.filter(pk=instance.pk).update(
            status=DocumentRequest.Status.IN_REVIEW,
        )
        instance.status = DocumentRequest.Status.IN_REVIEW  # keep instance in sync
        transaction.on_commit(lambda: _notify_first_pending_reviewer(instance))
        return

    if instance.document_type.requires_approval:
        _create_approval_chain(instance)
        DocumentRequest.objects.filter(pk=instance.pk).update(
            status=DocumentRequest.Status.PENDING,
        )
        instance.status = DocumentRequest.Status.PENDING
        transaction.on_commit(lambda: _notify_first_pending_approver(instance))
        return

    # No reviews, no approvals: auto-approve only when explicitly enabled.
    if instance.document_type.auto_approve:
        note = f"Auto-approved: {instance.document_type.auto_approve_reason}"
        DocumentRequest.objects.filter(pk=instance.pk).update(
            status=DocumentRequest.Status.APPROVED,
            process_note=note,
        )
        instance.status = DocumentRequest.Status.APPROVED
        instance.process_note = note
        # Touching the row again via .save() would re-fire post_save handlers;
        # we instead emit a synthetic save signal so generate_pdf_when_approved
        # picks it up. Cheapest path: re-fetch & .save() with an explicit guard.
        from django.utils import timezone
        DocumentRequest.objects.filter(pk=instance.pk).update(processed_at=timezone.now())
        # Trigger the PDF flow by manually invoking the post-approval handler.
        transaction.on_commit(lambda: defer(_post_approval, instance.pk))
        return

    logger.warning(
        "DocumentType %r has no review/approval config and is not auto_approve; "
        "request %s is stuck.", instance.document_type.code, instance.pk,
    )


# ---------------------------------------------------------------------------
# Signal 2 — review state machine
# ---------------------------------------------------------------------------

@receiver(post_save, sender=RequestReview, dispatch_uid="documents.update_status_from_reviews")
def update_request_status_from_reviews(sender, instance: RequestReview, **kwargs):
    req = instance.request
    if instance.status == RequestReview.Status.REJECTED:
        req.append_rejection(
            stage="review",
            step_order=instance.order,
            reason=instance.remarks,
            by=instance.reviewer,
        )
        DocumentRequest.objects.filter(pk=req.pk).update(
            status=DocumentRequest.Status.REJECTED,
            processed_by=instance.reviewer,
            processed_at=instance.reviewed_at,
            process_note=f"Rejected at review step {instance.order}",
        )
        req.refresh_from_db(fields=["status"])
        transaction.on_commit(lambda: _notify_request_rejected(req))
        return

    if instance.status != RequestReview.Status.REVIEWED:
        return

    # Are there more pending reviews?
    if req.reviews.exclude(status=RequestReview.Status.REVIEWED).exists():
        # Still IN_REVIEW; notify the next reviewer group.
        transaction.on_commit(lambda: _notify_first_pending_reviewer(req))
        return

    # All reviews done — proceed to approval (or auto-approve).
    if req.document_type.requires_approval:
        _create_approval_chain(req)
        DocumentRequest.objects.filter(pk=req.pk).update(
            status=DocumentRequest.Status.PENDING,
        )
        req.refresh_from_db(fields=["status"])
        transaction.on_commit(lambda: _notify_first_pending_approver(req))
    else:
        # Reviews-only flow: complete.
        DocumentRequest.objects.filter(pk=req.pk).update(
            status=DocumentRequest.Status.APPROVED,
            process_note="Approved after final review",
        )
        req.refresh_from_db(fields=["status", "process_note"])
        transaction.on_commit(lambda: defer(_post_approval, req.pk))


# ---------------------------------------------------------------------------
# Signal 3 — approval state machine
# ---------------------------------------------------------------------------

@receiver(post_save, sender=RequestApproval, dispatch_uid="documents.update_status_from_approvals")
def update_request_status_from_approvals(sender, instance: RequestApproval, **kwargs):
    req = instance.request
    if instance.status == RequestApproval.Status.REJECTED:
        req.append_rejection(
            stage="approval",
            step_order=instance.order,
            reason=instance.remarks,
            by=instance.signatory,
        )
        DocumentRequest.objects.filter(pk=req.pk).update(
            status=DocumentRequest.Status.REJECTED,
            processed_by=instance.signatory,
            processed_at=instance.signed_at,
            process_note=f"Rejected at approval step {instance.order}",
        )
        req.refresh_from_db(fields=["status"])
        transaction.on_commit(lambda: _notify_request_rejected(req))
        return

    if instance.status != RequestApproval.Status.APPROVED:
        return

    pending = req.approvals.filter(status=RequestApproval.Status.PENDING)
    if pending.exists():
        DocumentRequest.objects.filter(pk=req.pk).update(
            status=DocumentRequest.Status.PARTIALLY_APPROVED,
        )
        req.refresh_from_db(fields=["status"])
        transaction.on_commit(lambda: _notify_first_pending_approver(req))
        return

    # All approved → final approval.
    from django.utils import timezone
    DocumentRequest.objects.filter(pk=req.pk).update(
        status=DocumentRequest.Status.APPROVED,
        processed_by=instance.signatory,
        processed_at=instance.signed_at or timezone.now(),
    )
    req.refresh_from_db(fields=["status", "processed_by", "processed_at"])
    transaction.on_commit(lambda: defer(_post_approval, req.pk))


# ---------------------------------------------------------------------------
# Signal 4 — track previous status on the instance (for transition detection)
# ---------------------------------------------------------------------------

@receiver(pre_save, sender=DocumentRequest, dispatch_uid="documents.stash_prev_status")
def stash_prev_status(sender, instance: DocumentRequest, **kwargs):
    if not instance.pk:
        instance._prev_status = None
        return
    try:
        instance._prev_status = sender.objects.only("status").get(pk=instance.pk).status
    except sender.DoesNotExist:
        instance._prev_status = None


# ---------------------------------------------------------------------------
# Post-approval action (PDF + notification). Idempotent.
# ---------------------------------------------------------------------------

def _post_approval(request_pk) -> None:
    """Generate the PDF and email the employee. Safe to call multiple times —
    PDF generation is idempotent (overwrites the same path)."""
    from documents.pdf import generate_and_attach_pdf
    try:
        generate_and_attach_pdf(request_pk)
    except Exception:
        logger.exception("PDF generation failed for DocumentRequest %s", request_pk)
        req = DocumentRequest.objects.filter(pk=request_pk).first()
        if req is not None:
            req.append_rejection(
                stage="pdf",
                step_order=0,
                reason="PDF generation failed; see server logs.",
                by=None,
            )
        return

    req = DocumentRequest.objects.filter(pk=request_pk).first()
    if req is not None:
        _notify_request_approved(req)
