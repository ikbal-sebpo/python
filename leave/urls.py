from django.urls import path

from leave import upload_views, views

urlpatterns = [
    # Browse + apply
    path("", views.LeaveListView.as_view(), name="leave_list"),
    path("apply/", views.LeaveApplyView.as_view(), name="leave_apply"),
    path("preview/", views.LeavePreviewView.as_view(), name="leave_preview"),

    # Detail + actions
    path("<int:pk>/", views.LeaveDetailView.as_view(), name="leave_detail"),
    path("<int:pk>/edit/", views.LeaveEditView.as_view(), name="leave_edit"),
    path("<int:pk>/approve/", views.LeaveApproveView.as_view(), name="leave_approve"),
    path("<int:pk>/decline/", views.LeaveDeclineView.as_view(), name="leave_decline"),
    path("<int:pk>/cancel/",  views.LeaveCancelView.as_view(),  name="leave_cancel"),

    # Balance dashboard
    path("balance/", views.LeaveBalanceView.as_view(), name="leave_balance"),
    path(
        "balance/<int:pk>/transactions/",
        views.LeaveBalanceTransactionsView.as_view(),
        name="leave_balance_transactions",
    ),

    # AL historical bank backfill (HR, one-time per employee)
    path("bank/upload/", upload_views.UploadAnnualLeaveBankView.as_view(), name="leave_al_bank_upload"),
    path(
        "bank/upload/template/",
        upload_views.AnnualLeaveBankUploadTemplateView.as_view(),
        name="leave_al_bank_upload_template",
    ),

    # Pre-Orbis AL record import (HR, Service-Book-only — docs/LEGACY_60_IMPORT_PLAN.md §4)
    path("al-history/upload/", upload_views.UploadAnnualLeaveHistoryView.as_view(),
         name="leave_al_history_upload"),
    path("al-history/upload/template/", upload_views.AnnualLeaveHistoryUploadTemplateView.as_view(),
         name="leave_al_history_upload_template"),

    # Encashment
    path("encashment/", views.EncashmentListView.as_view(), name="encashment_list"),
    path("encashment/request/", views.EncashmentRequestView.as_view(), name="encashment_request"),
    path("encashment/<int:pk>/decide/", views.EncashmentDecideView.as_view(), name="encashment_decide"),

    # Compensatory leave register + manual grant entry (worked a weekly off day, etc.)
    path("cpl-grants/", views.CplGrantListView.as_view(), name="cpl_grant_list"),
    path("cpl-grants/add/", views.CplGrantManualCreateView.as_view(), name="cpl_grant_manual_create"),
    path("cpl-grants/upload/", upload_views.CplGrantUploadView.as_view(), name="cpl_grant_upload"),
    path(
        "cpl-grants/upload/template/",
        upload_views.CplGrantUploadTemplateView.as_view(),
        name="cpl_grant_upload_template",
    ),

    # Reports
    path("reports/shift-summary/", views.ShiftSummaryView.as_view(), name="leave_shift_summary"),
    path("reports/monthly/", views.MonthlyHRReportView.as_view(), name="leave_monthly_report"),
    path(
        "employees/<uuid:pk>/register/",
        views.EmployeeLeaveRegisterView.as_view(),
        name="employee_leave_register",
    ),
]
