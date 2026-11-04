"""Public (no-auth) views for the documents app.

The QR code on every issued letter points at ``/verify/<uuid>/`` so anyone
can confirm the document is genuine. ``/verify/<uuid>/preview/`` embeds
the PDF for inline viewing.
"""

from __future__ import annotations

from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import DetailView, TemplateView

from documents.models import DocumentRequest, RequestApproval


def _throttled(request, limit: int = 60, window: int = 60) -> bool:
    """Soft per-IP rate limit for the unauthenticated verify surface.

    UUID4 keys make enumeration infeasible; this just keeps automated
    scanning/scraping of the public endpoints cheaply bounded. Uses the
    first X-Forwarded-For hop (set by nginx) with REMOTE_ADDR fallback.
    """
    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "?")
    )
    key = f"documents:verify_throttle:{ip}"
    if cache.add(key, 1, window):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:  # expired between add and incr
        return False


def _too_many_requests() -> HttpResponse:
    return HttpResponse("Too many requests. Try again shortly.", status=429)


class PublicVerifyView(DetailView):
    """The 'is this real?' page. Shows the employee + doc type + signer list
    when status=APPROVED; otherwise renders a polite failure page."""

    model = DocumentRequest
    pk_url_kwarg = "uid"
    template_name = "documents/public_verify_success.html"
    context_object_name = "doc"

    def get_object(self, queryset=None):
        return get_object_or_404(
            DocumentRequest.objects.select_related("employee", "document_type"),
            pk=self.kwargs[self.pk_url_kwarg],
        )

    def get(self, request, *args, **kwargs):
        if _throttled(request):
            return _too_many_requests()
        obj = self.get_object()
        if obj.status != DocumentRequest.Status.APPROVED:
            # Deliberately slim context: an unauthenticated visitor holding a
            # non-approved reference learns only "not verified" — not the
            # internal state, employee, or document type.
            return render(
                request, "documents/public_verify_fail.html", {"ref": obj.pk},
            )
        signers = list(
            obj.approvals.filter(status=RequestApproval.Status.APPROVED)
            .select_related("signatory", "approver_group")
            .order_by("order")
        )
        return render(request, self.template_name, {
            "doc": obj,
            "signers": signers,
            "generated_at": obj.updated_at,
            "preview_url": reverse("documents_public:public_preview", args=[obj.id]),
        })


class PublicPreviewView(TemplateView):
    template_name = "documents/public_preview.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        doc = get_object_or_404(
            DocumentRequest.objects.select_related("document_type"),
            pk=kwargs["uid"],
        )
        if doc.status != DocumentRequest.Status.APPROVED or not doc.generated_file:
            # Slim context on purpose — see PublicVerifyView.get.
            self.template_name = "documents/public_preview_unavailable.html"
            return ctx
        ctx["doc"] = doc
        ctx["pdf_url"] = reverse("documents_public:public_pdf", args=[doc.id])
        ctx["verify_url"] = reverse("documents_public:verify_document", args=[doc.id])
        return ctx


@xframe_options_sameorigin
def public_pdf(request, uid):
    """Stream the PDF inline for embedding inside the preview iframe."""
    if _throttled(request):
        return _too_many_requests()
    doc = get_object_or_404(DocumentRequest, pk=uid)
    if doc.status != DocumentRequest.Status.APPROVED or not doc.generated_file:
        raise Http404("Document not available.")
    response = FileResponse(
        doc.generated_file.open("rb"),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'inline; filename="{doc.document_type.code}_{doc.id}.pdf"'
    )
    return response
