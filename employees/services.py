"""Cross-cutting employees business logic shared by forms.py and views.py.

Split out to avoid a views.py <-> forms.py circular import (views.py already
imports AvatarForm from forms.py).
"""

from datetime import date, timedelta

from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from accounts.models import AccessLevel, CustomUser
from accounts.permissions import ALL_DEPARTMENTS_GROUPS

# "Team Lead and above" floor — mirrors accounts/permissions.py's
# SELF_EDIT_ROSTER_MIN_ACCESS_LEVEL_NAME rule for who may be picked as a manager.
MANAGER_MIN_ACCESS_LEVEL_NAME = "Supervisor"


def reports_to_candidates(target):
    """Eligible "Reports to" picks for `target`.

    Supervisor+ access level, same department as target, plus anyone in
    ALL_DEPARTMENTS_GROUPS (User Admins/HR/Senior Leadership) regardless of
    department — covers execs who oversee multiple departments (e.g. a CTO
    over IT + Software Development) without needing their own department
    field changed just to make them selectable.
    """
    qs = CustomUser.objects.filter(
        is_active=True,
        designation__access_level__rank__gte=(
            AccessLevel.objects.filter(name=MANAGER_MIN_ACCESS_LEVEL_NAME)
            .values_list("rank", flat=True).first() or 0
        ),
    )
    if target:
        qs = qs.exclude(pk=target.pk)
        if target.department_id:
            qs = qs.filter(
                Q(department_id=target.department_id)
                | Q(groups__name__in=ALL_DEPARTMENTS_GROUPS)
            ).distinct()
    return qs


def build_service_book_context(employee) -> dict:
    """Assemble every data point for the Bangladesh Labour Rules 2015/2022
    Form-7 "Service Book" (Parts 1-5) from data already tracked elsewhere in
    Orbis — see /home/smrashel/.claude/plans/vivid-honking-pillow.md for the
    field-by-field mapping this implements.

    Shared verbatim by the profile's read-only Service Book page
    (employees/views.py::EmployeeServiceBookView) and the on-demand PDF
    render (documents/pdf.py's service-book context block) so the page and
    the PDF can never drift apart — both are just different templates over
    this one dict, computed fresh on every call (no caching, no persisted
    snapshot: it's meant to always reflect current data).
    """
    from documents.models import CompanyBranding
    from documents.pdf import _field_to_data_uri
    from employees.models import (
        Address, DesignationHistory, Education, IdentificationNumber,
        Image, ProfessionalCertification,
    )
    from leave.models import EncashmentRequest, LeaveApplication, LeavePolicy
    from leave.services import get_or_create_balance, live_accrued_al, live_used
    from offboarding.models import Resignation
    from payroll.models import SalaryStructure, VariablePayEntry
    from roster.models import Shift

    today = date.today()

    def _format_address(row) -> str:
        if row is None:
            return ""
        parts = [
            row.flat_no, row.holding_no, row.village_area,
            row.post_office, row.union_name,
            str(row.city) if row.city_id else "", str(row.state) if row.state_id else "",
            row.post_code, str(row.country),
        ]
        return ", ".join(p for p in parts if p)

    def _age(dob):
        if not dob:
            return None
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    def _height_cm(personal):
        if not personal or not personal.height_in_inch:
            return None
        return (personal.height_in_inch * Decimal("2.54")).quantize(Decimal("1"))

    def _highest_education(rows):
        rank = {
            Education.DegreeLevel.PSC: 0, Education.DegreeLevel.JSC: 1,
            Education.DegreeLevel.SSC: 2, Education.DegreeLevel.HSC: 3,
            Education.DegreeLevel.BACHELOR: 4, Education.DegreeLevel.MASTER: 5,
            Education.DegreeLevel.DOCTORAL: 6,
        }
        return max(rows, key=lambda e: rank.get(e.degree_level, -1)) if rows else None

    def _al_leave_periods(al_policy):
        """Chronological Annual Leave periods for Form-7 Part 4 — merges
        approved LeaveApplications with contiguous-date-grouped roster-direct
        AL Shift rows (bypassing the application form is the common real
        path, per leave/signals.py), since neither source alone is complete.
        `leave_application__isnull=True` excludes Shift rows already
        materialised from one of the LeaveApplications above, avoiding
        double-counting the same days. Each row's remaining balance reuses
        live_accrued_al/live_used's `upto` kwarg rather than re-deriving a
        running ledger total by hand.
        """
        periods = [
            {"start": a.start_date, "end": a.end_date, "total_days": a.effective_days}
            for a in LeaveApplication.objects.filter(
                employee=employee, leave_type=al_policy.shift_type,
                status=LeaveApplication.Status.APPROVED,
            )
        ]
        direct_dates = list(
            Shift.objects.filter(
                employee=employee, shift_type=al_policy.shift_type,
                leave_application__isnull=True,
            ).order_by("date").values_list("date", flat=True)
        )
        run_start = prev = None
        for d in direct_dates:
            if run_start is None:
                run_start = d
            elif (d - prev).days > 1:
                periods.append({
                    "start": run_start, "end": prev,
                    "total_days": Decimal((prev - run_start).days + 1),
                })
                run_start = d
            prev = d
        if run_start is not None:
            periods.append({
                "start": run_start, "end": prev,
                "total_days": Decimal((prev - run_start).days + 1),
            })
        periods.sort(key=lambda p: p["start"])

        rows = []
        for p in periods:
            bal = get_or_create_balance(employee, al_policy, p["end"])
            accrued = live_accrued_al(employee, al_policy, bal, upto=p["end"])
            used = live_used(employee, al_policy, bal, upto=p["end"])
            rows.append({
                **p,
                "remaining": bal.opening + accrued + bal.carried_forward - used - bal.encashed,
            })
        return rows

    def _al_legacy_rows(al_policy):
        """HR's pre-Orbis AL record rolled forward one year (2026-10-05,
        docs/LEGACY_60_IMPORT_PLAN.md §4.4) — the Service Book is the ONLY
        place the record appears. Per row:

          accrued_prior  = the balance HR carried into Orbis (the 60-cap column)
          taken          = AL Orbis has recorded in the year after `as_of`
                           (approved applications' effective days + roster AL
                           days not tied to an application — no double count)
          year_balance   = the year's entitlement (policy max, 12) − taken,
                           floored at 0; any excess comes off the prior balance
          carried        = min(cap 60, accrued_prior − excess + year_balance)

        Same arithmetic as HR's own sheet, one year on. Pure display — never
        written anywhere, never read by the live ledger.
        """
        from leave.models import LegacyAnnualLeaveRecord

        entitlement = Decimal(al_policy.max_days_per_year or 12)
        cap = Decimal(al_policy.max_carry_forward_days or 60)
        today = date.today()
        # One stored record per employee in practice (the cut-over snapshot);
        # the latest wins if there were ever more. From it, one COMPUTED row
        # per calendar year up to today, each year's "accrued to prior
        # year-end" being the previous year's carried balance — so the table
        # grows a row a year without anything being stored.
        rec = LegacyAnnualLeaveRecord.objects.filter(employee=employee).order_by("-as_of").first()
        if rec is None:
            return []
        rows = []
        accrued_prior = rec.capped_balance
        for year in range(rec.as_of.year + 1, today.year + 1):
            year_start, year_end = date(year, 1, 1), min(date(year, 12, 31), today)
            from_apps = sum(
                (a.effective_days for a in LeaveApplication.objects.filter(
                    employee=employee, leave_type=al_policy.shift_type,
                    status=LeaveApplication.Status.APPROVED,
                    start_date__gte=year_start, start_date__lte=year_end,
                )),
                Decimal("0"),
            )
            direct = Shift.objects.filter(
                employee=employee, shift_type=al_policy.shift_type,
                leave_application__isnull=True, date__gte=year_start, date__lte=year_end,
            ).count()
            taken = from_apps + Decimal(direct)
            year_balance = max(Decimal("0"), entitlement - taken)
            excess = max(Decimal("0"), taken - entitlement)
            carried = min(cap, accrued_prior - excess + year_balance)
            first = year == rec.as_of.year + 1
            note = rec.note if first else ""
            source = f"HR records as at {rec.as_of.isoformat()}" if first else ""
            rows.append({
                "year": year, "accrued_prior": accrued_prior, "taken": taken,
                "entitlement": entitlement, "year_balance": year_balance, "carried": carried,
                "note": "; ".join(part for part in (source, note) if part),
                "as_of": rec.as_of,
            })
            accrued_prior = carried
        return rows

    def _al_encashment_log(al_policy):
        rows = []
        for e in EncashmentRequest.objects.filter(
            employee=employee, leave_policy=al_policy,
            status__in=(EncashmentRequest.Status.APPROVED, EncashmentRequest.Status.PAID),
        ).order_by("processed_at"):
            as_of = e.processed_at.date() if e.processed_at else today
            bal = get_or_create_balance(employee, al_policy, as_of)
            accrued = live_accrued_al(employee, al_policy, bal, upto=as_of)
            used = live_used(employee, al_policy, bal, upto=as_of)
            rows.append({
                "total_days": e.days_approved,
                "date": as_of,
                "remaining": bal.opening + accrued + bal.carried_forward - used - bal.encashed,
            })
        return rows

    addresses = {row.address_type: row for row in Address.objects.filter(employee=employee)}
    identifications = list(
        IdentificationNumber.objects.filter(
            employee=employee,
            id_type__in=(IdentificationNumber.IdType.NID, IdentificationNumber.IdType.BIRTH),
        )
    )
    certifications = list(ProfessionalCertification.objects.filter(employee=employee))
    education = _highest_education(list(Education.objects.filter(employee=employee)))
    # Prefer the profile hero picture (CustomUser.profile_picture — the same
    # image shown in the topbar/employee list/detail hero) since that's the
    # one employees actually curate as "my photo"; fall back to the most
    # recent "Photos & documents" upload when no profile picture is set.
    # Reuses the same ImageField->data URI conversion the PDF pipeline
    # already uses for the company logo and signatory signatures.
    if employee.profile_picture:
        photo_data_uri = _field_to_data_uri(employee.profile_picture)
    else:
        latest_photo = Image.objects.filter(employee=employee).order_by("-added_at").first()
        photo_data_uri = _field_to_data_uri(latest_photo.image) if latest_photo else ""

    latest_resignation = (
        Resignation.objects
        .filter(employee=employee, status=Resignation.Status.ACCEPTED)
        .order_by("-last_working_date")
        .first()
    )

    designation_history = list(
        DesignationHistory.objects
        .filter(employee=employee)
        .select_related("designation")
        .order_by("effective_from")
    )
    salary_history = list(
        SalaryStructure.objects.filter(employee=employee).order_by("effective_from")
    )

    conduct_entries = list(
        VariablePayEntry.objects
        .filter(
            employee=employee,
            status=VariablePayEntry.Status.APPROVED,
            component__code__in=("DISCIPLINARY", "BONUS", "INCENTIVE"),
        )
        .select_related("component", "processed_by", "period")
        .order_by("processed_at", "period__start_date")
    )

    al_policy = LeavePolicy.objects.select_related("shift_type").filter(shift_type__name="AL").first()
    # "Annual Leave availed (Orbis)" table disabled 2026-10-05 at HR's request
    # (docs/LEGACY_60_IMPORT_PLAN.md §4.4) — _al_leave_periods is kept for
    # when it comes back. Not computed: it would get_or_create balance rows
    # as a side effect of a read-only page.
    al_periods: list = []
    al_encashment = _al_encashment_log(al_policy) if al_policy else []
    al_legacy = _al_legacy_rows(al_policy) if al_policy else []

    personal = getattr(employee, "personal_info", None)
    company = CompanyBranding.load()

    return {
        "sb_personal": personal,
        "sb_family": getattr(employee, "family_info", None),
        "sb_age": _age(getattr(personal, "date_of_birth", None)),
        "sb_height_cm": _height_cm(personal),
        "sb_photo_data_uri": photo_data_uri,
        "sb_education": education,
        "sb_certifications": certifications,
        "sb_address_permanent": _format_address(addresses.get(Address.AddressType.PERMANENT)) or None,
        "sb_address_present": _format_address(addresses.get(Address.AddressType.PRESENT)) or None,
        "sb_identifications": identifications,
        "sb_service_book_opened": employee.joining_date,
        "sb_company": company,
        "sb_authorized_signatory": company.default_signatory,
        "sb_register_serial_no": employee.register_serial_no,
        "sb_employment": {
            "joining_date": employee.joining_date,
            "last_working_date": employee.last_working_date,
            "resignation": latest_resignation,
        },
        "sb_designation_history": designation_history,
        "sb_salary_history": salary_history,
        "sb_al_periods": al_periods,
        "sb_al_encashment": al_encashment,
        "sb_al_legacy": al_legacy,
        "sb_conduct": conduct_entries,
    }


# ---------------------------------------------------------------------------
# Night shift consent (Bangladesh Labour Act 2006 s.109, amended 2018) — see
# employees/models.py::NightShiftConsent for the full legal/design rationale.
# ---------------------------------------------------------------------------

def current_night_shift_consent_period(today: date | None = None) -> date:
    """The calendar-month key (always the 1st) a fresh consent decision
    belongs to right now."""
    return (today or date.today()).replace(day=1)


def _month_bounds(period: date) -> tuple[date, date]:
    start = period.replace(day=1)
    if start.month == 12:
        end = date(start.year, 12, 31)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return start, end


def is_female_employee(employee) -> bool:
    personal = getattr(employee, "personal_info", None)
    return bool(personal and personal.gender == personal.Gender.FEMALE)


def _has_night_shift_in_range(employee, start: date, end: date) -> bool:
    """True iff `employee` has at least one WORK-category, midnight-crossing
    shift-day in [start, end] — a concrete `roster.Shift` row wins, falling
    back to the `roster.Roster` weekly pattern for any date without one
    (same resolution order as core/dashboard.py::_build_week_strip).

    Reuses the single canonical "crosses midnight" derivation
    (payroll.inputs.is_night_shift/effective_shift_times) rather than
    re-deriving it — see docs/PAYROLL_PLAN.md's night-shift-detection note
    for why a second, independent implementation of this rule is exactly
    what bit this codebase once already (the reverted
    ShiftType.is_night_shift boolean).
    """
    from payroll.inputs import effective_shift_times, is_night_shift
    from roster.models import Roster, Shift, ShiftType

    shifts = {
        s.date: s for s in (
            Shift.objects.filter(employee=employee, date__gte=start, date__lte=end)
            .select_related("shift_type", "shift_attribute")
        )
    }
    pattern = {
        r.weekday: r for r in (
            Roster.objects.filter(employee=employee)
            .select_related("shift_type", "shift_attribute")
        )
    }
    day = start
    while day <= end:
        cell = shifts.get(day) or pattern.get(day.weekday())
        if cell is not None and cell.shift_type.category == ShiftType.Category.WORK:
            if is_night_shift(*effective_shift_times(cell)):
                return True
        day += timedelta(days=1)
    return False


def qualifies_for_night_shift_consent(employee, period: date | None = None) -> bool:
    """True iff `employee` is a female employee with at least one night
    shift scheduled anywhere in `period`'s calendar month (defaults to the
    current month) — the trigger for showing/needing a consent record."""
    if not employee or not employee.is_active or not is_female_employee(employee):
        return False
    period = period or current_night_shift_consent_period()
    start, end = _month_bounds(period)
    return _has_night_shift_in_range(employee, start, end)


def get_or_create_night_shift_consent(employee, period: date | None = None, actor=None):
    """Self-healing getter — mirrors leave.services.get_or_create_balance.

    Returns `(consent, created)`, or `(None, False)` when `employee` doesn't
    currently qualify (so callers never manufacture a record for someone
    with no scheduled night shift this month). A newly-created row defaults
    to `consents=True` and logs a GENERATED event.
    """
    from employees.models import NightShiftConsent, NightShiftConsentEvent

    period = period or current_night_shift_consent_period()
    if not qualifies_for_night_shift_consent(employee, period):
        return None, False

    consent, created = NightShiftConsent.objects.get_or_create(
        employee=employee, period=period,
        defaults={"source": NightShiftConsent.Source.AUTO_DEFAULT},
    )
    if created:
        NightShiftConsentEvent.objects.create(
            consent=consent, event=NightShiftConsentEvent.Event.GENERATED, actor=actor,
        )
    return consent, created


def record_night_shift_consent_decision(employee, consents: bool, actor, note: str = ""):
    """Record `employee`'s own (or HR's on-behalf) decision for the current
    month. Always updates the current-month row — a decision only ever
    applies to the month it's made in, never retroactively to a closed one.
    """
    from django.core.exceptions import ValidationError

    from employees.models import NightShiftConsent, NightShiftConsentEvent

    period = current_night_shift_consent_period()
    consent, _created = get_or_create_night_shift_consent(employee, period, actor=actor)
    if consent is None:
        raise ValidationError(
            "This employee has no night shift scheduled this month — "
            "there's nothing to consent to or decline."
        )

    consent.consents = consents
    consent.source = (
        NightShiftConsent.Source.EMPLOYEE if actor and actor.pk == employee.pk
        else NightShiftConsent.Source.HR_ON_BEHALF
    )
    consent.decided_at = timezone.now()
    consent.decided_by = actor
    consent.note = note
    consent.updated_by = actor
    consent.save(update_fields=[
        "consents", "source", "decided_at", "decided_by", "note", "updated_by", "updated_at",
    ])
    NightShiftConsentEvent.objects.create(
        consent=consent,
        event=NightShiftConsentEvent.Event.CONFIRMED if consents else NightShiftConsentEvent.Event.DECLINED,
        actor=actor,
        note=note,
    )
    return consent


# ---------------------------------------------------------------------------
# Profile self-service toggle
# ---------------------------------------------------------------------------

def set_profile_self_service(employee, enabled: bool, actor, note: str = ""):
    """Flip CustomUser.profile_self_service_enabled and append the ledger
    row in one atomic write, so ProfileSelfServiceEvent can never drift from
    the flag. A no-op click (state already matches) still logs — HR
    re-confirming "still off" for an audit is a legitimate action, not
    noise, and it costs nothing to record.
    """
    from django.db import transaction

    from employees.models import ProfileSelfServiceEvent

    with transaction.atomic():
        employee.profile_self_service_enabled = enabled
        employee.save(update_fields=["profile_self_service_enabled"])
        ProfileSelfServiceEvent.objects.create(
            employee=employee, enabled=enabled, actor=actor, note=note,
        )
    return employee


# ---------------------------------------------------------------------------
# Dedicated Night Shift — audit ledger
# ---------------------------------------------------------------------------

def record_dedicated_night_shift_event(
    assignment, event, actor, *, old_window=None, note: str = "",
):
    """Append one immutable row to a Dedicated Night Shift assignment's
    audit ledger (DedicatedNightShiftAssignmentEvent).

    Every write path funnels through here so the ledger can't drift from
    the row: the request view, the edit view, and the shared
    _approve_night_shift/_decline_night_shift helpers — which means the
    single-row and bulk approve/decline actions both record without
    knowing they do.

    `old_window` is a (start_date, end_date) tuple, passed only by callers
    that actually move the window; the new window is always read off
    `assignment`, so it can't disagree with what was saved. `actor` may be
    None for system/import writes — the display layer shows that as
    "Imported", never as an unknown person.
    """
    from employees.models import DedicatedNightShiftAssignmentEvent

    old_start, old_end = old_window if old_window else (None, None)
    return DedicatedNightShiftAssignmentEvent.objects.create(
        assignment=assignment,
        event=event,
        actor=actor,
        old_start_date=old_start,
        old_end_date=old_end,
        new_start_date=assignment.start_date,
        new_end_date=assignment.end_date,
        note=note,
    )
