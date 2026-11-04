"""Views for the policies app.

Manager-facing views (list, detail/acceptance, create/edit/version drawers,
publish, retire, reminders) are gated on ``services.can_manage_policies``.
Employee-facing views ("My policies to sign", sign, PDF) are gated per-row on
the caller's own acknowledgements.

Create/edit/version use the htmx-drawer pattern: an htmx GET returns the drawer
fragment, a non-htmx GET returns a full-page fallback. A successful POST returns
``204`` + ``HX-Redirect`` (htmx) or a normal redirect, so the page reloads with
fresh data; a failing POST re-renders the form with errors.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from accounts.permissions import effective_department_id
from core.pagination import PageSizeMixin

from policies import services
from policies.forms import PolicyForm, PolicyVersionUploadForm, SignForm
from policies.models import Policy, PolicyAcknowledgement

User = get_user_model()


# ---------------------------------------------------------------------------
# Mixins / helpers
# ---------------------------------------------------------------------------

class PolicyManagerRequiredMixin(LoginRequiredMixin):
    """Gate a view on ``can_manage_policies`` (Policy Managers / Admins)."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not services.can_manage_policies(
            request.user, request,
        ):
            raise PermissionDenied("You are not a policy manager.")
        return super().dispatch(request, *args, **kwargs)


def _viewer_watermark(user) -> str:
    """Identity + timestamp baked into the PDF viewer's canvas pages — see
    policy-pdf-viewer.js::drawWatermark. Deterrent for screenshots/photos,
    not prevention (nothing on the web can prevent those)."""
    return f"{user.email} · {timezone.now():%Y-%m-%d %H:%M}"


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _hx_redirect(url: str) -> HttpResponse:
    """204 No Content with HX-Redirect — htmx performs a full client-side
    navigation so the page reloads with fresh data."""
    resp = HttpResponse(status=204)
    resp["HX-Redirect"] = url
    return resp


# ---------------------------------------------------------------------------
# Manager — list + detail/acceptance
# ---------------------------------------------------------------------------

class PolicyListView(PolicyManagerRequiredMixin, PageSizeMixin, ListView):
    template_name = "policies/policy_list.html"
    context_object_name = "policies"
    paginate_by = 25

    def get_queryset(self):
        # Counts are annotated in SQL: the signed_count/outstanding_count
        # model properties issue a COUNT query per row (and would ignore a
        # prefetch anyway, since .filter() bypasses the prefetch cache).
        # services.visible_policies() already scopes to the caller's own
        # department unless they're privileged (can_see_all_departments).
        return (
            services.visible_policies(self.request.user, self.request)
            .annotate(
                signed_total=Count(
                    "acknowledgements",
                    filter=Q(acknowledgements__status=PolicyAcknowledgement.Status.SIGNED),
                ),
                outstanding_total=Count(
                    "acknowledgements",
                    filter=~Q(acknowledgements__status=PolicyAcknowledgement.Status.SIGNED),
                ),
            )
            .order_by("-added_at")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["create_form"] = PolicyForm(user=self.request.user, request=self.request)
        return ctx


class PolicyDetailView(PolicyManagerRequiredMixin, DetailView):
    model = Policy
    template_name = "policies/policy_detail.html"
    context_object_name = "policy"
    pk_url_kwarg = "pk"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not services.can_access_policy(self.request.user, obj, self.request):
            raise PermissionDenied("This policy belongs to a different department.")
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        policy = self.object
        acks = (
            policy.acknowledgements
            .select_related("employee", "version")
            .order_by("status", "employee__first_name", "employee__last_name")
        )
        ctx["acknowledgements"] = acks
        ctx["outstanding"] = [a for a in acks if a.is_outstanding]
        ctx["signed"] = [a for a in acks if a.is_signed]
        ctx["versions"] = policy.versions.order_by("-version")
        # "Who must sign" — the live targeting, visible *before* circulation
        # so a manager sees exactly how many people Circulate will reach.
        # Computed by the same resolver assign_adherents uses, never a
        # second rule.
        ctx["target_groups"] = list(policy.target_groups.order_by("name"))
        ctx["custom_adherents"] = list(
            policy.custom_adherents.filter(is_active=True).order_by("first_name", "last_name")
        )
        ctx["target_count"] = len(services.target_adherent_ids(policy))
        ctx["edit_form"] = PolicyForm(instance=policy, user=self.request.user, request=self.request)
        ctx["version_form"] = PolicyVersionUploadForm()
        return ctx


# ---------------------------------------------------------------------------
# Manager — create / edit (htmx drawer)
# ---------------------------------------------------------------------------

def _selected_adherents_context(form):
    """``{id: {name, email}}`` for the custom-adherent picker's initial chips.

    On a bound (re-rendered-after-error) form, read back the submitted ids
    rather than the instance, so a failed save doesn't drop the manager's
    picks. Both branches intersect with the field's own queryset (already
    scoped to *eligible* employees) so a stale/tampered id can't surface a
    chip the field would reject on save.
    """
    field = form.fields["custom_adherents"]
    if form.is_bound:
        qs = field.queryset.filter(pk__in=form.data.getlist("custom_adherents"))
    elif form.instance.pk:
        qs = field.queryset.filter(
            pk__in=form.instance.custom_adherents.values_list("pk", flat=True)
        )
    else:
        qs = field.queryset.none()
    return {
        str(u.pk): {"name": u.get_full_name() or u.email, "email": u.email}
        for u in qs
    }


class _PolicyFormViewBase(PolicyManagerRequiredMixin, View):
    drawer_title = ""

    def _render_form(self, request, form, *, status=200):
        ctx = {
            "form": form,
            "action_url": self.action_url,
            "drawer_title": self.drawer_title,
            "initial_adherents": _selected_adherents_context(form),
            "adherent_search_url": reverse("policies:adherents_search"),
            "adherent_select_all_url": reverse("policies:adherents_select_all"),
            "policy_pk": getattr(self, "policy", None) and self.policy.pk,
        }
        if request.htmx:
            return render(request, "policies/_drawer_policy.html", ctx, status=status)
        return render(request, "policies/policy_form_page.html", ctx, status=status)


class PolicyCreateView(_PolicyFormViewBase):
    drawer_title = "New policy"

    @property
    def action_url(self):
        return reverse("policies:create")

    def get(self, request, *args, **kwargs):
        return self._render_form(request, PolicyForm(user=request.user, request=request))

    def post(self, request, *args, **kwargs):
        form = PolicyForm(request.POST, user=request.user, request=request)
        if not form.is_valid():
            return self._render_form(request, form, status=400)
        policy = form.save(commit=False)
        policy.added_by = request.user
        policy.updated_by = request.user
        policy.save()
        form.save_m2m()
        messages.success(request, f"Policy {policy.key} created as Draft. Upload a PDF, then publish.")
        url = reverse("policies:detail", args=[policy.pk])
        return _hx_redirect(url) if request.htmx else redirect(url)


class PolicyEditView(_PolicyFormViewBase):
    drawer_title = "Edit policy"

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, self.policy, request):
            raise PermissionDenied("This policy belongs to a different department.")

    @property
    def action_url(self):
        return reverse("policies:edit", args=[self.policy.pk])

    def get(self, request, *args, **kwargs):
        return self._render_form(request, PolicyForm(instance=self.policy, user=request.user, request=request))

    def post(self, request, *args, **kwargs):
        form = PolicyForm(request.POST, instance=self.policy, user=request.user, request=request)
        if not form.is_valid():
            return self._render_form(request, form, status=400)
        policy = form.save(commit=False)
        policy.updated_by = request.user
        policy.save()
        form.save_m2m()
        # If already circulated, top up coverage for any newly-targeted
        # adherents. An Active-but-not-yet-circulated policy has no
        # adherents yet — that's what Circulate is for.
        if policy.is_circulated:
            services.assign_adherents(policy, actor=request.user)
        messages.success(request, f"Policy {policy.key} updated.")
        url = reverse("policies:detail", args=[policy.pk])
        return _hx_redirect(url) if request.htmx else redirect(url)


class PolicyOwnerOptionsView(PolicyManagerRequiredMixin, View):
    """htmx: re-render the Owner <select>'s <option>s scoped to a department.

    Fired by the Department select's change event. Reuses
    ``effective_department_id`` (the same helper other department-scoped
    dropdowns use) so a locked (non-privileged) manager can't widen the owner
    list to a department they can't otherwise pick, even by editing the
    querystring by hand.
    """

    def get(self, request, *args, **kwargs):
        department_id = effective_department_id(request.user, request)
        owners = User.objects.filter(is_active=True)
        if department_id:
            owners = owners.filter(department_id=department_id)
        owners = owners.order_by("first_name", "last_name")
        return render(request, "policies/_owner_options.html", {"owners": owners})


class PolicyAdherentSearchView(PolicyManagerRequiredMixin, View):
    """htmx search-as-you-type results for the custom-adherents picker.

    ``?policy=<pk>`` scopes eligibility to that policy (excludes its existing
    adherents/signers); omitted for the create-policy drawer, where no policy
    exists yet.
    """

    def get(self, request, *args, **kwargs):
        policy = None
        policy_pk = request.GET.get("policy")
        if policy_pk:
            policy = get_object_or_404(Policy, pk=policy_pk)
            if not services.can_access_policy(request.user, policy, request):
                raise PermissionDenied("This policy belongs to a different department.")
        q = request.GET.get("q", "")
        results = services.search_eligible_custom_adherents(policy, q)
        return render(
            request, "policies/_adherent_search_results.html",
            {"results": results, "q": q.strip()},
        )


class PolicyAdherentSelectAllView(PolicyManagerRequiredMixin, View):
    """JSON payload of every currently-eligible custom adherent, for the
    picker's "Select all remaining eligible" bulk action.

    Typically used after a policy has been Active for a while: most of the
    org is already covered via target_groups, so the eligible set is just
    the new joiners since publish — small enough to bulk-add in one click
    instead of searching each one individually. Capped at ``MAX`` so a
    draft policy (where nobody has signed yet, so *everyone* is "eligible")
    can't dump the whole company into the picker as chips.
    """

    MAX = 500

    def get(self, request, *args, **kwargs):
        policy = None
        policy_pk = request.GET.get("policy")
        if policy_pk:
            policy = get_object_or_404(Policy, pk=policy_pk)
            if not services.can_access_policy(request.user, policy, request):
                raise PermissionDenied("This policy belongs to a different department.")
        qs = services.eligible_custom_adherents(policy)
        total = qs.count()
        people = [
            {"id": str(u.pk), "name": u.get_full_name() or u.email, "email": u.email}
            for u in qs[: self.MAX]
        ]
        return JsonResponse({"count": total, "truncated": total > self.MAX, "people": people})


class PolicyVersionUploadView(PolicyManagerRequiredMixin, View):
    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, self.policy, request):
            raise PermissionDenied("This policy belongs to a different department.")

    def _render_form(self, request, form, *, status=200):
        ctx = {
            "form": form,
            "action_url": reverse("policies:upload_version", args=[self.policy.pk]),
            "drawer_title": f"Upload new version — {self.policy.key}",
            "policy": self.policy,
        }
        if request.htmx:
            return render(request, "policies/_drawer_version.html", ctx, status=status)
        return render(request, "policies/policy_form_page.html", ctx, status=status)

    def get(self, request, *args, **kwargs):
        return self._render_form(request, PolicyVersionUploadForm())

    def post(self, request, *args, **kwargs):
        form = PolicyVersionUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return self._render_form(request, form, status=400)
        version = services.add_policy_version(
            self.policy,
            pdf=form.cleaned_data["pdf"],
            change_note=form.cleaned_data.get("change_note", ""),
            requires_reack=form.cleaned_data.get("requires_reack", True),
            actor=request.user,
        )
        messages.success(request, f"Uploaded {self.policy.key} v{version.version}.")
        url = reverse("policies:detail", args=[self.policy.pk])
        return _hx_redirect(url) if request.htmx else redirect(url)


# ---------------------------------------------------------------------------
# Manager — lifecycle actions (POST only)
# ---------------------------------------------------------------------------

class PolicyActivateView(PolicyManagerRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, policy, request):
            raise PermissionDenied("This policy belongs to a different department.")
        if not policy.is_draft:
            messages.error(
                request,
                f"{policy.key} is {policy.get_status_display()} — only a Draft "
                "policy can be activated.",
            )
            return redirect("policies:detail", pk=policy.pk)
        if policy.current_version is None:
            messages.error(request, "Upload a PDF version before activating.")
            return redirect("policies:detail", pk=policy.pk)
        services.activate_policy(policy, actor=request.user)
        messages.success(
            request,
            f"Activated {policy.key}. Circulate it to notify employees when you're ready.",
        )
        return redirect("policies:detail", pk=policy.pk)


class PolicyCirculateView(PolicyManagerRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, policy, request):
            raise PermissionDenied("This policy belongs to a different department.")
        if not policy.is_active or policy.is_circulated:
            messages.error(
                request,
                f"{policy.key} can't be circulated right now "
                f"({policy.get_status_display()}"
                f"{', already circulated' if policy.is_circulated else ''}).",
            )
            return redirect("policies:detail", pk=policy.pk)
        if policy.current_version is None:
            messages.error(request, "Upload a PDF version before circulating.")
            return redirect("policies:detail", pk=policy.pk)
        created = services.circulate_policy(policy, actor=request.user)
        messages.success(
            request,
            f"Circulated {policy.key}. {len(created)} adherent(s) notified to sign.",
        )
        return redirect("policies:detail", pk=policy.pk)


class PolicyRetireView(PolicyManagerRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, policy, request):
            raise PermissionDenied("This policy belongs to a different department.")
        services.retire_policy(policy, actor=request.user)
        messages.success(request, f"Retired {policy.key}.")
        return redirect("policies:detail", pk=policy.pk)


class PolicyResetView(PolicyManagerRequiredMixin, View):
    """Wipe a *Retired* policy's acknowledgement/event data and return it to
    Draft — the UI counterpart of ``manage.py reset_policy_signing_data``.

    Exists for the "circulated to the wrong audience, then retired" case:
    retiring leaves every pending row in place, so the detail page keeps
    reporting them as outstanding and the custom-adherent picker hides
    everyone already covered. Retired-only, checked here and not just in
    the template (same defense-in-depth as activate/circulate).
    """

    def post(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, policy, request):
            raise PermissionDenied("This policy belongs to a different department.")
        if not policy.is_retired:
            messages.error(
                request,
                f"Only a Retired policy can be reset ({policy.key} is "
                f"{policy.get_status_display()}). Retire it first.",
            )
            return redirect("policies:detail", pk=policy.pk)
        acks, events = services.reset_signing_data(Policy.objects.filter(pk=policy.pk))
        messages.success(
            request,
            f"Reset {policy.key}: removed {acks} acknowledgement(s) and {events} "
            f"event(s). It is back in Draft — re-check \"Who must sign\" before circulating.",
        )
        return redirect("policies:detail", pk=policy.pk)


class SendRemindersView(PolicyManagerRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        if not services.can_access_policy(request.user, policy, request):
            raise PermissionDenied("This policy belongs to a different department.")
        reminded = services.remind_outstanding(policy, actor=request.user)
        messages.success(request, f"Reminder sent to {len(reminded)} outstanding adherent(s).")
        return redirect("policies:detail", pk=policy.pk)


# ---------------------------------------------------------------------------
# Employee — my policies + sign + PDF
# ---------------------------------------------------------------------------

class MyPoliciesView(LoginRequiredMixin, TemplateView):
    template_name = "policies/my_policies.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        acks = list(services.my_acknowledgements(self.request.user))
        # A retired policy's pending rows are inert — reminders stop and the
        # sign page refuses it — but until 2026-10-28 they still appeared
        # under "To sign" for every employee. Signed history stays listed.
        ctx["outstanding"] = [a for a in acks if a.is_outstanding and a.policy.is_active]
        ctx["signed"] = [a for a in acks if a.is_signed]
        ctx["total_count"] = len(acks)
        ctx["page_title"] = "My Policies"
        return ctx


class PolicySignView(LoginRequiredMixin, View):
    template_name = "policies/sign.html"

    def _load(self, request, pk):
        policy = get_object_or_404(Policy, pk=pk)
        ack = policy.acknowledgements.filter(employee=request.user).first()
        is_manager = services.can_manage_policies(request.user, request)
        if ack is None and not is_manager:
            raise PermissionDenied("This policy is not assigned to you.")
        return policy, ack, is_manager

    def get(self, request, *args, **kwargs):
        policy, ack, is_manager = self._load(request, kwargs["pk"])
        return render(request, self.template_name, {
            "policy": policy,
            "acknowledgement": ack,
            "is_manager": is_manager,
            "current_version": policy.current_version,
            "form": SignForm(),
            "page_title": policy.title,
            "watermark": _viewer_watermark(request.user),
        })

    def post(self, request, *args, **kwargs):
        policy, ack, is_manager = self._load(request, kwargs["pk"])
        current = policy.current_version
        if ack is None:
            raise PermissionDenied("This policy is not assigned to you.")
        if current is None or not policy.is_active:
            messages.error(request, "This policy isn't open for signing right now.")
            return redirect("policies:sign", pk=policy.pk)

        form = SignForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {
                "policy": policy, "acknowledgement": ack, "is_manager": is_manager,
                "current_version": current, "form": form, "page_title": policy.title,
            })

        services.sign(
            ack, version=current, ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""), actor=request.user,
        )
        messages.success(request, f"Thank you — your acknowledgement of {policy.key} is recorded.")
        return redirect("policies:my_policies")


class PolicyPdfPreviewView(LoginRequiredMixin, View):
    """Same-origin, no-download PDF preview — the manager-facing counterpart of
    the sign page's viewer. Renders ``policies/preview.html``, which loads
    ``policy-pdf-viewer.js`` against ``PolicyPdfView`` exactly like the sign
    page does, instead of linking straight to the raw PDF (which would open
    the browser's native, right-click-savable PDF viewer in a new tab)."""

    def get(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        is_manager = services.can_manage_policies(request.user, request)
        is_adherent = policy.acknowledgements.filter(employee=request.user).exists()
        if not (is_manager or is_adherent):
            raise PermissionDenied("This policy is not assigned to you.")

        version = policy.current_version
        v_param = request.GET.get("v")
        if v_param and is_manager:
            version = get_object_or_404(policy.versions, version=v_param)
        if version is None or not version.pdf:
            raise Http404("No PDF available for this policy.")

        return render(request, "policies/preview.html", {
            "policy": policy,
            "version": version,
            "page_title": f"{policy.key} — v{version.version}",
            "watermark": _viewer_watermark(request.user),
        })


class PolicyPdfView(LoginRequiredMixin, View):
    """Serve a policy version's PDF inline (for the embedded viewer + download).

    Access: managers, or an employee who is an adherent of the policy. Defaults
    to the current version; ``?v=<n>`` serves a specific version (managers only).
    ``?download=1`` (forces ``Content-Disposition: attachment``) is also
    managers-only — adherents always get ``inline``, so they can't bypass the
    sign page's download deterrents by editing the querystring themselves."""

    def get(self, request, *args, **kwargs):
        policy = get_object_or_404(Policy, pk=kwargs["pk"])
        is_manager = services.can_manage_policies(request.user, request)
        is_adherent = policy.acknowledgements.filter(employee=request.user).exists()
        if not (is_manager or is_adherent):
            raise PermissionDenied("This policy is not assigned to you.")

        version = policy.current_version
        v_param = request.GET.get("v")
        if v_param and is_manager:
            version = get_object_or_404(policy.versions, version=v_param)
        if version is None or not version.pdf:
            raise Http404("No PDF available for this policy.")

        download = is_manager and request.GET.get("download") == "1"
        resp = FileResponse(version.pdf.open("rb"), content_type="application/pdf")
        disposition = "attachment" if download else "inline"
        resp["Content-Disposition"] = f'{disposition}; filename="{policy.key}_v{version.version}.pdf"'
        return resp
