"""Report-oriented read API for external roster consumers.

Unlike the TimeLy compatibility endpoints, this API exposes stable IDs and
semantic shift fields so consumers can safely build reports without deriving
business meaning from a display label.
"""

from __future__ import annotations

from datetime import date, datetime

from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from accounts.api.auth import ApiClientTokenAuthentication, HasApiScopeOrEnvToken
from accounts.locations import office_bucket, work_location_code
from accounts.models import ApiClient, CustomUser
from roster.api.shift_writes import ExternalTokenAuthentication
from roster.models import Shift, ShiftType


MAX_RANGE_DAYS = 92
MAX_EMPLOYEE_IDS = 1000


def _parse_iso_date(value: str | None, field_name: str) -> date:
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError({field_name: "Required, format YYYY-MM-DD"})


class ReportShiftSerializer(serializers.Serializer):
    """Stable report contract for one concrete :class:`roster.models.Shift`."""

    orbis_shift_id = serializers.IntegerField(source="id", read_only=True)
    employee_id = serializers.CharField(source="employee.employee_id", read_only=True)
    employee_name = serializers.CharField(source="employee.display_name", read_only=True)
    date = serializers.DateField(read_only=True)
    shift_type_id = serializers.IntegerField(read_only=True)
    shift_code = serializers.CharField(source="shift_type.name", read_only=True)
    shift_full_name = serializers.CharField(source="shift_type.full_name", read_only=True)
    shift_attribute_id = serializers.IntegerField(allow_null=True, read_only=True)
    shift_label = serializers.SerializerMethodField()
    category = serializers.CharField(source="shift_type.category", read_only=True)
    is_working_shift = serializers.BooleanField(source="shift_type.is_working_shift", read_only=True)
    start_time = serializers.SerializerMethodField()
    end_time = serializers.SerializerMethodField()
    office_location = serializers.SerializerMethodField()
    work_location = serializers.SerializerMethodField()
    note = serializers.CharField(read_only=True)
    leave_locked = serializers.BooleanField(source="leave_application_id", read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_shift_label(self, obj: Shift) -> str:
        return obj.shift_attribute.name if obj.shift_attribute_id else obj.shift_type.name

    def _resolved_times(self, obj: Shift):
        if obj.shift_attribute_id and obj.shift_attribute and obj.shift_attribute.start_time:
            return obj.shift_attribute.start_time, obj.shift_attribute.end_time
        return obj.shift_type.shift_start_time, obj.shift_type.shift_end_time

    def get_start_time(self, obj: Shift) -> str | None:
        start, _ = self._resolved_times(obj)
        return start.strftime("%H:%M") if start else None

    def get_end_time(self, obj: Shift) -> str | None:
        _, end = self._resolved_times(obj)
        return end.strftime("%H:%M") if end else None

    def get_office_location(self, obj: Shift) -> str | None:
        if obj.shift_type.category != ShiftType.Category.WORK:
            return None
        location = obj.job_location or obj.employee.job_location
        if location is None:
            return None
        bucket = office_bucket(location)
        return bucket if bucket in {"MBD", "MHK", "RAOWA"} else None

    def get_work_location(self, obj: Shift) -> str | None:
        if obj.shift_type.category != ShiftType.Category.WORK:
            return None
        return work_location_code(obj.work_location or obj.employee.work_location)


class ReportShiftPagination(PageNumberPagination):
    page_size = 1000
    page_size_query_param = "page_size"
    max_page_size = 5000


class ExternalReportShiftAPI(ListAPIView):
    """``GET /api/v1/external/report-shifts/``.

    ``start_date`` and ``end_date`` are required ISO dates and may span at
    most 92 days. Optional employee selectors accept either repeated
    ``employee_id`` parameters or comma-separated/repeated ``employee_ids``
    parameters. Omitting both selects all active employees; explicitly named
    employees are included even when inactive so historical reports remain
    possible. ``include_inactive=true`` expands an all-employee request.
    """

    authentication_classes = [ApiClientTokenAuthentication, ExternalTokenAuthentication]
    permission_classes = [HasApiScopeOrEnvToken]
    required_scope = ApiClient.Scope.ROSTER_READ
    serializer_class = ReportShiftSerializer
    pagination_class = ReportShiftPagination

    _select_related = (
        "shift_type", "shift_attribute", "job_location", "work_location",
        "employee", "employee__job_location", "employee__work_location",
    )

    def _filters(self):
        if hasattr(self, "_report_filters"):
            return self._report_filters

        params = self.request.query_params
        start = _parse_iso_date(params.get("start_date"), "start_date")
        end = _parse_iso_date(params.get("end_date"), "end_date")
        if end < start:
            raise ValidationError({"end_date": "Must be on or after start_date"})
        if (end - start).days > MAX_RANGE_DAYS:
            raise ValidationError({"end_date": f"Date range exceeds {MAX_RANGE_DAYS} days"})

        employee_ids = []
        for value in [*params.getlist("employee_id"), *params.getlist("employee_ids")]:
            employee_ids.extend(part.strip() for part in value.split(",") if part.strip())
        employee_ids = list(dict.fromkeys(employee_ids))
        if len(employee_ids) > MAX_EMPLOYEE_IDS:
            raise ValidationError({"employee_ids": f"At most {MAX_EMPLOYEE_IDS} employee IDs are allowed"})

        include_inactive = params.get("include_inactive", "").lower() in {"1", "true", "yes"}
        self._report_filters = start, end, employee_ids, include_inactive
        return self._report_filters

    def get_queryset(self):
        start, end, employee_ids, include_inactive = self._filters()
        queryset = Shift.objects.filter(
            date__range=(start, end), employee__employee_id__isnull=False,
        ).exclude(employee__employee_id="")

        if employee_ids:
            known_ids = set(CustomUser.objects.filter(employee_id__in=employee_ids).values_list("employee_id", flat=True))
            missing = [employee_id for employee_id in employee_ids if employee_id not in known_ids]
            if missing:
                raise ValidationError({"employee_ids": f"Unknown employee_id values: {', '.join(missing)}"})
            queryset = queryset.filter(employee__employee_id__in=employee_ids)
        elif not include_inactive:
            queryset = queryset.filter(employee__is_active=True)

        return queryset.select_related(*self._select_related).order_by("date", "employee__employee_id", "id")

    def get_paginated_response(self, data):
        start, end, employee_ids, include_inactive = self._filters()
        return Response({
            "count": self.paginator.page.paginator.count,
            "next": self.paginator.get_next_link(),
            "previous": self.paginator.get_previous_link(),
            "meta": {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "range_days": (end - start).days + 1,
                "employee_scope": "specified" if employee_ids else "all",
                "employee_ids": employee_ids or None,
                "include_inactive": include_inactive if not employee_ids else True,
            },
            "results": data,
        })
