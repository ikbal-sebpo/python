"""Views for the documents app.

Every form view branches on ``request.htmx``: returns a drawer body fragment
on htmx requests, a full-page form template otherwise. POST success returns
an OOB row + ``HX-Trigger: orbis:drawer-close`` header that the page-level
Alpine handler picks up.

Permission gating goes through ``documents.services`` helpers which in turn
call ``accounts.permissions`` — never reimplement access policy here.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from core.pagination import PageSizeMixin

from accounts.filters import employee_search_q
from accounts.permissions import can_see_all
from documents.forms import (
    ApprovalActionForm,
    DocumentRequestFilterForm,
    DocumentRequestForm,
    MyDocumentsFilterForm,
    ReviewActionForm,
)
from documents.models import (
    DocumentRequest,
    DocumentType,
    RequestApproval,
    RequestReview,
)
from documents.services import (
    can_act_on_document_request,
    can_approve_step,
    can_cancel_request,
    can_review_step,
    visible_document_requests,
)


# ---------------------------------------------------------------------------
# Employee-facing
# ---------------------------------------------------------------------------

class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "documents/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        user_groups = list(user.groups.values_list("pk", flat=True))
        ctx["available_doc_types"] = DocumentType.objects.filter(is_active=True)
        ctx["my_recent_requests"] = (
            DocumentRequest.objects.filter(employee=user)
            .select_related("document_type")
            .order_by("-applied_at")[:5]
        )
        ctx["pending_reviews"] = (
            RequestReview.objects.filter(
                status=RequestReview.Status.PENDING,
                reviewer_group__in=user_groups,
            )
            .exclude(request__employee=user)
            .select_related("request", "request__employee", "request__document_type")
            .order_by("request__applied_at")[:10]
        )
        ctx["pending_approvals"] = (
            RequestApproval.objects.filter(
                status=RequestApproval.Status.PENDING,
                approver_group__in=user_groups,
            )
            .exclude(request__employee=user)
            .select_related("request", "request__employee", "request__document_type")
            .order_by("request__applied_at")[:10]
        )
        ctx["page_title"] = "Documents"
        return ctx


class MyDocumentsView(LoginRequiredMixin, PageSizeMixin, ListView):
    template_name = "documents/my_documents.html"
    context_object_name = "requests"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            DocumentRequest.objects.filter(employee=self.request.user)
            .select_related("document_type")
            .order_by("-applied_at")
        )
        self.filter_form = MyDocumentsFilterForm(self.request.GET or None)
        if self.filter_form.is_valid():
            q = self.filter_form.cleaned_data.get("q")
            dt = self.filter_form.cleaned_data.get("document_type")
            if q:
                qs = qs.filter(document_type__display_name__icontains=q)
            if dt:
                qs = qs.filter(document_type=dt)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        ctx["page_title"] = "My documents"
        return ctx


class RequestStartView(LoginRequiredMixin, TemplateView):
    template_name = "documents/request_start.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["doc_types"] = DocumentType.objects.filter(is_active=True).order_by("display_name")
        ctx["page_title"] = "Request a document"
        return ctx


class RequestFormView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, doc_type_id, *args, **kwargs):
        self.doc_type = get_object_or_404(DocumentType, pk=doc_type_id, is_active=True)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = DocumentRequestForm(document_type=self.doc_type, user=request.user)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = DocumentRequestForm(
            request.POST, document_type=self.doc_type, user=request.user,
        )
        if form.is_valid():
            doc_request = form.save(employee=request.user)
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = reverse("documents:request_detail",
                                                  args=[doc_request.id])
                return response
            messages.success(request, "Request submitted.")
            return redirect("documents:request_detail", request_id=doc_request.id)
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "doc_type": self.doc_type,
            "action_url": reverse("documents:request_form", args=[self.doc_type.pk]),
            "drawer_title": f"Request: {self.doc_type.display_name}",
            "page_title": self.drawer_title if False else self.doc_type.display_name,
        }
        template = (
            "documents/_drawer_request.html" if request.htmx
            else "documents/request_form.html"
        )
        return render(request, template, ctx)


class RequestDetailView(LoginRequiredMixin, DetailView):
    model = DocumentRequest
    pk_url_kwarg = "request_id"
    template_name = "documents/request_detail.html"
    context_object_name = "doc_request"

    def get_queryset(self):
        return DocumentRequest.objects.select_related(
            "employee", "document_type", "applied_by", "processed_by",
        ).prefetch_related("reviews", "approvals", "extra_field_values__field_type")

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        user = self.request.user
        if obj.employee_id == user.pk:
            return obj
        if can_see_all(user, request=self.request):
            return obj
        # Reviewer / approver also gets to see the detail.
        user_groups = set(user.groups.values_list("pk", flat=True))
        review_groups = set(obj.reviews.values_list("reviewer_group_id", flat=True))
        approval_groups = set(obj.approvals.values_list("approver_group_id", flat=True))
        if user_groups & (review_groups | approval_groups):
            return obj
        raise PermissionDenied("You can't view this request.")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_cancel"] = can_cancel_request(self.request.user, ctx["doc_request"])
        ctx["page_title"] = ctx["doc_request"].document_type.display_name
        return ctx


class RequestCancelView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, request_id):
        doc_request = get_object_or_404(DocumentRequest, pk=request_id)
        if not can_cancel_request(request.user, doc_request):
            raise PermissionDenied("You can't cancel this request.")
        DocumentRequest.objects.filter(pk=doc_request.pk).update(
            status=DocumentRequest.Status.CANCELLED,
            processed_by=request.user,
            processed_at=timezone.now(),
            process_note="Cancelled by " + request.user.display_name,
        )
        messages.success(request, "Request cancelled.")
        return redirect("documents:request_detail", request_id=doc_request.id)


class DownloadPdfView(LoginRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request, request_id):
        doc_request = get_object_or_404(
            DocumentRequest.objects.select_related("document_type", "employee"),
            pk=request_id,
        )
        if not doc_request.generated_file:
            raise Http404("PDF not generated yet.")
        if doc_request.status != DocumentRequest.Status.APPROVED:
            raise Http404("Document not approved.")
        user = request.user
        from audit.capture import note
        note(request, subject=doc_request.employee, document_type=doc_request.document_type.code)
        is_owner = doc_request.employee_id == user.pk
        is_signer = doc_request.approvals.filter(signatory=user).exists()
        is_reviewer = doc_request.reviews.filter(reviewer=user).exists()
        if not (is_owner or is_signer or is_reviewer or can_see_all(user)):
            raise PermissionDenied("You can't download this document.")
        return FileResponse(
            doc_request.generated_file.open("rb"),
            as_attachment=True,
            filename=f"{doc_request.document_type.code}_{doc_request.id}.pdf",
            content_type="application/pdf",
        )


# ---------------------------------------------------------------------------
# Reviewer / approver action views (shared shape)
# ---------------------------------------------------------------------------

class _StepActionViewBase(LoginRequiredMixin, View):
    """Shared GET/POST drawer flow for review + approval actions."""

    http_method_names = ["get", "post"]
    model = None             # subclass: RequestReview or RequestApproval
    form_class = None        # subclass
    can_act = staticmethod(lambda u, s: False)  # subclass override
    drawer_template = ""     # subclass
    page_template = ""       # subclass
    drawer_title_prefix = ""
    url_name = ""            # used to reverse action_url
    success_message = "Saved."

    def dispatch(self, request, step_id, *args, **kwargs):
        self.step = get_object_or_404(
            self.model.objects.select_related(
                "request", "request__employee", "request__document_type",
            ),
            pk=step_id,
        )
        if not self.can_act(request.user, self.step):
            raise PermissionDenied("You can't act on this step.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = self.form_class(instance=self.step)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = self.form_class(request.POST, instance=self.step)
        if form.is_valid():
            self._apply(form, request.user)
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = reverse(
                    "documents:request_detail", args=[self.step.request_id],
                )
                return response
            messages.success(request, self.success_message)
            return redirect("documents:request_detail", request_id=self.step.request_id)
        return self._render(request, form)

    def _apply(self, form, user):  # subclass override
        raise NotImplementedError

    def _render(self, request, form):
        ctx = {
            "form": form,
            "step": self.step,
            "doc_request": self.step.request,
            "action_url": reverse(self.url_name, args=[self.step.pk]),
            "drawer_title": f"{self.drawer_title_prefix}: {self.step.request.document_type.display_name}",
            "page_title": self.drawer_title_prefix,
        }
        template = self.drawer_template if request.htmx else self.page_template
        return render(request, template, ctx)


class ReviewActionView(_StepActionViewBase):
    model = RequestReview
    form_class = ReviewActionForm
    can_act = staticmethod(can_review_step)
    drawer_template = "documents/_drawer_review.html"
    page_template = "documents/review_action.html"
    drawer_title_prefix = "Review"
    url_name = "documents:review_action"
    success_message = "Review recorded."

    def _apply(self, form, user):
        obj = form.save(commit=False)
        obj.reviewer = user
        obj.reviewed_at = timezone.now()
        obj.save()


class ApprovalActionView(_StepActionViewBase):
    model = RequestApproval
    form_class = ApprovalActionForm
    can_act = staticmethod(can_approve_step)
    drawer_template = "documents/_drawer_approval.html"
    page_template = "documents/approval_action.html"
    drawer_title_prefix = "Approve"
    url_name = "documents:approval_action"
    success_message = "Approval recorded."

    def _apply(self, form, user):
        obj = form.save(commit=False)
        obj.signatory = user
        obj.signed_at = timezone.now()
        if hasattr(form, "save_extra_values") and obj.status == RequestApproval.Status.APPROVED:
            form.save_extra_values(submitted_by=user)
        obj.save()


# ---------------------------------------------------------------------------
# Queues (reviewer + approver + HR all-pending)
# ---------------------------------------------------------------------------

class PendingQueueView(LoginRequiredMixin, TemplateView):
    """One page combining the user's pending reviews and pending approvals
    in two cards. Works equally well for a reviewer (sees reviews only),
    an approver (sees approvals only), or HR (sees their group's queue —
    they still have to be in the configured reviewer/approver group)."""

    template_name = "documents/pending_queue.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        user_groups = list(user.groups.values_list("pk", flat=True))
        ctx["pending_reviews"] = (
            RequestReview.objects.filter(
                status=RequestReview.Status.PENDING,
                reviewer_group__in=user_groups,
            )
            .exclude(request__employee=user)
            .select_related("request", "request__employee", "request__document_type")
            .order_by("request__applied_at")
        )
        ctx["pending_approvals"] = (
            RequestApproval.objects.filter(
                status=RequestApproval.Status.PENDING,
                approver_group__in=user_groups,
            )
            .exclude(request__employee=user)
            .select_related("request", "request__employee", "request__document_type")
            .order_by("request__applied_at")
        )
        ctx["page_title"] = "Pending queue"
        return ctx


class AllDocumentsView(LoginRequiredMixin, PageSizeMixin, ListView):
    """HR-facing list of all requests. Filters by status / type / date /
    employee. Visible to anyone who passes ``can_see_all`` or has at least
    one request in scope."""

    template_name = "documents/all_documents.html"
    context_object_name = "requests"
    paginate_by = 25

    def get_queryset(self):
        self.filter_form = DocumentRequestFilterForm(self.request.GET or None)
        qs = visible_document_requests(self.request.user, request=self.request)

        if self.filter_form.is_valid():
            cd = self.filter_form.cleaned_data
            if cd.get("q"):
                qs = qs.filter(employee_search_q(cd["q"], prefix="employee__"))
            if cd.get("document_type"):
                qs = qs.filter(document_type=cd["document_type"])
            if cd.get("status"):
                qs = qs.filter(status=cd["status"])
            if cd.get("date_from"):
                qs = qs.filter(applied_at__date__gte=cd["date_from"])
            if cd.get("date_to"):
                qs = qs.filter(applied_at__date__lte=cd["date_to"])
            if cd.get("mine_only"):
                user_groups = list(self.request.user.groups.values_list("pk", flat=True))
                qs = qs.filter(
                    Q(reviews__status=RequestReview.Status.PENDING,
                      reviews__reviewer_group__in=user_groups)
                    | Q(approvals__status=RequestApproval.Status.PENDING,
                        approvals__approver_group__in=user_groups)
                ).distinct()

        return qs.order_by("-applied_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        ctx["page_title"] = "All documents"
        return ctx
