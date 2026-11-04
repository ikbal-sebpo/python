"""Canonical office buckets for SEBPO's Dhaka offices.

Azure AD's ``officeLocation`` arrives as free-text and the SSO sync at
``accounts/auth_backends.py`` writes the raw value straight into
``JobLocation.name``. We bucket each ``JobLocation`` into one of three short
codes — ``MBD``, ``MHK``, ``RAOWA`` — by substring match. Anything that
doesn't match (e.g. Chattogram, Gulshan) goes into ``OTHER``. Employees with
no ``job_location`` go into ``UNKNOWN``.

Match order is significant: RAOWA Complex addresses contain "Mohakhali",
so RAOWA is checked before MHK.
"""

from __future__ import annotations

import re

OFFICE_CODES = ("MBD", "MHK", "RAOWA")

_PATTERNS = (
    ("RAOWA", re.compile(r"\bRAOWA\b", re.IGNORECASE)),
    ("MBD",   re.compile(r"\bMBD\b|Monem|Business\s+District", re.IGNORECASE)),
    ("MHK",   re.compile(r"\bMHK\b|Mohakhali|Abbas\s+Garden|\b1206\b", re.IGNORECASE)),
)


def work_location_code(work_location) -> str | None:
    """Map a ``WorkLocation`` row to a short code: WFO / WFH / HYB / None.
    Shared by the roster and directory APIs."""
    if work_location is None:
        return None
    name = (work_location.name or "").upper()
    if "WFH" in name or "HOME" in name:
        return "WFH"
    if "HYBRID" in name:
        return "HYB"
    if "WFO" in name or "OFFICE" in name:
        return "WFO"
    return None


def office_bucket(job_location) -> str:
    """Return one of ``MBD`` / ``MHK`` / ``RAOWA`` / ``OTHER`` / ``UNKNOWN``.

    Accepts a ``JobLocation`` instance, a raw string, or ``None``.
    """
    if job_location is None:
        return "UNKNOWN"
    if hasattr(job_location, "name"):
        haystack = " ".join(filter(None, [
            job_location.name or "",
            getattr(job_location, "description", "") or "",
        ]))
    else:
        haystack = str(job_location)
    if not haystack.strip():
        return "UNKNOWN"
    for code, pattern in _PATTERNS:
        if pattern.search(haystack):
            return code
    return "OTHER"


# ---------------------------------------------------------------------------
# Canonical row resolution for the per-day roster cell location dropdown.
# Surfaces a stable (MBD / MHK / RAOWA / WFH) choice — backed by real
# JobLocation / WorkLocation rows that we get-or-create on first use.
# ---------------------------------------------------------------------------

LOCATION_DROPDOWN_CHOICES = (
    ("MHK",   "MHK"),
    ("MBD",   "MBD"),
    ("RAOWA", "RAOWA"),
    ("WFH",   "WFH"),
)
LOCATION_DROPDOWN_VALUES = {c for c, _ in LOCATION_DROPDOWN_CHOICES}


def get_canonical_office_jobloc(code: str):
    """Return (or create) the canonical ``JobLocation`` for an office code.

    ``code`` is one of ``MBD`` / ``MHK`` / ``RAOWA``. If an existing active
    row already buckets to that code (handles the messy Azure-AD raw
    addresses), we reuse it. Otherwise we create a minimal canonical row
    named after the code so the FK target always exists.
    """
    if code not in OFFICE_CODES:
        return None
    from accounts.models import JobLocation
    # First pass: any existing active row that buckets to this code.
    for jl in JobLocation.objects.filter(is_active=True):
        if office_bucket(jl) == code:
            return jl
    # Fallback: create canonical placeholder. SSO can later auto-create the
    # rich Azure-AD address row; both will continue to bucket to the same code.
    jl, _ = JobLocation.objects.get_or_create(
        name=code, defaults={"code": code, "is_active": True},
    )
    if not jl.code:
        jl.code = code
        jl.save(update_fields=["code"])
    return jl


def get_wfh_worklocation():
    """Return (or create) the WFH ``WorkLocation``."""
    from accounts.models import WorkLocation
    wl = WorkLocation.objects.filter(is_active=True, name__icontains="home").first()
    if wl:
        return wl
    wl, _ = WorkLocation.objects.get_or_create(
        name="Work from Home (WFH)", defaults={"is_active": True},
    )
    return wl


def get_wfo_worklocation():
    """Return (or create) the WFO ``WorkLocation`` (for office days)."""
    from accounts.models import WorkLocation
    wl = WorkLocation.objects.filter(is_active=True, name__icontains="office").first()
    if wl:
        return wl
    wl, _ = WorkLocation.objects.get_or_create(
        name="Work from Office (WFO)", defaults={"is_active": True},
    )
    return wl


def resolve_location_choice(choice: str):
    """Map a dropdown choice (``MHK`` / ``MBD`` / ``RAOWA`` / ``WFH``) to
    a ``(job_location, work_location)`` tuple suitable for assigning to
    a ``Roster`` or ``Shift`` row.

    Empty / unknown choice returns ``(None, None)`` — caller should treat
    that as "leave both unset" so the cell falls back to inherited values.
    """
    if not choice:
        return None, None
    if choice == "WFH":
        return None, get_wfh_worklocation()
    if choice in OFFICE_CODES:
        return get_canonical_office_jobloc(choice), get_wfo_worklocation()
    return None, None


def resolve_to_dropdown_choice(job_location, work_location) -> str:
    """Inverse of ``resolve_location_choice``: collapse a (job, work) pair
    into one of the four dropdown values, or empty if neither matches.
    Falls back to ``office_bucket`` so the canonical short code always wins.
    """
    if work_location:
        up = (work_location.name or "").upper()
        if up.startswith("WORK FROM HOME") or up == "WFH":
            return "WFH"
    if job_location:
        bucket = office_bucket(job_location)
        if bucket in OFFICE_CODES:
            return bucket
    return ""
