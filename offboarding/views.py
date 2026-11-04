"""Views for the offboarding app.

Same drawer pattern as documents/employees: ``request.htmx`` returns a
drawer body partial; otherwise a full-page template. Success returns 204
with ``HX-Trigger: orbis:drawer-close`` + optional ``HX-Redirect``.

Permission gating goes through ``offboarding.permissions``.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator

from core.pagination import PAGE_SIZES, resolve_page_size
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_GET
from django.views.generic import DetailView, TemplateView

from accounts.filters import employee_search_q
from accounts.permissions import can_see_all, can_edit_all, has_managed_scope, visible_employee_pks
from offboarding.forms import (
    AssetReturnForm,
    ClearanceStepActionForm,
    ClearanceQueueFilterForm,
    ExitInterviewForm,
    HRCreateResignationForm,
    OffboardingNoteForm,
    ResignationDecideForm,
    ResignationHREditForm,
    ResignationSubmitForm,
    StepAttachmentForm,
)
from offboarding.models import (
    AssetReturn,
    ClearanceStep,
    ClearanceStepAttachment,
    ExitInterview,
    OffboardingCase,
    OffboardingNote,
    Resignation,
    step_attachments_supported,
)
from offboarding.permissions import (
    can_see_all_offboarding,
    can_act_on_clearance_step,
    can_act_on_resignation,
    can_view_case,
    can_withdraw_resignation,
)
from offboarding.services import (
    auto_issue_letters,
    find_unbacked_separations,
    offboarding_candidates,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public (post-logout) — no auth gate
# ---------------------------------------------------------------------------

class AccessEndedView(TemplateView):
    """Friendly landing page for users redirected by the LWD middleware.
    Reachable without auth — never gate it."""

    template_name = "offboarding/access_ended.html"


# ---------------------------------------------------------------------------
# Employee self-service
# ---------------------------------------------------------------------------

class MyOffboardingView(LoginRequiredMixin, TemplateView):
    template_name = "offboarding/my_offboarding.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        latest = (Resignation.objects.filter(employee=user)
                  .order_by("-applied_at").first())
        ctx["resignation"] = latest
        ctx["case"] = getattr(latest, "case", None) if latest else None
        ctx["page_title"] = "My offboarding"
        return ctx


class ResignationSubmitView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, *args, **kwargs):
        # Block submission if there's already an active resignation.
        self.existing = (Resignation.objects.filter(
            employee=request.user,
            status__in=[Resignation.Status.PENDING, Resignation.Status.ACCEPTED],
        ).first())
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if self.existing:
            messages.info(request, "You already have an active resignation.")
            return redirect("offboarding:my_offboarding")
        form = ResignationSubmitForm(employee=request.user)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        if self.existing:
            raise PermissionDenied("Already an active resignation.")
        form = ResignationSubmitForm(request.POST)
        if form.is_valid():
            res = form.save(commit=False)
            res.employee = request.user
            res.applied_by = request.user
            res.added_by = request.user
            res.updated_by = request.user
            res.full_clean()
            res.save()
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = reverse("offboarding:my_offboarding")
                return response
            messages.success(request, "Resignation submitted. HR has been notified.")
            return redirect("offboarding:my_offboarding")
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "action_url": reverse("offboarding:resignation_submit"),
            "drawer_title": "Submit resignation",
            "page_title": "Submit resignation",
        }
        template = ("offboarding/resignation_submit_drawer.html"
                    if request.htmx else "offboarding/resignation_submit.html")
        return render(request, template, ctx)


class ResignationWithdrawView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        res = get_object_or_404(Resignation, pk=pk)
        if not can_withdraw_resignation(request.user, res, request=request):
            raise PermissionDenied("You can't withdraw this resignation.")
        Resignation.objects.filter(pk=res.pk).update(
            status=Resignation.Status.WITHDRAWN,
            processed_by=request.user,
            processed_at=timezone.now(),
            process_note=f"Withdrawn by {request.user.display_name}",
        )
        messages.success(request, "Resignation withdrawn.")
        return redirect("offboarding:my_offboarding")


class HRCreateResignationView(LoginRequiredMixin, View):
    """Admins / HR create a resignation on behalf of another employee.

    Use cases: involuntary termination, absconding, retirement processing,
    contract end administration. Optional ``auto_accept`` shortcut creates
    the row already in ACCEPTED state, which spawns the clearance case
    immediately via the existing signal chain.
    """

    http_method_names = ["get", "post"]

    def dispatch(self, request, *args, **kwargs):
        if not can_act_on_resignation(request.user, request=request):
            raise PermissionDenied("Admins / HR only.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = HRCreateResignationForm(
            viewer=request.user, request=request,
            initial=self._prefill(request, form_cls=HRCreateResignationForm),
        )
        return self._render(request, form)

    @staticmethod
    def _prefill(request, form_cls):
        """Pre-fill from ``?employee=<pk>`` — the "Needs offboarding" queue
        deep-links here for an employee whose LWD is already on record.

        Deliberately does NOT prefill separation_type or reason: those can't
        be inferred from a date, and a pre-selected guess is one distracted
        click away from becoming a wrong HR record. HR picks them.

        The lookup is scoped to the form's own employee queryset, so a
        tampered ?employee= for someone outside the viewer's scope prefills
        nothing (and would fail validation on POST regardless).
        """
        raw = request.GET.get("employee")
        if not raw:
            return {}
        probe = form_cls(viewer=request.user, request=request)
        employee = probe.fields["employee"].queryset.filter(pk=raw).first()
        if employee is None or not employee.last_working_date:
            return {}
        notice_days = (employee.notice_period_months or 1) * 30
        return {
            "employee": employee.pk,
            "last_working_date": employee.last_working_date,
            "notice_period_days": notice_days,
            "notice_start_date": employee.last_working_date - timedelta(days=notice_days),
        }

    def post(self, request, *args, **kwargs):
        form = HRCreateResignationForm(
            request.POST, viewer=request.user, request=request,
        )
        if form.is_valid():
            res = form.save(commit=False)
            res.applied_by = request.user
            res.added_by = request.user
            res.updated_by = request.user
            if form.cleaned_data.get("auto_accept"):
                res.status = Resignation.Status.ACCEPTED
                res.processed_by = request.user
                res.processed_at = timezone.now()
                res.process_note = (
                    res.process_note
                    or f"Created and accepted by {request.user.display_name}."
                )
            res.full_clean()
            res.save()  # post_save signal sends the HR-initiated email AND,
                        # when auto_accept, writes LWD + builds the case.

            target = (
                reverse("offboarding:case_detail", args=[res.case.pk])
                if hasattr(res, "case") and res.case is not None
                else reverse("offboarding:case_list")
            )
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = target
                return response
            messages.success(
                request,
                f"Resignation created for {res.employee.display_name}.",
            )
            return redirect(target)
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "action_url": reverse("offboarding:hr_create_resignation"),
            "drawer_title": "Create resignation",
            "page_title": "Create resignation (HR)",
        }
        template = (
            "offboarding/hr_create_resignation_drawer.html" if request.htmx
            else "offboarding/hr_create_resignation.html"
        )
        return render(request, template, ctx)


@login_required
@require_GET
def load_offboarding_employees(request):
    """HTML-fragment search backing the Create-resignation employee picker.

    Deliberately NOT employees.views.load_employees: that one is hardwired to
    reports_to_candidates (Team Lead+ in the same department) because it
    backs the manager picker. Offboarding needs anyone in the actor's scope,
    including already-deactivated unbacked separations, so it shares the
    form's own candidate queryset via offboarding_candidates().
    """
    if not can_act_on_resignation(request.user, request=request):
        raise PermissionDenied("Admins / HR only.")

    q = (request.GET.get("q") or "").strip()
    qs = offboarding_candidates(request.user, request=request)
    if q:
        qs = qs.filter(employee_search_q(q))
    return render(request, "employees/_employee_search_results.html", {
        "q": q,
        "results": qs.order_by("first_name", "last_name")[:25],
        "empty_message": "No matching employee in your scope.",
    })


# ---------------------------------------------------------------------------
# Manager acknowledgement queue (subset of the clearance queue, scoped to
# steps where the assigned_user IS the manager)
# ---------------------------------------------------------------------------

class ManagerAckQueueView(LoginRequiredMixin, TemplateView):
    template_name = "offboarding/manager_ack_queue.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["pending_steps"] = (
            ClearanceStep.objects
            .filter(
                assigned_user=user,
                status__in=[ClearanceStep.Status.PENDING, ClearanceStep.Status.IN_PROGRESS],
            )
            .select_related("case", "case__resignation", "case__resignation__employee")
            .order_by("case__started_at")
        )
        ctx["page_title"] = "Pending acknowledgements"
        return ctx


# ---------------------------------------------------------------------------
# Clearance queue (all groups)
# ---------------------------------------------------------------------------

class ClearanceQueueView(LoginRequiredMixin, TemplateView):
    template_name = "offboarding/clearance_queue.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        form = ClearanceQueueFilterForm(self.request.GET or None)
        case_qs = (
            OffboardingCase.objects.select_related(
                "resignation", "resignation__employee", "workflow_template",
            )
            .prefetch_related("steps", "steps__assigned_group", "steps__assigned_user")
            .order_by("-started_at", "-added_at")
        )
        if not can_see_all(user, request=self.request):
            user_groups = list(user.groups.values_list("pk", flat=True))
            case_qs = case_qs.filter(
                Q(steps__assigned_user=user) | Q(steps__assigned_group__in=user_groups),
            ).distinct()
        if form.is_valid():
            q = (form.cleaned_data.get("q") or "").strip()
            employee_id = (form.cleaned_data.get("employee_id") or "").strip()
            separation_type = form.cleaned_data.get("separation_type") or ""
            step_status = form.cleaned_data.get("step_status") or ""
            case_status = form.cleaned_data.get("case_status") or ""
            if q:
                case_qs = case_qs.filter(
                    employee_search_q(q, prefix="resignation__employee__")
                    | Q(steps__step_name__icontains=q)
                    | Q(steps__step_code__icontains=q)
                ).distinct()
            if employee_id:
                case_qs = case_qs.filter(resignation__employee__employee_id__iexact=employee_id)
            if separation_type:
                case_qs = case_qs.filter(resignation__separation_type=separation_type)
            if case_status:
                case_qs = case_qs.filter(status=case_status)
            if step_status:
                case_qs = case_qs.filter(steps__status=step_status).distinct()

        group_pks = set(user.groups.values_list("pk", flat=True))
        items = [self._build_case_row(case, user, group_pks) for case in case_qs]
        size = resolve_page_size(self.request)
        paginator = Paginator(items, size)
        page_number = self.request.GET.get("page")
        page = paginator.get_page(page_number)
        ctx["queue_form"] = form
        ctx["page_obj"] = page
        ctx["paginator"] = paginator
        ctx["is_paginated"] = page.has_other_pages()
        ctx["page_sizes"] = PAGE_SIZES
        ctx["current_page_size"] = size
        ctx["queue_rows"] = page.object_list
        ctx["waiting_count"] = sum(1 for row in items if row["waiting_on_me"])

        # Separations that never went through a Resignation — LWD written
        # straight onto CustomUser, so no case exists and the employee is
        # deactivated on schedule with no clearance at all. Only HR / admins
        # can act on these, so only they see the section.
        ctx["unbacked_separations"] = (
            list(find_unbacked_separations())
            if can_act_on_resignation(user, request=self.request) else []
        )
        ctx["page_title"] = "Clearance queue"
        return ctx

    def _build_case_row(self, case, user, group_pks):
        # sorted() over .all() keeps the prefetch cache warm — chaining
        # .order_by() onto a prefetched relation re-queries per case.
        steps = sorted(case.steps.all(), key=lambda s: s.order)
        pending = next(
            (step for step in steps if step.status in [ClearanceStep.Status.PENDING, ClearanceStep.Status.IN_PROGRESS]),
            None,
        )
        done = sum(1 for step in steps if step.status in [ClearanceStep.Status.DONE, ClearanceStep.Status.SKIPPED])
        total = len(steps)
        waiting_on_me = False
        if pending is not None:
            waiting_on_me = (
                pending.assigned_user_id == user.id
                or (pending.assigned_group_id and pending.assigned_group_id in group_pks)
            )
        return {
            "case": case,
            "steps": steps,
            "current_step": pending,
            "done": done,
            "total": total,
            "progress_percent": int((done / total) * 100) if total else 0,
            "waiting_on_me": waiting_on_me,
        }


# ---------------------------------------------------------------------------
# Step action — drawer + page
# ---------------------------------------------------------------------------

class ClearanceStepActionView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.step = get_object_or_404(
            ClearanceStep.objects.select_related(
                "case", "case__resignation", "case__resignation__employee", "template",
            ),
            pk=pk,
        )
        if not can_act_on_clearance_step(request.user, self.step, request=request):
            raise PermissionDenied("You can't act on this step.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = ClearanceStepActionForm(instance=self.step)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = ClearanceStepActionForm(request.POST, instance=self.step)
        if form.is_valid():
            form.save(acted_by=request.user)
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = reverse(
                    "offboarding:case_detail", args=[self.step.case_id],
                )
                return response
            messages.success(request, f"{self.step.step_name} updated.")
            return redirect("offboarding:case_detail", pk=self.step.case_id)
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "step": self.step,
            "case": self.step.case,
            "action_url": reverse("offboarding:clearance_step_action",
                                   args=[self.step.pk]),
            "drawer_title": f"{self.step.step_name}",
            "page_title": self.step.step_name,
        }
        template = ("offboarding/clearance_step_drawer.html"
                    if request.htmx else "offboarding/clearance_step_action.html")
        return render(request, template, ctx)


# ---------------------------------------------------------------------------
# HR — case list + detail + decide + edit + cancel + issue letters
# ---------------------------------------------------------------------------

class CaseListView(LoginRequiredMixin, TemplateView):
    """Show offboarding cases and resignation rows in one place with tabs."""

    template_name = "offboarding/case_list.html"
    paginate_by = 25
    TAB_CASES = "cases"
    TAB_PENDING = "pending"
    TAB_ALL = "all"

    def get_context_data(self, **kwargs):
        user = self.request.user
        tab = self._active_tab()
        q = (self.request.GET.get("q") or "").strip()

        case_qs = self._case_queryset()
        resignation_qs = self._resignation_queryset()

        if q:
            case_qs = case_qs.filter(
                employee_search_q(q, prefix="resignation__employee__")
                | Q(resignation__separation_type__icontains=q)
                | Q(status__icontains=q)
            ).distinct()
            resignation_qs = resignation_qs.filter(
                employee_search_q(q, prefix="employee__")
                | Q(separation_type__icontains=q)
                | Q(status__icontains=q)
            ).distinct()

        pending_qs = resignation_qs.filter(status=Resignation.Status.PENDING)

        if tab == self.TAB_CASES:
            page_source = self._paginate(case_qs)
            selected_cases = list(page_source.object_list)
            selected_resignations = []
        elif tab == self.TAB_PENDING:
            page_source = self._paginate(pending_qs)
            selected_cases = []
            selected_resignations = list(page_source.object_list)
        else:
            page_source = self._paginate(resignation_qs)
            selected_cases = []
            selected_resignations = list(page_source.object_list)

        ctx = {
            "cases": selected_cases,
            "resignation_rows": selected_resignations,
            "row_type": tab,
            "search_query": q,
            "active_tab": tab,
            "page_title": "Offboarding cases",
            "can_act_on_resignation": can_edit_all(user, request=self.request),
            "counts": {
                "cases": case_qs.count(),
                "pending": pending_qs.count(),
                "all": resignation_qs.count(),
            },
            "page_obj": page_source,
            "is_paginated": page_source.has_other_pages(),
            "paginator": page_source.paginator,
            "page_sizes": PAGE_SIZES,
            "current_page_size": resolve_page_size(self.request, self.paginate_by),
        }
        ctx.update(kwargs)
        return ctx

    def _active_tab(self):
        tab = self.request.GET.get("tab", self.TAB_CASES)
        if tab in {self.TAB_CASES, self.TAB_PENDING, self.TAB_ALL}:
            return tab
        return self.TAB_CASES

    def _paginate(self, queryset):
        size = resolve_page_size(self.request, self.paginate_by)
        paginator = Paginator(queryset, size)
        page_number = self.request.GET.get("page")
        return paginator.get_page(page_number)

    def _case_queryset(self):
        qs = (
            OffboardingCase.objects.select_related(
                "resignation", "resignation__employee", "workflow_template",
            ).order_by("-added_at")
        )
        if can_see_all_offboarding(self.request.user, request=self.request):
            return qs
        pks = visible_employee_pks(self.request.user, request=self.request)
        return qs.filter(
            Q(resignation__employee=self.request.user) | Q(resignation__employee_id__in=pks),
        )

    def _resignation_queryset(self):
        qs = (
            Resignation.objects.select_related("employee")
            .prefetch_related("case")
            .order_by("-applied_at")
        )
        if can_see_all_offboarding(self.request.user, request=self.request):
            return qs
        pks = visible_employee_pks(self.request.user, request=self.request)
        return qs.filter(Q(employee=self.request.user) | Q(employee_id__in=pks))


class CaseDetailView(LoginRequiredMixin, DetailView):
    model = OffboardingCase
    template_name = "offboarding/case_detail.html"
    context_object_name = "case"

    def get_queryset(self):
        return OffboardingCase.objects.select_related(
            "resignation", "resignation__employee", "primary_letter",
            "workflow_template",
        ).prefetch_related(
            # "letters" is deliberately absent: get_context_data refetches it
            # with select_related("document_type"), which a plain prefetch
            # can't cover.
            "steps__assigned_group", "steps__assigned_user", "steps__acted_by",
            "steps__template", "steps__template__field_definitions",
            "asset_returns__receiver", "notes__author",
        )

    def get_object(self, queryset=None):
        case = super().get_object(queryset)
        if not can_view_case(self.request.user, case, request=self.request):
            raise PermissionDenied("You can't view this case.")
        return case

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        action = request.POST.get("action")
        if action == "step":
            return self._post_step_action(request)
        if action == "attachment":
            return self._post_attachment_action(request)
        return redirect("offboarding:case_detail", pk=self.object.pk)

    def _post_step_action(self, request):
        step = get_object_or_404(
            ClearanceStep.objects.select_related("case", "template", "assigned_group", "assigned_user"),
            pk=request.POST.get("step_id"),
            case=self.object,
        )
        if not can_act_on_clearance_step(request.user, step, request=request):
            raise PermissionDenied("You can't act on this step.")
        step_form = ClearanceStepActionForm(request.POST, instance=step)
        attachment_form = StepAttachmentForm(request.POST, request.FILES)
        if step_form.is_valid() and attachment_form.is_valid():
            updated = step_form.save(acted_by=request.user)
            attachment_form.save(step=updated, user=request.user)
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                return response
            messages.success(request, f"{updated.step_name} updated.")
            return redirect("offboarding:case_detail", pk=self.object.pk)
        return self.render_to_response(self.get_context_data(
            active_step_id=step.pk,
            step_form=step_form,
            attachment_form=attachment_form,
        ))

    def _post_attachment_action(self, request):
        step = get_object_or_404(
            ClearanceStep.objects.select_related("case"),
            pk=request.POST.get("step_id"),
            case=self.object,
        )
        if not can_act_on_clearance_step(request.user, step, request=request):
            raise PermissionDenied("You can't act on this step.")
        attachment_form = StepAttachmentForm(request.POST, request.FILES)
        if attachment_form.is_valid():
            attachment_form.save(step=step, user=request.user)
            messages.success(request, "Attachments uploaded.")
            return redirect("offboarding:case_detail", pk=self.object.pk)
        return self.render_to_response(self.get_context_data(
            active_step_id=step.pk,
            attachment_errors={step.pk: attachment_form.errors},
        ))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        case = ctx["case"]
        user = self.request.user
        ctx["exit_interview"] = getattr(case, "exit_interview", None)
        ctx["can_decide_resignation"] = can_act_on_resignation(
            user, case.resignation, request=self.request,
        )
        ctx["can_issue_letters"] = can_see_all(user, request=self.request)
        ctx["letters"] = list(case.letters.select_related("document_type"))
        ctx["step_attachments_supported"] = step_attachments_supported()
        # sorted() keeps the steps prefetch warm (chained .order_by() re-queries).
        steps = sorted(case.steps.all(), key=lambda s: s.order)
        done = sum(1 for s in steps if s.status in [ClearanceStep.Status.DONE, ClearanceStep.Status.SKIPPED])
        ctx["progress_done"] = done
        ctx["progress_total"] = len(steps)
        ctx["progress_percent"] = int((done / len(steps)) * 100) if steps else 0
        ctx["steps"] = steps

        if "active_step_id" in kwargs:
            active_step_id = kwargs["active_step_id"]
        else:
            # This is compared against step.pk below — the previous fallback
            # used current_step_order (an order, not a pk), so the active
            # drawer/attachment form landed on the wrong step whenever pks
            # and orders diverged. Resolve from the prefetched steps.
            current = next(
                (s for s in steps if s.order == case.current_step_order), None,
            )
            active_step_id = current.pk if current else None
        ctx["active_step_id"] = active_step_id
        
        passed_step_form = kwargs.get("step_form")
        passed_attachment_form = kwargs.get("attachment_form")

        for step in steps:
            step.can_act = can_act_on_clearance_step(user, step, request=self.request)
            if passed_step_form and passed_step_form.instance.pk == step.pk:
                step.inline_form = passed_step_form
            else:
                step.inline_form = ClearanceStepActionForm(instance=step)
            
            if passed_attachment_form and active_step_id == step.pk:
                step.inline_attachment_form = passed_attachment_form
            else:
                step.inline_attachment_form = StepAttachmentForm()

        ctx["page_title"] = f"Offboarding · {case.employee.display_name}"
        return ctx


class ResignationDecideView(LoginRequiredMixin, View):
    """HR accepts / rejects (and can tune notice / LWD before accepting)."""

    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.resignation = get_object_or_404(
            Resignation.objects.select_related("employee"), pk=pk,
        )
        if not can_act_on_resignation(request.user, self.resignation, request=request):
            raise PermissionDenied("You can't decide on this resignation.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = ResignationDecideForm(instance=self.resignation)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = ResignationDecideForm(request.POST, instance=self.resignation)
        if form.is_valid():
            obj = form.save(commit=False)
            decision = form.cleaned_data["decision"]
            obj.status = decision
            obj.processed_by = request.user
            obj.processed_at = timezone.now()
            obj.process_note = form.cleaned_data.get("process_note", "") or ""
            obj.updated_by = request.user
            obj.save()  # triggers post_save signal → on accept: create case + LWD
            next_url = self._next_url()
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                target = self._pick_success_target(obj, next_url)
                response["HX-Redirect"] = target
                return response
            messages.success(request, f"Resignation {obj.get_status_display().lower()}.")
            return redirect(self._pick_success_target(obj, next_url))
        return self._render(request, form)

    def _render(self, request, form):
        next_url = self._next_url()
        action_url = reverse("offboarding:resignation_decide", args=[self.resignation.pk])
        if next_url:
            action_url = f"{action_url}?next={quote(next_url)}"
        ctx = {
            "form": form,
            "resignation": self.resignation,
            "action_url": action_url,
            "cancel_url": next_url or reverse("offboarding:case_list"),
            "drawer_title": f"Decide · {self.resignation.employee.display_name}",
            "page_title": "Decide resignation",
        }
        template = ("offboarding/resignation_decide_drawer.html"
                    if request.htmx else "offboarding/resignation_decide.html")
        return render(request, template, ctx)

    def _next_url(self):
        next_url = self.request.GET.get("next") or self.request.POST.get("next")
        if not next_url:
            return ""
        if url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return next_url
        return ""

    def _pick_success_target(self, resignation, next_url):
        if (resignation.status == Resignation.Status.ACCEPTED
                and getattr(resignation, "case", None)):
            return reverse("offboarding:case_detail", args=[resignation.case.pk])
        if next_url:
            return next_url
        return reverse("offboarding:case_list")


class ResignationHREditView(LoginRequiredMixin, View):
    """HR fix-typos before deciding."""

    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.resignation = get_object_or_404(Resignation, pk=pk)
        if not can_act_on_resignation(request.user, self.resignation, request=request):
            raise PermissionDenied("HR only.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = ResignationHREditForm(instance=self.resignation)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = ResignationHREditForm(request.POST, instance=self.resignation)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                return response
            messages.success(request, "Resignation updated.")
            return redirect("offboarding:case_list")
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "resignation": self.resignation,
            "action_url": reverse("offboarding:resignation_hr_edit",
                                   args=[self.resignation.pk]),
            "drawer_title": "Edit resignation",
            "page_title": "Edit resignation",
        }
        template = ("offboarding/resignation_hr_edit_drawer.html"
                    if request.htmx else "offboarding/resignation_hr_edit.html")
        return render(request, template, ctx)


class CaseCancelView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        case = get_object_or_404(OffboardingCase, pk=pk)
        if not can_see_all(request.user, request=request):
            raise PermissionDenied("HR only.")
        OffboardingCase.objects.filter(pk=case.pk).update(
            status=OffboardingCase.Status.CANCELLED,
            updated_by=request.user,
            updated_at=timezone.now(),
        )
        messages.success(request, "Case cancelled.")
        return redirect("offboarding:case_detail", pk=case.pk)


class IssueLettersView(LoginRequiredMixin, View):
    """Manual fallback when the final step's auto-issue didn't fire (e.g.
    a templated workflow without ``triggers_letters=True``)."""

    http_method_names = ["post"]

    def post(self, request, pk):
        case = get_object_or_404(OffboardingCase, pk=pk)
        if not can_see_all(request.user, request=request):
            raise PermissionDenied("HR only.")
        created = auto_issue_letters(case)
        if created:
            messages.success(request, f"Issued {len(created)} letter(s).")
        else:
            messages.info(request, "Letters were already issued (or no matching DocumentType).")
        return redirect("offboarding:case_detail", pk=case.pk)


# ---------------------------------------------------------------------------
# Exit interview (full page — long form)
# ---------------------------------------------------------------------------

class ExitInterviewView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.case = get_object_or_404(OffboardingCase, pk=pk)
        if not can_see_all(request.user, request=request):
            raise PermissionDenied("HR only.")
        self.instance = getattr(self.case, "exit_interview", None)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = ExitInterviewForm(instance=self.instance)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = ExitInterviewForm(request.POST, instance=self.instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.case = self.case
            obj.conducted_by = request.user
            obj.updated_by = request.user
            if not obj.pk:
                obj.added_by = request.user
            obj.save()
            messages.success(request, "Exit interview saved.")
            return redirect("offboarding:case_detail", pk=self.case.pk)
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "case": self.case,
            "instance": self.instance,
            "action_url": reverse("offboarding:exit_interview", args=[self.case.pk]),
            "page_title": "Exit interview",
        }
        return render(request, "offboarding/exit_interview_form.html", ctx)


# ---------------------------------------------------------------------------
# Asset return — add + edit + delete
# ---------------------------------------------------------------------------

class _AssetReturnEditBase(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]
    is_add: bool = True

    def dispatch(self, request, pk, row_pk=None, *args, **kwargs):
        self.case = get_object_or_404(OffboardingCase, pk=pk)
        if not can_see_all(request.user, request=request):
            raise PermissionDenied("Admins / HR only.")
        if row_pk is not None:
            self.row = get_object_or_404(AssetReturn, pk=row_pk, case=self.case)
        else:
            self.row = None
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = AssetReturnForm(instance=self.row)
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = AssetReturnForm(request.POST, instance=self.row)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.case = self.case
            obj.updated_by = request.user
            if not obj.pk:
                obj.added_by = request.user
            obj.save()
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = reverse(
                    "offboarding:case_detail", args=[self.case.pk],
                )
                return response
            messages.success(request, "Asset record saved.")
            return redirect("offboarding:case_detail", pk=self.case.pk)
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "case": self.case,
            "row": self.row,
            "action_url": self._action_url(),
            "drawer_title": ("Add asset" if self.is_add else "Edit asset"),
            "page_title": ("Add asset" if self.is_add else "Edit asset"),
        }
        template = ("offboarding/asset_return_drawer.html"
                    if request.htmx else "offboarding/asset_return_form.html")
        return render(request, template, ctx)

    def _action_url(self):
        if self.row:
            return reverse("offboarding:asset_return_edit",
                           args=[self.case.pk, self.row.pk])
        return reverse("offboarding:asset_return_add", args=[self.case.pk])


class AssetReturnAddView(_AssetReturnEditBase):
    is_add = True


class AssetReturnEditView(_AssetReturnEditBase):
    is_add = False


class AssetReturnDeleteView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk, row_pk):
        case = get_object_or_404(OffboardingCase, pk=pk)
        if not can_see_all(request.user, request=request):
            raise PermissionDenied("Admins / HR only.")
        AssetReturn.objects.filter(pk=row_pk, case=case).delete()
        messages.success(request, "Asset record removed.")
        return redirect("offboarding:case_detail", pk=case.pk)


# ---------------------------------------------------------------------------
# HR notes (append-only)
# ---------------------------------------------------------------------------

class NoteAddView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.case = get_object_or_404(OffboardingCase, pk=pk)
        if not can_see_all(request.user, request=request):
            raise PermissionDenied("Admins / HR only.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = OffboardingNoteForm()
        return self._render(request, form)

    def post(self, request, *args, **kwargs):
        form = OffboardingNoteForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.case = self.case
            obj.author = request.user
            obj.save()
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "orbis:drawer-close"
                response["HX-Redirect"] = reverse(
                    "offboarding:case_detail", args=[self.case.pk],
                )
                return response
            messages.success(request, "Note added.")
            return redirect("offboarding:case_detail", pk=self.case.pk)
        return self._render(request, form)

    def _render(self, request, form):
        ctx = {
            "form": form,
            "case": self.case,
            "action_url": reverse("offboarding:case_note_add", args=[self.case.pk]),
            "drawer_title": "Add note",
            "page_title": "Add note",
        }
        template = ("offboarding/note_add_drawer.html"
                    if request.htmx else "offboarding/note_add.html")
        return render(request, template, ctx)
