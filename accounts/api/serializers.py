"""Directory API record shapes — docs/DIRECTORY_API_PLAN.md §5.

Directory data only (D8): every serializer lists its fields explicitly, and
`accounts/api/tests.py` pins them, so nothing personal or payroll-related can
slip in through a model change.
"""

from __future__ import annotations

from rest_framework import serializers

from accounts.locations import office_bucket, work_location_code
from accounts.models import CustomUser, Department, Designation, JobLocation, Team


class EmployeeRefSerializer(serializers.ModelSerializer):
    """A pointer to a person — used for department heads."""

    full_name = serializers.CharField(source="display_name", read_only=True)

    class Meta:
        model = CustomUser
        fields = ("employee_id", "email", "full_name")


class DepartmentSerializer(serializers.ModelSerializer):
    head_of_department = EmployeeRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Department
        fields = ("id", "name", "is_active", "head_of_department")


class DesignationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Designation
        fields = ("id", "name", "is_active")


class TeamSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", default=None, read_only=True)

    class Meta:
        model = Team
        fields = ("id", "name", "is_active", "department_id", "department_name")


class OfficeSerializer(serializers.ModelSerializer):
    # The canonical bucket (MBD / MHK / RAOWA / OTHER), not the free-text
    # `JobLocation.code`, so an Entra-imported duplicate still reads as its office.
    code = serializers.SerializerMethodField()

    class Meta:
        model = JobLocation
        fields = ("id", "name", "code", "is_active")

    def get_code(self, obj: JobLocation) -> str:
        return office_bucket(obj)


# ---------------------------------------------------------------------------
# Employees — §5.1. Built over `accounts/api/views.py::employee_queryset`,
# which select_relates everything read here and annotates
# `active_direct_reports` and `record_updated_at`.
# ---------------------------------------------------------------------------

def _status(user: CustomUser) -> str:
    return "active" if user.is_active else "inactive"


def _named(obj) -> dict | None:
    return None if obj is None else {"id": obj.pk, "name": obj.name}


def _job_info(user: CustomUser):
    # Reverse one-to-one: raises when the row is missing, even when select_related.
    try:
        return user.job_info
    except CustomUser.job_info.RelatedObjectDoesNotExist:
        return None


class EmployeeSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="display_name", read_only=True)
    status = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    team = serializers.SerializerMethodField()
    designation = serializers.SerializerMethodField()
    office = serializers.SerializerMethodField()
    work_location = serializers.SerializerMethodField()
    supervisor = serializers.SerializerMethodField()
    direct_reports_count = serializers.IntegerField(source="active_direct_reports", read_only=True)
    updated_at = serializers.DateTimeField(source="record_updated_at", read_only=True)

    class Meta:
        model = CustomUser
        fields = (
            "employee_id", "entra_object_id", "email", "full_name", "first_name", "last_name",
            "status", "joining_date", "last_working_date",
            "department", "team", "designation", "office", "work_location",
            "supervisor", "direct_reports_count", "updated_at",
        )

    def get_status(self, obj) -> str:
        return _status(obj)

    def get_department(self, obj):
        return _named(obj.department)

    def get_team(self, obj):
        if obj.team is None:
            return None
        return {"id": obj.team.pk, "name": obj.team.name, "department_id": obj.team.department_id}

    def get_designation(self, obj):
        return _named(obj.designation)

    def get_office(self, obj):
        jl = obj.job_location
        return None if jl is None else {"id": jl.pk, "code": office_bucket(jl), "name": jl.name}

    def get_work_location(self, obj):
        return work_location_code(obj.work_location)

    def get_supervisor(self, obj):
        job_info = _job_info(obj)
        boss = job_info.reports_to if job_info else None
        if boss is None:
            return None
        return {
            "employee_id": boss.employee_id, "email": boss.email,
            "full_name": boss.display_name, "status": _status(boss),
        }


class DirectReportSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="display_name", read_only=True)
    designation = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ("employee_id", "email", "full_name", "designation", "status")

    def get_designation(self, obj):
        return _named(obj.designation)

    def get_status(self, obj) -> str:
        return _status(obj)


class EmployeeDetailSerializer(EmployeeSerializer):
    """One record plus its active direct reports, passed in as
    ``context["direct_reports"]`` (one extra query, made by the view)."""

    direct_reports = serializers.SerializerMethodField()

    class Meta(EmployeeSerializer.Meta):
        fields = (*EmployeeSerializer.Meta.fields, "direct_reports")

    def get_direct_reports(self, obj):
        return DirectReportSerializer(self.context.get("direct_reports", []), many=True).data


class ScopedEmployeeSerializer(EmployeeSerializer):
    """An employee record plus how it falls in a manager's scope:
    ``{"via": ["reports_to", "department", "team"], "depth": 2 | null}`` —
    ``via`` lists every grant that applies, ``depth`` is the reports-to
    distance (null when the person is in scope only through a grant).
    Reasons come in as ``context["scope"]`` (``{pk: {...}}``)."""

    scope = serializers.SerializerMethodField()

    class Meta(EmployeeSerializer.Meta):
        fields = (*EmployeeSerializer.Meta.fields, "scope")

    def get_scope(self, obj):
        return self.context["scope"][obj.pk]
