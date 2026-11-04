"""Employees bulk update — page + template download."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from accounts.permissions import can_edit_all, has_managed_scope, managed_scope_pks
from employees.upload import (
    ApplyReport,
    apply_upload,
    parse_upload,
    render_template_response,
)

User = get_user_model()


MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 MB

PAGE_TEMPLATE = "employees/upload.html"
FRAGMENT_TEMPLATE = "employees/_upload_report.html"


class _ManagedScopeRequired(LoginRequiredMixin, View):
    """Same gate as roster/upload_views.py — HR/Admin or any manager with a
    non-empty managed scope (reports-to downline or Department.additional_managers)."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not has_managed_scope(
            request.user, request=request,
        ):
            raise PermissionDenied(
                "This upload is restricted to managers and HR."
            )
        return super().dispatch(request, *args, **kwargs)


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False))


def _respond(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, FRAGMENT_TEMPLATE, ctx)
    return render(request, PAGE_TEMPLATE, ctx)


def _editable_pks(user, request) -> set:
    if can_edit_all(user, request=request):
        return set(User.objects.filter(is_active=True).values_list("pk", flat=True))
    return managed_scope_pks(user, request=request)


class UploadEmployeeDataView(_ManagedScopeRequired):
    def get(self, request) -> HttpResponse:
        return render(request, PAGE_TEMPLATE, {
            "page_title": "Bulk update employee data",
            "can_edit_all": can_edit_all(request.user, request=request),
        })

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {
            "page_title": "Bulk update employee data",
            "preview": preview,
            "can_edit_all": can_edit_all(request.user, request=request),
        }

        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond(request, ctx)

        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = (
                f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            )
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond(request, ctx)

        try:
            parsed, parse_errors, dup_errors, warnings = parse_upload(
                upload, upload.name, viewer=request.user, request=request,
            )
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond(request, ctx)

        report = ApplyReport(parse_errors=parse_errors, duplicates=dup_errors, warnings=warnings)

        if parse_errors or dup_errors:
            ctx["report"] = report
            ctx["aborted"] = True
            if not _is_htmx(request):
                messages.error(
                    request,
                    "Upload aborted — fix the errors below and re-upload.",
                )
            return _respond(request, ctx)

        if preview:
            ctx.update({
                "report": report,
                "parsed_preview": parsed,
                # Payroll note: a moved confirmation date moves the night
                # allowance start (payroll/inputs.py::confirmed_from).
                "confirmation_changes": sum(
                    1 for r in parsed
                    if "confirmation_date" in r.changes
                    and r.changes["confirmation_date"] != r.employee.confirmation_date
                ),
                "preview": True,
            })
            if not _is_htmx(request):
                messages.info(
                    request,
                    f"Preview only — {len(parsed)} row(s) would be applied.",
                )
            return _respond(request, ctx)

        editable_pks = _editable_pks(request.user, request)
        report = apply_upload(request.user, parsed, editable_pks, warnings=warnings)
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                messages.success(
                    request,
                    f"Applied {report.total_writes} row(s): "
                    f"{len(report.created)} created, {len(report.updated)} updated."
                    + (
                        f" {len(report.skipped_scope)} skipped (outside scope)."
                        if report.skipped_scope else ""
                    ),
                )
            elif report.skipped_scope:
                messages.warning(
                    request,
                    "Nothing applied — every row was outside your managed scope.",
                )
            else:
                messages.info(request, "No rows to apply.")

        return _respond(request, ctx)


class UploadEmployeeDataTemplateView(_ManagedScopeRequired):
    http_method_names = ["get"]

    def get(self, request, fmt) -> HttpResponse:
        return render_template_response(request.user, fmt, request=request)
