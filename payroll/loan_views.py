"""Salary loans — list, drawers, document (docs/SALARY_LOAN_PLAN.md §5).

Access: ``can_manage_salary_loans`` (payroll-HR pair + Payroll Admins) for
everything on the list; ``can_disburse_salary_loans`` (Payroll Admins) for
the money-moving acts — disburse, skip, adjust. Every drawer targets
``#drawer-body`` so a validation error renders inside the drawer (the
2026-10-07 fix), and a successful POST answers 204 + HX-Redirect back to
the list so the row re-renders with its new figures.
"""

from __future__ import annotations

from datetime import date as _date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.db.models import Q

from accounts.filters import employee_search_q
from accounts.models import CustomUser
from accounts.permissions import can_disburse_salary_loans, can_manage_salary_loans, can_view_salary_loan
from core.pagination import paginate_list
from dynamic_menu.decorators import dynamic_allowed_users
from payroll.forms import (
    SalaryLoanAdjustmentForm, SalaryLoanCancelForm, SalaryLoanDisburseForm, SalaryLoanForm, SalaryLoanSkipForm,
)
from payroll.loans import loan_balances, loan_history, repayment_lines
from payroll.models import PayrollPeriod, PayrollRun, SalaryLoan, SalaryLoanSkip
from payroll.periods import first_loan_deduction_month, period_for_date

TABS = (
    ("active", "Active"),
    ("pending", "Awaiting disbursement"),
    ("settled", "Settled"),
    ("cancelled", "Cancelled"),
)


def _list_url(request) -> str:
    base = reverse("payroll_loan_list")
    keep = {k: v for k, v in request.GET.items() if k in ("tab", "q", "page", "per_page")}
    if keep:
        from urllib.parse import urlencode
        return f"{base}?{urlencode(keep)}"
    return base


def _done(request, message: str):
    messages.success(request, message)
    target = _list_url(request)
    if getattr(request, "htmx", False):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response
    return redirect(target)


class _LoanAccessMixin(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_manage_salary_loans(request.user, request=request):
            raise PermissionDenied("Salary loans are managed by Payroll-HR and Payroll Admins.")
        return super().dispatch(request, *args, **kwargs)

    def _loan(self, pk):
        return get_object_or_404(
            SalaryLoan.objects.select_related(
                "employee", "employee__department", "first_deduction_period", "disbursed_by",
            ),
            pk=pk,
        )


class _LoanDisburserMixin(_LoanAccessMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_disburse_salary_loans(request.user, request=request):
            raise PermissionDenied("Only Payroll Admins may do this.")
        return super().dispatch(request, *args, **kwargs)


def _decorate(loans, balances):
    for loan in loans:
        loan.balance = balances[loan.pk]


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanListView(_LoanAccessMixin):
    template_name = "payroll/loan_list.html"

    def get(self, request):
        tab = (request.GET.get("tab") or "active").lower()
        if tab not in dict(TABS):
            tab = "active"
        q = (request.GET.get("q") or "").strip()

        qs = SalaryLoan.objects.select_related(
            "employee", "employee__department", "employee__team", "first_deduction_period", "disbursed_by",
        )
        if q:
            qs = qs.filter(
                employee_search_q(q, prefix="employee__") | Q(file_reference__icontains=q)
            )
        loans = list(qs)
        balances = loan_balances(loans)
        _decorate(loans, balances)

        buckets = {"active": [], "pending": [], "settled": [], "cancelled": []}
        for loan in loans:
            if loan.is_cancelled:
                buckets["cancelled"].append(loan)
            elif loan.is_pending:
                buckets["pending"].append(loan)
            elif loan.balance.is_settled:
                buckets["settled"].append(loan)
            else:
                buckets["active"].append(loan)
        rows = buckets[tab]
        if tab == "active":
            rows.sort(key=lambda l: (l.employee.employee_id or "", l.approved_on))
        ctx = paginate_list(request, rows)
        ctx.update({
            "page_title": "Salary loans",
            "tab": tab, "q": q,
            "tabs": [(key, label, len(buckets[key])) for key, label in TABS],
            "can_disburse": can_disburse_salary_loans(request.user, request=request),
            "missing_documents": sum(1 for l in loans if l.needs_document),
        })
        return render(request, self.template_name, ctx)


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanEditView(_LoanAccessMixin):
    """Add (no pk) or edit one loan — the drawer behind "Add loan" and the
    pencil. Terms lock once an instalment exists in a calculated run."""

    template_name = "payroll/_loan_drawer.html"

    def _form(self, data=None, files=None, instance=None):
        terms_locked = bool(instance and repayment_lines([instance.pk]).exists())
        return SalaryLoanForm(
            data, files, instance=instance, viewer=self.request.user,
            can_set_opening=can_disburse_salary_loans(self.request.user, request=self.request),
            terms_locked=terms_locked,
        )

    def _render(self, request, form, loan, *, status=200):
        return render(request, self.template_name, {
            "form": form, "loan": loan,
            "action_url": reverse("payroll_loan_edit", args=[loan.pk]) if loan else reverse("payroll_loan_create"),
        }, status=status)

    def get(self, request, pk=None):
        loan = self._loan(pk) if pk else None
        if loan and loan.is_cancelled:
            raise PermissionDenied("A cancelled loan can't be edited.")
        return self._render(request, self._form(instance=loan), loan)

    def post(self, request, pk=None):
        loan = self._loan(pk) if pk else None
        if loan and loan.is_cancelled:
            raise PermissionDenied("A cancelled loan can't be edited.")
        form = self._form(request.POST, request.FILES, instance=loan)
        if not form.is_valid():
            return self._render(request, form, loan, status=400)
        obj = form.save(commit=False)
        if loan is None:
            obj.added_by = request.user
        obj.updated_by = request.user
        obj.save()
        return _done(request, f"Loan {obj.file_reference} {'updated' if loan else 'recorded'}.")


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanDisburseView(_LoanDisburserMixin):
    template_name = "payroll/_loan_disburse_drawer.html"

    def _render(self, request, form, loan, *, status=200):
        preview_period = None
        if loan.first_deduction_period_id:
            preview_period = loan.first_deduction_period
        else:
            raw = form.data.get("disbursed_on") if form.is_bound else None
            try:
                on = _date.fromisoformat(raw) if raw else timezone.localdate()
            except ValueError:
                on = timezone.localdate()
            year, month = first_loan_deduction_month(on)
            from payroll.periods import period_bounds
            start, end = period_bounds(year, month)
            preview_period = f"{_date(year, month, 1):%B %Y} payroll ({start:%b %d} – {end:%b %d})"
        return render(request, self.template_name, {
            "form": form, "loan": loan, "preview_period": preview_period,
            "action_url": reverse("payroll_loan_disburse", args=[loan.pk]),
        }, status=status)

    def get(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_pending:
            raise PermissionDenied("This loan is not awaiting disbursement.")
        return self._render(request, SalaryLoanDisburseForm(initial={"disbursed_on": timezone.localdate()}), loan)

    def post(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_pending:
            raise PermissionDenied("This loan is not awaiting disbursement.")
        form = SalaryLoanDisburseForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, loan, status=400)
        try:
            loan.mark_disbursed(request.user, form.cleaned_data["disbursed_on"], note=form.cleaned_data["note"])
            loan.full_clean()
        except ValidationError as exc:
            for msg in exc.messages:
                form.add_error(None, msg)
            return self._render(request, form, loan, status=400)
        loan.save()
        return _done(
            request,
            f"Loan {loan.file_reference} marked disbursed — first instalment in the {loan.first_deduction_period} payroll.",
        )


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanCancelView(_LoanAccessMixin):
    template_name = "payroll/_loan_cancel_drawer.html"

    def _render(self, request, form, loan, *, status=200):
        return render(request, self.template_name, {
            "form": form, "loan": loan, "action_url": reverse("payroll_loan_cancel", args=[loan.pk]),
        }, status=status)

    def get(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_pending:
            raise PermissionDenied("Only a loan awaiting disbursement can be cancelled.")
        return self._render(request, SalaryLoanCancelForm(), loan)

    def post(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_pending:
            raise PermissionDenied("Only a loan awaiting disbursement can be cancelled.")
        form = SalaryLoanCancelForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, loan, status=400)
        loan.cancel(request.user, form.cleaned_data["reason"])
        loan.save()
        return _done(request, f"Loan {loan.file_reference} cancelled.")


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanAdjustmentView(_LoanDisburserMixin):
    template_name = "payroll/_loan_adjustment_drawer.html"

    def _render(self, request, form, loan, balance, *, status=200):
        return render(request, self.template_name, {
            "form": form, "loan": loan, "balance": balance,
            "action_url": reverse("payroll_loan_adjust", args=[loan.pk]),
        }, status=status)

    def _balance(self, loan):
        return loan_balances([loan])[loan.pk]

    def get(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_active:
            raise PermissionDenied("Adjustments apply to active loans only.")
        balance = self._balance(loan)
        return self._render(request, SalaryLoanAdjustmentForm(outstanding=balance.outstanding), loan, balance)

    def post(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_active:
            raise PermissionDenied("Adjustments apply to active loans only.")
        balance = self._balance(loan)
        form = SalaryLoanAdjustmentForm(request.POST, outstanding=balance.outstanding)
        if not form.is_valid():
            return self._render(request, form, loan, balance, status=400)
        adjustment = form.save(commit=False)
        adjustment.loan = loan
        adjustment.created_by = request.user
        adjustment.save()
        return _done(request, f"Adjustment recorded on loan {loan.file_reference}.")


def _skippable_periods():
    """Standard periods from the current one onwards (ensured to exist), in
    order — a past period's payroll is settled, nothing to skip."""
    today = timezone.localdate()
    year, month = period_for_date(today)
    current = PayrollPeriod.for_month(year, month)
    nxt_month = month + 1
    nxt_year = year
    if nxt_month == 13:
        nxt_month, nxt_year = 1, year + 1
    PayrollPeriod.for_month(nxt_year, nxt_month)
    return PayrollPeriod.objects.filter(
        period_type=PayrollPeriod.PeriodType.STANDARD, start_date__gte=current.start_date,
    ).order_by("start_date")


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanSkipView(_LoanDisburserMixin):
    template_name = "payroll/_loan_skip_drawer.html"

    def _render(self, request, form, loan, *, status=200):
        return render(request, self.template_name, {
            "form": form, "loan": loan, "action_url": reverse("payroll_loan_skip", args=[loan.pk]),
        }, status=status)

    def _form(self, loan, data=None):
        return SalaryLoanSkipForm(data, loan=loan, period_queryset=_skippable_periods())

    def get(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_active:
            raise PermissionDenied("Only an active loan has instalments to skip.")
        return self._render(request, self._form(loan), loan)

    def post(self, request, pk):
        loan = self._loan(pk)
        if not loan.is_active:
            raise PermissionDenied("Only an active loan has instalments to skip.")
        form = self._form(loan, request.POST)
        if not form.is_valid():
            return self._render(request, form, loan, status=400)
        skip = form.save(commit=False)
        skip.loan = loan
        skip.created_by = request.user
        skip.save()
        return _done(
            request,
            f"{skip.period} instalment skipped for loan {loan.file_reference} — recalculate that run if it is already calculated.",
        )


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanSkipDeleteView(_LoanDisburserMixin):
    """Undo a skip whose payroll has not been calculated yet."""

    http_method_names = ["post"]

    def post(self, request, pk, skip_pk):
        loan = self._loan(pk)
        skip = get_object_or_404(SalaryLoanSkip.objects.select_related("period"), pk=skip_pk, loan=loan)
        calculated = PayrollRun.objects.filter(
            period=skip.period, status=PayrollRun.Status.CALCULATED, run_type=PayrollRun.RunType.REGULAR,
        ).exists()
        if calculated:
            messages.error(request, f"{skip.period} is already calculated — the skip stays as history.")
        else:
            skip.delete()
            messages.success(request, f"Skip for {skip.period} removed from loan {loan.file_reference}.")
        return redirect(_list_url(request))


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanHistoryView(_LoanAccessMixin):
    """Drawer: every instalment, adjustment and skip, newest first."""

    def get(self, request, pk):
        loan = self._loan(pk)
        balance = loan_balances([loan])[loan.pk]
        skips_open = set()
        for skip in loan.skips.select_related("period"):
            if not PayrollRun.objects.filter(
                period=skip.period, status=PayrollRun.Status.CALCULATED, run_type=PayrollRun.RunType.REGULAR,
            ).exists():
                skips_open.add(skip.pk)
        return render(request, "payroll/_loan_history_drawer.html", {
            "loan": loan, "balance": balance, "history": loan_history(loan),
            "skips": list(loan.skips.select_related("period", "approved_by_hr")),
            "skips_open": skips_open,
            "can_disburse": can_disburse_salary_loans(request.user, request=request),
        })


class SalaryLoanDocumentView(LoginRequiredMixin, View):
    """Stream the scanned file note — never linked by media URL (prod nginx
    serves /media/ openly), same as the AIT document."""

    def get(self, request, pk):
        loan = get_object_or_404(SalaryLoan.objects.select_related("employee"), pk=pk)
        from audit.capture import note
        note(request, subject=loan.employee)
        if not can_view_salary_loan(request.user, loan, request=request):
            raise PermissionDenied("You can't open this document.")
        if not loan.approval_document:
            raise Http404("No document on file.")
        return FileResponse(loan.approval_document.open("rb"), as_attachment=False)


@method_decorator(dynamic_allowed_users, name="dispatch")
class SalaryLoanEmployeeSearchView(_LoanAccessMixin):
    """Company-wide picker for the add-loan drawer — loans are HR-wide, not
    managed-scope (OvertimeEmployeeSearchView would clamp to reportees)."""

    http_method_names = ["get"]

    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        qs = CustomUser.objects.filter(is_active=True)
        if q:
            qs = qs.filter(employee_search_q(q))
        return render(request, "employees/_employee_search_results.html", {
            "q": q,
            "results": qs.order_by("first_name", "last_name")[:25],
            "empty_message": "No matching employee.",
        })
