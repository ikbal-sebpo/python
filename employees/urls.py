from django.urls import path

from employees import upload_views, views

urlpatterns = [
    path("", views.EmployeeListView.as_view(), name="employee_list"),
    # Org chart (added 2026-10-11) — the live reporting tree + the htmx
    # children fragment it expands with.
    path("org-chart/", views.OrgChartView.as_view(), name="employee_org_chart"),
    path("org-chart/node/<uuid:pk>/",
         views.OrgChartNodeView.as_view(), name="employee_org_chart_node"),
    path("bulk-upload/",
         upload_views.UploadEmployeeDataView.as_view(), name="employee_bulk_upload"),
    path("bulk-upload/template/<str:fmt>/",
         upload_views.UploadEmployeeDataTemplateView.as_view(), name="employee_bulk_upload_template"),

    # Dedicated Night Shift assignments (docs/PAYROLL_PLAN.md, 2026-09-07)
    path("dedicated-night-shifts/",
         views.DedicatedNightShiftListView.as_view(), name="dedicated_night_shift_list"),
    path("dedicated-night-shifts/request/",
         views.DedicatedNightShiftRequestView.as_view(), name="dedicated_night_shift_request"),
    path("dedicated-night-shifts/<int:pk>/edit/",
         views.DedicatedNightShiftEditView.as_view(), name="dedicated_night_shift_edit"),
    path("dedicated-night-shifts/<int:pk>/approve/",
         views.DedicatedNightShiftApproveView.as_view(), name="dedicated_night_shift_approve"),
    path("dedicated-night-shifts/<int:pk>/decline/",
         views.DedicatedNightShiftDeclineView.as_view(), name="dedicated_night_shift_decline"),
    path("dedicated-night-shifts/<int:pk>/cancel/",
         views.DedicatedNightShiftCancelView.as_view(), name="dedicated_night_shift_cancel"),
    path("dedicated-night-shifts/candidates/",
         views.dedicated_night_shift_candidates, name="dedicated_night_shift_candidates"),
    path("dedicated-night-shifts/<int:pk>/history/",
         views.DedicatedNightShiftHistoryView.as_view(), name="dedicated_night_shift_history"),
    path("dedicated-night-shifts/bulk/",
         views.DedicatedNightShiftBulkActionView.as_view(), name="dedicated_night_shift_bulk"),

    # Night shift consent (Bangladesh Labour Act 2006 s.109, added 2026-09-13)
    path("night-shift-consent/",
         views.NightShiftConsentListView.as_view(), name="night_shift_consent_list"),
    path("night-shift-consent/update/",
         views.NightShiftConsentUpdateView.as_view(), name="night_shift_consent_update"),

    # Profile self-service toggle (added 2026-09-17): HR-only switch controlling
    # whether an employee may add/update/delete their own personal-data
    # profile sections.
    path("self-service/",
         views.ProfileSelfServiceAuditListView.as_view(), name="profile_self_service_list"),

    path("<uuid:pk>/", views.EmployeeDetailView.as_view(), name="employee_detail"),
    path("<uuid:pk>/avatar/", views.AvatarUpdateView.as_view(), name="employee_avatar"),
    path("<uuid:pk>/self-service/toggle/",
         views.ProfileSelfServiceToggleView.as_view(), name="profile_self_service_toggle"),
    path("<uuid:pk>/service-book/", views.EmployeeServiceBookView.as_view(), name="employee_service_book"),
    path("<uuid:pk>/service-book/pdf/", views.ServiceBookPDFView.as_view(), name="employee_service_book_pdf"),

    # Singleton edit (no row_pk) + repeating edit (with row_pk)
    path("<uuid:pk>/edit/<slug:section>/",
         views.SectionEditView.as_view(), name="section_edit"),
    path("<uuid:pk>/edit/<slug:section>/<int:row_pk>/",
         views.SectionEditView.as_view(), name="section_edit_row"),
    path("<uuid:pk>/add/<slug:section>/",
         views.SectionEditView.as_view(), name="section_add"),
    path("<uuid:pk>/delete/<slug:section>/<int:row_pk>/",
         views.SectionDeleteView.as_view(), name="section_delete_row"),
    path("bank-account/<int:pk>/verify/",
         views.BankAccountVerifyView.as_view(), name="bank_account_verify"),

    # AJAX
    path("ajax/states/",    views.load_states,    name="ajax_load_states"),
    path("ajax/cities/",    views.load_cities,    name="ajax_load_cities"),
    path("ajax/union-wards/", views.load_union_wards, name="ajax_load_union_wards"),
    path("ajax/teams/",     views.load_teams,     name="ajax_load_teams"),
    path("ajax/subteams/",  views.load_subteams,  name="ajax_load_subteams"),
    path("ajax/employees/", views.load_employees, name="ajax_load_employees"),
]
