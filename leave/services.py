"""Leave services — sandwich algorithm, balance ledger, materialisation.

Implementation reference: docs/LEAVE_POLICY_IMPLEMENTATION.md

Public surface:

- compute_application_billable_days(app) → (working, sandwich, total) Decimals.
- recompute_sandwich(employee, around_date) → idempotent reconcile of all
  approved leaves in a ±14 day window. Adjusts `effective_days`, materialises
  Shifts, writes ledger transactions.
- materialise_shifts_for_application(app, approver) — sandwich-aware writes.
- reverse_shifts_for_application(app) — undo this app's Shift writes.
- consume_balance / restore_balance — first-time approval / cancel.
- validate_application_against_balance(app) — pre-approval guard.
- describe_balance_impact(app) — the same arithmetic as data rather than
  an exception, so the balance drawer can show a manager the shortfall
  before they click Approve. The guard above is a wrapper over it.
- prorate_entitlement(policy, joining_date, year) — for FIXED_ANNUAL policies.
- get_or_create_balance(employee, policy, as_of) — the balance row covering
  `as_of`; ANNIVERSARY-cycle policies (AL) key off the employee's own
  joining-date cycle (anniversary_cycle_window), CALENDAR-cycle policies
  key off as_of.year.
- reconcile_single_shift(shift) / reconcile_orphan_shift_usage(...) — debit
  LeaveBalance for LEAVE-category Shift rows that never went through a
  LeaveApplication (most real leave — see docs/LEAVE_POLICY_IMPLEMENTATION.md).
  Idempotent via LeaveBalanceTransaction.shift. reconcile_single_shift is
  called synchronously from leave/signals.py on every Shift save;
  reconcile_orphan_shift_usage is the nightly catch-up batch for anything
  written via bulk_create (bypasses signals).
- reverse_shift_ledger_transaction(txn) — shared reversal helper for a
  shift-linked USED debit; also used by _absorb_shift_debits, which folds a
  pre-existing roster-direct debit into a LeaveApplication's own debit on
  approval so a day a supervisor already marked (e.g. a called-in-sick
  roster edit) isn't charged twice when the employee later files the
  LeaveApplication for the record.
- reconcile_stale_shift_leave_reversals(...) — the mirror image: reverses a
  USED debit whose Shift has since been bulk_create-retyped away from that
  leave policy (bulk_create bypasses the signal that reverses this
  synchronously). Same nightly command as reconcile_orphan_shift_usage.
- live_accrued_al(employee, policy, bal) — AL's open-cycle accrued computed
  live from worked-day counts (WORK-category shifts, plus any shift under a
  paid LeavePolicy — CL/SL/CPL/MTL/AL itself — so taking paid leave doesn't
  cost AL accrual), clamped to policy.max_days_per_year when set (12 for
  AL). Closed cycles and entitlement_frozen balances use the persisted
  snapshot.
- live_used(employee, policy, bal) — bal.used topped up with any orphan
  Shift usage not yet reconciled into the ledger (closes the bulk_create
  lag; already-reconciled usage, including half-days, is untouched).
- live_available(...) — opening + live_accrued_al + carried_forward -
  live_used - encashed. Use for display/validation; closed cycles fall
  back to the frozen stored values throughout.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F, Q
from django.db.models.signals import post_save

from leave.models import (
    CplGrant,
    LeaveApplication,
    LeaveBalance,
    LeaveBalanceTransaction,
    LeavePolicy,
)
from roster.models import Holiday, Roster, Shift, ShiftType


# How many days on either side of an application we look for adjacent leaves
# when checking sandwich. SEBPO leave applications rarely span more than two
# weeks; this gives plenty of headroom.
WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Working-day determination
# ---------------------------------------------------------------------------

# Active holidays are a tiny reference set read repeatedly by every billing/
# materialisation path (often several times per request). Cached whole; the
# Holiday signal handlers in leave/signals.py invalidate before recomputing.
# TTL from LEAVE_HOLIDAY_CACHE_SECONDS (0 = bypass, used by settings_test).
_HOLIDAY_CACHE_KEY = "leave:active_holiday_dates"


def _all_active_holiday_dates() -> tuple[date, ...]:
    from django.conf import settings as dj_settings

    ttl = dj_settings.LEAVE_HOLIDAY_CACHE_SECONDS
    dates = cache.get(_HOLIDAY_CACHE_KEY) if ttl > 0 else None
    if dates is None:
        dates = tuple(
            Holiday.objects
            .filter(is_active=True)
            .order_by("date")
            .values_list("date", flat=True)
        )
        if ttl > 0:
            cache.set(_HOLIDAY_CACHE_KEY, dates, ttl)
    return dates


def invalidate_holiday_cache() -> None:
    cache.delete(_HOLIDAY_CACHE_KEY)


def _holiday_dates_in_range(start: date, end: date) -> set[date]:
    return {d for d in _all_active_holiday_dates() if start <= d <= end}


def _pattern_category_by_weekday(employee) -> dict[int, str]:
    """Map weekday (0=Mon..6=Sun) → ShiftType.Category for this employee."""
    return {
        r.weekday: r.shift_type.category
        for r in (
            Roster.objects
            .filter(employee=employee)
            .select_related("shift_type")
        )
    }


def is_working_day(d: date, pattern: dict[int, str], holidays: set[date]) -> bool:
    """A working day is one whose pattern is WORK and which isn't a Holiday."""
    if d in holidays:
        return False
    return pattern.get(d.weekday()) == ShiftType.Category.WORK


# ---------------------------------------------------------------------------
# Sandwich algorithm
# ---------------------------------------------------------------------------

def _enumerate_app_days(app: LeaveApplication) -> list[date]:
    return [app.start_date + timedelta(days=i) for i in range(app.day_count)]


def _approved_leave_days(employee, window_start: date, window_end: date,
                        exclude_pk: int | None = None) -> dict[date, int]:
    """Map of date -> leave_type_id for every date covered by the employee's
    APPROVED LeaveApplications overlapping [window_start, window_end],
    excluding the optional pk. The leave type travels with each date because
    the sandwich rule only bridges a gap when the anchors on both sides are
    the SAME leave type (see `_compute_billing_core`)."""
    qs = LeaveApplication.objects.filter(
        employee=employee,
        status=LeaveApplication.Status.APPROVED,
        end_date__gte=window_start,
        start_date__lte=window_end,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    days: dict[date, int] = {}
    for app in qs:
        for d in _enumerate_app_days(app):
            days[d] = app.leave_type_id
    return days


def _find_anchor_before(d: date, anchors: dict[date, int], lower: date, is_working) -> date | None:
    """Nearest leave anchor before `d`, but only across a contiguous run of
    non-working days. A worked (non-leave) day between `d` and the anchor
    breaks the sandwich — the weekend isn't bracketed by leave then."""
    for k in range(1, WINDOW_DAYS + 1):
        candidate = d - timedelta(days=k)
        if candidate < lower:
            break
        if candidate in anchors:
            return candidate
        if is_working(candidate):
            break  # a real worked day severs the bridge
    return None


def _find_anchor_after(d: date, anchors: dict[date, int], upper: date, is_working) -> date | None:
    """Nearest leave anchor after `d`, but only across a contiguous run of
    non-working days (see `_find_anchor_before`)."""
    for k in range(1, WINDOW_DAYS + 1):
        candidate = d + timedelta(days=k)
        if candidate > upper:
            break
        if candidate in anchors:
            return candidate
        if is_working(candidate):
            break  # a real worked day severs the bridge
    return None


def _compute_billing_core(application: LeaveApplication):
    """Shared core for `compute_application_billable_days` and
    `_billable_dates_for_application`. Returns:

      (working_days, sandwich_days, billable_dates)

    Billing rules:
    - Working leave days within [start, end] count as 1 (or 0.5 if half-day).
    - Non-working days within [start, end] count as 1 if sandwiched between
      working leave-day anchors of the SAME leave type (this app + adjacent
      APPROVED apps) — a CL anchor before and an SL anchor after does NOT
      sandwich the gap between them, even though each side individually is
      a leave anchor.
    - Non-working days OUTSIDE [start, end] but within ±WINDOW_DAYS are
      billed to *this* app only when the next anchor after them belongs
      to this app — i.e., the trailing app picks up cross-app bridges.
      This makes a Fri-only A + Mon-only B chain bill 1 to A and 3 to B
      (Mon + Sat + Sun) on B's approval, with no double-counting — but
      only when A and B are the same leave type.

    Anchors are WORKING leave days only. Non-working days never act as
    sandwich anchors — they only get absorbed.
    """
    employee = application.employee
    start = application.start_date
    end = application.end_date
    window_start = start - timedelta(days=WINDOW_DAYS)
    window_end = end + timedelta(days=WINDOW_DAYS)

    holidays = _holiday_dates_in_range(window_start, window_end)
    pattern = _pattern_category_by_weekday(employee)

    other_leave_days = _approved_leave_days(
        employee, window_start, window_end, exclude_pk=application.pk,
    )
    this_app_days = set(_enumerate_app_days(application))

    def _working(d):
        return is_working_day(d, pattern, holidays)

    # Working leave days are anchors for sandwich detection, tagged with
    # their leave type — see the type-matching rule in the docstring above.
    this_app_anchors = {d: application.leave_type_id for d in this_app_days if _working(d)}
    other_anchors = {d: t for d, t in other_leave_days.items() if _working(d)}
    all_anchors = {**other_anchors, **this_app_anchors}

    working = Decimal("0")
    sandwich = Decimal("0")
    billable: set[date] = set()

    # 1) In-range days.
    for d in _enumerate_app_days(application):
        if _working(d):
            inc = Decimal("0.5") if application.is_half_day else Decimal("1")
            working += inc
            billable.add(d)
        else:
            prev = _find_anchor_before(d, all_anchors, window_start, _working)
            nxt = _find_anchor_after(d, all_anchors, window_end, _working)
            if (
                prev is not None
                and nxt is not None
                and all_anchors[prev] == all_anchors[nxt]
            ):
                sandwich += Decimal("1")
                billable.add(d)

    # 2) Out-of-range bridge days — only billed to *this* app when the
    # NEXT anchor is one of this app's working days. Symmetric pre-range
    # bridges attach to this app; post-range bridges attach to the next
    # app whose anchors come after.
    cursor = window_start
    while cursor <= window_end:
        if cursor < start or cursor > end:
            if not _working(cursor):
                prev = _find_anchor_before(cursor, all_anchors, window_start, _working)
                nxt = _find_anchor_after(cursor, all_anchors, window_end, _working)
                if (
                    prev is not None
                    and nxt is not None
                    and all_anchors[prev] == all_anchors[nxt]
                    and nxt in this_app_anchors
                ):
                    sandwich += Decimal("1")
                    billable.add(cursor)
        cursor = cursor + timedelta(days=1)

    return working, sandwich, billable


def compute_application_billable_days(application: LeaveApplication):
    """Apply the SEBPO sandwich rule.

    Returns (working_days, sandwich_days, total) — Decimals.
    """
    working, sandwich, _ = _compute_billing_core(application)
    return working, sandwich, working + sandwich


def _billable_dates_for_application(application: LeaveApplication) -> set[date]:
    _, _, billable = _compute_billing_core(application)
    return billable


# ---------------------------------------------------------------------------
# Shift materialisation
# ---------------------------------------------------------------------------

def _tag(application: LeaveApplication) -> str:
    """Fingerprint note used to identify Shift rows this leave wrote."""
    return f"Leave #{application.pk}"


def materialise_shifts_for_application(application: LeaveApplication, approver) -> int:
    """Sandwich-aware: writes LEAVE Shifts on every billable date (both
    in-range and out-of-range bridges) and removes any previously-tagged
    Shifts that are no longer billable.

    Idempotent. Returns the number of dates that ended up with a LEAVE Shift
    written for this app.

    Dates past the employee's `last_working_date` are dropped (see
    roster.services.purge_shifts_after_last_working_date): once someone has
    left, no Shift may exist beyond their LWD, and an approved leave whose
    range runs past a later-set LWD must not keep re-materialising them. The
    LeaveApplication row itself is left alone — trimming or cancelling it is
    a manual HR step, same as the separation payout.
    """
    tag = _tag(application)
    billable = _billable_dates_for_application(application)
    lwd = getattr(application.employee, "last_working_date", None)
    if lwd is not None:
        billable = {d for d in billable if d <= lwd}

    for d in billable:
        Shift.objects.update_or_create(
            employee=application.employee, date=d,
            defaults={
                "shift_type": application.leave_type,
                "shift_attribute": None,
                "updated_by": approver,
                "note": tag,
                "leave_application": application,
            },
        )
        Shift.objects.filter(
            employee=application.employee, date=d, added_by__isnull=True,
        ).update(added_by=approver)

    # Any linked Shift NOT in the new billable set is stale (e.g., a bridge
    # day that was vacated when an adjacent app cancelled).
    Shift.objects.filter(
        employee=application.employee,
        leave_application=application,
    ).exclude(date__in=billable).delete()

    return len(billable)


def reverse_shifts_for_application(application: LeaveApplication) -> int:
    """Delete every Shift linked to this application — not constrained by
    [start, end] because cross-app bridge days may extend outside that range.
    The `leave_application` FK is the source of truth (the note tag is only
    a human-readable label)."""
    qs = Shift.objects.filter(
        employee=application.employee,
        leave_application=application,
    )
    count, _ = qs.delete()
    return count


# Back-compat aliases for the older names.
materialise_shifts_for_leave = materialise_shifts_for_application
reverse_shifts_for_leave = reverse_shifts_for_application


# ---------------------------------------------------------------------------
# Balance ledger
# ---------------------------------------------------------------------------

def _safe_anniversary(joining_date: date, year: int) -> date:
    """`joining_date` with its year replaced by `year`. A Feb-29 anchor falls
    back to Feb 28 in a non-leap target year."""
    try:
        return joining_date.replace(year=year)
    except ValueError:
        return joining_date.replace(year=year, day=28)


def anniversary_cycle_window(joining_date: date, as_of: date) -> tuple[date, date]:
    """[cycle_start, cycle_end) containing `as_of`, anchored on
    `joining_date`'s month/day. The first cycle is the one starting at the
    1-year anniversary — `LeavePolicy.min_service_months` already gates
    eligibility before this is ever called for AL, so pre-eligibility years
    don't need special-casing here.
    """
    n = as_of.year - joining_date.year
    start = _safe_anniversary(joining_date, joining_date.year + n)
    if start > as_of:
        n -= 1
        start = _safe_anniversary(joining_date, joining_date.year + n)
    end = _safe_anniversary(joining_date, joining_date.year + n + 1)
    return start, end


def get_or_create_balance(employee, leave_policy: LeavePolicy, as_of: date) -> LeaveBalance:
    """Fetch (or create) the balance row covering `as_of`.

    - CALENDAR-cycle policies (CL, SL, MtL, ...): keyed by as_of.year, as
      always — cycle_start/cycle_end are also populated ([Jan 1, Jan 1 next
      year)) so cycle-close code can treat both kinds of policy uniformly.
    - ANNIVERSARY-cycle policies (AL): keyed by the employee's own cycle
      window containing `as_of` (see anniversary_cycle_window) — the row's
      `year` is the cycle's start year.

    On creation:
    - FIXED_ANNUAL policies (CL, SL, MtL) — initialize `opening` from the
      employee's joining_date (pro-rated entitlement).
    - WORKED_DAY_RATIO policies (AL) — opening starts at 0; the monthly
      accrual job credits `accrued`.
    - CpL — opening starts at 0; the holiday-work signal credits `accrued`.

    Existing rows are returned untouched.

    For non-balance-tracking policies (FL, LWP, HdL) this still works
    (creates a zero row), but callers usually filter by
    `leave_policy.tracks_balance` first.
    """
    if leave_policy.cycle == LeavePolicy.Cycle.ANNIVERSARY:
        joining = getattr(employee, "joining_date", None)
        if not joining:
            raise ValidationError(
                f"{employee} has no joining_date — cannot compute an "
                f"anniversary cycle for {leave_policy.shift_type.name}."
            )
        cycle_start, cycle_end = anniversary_cycle_window(joining, as_of)
        bal, created = LeaveBalance.objects.get_or_create(
            employee=employee, leave_policy=leave_policy, year=cycle_start.year,
            defaults={"cycle_start": cycle_start, "cycle_end": cycle_end},
        )
        # `year` alone collides with a pre-anniversary-rework legacy row
        # whenever an employee's current cycle happens to start in the same
        # calendar year one of those old rows was keyed to — get_or_create
        # above then silently returns that stale row instead of creating a
        # fresh one, leaving cycle_start/cycle_end NULL forever (found
        # 2026-09-02 auditing Imran Ahmed's allocation: ~55% of AL balance
        # rows company-wide had this). Heal it in place, but only when this
        # really is the employee's *live* open cycle right now (cycle_end
        # still in the future) — a report requesting a genuinely past year
        # must not resurrect an old, never-closed cycle as if it were open.
        # Numeric fields (opening/accrued/used/carried_forward) are left
        # untouched; live_accrued_al/live_used take over correctly the
        # moment cycle_start is non-null. See docs/LEAVE_POLICY_IMPLEMENTATION.md §5.7.
        if not created and bal.cycle_start is None and cycle_end > date.today():
            bal.cycle_start = cycle_start
            bal.cycle_end = cycle_end
            bal.save(update_fields=["cycle_start", "cycle_end", "updated_at"])
    else:
        year = as_of.year
        bal, created = LeaveBalance.objects.get_or_create(
            employee=employee, leave_policy=leave_policy, year=year,
            defaults={
                "cycle_start": date(year, 1, 1),
                "cycle_end": date(year + 1, 1, 1),
            },
        )
    if created and leave_policy.accrual_method == LeavePolicy.AccrualMethod.FIXED_ANNUAL:
        joining = getattr(employee, "joining_date", None)
        bal.opening = prorate_entitlement(leave_policy, joining, bal.year)
        bal.save(update_fields=["opening", "updated_at"])
    return bal


def _existing_application_delta(application: LeaveApplication) -> Decimal:
    """Sum of all balance-transaction deltas for this application.
    Negative when the app currently consumes balance; zero if untouched."""
    return sum(
        (txn.delta for txn in LeaveBalanceTransaction.objects.filter(application=application)),
        Decimal("0"),
    )


def consume_balance(application: LeaveApplication, created_by) -> LeaveBalanceTransaction | None:
    """First-time debit on approval. No-op if the application's balance
    impact is already correct (e.g., a re-call after recompute_sandwich
    handled it). Skipped entirely for unpaid policies (LWP, HdL).
    """
    policy = application.leave_type.leave_policy
    if not policy.is_paid:
        return None
    if application.effective_days <= 0:
        return None

    expected = -application.effective_days  # negative debit
    current = _existing_application_delta(application)
    if current == expected:
        return None

    # Apply the missing portion.
    delta = expected - current
    bal = get_or_create_balance(application.employee, policy, application.start_date)
    txn = LeaveBalanceTransaction.objects.create(
        balance=bal,
        delta=delta,
        reason=LeaveBalanceTransaction.Reason.USED,
        application=application,
        created_by=created_by,
    )
    bal.used = F("used") - delta  # delta is negative; used grows by |delta|
    bal.save(update_fields=["used", "updated_at"])
    return txn


def restore_balance(application: LeaveApplication, created_by) -> None:
    """Reverse all balance impact for this application. Idempotent — if the
    net is already zero, this is a no-op.
    """
    current = _existing_application_delta(application)
    if current == 0:
        return

    policy = application.leave_type.leave_policy
    bal = get_or_create_balance(application.employee, policy, application.start_date)
    delta = -current  # positive: refund
    LeaveBalanceTransaction.objects.create(
        balance=bal,
        delta=delta,
        reason=LeaveBalanceTransaction.Reason.REVERSED,
        application=application,
        created_by=created_by,
        note="Cancelled / declined",
    )
    bal.used = F("used") - delta
    bal.save(update_fields=["used", "updated_at"])


CPL_GRANT_NOTE_FMT = "CpL grant: shift #{shift_pk}"


def _cpl_policy() -> LeavePolicy | None:
    return (
        LeavePolicy.objects
        .filter(cpl_grant_on_holiday_work=True)
        .select_related("shift_type")
        .first()
    )


def grant_or_backfill_cpl_grant(shift, holiday, cpl_policy, *, created_by=None) -> CplGrant | None:
    """Create (or backfill) the `CplGrant` history row for one WORK-on-Holiday
    `Shift`, for an employee whose department/team is AUTO-treatment (see
    `accounts.permissions.effective_cpl_treatment`) — callers are expected to
    have already checked that gate; this function doesn't re-check it, so it
    can also be used to backfill MANUAL/UPLOAD-sourced history in the future
    without fighting its own gate.

    Idempotent two ways, checked under a row lock to survive concurrent
    saves of the same Shift:
    - A `CplGrant` already linked to this `work_shift` — no-op.
    - A pre-existing ACCRUAL `LeaveBalanceTransaction` (from before this
      history table existed, or an earlier run of this function) —
      backfill a `CplGrant` pointing at it instead of granting again.
    Otherwise, credit the balance (same arithmetic the old note-only
    signal used) and create both rows together.
    """
    if CplGrant.objects.filter(work_shift=shift).exists():
        return None

    note = CPL_GRANT_NOTE_FMT.format(shift_pk=shift.pk)

    with transaction.atomic():
        bal, _ = LeaveBalance.objects.get_or_create(
            employee=shift.employee, leave_policy=cpl_policy, year=shift.date.year,
        )
        bal = LeaveBalance.objects.select_for_update().get(pk=bal.pk)

        if CplGrant.objects.filter(work_shift=shift).exists():
            return None  # lost the race to a concurrent save

        existing_txn = LeaveBalanceTransaction.objects.filter(
            balance=bal, reason=LeaveBalanceTransaction.Reason.ACCRUAL, note=note,
        ).first()
        if existing_txn is not None:
            txn = existing_txn
            if txn.shift_id is None:
                LeaveBalanceTransaction.objects.filter(pk=txn.pk).update(shift=shift)
        else:
            txn = LeaveBalanceTransaction.objects.create(
                balance=bal,
                delta=Decimal("1"),
                reason=LeaveBalanceTransaction.Reason.ACCRUAL,
                shift=shift,
                created_by=created_by,
                note=note,
            )
            bal.accrued = F("accrued") + Decimal("1")
            bal.save(update_fields=["accrued", "updated_at"])

        return CplGrant.objects.create(
            employee=shift.employee,
            department=shift.employee.department,
            work_date=shift.date,
            work_context=CplGrant.WorkContext.HOLIDAY,
            holiday=holiday,
            work_shift=shift,
            source=CplGrant.Source.AUTO,
            status=CplGrant.Status.OPEN,
            accrual_transaction=txn,
            note="Backfilled from a pre-existing ledger grant" if existing_txn is not None else "",
        )


def credit_manual_cpl_grant(grant: CplGrant, *, created_by=None) -> CplGrant:
    """Credit the ledger for a `CplGrant` that was created directly (Django
    admin, or `leave/views.py::CplGrantManualCreateView`) without going
    through `grant_or_backfill_cpl_grant`'s Shift-driven path — a manual
    entry has no Shift to key a signal off of, so nothing else does this.

    Idempotent: a no-op if `grant.accrual_transaction` is already set (so
    it's safe to call from `save_model` on every admin save, not just the
    first one). Mirrors the arithmetic `grant_or_backfill_cpl_grant` uses.
    """
    if grant.accrual_transaction_id:
        return grant

    cpl_policy = _cpl_policy()
    if cpl_policy is None:
        return grant

    with transaction.atomic():
        bal = get_or_create_balance(grant.employee, cpl_policy, grant.work_date)
        bal = LeaveBalance.objects.select_for_update().get(pk=bal.pk)
        txn = LeaveBalanceTransaction.objects.create(
            balance=bal,
            delta=Decimal("1"),
            reason=LeaveBalanceTransaction.Reason.ACCRUAL,
            created_by=created_by,
            note=f"Manual CpL grant #{grant.pk}: {grant.get_work_context_display()}",
        )
        bal.accrued = F("accrued") + Decimal("1")
        bal.save(update_fields=["accrued", "updated_at"])

        grant.accrual_transaction = txn
        grant.save(update_fields=["accrual_transaction", "updated_at"])
    return grant


def _pair_oldest_open_cpl_grant(shift, consumption_txn) -> None:
    """Link a CpL-typed Shift's USED debit to the employee's oldest still-
    OPEN CplGrant. Not FIFO-by-consumption-date: some departments (Data
    Solutions & Due Diligence, confirmed against real data) grant CpL in
    advance within the same rostering week, so a grant's `holiday.date` can
    land AFTER its own `consumed_date` — pairing walks grant age (oldest
    un-consumed grant first) rather than assuming the two dates are in
    chronological order relative to each other."""
    grant = (
        CplGrant.objects
        .filter(employee=shift.employee, status=CplGrant.Status.OPEN)
        .order_by("holiday__date", "added_at")
        .first()
    )
    if grant is None:
        return
    grant.consumption_shift = shift
    grant.consumed_date = shift.date
    grant.status = CplGrant.Status.CONSUMED
    grant.consumption_transaction = consumption_txn
    grant.save(update_fields=[
        "consumption_shift", "consumed_date", "status", "consumption_transaction", "updated_at",
    ])


def reconcile_single_shift(shift, *, created_by=None) -> bool:
    """Debit `LeaveBalance` for one LEAVE-category `Shift` that has no
    `LeaveApplication` and no existing shift-linked ledger transaction yet.
    Returns True if a debit was written, False if there was nothing to do
    (not a leave shift, application-backed, unpaid/no-quota policy, or
    already reconciled).

    Idempotent via `LeaveBalanceTransaction.shift`'s unique constraint —
    safe to call from both the nightly batch (`reconcile_orphan_shift_usage`)
    and the synchronous `post_save` signal
    (`leave/signals.py::sync_shift_leave_ledger`), which is what makes leave
    usage show up on the balance page immediately instead of waiting for
    the nightly job.
    """
    if shift.leave_application_id:
        return False
    shift_type = shift.shift_type
    if shift_type is None:
        return False
    policy = getattr(shift_type, "leave_policy", None)
    if policy is None or not policy.is_paid or not policy.tracks_balance:
        return False
    if LeaveBalanceTransaction.objects.filter(shift=shift).exists():
        return False

    with transaction.atomic():
        bal = get_or_create_balance(shift.employee, policy, shift.date)
        txn = LeaveBalanceTransaction.objects.create(
            balance=bal,
            delta=Decimal("-1"),
            reason=LeaveBalanceTransaction.Reason.USED,
            shift=shift,
            created_by=created_by,
            note="Roster-direct leave (reconciled from Shift)",
        )
        bal.used = F("used") + 1
        bal.save(update_fields=["used", "updated_at"])
        if policy.cpl_grant_on_holiday_work:
            _pair_oldest_open_cpl_grant(shift, txn)
    return True


def reverse_shift_ledger_transaction(txn: LeaveBalanceTransaction, *, note: str) -> None:
    """Reverse a Shift-derived USED debit (see reconcile_single_shift /
    reconcile_orphan_shift_usage above). Writes a REVERSED entry
    (append-only ledger, never mutate/delete the original) and frees the
    original transaction's `shift` FK so a re-typed-but-not-deleted Shift
    becomes eligible for reconciliation again under its new leave policy
    (or simply stops matching the reconciliation query if it's no longer a
    LEAVE-category shift at all, or has been claimed by a LeaveApplication —
    see _absorb_shift_debits below).

    Shared by leave/signals.py::sync_shift_leave_ledger (Shift retyped/
    deleted away from a roster-direct leave debit) and
    _absorb_shift_debits (a LeaveApplication claims a day a supervisor
    already roster-debited)."""
    credit = -txn.delta  # txn.delta is negative (a debit); credit is positive
    LeaveBalanceTransaction.objects.create(
        balance=txn.balance,
        delta=credit,
        reason=LeaveBalanceTransaction.Reason.REVERSED,
        shift=None,
        created_by=None,
        note=note,
    )
    txn.balance.used = F("used") - credit
    txn.balance.save(update_fields=["used", "updated_at"])
    LeaveBalanceTransaction.objects.filter(pk=txn.pk).update(shift=None)


def _absorb_shift_debits(application: LeaveApplication, billable: set[date]) -> None:
    """Fold any pre-existing roster-direct (shift-linked, no-application)
    USED debit for this employee/policy on the application's billable
    dates into this application's own debit, so the same leave-day is
    never charged twice: once when a supervisor sets the roster cell
    directly (e.g. an employee calls in sick), and again when that
    employee later files the LeaveApplication for the record.

    Must run before the ledger-gap block below writes the application's
    own debit, and before materialise_shifts_for_application re-saves the
    same Shift row(s) — freeing the old transaction's `shift` FK here is
    what keeps that later re-save's post_save signal a no-op instead of a
    third write."""
    stale = (
        LeaveBalanceTransaction.objects
        .filter(
            reason=LeaveBalanceTransaction.Reason.USED,
            application__isnull=True,
            shift__isnull=False,
            shift__employee=application.employee,
            shift__date__in=billable,
            balance__leave_policy=application.leave_type.leave_policy,
        )
        .select_related("balance")
    )
    for txn in stale:
        reverse_shift_ledger_transaction(
            txn, note=f"Absorbed into Leave #{application.pk} on approval",
        )


def reconcile_orphan_shift_usage(
    *,
    since: date | None = None,
    upto: date | None = None,
    employee=None,
    policy: LeavePolicy | None = None,
    created_by=None,
    dry_run: bool = False,
) -> dict:
    """Debit `LeaveBalance` for LEAVE-category `Shift` rows that were never
    written through a `LeaveApplication` (a supervisor set the roster cell
    directly). This is most of the org's real leave usage — see
    docs/LEAVE_POLICY_IMPLEMENTATION.md, "Risks worth calling out".

    Most such Shifts are now reconciled instantly by
    `leave/signals.py::sync_shift_leave_ledger` the moment they're saved —
    this batch function exists as a nightly catch-up for anything written
    via `bulk_create` (bulk roster uploads bypass signals entirely, on
    purpose, for performance) or from before that signal existed.

    Idempotent: a Shift already reconciled has a `LeaveBalanceTransaction`
    with `shift=<that row>` (enforced unique), so it's excluded from the
    query and can never be double-counted. `dry_run=True` computes the same
    set and tallies deltas per (policy, year) without writing anything —
    use this first; see leave/management/commands/reconcile_shift_leave_usage.py.

    Half-day caveat: there's no per-Shift half-day marker for leave types
    today (only `LeaveApplication.is_half_day` expresses that) — every
    orphan Shift is counted as a full day.
    """
    qs = (
        Shift.objects
        .filter(
            shift_type__leave_policy__isnull=False,
            leave_application__isnull=True,
            leave_ledger_transactions__isnull=True,
        )
        .select_related("employee", "shift_type__leave_policy")
    )
    if since is not None:
        qs = qs.filter(date__gte=since)
    if upto is not None:
        qs = qs.filter(date__lte=upto)
    if employee is not None:
        qs = qs.filter(employee=employee)
    if policy is not None:
        qs = qs.filter(shift_type__leave_policy=policy)

    processed = 0
    skipped_untracked = 0
    by_policy_year: dict[tuple[str, int], Decimal] = {}

    for shift in qs.order_by("date"):
        pol = shift.shift_type.leave_policy
        if not pol.is_paid or not pol.tracks_balance:
            skipped_untracked += 1
            continue

        key = (pol.shift_type.name, shift.date.year)
        by_policy_year[key] = by_policy_year.get(key, Decimal("0")) - 1
        processed += 1
        if dry_run:
            continue

        reconcile_single_shift(shift, created_by=created_by)

    return {
        "processed": processed,
        "skipped_untracked": skipped_untracked,
        "by_policy_year": by_policy_year,
    }


def import_cpl_grant_row(
    employee_id: str, work_date: date, status: str, consumed_date: date | None = None,
    *, created_by=None,
) -> dict:
    """Import one row of a staged CplGrant workbook (Department, Employee
    ID, Working Date, Status [OPEN/CONSUMED], Consumed Date) — the shape
    this session's HR-provided historical files (IT & Cybersecurity's own
    tracker, Data Solutions' entitlement file) were staged into. Every row
    imported this way is `source=UPLOAD`, `work_context=HOLIDAY` — a real
    government holiday, resolved via `roster.Holiday`.

    Idempotent via `(employee, holiday)` — a row already imported is
    skipped, not duplicated. For a CONSUMED row, if a matching CpL-typed
    roster Shift already exists on `consumed_date` *and* already has a
    reconciled USED debit (most of them do — the shift-leave-ledger sync
    already caught these independently of this import), that existing
    debit is linked rather than double-counted; otherwise a new USED debit
    is written directly against `consumed_date` (no Shift to attach it
    to), trusting the uploaded file until/unless a reconciliation review
    corrects it (see docs/LEAVE_POLICY_IMPLEMENTATION.md §5.12).
    """
    from accounts.models import CustomUser

    employee = CustomUser.objects.filter(employee_id=str(employee_id)).select_related("department").first()
    if employee is None:
        return {"ok": False, "skipped": False, "reason": f"No employee with ID {employee_id}"}
    if not employee.department_id:
        return {"ok": False, "skipped": False, "reason": f"{employee} has no department set"}

    holiday = Holiday.objects.filter(date=work_date, is_active=True).first()
    if holiday is None:
        return {"ok": False, "skipped": False, "reason": f"No active Holiday on {work_date}"}

    if CplGrant.objects.filter(employee=employee, holiday=holiday).exists():
        return {"ok": False, "skipped": True, "reason": "already imported"}

    cpl_policy = _cpl_policy()
    if cpl_policy is None:
        return {"ok": False, "skipped": False, "reason": "No CpL LeavePolicy configured"}

    work_shift = Shift.objects.filter(
        employee=employee, date=work_date, shift_type__category="WORK",
    ).first()

    with transaction.atomic():
        bal = get_or_create_balance(employee, cpl_policy, work_date)
        bal = LeaveBalance.objects.select_for_update().get(pk=bal.pk)
        accrual_txn = LeaveBalanceTransaction.objects.create(
            balance=bal,
            delta=Decimal("1"),
            reason=LeaveBalanceTransaction.Reason.ACCRUAL,
            shift=work_shift,
            created_by=created_by,
            note=f"CpL grant (uploaded history): worked {work_date}",
        )
        bal.accrued = F("accrued") + Decimal("1")
        bal.save(update_fields=["accrued", "updated_at"])

        grant = CplGrant.objects.create(
            employee=employee,
            department=employee.department,
            work_date=work_date,
            work_context=CplGrant.WorkContext.HOLIDAY,
            holiday=holiday,
            work_shift=work_shift,
            source=CplGrant.Source.UPLOAD,
            status=CplGrant.Status.OPEN,
            accrual_transaction=accrual_txn,
            added_by=created_by,
            updated_by=created_by,
        )

        if status == CplGrant.Status.CONSUMED and consumed_date is not None:
            consumption_shift = Shift.objects.filter(
                employee=employee, date=consumed_date, shift_type__name="CPL",
            ).first()
            existing_used_txn = None
            if consumption_shift is not None:
                existing_used_txn = LeaveBalanceTransaction.objects.filter(
                    shift=consumption_shift, reason=LeaveBalanceTransaction.Reason.USED,
                ).first()

            if existing_used_txn is not None:
                consumption_txn = existing_used_txn
                note = "Consumption linked to an existing roster-reconciled debit."
            else:
                consumption_txn = LeaveBalanceTransaction.objects.create(
                    balance=bal,
                    delta=Decimal("-1"),
                    reason=LeaveBalanceTransaction.Reason.USED,
                    shift=consumption_shift,
                    created_by=created_by,
                    note=f"CpL consumption (uploaded history): took {consumed_date}",
                )
                bal.used = F("used") + 1
                bal.save(update_fields=["used", "updated_at"])
                note = (
                    "No matching roster CpL shift/debit found on consumed_date — "
                    "recorded from the uploaded file; pending reconciliation."
                )

            grant.status = CplGrant.Status.CONSUMED
            grant.consumed_date = consumed_date
            grant.consumption_shift = consumption_shift
            grant.consumption_transaction = consumption_txn
            grant.note = note
            grant.save(update_fields=[
                "status", "consumed_date", "consumption_shift",
                "consumption_transaction", "note", "updated_at",
            ])

    return {"ok": True, "skipped": False, "grant": grant}


def reconcile_missing_cpl_grants(
    *,
    since: date | None = None,
    upto: date | None = None,
    employee=None,
    dry_run: bool = False,
    created_by=None,
) -> dict:
    """Batch catch-up for CpL grants missing because a WORK-on-Holiday
    `Shift` was written via `bulk_create` (bypasses
    `leave/signals.py::grant_cpl_on_holiday_work`) — the grant-side
    counterpart of `reconcile_orphan_shift_usage`.

    **AUTO-treatment departments/teams only** (see
    `accounts.permissions.effective_cpl_treatment`) — a MANUAL department
    (e.g. Data Solutions & Due Diligence) decides CpL vs. overtime per
    employee/instance outside Orbis and never gets an auto-grant; a NONE
    department (e.g. Digital Advertising) never gets CpL at all. Blindly
    granting every WORK-on-Holiday shift company-wide would be wrong — see
    docs/LEAVE_POLICY_IMPLEMENTATION.md §5.12.

    Idempotent via `CplGrant.work_shift`'s unique constraint (checked
    inside `grant_or_backfill_cpl_grant`, which also backfills a `CplGrant`
    for a pre-existing note-matched ACCRUAL transaction instead of granting
    twice). `dry_run=True` computes the same set and tallies per-date
    counts without writing; see
    `leave/management/commands/reconcile_cpl_grants.py`.
    """
    cpl_policy = _cpl_policy()
    if cpl_policy is None:
        return {"processed": 0, "skipped_treatment": 0, "by_date": {}}

    from accounts.permissions import effective_cpl_treatment

    holidays = list(Holiday.objects.filter(is_active=True))
    holiday_by_date = {h.date: h for h in holidays}
    if since is not None:
        holiday_by_date = {d: h for d, h in holiday_by_date.items() if d >= since}
    if upto is not None:
        holiday_by_date = {d: h for d, h in holiday_by_date.items() if d <= upto}

    already_granted = CplGrant.objects.filter(
        work_shift__isnull=False,
    ).values_list("work_shift_id", flat=True)

    qs = (
        Shift.objects
        .filter(date__in=holiday_by_date.keys(), shift_type__category="WORK")
        .exclude(pk__in=already_granted)
        .select_related("employee__department", "employee__team")
    )
    if employee is not None:
        qs = qs.filter(employee=employee)

    processed = 0
    skipped_treatment = 0
    by_date: dict[date, int] = {}

    for shift in qs.order_by("date"):
        if effective_cpl_treatment(shift.employee) != "AUTO":
            skipped_treatment += 1
            continue

        by_date[shift.date] = by_date.get(shift.date, 0) + 1
        processed += 1
        if dry_run:
            continue

        grant_or_backfill_cpl_grant(
            shift, holiday_by_date[shift.date], cpl_policy, created_by=created_by,
        )

    return {
        "processed": processed,
        "skipped_treatment": skipped_treatment,
        "by_date": by_date,
    }


def reconcile_stale_cpl_grants(
    *,
    since: date | None = None,
    upto: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Reverse an AUTO-sourced, still-OPEN `CplGrant` whose `work_shift` no
    longer qualifies (bulk-retyped away from WORK, or its date is no longer
    an active Holiday) — the mirror of `reconcile_missing_cpl_grants`, for
    rows a bulk roster edit changed without going through
    `leave/signals.py::revoke_cpl_on_shift_delete`.

    Only ever touches `OPEN` grants — a `CONSUMED` grant's consumption is a
    separate, already-real fact and is left alone for manual review rather
    than silently reversed. Writes a reversing ADJUSTMENT transaction
    (append-only ledger, mirrors `leave/signals.py::revoke_cpl_on_shift_delete`)
    and marks the grant `FORFEITED`.
    """
    # HOLIDAY-context only — that's the only kind this validity check (Shift
    # still WORK, date still an active Holiday) knows how to evaluate.
    # WEEKLY_OFF/OTHER grants are always MANUAL today and untouched by any
    # batch job.
    holiday_dates = set(_all_active_holiday_dates())
    qs = (
        CplGrant.objects
        .filter(
            source=CplGrant.Source.AUTO,
            status=CplGrant.Status.OPEN,
            work_context=CplGrant.WorkContext.HOLIDAY,
        )
        .select_related("work_shift__shift_type", "accrual_transaction__balance", "holiday")
    )
    if since is not None:
        qs = qs.filter(work_date__gte=since)
    if upto is not None:
        qs = qs.filter(work_date__lte=upto)

    reversed_count = 0
    by_date: dict[date, int] = {}

    for grant in qs:
        shift = grant.work_shift
        still_valid = (
            shift is not None
            and shift.shift_type is not None
            and shift.shift_type.category == "WORK"
            and shift.date in holiday_dates
        )
        if still_valid:
            continue

        by_date[grant.work_date] = by_date.get(grant.work_date, 0) + 1
        reversed_count += 1
        if dry_run:
            continue

        with transaction.atomic():
            txn = grant.accrual_transaction
            if txn is not None:
                LeaveBalanceTransaction.objects.create(
                    balance=txn.balance,
                    delta=-txn.delta,
                    reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
                    created_by=None,
                    note=f"Reverse CpL grant #{grant.pk} (work shift no longer qualifies)",
                )
                txn.balance.accrued = F("accrued") - txn.delta
                txn.balance.save(update_fields=["accrued", "updated_at"])
            grant.status = CplGrant.Status.FORFEITED
            grant.note = (
                (grant.note + " " if grant.note else "")
                + "Forfeited: work shift no longer WORK-on-Holiday."
            )
            grant.save(update_fields=["status", "note", "updated_at"])

    return {"reversed": reversed_count, "by_date": by_date}


def _leave_policy_for_shift_type_name(name: str) -> LeavePolicy:
    return LeavePolicy.objects.select_related("shift_type").get(shift_type__name=name)


def _revert_cpl_grant_consumption(grant: CplGrant, bal: LeaveBalance) -> str | None:
    """Undo a CplGrant's existing CPL consumption in place: reverses its
    `consumption_transaction` with an append-only ADJUSTMENT credit, frees
    that transaction's `shift` FK (so a subsequent retype of that Shift is
    picked up fresh by `sync_shift_leave_ledger` instead of being ignored as
    "already reconciled"), and resets the grant back to OPEN so it stays
    available for a future real CpL day off. No-op (returns None) if the
    grant isn't currently CONSUMED — safe to call on every run."""
    if grant.status != CplGrant.Status.CONSUMED:
        return None
    txn = grant.consumption_transaction
    if txn is not None:
        LeaveBalanceTransaction.objects.create(
            balance=bal, delta=Decimal("1"),
            reason=LeaveBalanceTransaction.Reason.ADJUSTMENT,
            created_by=None,
            note=f"Reverse CpL consumption for grant #{grant.pk} (ITCS reconciliation: recorded as CL instead)",
        )
        bal.used = F("used") - 1
        bal.save(update_fields=["used", "updated_at"])
        if txn.shift_id is not None:
            LeaveBalanceTransaction.objects.filter(pk=txn.pk).update(shift=None)
    grant.status = CplGrant.Status.OPEN
    grant.consumption_shift = None
    grant.consumed_date = None
    grant.consumption_transaction = None
    grant.save(update_fields=[
        "status", "consumption_shift", "consumed_date", "consumption_transaction", "updated_at",
    ])
    return f"grant #{grant.pk}: reverted CPL consumption -> OPEN"


def _get_or_retype_shift(employee, day: date, shift_type: ShiftType, created_by) -> tuple[Shift, str]:
    """update_or_create-shaped helper used by apply_cpl_reconciliation_row:
    creates the Shift if missing, retypes it in place if present with a
    different shift_type, or no-ops (no `.save()` call at all) if it's
    already correct — the no-op case is what makes a re-run idempotent.

    Clears `shift_attribute` on a retype: `Shift.save()` re-derives
    `shift_type` from `shift_attribute` whenever one is set (roster/models.py
    — the attribute pins the parent type), which would otherwise silently
    discard this retype the moment it hits `.save()`. CL/CPL never carry a
    time-variant attribute, so dropping it here is correct, not a
    side-effect to route around."""
    shift, created = Shift.objects.get_or_create(
        employee=employee, date=day,
        defaults={"shift_type": shift_type, "added_by": created_by, "updated_by": created_by},
    )
    if created:
        return shift, f"Shift#{shift.pk} ({day}): created as {shift_type.name}"
    if shift.shift_type_id != shift_type.pk:
        shift.shift_type = shift_type
        shift.shift_attribute = None
        shift.updated_by = created_by
        shift.save()
        return shift, f"Shift#{shift.pk} ({day}): retyped -> {shift_type.name}"
    return shift, ""


def apply_cpl_reconciliation_row(
    *,
    employee,
    action: str,
    grant_work_date: date,
    consumed_date: date,
    worked_date: date | None = None,
    work_shift_type_name: str | None = None,
    source_grant_work_date: date | None = None,
    created_by=None,
) -> dict:
    """Apply one manager-confirmed row of a CpL/ITCS reconciliation file —
    correcting both the roster Shift cells and the `leave.CplGrant`/ledger
    entries a prior historical bulk import (`leave/upload_cpl.py`) recorded
    provisionally, for a department already on the AUTO CpL-treatment path
    (see `accounts.permissions.effective_cpl_treatment`).

    `action`:
      - "CPL_LINK": the target grant (keyed by `grant_work_date` — normally
        the row's own worked date) already carries the right `consumed_date`
        from the original import but has no real Shift behind it (a floating
        ledger entry). Ensures a WORK Shift on `worked_date` (creating one
        typed `work_shift_type_name` if none exists — pass `worked_date=None`
        to skip when it's already correct) and links it as `work_shift`;
        ensures a CPL Shift on `consumed_date` and links it as
        `consumption_shift`, backfilling the grant's existing floating
        `consumption_transaction` rather than writing a second debit.
      - "CPL_REDIRECT": the row's own worked-date grant (keyed by
        `source_grant_work_date`, required for this action) was wrongly
        credited with this consumption by the original import — a "CPL
        adjusted with <date>" manager note means it really belongs to a
        *different*, already-OPEN grant (`grant_work_date`). First reverts
        `source_grant_work_date`'s grant back to OPEN
        (`_revert_cpl_grant_consumption` — safe even when its prior
        consumption was a real, already-correctly-typed Shift: that Shift is
        untouched, only the grant/ledger attribution moves), then applies
        the same consumption-side fix as CPL_LINK against the target grant.
        The worked-date side of the row's *own* grant is never touched by
        this action.
      - "CL_REVERT": the grant is wrongly marked CONSUMED against a manager
        decision that this day should just count as plain CL. Reverses the
        grant's existing CPL consumption (`_revert_cpl_grant_consumption`)
        back to OPEN, then retypes the Shift on `consumed_date` to CL — a
        plain `.save()` lets `leave/signals.py::sync_shift_leave_ledger`
        write the fresh CL debit itself, since the reversal above already
        freed the old transaction's `shift` FK.

    Both `Shift` post_save receivers that would otherwise mis-fire during a
    CPL_LINK/CPL_REDIRECT consumption save — `grant_cpl_on_holiday_work`
    (would spawn a second, duplicate grant for a floating-import grant with
    no `work_shift` yet) and `sync_shift_leave_ledger` (would write a second
    debit and auto-pair it to the wrong OPEN grant via oldest-holiday-date
    FIFO) — are disconnected for the duration of that one save; the
    correction is instead applied explicitly and deterministically here,
    mirroring how `import_cpl_grant_row` already writes this ledger by hand.

    Idempotent: every write is preceded by a check that the target state
    doesn't already hold, so re-running against an already-corrected row
    (e.g. replaying the same file in production after validating in dev) is
    a safe no-op.
    """
    from leave.signals import grant_cpl_on_holiday_work, sync_shift_leave_ledger

    holiday = Holiday.objects.filter(date=grant_work_date, is_active=True).first()
    if holiday is None:
        return {"ok": False, "reason": f"No active Holiday on {grant_work_date}"}

    cpl_policy = _cpl_policy()
    if cpl_policy is None:
        return {"ok": False, "reason": "No CpL LeavePolicy configured"}

    changes: list[str] = []

    with transaction.atomic():
        grant = (
            CplGrant.objects
            .select_for_update()
            .filter(employee=employee, holiday=holiday)
            .select_related("consumption_transaction")
            .first()
        )
        if grant is None:
            return {"ok": False, "reason": f"No CplGrant for {employee} on holiday {grant_work_date}"}

        bal = LeaveBalance.objects.select_for_update().get(
            pk=get_or_create_balance(employee, cpl_policy, grant_work_date).pk
        )

        if action == "CL_REVERT":
            note = _revert_cpl_grant_consumption(grant, bal)
            if note:
                changes.append(note)

            cl_shift_type = _leave_policy_for_shift_type_name("CL").shift_type
            _, note = _get_or_retype_shift(employee, consumed_date, cl_shift_type, created_by)
            if note:
                changes.append(note)

        elif action in ("CPL_LINK", "CPL_REDIRECT"):
            if action == "CPL_REDIRECT":
                if source_grant_work_date is None:
                    return {"ok": False, "reason": "CPL_REDIRECT requires source_grant_work_date"}
                source_holiday = Holiday.objects.filter(date=source_grant_work_date, is_active=True).first()
                if source_holiday is None:
                    return {"ok": False, "reason": f"No active Holiday on {source_grant_work_date}"}
                source_grant = (
                    CplGrant.objects
                    .select_for_update()
                    .filter(employee=employee, holiday=source_holiday)
                    .select_related("consumption_transaction")
                    .first()
                )
                if source_grant is None:
                    return {"ok": False, "reason": f"No source CplGrant for {employee} on holiday {source_grant_work_date}"}
                note = _revert_cpl_grant_consumption(source_grant, bal)
                if note:
                    changes.append(note)

            if action == "CPL_LINK" and worked_date is not None and work_shift_type_name:
                work_shift_type = ShiftType.objects.get(name=work_shift_type_name)
                post_save.disconnect(grant_cpl_on_holiday_work, sender=Shift)
                try:
                    work_shift, note = _get_or_retype_shift(employee, worked_date, work_shift_type, created_by)
                finally:
                    post_save.connect(grant_cpl_on_holiday_work, sender=Shift)
                if note:
                    changes.append(note)
                if grant.work_shift_id != work_shift.pk:
                    grant.work_shift = work_shift
                    grant.save(update_fields=["work_shift", "updated_at"])
                    changes.append(f"grant #{grant.pk}: work_shift -> Shift#{work_shift.pk}")

            cpl_shift_type = _leave_policy_for_shift_type_name("CPL").shift_type
            post_save.disconnect(grant_cpl_on_holiday_work, sender=Shift)
            post_save.disconnect(sync_shift_leave_ledger, sender=Shift)
            try:
                consumption_shift, note = _get_or_retype_shift(employee, consumed_date, cpl_shift_type, created_by)
            finally:
                post_save.connect(grant_cpl_on_holiday_work, sender=Shift)
                post_save.connect(sync_shift_leave_ledger, sender=Shift)
            if note:
                changes.append(note)

            update_fields = []

            if grant.consumption_transaction_id is None:
                txn = LeaveBalanceTransaction.objects.create(
                    balance=bal, delta=Decimal("-1"),
                    reason=LeaveBalanceTransaction.Reason.USED,
                    shift=consumption_shift, created_by=created_by,
                    note=f"CpL consumption (ITCS reconciliation): took {consumed_date}",
                )
                bal.used = F("used") + 1
                bal.save(update_fields=["used", "updated_at"])
                grant.consumption_transaction = txn
                update_fields.append("consumption_transaction")
                changes.append(f"grant #{grant.pk}: wrote new USED transaction #{txn.pk}")
            elif grant.consumption_transaction.shift_id != consumption_shift.pk:
                LeaveBalanceTransaction.objects.filter(pk=grant.consumption_transaction_id).update(
                    shift=consumption_shift,
                )
                changes.append(f"grant #{grant.pk}: linked existing transaction -> Shift#{consumption_shift.pk}")

            if grant.status != CplGrant.Status.CONSUMED:
                grant.status = CplGrant.Status.CONSUMED
                update_fields.append("status")
            if grant.consumption_shift_id != consumption_shift.pk:
                grant.consumption_shift = consumption_shift
                update_fields.append("consumption_shift")
            if grant.consumed_date != consumed_date:
                grant.consumed_date = consumed_date
                update_fields.append("consumed_date")
            if update_fields:
                grant.save(update_fields=[*update_fields, "updated_at"])
                changes.append(f"grant #{grant.pk}: {'/'.join(update_fields)} updated")

        else:
            return {"ok": False, "reason": f"Unknown action {action!r}"}

    return {"ok": True, "changes": changes}


def reconcile_stale_shift_leave_reversals(
    *,
    since: date | None = None,
    upto: date | None = None,
    employee=None,
    policy: LeavePolicy | None = None,
    dry_run: bool = False,
) -> dict:
    """Reverse a `LeaveBalanceTransaction` whose linked `Shift` has since
    stopped matching the leave type it was debited under — the mirror image
    of `reconcile_orphan_shift_usage` above.

    `leave/signals.py::sync_shift_leave_ledger` normally reverses a stale
    shift-linked debit the instant the Shift is re-typed away from its leave
    policy (or deleted). That signal never fires for a `bulk_create` write —
    bulk roster uploads write that way on purpose — so a roster upload that
    retypes a previously-reconciled leave Shift (e.g. CL -> a worked shift)
    leaves its old USED debit standing forever unless something else catches
    it. This is that catch-up, run from the same nightly command as the
    forward-direction `reconcile_orphan_shift_usage`
    (`manage.py reconcile_shift_leave_usage`).

    Idempotent: `_reverse_shift_ledger_transaction` nulls the original
    transaction's `shift` FK once reversed (see leave/signals.py), so it
    drops out of the `shift__isnull=False` filter below and can never be
    reversed twice.
    """
    qs = (
        LeaveBalanceTransaction.objects
        .filter(reason=LeaveBalanceTransaction.Reason.USED, shift__isnull=False)
        .select_related(
            "balance__leave_policy__shift_type",
            "shift__shift_type__leave_policy",
        )
    )
    if since is not None:
        qs = qs.filter(shift__date__gte=since)
    if upto is not None:
        qs = qs.filter(shift__date__lte=upto)
    if employee is not None:
        qs = qs.filter(balance__employee=employee)
    if policy is not None:
        qs = qs.filter(balance__leave_policy=policy)

    from leave.signals import _reverse_shift_ledger_transaction  # avoid import-order issues at app load

    reversed_count = 0
    by_policy_year: dict[tuple[str, int], Decimal] = {}

    for txn in qs.order_by("shift__date"):
        shift = txn.shift
        current_policy = (
            getattr(shift.shift_type, "leave_policy", None)
            if shift.shift_type_id else None
        )
        if current_policy is not None and current_policy.pk == txn.balance.leave_policy_id:
            continue  # still matches its reconciled leave type -- not stale

        key = (txn.balance.leave_policy.shift_type.name, shift.date.year)
        by_policy_year[key] = by_policy_year.get(key, Decimal("0")) + (-txn.delta)
        reversed_count += 1
        if dry_run:
            continue

        _reverse_shift_ledger_transaction(
            txn,
            note=f"Shift #{shift.pk} edited away from its reconciled leave type (bulk reconcile)",
        )

    return {
        "reversed": reversed_count,
        "by_policy_year": by_policy_year,
    }


# ---------------------------------------------------------------------------
# Live-computed AL accrual (open cycle only — closed cycles keep the
# persisted, ledger-backed snapshot frozen at close_al_cycles.py time)
# ---------------------------------------------------------------------------

def _round_half(value: Decimal) -> Decimal:
    """Round to nearest 0.5 (half rounds up). Shared rounding rule for
    every AL accrual computation — batch (historically) and live (now)."""
    return (value * Decimal("2")).quantize(Decimal("1")) / Decimal("2")


def live_accrued_al(employee, policy: LeavePolicy, bal: LeaveBalance, *, upto: date | None = None) -> Decimal:
    """AL's accrual for `bal`'s cycle, computed live from worked days
    instead of read from a monthly-batch-updated field — worked days from
    `bal.cycle_start` to `upto` (default today, capped at the cycle's last
    day) ÷ `accrual_ratio_denominator`, rounded to the nearest 0.5, then
    clamped to `policy.max_days_per_year` when that's set above 0 (12 for
    AL — reusing the same field FIXED_ANNUAL policies use for their flat
    entitlement; harmless here since AL never takes that code path).

    "Worked days" counts WORK-category shifts plus any shift under a
    *paid* LeavePolicy (`is_paid=True` — currently AL, CL, CPL, MTL, SL) so
    that taking paid leave doesn't reduce AL accrual. Unpaid statuses
    (LWP, HDL, ABS) still don't count, so genuine unpaid absence still
    reduces accrual — that part of the worked-day-ratio design is
    intentional and unchanged.

    Falls back to the persisted `bal.accrued` (no live computation) when:
    - the cycle is already closed (frozen historical snapshot — a later
      correction to that window's Shift data must go through an explicit
      ADJUSTMENT, not silently change a closed cycle's numbers), or
    - the balance is `entitlement_frozen` (no further accrual, ever), or
    - the policy isn't ANNIVERSARY + WORKED_DAY_RATIO (i.e. isn't AL —
      other policies don't accrue this way).
    """
    if bal.cycle_closed or bal.entitlement_frozen:
        return bal.accrued
    if (policy.cycle != LeavePolicy.Cycle.ANNIVERSARY
            or policy.accrual_method != LeavePolicy.AccrualMethod.WORKED_DAY_RATIO):
        return bal.accrued
    if not bal.cycle_start:
        return bal.accrued

    upto = upto or date.today()
    window_end = upto
    if bal.cycle_end is not None:
        window_end = min(upto, bal.cycle_end - timedelta(days=1))
    if window_end < bal.cycle_start:
        return Decimal("0")

    denom = policy.accrual_ratio_denominator
    if denom <= 0:
        return Decimal("0")

    paid_type_ids = LeavePolicy.objects.filter(is_paid=True).values_list("shift_type_id", flat=True)
    worked = Shift.objects.filter(
        employee=employee, date__gte=bal.cycle_start, date__lte=window_end,
    ).filter(
        Q(shift_type__category=ShiftType.Category.WORK) | Q(shift_type_id__in=paid_type_ids)
    ).count()
    accrued = _round_half(Decimal(worked) / Decimal(denom))
    if policy.max_days_per_year > 0:
        accrued = min(accrued, Decimal(policy.max_days_per_year))
    return accrued


def live_used(employee, policy: LeavePolicy, bal: LeaveBalance, *, upto: date | None = None) -> Decimal:
    """`bal.used`, but with any not-yet-reconciled orphan Shift usage in
    the open cycle counted in immediately, instead of waiting for the
    nightly `reconcile_shift_leave_usage` catch-up (which only matters for
    rows written via `bulk_create` — bulk roster uploads bypass Django
    signals on purpose, so `leave/signals.py::sync_shift_leave_ledger`
    never sees them).

    `bal.used` itself is *not* recomputed from scratch here — it's already
    exact for everything already reconciled, including application-driven
    half-day debits (`consume_balance` handles those synchronously, with
    no batch lag at all). This only adds the remainder: LEAVE-category
    Shift rows for this policy in the window with no `LeaveApplication`
    and no ledger transaction yet — the same set
    `reconcile_orphan_shift_usage` would pick up, just counted live
    instead of persisted.

    Falls back to the persisted `bal.used` for a closed cycle (frozen
    historical snapshot, same posture as `live_accrued_al`).
    """
    if bal.cycle_closed or not bal.cycle_start:
        return bal.used

    upto = upto or date.today()
    window_end = upto
    if bal.cycle_end is not None:
        window_end = min(upto, bal.cycle_end - timedelta(days=1))
    if window_end < bal.cycle_start:
        return bal.used

    pending = Shift.objects.filter(
        employee=employee, date__gte=bal.cycle_start, date__lte=window_end,
        shift_type=policy.shift_type,
        leave_application__isnull=True,
        leave_ledger_transactions__isnull=True,
    ).count()
    return bal.used + Decimal(pending)


def live_available(employee, policy: LeavePolicy, bal: LeaveBalance, *, upto: date | None = None) -> Decimal:
    """`bal.available`, but with AL's open-cycle accrued computed live
    (see `live_accrued_al`) and usage topped up for anything not yet
    reconciled (see `live_used`), instead of trusting the batch-updated
    stored fields. Use this anywhere a balance is *shown or checked*
    (balance page, leave application preview/validation) — not in
    `close_al_cycles.py`'s freeze-write path, which persists the final
    live-computed numbers once, at closure, instead.
    """
    accrued = live_accrued_al(employee, policy, bal, upto=upto)
    used = live_used(employee, policy, bal, upto=upto)
    return bal.opening + accrued + bal.carried_forward - used - bal.encashed


# ---------------------------------------------------------------------------
# Batch variants for reports (shift summary export) — same numbers as the
# per-employee functions above, in a constant number of queries. Added
# 2026-10-13: the per-employee loop cost ~3 queries × ~1,500 employees (~4 s).
# leave/tests_live_bulk.py asserts equality with the per-employee versions;
# change both together.
# ---------------------------------------------------------------------------

def get_or_create_balances(employees, leave_policy: LeavePolicy, as_of: date) -> dict:
    """`get_or_create_balance` for many employees: {employee_pk: LeaveBalance}.

    Existing rows come back in one query. Anything that would make the
    single-employee function *write* — a missing row, or a legacy row it
    would heal (see the comment in `get_or_create_balance`) — is routed
    through that function, so creation and healing stay in one place.
    Anniversary policies only; other cycles just loop. Employees without a
    joining_date are skipped (the single version raises for them).
    """
    if leave_policy.cycle != LeavePolicy.Cycle.ANNIVERSARY:
        return {e.pk: get_or_create_balance(e, leave_policy, as_of) for e in employees}

    windows = {
        e.pk: anniversary_cycle_window(e.joining_date, as_of)
        for e in employees if getattr(e, "joining_date", None)
    }
    existing = {
        (b.employee_id, b.year): b
        for b in LeaveBalance.objects.filter(
            leave_policy=leave_policy,
            employee_id__in=list(windows),
            year__in={start.year for start, _ in windows.values()},
        )
    }
    today = date.today()
    out = {}
    for emp in employees:
        if emp.pk not in windows:
            continue
        cycle_start, cycle_end = windows[emp.pk]
        bal = existing.get((emp.pk, cycle_start.year))
        if bal is None or (bal.cycle_start is None and cycle_end > today):
            bal = get_or_create_balance(emp, leave_policy, as_of)
        else:
            bal.employee = emp  # spare a lazy FK fetch per row
        out[emp.pk] = bal
    return out


def live_accrued_and_used_bulk(balances, policy: LeavePolicy, *, upto: date | None = None) -> dict:
    """{balance_pk: (live_accrued_al, live_used)} for many balances of one
    policy — the per-employee functions' results, from one query.

    Both live counts are correlated subqueries over the same window those
    functions use: [cycle_start, min(upto, cycle_end − 1 day)], i.e.
    `cycle_start <= date <= upto AND date < cycle_end`. Every early return
    they make (closed/frozen cycle, non-AL policy, NULL cycle_start, …) is
    reproduced in Python below. A row with a cycle_start but no cycle_end
    (can't be expressed in the shared filter) falls back to the
    per-employee functions.
    """
    from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
    from django.db.models.functions import Coalesce

    upto = upto or date.today()
    balances = list(balances)
    out = {}

    is_al_shaped = (policy.cycle == LeavePolicy.Cycle.ANNIVERSARY
                    and policy.accrual_method == LeavePolicy.AccrualMethod.WORKED_DAY_RATIO)
    live, fallback = [], []
    for bal in balances:
        if bal.cycle_closed or not bal.cycle_start:
            # Both functions return their stored field (accrual also when frozen).
            out[bal.pk] = (bal.accrued, bal.used)
        elif bal.cycle_end is None:
            fallback.append(bal)
        else:
            live.append(bal)

    for bal in fallback:
        out[bal.pk] = (
            live_accrued_al(bal.employee, policy, bal, upto=upto),
            live_used(bal.employee, policy, bal, upto=upto),
        )
    if not live:
        return out

    def _count(shifts):
        counted = (
            shifts.filter(
                employee_id=OuterRef("employee_id"),
                date__gte=OuterRef("cycle_start"),
                date__lt=OuterRef("cycle_end"),
                date__lte=upto,
            )
            .order_by().values("employee_id").annotate(n=Count("pk")).values("n")
        )
        return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))

    paid_type_ids = list(
        LeavePolicy.objects.filter(is_paid=True).values_list("shift_type_id", flat=True)
    )
    worked_shifts = Shift.objects.filter(
        Q(shift_type__category=ShiftType.Category.WORK) | Q(shift_type_id__in=paid_type_ids)
    )
    pending_shifts = Shift.objects.filter(
        shift_type=policy.shift_type,
        leave_application__isnull=True,
        leave_ledger_transactions__isnull=True,
    )
    counts = {
        row["pk"]: row
        for row in LeaveBalance.objects.filter(pk__in=[b.pk for b in live])
        .annotate(worked=_count(worked_shifts), pending=_count(pending_shifts))
        .values("pk", "worked", "pending")
    }

    denom = policy.accrual_ratio_denominator
    for bal in live:
        row = counts[bal.pk]
        if bal.entitlement_frozen or not is_al_shaped:
            accrued = bal.accrued
        elif denom <= 0:
            accrued = Decimal("0")
        else:
            # An empty window (upto before cycle_start) counts 0 → 0, as the
            # single version's explicit early return does.
            accrued = _round_half(Decimal(row["worked"]) / Decimal(denom))
            if policy.max_days_per_year > 0:
                accrued = min(accrued, Decimal(policy.max_days_per_year))
        out[bal.pk] = (accrued, bal.used + Decimal(row["pending"]))
    return out


def _add_working_days(
    start: date, n: int, employee, *, pattern=None, holidays=None,
) -> date:
    """Return a date that is `n` working days after `start` for this employee.
    Working day = the employee's pattern WORKs that weekday and the date isn't
    a Holiday. Bounds the search at 30 calendar days for safety.

    Callers looping over many dates pass precomputed `pattern`/`holidays`
    (see cpl_unexpired_balance) — the default per-call fetch is 2 queries.
    """
    if pattern is None:
        pattern = _pattern_category_by_weekday(employee)
    if holidays is None:
        holidays = _holiday_dates_in_range(start, start + timedelta(days=n * 3 + 14))
    d = start
    remaining = n
    safety = 60
    while remaining > 0 and safety > 0:
        d = d + timedelta(days=1)
        safety -= 1
        if is_working_day(d, pattern, holidays):
            remaining -= 1
    return d


def cpl_unexpired_balance(employee, policy: LeavePolicy, today: date | None = None) -> Decimal:
    """For policies where CpL grants expire (`cpl_use_within_days` > 0),
    walk the ACCRUAL ledger FIFO: oldest grants are consumed first. Sum
    grants whose deadline (grant_date + cpl_use_within_days working days)
    is on or after `today`. Pre-existing USED transactions are paired off
    against the oldest grants first.

    For policies without an expiry, returns `LeaveBalance.available`.
    """
    today = today or _today()
    bal = get_or_create_balance(employee, policy, today)

    if not policy.cpl_use_within_days:
        return bal.available

    accruals = list(
        bal.transactions
        .filter(reason=LeaveBalanceTransaction.Reason.ACCRUAL)
        .order_by("occurred_at")
    )
    used_total = bal.used  # cumulative debit for this balance/year

    # Precompute the employee's pattern and the holiday set covering every
    # grant's deadline window once — the per-grant deadline walk otherwise
    # costs 2 queries per unconsumed grant.
    pattern = _pattern_category_by_weekday(employee)
    holidays: set[date] = set()
    if accruals:
        span_pad = timedelta(days=policy.cpl_use_within_days * 3 + 14)
        holidays = _holiday_dates_in_range(
            accruals[0].occurred_at.date(),
            accruals[-1].occurred_at.date() + span_pad,
        )

    # Walk accruals oldest-first, deducting `used_total` from each grant
    # in turn. Whatever remains is "still available"; among that remainder,
    # only grants whose deadline hasn't passed count toward the balance.
    unexpired_remaining = Decimal("0")
    for txn in accruals:
        granted = txn.delta  # positive
        if used_total > 0:
            taken = min(granted, used_total)
            granted -= taken
            used_total -= taken
        if granted <= 0:
            continue
        grant_date = txn.occurred_at.date()
        deadline = _add_working_days(
            grant_date, policy.cpl_use_within_days, employee,
            pattern=pattern, holidays=holidays,
        )
        if today <= deadline:
            unexpired_remaining += granted
    return unexpired_remaining


def _today() -> date:
    """Indirection so tests can monkeypatch."""
    return date.today()


def describe_balance_impact(application: LeaveApplication) -> dict:
    """The arithmetic behind `validate_application_against_balance`, returned
    as data instead of raised as an exception — so a manager deciding on an
    application can be shown the same numbers the approve-time check will
    apply to it.

    `validate_application_against_balance` is a thin wrapper over this, which
    is the point: what the balance drawer shows a manager is, by
    construction, what Approve will enforce a moment later.

    **Note the window.** Availability is computed as of the application's own
    `start_date`, against the balance row covering that date — not as of
    today, and not against the `?year=` row. So this cannot be derived by
    subtracting the requested days from the balance dashboard's "available"
    column; for a leave in another calendar year (or another AL anniversary
    cycle) those are different rows entirely.

    Keys:
      requested / working / sandwich   billable days (sandwich rule applied)
      available_before / available_after   None when the check is bypassed
      sufficient   False only when approve would actually be blocked
      bypass       why the check doesn't apply, "" when it does:
                   "unpaid" (salary-deduct, not balance-deduct) |
                   "no_billable_days" | "unseeded" (HR hasn't configured
                   any entitlement yet — the form deliberately lets these
                   through)
      message      the exact ValidationError text, when insufficient
      is_advance   True when the request exceeds the live available balance
                   but the policy allows it (allow_advance_against_entitlement)
                   and it still fits under `ceiling` — sufficient stays True,
                   this is purely "the supervisor should know before deciding".
      ceiling      max_days_per_year + opening + carried_forward − used −
                   encashed — what the employee could ever have this cycle/
                   year, not just what's accrued so far. None when the
                   policy doesn't allow advances. Requests beyond this are
                   still hard-blocked (sufficient=False) — arithmetically
                   impossible this cycle regardless of supervisor discretion.
    """
    policy = application.leave_type.leave_policy
    working, sandwich, total = compute_application_billable_days(application)
    base = {
        "policy": policy,
        "leave_type_name": policy.shift_type.name,
        "is_paid": policy.is_paid,
        "working": working,
        "sandwich": sandwich,
        "requested": total,
        # The date the availability figures are computed as of — the leave's
        # own start, not today (see the note above). Surfaced so the UI can
        # say so: for a future-dated leave these numbers legitimately differ
        # from the balance dashboard's as-of-today column.
        "as_of": application.start_date,
        "available_before": None,
        "available_after": None,
        "sufficient": True,
        "bypass": "",
        "expiry_window_days": policy.cpl_use_within_days or 0,
        "message": "",
        "is_advance": False,
        "ceiling": None,
    }

    if not policy.is_paid:
        return {**base, "bypass": "unpaid"}
    if total <= 0:
        return {**base, "bypass": "no_billable_days"}

    bal = get_or_create_balance(application.employee, policy, application.start_date)

    # Serialize concurrent approvals: inside the approve transaction, re-read
    # the balance row under a row lock so two simultaneous approvals (or a
    # double-submit) can't both pass this check and over-debit. Outside a
    # transaction — the form-time pre-check, and the read-only balance drawer —
    # this is skipped; approve re-validates under its own lock.
    if connection.in_atomic_block:
        bal = LeaveBalance.objects.select_for_update().get(pk=bal.pk)

    # CpL-style: respect grant-day expiry.
    if policy.cpl_use_within_days:
        usable = cpl_unexpired_balance(application.employee, policy, application.start_date)
        return {
            **base,
            "available_before": usable,
            "available_after": usable - total,
            "sufficient": total <= usable,
            "message": "" if total <= usable else (
                f"Insufficient un-expired {policy.shift_type.name}: "
                f"requesting {total} day(s), un-expired available {usable} day(s). "
                f"{policy.shift_type.name} grants must be used within "
                f"{policy.cpl_use_within_days} working days of being earned."
            ),
        }

    # AL's accrued is computed live here (worked days so far this cycle),
    # not read from the batch-updated field, and usage is topped up with
    # anything not yet reconciled — so an application is checked against
    # the employee's true current entitlement, not a stale snapshot.
    # No-op for anything that isn't an open cycle (live_accrued_al/
    # live_used both fall back to the stored fields for a closed cycle or,
    # for accrued, a non-AL policy).
    accrued = live_accrued_al(application.employee, policy, bal, upto=application.start_date)
    seeded = bal.opening + accrued + bal.carried_forward
    # "Not yet configured" bypass: lets HR set entitlement-based policies
    # (CL/SL/MtL/AL) up ahead of real use without blocking the form. A
    # grant-driven policy (CpL) has no entitlement to configure — its
    # balance *is* the grants — so a zero there means "nothing earned", and
    # must hit the hard block below instead of being waved through.
    if seeded == 0 and not policy.cpl_grant_on_holiday_work:
        return {**base, "bypass": "unseeded"}  # not yet configured — bypass

    used = live_used(application.employee, policy, bal, upto=application.start_date)
    available = seeded - used - bal.encashed
    if total <= available:
        return {
            **base,
            "available_before": available,
            "available_after": available - total,
            "sufficient": True,
            "message": "",
        }

    # max_days_per_year == 0 means "uncapped" everywhere else in this model
    # (see its help_text) — there's no meaningful "everything you could ever
    # earn" ceiling to advance against, so fall back to the hard block.
    if not policy.allow_advance_against_entitlement or policy.max_days_per_year <= 0:
        return {
            **base,
            "available_before": available,
            "available_after": available - total,
            "sufficient": False,
            "message": (
                f"Insufficient {policy.shift_type.name} balance: "
                f"requesting {total} day(s), available {available} day(s)."
            ),
        }

    # Advance zone: short on live/accrued-so-far balance, but the policy
    # lets a request draw against what could still be earned this cycle/
    # year. Ceiling assumes the employee earns everything they possibly
    # could this cycle — max_days_per_year — rather than what's accrued/
    # granted so far.
    ceiling = policy.max_days_per_year + bal.opening + bal.carried_forward - used - bal.encashed
    if total <= ceiling:
        return {
            **base,
            "available_before": available,
            "available_after": available - total,
            "sufficient": True,
            "is_advance": True,
            "ceiling": ceiling,
            "message": (
                f"Exceeds current {policy.shift_type.name} balance by "
                f"{total - available} day(s) — within your remaining "
                f"{ceiling} day(s) for this cycle/year. Requires supervisor "
                f"approval."
            ),
        }

    return {
        **base,
        "available_before": available,
        "available_after": available - total,
        "sufficient": False,
        "ceiling": ceiling,
        "message": (
            f"Insufficient {policy.shift_type.name} balance: requesting "
            f"{total} day(s), but at most {ceiling} day(s) is possible this "
            f"cycle/year (max {policy.max_days_per_year} + opening "
            f"{bal.opening} + carried forward {bal.carried_forward} − used "
            f"{used} − encashed {bal.encashed})."
        ),
    }


def validate_application_against_balance(application: LeaveApplication) -> None:
    """Raise ValidationError if the application would exceed available balance.

    Skips unpaid policies (LWP, HdL) — those are salary-deduct, not balance-deduct.
    For paid policies that haven't been seeded with any entitlement yet
    (opening + accrued + carried_forward == 0), we let the application through
    so HR can configure balances ahead of real use without blocking the form.
    For CpL-style policies with `cpl_use_within_days`, expired grants don't
    count toward the available pool.

    All of that arithmetic lives in `describe_balance_impact`, which the
    balance drawer renders — so the manager sees the shortfall before
    clicking Approve rather than after.
    """
    impact = describe_balance_impact(application)
    if not impact["sufficient"]:
        raise ValidationError(impact["message"])


# ---------------------------------------------------------------------------
# Recompute orchestrator
# ---------------------------------------------------------------------------

@transaction.atomic
def recompute_sandwich(employee, around_date: date, *, request_user=None) -> None:
    """Re-evaluate every APPROVED LeaveApplication for `employee` within
    ±WINDOW_DAYS of `around_date`. Updates billing fields, reconciles the
    ledger, and re-materialises Shifts. Idempotent.
    """
    window_start = around_date - timedelta(days=WINDOW_DAYS)
    window_end = around_date + timedelta(days=WINDOW_DAYS)

    affected = (
        LeaveApplication.objects
        .select_related("leave_type", "leave_type__leave_policy")
        .filter(
            employee=employee,
            status=LeaveApplication.Status.APPROVED,
            end_date__gte=window_start,
            start_date__lte=window_end,
        )
    )

    for app in affected:
        _reconcile_application(app, request_user)


def _reconcile_application(application: LeaveApplication, request_user) -> None:
    new_working, new_sandwich, new_total = compute_application_billable_days(application)

    # Update billing fields if drifted.
    fields_changed = []
    if application.working_days != new_working:
        application.working_days = new_working
        fields_changed.append("working_days")
    if application.sandwich_days != new_sandwich:
        application.sandwich_days = new_sandwich
        fields_changed.append("sandwich_days")
    if application.effective_days != new_total:
        application.effective_days = new_total
        fields_changed.append("effective_days")
    if fields_changed:
        application.save(update_fields=fields_changed + ["updated_at"])

    # Reconcile ledger only for paid policies.
    policy = application.leave_type.leave_policy
    if policy.is_paid:
        _absorb_shift_debits(application, _billable_dates_for_application(application))
        expected_delta = -new_total
        current_delta = _existing_application_delta(application)
        gap = expected_delta - current_delta
        if gap != 0:
            bal = get_or_create_balance(
                application.employee, policy, application.start_date,
            )
            if gap < 0:
                # Need more debit (under-debited).
                LeaveBalanceTransaction.objects.create(
                    balance=bal,
                    delta=gap,
                    reason=LeaveBalanceTransaction.Reason.USED,
                    application=application,
                    created_by=request_user,
                    note="Sandwich recompute (additional debit)",
                )
            else:
                # Over-debited: refund (gap > 0).
                LeaveBalanceTransaction.objects.create(
                    balance=bal,
                    delta=gap,
                    reason=LeaveBalanceTransaction.Reason.REVERSED,
                    application=application,
                    created_by=request_user,
                    note="Sandwich recompute (refund)",
                )
            bal.used = F("used") - gap
            bal.save(update_fields=["used", "updated_at"])

    # Re-materialise shifts to reflect the current correct billable date set.
    materialise_shifts_for_application(application, request_user)


# ---------------------------------------------------------------------------
# Pro-rating
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Eligibility helpers
# ---------------------------------------------------------------------------

def _months_of_service(joining_date: date | None, as_of: date) -> int:
    """Whole months between joining_date and as_of. 0 if joining_date is None."""
    if not joining_date:
        return 0
    months = (as_of.year - joining_date.year) * 12 + (as_of.month - joining_date.month)
    if as_of.day < joining_date.day:
        months -= 1
    return max(months, 0)


def _employee_gender(employee) -> str:
    """Read gender from PersonalInfo if present. Returns 'M', 'F', or ''."""
    pi = getattr(employee, "personal_info", None)
    return pi.gender if pi else ""


def is_eligible_for_policy(employee, policy: LeavePolicy, as_of: date | None = None) -> tuple[bool, str]:
    """Return (eligible, reason). reason is human-readable when ineligible."""
    as_of = as_of or date.today()

    # Gender gate
    if policy.applies_to_gender == LeavePolicy.Gender.FEMALE:
        if _employee_gender(employee) != "F":
            return False, f"{policy.shift_type.name} is available to female employees only."
    elif policy.applies_to_gender == LeavePolicy.Gender.MALE:
        if _employee_gender(employee) != "M":
            return False, f"{policy.shift_type.name} is available to male employees only."

    # Service-length gate
    if policy.min_service_months > 0:
        joining = getattr(employee, "joining_date", None)
        months = _months_of_service(joining, as_of)
        if months < policy.min_service_months:
            return False, (
                f"{policy.shift_type.name} requires at least "
                f"{policy.min_service_months} months of service "
                f"(currently {months})."
            )

    # Surviving children gate (Maternity)
    if policy.max_surviving_children is not None:
        ji = getattr(employee, "job_info", None)
        children = ji.surviving_children_count if ji else 0
        if children > policy.max_surviving_children:
            return False, (
                f"{policy.shift_type.name} requires at most "
                f"{policy.max_surviving_children} surviving children "
                f"(currently {children})."
            )

    return True, ""


def eligible_leave_policies(employee, *, as_of: date | None = None):
    """QuerySet of LeavePolicy rows the employee may apply for. Used to filter
    the leave_type dropdown so non-applicable types are hidden."""
    as_of = as_of or date.today()
    eligible_pks = []
    for policy in LeavePolicy.objects.select_related("shift_type"):
        ok, _ = is_eligible_for_policy(employee, policy, as_of)
        if ok:
            eligible_pks.append(policy.pk)
    return LeavePolicy.objects.filter(pk__in=eligible_pks)


def validate_application_eligibility(application: LeaveApplication) -> None:
    """Raise ValidationError when the employee fails an eligibility gate for
    the chosen leave type. Run BEFORE balance check so the user sees the
    most actionable error."""
    policy = getattr(application.leave_type, "leave_policy", None)
    if policy is None:
        return
    ok, reason = is_eligible_for_policy(
        application.employee, policy, as_of=application.start_date or date.today(),
    )
    if not ok:
        raise ValidationError({"leave_type": reason})


def validate_application_attachment(application: LeaveApplication) -> None:
    """Raise ValidationError when the policy requires a doctor's certificate
    (or equivalent) and the application has no attachment. Threshold is
    `policy.cert_required_min_days`: 0 = never required, >=1 = required when
    `application.day_count >= cert_required_min_days`.
    """
    policy = getattr(application.leave_type, "leave_policy", None)
    if policy is None:
        return
    if policy.cert_required_min_days <= 0:
        return
    if application.day_count < policy.cert_required_min_days:
        return
    if not application.attachment:
        raise ValidationError({
            "attachment": (
                f"A supporting document is required for "
                f"{policy.shift_type.name} of "
                f"{policy.cert_required_min_days} or more days."
            ),
        })


# ---------------------------------------------------------------------------
# Pro-rating
# ---------------------------------------------------------------------------

def prorate_entitlement(policy: LeavePolicy, joining_date: date | None, year: int) -> Decimal:
    """For policies with `prorate_on_join=True`, reduce the annual entitlement
    by the fraction of the year already elapsed at the employee's joining
    date. Rounds to 0.5 days.
    """
    base = Decimal(policy.max_days_per_year)
    if not policy.prorate_on_join or not joining_date:
        return base
    if joining_date.year < year:
        return base
    if joining_date.year > year:
        return Decimal("0")
    # Joined this year: pro-rate by months remaining (incl. joining month).
    months_remaining = 13 - joining_date.month
    raw = base * Decimal(months_remaining) / Decimal("12")
    rounded_half = (raw * Decimal("2")).to_integral_value() / Decimal("2")
    return rounded_half
