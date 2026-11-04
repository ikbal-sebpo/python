"""Roster app — shift catalog + daily assignments + weekly pattern + holidays.

Unified cell model per docs/PLAN.md: one `ShiftType` table holds every value
that can appear in a roster cell, categorised as WORK / LEAVE / HOLIDAY / OFF.
The weekly grid renders `{{ cell.shift_type.name }}` with no branching.
"""

from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import models
from django.utils import timezone

from accounts.locations import LOCATION_DROPDOWN_CHOICES, LOCATION_DROPDOWN_VALUES


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


class ShiftType(_Auditable):
    """Every value that can appear in a roster cell."""

    class Category(models.TextChoices):
        WORK = "WORK", "Working shift"
        LEAVE = "LEAVE", "Leave"
        HOLIDAY = "HOLIDAY", "Holiday"
        OFF = "OFF", "Weekly off"

    name = models.CharField(max_length=100, unique=True, help_text="e.g. Morning, Evening, Night, Casual Leave")
    full_name = models.CharField(
        max_length=100, blank=True,
        help_text="Spelled-out form of an abbreviated name, e.g. AL → Annual Leave. Shown alongside the code in dropdowns.",
    )
    description = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.WORK)
    is_working_shift = models.BooleanField(
        default=True,
        help_text="Usually derived from category; kept for payroll filter compatibility.",
    )
    shift_timezone = models.CharField(max_length=64, blank=True)
    shift_start_time = models.TimeField(null=True, blank=True)
    shift_end_time = models.TimeField(null=True, blank=True)
    color = models.CharField(
        max_length=20, blank=True,
        help_text="Optional tailwind class or hex to colour-code the cell (e.g. brand-accent, #FFC638).",
    )
    sort_order = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Optional manual order. Lower numbers appear first; blank rows sort alphabetically.",
    )
    has_attribute = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = [models.F("sort_order").asc(nulls_last=True), "name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # Keep is_working_shift in sync with category — simpler for reports.
        self.is_working_shift = self.category == self.Category.WORK
        super().save(*args, **kwargs)


class ShiftAttribute(_Auditable):
    """A named time-variant of a ShiftType.

    Example: `Evening` ShiftType has attributes `1PM start` (13:00–22:00)
    and `2PM start` (14:00–23:00). The roster picks a specific attribute so
    the employee knows exactly when to show up.

    When the user assigns a ShiftAttribute, the parent ShiftType is inferred
    (see `Roster.save()` / `Shift.save()` below). ShiftType's own `has_attribute`
    flag is kept in sync by this model's save / delete overrides.
    """

    shift_type = models.ForeignKey(
        ShiftType, on_delete=models.CASCADE, null=True, blank=True,
        related_name="attributes",
    )
    name = models.CharField(max_length=100)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Optional manual order. Lower numbers appear first within the parent shift type; blank rows sort alphabetically.",
    )
    is_active = models.BooleanField(default=True)
    departments = models.ManyToManyField(
        "accounts.Department", blank=True, related_name="shift_attributes",
        help_text=(
            "Restrict this shift option to these departments' roster dropdowns. "
            "Leave blank to show it everywhere (default). Only meaningful for "
            "Working-shift attributes — Leave/Holiday/Off types always show to "
            "everyone regardless of this field."
        ),
    )

    class Meta:
        ordering = [models.F("sort_order").asc(nulls_last=True), "start_time", "name"]
        unique_together = [("shift_type", "name")]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Keep the parent ShiftType's has_attribute flag in sync.
        if self.shift_type:
            self.shift_type.has_attribute = self.shift_type.attributes.filter(is_active=True).exists()
            self.shift_type.save(update_fields=["has_attribute"])

    def delete(self, *args, **kwargs):
        parent = self.shift_type
        super().delete(*args, **kwargs)
        if parent:
            parent.has_attribute = parent.attributes.filter(is_active=True).exists()
            parent.save(update_fields=["has_attribute"])


class _LocatedRosterCell(models.Model):
    """Mixin: per-cell job/work location with employee-default fallback.

    Both `Roster` (weekly pattern) and `Shift` (per-date concrete assignment)
    can override the employee's contractual `CustomUser.job_location` /
    `CustomUser.work_location` for a specific weekday or date — e.g., "Tue
    in MBD office, Fri WFH." Null on either field means *inherit from the
    employee's contract default*.
    """

    job_location = models.ForeignKey(
        "accounts.JobLocation", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="Office for this cell. Blank = inherit from employee profile.",
    )
    work_location = models.ForeignKey(
        "accounts.WorkLocation", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="Mode (WFO/WFH/Hybrid) for this cell. Blank = inherit.",
    )

    class Meta:
        abstract = True

    @property
    def location_badge(self) -> str:
        """Short label for the cell badge — 'MHK', 'MBD', 'RAOWA', 'WFH', etc.

        Falls back to the employee's contract location when the row hasn't
        explicitly overridden it. WFH wins over the office bucket so a remote
        day reads as 'WFH' regardless of which office the employee normally
        sits at.
        """
        wl = self.work_location or getattr(self.employee, "work_location", None)
        if wl:
            up = (wl.name or "").upper()
            if up.startswith("WORK FROM HOME") or up == "WFH":
                return "WFH"
            if "HYBRID" in up:
                return "HYB"
        jl = self.job_location or getattr(self.employee, "job_location", None)
        if jl:
            # Prefer the canonical bucket (MHK/MBD/RAOWA) over the raw name —
            # handles both seeded short codes and Azure-AD full-address rows.
            from accounts.locations import office_bucket
            bucket = office_bucket(jl)
            if bucket in ("MHK", "MBD", "RAOWA"):
                return bucket
            return (jl.code or jl.name)[:6]
        return ""


class Roster(_Auditable, _LocatedRosterCell):
    """Default weekly pattern for an employee.

    One row per (employee, weekday). Used as a fallback when no `Shift` row
    exists for a concrete (employee, date).
    """

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="roster_pattern",
    )
    weekday = models.IntegerField(choices=Weekday.choices)
    shift_type = models.ForeignKey(ShiftType, on_delete=models.PROTECT, related_name="+")
    shift_attribute = models.ForeignKey(
        ShiftAttribute, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["employee", "weekday"]
        unique_together = [("employee", "weekday")]

    def save(self, *args, **kwargs):
        # If an attribute is set, its parent ShiftType always wins.
        if self.shift_attribute_id and self.shift_attribute.shift_type_id:
            self.shift_type = self.shift_attribute.shift_type
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        variant = f" / {self.shift_attribute}" if self.shift_attribute_id else ""
        return f"{self.employee} · {self.get_weekday_display()} → {self.shift_type}{variant}"


class Shift(_Auditable, _LocatedRosterCell):
    """Actual daily assignment for (employee, date).

    This is what the weekly grid UI and the Roster API surface. If a cell has
    no Shift row, the view falls back to the employee's Roster pattern, then
    shows empty.
    """

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shifts",
    )
    date = models.DateField()
    shift_type = models.ForeignKey(ShiftType, on_delete=models.PROTECT, related_name="shifts")
    shift_attribute = models.ForeignKey(
        ShiftAttribute, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    note = models.CharField(max_length=255, blank=True)
    # Set when this row was materialised by an approved leave. Indexed FK —
    # the leave services previously located their rows by scanning `note`
    # ("Leave #<pk>"), which is unindexed and grows with employees × 365/yr.
    # `note` still carries the human-readable tag for the grid tooltip.
    leave_application = models.ForeignKey(
        "leave.LeaveApplication", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="materialised_shifts",
    )

    class Meta:
        ordering = ["date", "employee"]
        unique_together = [("employee", "date")]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["employee", "date"]),
        ]

    def save(self, *args, **kwargs):
        if self.shift_attribute_id and self.shift_attribute.shift_type_id:
            self.shift_type = self.shift_attribute.shift_type
        super().save(*args, **kwargs)

    @property
    def display_times(self):
        """Return (start, end) tuple from the attribute if set, else the type."""
        if self.shift_attribute_id and self.shift_attribute.start_time:
            return self.shift_attribute.start_time, self.shift_attribute.end_time
        return self.shift_type.shift_start_time, self.shift_type.shift_end_time

    def __str__(self) -> str:
        variant = f" / {self.shift_attribute}" if self.shift_attribute_id else ""
        return f"{self.employee} · {self.date} → {self.shift_type}{variant}"


class Holiday(_Auditable):
    date = models.DateField(unique=True)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["date"]

    def __str__(self) -> str:
        return f"{self.date} — {self.name}"


class ShiftChangeLog(models.Model):
    """Append-only audit trail of every `Shift` create/update/delete.

    Populated entirely by signals (`roster/signals.py`), not by callers —
    every existing write path (roster grid, bulk upload, leave
    materialisation, the external write API) already sets `added_by`/
    `updated_by` on `Shift`, so this captures all of them for free. All FKs
    are `SET_NULL` + nullable, including `shift` itself, so a row survives
    deletion of the thing it describes (a DELETED entry must outlive the
    deleted `Shift`).
    """

    class ActionType(models.TextChoices):
        CREATED = "CREATED", "Created"
        UPDATED = "UPDATED", "Updated"
        DELETED = "DELETED", "Deleted"

    shift = models.ForeignKey(
        Shift, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="change_logs",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="Whose shift this is.",
    )
    date = models.DateField(null=True, blank=True)
    action_type = models.CharField(max_length=10, choices=ActionType.choices)

    previous_shift_type = models.ForeignKey(
        ShiftType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    new_shift_type = models.ForeignKey(
        ShiftType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    previous_shift_attribute = models.ForeignKey(
        ShiftAttribute, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    new_shift_attribute = models.ForeignKey(
        ShiftAttribute, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    previous_job_location = models.ForeignKey(
        "accounts.JobLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    new_job_location = models.ForeignKey(
        "accounts.JobLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    previous_work_location = models.ForeignKey(
        "accounts.WorkLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    new_work_location = models.ForeignKey(
        "accounts.WorkLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    previous_note = models.CharField(max_length=255, blank=True, default="")
    new_note = models.CharField(max_length=255, blank=True, default="")

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="The real person who made the change (dsndd actor, roster editor, etc.).",
    )
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at"]
        indexes = [
            models.Index(fields=["employee", "date"]),
            models.Index(fields=["date"]),
            models.Index(fields=["action_type"]),
            models.Index(fields=["changed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.date} → {self.get_action_type_display()}"


class ShiftChangeRequest(_Auditable):
    """An employee's (or HR / a manager's on-behalf) request to change the
    shift and/or work location on one rostered date, subject to the same
    approve/decline rules as `leave.LeaveApplication`.

    The request/decide shape mirrors LeaveApplication deliberately
    (applied_by/applied_at, processed_by/processed_at/process_note). On
    approval `roster.services.apply_shift_change` writes the requested
    values onto the `Shift` row for that date — creating it if the day was
    only covered by the weekly `Roster` pattern — through a normal
    `update_or_create`, so `ShiftChangeLog` and the leave-ledger / CpL
    signals all fire as for a grid edit. Nothing on `Shift` points back
    here: an approved change is a one-shot overwrite, never reversed from
    the request (the cell may have been edited again since).

    `original_*` snapshot what the roster said when the request was filed
    (Shift row, else pattern — `original_was_concrete` says which), frozen
    so a later roster edit can't rewrite the before/after diff reviewers
    saw. Locations are stored as the four-value dropdown code the grid
    uses (`accounts.locations.LOCATION_DROPDOWN_CHOICES`), not as the two
    FKs — `resolve_location_choice` maps it to (job_location,
    work_location) at apply time, exactly like `CellUpdateView`.

    Spec: docs/SHIFT_CHANGE_REQUEST_PLAN.md.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="shift_change_requests",
    )
    date = models.DateField()

    original_shift_type = models.ForeignKey(
        ShiftType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    original_shift_attribute = models.ForeignKey(
        ShiftAttribute, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    original_was_concrete = models.BooleanField(
        default=False,
        help_text="True when a Shift row existed for the date at filing time; "
                  "False when the weekly pattern was the fallback.",
    )
    original_location = models.CharField(
        max_length=5, blank=True, default="",
        help_text="MHK / MBD / RAOWA / WFH as resolved at filing time; blank when "
                  "neither the row nor the employee's contract resolved to one.",
    )

    requested_shift_type = models.ForeignKey(
        ShiftType, on_delete=models.PROTECT, related_name="+",
    )
    requested_shift_attribute = models.ForeignKey(
        ShiftAttribute, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    requested_location = models.CharField(max_length=5, choices=LOCATION_DROPDOWN_CHOICES)
    reason = models.TextField()

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
    )
    applied_at = models.DateTimeField(default=timezone.now)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    process_note = models.CharField(max_length=255, blank=True)

    class Meta:
        # Newest roster date first, same reasoning as LeaveApplication: the
        # lists read as "when is this change for", not "when was it filed".
        ordering = ["-date", "-applied_at"]
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "date"]),
            models.Index(fields=["employee", "date"]),
        ]

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == self.Status.APPROVED

    @property
    def original_label(self) -> str:
        if self.original_shift_attribute_id:
            return self.original_shift_attribute.name
        if self.original_shift_type_id:
            return self.original_shift_type.name
        return ""

    @property
    def requested_label(self) -> str:
        if self.requested_shift_attribute_id:
            return self.requested_shift_attribute.name
        return self.requested_shift_type.name

    @property
    def shift_changed(self) -> bool:
        return (
            self.original_shift_type_id != self.requested_shift_type_id
            or self.original_shift_attribute_id != self.requested_shift_attribute_id
        )

    @property
    def location_changed(self) -> bool:
        return self.original_location != self.requested_location

    def clean(self):
        # Keyed on NON_FIELD_ERRORS rather than the FK names: the request
        # form exposes the pair as one `shift_ref` field, and a ModelForm
        # raises ValueError on a model error keyed to a field it doesn't
        # render. `requested_location` IS a form field, so that one stays keyed.
        errors = {}
        general = []
        if self.requested_shift_attribute_id:
            parent_id = self.requested_shift_attribute.shift_type_id
            if parent_id and parent_id != self.requested_shift_type_id:
                general.append("The attribute must belong to the requested shift type.")
        if self.requested_shift_type_id and self.requested_shift_type.category not in (
            ShiftType.Category.WORK, ShiftType.Category.OFF,
        ):
            general.append(
                "Only working shifts and weekly-off days can be requested — "
                "leave goes through the leave application form."
            )
        if self.requested_location not in LOCATION_DROPDOWN_VALUES:
            errors["requested_location"] = "Pick one of MHK / MBD / RAOWA / WFH."
        if general:
            errors[NON_FIELD_ERRORS] = general
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.requested_shift_attribute_id and self.requested_shift_attribute.shift_type_id:
            self.requested_shift_type = self.requested_shift_attribute.shift_type
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.employee} · {self.date} → {self.requested_label} @ {self.requested_location}"


# ---------------------------------------------------------------------------
# Weekly team roster email (roster/emails.py renders, roster/services_email.py
# sends). One config per team; manual sends from the roster page and the
# Friday beat job (`send_weekly_rosters`) both log to RosterEmailLog, which is
# also what stops the scheduled job re-sending a week a manager already sent.
# ---------------------------------------------------------------------------

DEFAULT_ROSTER_EMAIL_INTRO = (
    "Dear Team,\n\n"
    "Good Day! I hope this email finds you well.\n\n"
    "I am writing to share the team schedule for next week {week}."
)


class RosterEmailConfig(models.Model):
    """Per-team settings for the weekly roster email."""

    team = models.OneToOneField(
        "accounts.Team", on_delete=models.CASCADE, related_name="roster_email_config",
    )
    to_emails = models.TextField(
        blank=True, help_text="Recipients, separated by commas or new lines.",
    )
    cc_emails = models.TextField(
        blank=True, help_text="CC recipients, separated by commas or new lines.",
    )
    poc = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        verbose_name="Team manager / POC",
        help_text="Shown in the email header. Blank = the department head.",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        verbose_name="Send as",
        help_text=(
            "The email goes out from the system address under this person's name "
            "(\"Name (via Orbis)\"), with Reply-To and a CC to them. "
            "Blank = the system name, Reply-To the POC."
        ),
    )
    intro_text = models.TextField(
        default=DEFAULT_ROSTER_EMAIL_INTRO,
        help_text="Opening paragraph(s). {week} is replaced with the date range, e.g. Sep/28 - Oct/04.",
    )
    signature = models.TextField(
        blank=True,
        help_text="Closing lines. Blank = \"Best regards,\" + the POC's name + the team name.",
    )
    send_team_copy = models.BooleanField(
        default=False,
        verbose_name="Also send the team their own copy",
        help_text=(
            "After each real send, everyone on the roster (trainees included) gets a separate "
            "copy with their row highlighted. No client addresses on it, so Reply All stays internal."
        ),
    )
    is_active = models.BooleanField(
        default=False,
        verbose_name="Send automatically",
        help_text="Email next week's roster every Friday at 22:00 unless it was already sent manually.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Roster email config"

    def __str__(self) -> str:
        return f"Roster email · {self.team}"

    @staticmethod
    def split_emails(raw: str) -> list[str]:
        import re
        return [e for e in (p.strip() for p in re.split(r"[,;\s]+", raw or "")) if e]

    @property
    def to_list(self) -> list[str]:
        return self.split_emails(self.to_emails)

    @property
    def cc_list(self) -> list[str]:
        return self.split_emails(self.cc_emails)

    @property
    def effective_poc(self):
        if self.poc_id:
            return self.poc
        dept = self.team.department
        return dept.head_of_department if dept else None


class RosterEmailMember(models.Model):
    """Per-person tweaks to one team's roster email.

    - `role_label` overrides the Role column (blank = designation).
    - `always_include` puts the person on the email regardless of team or
      status: someone from another team who covers this one (e.g. a night
      lead from CMX), or a trainee working a live shift early. Without it,
      the email lists the team's own members minus trainees.

    Deliberately not `Team.additional_managers`: being listed on a client
    email must not grant roster/management scope over the team.
    """

    config = models.ForeignKey(RosterEmailConfig, on_delete=models.CASCADE, related_name="members")
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role_label = models.CharField(max_length=100, blank=True)
    always_include = models.BooleanField(
        default=False,
        help_text="List this person even if they're outside the team or a trainee.",
    )
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers are listed first within their shift group.",
    )

    class Meta:
        ordering = ["sort_order", "role_label"]
        unique_together = [("config", "employee")]

    def __str__(self) -> str:
        return f"{self.employee} → {self.role_label or '(designation)'}"


class RosterEmailLog(models.Model):
    """One row per send attempt — the audit trail and the duplicate guard."""

    class Trigger(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        SCHEDULED = "SCHEDULED", "Scheduled"
        TEST = "TEST", "Test"

    class Kind(models.TextChoices):
        CLIENT = "CLIENT", "Client email"
        TEAM_COPY = "TEAM_COPY", "Team copy"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    team = models.ForeignKey("accounts.Team", on_delete=models.CASCADE, related_name="roster_email_logs")
    week_start = models.DateField()
    trigger = models.CharField(max_length=10, choices=Trigger.choices)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CLIENT)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    to_emails = models.TextField()
    cc_emails = models.TextField(blank=True)
    from_email = models.CharField(max_length=254, blank=True)
    reply_to = models.CharField(max_length=254, blank=True)
    subject = models.CharField(max_length=255)
    html_body = models.TextField()
    text_body = models.TextField()
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="Blank for the scheduled job.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["team", "week_start"])]

    def __str__(self) -> str:
        return f"{self.team} · {self.week_start} · {self.get_trigger_display()} · {self.get_status_display()}"

    # Sends that mean "this week has gone out" — client emails only; TEST
    # never counts, FAILED doesn't either (so the Friday job still retries a week whose manual
    # send bounced).
    COUNTING_TRIGGERS = (Trigger.MANUAL, Trigger.SCHEDULED)
    COUNTING_STATUSES = (Status.QUEUED, Status.SENT)

    @classmethod
    def already_sent(cls, team_id, week_start):
        return (
            cls.objects
            .filter(
                team_id=team_id, week_start=week_start, kind=cls.Kind.CLIENT,
                trigger__in=cls.COUNTING_TRIGGERS, status__in=cls.COUNTING_STATUSES,
            )
            .select_related("sent_by")
            .order_by("-created_at")
            .first()
        )
