import uuid
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_GET
from django.views.generic import DetailView, ListView, TemplateView

from accounts.models import CustomUser
from accounts.filters import (
    apply_employee_filters, effective_filter_ids, employee_filter_context, employee_search_q,
    search_active,
)
from core.approvals import night_shift_inbox_pks
from core.pagination import PageSizeMixin, paginate_list
from accounts.permissions import (
    can_approve_dedicated_night_shift,
    can_cancel_dedicated_night_shift,
    dedicated_night_shift_team_pks,
    can_edit_gross_salary,
    can_edit_dedicated_night_shift,
    can_confirm_tax_declarations_for_others,
    can_manage_salary_loans,
    can_edit_all,
    can_edit_employee_basic_info,
    can_edit_employee_profile,
    can_edit_roster_pattern,
    can_manage_night_shift_consent,
    can_manage_profile_self_service,
    can_request_dedicated_night_shift,
    can_see_all_departments,
    can_set_own_night_shift_consent,
    can_view_employee,
    can_view_salary_history,
    direct_report_pks,
    editable_section_slugs,
    effective_department_id,
    has_dedicated_night_shift_approval_scope,
    has_managed_scope,
    managed_scope_pks,
    visible_departments,
    visible_teams,
    visible_employees,
    visible_employee_pks,
)
from audit.capture import note
from dynamic_menu.decorators import dynamic_allowed_users
from employees.exports import build_employee_export
from employees.forms import (
    AvatarForm, DedicatedNightShiftAssignmentForm, DedicatedNightShiftRequestForm,
)
from employees.org_chart import build_org_tree
from employees.models import (
    City,
    DedicatedNightShiftAssignment,
    DedicatedNightShiftAssignmentEvent,
    NightShiftConsent,
    ProfileSelfServiceEvent,
    State,
    UnionWard,
)
from employees.sections import SECTIONS, SectionSpec, ordered_sections
from employees.services import (
    current_night_shift_consent_period,
    get_or_create_night_shift_consent,
    qualifies_for_night_shift_consent,
    record_dedicated_night_shift_event,
    record_night_shift_consent_decision,
    reports_to_candidates,
    set_profile_self_service,
)
from payroll.models import EmployeeBankAccount


# Map section slug → related-attribute name on CustomUser
_SINGLETON_ATTR = {
    "jobinfo": "job_info",
    "personal": "personal_info",
    "family": "family_info",
    "legal": "legal_issue",
}
_REPEATING_ATTR = {
    "address": "addresses",
    "education": "educations",
    "contact": "contacts",
    "nominee": "nominees",
    "email": "extra_emails",
    "id-number": "id_numbers",
    "reference": "references",
    "certification": "certifications",
    "vaccination": "vaccinations",
    "image": "images",
    "bank-account": "bank_accounts",
    "tax-declaration": "tax_declarations",
    "designation-history": "designation_history",
}


# ---------------------------------------------------------------------------
# List + detail
# ---------------------------------------------------------------------------

class EmployeeListView(PageSizeMixin, LoginRequiredMixin, ListView):
    model = CustomUser
    template_name = "employees/employee_list.html"
    context_object_name = "employees"
    paginate_by = 25
    extra_context = {"page_title": "Employees"}

    def get_queryset(self):
        # A ?q= search widens past the is_active clamp so a deactivated
        # employee can still be looked up; the default list stays active-only.
        qs = (
            visible_employees(
                self.request.user, request=self.request,
                include_separated=search_active(self.request),
            )
            .select_related(
                "department", "designation", "team", "subteam",
                "job_location", "work_location", "job_info__reports_to",
            )
        )
        qs = apply_employee_filters(qs, self.request.user, self.request)
        return qs.order_by("first_name", "last_name")

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx":
            return build_employee_export(self.get_queryset())
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(employee_filter_context(self.request.user, self.request))
        ctx["can_bulk_upload"] = has_managed_scope(self.request.user, request=self.request)
        ctx["can_manage_profile_self_service"] = can_manage_profile_self_service(
            self.request.user, request=self.request,
        )
        return ctx


class EmployeeDetailView(LoginRequiredMixin, DetailView):
    model = CustomUser
    template_name = "employees/employee_detail.html"
    context_object_name = "employee"

    def get_queryset(self):
        return (
            CustomUser.objects
            .select_related(
                "department", "designation", "team", "subteam",
                "job_location", "work_location", "access_level",
            )
            .prefetch_related(
                "addresses__city", "addresses__state", "addresses__country",
                "addresses__union_ward",
                "educations",
                "contacts__relation_with_emergency_contact",
                "nominees__relation_with_nominee",
                "extra_emails", "id_numbers",
                "references__relation_with_reference",
                "certifications",
                "vaccinations__vaccine", "vaccinations__dose",
                "images",
                "bank_accounts__bank", "bank_accounts__branch",
                "tax_declarations__tax_year", "tax_declarations__category", "tax_declarations__submitted_by",
            )
        )

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not can_view_employee(self.request.user, obj, request=self.request):
            raise PermissionDenied("You can't view this employee.")
        return obj

    def get_context_data(self, **kwargs):
        from roster.models import Roster as RosterModel  # avoid circular import
        from leave.models import LeaveApplication

        ctx = super().get_context_data(**kwargs)
        employee = ctx["employee"]
        user = self.request.user

        sections = ordered_sections()
        ctx["sections"] = [_section_context(employee, spec) for spec in sections]

        # Per-section edit gating, computed once. Templates use
        # `{% if spec.slug in editable_section_slugs %}` for O(1) lookups
        # instead of calling can_edit_employee_profile per section.
        ctx["editable_section_slugs"] = editable_section_slugs(
            user, employee, sections, request=self.request,
        )
        ctx["can_edit_basic_info"] = can_edit_employee_basic_info(
            user, employee, request=self.request,
        )
        ctx["can_manage_profile_self_service"] = can_manage_profile_self_service(
            user, request=self.request,
        )
        # Payroll-HR / Payroll Admins may confirm a tax declaration on the
        # employee's behalf (sections/_tax_declaration.html); the employee's
        # own button keys off employee.pk == request.user.pk instead.
        ctx["can_confirm_tax_declarations_for_others"] = can_confirm_tax_declarations_for_others(
            user, request=self.request,
        )
        # Salary loans (docs/SALARY_LOAN_PLAN.md §5.3) — read-only card for
        # the employee themself and for whoever manages loans; HR edits from
        # /payroll/loans/, never here.
        ctx["can_manage_salary_loans"] = can_manage_salary_loans(user, request=self.request)
        # Access history card (audit app) — readers of the access log only.
        from accounts.permissions import can_view_access_log
        ctx["can_view_access_log"] = can_view_access_log(user, request=self.request)
        if ctx["can_view_access_log"]:
            from audit.models import AccessLog
            ctx["access_history"] = list(
                AccessLog.objects.filter(subject=employee).pages()
                .exclude(actor=employee).select_related("actor")[:10]
            )
        if ctx["can_manage_salary_loans"] or employee.pk == user.pk:
            from payroll.loans import loan_balances
            from payroll.models import SalaryLoan
            loans = list(
                SalaryLoan.objects.filter(employee=employee)
                .exclude(status=SalaryLoan.Status.CANCELLED)
                .select_related("first_deduction_period")
            )
            balances = loan_balances(loans)
            for loan in loans:
                loan.balance = balances[loan.pk]
            ctx["salary_loans"] = loans
        else:
            ctx["salary_loans"] = []

        weekday_labels = {
            0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
            4: "Friday", 5: "Saturday", 6: "Sunday",
        }
        pattern_rows_by_wd = {
            r.weekday: r for r in
            RosterModel.objects.filter(employee=employee).select_related("shift_type", "shift_attribute")
        }
        ctx["pattern_rows"] = [
            {
                "weekday": wd,
                "label": weekday_labels[wd],
                "shift_type": pattern_rows_by_wd[wd].shift_type if wd in pattern_rows_by_wd else None,
                "shift_attribute": pattern_rows_by_wd[wd].shift_attribute if wd in pattern_rows_by_wd else None,
            }
            for wd in range(7)
        ]
        ctx["can_edit_pattern"] = can_edit_roster_pattern(
            user, employee, request=self.request,
        )

        # Leave history — last 5 applications, visible to the applicant
        # themselves and anyone with view scope on this employee (already
        # enforced via can_view_employee, so always show when we got here).
        ctx["can_view_leaves"] = True
        ctx["recent_leaves"] = list(
            LeaveApplication.objects
            .filter(employee=employee)
            .select_related("leave_type")
            .order_by("-applied_at")[:5]
        )

        # Separation card (view-only; offboarding-owned). Returns {} when the
        # employee has no resignation and no LWD → template renders nothing.
        from offboarding.services import build_separation_card_context
        ctx["separation_card"] = build_separation_card_context(
            employee, request=self.request,
        )

        # Dedicated Night Shift assignments — last 5, visible to anyone who
        # can already view this profile (can_view_employee already gated
        # access to get here); the "Request" action itself is separately
        # gated by can_request_dedicated_night_shift. Each row also carries
        # its own can_edit flag (TL+ bar for both PENDING and APPROVED as
        # of 2026-09-15 — an approved row a below-Manager editor saves gets
        # demoted back to PENDING by DedicatedNightShiftEditView rather
        # than barred here; see can_edit_dedicated_night_shift) so the
        # template can show/hide
        # a per-row Edit link the same way editable_section_slugs does for
        # every other section.
        dedicated_night_shifts = list(
            DedicatedNightShiftAssignment.objects.filter(employee=employee).order_by("-start_date")[:5]
        )
        for assignment in dedicated_night_shifts:
            assignment.can_edit = can_edit_dedicated_night_shift(user, assignment, request=self.request)
        ctx["dedicated_night_shifts"] = dedicated_night_shifts
        ctx["can_request_dedicated_night_shift"] = can_request_dedicated_night_shift(
            user, employee, request=self.request,
        )

        ctx["can_view_service_book"] = _can_view_service_book(user, employee)
        # The gross-salary page link on the Service Book card (payroll-HR
        # pair + Payroll Admins) — the pair's route to "Revise salary".
        ctx["can_edit_gross_salary"] = can_edit_gross_salary(user, request=self.request)

        ctx["page_title"] = employee.display_name
        return ctx


def _section_context(employee, spec: SectionSpec) -> dict:
    if spec.is_singleton:
        obj = getattr(employee, _SINGLETON_ATTR[spec.slug], None)
        return {"spec": spec, "object": obj, "rows": []}
    rows = list(getattr(employee, _REPEATING_ATTR[spec.slug]).all())
    return {"spec": spec, "object": None, "rows": rows}


# ---------------------------------------------------------------------------
# Section CRUD (htmx-aware + full-page fallback)
# ---------------------------------------------------------------------------

def _get_spec(slug: str) -> SectionSpec:
    try:
        return SECTIONS[slug]
    except KeyError as exc:
        raise Http404(f"Unknown section: {slug}") from exc


def _render_section_card(request, employee, spec: SectionSpec) -> str:
    ctx = _section_context(employee, spec)
    ctx["employee"] = employee
    ctx["oob"] = True
    # Without this, the OOB-swapped card loses the per-section edit-button
    # gating that the parent template provided.
    ctx["editable_section_slugs"] = editable_section_slugs(
        request.user, employee, [spec], request=request,
    )
    ctx["can_confirm_tax_declarations_for_others"] = can_confirm_tax_declarations_for_others(
        request.user, request=request,
    )
    return render_to_string("employees/_section_card.html", ctx, request=request)


def _drawer_title(spec: SectionSpec, employee, row=None) -> str:
    if spec.is_singleton:
        return spec.label
    verb = "Edit" if row else "Add"
    return f"{verb} {spec.singular_label.lower()}"


class SectionEditView(LoginRequiredMixin, View):
    """Handles singleton-edit, repeating-add, and repeating-edit-row.

    Permission: any authenticated user can hit the URL; the section is
    saveable only if `can_edit_employee_profile(user, target, section=...)`
    is True. Self-edit is allowed for personal-data sections; JobInfo is
    HR/manager-only.
    """

    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, section, row_pk=None, *args, **kwargs):
        self.employee = get_object_or_404(CustomUser, pk=pk)
        self.spec = _get_spec(section)
        self.row_pk = row_pk
        if self.spec.is_singleton and row_pk is not None:
            raise Http404("Singleton section doesn't take a row_pk.")
        if not can_edit_employee_profile(
            request.user, self.employee,
            section=self.spec.model.__name__, request=request,
        ):
            raise PermissionDenied("You can't edit this section.")
        return super().dispatch(request, *args, **kwargs)

    def _get_instance(self):
        if self.spec.is_singleton:
            obj, _ = self.spec.model.objects.get_or_create(employee=self.employee)
            return obj
        if self.row_pk is not None:
            return get_object_or_404(self.spec.model, pk=self.row_pk, employee=self.employee)
        return None

    def _action_url(self):
        if self.spec.is_singleton:
            return reverse("section_edit", args=[self.employee.pk, self.spec.slug])
        if self.row_pk is not None:
            return reverse("section_edit_row", args=[self.employee.pk, self.spec.slug, self.row_pk])
        return reverse("section_add", args=[self.employee.pk, self.spec.slug])

    def get(self, request, *args, **kwargs):
        instance = self._get_instance()
        form = self.spec.form(instance=instance)
        row = instance if (not self.spec.is_singleton and self.row_pk) else None
        title = _drawer_title(self.spec, self.employee, row)
        return self._render(request, form, title)

    def post(self, request, *args, **kwargs):
        instance = self._get_instance()
        form = self.spec.form(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.employee = self.employee
            if hasattr(obj, "updated_by"):
                obj.updated_by = request.user
                if not obj.pk:
                    obj.added_by = request.user
            obj.save()
            form.save_m2m()
            return self._success(request)
        row = instance if (not self.spec.is_singleton and self.row_pk) else None
        title = _drawer_title(self.spec, self.employee, row)
        return self._render(request, form, title)

    def _render(self, request, form, title):
        ctx = {
            "form": form, "spec": self.spec, "employee": self.employee,
            "action_url": self._action_url(),
            "drawer_title": title, "page_title": f"{title} — {self.employee.display_name}",
        }
        template = "employees/_drawer.html" if request.htmx else "employees/section_form_page.html"
        return render(request, template, ctx)

    def _success(self, request):
        if request.htmx:
            card_html = _render_section_card(request, self.employee, self.spec)
            response = HttpResponse(card_html)
            # The drawer form targets #drawer-body (so validation errors
            # re-render in place); on success redirect this one swap to the
            # section card on the page instead. See templates/employees/_drawer.html.
            response["HX-Retarget"] = f"#{self.spec.dom_id}"
            response["HX-Reswap"] = "outerHTML"
            response["HX-Trigger"] = "orbis:drawer-close"
            return response
        messages.success(request, f"{self.spec.label} saved.")
        return redirect("employee_detail", pk=self.employee.pk)


class SectionDeleteView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, pk, section, row_pk, *args, **kwargs):
        self.employee = get_object_or_404(CustomUser, pk=pk)
        self.spec = _get_spec(section)
        if self.spec.is_singleton:
            raise Http404("Singleton sections are not deletable.")
        self.row = get_object_or_404(self.spec.model, pk=row_pk, employee=self.employee)
        if not can_edit_employee_profile(
            request.user, self.employee,
            section=self.spec.model.__name__, request=request,
        ):
            raise PermissionDenied("You can't delete this row.")
        # A confirmed tax declaration (EmployeeTaxDeclaration.is_locked) is
        # frozen for the year — payroll calculates on it. Admin-only undo.
        if getattr(self.row, "is_locked", False):
            raise PermissionDenied("This row is locked and can't be deleted.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        ctx = {
            "spec": self.spec, "employee": self.employee, "row": self.row,
            "action_url": reverse("section_delete_row",
                                   args=[self.employee.pk, self.spec.slug, self.row.pk]),
            "drawer_title": f"Delete {self.spec.singular_label.lower()}",
            "page_title": f"Delete {self.spec.singular_label.lower()} — {self.employee.display_name}",
        }
        template = ("employees/_drawer_confirm_delete.html" if request.htmx
                    else "employees/section_confirm_delete.html")
        return render(request, template, ctx)

    def post(self, request, *args, **kwargs):
        self.row.delete()
        if request.htmx:
            card_html = _render_section_card(request, self.employee, self.spec)
            response = HttpResponse(card_html)
            response["HX-Trigger"] = "orbis:drawer-close"
            return response
        messages.success(request, f"{self.spec.label} row deleted.")
        return redirect("employee_detail", pk=self.employee.pk)


class BankAccountVerifyView(LoginRequiredMixin, View):
    """Toggles EmployeeBankAccount.is_verified. Not self-editable — the same
    population that can edit this section (HR/admins/downline managers, per
    Department.additional_managers's "e.g. for payroll review" scope) can
    also verify it; there's no narrower payroll-specific gate yet.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        account = get_object_or_404(EmployeeBankAccount, pk=pk)
        if not can_edit_employee_profile(
            request.user, account.employee,
            section=EmployeeBankAccount.__name__, request=request,
        ):
            raise PermissionDenied("You can't verify this employee's bank account.")

        if account.is_verified:
            account.is_verified = False
            account.verified_by = None
            account.verified_at = None
        else:
            account.is_verified = True
            account.verified_by = request.user
            account.verified_at = timezone.now()
        account.updated_by = request.user
        account.save(update_fields=["is_verified", "verified_by", "verified_at", "updated_by", "updated_at"])

        spec = SECTIONS["bank-account"]
        if request.htmx:
            return HttpResponse(_render_section_card(request, account.employee, spec))
        messages.success(request, "Bank account verification updated.")
        return redirect("employee_detail", pk=account.employee.pk)


class AvatarUpdateView(LoginRequiredMixin, View):
    """Set CustomUser.profile_picture from the profile hero.

    Self-service (anyone may set their own avatar) or anyone who can edit
    the employee's basic info.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        employee = get_object_or_404(CustomUser, pk=pk)
        if not (employee.pk == request.user.pk
                or can_edit_employee_basic_info(request.user, employee, request=request)):
            raise PermissionDenied("You can't change this employee's picture.")

        form = AvatarForm(request.POST, request.FILES, instance=employee)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile picture updated.")
        else:
            for error in form.errors.get("profile_picture", ["Invalid image."]):
                messages.error(request, error)
        return redirect("employee_detail", pk=employee.pk)


# ---------------------------------------------------------------------------
# Service Book (Bangladesh Labour Rules 2015/2022, Form-7) — see
# /home/smrashel/.claude/plans/vivid-honking-pillow.md for the full mapping.
# ---------------------------------------------------------------------------

def _can_view_service_book(user, employee) -> bool:
    """HR group or a superuser only (accounts.permissions.can_view_salary_history)
    — deliberately narrower than can_edit_employee_profile's manager-inclusive
    scope: this is a personal legal record carrying gross salary history
    (Part 3) and HR's pre-Orbis leave record (Part 4). User Admins were
    removed 2026-10-05 for the salary history; the employee's own self-view
    was removed the same day (docs/LEGACY_60_IMPORT_PLAN.md decision 4)."""
    return bool(user and user.is_authenticated and can_view_salary_history(user))


class EmployeeServiceBookView(LoginRequiredMixin, DetailView):
    model = CustomUser
    template_name = "employees/service_book.html"
    context_object_name = "employee"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not _can_view_service_book(self.request.user, obj):
            raise PermissionDenied("You can't view this employee's service book.")
        return obj

    def get_context_data(self, **kwargs):
        from employees.services import build_service_book_context

        ctx = super().get_context_data(**kwargs)
        ctx.update(build_service_book_context(ctx["employee"]))
        ctx["page_title"] = f"Service Book — {ctx['employee'].display_name}"
        return ctx


class ServiceBookPDFView(LoginRequiredMixin, View):
    """Streams an always-fresh PDF — no DocumentRequest row is ever created
    (this is a live snapshot generated on demand, not a formal issued/
    approved document — see the plan doc). Reuses documents.pdf's WeasyPrint
    plumbing via the same in-memory-unsaved-DocumentRequest technique
    documents.pdf.test_render_pdf() already uses for the admin "test
    render" action, so nothing here is written to the database."""

    def get(self, request, pk):
        import uuid as uuid_module

        from documents.models import DocumentRequest, DocumentType
        from documents.pdf import render_request_to_pdf_bytes

        employee = get_object_or_404(CustomUser, pk=pk)
        if not _can_view_service_book(request.user, employee):
            raise PermissionDenied("You can't view this employee's service book.")

        doc_type = get_object_or_404(DocumentType, code="SERVICE_BOOK", is_active=True)
        req = DocumentRequest(
            id=uuid_module.uuid4(),
            document_type=doc_type,
            employee=employee,
            status=DocumentRequest.Status.APPROVED,
            input_data={},
        )
        pdf_bytes = render_request_to_pdf_bytes(req)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'inline; filename="service_book_{employee.employee_id or employee.pk}.pdf"'
        )
        return response


# ---------------------------------------------------------------------------
# AJAX (JSON) for cascading dropdowns
# ---------------------------------------------------------------------------

@login_required
@require_GET
def load_states(request):
    country_id = request.GET.get("country")
    rows = State.objects.filter(country_id=country_id, is_active=True).order_by("name")
    return JsonResponse({"results": [{"id": r.pk, "label": r.name} for r in rows]})


@login_required
@require_GET
def load_cities(request):
    state_id = request.GET.get("state")
    rows = City.objects.filter(state_id=state_id, is_active=True).order_by("name")
    return JsonResponse({"results": [{"id": r.pk, "label": r.name} for r in rows]})


@login_required
@require_GET
def load_union_wards(request):
    city_id = request.GET.get("city")
    rows = UnionWard.objects.filter(city_id=city_id, is_active=True).order_by("name")
    return JsonResponse({"results": [{"id": r.pk, "label": r.name} for r in rows]})


@login_required
@require_GET
def load_teams(request):
    """Teams for a department, scoped to what the viewer may see.

    Locked (non-privileged) users are clamped to their own department by
    ``visible_teams``, so this can't be used to enumerate other departments'
    teams via a crafted ``?department=`` value.
    """
    dept_id = (request.GET.get("department") or "").strip()
    dept_id = int(dept_id) if dept_id.isdigit() else None
    rows = visible_teams(request.user, request=request, department_id=dept_id)
    return JsonResponse({"results": [{"id": r.pk, "label": r.name} for r in rows]})


@login_required
@require_GET
def load_subteams(request):
    """Sub-teams for a team — the third rung of the department -> team ->
    sub-team cascade on the user form.

    Scoped through ``visible_teams`` for the same reason ``load_teams`` is: a
    locked user asking for someone else's team id gets nothing back.
    """
    from accounts.models import SubTeam

    team_id = (request.GET.get("team") or "").strip()
    if not team_id.isdigit():
        return JsonResponse({"results": []})
    allowed = visible_teams(request.user, request=request)
    if not allowed.filter(pk=team_id).exists():
        return JsonResponse({"results": []})
    rows = SubTeam.objects.filter(team_id=team_id, is_active=True).order_by("name")
    return JsonResponse({"results": [{"id": r.pk, "label": r.name} for r in rows]})


@login_required
@require_GET
def load_employees(request):
    """HTML-fragment search backing EmployeeSearchWidget (employees/widgets.py).

    Candidates come from employees.services.reports_to_candidates: same
    department as `for_employee` (plus org-wide-visibility groups), holding a
    designation whose access_level is Supervisor-or-above ("Team Lead+").
    """
    q = request.GET.get("q", "").strip()
    for_employee_pk = request.GET.get("for_employee") or None
    target = CustomUser.objects.filter(pk=for_employee_pk).first() if for_employee_pk else None

    qs = reports_to_candidates(target)
    if q:
        qs = qs.filter(employee_search_q(q))
    results = qs.order_by("first_name", "last_name")[:25]
    return render(request, "employees/_employee_search_results.html", {"q": q, "results": results})


# ---------------------------------------------------------------------------
# Dedicated Night Shift assignments — added 2026-09-07, docs/PAYROLL_PLAN.md.
#
# Deliberately NOT wired through the generic SectionSpec/SectionEditView
# machinery: that framework assumes one symmetric can_edit_employee_profile
# gate for both edit and delete, but this model has two genuinely different
# actors (a TL+ manager requests, an APM+ manager approves — neither is
# "self-edit"). Same request/approve shape as attendance.AttendanceCorrection,
# which isn't a SectionSpec either — its own view/url/template set.
# ---------------------------------------------------------------------------

def _hx_redirect(url: str) -> HttpResponse:
    """204 No Content with HX-Redirect — same convention as
    attendance/views.py and policies/views.py's helper of the same name."""
    resp = HttpResponse(status=204)
    resp["HX-Redirect"] = url
    return resp


def _night_shift_request_candidates(user, request):
    """Who the list page's "New request" picker offers: every active
    employee for HR/Admins (can_request_dedicated_night_shift's can_edit_all
    branch), else the requester's own team (dedicated_night_shift_team_pks).
    Empty for anyone below the TL+ bar."""
    if can_edit_all(user, request=request):
        return CustomUser.objects.filter(is_active=True)
    return CustomUser.objects.filter(pk__in=dedicated_night_shift_team_pks(user, request=request))


@login_required
@require_GET
def dedicated_night_shift_candidates(request):
    """HTML-fragment search backing the request form's employee picker
    (DedicatedNightShiftRequestForm) — same fragment as load_employees, over
    the requester's own candidate set rather than reports-to candidates."""
    q = request.GET.get("q", "").strip()
    results = []
    if q:
        results = (
            _night_shift_request_candidates(request.user, request)
            .filter(employee_search_q(q))
            .order_by("first_name", "last_name")[:25]
        )
    return render(request, "employees/_employee_search_results.html", {
        "q": q,
        "results": results,
        "empty_message": "Nobody in your team matches that search.",
    })


class DedicatedNightShiftRequestView(LoginRequiredMixin, View):
    """File a Dedicated Night Shift request — for `?for=<employee_pk>` (the
    profile card's "Request") or, without it, for an employee picked in the
    form itself (the list page's "New request", added 2026-11-03).

    Same on-behalf-of convention as leave.views.LeaveApplyView /
    attendance.views.AttendanceCorrectionRequestView. Unlike those two, this
    is never self-service — can_request_dedicated_night_shift requires the
    requester to be Team-Leader-tier-or-above AND managing the target (or an
    admin), so a bare `?for=<self>` from a Supervisor-tier employee with no
    reports still 403s, and the picker's choice is checked the same way.
    """

    def _resolve_employee(self, request):
        """The `?for=` target, or None when the form picks one."""
        raw = request.GET.get("for") or request.POST.get("for")
        if not raw:
            if not dedicated_night_shift_team_pks(request.user, request=request) \
                    and not can_edit_all(request.user, request=request):
                raise PermissionDenied("You can't request dedicated night shifts.")
            return None
        target = get_object_or_404(CustomUser, pk=raw)
        if not can_request_dedicated_night_shift(request.user, target, request=request):
            raise PermissionDenied("You can't request a dedicated night shift assignment for that employee.")
        return target

    def _form(self, request, target, data=None):
        if target is not None:
            return DedicatedNightShiftAssignmentForm(data)
        return DedicatedNightShiftRequestForm(
            data, candidates=_night_shift_request_candidates(request.user, request),
        )

    def _render(self, request, form, target, *, status=200):
        action_url = reverse("dedicated_night_shift_request")
        if target is not None:
            action_url = f"{action_url}?for={target.pk}"
        ctx = {"form": form, "target_employee": target, "action_url": action_url}
        if request.htmx:
            return render(request, "employees/_dedicated_night_shift_request_drawer.html", ctx, status=status)
        ctx["page_title"] = (
            f"Request dedicated night shift — {target.display_name}" if target
            else "Request dedicated night shift"
        )
        return render(request, "employees/dedicated_night_shift_request.html", ctx, status=status)

    def get(self, request):
        target = self._resolve_employee(request)
        return self._render(request, self._form(request, target), target)

    def post(self, request):
        target = self._resolve_employee(request)
        form = self._form(request, target, request.POST)
        if not form.is_valid():
            return self._render(request, form, target, status=400)
        # A picked employee re-renders with the picker (still bound to the
        # choice), not as a fixed ?for= target.
        rerender_target = target
        if target is None:
            target = form.cleaned_data["employee"]
            # The picker's queryset already is the requester's scope; this
            # is the same gate the ?for= path runs, so the two can't drift.
            if not can_request_dedicated_night_shift(request.user, target, request=request):
                form.add_error("employee", "You can't request a dedicated night shift for that employee.")
                return self._render(request, form, None, status=400)
        assignment = form.save(commit=False)
        assignment.employee = target
        assignment.status = DedicatedNightShiftAssignment.Status.PENDING
        assignment.added_by = request.user
        assignment.updated_by = request.user
        try:
            assignment.full_clean()
        except ValidationError as exc:
            for field, messages_ in exc.message_dict.items():
                for message in messages_:
                    form.add_error(field if field in form.fields else None, message)
            return self._render(request, form, rerender_target, status=400)
        assignment.save()
        record_dedicated_night_shift_event(
            assignment, DedicatedNightShiftAssignmentEvent.Event.REQUESTED, request.user,
        )
        messages.success(
            request,
            f"Dedicated night shift requested for {target.display_name} "
            f"({assignment.start_date} – {assignment.end_date}) — awaiting approval.",
        )
        url = reverse("dedicated_night_shift_list")
        return _hx_redirect(url) if request.htmx else redirect(url)


class DedicatedNightShiftEditView(LoginRequiredMixin, View):
    """Right-hand drawer to revise an existing assignment's window/note —
    added 2026-09-08 so the profile card has the same "update it" action
    every other section does. Same shape as
    attendance.views.AttendanceCorrectionEditView, plus one rule that's
    specific to this model: editing an APPROVED window changes
    NIGHT_QUARTERLY pay, so an editor who couldn't have approved the row
    in the first place doesn't get to change what it pays silently.

    Revised 2026-09-15 (see can_edit_dedicated_night_shift). A TL+ manager
    may now edit an APPROVED row — Supervisors are the ones maintaining
    these windows — but their save **demotes it back to PENDING** and
    clears approved_by/approved_at, so it drops straight out of
    payroll.inputs' `status=APPROVED` collection until an APM/PM
    re-approves. A Manager-tier editor (one who could approve it) saves in
    place, still APPROVED. Previously the APPROVED case was simply barred
    below Manager tier, which left Supervisors unable to fix a date at all.

    DECLINED/CANCELLED still 403 outright — terminal, file a new request.
    Status is never set *forward* here; approve/decline stays on the
    dedicated actions below.
    """

    def dispatch(self, request, pk, *args, **kwargs):
        self.assignment = get_object_or_404(DedicatedNightShiftAssignment, pk=pk)
        if not can_edit_dedicated_night_shift(request.user, self.assignment, request=request):
            raise PermissionDenied("You can't edit this dedicated night shift assignment.")
        # Both captured here, before any form binds to self.assignment and
        # overwrites the fields we're comparing against.
        self.was_approved = self.assignment.status == DedicatedNightShiftAssignment.Status.APPROVED
        self.can_approve = can_approve_dedicated_night_shift(
            request.user, self.assignment, request=request,
        )
        # The pre-edit window, for the ledger's before/after pair. Same
        # reason as the two flags above: the form is about to overwrite it.
        self.old_window = (self.assignment.start_date, self.assignment.end_date)
        return super().dispatch(request, pk, *args, **kwargs)

    @property
    def demotes_on_save(self) -> bool:
        """True when this editor's save will cost the row its approval."""
        return self.was_approved and not self.can_approve

    def _render(self, request, form, *, status=200):
        ctx = {
            "assignment": self.assignment,
            "target_employee": self.assignment.employee,
            "form": form,
            "action_url": reverse("dedicated_night_shift_edit", args=[self.assignment.pk]),
            "demotes_on_save": self.demotes_on_save,
        }
        if request.htmx:
            return render(request, "employees/_dedicated_night_shift_edit_drawer.html", ctx, status=status)
        ctx["page_title"] = f"Edit dedicated night shift — {self.assignment.employee.display_name}"
        return render(request, "employees/dedicated_night_shift_edit.html", ctx, status=status)

    def get(self, request, pk):
        return self._render(request, DedicatedNightShiftAssignmentForm(instance=self.assignment))

    def post(self, request, pk):
        form = DedicatedNightShiftAssignmentForm(request.POST, instance=self.assignment)
        if not form.is_valid():
            return self._render(request, form, status=400)
        assignment = form.save(commit=False)
        assignment.updated_by = request.user
        demoted = self.demotes_on_save
        if demoted:
            # Below-Manager edit of an approved window: send it back for
            # re-approval rather than letting it keep paying on terms
            # nobody with approval authority has seen.
            #
            # Order matters, and not by accident: this runs *after*
            # form.is_valid(), which is where ModelForm._post_clean() calls
            # instance.full_clean(). `status` isn't a form field, so the
            # instance still carries APPROVED at that point and the model's
            # overlap check (clean(), which only guards APPROVED rows) does
            # fire. That's what we want — an approved window is validated
            # as if it stays approved, so a clash is rejected here with the
            # error on the form rather than saved as a PENDING row nobody
            # can ever approve. Don't move this demotion above the
            # is_valid() call; it would silently disable that guard.
            assignment.status = DedicatedNightShiftAssignment.Status.PENDING
            assignment.approved_by = None
            assignment.approved_at = None
        try:
            assignment.full_clean()
        except ValidationError as exc:
            for field, messages_ in exc.message_dict.items():
                for message in messages_:
                    form.add_error(field if field in form.fields else None, message)
            return self._render(request, form, status=400)
        assignment.save()
        record_dedicated_night_shift_event(
            assignment,
            DedicatedNightShiftAssignmentEvent.Event.DEMOTED if demoted
            else DedicatedNightShiftAssignmentEvent.Event.EDITED,
            request.user,
            old_window=self.old_window,
        )
        if demoted:
            # A warning, not a success: the editor needs to know the
            # dedicated night rate has stopped applying until someone
            # approves the new window.
            messages.warning(
                request,
                f"Dedicated night shift window updated for "
                f"{assignment.employee.display_name} and sent back for approval. "
                f"The dedicated night rate won't apply until an APM or above approves it.",
            )
        else:
            messages.success(request, "Dedicated night shift assignment updated.")
        if request.htmx:
            # The profile card (and, on the list page, the row) both need
            # the new dates to show — simplest correct option is a full
            # reload rather than hand-building an OOB swap for a card that
            # isn't a SectionSpec. The message above survives the reload in
            # the session and renders as a toast on the way back.
            response = HttpResponse(status=204)
            response["HX-Refresh"] = "true"
            return response
        return redirect("employee_detail", pk=self.assignment.employee_id)


class DedicatedNightShiftHistoryView(LoginRequiredMixin, View):
    """Read-only audit timeline for one assignment
    (DedicatedNightShiftAssignmentEvent).

    Gated by can_view_employee, not by the edit/approve permissions: if you
    can already see the Dedicated Night Shift card on someone's profile,
    you can see how that record got to its current state. Nothing here is
    actionable, so a narrower gate would only hide provenance from people
    already looking at the record.

    Drawer-or-full-page, the same split the request/edit views use.
    """

    def dispatch(self, request, pk, *args, **kwargs):
        self.assignment = get_object_or_404(
            DedicatedNightShiftAssignment.objects.select_related("employee"), pk=pk,
        )
        if not can_view_employee(request.user, self.assignment.employee, request=request):
            raise PermissionDenied("You can't view this employee.")
        return super().dispatch(request, pk, *args, **kwargs)

    def get(self, request, pk):
        ctx = {
            "assignment": self.assignment,
            "target_employee": self.assignment.employee,
            "events": list(self.assignment.events.select_related("actor")),
        }
        if request.htmx:
            return render(request, "employees/_dedicated_night_shift_history_drawer.html", ctx)
        ctx["page_title"] = f"History — {self.assignment.employee.display_name}"
        return render(request, "employees/dedicated_night_shift_history.html", ctx)


def _parse_iso_date(raw):
    try:
        return date.fromisoformat((raw or "").strip()) if raw else None
    except ValueError:
        return None


def _apply_night_shift_filters(qs, request):
    """q + department + team + status + "active on" for the assignment list
    — accounts.filters' fields reached through `employee__` (the same shape
    as attendance.views._apply_correction_filters). Applied inside each
    tab's own scope, so a filter can only narrow it."""
    cond = employee_search_q(request.GET.get("q"), prefix="employee__")
    if cond is not None:
        qs = qs.filter(cond)
    dept_id, team_id = effective_filter_ids(request.user, request)
    if dept_id:
        qs = qs.filter(employee__department_id=dept_id)
    if team_id:
        qs = qs.filter(employee__team_id=team_id)
    status = request.GET.get("status")
    if status in DedicatedNightShiftAssignment.Status.values:
        qs = qs.filter(status=status)
    active_on = _parse_iso_date(request.GET.get("active_on"))
    if active_on:
        qs = qs.filter(start_date__lte=active_on, end_date__gte=active_on)
    return qs


class DedicatedNightShiftListView(LoginRequiredMixin, View):
    """Mine / My team / Pending my approval / All — same tab shape as
    attendance.views.AttendanceCorrectionListView. Reached from the Roster
    sidebar section since 2026-11-03.

    "My team" is every status for the people the viewer may file for
    (dedicated_night_shift_team_pks), so a Team Leader can find and maintain
    the windows they requested after they're decided — before it existed,
    an approved row dropped out of every tab a TL could open. It's the
    default tab whenever it's shown.
    """

    template_name = "employees/dedicated_night_shift_list.html"

    def get(self, request):
        user = request.user
        team_pks = dedicated_night_shift_team_pks(user, request=request)
        show_team_tab = bool(team_pks)
        tab = request.GET.get("tab") or ("team" if show_team_tab else "mine")

        base_qs = (
            DedicatedNightShiftAssignment.objects
            .select_related("employee", "employee__team", "employee__department",
                            "added_by", "approved_by", "updated_by")
        )
        mine_qs = base_qs.filter(employee=user)
        team_qs = base_qs.filter(employee_id__in=team_pks) if show_team_tab else base_qs.none()
        user_can_edit_all = can_edit_all(user, request=request)
        pending_qs = base_qs.filter(status=DedicatedNightShiftAssignment.Status.PENDING).exclude(employee=user)
        if not user_can_edit_all:
            pending_qs = pending_qs.filter(employee_id__in=managed_scope_pks(user, request=request))
        show_pending_tab = has_dedicated_night_shift_approval_scope(user, request=request)
        show_all_tab = user_can_edit_all
        all_qs = base_qs if show_all_tab else base_qs.none()
        # "Direct reports only" scope switch — same default-on rule as the
        # leave list (leave.views._direct_reports_only): narrows within the
        # visible scope, hidden for a viewer with no direct reports. Night
        # shifts need Manager tier, so on the approval tabs "direct" also
        # reaches past below-tier managers to the requests actually waiting
        # on this viewer (core.approvals.night_shift_inbox_pks — the bell's
        # rule too). "My team" is about who the viewer files for, not who
        # they approve, so there it means plain direct reports.
        direct_pks = direct_report_pks(user, request=request)
        direct_only = bool(direct_pks) and request.GET.get("direct") != "0"
        if direct_only:
            inbox_pks = night_shift_inbox_pks(user, request=request)
            pending_qs = pending_qs.filter(employee_id__in=inbox_pks)
            all_qs = all_qs.filter(employee_id__in=inbox_pks)
            team_qs = team_qs.filter(employee_id__in=direct_pks)

        if tab == "team" and show_team_tab:
            selected_qs = team_qs
        elif tab == "pending" and show_pending_tab:
            selected_qs = pending_qs
        elif tab == "all" and show_all_tab:
            selected_qs = all_qs
        else:
            tab = "mine"
            selected_qs = mine_qs

        page = paginate_list(request, _apply_night_shift_filters(selected_qs, request))
        rows = page["rows"]
        # Approved windows still running or ahead: flag working shifts other
        # than NIG/CUST left inside them (roster/night_window.py) until fixed.
        from roster.night_window import conflicts_by_assignment

        today = timezone.localdate()
        night_conflicts = conflicts_by_assignment([a for a in rows if a.end_date >= today])
        for a in rows:
            a.night_conflicts = night_conflicts.get(a.pk, [])
        actionable_pks = [
            a.pk for a in rows
            if a.is_pending and can_approve_dedicated_night_shift(user, a, request=request)
        ]
        editable_pks = [a.pk for a in rows if can_edit_dedicated_night_shift(user, a, request=request)]
        cancellable_pks = [
            a.pk for a in rows if can_cancel_dedicated_night_shift(user, a, request=request, today=today)
        ]
        filter_keys = ("q", "department", "team", "status", "active_on")
        ctx = {
            "page_title": "Dedicated night shift assignments",
            "tab": tab,
            "actionable_pks": actionable_pks,
            "editable_pks": editable_pks,
            "cancellable_pks": cancellable_pks,
            **page,
            "mine_count": mine_qs.count(),
            "team_count": team_qs.count() if show_team_tab else 0,
            "pending_count": pending_qs.count(),
            "all_count": all_qs.count() if show_all_tab else 0,
            "show_team_tab": show_team_tab,
            "show_pending_tab": show_pending_tab,
            "show_all_tab": show_all_tab,
            "can_request": show_team_tab or user_can_edit_all,
            "has_direct_reports": bool(direct_pks),
            "direct_only": direct_only,
            "filters_active": any(request.GET.get(k) for k in filter_keys),
            "status_choices": DedicatedNightShiftAssignment.Status.choices,
            "selected_status": request.GET.get("status", ""),
            "active_on_input": (request.GET.get("active_on") or "").strip(),
            **employee_filter_context(user, request),
        }
        return render(request, self.template_name, ctx)


class DedicatedNightShiftCancelView(LoginRequiredMixin, View):
    """Withdraw an assignment → CANCELLED (added 2026-11-03). Who may, and
    when, is can_cancel_dedicated_night_shift: pending by its maintainer,
    approved only before it starts (HR/Admins any time). Terminal — the
    row stays, drops out of payroll.inputs' APPROVED-only collection, and
    the reason lands on the event ledger."""

    http_method_names = ["post"]

    def post(self, request, pk):
        assignment = get_object_or_404(
            DedicatedNightShiftAssignment.objects.select_related("employee"), pk=pk,
        )
        if not can_cancel_dedicated_night_shift(request.user, assignment, request=request):
            raise PermissionDenied("You can't cancel this dedicated night shift assignment.")
        reason = request.POST.get("reason", "").strip()
        was_approved = assignment.status == DedicatedNightShiftAssignment.Status.APPROVED
        assignment.status = DedicatedNightShiftAssignment.Status.CANCELLED
        assignment.updated_by = request.user
        assignment.save()
        record_dedicated_night_shift_event(
            assignment, DedicatedNightShiftAssignmentEvent.Event.CANCELLED, request.user,
            note=reason[:255],
        )
        msg = f"Dedicated night shift cancelled for {assignment.employee.display_name}."
        if was_approved:
            msg += " The dedicated night rate no longer applies to this window."
        messages.success(request, msg)
        next_url = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
        ):
            next_url = reverse("dedicated_night_shift_list")
        return redirect(next_url)


def _approve_night_shift(assignment: DedicatedNightShiftAssignment, user) -> None:
    """Mark APPROVED, running the same overlap validation the model's
    clean() enforces. Raises ValidationError on an overlapping window —
    caller is responsible for the can_approve_dedicated_night_shift +
    is_pending checks, shared by the single-row and bulk approve views so
    the two can't drift apart.
    """
    assignment.status = DedicatedNightShiftAssignment.Status.APPROVED
    assignment.approved_by = user
    assignment.approved_at = timezone.now()
    assignment.updated_by = user
    assignment.full_clean()
    assignment.save()
    record_dedicated_night_shift_event(
        assignment, DedicatedNightShiftAssignmentEvent.Event.APPROVED, user,
    )


def _warn_night_window_conflicts(request, assignment) -> None:
    """Option (b), 2026-11-03: approving a window over working shifts other
    than NIG/CUST already on the roster (or in the weekly pattern) leaves
    them in place but says so — a warning, never a block, like the payroll
    soft checks. Fixing them is the department head's call."""
    from roster.night_window import conflicting_shifts, describe_conflicts

    conflicts = conflicting_shifts(assignment)
    if conflicts:
        messages.warning(request, describe_conflicts(assignment.employee, conflicts))


def _decline_night_shift(assignment: DedicatedNightShiftAssignment, user, decline_reason: str = "") -> None:
    assignment.status = DedicatedNightShiftAssignment.Status.DECLINED
    assignment.decline_reason = decline_reason
    assignment.updated_by = user
    assignment.save()
    record_dedicated_night_shift_event(
        assignment, DedicatedNightShiftAssignmentEvent.Event.DECLINED, user,
        note=decline_reason[:255],
    )


class _DedicatedNightShiftActionBase(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def dispatch(self, request, pk, *args, **kwargs):
        self.assignment = get_object_or_404(DedicatedNightShiftAssignment, pk=pk)
        if not can_approve_dedicated_night_shift(request.user, self.assignment, request=request) \
                or not self.assignment.is_pending:
            raise PermissionDenied
        return super().dispatch(request, pk, *args, **kwargs)


class DedicatedNightShiftApproveView(_DedicatedNightShiftActionBase):
    def post(self, request, pk):
        assignment = self.assignment
        try:
            _approve_night_shift(assignment, request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
            return redirect(f"{reverse('dedicated_night_shift_list')}?tab=pending")
        messages.success(request, f"Dedicated night shift approved for {assignment.employee.display_name}.")
        _warn_night_window_conflicts(request, assignment)
        return redirect(f"{reverse('dedicated_night_shift_list')}?tab=pending")


class DedicatedNightShiftDeclineView(_DedicatedNightShiftActionBase):
    def post(self, request, pk):
        assignment = self.assignment
        _decline_night_shift(assignment, request.user, request.POST.get("decline_reason", "").strip())
        messages.success(request, f"Dedicated night shift declined for {assignment.employee.display_name}.")
        return redirect(f"{reverse('dedicated_night_shift_list')}?tab=pending")


class DedicatedNightShiftBulkActionView(LoginRequiredMixin, View):
    """Approve or decline several PENDING assignments in one submit — same
    checkbox column + "Approve selected"/"Decline selected" toolbar shape as
    attendance.views.AttendanceCorrectionBulkActionView. Each id is still
    individually re-checked for can_approve_dedicated_night_shift + is_pending
    (never trust a stale page's checkbox state); an approval that fails the
    overlap check (e.g. another window was approved for the same employee
    since the page loaded) is skipped the same way a permission failure is,
    rather than aborting the batch.
    """

    http_method_names = ["post"]

    def post(self, request):
        return_tab = request.POST.get("tab") or "pending"
        return_url = f"{reverse('dedicated_night_shift_list')}?tab={return_tab}"

        action = request.POST.get("action")
        if action not in ("approve", "decline"):
            messages.error(request, "Unknown bulk action.")
            return redirect(return_url)

        ids = list(dict.fromkeys(request.POST.getlist("ids")))
        if not ids:
            messages.info(request, "No assignments selected.")
            return redirect(return_url)

        acted = 0
        with transaction.atomic():
            assignments = DedicatedNightShiftAssignment.objects.filter(pk__in=ids).select_related("employee")
            for assignment in assignments:
                if not assignment.is_pending or not can_approve_dedicated_night_shift(
                    request.user, assignment, request=request,
                ):
                    continue
                if action == "approve":
                    try:
                        _approve_night_shift(assignment, request.user)
                    except ValidationError:
                        continue
                    _warn_night_window_conflicts(request, assignment)
                else:
                    _decline_night_shift(assignment, request.user)
                acted += 1
        # len(ids) rather than a per-row counter, so a submitted id that
        # doesn't even exist (a stale page, tampered form) is counted as
        # skipped too, not silently dropped from the tally.
        skipped = len(ids) - acted

        verb = "Approved" if action == "approve" else "Declined"
        summary = f"{verb} {acted} assignment(s)."
        if skipped:
            summary += f" Skipped {skipped} (already handled, not yours to act on, or overlapping an approved window)."
        (messages.success if action == "approve" else messages.info)(request, summary)
        return redirect(return_url)


# ---------------------------------------------------------------------------
# Night shift consent — Bangladesh Labour Act 2006 s.109 (added 2026-09-13).
# See employees/models.py::NightShiftConsent and employees/services.py for
# the eligibility/self-healing logic; this view is deliberately thin.
# ---------------------------------------------------------------------------

class NightShiftConsentUpdateView(LoginRequiredMixin, View):
    """Record a night-shift consent decision for `?for=<employee_pk>`
    (defaults to the requester) — same on-behalf-of convention as
    leave.views.LeaveApplyView / attendance.views.AttendanceCorrectionRequestView.

    Self-service is always allowed for one's own record
    (can_set_own_night_shift_consent); acting on someone else's requires
    can_manage_night_shift_consent (HR/Admins). POST-only, plain redirect —
    this is a two-button dashboard-card form, not a drawer.
    """

    http_method_names = ["post"]

    def post(self, request):
        raw = request.GET.get("for") or request.POST.get("for") or request.user.pk
        target = get_object_or_404(CustomUser, pk=raw)
        is_self = target.pk == request.user.pk
        if is_self:
            if not can_set_own_night_shift_consent(request.user, target, request=request):
                raise PermissionDenied("You can't record night shift consent for yourself.")
        elif not can_manage_night_shift_consent(request.user, request=request):
            raise PermissionDenied("You can't record night shift consent on behalf of this employee.")

        consents_raw = request.POST.get("consents")
        if consents_raw not in ("true", "false"):
            raise Http404("Missing or invalid 'consents' value.")
        consents = consents_raw == "true"
        note = (request.POST.get("note") or "").strip()

        try:
            record_night_shift_consent_decision(target, consents, actor=request.user, note=note)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))
        else:
            who = "you" if is_self else target.display_name
            verb = "recorded" if consents else "declined"
            messages.success(request, f"Night shift consent {verb} for {who} this month.")

        next_url = request.POST.get("next") or reverse("home")
        return redirect(next_url)


@method_decorator(dynamic_allowed_users, name="dispatch")
class NightShiftConsentListView(LoginRequiredMixin, View):
    """HR/Admin audit list — current month's consent status for every
    qualifying female employee (self-healing: visiting this page also
    lazily materialises any missing current-month row, same belt-and-
    suspenders relationship the monthly beat job has with the dashboard
    card). ``?period=YYYY-MM-01`` reads a past month's already-recorded
    rows as-is (no lazy creation — a closed month's eligibility isn't
    re-evaluated), which is how this page serves the audit-history
    requirement.
    """

    template_name = "employees/night_shift_consent_list.html"

    def get(self, request):
        if not can_manage_night_shift_consent(request.user, request=request):
            raise PermissionDenied("You can't view night shift consent records.")

        this_month = current_night_shift_consent_period()
        period = this_month
        period_raw = request.GET.get("period")
        if period_raw:
            try:
                period = date.fromisoformat(period_raw).replace(day=1)
            except ValueError:
                period = this_month

        scoped_pks = set(
            apply_employee_filters(CustomUser.objects.all(), request.user, request)
            .values_list("pk", flat=True)
        )

        if period == this_month:
            candidates = (
                CustomUser.objects
                .filter(pk__in=scoped_pks, is_active=True, personal_info__gender="F")
                .select_related("department", "team", "personal_info")
                .order_by("first_name", "last_name")
            )
            rows = []
            # One Shift/Roster query pair per candidate — acceptable at this
            # org's headcount; mirrors the per-employee loops already used by
            # core/dashboard.py's birthday/office-distribution builders.
            for employee in candidates:
                if not qualifies_for_night_shift_consent(employee, period):
                    continue
                consent, _created = get_or_create_night_shift_consent(employee, period)
                rows.append(consent)
        else:
            rows = list(
                NightShiftConsent.objects
                .filter(period=period, employee_id__in=scoped_pks)
                .select_related("employee", "employee__department", "employee__team", "decided_by")
                .order_by("employee__first_name", "employee__last_name")
            )

        page = paginate_list(request, rows)
        ctx = {
            "page_title": "Night shift consent",
            "period": period,
            "this_month": this_month,
            "prev_period": (period.replace(day=1) - timedelta(days=1)).replace(day=1),
            "next_period": (period.replace(day=28) + timedelta(days=4)).replace(day=1),
            **page,
            **employee_filter_context(request.user, request),
        }
        return render(request, self.template_name, ctx)


# ---------------------------------------------------------------------------
# Profile self-service toggle (added 2026-09-17)
# ---------------------------------------------------------------------------

class ProfileSelfServiceToggleView(LoginRequiredMixin, View):
    """HR-only: flip CustomUser.profile_self_service_enabled for one
    employee on/off. Plain POST + redirect, same "two-button, not a
    drawer" shape as NightShiftConsentUpdateView — this is a rare,
    deliberate HR action, not something rendered per-row in bulk.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        employee = get_object_or_404(CustomUser, pk=pk)
        if not can_manage_profile_self_service(request.user, request=request):
            raise PermissionDenied("You can't manage profile self-service for this employee.")

        enabled_raw = request.POST.get("enabled")
        if enabled_raw not in ("true", "false"):
            raise Http404("Missing or invalid 'enabled' value.")
        enabled = enabled_raw == "true"
        note = (request.POST.get("note") or "").strip()

        set_profile_self_service(employee, enabled, actor=request.user, note=note)
        verb = "enabled" if enabled else "disabled"
        messages.success(request, f"Profile self-service {verb} for {employee.display_name}.")

        next_url = request.POST.get("next") or reverse("employee_detail", args=[employee.pk])
        return redirect(next_url)


@method_decorator(dynamic_allowed_users, name="dispatch")
class ProfileSelfServiceAuditListView(LoginRequiredMixin, View):
    """HR/Admin audit list — every employee currently self-service ON, when
    it was switched on, and by whom, so a toggle HR forgot to flip back off
    doesn't sit open indefinitely with no record anyone would notice. The
    toggle itself lives on each employee's own profile page, not here —
    this page is read-only.
    """

    template_name = "employees/profile_self_service_list.html"

    def get(self, request):
        if not can_manage_profile_self_service(request.user, request=request):
            raise PermissionDenied("You can't view profile self-service records.")

        scoped_qs = apply_employee_filters(CustomUser.objects.all(), request.user, request)
        rows = list(
            scoped_qs.filter(profile_self_service_enabled=True)
            .select_related("department", "team")
            .order_by("first_name", "last_name")
        )
        for employee in rows:
            employee.last_event = (
                employee.profile_self_service_events
                .select_related("actor")
                .order_by("-occurred_at")
                .first()
            )

        page = paginate_list(request, rows)
        ctx = {
            "page_title": "Profile self-service",
            **page,
            **employee_filter_context(request.user, request),
        }
        return render(request, self.template_name, ctx)


# ---------------------------------------------------------------------------
# Org chart (added 2026-10-11, docs/PLAN.md "Org chart")
# ---------------------------------------------------------------------------

#: One tier open on first paint: the leader and their direct reports fit
#: one screen; a second tier of a 1,300-person tree is ~9,000px wide.
ORG_CHART_DEFAULT_LEVELS = 1
#: 99 = open every tier. Anything else in ?levels= falls back to the default.
ORG_CHART_LEVEL_CHOICES = (1, 2, 3, 99)
#: Individual contributors shown under a manager before "Show all".
ORG_CHART_LEAF_PREVIEW = 12


def _org_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _org_chart_render_context(request, tree):
    """Context shared by the full page and the htmx children fragment."""
    me = tree.get(request.user.pk)
    if me is not None:
        me.is_me = True
    return {
        "tree": tree,
        "me": me,
        # Profile links only where the viewer may open the profile; the
        # card itself (name, title, department, team) is company-wide.
        "visible_pks": visible_employee_pks(request.user, request=request),
        "leaf_preview": ORG_CHART_LEAF_PREVIEW,
    }


class OrgChartView(LoginRequiredMixin, TemplateView):
    """Top-down reporting chart, rebuilt from JobInfo.reports_to per request.

    Company-wide by design: an org chart is a directory, and the cards carry
    nothing the sidebar's people list doesn't (name, designation,
    department, team). A locked viewer's scope still gates the profile
    *links*, never the tree itself. ``?root=`` picks which top-level leader
    to start from (or ``unlinked`` for the people outside every tree),
    ``?levels=`` how many tiers open at once, ``?focus=`` expands the path
    to one person and ``?q=`` searches (one hit focuses it, several list).
    """

    template_name = "employees/org_chart.html"
    extra_context = {"page_title": "Org Chart"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        tree = build_org_tree()
        ctx.update(_org_chart_render_context(request, tree))

        levels = ORG_CHART_DEFAULT_LEVELS
        try:
            requested = int(request.GET.get("levels", levels))
        except (TypeError, ValueError):
            requested = levels
        if requested in ORG_CHART_LEVEL_CHOICES:
            levels = requested

        focus = None
        results = []
        q = (request.GET.get("q") or "").strip()
        focus_pk = _org_uuid(request.GET.get("focus"))
        if focus_pk is not None:
            focus = tree.get(focus_pk)
        elif q:
            results = tree.search(q)
            if len(results) == 1:
                focus, results = results[0], []

        mode = "tree"
        root = None
        expanded = set()
        if focus is not None:
            focus.focused = True
            if focus.is_root and not focus.children:
                mode = "unlinked"
            else:
                root = tree.get(tree.root_of(focus.pk))
                expanded = set(tree.ancestors(focus.pk))
                if focus.children:
                    expanded.add(focus.pk)
            note(request, subject=focus.user)
        else:
            root_param = request.GET.get("root")
            if root_param == "unlinked":
                mode = "unlinked"
            else:
                root_pk = _org_uuid(root_param)
                root = tree.get(root_pk) if root_pk else None
                if root is None or not root.children:
                    root = tree.roots[0] if tree.roots else None
                if root is not None:
                    expanded = tree.expanded_to_level(root.pk, levels)
        if mode == "unlinked":
            root = None
        for pk in expanded:
            node = tree.get(pk)
            if node is not None:
                node.open = True

        is_hr = can_edit_all(request.user, request=request)
        ctx.update({
            "mode": mode,
            "root": root,
            "focus": focus,
            "q": q,
            "results": results,
            "levels": levels,
            "level_choices": ORG_CHART_LEVEL_CHOICES,
            "stats": {
                "active": len(tree.nodes),
                "linked": tree.linked_count,
                "roots": len(tree.roots),
                "depth": tree.max_depth,
                "unlinked": len(tree.unlinked),
                "manager_inactive": len(tree.manager_inactive_nodes),
            },
            "quality": {
                "unlinked": tree.unlinked,
                "manager_inactive": tree.manager_inactive_nodes,
                "missing_job_info": tree.missing_job_info_nodes,
                "wide_spans": tree.wide_spans(),
            } if is_hr else None,
        })
        return ctx


class OrgChartNodeView(LoginRequiredMixin, View):
    """htmx: one manager's children, rendered on first expand."""

    def get(self, request, pk):
        tree = build_org_tree()
        node = tree.get(pk)
        if node is None:
            raise Http404("No active employee with that id.")
        note(request, subject=node.user)
        ctx = _org_chart_render_context(request, tree)
        ctx["node"] = node
        return render(request, "employees/_org_children.html", ctx)
