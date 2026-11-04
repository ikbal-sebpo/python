from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path

from accounts.api.guide import api_guide
from core import views
from media_store.views import MediaServeView

# Admin site titles + the people-picker autocomplete override live on
# core.admin_site.OrbisAdminSite (wired via core.apps.OrbisAdminConfig).

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("employees/", include("employees.urls")),
    path("roster/", include("roster.urls")),
    path("attendance/", include("attendance.urls")),
    path("leave/", include("leave.urls")),
    path("documents/", include("documents.urls")),
    path("verify/", include("documents.public_urls")),
    path("offboarding/", include("offboarding.urls")),
    path("payroll/", include("payroll.urls")),
    path("policies/", include("policies.urls")),
    path("audit/", include("audit.urls")),
    # Required by the CKEditor 5 widget template (it reverses the upload URL
    # even though our toolbar exposes no image-upload button). The view is
    # staff-only via CKEDITOR_5_FILE_UPLOAD_PERMISSION.
    path("ckeditor5/", include("django_ckeditor_5.urls")),
    # Every media file, every environment: login + per-prefix authorisation
    # (media_store.rules), streamed from STORAGES["default"]. Replaces the
    # DEBUG-only static() helper and nginx's unauthenticated alias.
    path("media/<path:name>", MediaServeView.as_view(), name="media_serve"),
    path("api/v1/directory/", include("accounts.api.urls")),
    path("developers/api/", api_guide, name="developer_api_guide"),
    path("developers/api/<slug:page>/", api_guide, name="developer_api_guide_page"),
    path("api/v1/", include("roster.api.timely_urls")),
    path("api/v1/external/", include("roster.api.shift_writes_urls")),
    path("", login_required(views.dashboard), name="home"),
    path("credits/", login_required(views.project_credits), name="project_credits"),
    path("notifications/bell/", views.notification_bell, name="notification_bell"),
    path(
        "dashboard/status/",
        login_required(views.dashboard_status_today),
        name="dashboard_status_today",
    ),
    path(
        "dashboard/team/<uuid:pk>/downline/",
        login_required(views.team_downline),
        name="team_downline",
    ),
    path(
        "dashboard/team/reports/",
        login_required(views.team_reports),
        name="team_reports",
    ),
]

if settings.SSO_ENABLED:
    # Mount mozilla-django-oidc views at the same paths django-allauth uses for
    # its Microsoft provider. This lets Orbis reuse veridoc's existing Azure app
    # registration (which already has `http://127.0.0.1:8000/accounts/microsoft/
    # login/callback/` registered) for local testing — no Azure-side changes
    # needed. When the dedicated Orbis app reg is provisioned, swap to
    # `path('oidc/', include('mozilla_django_oidc.urls'))`.
    from mozilla_django_oidc import views as oidc_views

    urlpatterns += [
        path("oidc/", include("mozilla_django_oidc.urls")),
    ]

