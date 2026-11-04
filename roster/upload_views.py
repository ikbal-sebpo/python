"""Roster upload — page + template download."""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from accounts.permissions import has_managed_scope, visible_employee_pks
from roster.upload import (
    ApplyReport,
    apply_upload,
    parse_upload,
    render_template_response,
)


MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 MB

PAGE_TEMPLATE = "roster/upload.html"
FRAGMENT_TEMPLATE = "roster/_upload_report.html"


class _ManagedScopeRequired(LoginRequiredMixin, View):
    """Mirror the gate at roster.views.RosterWeekView.dispatch."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not has_managed_scope(
            request.user, request=request,
        ):
            raise PermissionDenied(
                "Roster upload is restricted to managers and HR."
            )
        return super().dispatch(request, *args, **kwargs)


def _is_htmx(request) -> bool:
    """django-htmx middleware sets `request.htmx` truthy on htmx requests."""
    return bool(getattr(request, "htmx", False))


def _respond(request, ctx: dict) -> HttpResponse:
    """Render the result fragment for HTMX requests; the full page for plain
    submits and GETs. Either way the file input in the form is preserved
    (HTMX path: the form element isn't swapped; full-page path: legacy
    behavior, file would need re-attaching — only used as a JS-off fallback)."""
    if _is_htmx(request):
        return render(request, FRAGMENT_TEMPLATE, ctx)
    return render(request, PAGE_TEMPLATE, ctx)


class UploadRosterView(_ManagedScopeRequired):
    def get(self, request) -> HttpResponse:
        return render(request, PAGE_TEMPLATE, {"page_title": "Upload roster"})

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {"page_title": "Upload roster", "preview": preview}

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
            parsed, parse_errors, dup_errors = parse_upload(
                upload, upload.name, today=date.today(),
            )
        except ValueError as exc:
            # Header-level / wholly-malformed file → single banner error.
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond(request, ctx)

        # Build a baseline report so the template can render parse errors even
        # when nothing was applied.
        report = ApplyReport(parse_errors=parse_errors, duplicates=dup_errors)

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
            # Dry-run: build a synthetic report without writing.
            ctx.update({
                "report": report,
                "parsed_preview": parsed,
                "preview": True,
            })
            if not _is_htmx(request):
                messages.info(
                    request,
                    f"Preview only — {len(parsed)} row(s) would be applied.",
                )
            return _respond(request, ctx)

        override_leave = request.POST.get("override_leave") == "1"

        visible_pks = visible_employee_pks(request.user, request=request)
        report = apply_upload(
            request.user, parsed, visible_pks, override_leave=override_leave,
        )
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
                    )
                    + (
                        f" {len(report.skipped_past_lwd)} skipped "
                        "(past last working date)."
                        if report.skipped_past_lwd else ""
                    )
                    + (
                        f" {len(report.skipped_before_joining)} skipped "
                        "(before joining date)."
                        if report.skipped_before_joining else ""
                    )
                    + (
                        f" {len(report.skipped_night_window)} skipped "
                        "(dedicated night shift window — department head only)."
                        if report.skipped_night_window else ""
                    )
                    + (
                        f" {len(report.overridden_leave)} overrode approved leave."
                        if report.overridden_leave else ""
                    ),
                )
            elif report.skipped_past_lwd and not report.skipped_scope and not report.skipped_before_joining:
                messages.warning(
                    request,
                    "Nothing applied — every row was past the employee's "
                    "last working date.",
                )
            elif report.skipped_before_joining and not report.skipped_scope and not report.skipped_past_lwd:
                messages.warning(
                    request,
                    "Nothing applied — every row was before the employee's "
                    "joining date.",
                )
            elif report.skipped_night_window and not report.skipped_scope:
                messages.warning(
                    request,
                    "Nothing applied — the remaining rows fell in a dedicated "
                    "night shift window; only the department head can set "
                    "working shifts other than NIG/CUST there.",
                )
            elif report.skipped_scope:
                messages.warning(
                    request,
                    "Nothing applied — every row was outside your managed scope.",
                )
            else:
                messages.info(request, "No rows to apply.")

        return _respond(request, ctx)


class UploadTemplateView(_ManagedScopeRequired):
    http_method_names = ["get"]

    def get(self, request, fmt) -> HttpResponse:
        return render_template_response(request.user, fmt)
