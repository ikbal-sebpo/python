"""URL conf for the offboarding app (mounted at /offboarding/)."""

from django.urls import path

from offboarding import views

app_name = "offboarding"

urlpatterns = [
    # Public (post-logout landing — no auth gate).
    path("access-ended/", views.AccessEndedView.as_view(), name="access_ended"),

    # Employee
    path("my/", views.MyOffboardingView.as_view(), name="my_offboarding"),
    path("submit/", views.ResignationSubmitView.as_view(), name="resignation_submit"),
    path("resignations/<int:pk>/withdraw/",
         views.ResignationWithdrawView.as_view(), name="resignation_withdraw"),

    # HR-initiated separation (on behalf of an employee)
    path("create/", views.HRCreateResignationView.as_view(),
         name="hr_create_resignation"),

    # Manager
    path("manager/queue/", views.ManagerAckQueueView.as_view(), name="manager_ack_queue"),

    # Clearance team
    path("queue/", views.ClearanceQueueView.as_view(), name="clearance_queue"),
    path("steps/<int:pk>/",
         views.ClearanceStepActionView.as_view(), name="clearance_step_action"),

    # HR
    path("cases/", views.CaseListView.as_view(), name="case_list"),
    path("cases/<int:pk>/", views.CaseDetailView.as_view(), name="case_detail"),
    path("cases/<int:pk>/note/", views.NoteAddView.as_view(), name="case_note_add"),
    path("cases/<int:pk>/issue-letters/",
         views.IssueLettersView.as_view(), name="case_issue_letters"),
    path("cases/<int:pk>/cancel/", views.CaseCancelView.as_view(), name="case_cancel"),

    # Exit interview
    path("cases/<int:pk>/exit-interview/",
         views.ExitInterviewView.as_view(), name="exit_interview"),

    # Asset return
    path("cases/<int:pk>/assets/add/",
         views.AssetReturnAddView.as_view(), name="asset_return_add"),
    path("cases/<int:pk>/assets/<int:row_pk>/edit/",
         views.AssetReturnEditView.as_view(), name="asset_return_edit"),
    path("ajax/employees/", views.load_offboarding_employees,
         name="ajax_offboarding_employees"),
    path("cases/<int:pk>/assets/<int:row_pk>/delete/",
         views.AssetReturnDeleteView.as_view(), name="asset_return_delete"),

    # Resignation HR edit/decide
    path("resignations/<int:pk>/edit/",
         views.ResignationHREditView.as_view(), name="resignation_hr_edit"),
    path("resignations/<int:pk>/decide/",
         views.ResignationDecideView.as_view(), name="resignation_decide"),
]
