"""Bulk new-joiner onboarding — page + template/results download.

Mirrors employees/upload_views.py and payroll/upload_views.py's
preview/apply htmx flow. Gated by HRRequiredMixin (can_view_salary_history —
the HR group or a superuser), narrower than /users/create/'s
StaffRequiredMixin, because the sheet, the preview and the results workbook
all carry gross salary (docs/PAYROLL_DATA_PROTECTION_PLAN.md §3, 2026-10-05).
"""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from accounts.upload import (
    ApplyReport,
    apply_upload,
    build_onboard_results_xlsx,
    build_onboard_xlsx_template,
    parse_upload,
)
from accounts.views import HRRequiredMixin

User = get_user_model()

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

PAGE_TEMPLATE = "accounts/onboard_upload.html"
FRAGMENT_TEMPLATE = "accounts/_onboard_upload_report.html"


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False))


def _respond(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, FRAGMENT_TEMPLATE, ctx)
    return render(request, PAGE_TEMPLATE, ctx)


class OnboardUploadView(HRRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        return render(request, PAGE_TEMPLATE, {"page_title": "Bulk create new joiners"})

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {"page_title": "Bulk create new joiners", "preview": preview}

        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond(request, ctx)

        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond(request, ctx)

        try:
            parsed, parse_errors, dup_errors, warnings = parse_upload(upload, upload.name, today=date.today())
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
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} row(s) would be created.")
            return _respond(request, ctx)

        report = apply_upload(request.user, parsed, warnings=warnings)
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                messages.success(
                    request,
                    f"Created {report.total_writes} new employee(s)."
                    + (f" {len(report.apply_errors)} failed." if report.apply_errors else ""),
                )
            else:
                messages.info(request, "No rows to apply.")

        return _respond(request, ctx)


class OnboardUploadTemplateView(HRRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_onboard_xlsx_template()
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        stamp = date.today().isoformat()
        resp["Content-Disposition"] = f'attachment; filename="new-joiners-upload-template-{stamp}.xlsx"'
        return resp


class OnboardResultsView(HRRequiredMixin, View):
    """Downloads the filled-in results workbook for the rows just created.

    The apply-report page posts the created employees' PKs (and their
    freshly-generated plaintext passwords, in the same order — never
    persisted server-side, this is the only place they exist outside the
    apply response that generated them) here in a hidden form.
    HRRequiredMixin already grants this viewer org-wide read access (HR is in
    ALL_DEPARTMENTS_GROUPS), so re-querying by PK carries no extra exposure
    over what /users/ already shows them.
    """

    http_method_names = ["post"]

    def post(self, request) -> HttpResponse:
        pks = request.POST.getlist("pk")
        passwords = dict(zip(pks, request.POST.getlist("password")))
        employees = list(
            User.objects.filter(pk__in=pks)
            .select_related("department", "designation", "team", "job_location",
                             "work_location", "employment_type", "job_info", "job_info__reports_to")
            .prefetch_related("salary_structures")
            .annotate(employee_id_num=Cast("employee_id", IntegerField()))
            .order_by("employee_id_num")
        )
        body = build_onboard_results_xlsx(employees, passwords)
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        stamp = date.today().isoformat()
        resp["Content-Disposition"] = f'attachment; filename="new-joiners-created-{stamp}.xlsx"'
        return resp
