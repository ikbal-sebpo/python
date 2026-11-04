"""HR profile + taxonomy models for Orbis.

All profile models FK to `accounts.CustomUser` via a field named `employee`.
Singletons use OneToOneField, repeating sections use ForeignKey.

Typos from ttms fixed on the way in per docs/PLAN.md:
- `ProfessionalCertification` (was `ProfessionalCertificatiion`)
- `height_in_inch` (was `hight_in_inch`)
- `resource_status` (was `resouce_status`)
"""

from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def year_validator(value: int) -> None:
    current = date.today().year
    if value < 1900 or value > current:
        raise ValidationError(f"Year must be between 1900 and {current}.")


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
# Taxonomy
# ---------------------------------------------------------------------------

class Country(models.Model):
    name = models.CharField(max_length=150, unique=True)
    iso_code = models.CharField(max_length=3, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Countries"

    def __str__(self) -> str:
        return self.name


class Division(models.Model):
    country = models.ForeignKey(Country, on_delete=models.CASCADE, related_name="divisions")
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["country__name", "name"]
        unique_together = [("country", "name")]

    def __str__(self) -> str:
        return self.name


class State(models.Model):
    class StateKind(models.TextChoices):
        DISTRICT = "DISTRICT", "District"
        CITY_CORPORATION = "CITY_CORPORATION", "City Corporation"

    country = models.ForeignKey(Country, on_delete=models.CASCADE, related_name="states")
    division = models.ForeignKey(
        Division, on_delete=models.SET_NULL, null=True, blank=True, related_name="states"
    )
    name = models.CharField(max_length=150)
    kind = models.CharField(
        max_length=20,
        choices=StateKind.choices,
        default=StateKind.DISTRICT,
    )
    source_code = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["country__name", "division__name", "name"]
        unique_together = [("country", "name")]

    def __str__(self) -> str:
        return self.name


class City(models.Model):
    class CityKind(models.TextChoices):
        UPAZILA_THANA = "UPAZILA_THANA", "Upazila / Thana"
        CITY_CORPORATION_THANA = "CITY_CORPORATION_THANA", "City Corporation Thana"
        PAURASHAVA = "PAURASHAVA", "Paurashava"

    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="cities")
    name = models.CharField(max_length=150)
    kind = models.CharField(
        max_length=30,
        choices=CityKind.choices,
        default=CityKind.UPAZILA_THANA,
    )
    source_code = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["state__name", "name"]
        unique_together = [("state", "name")]
        verbose_name_plural = "Cities"

    def __str__(self) -> str:
        return self.name


class UnionWard(models.Model):
    class UnionWardKind(models.TextChoices):
        UNION = "UNION", "Union"
        WARD = "WARD", "Ward"

    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name="union_wards")
    name = models.CharField(max_length=150)
    kind = models.CharField(
        max_length=10,
        choices=UnionWardKind.choices,
        default=UnionWardKind.UNION,
    )
    source_code = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["city__state__name", "city__name", "name"]
        unique_together = [("city", "name")]
        verbose_name = "Union / Ward"
        verbose_name_plural = "Unions / Wards"

    def __str__(self) -> str:
        return self.name


class Religion(models.Model):
    name = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Relation(models.Model):
    name = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Vaccine(models.Model):
    name = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Dose(models.Model):
    name = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Client(models.Model):
    name = models.CharField(max_length=200, unique=True)
    code = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Profile — singletons (OneToOne CustomUser)
# ---------------------------------------------------------------------------

class JobInfo(_Auditable):
    class ResourceStatus(models.TextChoices):
        BENCH = "BENCH", "Bench"
        GENERAL = "GENERAL", "General"
        LIVE = "LIVE", "Live"
        TRAINEE = "TRAINEE", "Trainee"

    class BillingStatus(models.TextChoices):
        BILLABLE = "BILLABLE", "Billable"
        NON_BILLABLE = "NON_BILLABLE", "Non-Billable"

    employee = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="job_info",
    )
    reports_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="direct_reports",
    )
    resource_status = models.CharField(
        max_length=20, choices=ResourceStatus.choices, blank=True,
    )
    billing_status = models.CharField(
        max_length=20, choices=BillingStatus.choices, blank=True,
    )
    cost_center = models.ForeignKey(
        "accounts.CostCenter", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="job_infos",
    )
    sub_project = models.ForeignKey(
        "accounts.SubProject", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="job_infos",
    )
    surviving_children_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="Used by the Maternity Leave eligibility rule (must be < 2).",
    )
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Job info"
        verbose_name_plural = "Job info"

    def __str__(self) -> str:
        return f"{self.employee} — job info"


class PersonalInfo(_Auditable):
    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    class BloodGroup(models.TextChoices):
        A_POS = "A+", "A+"
        A_NEG = "A-", "A-"
        B_POS = "B+", "B+"
        B_NEG = "B-", "B-"
        AB_POS = "AB+", "AB+"
        AB_NEG = "AB-", "AB-"
        O_POS = "O+", "O+"
        O_NEG = "O-", "O-"

    employee = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="personal_info",
    )
    date_of_birth = models.DateField(null=True, blank=True, db_index=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    religion = models.ForeignKey(
        Religion, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    height_in_inch = models.PositiveIntegerField(null=True, blank=True)
    weight_in_kg = models.PositiveIntegerField(null=True, blank=True)
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True)
    identification_mark = models.CharField(max_length=200, blank=True)
    nationality = models.CharField(max_length=100, blank=True, default="Bangladeshi")
    father_name = models.CharField(max_length=200, blank=True)
    mother_name = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Personal info"
        verbose_name_plural = "Personal info"

    def __str__(self) -> str:
        return f"{self.employee} — personal info"


class FamilyInfo(_Auditable):
    class MaritalStatus(models.TextChoices):
        MARRIED = "MARRIED", "Married"
        UNMARRIED = "UNMARRIED", "Unmarried"
        DIVORCED = "DIVORCED", "Divorced"
        WIDOW = "WIDOW", "Widow"
        WIDOWER = "WIDOWER", "Widower"

    employee = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="family_info",
    )
    marital_status = models.CharField(max_length=20, choices=MaritalStatus.choices, blank=True)
    spouse_name = models.CharField(max_length=200, blank=True)
    marriage_date = models.DateField(null=True, blank=True)
    total_number_of_children = models.PositiveSmallIntegerField(default=0)
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Family info"
        verbose_name_plural = "Family info"

    def __str__(self) -> str:
        return f"{self.employee} — family info"


class LegalIssue(_Auditable):
    employee = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="legal_issue",
    )
    has_history_of_drug_use = models.BooleanField(default=False)
    is_convicted = models.BooleanField(default=False)
    case_document = models.ImageField(upload_to="legal/", null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Legal issue"
        verbose_name_plural = "Legal issues"

    def __str__(self) -> str:
        return f"{self.employee} — legal"


# ---------------------------------------------------------------------------
# Profile — repeating (FK CustomUser)
# ---------------------------------------------------------------------------

class Address(_Auditable):
    class AddressType(models.TextChoices):
        PERMANENT = "PERMANENT", "Permanent"
        PRESENT = "PRESENT", "Present"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses",
    )
    address_type = models.CharField(max_length=10, choices=AddressType.choices)
    flat_no = models.CharField(max_length=100, blank=True)
    holding_no = models.CharField(max_length=100, blank=True)
    village_area = models.CharField(max_length=200, blank=True)
    post_office = models.CharField(max_length=200, blank=True)
    union_name = models.CharField(max_length=200, blank=True)
    post_code = models.CharField(max_length=5, blank=True)
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="+")
    state = models.ForeignKey(
        State, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    city = models.ForeignKey(
        City, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    union_ward = models.ForeignKey(
        UnionWard, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["employee", "address_type"]
        verbose_name_plural = "Addresses"

    def __str__(self) -> str:
        return f"{self.get_address_type_display()} — {self.employee}"


class Education(_Auditable):
    class DegreeLevel(models.TextChoices):
        PSC = "PSC", "PSC"
        JSC = "JSC", "JSC"
        SSC = "SSC", "SSC"
        HSC = "HSC", "HSC"
        BACHELOR = "BACHELOR", "Bachelor's Degree"
        MASTER = "MASTER", "Master's Degree"
        DOCTORAL = "DOCTORAL", "Doctoral Degree"

    class Result(models.TextChoices):
        DIVISION = "DIVISION", "Division"
        CLASS = "CLASS", "Class"
        GRADE = "GRADE", "Grade"

    class InstitutionCategory(models.TextChoices):
        NATIONAL = "NATIONAL", "National"
        PUBLIC = "PUBLIC", "Public"
        PRIVATE = "PRIVATE", "Private"
        DISTANCE = "DISTANCE", "Distance Learning"
        FOREIGN = "FOREIGN", "Foreign"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="educations",
    )
    # Only degree_level is required. result / result_details / passing_year /
    # academic_institution / institution_category were relaxed 2026-10-05
    # (docs/LEGACY_60_IMPORT_PLAN.md §2.3): HR's legacy records carry just a
    # level label ("Graduate", "Post Graduate"), and those rows are needed on
    # file for audit before the details can be completed.
    degree_level = models.CharField(max_length=20, choices=DegreeLevel.choices)
    degree_name = models.CharField(max_length=200, blank=True)
    major = models.CharField(max_length=200, blank=True)
    result = models.CharField(max_length=20, choices=Result.choices, blank=True)
    result_details = models.CharField(
        max_length=200, blank=True,
        help_text="e.g. First Class, 3.73 out of 4.00, A+",
    )
    passing_year = models.PositiveIntegerField(null=True, blank=True, validators=[year_validator])
    academic_institution = models.CharField(max_length=255, blank=True)
    institution_category = models.CharField(
        max_length=20, choices=InstitutionCategory.choices, blank=True,
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-passing_year"]

    def __str__(self) -> str:
        level = self.get_degree_level_display()
        return f"{level} — {self.academic_institution}" if self.academic_institution else level


class Contact(_Auditable):
    class ContactType(models.TextChoices):
        SELF = "SELF", "Self"
        EMERGENCY = "EMERGENCY", "Emergency"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="contacts",
    )
    contact_type = models.CharField(max_length=10, choices=ContactType.choices)
    contact_number = models.CharField(max_length=50)
    emergency_contact_name = models.CharField(max_length=200, blank=True)
    relation_with_emergency_contact = models.ForeignKey(
        Relation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    emergency_contact_address = models.TextField(blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["employee", "contact_type"]

    def __str__(self) -> str:
        return f"{self.get_contact_type_display()} — {self.contact_number}"


class Nominee(_Auditable):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="nominees",
    )
    nominee_name = models.CharField(max_length=200)
    relation_with_nominee = models.ForeignKey(
        Relation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    nominee_occupation = models.CharField(max_length=200, blank=True)
    nominee_contact_number = models.CharField(max_length=50)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["employee", "nominee_name"]

    def __str__(self) -> str:
        return self.nominee_name


class Email(_Auditable):
    class EmailType(models.TextChoices):
        PERSONAL = "PERSONAL", "Personal"
        OFFICIAL = "OFFICIAL", "Official"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="extra_emails",
    )
    email_type = models.CharField(max_length=10, choices=EmailType.choices)
    email_address = models.EmailField()

    class Meta:
        ordering = ["employee", "email_type"]

    def __str__(self) -> str:
        return f"{self.get_email_type_display()} — {self.email_address}"


class IdentificationNumber(_Auditable):
    class IdType(models.TextChoices):
        NID = "NID", "NID Number"
        BIRTH = "BIRTH", "Birth Certificate Number"
        PASSPORT = "PASSPORT", "Passport Number"
        TIN = "TIN", "TIN"
        DRIVING = "DRIVING", "Driving License Number"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="id_numbers",
    )
    id_type = models.CharField(max_length=20, choices=IdType.choices)
    id_number = models.CharField(max_length=100)

    class Meta:
        ordering = ["employee", "id_type"]
        verbose_name = "Identification number"

    def __str__(self) -> str:
        return f"{self.get_id_type_display()} — {self.id_number}"


class Reference(_Auditable):
    class ReferenceType(models.TextChoices):
        PERSONAL = "PERSONAL", "Personal"
        OFFICIAL = "OFFICIAL", "Official"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="references",
    )
    reference_type = models.CharField(max_length=10, choices=ReferenceType.choices)
    reference_name = models.CharField(max_length=200)
    reference_contact_number = models.CharField(max_length=50)
    relation_with_reference = models.ForeignKey(
        Relation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    reference_organization = models.CharField(max_length=200, blank=True)
    reference_designation = models.CharField(max_length=200, blank=True)
    reference_email_address = models.EmailField(blank=True)

    class Meta:
        ordering = ["employee", "reference_name"]

    def __str__(self) -> str:
        return f"{self.get_reference_type_display()} — {self.reference_name}"


class ProfessionalCertification(_Auditable):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="certifications",
    )
    certificate_name = models.CharField(max_length=255)
    # institution / year relaxed 2026-10-05 — same reason as Education above.
    certificate_institution = models.CharField(max_length=255, blank=True)
    certificate_year = models.PositiveIntegerField(null=True, blank=True, validators=[year_validator])

    class Meta:
        ordering = ["-certificate_year"]
        verbose_name = "Professional certification"

    def __str__(self) -> str:
        if self.certificate_year:
            return f"{self.certificate_name} ({self.certificate_year})"
        return self.certificate_name


class Vaccination(_Auditable):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vaccinations",
    )
    vaccine = models.ForeignKey(Vaccine, on_delete=models.PROTECT, related_name="+")
    dose = models.ForeignKey(Dose, on_delete=models.PROTECT, related_name="+")
    taken_date = models.DateField()

    class Meta:
        ordering = ["-taken_date"]

    def __str__(self) -> str:
        return f"{self.vaccine} — {self.dose} ({self.taken_date})"


class Image(_Auditable):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="images",
    )
    image = models.ImageField(upload_to="employees/")
    caption = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-added_at"]

    def __str__(self) -> str:
        return self.caption or str(self.image)


class DesignationHistory(_Auditable):
    """Effective-dated designation/title history — mirrors
    payroll.SalaryStructure's effective-dated-revision shape
    (payroll/models.py). Feeds the Service Book's Part 3 ("post of
    service" record, Bangladesh Labour Rules 2015/2022 Form-7), which
    CustomUser.designation alone can't provide — it's a single current FK
    with no record of past titles.

    Kept in sync automatically by employees/signals.py whenever
    CustomUser.designation changes (profile edits, bulk onboarding, SSO
    auto-provisioning) — HR never has to remember a second step. The
    profile drawer here is for backfilling genuine historical rows the
    signal never saw (pre-Orbis promotions).

    Only one open-ended (``effective_to`` null) row per employee — the
    "current" designation. MySQL doesn't enforce the partial unique below,
    so ``clean()`` mirrors it (same posture as SalaryStructure/
    DedicatedNightShiftAssignment).
    """

    class Reason(models.TextChoices):
        INITIAL = "INITIAL", "Initial designation"
        PROMOTION = "PROMOTION", "Promotion"
        DEMOTION = "DEMOTION", "Demotion"
        TRANSFER = "TRANSFER", "Transfer"
        CORRECTION = "CORRECTION", "Correction"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="designation_history",
    )
    designation = models.ForeignKey(
        "accounts.Designation", on_delete=models.PROTECT, related_name="+",
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True, help_text="Blank = current.")
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.CORRECTION)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-effective_from"]
        verbose_name = "Designation history"
        verbose_name_plural = "Designation history"
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(effective_to__isnull=True),
                name="employees_one_current_designation_per_employee",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.designation} from {self.effective_from}"

    def clean(self):
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Effective-to cannot precede effective-from."})
        if self.employee_id and self.effective_to is None:
            conflict_qs = DesignationHistory.objects.filter(
                employee_id=self.employee_id, effective_to__isnull=True,
            )
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError(
                    "This employee already has a current designation-history row "
                    "— set its effective_to before adding another open row."
                )


class TeamAssignment(_Auditable):
    """Effective-dated team + resource-status history — the dated record
    ``CustomUser.team`` / ``JobInfo.resource_status`` cannot provide, since
    both are single mutable values with no memory of when they changed.

    Added 2026-09-19 for Data Solutions' trainee go-live
    (docs/DSNDD_PAYROLL_AUTOMATION_PLAN.md Phase B). A trainee on 11,000
    moves to team MARS on a known date and their package becomes 18,000
    from that date — but "the date" existed only in a free-text Supervisor
    Note, so payroll could not split the period even after the engine
    learned how to (Phase A). This is what supplies the date.

    Same shape as ``DesignationHistory`` directly above, including its
    auto-sync signal (``employees/signals.py``): HR's ordinary "move to
    MARS, set Live" profile edit *is* the dated record, with no second step
    to remember. The drawer exists to backdate — HR usually updates a few
    days after the real go-live — and to correct.

    One refinement over DesignationHistory's signal: a change on a day that
    already has an open row starting *today* updates that row in place
    rather than closing it and opening another. Team and resource status
    live on two different models (``CustomUser`` and ``JobInfo``) and a
    go-live changes both, so without this a single HR action would leave a
    one-day orphan row behind.

    Only one open-ended (``effective_to`` null) row per employee. MySQL
    doesn't enforce the partial unique below, so ``clean()`` mirrors it
    (same posture as SalaryStructure / DesignationHistory).

    **This model records what happened; it never decides pay.** Resolving a
    package for the new assignment and proposing a salary revision is
    ``payroll.services``' job — see ``propose_salary_for_assignment``.
    """

    class Reason(models.TextChoices):
        INITIAL = "INITIAL", "Initial assignment"
        GO_LIVE = "GO_LIVE", "Go live"
        TRANSFER = "TRANSFER", "Transfer"
        BENCH = "BENCH", "Moved to bench"
        CORRECTION = "CORRECTION", "Correction"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_assignments",
    )
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Blank = no team on file at this point.",
    )
    resource_status = models.CharField(
        max_length=20, choices=JobInfo.ResourceStatus.choices, blank=True,
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True, help_text="Blank = current.")
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.CORRECTION)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-effective_from"]
        indexes = [
            models.Index(fields=["employee", "effective_from"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(effective_to__isnull=True),
                name="employees_one_current_team_assignment_per_employee",
            ),
        ]

    def __str__(self) -> str:
        where = self.team or self.department or "unassigned"
        return f"{self.employee} · {where} from {self.effective_from}"

    def clean(self):
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Effective-to cannot precede effective-from."})
        if self.employee_id and self.effective_to is None:
            conflict_qs = TeamAssignment.objects.filter(
                employee_id=self.employee_id, effective_to__isnull=True,
            )
            if self.pk:
                conflict_qs = conflict_qs.exclude(pk=self.pk)
            if conflict_qs.exists():
                raise ValidationError(
                    "This employee already has a current team assignment "
                    "— set its effective_to before adding another open row."
                )


class DedicatedNightShiftAssignment(_Auditable):
    """An employee's dedicated-night-duty window — commonly one quarter, but
    an employee can cycle in and out of dedicated duty repeatedly over their
    tenure, so this is a repeatable dated row, not a single flag/date-pair on
    JobInfo. Added 2026-09-07 for AdOps's night-shift policy (docs/PAYROLL_PLAN.md).

    A Team-Leader-or-above manager may request one for someone in their
    managed scope (accounts.permissions.can_request_dedicated_night_shift);
    only an APM-or-above manager may approve it
    (can_approve_dedicated_night_shift). `added_by`/`added_at` (from
    _Auditable) record who requested it and when — the same role
    AttendanceCorrection.requested_by/requested_at play there.

    Only an APPROVED row whose window covers a shift's date makes
    payroll.PayRate.RateType.NIGHT_QUARTERLY resolve for that employee/date
    (payroll/inputs.py's shared night-shift classification helper) — a
    PENDING/DECLINED/CANCELLED row pays nothing extra. Lives here, not in
    payroll: this is an HR-managed org fact approved through the reporting
    hierarchy (leave.LeaveApplication/attendance.AttendanceCorrection's
    shape), not a payroll-gated financial record like
    payroll.EmployeeBankAccount. payroll's engine reads this the same way it
    already reads JobInfo — it never owns a second copy.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dedicated_night_shift_assignments",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "start_date"]),
        ]
        verbose_name = "Dedicated night shift assignment"

    def __str__(self) -> str:
        return f"{self.employee} · {self.start_date}–{self.end_date} ({self.get_status_display()})"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after the start date."})
        if self.status == self.Status.APPROVED and self.employee_id and self.start_date and self.end_date:
            # MySQL doesn't enforce partial-unique/range constraints — this
            # clean() mirror is the enforcement, same posture as
            # offboarding.Resignation.clean().
            overlapping = DedicatedNightShiftAssignment.objects.filter(
                employee_id=self.employee_id, status=self.Status.APPROVED,
                start_date__lte=self.end_date, end_date__gte=self.start_date,
            ).exclude(pk=self.pk)
            if overlapping.exists():
                raise ValidationError(
                    "This employee already has an approved dedicated night shift assignment "
                    "overlapping this window."
                )


class DedicatedNightShiftAssignmentEvent(models.Model):
    """Append-only audit ledger for a Dedicated Night Shift assignment —
    one immutable row per transition. Same denormalised-state +
    event-ledger split as NightShiftConsent/NightShiftConsentEvent and
    policies.PolicyAcknowledgement/...Event; this model was built in
    2026-09-07 without the ledger half, and got it on 2026-09-15.

    The prompt was a real gap: `_Auditable` carries a single
    updated_by/updated_at pair, so a second edit silently overwrote the
    first, and nothing recorded *what* changed. Once a Supervisor could
    edit an APPROVED window (2026-09-15), that mattered — the window drives
    NIGHT_QUARTERLY pay, so "who moved this date, when, from what" needs to
    survive the next edit.

    `old_*`/`new_*` are only populated for events that actually move the
    window (EDITED, DEMOTED); an approve/decline leaves them null and the
    window is read off the assignment itself. A null `actor` means the
    system or a bulk import, never "unknown person" — the display layer
    renders that as "Imported" rather than inventing a name.
    """

    class Event(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        EDITED = "EDITED", "Window edited"
        DEMOTED = "DEMOTED", "Edited, sent back for approval"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"
        IMPORTED = "IMPORTED", "Imported"
        CANCELLED = "CANCELLED", "Cancelled"

    assignment = models.ForeignKey(
        DedicatedNightShiftAssignment, on_delete=models.CASCADE, related_name="events",
    )
    event = models.CharField(max_length=10, choices=Event.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="Who triggered it. Null = system or bulk import, not 'unknown'.",
    )
    old_start_date = models.DateField(null=True, blank=True)
    old_end_date = models.DateField(null=True, blank=True)
    new_start_date = models.DateField(null=True, blank=True)
    new_end_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        verbose_name = "Dedicated night shift assignment event"
        indexes = [
            models.Index(fields=["assignment", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.assignment_id} · {self.get_event_display()} @ {self.occurred_at:%Y-%m-%d %H:%M}"

    @property
    def window_changed(self) -> bool:
        """True when this event carries a real before/after window pair."""
        return bool(
            (self.old_start_date or self.old_end_date)
            and (self.old_start_date, self.old_end_date) != (self.new_start_date, self.new_end_date)
        )


class NightShiftConsent(_Auditable):
    """Monthly written consent from a female employee to work her scheduled
    night shift(s) — Bangladesh Labour Act 2006 s.109 (as amended 2018)
    requires a female worker's consent to work between 10pm-6am, and she may
    withdraw it at any time. One row per (employee, period); `period` is
    always the first day of a calendar month.

    Denormalised current state, mirroring policies.PolicyAcknowledgement —
    every change is also appended to NightShiftConsentEvent for the
    immutable compliance trail. Defaults to `consents=True` the instant a
    row is created (an employee is assumed willing unless she actively
    declines) via employees.services.get_or_create_night_shift_consent,
    the same self-healing get-or-create pattern
    leave.services.get_or_create_balance uses. A monthly Celery beat job
    (generate_night_shift_consents) also bulk-creates rows so HR's audit
    list is complete even for an employee who never opens the dashboard
    that month.
    """

    class Source(models.TextChoices):
        AUTO_DEFAULT = "AUTO_DEFAULT", "Auto-defaulted"
        EMPLOYEE = "EMPLOYEE", "Employee"
        HR_ON_BEHALF = "HR_ON_BEHALF", "HR on behalf"

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="night_shift_consents",
    )
    period = models.DateField(help_text="First day of the calendar month this consent covers.")
    consents = models.BooleanField(default=True)
    source = models.CharField(max_length=15, choices=Source.choices, default=Source.AUTO_DEFAULT)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-period", "employee"]
        verbose_name = "Night shift consent"
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "period"],
                name="employees_unique_night_shift_consent_per_month",
            ),
        ]
        indexes = [
            models.Index(fields=["period", "consents"]),
            models.Index(fields=["employee", "period"]),
        ]

    def __str__(self) -> str:
        state = "Consents" if self.consents else "Declined"
        return f"{self.employee} · {self.period:%Y-%m} · {state}"

    def clean(self):
        if self.period and self.period.day != 1:
            raise ValidationError({"period": "Period must be the first day of a calendar month."})


class NightShiftConsentEvent(models.Model):
    """Append-only audit ledger — one row per state change. Mirrors
    policies.PolicyAcknowledgementEvent / leave.LeaveBalanceTransaction:
    immutable, never updated, the source of truth for "who decided what and
    when" for a compliance record that must survive an audit.
    """

    class Event(models.TextChoices):
        GENERATED = "GENERATED", "Generated"
        CONFIRMED = "CONFIRMED", "Confirmed"
        DECLINED = "DECLINED", "Declined"

    consent = models.ForeignKey(NightShiftConsent, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=12, choices=Event.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="Who triggered it (system=null, the employee, or HR on her behalf).",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["occurred_at"]
        verbose_name = "Night shift consent event"
        indexes = [
            models.Index(fields=["consent", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.consent_id} · {self.get_event_display()} @ {self.occurred_at:%Y-%m-%d %H:%M}"


class ProfileSelfServiceEvent(models.Model):
    """Append-only audit ledger for CustomUser.profile_self_service_enabled.

    The current state lives directly on CustomUser (checked on every
    profile-page permission call, so it shouldn't cost a join); this table
    is only the "who flipped it and when" trail HR needs since the whole
    workflow (employee asks out-of-band, HR flips it on, employee edits,
    employee notifies HR, HR flips it back off) otherwise leaves no record
    of itself. Every write funnels through
    employees.services::set_profile_self_service so the ledger can't drift
    from the flag — mirrors NightShiftConsentEvent / DedicatedNightShiftAssignmentEvent.
    """

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="profile_self_service_events",
    )
    enabled = models.BooleanField()
    occurred_at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="HR user who made this change.",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["occurred_at"]
        verbose_name = "Profile self-service event"
        indexes = [
            models.Index(fields=["employee", "occurred_at"]),
        ]

    def __str__(self) -> str:
        state = "Enabled" if self.enabled else "Disabled"
        return f"{self.employee_id} · {state} @ {self.occurred_at:%Y-%m-%d %H:%M}"
