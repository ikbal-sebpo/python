from django.urls import path

from roster.api import timely

urlpatterns = [
    path("shift-types/", timely.ShiftTypeListAPI.as_view(), name="api_shift_types"),
    path("holidays/",    timely.HolidayListAPI.as_view(),   name="api_holidays"),
    path("employees/",   timely.EmployeeListAPI.as_view(),  name="api_employees"),
    path("roster/",      timely.DailyRosterAPI.as_view(),   name="api_roster"),
    path("roster/<str:employee_id>/", timely.EmployeeRangeRosterAPI.as_view(), name="api_employee_roster"),
]
