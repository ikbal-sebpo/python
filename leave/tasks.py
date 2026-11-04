"""Deferred leave tasks (dispatched via core.tasks.defer)."""

from __future__ import annotations

from datetime import date, timedelta


def recompute_holiday_window(date_iso: str) -> None:
    """Recompute sandwich billing for every employee holding an approved
    leave that overlaps ±14 days of a changed Holiday date.

    Runs on the task queue: a single Holiday save can fan out into a
    recompute per affected employee, which is far too slow for the admin
    request that saved the Holiday. Idempotent — recompute_sandwich
    reconciles to the current state whatever the starting point.
    """
    from accounts.models import CustomUser
    from leave.models import LeaveApplication
    from leave.services import recompute_sandwich

    around = date.fromisoformat(date_iso)
    affected_employee_pks = set(
        LeaveApplication.objects
        .filter(
            status=LeaveApplication.Status.APPROVED,
            start_date__lte=around + timedelta(days=14),
            end_date__gte=around - timedelta(days=14),
        )
        .values_list("employee_id", flat=True)
    )
    for emp in CustomUser.objects.filter(pk__in=affected_employee_pks):
        recompute_sandwich(emp, around, request_user=None)
