"""Directory API — Orbis as the employee data source for other applications.

`/api/v1/directory/…`, read-only, `ApiClient` tokens with the
`directory:read` scope. docs/DIRECTORY_API_PLAN.md §5; the consumer-facing
contract is docs/DIRECTORY_API.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Greatest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api.auth import ApiClientTokenAuthentication, HasApiScope
from accounts.api.serializers import (
    DepartmentSerializer,
    DesignationSerializer,
    EmployeeDetailSerializer,
    EmployeeSerializer,
    OfficeSerializer,
    ScopedEmployeeSerializer,
    TeamSerializer,
)
from accounts.models import ApiClient, CustomUser, Department, Designation, JobLocation, Team
from accounts.permissions import department_grant_pks, reportee_depths, team_grant_pks
from audit.capture import note
from employees.models import JobInfo

_TRUE = ("1", "true", "yes")


# Lookup keys in the order they are trusted (D1).
_LOOKUP_KEYS = ("oid", "email", "employee_id")

# Identifiers that select one person: on any endpoint but lookup they point
# the caller there instead of being silently ignored.
_LOOKUP_HINT = "Unknown parameter. Use employees/lookup/?{name}= to find one person."
# DRF's own format override (?format=json), accepted everywhere.
_ALWAYS_ALLOWED = frozenset({"format"})


class _DirectoryAPI:
    """Shared auth/scope, plus strict query parameters: anything a view
    doesn't list in ``allowed_params`` is a 400 naming the parameter. A
    silently ignored filter (``employees/?email=…``) returns the whole
    company and looks like it worked."""

    authentication_classes = [ApiClientTokenAuthentication]
    permission_classes = [HasApiScope]
    required_scope = ApiClient.Scope.DIRECTORY_READ
    allowed_params: frozenset = frozenset()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)  # authenticate + scope first
        unknown = set(request.query_params) - self.allowed_params - _ALWAYS_ALLOWED
        if unknown:
            raise ValidationError({
                name: _LOOKUP_HINT.format(name=name) if name in _LOOKUP_KEYS else (
                    "Unknown parameter. Allowed: " + (", ".join(sorted(self.allowed_params)) or "none")
                )
                for name in sorted(unknown)
            })


class _DirectoryListAPI(_DirectoryAPI, ListAPIView):
    pagination_class = None  # reference lists are small (tens of rows)
    allowed_params = frozenset({"is_active"})

    def filter_queryset(self, queryset):
        # Inactive rows are returned by default so a consumer can still resolve
        # an old employee's department; ?is_active=true narrows.
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() in _TRUE)
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        note(request._request, rows=len(response.data))
        return response


class DepartmentListAPI(_DirectoryListAPI):
    serializer_class = DepartmentSerializer
    queryset = Department.objects.select_related("head_of_department").order_by("name")


class DesignationListAPI(_DirectoryListAPI):
    serializer_class = DesignationSerializer
    queryset = Designation.objects.order_by("name")


class TeamListAPI(_DirectoryListAPI):
    serializer_class = TeamSerializer
    queryset = Team.objects.select_related("department").order_by("name")


class OfficeListAPI(_DirectoryListAPI):
    serializer_class = OfficeSerializer
    queryset = JobLocation.objects.order_by("name")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

def directory_people():
    """Everyone the directory exposes: users with an employee id. Service
    accounts (e.g. the external-integrations user) have none, so never appear."""
    return CustomUser.objects.exclude(employee_id__isnull=True).exclude(employee_id="")


def employee_queryset():
    """The §5.1 record's queryset — a constant number of queries per page.

    ``active_direct_reports`` is a correlated subquery rather than a
    ``Count()`` join, so the select_related row stays one-per-person.
    ``record_updated_at`` is the later of the directory stamp and the
    ``JobInfo`` row (where ``reports_to`` lives) — the ``updated_since`` key.
    """
    reports = (
        JobInfo.objects.filter(reports_to=OuterRef("pk"), employee__in=directory_people().filter(is_active=True))
        .order_by().values("reports_to").annotate(n=Count("pk")).values("n")
    )
    return (
        directory_people()
        .select_related(
            "department", "team", "designation", "job_location", "work_location",
            "job_info__reports_to",
        )
        .annotate(
            active_direct_reports=Coalesce(Subquery(reports, output_field=IntegerField()), 0),
            record_updated_at=Greatest(
                "directory_updated_at", Coalesce("job_info__updated_at", "directory_updated_at"),
            ),
        )
        .order_by("employee_id")
    )


_STATUS_FILTERS = {"active": Q(is_active=True), "inactive": Q(is_active=False), "all": Q()}


def _status_param(params, default: str = "active") -> str:
    status = (params.get("status") or default).lower()
    if status not in _STATUS_FILTERS:
        raise ValidationError({"status": "One of: active, inactive, all"})
    return status


def _status_filter(params, default: str = "active") -> Q:
    return _STATUS_FILTERS[_status_param(params, default)]


def _int_param(params, name: str) -> int | None:
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValidationError({name: "Must be an integer id"})


def _parse_since(raw: str) -> datetime:
    """ISO datetime (with or without offset) or a bare date. A naive value is
    read in the server's zone (Asia/Dhaka)."""
    value = parse_datetime(raw)
    if value is None:
        day = parse_date(raw)
        if day is None:
            raise ValidationError({"updated_since": "ISO 8601 datetime or YYYY-MM-DD"})
        value = datetime.combine(day, time.min)
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def _active_reports_of(user: CustomUser):
    return (
        directory_people()
        .filter(job_info__reports_to=user, is_active=True)
        .select_related("designation")
        .order_by("employee_id")
    )


class DirectoryPagination(PageNumberPagination):
    page_size = 500
    page_size_query_param = "page_size"
    max_page_size = 1000

    def get_paginated_response(self, data):
        response = super().get_paginated_response(data)
        # The time this sync started: the consumer's next `updated_since`.
        response.data["as_of"] = self.request.directory_as_of.isoformat()
        return response


class EmployeeListAPI(_DirectoryAPI, ListAPIView):
    """``GET employees/?status=&department=&team=&updated_since=&page=&page_size=``"""

    serializer_class = EmployeeSerializer
    pagination_class = DirectoryPagination
    allowed_params = frozenset({"status", "department", "team", "updated_since", "page", "page_size"})

    def get_queryset(self):
        params = self.request.query_params
        qs = employee_queryset().filter(_status_filter(params))
        department = _int_param(params, "department")
        if department is not None:
            qs = qs.filter(department_id=department)
        team = _int_param(params, "team")
        if team is not None:
            qs = qs.filter(team_id=team)
        since = params.get("updated_since")
        if since:
            moment = _parse_since(since)
            qs = qs.filter(Q(directory_updated_at__gt=moment) | Q(job_info__updated_at__gt=moment))
        return qs

    def list(self, request, *args, **kwargs):
        # Taken before the query runs, so a change landing mid-sync is picked
        # up by the next one rather than lost.
        request.directory_as_of = timezone.now()
        response = super().list(request, *args, **kwargs)
        note(request._request, rows=len(response.data["results"]))
        return response


class EmployeeDetailAPI(_DirectoryAPI, RetrieveAPIView):
    """``GET employees/<employee_id>/`` — any status (D3)."""

    serializer_class = EmployeeDetailSerializer
    lookup_field = "employee_id"

    def get_queryset(self):
        return employee_queryset()

    def retrieve(self, request, *args, **kwargs):
        user = self.get_object()
        note(request._request, subject=user)
        serializer = self.get_serializer(user, context={
            **self.get_serializer_context(), "direct_reports": list(_active_reports_of(user)),
        })
        return Response(serializer.data)


class DirectReportsAPI(_DirectoryAPI, ListAPIView):
    """``GET employees/<employee_id>/direct-reports/?status=`` — full records,
    unpaginated (a manager has tens of reports, not thousands)."""

    serializer_class = EmployeeSerializer
    pagination_class = None
    allowed_params = frozenset({"status"})

    def get_queryset(self):
        manager = get_object_or_404(directory_people(), employee_id=self.kwargs["employee_id"])
        self.manager = manager
        return employee_queryset().filter(job_info__reports_to=manager).filter(
            _status_filter(self.request.query_params),
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        note(request._request, subject=self.manager, rows=len(response.data))
        return response


def managed_scope_reasons(manager: CustomUser, include_separated: bool = False) -> dict:
    """``{pk: {"via": [...], "depth": n | None}}`` for everyone in the manager's
    Orbis managed scope — the same three sources, with the same rules, as
    ``accounts.permissions.managed_scope_pks`` (reports-to downline,
    department head/co-manager grants, team grants), kept apart so each
    person can say how they got there. Self is never included."""
    depths = reportee_depths(manager, include_separated=include_separated)
    by_department = department_grant_pks(manager, include_separated=include_separated)
    by_team = team_grant_pks(manager, include_separated=include_separated)
    sources = (("reports_to", depths), ("department", by_department), ("team", by_team))
    return {
        pk: {"via": [name for name, pks in sources if pk in pks], "depth": depths.get(pk)}
        for pk in set(depths) | by_department | by_team
    }


class EmployeeScopeAPI(_DirectoryAPI, ListAPIView):
    """``GET employees/<employee_id>/scope/?status=`` — everyone this employee
    manages in Orbis: their whole reports-to downline plus the departments and
    teams they were granted. Paged like the list; each record carries
    ``scope`` (``ScopedEmployeeSerializer``).

    Default ``status=active`` is exactly Orbis's own managed scope.
    ``inactive``/``all`` also walk through people who have left — the same
    ``include_separated`` traversal payroll uses for leavers' final inputs.
    """

    serializer_class = ScopedEmployeeSerializer
    pagination_class = DirectoryPagination
    allowed_params = frozenset({"status", "page", "page_size"})

    def get_queryset(self):
        status = _status_param(self.request.query_params)
        self.manager = get_object_or_404(directory_people(), employee_id=self.kwargs["employee_id"])
        self.reasons = managed_scope_reasons(self.manager, include_separated=status != "active")
        return employee_queryset().filter(pk__in=self.reasons).filter(_STATUS_FILTERS[status])

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "scope": getattr(self, "reasons", {})}

    def list(self, request, *args, **kwargs):
        request.directory_as_of = timezone.now()
        response = super().list(request, *args, **kwargs)
        note(request._request, subject=self.manager, rows=len(response.data["results"]))
        return response


class EmployeeLookupAPI(_DirectoryAPI, APIView):
    """``GET employees/lookup/?oid=&email=&employee_id=`` — resolve the person
    a consumer just signed in through SSO. §5.3.

    Every supplied key is resolved; the first in trust order wins and is
    reported as ``matched_by``. Two keys naming different people is a 409 —
    never a silent pick. Inactive employees are returned; the consumer
    decides whether to let them in.
    """

    allowed_params = frozenset(_LOOKUP_KEYS)

    def get(self, request):
        keys = self._keys(request.query_params)
        clauses = {
            "oid": lambda v: Q(entra_object_id=v),
            "email": lambda v: Q(email__iexact=v),
            "employee_id": lambda v: Q(employee_id=v),
        }
        where = Q()
        for key, value in keys.items():
            where |= clauses[key](value)
        candidates = list(employee_queryset().filter(where))

        matches = {}
        for key, value in keys.items():
            person = next((c for c in candidates if self._is(c, key, value)), None)
            if person is not None:
                matches[key] = person

        if not matches:
            raise NotFound("No employee matches the supplied keys.")
        if len({person.pk for person in matches.values()}) > 1:
            return Response({
                "detail": "The supplied keys identify different employees.",
                "matches": {key: person.employee_id for key, person in matches.items()},
            }, status=409)

        matched_by = next(key for key in _LOOKUP_KEYS if key in matches)
        user = matches[matched_by]
        note(request._request, subject=user, matched_by=matched_by)
        data = EmployeeDetailSerializer(user, context={
            "request": request, "direct_reports": list(_active_reports_of(user)),
        }).data
        return Response({**data, "matched_by": matched_by})

    @staticmethod
    def _keys(params) -> dict:
        keys = {}
        oid = (params.get("oid") or "").strip()
        if oid:
            try:
                keys["oid"] = str(uuid.UUID(oid))
            except ValueError:
                raise ValidationError({"oid": "Must be an Entra object id (a UUID)"})
        email = (params.get("email") or "").strip()
        if email:
            keys["email"] = email
        employee_id = (params.get("employee_id") or "").strip()
        if employee_id:
            keys["employee_id"] = employee_id
        if not keys:
            raise ValidationError({"detail": "Supply at least one of: oid, email, employee_id"})
        return keys

    @staticmethod
    def _is(user: CustomUser, key: str, value: str) -> bool:
        if key == "oid":
            return user.entra_object_id == value
        if key == "email":
            return (user.email or "").lower() == value.lower()
        return user.employee_id == value
