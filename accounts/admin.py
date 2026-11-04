from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.contrib.auth.admin import UserAdmin
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_POST
from import_export.admin import ImportExportModelAdmin

from accounts.models import (
    AccessLevel,
    ApiClient,
    CostCenter,
    CustomUser,
    Department,
    Designation,
    EmploymentType,
    Grade,
    JobLocation,
    SubProject,
    SubTeam,
    Team,
    WorkLocation,
)
from accounts.resources import DesignationResource, TeamResource


class HasGroupFilter(admin.SimpleListFilter):
    """"Has group: yes / no" — a plain M2M filter can't express "none".

    Every user is meant to carry at least `All Employees` (added by
    `accounts.signals`), so "No" is a data-quality check for rows created
    outside the normal paths.
    """

    title = "group membership"
    parameter_name = "has_group"

    def lookups(self, request, model_admin):
        return (("yes", "Has at least one group"), ("no", "In no group"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(groups__isnull=False).distinct()
        if self.value() == "no":
            return queryset.filter(groups__isnull=True)
        return queryset


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    ordering = ("email",)
    list_display = (
        "email", "register_serial_no", "employee_id",
        "first_name", "last_name",
        "department", "designation",
        "groups_list",
        "is_active", "is_staff",
    )
    list_display_links = ("email",)
    # Nullable FKs are skipped by the admin's automatic select_related().
    list_select_related = ("department", "designation")
    list_filter = (
        "is_active", "is_staff", "is_superuser",
        "groups", HasGroupFilter,
        "department", "work_location",
    )
    search_fields = ("email", "username", "first_name", "last_name", "employee_id", "register_serial_no", "entra_object_id")

    fieldsets = (
        (None, {"fields": ("email", "username", "password")}),
        ("Personal", {"fields": ("first_name", "last_name", "contact_number",
                                  "profile_picture", "signature")}),
        ("Employment", {"fields": ("employee_id", "register_serial_no", "entra_object_id",
                                    "joining_date", "probation_month",
                                    "notice_period_months",
                                    "confirmation_date", "last_working_date")}),
        ("Org structure", {"fields": ("department", "designation", "access_level",
                                       "team", "subteam",
                                       "job_location", "work_location",
                                       "grade", "employment_type")}),
        ("Roster defaults", {"fields": ("timezone_name", "shift_start_hour")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser",
                                     "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "username", "first_name", "last_name",
                       "password1", "password2"),
        }),
    )

    def get_queryset(self, request):
        # `groups_list` reads the M2M per row; prefetch keeps the changelist
        # at a constant number of queries regardless of page size.
        return super().get_queryset(request).prefetch_related("groups")

    @admin.display(description="Groups")
    def groups_list(self, obj):
        return ", ".join(sorted(g.name for g in obj.groups.all())) or "—"


class _NamedAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Department)
class DepartmentAdmin(_NamedAdmin):
    list_display = ("name", "cpl_treatment", "head_of_department", "is_active", "updated_at")
    list_filter = ("is_active", "cpl_treatment")
    autocomplete_fields = ("head_of_department", "additional_managers")
    fields = ("name", "description", "cpl_treatment", "head_of_department",
              "additional_managers", "is_active")


@admin.register(Designation)
class DesignationAdmin(ImportExportModelAdmin, _NamedAdmin):
    resource_classes = [DesignationResource]
    list_display = ("name", "access_level", "is_active", "updated_at")
    list_editable = ("access_level",)
    list_filter = ("access_level", "is_active")


@admin.register(AccessLevel)
class AccessLevelAdmin(_NamedAdmin):
    list_display = ("name", "rank", "is_active", "updated_at")
    ordering = ("-rank", "name")


@admin.register(JobLocation)
class JobLocationAdmin(_NamedAdmin):
    list_display = ("name", "code", "is_active", "updated_at")
    search_fields = ("name", "code")


@admin.register(WorkLocation)
class WorkLocationAdmin(_NamedAdmin):
    pass


@admin.register(Team)
class TeamAdmin(ImportExportModelAdmin, _NamedAdmin):
    resource_classes = [TeamResource]
    list_display = ("name", "department", "cpl_treatment_override", "additional_managers_list",
                     "is_active", "updated_at")
    list_filter = ("is_active", "department", "cpl_treatment_override")
    autocomplete_fields = ("department", "additional_managers")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("additional_managers")

    @admin.display(description="Additional managers")
    def additional_managers_list(self, obj):
        return ", ".join(
            f"{m.display_name} ({m.employee_id})" for m in obj.additional_managers.all()
        ) or "—"


@admin.register(SubTeam)
class SubTeamAdmin(_NamedAdmin):
    list_display = ("name", "team", "is_active", "updated_at")
    list_filter = ("is_active", "team")


@admin.register(Grade)
class GradeAdmin(_NamedAdmin):
    list_display = ("name", "rank", "is_active", "updated_at")
    ordering = ("-rank", "name")


@admin.register(EmploymentType)
class EmploymentTypeAdmin(_NamedAdmin):
    pass


@admin.register(CostCenter)
class CostCenterAdmin(_NamedAdmin):
    list_display = ("name", "code", "gl_code", "is_active", "updated_at")
    search_fields = ("name", "code", "gl_code")


@admin.register(SubProject)
class SubProjectAdmin(_NamedAdmin):
    pass


class ApiClientForm(forms.ModelForm):
    scopes = forms.MultipleChoiceField(
        choices=ApiClient.Scope.choices, widget=forms.CheckboxSelectMultiple, required=False,
    )

    class Meta:
        model = ApiClient
        fields = ("name", "scopes", "service_account", "is_active", "note")

    def clean(self):
        cleaned = super().clean()
        if ApiClient.Scope.ROSTER_WRITE in (cleaned.get("scopes") or []) and not cleaned.get("service_account"):
            self.add_error("service_account", "A client with roster:write needs a service account.")
        return cleaned


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    """Applications allowed to call the API — docs/DIRECTORY_API_PLAN.md §4.5.

    Superusers and User Admins only. Creating a client issues no token; the
    "Generate new token" button on the edit page (or the list action of the
    same name) does, showing the plaintext once.
    """

    change_form_template = "admin/accounts/apiclient/change_form.html"

    form = ApiClientForm
    list_display = ("name", "token_prefix", "scope_list", "is_active", "last_used_at", "token_issued_at")
    list_filter = ("is_active",)
    search_fields = ("name", "token_prefix")
    readonly_fields = ("token_prefix", "token_issued_at", "last_used_at", "created_by", "created_at")
    autocomplete_fields = ("service_account",)
    actions = ("generate_token",)

    @admin.display(description="Scopes")
    def scope_list(self, obj):
        return ", ".join(obj.scopes or []) or "—"

    def _allowed(self, request) -> bool:
        from accounts.permissions import ADMIN_GROUP

        user = request.user
        return user.is_active and (
            user.is_superuser or (user.is_staff and user.groups.filter(name=ADMIN_GROUP).exists())
        )

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Generate new token (replaces the current one)")
    def generate_token(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one API client.", messages.ERROR)
            return None
        return self._issue_and_show(request, queryset.get())

    def get_urls(self):
        return [
            path(
                "<int:pk>/generate-token/",
                self.admin_site.admin_view(require_POST(self.generate_token_view)),
                name="accounts_apiclient_generate_token",
            ),
            *super().get_urls(),
        ]

    def generate_token_view(self, request, pk):
        if not self._allowed(request):
            raise PermissionDenied
        return self._issue_and_show(request, get_object_or_404(ApiClient, pk=pk))

    def _issue_and_show(self, request, client):
        token = client.issue_token()
        # Rendered straight into this response — never put in a message or the
        # session, so the plaintext is never stored anywhere.
        response = TemplateResponse(request, "admin/accounts/apiclient/token_issued.html", {
            **self.admin_site.each_context(request),
            "title": f"New token for {client.name}",
            "client": client,
            "token": token,
            "opts": self.model._meta,
        })
        add_never_cache_headers(response)
        return response
