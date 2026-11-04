"""Payroll app — compensation master (Phase 1) and rate/input pipeline (Phase 2).

Implementation reference: docs/PAYROLL_PLAN.md (technical spec) and
docs/PAYROLL_PROCESS.md (business design, still awaiting formal HR/Finance
sign-off on the calculation engine — see PAYROLL_PLAN.md's status line).
Only Phase 1 (compensation master) and Phase 2 (rate tables + input
pipeline) are implemented so far; the calculation engine, PayrollRun and
everything downstream (Phase 3+) is not built yet.

Structural conventions copied from sibling apps rather than reinvented:
* ``_Auditable`` — local copy, same shape as leave/dynamic_menu/policies.
* ``PayrollSettings`` — singleton, mirrors documents.CompanyBranding.
* ``PayslipCounter`` — singleton counter, mirrors policies.PolicyCounter.
* Approval-ish fields (``status`` + ``processed_by`` + ``processed_at`` +
  a short note) mirror leave.LeaveApplication / EncashmentRequest.
* MySQL doesn't enforce partial-unique constraints (W036, already silenced
  project-wide in core/settings.py) — every partial-unique here has a
  ``clean()`` mirror, the offboarding.Resignation.clean() posture.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.fields import EncryptedCharField, EncryptedDecimalField, EncryptedJSONField


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
# Configuration
# ---------------------------------------------------------------------------

class PayrollSettings(models.Model):
    """One row. No gratuity field — SEBPO has no gratuity scheme (locked 2026-08-11)."""

    class PayableDaysBasis(models.TextChoices):
        FIXED_30 = "FIXED_30", "Fixed 30-day month"
        CALENDAR = "CALENDAR", "Actual days in period"

    class RoundingMode(models.TextChoices):
        ROUND_HALF_UP = "ROUND_HALF_UP", "Round half up"
        ROUND_DOWN = "ROUND_DOWN", "Round down"

    class PayslipDeliveryMode(models.TextChoices):
        EMAIL_BODY = "EMAIL_BODY", "Numbers in the email body only (no PDF/link)"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    period_start_day = models.PositiveSmallIntegerField(
        default=16, help_text="Day of month a payroll period opens on (16 -> 15 cycle).",
    )
    payable_days_basis = models.CharField(
        max_length=20, choices=PayableDaysBasis.choices, default=PayableDaysBasis.FIXED_30,
    )
    net_rounding_mode = models.CharField(
        max_length=20, choices=RoundingMode.choices, default=RoundingMode.ROUND_HALF_UP,
    )
    ot_rounding_minutes = models.PositiveSmallIntegerField(
        default=0, help_text="0 = no rounding; exact-second OT basis is unaffected.",
    )
    encashment_daily_basis = models.PositiveSmallIntegerField(
        default=30,
        help_text="Divisor for gross-based leave encashment (30 or 26 — PAYROLL_PROCESS.md §12 Q22, still open).",
    )
    payslip_delivery_mode = models.CharField(
        max_length=20, choices=PayslipDeliveryMode.choices, default=PayslipDeliveryMode.EMAIL_BODY,
    )
    notice_pay_basis = models.PositiveSmallIntegerField(default=30)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Payroll settings"
        verbose_name_plural = "Payroll settings"

    def save(self, *args, **kwargs):
        self.id = 1  # enforce singleton
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover — defensive
        raise ValidationError("PayrollSettings is a singleton; it cannot be deleted.")

    @classmethod
    def load(cls) -> "PayrollSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Payroll settings"


class PayslipCounter(models.Model):
    """Singleton sequence for payslip numbering. Bump under select_for_update — see services.py."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    last_value = models.PositiveIntegerField(default=0)

    def save(self, *args, **kwargs):
        self.id = 1  # enforce singleton
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Payslip counter (last: {self.last_value})"


class PayComponent(models.Model):
    """The catalogue of everything that can appear as a payslip line."""

    class Kind(models.TextChoices):
        EARNING = "EARNING", "Earning"
        DEDUCTION = "DEDUCTION", "Deduction"
        EMPLOYER_COST = "EMPLOYER_COST", "Employer cost"
        INFORMATIONAL = "INFORMATIONAL", "Informational"

    class EntryMode(models.TextChoices):
        MASTER = "MASTER", "Set on the compensation master"
        INPUT = "INPUT", "Entered per period"
        COMPUTED = "COMPUTED", "Engine-only, never entered"

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    short_name = models.CharField(max_length=30, blank=True, help_text="Payslip line label, if shorter than name.")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    entry_mode = models.CharField(max_length=10, choices=EntryMode.choices)
    is_taxable = models.BooleanField(default=True)
    is_prorated = models.BooleanField(default=False)
    affects_gross = models.BooleanField(default=True)
    sequence = models.PositiveIntegerField(default=0, help_text="Display order within its stage.")
    gl_code = models.CharField(max_length=30, blank=True)
    show_on_payslip = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "code"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------

class PayrollPeriod(models.Model):
    """One payroll cycle. Usually the standard 16th prev -> 15th cycle
    (``period_type=STANDARD``, one per (year, month), named after the month
    it closes in) but a run can instead be created for an arbitrary
    ``CUSTOM`` range — e.g. a 20-day Eid-month cycle — via ``for_range()``.
    Both kinds store their own resolved ``start_date``/``end_date`` and are
    otherwise interchangeable: the input-collection helpers in
    ``payroll/inputs.py`` (LWP/night/overtime) read ``start_date``/
    ``end_date`` directly rather than the ``period`` FK, so they work
    unchanged for a custom range. TDSEntry/VariablePayEntry rows *are*
    tied to `period` by FK, but both are only ever created against
    ``run.period`` (see ``PayrollRunEmployeeAdjustView``), never uploaded
    against a separately-chosen period, so that holds for custom ranges too.

    See ``payroll/periods.py`` for the 16->15 date math (``period_bounds`` /
    ``period_for_date``), used only for the STANDARD case.
    """

    class PeriodType(models.TextChoices):
        STANDARD = "STANDARD", "Standard (16th–15th)"
        CUSTOM = "CUSTOM", "Custom range"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open — inputs may be uploaded"
        CLOSED = "CLOSED", "Closed — inputs no longer accrue"

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    days_in_period = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    period_type = models.CharField(max_length=10, choices=PeriodType.choices, default=PeriodType.STANDARD)

    class Meta:
        ordering = ["-year", "-month", "-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["year", "month"],
                condition=models.Q(period_type="STANDARD"),
                name="payroll_period_unique_standard_year_month",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.start_date:%b %d} – {self.end_date:%b %d, %Y}"

    def clean(self):
        # MySQL doesn't enforce the partial-unique constraint above (mirrors
        # PayrollRun.clean()'s same note) — only STANDARD periods are keyed
        # by (year, month); CUSTOM ranges may share a nominal month.
        if self.end_date and self.start_date and self.end_date <= self.start_date:
            raise ValidationError("End date must be after start date.")
        if self.period_type == self.PeriodType.STANDARD:
            conflict_qs = PayrollPeriod.objects.filter(
                year=self.year, month=self.month, period_type=self.PeriodType.STANDARD,
            )
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError("A standard period for this year/month already exists.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def for_month(cls, year: int, month: int) -> "PayrollPeriod":
        from payroll.periods import period_bounds  # local import — avoid cycle at module load

        start_date, end_date = period_bounds(year, month)
        obj, _created = cls.objects.get_or_create(
            year=year, month=month, period_type=cls.PeriodType.STANDARD,
            defaults={
                "start_date": start_date,
                "end_date": end_date,
                "days_in_period": (end_date - start_date).days + 1,
            },
        )
        return obj

    @classmethod
    def for_range(cls, start_date, end_date) -> "PayrollPeriod":
        """Resolve/create a period for an explicit [start_date, end_date].

        Delegates to ``for_month`` (STANDARD) when the range is exactly a
        normal 16th->15th cycle, so picking the current or a past standard
        period through this same entry point doesn't spawn a duplicate
        CUSTOM row next to the canonical one. Anything else — a shorter or
        differently-aligned range, e.g. an Eid-month special run — becomes
        its own CUSTOM period, keyed by its own dates rather than (year,
        month) since more than one may fall in the same nominal month.
        """
        from payroll.periods import period_bounds, period_for_date

        if end_date <= start_date:
            raise ValidationError("End date must be after start date.")

        year, month = period_for_date(end_date)
        if (start_date, end_date) == period_bounds(year, month):
            return cls.for_month(year, month)

        obj, _created = cls.objects.get_or_create(
            start_date=start_date, end_date=end_date, period_type=cls.PeriodType.CUSTOM,
            defaults={
                "year": year, "month": month,
                "days_in_period": (end_date - start_date).days + 1,
            },
        )
        return obj


# ---------------------------------------------------------------------------
# Compensation master
# ---------------------------------------------------------------------------

class SalaryStructure(_Auditable):
    """Effective-dated gross salary. No component breakdown — locked 2026-08-11.

    Only one APPROVED, open-ended (``effective_to`` null) row per employee at
    a time — the "current" structure. MySQL doesn't enforce the partial
    unique below, so ``clean()`` mirrors it (offboarding.Resignation posture).
    """

    class Reason(models.TextChoices):
        NEW_HIRE = "NEW_HIRE", "New hire"
        CONFIRMATION = "CONFIRMATION", "Confirmation"
        ANNUAL_INCREMENT = "ANNUAL_INCREMENT", "Annual increment"
        PROMOTION = "PROMOTION", "Promotion"
        TRANSFER = "TRANSFER", "Transfer"
        CORRECTION = "CORRECTION", "Correction"
        DEMOTION = "DEMOTION", "Demotion"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="salary_structures",
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True, help_text="Blank = still current.")
    revision_no = models.PositiveIntegerField(default=1)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    gross_monthly = EncryptedDecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    batch = models.ForeignKey(
        "PayrollInputBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="salary_structure_entries",
    )

    class Meta:
        ordering = ["-effective_from"]
        indexes = [
            models.Index(fields=["employee", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(status="APPROVED", effective_to__isnull=True),
                name="payroll_one_current_salary_structure_per_employee",
            ),
        ]

    def __str__(self) -> str:
        # No amount: str(obj) lands in django_admin_log.object_repr and in
        # admin markup for every staff user (PAYROLL_DATA_PROTECTION_PLAN.md §5).
        return f"{self.employee} · rev {self.revision_no} from {self.effective_from}"

    def clean(self):
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Effective-to cannot precede effective-from."})
        # MySQL doesn't enforce the partial-unique constraint above, so we
        # re-check at the model layer. New/changed row: must not duplicate
        # another employee's current approved structure.
        if self.employee_id and self.status == self.Status.APPROVED and self.effective_to is None:
            conflict_qs = SalaryStructure.objects.filter(
                employee_id=self.employee_id, status=self.Status.APPROVED, effective_to__isnull=True,
            )
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError(
                    "This employee already has a current approved salary structure "
                    "— supersede it (set its effective_to) before approving a new one.",
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Bank accounts
# ---------------------------------------------------------------------------

class Bank(models.Model):
    """A disbursement bank. ``short_code`` is the bank-advice format key (Phase 5)."""

    name = models.CharField(max_length=150, unique=True)
    short_code = models.CharField(max_length=20, blank=True, help_text="e.g. BAL, EBL — Phase 5 advice format key.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class BankBranch(models.Model):
    bank = models.ForeignKey(Bank, on_delete=models.PROTECT, related_name="branches")
    name = models.CharField(max_length=150)
    routing_number = models.CharField(max_length=20, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["bank__name", "name"]

    def __str__(self) -> str:
        return f"{self.bank} · {self.name}"


class EmployeeBankAccount(_Auditable):
    """Lives in ``payroll`` (not ``employees``) so salary/bank access is gated
    independently of ``can_view_employee`` — but registers as an
    ``employees.SectionSpec`` so it renders in the profile drawer.
    """

    class AccountType(models.TextChoices):
        SAVINGS = "SAVINGS", "Savings"
        CURRENT = "CURRENT", "Current"
        MFS_BKASH = "MFS_BKASH", "bKash"
        MFS_NAGAD = "MFS_NAGAD", "Nagad"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bank_accounts",
    )
    bank = models.ForeignKey(
        Bank, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Blank for MFS wallets (bKash/Nagad).",
    )
    branch = models.ForeignKey(
        BankBranch, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    account_type = models.CharField(
        max_length=10, choices=AccountType.choices, default=AccountType.SAVINGS,
        help_text="Pick bKash or Nagad for a mobile wallet instead of a bank account — Bank/Branch aren't needed then.",
    )
    account_name = models.CharField(max_length=150)
    account_number = EncryptedCharField(
        max_length=50, help_text="For MFS wallets, this is the registered wallet phone number.",
    )
    routing_number = EncryptedCharField(
        max_length=20, blank=True, help_text="Denormalised from branch, snapshotted for record safety.",
    )
    is_primary = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Bank account"
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(is_primary=True),
                name="payroll_one_primary_bank_account_per_employee",
            ),
        ]

    def __str__(self) -> str:
        # Last four digits only — the full number is encrypted at rest.
        tail = (self.account_number or "")[-4:]
        return f"{self.employee} · {self.get_account_type_display()} · ····{tail}"

    def clean(self):
        if self.branch_id and self.routing_number and self.branch.routing_number != self.routing_number:
            raise ValidationError({"routing_number": "Must match the selected branch's routing number."})
        if self.branch_id and not self.routing_number:
            self.routing_number = self.branch.routing_number
        # MySQL doesn't enforce the partial-unique constraint above.
        if self.employee_id and self.is_primary:
            conflict_qs = EmployeeBankAccount.objects.filter(employee_id=self.employee_id, is_primary=True)
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError("This employee already has a primary bank account.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Rate tables (Phase 2)
# ---------------------------------------------------------------------------

class OvertimeType(models.Model):
    """Regular OT, GH OT, Eid Package, Special OT, ... — see PAYROLL_PROCESS.md §7."""

    class Basis(models.TextChoices):
        PER_HOUR = "PER_HOUR", "Per hour worked"
        FLAT_PER_OCCURRENCE = "FLAT_PER_OCCURRENCE", "Flat amount, regardless of hours"

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    aliases = models.JSONField(
        default=list, blank=True,
        help_text="Alternate spellings the upload importer matches case-insensitively, e.g. ['ROT', 'regular_ot'].",
    )
    basis = models.CharField(
        max_length=20, choices=Basis.choices,
        help_text="Fallback only — PayRate.basis, resolved per department, is the source of truth at calculation time.",
    )
    component = models.ForeignKey(PayComponent, on_delete=models.PROTECT, related_name="overtime_types")
    requires_holiday = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class PayRate(models.Model):
    """One table for OT, night-duty and transport rates (meal uses MealDeductionTier instead).

    Resolution is most-specific-scope-wins — see ``payroll/rates.py::resolve_rate()``.
    """

    class RateType(models.TextChoices):
        OVERTIME = "OVERTIME", "Overtime"
        NIGHT_REGULAR = "NIGHT_REGULAR", "Night duty — regular"
        NIGHT_QUARTERLY = "NIGHT_QUARTERLY", "Night duty — dedicated/quarterly"
        NIGHT_CUSTOM = "NIGHT_CUSTOM", "Night duty — custom shift"
        EID_COVERAGE = "EID_COVERAGE", "Eid coverage package"
        TRANSPORT_SPECIAL_SHIFT = "TRANSPORT_SPECIAL_SHIFT", "Special shift transport"
        # Priced here rather than as a constant so the figure is effective-dated
        # and can differ per team/department later, same as every other rate.
        # Resolved by the SLA uploader, not by the engine — the engine only ever
        # sees the resulting VariablePayEntry amount.
        SLA_INCENTIVE = "SLA_INCENTIVE", "SLA achievement incentive"
        # Data Solutions' "Test Salary": a DEDUCTION of the daily slice of an
        # employee's variable pay portion, for each day of a test window.
        # The portion is defined as 30 days of this rate (200 x 30 = 6,000),
        # which is also the per-period cap — see payroll/upload.py.
        TEST_SALARY_DAY = "TEST_SALARY_DAY", "Test salary deduction (per day)"

    class Basis(models.TextChoices):
        PER_HOUR = "PER_HOUR", "Per hour"
        PER_NIGHT = "PER_NIGHT", "Per night/day of duty"
        PER_DAY = "PER_DAY", "Per day"
        PER_MEAL = "PER_MEAL", "Per meal"
        PER_OCCURRENCE = "PER_OCCURRENCE", "Flat, per occurrence"
        GROSS_30TH_PER_DAY = "GROSS_30TH_PER_DAY", "Gross \u00f7 30 \u00d7 multiplier, per OT day"

    rate_type = models.CharField(max_length=30, choices=RateType.choices)
    overtime_type = models.ForeignKey(
        OvertimeType, on_delete=models.PROTECT, null=True, blank=True, related_name="rates",
        help_text="Only set when rate_type=OVERTIME.",
    )
    component = models.ForeignKey(PayComponent, on_delete=models.PROTECT, related_name="rates")

    # Scope — most specific match wins (see resolve_rate). All optional; a
    # row scoped to nothing but rate_type is the global fallback.
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    sub_project = models.ForeignKey(
        "accounts.SubProject", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    work_location = models.ForeignKey(
        "accounts.WorkLocation", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    grade = models.ForeignKey(
        "accounts.Grade", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    min_gross = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Salary-banded rates only (currently Software Development / Creative Development night duty).",
    )
    max_gross = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    resource_status = models.CharField(
        max_length=20, blank=True,
        help_text="employees.JobInfo.ResourceStatus value this row applies to (e.g. TRAINEE). "
                   "Blank = applies regardless of resource status. Lets one department carve out "
                   "an exception (e.g. Trainees earning no night allowance) as data, not code — "
                   "added 2026-09-07 for the AdOps night-shift policy.",
    )
    min_access_level = models.ForeignKey(
        "accounts.AccessLevel", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
        help_text="Floor, not a band: this row applies to employees at this access level or above "
                   "(by AccessLevel.rank, resolved the same way accounts.permissions._has_min_access_level "
                   "does). Blank = no floor. Added 2026-09-07 so 'Team Leader and above get a higher OT "
                   "rate' is a data row, not a grade lookup — AdOps has no accounts.Grade data but its "
                   "designations already map onto the existing AccessLevel ladder.",
    )

    basis = models.CharField(max_length=20, choices=Basis.choices)
    amount = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Taka, for every basis except GROSS_30TH_PER_DAY \u2014 where it is the "
                  "multiplier applied to the employee's own gross \u00f7 30 (e.g. 1.50 for "
                  "Software/Creative Development's holiday overtime).",
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["rate_type", "-effective_from"]
        indexes = [
            models.Index(fields=["rate_type", "is_active"]),
        ]

    def __str__(self) -> str:
        scope = " · ".join(
            str(v) for v in (
                self.department, self.team, self.sub_project, self.work_location, self.grade,
                self.min_access_level and f"{self.min_access_level}+",
                self.resource_status,
            ) if v
        ) or "global"
        return f"{self.get_rate_type_display()} · {scope} · {self.amount}"

    def clean(self):
        if self.rate_type == self.RateType.OVERTIME and not self.overtime_type_id:
            raise ValidationError({"overtime_type": "Required when rate type is Overtime."})
        if self.rate_type != self.RateType.OVERTIME and self.overtime_type_id:
            raise ValidationError({"overtime_type": "Only set this when rate type is Overtime."})
        if self.basis == self.Basis.GROSS_30TH_PER_DAY:
            # `amount` is a multiplier here, not taka — see the field's help_text.
            # Guard the easy mistake of pasting a taka figure in out of habit.
            if self.rate_type != self.RateType.OVERTIME:
                raise ValidationError({"basis": "This basis only applies to Overtime rates."})
            if self.amount is not None and not (Decimal("0") < self.amount <= Decimal("10")):
                raise ValidationError({
                    "amount": "For this basis, amount is a multiplier (e.g. 1.50), not a taka figure.",
                })
        if self.min_gross is not None and self.max_gross is not None and self.min_gross > self.max_gross:
            raise ValidationError({"max_gross": "Must be greater than min_gross."})
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Effective-to cannot precede effective-from."})


class MealDeductionTier(models.Model):
    """Tiered by the employee's own gross salary — locked 2026-08-11. Not a PayRate row."""

    min_gross = models.DecimalField(max_digits=12, decimal_places=2)
    max_gross = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Blank = no upper bound.")
    amount_per_meal = models.DecimalField(max_digits=8, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["min_gross"]

    def __str__(self) -> str:
        upper = f"–{self.max_gross}" if self.max_gross else "+"
        return f"{self.min_gross}{upper} → {self.amount_per_meal}/meal"

    def clean(self):
        if self.max_gross is not None and self.max_gross <= self.min_gross:
            raise ValidationError({"max_gross": "Must be greater than min_gross."})


class TeamSalaryPackage(models.Model):
    """The gross a given (department, team, resource status, designation)
    is paid — the compensation *policy* behind an employee's own
    ``SalaryStructure`` amount.

    Added 2026-09-19 (docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase B). Data
    Solutions' entire go-live policy is three numbers — Trainee 11,000,
    MARS 18,000, Item Review 16,000 — and before this they were encoded
    nowhere but in free-text Supervisor Notes, so Finance retyped them per
    employee with nothing to check the figure against.

    **This does not replace SalaryStructure and does not reopen the
    gross-only decision** (locked 2026-08-11). SalaryStructure remains the
    single source of what a person is actually paid; this says what the
    package *should* be, which is what lets a go-live propose a revision
    (``payroll.services.propose_salary_for_assignment``) and lets the engine
    flag a mismatch. An employee on an individually negotiated salary simply
    won't match their package, which is information, not an error.

    Scope resolution is most-specific-wins, sharing ``payroll/rates.py``'s
    scoring with PayRate rather than reimplementing it — a second copy of a
    resolution rule is how the engine and the report drifted apart once
    already (PAYROLL_PLAN.md Risk 2).
    """

    department = models.ForeignKey(
        "accounts.Department", on_delete=models.CASCADE, related_name="salary_packages",
    )
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
        help_text="Blank = applies to every team in the department.",
    )
    designation = models.ForeignKey(
        "accounts.Designation", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    resource_status = models.CharField(
        max_length=20, blank=True,
        help_text="employees.JobInfo.ResourceStatus value (e.g. TRAINEE). Blank = any.",
    )

    gross_monthly = EncryptedDecimalField(max_digits=12, decimal_places=2)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["department__name", "-effective_from"]
        indexes = [
            models.Index(fields=["department", "is_active"]),
        ]

    def __str__(self) -> str:
        scope = " · ".join(
            str(v) for v in (self.department, self.team, self.designation, self.resource_status) if v
        )
        return f"{scope} package from {self.effective_from}"

    def clean(self):
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Effective-to cannot precede effective-from."})
        if self.team_id and self.department_id and self.team.department_id != self.department_id:
            raise ValidationError({"team": "That team doesn't belong to the selected department."})


# ---------------------------------------------------------------------------
# Period inputs (Phase 2)
# ---------------------------------------------------------------------------

class PayrollInputBatch(_Auditable):
    """The upload/entry envelope — the audit trail roster/upload.py currently lacks."""

    class InputKind(models.TextChoices):
        OVERTIME = "OVERTIME", "Overtime"
        VARIABLE_PAY = "VARIABLE_PAY", "Bonus / incentive / disciplinary"
        TDS = "TDS", "TDS"
        TAX_OPENING = "TAX_OPENING", "Tax year openings (cutover)"
        SALARY_LOAN = "SALARY_LOAN", "Salary loans (cutover)"
        LEGACY_MONTH = "LEGACY_MONTH", "Pre-Orbis payroll months (cutover)"
        TAX_CHALLAN = "TAX_CHALLAN", "Tax challans"
        CHALLAN_ALLOC = "CHALLAN_ALLOC", "TDS by challan (per employee)"
        SALARY = "SALARY", "Gross salary"
        MEAL_COUNT = "MEAL_COUNT", "Meal count"

    class Source(models.TextChoices):
        UPLOAD = "UPLOAD", "File upload"
        MANUAL = "MANUAL", "Manual entry"
        API = "API", "External API pull"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        VALIDATED = "VALIDATED", "Validated"
        APPLIED = "APPLIED", "Applied"
        FAILED = "FAILED", "Failed"
        REVERSED = "REVERSED", "Reversed"

    input_kind = models.CharField(max_length=20, choices=InputKind.choices)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.UPLOAD)
    period = models.ForeignKey(
        PayrollPeriod, on_delete=models.PROTECT, null=True, blank=True, related_name="input_batches",
    )
    uploaded_file = models.FileField(upload_to="payroll/batches/", null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    row_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    report_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-added_at"]

    def __str__(self) -> str:
        return f"{self.get_input_kind_display()} batch · {self.added_at:%Y-%m-%d}"


class OvertimeEntry(_Auditable):
    class Status(models.TextChoices):
        # The two-step chain (2026-10-17, docs/OVERTIME_APPROVAL_CHAIN_PLAN.md):
        # PENDING ("Pending review") -> REVIEWED -> APPROVED, Reject from
        # either stage -> VOID. The PENDING *value* is kept from the Q55
        # single-step shape so existing rows and ?status=PENDING links keep
        # working; only its label changed.
        PENDING = "PENDING", "Pending review"
        REVIEWED = "REVIEWED", "Reviewed"
        APPROVED = "APPROVED", "Approved"
        VOID = "VOID", "Void"

    # Statuses the chain still has to act on — "unapproved, not cancelled".
    # Everything that counts pending overtime (run banners, control check,
    # bell) reads this rather than PENDING alone, or a Reviewed row would
    # drop out of every warning while still unpaid.
    UNAPPROVED_STATUSES = ("PENDING", "REVIEWED")

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="overtime_entries",
    )
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="overtime_entries")
    work_date = models.DateField()
    overtime_type = models.ForeignKey(OvertimeType, on_delete=models.PROTECT, related_name="entries")
    worked_in = models.ForeignKey(
        "accounts.Team", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text=(
            "Which team this overtime was actually worked for — payroll.PayRate rows can be "
            "team-scoped (e.g. Data Solutions' MARS team rates differ from its department "
            "default), so this drives rate resolution, not the employee's own department. "
            "Blank on write defaults to the employee's current CustomUser.team at that moment "
            "(snapshotted, not re-derived later); stays blank if the employee has no team. "
            "Added 2026-09-16 — see docs/PAYROLL_PLAN.md."
        ),
    )
    seconds = models.PositiveIntegerField()
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="overtime_entries",
    )
    # History of this field, oldest first:
    #   Q4 (2026-08-11): "no approval gate — a supervisor's upload is final;
    #   status exists only so HR can void/correct".
    #   Q55 (2026-09-15): partially reversed — a write into a LOCKED period
    #   lands PENDING and HR-Payroll promotes it.
    #   2026-10-17 (docs/OVERTIME_APPROVAL_CHAIN_PLAN.md): Q4 fully reversed.
    #   EVERY write — open or locked period, upload, drawer, or the GH
    #   auto-generator — lands PENDING. A Manager-tier person in the
    #   employee's managed scope (APM/PM, accounts.permissions::
    #   can_review_overtime) marks it REVIEWED; the department head, a
    #   department additional manager, HR-Payroll or a Payroll Admin
    #   (can_approve_overtime) marks it APPROVED, which is the only status
    #   the engine pays (payroll.inputs.collect_overtime). An approver may
    #   approve straight from PENDING; the review stamp is then theirs.
    #   payroll.services.OvertimeWindow still decides *whether* a date may
    #   be written; the status it hands back is now always PENDING.
    #
    # PENDING/REVIEWED are deliberately NOT reused VOID: VOID means "somebody
    # deliberately cancelled this", and collapsing them would lose the
    # difference between "needs a decision" and "already rejected".
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Review stamp (the APM/PM step). Cleared, together with the approval
    # stamp, whenever the entry's values change after the fact — see
    # reset_to_pending().
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Decision stamp: who approved (-> APPROVED) or rejected (-> VOID), and
    # when. Named processed_* since Q55; kept rather than renamed.
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-work_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "work_date", "overtime_type"], name="payroll_ot_entry_unique_per_day_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.overtime_type} · {self.work_date}"

    def clean(self):
        if self.work_date and self.period_id and not (self.period.start_date <= self.work_date <= self.period.end_date):
            raise ValidationError({"work_date": "Outside the selected period's date range."})

    # Fields whose change after a review/approval invalidates that decision.
    # Anything else (note, batch, period re-derivation) leaves the stamps.
    DECISION_FIELDS = ("seconds", "overtime_type_id", "worked_in_id", "work_date")

    @property
    def is_unapproved(self) -> bool:
        return self.status in self.UNAPPROVED_STATUSES

    def reset_to_pending(self) -> None:
        """Back to the start of the chain, clearing both stamps. Called by
        every edit path that changes a DECISION_FIELDS value on a row that
        has already been reviewed or approved — a sign-off covers the
        figures that were signed, not whatever they are changed to later.
        In-memory only; the caller saves."""
        self.status = self.Status.PENDING
        self.reviewed_by = None
        self.reviewed_at = None
        self.processed_by = None
        self.processed_at = None


class VariablePayEntry(_Auditable):
    """One table for performance bonus, incentive, SLA achievement pay and
    disciplinary adjustment.

    ``component.kind`` decides the sign. Manually-entered rows carry no
    unique constraint — one employee can legitimately have two incentives in
    a period — but a row written by a bulk uploader carries a
    ``source_key`` and IS constrained, so re-uploading the same sheet
    corrects rather than duplicates. See that field.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="variable_pay_entries",
    )
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="variable_pay_entries")
    component = models.ForeignKey(PayComponent, on_delete=models.PROTECT, related_name="variable_pay_entries")
    amount = EncryptedDecimalField(max_digits=10, decimal_places=2, help_text="Always a positive magnitude.")
    reason = models.CharField(max_length=255)
    attachment = models.FileField(upload_to="payroll/variable_pay/", null=True, blank=True)
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="variable_pay_entries",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    days = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Day-count inputs only (e.g. Test Salary). Blank for everything else.",
    )
    source_key = models.CharField(
        max_length=60, blank=True,
        help_text=(
            "Idempotency key for bulk-uploaded rows, e.g. 'SLA:2026-08' — the real-world "
            "event this row pays for, which is NOT the same as the period it is paid in. "
            "Blank for manual entries, which stay unconstrained. See the unique constraint."
        ),
    )

    class Meta:
        ordering = ["-added_at"]
        constraints = [
            # Bulk-uploaded rows only (source_key non-blank). Lets a supervisor
            # re-upload the same SLA month to correct it, while still allowing a
            # genuine retro claim alongside the current month — August and a
            # missed July land in the SAME period as two rows with different
            # source_keys, which is exactly the case the free-text Supervisor
            # Note could never distinguish from a double-payment.
            #
            # MySQL doesn't enforce partial uniques (W036, silenced project-wide)
            # so upload.py mirrors this by keying its update_conflicts on the same
            # four columns — the offboarding.Resignation.clean() posture.
            models.UniqueConstraint(
                fields=["employee", "period", "component", "source_key"],
                condition=~models.Q(source_key=""),
                name="payroll_variable_pay_unique_per_source_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.component} · {self.period}"


class TDSEntry(_Auditable):
    """Finance uploads computed TDS monthly — no in-system slab calculation yet (locked 2026-08-11)."""

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tds_entries",
    )
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="tds_entries")
    amount = EncryptedDecimalField(max_digits=10, decimal_places=2)
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="tds_entries",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-period__year", "-period__month"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "period"], name="payroll_tds_entry_unique_per_period"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.period}"


class MealCount(_Auditable):
    """Meal counts pulled from meals.sebpo.net (payroll/meals.py::pull_meal_counts)
    and persisted per period — the engine (payroll/inputs.py::collect_meal_counts)
    reads only these rows, never the live API, so a locked run's meal deduction
    stays reproducible on Recalculate and is captured by run.config_snapshot the
    same as every other input. ``total_meals`` is already deduction-eligible only
    (see payroll/meals.py's module docstring). This is the Phase 7 piece of
    docs/PAYROLL_PLAN.md's meals.sebpo.net note — pull_meal_counts is the one
    payroll input that deliberately hard-blocks INPUTS_LOCKED on failure rather
    than soft-warning, unlike the payroll report's use of the same client.
    """

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="meal_counts",
    )
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="meal_counts")
    total_meals = models.PositiveIntegerField()
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="meal_counts",
    )

    class Meta:
        ordering = ["-period__year", "-period__month"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "period"], name="payroll_meal_count_unique_per_period"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.period} · {self.total_meals} meals"


# ---------------------------------------------------------------------------
# Run & results (Phase 3)
#
# Only the calculation subset of the run lifecycle is implemented —
# DRAFT -> INPUTS_OPEN -> INPUTS_LOCKED -> CALCULATING -> CALCULATED (+
# reopen/recalculate). No approval chain, no lock/paid/reversal: those need
# PayrollRunApproval/PayrollRunDepartmentReview, which are Phase 5 and still
# gated on HR/Finance sign-off. The status choices below deliberately don't
# include states nothing transitions into yet — an unused choice is exactly
# the kind of silently-stale field this app has already been burned by once
# (see PAYROLL_PLAN.md Risk 2 and the Phase 2 is_night_shift revert).
# ---------------------------------------------------------------------------

ENGINE_VERSION = "1"


class PayrollRun(_Auditable):
    class RunType(models.TextChoices):
        REGULAR = "REGULAR", "Regular"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        INPUTS_OPEN = "INPUTS_OPEN", "Inputs open"
        INPUTS_LOCKED = "INPUTS_LOCKED", "Inputs locked"
        CALCULATING = "CALCULATING", "Calculating"
        CALCULATED = "CALCULATED", "Calculated"

    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="runs")
    run_type = models.CharField(max_length=20, choices=RunType.choices, default=RunType.REGULAR)
    name = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    employee_count = models.PositiveIntegerField(default=0)

    # Non-monetary rollups for the runs list (added 2026-09-16) — summed here
    # at calculate time from the same payroll_inputs.classify_night_shifts /
    # collect_overtime calls the money figures below are built from, so the
    # list page can show useful stats without exposing pay.
    night_regular_total = models.PositiveIntegerField(default=0)
    night_custom_total = models.PositiveIntegerField(default=0)
    night_dedicated_total = models.PositiveIntegerField(default=0)
    transport_total = models.PositiveIntegerField(default=0)
    ot_seconds_by_type_total = models.JSONField(default=dict, blank=True)

    gross_total = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    total_additions = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    total_deductions = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)
    net_total = EncryptedDecimalField(max_digits=14, decimal_places=2, default=0)

    # Salary adjustment instruction (docs/SALARY_ADJUSTMENT_PLAN.md): how
    # many more *months* of each employee's adjustment balance this run
    # settles — the Payroll Admin's call per payroll ("3 this time, then 3,
    # then the last 1"). Null/0 = none this run; settle_all = everything
    # still owed. The taka is derived by the engine from the compensation
    # master vs what each month actually paid, never typed.
    adjustment_months = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Months of salary adjustment to settle in this run (blank = none).",
    )
    adjustment_settle_all = models.BooleanField(
        default=False, help_text="Settle every month still owed in this run, whatever the count.",
    )
    engine_version = models.CharField(max_length=10, blank=True)
    config_snapshot = models.JSONField(default=dict, blank=True)
    calculated_at = models.DateTimeField(null=True, blank=True)

    rejection_history = models.JSONField(
        default=list, blank=True,
        help_text="Append-only list of {at, by_pk, by_email, stage, step_order, reason} dicts — "
                   "mirrors documents.DocumentRequest.rejection_history. Unused until Phase 5's approval chain.",
    )

    class Meta:
        ordering = ["-period__year", "-period__month", "-added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["period", "run_type"],
                condition=models.Q(status__in=["DRAFT", "INPUTS_OPEN", "INPUTS_LOCKED", "CALCULATING", "CALCULATED"]),
                name="payroll_one_active_run_per_period_type",
            ),
        ]

    def __str__(self) -> str:
        return self.name or f"{self.get_run_type_display()} · {self.period}"

    # --- "is this run still telling the truth?" (added 2026-09-15) --------
    #
    # Both are computed live at render rather than stored, because the failure
    # they exist to catch happens AFTER the last calculate: HR approves a
    # pending entry on a CALCULATED run and nobody re-runs it, so the
    # overtime silently never gets paid. A PayrollControlCheck cannot cover
    # that — calculate_run() regenerates checks, so one only ever describes
    # the state at the moment of the last calculation.

    @property
    def pending_overtime_count(self) -> int:
        """Overtime filed into this period and not yet approved — still
        awaiting review or awaiting the department head (both stages of
        the chain), excluding voided rows."""
        return OvertimeEntry.objects.filter(
            period_id=self.period_id, status__in=OvertimeEntry.UNAPPROVED_STATUSES,
        ).count()

    @property
    def overtime_changed_since_calculation(self) -> bool:
        """True when any overtime for this period was written after this run
        was last calculated — i.e. the register on screen is stale.

        False for a run that has never been calculated: there is no stale
        result to warn about yet.
        """
        if not self.calculated_at:
            return False
        return OvertimeEntry.objects.filter(
            period_id=self.period_id, updated_at__gt=self.calculated_at,
        ).exists()

    @property
    def adjustment_instruction_label(self) -> str:
        if self.adjustment_settle_all:
            return "all remaining months"
        if self.adjustment_months:
            return f"{self.adjustment_months} month{'s' if self.adjustment_months != 1 else ''}"
        return ""

    def clean(self):
        # MySQL doesn't enforce the partial-unique constraint above.
        if self.period_id:
            conflict_qs = PayrollRun.objects.filter(period_id=self.period_id, run_type=self.run_type)
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError("This period already has a run of this type.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def latest_rejection(self):
        return self.rejection_history[-1] if self.rejection_history else None

    def append_rejection(self, *, stage: str, step_order: int, reason: str, by) -> None:
        entry = {
            "at": timezone.now().isoformat(),
            "stage": stage,
            "step_order": step_order,
            "reason": reason or "",
            "by_pk": str(by.pk) if by else None,
            "by_email": getattr(by, "email", "") if by else "",
        }
        history = list(self.rejection_history or [])
        history.append(entry)
        self.rejection_history = history
        self.save(update_fields=["rejection_history", "updated_at"])


class PayrollRunEmployee(models.Model):
    """The register row. Every display field is a snapshot — FKs exist for
    traceability only and are never read for rendering."""

    class Status(models.TextChoices):
        INCLUDED = "INCLUDED", "Included"
        EXCLUDED = "EXCLUDED", "Excluded"
        HELD = "HELD", "Held"

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="run_employees")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    # org snapshot
    employee_code = models.CharField(max_length=50, blank=True)
    full_name = models.CharField(max_length=200, blank=True)
    department_name = models.CharField(max_length=150, blank=True)
    team_name = models.CharField(max_length=150, blank=True)
    designation_name = models.CharField(max_length=150, blank=True)

    # lifecycle
    joining_date = models.DateField(null=True, blank=True)
    last_working_date = models.DateField(null=True, blank=True)
    resource_status = models.CharField(max_length=20, blank=True)
    billing_status = models.CharField(max_length=20, blank=True)

    # bank (primary account snapshot, if any)
    bank_name = models.CharField(max_length=150, blank=True)
    bank_account_number = EncryptedCharField(max_length=50, blank=True)

    # attendance
    period_days = models.PositiveSmallIntegerField(default=0)
    eligible_days = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    lwp_days = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    payable_days = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    proration_factor = models.DecimalField(max_digits=8, decimal_places=6, default=0)

    # variable counts
    night_shift_count = models.PositiveSmallIntegerField(default=0)
    night_regular_count = models.PositiveSmallIntegerField(default=0)
    night_custom_count = models.PositiveSmallIntegerField(default=0)
    night_dedicated_count = models.PositiveSmallIntegerField(default=0)
    transport_count = models.PositiveSmallIntegerField(default=0)
    ot_seconds_total = models.PositiveIntegerField(default=0)
    ot_seconds_by_type = models.JSONField(default=dict, blank=True)

    # money
    gross_earnings = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    total_additions = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    total_deductions = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    taxable_gross = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    tax_deducted = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    net_pay = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    # "Did the engine pay this row anything?" (gross_earnings > 0) — the
    # register's "calculated" filter. Persisted because gross_earnings is
    # encrypted at rest and can't be compared in SQL (plan §4.4).
    has_pay = models.BooleanField(default=False)

    # Months of salary adjustment this run settled for the employee
    # (docs/SALARY_ADJUSTMENT_PLAN.md) — regenerated with the row on every
    # calculate; the engine sums it over earlier CALCULATED runs to know
    # where the next instalment starts. The taka is the SALARY_ADJUSTMENT line.
    adjustment_months = models.PositiveSmallIntegerField(default=0)

    # control
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.INCLUDED)
    exclusion_reason = models.CharField(max_length=255, blank=True)
    calc_warnings = models.JSONField(default=list, blank=True)
    inputs_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["employee_code"]
        constraints = [
            models.UniqueConstraint(fields=["run", "employee"], name="payroll_run_employee_unique_per_run"),
        ]
        indexes = [
            models.Index(fields=["run", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} · {self.run}"

    def save(self, *args, **kwargs):
        if getattr(self.run, "status", None) == PayrollRun.Status.CALCULATED and not kwargs.pop("_allow_locked_write", False):
            raise ValidationError("This run is calculated — recalculate the run instead of editing a row directly.")
        super().save(*args, **kwargs)


class PayrollRunLine(models.Model):
    run_employee = models.ForeignKey(PayrollRunEmployee, on_delete=models.CASCADE, related_name="lines")
    component = models.ForeignKey(PayComponent, on_delete=models.PROTECT, related_name="+")
    component_code = models.CharField(max_length=50, blank=True)
    component_name = models.CharField(max_length=150, blank=True)
    component_kind = models.CharField(max_length=20, blank=True)
    sequence = models.PositiveIntegerField(default=0)

    amount = EncryptedDecimalField(max_digits=12, decimal_places=2, help_text="Always a positive magnitude.")
    full_amount = EncryptedDecimalField(max_digits=12, decimal_places=2, help_text="Un-prorated amount.")
    quantity = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate = EncryptedDecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)

    source_kind = models.CharField(max_length=30, blank=True)
    source_ref = models.CharField(max_length=100, blank=True)
    # Free text since 2026-10-09 (was 255 chars): a salary adjustment line
    # carries its one-line summary plus the four-step breakdown HR and
    # Finance read, and nothing filters or sorts on it.
    calc_note = models.TextField(blank=True)

    class Meta:
        ordering = ["run_employee", "sequence"]

    def __str__(self) -> str:
        return f"{self.run_employee} · {self.component_code}"


class PayrollRunAudit(models.Model):
    """Append-only event log. SET_NULL-everywhere so rows outlive their subject — roster.ShiftChangeLog's posture."""

    run = models.ForeignKey(PayrollRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events")
    event = models.CharField(max_length=30)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    occurred_at = models.DateTimeField(default=timezone.now)
    detail_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at"]

    def __str__(self) -> str:
        return f"{self.event} · {self.occurred_at:%Y-%m-%d %H:%M}"


class PayrollControlCheck(models.Model):
    class Severity(models.TextChoices):
        BLOCKING = "BLOCKING", "Blocking"
        WARNING = "WARNING", "Warning"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"
        HELD = "HELD", "Held"

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="control_checks")
    check_code = models.CharField(max_length=50)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    message = models.CharField(max_length=255)
    detail_json = models.JSONField(default=dict, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-severity", "check_code"]
        indexes = [
            models.Index(fields=["run", "severity", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.check_code} · {self.message}"


class PayrollVariance(models.Model):
    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="variances")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    prev_net = EncryptedDecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    curr_net = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    delta = EncryptedDecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    delta_pct = EncryptedDecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    flagged = models.BooleanField(default=False)
    explanation = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "employee"], name="payroll_variance_unique_per_run"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.run}" + (" · flagged" if self.flagged else "")


# ---------------------------------------------------------------------------
# In-house tax/TDS calculation (Phase 4)
#
# Built from Finance's actual Tax Calculation.xlsx (ITA-2023 salary tax),
# confirmed 2026-08-24 — see docs/PAYROLL_PLAN.md §4. TDSEntry (above) stays
# as a manual override the engine checks first; this is the calculated
# fallback that replaced "TDS is uploaded, never calculated" (locked
# 2026-08-11, reopened 2026-08-24 per the user's own framing: "we don't need
# to upload tds from an excel anymore").
# ---------------------------------------------------------------------------

class TaxYear(models.Model):
    """BD tax year runs 1 July – 30 June. One row per year; ``is_active`` picks
    the row the engine resolves against for a given payroll period.
    """

    class CalculationMethod(models.TextChoices):
        ITA_2023_SALARY = "ITA_2023_SALARY", "ITA 2023 — salary income (flat ÷ 12)"
        # 2026-10-07, docs/TAX_CALCULATION_TRUEUP_PLAN.md: the year's taxable
        # extra pay enters the projection and the monthly deduction is
        # (remaining tax − TDS already deducted) ÷ months left.
        ITA_2023_SALARY_TRUEUP = "ITA_2023_SALARY_TRUEUP", "ITA 2023 — salary income, year-to-date true-up"

    code = models.CharField(max_length=10, unique=True, help_text="e.g. '2026-27'.")
    start_date = models.DateField()
    end_date = models.DateField()
    calculation_method = models.CharField(
        max_length=30, choices=CalculationMethod.choices, default=CalculationMethod.ITA_2023_SALARY,
        help_text="A future Finance Act that changes the formula's shape lands as a new tax.py "
                   "branch keyed by this field — never an edit to a branch historical runs depend on.",
    )
    exemption_divisor = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("3"),
        help_text="Annual gross is divided by this to get the flat income exemption (currently 1/3).",
    )
    exemption_cap = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("500000"))
    minimum_tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("5000"))
    rebate_income_pct = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal("0.0300"), help_text="Fraction, e.g. 0.0300 = 3%.",
    )
    rebate_investment_pct = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.1000"))
    rebate_cap = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("750000"))
    allowable_investment_pct = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal("0.3000"),
        help_text="Fraction of taxable income an employee may count as rebate-eligible investment "
                  "(Finance's 'Allowable Investment' line — 0.3000 = 30%).",
    )
    declaration_deadline = models.DateField(
        null=True, blank=True,
        help_text="After this date every unconfirmed declaration for the year locks on its defaults, "
                  "so the rebate basis can't move once payroll is running. Blank = no cut-off.",
    )
    orbis_live_from = models.ForeignKey(
        "PayrollPeriod", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="First period of this tax year whose TDS Orbis is authoritative for. Earlier months "
                  "come from each declaration's opening figures (cutover upload). Blank = every month.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self) -> str:
        return self.code

    def clean(self):
        if self.end_date and self.start_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": "Must be after start_date."})


class TaxCategory(models.Model):
    """Stable across years — the threshold *amount* per category varies year to
    year, hence it lives on TaxExemptionThreshold rather than as a field here.
    """

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    sequence = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "code"]
        verbose_name_plural = "Tax categories"

    def __str__(self) -> str:
        return self.name


class TaxExemptionThreshold(models.Model):
    """The 0%-band ceiling for one category in one tax year."""

    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="exemption_thresholds")
    category = models.ForeignKey(TaxCategory, on_delete=models.PROTECT, related_name="exemption_thresholds")
    threshold_amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tax_year", "category"], name="payroll_tax_threshold_unique_per_year_category"),
        ]

    def __str__(self) -> str:
        return f"{self.tax_year} · {self.category} · {self.threshold_amount}"


class TaxSlab(models.Model):
    """Progressive brackets above the category's exemption threshold — identical
    across every category; only the 0%-band width (TaxExemptionThreshold) varies.
    """

    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="slabs")
    sequence = models.PositiveIntegerField()
    width = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Blank = remaining income (the last bracket).",
    )
    rate = models.DecimalField(max_digits=6, decimal_places=4, help_text="Fraction, e.g. 0.1000 = 10%.")

    class Meta:
        ordering = ["tax_year", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["tax_year", "sequence"], name="payroll_tax_slab_unique_sequence_per_year"),
        ]

    def __str__(self) -> str:
        width = f"{self.width}" if self.width is not None else "remaining"
        return f"{self.tax_year} · #{self.sequence} · {width} @ {self.rate}"


def tax_declaration_document_path(instance, filename):
    """``media/payroll/tax_declarations/<employee_id>/<uuid>.<ext>`` — a
    random name, never the uploader's. ``/media/payroll/`` is 404 for everyone
    at the generic media view (media_store/rules.py); the only link to the
    file is the permission-checked ``payroll_tax_declaration_document`` view.
    """
    ext = Path(filename).suffix.lower()[:10]
    return f"payroll/tax_declarations/{instance.employee_id}/{uuid.uuid4().hex}{ext}"


class EmployeeTaxDeclaration(_Auditable):
    """One row per employee per tax year — tax category, the year's
    investment commitment and any advance tax already paid.

    **Revised 2026-10-07 with Finance (reverses question 41's "real
    itemised declarations").** Finance's working template assumes every
    employee invests the full *Allowable Investment* (30% of taxable income,
    ``TaxYear.allowable_investment_pct``), so the rebate is simply 3% of
    taxable income unless someone commits to less. Orbis now mirrors that:

    * ``allowable_investment`` is a **snapshot** taken when the year's row is
      issued (``seed_tax_declarations`` each July, or ``save()`` for a row HR
      adds by hand) from the gross in force at that moment — it is never
      recomputed, because the rebate basis must not move mid-year.
    * ``actual_investment`` is what the employee commits to invest. It
      defaults to the allowable figure; the employee may lower it (never
      raise it above the allowable) exactly once, then the row locks.
    * ``ait_car_advance_tax`` is Finance's "AIT (Car & Adv. Tax)" line —
      advance income tax already paid, which reduces the TDS Orbis deducts.
      A non-zero figure requires ``ait_document`` (the payment evidence).
    * ``submitted_at``/``submitted_by``/``submission_source`` record the
      employee's confirmation. A confirmed row is **locked** for everyone in
      the UI (``is_locked``); so is any row once the year's
      ``TaxYear.declaration_deadline`` has passed. The Django admin
      (superuser / Payroll Admins) is the deliberate escape hatch. FY2026-27
      was collected outside Orbis, so its rows are stamped
      ``OUTSIDE_ORBIS`` by ``seed_tax_declarations --mark-agreed-outside``.

    The six itemised ``investment_*`` columns were dropped in the same change
    (every row on production was zero).

    ``category`` auto-defaults from ``employees.PersonalInfo.gender`` when
    left blank (reopened 2026-08-24 — question 40's original "manual entry
    only" now covers only the three categories gender can't determine).
    Male -> Men, Female -> Women & Senior Citizen — see ``clean()``. This is
    a default, never a lock: a senior male citizen (65+) also qualifies for
    the higher Women & Senior Citizen threshold, and Disabled & 3rd gender /
    Freedom Fighter / Parents of a disabled child have no data source in
    Orbis yet, so HR can always override the picked category by hand.
    """

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tax_declarations",
    )
    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="declarations")
    category = models.ForeignKey(
        TaxCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="declarations",
        help_text="Blank until HR/Finance sets it — the engine treats a missing category as no declaration.",
    )

    class SubmissionSource(models.TextChoices):
        IN_ORBIS = "IN_ORBIS", "Confirmed in Orbis"
        OUTSIDE_ORBIS = "OUTSIDE_ORBIS", "Agreed outside Orbis"

    allowable_investment = EncryptedDecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Snapshot of allowable_investment_pct × projected taxable income, taken when the "
                  "row was issued. Blank only when no salary structure existed at that point.",
    )
    actual_investment = EncryptedDecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="What the employee commits to invest this tax year. Defaults to the allowable "
                  "figure; may be lowered, never raised above it.",
    )

    source_tax = EncryptedDecimalField(
        max_digits=12, decimal_places=2, default=0, blank=True,
        help_text="Tax already deducted elsewhere, e.g. bank interest.",
    )
    ait_car_advance_tax = EncryptedDecimalField(
        max_digits=12, decimal_places=2, default=0, blank=True,
        verbose_name="AIT (Car & Adv. Tax)",
        help_text="Advance income tax already paid this year (e.g. on a car). "
                  "Reduces the TDS Orbis deducts; needs the payment document.",
    )
    ait_document = models.FileField(
        upload_to=tax_declaration_document_path, null=True, blank=True,
        verbose_name="AIT document",
        help_text="Evidence of the advance tax payment (PDF or image). Required when AIT is above zero.",
    )
    note = models.CharField(max_length=255, blank=True)

    # Cutover facts for the months of this tax year before TaxYear.orbis_live_from
    # (Finance-supplied, uploaded; never recomputed; outside the employee's
    # confirm-and-lock, so still editable after submission).
    opening_tds_deducted = EncryptedDecimalField(
        max_digits=12, decimal_places=2, default=0, blank=True,
        help_text="TDS deducted outside Orbis for this tax year's months before orbis_live_from.",
    )
    opening_additional_pay = EncryptedDecimalField(
        max_digits=12, decimal_places=2, default=0, blank=True,
        help_text="Taxable extra pay (OT, allowances, bonuses other than festival) in those same months. "
                  "0 when Finance could only supply the TDS figure.",
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="The employee, or the Payroll-HR person who confirmed on their behalf.",
    )
    submission_source = models.CharField(
        max_length=20, choices=SubmissionSource.choices, blank=True, default="",
    )

    class Meta:
        ordering = ["-tax_year__start_date"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "tax_year"], name="payroll_tax_declaration_unique_per_employee_year"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.tax_year}"

    # -- state -------------------------------------------------------------

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None

    @property
    def is_past_deadline(self) -> bool:
        deadline = self.tax_year.declaration_deadline if self.tax_year_id else None
        return bool(deadline and timezone.localdate() > deadline)

    @property
    def is_locked(self) -> bool:
        """Confirmed by the employee, or the year's declaration window has
        closed. Nothing in the UI may change a locked row; the Django admin
        is the only way back (clear ``submitted_at``)."""
        return self.is_submitted or self.is_past_deadline

    @property
    def submitted_on_behalf(self) -> bool:
        return self.is_submitted and self.submitted_by_id is not None and self.submitted_by_id != self.employee_id

    @property
    def declared_investment(self) -> Decimal:
        """What the rebate is computed on: the employee's commitment, falling
        back to the allowable snapshot for a row nobody has touched yet, and
        0 when the row never got an allowable figure (no salary structure)."""
        if self.actual_investment is not None:
            return self.actual_investment
        return self.allowable_investment if self.allowable_investment is not None else Decimal("0")

    def mark_submitted(self, actor, *, source: str = SubmissionSource.IN_ORBIS, when=None) -> None:
        self.submitted_at = when or timezone.now()
        self.submitted_by = actor
        self.submission_source = source

    # -- validation ----------------------------------------------------------

    def clean(self):
        # Auto-default from gender when left blank. String literals, not an
        # employees.PersonalInfo.Gender import — payroll stays decoupled
        # from employees' model internals, same posture as EmployeeBankAccount.
        if self.category_id is None and self.employee_id:
            personal_info = getattr(self.employee, "personal_info", None)
            gender = personal_info.gender if personal_info else ""
            if gender == "F":
                self.category = TaxCategory.objects.filter(code="WOMEN_SENIOR").first()
            elif gender == "M":
                self.category = TaxCategory.objects.filter(code="MEN").first()

        errors = {}
        if self.actual_investment is not None:
            if self.actual_investment < 0:
                errors["actual_investment"] = "Can't be negative."
            elif self.allowable_investment is not None and self.actual_investment > self.allowable_investment:
                errors["actual_investment"] = (
                    f"Can't exceed the allowable investment of Tk {self.allowable_investment:,.0f}."
                )
        if (self.ait_car_advance_tax or 0) < 0:
            errors["ait_car_advance_tax"] = "Can't be negative."
        elif (self.ait_car_advance_tax or 0) > 0 and not self.ait_document:
            errors["ait_document"] = "Upload the advance-tax payment document when AIT is above zero."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Issue the allowable figure the first time a row is saved without
        # one (HR adding a mid-year joiner by hand). seed_tax_declarations
        # does the same in bulk; neither path ever overwrites an existing
        # snapshot — that's the whole point of it being a snapshot.
        if self.allowable_investment is None and self.employee_id and self.tax_year_id:
            from payroll.services import allowable_investment_for  # local: services imports models
            self.allowable_investment = allowable_investment_for(self.employee_id, self.tax_year)
        if self.actual_investment is None and self.allowable_investment is not None:
            self.actual_investment = self.allowable_investment
        # full_clean() here (not just via ModelForm) because the gender
        # default needs `employee` set, which the SectionEditView only does
        # *after* form.is_valid()'s own full_clean() already ran.
        self.full_clean()
        super().save(*args, **kwargs)


class TaxComputation(models.Model):
    """The calculated breakdown, snapshotted per run — deleted and rebuilt every
    recalculate, same posture as PayrollRunEmployee/Line. ``run``/``employee``
    form a natural key, so PayrollRunLine.source_ref can address a row as
    ``f"TaxComputation:{run.pk}:{employee.pk}"`` without needing its auto pk —
    sidesteps the MySQL bulk_create-doesn't-return-pks issue entirely.
    """

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="tax_computations")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="+")
    category_name = models.CharField(max_length=100, blank=True)

    gross_annual_projected = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    exempted_income = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    taxable_income = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    tax_before_rebate = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    rebate_amount = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    net_tax_payable = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    source_tax = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    ait_car_advance_tax = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    annual_tds = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    monthly_tds = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    # Year-to-date true-up inputs (TRUEUP method; zero / 12 under the flat method).
    additional_pay_ytd = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    tds_already_deducted = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    tax_remaining = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    months_remaining = models.PositiveSmallIntegerField(default=12)
    breakdown_json = EncryptedJSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-run__added_at", "employee_id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "employee"], name="payroll_tax_computation_unique_per_run"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.run}"


class PayrollRunEmployeeNote(_Auditable):
    """One free-text note per (run, employee, role) — Supervisor, HR, or
    Finance can each leave their own note on an employee for a given run.

    FK's to ``PayrollRun`` (never ``PayrollRunEmployee``): calculate_run()
    deletes and rebuilds every PayrollRunEmployee row on every Recalculate
    with no PK continuity (see PayrollRunEmployeeAdjustView's docstring),
    but the parent PayrollRun row itself is only ever updated, never
    replaced — so a note keyed off it survives every Recalculate.

    Visibility: anyone who can reach the run notes page sees all three
    notes; write access is per-role (accounts.permissions::
    can_write_payroll_note) — Supervisor via managed-scope, HR/Finance via
    the "Payroll"/"Payroll Admins" groups.
    """

    class Role(models.TextChoices):
        SUPERVISOR = "SUPERVISOR", "Supervisor"
        HR = "HR", "HR"
        FINANCE = "FINANCE", "Finance"

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="employee_notes")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=12, choices=Role.choices)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["role"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "employee", "role"],
                name="payroll_one_note_per_role_per_employee_per_run",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_role_display()} note · {self.employee} · {self.run}"


# ---------------------------------------------------------------------------
# Salary loans (docs/SALARY_LOAN_PLAN.md, 2026-10-08)
#
# HR records a management-approved loan from the paper file note, Finance
# marks it disbursed, and the engine recovers it one instalment per
# calculated run until the balance is zero. Repayments are NOT a ledger here:
# "recovered so far" is read from the LOAN_REPAYMENT lines of CALCULATED runs
# (payroll/loans.py), exactly as the TDS true-up reads "already deducted" —
# calculate_run() is delete-and-rebuild, so a ledger row written at calculate
# time would be duplicated by every Recalculate. Only non-payroll events
# (prepayment in cash, waiver, correction) are rows: SalaryLoanAdjustment.
# "Settled" is derived (outstanding <= 0), never a status.
# ---------------------------------------------------------------------------

def salary_loan_document_path(instance, filename):
    """Random filename under the employee's folder — see
    tax_declaration_document_path."""
    ext = Path(filename).suffix.lower()[:10]
    return f"payroll/salary_loans/{instance.employee_id}/{uuid.uuid4().hex}{ext}"


class SalaryLoan(_Auditable):
    class Status(models.TextChoices):
        PENDING_DISBURSEMENT = "PENDING_DISBURSEMENT", "Awaiting disbursement"
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Cancelled"

    class RecordedSource(models.TextChoices):
        IN_ORBIS = "IN_ORBIS", "Recorded in Orbis"
        OUTSIDE_ORBIS = "OUTSIDE_ORBIS", "Cutover (recorded before Orbis)"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="salary_loans",
    )
    file_reference = models.CharField(
        max_length=50, unique=True, help_text="HR's file note reference, e.g. HR/21092026/01.",
    )
    approved_on = models.DateField(help_text="Date on the approved file note.")
    approval_document = models.FileField(
        upload_to=salary_loan_document_path, null=True, blank=True,
        help_text="Scan of the signed file note. Required for loans recorded in Orbis.",
    )
    recorded_source = models.CharField(
        max_length=15, choices=RecordedSource.choices, default=RecordedSource.IN_ORBIS,
    )
    principal = EncryptedDecimalField(max_digits=12, decimal_places=2)
    installment_count = models.PositiveSmallIntegerField(help_text="Agreed number of monthly instalments.")
    installment_amount = EncryptedDecimalField(
        max_digits=10, decimal_places=2, help_text="Agreed monthly deduction; the last one is whatever remains.",
    )
    first_deduction_period = models.ForeignKey(
        PayrollPeriod, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text=(
            "First payroll the instalment comes out of. Blank until disbursement, when it defaults to "
            "the payroll named for the month after the disbursement month (3 Mar → Mar 16–Apr 15)."
        ),
    )
    opening_recovered = EncryptedDecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Cutover only — instalments Finance already recovered before Orbis.",
    )
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.PENDING_DISBURSEMENT)
    disbursed_on = models.DateField(null=True, blank=True)
    disbursed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    disbursement_note = models.CharField(max_length=255, blank=True)
    cancelled_reason = models.CharField(max_length=255, blank=True)
    note = models.TextField(blank=True, help_text="The reason on the file note.")

    class Meta:
        ordering = ["-approved_on", "-id"]

    def __str__(self) -> str:
        return f"Loan {self.file_reference} · {self.employee}"

    # ---- state ---------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING_DISBURSEMENT

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    @property
    def needs_document(self) -> bool:
        return not self.approval_document and not self.is_cancelled

    def clean(self):
        errors = {}
        principal = self.principal if self.principal is not None else Decimal("0")
        if principal <= 0:
            errors["principal"] = "Principal must be greater than zero."
        if self.installment_count is not None and self.installment_count < 1:
            errors["installment_count"] = "At least one instalment."
        if self.installment_amount is not None:
            if self.installment_amount <= 0:
                errors["installment_amount"] = "Instalment must be greater than zero."
            elif self.installment_amount > principal:
                errors["installment_amount"] = "An instalment can't exceed the principal."
        if (self.opening_recovered or Decimal("0")) > principal:
            errors["opening_recovered"] = "Recovered before Orbis can't exceed the principal."
        if (self.opening_recovered or Decimal("0")) < 0:
            errors["opening_recovered"] = "Recovered before Orbis can't be negative."
        if self.recorded_source == self.RecordedSource.IN_ORBIS and not self.approval_document and not self.is_cancelled:
            errors["approval_document"] = "Attach the signed file note."
        if self.is_active:
            if not self.disbursed_on:
                errors["disbursed_on"] = "An active loan needs its disbursement date."
            if not self.first_deduction_period_id:
                errors["first_deduction_period"] = "An active loan needs its first deduction period."
        if errors:
            raise ValidationError(errors)

    @staticmethod
    def default_installment(principal, count) -> Decimal:
        """ceil(principal / count) in whole taka — 50,000 / 12 → 4,167."""
        from decimal import ROUND_CEILING
        principal = Decimal(principal or 0)
        count = int(count or 0)
        if count < 1 or principal <= 0:
            return Decimal("0")
        return (principal / count).quantize(Decimal("1"), rounding=ROUND_CEILING)

    def mark_disbursed(self, actor, on, *, note=""):
        """PENDING_DISBURSEMENT -> ACTIVE. Defaults first_deduction_period to
        the payroll named for the month after ``on``'s (Q71 — payroll.periods
        .first_loan_deduction_month) when HR left it blank."""
        if not self.is_pending:
            raise ValidationError("Only a loan awaiting disbursement can be marked disbursed.")
        from payroll.periods import first_loan_deduction_month  # local import — avoid cycle at module load
        self.disbursed_on = on
        self.disbursed_by = actor
        self.disbursement_note = note or ""
        if self.first_deduction_period_id is None:
            self.first_deduction_period = PayrollPeriod.for_month(*first_loan_deduction_month(on))
        self.status = self.Status.ACTIVE
        self.updated_by = actor

    def cancel(self, actor, reason):
        if not self.is_pending:
            raise ValidationError("Only a loan awaiting disbursement can be cancelled — an active loan is waived instead.")
        self.status = self.Status.CANCELLED
        self.cancelled_reason = reason
        self.updated_by = actor


class SalaryLoanAdjustment(models.Model):
    """Append-only. Everything that changes a loan's balance OUTSIDE payroll —
    the shape of leave.LeaveBalanceTransaction minus the instalment reason
    (instalments live on the register, see SalaryLoan). Positive ``delta``
    reduces what is owed; a CORRECTION may go either way."""

    class Reason(models.TextChoices):
        PREPAYMENT = "PREPAYMENT", "Repaid outside payroll"
        WAIVER = "WAIVER", "Waived by management"
        CORRECTION = "CORRECTION", "Correction"

    loan = models.ForeignKey(SalaryLoan, on_delete=models.CASCADE, related_name="adjustments")
    occurred_on = models.DateField(default=timezone.localdate)
    delta = EncryptedDecimalField(max_digits=12, decimal_places=2, help_text="Positive reduces the outstanding balance.")
    reason = models.CharField(max_length=15, choices=Reason.choices)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-occurred_on", "-id"]

    def __str__(self) -> str:
        return f"{self.get_reason_display()} · {self.loan.file_reference} · {self.occurred_on}"


class SalaryLoanSkip(models.Model):
    """"Skip this month" (Q68): the employee asks, HR approves, a Payroll
    Admin records it. The engine deducts nothing for the loan in that period
    and warns; the balance is untouched, the schedule stretches a month."""

    loan = models.ForeignKey(SalaryLoan, on_delete=models.CASCADE, related_name="skips")
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=255)
    approved_by_hr = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        help_text="The HR person who approved the employee's request.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-period__start_date"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "period"], name="payroll_loan_skip_unique_per_period"),
        ]

    def __str__(self) -> str:
        return f"Skip · {self.loan.file_reference} · {self.period}"


# ---------------------------------------------------------------------------
# Pre-Orbis payroll months + tax challans (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md)
# ---------------------------------------------------------------------------

class TaxChallan(models.Model):
    """One treasury deposit of TDS (a "challan"). Company-level: the number,
    date, bank and total on the challan form, plus the payroll month it paid
    (``period_year``/``period_month``, typed on the challan sheet) and, once
    that month's run is calculated, the run itself (``run``). Which
    employees' tax it carried — and how much of each — is the separate
    ``TaxChallanAllocation`` table (docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md
    Revision 2), the same for a pre-Orbis month and an Orbis run.

    ``total_amount`` is deliberately a plaintext DecimalField: it is the
    company's deposit, not any one person's pay — the same exception class
    as ``PayRate`` (``core/tests_encryption.py::RegistryGuardTests``).
    """

    class Source(models.TextChoices):
        IMPORTED = "IMPORTED", "Imported (cutover sheet)"
        RUN = "RUN", "Recorded against a payroll run"

    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="challans")
    challan_number = models.CharField(max_length=40, help_text="As printed on the challan, e.g. 2526-00042394161.")
    deposit_date = models.DateField()
    bank = models.ForeignKey(Bank, on_delete=models.PROTECT, related_name="+")
    total_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        help_text="The whole deposit — every employee's tax it carried, not one person's.",
    )
    period_year = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Payroll month this deposit paid (year the period closes in). Blank = not tied to a month.",
    )
    period_month = models.PositiveSmallIntegerField(null=True, blank=True)
    run = models.ForeignKey(
        PayrollRun, on_delete=models.PROTECT, null=True, blank=True, related_name="tax_challans",
        help_text="The calculated run of that payroll month, once it exists. Blank for a pre-Orbis month or an untied deposit.",
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.IMPORTED)
    note = models.CharField(max_length=255, blank=True)
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["deposit_date", "challan_number"]
        constraints = [
            models.UniqueConstraint(fields=["tax_year", "challan_number"], name="payroll_taxchallan_unique_per_year"),
        ]

    def __str__(self) -> str:
        return f"{self.challan_number} · {self.deposit_date:%d %b %Y} · {self.bank}"

    @property
    def month_label(self) -> str:
        return f"{self.period_year}-{self.period_month:02d}" if self.period_year and self.period_month else ""


class TaxChallanAllocation(models.Model):
    """How much of one employee's tax a challan carried — one row per
    (challan, employee), uploaded by Payroll Admins from the "TDS by
    challan" Upload Center card after Finance records the deposit.

    The amount is **stored, not derived** (a reversal of the certificate
    plan's first draft, recorded in docs/PRE_ORBIS_PAYROLL_IMPORT_PLAN.md
    Revision 2): a deposit is a historical fact, so if the run is later
    recalculated the thing to surface is a warning that the run's tax no
    longer equals what was deposited — never a silently moved figure. One
    table serves a pre-Orbis month, an Orbis run and a deposit tied to no
    month (a year-end top-up) alike; the certificate's challan column is a
    read of these rows.
    """

    class Source(models.TextChoices):
        IMPORTED = "IMPORTED", "Uploaded sheet"
        RUN = "RUN", "Auto-allocated from a payroll run"

    challan = models.ForeignKey(TaxChallan, on_delete=models.PROTECT, related_name="allocations")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tax_challan_allocations")
    amount = EncryptedDecimalField(
        max_digits=12, decimal_places=2, help_text="The taka of this employee's tax inside this deposit.",
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.IMPORTED)
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["challan__deposit_date", "challan__challan_number", "employee__employee_id"]
        constraints = [
            models.UniqueConstraint(fields=["challan", "employee"], name="payroll_challanalloc_unique_employee"),
        ]

    def __str__(self) -> str:
        return f"{self.challan_id} · {self.employee_id}"


class LegacyPayrollMonth(models.Model):
    """One employee's one pre-Orbis payroll month, stored exactly as Finance
    supplied it — never recomputed (the ``leave.LegacyAnnualLeaveRecord``
    posture). Exists only for the months of a tax year before
    ``TaxYear.orbis_live_from``; an Orbis month is read from its run.

    The shape is the *monthly payroll ledger* (plan §1): ``lines`` holds
    ``{component_code: amount}`` for every pay component paid or deducted
    that month — earnings and deductions alike (TDS has its own column), so
    an imported month and an Orbis month look identical to
    ``payroll/tax_ledger.py``, to Finance's reconciliation export and to the
    certificate (which reads only the taxable earnings out of it). Exactly
    what was paid, nothing derived: the mobile & internet reimbursement is
    the policy tier of the gross (computed where needed, never typed —
    Revision 2), and which challan carried the month's TDS is
    ``TaxChallanAllocation``.
    """

    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="legacy_months")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="legacy_payroll_months")
    year = models.PositiveSmallIntegerField(help_text="Payroll period year (the month the period closes in).")
    month = models.PositiveSmallIntegerField()
    lines = EncryptedJSONField(
        default=dict, blank=True,
        help_text="{component_code: amount} for every pay component paid or deducted that month (TDS has its own column).",
    )
    tds_deducted = EncryptedDecimalField(max_digits=12, decimal_places=2, default=0)
    net_pay = EncryptedDecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="What was actually paid out, as in Finance's register. Reference only; blank if not supplied.",
    )
    adjustment_months = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="How many months of salary adjustment the SALARY_ADJUSTMENT line of this month settled (docs/SALARY_ADJUSTMENT_PLAN.md).",
    )
    batch = models.ForeignKey(
        PayrollInputBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["year", "month", "employee__employee_id"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "year", "month"], name="payroll_legacymonth_unique_employee_month"),
        ]

    def __str__(self) -> str:
        return f"Legacy month {self.year}-{self.month:02d} · {self.employee_id}"

    def line_amount(self, code: str) -> Decimal:
        return Decimal(str((self.lines or {}).get(code, 0) or 0))

    def taxable_additional_pay(self, taxable_codes: set[str] | None = None) -> Decimal:
        """What the true-up engine would count from this month: every
        taxable EARNING line except the codes in
        ``payroll.tax.TAX_EXCLUDED_EARNING_CODES``. Deduction lines (meal,
        loan, disciplinary…) in ``lines`` are ignored. ``taxable_codes`` may
        be passed by a caller looping over many rows to save the query."""
        from payroll.tax import TAX_EXCLUDED_EARNING_CODES  # local import — avoid cycle at module load
        if taxable_codes is None:
            taxable_codes = set(
                PayComponent.objects.filter(kind=PayComponent.Kind.EARNING, is_taxable=True)
                .values_list("code", flat=True)
            )
        return sum(
            (
                Decimal(str(v or 0)) for k, v in (self.lines or {}).items()
                if k in taxable_codes and k not in TAX_EXCLUDED_EARNING_CODES
            ),
            Decimal("0"),
        )
