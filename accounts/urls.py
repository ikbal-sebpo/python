from django.urls import path

from accounts import upload_views, views

urlpatterns = [
    path("login/", views.OrbisLoginView.as_view(), name="login"),
    path("logout/", views.OrbisLogoutView.as_view(), name="logout"),

    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
    path("users/ajax/suggest-identity/", views.suggest_new_hire_identity, name="ajax_suggest_identity"),
    path("users/ajax/check-nid/", views.check_nid_prior_employment, name="ajax_check_nid"),
    path("users/<uuid:pk>/update/", views.UserUpdateView.as_view(), name="user_update"),

    path("users/bulk-create/", upload_views.OnboardUploadView.as_view(), name="user_bulk_create"),
    path("users/bulk-create/template/",
         upload_views.OnboardUploadTemplateView.as_view(), name="user_bulk_create_template"),
    path("users/bulk-create/results/",
         upload_views.OnboardResultsView.as_view(), name="user_bulk_create_results"),
]
