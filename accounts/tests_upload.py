"""Unit tests for accounts.upload — email/ID generation, parser, applier."""

from __future__ import annotations

import io
from datetime import date

import openpyxl
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import (
    AccessLevel, Department, Designation, EmploymentType, Grade, JobLocation, WorkLocation,
)
from accounts.upload import (
    PASSWORD_LENGTH,
    TEMPLATE_HEADERS,
    apply_upload,
    build_onboard_results_xlsx,
    build_onboard_xlsx_template,
    generate_email,
    generate_password,
    next_employee_id_seed,
    parse_upload,
)
from employees.models import IdentificationNumber, JobInfo
from payroll.models import SalaryStructure

User = get_user_model()


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        employee_id=employee_id, **extra,
    )


def _xlsx(header: list[str], rows: list[list]) -> tuple[io.BytesIO, str]:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream, "upload.xlsx"


_VALID_ROW_DEFAULTS = {
    "Full Name": "John Michael Smith",
    "Employee ID": "",
    "Email": "",
    "Department": "Marketing",
    "Designation": "Associate",
    "Grade": "G1",
    "Team": "",
    "Job Location": "Dhaka",
    "Work Location": "MHK",
    "Employment Type": "Full Time",
    "Contact Number": "01700000000",
    "NID Number": "1234567890123",
    "Joining Date": "2026-09-01",
    "Probation Month": "3",
    "Notice Period (Months)": "2",
    "Reports To (Employee ID)": "500",
    "Resource Status": "Live",
    "Billing Status": "Billable",
    "Gross Monthly Salary": "50000",
    "Effective From": "2026-09-01",
    "Reason": "New hire",
}


def _valid_row(**overrides) -> list:
    """One valid full-width row matching TEMPLATE_HEADERS, with named
    overrides — every column is required except Team/Employee ID/Email, so
    tests build on this rather than repeating all 21 columns each time."""
    values = {**_VALID_ROW_DEFAULTS, **overrides}
    return [values[h] for h in TEMPLATE_HEADERS]


class EmailGenerationTests(TestCase):
    def test_default_department_first_dot_last(self):
        taken: set[str] = set()
        self.assertEqual(
            generate_email("John", "", "Smith", "Marketing", taken=taken),
            "john.smith@sebpo.com",
        )

    def test_default_department_collision_widens_before_middle_name_absent(self):
        taken = {"john.smith@sebpo.com"}
        email = generate_email("John", "", "Smith", "Marketing", taken=taken)
        self.assertNotEqual(email, "john.smith@sebpo.com")
        self.assertTrue(email.endswith("@sebpo.com"))
        self.assertFalse(any(ch.isdigit() for ch in email.split("@")[0]))

    def test_default_department_collision_uses_middle_name_first(self):
        taken = {"john.smith@sebpo.com"}
        email = generate_email("John", "Michael", "Smith", "Marketing", taken=taken)
        self.assertEqual(email, "john.michael.smith@sebpo.com")

    def test_data_solutions_uses_initials(self):
        taken: set[str] = set()
        email = generate_email("John", "Michael", "Smith", "Data Solutions", taken=taken)
        self.assertEqual(email, "jmsmith@sebpo.com")

    def test_data_solutions_no_middle_name_widens(self):
        taken: set[str] = set()
        email = generate_email("John", "", "Smith", "Data Solutions", taken=taken)
        self.assertTrue(email.endswith("@sebpo.com"))
        self.assertNotIn("@", email.split("@")[0])

    def test_data_solutions_initials_collision_widens(self):
        taken = {"jmsmith@sebpo.com"}
        email = generate_email("Jane", "Marie", "Smith", "Data Solutions", taken=taken)
        self.assertNotEqual(email, "jmsmith@sebpo.com")
        self.assertTrue(email.endswith("@sebpo.com"))
        # Data Solutions never uses a dot anywhere, even once collisions
        # force the ladder to widen past the initials.
        self.assertNotIn(".", email.split("@")[0])

    def test_no_numeric_suffix_ever(self):
        taken: set[str] = set()
        first_email = generate_email("Amy", "", "Lee", "Marketing", taken=taken)
        second_email = generate_email("Amy", "", "Lee", "Marketing", taken=taken)
        self.assertNotEqual(first_email, second_email)
        for email in (first_email, second_email):
            local = email.split("@")[0]
            self.assertFalse(any(ch.isdigit() for ch in local), f"{email} contains a digit")

    def test_taken_set_is_mutated_in_place(self):
        taken: set[str] = set()
        email = generate_email("Amy", "", "Lee", "Marketing", taken=taken)
        self.assertIn(email, taken)

    def test_exhausted_name_space_raises(self):
        # Force every rung of the ladder to already be taken.
        taken = {
            "al.lee@sebpo.com", "a.lee@sebpo.com", "al.lee@sebpo.com",
            "allee@sebpo.com", "alee@sebpo.com",
        }
        with self.assertRaises(ValueError):
            generate_email("Al", "", "Lee", "Marketing", taken=taken)


class GeneratePasswordTests(TestCase):
    def test_length_and_alphabet(self):
        password = generate_password()
        self.assertEqual(len(password), PASSWORD_LENGTH)
        for ch in password:
            self.assertNotIn(ch, "0O1Il")

    def test_passwords_are_not_repeated(self):
        passwords = {generate_password() for _ in range(20)}
        self.assertEqual(len(passwords), 20)


class NextEmployeeIdSeedTests(TestCase):
    def test_seed_ignores_lexicographic_ordering(self):
        _user("a@sebpo.com", "992")
        _user("b@sebpo.com", "55134")
        self.assertEqual(next_employee_id_seed(), 55134)

    def test_seed_zero_when_no_employees(self):
        self.assertEqual(next_employee_id_seed(), 0)


class ParseApplyRoundTripTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name="Marketing")
        cls.access_level = AccessLevel.objects.create(name="IC", rank=10)
        cls.designation = Designation.objects.create(name="Associate", access_level=cls.access_level)
        Grade.objects.create(name="G1")
        JobLocation.objects.create(name="Dhaka")
        WorkLocation.objects.create(name="MHK")
        EmploymentType.objects.create(name="Full Time")
        cls.manager = _user("manager@sebpo.com", "500", department=cls.dept)

    def test_happy_path_creates_user_jobinfo_salary(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row()])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parse_errors, [])
        self.assertEqual(dup_errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(parsed), 1)
        row = parsed[0]
        self.assertEqual(row.email, "john.smith@sebpo.com")
        self.assertTrue(row.employee_id.isdigit())
        self.assertEqual(row.nid_number, "1234567890123")
        self.assertEqual(row.notice_period_months, 2)

        report = apply_upload(self.manager, parsed, warnings=warnings)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(report.apply_errors, [])

        user = User.objects.get(pk=row.created_employee_pk)
        self.assertEqual(user.email, "john.smith@sebpo.com")
        self.assertEqual(user.department, self.dept)
        self.assertEqual(user.access_level, self.access_level)
        self.assertEqual(user.notice_period_months, 2)
        created_row = report.created[0]
        self.assertEqual(len(created_row.generated_password), 12)
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password(created_row.generated_password))

        nid = IdentificationNumber.objects.get(employee=user)
        self.assertEqual(nid.id_type, IdentificationNumber.IdType.NID)
        self.assertEqual(nid.id_number, "1234567890123")

        job_info = JobInfo.objects.get(employee=user)
        self.assertEqual(job_info.reports_to, self.manager)
        self.assertEqual(job_info.resource_status, JobInfo.ResourceStatus.LIVE)

        salary = SalaryStructure.objects.get(employee=user)
        self.assertEqual(salary.gross_monthly, 50000)
        self.assertEqual(salary.reason, SalaryStructure.Reason.NEW_HIRE)
        self.assertEqual(salary.status, SalaryStructure.Status.APPROVED)

    def test_missing_department_is_a_parse_error(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row(**{"Department": ""})])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parsed, [])
        self.assertEqual(len(parse_errors), 1)

    def test_unknown_department_is_a_parse_error(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row(**{"Department": "Nope"})])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parsed, [])
        self.assertEqual(len(parse_errors), 1)

    def test_missing_effective_from_is_a_parse_error(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row(**{"Effective From": ""})])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parsed, [])
        self.assertEqual(len(parse_errors), 1)

    def test_missing_team_is_not_an_error(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row(**{"Team": ""})])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parse_errors, [])
        self.assertEqual(len(parsed), 1)

    def test_duplicate_employee_id_within_file(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [
            _valid_row(**{"Full Name": "Jane Doe", "Employee ID": "9001"}),
            _valid_row(**{"Full Name": "Jane Two", "Employee ID": "9001"}),
        ])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(dup_errors), 1)

    def test_explicit_employee_id_already_exists(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row(**{"Employee ID": "500"})])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parsed, [])
        self.assertEqual(len(parse_errors), 1)

    def test_two_same_name_rows_get_distinct_emails_and_ids(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [
            _valid_row(**{"Full Name": "John Smith"}),
            _valid_row(**{"Full Name": "John Smith"}),
        ])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parse_errors, [])
        self.assertEqual(dup_errors, [])
        self.assertEqual(len(parsed), 2)
        self.assertNotEqual(parsed[0].email, parsed[1].email)
        self.assertNotEqual(parsed[0].employee_id, parsed[1].employee_id)

    def test_bad_row_does_not_block_the_rest_of_the_batch(self):
        stream, name = _xlsx(TEMPLATE_HEADERS, [
            _valid_row(**{"Full Name": "Bad Row", "Department": "Nope"}),
            _valid_row(**{"Full Name": "Good Row"}),
        ])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(len(parse_errors), 1)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].full_name, "Good Row")

    def test_rehire_nid_match_is_a_warning_not_an_error(self):
        former = _user("former@sebpo.com", "600", department=self.dept, is_active=False)
        IdentificationNumber.objects.create(
            employee=former, id_type=IdentificationNumber.IdType.NID, id_number="1234567890123",
            added_by=self.manager, added_at=timezone.now(),
            updated_by=self.manager, updated_at=timezone.now(),
        )
        stream, name = _xlsx(TEMPLATE_HEADERS, [_valid_row()])
        parsed, parse_errors, dup_errors, warnings = parse_upload(stream, name, today=date(2026, 8, 31))
        self.assertEqual(parse_errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Possible rehire", warnings[0].message)


class TemplateBuilderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Department.objects.create(name="Marketing")

    def test_template_builds_without_error(self):
        body = build_onboard_xlsx_template()
        self.assertTrue(body.startswith(b"PK"))  # xlsx is a zip archive

    def test_results_workbook_builds_without_error(self):
        user = _user("jane.doe@sebpo.com", "9999", department=Department.objects.first())
        body = build_onboard_results_xlsx(
            User.objects.filter(pk=user.pk), {str(user.pk): "Sample1Pass"},
        )
        self.assertTrue(body.startswith(b"PK"))

        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.worksheets[0]
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        self.assertIn("Password", header)
        self.assertIn("NID Number", header)
        self.assertIn("Notice Period (Months)", header)
        password_col = header.index("Password") + 1
        self.assertEqual(ws.cell(row=2, column=password_col).value, "Sample1Pass")
