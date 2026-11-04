from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class AttendancePunch(models.Model):
    """One employee's office presence for one date, synced from TimeLy (or
    written by an approved AttendanceCorrection).

    Keyed by (employee, date) rather than FK'd to `roster.Shift` — a punch
    can exist for a date with no concrete Shift row (the employee is covered
    by their weekly Roster pattern instead), and this is an observed fact
    from an external system, not a scheduling record. Joined to Shift/Roster
    at query time by (employee_id, date) when a scheduled-vs-actual
    comparison is needed.

    `raw_payload` is populated from whatever the current TimeLy API happens
    to return — see `attendance/timely.py`. The API is pre-final (a
    dedicated version is still coming), so `raw_payload` keeps the exact
    record synced each day as insurance against fields we didn't anticipate.
    A row with `source=MANUAL_CORRECTION` has no TimeLy payload behind it —
    `raw_payload` stays whatever it was before the correction (or null).
    """

    class Source(models.TextChoices):
        TIMELY_API = "TIMELY_API", "TimeLy API"
        MANUAL_CORRECTION = "MANUAL_CORRECTION", "Manual correction"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attendance_punches",
    )
    date = models.DateField()
    check_in = models.DateTimeField(null=True, blank=True, help_text="TimeLy's first_punch_at.")
    check_out = models.DateTimeField(null=True, blank=True, help_text="TimeLy's last_punch_at.")
    time_in_office = models.DurationField(
        null=True, blank=True,
        help_text="TimeLy's own duration_seconds when the API provides it, else check_out − "
                   "check_in as a fallback. duration_seconds is authoritative because "
                   "first_punch_at/last_punch_at can be truncated to the queried calendar date for "
                   "a shift crossing midnight (seen 2026-09-16 on night-shift employees — the punch "
                   "span looked like ~1h when the real shift was ~9h); duration_seconds isn't. An "
                   "approved AttendanceCorrection can instead set this directly, to net out an "
                   "in-building gap (e.g. a lunch break) that neither source can see.",
    )
    raw_office = models.CharField(max_length=100, blank=True, help_text="TimeLy's own office value, unnormalized.")
    office_bucket = models.CharField(max_length=10, blank=True, help_text="Derived via accounts.locations.office_bucket.")
    punch_count = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.TIMELY_API)
    raw_payload = models.JSONField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "employee"]
        unique_together = [("employee", "date")]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["employee", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.date}"


class AttendanceCorrection(models.Model):
    """Employee-submitted (or HR-on-behalf) request to fix a wrong punch,
    subject to the employee's own manager (or HR/Admins) approving it.

    Same shape as leave.LeaveApplication's request/decide flow deliberately —
    requested_by/requested_at mirror applied_by/applied_at, processed_by/
    processed_at/process_note mirror the same fields there. On approval,
    attendance/views.py::AttendanceCorrectionApproveView writes
    requested_check_in/requested_check_out and effective_time_in_office onto
    the target AttendancePunch row with source=MANUAL_CORRECTION —
    sync_attendance_punches.py then leaves that row's timestamps alone on any
    future resync for the same date.

    requested_time_in_office is an optional override, separate from the two
    punch times: an employee can punch in at 07:00 and out at 23:00 (a 16h
    span) but only actually have worked 9h of it — they left the building
    for hours in between and TimeLy has no record of that gap, only the
    first and last punch. Leaving it blank falls back to the plain
    check_out − check_in subtraction (see effective_time_in_office).

    `punch` may be null: a day with no punch at all (the attendance list's
    "Unaccounted" case) has nothing to point at until one is created on
    approval.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attendance_corrections",
    )
    date = models.DateField()
    punch = models.ForeignKey(
        AttendancePunch, on_delete=models.SET_NULL, null=True, blank=True, related_name="corrections",
    )

    # Snapshot of what was on file when the request was filed — the diff
    # shown for review, independent of whatever the punch holds by the time
    # someone actually acts on it.
    original_check_in = models.DateTimeField(null=True, blank=True)
    original_check_out = models.DateTimeField(null=True, blank=True)

    requested_check_in = models.DateTimeField()
    requested_check_out = models.DateTimeField()
    requested_time_in_office = models.DurationField(
        null=True, blank=True,
        help_text="Optional override, HH:MM:SS. Leave blank to use check-out minus check-in; "
                   "fill in only when time away from the office between the two punches "
                   "(e.g. a lunch break) shouldn't count.",
    )
    reason = models.TextField()

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    process_note = models.CharField(max_length=255, blank=True)

    class Meta:
        # Ordered by the working date itself, newest first — same reasoning
        # as leave.LeaveApplication.Meta.ordering: the list reads as "which
        # day was this about", so the filing timestamp is a poor primary
        # sort. requested_at is only the tiebreaker for same-day requests.
        ordering = ["-date", "-requested_at"]
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.date} ({self.get_status_display()})"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def effective_time_in_office(self):
        """What actually gets written to AttendancePunch on approval: the
        manual override if one was given, else the plain punch-span subtraction."""
        if self.requested_time_in_office is not None:
            return self.requested_time_in_office
        if self.requested_check_in and self.requested_check_out:
            return self.requested_check_out - self.requested_check_in
        return None

    def clean(self):
        if self.requested_check_in and self.requested_check_out:
            if self.requested_check_out <= self.requested_check_in:
                raise ValidationError({"requested_check_out": "Check-out must be after check-in."})
            span = self.requested_check_out - self.requested_check_in
            if self.requested_time_in_office is not None and self.requested_time_in_office > span:
                raise ValidationError({
                    "requested_time_in_office":
                        "Time in office can't be longer than the span between check-in and check-out.",
                })
