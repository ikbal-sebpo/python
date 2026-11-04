from django.urls import path

from roster.api import report_shifts, shift_writes

urlpatterns = [
    path("shifts/batch/", shift_writes.ExternalShiftBatchAPI.as_view(), name="api_external_shifts_batch"),
    path("report-shifts/", report_shifts.ExternalReportShiftAPI.as_view(), name="api_external_report_shifts"),
]
