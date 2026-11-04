"""Leave module — policies, applications, balance ledger, encashment.

Composes with roster by writing `Shift` rows (LEAVE-category shift type)
on approval. The roster prefill helper preserves those rows so the grid
reflects approved leaves without any extra wiring.

Implementation reference: docs/LEAVE_POLICY_IMPLEMENTATION.md
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class _Auditable(models.Model):
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class LeavePolicy(_Auditable):
    """Rules that apply to a single LEAVE-category ShiftType.

    Managed by HR through the Django admin. One policy per leave type.
    All knobs match SEBPO Leave Policy v2.0 (see
    docs/LEAVE_POLICY_IMPLEMENTATION.md).
    """

    class Gender(models.TextChoices):
        ANY = "ANY", "Any"
        FEMALE = "FEMALE", "Female only"
        MALE = "MALE", "Male only"

    class Cycle(models.TextChoices):
        CALENDAR = "CALENDAR", "Calendar year (Jan–Dec)"
        ANNIVERSARY = "ANNIVERSARY", "From joining anniversary"

    class AccrualMethod(models.TextChoices):
        NONE = "NONE", "No accrual (fixed entitlement)"
        FIXED_ANNUAL = "FIXED_ANNUAL", "Fixed annual grant on Jan 1"
        WORKED_DAY_RATIO = "WORKED_DAY_RATIO", "1 day per N worked days"

    shift_type = models.OneToOneField(
        "roster.ShiftType",
        on_delete=models.CASCADE,
        related_name="leave_policy",
        limit_choices_to={"category": "LEAVE"},
    )

    # --- Entitlement & wage ---
    max_days_per_year = models.PositiveIntegerField(
        default=0,
        help_text=(
            "For FIXED_ANNUAL policies (CL, SL, MtL): the flat annual "
            "entitlement in days. For WORKED_DAY_RATIO policies (AL): the "
            "per-cycle cap on live-computed accrual (12 for AL) — accrual "
            "never exceeds this regardless of worked/paid-leave days. "
            "0 means uncapped (e.g. LWP, HdL, and AL before this cap existed)."
        ),
    )
    is_paid = models.BooleanField(
        default=True,
        help_text="Whether days off under this policy are paid. False for LWP.",
    )

    # --- Eligibility gates ---
    applies_to_gender = models.CharField(
        max_length=10, choices=Gender.choices, default=Gender.ANY,
        help_text="FEMALE for Maternity Leave; ANY for the rest.",
    )
    min_service_months = models.PositiveIntegerField(
        default=0,
        help_text="12 for Annual Leave; 6 for Maternity Leave; 0 for the rest.",
    )
    max_surviving_children = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Maternity: must have <= this many surviving children. 1 for MtL; null = no cap.",
    )

    # --- Pro-rating + cycle ---
    prorate_on_join = models.BooleanField(
        default=True,
        help_text="Pro-rate the entitlement for mid-year joiners. False for AL "
                  "(which has its own 12-month service gate instead).",
    )
    cycle = models.CharField(
        max_length=12, choices=Cycle.choices, default=Cycle.CALENDAR,
        help_text="ANNIVERSARY only for AL accrual eligibility.",
    )

    # --- Accrual ---
    accrual_method = models.CharField(
        max_length=20, choices=AccrualMethod.choices, default=AccrualMethod.NONE,
    )
    accrual_ratio_denominator = models.PositiveIntegerField(
        default=0,
        help_text="N in 1:N (1 day per N worked days). 18 for AL.",
    )

    # --- Sandwich rule ---
    bridges_sandwich = models.BooleanField(
        default=True,
        help_text="Whether holidays/weekends bracketed by this leave count "
                  "(sandwich rule). False for LWP and HdL — they don't sandwich.",
    )

    # --- Carry-forward + encashment ---
    carry_forward_pct = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("0"),
        help_text="Fraction (0–1) of unused entitlement that carries to next year. "
                  "0.50 for AL; 0 for the rest.",
    )
    max_carry_forward_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Cap on accumulated carry-forward. 60 for AL (triggers freeze).",
    )
    freezes_entitlement_at_cap = models.BooleanField(
        default=False,
        help_text="If True and carry_forward reaches max_carry_forward_days, the "
                  "employee's future accrual is frozen (LeaveBalance.entitlement_frozen).",
    )
    encash_carry_forward_excess = models.BooleanField(
        default=False,
        help_text="If True, year-end rollover auto-creates an EncashmentRequest "
                  "for unused balance not carried forward. True for AL.",
    )

    # --- Advance requests ---
    allow_advance_against_entitlement = models.BooleanField(
        default=False,
        help_text=(
            "If True, an application may exceed the live/currently-accrued "
            "balance and still be submitted + approved, so long as it fits "
            "within max_days_per_year + opening + carried_forward − used − "
            "encashed (i.e. what the employee could ever have this cycle/"
            "year, not just what's accrued/granted so far). The supervisor "
            "decides whether to approve within that ceiling; requests past "
            "it are still hard-blocked as arithmetically impossible. True "
            "for AL/CL/SL, revised 2026-09-10 — see "
            "docs/LEAVE_POLICY_IMPLEMENTATION.md."
        ),
    )

    # --- Documentation ---
    cert_required_min_days = models.PositiveIntegerField(
        default=0,
        help_text="0 = never required; >=1 = attachment required when "
                  "application.day_count >= this value. SL=3, MtL=1, others=0.",
    )

    # --- Compensatory leave behaviour ---
    cpl_grant_on_holiday_work = models.BooleanField(
        default=False,
        help_text="If True, a WORK Shift saved on a Holiday-tagged date "
                  "credits +1 day to this policy's balance for that employee. "
                  "True for CpL.",
    )
    cpl_use_within_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Working-day deadline to consume a granted CpL day. "
                  "0 or blank = grants never expire (CpL since 2026-10-01; "
                  "it was 3 before that).",
    )

    class Meta:
        verbose_name = "Leave policy"
        verbose_name_plural = "Leave policies"

    def __str__(self) -> str:
        return f"Policy for {self.shift_type}"

    @property
    def tracks_balance(self) -> bool:
        """Whether this policy needs a per-(employee, year) running balance.

        True for entitlement-tracking types:
            - Annual Leave (accrual-based)
            - Casual Leave / Sick Leave / Maternity Leave (fixed annual entitlement)
            - Compensatory Leave (signal-driven grants)

        False for types that have no quota (Festival Leave, Leave Without Pay,
        Half Day Leave) — those don't need a `LeaveBalance` row.
        """
        if not self.is_paid:
            return False  # LWP, HdL — salary deduction, no entitlement
        if self.max_days_per_year > 0:
            return True   # CL, SL, MtL
        if self.accrual_method != self.AccrualMethod.NONE:
            return True   # AL (WORKED_DAY_RATIO)
        if self.cpl_grant_on_holiday_work:
            return True   # CpL — accrued via signal
        return False      # FL — observed dates, no quota


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class LeaveApplication(_Auditable):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    class HalfDayPeriod(models.TextChoices):
        AM = "AM", "First half (AM)"
        PM = "PM", "Second half (PM)"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="leave_applications",
    )
    leave_type = models.ForeignKey(
        "roster.ShiftType",
        on_delete=models.PROTECT,
        limit_choices_to={"category": "LEAVE", "is_active": True},
        related_name="+",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    note = models.TextField(blank=True)
    attachment = models.FileField(upload_to="leave/", null=True, blank=True)

    # Half-day support — when True, start_date == end_date and the balance
    # debits 0.5 instead of 1.
    is_half_day = models.BooleanField(default=False)
    half_day_period = models.CharField(
        max_length=2, choices=HalfDayPeriod.choices, blank=True, default="",
    )

    # Sandwich-aware billing — populated at approval time by
    # leave/services.py. `working_days` and `sandwich_days` are kept as separate
    # columns for reporting; `effective_days` = working + sandwich and is what
    # gets debited from the balance.
    working_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    sandwich_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    effective_days = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    applied_at = models.DateTimeField(default=timezone.now)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    process_note = models.CharField(max_length=255, blank=True)

    # Set once, at LeaveApproveView approval time, when the live balance was
    # short but the request still fit inside allow_advance_against_entitlement's
    # ceiling — a supervisor knowingly approved an advance. Frozen at that
    # moment rather than recomputed later, since the ledger moves on and the
    # point-in-time shortfall can't be reconstructed after the fact.
    approved_over_balance = models.BooleanField(default=False)
    balance_shortfall_days = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="requested − available_before at approval time, only when "
                  "approved_over_balance is True.",
    )

    class Meta:
        # Ordered by the leave date itself, newest first — the list views all
        # read as "when was this person out", so the filing timestamp is a
        # poor primary sort. Kept on the model rather than per-view so the
        # tabs can't drift apart.
        ordering = ["-start_date", "-end_date", "-applied_at"]
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "start_date"]),
            # Matches Meta.ordering — unfiltered/loosely-filtered list views
            # would otherwise filesort the whole table.
            models.Index(fields=["-start_date", "-end_date"]),
            # Still earns its keep: the dashboard and profile cards explicitly
            # order by filing time to show "recently applied".
            models.Index(fields=["-applied_at"]),
        ]

    @property
    def day_count(self) -> int:
        """Calendar-day count (start..end inclusive). Use `effective_days` for billing."""
        return (self.end_date - self.start_date).days + 1

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == self.Status.APPROVED

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})
        if self.leave_type_id and self.leave_type.category != "LEAVE":
            raise ValidationError({"leave_type": "Must be a LEAVE-category shift type."})
        if self.is_half_day:
            if self.start_date != self.end_date:
                raise ValidationError(
                    {"end_date": "Half-day leave must start and end on the same date."}
                )
            if not self.half_day_period:
                raise ValidationError(
                    {"half_day_period": "Pick AM or PM for a half-day leave."}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.employee} · {self.leave_type} · {self.start_date}–{self.end_date}"


# ---------------------------------------------------------------------------
# Balance ledger
# ---------------------------------------------------------------------------

class LeaveBalance(_Auditable):
    """One row per (employee, leave_policy, year).

    `available` is computed: opening + accrued + carried_forward − used − encashed.

    Mutations always go through `LeaveBalanceTransaction` rows so the ledger
    is auditable. The fields here are running totals for fast reads.
    """

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="leave_balances",
    )
    leave_policy = models.ForeignKey(
        LeavePolicy, on_delete=models.PROTECT, related_name="balances",
    )
    year = models.PositiveIntegerField()

    opening = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    accrued = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    used = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    encashed = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    carried_forward = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))

    entitlement_frozen = models.BooleanField(
        default=False,
        help_text="Set when AL carry-forward reaches the 60-day cap. "
                  "Subsequent monthly accrual skips this row.",
    )

    # --- Anniversary-cycle window (ANNIVERSARY-cycle policies only, e.g. AL) ---
    # CALENDAR-cycle policies (CL/SL/...) also get these populated as
    # [Jan 1, Jan 1 next year) for uniform handling by cycle-close code, but
    # their behaviour is unchanged — only ANNIVERSARY policies actually key
    # off cycle_end instead of the calendar. See leave/services.py::
    # anniversary_cycle_window / get_or_create_balance.
    cycle_start = models.DateField(null=True, blank=True)
    cycle_end = models.DateField(
        null=True, blank=True,
        help_text="Exclusive upper bound of this row's cycle window.",
    )
    cycle_closed = models.BooleanField(
        default=False,
        help_text="Set once this row's cycle has been rolled over (carry-"
                  "forward computed, next cycle opened). Historical rows "
                  "predating the anniversary-cycle rework are marked closed "
                  "at backfill time and never reprocessed.",
    )

    class Meta:
        unique_together = [("employee", "leave_policy", "year")]
        indexes = [
            models.Index(fields=["employee", "year"]),
            models.Index(fields=["leave_policy", "year"]),
        ]
        ordering = ["-year", "leave_policy"]

    @property
    def available(self) -> Decimal:
        return (
            self.opening + self.accrued + self.carried_forward
            - self.used - self.encashed
        )

    def __str__(self) -> str:
        return f"{self.employee} · {self.leave_policy.shift_type.name} · {self.year}"


class LeaveBalanceTransaction(models.Model):
    """Append-only ledger of every change to a LeaveBalance.

    Reasons:
      - ACCRUAL: monthly accrual job credit, or initial fixed-annual grant
      - USED: leave application approved → balance debit
      - REVERSED: leave application cancelled → balance credit (undo of USED)
      - CARRIED_FORWARD: year-end rollover credit on next year's row
      - ENCASHED: encashment request paid → balance debit
      - FROZEN: marker that this balance was frozen at the cap (delta=0)
      - ADJUSTMENT: manual HR correction (delta signed) — also used for the
        one-time historical opening-balance import (leave/upload_bank.py)
    """

    class Reason(models.TextChoices):
        ACCRUAL = "ACCRUAL", "Accrual"
        USED = "USED", "Used"
        REVERSED = "REVERSED", "Reversed"
        CARRIED_FORWARD = "CARRIED_FORWARD", "Carried forward"
        ENCASHED = "ENCASHED", "Encashed"
        FROZEN = "FROZEN", "Frozen at cap"
        ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"

    balance = models.ForeignKey(
        LeaveBalance, on_delete=models.CASCADE, related_name="transactions",
    )
    occurred_at = models.DateTimeField(default=timezone.now)
    delta = models.DecimalField(
        max_digits=6, decimal_places=2,
        help_text="Signed change. Positive credits the balance; negative debits.",
    )
    reason = models.CharField(max_length=20, choices=Reason.choices)
    application = models.ForeignKey(
        LeaveApplication, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="balance_transactions",
    )
    # Set when this USED/REVERSED transaction was produced by reconciling a
    # roster Shift that never went through a LeaveApplication (supervisor set
    # the shift status directly). Mirrors `application` — mutually exclusive
    # in practice, both nullable. The partial unique constraint below is the
    # idempotency key for leave/services.py::reconcile_orphan_shift_usage —
    # a given Shift can only ever be reconciled once.
    shift = models.ForeignKey(
        "roster.Shift", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="leave_ledger_transactions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["balance", "-occurred_at"]),
            models.Index(fields=["reason"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["shift"],
                condition=models.Q(shift__isnull=False),
                name="uniq_leavebalancetransaction_shift",
            ),
        ]

    def __str__(self) -> str:
        sign = "+" if self.delta >= 0 else ""
        return f"{self.balance} · {sign}{self.delta} · {self.get_reason_display()}"


# ---------------------------------------------------------------------------
# Encashment
# ---------------------------------------------------------------------------

class EncashmentRequest(_Auditable):
    """Convert unused leave (typically AL) to a payout.

    Generated automatically by the year-end rollover for AL excess, or
    requested manually by the employee. Approved by HR. The actual money
    movement happens in payroll, outside Orbis.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="encashment_requests",
    )
    leave_policy = models.ForeignKey(
        LeavePolicy, on_delete=models.PROTECT, related_name="encashment_requests",
    )
    year = models.PositiveIntegerField()
    days_requested = models.DecimalField(max_digits=5, decimal_places=2)
    days_approved = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
    )

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
    )
    requested_at = models.DateTimeField(default=timezone.now)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["employee", "year"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return (
            f"Encashment · {self.employee} · {self.leave_policy.shift_type.name} "
            f"· {self.year} · {self.days_requested}d"
        )


# ---------------------------------------------------------------------------
# Compensatory leave history
# ---------------------------------------------------------------------------

class CplGrant(_Auditable):
    """One row per CpL day earned for working a government holiday: which
    holiday was worked, and which day (if any) it was taken off — the
    detail record behind the CpL ACCRUAL/USED `LeaveBalanceTransaction`
    pair, same relationship `LeaveApplication` already has to its own
    transactions. `LeaveBalance`/`LeaveBalanceTransaction` remain the
    source of truth for balance display and reports; this table exists so
    "who earned CpL for which holiday, and when they used it" is an actual
    queryable history instead of something inferred from ledger notes.

    Not every department earns CpL for holiday work the same way — see
    `accounts.permissions.effective_cpl_treatment` — so `source` records
    how this row came to exist:
      - AUTO: the WORK-on-Holiday signal/batch reconcile detected it
        (only for AUTO-treatment departments/teams).
      - UPLOAD: an HR file upload for a MANUAL-treatment department
        (e.g. Data Solutions & Due Diligence), which decides CpL vs.
        overtime per employee/instance outside Orbis.
      - MANUAL: a one-off HR/manager entry — today, always for
        `work_context=WEEKLY_OFF`/`OTHER` (see below); a `HOLIDAY`-context
        grant should go through the AUTO signal or the UPLOAD path instead,
        since those are the only paths that pin a real `holiday` FK.

    `work_context` records WHY a day counts as CpL-earning, independent of
    `source` (HOW the row was recorded):
      - HOLIDAY: worked a government-holiday-tagged date (`holiday` set).
      - WEEKLY_OFF: worked a date that's a scheduled weekly off day per the
        employee's own roster pattern, for an ad-hoc operational reason
        (e.g. preparing for a labour department audit) — confirmed as a
        real, recurring scenario distinct from holiday work. There is no
        signal for this yet (deliberately: telling "genuinely unscheduled
        extra work" apart from an ordinary roster swap isn't reliable from
        data alone), so these are always MANUAL today — `work_context` is
        still a first-class field, not inferred from `holiday` being null,
        so a future auto-detect signal (Shift vs. Roster pattern) can slot
        in later without a schema change.
      - OTHER: any other manually-approved reason (`note` explains it).

    Consumption can precede the work date itself — a department may grant
    CpL in advance within the same rostering week (confirmed against real
    Data Solutions data, e.g. worked a holiday on the 4th but already took
    the day off on the 31st of the prior month) — so there is deliberately
    no constraint requiring `consumed_date >= work_date`.

    Expiry (`LeavePolicy.cpl_use_within_days`) is intentionally NOT
    tracked here — that stays a live computation against the ledger
    (`leave/services.py::cpl_unexpired_balance`), independent of this
    model. An `OPEN` row here stays `OPEN` even once expired for balance
    purposes.
    """

    class Source(models.TextChoices):
        AUTO = "AUTO", "Auto-detected from roster"
        UPLOAD = "UPLOAD", "HR file upload"
        MANUAL = "MANUAL", "Manual HR/manager entry"

    class WorkContext(models.TextChoices):
        HOLIDAY = "HOLIDAY", "Worked a government holiday"
        WEEKLY_OFF = "WEEKLY_OFF", "Worked a scheduled weekly off day"
        OTHER = "OTHER", "Other (see note)"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open — not yet consumed"
        CONSUMED = "CONSUMED", "Consumed"
        FORFEITED = "FORFEITED", "Forfeited"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="cpl_grants",
    )
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.PROTECT, related_name="+",
        help_text="Snapshotted at grant time — stays accurate even if the "
                  "employee later transfers departments.",
    )
    work_date = models.DateField(
        help_text="The date actually worked that earned this CpL day — a "
                  "holiday date, or a scheduled weekly off day.",
    )
    work_context = models.CharField(
        max_length=10, choices=WorkContext.choices, default=WorkContext.HOLIDAY,
    )
    holiday = models.ForeignKey(
        "roster.Holiday", on_delete=models.PROTECT, related_name="cpl_grants",
        null=True, blank=True,
        help_text="Set when work_context=HOLIDAY; null for a worked "
                  "weekly-off day or other manual reason.",
    )
    work_shift = models.ForeignKey(
        "roster.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
        help_text="The WORK-category Shift on work_date, when known.",
    )
    source = models.CharField(max_length=10, choices=Source.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    consumption_shift = models.ForeignKey(
        "roster.Shift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
        help_text="The CpL-typed Shift the employee was actually taken off on.",
    )
    consumed_date = models.DateField(
        null=True, blank=True,
        help_text="May be recorded before a matching roster Shift exists "
                  "(e.g. straight from an HR upload).",
    )

    accrual_transaction = models.ForeignKey(
        LeaveBalanceTransaction, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    consumption_transaction = models.ForeignKey(
        LeaveBalanceTransaction, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-work_date"]
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["department", "status"]),
            models.Index(fields=["work_context"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["work_shift"],
                condition=models.Q(work_shift__isnull=False),
                name="uniq_cplgrant_work_shift",
            ),
            models.UniqueConstraint(
                fields=["consumption_shift"],
                condition=models.Q(consumption_shift__isnull=False),
                name="uniq_cplgrant_consumption_shift",
            ),
        ]

    def clean(self):
        if self.work_context == self.WorkContext.HOLIDAY and not self.holiday_id:
            raise ValidationError({"holiday": "Required when work context is 'Worked a government holiday'."})
        if self.work_context != self.WorkContext.HOLIDAY and self.holiday_id:
            raise ValidationError({"holiday": "Must be blank unless work context is 'Worked a government holiday'."})
        if self.holiday_id and self.holiday.date != self.work_date:
            raise ValidationError({"holiday": "Holiday date doesn't match work_date."})

    def __str__(self) -> str:
        return f"CpL · {self.employee} · worked {self.work_date} · {self.status}"


# ---------------------------------------------------------------------------
# Pre-Orbis annual-leave record (2026-10-05, docs/LEGACY_60_IMPORT_PLAN.md §4)
# ---------------------------------------------------------------------------

class LegacyAnnualLeaveRecord(_Auditable):
    """HR's pre-Orbis Annual Leave position for one employee, stored exactly
    as HR calculated it (the four columns of their "AL Calc" sheet) as at a
    cut-over date — a historical record for the Service Book, **not** a
    ledger credit.

    Deliberately never read by `LeaveBalance`, `live_available`, the balance
    page or leave validation: the live Orbis ledger runs on roster data from
    2026-01-01 alone (decision 3 in the import plan, accepted by the user).
    Shown only in the Service Book's Part 4 (HR + superuser), uploaded via
    `leave/upload_al_history.py`. Upsert on (employee, as_of) — a corrected
    re-upload replaces the row; `added_by`/`updated_by` carry the audit.
    """

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="legacy_al_records",
    )
    as_of = models.DateField(
        help_text="The date HR's figures are stated at — Orbis's own ledger takes over the next day.",
    )
    accrued_before_year = models.DecimalField(
        max_digits=6, decimal_places=2,
        help_text="AL accrued up to the end of the year before `as_of`'s year (HR: \"Till 2024\").",
    )
    taken_in_year = models.DecimalField(
        max_digits=6, decimal_places=2, help_text="AL taken during `as_of`'s year (HR: \"2025 AL Taken\").",
    )
    balance_in_year = models.DecimalField(
        max_digits=6, decimal_places=2, help_text="That year's entitlement minus taken (HR: \"2025 AL Balance\").",
    )
    capped_balance = models.DecimalField(
        max_digits=6, decimal_places=2,
        help_text="Total balance carried into Orbis after the 60-day lifetime cap (HR: \"60 Cap Till 2025\").",
    )
    note = models.CharField(max_length=255, blank=True)
    source_file = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = [("employee", "as_of")]
        ordering = ["employee", "-as_of"]
        verbose_name = "Pre-Orbis annual leave record"

    def __str__(self) -> str:
        return f"Pre-Orbis AL · {self.employee} · as of {self.as_of} · {self.capped_balance}"
