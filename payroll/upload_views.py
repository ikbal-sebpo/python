"""Overtime upload — page + dry-run/apply. Mirrors roster/upload_views.py's shape."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from accounts.permissions import (
    can_bulk_upload_payroll_notes, has_managed_scope, payroll_visible_employee_pks,
    writable_payroll_note_roles,
)
from payroll.services import LOCKED_RUN_STATUSES, OvertimeWindow
from dynamic_menu.decorators import dynamic_allowed_users
from dynamic_menu.models import ProtectedView
from payroll.forms import SalaryUploadMetaForm
from payroll.models import (
    OvertimeEntry, PayComponent, PayrollInputBatch, PayrollPeriod, PayrollRun,
    PayrollRunEmployeeNote, SalaryStructure,
)
from payroll.upload import ApplyReport, apply_upload, build_overtime_xlsx_template, parse_upload
from payroll.upload import SalaryApplyReport, apply_salary_upload, build_salary_xlsx_template, parse_salary_upload
from payroll.upload import (
    SalaryHistoryApplyReport, apply_salary_history_upload, build_salary_history_xlsx_template,
    parse_salary_history_upload,
)
from payroll.upload import NoteApplyReport, apply_notes_upload, build_notes_xlsx_template, parse_notes_upload
from payroll.upload import SlaApplyReport, apply_sla_upload, build_sla_xlsx_template, parse_sla_upload
from payroll.upload import (
    TestSalaryApplyReport, apply_test_salary_upload, build_test_salary_xlsx_template,
    parse_test_salary_upload,
)
from payroll.upload import GoLiveApplyReport, apply_go_live_upload, build_go_live_xlsx_template, parse_go_live_upload
from payroll.upload import (
    SalaryLoanApplyReport, apply_salary_loan_upload, build_salary_loan_xlsx_template, parse_salary_loan_upload,
)
from payroll.upload import (
    LegacyPayrollApplyReport, apply_legacy_payroll_upload, build_legacy_payroll_xlsx_template,
    parse_legacy_payroll_upload,
)
from payroll.upload import (
    TaxChallanApplyReport, apply_tax_challan_upload, build_tax_challan_xlsx_template, parse_tax_challan_upload,
)
from payroll.tax_ledger import (
    ALLOCATION_HEADERS, build_month_ledger_xlsx, challan_allocation_summary, ledger_headers, legacy_month_summary,
    legacy_months, tax_year_months,
)
from payroll.upload import (
    ChallanAllocationApplyReport, apply_challan_allocation_upload, build_challan_allocation_xlsx_template,
    parse_challan_allocation_upload,
)
from payroll.upload import (
    TaxOpeningApplyReport, apply_tax_opening_upload, build_tax_opening_xlsx_template, parse_tax_opening_upload,
)


MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

PAGE_TEMPLATE = "payroll/overtime_upload.html"
FRAGMENT_TEMPLATE = "payroll/_overtime_upload_report.html"

SALARY_PAGE_TEMPLATE = "payroll/salary_upload.html"
SALARY_FRAGMENT_TEMPLATE = "payroll/_salary_upload_report.html"
SALARY_HISTORY_PAGE_TEMPLATE = "payroll/salary_history_upload.html"
SALARY_HISTORY_FRAGMENT_TEMPLATE = "payroll/_salary_history_upload_report.html"
TAX_OPENING_PAGE_TEMPLATE = "payroll/tax_opening_upload.html"
TAX_OPENING_FRAGMENT_TEMPLATE = "payroll/_tax_opening_upload_report.html"
LEGACY_PAYROLL_PAGE_TEMPLATE = "payroll/legacy_payroll_upload.html"
LEGACY_PAYROLL_FRAGMENT_TEMPLATE = "payroll/_legacy_payroll_upload_report.html"
TAX_CHALLAN_PAGE_TEMPLATE = "payroll/tax_challan_upload.html"
TAX_CHALLAN_FRAGMENT_TEMPLATE = "payroll/_tax_challan_upload_report.html"
CHALLAN_ALLOCATION_PAGE_TEMPLATE = "payroll/tax_challan_allocation_upload.html"
CHALLAN_ALLOCATION_FRAGMENT_TEMPLATE = "payroll/_tax_challan_allocation_upload_report.html"
LOAN_PAGE_TEMPLATE = "payroll/loan_upload.html"
LOAN_FRAGMENT_TEMPLATE = "payroll/_loan_upload_report.html"

NOTES_PAGE_TEMPLATE = "payroll/notes_upload.html"
NOTES_FRAGMENT_TEMPLATE = "payroll/_notes_upload_report.html"


class ManagedScopeRequiredMixin(LoginRequiredMixin, View):
    """Shared by the overtime uploader and the overtime management views
    (payroll/views.py imports it) — a supervisor with at least one reportee,
    or HR/Admins. Row-level scoping is payroll_visible_employee_pks' job."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not has_managed_scope(request.user, request=request):
            raise PermissionDenied("Overtime upload is restricted to managers and HR.")
        return super().dispatch(request, *args, **kwargs)


def _is_htmx(request) -> bool:
    return bool(getattr(request, "htmx", False))


def _respond(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, FRAGMENT_TEMPLATE, ctx)
    return render(request, PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadOvertimeView(ManagedScopeRequiredMixin):
    """parse_upload still derives each row's own PayrollPeriod from its date
    rather than binding the whole file to one upload-time period (2026-09-07).

    What came back on 2026-09-15 is a *date window*, not a period binding:
    payroll.services.OvertimeWindow rejects rows dated before the open payroll
    period, in the future, or into an already-paid period. A file spanning two
    open periods still applies correctly to both.

    Rows landing in the most recently LOCKED period are not rejected — they
    apply as PENDING and wait for HR-Payroll to approve them (Q55), so late
    overtime is captured rather than lost. See docs/PAYROLL_PLAN.md.
    """

    def get(self, request) -> HttpResponse:
        return render(request, PAGE_TEMPLATE, {
            "page_title": "Upload overtime",
            "window": OvertimeWindow(request.user, today=date.today(), request=request),
        })

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        # Built up-front so every early return (no file, too large, parse
        # errors) still renders the page's accepted-date-range line.
        window = OvertimeWindow(request.user, today=date.today(), request=request)
        ctx: dict = {"page_title": "Upload overtime", "preview": preview, "window": window}

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
            parsed, parse_errors, dup_errors = parse_upload(
                upload, upload.name, today=date.today(),
            )
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond(request, ctx)

        report = ApplyReport(parse_errors=parse_errors, duplicates=dup_errors)

        if parse_errors or dup_errors:
            ctx["report"] = report
            ctx["aborted"] = True
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond(request, ctx)

        # Date-window gate (2026-09-15). Applied here rather than inside
        # parse_upload because the rule depends on WHO is uploading, and
        # parse_upload is deliberately a pure, user-agnostic parser. Doing it
        # before the preview branch is what makes a Preview show these
        # rejections instead of promising rows that would then be skipped.
        parsed, out_of_window = window.partition(parsed)
        report.skipped_window = out_of_window

        if preview:
            pending_preview = sum(
                1 for row in parsed
                if getattr(row, "window_status", None) == OvertimeEntry.Status.PENDING
            )
            ctx.update({
                "report": report, "parsed_preview": parsed, "preview": True,
                "pending_preview": pending_preview,
            })
            if not _is_htmx(request):
                note = (
                    f"Preview only — {len(parsed)} row(s) would be applied, each as "
                    "Pending review."
                )
                messages.info(request, note)
            return _respond(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.OVERTIME,
            source=PayrollInputBatch.Source.UPLOAD,
            # No single period — a file can legitimately span more than one
            # now that parse_upload derives each row's own period. period is
            # nullable on PayrollInputBatch specifically for this case.
            period=None,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed),
            added_by=request.user, updated_by=request.user,
        )
        # payroll_visible_employee_pks, not visible_employee_pks — a
        # supervisor must still be able to submit OT for someone who
        # resigned since the work was done (2026-09-08, see
        # accounts.permissions.payroll_visible_employee_pks's docstring).
        visible_pks = payroll_visible_employee_pks(request.user, request=request)
        report = apply_upload(request.user, parsed, visible_pks, batch=batch, window=window)
        report.skipped_window = out_of_window + report.skipped_window
        batch.status = (
            PayrollInputBatch.Status.APPLIED if not report.total_skipped or report.total_writes
            else PayrollInputBatch.Status.FAILED
        )
        batch.error_count = report.total_skipped
        batch.save(update_fields=["status", "error_count"])
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                skipped_bits = []
                if report.skipped_scope:
                    skipped_bits.append(f"{len(report.skipped_scope)} outside scope")
                if report.skipped_window:
                    skipped_bits.append(f"{len(report.skipped_window)} outside the open period")
                if report.skipped_locked:
                    skipped_bits.append(f"{len(report.skipped_locked)} already settled by HR")
                body = (
                    f"Applied {report.total_writes} row(s): "
                    f"{len(report.created)} created, {len(report.updated)} updated — "
                    "all Pending review until a manager reviews and the department "
                    "head approves them."
                )
                if report.resubmitted:
                    body += (
                        f" {len(report.resubmitted)} already-signed-off entr"
                        f"{'y' if len(report.resubmitted) == 1 else 'ies'} changed and went "
                        "back to Pending review."
                    )
                if report.unchanged:
                    body += f" {len(report.unchanged)} unchanged (sign-off kept)."
                if skipped_bits:
                    body += f" Skipped: {', '.join(skipped_bits)}."
                messages.success(request, body)
            elif report.skipped_locked:
                messages.warning(
                    request,
                    "Nothing applied — those entries are already approved or voided for "
                    "a locked period. Ask HR-Payroll to change them.",
                )
            elif report.skipped_window:
                messages.warning(
                    request,
                    f"Nothing applied — every row was dated outside the open payroll "
                    f"period (from {window.floor.isoformat()}).",
                )
            elif report.skipped_scope:
                messages.warning(request, "Nothing applied — every row was outside your managed scope.")
            else:
                messages.info(request, "No rows to apply.")

        return _respond(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class OvertimeUploadTemplateView(ManagedScopeRequiredMixin):
    """Downloads the blank .xlsx template — same managed-scope gate as
    UploadOvertimeView, via a separate named view/ProtectedView row."""

    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_overtime_xlsx_template(request.user)
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        stamp = date.today().isoformat()
        resp["Content-Disposition"] = f'attachment; filename="overtime-upload-template-{stamp}.xlsx"'
        return resp


def _respond_salary(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, SALARY_FRAGMENT_TEMPLATE, ctx)
    return render(request, SALARY_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadSalaryView(LoginRequiredMixin, View):
    """Gross-salary bulk uploader.

    Restricted to Finance via ProtectedView.allowed_groups (set in Django
    admin, or by accounts.management.commands.seed_orbis) — deliberately
    NOT scoped through has_managed_scope/visible_employee_pks like overtime,
    since Finance's access to gross salary is company-wide, not per-manager.
    """

    def get(self, request) -> HttpResponse:
        form = SalaryUploadMetaForm()
        return render(request, SALARY_PAGE_TEMPLATE, {
            "page_title": "Upload gross salary", "form": form,
            "reason_choices": SalaryStructure.Reason.choices,
        })

    def post(self, request) -> HttpResponse:
        form = SalaryUploadMetaForm(request.POST)
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {"page_title": "Upload gross salary", "preview": preview, "form": form}

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_salary(request, ctx)

        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_salary(request, ctx)

        if not form.is_valid():
            ctx["inline_error"] = "Fix the effective date / reason below and re-submit."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_salary(request, ctx)

        try:
            parsed, parse_errors, dup_errors = parse_salary_upload(upload, upload.name, today=date.today())
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_salary(request, ctx)

        report = SalaryApplyReport(parse_errors=parse_errors, duplicates=dup_errors)

        if parse_errors or dup_errors:
            ctx["report"] = report
            ctx["aborted"] = True
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_salary(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} row(s) would be applied.")
            return _respond_salary(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.SALARY,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed),
            added_by=request.user, updated_by=request.user,
        )
        report = apply_salary_upload(
            request.user, parsed,
            note=form.cleaned_data["note"],
            batch=batch,
        )
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.error_count = len(report.apply_errors)
        batch.save(update_fields=["status", "error_count"])
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                messages.success(
                    request,
                    f"Applied {report.total_writes} row(s): "
                    f"{len(report.created)} created, {len(report.updated)} updated."
                    + (f" {len(report.apply_errors)} failed." if report.apply_errors else ""),
                )
            else:
                messages.info(request, "No rows to apply.")

        return _respond_salary(request, ctx)


def _respond_salary_history(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, SALARY_HISTORY_FRAGMENT_TEMPLATE, ctx)
    return render(request, SALARY_HISTORY_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadSalaryHistoryView(LoginRequiredMixin, View):
    """Gross-salary HISTORY importer — the whole chain for employees with no
    salary records yet (docs/LEGACY_60_IMPORT_PLAN.md §3). Same Payroll
    Admins-only ProtectedView gate as UploadSalaryView: the file is gross
    salary."""

    def get(self, request) -> HttpResponse:
        return render(request, SALARY_HISTORY_PAGE_TEMPLATE, {
            "page_title": "Import salary history", "form": SalaryUploadMetaForm(),
            "reason_choices": SalaryStructure.Reason.choices,
        })

    def post(self, request) -> HttpResponse:
        form = SalaryUploadMetaForm(request.POST)
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx: dict = {"page_title": "Import salary history", "preview": preview, "form": form}

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_salary_history(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_salary_history(request, ctx)
        if not form.is_valid():
            ctx["inline_error"] = "Fix the note below and re-submit."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_salary_history(request, ctx)

        try:
            parsed, parse_errors, warnings = parse_salary_history_upload(upload, upload.name)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_salary_history(request, ctx)

        report = SalaryHistoryApplyReport(parse_errors=parse_errors, warnings=warnings)
        if parse_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Import aborted — fix the errors below and re-upload.")
            return _respond_salary_history(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} employee(s) would be imported.")
            return _respond_salary_history(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.SALARY,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=sum(len(h.rows) for h in parsed),
            added_by=request.user, updated_by=request.user,
        )
        note = form.cleaned_data["note"] or f"Salary history import from {upload.name}"
        report = apply_salary_history_upload(request.user, parsed, note=note, batch=batch)
        report.warnings = warnings
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.error_count = len(report.skipped_existing)
        batch.save(update_fields=["status", "error_count"])
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                messages.success(
                    request,
                    f"Imported {report.total_writes} salary record(s) for {report.employees_written} employee(s)."
                    + (f" {len(report.skipped_existing)} employee(s) skipped." if report.skipped_existing else ""),
                )
            else:
                messages.info(request, "Nothing imported.")
        return _respond_salary_history(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryHistoryUploadTemplateView(LoginRequiredMixin, View):
    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        resp = HttpResponse(
            build_salary_history_xlsx_template(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="salary-history-import-template-{date.today().isoformat()}.xlsx"'
        )
        return resp


def _respond_tax_opening(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, TAX_OPENING_FRAGMENT_TEMPLATE, ctx)
    return render(request, TAX_OPENING_PAGE_TEMPLATE, ctx)


def _tax_opening_year():
    """The active tax year the openings belong to — same pick as the engine."""
    from payroll.models import TaxYear
    return TaxYear.objects.filter(is_active=True).order_by("-start_date").first()


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadTaxOpeningView(LoginRequiredMixin, View):
    """Tax-year openings for the year-to-date TDS true-up
    (docs/TAX_CALCULATION_TRUEUP_PLAN.md §6): per employee, TDS already
    deducted and taxable extra pay received in the months of the tax year
    before Orbis took over (TaxYear.orbis_live_from). Payroll Admins only,
    via ProtectedView — the file is per-employee tax."""

    def _ctx(self, request, **extra) -> dict:
        tax_year = _tax_opening_year()
        return {
            "page_title": "Upload tax-year openings", "tax_year": tax_year,
            "live_from": tax_year.orbis_live_from if tax_year else None, **extra,
        }

    def get(self, request) -> HttpResponse:
        return render(request, TAX_OPENING_PAGE_TEMPLATE, self._ctx(request))

    def post(self, request) -> HttpResponse:
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx = self._ctx(request, preview=preview)
        tax_year = ctx["tax_year"]
        if tax_year is None:
            ctx["inline_error"] = "No active tax year — run seed_tax_year first."
            return _respond_tax_opening(request, ctx)

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_tax_opening(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_tax_opening(request, ctx)

        try:
            parsed, parse_errors = parse_tax_opening_upload(upload, upload.name)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_tax_opening(request, ctx)

        report = TaxOpeningApplyReport(parse_errors=parse_errors)
        if parse_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_tax_opening(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} employee(s) would be updated.")
            return _respond_tax_opening(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.TAX_OPENING,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed),
            added_by=request.user, updated_by=request.user,
        )
        report = apply_tax_opening_upload(request.user, parsed, tax_year=tax_year, batch=batch)
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.save(update_fields=["status"])
        ctx["report"] = report
        if not _is_htmx(request):
            messages.success(request, f"Openings written for {report.total_writes} employee(s).")
        return _respond_tax_opening(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class TaxOpeningUploadTemplateView(LoginRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        tax_year = _tax_opening_year()
        if tax_year is None:
            messages.error(request, "No active tax year — run seed_tax_year first.")
            return redirect("payroll_upload_center")
        response = HttpResponse(
            build_tax_opening_xlsx_template(tax_year),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="tax_openings_{tax_year.code}.xlsx"'
        return response


def _respond_tax_challan(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, TAX_CHALLAN_FRAGMENT_TEMPLATE, ctx)
    return render(request, TAX_CHALLAN_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadTaxChallanView(LoginRequiredMixin, View):
    """Tax challans (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §3): one row per
    treasury deposit of TDS — number, date, bank, total, optional payroll
    month (links the challan to that month's calculated run). Permanent:
    Finance records every month's deposit here; the tax certificate's
    challan table is a read of these rows. Finance + Payroll Admins via
    ProtectedView (company-level figures, not per-employee pay)."""

    def _ctx(self, request, **extra) -> dict:
        from payroll.models import TaxChallan
        tax_year = _tax_opening_year()
        recent = list(
            TaxChallan.objects.select_related("bank", "run__period", "tax_year").order_by("-deposit_date", "-pk")[:24]
        )
        return {"page_title": "Upload tax challans", "tax_year": tax_year, "recent_challans": recent, **extra}

    def get(self, request) -> HttpResponse:
        return render(request, TAX_CHALLAN_PAGE_TEMPLATE, self._ctx(request))

    def post(self, request) -> HttpResponse:
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx = self._ctx(request, preview=preview)
        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_tax_challan(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_tax_challan(request, ctx)
        try:
            parsed = parse_tax_challan_upload(upload, upload.name)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_tax_challan(request, ctx)
        if parsed.has_errors:
            ctx.update({"report": TaxChallanApplyReport(parse_errors=parsed.parse_errors), "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_tax_challan(request, ctx)
        if preview:
            ctx.update({"report": TaxChallanApplyReport(warnings=parsed.warnings), "parsed": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed.challans)} challan(s) would be written.")
            return _respond_tax_challan(request, ctx)
        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.TAX_CHALLAN,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed.challans),
            added_by=request.user, updated_by=request.user,
        )
        report = apply_tax_challan_upload(request.user, parsed, batch=batch)
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.save(update_fields=["status"])
        ctx = self._ctx(request, preview=False, report=report, parsed=parsed)
        if not _is_htmx(request):
            messages.success(request, f"{report.created} challan(s) created, {report.updated} updated.")
        return _respond_tax_challan(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class TaxChallanUploadTemplateView(LoginRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        response = HttpResponse(
            build_tax_challan_xlsx_template(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="tax_challans.xlsx"'
        return response


def _respond_legacy_payroll(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, LEGACY_PAYROLL_FRAGMENT_TEMPLATE, ctx)
    return render(request, LEGACY_PAYROLL_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadLegacyPayrollView(LoginRequiredMixin, View):
    """Pre-Orbis payroll months (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md §3):
    the one-time cutover sheet — one monthly payroll ledger row (every pay
    component) per employee per month before TaxYear.orbis_live_from.
    Challans are uploaded separately (UploadTaxChallanView) and referenced
    by number. Apply also rewrites the declaration openings the true-up
    reads. The same page lists what's in and exports any month of the year
    in the same shape. Payroll Admins only via ProtectedView."""

    def _ctx(self, request, **extra) -> dict:
        tax_year = _tax_opening_year()
        live_from = tax_year.orbis_live_from if tax_year else None
        return {
            "page_title": "Upload pre-Orbis payroll months", "tax_year": tax_year, "live_from": live_from,
            "legacy_months": legacy_months(tax_year) if tax_year else [],
            "month_summary": legacy_month_summary(tax_year) if tax_year else [],
            "ledger_headers": ledger_headers(template=True),
            **extra,
        }

    def get(self, request) -> HttpResponse:
        return render(request, LEGACY_PAYROLL_PAGE_TEMPLATE, self._ctx(request))

    def post(self, request) -> HttpResponse:
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx = self._ctx(request, preview=preview)
        tax_year = ctx["tax_year"]
        if tax_year is None:
            ctx["inline_error"] = "No active tax year — run seed_tax_year first."
            return _respond_legacy_payroll(request, ctx)

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_legacy_payroll(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_legacy_payroll(request, ctx)

        try:
            parsed = parse_legacy_payroll_upload(upload, upload.name, tax_year=tax_year)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_legacy_payroll(request, ctx)

        if parsed.has_errors:
            ctx.update({"report": LegacyPayrollApplyReport(parse_errors=parsed.parse_errors), "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_legacy_payroll(request, ctx)

        if preview:
            ctx.update({"report": LegacyPayrollApplyReport(warnings=parsed.warnings), "parsed": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed.months)} month row(s) would be written.")
            return _respond_legacy_payroll(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.LEGACY_MONTH,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed.months),
            added_by=request.user, updated_by=request.user,
        )
        report = apply_legacy_payroll_upload(request.user, parsed, tax_year=tax_year, batch=batch)
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.save(update_fields=["status"])
        ctx.update({"report": report, "parsed": parsed, "month_summary": legacy_month_summary(tax_year)})
        if not _is_htmx(request):
            messages.success(request, f"{report.total_writes} month row(s) written for {report.employees_updated} employee(s).")
        return _respond_legacy_payroll(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class LegacyPayrollUploadTemplateView(LoginRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        tax_year = _tax_opening_year()
        if tax_year is None:
            messages.error(request, "No active tax year — run seed_tax_year first.")
            return redirect("payroll_upload_center")
        if tax_year.orbis_live_from_id is None:
            messages.error(request, "Set 'Orbis live from' on the tax year in the admin first — it decides which months the template carries.")
            return redirect("payroll_legacy_payroll_upload")
        response = HttpResponse(
            build_legacy_payroll_xlsx_template(tax_year),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="pre_orbis_payroll_months_{tax_year.code}.xlsx"'
        return response


@method_decorator(dynamic_allowed_users, name="dispatch")
class LegacyPayrollMonthExportView(LoginRequiredMixin, View):
    """"Export month": the monthly tax ledger for any payroll month of the
    active tax year, in the import's own two-sheet shape — legacy rows for a
    pre-Orbis month, run-derived rows for an Orbis month. Same gate as the
    upload (per-employee pay and tax)."""

    def get(self, request) -> HttpResponse:
        tax_year = _tax_opening_year()
        if tax_year is None:
            messages.error(request, "No active tax year — run seed_tax_year first.")
            return redirect("payroll_upload_center")
        try:
            year, month = int(request.GET.get("year", "")), int(request.GET.get("month", ""))
        except ValueError:
            messages.error(request, "Pick a month to export.")
            return redirect("payroll_legacy_payroll_upload")
        if (year, month) not in set(tax_year_months(tax_year)):
            messages.error(request, f"{year}-{month:02d} is not a month of tax year {tax_year.code}.")
            return redirect("payroll_legacy_payroll_upload")
        response = HttpResponse(
            build_month_ledger_xlsx(tax_year, year, month),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="tax_ledger_{year}-{month:02d}.xlsx"'
        return response


def _respond_challan_allocation(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, CHALLAN_ALLOCATION_FRAGMENT_TEMPLATE, ctx)
    return render(request, CHALLAN_ALLOCATION_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadChallanAllocationView(LoginRequiredMixin, View):
    """TDS by challan, per employee (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md
    R2.3): Employee ID | Challan No | Amount — how much of each employee's
    tax a deposit carried. Permanent and monthly, after Finance has recorded
    the deposit on the Tax challans card. Pre-Orbis months and Orbis runs
    alike. Payroll Admins only via ProtectedView (per-employee tax)."""

    def _ctx(self, request, **extra) -> dict:
        tax_year = _tax_opening_year()
        return {
            "page_title": "Upload TDS by challan", "tax_year": tax_year,
            "allocation_headers": list(ALLOCATION_HEADERS),
            "challan_summary": challan_allocation_summary(tax_year) if tax_year else [],
            **extra,
        }

    def get(self, request) -> HttpResponse:
        return render(request, CHALLAN_ALLOCATION_PAGE_TEMPLATE, self._ctx(request))

    def post(self, request) -> HttpResponse:
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx = self._ctx(request, preview=preview)
        tax_year = ctx["tax_year"]
        if tax_year is None:
            ctx["inline_error"] = "No active tax year — run seed_tax_year first."
            return _respond_challan_allocation(request, ctx)
        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_challan_allocation(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_challan_allocation(request, ctx)
        try:
            parsed = parse_challan_allocation_upload(upload, upload.name, tax_year=tax_year)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_challan_allocation(request, ctx)
        if parsed.has_errors:
            ctx.update({"report": ChallanAllocationApplyReport(parse_errors=parsed.parse_errors), "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_challan_allocation(request, ctx)
        if preview:
            ctx.update({"report": ChallanAllocationApplyReport(warnings=parsed.warnings), "parsed": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed.rows)} allocation(s) would be written.")
            return _respond_challan_allocation(request, ctx)
        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.CHALLAN_ALLOC,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed.rows),
            added_by=request.user, updated_by=request.user,
        )
        report = apply_challan_allocation_upload(request.user, parsed, batch=batch)
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.save(update_fields=["status"])
        ctx = self._ctx(request, preview=False, report=report, parsed=parsed)
        if not _is_htmx(request):
            messages.success(request, f"{report.created} allocation(s) created, {report.updated} updated.")
        return _respond_challan_allocation(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class ChallanAllocationUploadTemplateView(LoginRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        tax_year = _tax_opening_year()
        if tax_year is None:
            messages.error(request, "No active tax year — run seed_tax_year first.")
            return redirect("payroll_upload_center")
        response = HttpResponse(
            build_challan_allocation_xlsx_template(tax_year),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="tds_by_challan_{tax_year.code}.xlsx"'
        return response


def _respond_loan(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, LOAN_FRAGMENT_TEMPLATE, ctx)
    return render(request, LOAN_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadSalaryLoanView(LoginRequiredMixin, View):
    """Cutover sheet for the loans live at go-live (docs/SALARY_LOAN_PLAN.md
    §5.5, Q72). Rows land ACTIVE with recorded_source=OUTSIDE_ORBIS; a file
    reference already in Orbis is corrected (opening + note), never
    duplicated. Payroll Admins only via ProtectedView."""

    def _ctx(self, request, **extra) -> dict:
        return {"page_title": "Upload salary loans (cutover)", **extra}

    def get(self, request) -> HttpResponse:
        return render(request, LOAN_PAGE_TEMPLATE, self._ctx(request))

    def post(self, request) -> HttpResponse:
        preview = (request.POST.get("action") or "apply").lower() == "preview"
        ctx = self._ctx(request, preview=preview)
        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_loan(request, ctx)
        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_loan(request, ctx)
        try:
            parsed, parse_errors = parse_salary_loan_upload(upload, upload.name)
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_loan(request, ctx)

        report = SalaryLoanApplyReport(parse_errors=parse_errors)
        if parse_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_loan(request, ctx)
        if preview:
            ctx.update({"report": report, "parsed_preview": parsed})
            if not _is_htmx(request):
                messages.info(request, f"Preview only — {len(parsed)} loan(s) would be written.")
            return _respond_loan(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.SALARY_LOAN,
            source=PayrollInputBatch.Source.UPLOAD,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed),
            added_by=request.user, updated_by=request.user,
        )
        report = apply_salary_loan_upload(request.user, parsed, batch=batch)
        batch.status = PayrollInputBatch.Status.APPLIED if report.total_writes else PayrollInputBatch.Status.FAILED
        batch.save(update_fields=["status"])
        ctx["report"] = report
        if not _is_htmx(request):
            messages.success(request, f"{len(report.created)} loan(s) created, {len(report.updated)} corrected.")
        return _respond_loan(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanUploadTemplateView(LoginRequiredMixin, View):
    def get(self, request) -> HttpResponse:
        response = HttpResponse(
            build_salary_loan_xlsx_template(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="salary_loans_cutover.xlsx"'
        return response


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryUploadTemplateView(LoginRequiredMixin, View):
    """Downloads the prefilled .xlsx template — same Finance-only gate as
    UploadSalaryView, via a separate named view/ProtectedView row."""

    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_salary_xlsx_template()
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        stamp = date.today().isoformat()
        resp["Content-Disposition"] = f'attachment; filename="gross-salary-upload-template-{stamp}.xlsx"'
        return resp


class NotesUploadAccessMixin(LoginRequiredMixin, View):
    """Gate for the bulk note uploader. Broader than ManagedScopeRequiredMixin
    (used by overtime): an HR-Payroll or Payroll Admins viewer with no
    reportees of their own must still reach this page to file HR/Finance
    notes in bulk — see accounts.permissions::can_bulk_upload_payroll_notes.
    Row/role-specific enforcement is still can_write_payroll_note, exactly
    as for the per-employee drawer."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_bulk_upload_payroll_notes(request.user, request=request):
            raise PermissionDenied("Bulk note upload is restricted to managers, HR-Payroll, and Payroll Admins.")
        return super().dispatch(request, *args, **kwargs)


def _is_htmx_notes(request) -> bool:
    return bool(getattr(request, "htmx", False))


def _respond_notes(request, ctx: dict) -> HttpResponse:
    if _is_htmx_notes(request):
        return render(request, NOTES_FRAGMENT_TEMPLATE, ctx)
    return render(request, NOTES_PAGE_TEMPLATE, ctx)


# Fixed priority, never a user choice: whichever of these the viewer is
# eligible for, first, wins — see _default_note_role. SUPERVISOR sits first
# on purpose. This uploader exists for the mass case (a supervisor filing
# notes for a department of hundreds); HR and Finance notes are few and
# sensitive by comparison. A viewer who happens to qualify for more than one
# role (e.g. HR-Payroll staff who also manages people, or a Payroll Admin
# with reports of their own) must never be handed a dropdown where a
# misclick quietly drops a note into the wrong bucket — so there isn't one.
# Someone who genuinely needs to bulk-file HR/Finance notes despite also
# having reportees isn't blocked; they just can't do it from this page —
# the per-employee drawer on the Support Info tab still writes any role
# they're authorized for, one employee at a time.
_ROLE_PRIORITY = ("SUPERVISOR", "HR", "FINANCE")


def _default_note_role(request) -> str | None:
    allowed = writable_payroll_note_roles(request.user, request=request)
    for role in _ROLE_PRIORITY:
        if role in allowed:
            return role
    return None


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadPayrollNotesView(NotesUploadAccessMixin):
    """Bulk-upload notes for one PayrollRun, for exactly one
    PayrollRunEmployeeNote.Role — the viewer's single _default_note_role,
    never a choice they make on the page (see that function's docstring).
    Scoped to a single run — payroll/upload.py's notes-upload section
    explains why (PayrollRunEmployeeNote FKs to PayrollRun, not
    PayrollRunEmployee).
    """

    def _base_ctx(self, request, run) -> dict:
        role = _default_note_role(request)
        role_label = dict(PayrollRunEmployeeNote.Role.choices).get(role)
        return {
            "page_title": "Upload notes", "run": run,
            "role": role, "role_label": role_label,
            "run_locked": run.status in LOCKED_RUN_STATUSES,
        }

    def get(self, request, pk) -> HttpResponse:
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        return render(request, NOTES_PAGE_TEMPLATE, self._base_ctx(request, run))

    def post(self, request, pk) -> HttpResponse:
        run = get_object_or_404(PayrollRun.objects.select_related("period"), pk=pk)
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx: dict = {**self._base_ctx(request, run), "preview": preview}
        role = ctx["role"]

        # Defense-in-depth, not a UI path: the page never offers a role
        # field to post, so this only fires if eligibility changed (e.g. a
        # group membership was pulled) between GET and POST.
        if role is None:
            ctx["inline_error"] = "You don't have write access to any note type on this run."
            if not _is_htmx_notes(request):
                messages.error(request, ctx["inline_error"])
            return _respond_notes(request, ctx)

        upload = request.FILES.get("file")
        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx_notes(request):
                messages.error(request, ctx["inline_error"])
            return _respond_notes(request, ctx)

        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx_notes(request):
                messages.error(request, ctx["inline_error"])
            return _respond_notes(request, ctx)

        try:
            parsed, parse_errors, dup_errors = parse_notes_upload(upload, upload.name, today=date.today())
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx_notes(request):
                messages.error(request, str(exc))
            return _respond_notes(request, ctx)

        report = NoteApplyReport(parse_errors=parse_errors, duplicates=dup_errors)

        if parse_errors or dup_errors:
            ctx["report"] = report
            ctx["aborted"] = True
            if not _is_htmx_notes(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_notes(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx_notes(request):
                messages.info(request, f"Preview only — up to {len(parsed)} row(s) would be applied.")
            return _respond_notes(request, ctx)

        report = apply_notes_upload(request.user, parsed, role, run=run, request=request)
        ctx["report"] = report

        if not _is_htmx_notes(request):
            if report.total_writes:
                skipped_bits = []
                if report.skipped_scope:
                    skipped_bits.append(f"{len(report.skipped_scope)} outside scope")
                if report.skipped_blank:
                    skipped_bits.append(f"{len(report.skipped_blank)} blank")
                if report.skipped_locked:
                    skipped_bits.append(f"{len(report.skipped_locked)} — run is locked")
                body = (
                    f"Saved {report.total_writes} note(s): "
                    f"{len(report.created)} added, {len(report.updated)} updated."
                )
                if skipped_bits:
                    body += f" Skipped: {', '.join(skipped_bits)}."
                messages.success(request, body)
            elif report.skipped_locked:
                messages.warning(
                    request,
                    "Nothing saved — this run's inputs are locked, so bulk note uploads are closed.",
                )
            elif report.skipped_scope:
                messages.warning(request, "Nothing saved — every row was outside your scope for this note type.")
            elif report.skipped_blank:
                messages.info(request, "Nothing saved — every row's Note cell was blank.")
            else:
                messages.info(request, "No rows to apply.")

        return _respond_notes(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class PayrollNotesUploadTemplateView(NotesUploadAccessMixin):
    """Downloads the .xlsx template for the viewer's _default_note_role —
    same role, same access gate, as UploadPayrollNotesView, via a separate
    named view/ProtectedView row (mirrors OvertimeUploadTemplateView)."""

    http_method_names = ["get"]

    def get(self, request, pk) -> HttpResponse:
        get_object_or_404(PayrollRun, pk=pk)  # 404 before 403 — a bad run id shouldn't look like a permission error
        role = _default_note_role(request)
        if role is None:
            raise PermissionDenied("No note type is available for you to upload.")
        body = build_notes_xlsx_template(request.user, role)
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        stamp = date.today().isoformat()
        resp["Content-Disposition"] = f'attachment; filename="payroll-notes-template-{role.lower()}-{stamp}.xlsx"'
        return resp


SLA_PAGE_TEMPLATE = "payroll/sla_upload.html"
SLA_FRAGMENT_TEMPLATE = "payroll/_sla_upload_report.html"


def _respond_sla(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, SLA_FRAGMENT_TEMPLATE, ctx)
    return render(request, SLA_PAGE_TEMPLATE, ctx)


def _open_period():
    """The period SLA pay lands in — the latest OPEN one, else the latest.

    SLA pay always lags the month it was earned for ("Meet SLA-August" is
    paid in the 16 Aug - 15 Sep run), so the payment period is a property of
    *when you're uploading*, not of the SLA month. That's why it isn't
    derived per row the way overtime derives a period from its work date.
    """
    return (
        PayrollPeriod.objects.filter(status=PayrollPeriod.Status.OPEN).order_by("-start_date").first()
        or PayrollPeriod.objects.order_by("-start_date").first()
    )


def _sla_component():
    return PayComponent.objects.filter(code="SLA_INCENTIVE", is_active=True).first()


def _previous_month(today: date) -> date:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadSlaView(ManagedScopeRequiredMixin):
    """SLA achievement bulk uploader — the supervisor ticks who met SLA and
    the system prices each row from PayRate (payroll/upload.py's SLA
    section explains why the amount is never a column).

    Same managed-scope gate as the overtime uploader: a supervisor uploads
    for their own reportees. Unlike overtime there is no date window —
    an SLA month is a claim about the past, and a retro claim for a missed
    month is legitimate (and expected: employee 3503 claimed July and
    August together in September 2026). The guard against paying one twice
    is VariablePayEntry.source_key's unique constraint, not a date rule.
    """

    def _base_ctx(self, request) -> dict:
        return {
            "page_title": "Upload SLA achievement",
            "period": _open_period(),
            "component": _sla_component(),
            "default_month": _previous_month(date.today()),
        }

    def get(self, request) -> HttpResponse:
        return render(request, SLA_PAGE_TEMPLATE, self._base_ctx(request))

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx = self._base_ctx(request)
        ctx["preview"] = preview
        period, component = ctx["period"], ctx["component"]

        if period is None:
            ctx["inline_error"] = "No payroll period exists yet — create one before uploading SLA achievement."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_sla(request, ctx)
        if component is None:
            ctx["inline_error"] = (
                "The SLA_INCENTIVE pay component is missing — run "
                "manage.py seed_payroll_components first."
            )
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_sla(request, ctx)

        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_sla(request, ctx)

        if upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_sla(request, ctx)

        try:
            parsed, parse_errors, dup_errors = parse_sla_upload(upload, upload.name, today=date.today())
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_sla(request, ctx)

        report = SlaApplyReport(parse_errors=parse_errors, duplicates=dup_errors)
        if parse_errors or dup_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_sla(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx(request):
                messages.info(
                    request,
                    f"Preview only — {len(parsed)} row(s) claim the incentive and would be "
                    "written as Pending for the department head to approve.",
                )
            return _respond_sla(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.VARIABLE_PAY,
            source=PayrollInputBatch.Source.UPLOAD,
            period=period,
            uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed),
            added_by=request.user, updated_by=request.user,
        )
        # payroll_visible_employee_pks, not visible_employee_pks — someone who
        # resigned after earning the SLA month must still be payable, same
        # reasoning as the overtime uploader.
        visible_pks = payroll_visible_employee_pks(request.user, request=request)
        report = apply_sla_upload(
            request.user, parsed, visible_pks, period=period, component=component, batch=batch,
        )
        batch.status = (
            PayrollInputBatch.Status.APPLIED if report.total_writes or not report.total_skipped
            else PayrollInputBatch.Status.FAILED
        )
        batch.error_count = report.total_skipped
        batch.save(update_fields=["status", "error_count"])
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                body = (
                    f"Applied {report.total_writes} row(s): {len(report.created)} created, "
                    f"{len(report.updated)} updated. All are Pending — the department head "
                    "must approve them, and the run must be recalculated, before they are paid."
                )
                skipped_bits = []
                if report.skipped_scope:
                    skipped_bits.append(f"{len(report.skipped_scope)} outside scope")
                if report.skipped_no_rate:
                    skipped_bits.append(f"{len(report.skipped_no_rate)} with no configured rate")
                if report.skipped_decided:
                    skipped_bits.append(f"{len(report.skipped_decided)} already decided")
                if skipped_bits:
                    body += f" Skipped: {', '.join(skipped_bits)}."
                messages.success(request, body)
            elif report.skipped_no_rate:
                messages.warning(
                    request,
                    "Nothing applied — no SLA incentive rate is configured for these "
                    "employees' department. Ask Finance to add one.",
                )
            elif report.skipped_decided:
                messages.warning(request, "Nothing applied — every row was already approved or rejected.")
            elif report.skipped_scope:
                messages.warning(request, "Nothing applied — every row was outside your managed scope.")
            else:
                messages.info(request, "No rows to apply — nothing in the file was marked as meeting SLA.")

        return _respond_sla(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class SlaUploadTemplateView(ManagedScopeRequiredMixin):
    """Prefilled .xlsx — one row per employee in the viewer's managed scope,
    names and IDs filled in, SLA month left blank for the supervisor to add
    only for whoever achieved it."""

    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        default_month = _previous_month(date.today())
        body = build_sla_xlsx_template(request.user, default_month=default_month)
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="sla-achievement-{default_month:%Y-%m}.xlsx"'
        )
        return resp


TEST_SALARY_PAGE_TEMPLATE = "payroll/test_salary_upload.html"
TEST_SALARY_FRAGMENT_TEMPLATE = "payroll/_test_salary_upload_report.html"


def _respond_test_salary(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, TEST_SALARY_FRAGMENT_TEMPLATE, ctx)
    return render(request, TEST_SALARY_PAGE_TEMPLATE, ctx)


def _test_salary_component():
    return PayComponent.objects.filter(code="TEST_SALARY_DEDUCTION", is_active=True).first()


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadTestSalaryView(ManagedScopeRequiredMixin):
    """Test Salary bulk uploader — a supervisor supplies the total day count
    for the period, the system prices the deduction.

    Same managed-scope gate and PENDING-then-department-head-approves flow as
    the SLA uploader next to it; only the pricing rule differs. See
    payroll/upload.py's Test Salary section for why the count is entered
    directly rather than derived from a date window.
    """

    def _base_ctx(self, request) -> dict:
        return {
            "page_title": "Upload test salary",
            "period": _open_period(),
            "component": _test_salary_component(),
        }

    def get(self, request) -> HttpResponse:
        return render(request, TEST_SALARY_PAGE_TEMPLATE, self._base_ctx(request))

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx = self._base_ctx(request)
        ctx["preview"] = preview
        period, component = ctx["period"], ctx["component"]

        if period is None:
            ctx["inline_error"] = "No payroll period exists yet — create one before uploading test salary."
        elif component is None:
            ctx["inline_error"] = (
                "The TEST_SALARY_DEDUCTION pay component is missing — run "
                "manage.py seed_payroll_components first."
            )
        elif upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
        elif upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
        if ctx.get("inline_error"):
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_test_salary(request, ctx)

        try:
            parsed, parse_errors, dup_errors = parse_test_salary_upload(
                upload, upload.name, today=date.today(),
            )
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_test_salary(request, ctx)

        report = TestSalaryApplyReport(parse_errors=parse_errors, duplicates=dup_errors)
        if parse_errors or dup_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_test_salary(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx(request):
                messages.info(
                    request,
                    f"Preview only — {len(parsed)} row(s) would be written as Pending "
                    "for the department head to approve. Amounts are computed on apply.",
                )
            return _respond_test_salary(request, ctx)

        batch = PayrollInputBatch.objects.create(
            input_kind=PayrollInputBatch.InputKind.VARIABLE_PAY,
            source=PayrollInputBatch.Source.UPLOAD,
            period=period, uploaded_file=upload,
            status=PayrollInputBatch.Status.VALIDATED,
            row_count=len(parsed),
            added_by=request.user, updated_by=request.user,
        )
        visible_pks = payroll_visible_employee_pks(request.user, request=request)
        report = apply_test_salary_upload(
            request.user, parsed, visible_pks, period=period, component=component, batch=batch,
        )
        batch.status = (
            PayrollInputBatch.Status.APPLIED if report.total_writes or not report.total_skipped
            else PayrollInputBatch.Status.FAILED
        )
        batch.error_count = report.total_skipped
        batch.save(update_fields=["status", "error_count"])
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_writes:
                total = sum((r.amount for r in report.created + report.updated), Decimal("0"))
                body = (
                    f"Applied {report.total_writes} row(s) totalling {total} Tk in deductions: "
                    f"{len(report.created)} created, {len(report.updated)} updated. All are Pending — "
                    "the department head must approve them, and the run must be recalculated, "
                    "before they take effect."
                )
                bits = []
                for label, rows in (
                    ("outside scope", report.skipped_scope),
                    ("with no configured rate", report.skipped_no_rate),
                    ("for trainees", report.skipped_trainee),
                    ("already decided", report.skipped_decided),
                ):
                    if rows:
                        bits.append(f"{len(rows)} {label}")
                if bits:
                    body += f" Skipped: {', '.join(bits)}."
                messages.success(request, body)
            elif report.skipped_no_rate:
                messages.warning(
                    request,
                    "Nothing applied — no test-salary rate is configured for these "
                    "employees' department. Ask Finance to add one.",
                )
            elif report.total_skipped:
                messages.warning(request, "Nothing applied — every row was skipped; see the report below.")
            else:
                messages.info(request, "No windows to apply.")

        return _respond_test_salary(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class TestSalaryUploadTemplateView(ManagedScopeRequiredMixin):
    """Blank .xlsx with a reference sheet of the viewer's managed employees."""

    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_test_salary_xlsx_template(request.user)
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="test-salary-{date.today().isoformat()}.xlsx"'
        )
        return resp


GO_LIVE_PAGE_TEMPLATE = "payroll/go_live_upload.html"
GO_LIVE_FRAGMENT_TEMPLATE = "payroll/_go_live_upload_report.html"


def _respond_go_live(request, ctx: dict) -> HttpResponse:
    if _is_htmx(request):
        return render(request, GO_LIVE_FRAGMENT_TEMPLATE, ctx)
    return render(request, GO_LIVE_PAGE_TEMPLATE, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadGoLiveView(ManagedScopeRequiredMixin):
    """Data Solutions trainee go-live bulk uploader — a supervisor supplies
    the new team and the real go-live date; the system prices the resulting
    salary revision from TeamSalaryPackage and proposes it for Finance.

    Narrower access than the other three managed-scope uploaders next to it
    (see seed_orbis.py's PROTECTED_VIEW_GROUPS): this changes
    CustomUser.team/JobInfo.resource_status directly, not just a payroll
    input row, so it's confined to payroll_ops rather than every supervisor.
    """

    def _base_ctx(self, request) -> dict:
        return {"page_title": "Upload go-live"}

    def get(self, request) -> HttpResponse:
        return render(request, GO_LIVE_PAGE_TEMPLATE, self._base_ctx(request))

    def post(self, request) -> HttpResponse:
        upload = request.FILES.get("file")
        action = (request.POST.get("action") or "apply").lower()
        preview = action == "preview"

        ctx = self._base_ctx(request)
        ctx["preview"] = preview

        if upload is None:
            ctx["inline_error"] = "Choose a .xlsx or .csv file before submitting."
        elif upload.size > MAX_UPLOAD_BYTES:
            ctx["inline_error"] = f"File is too large ({upload.size:,} bytes). Limit is 5 MB."
        if ctx.get("inline_error"):
            if not _is_htmx(request):
                messages.error(request, ctx["inline_error"])
            return _respond_go_live(request, ctx)

        try:
            parsed, parse_errors, dup_errors = parse_go_live_upload(
                upload, upload.name, today=date.today(),
            )
        except ValueError as exc:
            ctx["inline_error"] = str(exc)
            if not _is_htmx(request):
                messages.error(request, str(exc))
            return _respond_go_live(request, ctx)

        report = GoLiveApplyReport(parse_errors=parse_errors, duplicates=dup_errors)
        if parse_errors or dup_errors:
            ctx.update({"report": report, "aborted": True})
            if not _is_htmx(request):
                messages.error(request, "Upload aborted — fix the errors below and re-upload.")
            return _respond_go_live(request, ctx)

        if preview:
            ctx.update({"report": report, "parsed_preview": parsed, "preview": True})
            if not _is_htmx(request):
                messages.info(
                    request,
                    f"Preview only — {len(parsed)} row(s) would be processed. Nothing is "
                    "written until you apply.",
                )
            return _respond_go_live(request, ctx)

        visible_pks = payroll_visible_employee_pks(request.user, request=request)
        report = apply_go_live_upload(request.user, parsed, visible_pks)
        ctx["report"] = report

        if not _is_htmx(request):
            if report.total_applied:
                no_proposal = sum(1 for r in report.applied if not r.proposal_created)
                body = (
                    f"Processed {report.total_applied} go-live(s). "
                    f"{report.total_applied - no_proposal} salary proposal(s) sent to Finance "
                    "for approval."
                )
                if no_proposal:
                    body += (
                        f" {no_proposal} had no current salary on file to revise — the team/"
                        "status change was still applied; check with Finance."
                    )
                bits = []
                for label, rows in (
                    ("outside scope", report.skipped_scope),
                    ("not currently a trainee", report.skipped_not_trainee),
                    ("with no configured package", report.skipped_no_package),
                ):
                    if rows:
                        bits.append(f"{len(rows)} {label}")
                if bits:
                    body += f" Skipped: {', '.join(bits)}."
                messages.success(request, body)
            elif report.total_skipped:
                messages.warning(request, "Nothing applied — every row was skipped; see the report below.")
            else:
                messages.info(request, "No rows to apply.")

        return _respond_go_live(request, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class GoLiveUploadTemplateView(ManagedScopeRequiredMixin):
    """Blank .xlsx with a reference sheet of the viewer's managed trainees."""

    http_method_names = ["get"]

    def get(self, request) -> HttpResponse:
        body = build_go_live_xlsx_template(request.user)
        resp = HttpResponse(
            body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="go-live-{date.today().isoformat()}.xlsx"'
        )
        return resp


# ---------------------------------------------------------------------------
# Upload Center — one Payroll submenu listing every bulk uploader.
#
# Added 2026-09-21, replacing the standalone "Upload salary" submenu entry
# (seed_orbis.py's ORBIS_SUB_MENUS). Before this, every new upload type
# either needed its own sidebar row or, like SLA and Test Salary, got none
# at all — reachable only via a button on the Variable Pay list. Adding a
# future uploader here means one UPLOAD_CENTER_ENTRIES row, not a new menu
# item.
#
# Visibility per card mirrors the destination's OWN real gate, not just the
# broad ProtectedView group list every uploader shares under
# dynamic_allowed_users — that list is deliberately wide (it also covers
# HR/Finance/Payroll-ops oversight), so a plain "Supervisors" member with
# zero reportees would otherwise see the Overtime/SLA/Test Salary cards and
# then hit a PermissionDenied on click. Gross salary has no equivalent
# function of its own (UploadSalaryView is plain LoginRequiredMixin, gated
# purely by ProtectedView.allowed_groups — Finance, company-wide by design),
# so its card visibility re-checks that same ProtectedView row directly.
# ---------------------------------------------------------------------------

@dataclass
class UploadCenterEntry:
    title: str
    description: str
    icon: str
    upload_url: str
    template_url: str
    visible_if: Callable[[object, object], bool]


def _can_open_protected_view(user, view_name: str) -> bool:
    """Same check dynamic_allowed_users makes, for a card whose destination
    has no finer permission function of its own to reuse."""
    if getattr(user, "is_superuser", False):
        return True
    try:
        pv = ProtectedView.objects.get(view_name=view_name, is_active=True)
    except ProtectedView.DoesNotExist:
        return False
    return pv.allowed_groups.filter(pk__in=user.groups.values_list("pk", flat=True)).exists()


UPLOAD_CENTER_ENTRIES = [
    UploadCenterEntry(
        title="Overtime",
        description="Bulk-file overtime hours for your managed employees.",
        icon="clock",
        upload_url="payroll_overtime_upload",
        template_url="payroll_overtime_upload_template",
        visible_if=lambda user, request: has_managed_scope(user, request=request),
    ),
    UploadCenterEntry(
        title="SLA achievement",
        description="Bulk-file the SLA incentive for your managed employees.",
        icon="badge-check",
        upload_url="payroll_sla_upload",
        template_url="payroll_sla_upload_template",
        visible_if=lambda user, request: has_managed_scope(user, request=request),
    ),
    UploadCenterEntry(
        title="Test salary",
        description="Bulk-file Data Solutions' test-salary deduction (in days) for your managed employees.",
        icon="calendar-minus-2",
        upload_url="payroll_test_salary_upload",
        template_url="payroll_test_salary_upload_template",
        visible_if=lambda user, request: has_managed_scope(user, request=request),
    ),
    UploadCenterEntry(
        title="Gross salary",
        description="Bulk-upload compensation-master changes. Finance only, company-wide.",
        icon="banknote",
        upload_url="payroll_salary_upload",
        template_url="payroll_salary_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_salary_upload"),
    ),
    UploadCenterEntry(
        title="Salary history",
        description="One-time import of an employee's full gross-salary history (every past figure with its dates). Payroll Admins only.",
        icon="history",
        upload_url="payroll_salary_upload_history",
        template_url="payroll_salary_upload_history_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_salary_upload_history"),
    ),
    UploadCenterEntry(
        title="Tax-year openings",
        description="Cutover for the year-to-date TDS true-up: per employee, TDS already deducted (and taxable extra pay) in this tax year's months before Orbis took over. Payroll Admins only.",
        icon="calculator",
        upload_url="payroll_tax_opening_upload",
        template_url="payroll_tax_opening_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_tax_opening_upload"),
    ),
    UploadCenterEntry(
        title="Pre-Orbis payroll months (cutover)",
        description="Step 1 of the cutover, and the only upload payroll processing waits on: the months of this tax year paid before Orbis went live — exactly what each payroll paid each employee, per pay component, with TDS, net pay and the salary adjustment months settled. Rewrites the true-up openings; recalculate the open run after. Exports any month in the same shape. Payroll Admins only.",
        icon="calendar-range",
        upload_url="payroll_legacy_payroll_upload",
        template_url="payroll_legacy_payroll_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_legacy_payroll_upload"),
    ),
    UploadCenterEntry(
        title="Tax challans",
        description="Step 2, in Finance's own time: every treasury deposit of TDS — challan number, date, bank, total and the payroll month it paid — recorded as it is deposited, month by month. Nothing in payroll waits on it; the tax certificate (next July) and the TDS by challan sheet read these rows.",
        icon="receipt",
        upload_url="payroll_tax_challan_upload",
        template_url="payroll_tax_challan_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_tax_challan_upload"),
    ),
    UploadCenterEntry(
        title="TDS by challan (per employee)",
        description="Step 3, once a month's payroll figures and its challans are both in: which employees' tax each challan carried and how much of each. The template is prefilled from the month's TDS, challan number filled in where the month has one. Pre-Orbis months and Orbis runs alike. Payroll Admins only.",
        icon="list-checks",
        upload_url="payroll_tax_challan_allocation_upload",
        template_url="payroll_tax_challan_allocation_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_tax_challan_allocation_upload"),
    ),
    UploadCenterEntry(
        title="Salary loans (cutover)",
        description="The loans live at go-live: one row per approved loan with Finance's recovered-to-date figure. New loans are recorded one at a time on the Loans page. Payroll Admins only.",
        icon="hand-coins",
        upload_url="payroll_loan_upload",
        template_url="payroll_loan_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "payroll_loan_upload"),
    ),
    UploadCenterEntry(
        title="Pre-Orbis AL record",
        description="HR's annual-leave position as at the Orbis cut-over, stored as given for the Service Book only — never a ledger credit.",
        icon="archive",
        upload_url="leave_al_history_upload",
        template_url="leave_al_history_upload_template",
        visible_if=lambda user, request: _can_open_protected_view(user, "leave_al_history_upload"),
    ),
    UploadCenterEntry(
        title="Go live",
        description="Bulk-move Data Solutions trainees to their new team and propose the salary revision.",
        icon="rocket",
        upload_url="payroll_go_live_upload",
        template_url="payroll_go_live_upload_template",
        # Narrower than the other managed-scope uploaders — both the page's
        # ProtectedView group list AND has_managed_scope must pass, matching
        # UploadGoLiveView's own gate exactly (see its docstring).
        visible_if=lambda user, request: (
            _can_open_protected_view(user, "payroll_go_live_upload")
            and has_managed_scope(user, request=request)
        ),
    ),
]


@method_decorator(dynamic_allowed_users, name="dispatch")
class UploadCenterView(LoginRequiredMixin, View):
    """One page listing every payroll bulk uploader the viewer can use."""

    def get(self, request) -> HttpResponse:
        entries = [e for e in UPLOAD_CENTER_ENTRIES if e.visible_if(request.user, request)]
        return render(request, "payroll/upload_center.html", {
            "page_title": "Upload Center", "entries": entries,
        })
