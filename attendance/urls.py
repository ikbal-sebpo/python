from django.urls import path

from attendance import views

urlpatterns = [
    path("", views.AttendanceListView.as_view(), name="attendance_list"),

    path("corrections/", views.AttendanceCorrectionListView.as_view(), name="attendance_correction_list"),
    path(
        "corrections/request/", views.AttendanceCorrectionRequestView.as_view(),
        name="attendance_correction_request",
    ),
    path(
        "corrections/<int:pk>/edit/", views.AttendanceCorrectionEditView.as_view(),
        name="attendance_correction_edit",
    ),
    path(
        "corrections/<int:pk>/approve/", views.AttendanceCorrectionApproveView.as_view(),
        name="attendance_correction_approve",
    ),
    path(
        "corrections/<int:pk>/decline/", views.AttendanceCorrectionDeclineView.as_view(),
        name="attendance_correction_decline",
    ),
    path(
        "corrections/<int:pk>/cancel/", views.AttendanceCorrectionCancelView.as_view(),
        name="attendance_correction_cancel",
    ),
    path(
        "corrections/bulk/", views.AttendanceCorrectionBulkActionView.as_view(),
        name="attendance_correction_bulk",
    ),
]
