"""AL historical-bank upload — page + template download.

Mirrors payroll/upload_views.py's UploadSalaryView shape: restricted via
ProtectedView.allowed_groups (dynamic_allowed_users), not
has_managed_scope/visible_employee_pks, since this is an HR-wide one-time
backfill, not a per-manager scoped action.

After deploy, run `manage.py sync_protected_views` and set
allowed_groups=["HR"] (or similar) on the `leave_al_bank_upload` and
`leave_al_bank_upload_template` ProtectedView rows via Django admin — same
manual step payroll_salary_upload's own rollout needed.
"""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View

from accounts.permissions import can_edit_all, has_managed_scope
from dynamic_menu.decorators import dynamic_allowed_users
from leave.upload_bank import (
    BankApplyReport,
    apply_bank_upload,
    build_bank_xlsx_template,
    parse_bank_upload,
)
from leave.forms import ALHistoryUploadForm
from leave.upload_al_history import (
    ALHistoryApplyReport,
    apply_al_history_upload,
    build_al_history_xlsx_template,
    parse_al_history_upload,
)
from leave.upload_cpl import (
    ApplyReport as CplApplyReport,
    apply_upload as apply_cpl_upload,
    build_cpl_grant_upload_template,
    parse_upload as parse_cpl_upload,
    preview_upload as preview_cpl_upload,
)


MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

PAGE_TEMPLATE = "leave/al_bank_upload.html"
FRAGMENT_TEMPLATE = "leave/_al_bank_upload_report.html"


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False))


def _respond(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, FRAGMENT_TEMPLATE, ctx)
    return render(request, PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadAnnualLeaveBankView(LoginRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        return render(request, PAGE_TEMPLATE, {"page_title": "Upload AL bank"})

    def post(self, request) -> HttpResponse:
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {"page_title": "Upload AL bank", "preview": preview}

        upload = request.FILES.get("file")
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
            parsed, parse_errors, dup_errors = parse_bank_upload(upload, upload.name)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond(request, ctx)

        report = BankApplyReport(parse_errors=parse_errors, duplicates=dup_errors)

        if parse_errors or dup_errors:
            ctx["report"] = report
            ctx["aborted"] = True
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} row(s) would be applied.")
            return _respond(request, ctx)

        report = apply_bank_upload(request.user, parsed)
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                messages.success(
                    request,
                    f"Imported {report.total_writes} row(s)."
                    + (
                        f" {len(report.skipped_existing)} skipped (already imported)."
                        if report.skipped_existing else ""
                    ),
                )
            elif report.skipped_existing:
                messages.warning(request, "Nothing imported — every row was already imported.")
            else:
                messages.info(request, "No rows to import.")

        return _respond(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class AnnualLeaveBankUploadTemplateView(LoginRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_bank_xlsx_template()
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        stamp = date.today().isoformat()
        resp["Content-Disposition"] = f'attachment; filename="al-bank-upload-template-{stamp}.xlsx"'
        return resp


# ---------------------------------------------------------------------------
# Pre-Orbis annual-leave record upload (docs/LEGACY_60_IMPORT_PLAN.md §4)
# ---------------------------------------------------------------------------

AL_HISTORY_PAGE_TEMPLATE = "leave/al_history_upload.html"
AL_HISTORY_FRAGMENT_TEMPLATE = "leave/_al_history_upload_report.html"


def _respond_al_history(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, AL_HISTORY_FRAGMENT_TEMPLATE, ctx)
    return render(request, AL_HISTORY_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadAnnualLeaveHistoryView(LoginRequiredMixin, View):
    """HR's pre-Orbis AL figures, as given, into LegacyAnnualLeaveRecord —
    Service Book only, never the ledger. ProtectedView-gated like the bank
    uploader (HR / User Admins via seed_orbis)."""

    def get(self, request) -> HttpResponse:
        return render(request, AL_HISTORY_PAGE_TEMPLATE, {
            "page_title": "Import pre-Orbis AL record", "form": ALHistoryUploadForm(),
        })

    def post(self, request) -> HttpResponse:
        form = ALHistoryUploadForm(request.POST)
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx: dict = {"page_title": "Import pre-Orbis AL record", "preview": preview, "form": form}

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_al_history(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_al_history(request, ctx)
        if not form.is_valid():
            ctx["inline_error"] = "Enter the as-of date the figures are stated at and re-submit."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_al_history(request, ctx)
        as_of = form.cleaned_data["as_of"]
        ctx["as_of"] = as_of

        try:
            parsed, parse_errors, dup_errors, skipped, warnings = parse_al_history_upload(
                upload, upload.name, as_of=as_of,
            )
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_al_history(request, ctx)

        report = ALHistoryApplyReport(
            parse_errors=parse_errors, duplicates=dup_errors, skipped_ineligible=skipped, warnings=warnings,
        )
        if parse_errors or dup_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_al_history(request, ctx)
        if preview:
            ctx.update({"report": report, "parsed_preview": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} record(s) would be written, {len(skipped)} skipped.")
            return _respond_al_history(request, ctx)

        applied = apply_al_history_upload(request.user, parsed, as_of=as_of, source_file=upload.name)
        applied.skipped_ineligible, applied.warnings = skipped, warnings
        ctx["report"] = applied
        if not _is_htmx(request):
            if applied.total_writes:
                messages.success(
                    request,
                    f"Wrote {applied.total_writes} record(s) as of {as_of.isoformat()} "
                    f"({len(applied.created)} new, {len(applied.updated)} replaced)."
                    + (f" {len(skipped)} skipped as ineligible." if skipped else ""),
                )
            else:
                messages.info(request, "No records written.")
        return _respond_al_history(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class AnnualLeaveHistoryUploadTemplateView(LoginRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        as_of = None
        raw = request.GET.get("as_of")
        if raw:
            try:
                as_of = date.fromisoformat(raw)
            except ValueError:
                as_of = None
        resp = HttpResponse(
            build_al_history_xlsx_template(as_of=as_of),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="al-history-upload-template-{date.today().isoformat()}.xlsx"'
        return resp


# ---------------------------------------------------------------------------
# Compensatory leave grant upload
# ---------------------------------------------------------------------------

CPL_PAGE_TEMPLATE = "leave/cpl_grant_upload.html"
CPL_FRAGMENT_TEMPLATE = "leave/_cpl_grant_upload_report.html"


class CplUploaderRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """HR/Admins, or any manager with a managed scope (reportees, or a
    department/team grant) — per-row scope is still enforced separately
    by leave.upload_cpl.parse_upload (accounts.permissions::
    can_manage_cpl_grants), this just keeps the page itself away from a
    plain employee with nobody to manage."""

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            can_edit_all(user, request=self.request)
            or has_managed_scope(user, request=self.request)
        )


class CplGrantUploadView(CplUploaderRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        return render(request, CPL_PAGE_TEMPLATE, {"page_title": "Upload CpL grants"})

    def post(self, request) -> HttpResponse:
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {"page_title": "Upload CpL grants", "preview": preview}

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _cpl_respond(request, ctx)

        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _cpl_respond(request, ctx)

        try:
            parsed, parse_errors = parse_cpl_upload(upload, upload.name, viewer=request.user)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _cpl_respond(request, ctx)

        if parse_errors:
            report = CplApplyReport(parse_errors=parse_errors)
            ctx["report"] = report
            ctx["aborted"] = True
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _cpl_respond(request, ctx)

        if preview:
            report = preview_cpl_upload(parsed, viewer=request.user)
            ctx.update({"report": report, "preview": True})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {report.total_writes} row(s) would be imported.")
            return _cpl_respond(request, ctx)

        report = apply_cpl_upload(parsed, viewer=request.user)
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                messages.success(
                    request,
                    f"Imported {report.total_writes} row(s)."
                    + (f" {len(report.skipped)} skipped (already imported)." if report.skipped else "")
                    + (f" {len(report.apply_errors)} failed." if report.apply_errors else ""),
                )
            else:
                messages.info(request, "No rows to import.")

        return _cpl_respond(request, ctx)


def _cpl_respond(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, CPL_FRAGMENT_TEMPLATE, ctx)
    return render(request, CPL_PAGE_TEMPLATE, ctx)


class CplGrantUploadTemplateView(CplUploaderRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_cpl_grant_upload_template()
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = 'attachment; filename="cpl-grant-upload-template.xlsx"'
        return resp
