"""GH OT auto-generation — the shared logic behind every `holiday_ot`
generator command and its Celery beat entry, so a CLI/UI path can never
drift from the write rules. Two independent sources feed the same write
path (`_apply_candidates`):

- **Software Development / Creative Development** (`generate_gh_overtime_for_date`,
  `manage.py generate_gh_overtime`): presence from `AttendancePunch`/TimeLy,
  a flat 8h grant for anyone checked in with >= --min-hours of office time.
  See that command's module docstring and docs/PAYROLL_PLAN.md's 2026-09-17
  section for the full policy rationale.
- **General BPO** (`generate_bpo_gh_overtime_for_date`, `manage.py
  generate_gh_overtime_bpo`, added 2026-09-17): presence AND hours both come
  from ttms.sebpo.net's daily-hours API (`payroll/ttms.py`) — each entry's
  seconds is the employee's own TTMS-reported worked time for that date, not
  a flat grant, since General BPO's `holiday_ot` PayRate is PER_HOUR.

Both are additive-only and idempotent: an employee who already has a
`holiday_ot` OvertimeEntry for that date, in ANY status (PENDING, APPROVED,
or a deliberate VOID), is left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser, Department
from attendance.models import AttendancePunch
from payroll.models import OvertimeEntry, OvertimeType, PayrollPeriod
from payroll.periods import period_for_date
from payroll.services import OvertimeWindow
from payroll.ttms import fetch_daily_hours
from payroll.upload import employed_on
from roster.models import Holiday

DEPARTMENT_NAMES = ("Software Development", "Creative Development")
OT_TYPE_CODE = "holiday_ot"
DEFAULT_MIN_HOURS = 4.0
DEFAULT_GRANT_HOURS = 8.0

BPO_DEPARTMENT_NAME = "General BPO"
DEFAULT_BPO_MIN_HOURS = 0.0  # TTMS reports exact worked hours already — any positive amount counts


@dataclass
class GHOvertimeCandidate:
    employee: object  # CustomUser
    seconds: int


@dataclass
class GHOvertimeRow:
    employee: object  # CustomUser
    status: str  # "created" | "would_create" | "already_exists" | "not_employed" | "window_rejected"
    reason: str = ""
    seconds: int | None = None

    @property
    def hours(self) -> float:
        return (self.seconds or 0) / 3600


@dataclass
class GHOvertimeReport:
    """Everything about one date's run — enough for both the CLI's text
    output and the UI's preview/result fragment to render from."""

    work_date: date
    is_holiday: bool = False
    ot_type_missing: bool = False
    departments_missing: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # GHOvertimeRow, only present employees
    applied: bool = False

    def _by_status(self, status: str) -> list:
        return [r for r in self.rows if r.status == status]

    @property
    def created(self) -> list:
        return self._by_status("created") + self._by_status("would_create")

    @property
    def already_existing(self) -> list:
        return self._by_status("already_exists")

    @property
    def not_employed(self) -> list:
        return self._by_status("not_employed")

    @property
    def window_rejected(self) -> list:
        return self._by_status("window_rejected")


def _apply_candidates(
    report: GHOvertimeReport, candidates: list[GHOvertimeCandidate],
    *, ot_type: OvertimeType, apply: bool, actor, note: str,
) -> GHOvertimeReport:
    """Shared write path: given employees who worked `report.work_date` and
    how many seconds each, resolve the payroll window and write (or
    preview) one `ot_type` OvertimeEntry per candidate not already holding
    one — mutates and returns `report`. Used by every GH OT source so they
    can't drift on the actual write/idempotency/window rules.
    """
    if not candidates:
        return report

    work_date = report.work_date
    employee_pks = [c.employee.pk for c in candidates]
    existing_pks = set(
        OvertimeEntry.objects.filter(
            employee_id__in=employee_pks, work_date=work_date, overtime_type=ot_type,
        ).values_list("employee_id", flat=True)
    )

    window = OvertimeWindow(actor, today=timezone.localdate())
    period = PayrollPeriod.for_month(*period_for_date(work_date))

    with transaction.atomic():
        for c in candidates:
            employee = c.employee
            if employee.pk in existing_pks:
                report.rows.append(GHOvertimeRow(employee, "already_exists", seconds=c.seconds))
                continue
            if not employed_on(employee, work_date):
                report.rows.append(GHOvertimeRow(employee, "not_employed", seconds=c.seconds))
                continue
            decision = window.resolve(work_date)
            if not decision.allowed:
                report.rows.append(GHOvertimeRow(employee, "window_rejected", decision.reason, seconds=c.seconds))
                continue
            if not apply:
                report.rows.append(GHOvertimeRow(employee, "would_create", seconds=c.seconds))
                continue
            OvertimeEntry.objects.create(
                employee=employee, period=period, work_date=work_date,
                overtime_type=ot_type, worked_in=employee.team,
                seconds=c.seconds, status=decision.status, note=note,
                added_by=actor, updated_by=actor,
            )
            report.rows.append(GHOvertimeRow(employee, "created", seconds=c.seconds))

    report.applied = apply
    return report


def generate_gh_overtime_for_date(
    work_date: date, *, min_hours: float = DEFAULT_MIN_HOURS,
    grant_hours: float = DEFAULT_GRANT_HOURS, apply: bool, actor=None,
) -> GHOvertimeReport:
    """Software Development / Creative Development. apply=False is a pure
    read — no writes; used for the CLI's --dry-run and the UI's preview
    step. `actor` is stamped as added_by/updated_by on created rows — None
    for the headless beat job, the triggering superuser from the UI.
    """
    report = GHOvertimeReport(work_date=work_date)

    report.is_holiday = Holiday.objects.filter(date=work_date, is_active=True).exists()
    if not report.is_holiday:
        return report

    try:
        ot_type = OvertimeType.objects.get(code=OT_TYPE_CODE)
    except OvertimeType.DoesNotExist:
        report.ot_type_missing = True
        return report

    departments = list(Department.objects.filter(name__in=DEPARTMENT_NAMES))
    report.departments_missing = sorted(set(DEPARTMENT_NAMES) - {d.name for d in departments})
    if not departments:
        return report

    employees_by_pk = {
        e.pk: e for e in CustomUser.objects.filter(department__in=departments).select_related("team")
    }
    if not employees_by_pk:
        return report

    present_pks = set(
        AttendancePunch.objects.filter(
            date=work_date, employee_id__in=employees_by_pk.keys(),
            check_in__isnull=False, time_in_office__gte=timedelta(hours=min_hours),
        ).values_list("employee_id", flat=True)
    )
    if not present_pks:
        return report

    seconds = int(round(grant_hours * 3600))
    candidates = [GHOvertimeCandidate(employees_by_pk[pk], seconds) for pk in sorted(present_pks)]

    return _apply_candidates(
        report, candidates, ot_type=ot_type, apply=apply, actor=actor,
        note="Auto-generated GH OT from attendance presence.",
    )


def generate_bpo_gh_overtime_for_date(
    work_date: date, *, min_hours: float = DEFAULT_BPO_MIN_HOURS, apply: bool, actor=None,
) -> GHOvertimeReport:
    """General BPO. Same holiday-gate/idempotency/window rules as
    generate_gh_overtime_for_date, but presence AND hours both come from
    ttms.sebpo.net's daily-hours API rather than AttendancePunch, and each
    entry's seconds is the employee's own TTMS-reported work_hour for that
    date — General BPO's holiday_ot PayRate is PER_HOUR (added 2026-09-17),
    so paying exact worked hours rather than a flat day is the correct
    amount.

    Raises payroll.ttms.TTMSUnavailable if the integration isn't configured
    or the fetch fails — deliberately not swallowed, since a silently-empty
    result would be indistinguishable from "nobody worked" on a payroll
    write. Callers (the management command) turn that into a clear
    CommandError rather than a raw traceback.
    """
    report = GHOvertimeReport(work_date=work_date)

    report.is_holiday = Holiday.objects.filter(date=work_date, is_active=True).exists()
    if not report.is_holiday:
        return report

    try:
        ot_type = OvertimeType.objects.get(code=OT_TYPE_CODE)
    except OvertimeType.DoesNotExist:
        report.ot_type_missing = True
        return report

    department = Department.objects.filter(name=BPO_DEPARTMENT_NAME).first()
    if department is None:
        report.departments_missing = [BPO_DEPARTMENT_NAME]
        return report

    employees_by_employee_id = {
        e.employee_id: e
        for e in CustomUser.objects.filter(department=department).select_related("team")
        if e.employee_id
    }
    if not employees_by_employee_id:
        return report

    rows = fetch_daily_hours(BPO_DEPARTMENT_NAME, work_date, work_date)  # raises TTMSUnavailable

    min_seconds = int(round(min_hours * 3600))
    candidates: list[GHOvertimeCandidate] = []
    seen_employee_ids: set[str] = set()
    for row in rows:
        employee_id = str(row.get("employee_id"))
        if employee_id in seen_employee_ids:
            continue  # defensive: the API is documented as one row per employee/date
        employee = employees_by_employee_id.get(employee_id)
        if employee is None:
            continue  # not an Orbis General BPO employee (or an employee_id mismatch) — skip, not an error
        seconds = int(row.get("work_hour") or 0)
        if seconds <= min_seconds:
            continue
        seen_employee_ids.add(employee_id)
        candidates.append(GHOvertimeCandidate(employee, seconds))
    candidates.sort(key=lambda c: c.employee.pk)

    return _apply_candidates(
        report, candidates, ot_type=ot_type, apply=apply, actor=actor,
        note="Auto-generated GH OT from TTMS daily hours.",
    )
