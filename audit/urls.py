from django.urls import path

from audit import views

urlpatterns = [
    path("access-log/", views.AccessLogListView.as_view(), name="audit_access_log"),
]
