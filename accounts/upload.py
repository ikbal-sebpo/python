"""Bulk new-joiner onboarding — flat one-row-per-employee upload.

Creates CustomUser (+ optional JobInfo, + optional SalaryStructure) for a
batch of new hires from a single spreadsheet. Employee ID and Email are
auto-assigned when left blank (see next_employee_id / generate_email), and
each new employee gets a random local password (generate_password) so HR
can hand back a filled-in results workbook (accounts/upload_views.py) that
lets the new joiner sign in and fill in their own profile on day one —
before IT has necessarily provisioned their Entra mailbox. SSO still works
once that email exists in Entra (ModelBackend and the OIDC backend are both
active — see core/settings.py::AUTHENTICATION_BACKENDS), the local password
is just an earlier path to the same account.

Structurally mirrors employees/upload.py and payroll/upload.py: ParsedRow /
RowError / ApplyReport dataclasses, a read-only parse_upload() dry-run, and
apply_upload() writing inside per-row transaction.atomic() savepoints so one
bad row doesn't abort the rest of the batch.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import openpyxl
from dateutil import parser as date_parser
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel
from openpyxl.worksheet.datavalidation import DataValidation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.utils import timezone

from accounts.models import Department, Designation, EmploymentType, Grade, JobLocation, Team, WorkLocation
from accounts.services import check_prior_employment
from core.exports import escape_spreadsheet
from employees.models import IdentificationNumber, JobInfo
from payroll.models import SalaryStructure

User = get_user_model()

# The actual Department.name in Orbis today — not just "Data Solutions".
# Matched case-insensitively as a substring so a minor future rename (e.g.
# dropping "and Due Diligence") doesn't silently stop triggering this.
DATA_SOLUTIONS_DEPARTMENT_NAME = "Data solution"
EMAIL_DOMAIN = "sebpo.com"

# No 0/O or 1/I/l — a new joiner is typing this by hand from a printed/shared
# sheet on their first day.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
PASSWORD_LENGTH = 12


def generate_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    line_no: int
    full_name: str
    first_name: str
    last_name: str
    employee_id: str
    email: str
    nid_number: str
    department: object                  # accounts.Department
    designation: object | None          # accounts.Designation
    grade: object | None                # accounts.Grade
    team: object | None                 # accounts.Team
    job_location: object | None         # accounts.JobLocation
    work_location: object | None        # accounts.WorkLocation
    employment_type: object | None      # accounts.EmploymentType
    contact_number: str
    joining_date: date | None
    probation_month: int | None
    notice_period_months: int | None
    reports_to: object | None           # CustomUser
    resource_status: str
    billing_status: str
    gross_monthly: Decimal | None
    effective_from: date | None
    reason: str
    raw: dict
    created_employee_pk: object | None = None   # set by apply_upload on success
    generated_password: str = ""                # set by apply_upload on success — plaintext, in-memory only


@dataclass
class RowError:
    line_no: int
    col_letter: str | None
    raw: dict
    message: str


@dataclass
class RowWarning:
    """Non-fatal, informational — never counts toward ApplyReport.has_errors
    and never aborts a batch. Distinct from RowError so nothing accidentally
    treats one as a blocking failure."""
    line_no: int
    col_letter: str | None
    raw: dict
    message: str
    employee_name: str = ""
    employee_id: str = ""
    employee_is_active: bool = True
    separation_type_display: str = ""
    last_working_date: date | None = None
    is_involuntary: bool = False


@dataclass
class ApplyReport:
    created: list[ParsedRow] = field(default_factory=list)
    apply_errors: list[RowError] = field(default_factory=list)
    parse_errors: list[RowError] = field(default_factory=list)
    duplicates: list[RowError] = field(default_factory=list)
    warnings: list[RowWarning] = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created)


# ---------------------------------------------------------------------------
# Cell coercion + lookup caches (same idioms as employees/upload.py and
# payroll/upload.py — each upload module keeps its own small copies rather
# than sharing a common utility module, matching existing convention).
# ---------------------------------------------------------------------------

def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _safe_str(value) -> str:
    return "" if value is None else str(value).strip()


def _row_value(row: list, idx: int | None):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _norm(value) -> str:
    return "" if value is None else str(value).strip().lower()


def _norm_header(value) -> str:
    return "".join(ch for ch in _norm(value) if ch.isalnum())


def _coerce_employee_id(value) -> str:
    """xlsx may hand us int/float for numeric employee_id cells. CSV gives str."""
    if value is None:
        raise ValueError("Empty employee_id.")
    if isinstance(value, bool):
        raise ValueError(f"Bad employee_id: {value!r}.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Employee_id '{value}' looks like a decimal — expected an integer or text.")
        return str(int(value))
    s = str(value).strip()
    if not s:
        raise ValueError("Empty employee_id.")
    return s


def _coerce_nid(value) -> str:
    """xlsx may hand us int/float for a numeric-looking NID cell. CSV gives str."""
    if value is None:
        raise ValueError("Empty NID Number.")
    if isinstance(value, bool):
        raise ValueError(f"Bad NID Number: {value!r}.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"NID Number '{value}' looks like a decimal — expected an integer or text.")
        return str(int(value))
    s = str(value).strip()
    if not s:
        raise ValueError("Empty NID Number.")
    return s


def _coerce_date(raw) -> date:
    """Accept native Excel dates, Excel serial dates, and common text dates."""
    if isinstance(raw, datetime):
        parsed = raw.date()
    elif isinstance(raw, date):
        parsed = raw
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            parsed = from_excel(raw).date()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Bad date '{raw}' — use a valid Excel date or text date.")
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("Empty date.")
        parsed = None
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y",
            "%m-%d-%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y",
            "%b %d, %Y", "%B %d, %Y",
        ):
            try:
                parsed = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = date_parser.parse(s, fuzzy=False).date()
            except (ValueError, OverflowError, TypeError):
                raise ValueError(f"Bad date '{s}' — use a valid Excel date or text date.")
    if parsed > date.today() + timedelta(days=365):
        raise ValueError(f"Date {parsed.isoformat()} is more than a year in the future — likely a typo.")
    return parsed


def _coerce_probation_month(raw) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"Bad probation month: {raw!r}.")
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError(f"Probation month '{raw}' looks like a decimal — expected a whole number.")
        val = int(raw)
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("Empty probation month.")
        try:
            val = int(s)
        except ValueError:
            raise ValueError(f"Bad probation month '{raw}' — expected a whole number.")
    if val < 0:
        raise ValueError("Probation month cannot be negative.")
    return val


def _coerce_notice_period_months(raw) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"Bad notice period: {raw!r}.")
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError(f"Notice period '{raw}' looks like a decimal — expected a whole number.")
        val = int(raw)
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("Empty notice period.")
        try:
            val = int(s)
        except ValueError:
            raise ValueError(f"Bad notice period '{raw}' — expected a whole number.")
    if val < 0:
        raise ValueError("Notice period cannot be negative.")
    return val


def _parse_gross_monthly(raw) -> Decimal:
    if raw is None:
        raise ValueError("Empty gross monthly salary.")
    if isinstance(raw, bool):
        raise ValueError(f"Bad gross monthly salary: {raw!r}.")
    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        s = str(raw).strip().replace(",", "")
        if not s:
            raise ValueError("Empty gross monthly salary.")
        try:
            value = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"Bad gross monthly salary '{raw}' — expected a number.")
    if value <= 0:
        raise ValueError("Gross monthly salary must be greater than zero.")
    return value.quantize(Decimal("0.01"))


def _build_named_cache(model) -> dict[str, object]:
    return {obj.name.strip().lower(): obj for obj in model.objects.filter(is_active=True)}


def _resolve_named(raw, cache: dict[str, object], label: str):
    key = str(raw).strip().lower()
    hit = cache.get(key)
    if hit is None:
        raise ValueError(f"Unknown {label} '{str(raw).strip()}'.")
    return hit


def _build_choice_cache(choices) -> dict[str, str]:
    cache: dict[str, str] = {}
    for code, label in choices:
        cache[code.lower()] = code
        cache[label.lower()] = code
    return cache


def _resolve_choice(raw, cache: dict[str, str], label: str, choices) -> str:
    key = str(raw).strip().lower()
    hit = cache.get(key)
    if hit is None:
        accepted = ", ".join(f"{code} ({name})" for code, name in choices)
        raise ValueError(f"Unknown {label} '{str(raw).strip()}'. Accepted values: {accepted}.")
    return hit


def _coerce_email(raw) -> str:
    s = str(raw).strip().lower()
    if not s:
        raise ValueError("Empty email.")
    try:
        validate_email(s)
    except ValidationError:
        raise ValueError(f"'{s}' doesn't look like a valid email address.")
    return s


RESOURCE_STATUS_CHOICES = JobInfo.ResourceStatus.choices
BILLING_STATUS_CHOICES = JobInfo.BillingStatus.choices
REASON_CHOICES = SalaryStructure.Reason.choices


# ---------------------------------------------------------------------------
# Employee ID + email generation
# ---------------------------------------------------------------------------

def next_employee_id_seed() -> int:
    """Highest numeric employee_id currently in use — existing IDs are plain,
    unpadded digit strings (e.g. "992"), same numeric-cast approach as
    employees/exports.py::_employee_id_sort_key. Callers increment from here
    for each blank-Employee-ID row, in file order."""
    ids = User.objects.exclude(employee_id__isnull=True).exclude(employee_id="") \
        .filter(employee_id__regex=r"^[0-9]+$") \
        .annotate(employee_id_num=Cast("employee_id", IntegerField())) \
        .order_by("-employee_id_num") \
        .values_list("employee_id_num", flat=True)[:1]
    return (ids[0] if ids else 0)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def generate_email(first: str, middle: str, last: str, department_name: str, *, taken: set[str]) -> str:
    """Generate a unique first.last@sebpo.com-style address.

    No numeric suffixes — collisions are resolved using more of the
    person's own name, mirroring how enterprises actually keep
    first.last@company.com addresses unique (widen to a middle name, then
    widen the prefix, then drop the separator) before ever falling back to
    a digit. `taken` is the caller's shared, lowercased set of emails
    already in use (existing CustomUser rows + earlier rows in this same
    upload) — mutated in place on success so later rows see this one.
    """
    first_s, last_s, middle_s = _slug(first), _slug(last), _slug(middle)
    is_data_solutions = DATA_SOLUTIONS_DEPARTMENT_NAME.lower() in (department_name or "").strip().lower()

    candidates: list[str] = []

    def add(local: str):
        if local and local not in candidates:
            candidates.append(local)

    if is_data_solutions and middle_s:
        add(f"{first_s[0]}{middle_s[0]}{last_s}")
    elif not is_data_solutions:
        add(f"{first_s}.{last_s}")

    if not is_data_solutions and middle_s:
        add(f"{first_s}.{middle_s}.{last_s}")

    # Widen the prefix up to the full first name. Data Solutions never uses
    # a dot anywhere (its base convention is undotted initials), so only
    # widen undotted there; the default convention tries dotted first, then
    # undotted as a visually distinct fallback.
    seps = ("",) if is_data_solutions else (".", "")
    for sep in seps:
        for k in range(2, len(first_s) + 1):
            f_part = first_s[:k]
            m_part = middle_s[:k] if middle_s else ""
            local = f"{f_part}{sep}{m_part}{sep}{last_s}" if m_part else f"{f_part}{sep}{last_s}"
            add(local)

    for local in candidates:
        email = f"{local}@{EMAIL_DOMAIN}"
        if email not in taken:
            taken.add(email)
            return email

    raise ValueError(
        f"Couldn't generate a unique email for '{first} {last}' — the name space is "
        "exhausted (an unusual name collision). Fill in the Email column by hand for this row."
    )


# ---------------------------------------------------------------------------
# File reading + header discovery
# ---------------------------------------------------------------------------

def _load_rows(file_obj, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return [list(row) for row in ws.iter_rows(values_only=True)]
    if name.endswith(".csv"):
        raw = file_obj.read()
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
        return [list(row) for row in csv.reader(io.StringIO(text))]
    raise ValueError("Unsupported file type — upload .xlsx or .csv.")


_HEADER_ALIASES = {
    "full_name": {"fullname", "name", "employeename"},
    "employee_id": {"employeeid"},
    "email": {"email", "emailaddress"},
    "department": {"department"},
    "designation": {"designation"},
    "grade": {"grade"},
    "team": {"team"},
    "job_location": {"joblocation"},
    "work_location": {"worklocation"},
    "employment_type": {"employmenttype"},
    "contact_number": {"contactnumber", "phone", "mobile", "mobilenumber"},
    "nid_number": {"nidnumber", "nid", "nationalid", "nationalidnumber"},
    "joining_date": {"joiningdate", "dateofjoining", "doj"},
    "probation_month": {"probationmonth", "probationmonths", "probationperiod"},
    "notice_period_months": {"noticeperiodmonths", "noticeperiod", "noticeperiodinmonths"},
    "reports_to": {"reportsto", "reportstoemployeeid", "managerid", "managersemployeeid"},
    "resource_status": {"resourcestatus"},
    "billing_status": {"billingstatus"},
    "gross_monthly": {"grossmonthlysalary", "grossmonthly", "grosssalary"},
    "effective_from": {"effectivefrom"},
    "reason": {"reason"},
}

TEMPLATE_HEADERS = [
    "Full Name", "Employee ID", "Email", "Department", "Designation", "Grade", "Team",
    "Job Location", "Work Location", "Employment Type", "Contact Number", "NID Number",
    "Joining Date", "Probation Month", "Notice Period (Months)", "Reports To (Employee ID)",
    "Resource Status", "Billing Status", "Gross Monthly Salary",
    "Effective From", "Reason",
]

# Every column is required except these — Team stays optional per HR's ask,
# and Employee ID / Email keep their auto-assign-when-blank behavior
# (next_employee_id_seed / generate_email) rather than becoming mandatory.
_REQUIRED_COLUMNS = [f for f in _HEADER_ALIASES if f not in ("team", "employee_id", "email")]


def _discover_columns(header_row: list) -> dict[str, int]:
    columns: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        key = _norm_header(cell)
        for field_name, aliases in _HEADER_ALIASES.items():
            if key in aliases and field_name not in columns:
                columns[field_name] = idx
    missing = [f for f in _REQUIRED_COLUMNS if f not in columns]
    if missing:
        raise ValueError(
            "Missing required column(s): " + ", ".join(m.replace("_", " ").title() for m in missing)
            + f". Expected columns: {', '.join(TEMPLATE_HEADERS)}."
        )
    return columns


# ---------------------------------------------------------------------------
# Parse (read-only)
# ---------------------------------------------------------------------------

def parse_upload(
    file_obj, filename: str, *, today: date,
) -> tuple[list[ParsedRow], list[RowError], list[RowError], list[RowWarning]]:
    """Read the uploaded file and produce ParsedRow / RowError lists. Read-only.

    Employee ID and Email are resolved here (not in apply_upload) so the
    preview step shows the actual values that would be assigned — auto
    assignment consumes the next employee_id / a generated email only for
    rows that pass every other validation, in file order, so a rejected row
    never "spends" a number or address another row could have used.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    body = rows[1:]
    if not body:
        raise ValueError("File has a header but no data rows.")

    columns = _discover_columns(rows[0])

    def has(col: str) -> bool:
        return col in columns

    department_cache = _build_named_cache(Department)
    designation_cache = _build_named_cache(Designation) if has("designation") else {}
    grade_cache = _build_named_cache(Grade) if has("grade") else {}
    team_cache = _build_named_cache(Team) if has("team") else {}
    job_location_cache = _build_named_cache(JobLocation) if has("job_location") else {}
    work_location_cache = _build_named_cache(WorkLocation) if has("work_location") else {}
    employment_type_cache = _build_named_cache(EmploymentType) if has("employment_type") else {}
    resource_status_cache = _build_choice_cache(RESOURCE_STATUS_CHOICES)
    billing_status_cache = _build_choice_cache(BILLING_STATUS_CHOICES)
    reason_cache = _build_choice_cache(REASON_CHOICES)

    reports_to_ids: set[str] = set()
    if has("reports_to"):
        for raw_row in body:
            cell = _row_value(raw_row, columns["reports_to"])
            if not _is_blank(cell):
                try:
                    reports_to_ids.add(_coerce_employee_id(cell))
                except ValueError:
                    continue
    manager_cache = (
        {u.employee_id: u for u in User.objects.filter(is_active=True, employee_id__in=reports_to_ids)}
        if reports_to_ids else {}
    )

    existing_ids = set(
        User.objects.exclude(employee_id__isnull=True).exclude(employee_id="")
        .values_list("employee_id", flat=True)
    )
    existing_emails = set(User.objects.values_list("email", flat=True))
    used_emails = {e.lower() for e in existing_emails}

    next_id_counter = next_employee_id_seed()

    def take_next_id() -> str:
        nonlocal next_id_counter
        next_id_counter += 1
        candidate = str(next_id_counter)
        while candidate in existing_ids:
            next_id_counter += 1
            candidate = str(next_id_counter)
        existing_ids.add(candidate)
        return candidate

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    warnings: list[RowWarning] = []
    parsed: list[ParsedRow] = []
    seen_ids: dict[str, int] = {}
    seen_emails: dict[str, int] = {}

    for line_no, raw_row in enumerate(body, start=2):
        if not raw_row or all(_is_blank(c) for c in raw_row):
            continue

        def cell(name):
            return _row_value(raw_row, columns.get(name))

        raw_snapshot = {
            "Full_Name": _safe_str(cell("full_name")),
            "Employee_ID": _safe_str(cell("employee_id")),
            "Email": _safe_str(cell("email")),
            "Department": _safe_str(cell("department")),
            "Gross_Monthly": _safe_str(cell("gross_monthly")),
            "Effective_From": _safe_str(cell("effective_from")),
        }

        row_failed = False

        def fail(col_name, message):
            nonlocal row_failed
            parse_errors.append(RowError(
                line_no, get_column_letter(columns[col_name] + 1) if col_name in columns else None,
                raw_snapshot, message,
            ))
            row_failed = True

        full_name_cell = cell("full_name")
        if _is_blank(full_name_cell):
            fail("full_name", "Empty Full Name.")
            continue
        tokens = str(full_name_cell).split()
        if len(tokens) < 2:
            fail("full_name", "Full Name must include at least a first and last name.")
            continue
        first_name, last_name = tokens[0], tokens[-1]
        middle_name = " ".join(tokens[1:-1])

        dept_cell = cell("department")
        department = None
        if _is_blank(dept_cell):
            fail("department", "Empty Department.")
        else:
            try:
                department = _resolve_named(dept_cell, department_cache, "department")
            except ValueError as exc:
                fail("department", str(exc))

        designation = None
        designation_cell = cell("designation")
        if _is_blank(designation_cell):
            fail("designation", "Empty Designation.")
        else:
            try:
                designation = _resolve_named(designation_cell, designation_cache, "designation")
            except ValueError as exc:
                fail("designation", str(exc))

        grade = None
        grade_cell = cell("grade")
        if _is_blank(grade_cell):
            fail("grade", "Empty Grade.")
        else:
            try:
                grade = _resolve_named(grade_cell, grade_cache, "grade")
            except ValueError as exc:
                fail("grade", str(exc))

        team = None
        if has("team") and not _is_blank(cell("team")):
            try:
                team = _resolve_named(cell("team"), team_cache, "team")
            except ValueError as exc:
                fail("team", str(exc))

        job_location = None
        job_location_cell = cell("job_location")
        if _is_blank(job_location_cell):
            fail("job_location", "Empty Job Location.")
        else:
            try:
                job_location = _resolve_named(job_location_cell, job_location_cache, "job location")
            except ValueError as exc:
                fail("job_location", str(exc))

        work_location = None
        work_location_cell = cell("work_location")
        if _is_blank(work_location_cell):
            fail("work_location", "Empty Work Location.")
        else:
            try:
                work_location = _resolve_named(work_location_cell, work_location_cache, "work location")
            except ValueError as exc:
                fail("work_location", str(exc))

        employment_type = None
        employment_type_cell = cell("employment_type")
        if _is_blank(employment_type_cell):
            fail("employment_type", "Empty Employment Type.")
        else:
            try:
                employment_type = _resolve_named(employment_type_cell, employment_type_cache, "employment type")
            except ValueError as exc:
                fail("employment_type", str(exc))

        contact_number_cell = cell("contact_number")
        if _is_blank(contact_number_cell):
            fail("contact_number", "Empty Contact Number.")
            contact_number = ""
        else:
            contact_number = _safe_str(contact_number_cell)

        nid_number = ""
        prior_warning = None
        nid_cell = cell("nid_number")
        if _is_blank(nid_cell):
            fail("nid_number", "Empty NID Number.")
        else:
            try:
                nid_number = _coerce_nid(nid_cell)
            except ValueError as exc:
                fail("nid_number", str(exc))
            else:
                prior_warning = check_prior_employment(nid_number)

        joining_date = None
        joining_date_cell = cell("joining_date")
        if _is_blank(joining_date_cell):
            fail("joining_date", "Empty Joining Date.")
        else:
            try:
                joining_date = _coerce_date(joining_date_cell)
            except ValueError as exc:
                fail("joining_date", str(exc))

        probation_month = None
        probation_month_cell = cell("probation_month")
        if _is_blank(probation_month_cell):
            fail("probation_month", "Empty Probation Month.")
        else:
            try:
                probation_month = _coerce_probation_month(probation_month_cell)
            except ValueError as exc:
                fail("probation_month", str(exc))

        notice_period_months = None
        notice_period_cell = cell("notice_period_months")
        if _is_blank(notice_period_cell):
            fail("notice_period_months", "Empty Notice Period (Months).")
        else:
            try:
                notice_period_months = _coerce_notice_period_months(notice_period_cell)
            except ValueError as exc:
                fail("notice_period_months", str(exc))

        reports_to = None
        reports_to_cell = cell("reports_to")
        if _is_blank(reports_to_cell):
            fail("reports_to", "Empty Reports To (Employee ID).")
        else:
            try:
                mgr_id = _coerce_employee_id(reports_to_cell)
            except ValueError as exc:
                fail("reports_to", str(exc))
            else:
                reports_to = manager_cache.get(mgr_id)
                if reports_to is None:
                    fail("reports_to", f"No active employee with employee_id '{mgr_id}' for Reports To.")

        resource_status = ""
        resource_status_cell = cell("resource_status")
        if _is_blank(resource_status_cell):
            fail("resource_status", "Empty Resource Status.")
        else:
            try:
                resource_status = _resolve_choice(
                    resource_status_cell, resource_status_cache, "resource status", RESOURCE_STATUS_CHOICES,
                )
            except ValueError as exc:
                fail("resource_status", str(exc))

        billing_status = ""
        billing_status_cell = cell("billing_status")
        if _is_blank(billing_status_cell):
            fail("billing_status", "Empty Billing Status.")
        else:
            try:
                billing_status = _resolve_choice(
                    billing_status_cell, billing_status_cache, "billing status", BILLING_STATUS_CHOICES,
                )
            except ValueError as exc:
                fail("billing_status", str(exc))

        gross_monthly = None
        gross_monthly_cell = cell("gross_monthly")
        if _is_blank(gross_monthly_cell):
            fail("gross_monthly", "Empty Gross Monthly Salary.")
        else:
            try:
                gross_monthly = _parse_gross_monthly(gross_monthly_cell)
            except ValueError as exc:
                fail("gross_monthly", str(exc))

        effective_from = None
        effective_from_cell = cell("effective_from")
        if _is_blank(effective_from_cell):
            fail("effective_from", "Empty Effective From.")
        else:
            try:
                effective_from = _coerce_date(effective_from_cell)
            except ValueError as exc:
                fail("effective_from", str(exc))

        reason = ""
        reason_cell = cell("reason")
        if _is_blank(reason_cell):
            fail("reason", "Empty Reason.")
        else:
            try:
                reason = _resolve_choice(reason_cell, reason_cache, "reason", REASON_CHOICES)
            except ValueError as exc:
                fail("reason", str(exc))

        employee_id = None
        emp_id_cell = cell("employee_id")
        if not _is_blank(emp_id_cell):
            try:
                candidate_id = _coerce_employee_id(emp_id_cell)
            except ValueError as exc:
                fail("employee_id", str(exc))
            else:
                if candidate_id in existing_ids:
                    if candidate_id in seen_ids:
                        dup_errors.append(RowError(
                            line_no, get_column_letter(columns["employee_id"] + 1), raw_snapshot,
                            f"Duplicate of row {seen_ids[candidate_id]} — Employee ID {candidate_id} appears twice.",
                        ))
                    else:
                        fail("employee_id", f"Employee ID '{candidate_id}' already exists.")
                    row_failed = True
                else:
                    seen_ids[candidate_id] = line_no
                    existing_ids.add(candidate_id)
                    employee_id = candidate_id

        email = None
        email_cell = cell("email")
        if not _is_blank(email_cell):
            try:
                candidate_email = _coerce_email(email_cell)
            except ValueError as exc:
                fail("email", str(exc))
            else:
                if candidate_email in used_emails:
                    if candidate_email in seen_emails:
                        dup_errors.append(RowError(
                            line_no, get_column_letter(columns["email"] + 1), raw_snapshot,
                            f"Duplicate of row {seen_emails[candidate_email]} — {candidate_email} appears twice.",
                        ))
                    else:
                        fail("email", f"'{candidate_email}' already exists.")
                    row_failed = True
                else:
                    seen_emails[candidate_email] = line_no
                    used_emails.add(candidate_email)
                    email = candidate_email

        if row_failed:
            continue

        if employee_id is None:
            employee_id = take_next_id()
        if email is None:
            email = generate_email(
                first_name, middle_name, last_name,
                department.name if department else "", taken=used_emails,
            )

        if prior_warning:
            warnings.append(RowWarning(
                line_no=line_no,
                col_letter=get_column_letter(columns["nid_number"] + 1) if "nid_number" in columns else None,
                raw=raw_snapshot, message=prior_warning.as_message(),
                employee_name=prior_warning.employee_name,
                employee_id=prior_warning.employee_id,
                employee_is_active=prior_warning.employee_is_active,
                separation_type_display=prior_warning.separation_type_display,
                last_working_date=prior_warning.last_working_date,
                is_involuntary=prior_warning.is_involuntary,
            ))

        parsed.append(ParsedRow(
            line_no=line_no, full_name=str(full_name_cell).strip(),
            first_name=first_name, last_name=last_name,
            employee_id=employee_id, email=email, nid_number=nid_number,
            department=department, designation=designation, grade=grade, team=team,
            job_location=job_location, work_location=work_location,
            employment_type=employment_type, contact_number=contact_number,
            joining_date=joining_date, probation_month=probation_month,
            notice_period_months=notice_period_months,
            reports_to=reports_to, resource_status=resource_status,
            billing_status=billing_status, gross_monthly=gross_monthly,
            effective_from=effective_from, reason=reason, raw=raw_snapshot,
        ))

    return parsed, parse_errors, dup_errors, warnings


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_upload(viewer, parsed_rows: list[ParsedRow], *, warnings: list[RowWarning] | None = None) -> ApplyReport:
    """Create a CustomUser (+ optional JobInfo, + optional SalaryStructure)
    for each row, one transaction.atomic() savepoint per row (mirrors
    payroll/upload.py::apply_salary_upload) — a race against another
    concurrent upload (same Employee ID or Email assigned twice) is reported
    per-row in apply_errors rather than aborting the batch; parse_upload
    already validated everything else, so this is the only realistic
    failure mode here.

    `warnings` (rehire/prior-employment flags computed at parse time) is
    carried through unchanged so they're still visible on the post-apply
    results screen, not just in the preview step.
    """
    report = ApplyReport(warnings=list(warnings or []))
    now = timezone.now()

    for row in parsed_rows:
        password = generate_password()
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=row.email, username=row.email[:150],
                    first_name=row.first_name, last_name=row.last_name,
                    employee_id=row.employee_id,
                    department=row.department, designation=row.designation,
                    access_level=row.designation.access_level if row.designation else None,
                    grade=row.grade,
                    team=row.team, job_location=row.job_location,
                    work_location=row.work_location, employment_type=row.employment_type,
                    contact_number=row.contact_number,
                    joining_date=row.joining_date, probation_month=row.probation_month,
                    notice_period_months=row.notice_period_months,
                    is_active=True,
                )
                user.set_password(password)
                user.save(update_fields=["password"])

                if row.nid_number:
                    IdentificationNumber.objects.create(
                        employee=user, id_type=IdentificationNumber.IdType.NID, id_number=row.nid_number,
                        added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
                    )

                if row.reports_to or row.resource_status or row.billing_status:
                    JobInfo.objects.create(
                        employee=user, reports_to=row.reports_to,
                        resource_status=row.resource_status, billing_status=row.billing_status,
                        added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
                    )

                if row.gross_monthly is not None:
                    SalaryStructure(
                        employee=user, effective_from=row.effective_from, effective_to=None,
                        revision_no=1, reason=row.reason or SalaryStructure.Reason.NEW_HIRE,
                        gross_monthly=row.gross_monthly, status=SalaryStructure.Status.APPROVED,
                        processed_by=viewer, processed_at=now,
                        added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
                    ).save()
        except (IntegrityError, ValidationError) as exc:
            messages_ = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            report.apply_errors.append(RowError(row.line_no, None, row.raw, messages_))
            continue

        row.created_employee_pk = user.pk
        row.generated_password = password
        report.created.append(row)

    report.applied = True
    return report


# ---------------------------------------------------------------------------
# Template + results builders
# ---------------------------------------------------------------------------

_DROPDOWN_COLUMNS = [
    ("Department", lambda: Department.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Designation", lambda: Designation.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Grade", lambda: Grade.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Team", lambda: Team.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Job Location", lambda: JobLocation.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Work Location", lambda: WorkLocation.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Employment Type", lambda: EmploymentType.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
    ("Resource Status", lambda: [label for _, label in RESOURCE_STATUS_CHOICES]),
    ("Billing Status", lambda: [label for _, label in BILLING_STATUS_CHOICES]),
    ("Reason", lambda: [label for _, label in REASON_CHOICES]),
]


def build_onboard_xlsx_template() -> bytes:
    """Blank template — headers per TEMPLATE_HEADERS, a data-validation
    dropdown on every reference-table column (sourced from a hidden Lookups
    sheet, same DataValidation pattern as roster/upload.py and
    payroll/upload.py), and a plain reference sheet of existing employees
    (Employee ID + Name) for filling in Reports To by hand.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "New Joiners"

    for col, label in enumerate(TEMPLATE_HEADERS, start=1):
        ws.cell(row=1, column=col, value=label)

    blank_rows = 40
    last_row = 1 + blank_rows
    emp_id_col = TEMPLATE_HEADERS.index("Employee ID") + 1
    contact_col = TEMPLATE_HEADERS.index("Contact Number") + 1
    nid_col = TEMPLATE_HEADERS.index("NID Number") + 1
    joining_col = TEMPLATE_HEADERS.index("Joining Date") + 1
    effective_col = TEMPLATE_HEADERS.index("Effective From") + 1
    for row in range(2, last_row + 1):
        # "@" (Text) on Employee ID / Contact Number / NID Number so Excel
        # keeps a leading zero (e.g. "01770510901") instead of silently
        # parsing the cell as a number and dropping it — must be set before
        # the cell is typed into, not after.
        ws.cell(row=row, column=emp_id_col).number_format = "@"
        ws.cell(row=row, column=contact_col).number_format = "@"
        ws.cell(row=row, column=nid_col).number_format = "@"
        ws.cell(row=row, column=joining_col).number_format = "yyyy-mm-dd"
        ws.cell(row=row, column=effective_col).number_format = "yyyy-mm-dd"

    lookups = wb.create_sheet("Lookups")
    lookup_col = 1
    for header, fetch in _DROPDOWN_COLUMNS:
        values = list(fetch())
        lookups.cell(row=1, column=lookup_col, value=header)
        for i, value in enumerate(values, start=2):
            lookups.cell(row=i, column=lookup_col, value=value)
        if values:
            col_letter = get_column_letter(lookup_col)
            list_range = f"=Lookups!${col_letter}$2:${col_letter}${1 + len(values)}"
            dv = DataValidation(type="list", formula1=list_range, allow_blank=True)
            dv.error = "Pick a value from the dropdown."
            dv.errorTitle = "Invalid value"
            ws.add_data_validation(dv)
            target_col = TEMPLATE_HEADERS.index(header) + 1
            target_letter = get_column_letter(target_col)
            dv.add(f"{target_letter}2:{target_letter}{last_row}")
        lookup_col += 1
    lookups.sheet_state = "hidden"

    employees_sheet = wb.create_sheet("Existing employees (reference)")
    employees_sheet.cell(row=1, column=1, value="Employee ID")
    employees_sheet.cell(row=1, column=2, value="Name")
    qs = (
        User.objects.filter(is_active=True, employee_id__isnull=False).exclude(employee_id="")
        .annotate(employee_id_num=Cast("employee_id", IntegerField())).order_by("employee_id_num")
    )
    for i, emp in enumerate(qs, start=2):
        eid_cell = employees_sheet.cell(row=i, column=1, value=emp.employee_id)
        eid_cell.number_format = "@"
        employees_sheet.cell(row=i, column=2, value=escape_spreadsheet(emp.get_full_name()))

    ws.freeze_panes = "A2"
    for col in range(1, len(TEMPLATE_HEADERS) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


RESULTS_HEADERS = TEMPLATE_HEADERS[:3] + ["Password"] + TEMPLATE_HEADERS[3:]


def build_onboard_results_xlsx(employees, passwords: dict) -> bytes:
    """Results workbook handed back to HR/IT — same columns as the input
    template plus a Password column (right after Email), Employee ID and
    Email now filled in for every row. This is what IT provisions the Entra
    mailbox against, and — via the local password, generated fresh per
    employee and never persisted anywhere except as a hash on CustomUser —
    what lets a new joiner sign in and fill in their own profile on day one,
    before Entra provisioning necessarily catches up.

    `passwords` maps employee pk -> plaintext password. The plaintext only
    ever exists in memory for the single apply request that generated it
    (accounts.upload.apply_upload) and this immediate results download —
    it is never written to the database or logged.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "New Joiners — Created"

    for col, label in enumerate(RESULTS_HEADERS, start=1):
        ws.cell(row=1, column=col, value=label)

    password_col = RESULTS_HEADERS.index("Password") + 1
    emp_id_col = RESULTS_HEADERS.index("Employee ID") + 1
    contact_col = RESULTS_HEADERS.index("Contact Number") + 1
    nid_col = RESULTS_HEADERS.index("NID Number") + 1
    joining_col = RESULTS_HEADERS.index("Joining Date") + 1
    effective_col = RESULTS_HEADERS.index("Effective From") + 1

    nid_by_employee = dict(
        IdentificationNumber.objects.filter(
            employee__in=employees, id_type=IdentificationNumber.IdType.NID,
        ).values_list("employee_id", "id_number")
    )

    row_idx = 2
    for emp in employees:
        job_info = getattr(emp, "job_info", None)
        current_salary = next(
            (s for s in emp.salary_structures.all()
             if s.status == SalaryStructure.Status.APPROVED and s.effective_to is None),
            None,
        ) if hasattr(emp, "salary_structures") else None

        values = [
            escape_spreadsheet(emp.get_full_name()),
            emp.employee_id,
            emp.email,
            passwords.get(str(emp.pk), ""),
            emp.department.name if emp.department else "",
            emp.designation.name if emp.designation else "",
            emp.grade.name if emp.grade else "",
            emp.team.name if emp.team else "",
            emp.job_location.name if emp.job_location else "",
            emp.work_location.name if emp.work_location else "",
            emp.employment_type.name if emp.employment_type else "",
            emp.contact_number,
            nid_by_employee.get(emp.pk, ""),
            emp.joining_date,
            emp.probation_month,
            emp.notice_period_months,
            job_info.reports_to.employee_id if job_info and job_info.reports_to else "",
            job_info.get_resource_status_display() if job_info and job_info.resource_status else "",
            job_info.get_billing_status_display() if job_info and job_info.billing_status else "",
            current_salary.gross_monthly if current_salary else "",
            current_salary.effective_from if current_salary else "",
            current_salary.get_reason_display() if current_salary else "",
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col, value=value)
            if col in (joining_col, effective_col):
                cell.number_format = "yyyy-mm-dd"
            if col in (emp_id_col, contact_col, nid_col, password_col):
                cell.number_format = "@"
        row_idx += 1

    ws.freeze_panes = "A2"
    for col in range(1, len(RESULTS_HEADERS) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()
