"""Employees bulk update — flat parser, applier, and template builder.

One row per employee (unlike roster's per-employee-per-date grid):

    Row 1: Employee ID | Employee Name | Team | Reports To | Resource Status | Billing Status
           | Job Location | Work Location
    Row 2: <emp_id>    | <name>        | ...

Profile-section columns (added 2026-10-05 for the legacy HR import,
docs/LEGACY_60_IMPORT_PLAN.md §2): Father's/Mother's Name, Date of Birth,
Blood Group, Height (inches), Identification Mark, Spouse Name, NID,
"Permanent …"/"Present …" address fields (Flat No, Holding No, Village/Area,
Post Office, Union, Post Code, District, Police Station), Highest Education
and Professional Certification. These write the employee's PersonalInfo /
FamilyInfo / Address / IdentificationNumber / Education /
ProfessionalCertification rows under the same managed-scope rule the profile
drawers use. Education and Certification are repeating sections, so for them
"blank = unchanged" becomes "create once": a row at the same degree level /
with the same certificate name is never duplicated by a re-upload.

A blank cell in any optional column means "leave this field unchanged" — it
never clears an existing value. The parser is read-only; the applier writes
inside one transaction.atomic() and only if parse_errors is empty
(all-or-nothing), same convention as roster/payroll uploads.
"""

from __future__ import annotations

import csv
import difflib
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import openpyxl
from dateutil import parser as date_parser
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel
from openpyxl.worksheet.datavalidation import DataValidation

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404, HttpResponse
from django.utils import timezone

from accounts.models import Department, JobLocation, Team, WorkLocation, directory_stamp
from accounts.permissions import can_edit_all, managed_scope_pks
from core.exports import escape_spreadsheet
from employees.models import (
    Address, City, Country, Education, FamilyInfo, IdentificationNumber, JobInfo,
    PersonalInfo, ProfessionalCertification, State, UnionWard,
)
from offboarding.models import Resignation
from roster.services import purge_shifts_after_last_working_date

User = get_user_model()


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    line_no: int
    employee: object            # CustomUser
    employee_id: str
    changes: dict                # subset of team/reports_to/resource_status/billing_status
    raw: dict


@dataclass
class RowError:
    line_no: int
    col_letter: str | None
    raw: dict
    message: str


@dataclass
class RowWarning:
    """Non-fatal, informational — never counts toward ApplyReport.has_errors
    and never aborts the batch. Used for the Last Working Date column when
    it conflicts with an existing PENDING/ACCEPTED Resignation on file."""
    line_no: int
    col_letter: str | None
    raw: dict
    message: str
    employee_name: str = ""
    employee_id: str = ""


@dataclass
class ApplyReport:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    skipped_scope: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    applied: bool = False

    @property
    def has_errors(self) -> bool:
        return bool(self.parse_errors or self.duplicates)

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.updated)


# ---------------------------------------------------------------------------
# Cell coercion + lookup caches
# ---------------------------------------------------------------------------

def _coerce_identifier(value, label: str) -> str:
    """xlsx may hand us int/float for numeric identifier cells. CSV gives str.
    Normalise to a clean string suitable for a unique-identifier lookup.
    """
    if value is None:
        raise ValueError(f"Empty {label}.")
    if isinstance(value, bool):
        raise ValueError(f"Bad {label}: {value!r}.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(
                f"{label.capitalize()} '{value}' looks like a decimal — expected an integer or text."
            )
        return str(int(value))
    s = str(value).strip()
    if not s:
        raise ValueError(f"Empty {label}.")
    return s


def _coerce_employee_id(value) -> str:
    return _coerce_identifier(value, "employee_id")


def _coerce_register_serial_no(value) -> str:
    return _coerce_identifier(value, "register serial no")


def _coerce_date(raw) -> date:
    """Accept native Excel dates, Excel serial dates, and common text dates.

    No lower bound (joining dates can be decades old); reject anything more
    than a year out from today as a likely typo.
    """
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
        raise ValueError(
            f"Date {parsed.isoformat()} is more than a year in the future — likely a typo."
        )
    return parsed


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _safe_str(value) -> str:
    if value is None:
        return ""
    return str(value)


def _row_value(row: list, idx: int | None):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _norm(value) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _norm_header(value) -> str:
    return "".join(ch for ch in _norm(value) if ch.isalnum())


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


RESOURCE_STATUS_CHOICES = JobInfo.ResourceStatus.choices
BILLING_STATUS_CHOICES = JobInfo.BillingStatus.choices
GENDER_CHOICES = PersonalInfo.Gender.choices
BLOOD_GROUP_CHOICES = PersonalInfo.BloodGroup.choices
DEGREE_LEVEL_CHOICES = Education.DegreeLevel.choices


# ---------------------------------------------------------------------------
# Profile-column coercion (2026-10-05)
# ---------------------------------------------------------------------------

def _coerce_text(raw, label: str, max_length: int) -> str:
    """xlsx may hand us int/float for numeric-looking text (post codes, NIDs).
    Normalise to a trimmed string and enforce the model's max_length."""
    if isinstance(raw, bool):
        raise ValueError(f"Bad {label}: {raw!r}.")
    if isinstance(raw, float):
        if raw.is_integer():
            raw = int(raw)
        else:
            raise ValueError(f"{label.capitalize()} '{raw}' looks like a decimal — expected text.")
    s = " ".join(str(raw).split())
    if not s:
        raise ValueError(f"Empty {label}.")
    if len(s) > max_length:
        raise ValueError(f"{label.capitalize()} is longer than {max_length} characters.")
    return s


def _coerce_nid(raw) -> str:
    """A NID is a 10/13/17-digit number. Excel silently turns a 17-digit cell
    into a float and loses the last digits — catch that rather than store a
    mangled number."""
    if isinstance(raw, float) and (not raw.is_integer() or abs(raw) >= 2 ** 53):
        # Beyond 2**53 a float can't hold every integer, so a 17-digit NID
        # that Excel stored as a number has already lost digits.
        raise ValueError(f"NID '{raw}' arrived as a number too large to be exact — re-enter it as text in the sheet.")
    s = _coerce_text(raw, "NID number", 100).replace(" ", "")
    if "e+" in s.lower():
        raise ValueError(f"NID '{s}' is in scientific notation — re-enter it as text in the sheet.")
    if not s.isdigit():
        raise ValueError(f"NID '{s}' must be digits only.")
    if len(s) not in (10, 13, 17):
        raise ValueError(f"NID '{s}' has {len(s)} digits — expected 10, 13 or 17.")
    return s


def _coerce_post_code(raw) -> str:
    s = _coerce_text(raw, "post code", 5).replace(" ", "")
    if not s.isdigit():
        raise ValueError(f"Post code '{s}' must be digits only.")
    return s


def _coerce_blood_group(raw) -> str:
    """Accept 'O+', 'o +', 'O+ (O positive)', and the Unicode minus HR's
    sheets tend to carry ('O−')."""
    s = str(raw).strip()
    if "(" in s:
        s = s.split("(", 1)[0]
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace(" ", "").upper()
    if s.endswith("POSITIVE"):
        s = s[: -len("POSITIVE")] + "+"
    if s.endswith("NEGATIVE"):
        s = s[: -len("NEGATIVE")] + "-"
    valid = {code for code, _ in BLOOD_GROUP_CHOICES}
    if s not in valid:
        raise ValueError(f"Unknown blood group '{str(raw).strip()}'. Accepted values: {', '.join(sorted(valid))}.")
    return s


def _coerce_height_inches(raw) -> int:
    """Whole inches. The prep step converts feet+inches (5'6\" -> 66)."""
    if isinstance(raw, bool):
        raise ValueError(f"Bad height: {raw!r}.")
    if isinstance(raw, (int, float)):
        value = raw
    else:
        s = str(raw).strip().lower().replace("inches", "").replace("inch", "").replace("in", "").strip()
        if not s:
            raise ValueError("Empty height.")
        try:
            value = float(s)
        except ValueError:
            raise ValueError(f"Bad height '{raw}' — expected whole inches, e.g. 66.")
    if float(value) != int(value):
        raise ValueError(f"Height '{raw}' must be whole inches.")
    value = int(value)
    if not 24 <= value <= 96:
        raise ValueError(f"Height {value} inches is outside the plausible 24–96 range.")
    return value


def _coerce_date_of_birth(raw) -> date:
    parsed = _coerce_date(raw)
    today = date.today()
    if parsed >= today:
        raise ValueError(f"Date of birth {parsed.isoformat()} is not in the past.")
    if parsed.year < 1900:
        raise ValueError(f"Date of birth {parsed.isoformat()} is before 1900 — likely a typo.")
    return parsed


def _resolve_education_level(raw) -> tuple[str, str]:
    """-> (DegreeLevel code, the label exactly as HR wrote it)."""
    label = " ".join(str(raw).split())
    key = _norm_header(label)
    code = _EDUCATION_LEVEL_ALIASES.get(key)
    if code is None:
        for c, display in DEGREE_LEVEL_CHOICES:
            if key in (c.lower(), _norm_header(display)):
                code = c
                break
    if code is None:
        accepted = ", ".join(display for _, display in DEGREE_LEVEL_CHOICES)
        raise ValueError(
            f"Unknown education level '{label}'. Accepted: {accepted}, or the HR labels "
            "Graduate / Post Graduate / Higher Secondary / Secondary School / "
            "Below Secondary School Certificate."
        )
    return code, label


def _norm_district(raw) -> str:
    key = _norm_header(raw)
    for suffix in ("district", "citycorporation", "citycorperation"):
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[: -len(suffix)]
    return _DISTRICT_ALIASES.get(key, key)


def _build_state_cache() -> dict[str, State]:
    """Header-normalised name -> State (districts win over same-named city
    corporations, which carry a longer name anyway)."""
    cache: dict[str, State] = {}
    for st in State.objects.filter(is_active=True).order_by("kind", "name"):
        cache.setdefault(_norm_header(st.name), st)
    return cache


def _resolve_district(raw, state_cache: dict[str, State]) -> State:
    key = _norm_district(raw)
    hit = state_cache.get(key)
    if hit is None:
        close = difflib.get_close_matches(key, list(state_cache), n=1, cutoff=0.85)
        if close:
            hit = state_cache[close[0]]
    if hit is None:
        raise ValueError(f"Unknown district '{str(raw).strip()}'.")
    return hit


_PS_SUFFIXES = ("policestation", "upazila", "thana", "sadar", "paurashava", "ps")


def _strip_ps_suffix(key: str) -> str:
    changed = True
    while changed:
        changed = False
        for suffix in _PS_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix):
                key = key[: -len(suffix)]
                changed = True
    return key


def _ward_key(name) -> str:
    """"Matuail (part)" / "Ward No-06" / "Ward 6" / "06" all reduce to a
    comparable key: bracket dropped, "ward no" prefix dropped, and a purely
    numeric ward number compared without leading zeros."""
    key = _norm_header(str(name).split("(")[0])
    for prefix in ("wardno", "ward"):
        if key.startswith(prefix) and len(key) > len(prefix):
            key = key[len(prefix):]
    if key.isdigit():
        key = key.lstrip("0") or "0"
    return key


def _resolve_union_ward(raw, city: City, union_cache: dict[int, dict[str, UnionWard]]) -> UnionWard | None:
    """Union/ward lookup within the resolved upazila/thana. The text is kept
    in `union_name` either way; this only adds the FK when it matches."""
    per_city = union_cache.get(city.pk)
    if per_city is None:
        per_city = {}
        for u in UnionWard.objects.filter(city=city, is_active=True).order_by("kind", "name"):
            per_city.setdefault(_norm_header(u.name), u)
            per_city.setdefault(_ward_key(u.name), u)
        union_cache[city.pk] = per_city
    key = _ward_key(raw)
    hit = per_city.get(key) or per_city.get(_norm_header(raw))
    if hit is None:
        close = difflib.get_close_matches(key, list(per_city), n=1, cutoff=0.85)
        if close:
            hit = per_city[close[0]]
    return hit


def _resolve_police_station(raw, state: State, city_cache: dict[int, dict[str, City]]) -> City | None:
    """Upazila/thana lookup within the district; informal spellings get a
    close-match pass. None when nothing matches — the caller warns.

    A metro thana (Dhanmondi, Banani, Uttara, …) lives under the "<District>
    North/South City Corporation" State rows, not the district itself, so
    the search also covers every State whose name starts with the district's
    name. Both the sheet value and the City names are compared with
    "Sadar" / "Thana" / "Police Station" suffixes stripped, so "Bogura Sadar
    Thana" meets "Bogura Sadar"."""
    per_state = city_cache.get(state.pk)
    if per_state is None:
        sibling_states = State.objects.filter(
            is_active=True, country=state.country, name__istartswith=state.name,
        )
        per_state = {}
        cities = City.objects.filter(state__in=sibling_states, is_active=True).order_by("state_id", "kind", "name")
        # Exact names first so a stripped alias never shadows a real one.
        for c in cities:
            per_state.setdefault(_norm_header(c.name), c)
        for c in cities:
            per_state.setdefault(_strip_ps_suffix(_norm_header(c.name)), c)
        city_cache[state.pk] = per_state
    key = _norm_header(raw)
    hit = per_state.get(key) or per_state.get(_strip_ps_suffix(key))
    if hit is None:
        close = difflib.get_close_matches(_strip_ps_suffix(key), list(per_state), n=1, cutoff=0.8)
        if close:
            hit = per_state[close[0]]
    return hit


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _read_xlsx_rows(file_obj) -> list[list]:
    wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _read_csv_rows(file_obj) -> list[list]:
    raw = file_obj.read()
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw
    reader = csv.reader(io.StringIO(text))
    return [list(row) for row in reader]


def _load_rows(file_obj, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return _read_xlsx_rows(file_obj)
    if name.endswith(".csv"):
        return _read_csv_rows(file_obj)
    raise ValueError(
        f"Unsupported file type: {filename!r}. Upload an .xlsx or .csv file."
    )


# ---------------------------------------------------------------------------
# Header discovery
# ---------------------------------------------------------------------------

_HEADER_ALIASES = {
    "employee_id": {"employeeid"},
    "employee_name": {"employeename", "name"},
    "team": {"team"},
    "reports_to": {"reportsto", "reportstoemployeeid", "managerid", "managersemployeeid"},
    "resource_status": {"resourcestatus"},
    "billing_status": {"billingstatus"},
    "department": {"department"},
    "joining_date": {"joiningdate", "dateofjoining", "doj"},
    # Night allowance starts at confirmation (2026-11-03), so HR fills these in bulk.
    "probation_month": {
        "probationmonths", "probationmonth", "probationperiod", "probationperiodmonths", "probation",
    },
    "confirmation_date": {"confirmationdate", "dateofconfirmation", "confirmeddate"},
    "gender": {"gender"},
    "register_serial_no": {"employeeregisterserialno", "registerserialno"},
    "job_location": {"joblocation"},
    "work_location": {"worklocation", "workstation"},
    "last_working_date": {"lastworkingdate", "lwd"},
    # --- profile sections (2026-10-05) ---
    "father_name": {"fathersname", "fathername"},
    "mother_name": {"mothersname", "mothername"},
    "date_of_birth": {"dateofbirth", "dob", "birthday", "birthdate"},
    "blood_group": {"bloodgroup"},
    "height_in_inch": {"heightinches", "heightininch", "heightinch", "heightin", "heightininches"},
    "identification_mark": {"identificationmark", "identificationmarkifany"},
    "spouse_name": {"spousename", "spouse"},
    "nid": {"nid", "nidno", "nidnumber", "nationalid", "nationalidno", "nationalidnumber"},
    "education_level": {
        "highesteducation", "educationlevel", "lasteducationalqualification",
        "highestqualification", "educationalqualification",
    },
    "certification": {"professionalcertification", "certification", "certificatename"},
}

# Address columns come in two flavours, "Permanent …" and "Present …", one
# set per Address.AddressType. Generated so both stay identical.
_ADDRESS_PREFIXES = {"permanent": "PERMANENT", "present": "PRESENT"}
_ADDRESS_TEXT_FIELDS = {
    # column suffix: (Address field, max_length)
    "flat_no": ("flat_no", 100),
    "holding_no": ("holding_no", 100),
    "village_area": ("village_area", 200),
    "post_office": ("post_office", 200),
    "union_name": ("union_name", 200),
    "post_code": ("post_code", 5),
}
for _prefix in _ADDRESS_PREFIXES:
    _HEADER_ALIASES[f"{_prefix}_flat_no"] = {f"{_prefix}flatno"}
    _HEADER_ALIASES[f"{_prefix}_holding_no"] = {
        f"{_prefix}holdingno", f"{_prefix}houseno", f"{_prefix}holdinghouseno",
    }
    _HEADER_ALIASES[f"{_prefix}_village_area"] = {
        f"{_prefix}villagearea", f"{_prefix}villageorarea", f"{_prefix}addressline1", f"{_prefix}area",
    }
    _HEADER_ALIASES[f"{_prefix}_post_office"] = {f"{_prefix}postoffice", f"{_prefix}po"}
    _HEADER_ALIASES[f"{_prefix}_union_name"] = {
        f"{_prefix}union", f"{_prefix}unionname", f"{_prefix}unionward", f"{_prefix}unionwardno",
    }
    _HEADER_ALIASES[f"{_prefix}_post_code"] = {f"{_prefix}postcode", f"{_prefix}postalcode"}
    _HEADER_ALIASES[f"{_prefix}_district"] = {f"{_prefix}district", f"{_prefix}districtcitycorporation"}
    _HEADER_ALIASES[f"{_prefix}_police_station"] = {
        f"{_prefix}policestation", f"{_prefix}ps", f"{_prefix}thana", f"{_prefix}upazila",
        f"{_prefix}upazilathana",
    }

# Plain text profile columns: column key -> (change key, label, max_length).
_PROFILE_TEXT_COLUMNS = {
    "father_name": ("father_name", "father's name", 200),
    "mother_name": ("mother_name", "mother's name", 200),
    "identification_mark": ("identification_mark", "identification mark", 200),
    "spouse_name": ("spouse_name", "spouse name", 200),
    "nid": ("nid", "NID number", 100),
    "certification": ("certification", "professional certification", 255),
}

# PersonalInfo columns the applier writes (gender was already here).
_PERSONAL_INFO_KEYS = (
    "gender", "father_name", "mother_name", "identification_mark",
    "date_of_birth", "blood_group", "height_in_inch",
)

# HR's legacy level labels -> Education.DegreeLevel. "Below Secondary School
# Certificate" -> JSC confirmed by the user 2026-10-05.
_EDUCATION_LEVEL_ALIASES = {
    "psc": "PSC", "primary": "PSC",
    "jsc": "JSC", "belowsecondaryschoolcertificate": "JSC", "belowssc": "JSC",
    "ssc": "SSC", "secondaryschool": "SSC", "secondary": "SSC", "secondaryschoolcertificate": "SSC",
    "hsc": "HSC", "highersecondary": "HSC", "highersecondarycertificate": "HSC",
    "bachelor": "BACHELOR", "bachelors": "BACHELOR", "bachelorsdegree": "BACHELOR",
    "graduate": "BACHELOR", "graduation": "BACHELOR", "honours": "BACHELOR", "honors": "BACHELOR",
    "master": "MASTER", "masters": "MASTER", "mastersdegree": "MASTER",
    "postgraduate": "MASTER", "postgraduation": "MASTER",
    "doctoral": "DOCTORAL", "doctoraldegree": "DOCTORAL", "phd": "DOCTORAL", "doctorate": "DOCTORAL",
}

# Sheet spellings of districts -> the State table's (post-2018 official)
# names. Keys and values are header-normalised (lowercase alphanumerics).
_DISTRICT_ALIASES = {
    "chittagong": "chattogram", "comilla": "cumilla", "bogra": "bogura",
    "barisal": "barishal", "netrokona": "netrakona", "brahmmanbaria": "brahmanbaria",
    "jessore": "jashore", "moulvibazar": "maulvibazar", "coxsbazar": "coxsbazar",
    "nawabganj": "chapainawabganj", "chapainawabgonj": "chapainawabganj",
}

# HR-managed organizational facts — see SELF_EDITABLE_SECTIONS in
# accounts/permissions.py. Only org-wide write scope (can_edit_all) may
# include these columns in an upload; a manager's own downline is not enough.
_HR_ONLY_FIELDS = {
    "department", "joining_date", "gender", "register_serial_no", "last_working_date",
    # Decide when the night allowance starts (payroll/inputs.py::confirmed_from).
    "probation_month", "confirmation_date",
}

# A probation longer than this is almost certainly a typo (days, not months).
MAX_PROBATION_MONTHS = 24


def _coerce_probation_months(raw) -> int:
    """Whole months, 0..MAX_PROBATION_MONTHS — same rule as the bulk
    new-joiner upload (accounts/upload.py::_coerce_probation_month) plus
    an upper bound."""
    if isinstance(raw, bool):
        raise ValueError(f"Bad probation months: {raw!r}.")
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError(f"Probation months '{raw}' looks like a decimal — expected a whole number.")
        val = int(raw)
    else:
        text = str(raw).strip()
        try:
            val = int(text)
        except ValueError:
            raise ValueError(f"Bad probation months '{raw}' — expected a whole number.")
    if val < 0:
        raise ValueError("Probation months cannot be negative.")
    if val > MAX_PROBATION_MONTHS:
        raise ValueError(f"Probation months {val} is over {MAX_PROBATION_MONTHS} — likely a typo.")
    return val


def _discover_columns(header_row: list) -> dict[str, int]:
    columns: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        key = _norm_header(cell)
        for field_name, aliases in _HEADER_ALIASES.items():
            if key in aliases and field_name not in columns:
                columns[field_name] = idx
    if "employee_id" not in columns:
        raise ValueError(
            "Missing required column: Employee ID. Expected columns: "
            "Employee ID, Employee Name, Team, Reports To, Resource Status, Billing Status."
        )
    return columns


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_upload(
    file_obj,
    filename: str,
    viewer=None,
    request=None,
) -> tuple[list[ParsedRow], list[RowError], list[RowError], list[RowWarning]]:
    """Read the uploaded file and produce ParsedRow / RowError lists.

    Returns (parsed_rows, parse_errors, duplicate_errors, warnings). Read-only
    — no DB writes. Lookups (employee, team, department) are built once
    up-front.

    `viewer` gates the HR-only columns (Department, Joining Date, Gender,
    Employee Register Serial No., Last Working Date) — pass the request user
    so a manager without org-wide write scope can't smuggle those in.
    Omitted (e.g. in tests exercising other columns) means unrestricted.
    """
    rows = _load_rows(file_obj, filename)
    if not rows:
        raise ValueError("File is empty.")
    if len(rows) < 2:
        raise ValueError("File has only a header row — nothing to import.")

    columns = _discover_columns(rows[0])
    has_team_col = "team" in columns
    has_department_col = "department" in columns
    has_gender_col = "gender" in columns
    has_register_serial_col = "register_serial_no" in columns
    has_job_location_col = "job_location" in columns
    has_work_location_col = "work_location" in columns
    has_last_working_date_col = "last_working_date" in columns

    if viewer is not None and not can_edit_all(viewer, request=request):
        hr_only_present = _HR_ONLY_FIELDS & columns.keys()
        if hr_only_present:
            labels = ", ".join(sorted(f.replace("_", " ").title() for f in hr_only_present))
            raise ValueError(
                f"{labels} — HR-managed field(s), you don't have permission to bulk-update. "
                "Remove that column and re-upload, or ask HR/Admin to run it."
            )

    parse_errors: list[RowError] = []
    dup_errors: list[RowError] = []
    warnings: list[RowWarning] = []
    parsed: list[ParsedRow] = []
    seen: dict[str, int] = {}

    # Pre-scan employee_id + reports_to cells for a single batched employee query.
    candidate_ids: set[str] = set()
    for raw_row in rows[1:]:
        if not raw_row or all(_is_blank(c) for c in raw_row):
            continue
        for col_key in ("employee_id", "reports_to"):
            idx = columns.get(col_key)
            if idx is None:
                continue
            try:
                candidate_ids.add(_coerce_employee_id(_row_value(raw_row, idx)))
            except ValueError:
                pass

    emp_cache = (
        {u.employee_id: u for u in User.objects.filter(is_active=True, employee_id__in=candidate_ids)}
        if candidate_ids else {}
    )
    team_cache = _build_named_cache(Team) if has_team_col else {}
    department_cache = _build_named_cache(Department) if has_department_col else {}
    job_location_cache = _build_named_cache(JobLocation) if has_job_location_col else {}
    work_location_cache = _build_named_cache(WorkLocation) if has_work_location_col else {}
    resource_status_cache = _build_choice_cache(RESOURCE_STATUS_CHOICES)
    billing_status_cache = _build_choice_cache(BILLING_STATUS_CHOICES)
    gender_cache = _build_choice_cache(GENDER_CHOICES) if has_gender_col else {}

    # Profile-section columns (2026-10-05). Address lookups are built lazily
    # per district; the Country row is required up-front so a missing
    # reference row fails the whole file, not 60 individual rows.
    address_columns: dict[str, dict[str, int]] = {}
    for prefix in _ADDRESS_PREFIXES:
        present_cols = {
            suffix: columns[f"{prefix}_{suffix}"]
            for suffix in (*_ADDRESS_TEXT_FIELDS, "district", "police_station")
            if f"{prefix}_{suffix}" in columns
        }
        if present_cols:
            address_columns[prefix] = present_cols
    state_cache: dict[str, State] = _build_state_cache() if address_columns else {}
    city_cache: dict[int, dict[str, City]] = {}
    union_cache: dict[int, dict[str, UnionWard]] = {}
    bangladesh = None
    if address_columns:
        bangladesh = Country.objects.filter(name__iexact="Bangladesh").first()
        if bangladesh is None:
            raise ValueError(
                "Country 'Bangladesh' is not configured — address columns can't be imported."
            )

    # Existing PENDING/ACCEPTED Resignation per employee, for the LWD
    # mismatch warning below. Not a gate — Last Working Date is still a raw
    # CustomUser write with no Resignation required (same as the HR edit
    # form and Django admin today).
    resignation_by_emp_pk: dict = {}
    if has_last_working_date_col and emp_cache:
        resignation_by_emp_pk = {
            r.employee_id: r
            for r in Resignation.objects.filter(
                employee_id__in=[u.pk for u in emp_cache.values()],
                status__in=(Resignation.Status.PENDING, Resignation.Status.ACCEPTED),
            )
        }

    register_serial_cache: dict[str, object] = {}
    if has_register_serial_col:
        candidate_serials: set[str] = set()
        for raw_row in rows[1:]:
            if not raw_row or all(_is_blank(c) for c in raw_row):
                continue
            cell = _row_value(raw_row, columns["register_serial_no"])
            if _is_blank(cell):
                continue
            try:
                candidate_serials.add(_coerce_register_serial_no(cell))
            except ValueError:
                pass
        if candidate_serials:
            register_serial_cache = {
                u.register_serial_no: u
                for u in User.objects.filter(register_serial_no__in=candidate_serials)
            }

    seen_serials: dict[str, int] = {}

    for line_no, raw_row in enumerate(rows[1:], start=2):
        if not raw_row or all(_is_blank(c) for c in raw_row):
            continue

        emp_id_cell = _row_value(raw_row, columns["employee_id"])
        name_cell = _row_value(raw_row, columns.get("employee_name"))
        raw_snapshot = {
            "Employee_ID": _safe_str(emp_id_cell),
            "Employee_Name": _safe_str(name_cell),
        }

        try:
            emp_id_str = _coerce_employee_id(emp_id_cell)
        except ValueError as exc:
            parse_errors.append(RowError(
                line_no=line_no, col_letter=get_column_letter(columns["employee_id"] + 1),
                raw=raw_snapshot, message=str(exc),
            ))
            continue

        employee = emp_cache.get(emp_id_str)
        if employee is None:
            parse_errors.append(RowError(
                line_no=line_no, col_letter=get_column_letter(columns["employee_id"] + 1),
                raw=raw_snapshot,
                message=f"No active employee with employee_id '{emp_id_str}'.",
            ))
            continue

        if emp_id_str in seen:
            dup_errors.append(RowError(
                line_no=line_no, col_letter=get_column_letter(columns["employee_id"] + 1),
                raw=raw_snapshot,
                message=f"Duplicate of row {seen[emp_id_str]} — employee_id {emp_id_str} appears twice.",
            ))
            continue
        seen[emp_id_str] = line_no

        changes: dict = {}
        row_failed = False

        if has_team_col:
            team_cell = _row_value(raw_row, columns["team"])
            if not _is_blank(team_cell):
                try:
                    changes["team"] = _resolve_named(team_cell, team_cache, "team")
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["team"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if has_job_location_col:
            job_location_cell = _row_value(raw_row, columns["job_location"])
            if not _is_blank(job_location_cell):
                try:
                    changes["job_location"] = _resolve_named(
                        job_location_cell, job_location_cache, "job location",
                    )
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["job_location"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if has_work_location_col:
            work_location_cell = _row_value(raw_row, columns["work_location"])
            if not _is_blank(work_location_cell):
                try:
                    changes["work_location"] = _resolve_named(
                        work_location_cell, work_location_cache, "work location",
                    )
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["work_location"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if "reports_to" in columns:
            reports_to_cell = _row_value(raw_row, columns["reports_to"])
            if not _is_blank(reports_to_cell):
                try:
                    reports_to_id = _coerce_employee_id(reports_to_cell)
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["reports_to"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True
                else:
                    if reports_to_id == emp_id_str:
                        parse_errors.append(RowError(
                            line_no=line_no, col_letter=get_column_letter(columns["reports_to"] + 1),
                            raw=raw_snapshot,
                            message="An employee cannot report to themselves.",
                        ))
                        row_failed = True
                    else:
                        manager = emp_cache.get(reports_to_id)
                        if manager is None:
                            parse_errors.append(RowError(
                                line_no=line_no, col_letter=get_column_letter(columns["reports_to"] + 1),
                                raw=raw_snapshot,
                                message=f"No active employee with employee_id '{reports_to_id}' for Reports To.",
                            ))
                            row_failed = True
                        else:
                            changes["reports_to"] = manager

        if has_department_col:
            dept_cell = _row_value(raw_row, columns["department"])
            if not _is_blank(dept_cell):
                try:
                    changes["department"] = _resolve_named(dept_cell, department_cache, "department")
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["department"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if "joining_date" in columns:
            jd_cell = _row_value(raw_row, columns["joining_date"])
            if not _is_blank(jd_cell):
                try:
                    changes["joining_date"] = _coerce_date(jd_cell)
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["joining_date"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if "resource_status" in columns:
            cell = _row_value(raw_row, columns["resource_status"])
            if not _is_blank(cell):
                try:
                    changes["resource_status"] = _resolve_choice(
                        cell, resource_status_cache, "resource status", RESOURCE_STATUS_CHOICES,
                    )
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["resource_status"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if "billing_status" in columns:
            cell = _row_value(raw_row, columns["billing_status"])
            if not _is_blank(cell):
                try:
                    changes["billing_status"] = _resolve_choice(
                        cell, billing_status_cache, "billing status", BILLING_STATUS_CHOICES,
                    )
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["billing_status"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if has_gender_col:
            cell = _row_value(raw_row, columns["gender"])
            if not _is_blank(cell):
                try:
                    changes["gender"] = _resolve_choice(
                        cell, gender_cache, "gender", GENDER_CHOICES,
                    )
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["gender"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True

        if has_register_serial_col:
            cell = _row_value(raw_row, columns["register_serial_no"])
            if not _is_blank(cell):
                try:
                    serial = _coerce_register_serial_no(cell)
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["register_serial_no"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True
                else:
                    owner = register_serial_cache.get(serial)
                    if owner is not None and owner.pk != employee.pk:
                        parse_errors.append(RowError(
                            line_no=line_no, col_letter=get_column_letter(columns["register_serial_no"] + 1),
                            raw=raw_snapshot,
                            message=(
                                f"Register serial no '{serial}' is already assigned to "
                                f"{owner.display_name} ({owner.employee_id})."
                            ),
                        ))
                        row_failed = True
                    elif serial in seen_serials:
                        parse_errors.append(RowError(
                            line_no=line_no, col_letter=get_column_letter(columns["register_serial_no"] + 1),
                            raw=raw_snapshot,
                            message=(
                                f"Duplicate register serial no '{serial}' — "
                                f"also used on row {seen_serials[serial]}."
                            ),
                        ))
                        row_failed = True
                    else:
                        seen_serials[serial] = line_no
                        changes["register_serial_no"] = serial

        if has_last_working_date_col:
            lwd_cell = _row_value(raw_row, columns["last_working_date"])
            if not _is_blank(lwd_cell):
                try:
                    new_lwd = _coerce_date(lwd_cell)
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["last_working_date"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True
                else:
                    changes["last_working_date"] = new_lwd
                    existing_resignation = resignation_by_emp_pk.get(employee.pk)
                    if existing_resignation is not None and existing_resignation.last_working_date != new_lwd:
                        warnings.append(RowWarning(
                            line_no=line_no,
                            col_letter=get_column_letter(columns["last_working_date"] + 1),
                            raw=raw_snapshot,
                            message=(
                                f"Employee has a {existing_resignation.get_status_display()} resignation "
                                f"on file with Last Working Date "
                                f"{existing_resignation.last_working_date.isoformat()} — this upload sets "
                                f"{new_lwd.isoformat()}. Change still applies; reconcile the resignation "
                                "record separately if needed."
                            ),
                            employee_name=employee.display_name,
                            employee_id=emp_id_str,
                        ))

        # --- probation + confirmation (2026-11-03) -------------------------
        # A typed Confirmation Date always wins. With only Probation Months,
        # confirmation is worked out as joining date + probation (the create
        # form's rule), using this file's Joining Date if given, else the one
        # on file. Both typed but disagreeing = a soft warning: an extended
        # probation is legitimate.
        probation = None
        if "probation_month" in columns:
            cell = _row_value(raw_row, columns["probation_month"])
            if not _is_blank(cell):
                try:
                    probation = _coerce_probation_months(cell)
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=get_column_letter(columns["probation_month"] + 1),
                        raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True
                else:
                    changes["probation_month"] = probation
        joining_ref = changes.get("joining_date") or employee.joining_date
        typed_confirmation = None
        if "confirmation_date" in columns:
            cell = _row_value(raw_row, columns["confirmation_date"])
            if not _is_blank(cell):
                col_letter = get_column_letter(columns["confirmation_date"] + 1)
                try:
                    typed_confirmation = _coerce_date(cell)
                    if joining_ref and typed_confirmation < joining_ref:
                        raise ValueError(
                            f"Confirmation date {typed_confirmation.isoformat()} is before the "
                            f"joining date {joining_ref.isoformat()}."
                        )
                except ValueError as exc:
                    parse_errors.append(RowError(
                        line_no=line_no, col_letter=col_letter, raw=raw_snapshot, message=str(exc),
                    ))
                    row_failed = True
                    typed_confirmation = None
                else:
                    changes["confirmation_date"] = typed_confirmation
        if probation is not None and joining_ref:
            from dateutil.relativedelta import relativedelta

            expected = joining_ref + relativedelta(months=probation)
            if typed_confirmation is None and "confirmation_date" not in changes and not row_failed:
                changes["confirmation_date"] = expected
                changes["confirmation_date_calculated"] = True
            elif typed_confirmation is not None and typed_confirmation != expected:
                warnings.append(RowWarning(
                    line_no=line_no,
                    col_letter=get_column_letter(columns["confirmation_date"] + 1),
                    raw=raw_snapshot,
                    message=(
                        f"Confirmation Date {typed_confirmation.isoformat()} isn't joining date "
                        f"{joining_ref.isoformat()} + {probation} month(s) ({expected.isoformat()}). "
                        "The typed date is used — fine for an extended probation."
                    ),
                    employee_name=employee.display_name,
                    employee_id=emp_id_str,
                ))
        elif probation is not None and "confirmation_date" not in changes and not row_failed:
            warnings.append(RowWarning(
                line_no=line_no,
                col_letter=get_column_letter(columns["probation_month"] + 1),
                raw=raw_snapshot,
                message="No joining date on file, so the confirmation date can't be worked out — type it in.",
                employee_name=employee.display_name,
                employee_id=emp_id_str,
            ))

        # --- profile sections (2026-10-05) -------------------------------
        def _fail(col_key: str, exc: Exception) -> None:
            nonlocal row_failed
            parse_errors.append(RowError(
                line_no=line_no, col_letter=get_column_letter(columns[col_key] + 1),
                raw=raw_snapshot, message=str(exc),
            ))
            row_failed = True

        def _warn(col_key: str, message: str) -> None:
            warnings.append(RowWarning(
                line_no=line_no, col_letter=get_column_letter(columns[col_key] + 1),
                raw=raw_snapshot, message=message,
                employee_name=employee.display_name, employee_id=emp_id_str,
            ))

        for col_key, (change_key, label, max_length) in _PROFILE_TEXT_COLUMNS.items():
            if col_key not in columns:
                continue
            cell = _row_value(raw_row, columns[col_key])
            if _is_blank(cell):
                continue
            try:
                changes[change_key] = (
                    _coerce_nid(cell) if col_key == "nid" else _coerce_text(cell, label, max_length)
                )
            except ValueError as exc:
                _fail(col_key, exc)

        if "date_of_birth" in columns:
            cell = _row_value(raw_row, columns["date_of_birth"])
            if not _is_blank(cell):
                try:
                    changes["date_of_birth"] = _coerce_date_of_birth(cell)
                except ValueError as exc:
                    _fail("date_of_birth", exc)

        if "blood_group" in columns:
            cell = _row_value(raw_row, columns["blood_group"])
            if not _is_blank(cell):
                try:
                    changes["blood_group"] = _coerce_blood_group(cell)
                except ValueError as exc:
                    _fail("blood_group", exc)

        if "height_in_inch" in columns:
            cell = _row_value(raw_row, columns["height_in_inch"])
            if not _is_blank(cell) and str(cell).strip() not in ("-", "—", "n/a", "N/A"):
                try:
                    changes["height_in_inch"] = _coerce_height_inches(cell)
                except ValueError as exc:
                    _fail("height_in_inch", exc)

        if "education_level" in columns:
            cell = _row_value(raw_row, columns["education_level"])
            if not _is_blank(cell):
                try:
                    code, label = _resolve_education_level(cell)
                except ValueError as exc:
                    _fail("education_level", exc)
                else:
                    changes["education"] = {"degree_level": code, "degree_name": label}

        for prefix, present_cols in address_columns.items():
            addr: dict = {}
            for suffix, (model_field, max_length) in _ADDRESS_TEXT_FIELDS.items():
                if suffix not in present_cols:
                    continue
                cell = _row_value(raw_row, present_cols[suffix])
                if _is_blank(cell):
                    continue
                try:
                    addr[model_field] = (
                        _coerce_post_code(cell) if suffix == "post_code"
                        else _coerce_text(cell, f"{prefix} {suffix.replace('_', ' ')}", max_length)
                    )
                except ValueError as exc:
                    _fail(f"{prefix}_{suffix}", exc)
            state = None
            if "district" in present_cols:
                cell = _row_value(raw_row, present_cols["district"])
                if not _is_blank(cell):
                    try:
                        state = _resolve_district(cell, state_cache)
                    except ValueError as exc:
                        _fail(f"{prefix}_district", exc)
                    else:
                        addr["state"] = state
            if "police_station" in present_cols:
                cell = _row_value(raw_row, present_cols["police_station"])
                if not _is_blank(cell):
                    if state is None:
                        _warn(
                            f"{prefix}_police_station",
                            f"Police station '{str(cell).strip()}' ignored — a {prefix} district "
                            "is needed to look up the upazila/thana.",
                        )
                    else:
                        city = _resolve_police_station(cell, state, city_cache)
                        if city is None:
                            _warn(
                                f"{prefix}_police_station",
                                f"Police station '{str(cell).strip()}' not found under "
                                f"{state.name} — left blank, set it from the profile drawer.",
                            )
                        else:
                            addr["city"] = city
                            if addr.get("union_name"):
                                union = _resolve_union_ward(addr["union_name"], city, union_cache)
                                if union is not None:
                                    addr["union_ward"] = union
            if addr:
                addr["country"] = bangladesh
                changes[f"address_{_ADDRESS_PREFIXES[prefix].lower()}"] = addr

        if row_failed:
            continue
        if not changes:
            continue

        parsed.append(ParsedRow(
            line_no=line_no, employee=employee, employee_id=emp_id_str,
            changes=changes, raw=raw_snapshot,
        ))

    return parsed, parse_errors, dup_errors, warnings


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------

def apply_upload(
    viewer,
    parsed_rows: list[ParsedRow],
    editable_pks: set,
    warnings: list[RowWarning] | None = None,
) -> ApplyReport:
    """Apply parsed rows to CustomUser, JobInfo, and PersonalInfo inside one transaction.

    `editable_pks` is the set of employee pks this viewer may write to — the
    caller computes it as every active employee for HR/Admin, or
    `managed_scope_pks(viewer, request=request)` otherwise (same scope
    `can_edit_employee_profile`/`can_edit_employee_basic_info` already grant
    for JobInfo/CustomUser edits). Rows outside that scope aren't written;
    they appear in `report.skipped_scope` instead.

    `warnings` (from `parse_upload`) is carried straight onto the returned
    report so the caller can render it alongside the apply results.

    A Last Working Date change is a raw CustomUser write, same as the HR
    edit form and Django admin — no Resignation is created. Because it
    writes via `bulk_update()`, which doesn't fire the `CustomUser`
    pre_save/post_save pair in offboarding/signals.py, this function purges
    future roster shifts itself for any row whose LWD actually changed.
    """
    report = ApplyReport(warnings=list(warnings) if warnings else [])

    in_scope: list[ParsedRow] = []
    for row in parsed_rows:
        if row.employee.pk in editable_pks:
            in_scope.append(row)
        else:
            report.skipped_scope.append(RowError(
                line_no=row.line_no, col_letter=None,
                raw=row.raw, message="Outside your managed scope.",
            ))

    if not in_scope:
        report.applied = True
        return report

    emp_pks = {r.employee.pk for r in in_scope}
    existing_job_infos = {
        ji.employee_id: ji for ji in JobInfo.objects.filter(employee_id__in=emp_pks)
    }
    existing_personal_infos = {
        pi.employee_id: pi for pi in PersonalInfo.objects.filter(employee_id__in=emp_pks)
    }
    existing_family_infos = {
        fi.employee_id: fi for fi in FamilyInfo.objects.filter(employee_id__in=emp_pks)
    }
    # Address isn't unique per (employee, type); take the oldest row of each
    # type as "the" one to update so a re-upload never adds a second.
    existing_addresses: dict[tuple, Address] = {}
    for addr in Address.objects.filter(employee_id__in=emp_pks).order_by("id"):
        existing_addresses.setdefault((addr.employee_id, addr.address_type), addr)
    existing_nids: dict = {}
    for idn in (
        IdentificationNumber.objects
        .filter(employee_id__in=emp_pks, id_type=IdentificationNumber.IdType.NID)
        .order_by("id")
    ):
        existing_nids.setdefault(idn.employee_id, idn)
    existing_education_levels = set(
        Education.objects.filter(employee_id__in=emp_pks).values_list("employee_id", "degree_level")
    )
    existing_certifications = {
        (emp_pk, name.strip().lower())
        for emp_pk, name in ProfessionalCertification.objects
        .filter(employee_id__in=emp_pks).values_list("employee_id", "certificate_name")
    }

    now = timezone.now()
    user_updates: list = []
    job_info_creates: list[JobInfo] = []
    job_info_updates: list[JobInfo] = []
    personal_info_creates: list[PersonalInfo] = []
    personal_info_updates: list[PersonalInfo] = []
    family_info_creates: list[FamilyInfo] = []
    family_info_updates: list[FamilyInfo] = []
    address_creates: list[Address] = []
    address_updates: list[Address] = []
    nid_creates: list[IdentificationNumber] = []
    nid_updates: list[IdentificationNumber] = []
    education_creates: list[Education] = []
    certification_creates: list[ProfessionalCertification] = []
    touched_job_fields: set[str] = set()
    touched_user_fields: set[str] = set()
    touched_personal_fields: set[str] = set()
    touched_family_fields: set[str] = set()
    touched_address_fields: set[str] = set()
    lwd_changed_employees: list = []

    for row in in_scope:
        job_fields = {k: v for k, v in row.changes.items() if k in ("reports_to", "resource_status", "billing_status")}
        user_fields = {
            k: v for k, v in row.changes.items()
            if k in ("team", "department", "joining_date", "register_serial_no",
                      "job_location", "work_location", "last_working_date",
                      "probation_month", "confirmation_date")
        }
        personal_fields = {k: v for k, v in row.changes.items() if k in _PERSONAL_INFO_KEYS}
        family_fields = {k: v for k, v in row.changes.items() if k in ("spouse_name",)}

        if user_fields:
            if "last_working_date" in user_fields and row.employee.last_working_date != user_fields["last_working_date"]:
                lwd_changed_employees.append(row.employee)
            for k, v in user_fields.items():
                setattr(row.employee, k, v)
            user_updates.append(row.employee)
            touched_user_fields |= set(user_fields.keys())

        existing_ji = existing_job_infos.get(row.employee.pk)
        if job_fields:
            if existing_ji is None:
                existing_ji = JobInfo(
                    employee=row.employee,
                    added_by=viewer, added_at=now,
                    updated_by=viewer, updated_at=now,
                )
                for k, v in job_fields.items():
                    setattr(existing_ji, k, v)
                job_info_creates.append(existing_ji)
            else:
                for k, v in job_fields.items():
                    setattr(existing_ji, k, v)
                existing_ji.updated_by = viewer
                existing_ji.updated_at = now
                job_info_updates.append(existing_ji)
                touched_job_fields |= set(job_fields.keys())

        existing_pi = existing_personal_infos.get(row.employee.pk)
        if personal_fields:
            if existing_pi is None:
                existing_pi = PersonalInfo(
                    employee=row.employee,
                    added_by=viewer, added_at=now,
                    updated_by=viewer, updated_at=now,
                )
                for k, v in personal_fields.items():
                    setattr(existing_pi, k, v)
                personal_info_creates.append(existing_pi)
            else:
                for k, v in personal_fields.items():
                    setattr(existing_pi, k, v)
                existing_pi.updated_by = viewer
                existing_pi.updated_at = now
                personal_info_updates.append(existing_pi)
                touched_personal_fields |= set(personal_fields.keys())

        if family_fields:
            existing_fi = existing_family_infos.get(row.employee.pk)
            if "spouse_name" in family_fields and not (existing_fi and existing_fi.marital_status):
                # A spouse on file implies married; only fill the status when
                # nothing is recorded yet, never override what HR already set.
                family_fields["marital_status"] = FamilyInfo.MaritalStatus.MARRIED
            if existing_fi is None:
                existing_fi = FamilyInfo(
                    employee=row.employee,
                    added_by=viewer, added_at=now,
                    updated_by=viewer, updated_at=now,
                )
                for k, v in family_fields.items():
                    setattr(existing_fi, k, v)
                family_info_creates.append(existing_fi)
                existing_family_infos[row.employee.pk] = existing_fi
            else:
                for k, v in family_fields.items():
                    setattr(existing_fi, k, v)
                existing_fi.updated_by = viewer
                existing_fi.updated_at = now
                family_info_updates.append(existing_fi)
                touched_family_fields |= set(family_fields.keys())

        for address_type in (Address.AddressType.PERMANENT, Address.AddressType.PRESENT):
            addr_fields = row.changes.get(f"address_{address_type.lower()}")
            if not addr_fields:
                continue
            existing_addr = existing_addresses.get((row.employee.pk, address_type))
            if existing_addr is None:
                existing_addr = Address(
                    employee=row.employee, address_type=address_type,
                    added_by=viewer, added_at=now,
                    updated_by=viewer, updated_at=now,
                )
                for k, v in addr_fields.items():
                    setattr(existing_addr, k, v)
                address_creates.append(existing_addr)
                existing_addresses[(row.employee.pk, address_type)] = existing_addr
            else:
                for k, v in addr_fields.items():
                    setattr(existing_addr, k, v)
                existing_addr.updated_by = viewer
                existing_addr.updated_at = now
                address_updates.append(existing_addr)
                touched_address_fields |= set(addr_fields.keys())

        nid = row.changes.get("nid")
        if nid:
            existing_nid = existing_nids.get(row.employee.pk)
            if existing_nid is None:
                nid_creates.append(IdentificationNumber(
                    employee=row.employee, id_type=IdentificationNumber.IdType.NID,
                    id_number=nid,
                    added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
                ))
            elif existing_nid.id_number != nid:
                existing_nid.id_number = nid
                existing_nid.updated_by = viewer
                existing_nid.updated_at = now
                nid_updates.append(existing_nid)

        education = row.changes.get("education")
        if education and (row.employee.pk, education["degree_level"]) not in existing_education_levels:
            education_creates.append(Education(
                employee=row.employee,
                degree_level=education["degree_level"], degree_name=education["degree_name"],
                added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
            ))
            existing_education_levels.add((row.employee.pk, education["degree_level"]))

        certification = row.changes.get("certification")
        if certification and (row.employee.pk, certification.lower()) not in existing_certifications:
            certification_creates.append(ProfessionalCertification(
                employee=row.employee, certificate_name=certification,
                added_by=viewer, added_at=now, updated_by=viewer, updated_at=now,
            ))
            existing_certifications.add((row.employee.pk, certification.lower()))

        if existing_job_infos.get(row.employee.pk) is None and job_fields:
            report.created.append(row)
        else:
            report.updated.append(row)

    with transaction.atomic():
        if user_updates:
            # bulk_update skips CustomUser.save(), so stamp the Directory API
            # change feed here (docs/DIRECTORY_API_PLAN.md §5.4).
            stamp = directory_stamp(*touched_user_fields)
            if stamp:
                for employee in user_updates:
                    employee.directory_updated_at = stamp["directory_updated_at"]
            User.objects.bulk_update(user_updates, [*touched_user_fields, *stamp])
            for employee in lwd_changed_employees:
                purge_shifts_after_last_working_date(employee, actor=viewer)
        if job_info_creates:
            JobInfo.objects.bulk_create(job_info_creates)
        if job_info_updates:
            JobInfo.objects.bulk_update(
                job_info_updates,
                ["updated_by", "updated_at", *touched_job_fields],
            )
        if personal_info_creates:
            PersonalInfo.objects.bulk_create(personal_info_creates)
        if personal_info_updates:
            PersonalInfo.objects.bulk_update(
                personal_info_updates,
                ["updated_by", "updated_at", *touched_personal_fields],
            )
        if family_info_creates:
            FamilyInfo.objects.bulk_create(family_info_creates)
        if family_info_updates:
            FamilyInfo.objects.bulk_update(
                family_info_updates,
                ["updated_by", "updated_at", *(touched_family_fields | {"marital_status"})],
            )
        if address_creates:
            Address.objects.bulk_create(address_creates)
        if address_updates:
            Address.objects.bulk_update(
                address_updates,
                ["updated_by", "updated_at", *touched_address_fields],
            )
        if nid_creates:
            IdentificationNumber.objects.bulk_create(nid_creates)
        if nid_updates:
            IdentificationNumber.objects.bulk_update(
                nid_updates, ["id_number", "updated_by", "updated_at"],
            )
        if education_creates:
            Education.objects.bulk_create(education_creates)
        if certification_creates:
            ProfessionalCertification.objects.bulk_create(certification_creates)

    report.applied = True
    return report


# ---------------------------------------------------------------------------
# Template builders
# ---------------------------------------------------------------------------

def _template_employees(viewer, request=None) -> list:
    """Employees this viewer may bulk-edit, ordered for display."""
    qs = (
        User.objects
        .filter(is_active=True, employee_id__isnull=False)
        .exclude(employee_id="")
        .select_related(
            "team", "department", "job_info", "job_info__reports_to", "personal_info",
            "family_info", "job_location", "work_location",
        )
        .prefetch_related("addresses", "id_numbers", "educations", "certifications")
    )
    if not can_edit_all(viewer, request=request):
        qs = qs.filter(pk__in=managed_scope_pks(viewer, request=request))
    return list(qs.order_by("first_name", "last_name"))


_TEMPLATE_HEADERS = [
    "Employee ID", "Employee Name", "Team", "Reports To", "Resource Status", "Billing Status",
    "Job Location", "Work Location",
]

# Appended only for viewers with org-wide write scope — see _HR_ONLY_FIELDS.
_HR_TEMPLATE_HEADERS = [
    "Department", "Joining Date", "Probation Months", "Confirmation Date",
    "Gender", "Employee Register Serial No.", "Last Working Date",
]

# Profile-section columns (2026-10-05), appended last for every viewer —
# they follow the drawers' managed-scope rule, not the HR-only one.
_ADDRESS_TEMPLATE_SUFFIXES = [
    ("Flat No", "flat_no"), ("Holding No", "holding_no"), ("Village/Area", "village_area"),
    ("Post Office", "post_office"), ("Union", "union_name"), ("Post Code", "post_code"),
    ("District", "state"), ("Police Station", "city"),
]
_PROFILE_TEMPLATE_HEADERS = [
    "Father's Name", "Mother's Name", "Date of Birth", "Blood Group", "Height (inches)",
    "Identification Mark", "Spouse Name", "NID",
    *[f"Permanent {label}" for label, _ in _ADDRESS_TEMPLATE_SUFFIXES],
    *[f"Present {label}" for label, _ in _ADDRESS_TEMPLATE_SUFFIXES],
    "Highest Education", "Professional Certification",
]


def _address_template_values(addr) -> list:
    if addr is None:
        return [""] * len(_ADDRESS_TEMPLATE_SUFFIXES)
    out = []
    for _, attr in _ADDRESS_TEMPLATE_SUFFIXES:
        value = getattr(addr, attr)
        out.append(value.name if attr in ("state", "city") and value is not None else (value or ""))
    return out


def _profile_template_values(emp) -> list:
    personal = getattr(emp, "personal_info", None)
    family = getattr(emp, "family_info", None)
    addresses = {a.address_type: a for a in reversed(list(emp.addresses.all()))}
    nid = next((i.id_number for i in emp.id_numbers.all() if i.id_type == IdentificationNumber.IdType.NID), "")
    educations = list(emp.educations.all())
    rank = {code: i for i, (code, _) in enumerate(DEGREE_LEVEL_CHOICES)}
    highest = max(educations, key=lambda e: rank.get(e.degree_level, -1)) if educations else None
    certs = list(emp.certifications.all())
    return [
        personal.father_name if personal else "",
        personal.mother_name if personal else "",
        personal.date_of_birth.isoformat() if personal and personal.date_of_birth else "",
        personal.blood_group if personal else "",
        personal.height_in_inch if personal and personal.height_in_inch else "",
        personal.identification_mark if personal else "",
        family.spouse_name if family else "",
        nid,
        *_address_template_values(addresses.get(Address.AddressType.PERMANENT)),
        *_address_template_values(addresses.get(Address.AddressType.PRESENT)),
        highest.get_degree_level_display() if highest else "",
        certs[0].certificate_name if certs else "",
    ]


def _template_rows(employees: list, include_hr_fields: bool) -> list[list]:
    rows = []
    for emp in employees:
        job_info = getattr(emp, "job_info", None)
        personal_info = getattr(emp, "personal_info", None)
        row = [
            emp.employee_id or "",
            escape_spreadsheet(emp.display_name),
            emp.team.name if emp.team_id else "",
            job_info.reports_to.employee_id if job_info and job_info.reports_to_id and job_info.reports_to.employee_id else "",
            job_info.get_resource_status_display() if job_info and job_info.resource_status else "",
            job_info.get_billing_status_display() if job_info and job_info.billing_status else "",
            emp.job_location.name if emp.job_location_id else "",
            emp.work_location.name if emp.work_location_id else "",
        ]
        if include_hr_fields:
            row.append(emp.department.name if emp.department_id else "")
            row.append(emp.joining_date.isoformat() if emp.joining_date else "")
            row.append(emp.probation_month if emp.probation_month is not None else "")
            row.append(emp.confirmation_date.isoformat() if emp.confirmation_date else "")
            row.append(personal_info.get_gender_display() if personal_info and personal_info.gender else "")
            row.append(emp.register_serial_no or "")
            row.append(emp.last_working_date.isoformat() if emp.last_working_date else "")
        row.extend(escape_spreadsheet(v) if isinstance(v, str) else v for v in _profile_template_values(emp))
        rows.append(row)
    return rows


def build_xlsx_template(viewer, request=None) -> bytes:
    include_hr_fields = can_edit_all(viewer, request=request)

    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"

    employees = _template_employees(viewer, request=request)
    headers = (
        _TEMPLATE_HEADERS
        + (_HR_TEMPLATE_HEADERS if include_hr_fields else [])
        + _PROFILE_TEMPLATE_HEADERS
    )
    text_cols = {1, 4}
    if include_hr_fields:
        text_cols.add(1 + headers.index("Employee Register Serial No."))
    # NIDs and post codes must stay text — Excel turns a 17-digit NID into a
    # float and loses digits otherwise.
    text_cols |= {1 + i for i, h in enumerate(headers) if h == "NID" or h.endswith("Post Code")}

    for col, label in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=label)

    for ridx, row_values in enumerate(_template_rows(employees, include_hr_fields), start=2):
        for col, value in enumerate(row_values, start=1):
            cell = ws.cell(row=ridx, column=col, value=value)
            if col in text_cols:
                cell.number_format = "@"

    team_names = sorted(t.name for t in Team.objects.filter(is_active=True))
    resource_labels = [label for _, label in RESOURCE_STATUS_CHOICES]
    billing_labels = [label for _, label in BILLING_STATUS_CHOICES]
    job_location_names = sorted(l.name for l in JobLocation.objects.filter(is_active=True))
    work_location_names = sorted(l.name for l in WorkLocation.objects.filter(is_active=True))
    department_names = (
        sorted(d.name for d in Department.objects.filter(is_active=True)) if include_hr_fields else []
    )
    gender_labels = [label for _, label in GENDER_CHOICES] if include_hr_fields else []

    lookups = wb.create_sheet("Lookups")
    lookups.cell(row=1, column=1, value="Teams")
    lookups.cell(row=1, column=2, value="Resource Status")
    lookups.cell(row=1, column=3, value="Billing Status")
    lookups.cell(row=1, column=4, value="Job Locations")
    lookups.cell(row=1, column=5, value="Work Locations")
    for i, name in enumerate(team_names, start=2):
        lookups.cell(row=i, column=1, value=name)
    for i, name in enumerate(resource_labels, start=2):
        lookups.cell(row=i, column=2, value=name)
    for i, name in enumerate(billing_labels, start=2):
        lookups.cell(row=i, column=3, value=name)
    for i, name in enumerate(job_location_names, start=2):
        lookups.cell(row=i, column=4, value=name)
    for i, name in enumerate(work_location_names, start=2):
        lookups.cell(row=i, column=5, value=name)
    if include_hr_fields:
        lookups.cell(row=1, column=6, value="Departments")
        for i, name in enumerate(department_names, start=2):
            lookups.cell(row=i, column=6, value=name)
        lookups.cell(row=1, column=7, value="Genders")
        for i, name in enumerate(gender_labels, start=2):
            lookups.cell(row=i, column=7, value=name)
    lookups.sheet_state = "hidden"

    last_row = 1 + len(employees)
    if employees and team_names:
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$A$2:$A${1 + len(team_names)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid team"
        ws.add_data_validation(dv)
        dv.add(f"C2:C{last_row}")
    if employees:
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$B$2:$B${1 + len(resource_labels)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid resource status"
        ws.add_data_validation(dv)
        dv.add(f"E2:E{last_row}")

        dv = DataValidation(
            type="list", formula1=f"=Lookups!$C$2:$C${1 + len(billing_labels)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid billing status"
        ws.add_data_validation(dv)
        dv.add(f"F2:F{last_row}")

    if employees and job_location_names:
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$D$2:$D${1 + len(job_location_names)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid job location"
        ws.add_data_validation(dv)
        dv.add(f"G2:G{last_row}")

    if employees and work_location_names:
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$E$2:$E${1 + len(work_location_names)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid work location"
        ws.add_data_validation(dv)
        dv.add(f"H2:H{last_row}")

    if employees and include_hr_fields and department_names:
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$F$2:$F${1 + len(department_names)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid department"
        ws.add_data_validation(dv)
        dv.add(f"I2:I{last_row}")

    if employees and include_hr_fields and gender_labels:
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$G$2:$G${1 + len(gender_labels)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid gender"
        ws.add_data_validation(dv)
        # Located by header, not a fixed letter — the HR block grew two
        # columns on 2026-11-03 and a pinned "K" landed on the wrong one.
        gender_col = get_column_letter(1 + headers.index("Gender"))
        dv.add(f"{gender_col}2:{gender_col}{last_row}")

    if employees and include_hr_fields:
        probation_col = get_column_letter(1 + headers.index("Probation Months"))
        dv = DataValidation(
            type="whole", operator="between", formula1="0", formula2=str(MAX_PROBATION_MONTHS),
            allow_blank=True,
        )
        dv.error = f"Whole months, 0 to {MAX_PROBATION_MONTHS}."
        dv.errorTitle = "Invalid probation months"
        ws.add_data_validation(dv)
        dv.add(f"{probation_col}2:{probation_col}{last_row}")
        confirmation_col = get_column_letter(1 + headers.index("Confirmation Date"))
        dv = DataValidation(type="custom", formula1="TRUE", allow_blank=True)
        dv.prompt = (
            "YYYY-MM-DD. Leave blank and fill Probation Months to have it worked out "
            "as joining date + probation."
        )
        dv.promptTitle = "Confirmation Date"
        dv.showInputMessage = True
        ws.add_data_validation(dv)
        dv.add(f"{confirmation_col}2:{confirmation_col}{last_row}")

    blood_codes = [code for code, _ in BLOOD_GROUP_CHOICES]
    degree_labels = [label for _, label in DEGREE_LEVEL_CHOICES]
    lookups.cell(row=1, column=8, value="Blood Groups")
    for i, name in enumerate(blood_codes, start=2):
        lookups.cell(row=i, column=8, value=name)
    lookups.cell(row=1, column=9, value="Education Levels")
    for i, name in enumerate(degree_labels, start=2):
        lookups.cell(row=i, column=9, value=name)
    if employees:
        blood_col = get_column_letter(1 + headers.index("Blood Group"))
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$H$2:$H${1 + len(blood_codes)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid blood group"
        ws.add_data_validation(dv)
        dv.add(f"{blood_col}2:{blood_col}{last_row}")

        edu_col = get_column_letter(1 + headers.index("Highest Education"))
        dv = DataValidation(
            type="list", formula1=f"=Lookups!$I$2:$I${1 + len(degree_labels)}", allow_blank=True,
        )
        dv.error = "Pick a value from the dropdown."
        dv.errorTitle = "Invalid education level"
        ws.add_data_validation(dv)
        dv.add(f"{edu_col}2:{edu_col}{last_row}")

    widths = {
        "A": 14, "B": 26, "C": 20, "D": 16, "E": 16, "F": 16,
        "G": 18, "H": 16, "I": 20, "J": 16, "K": 12, "L": 24, "M": 16,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    for i, header in enumerate(headers, start=1):
        if header in _PROFILE_TEMPLATE_HEADERS:
            ws.column_dimensions[get_column_letter(i)].width = 18
        elif header in _HR_TEMPLATE_HEADERS:
            ws.column_dimensions[get_column_letter(i)].width = 24 if header.startswith("Employee Register") else 18
    ws.freeze_panes = "A2"

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def build_csv_template(viewer, request=None) -> str:
    include_hr_fields = can_edit_all(viewer, request=request)
    employees = _template_employees(viewer, request=request)
    headers = (
        _TEMPLATE_HEADERS
        + (_HR_TEMPLATE_HEADERS if include_hr_fields else [])
        + _PROFILE_TEMPLATE_HEADERS
    )
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row_values in _template_rows(employees, include_hr_fields):
        writer.writerow(row_values)
    return out.getvalue()


def render_template_response(viewer, fmt: str, request=None) -> HttpResponse:
    fmt = (fmt or "").lower()
    today_stamp = timezone.now().date().isoformat()
    if fmt == "xlsx":
        body = build_xlsx_template(viewer, request=request)
        resp = HttpResponse(
            body,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="employee-bulk-update-template-{today_stamp}.xlsx"'
        )
        return resp
    if fmt == "csv":
        body = build_csv_template(viewer, request=request)
        resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="employee-bulk-update-template-{today_stamp}.csv"'
        )
        return resp
    raise Http404("Unknown template format.")
