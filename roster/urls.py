from django.urls import path

from roster import upload_views, views

urlpatterns = [
    path("", views.RosterWeekView.as_view(), name="roster_week"),

    path("cell/<uuid:employee_pk>/<str:day>/",
         views.CellUpdateView.as_view(), name="roster_cell_update"),

    path("history/<uuid:employee_pk>/<str:day>/",
         views.ShiftHistoryView.as_view(), name="roster_shift_history"),

    path("patterns/<uuid:employee_pk>/",
         views.RosterPatternView.as_view(), name="roster_pattern"),

    path("email/",
         views.RosterEmailView.as_view(), name="roster_email"),
    path("email/people/",
         views.RosterEmailPeopleSearchView.as_view(), name="roster_email_people_search"),
    path("export/",
         views.RosterExportView.as_view(), name="roster_export"),
    path("upload/",
         upload_views.UploadRosterView.as_view(), name="roster_upload"),
    path("upload/template/<str:fmt>/",
         upload_views.UploadTemplateView.as_view(), name="roster_upload_template"),

    # Shift change requests (docs/SHIFT_CHANGE_REQUEST_PLAN.md) — same
    # list / apply / preview / detail / approve / decline / cancel shape as leave.
    path("change-requests/",
         views.ShiftChangeListView.as_view(), name="shift_change_list"),
    path("change-requests/apply/",
         views.ShiftChangeApplyView.as_view(), name="shift_change_apply"),
    path("change-requests/current/",
         views.ShiftChangeCurrentView.as_view(), name="shift_change_current"),
    path("change-requests/<int:pk>/",
         views.ShiftChangeDetailView.as_view(), name="shift_change_detail"),
    path("change-requests/<int:pk>/approve/",
         views.ShiftChangeApproveView.as_view(), name="shift_change_approve"),
    path("change-requests/<int:pk>/decline/",
         views.ShiftChangeDeclineView.as_view(), name="shift_change_decline"),
    path("change-requests/<int:pk>/cancel/",
         views.ShiftChangeCancelView.as_view(), name="shift_change_cancel"),

    path("shift-types/",                 views.ShiftTypeListView.as_view(),   name="shift_type_list"),
    path("shift-types/create/",          views.ShiftTypeCreateView.as_view(), name="shift_type_create"),
    path("shift-types/<int:pk>/update/", views.ShiftTypeUpdateView.as_view(), name="shift_type_update"),
    path("shift-types/<int:pk>/delete/", views.ShiftTypeDeleteView.as_view(), name="shift_type_delete"),
]
