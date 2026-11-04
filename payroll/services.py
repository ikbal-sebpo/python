"""Small cross-cutting helpers, plus the run lifecycle transitions Phase 3
actually supports: create -> open inputs -> lock inputs -> calculate
(engine.py) -> reopen. Lock/reverse/settlement (Phase 5+) land here once the
approval chain exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from payroll.models import PayrollRun, PayrollRunAudit, PayslipCounter


def next_payslip_number() -> str:
    """Atomically bump the singleton counter and return the next payslip number.

    Mirrors policies.services.next_policy_key's select_for_update idiom.
    Must run inside a transaction.
    """
    counter, _created = PayslipCounter.objects.select_for_update().get_or_create(pk=1)
    counter.last_value = (counter.last_value or 0) + 1
    counter.save(update_fields=["last_value"])
    return f"PS-{counter.last_value}"


def create_run(period, actor, *, run_type=PayrollRun.RunType.REGULAR, name: str = "") -> PayrollRun:
    with transaction.atomic():
        run = PayrollRun.objects.create(
            period=period, run_type=run_type, name=name,
            status=PayrollRun.Status.INPUTS_OPEN,
            added_by=actor, updated_by=actor,
        )
        PayrollRunAudit.objects.create(
            run=run, event="CREATED", from_status="", to_status=run.status, actor=actor,
        )
    return run


def lock_inputs(run: PayrollRun, actor) -> PayrollRun:
    """Locks inputs so Calculate can run.

    Deliberately does NOT gate on open PayrollControlCheck rows: those are
    only ever produced by a prior calculate, and calculate_run() deletes and
    regenerates every control check from scratch each time (delete-and-
    rebuild). Gating lock on "no open blocking checks" would deadlock the
    reopen -> lock -> recalculate loop the first time any check ever fires,
    since nothing marks stale checks resolved on reopen. Blocking checks are
    informational this phase — the actual submit-for-approval gate is
    Phase 5's job, once the approval chain exists.

    Meal counts ARE gated here, deliberately the odd one out: pull_meal_counts
    pulls this period's counts from meals.sebpo.net and persists them as
    MealCount rows (Phase 7, docs/PAYROLL_PLAN.md's meals.sebpo.net note); a
    fetch failure raises and blocks the lock rather than soft-warning, since
    the engine must never calculate meal deduction against a stale or missing
    count. Re-locking after Reopen re-pulls, so the count is always the
    freshest available at the moment inputs are actually frozen.
    """
    if run.status != PayrollRun.Status.INPUTS_OPEN:
        raise ValidationError(f"Cannot lock inputs for a run in status {run.status}.")
    from payroll.meals import pull_meal_counts
    pull_meal_counts(run.period, actor)
    with transaction.atomic():
        from_status = run.status
        run.status = PayrollRun.Status.INPUTS_LOCKED
        run.updated_by = actor
        run.save()
        PayrollRunAudit.objects.create(run=run, event="INPUTS_LOCKED", from_status=from_status, to_status=run.status, actor=actor)
    return run


def reopen_inputs(run: PayrollRun, actor) -> PayrollRun:
    if run.status not in (PayrollRun.Status.INPUTS_LOCKED, PayrollRun.Status.CALCULATED):
        raise ValidationError(f"Cannot reopen inputs for a run in status {run.status}.")
    with transaction.atomic():
        from_status = run.status
        run.status = PayrollRun.Status.INPUTS_OPEN
        run.updated_by = actor
        run.save()
        PayrollRunAudit.objects.create(run=run, event="REOPENED", from_status=from_status, to_status=run.status, actor=actor)
    return run


# ---------------------------------------------------------------------------
# Overtime input window (added 2026-09-15)
#
# The single date authority, shared by every write path — the bulk uploader
# (payroll/upload.py + upload_views.py), the manual add/edit drawer
# (payroll/forms.py::OvertimeEntryForm) and the void/delete views. Living here
# rather than in any one of them is the point: a rule the uploader enforced
# but the drawer didn't would be a hole, not a bug.
#
# Rather than allow/deny, the window answers "may this be written, and with
# WHAT STATUS" — because a locked period no longer refuses writes, it
# downgrades them to PENDING for HR to approve (Q55, 2026-09-15). Resolution
# order, applied by check():
#
#   1. work_date in the future                    -> reject
#   2. more than MAX_BACKDATE_DAYS ago            -> reject
#   3. locked, AND the most recently locked period-> allow as PENDING
#   4. locked, but an older locked period         -> reject
#   5. not locked, before the open floor          -> reject
#   6. otherwise                                  -> allow as PENDING
#
# Since 2026-10-17 (docs/OVERTIME_APPROVAL_CHAIN_PLAN.md) rules 3 and 6 both
# hand back PENDING: every write starts the review -> approve chain, open
# period or not, so the window is once again purely a date gate. The
# status is still carried on the decision so the write paths keep one
# source for it. Rules 3/4 (which locked period still accepts late filing)
# are unchanged.
#
# Rule 3 sits ABOVE rule 5 deliberately: when the locked period IS the current
# calendar period, its dates are above the floor yet must still be accepted.
#
# Rule 3 applies to EVERYONE, HR-Payroll included — "once inputs are locked
# nobody uploads as approved" (the user's own words). can_manage_locked_overtime
# is therefore the right to APPROVE a pending entry, not a right to write an
# approved one; that keeps every post-lock approval an explicit, recorded act
# (processed_by/processed_at) instead of one implied by whoever uploaded.
#
# NOTE: this partially reverses the 2026-09-07 "no period restriction at all"
# decision on parse_upload, and partially reverses Q4's "a supervisor's upload
# is final" — both recorded in docs/PAYROLL_PLAN.md rather than silently
# changed, per that doc's own posture.
# ---------------------------------------------------------------------------

REJECT_FUTURE_WORK_DATES = True
"""Overtime is filed after the work is done, so a work date later than today
is a typo every time. Flip to False to allow forward-dated entries."""

MAX_BACKDATE_DAYS = 365
"""Outer sanity bound, mirroring payroll.upload._parse_work_date's own guard.
Applies to everyone — there is no role that may file a year-old work date."""

OPEN_RUN_STATUSES = (PayrollRun.Status.DRAFT, PayrollRun.Status.INPUTS_OPEN)
LOCKED_RUN_STATUSES = (
    PayrollRun.Status.INPUTS_LOCKED,
    PayrollRun.Status.CALCULATING,
    PayrollRun.Status.CALCULATED,
)


@dataclass(frozen=True)
class WindowDecision:
    """What the window says about one work date.

    `status` is the OvertimeEntry.Status a write for this date must take —
    always PENDING since the review chain (2026-10-17); it used to be
    APPROVED on the open-period path. None when `allowed` is False.
    """

    allowed: bool
    status: str | None
    reason: str | None

    @property
    def needs_approval(self) -> bool:
        from payroll.models import OvertimeEntry

        return self.status == OvertimeEntry.Status.PENDING


class OvertimeWindow:
    """Which work dates `user` may file/edit overtime for, right now.

    Build one per request and reuse it: the run lookup is a single query
    resolved lazily, so checking 500 uploaded rows costs one round-trip, not
    500 — the same caching idiom as parse_upload's period_cache.

        window = OvertimeWindow(request.user, request=request)
        decision = window.resolve(work_date)
        if decision.allowed:
            entry.status = decision.status   # APPROVED, or PENDING once locked
    """

    def __init__(self, user, *, today=None, request=None):
        from django.utils import timezone as _tz

        self.user = user
        self.request = request
        self.today = today or _tz.localdate()
        self._runs = None
        self._floor = None

    # --- lazily-loaded state ------------------------------------------

    @property
    def can_manage_locked(self) -> bool:
        from accounts.permissions import can_manage_locked_overtime

        if not hasattr(self, "_can_manage_locked"):
            self._can_manage_locked = can_manage_locked_overtime(self.user, request=self.request)
        return self._can_manage_locked

    def _load_runs(self) -> list[tuple]:
        """[(start_date, end_date, status)] for every run. One query.

        Deliberately unfiltered: there is one run per period, so this is a
        few dozen rows even years in, and filtering by date would have to
        guess at CUSTOM periods' arbitrary ranges.
        """
        if self._runs is None:
            self._runs = list(
                PayrollRun.objects
                .values_list("period__start_date", "period__end_date", "status")
            )
        return self._runs

    @property
    def floor(self):
        """Earliest work date a non-privileged user may file for."""
        if self._floor is None:
            from payroll.periods import period_bounds, period_for_date

            calendar_start, _end = period_bounds(*period_for_date(self.today))
            open_starts = [
                start for start, _e, status in self._load_runs()
                if status in OPEN_RUN_STATUSES
            ]
            self._floor = min([*open_starts, calendar_start])
        return self._floor

    @property
    def pending_floor(self):
        """Start date of the MOST RECENTLY locked period — the only locked
        period still open to late filing (as PENDING).

        "Most recent" is by period start_date, not by when the lock happened:
        if Aug and Sep are both locked, only Sep accepts late overtime. Older
        locked periods have been paid and are closed for good. Returns None
        when nothing is locked.
        """
        starts = [
            start for start, _e, status in self._load_runs()
            if status in LOCKED_RUN_STATUSES
        ]
        return max(starts) if starts else None

    def covers_most_recent_locked(self, work_date) -> bool:
        """True when `work_date` falls inside the most recently locked period."""
        if work_date is None:
            return False
        target = self.pending_floor
        if target is None:
            return False
        return any(
            start == target and start <= work_date <= end
            for start, end, status in self._load_runs()
            if status in LOCKED_RUN_STATUSES
        )

    # --- the gates ----------------------------------------------------

    def is_locked(self, work_date) -> bool:
        """True when a run covering `work_date` has passed input lock.

        Any covering run being locked is enough — a CUSTOM period (e.g. an
        Eid run) can overlap the STANDARD one, and the stricter of the two
        should win.
        """
        if work_date is None:
            return False
        return any(
            start <= work_date <= end and status in LOCKED_RUN_STATUSES
            for start, end, status in self._load_runs()
        )

    def resolve(self, work_date) -> "WindowDecision":
        """The full answer for `work_date`: may it be written, and as what?

        See the resolution order at the top of this section. Callers that only
        care whether a write is permitted use check()/allows(); callers that
        WRITE must use this and honour `.status`, or a locked period would be
        silently filed as approved.
        """
        from payroll.models import OvertimeEntry

        if work_date is None:
            # The field's own `required` validation covers this.
            return WindowDecision(True, OvertimeEntry.Status.PENDING, None)

        # 1 + 2 — nonsense dates, refused for everyone including HR-Payroll.
        if REJECT_FUTURE_WORK_DATES and work_date > self.today:
            return WindowDecision(False, None, (
                f"{work_date.isoformat()} is in the future — overtime is filed "
                "after the work is done."
            ))
        if (self.today - work_date).days > MAX_BACKDATE_DAYS:
            return WindowDecision(False, None, (
                f"{work_date.isoformat()} is more than a year ago — likely a typo."
            ))

        # 3 + 4 — a locked period. The most recent one still accepts late
        # filing, as PENDING; anything older has been paid and is closed.
        if self.is_locked(work_date):
            if self.covers_most_recent_locked(work_date):
                return WindowDecision(True, OvertimeEntry.Status.PENDING, None)
            return WindowDecision(False, None, (
                f"Payroll for the period covering {work_date.isoformat()} has "
                "already been completed — that period is closed."
            ))

        # 5 — before the open period, and not the locked period handled above.
        if work_date < self.floor:
            return WindowDecision(False, None, (
                f"{work_date.isoformat()} is before the open payroll period "
                f"(from {self.floor.isoformat()}) — that period's inputs are closed."
            ))

        # 6 — the ordinary path. PENDING since the review chain; before
        # 2026-10-17 this was the one branch that handed back APPROVED.
        return WindowDecision(True, OvertimeEntry.Status.PENDING, None)

    def check(self, work_date) -> str | None:
        """None when `user` may file/edit overtime for `work_date`, else why not.

        Kept as the read-only shorthand so filtering/permission call sites did
        not all have to churn; anything that writes wants resolve().
        """
        return self.resolve(work_date).reason

    # --- UI state -----------------------------------------------------
    #
    # Both exist so the page can describe the window that ACTUALLY applies
    # right now instead of reciting the rule unconditionally. The banner used
    # to end with "once a period's inputs are locked, only HR-Payroll can
    # change its entries" for every non-HR viewer, whether or not anything was
    # locked — which read as "this period is locked" and had a supervisor
    # reporting the open Aug 16 - Sep 15 period as wrongly closed (2026-09-15)
    # when filing was in fact working normally.

    @property
    def has_locked_runs(self) -> bool:
        """True when any run has passed input lock — i.e. the lock rule is
        worth mentioning at all."""
        return any(status in LOCKED_RUN_STATUSES for _s, _e, status in self._load_runs())

    @property
    def current_period_locked(self) -> bool:
        """True when the period covering today is itself locked.

        Distinct from has_locked_runs because it changes what the viewer can
        do *now* rather than what they can't do to history: filing into a
        locked current period still works, but lands PENDING.
        """
        return self.is_locked(self.today)

    @property
    def files_as_pending(self) -> bool:
        """True when overtime filed for TODAY would land PENDING rather than
        counting straight away. Always True since the review chain
        (2026-10-17); kept so the templates/tests that read it stay valid.
        Use current_period_locked for the "and the run must be
        recalculated" remark."""
        return self.resolve(self.today).needs_approval

    def allows(self, work_date) -> bool:
        return self.check(work_date) is None

    def partition(self, rows):
        """Split parsed upload rows into (allowed, out_of_window).

        Each allowed row carries the status it must be written with, on
        `row.window_status` — rows landing in a locked period come back
        ALLOWED but PENDING rather than rejected (Q55). There is therefore no
        "locked" reject bucket any more; the only rejects left are dates
        nobody may file at all.

        Kept here rather than inside parse_upload because the rule is
        user-dependent and parse_upload is deliberately a pure, user-agnostic
        parser. The view calls this between parse and preview/apply so a
        Preview shows both the rejections and the pending count before
        anything is written.
        """
        from payroll.upload import RowError

        allowed, out_of_window = [], []
        for row in rows:
            decision = self.resolve(row.work_date)
            if decision.allowed:
                row.window_status = decision.status
                allowed.append(row)
                continue
            out_of_window.append(RowError(
                row.line_no, "A",
                {
                    "Employee_ID": row.employee_id,
                    "Working_date": row.work_date.isoformat(),
                    "Overtime_type": getattr(row.overtime_type, "name", ""),
                },
                decision.reason,
            ))
        return allowed, out_of_window


# ---------------------------------------------------------------------------
# Compensation master writes (Phase B, 2026-09-19)
#
# docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase B. `supersede_salary_structure`
# is the one place that knows how to close an employee's current row and open
# the next: the bulk uploader (payroll/upload.py::apply_salary_upload) and the
# go-live approval path both call it, so the two can't drift on revision
# numbering or on what effective_to the closed row gets. Same reasoning as
# attendance's shared _approve_correction / _decline_correction.
# ---------------------------------------------------------------------------

def supersede_salary_structure(
    employee, *, gross_monthly, effective_from, reason, actor=None,
    note: str = "", batch=None, status=None,
):
    """Close the employee's current APPROVED open-ended SalaryStructure and
    open a new one at `effective_from`, returning the new row.

    The closed row gets ``effective_to = effective_from - 1 day`` and
    ``status=SUPERSEDED``. That superseded row is **not** dead data — since
    2026-09-19 the engine reads it to price the days it actually covered
    (payroll/engine.py::_salary_segments), which is what makes a mid-period
    go-live prorate correctly.

    `status` defaults to APPROVED (the uploader's behaviour, unchanged). The
    go-live path passes PENDING instead: a package-derived revision is a
    *proposal*, and money keeps a human gate.
    """
    from datetime import timedelta

    from django.utils import timezone

    from payroll.models import SalaryStructure

    status = status or SalaryStructure.Status.APPROVED
    now = timezone.now()

    with transaction.atomic():
        current = (
            SalaryStructure.objects
            .select_for_update()
            .filter(
                employee=employee, status=SalaryStructure.Status.APPROVED,
                effective_to__isnull=True,
            )
            .first()
        )
        # A PENDING proposal must NOT close the current row — the employee is
        # still paid the old figure until someone approves the new one.
        if current is not None and status == SalaryStructure.Status.APPROVED:
            if current.effective_from == effective_from:
                # A same-date correction (docs/SALARY_ADJUSTMENT_PLAN.md §3):
                # the revised figure applies from the very day the old one
                # did, so the old row never priced a day — CANCELLED, not
                # closed "the day before it opened" (an inverted row the
                # engine would still match). The structure before it stays
                # SUPERSEDED and is what the adjustment ledger reads as the
                # rate those months were actually paid at.
                current.effective_to = effective_from
                current.status = SalaryStructure.Status.CANCELLED
                current.note = (
                    (current.note + " · " if current.note else "")
                    + f"Replaced by a same-date correction ({now:%Y-%m-%d})"
                )[:255]
            else:
                current.effective_to = effective_from - timedelta(days=1)
                current.status = SalaryStructure.Status.SUPERSEDED
            current.updated_by = actor
            current.save()

        new_structure = SalaryStructure(
            employee=employee,
            revision_no=(current.revision_no + 1) if current else 1,
            effective_from=effective_from, effective_to=None,
            reason=reason, gross_monthly=gross_monthly,
            status=status, note=note, batch=batch,
            processed_by=actor if status == SalaryStructure.Status.APPROVED else None,
            processed_at=now if status == SalaryStructure.Status.APPROVED else None,
            added_by=actor, added_at=now, updated_by=actor,
        )
        new_structure.save()
        return new_structure


def propose_salary_for_assignment(assignment, actor=None):
    """Given a new ``employees.TeamAssignment``, propose the salary revision
    its package implies — or return None when there is nothing to propose.

    Returns None when: no ``TeamSalaryPackage`` resolves for the new scope,
    the employee has no current salary structure to revise, the package
    matches what they are already paid, or a proposal for the same date and
    amount is already on file (so re-running is safe).

    **Proposes, never applies.** The new row is ``PENDING`` and the employee
    keeps being paid their current figure until Finance approves it
    (``approve_salary_structure`` below). Automating the *arithmetic* of a
    go-live is the point; automating the decision to change someone's pay is
    not, and ``SalaryStructure.Status.PENDING`` already existed for exactly
    this with no flow using it.
    """
    from payroll.models import SalaryStructure
    from payroll.rates import resolve_team_salary_package

    employee = assignment.employee
    package = resolve_team_salary_package(
        on_date=assignment.effective_from,
        department=assignment.department or employee.department,
        team=assignment.team,
        designation=employee.designation,
        resource_status=assignment.resource_status,
    )
    if package is None:
        return None

    current = (
        SalaryStructure.objects
        .filter(employee=employee, status=SalaryStructure.Status.APPROVED, effective_to__isnull=True)
        .first()
    )
    # Nothing to revise. A brand-new employee's starting salary comes from
    # the compensation master (the HR file, via the bulk uploader), not from
    # here — proposing a package for everyone with no structure yet would put
    # a row in Finance's queue for every new joiner. If the figure they are
    # eventually given is off-package, the run says so
    # (SALARY_PACKAGE_MISMATCH).
    if current is None:
        return None
    if current.gross_monthly == package.gross_monthly:
        return None

    # gross_monthly is encrypted at rest, so the amount match is done here
    # rather than in the filter (a handful of pending rows at most).
    already = next(
        (
            row for row in SalaryStructure.objects.filter(
                employee=employee, status=SalaryStructure.Status.PENDING,
                effective_from=assignment.effective_from,
            )
            if row.gross_monthly == package.gross_monthly
        ),
        None,
    )
    if already is not None:
        return already

    return supersede_salary_structure(
        employee,
        gross_monthly=package.gross_monthly,
        effective_from=assignment.effective_from,
        reason=SalaryStructure.Reason.TRANSFER,
        actor=actor,
        status=SalaryStructure.Status.PENDING,
        note=f"Proposed from package: {package}",
    )


def approve_salary_structure(structure, actor=None):
    """Promote a PENDING SalaryStructure to APPROVED, closing whatever row it
    supersedes. False when it wasn't pending (already handled).

    The close happens here rather than at proposal time because a proposal
    must not change anyone's pay — see supersede_salary_structure.
    """
    from datetime import timedelta

    from django.utils import timezone

    from payroll.models import SalaryStructure

    if structure.status != SalaryStructure.Status.PENDING:
        return False

    with transaction.atomic():
        current = (
            SalaryStructure.objects
            .select_for_update()
            .filter(
                employee_id=structure.employee_id, status=SalaryStructure.Status.APPROVED,
                effective_to__isnull=True,
            )
            .exclude(pk=structure.pk)
            .first()
        )
        if current is not None:
            if current.effective_from == structure.effective_from:
                # Same-date correction — see supersede_salary_structure.
                current.effective_to = structure.effective_from
                current.status = SalaryStructure.Status.CANCELLED
                current.note = (
                    (current.note + " · " if current.note else "")
                    + f"Replaced by a same-date correction ({timezone.now():%Y-%m-%d})"
                )[:255]
            else:
                current.effective_to = structure.effective_from - timedelta(days=1)
                current.status = SalaryStructure.Status.SUPERSEDED
            current.updated_by = actor
            current.save()
            structure.revision_no = current.revision_no + 1

        structure.status = SalaryStructure.Status.APPROVED
        structure.processed_by = actor
        structure.processed_at = timezone.now()
        structure.updated_by = actor
        structure.save()
    return True


def reject_salary_structure(structure, actor=None, note: str = ""):
    """PENDING -> CANCELLED. Nothing else moves — the employee keeps being
    paid whatever they were already on."""
    from django.utils import timezone

    from payroll.models import SalaryStructure

    if structure.status != SalaryStructure.Status.PENDING:
        return False
    structure.status = SalaryStructure.Status.CANCELLED
    structure.processed_by = actor
    structure.processed_at = timezone.now()
    structure.updated_by = actor
    if note:
        structure.note = note[:255]
    structure.save()
    return True


# ---------------------------------------------------------------------------
# Tax declarations — the once-a-year allowable-investment snapshot
# ---------------------------------------------------------------------------

def gross_in_force(employee_ids, on) -> dict:
    """``{employee_id: gross_monthly}`` for the salary structure in force on
    ``on`` — APPROVED or SUPERSEDED, ``effective_from <= on`` and not yet
    ended. An employee with nothing in force that day but a structure
    starting *later* (a joiner whose first salary row post-dates the issue
    date) gets that first later row instead, so a July seed still covers
    August joiners already loaded. Two queries, never per employee; the
    money column is encrypted, so the "latest wins" pick happens in Python.
    """
    from payroll.models import SalaryStructure  # local: avoid import cycle at module load

    employee_ids = list(employee_ids)
    live_statuses = (SalaryStructure.Status.APPROVED, SalaryStructure.Status.SUPERSEDED)
    result: dict = {}
    current = (
        SalaryStructure.objects
        .filter(employee_id__in=employee_ids, status__in=live_statuses, effective_from__lte=on)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on))
        .order_by("effective_from")
    )
    for structure in current:
        result[structure.employee_id] = structure.gross_monthly  # later effective_from overwrites
    missing = [pk for pk in employee_ids if pk not in result]
    if missing:
        upcoming = (
            SalaryStructure.objects
            .filter(employee_id__in=missing, status__in=live_statuses, effective_from__gt=on)
            .order_by("-effective_from")
        )
        for structure in upcoming:
            result[structure.employee_id] = structure.gross_monthly  # earliest effective_from wins
    return result


def allowable_investment_for(employee_id, tax_year, on=None):
    """The allowable-investment snapshot for one employee, or ``None`` when
    they have no salary structure to project from. ``on`` defaults to today
    (the issue date), not the tax year's start — the figure is "taken once a
    year" at the moment the row is issued."""
    from django.utils import timezone

    from payroll.tax import allowable_investment

    gross = gross_in_force([employee_id], on or timezone.localdate()).get(employee_id)
    if gross is None:
        return None
    return allowable_investment(tax_year, gross)
