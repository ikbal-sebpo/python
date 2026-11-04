from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET
from django.views.generic import CreateView, ListView, UpdateView

from accounts.forms import (
    CustomUserCreationForm,
    CustomUserUpdateForm,
    EmailLoginForm,
)
from accounts.models import CustomUser, Department
from accounts.filters import apply_employee_filters, employee_filter_context, search_active
from accounts.services import check_prior_employment
from accounts.upload import generate_email, next_employee_id_seed
from core.pagination import PageSizeMixin
from accounts.permissions import (
    can_edit_all,
    can_edit_employee_basic_info,
    can_manage_groups,
    can_view_salary_history,
    visible_employees,
)
from employees.models import IdentificationNumber


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """User must be authenticated AND in an admin write-scope group."""

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and can_edit_all(user, request=self.request)


class HRRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Narrower than StaffRequiredMixin: the HR group (or a superuser) only —
    for screens that carry gross salary (accounts.permissions.
    can_view_salary_history)."""

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and can_view_salary_history(user, request=self.request)


class OrbisLoginView(auth_views.LoginView):
    template_name = "auth/login.html"
    authentication_form = EmailLoginForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Detect the silent-bounce case: user signed in successfully at Microsoft
        # but Orbis refused them (most often because their CustomUser row doesn't
        # exist yet and OIDC_CREATE_USER is False). They land back here with
        # /oidc/callback/ as the referer — show them why instead of a blank form.
        referer = self.request.META.get("HTTP_REFERER", "")
        ctx["sso_bounce"] = "/oidc/callback/" in referer or "/oidc/" in referer
        return ctx

    def post(self, request, *args, **kwargs):
        # SSO-only when it's configured: the page has no password form, and a
        # hand-crafted POST is refused too. Superusers' break-glass is
        # /admin/login/ (accounts.auth_backends.SuperuserPasswordBackend).
        if settings.SSO_ENABLED:
            return HttpResponseNotAllowed(["GET"])
        return super().post(request, *args, **kwargs)


class OrbisLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("login")


class UserListView(StaffRequiredMixin, PageSizeMixin, ListView):
    model = CustomUser
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25
    extra_context = {"page_title": "Users"}

    def get_queryset(self):
        # A ?q= search widens past the is_active clamp so a deactivated
        # user can still be looked up; the default list stays active-only.
        qs = visible_employees(
            self.request.user, request=self.request,
            include_separated=search_active(self.request),
        ).select_related(
            "department", "designation", "team", "job_location",
        )
        qs = apply_employee_filters(qs, self.request.user, self.request)
        return qs.order_by("first_name", "last_name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(employee_filter_context(self.request.user, self.request))
        return ctx


class UserCreateView(StaffRequiredMixin, CreateView):
    model = CustomUser
    form_class = CustomUserCreationForm
    template_name = "accounts/user_form.html"

    def get_success_url(self):
        return reverse("employee_detail", args=[self.object.pk])

    def form_valid(self, form):
        nid_number = form.cleaned_data.get("nid_number", "")
        # Check before creating our own IdentificationNumber row below, so we
        # don't self-match against the row we're about to insert.
        warning = check_prior_employment(nid_number)
        with transaction.atomic():
            response = super().form_valid(form)
            now = timezone.now()
            IdentificationNumber.objects.create(
                employee=self.object,
                id_type=IdentificationNumber.IdType.NID,
                id_number=nid_number,
                added_by=self.request.user, added_at=now,
                updated_by=self.request.user, updated_at=now,
            )
        messages.success(self.request, f"Created user {self.object.email}.")
        if warning:
            messages.warning(self.request, warning.as_message())
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Create user"
        ctx["submit_label"] = "Create user"
        ctx["cancel_url"] = reverse("user_list")
        ctx["is_create"] = True
        return ctx


@login_required
@require_GET
def suggest_new_hire_identity(request):
    """Live preview for Employee ID / Email as HR types a new hire's name on
    /users/create/ — same generation rules as the bulk-onboarding upload
    (accounts/upload.py), just called for a single name instead of a whole
    workbook. Read-only: never reserves/commits an Employee ID or Email,
    the actual uniqueness check happens at form-save time as usual."""
    if not can_edit_all(request.user, request=request):
        return JsonResponse({}, status=403)

    # This form has one freeform First Name box, not separate first/middle
    # inputs — a compound name like "Nazmun Nahar" is typed there as two
    # words. Split it the same way bulk-onboarding splits a Full Name
    # (first token = first name, any remaining tokens = middle name) so the
    # Data Solutions initials convention sees "Nazmun" + "Nahar" as two
    # initials instead of _slug() silently squashing them into one token.
    first_raw = (request.GET.get("first_name") or "").strip()
    first_tokens = first_raw.split()
    first = first_tokens[0] if first_tokens else ""
    middle = " ".join(first_tokens[1:])
    last = (request.GET.get("last_name") or "").strip()
    dept_id = request.GET.get("department")
    department = Department.objects.filter(pk=dept_id).first() if dept_id else None

    candidate_id = next_employee_id_seed() + 1
    existing_ids = set(
        CustomUser.objects.exclude(employee_id__isnull=True).exclude(employee_id="")
        .values_list("employee_id", flat=True)
    )
    while str(candidate_id) in existing_ids:
        candidate_id += 1

    email = ""
    email_exhausted = False
    if first and last:
        taken = {e.lower() for e in CustomUser.objects.values_list("email", flat=True)}
        try:
            email = generate_email(first, middle, last, department.name if department else "", taken=taken)
        except ValueError:
            email = ""
            email_exhausted = True

    return JsonResponse({"employee_id": str(candidate_id), "email": email, "email_exhausted": email_exhausted})


@login_required
@require_GET
def check_nid_prior_employment(request):
    """Live rehire/duplicate-NID check as HR types on /users/create/ — same
    soft-warning logic as the post-submit check in UserCreateView.form_valid,
    just surfaced earlier so a blocked-on-Email situation (see
    suggest_new_hire_identity) doesn't hide it until after a failed submit."""
    if not can_edit_all(request.user, request=request):
        return JsonResponse({}, status=403)

    nid_number = (request.GET.get("nid_number") or "").strip()
    warning = check_prior_employment(nid_number)
    return JsonResponse({"warning": warning.as_message() if warning else None})


class UserUpdateView(LoginRequiredMixin, UpdateView):
    model = CustomUser
    form_class = CustomUserUpdateForm
    template_name = "accounts/user_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not can_edit_employee_basic_info(self.request.user, obj, request=self.request):
            raise PermissionDenied("You can't edit this employee.")
        return obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["limited_scope"] = not can_edit_all(self.request.user, request=self.request)
        kwargs["editor"] = self.request.user
        kwargs["editor_request"] = self.request
        kwargs["can_manage_groups"] = can_manage_groups(self.request.user, request=self.request)
        return kwargs

    def _next_url(self):
        """Where to return to after save/cancel — honors an explicit ?next=
        (e.g. the user list's Edit link wants to come back to the list)
        falling back to this employee's own detail page, so editing basic
        info from the employee detail page keeps you on that same page."""
        candidate = self.request.POST.get("next") or self.request.GET.get("next")
        if candidate and url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure(),
        ):
            return candidate
        return None

    def get_success_url(self):
        return self._next_url() or reverse("employee_detail", args=[self.object.pk])

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Updated user {self.object.email}.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.display_name}"
        ctx["submit_label"] = "Save changes"
        ctx["next_url"] = self._next_url()
        ctx["cancel_url"] = ctx["next_url"] or reverse("employee_detail", args=[self.object.pk])
        return ctx
