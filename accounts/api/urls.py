from django.urls import path

from accounts.api import views

urlpatterns = [
    path("departments/",  views.DepartmentListAPI.as_view(),  name="api_directory_departments"),
    path("designations/", views.DesignationListAPI.as_view(), name="api_directory_designations"),
    path("teams/",        views.TeamListAPI.as_view(),        name="api_directory_teams"),
    path("offices/",      views.OfficeListAPI.as_view(),      name="api_directory_offices"),
    path("employees/",        views.EmployeeListAPI.as_view(),   name="api_directory_employees"),
    path("employees/lookup/", views.EmployeeLookupAPI.as_view(), name="api_directory_employee_lookup"),
    path("employees/<str:employee_id>/", views.EmployeeDetailAPI.as_view(), name="api_directory_employee"),
    path(
        "employees/<str:employee_id>/direct-reports/",
        views.DirectReportsAPI.as_view(), name="api_directory_direct_reports",
    ),
    path("employees/<str:employee_id>/scope/", views.EmployeeScopeAPI.as_view(), name="api_directory_scope"),
]
