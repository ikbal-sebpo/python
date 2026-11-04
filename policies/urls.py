"""URL conf for the policies app (mounted at /policies/)."""

from django.urls import path

from policies import views

app_name = "policies"

urlpatterns = [
    # Employee-facing
    path("mine/", views.MyPoliciesView.as_view(), name="my_policies"),
    path("<uuid:pk>/sign/", views.PolicySignView.as_view(), name="sign"),
    path("<uuid:pk>/pdf/", views.PolicyPdfView.as_view(), name="pdf"),
    path("<uuid:pk>/preview/", views.PolicyPdfPreviewView.as_view(), name="preview"),

    # Manager-facing
    path("", views.PolicyListView.as_view(), name="list"),
    path("create/", views.PolicyCreateView.as_view(), name="create"),
    path("owners-by-department/", views.PolicyOwnerOptionsView.as_view(), name="owners_by_department"),
    path("adherents-search/", views.PolicyAdherentSearchView.as_view(), name="adherents_search"),
    path("adherents-select-all/", views.PolicyAdherentSelectAllView.as_view(), name="adherents_select_all"),
    path("<uuid:pk>/", views.PolicyDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.PolicyEditView.as_view(), name="edit"),
    path("<uuid:pk>/version/", views.PolicyVersionUploadView.as_view(), name="upload_version"),
    path("<uuid:pk>/activate/", views.PolicyActivateView.as_view(), name="activate"),
    path("<uuid:pk>/circulate/", views.PolicyCirculateView.as_view(), name="circulate"),
    path("<uuid:pk>/retire/", views.PolicyRetireView.as_view(), name="retire"),
    path("<uuid:pk>/reset/", views.PolicyResetView.as_view(), name="reset"),
    path("<uuid:pk>/remind/", views.SendRemindersView.as_view(), name="send_reminders"),
]
