"""Operational helpers for the roster module — pattern auto-creation and
shift pre-fill.

Permission helpers (visibility / edit gating) live in `accounts.permissions`;
this module only contains roster-domain business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.utils import timezone

from accounts.locations import (
    LOCATION_DROPDOWN_VALUES,
    resolve_location_choice,
    resolve_to_dropdown_choice,
)
from roster.models import Roster, Shift, ShiftAttribute, ShiftChangeRequest, ShiftType
from roster.night_window import is_restricted_shift_type


# Default Mon–Fri assignment is the EVE shift, specifically the "EVE 1-10"
# attribute (13:00–22:00). Sat/Sun fall back to the OFF shift.
#
# EVE 1-10 must stay globally unscoped (empty ShiftAttribute.departments) —
# ensure_default_pattern() below assigns it to every new hire regardless of
# department, so scoping it to specific departments would make new hires
# outside those departments land on an attribute invisible in their own
# roster dropdown from day one. See docs/PLAN.md, "Roster + leave — unified
# cell model".
DEFAULT_WORK_SHIFT_NAME = "EVE"
DEFAULT_WORK_ATTRIBUTE_NAME = "EVE 1-10"
DEFAULT_OFF_SHIFT_NAME = "OFF"


# --- Default-pattern auto-generation --------------------------------------

def _default_work_shift():
    """Return (ShiftType, ShiftAttribute|None) for new-user weekdays.

    Tries the canonical (EVE / EVE 1-10) pair first. If EVE doesn't exist
    (legacy DB), falls back to the first active WORK shift by configured order
    with no attribute.
    """
    eve = (
        ShiftType.objects
        .filter(is_active=True, name__iexact=DEFAULT_WORK_SHIFT_NAME)
        .first()
    )
    if eve:
        attr = ShiftAttribute.objects.filter(
            shift_type=eve, name__iexact=DEFAULT_WORK_ATTRIBUTE_NAME, is_active=True,
        ).first()
        return eve, attr
    fallback = (
        ShiftType.objects
        .filter(is_active=True, category=ShiftType.Category.WORK)
        .order_by(F("sort_order").asc(nulls_last=True), "name").first()
    )
    return fallback, None


def _default_off_shift():
    by_name = (
        ShiftType.objects
        .filter(is_active=True, name__iexact=DEFAULT_OFF_SHIFT_NAME)
        .first()
    )
    if by_name:
        return by_name
    return (
        ShiftType.objects
        .filter(is_active=True, category=ShiftType.Category.OFF)
        .order_by(F("sort_order").asc(nulls_last=True), "name").first()
    )


def ensure_default_pattern(user) -> bool:
    """Create Mon-Fri=<work>, Sat/Sun=<off> if the user has NO Roster rows.

    Mon–Fri rows are assigned the EVE ShiftType + EVE 1-10 ShiftAttribute
    (13:00–22:00). Sat/Sun get OFF.

    Returns True if rows were created. Idempotent — once a pattern exists
    (even a single row), this helper is a no-op so line managers' partial
    customisations aren't stomped on.
    """
    if not user or not getattr(user, "pk", None):
        return False
    if Roster.objects.filter(employee_id=user.pk).exists():
        return False

    work, work_attr = _default_work_shift()
    off = _default_off_shift()
    rows = []
    for wd in range(7):                       # 0=Mon … 6=Sun (Orbis convention)
        is_weekend = wd in (5, 6)
        target = off if is_weekend else work
        if target is None:
            continue
        rows.append(Roster(
            employee_id=user.pk, weekday=wd,
            shift_type=target,
            shift_attribute=None if is_weekend else work_attr,
            added_by_id=user.pk, updated_by_id=user.pk,
        ))
    if not rows:
        return False
    Roster.objects.bulk_create(rows, ignore_conflicts=True)
    return True


# --- Last-working-date cutoff ----------------------------------------------
#
# The rule, enforced at every write path that can produce a Shift: if an
# employee has a `last_working_date` (LWD) set, no Shift may exist for a date
# AFTER it. The LWD itself is fine — the person worked through their last day
# (same "strictly past" convention as offboarding's deactivation, which fires
# on `last_working_date < today`).
#
# Guards live in:
#   * `prefill_shifts_from_pattern` below — the roster week grid
#   * `leave.services.materialise_shifts_for_application` — approved leave
#   * `roster.upload.apply_upload` — the bulk roster importer
#   * `roster.api.shift_writes` — the external write API
#
# and the purge below is wired into the three points where an LWD gets set
# (resignation accept, manual HR edit, the nightly deactivation backstop),
# plus the on-demand `manage.py purge_shifts_past_lwd` backfill.

def shift_is_after_last_working_date(employee, day: date) -> bool:
    """True when `day` falls past this employee's LWD (no LWD → never)."""
    lwd = getattr(employee, "last_working_date", None)
    return lwd is not None and day > lwd


# --- Joining-date cutoff -----------------------------------------------------
#
# Mirror image of the LWD cutoff above: no Shift may exist for a date BEFORE
# an employee's `joining_date`. Without this, a new hire's auto-seeded weekly
# `Roster` pattern (ensure_default_pattern) gets materialised onto dates that
# predate their employment the moment anyone opens a roster week/report that
# spans their join date — same failure mode the LWD guard already prevents on
# the other end.
#
# Guards live in the same places as the LWD ones:
#   * `prefill_shifts_from_pattern` below — the roster week grid
#   * `roster.upload.apply_upload` — the bulk roster importer
#   * `roster.api.shift_writes` — the external write API
#   * `roster.views.CellUpdateView` — manual single-cell edits

def shift_is_before_joining_date(employee, day: date) -> bool:
    """True when `day` falls before this employee's joining date (no joining_date → never)."""
    joined = getattr(employee, "joining_date", None)
    return joined is not None and day < joined


def audit_shifts_before_joining_date() -> list[tuple]:
    """Read-only, company-wide report: every employee who has a `Shift` row
    dated before their own `joining_date`.

    Returns `(display_name, email, joining_date, shift_count, earliest_date,
    latest_date)` tuples, ordered by joining_date. Never deletes anything —
    this is the audit counterpart to `offboarding.services.purge_shifts_past_lwd`,
    which does the equivalent sweep (and actually deletes) for the LWD side.
    Backs `manage.py audit_shifts_before_joining_date`.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Count, Min, Max

    User = get_user_model()
    out: list[tuple] = []
    candidates = (
        User.objects
        .filter(joining_date__isnull=False)
        .order_by("joining_date", "email")
    )
    for user in candidates:
        agg = Shift.objects.filter(
            employee_id=user.pk, date__lt=user.joining_date,
        ).aggregate(n=Count("id"), earliest=Min("date"), latest=Max("date"))
        if not agg["n"]:
            continue
        out.append((
            getattr(user, "display_name", "") or user.email,
            user.email, user.joining_date, agg["n"], agg["earliest"], agg["latest"],
        ))
    return out


def purge_shifts_after_last_working_date(employee, *, actor=None) -> int:
    """Delete Shift rows dated after employee.last_working_date, if set.

    Uses a real queryset .delete() (not bulk raw SQL) so per-row pre_delete
    signals fire: leave-ledger reversal (leave/signals.py), CpL-grant
    reversal, and ShiftChangeLog audit rows (roster/signals.py).

    `actor` stamps `updated_by` before the delete so the audit rows name a
    person; pass None for system-triggered purges (there's no acting user in
    a signal or a nightly batch).

    Returns the number of Shift rows deleted.
    """
    if employee is None or not getattr(employee, "pk", None):
        return 0
    lwd = getattr(employee, "last_working_date", None)
    if lwd is None:
        return 0

    qs = Shift.objects.filter(employee_id=employee.pk, date__gt=lwd)
    count = qs.count()
    if not count:
        return 0
    if actor is not None and getattr(actor, "pk", None):
        qs.update(updated_by=actor, updated_at=timezone.now())
    qs.delete()
    return count


def purge_shifts_before_joining_date(employee, *, actor=None) -> int:
    """Delete Shift rows dated before employee.joining_date, if set.

    Mirror image of `purge_shifts_after_last_working_date` — same real
    queryset .delete() (not bulk raw SQL) so per-row pre_delete signals fire,
    same `actor`-stamps-`updated_by`-first contract.

    Returns the number of Shift rows deleted.
    """
    if employee is None or not getattr(employee, "pk", None):
        return 0
    joined = getattr(employee, "joining_date", None)
    if joined is None:
        return 0

    qs = Shift.objects.filter(employee_id=employee.pk, date__lt=joined)
    count = qs.count()
    if not count:
        return 0
    if actor is not None and getattr(actor, "pk", None):
        qs.update(updated_by=actor, updated_at=timezone.now())
    qs.delete()
    return count


# --- Prefill daily shifts from pattern -------------------------------------

def prefill_shifts_from_pattern(request_user, employees, date_from: date, date_to: date) -> int:
    """Materialise missing Shift rows for (employee, date) in the range.

    Contract: NEVER overwrites an existing Shift row — approved leaves,
    manual overrides, and bulk-imported rows are preserved. Both the
    (employee_id, date) membership check AND `ignore_conflicts=True` on
    bulk_create enforce this.

    Dates past an employee's `last_working_date`, or before their
    `joining_date`, are skipped — otherwise simply viewing the roster week
    grid would re-materialise shifts for someone who has already left, or for
    a new hire before they existed as an employee. The employee's weekly
    `Roster` pattern is deliberately left in place (non-destructive — it
    comes straight back if either boundary is later corrected or cleared).

    Returns the number of rows created.
    """
    if date_from > date_to:
        return 0
    days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
    emp_ids = [e.pk for e in employees]
    if not emp_ids:
        return 0

    existing = set(
        Shift.objects
        .filter(employee_id__in=emp_ids, date__gte=date_from, date__lte=date_to)
        .values_list("employee_id", "date")
    )

    pattern_rows = (
        Roster.objects
        .filter(employee_id__in=emp_ids)
        .select_related("shift_type", "shift_attribute", "job_location", "work_location")
    )
    patterns = {(p.employee_id, p.weekday): p for p in pattern_rows}

    to_create = []
    added_by_id = getattr(request_user, "pk", None) if request_user and request_user.is_authenticated else None
    for emp in employees:
        for d in days:
            if (emp.pk, d) in existing:
                continue
            if shift_is_after_last_working_date(emp, d):
                continue
            if shift_is_before_joining_date(emp, d):
                continue
            pat = patterns.get((emp.pk, d.weekday()))
            if pat is None:
                continue
            to_create.append(Shift(
                employee_id=emp.pk, date=d,
                shift_type=pat.shift_type,
                shift_attribute=pat.shift_attribute,
                job_location_id=pat.job_location_id,
                work_location_id=pat.work_location_id,
                added_by_id=added_by_id, updated_by_id=added_by_id,
            ))
    if not to_create:
        return 0
    Shift.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


# --- Department-scoped shift choices ---------------------------------------

def visible_shift_types(department_id=None, include_attr_ids=()):
    """ShiftType queryset (active, ordered) with attributes prefetched.

    `department_id=None` — no restriction: every active attribute (today's
    behavior; used when no single department applies, e.g. an admin viewing
    "all departments").

    `department_id=<id>` — attributes with no department set (global default),
    OR scoped to this department, OR whose pk is in `include_attr_ids` (an
    always-include escape hatch so an employee's already-assigned attribute
    never disappears from their own picker just because it's now out of
    scope for new picks — see the roster views/forms callers).

    ShiftType itself carries no department scoping — every WORK ShiftType
    today has attributes (`has_attribute=True`), so the bare-type dropdown
    branch is unaffected; see docs/PLAN.md.
    """
    attr_qs = ShiftAttribute.objects.filter(is_active=True)
    if department_id is not None:
        attr_qs = attr_qs.filter(
            Q(departments__isnull=True)
            | Q(departments__id=department_id)
            | Q(pk__in=include_attr_ids)
        ).distinct()
    attr_qs = attr_qs.order_by(F("sort_order").asc(nulls_last=True), "start_time", "name")
    return (
        ShiftType.objects.filter(is_active=True)
        .prefetch_related(Prefetch("attributes", queryset=attr_qs))
        .order_by(F("sort_order").asc(nulls_last=True), "name")
    )


# ---------------------------------------------------------------------------
# Shift-ref dropdown — one encoding shared by the grid cell, the weekly
# pattern form and the shift-change request form.
#
#   ""            → nothing selected
#   "type:<pk>"   → a ShiftType without attributes
#   "attr:<pk>"   → a specific ShiftAttribute (its parent type is implied)
# ---------------------------------------------------------------------------

class ShiftRefError(ValidationError):
    """A posted `shift_ref` that doesn't resolve to a visible option."""


def resolve_shift_ref(ref, department_id=None, include_attr_ids=()):
    """Parse a `shift_ref` value into `(shift_type, shift_attribute)`.

    Either may be None; an empty ref returns `(None, None)` and the caller
    decides what that means (the grid cell clears the row).

    `department_id`, when given, re-validates a posted attribute against the
    same department scoping `visible_shift_types` applies to the dropdown —
    closes the gap where a tampered POST could otherwise submit any active
    attribute pk regardless of what was rendered. `include_attr_ids` lets a
    row's own pre-existing attribute resubmit unchanged even once it's out
    of scope for new picks. `ShiftType` (the `type:` kind) carries no
    department scoping.

    Raises `ShiftRefError` (a `ValidationError`) rather than 404 so forms can
    surface it as a field error; the roster views wrap it into an Http404.
    """
    if not ref:
        return None, None
    try:
        kind, raw_pk = ref.split(":", 1)
        pk = int(raw_pk)
    except (ValueError, AttributeError):
        raise ShiftRefError("Invalid shift reference.")
    if kind == "attr":
        attr_qs = ShiftAttribute.objects.select_related("shift_type").filter(is_active=True)
        if department_id is not None:
            # `departments` is M2M — an attribute linked to multiple
            # departments joins once per linked row, so any OR branch true
            # independent of that join (e.g. pk__in) matches every joined
            # row. distinct() collapses those back to one row per attribute.
            attr_qs = attr_qs.filter(
                Q(departments__isnull=True)
                | Q(departments__id=department_id)
                | Q(pk__in=include_attr_ids)
            ).distinct()
        attr = attr_qs.filter(pk=pk).first()
        if attr is None:
            raise ShiftRefError("That shift option isn't available for this employee.")
        if not attr.shift_type_id:
            raise ShiftRefError("Attribute has no parent shift type.")
        return attr.shift_type, attr
    if kind == "type":
        st = ShiftType.objects.filter(pk=pk, is_active=True).first()
        if st is None:
            raise ShiftRefError("That shift type isn't available.")
        if st.has_attribute:
            raise ShiftRefError("Pick a specific attribute for this shift type.")
        return st, None
    raise ShiftRefError("Unknown shift reference kind.")


def shift_ref_for(shift_type, shift_attribute) -> str:
    """Inverse of `resolve_shift_ref` for a (type, attribute) pair."""
    if shift_attribute is not None:
        return f"attr:{shift_attribute.pk}"
    if shift_type is not None:
        return f"type:{shift_type.pk}"
    return ""


def _shift_option_sort_key(option: dict) -> tuple[bool, int, str]:
    sort_order = option["sort_order"]
    return (sort_order is None, sort_order or 0, option["label"].casefold())


def _time_suffix(start, end) -> str:
    if not start:
        return ""
    return f" ({start.strftime('%H:%M')}–{end.strftime('%H:%M')})"


def shift_choice_groups(department_id=None, include_attr_ids=(), *, categories=None,
                        attr_times=False):
    """Grouped dropdown options for a shift-ref `<select>`.

    Returns `[{"label": <category label>, "options": [{value, label,
    shift_type_id, shift_attribute_id, sort_order}, ...]}, ...]` — the shape
    `roster/_cell.html` renders. Empty categories are dropped.

    `categories` — an iterable of `ShiftType.Category` values to keep; None
    keeps everything (the grid). `attr_times` appends the attribute's
    HH:MM–HH:MM span to its label (the weekly pattern form does, the grid
    doesn't — the cell is too narrow).
    """
    keep = set(categories) if categories is not None else None
    by_category = {
        value: {"label": label, "options": []}
        for value, label in ShiftType.Category.choices
        if keep is None or value in keep
    }
    for st in visible_shift_types(department_id, include_attr_ids):
        if st.category not in by_category:
            continue
        options = by_category[st.category]["options"]
        # Greyed out in a dedicated night shift window for non-approvers.
        restricted = is_restricted_shift_type(st)
        if st.has_attribute:
            for attr in st.attributes.all():
                label = attr.name
                if attr_times:
                    label += _time_suffix(attr.start_time, attr.end_time)
                options.append({
                    "value": f"attr:{attr.pk}",
                    "label": label,
                    "shift_type_id": st.pk,
                    "shift_attribute_id": attr.pk,
                    "sort_order": attr.sort_order,
                    "night_restricted": restricted,
                })
        else:
            options.append({
                "value": f"type:{st.pk}",
                "label": st.name + _time_suffix(st.shift_start_time, st.shift_end_time),
                "shift_type_id": st.pk,
                "shift_attribute_id": None,
                "sort_order": st.sort_order,
                "night_restricted": restricted,
            })

    groups = []
    for group in by_category.values():
        if group["options"]:
            group["options"].sort(key=_shift_option_sort_key)
            groups.append(group)
    return groups


def shift_ref_choices(department_id=None, include_attr_ids=(), *, categories=None,
                      attr_times=False, blank_label="—"):
    """`shift_choice_groups` rendered as Django `ChoiceField`/`Select`
    choices: a blank first, then one optgroup tuple per category."""
    choices = [("", blank_label)]
    for group in shift_choice_groups(
        department_id, include_attr_ids, categories=categories, attr_times=attr_times,
    ):
        choices.append((
            group["label"],
            [(o["value"], o["label"]) for o in group["options"]],
        ))
    return choices


# ---------------------------------------------------------------------------
# Shift change requests (docs/SHIFT_CHANGE_REQUEST_PLAN.md)
# ---------------------------------------------------------------------------

# Categories an employee may request. LEAVE is owned by the leave module
# (balances, sandwich rule — use /leave/apply/); HOLIDAY is a calendar fact.
REQUESTABLE_SHIFT_CATEGORIES = frozenset({
    ShiftType.Category.WORK, ShiftType.Category.OFF,
})


@dataclass(frozen=True)
class CurrentCell:
    """What the roster says for one (employee, date) right now."""
    shift_type: ShiftType | None
    shift_attribute: ShiftAttribute | None
    location: str          # MHK / MBD / RAOWA / WFH / "" (resolved_location_choice)
    is_concrete: bool      # True = a Shift row exists, False = weekly-pattern fallback

    @property
    def shift_type_id(self) -> int | None:
        return self.shift_type.pk if self.shift_type is not None else None

    @property
    def shift_attribute_id(self) -> int | None:
        return self.shift_attribute.pk if self.shift_attribute is not None else None

    @property
    def shift_ref(self) -> str:
        return shift_ref_for(self.shift_type, self.shift_attribute)

    @property
    def label(self) -> str:
        if self.shift_attribute is not None:
            return self.shift_attribute.name
        if self.shift_type is not None:
            return self.shift_type.name
        return ""


def _cell_location(row, employee) -> str:
    """`roster_tags.resolved_location_choice` without the template dependency:
    the row's own override, else the employee's contract location."""
    if row is not None:
        own = resolve_to_dropdown_choice(row.job_location, row.work_location)
        if own:
            return own
    return resolve_to_dropdown_choice(
        getattr(employee, "job_location", None), getattr(employee, "work_location", None),
    )


def resolve_current_cell(employee, day: date) -> CurrentCell:
    """Shift row wins, else the weekly Roster pattern — the same order
    `attendance.views._resolve_shifts` and the dashboard use — with the
    pattern branch guarded by the employment window (a pattern is keyed
    only by weekday, so on its own it would "resolve" a shift before the
    employee joined or after they left)."""
    row = (
        Shift.objects.filter(employee=employee, date=day)
        .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        .first()
    )
    if row is not None:
        return CurrentCell(row.shift_type, row.shift_attribute, _cell_location(row, employee), True)
    if shift_is_before_joining_date(employee, day) or shift_is_after_last_working_date(employee, day):
        return CurrentCell(None, None, _cell_location(None, employee), False)
    pattern = (
        Roster.objects.filter(employee=employee, weekday=day.weekday())
        .select_related("shift_type", "shift_attribute", "job_location", "work_location")
        .first()
    )
    if pattern is not None:
        return CurrentCell(
            pattern.shift_type, pattern.shift_attribute, _cell_location(pattern, employee), False,
        )
    return CurrentCell(None, None, _cell_location(None, employee), False)


def leave_blocks_shift_change(employee, day: date) -> str:
    """Why a shift change can't be filed for this date because of leave, or
    "" when it can. Covers both a materialised/roster-direct LEAVE Shift
    and a leave application (pending or approved) spanning the date —
    those days are owned by the leave module."""
    from leave.models import LeaveApplication

    row = Shift.objects.filter(employee=employee, date=day).select_related("shift_type").first()
    if row is not None and row.shift_type.category == ShiftType.Category.LEAVE:
        return (
            f"{day.isoformat()} is a {row.shift_type.name} day. Cancel the leave "
            "before requesting a shift change."
        )
    app = (
        LeaveApplication.objects
        .filter(
            employee=employee,
            status__in=[LeaveApplication.Status.PENDING, LeaveApplication.Status.APPROVED],
            start_date__lte=day, end_date__gte=day,
        )
        .select_related("leave_type")
        .first()
    )
    if app is not None:
        return (
            f"A {app.get_status_display().lower()} {app.leave_type.name} application "
            f"already covers {day.isoformat()}. Cancel it before requesting a shift change."
        )
    return ""


def validate_shift_change(req: ShiftChangeRequest, *, actor=None, stage: str = "file") -> None:
    """Business rules for a shift-change request, shared by the form (at
    filing) and `apply_shift_change` (re-run inside the approve transaction
    so a leave approved in the meantime, or a departure set since, still
    blocks).

    `stage="file"` additionally enforces the date window (today or later
    unless `actor` has org-wide edit rights) and the no-op rule; both are
    decisions made when filing and aren't re-litigated at approval.

    Raises `ValidationError`, keyed by form field where one applies.
    """
    from accounts.permissions import can_edit_all

    employee, day = req.employee, req.date
    errors: dict[str, list[str]] = {}

    def add(field, msg):
        errors.setdefault(field, []).append(msg)

    if shift_is_before_joining_date(employee, day):
        add("date", f"{employee.display_name} joined on {employee.joining_date.isoformat()} — "
                    "the date must be on or after that.")
    if shift_is_after_last_working_date(employee, day):
        add("date", f"{employee.display_name}'s last working date is "
                    f"{employee.last_working_date.isoformat()} — the date must be on or before that.")

    if stage == "file" and day < timezone.localdate() and not can_edit_all(actor):
        add("date", "A shift change can only be requested for today or a future date.")

    blocked = leave_blocks_shift_change(employee, day)
    if blocked:
        add("date", blocked)

    dup = ShiftChangeRequest.objects.filter(
        employee=employee, date=day, status=ShiftChangeRequest.Status.PENDING,
    )
    if req.pk:
        dup = dup.exclude(pk=req.pk)
    if dup.exists():
        add("date", f"A shift change request for {day.isoformat()} is already pending.")

    if req.requested_shift_type_id is None:
        add("shift_ref", "Pick a shift.")
    elif req.requested_shift_type.category not in REQUESTABLE_SHIFT_CATEGORIES:
        add("shift_ref", "Only working shifts and weekly-off days can be requested here — "
                         "leave goes through the leave application form.")

    if req.requested_location not in LOCATION_DROPDOWN_VALUES:
        add("requested_location", "Pick a location.")

    if stage == "file" and not errors:
        current = resolve_current_cell(employee, day)
        same_shift = (
            current.shift_type_id == req.requested_shift_type_id
            and current.shift_attribute_id == req.requested_shift_attribute_id
        )
        if same_shift and current.location == req.requested_location:
            add("__all__", "That's already the shift and location on the roster for this "
                           "date — change at least one of them.")

    if errors:
        raise ValidationError(errors)


@transaction.atomic
def apply_shift_change(req: ShiftChangeRequest, approver) -> Shift:
    """Write an approved request onto the roster: the same `update_or_create`
    + `defaults` set `roster.views.CellUpdateView` writes, so an approval and
    a grid edit produce identical `Shift` rows — and identical
    `ShiftChangeLog` entries (signal-driven, `changed_by` = approver), and
    the same leave-ledger / CpL side effects. Never `bulk_create` here.

    Re-runs the approval-stage validation first; a request that no longer
    fits (leave approved since, departure set since, attribute retired or
    re-scoped) raises `ValidationError` and writes nothing.
    """
    validate_shift_change(req, actor=approver, stage="approve")
    # The attribute must still be a valid pick for this employee's department.
    resolve_shift_ref(
        shift_ref_for(req.requested_shift_type, req.requested_shift_attribute),
        department_id=req.employee.department_id,
    )
    jl, wl = resolve_location_choice(req.requested_location)
    shift, _ = Shift.objects.update_or_create(
        employee=req.employee, date=req.date,
        defaults={
            "shift_type": req.requested_shift_type,
            "shift_attribute": req.requested_shift_attribute,
            "job_location": jl,
            "work_location": wl,
            "updated_by": approver,
            "note": f"Shift change #{req.pk}",
        },
    )
    Shift.objects.filter(
        employee=req.employee, date=req.date, added_by__isnull=True,
    ).update(added_by=approver)
    return shift
