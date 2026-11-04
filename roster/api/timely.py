"""Read-only Roster API consumed by TimeLy (Laravel) and dsndd.

Auth: `Authorization: Token <token>` — either the consumer's own
`accounts.ApiClient` token (scope `roster:read`; `directory:read` for the
legacy `employees/` list), or, until each consumer has switched over, the
`.env` secrets `TIMELY_API_TOKEN` / `EXTERNAL_API_TOKEN`.
docs/DIRECTORY_API_PLAN.md §4.3, §5.5.
"""

from __future__ import annotations

from datetime import date, datetime

from django.conf import settings
from django.db.models import F, Prefetch
from django.utils.crypto import constant_time_compare
from rest_framework import authentication, exceptions, serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination

from accounts.api.auth import ApiClientTokenAuthentication, HasApiScopeOrEnvToken
from accounts.locations import office_bucket, work_location_code
from accounts.models import ApiClient, CustomUser
from roster.api.shift_writes import ExternalTokenAuthentication
from roster.models import Holiday, Shift, ShiftAttribute, ShiftType


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class _ServiceAccount:
    """Stand-in principal representing the TimeLy service account."""

    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    pk = 0

    def __str__(self) -> str:
        return "timely-service-account"


class TimelyTokenAuthentication(authentication.BaseAuthentication):
    """Match `Authorization: Token <TIMELY_API_TOKEN>` against settings.

    Returns `None` (rather than raising) on anything short of a structurally
    malformed header — including an unconfigured or mismatched token — so
    that composing this with another same-scheme authenticator
    (`ExternalTokenAuthentication`, which shares the "Token" keyword) lets
    DRF fall through and try the next one instead of this one unilaterally
    failing the whole request.
    """

    keyword = "Token"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        try:
            token = header[1].decode()
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed("Malformed token.") from exc

        expected = getattr(settings, "TIMELY_API_TOKEN", "")
        if not expected or not constant_time_compare(token, expected):
            return None
        return (_ServiceAccount(), token)

    def authenticate_header(self, request):
        return "Token"


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class ShiftAttributeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftAttribute
        fields = ("id", "name", "start_time", "end_time", "description", "sort_order")


class ShiftTypeSerializer(serializers.ModelSerializer):
    attributes = ShiftAttributeSerializer(many=True, read_only=True)

    class Meta:
        model = ShiftType
        fields = (
            "id", "name", "description", "category",
            "is_working_shift", "shift_timezone",
            "shift_start_time", "shift_end_time",
            "color", "sort_order", "has_attribute", "is_active",
            "attributes",
        )


class HolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holiday
        fields = ("id", "date", "name", "is_active")


class EmployeeSerializer(serializers.ModelSerializer):
    department = serializers.CharField(source="department.name", default=None, read_only=True)
    designation = serializers.CharField(source="designation.name", default=None, read_only=True)
    team = serializers.CharField(source="team.name", default=None, read_only=True)
    job_location = serializers.CharField(source="job_location.name", default=None, read_only=True)
    work_location = serializers.CharField(source="work_location.name", default=None, read_only=True)
    full_name = serializers.CharField(source="display_name", read_only=True)

    class Meta:
        model = CustomUser
        fields = (
            "id", "employee_id", "email", "full_name",
            "first_name", "last_name",
            "department", "designation", "team",
            "job_location", "work_location",
            "is_active",
        )


class TimelyShiftSerializer(serializers.Serializer):
    """Lean shape exposed to TimeLy. One row per concrete ``Shift``.

    Fields documented in /home/smrashel/.claude/plans/harmonic-sniffing-flute.md.
    """

    working_day = serializers.DateField(source="date", format="%Y-%m-%d")
    employee_id = serializers.CharField(source="employee.employee_id")
    name = serializers.CharField(source="employee.display_name")
    shift_attribute = serializers.SerializerMethodField()
    shift_start_time = serializers.SerializerMethodField()
    shift_end_time = serializers.SerializerMethodField()
    office_location = serializers.SerializerMethodField()
    work_location = serializers.SerializerMethodField()
    leave_locked = serializers.SerializerMethodField()

    # ----- attribute / type fallback -----

    def get_shift_attribute(self, obj: Shift) -> str:
        if obj.shift_attribute_id and obj.shift_attribute is not None:
            return obj.shift_attribute.name
        return obj.shift_type.name

    # ----- times: prefer attribute, fall back to type -----

    def _resolved_times(self, obj: Shift) -> tuple:
        sa = obj.shift_attribute
        if sa is not None and sa.start_time is not None:
            return sa.start_time, sa.end_time
        return obj.shift_type.shift_start_time, obj.shift_type.shift_end_time

    def get_shift_start_time(self, obj: Shift) -> str | None:
        start, _ = self._resolved_times(obj)
        return start.strftime("%H:%M") if start else None

    def get_shift_end_time(self, obj: Shift) -> str | None:
        _, end = self._resolved_times(obj)
        return end.strftime("%H:%M") if end else None

    # ----- locations: only meaningful for WORK shifts -----

    def get_office_location(self, obj: Shift) -> str | None:
        if obj.shift_type.category != ShiftType.Category.WORK:
            return None
        jl = obj.job_location or obj.employee.job_location
        if jl is None:
            return None
        bucket = office_bucket(jl)
        if bucket in ("MBD", "MHK", "RAOWA"):
            return bucket
        return None

    def get_work_location(self, obj: Shift) -> str | None:
        if obj.shift_type.category != ShiftType.Category.WORK:
            return None
        wl = obj.work_location or obj.employee.work_location
        return work_location_code(wl)

    # ----- consumed by external systems' reconciliation sweeps to know when to back off -----

    def get_leave_locked(self, obj: Shift) -> bool:
        return obj.leave_application_id is not None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class _BaseAPI:
    # DRF tries each in turn; a class returns None (not an exception) when
    # its own token doesn't match, so this composes cleanly — TimeLy and
    # external write-API consumers share these read-only endpoints without
    # knowing about each other.
    authentication_classes = [
        ApiClientTokenAuthentication, TimelyTokenAuthentication, ExternalTokenAuthentication,
    ]
    permission_classes = [HasApiScopeOrEnvToken]
    required_scope = ApiClient.Scope.ROSTER_READ


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse YYYY-MM-DD or raise DRF ``ValidationError`` keyed by field name."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValidationError(
            {field_name: "Bad format, expected YYYY-MM-DD"}
        )


_SHIFT_SELECT_RELATED = (
    "shift_type",
    "shift_attribute",
    "job_location",
    "work_location",
    "employee",
    "employee__job_location",
    "employee__work_location",
)


class ShiftTypeListAPI(_BaseAPI, ListAPIView):
    queryset = (
        ShiftType.objects.filter(is_active=True)
        .prefetch_related(Prefetch(
            "attributes",
            queryset=ShiftAttribute.objects.filter(is_active=True).order_by(
                F("sort_order").asc(nulls_last=True), "start_time", "name",
            ),
        ))
        .order_by(F("sort_order").asc(nulls_last=True), "name")
    )
    serializer_class = ShiftTypeSerializer


class HolidayListAPI(_BaseAPI, ListAPIView):
    serializer_class = HolidaySerializer

    def get_queryset(self):
        qs = Holiday.objects.filter(is_active=True).order_by("date")
        year = self.request.query_params.get("year")
        if year:
            try:
                qs = qs.filter(date__year=int(year))
            except (TypeError, ValueError):
                pass
        return qs


class EmployeeListAPI(_BaseAPI, ListAPIView):
    """Frozen legacy shape (D7) — new consumers use /api/v1/directory/employees/."""

    serializer_class = EmployeeSerializer
    required_scope = ApiClient.Scope.DIRECTORY_READ

    def get_queryset(self):
        qs = (
            CustomUser.objects.all()
            .select_related("department", "designation", "team", "job_location", "work_location")
            .order_by("first_name", "last_name")
        )
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))
        return qs


class RosterRangePagination(PageNumberPagination):
    page_size = 1000
    page_size_query_param = "page_size"
    max_page_size = 5000


class DailyRosterAPI(_BaseAPI, ListAPIView):
    """All concrete Shift rows for everyone, in one of two modes:

    - ``?date=YYYY-MM-DD`` — one day, a bare JSON array (unchanged contract).
    - ``?start_date=&end_date=`` — a range of at most ``_MAX_RANGE_DAYS``,
      paged (``{count, next, previous, results}``), ordered by date then
      employee id, same record shape. Lets a consumer fetch a month in a few
      calls instead of one per day (D9). Roster history starts 2026-01-01 (D13).
    """

    serializer_class = TimelyShiftSerializer
    pagination_class = RosterRangePagination

    _MAX_RANGE_DAYS = 31

    def _range(self):
        params = self.request.query_params
        start_str, end_str = params.get("start_date"), params.get("end_date")
        if start_str is None and end_str is None:
            return None
        if params.get("date"):
            raise ValidationError({"date": "Use either date or start_date/end_date, not both"})
        errors = {}
        if not start_str:
            errors["start_date"] = "Required, format YYYY-MM-DD"
        if not end_str:
            errors["end_date"] = "Required, format YYYY-MM-DD"
        if errors:
            raise ValidationError(errors)
        start = _parse_iso_date(start_str, "start_date")
        end = _parse_iso_date(end_str, "end_date")
        if end < start:
            raise ValidationError({"end_date": "Must be on or after start_date"})
        if (end - start).days >= self._MAX_RANGE_DAYS:
            raise ValidationError({"end_date": f"Date range exceeds {self._MAX_RANGE_DAYS} days"})
        return start, end

    @property
    def paginator(self):
        # The single-day mode keeps its bare-array response.
        if self._range() is None:
            return None
        return super().paginator

    def get_queryset(self):
        qs = (
            Shift.objects
            .filter(employee__is_active=True, employee__employee_id__isnull=False)
            .exclude(employee__employee_id="")
            .select_related(*_SHIFT_SELECT_RELATED)
        )
        span = self._range()
        if span is not None:
            return qs.filter(date__range=span).order_by("date", "employee__employee_id", "id")

        date_str = self.request.query_params.get("date")
        if not date_str:
            raise ValidationError(
                {"date": "Required (or start_date and end_date), format YYYY-MM-DD"}
            )
        day = _parse_iso_date(date_str, "date")
        return qs.filter(date=day).order_by("employee__first_name", "employee__last_name")


class EmployeeRangeRosterAPI(_BaseAPI, ListAPIView):
    """``GET /api/v1/roster/<str:employee_id>/?start_date=&end_date=``.

    Path param is the human ``CustomUser.employee_id`` string (e.g. ``"917"``),
    not the Orbis UUID. Range is required and capped at 92 days.
    """

    serializer_class = TimelyShiftSerializer
    pagination_class = None

    _MAX_RANGE_DAYS = 92

    def get_queryset(self):
        employee_id = self.kwargs["employee_id"]
        start_str = self.request.query_params.get("start_date")
        end_str = self.request.query_params.get("end_date")

        errors = {}
        if not start_str:
            errors["start_date"] = "Required, format YYYY-MM-DD"
        if not end_str:
            errors["end_date"] = "Required, format YYYY-MM-DD"
        if errors:
            raise ValidationError(errors)

        start = _parse_iso_date(start_str, "start_date")
        end = _parse_iso_date(end_str, "end_date")
        if end < start:
            raise ValidationError(
                {"end_date": "Must be on or after start_date"}
            )
        if (end - start).days > self._MAX_RANGE_DAYS:
            raise ValidationError(
                {"end_date": f"Date range exceeds {self._MAX_RANGE_DAYS} days"}
            )

        if not CustomUser.objects.filter(
            employee_id=employee_id, is_active=True,
        ).exists():
            raise NotFound(
                f"No active employee with employee_id={employee_id}"
            )

        return (
            Shift.objects
            .filter(
                employee__employee_id=employee_id,
                employee__is_active=True,
                date__gte=start, date__lte=end,
            )
            .select_related(*_SHIFT_SELECT_RELATED)
            .order_by("date")
        )
