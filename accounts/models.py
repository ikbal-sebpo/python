import hashlib
import secrets
import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from accounts.managers import CustomUserManager


class _Named(models.Model):
    """Abstract parent for simple reference tables used by org structure."""

    name = models.CharField(max_length=150, unique=True)
    description = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Department(_Named):
    class CplTreatment(models.TextChoices):
        NONE = "NONE", "Never CPL (overtime only)"
        MANUAL = "MANUAL", "Mixed — CPL only via manual/uploaded record"
        AUTO = "AUTO", "Auto-grant CPL from roster (default)"

    cpl_treatment = models.CharField(
        max_length=10, choices=CplTreatment.choices, default=CplTreatment.AUTO,
        help_text=(
            "How this department earns Compensatory Leave for working a "
            "government holiday. NONE: never CPL, always overtime (e.g. "
            "Digital Advertising). MANUAL: mixed per-employee/per-instance, "
            "decided outside Orbis — CPL only enters the ledger via an HR "
            "upload, never auto-detected (e.g. Data Solutions & Due "
            "Diligence). AUTO: a WORK shift on a Holiday-tagged date "
            "auto-grants CPL (default for everything else)."
        ),
    )
    head_of_department = models.ForeignKey(
        "accounts.CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="heads_departments",
        help_text=(
            "The department head. Used to assign the DEPT_HEAD offboarding "
            "clearance step, and grants department-wide read+write visibility "
            "(everyone in the department, regardless of the reports-to chain)."
        ),
    )
    additional_managers = models.ManyToManyField(
        "accounts.CustomUser",
        blank=True,
        related_name="managed_departments",
        help_text=(
            "Extra managers (beyond the head) who can view AND manage everyone "
            "in this department, in addition to the reports-to chain — e.g. for "
            "payroll review. Read + write scope."
        ),
    )


class Designation(_Named):
    access_level = models.ForeignKey(
        "accounts.AccessLevel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="designations",
        help_text=(
            "Seniority tier this designation maps to — used to decide who is "
            "eligible to be picked as someone's manager (Reports to). "
            "e.g. \"Team Lead\" -> Supervisor."
        ),
    )


class AccessLevel(_Named):
    rank = models.PositiveIntegerField(default=0, help_text="Higher rank = more senior.")

    class Meta(_Named.Meta):
        ordering = ["-rank", "name"]


class JobLocation(_Named):
    """Where the employee is contractually assigned (e.g. DHK-MHK, DHK-GSN)."""

    code = models.CharField(max_length=20, blank=True)


class WorkLocation(_Named):
    """Where the employee is actually working (WFO, WFH, hybrid)."""


class Team(_Named):
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="teams",
        null=True,
        blank=True,
    )
    cpl_treatment_override = models.CharField(
        max_length=10, choices=Department.CplTreatment.choices, blank=True, default="",
        help_text=(
            "Override this team's Compensatory Leave treatment, ignoring the "
            "parent department's setting. Blank = inherit from department. "
            "Use for a team whose CPL handling differs from the rest of its "
            "department (e.g. the \"Reporting\" team under Corporate, which "
            "is overtime-only even though other Corporate teams auto-grant)."
        ),
    )
    additional_managers = models.ManyToManyField(
        "accounts.CustomUser",
        blank=True,
        related_name="managed_teams",
        help_text=(
            "Extra managers who can view AND manage everyone on this team, "
            "in addition to the reports-to chain and any department-wide grant "
            "— e.g. an APM who needs roster/payroll visibility into one team "
            "without seeing the rest of the department."
        ),
    )


class SubTeam(_Named):
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name="subteams",
    )

    class Meta(_Named.Meta):
        unique_together = [("team", "name")]


class Grade(_Named):
    """Salary grade/band — payroll's designation-tier dimension (see payroll.PayRate)."""

    rank = models.PositiveIntegerField(default=0, help_text="Higher rank = more senior.")

    class Meta(_Named.Meta):
        ordering = ["-rank", "name"]


class EmploymentType(_Named):
    """e.g. Permanent, Contractual, Intern — payroll/HR classification, not roster shift type."""


class CostCenter(_Named):
    """Payroll/finance cost allocation unit."""

    code = models.CharField(max_length=30, blank=True)
    gl_code = models.CharField(max_length=30, blank=True)


class SubProject(_Named):
    """Finer-grained scope below Department/Team for payroll rate resolution (see payroll.PayRate)."""


# Sentinel for a deferred field in CustomUser's directory snapshot.
_UNLOADED = object()


class CustomUser(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(unique=True)
    employee_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    register_serial_no = models.CharField(
        max_length=50, unique=True, null=True, blank=True,
        help_text="HR's Employee Register serial number (physical HR register, not employee_id).",
    )

    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    designation = models.ForeignKey(
        Designation, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    access_level = models.ForeignKey(
        AccessLevel, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    subteam = models.ForeignKey(
        SubTeam, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    job_location = models.ForeignKey(
        JobLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    work_location = models.ForeignKey(
        WorkLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    grade = models.ForeignKey(
        Grade, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    employment_type = models.ForeignKey(
        EmploymentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )

    # Employment lifecycle (ported from ttms, trimmed to actually-used fields).
    # The date columns are indexed: the Org dashboard filters/counts on them
    # on every load, and deactivate_past_lwd scans last_working_date nightly.
    joining_date = models.DateField(null=True, blank=True, db_index=True)
    probation_month = models.PositiveSmallIntegerField(null=True, blank=True)
    notice_period_months = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Contracted resignation notice period agreed at hire, in months.",
    )
    confirmation_date = models.DateField(null=True, blank=True, db_index=True)
    last_working_date = models.DateField(null=True, blank=True, db_index=True)

    timezone_name = models.CharField(max_length=64, blank=True, default="")
    shift_start_hour = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
        help_text="Default shift start time as decimal hours, e.g. 9.5 for 09:30.",
    )

    signature = models.ImageField(upload_to="signatures/", null=True, blank=True)
    profile_picture = models.ImageField(upload_to="profile_pictures/", null=True, blank=True)
    contact_number = models.CharField(max_length=30, blank=True)

    # HR-controlled: whether this employee may add/update/delete their own
    # personal-data profile sections (employees.SELF_EDITABLE_SECTIONS). Off
    # by default — HR turns it on when an employee needs to make an update,
    # then off again once done. See accounts.permissions::can_edit_employee_profile
    # and employees.ProfileSelfServiceEvent for the audit trail.
    profile_self_service_enabled = models.BooleanField(default=False)

    # Microsoft Entra (Azure AD) object id — the stable identity other apps
    # get from SSO. Written only by SSO login (accounts/auth_backends.py) and
    # `import_entra_object_ids`, never by an API consumer. docs/DIRECTORY_API_PLAN.md §6.
    entra_object_id = models.CharField(
        max_length=36, unique=True, null=True, blank=True,
        help_text="Microsoft Entra object id. Filled in automatically at SSO login.",
    )
    # When a field the Directory API exposes last changed — the change feed
    # behind `/api/v1/directory/employees/?updated_since=`. Not `auto_now`:
    # that would move on every login (`last_login`) and still miss
    # `save(update_fields=...)`. `save()` below bumps it when a
    # DIRECTORY_FIELDS value changes; queryset `update()` / `bulk_update()`
    # callers that touch one of those fields must set it themselves
    # (`directory_stamp()`). docs/DIRECTORY_API_PLAN.md §5.4.
    directory_updated_at = models.DateTimeField(default=timezone.now, db_index=True, editable=False)

    #: Attnames whose change moves ``directory_updated_at``.
    DIRECTORY_FIELDS = (
        "email", "employee_id", "first_name", "last_name", "is_active",
        "department_id", "designation_id", "team_id",
        "job_location_id", "work_location_id",
        "joining_date", "last_working_date", "entra_object_id",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "first_name", "last_name"]

    objects = CustomUserManager()

    class Meta:
        verbose_name = "Employee"
        verbose_name_plural = "Employees"
        ordering = ["first_name", "last_name"]

    def __str__(self) -> str:
        return self.get_full_name() or self.email

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username or self.email

    @property
    def search_label(self) -> str:
        """"Name · Employee ID" — disambiguates same-named employees in pickers."""
        return f"{self.display_name} · {self.employee_id}" if self.employee_id else self.display_name

    # -- directory change tracking ------------------------------------------

    def _directory_values(self) -> tuple:
        # __dict__, not getattr: a deferred field must not trigger a query.
        return tuple(self.__dict__.get(name, _UNLOADED) for name in self.DIRECTORY_FIELDS)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._directory_snapshot = instance._directory_values()
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        current = self._directory_values()
        snapshot = getattr(self, "_directory_snapshot", None)
        if fields is None or snapshot is None:
            self._directory_snapshot = current
            return
        # Partial refresh (also how a deferred field loads on first access):
        # re-baseline only the refreshed fields, so an unsaved edit to another
        # directory field still counts as a change.
        refreshed = {self._meta.get_field(f).attname for f in fields}
        self._directory_snapshot = tuple(
            now if name in refreshed else before
            for name, now, before in zip(self.DIRECTORY_FIELDS, current, snapshot)
        )

    def save(self, *args, **kwargs):
        snapshot = getattr(self, "_directory_snapshot", None)
        if snapshot is not None and snapshot != self._directory_values():
            self.directory_updated_at = timezone.now()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "directory_updated_at"}
        super().save(*args, **kwargs)
        self._directory_snapshot = self._directory_values()



def directory_stamp(*field_names: str, model=None) -> dict:
    """Extra kwargs for a ``CustomUser`` queryset ``update()`` that bypasses
    ``save()``: ``{"directory_updated_at": now}`` when any of ``field_names``
    (name or attname) is a directory field, else ``{}``. Pass ``model`` from
    generic code that updates several models; anything but ``CustomUser``
    gets ``{}``.

        User.objects.filter(...).update(is_active=False, **directory_stamp("is_active"))
    """
    if model is not None and model is not CustomUser:
        return {}
    tracked = set(CustomUser.DIRECTORY_FIELDS)
    if any(name in tracked or f"{name}_id" in tracked for name in field_names):
        return {"directory_updated_at": timezone.now()}
    return {}


class ApiClient(models.Model):
    """One application allowed to call Orbis's API — its own token, its own
    scopes, revocable on its own. docs/DIRECTORY_API_PLAN.md §4.

    Only the SHA-256 of the token is stored; the plaintext exists once, in
    the admin response that issued it (``issue_token``).
    """

    class Scope(models.TextChoices):
        DIRECTORY_READ = "directory:read", "Directory — read"
        ROSTER_READ = "roster:read", "Roster — read"
        ROSTER_WRITE = "roster:write", "Roster — write"

    TOKEN_PREFIX = "orb_"

    name = models.SlugField(max_length=50, unique=True, help_text="e.g. timely, dsndd")
    token_prefix = models.CharField(
        max_length=12, blank=True, editable=False,
        help_text="First characters of the current token — identifies a leaked token.",
    )
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    token_issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    scopes = models.JSONField(default=list, blank=True)
    service_account = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text=(
            "Required for a client with roster:write — the persisted user its "
            "writes are stored under (Shift.added_by). Leave blank for read-only clients."
        ),
    )
    is_active = models.BooleanField(default=True, help_text="Untick to revoke the client immediately.")
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["name"]
        verbose_name = "API client"

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def hash_token(token: str) -> str:
        # A plain digest is enough: tokens are 32 random bytes, so there is
        # nothing to brute-force, and lookup stays an indexed equality.
        return hashlib.sha256(token.encode()).hexdigest()

    def issue_token(self) -> str:
        """Replace this client's token and return the new plaintext — the only
        time it exists. The previous token stops working immediately."""
        token = self.TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.token_hash = self.hash_token(token)
        self.token_prefix = token[:12]
        self.token_issued_at = timezone.now()
        self.save(update_fields=["token_hash", "token_prefix", "token_issued_at"])
        return token

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or [])
