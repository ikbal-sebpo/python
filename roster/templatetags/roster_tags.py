"""Template helpers for the roster grid."""

from __future__ import annotations

from django import template

from accounts.locations import resolve_to_dropdown_choice

register = template.Library()


@register.simple_tag
def resolved_location_choice(shift, employee) -> str:
    """Return the canonical dropdown value (MHK/MBD/RAOWA/WFH) for a cell.

    Order of precedence:
        1. The Shift row's own ``job_location`` / ``work_location``
        2. The employee's ``CustomUser.job_location`` / ``work_location``
        3. Empty string when neither resolves to a tracked office or WFH

    Used by ``_cell.html`` to preselect the right option in the per-cell
    location dropdown — explicit per-day overrides take priority, but a
    blank cell still shows the employee's contractual location.
    """
    if shift is not None:
        own = resolve_to_dropdown_choice(shift.job_location, shift.work_location)
        if own:
            return own
    if employee is not None:
        return resolve_to_dropdown_choice(
            getattr(employee, "job_location", None),
            getattr(employee, "work_location", None),
        )
    return ""
