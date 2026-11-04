"""Unit tests for employees.upload — parser, applier, template builder."""

from __future__ import annotations

import io

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings

from accounts.models import Department, JobLocation, Team, WorkLocation
from accounts.permissions import managed_scope_pks
from employees.models import JobInfo
from offboarding.models import Resignation
from roster.models import Shift, ShiftType
from employees.upload import (
    _coerce_date,
    _coerce_employee_id,
    _resolve_choice,
    _resolve_named,
    apply_upload,
    build_csv_template,
    build_xlsx_template,
    parse_upload,
)


User = get_user_model()


def _ensure_groups():
    for name in ("User Admins", "HR", "All Employees"):
        Group.objects.get_or_create(name=name)


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        employee_id=employee_id, **extra,
    )


def _csv(lines):
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class UploadHelpersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.dept = Department.objects.create(name="Engineering")
        cls.team = Team.objects.create(name="Platform", department=cls.dept)

    def test_resolve_named_case_insensitive(self):
        cache = {"platform": self.team}
        self.assertEqual(_resolve_named("PLATFORM", cache, "team"), self.team)

    def test_resolve_named_unknown(self):
        with self.assertRaises(ValueError):
            _resolve_named("Nope", {"platform": self.team}, "team")

    def test_resolve_choice_accepts_code_or_label(self):
        cache = {"bench": "BENCH", "live": "LIVE"}
        choices = [("BENCH", "Bench"), ("LIVE", "Live")]
        self.assertEqual(_resolve_choice("BENCH", cache, "resource status", choices), "BENCH")
        self.assertEqual(_resolve_choice("bench", cache, "resource status", choices), "BENCH")

    def test_resolve_choice_unknown(self):
        with self.assertRaises(ValueError):
            _resolve_choice("Nope", {"bench": "BENCH"}, "resource status", [("BENCH", "Bench")])

    def test_coerce_employee_id_numeric_and_text(self):
        self.assertEqual(_coerce_employee_id(101), "101")
        self.assertEqual(_coerce_employee_id(101.0), "101")
        self.assertEqual(_coerce_employee_id(" 101 "), "101")
        with self.assertRaises(ValueError):
            _coerce_employee_id(None)
        with self.assertRaises(ValueError):
            _coerce_employee_id(101.5)

    def test_coerce_date_accepts_common_formats(self):
        import datetime as dt

        self.assertEqual(_coerce_date("2009-05-02"), dt.date(2009, 5, 2))
        self.assertEqual(_coerce_date("15/05/2009"), dt.date(2009, 5, 15))
        self.assertEqual(_coerce_date("May 2, 2009"), dt.date(2009, 5, 2))
        self.assertEqual(_coerce_date(dt.datetime(2009, 5, 2)), dt.date(2009, 5, 2))
        self.assertEqual(_coerce_date(dt.date(2009, 5, 2)), dt.date(2009, 5, 2))

    def test_coerce_date_rejects_far_future(self):
        import datetime as dt

        future = (dt.date.today() + dt.timedelta(days=800)).isoformat()
        with self.assertRaises(ValueError):
            _coerce_date(future)

    def test_coerce_date_rejects_empty(self):
        with self.assertRaises(ValueError):
            _coerce_date("")


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class UploadParseAndApplyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.dept = Department.objects.create(name="Engineering")
        cls.other_dept = Department.objects.create(name="Finance")
        cls.team = Team.objects.create(name="Platform", department=cls.dept)
        cls.job_location = JobLocation.objects.create(name="DHK-MHK")
        cls.work_location = WorkLocation.objects.create(name="WFH")

        cls.manager = _user("manager@example.com", "100", department=cls.dept)
        cls.report = _user("report@example.com", "101", department=cls.dept)
        JobInfo.objects.create(employee=cls.report, reports_to=cls.manager)

        cls.no_jobinfo_report = _user("noji@example.com", "102", department=cls.dept)

        cls.dept_manager = _user("deptmgr@example.com", "103", department=cls.dept)
        cls.dept.additional_managers.add(cls.dept_manager)
        cls.dept_member = _user("deptmember@example.com", "104", department=cls.dept)

        cls.outsider = _user("outsider@example.com", "200", department=cls.other_dept)

        cls.hr_admin = _user("hradmin@example.com", "900", department=cls.dept)
        cls.hr_admin.groups.add(Group.objects.get(name="HR"))

    def _header(self):
        return "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status"

    def test_parse_happy_row_all_columns(self):
        f = _csv([
            self._header(),
            "101,Report T,Platform,100,BENCH,BILLABLE",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 1)
        row = parsed[0]
        self.assertEqual(row.changes["team"], self.team)
        self.assertEqual(row.changes["reports_to"], self.manager)
        self.assertEqual(row.changes["resource_status"], "BENCH")
        self.assertEqual(row.changes["billing_status"], "BILLABLE")

    def test_parse_blank_cells_mean_unchanged(self):
        f = _csv([
            self._header(),
            "101,Report T,Platform,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].changes, {"team": self.team})

    def test_parse_wholly_blank_optional_columns_skips_row(self):
        f = _csv([
            self._header(),
            "101,Report T,,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [])

    def test_parse_unknown_employee_id_error(self):
        f = _csv([
            self._header(),
            "999,Nobody,Platform,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("999", errors[0].message)

    def test_parse_duplicate_employee_id_in_file(self):
        f = _csv([
            self._header(),
            "101,Report T,Platform,,,",
            "101,Report T,Platform,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(dups), 1)
        self.assertEqual(len(parsed), 1)

    def test_parse_reports_to_self_rejected(self):
        f = _csv([
            self._header(),
            "101,Report T,,101,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot report to themselves", errors[0].message)

    def test_parse_reports_to_unknown_employee_id_error(self):
        f = _csv([
            self._header(),
            "101,Report T,,999,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("Reports To", errors[0].message)

    def test_parse_unknown_team_error(self):
        f = _csv([
            self._header(),
            "101,Report T,Nonexistent,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("team", errors[0].message.lower())

    def test_parse_unknown_status_errors(self):
        f = _csv([
            self._header(),
            "101,Report T,,,NOPE,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("resource status", errors[0].message.lower())

    def test_apply_creates_job_info_when_missing(self):
        f = _csv([
            self._header(),
            "102,No JI,,,BENCH,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        editable_pks = managed_scope_pks(self.manager, request=None) | {self.no_jobinfo_report.pk}
        report = apply_upload(self.manager, parsed, editable_pks)
        self.assertEqual(len(report.created), 1)
        ji = JobInfo.objects.get(employee=self.no_jobinfo_report)
        self.assertEqual(ji.resource_status, "BENCH")

    def test_apply_updates_existing_job_info_leaves_unset_fields_untouched(self):
        JobInfo.objects.filter(employee=self.report).update(billing_status="BILLABLE")
        f = _csv([
            self._header(),
            "101,Report T,,,BENCH,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        editable_pks = managed_scope_pks(self.manager, request=None)
        report = apply_upload(self.manager, parsed, editable_pks)
        self.assertEqual(len(report.updated), 1)
        ji = JobInfo.objects.get(employee=self.report)
        self.assertEqual(ji.resource_status, "BENCH")
        self.assertEqual(ji.billing_status, "BILLABLE")

    def test_apply_updates_customuser_team(self):
        f = _csv([
            self._header(),
            "101,Report T,Platform,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        editable_pks = managed_scope_pks(self.manager, request=None)
        apply_upload(self.manager, parsed, editable_pks)
        self.report.refresh_from_db()
        self.assertEqual(self.report.team, self.team)

    def test_apply_skips_rows_outside_visible_scope(self):
        f = _csv([
            self._header(),
            "200,Outsider,Platform,,,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        editable_pks = managed_scope_pks(self.manager, request=None)
        self.assertNotIn(self.outsider.pk, editable_pks)
        report = apply_upload(self.manager, parsed, editable_pks)
        self.assertEqual(len(report.skipped_scope), 1)
        self.assertEqual(report.total_writes, 0)

    def test_department_additional_manager_scope_includes_department_members(self):
        scope = managed_scope_pks(self.dept_manager, request=None)
        self.assertIn(self.dept_member.pk, scope)
        self.assertNotIn(self.outsider.pk, scope)

    def test_build_xlsx_template_prefills_current_values(self):
        JobInfo.objects.filter(employee=self.report).update(resource_status="LIVE")
        self.report.team = self.team
        self.report.save(update_fields=["team"])

        body = build_xlsx_template(self.manager, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertEqual(
            header[:8],
            ["Employee ID", "Employee Name", "Team", "Reports To", "Resource Status", "Billing Status",
             "Job Location", "Work Location"],
        )
        # Profile-section columns follow (no HR-only block for a manager).
        self.assertEqual(header[8], "Father's Name")
        self.assertIn("Permanent District", header)
        self.assertIn("Professional Certification", header)
        rows_by_id = {row[0].value: row for row in ws.iter_rows(min_row=2)}
        self.assertIn("101", rows_by_id)
        row = rows_by_id["101"]
        self.assertEqual(row[2].value, "Platform")
        self.assertEqual(row[3].value, "100")
        self.assertEqual(row[4].value, "Live")

    def test_build_csv_template_prefills_current_values(self):
        body = build_csv_template(self.manager, request=None)
        self.assertIn("101", body)
        self.assertIn("Report T", body)

    # --- Department / Joining Date (HR-only columns) ------------------

    def test_non_hr_viewer_cannot_include_hr_only_columns(self):
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Department,Joining Date",
            "101,Report T,,,,,Finance,2020-01-01",
        ])
        with self.assertRaises(ValueError) as ctx:
            parse_upload(f, "x.csv", viewer=self.manager, request=None)
        self.assertIn("Department", str(ctx.exception))
        self.assertIn("Joining Date", str(ctx.exception))

    def test_hr_viewer_can_parse_department_and_joining_date(self):
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Department,Joining Date",
            "101,Report T,,,,,Finance,2020-01-15",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].changes["department"], self.other_dept)
        import datetime as dt
        self.assertEqual(parsed[0].changes["joining_date"], dt.date(2020, 1, 15))

    def test_unrestricted_when_no_viewer_passed(self):
        """Back-compat: omitting `viewer` (e.g. tests exercising other columns) doesn't gate."""
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Department,Joining Date",
            "101,Report T,,,,,Finance,2020-01-15",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)

    def test_apply_updates_department_and_joining_date(self):
        import datetime as dt

        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Department,Joining Date",
            "101,Report T,,,,,Finance,2020-01-15",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        editable_pks = {self.report.pk}
        apply_upload(self.hr_admin, parsed, editable_pks)
        self.report.refresh_from_db()
        self.assertEqual(self.report.department, self.other_dept)
        self.assertEqual(self.report.joining_date, dt.date(2020, 1, 15))

    def test_build_xlsx_template_includes_hr_columns_for_hr_viewer(self):
        body = build_xlsx_template(self.hr_admin, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertIn("Department", header)
        self.assertIn("Joining Date", header)

    def test_build_xlsx_template_excludes_hr_columns_for_manager(self):
        body = build_xlsx_template(self.manager, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertNotIn("Department", header)
        self.assertNotIn("Joining Date", header)

    # --- Gender / Employee Register Serial No. (HR-only columns) ------

    def _hr_header(self):
        return (
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,"
            "Department,Joining Date,Gender,Employee Register Serial No."
        )

    def test_non_hr_viewer_cannot_include_gender_or_serial_columns(self):
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,Male,555",
        ])
        with self.assertRaises(ValueError) as ctx:
            parse_upload(f, "x.csv", viewer=self.manager, request=None)
        self.assertIn("Gender", str(ctx.exception))
        self.assertIn("Register Serial No", str(ctx.exception))

    def test_hr_viewer_can_parse_gender_and_register_serial_no(self):
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,Male,555",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].changes["gender"], "M")
        self.assertEqual(parsed[0].changes["register_serial_no"], "555")

    def test_parse_gender_accepts_code_or_label(self):
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,F,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].changes["gender"], "F")

    def test_parse_register_serial_no_duplicate_in_file_errors(self):
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,,555",
            "102,No JI,,,,,,,,555",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(len(errors), 1)
        self.assertIn("Duplicate register serial no", errors[0].message)

    def test_parse_register_serial_no_already_assigned_to_another_employee_errors(self):
        self.no_jobinfo_report.register_serial_no = "555"
        self.no_jobinfo_report.save(update_fields=["register_serial_no"])
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,,555",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(len(errors), 1)
        self.assertIn("already assigned", errors[0].message)

    def test_parse_register_serial_no_unchanged_for_same_employee_is_fine(self):
        self.report.register_serial_no = "555"
        self.report.save(update_fields=["register_serial_no"])
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,,555",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].changes["register_serial_no"], "555")

    def test_apply_updates_gender_and_register_serial_no(self):
        f = _csv([
            self._hr_header(),
            "101,Report T,,,,,,,Male,555",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        editable_pks = {self.report.pk}
        apply_upload(self.hr_admin, parsed, editable_pks)
        self.report.refresh_from_db()
        self.assertEqual(self.report.register_serial_no, "555")
        self.assertEqual(self.report.personal_info.gender, "M")

    def test_build_xlsx_template_includes_gender_and_serial_for_hr_viewer(self):
        body = build_xlsx_template(self.hr_admin, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertIn("Gender", header)
        self.assertIn("Employee Register Serial No.", header)

    def test_build_xlsx_template_excludes_gender_and_serial_for_manager(self):
        body = build_xlsx_template(self.manager, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertNotIn("Gender", header)
        self.assertNotIn("Employee Register Serial No.", header)

    # --- Probation Months / Confirmation Date (HR-only, 2026-11-03) ---

    def _confirm_upload(self, header, row, viewer=None):
        from datetime import date  # noqa: F401
        User.objects.filter(pk=self.report.pk).update(joining_date="2026-01-10")
        f = _csv([header, row])
        return parse_upload(f, "x.csv", viewer=viewer or self.hr_admin, request=None)

    def test_probation_only_works_out_the_confirmation_date(self):
        from datetime import date
        parsed, errors, dups, warnings = self._confirm_upload("Employee ID,Probation Months", "101,3")
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].changes["confirmation_date"], date(2026, 4, 10))
        self.assertTrue(parsed[0].changes["confirmation_date_calculated"])
        apply_upload(self.hr_admin, parsed, {self.report.pk})
        self.report.refresh_from_db()
        self.assertEqual((self.report.probation_month, self.report.confirmation_date), (3, date(2026, 4, 10)))

    def test_probation_uses_the_files_own_joining_date(self):
        from datetime import date
        parsed, errors, *_ = self._confirm_upload(
            "Employee ID,Joining Date,Probation Months", "101,2026-02-01,6",
        )
        self.assertEqual(parsed[0].changes["confirmation_date"], date(2026, 8, 1))

    def test_typed_confirmation_wins_and_a_mismatch_only_warns(self):
        from datetime import date
        parsed, errors, dups, warnings = self._confirm_upload(
            "Employee ID,Probation Months,Confirmation Date", "101,3,2026-05-10",
        )
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].changes["confirmation_date"], date(2026, 5, 10))
        self.assertNotIn("confirmation_date_calculated", parsed[0].changes)
        self.assertEqual(len(warnings), 1)
        self.assertIn("extended probation", warnings[0].message)

    def test_confirmation_before_joining_and_bad_probation_are_errors(self):
        _, errors, *_ = self._confirm_upload("Employee ID,Confirmation Date", "101,2025-12-31")
        self.assertIn("before the joining date", errors[0].message)
        for bad in ("2.5", "-1", "30", "three"):
            _, errors, *_ = self._confirm_upload("Employee ID,Probation Months", f"101,{bad}")
            self.assertEqual(len(errors), 1, bad)

    def test_blank_cells_leave_values_unchanged(self):
        from datetime import date
        User.objects.filter(pk=self.report.pk).update(probation_month=2, confirmation_date="2026-03-10")
        parsed, errors, *_ = self._confirm_upload(
            "Employee ID,Probation Months,Confirmation Date,Billing Status", "101,,,Billable",
        )
        apply_upload(self.hr_admin, parsed, {self.report.pk})
        self.report.refresh_from_db()
        self.assertEqual((self.report.probation_month, self.report.confirmation_date), (2, date(2026, 3, 10)))

    def test_managers_cannot_upload_probation_or_confirmation(self):
        for header in ("Employee ID,Probation Months", "Employee ID,Confirmation Date"):
            with self.assertRaises(ValueError):
                self._confirm_upload(header, "101,3", viewer=self.manager)

    def test_template_carries_current_values_for_hr_only(self):
        User.objects.filter(pk=self.report.pk).update(probation_month=3, confirmation_date="2026-04-10")
        ws = openpyxl.load_workbook(io.BytesIO(build_xlsx_template(self.hr_admin, request=None))).active
        header = [c.value for c in ws[1]]
        row = next(r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] == "101")
        self.assertEqual(row[header.index("Probation Months")], 3)
        self.assertEqual(row[header.index("Confirmation Date")], "2026-04-10")
        gender_col = openpyxl.utils.get_column_letter(1 + header.index("Gender"))
        sqrefs = [str(dv.sqref) for dv in ws.data_validations.dataValidation]
        self.assertTrue(any(ref.startswith(f"{gender_col}2:") for ref in sqrefs))

        ws = openpyxl.load_workbook(io.BytesIO(build_xlsx_template(self.manager, request=None))).active
        self.assertNotIn("Probation Months", [c.value for c in ws[1]])

    # --- Job Location / Work Location (regular columns, not HR-only) --

    def test_parse_job_location_and_work_location(self):
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Job Location,Work Location",
            "101,Report T,,,,,DHK-MHK,WFH",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].changes["job_location"], self.job_location)
        self.assertEqual(parsed[0].changes["work_location"], self.work_location)

    def test_parse_unknown_job_location_error(self):
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Job Location,Work Location",
            "101,Report T,,,,,Nowhere,",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        self.assertEqual(len(errors), 1)
        self.assertIn("job location", errors[0].message.lower())

    def test_non_hr_manager_can_include_job_and_work_location_columns(self):
        """Unlike Department/Gender, these aren't gated by can_edit_all."""
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Job Location,Work Location",
            "101,Report T,,,,,DHK-MHK,WFH",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.manager, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)

    def test_apply_updates_job_location_and_work_location(self):
        f = _csv([
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,Job Location,Work Location",
            "101,Report T,,,,,DHK-MHK,WFH",
        ])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv")
        editable_pks = managed_scope_pks(self.manager, request=None)
        apply_upload(self.manager, parsed, editable_pks)
        self.report.refresh_from_db()
        self.assertEqual(self.report.job_location, self.job_location)
        self.assertEqual(self.report.work_location, self.work_location)

    def test_build_xlsx_template_includes_job_and_work_location_for_manager(self):
        body = build_xlsx_template(self.manager, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertIn("Job Location", header)
        self.assertIn("Work Location", header)

    # --- Last Working Date (HR-only column) ----------------------------

    def _lwd_header(self):
        return (
            "Employee ID,Employee Name,Team,Reports To,Resource Status,Billing Status,"
            "Department,Joining Date,Gender,Employee Register Serial No.,Last Working Date"
        )

    def _lwd_row(self, employee_id="101", name="Report T", lwd=""):
        return ",".join([employee_id, name, "", "", "", "", "", "", "", "", lwd])

    def test_non_hr_viewer_cannot_include_last_working_date_column(self):
        f = _csv([self._lwd_header(), self._lwd_row(lwd="2026-12-31")])
        with self.assertRaises(ValueError) as ctx:
            parse_upload(f, "x.csv", viewer=self.manager, request=None)
        self.assertIn("Last Working Date", str(ctx.exception))

    def test_hr_viewer_can_parse_last_working_date(self):
        import datetime as dt

        f = _csv([self._lwd_header(), self._lwd_row(lwd="2026-12-31")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].changes["last_working_date"], dt.date(2026, 12, 31))
        self.assertEqual(warnings, [])

    def test_parse_last_working_date_blank_means_unchanged(self):
        f = _csv([self._lwd_header(), self._lwd_row(lwd="")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [])

    def test_parse_last_working_date_malformed_errors(self):
        f = _csv([self._lwd_header(), self._lwd_row(lwd="not-a-date")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(parsed, [])

    def test_apply_last_working_date_updates_user_and_purges_future_shifts(self):
        import datetime as dt

        shift_type = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        Shift.objects.create(employee=self.report, date=dt.date(2026, 12, 31), shift_type=shift_type)
        Shift.objects.create(employee=self.report, date=dt.date(2026, 11, 1), shift_type=shift_type)

        f = _csv([self._lwd_header(), self._lwd_row(lwd="2026-11-15")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        report = apply_upload(self.hr_admin, parsed, {self.report.pk}, warnings=warnings)
        self.assertEqual(report.total_writes, 1)
        self.report.refresh_from_db()
        self.assertEqual(self.report.last_working_date, dt.date(2026, 11, 15))
        self.assertFalse(Shift.objects.filter(employee=self.report, date=dt.date(2026, 12, 31)).exists())
        self.assertTrue(Shift.objects.filter(employee=self.report, date=dt.date(2026, 11, 1)).exists())

    def test_apply_last_working_date_unchanged_does_not_purge(self):
        import datetime as dt

        shift_type = ShiftType.objects.create(name="MOR", category=ShiftType.Category.WORK)
        self.report.last_working_date = dt.date(2026, 12, 31)
        self.report.save(update_fields=["last_working_date"])
        Shift.objects.create(employee=self.report, date=dt.date(2027, 1, 5), shift_type=shift_type)

        f = _csv([self._lwd_header(), self._lwd_row(lwd="2026-12-31")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        apply_upload(self.hr_admin, parsed, {self.report.pk}, warnings=warnings)
        self.assertTrue(Shift.objects.filter(employee=self.report, date=dt.date(2027, 1, 5)).exists())

    def test_parse_last_working_date_warns_on_resignation_mismatch(self):
        import datetime as dt

        Resignation.objects.create(
            employee=self.report,
            separation_type=Resignation.SeparationType.VOLUNTARY,
            status=Resignation.Status.PENDING,
            notice_start_date=dt.date(2026, 10, 1),
            last_working_date=dt.date(2026, 10, 31),
            applied_by=self.hr_admin,
        )
        f = _csv([self._lwd_header(), self._lwd_row(lwd="2026-11-15")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("resignation", warnings[0].message.lower())
        # Non-blocking — the change still applies.
        report = apply_upload(self.hr_admin, parsed, {self.report.pk}, warnings=warnings)
        self.assertEqual(len(report.warnings), 1)
        self.report.refresh_from_db()
        self.assertEqual(self.report.last_working_date, dt.date(2026, 11, 15))

    def test_parse_last_working_date_no_warning_when_matches_resignation(self):
        import datetime as dt

        Resignation.objects.create(
            employee=self.report,
            separation_type=Resignation.SeparationType.VOLUNTARY,
            status=Resignation.Status.PENDING,
            notice_start_date=dt.date(2026, 10, 1),
            last_working_date=dt.date(2026, 11, 15),
            applied_by=self.hr_admin,
        )
        f = _csv([self._lwd_header(), self._lwd_row(lwd="2026-11-15")])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr_admin, request=None)
        self.assertEqual(warnings, [])

    def test_build_xlsx_template_includes_last_working_date_for_hr_viewer(self):
        body = build_xlsx_template(self.hr_admin, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertIn("Last Working Date", header)

    def test_build_xlsx_template_excludes_last_working_date_for_manager(self):
        body = build_xlsx_template(self.manager, request=None)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertNotIn("Last Working Date", header)


class UploadProfileColumnsTests(TestCase):
    """Profile-section columns added 2026-10-05 (docs/LEGACY_60_IMPORT_PLAN.md §2)."""

    @classmethod
    def setUpTestData(cls):
        from employees.models import City, Country, Education, ProfessionalCertification, State  # noqa: F401

        _ensure_groups()
        cls.dept = Department.objects.create(name="Engineering")
        cls.hr = _user("hr@example.com", "900", department=cls.dept)
        cls.hr.groups.add(Group.objects.get(name="HR"))
        cls.emp = _user("emp@example.com", "101", department=cls.dept)
        cls.bd = Country.objects.create(name="Bangladesh", iso_code="BD")
        cls.dhaka = State.objects.create(country=cls.bd, name="Dhaka")
        cls.chattogram = State.objects.create(country=cls.bd, name="Chattogram")
        cls.anwara = City.objects.create(state=cls.chattogram, name="Anwara")
        cls.mirpur = City.objects.create(state=cls.dhaka, name="Mirpur", kind="CITY_CORPORATION_THANA")
        cls.dncc = State.objects.create(country=cls.bd, name="Dhaka North City Corperation", kind="CITY_CORPORATION")
        cls.banani = City.objects.create(state=cls.dncc, name="Banani", kind="CITY_CORPORATION_THANA")
        cls.bogura = State.objects.create(country=cls.bd, name="Bogura")
        cls.bogura_sadar = City.objects.create(state=cls.bogura, name="Bogura Sadar")
        from employees.models import UnionWard
        cls.haildhar = UnionWard.objects.create(city=cls.anwara, name="Haildhar")
        cls.ward6 = UnionWard.objects.create(city=cls.banani, name="Ward No-06", kind="WARD")

    def _run(self, header, row):
        f = _csv([header, row])
        parsed, errors, dups, warnings = parse_upload(f, "x.csv", viewer=self.hr, request=None)
        return parsed, errors, warnings

    def _apply(self, parsed, warnings=None):
        return apply_upload(self.hr, parsed, {self.emp.pk}, warnings=warnings)

    def test_personal_family_and_nid_columns(self):
        from employees.models import FamilyInfo, IdentificationNumber, PersonalInfo

        parsed, errors, warnings = self._run(
            "Employee ID,Father's Name,Mother's Name,Birthday,Blood Group,Height (inches),"
            "Identification Mark,Spouse Name,National ID no.",
            "101,Abu T,Amma T,1990-05-04,O+ (O positive),66,Scar on left hand,Begum T,1234567890",
        )
        self.assertEqual(errors, [])
        c = parsed[0].changes
        self.assertEqual(c["blood_group"], "O+")
        self.assertEqual(c["height_in_inch"], 66)
        self.assertEqual(c["nid"], "1234567890")
        self._apply(parsed)
        pi = PersonalInfo.objects.get(employee=self.emp)
        self.assertEqual((pi.father_name, pi.mother_name, pi.blood_group, pi.height_in_inch),
                         ("Abu T", "Amma T", "O+", 66))
        self.assertEqual(str(pi.date_of_birth), "1990-05-04")
        fi = FamilyInfo.objects.get(employee=self.emp)
        self.assertEqual(fi.spouse_name, "Begum T")
        self.assertEqual(fi.marital_status, FamilyInfo.MaritalStatus.MARRIED)
        self.assertEqual(
            IdentificationNumber.objects.filter(employee=self.emp, id_type="NID").count(), 1,
        )

    def test_nid_upsert_never_duplicates_and_blank_leaves_values(self):
        from employees.models import IdentificationNumber, PersonalInfo

        PersonalInfo.objects.create(employee=self.emp, father_name="Keep Me", blood_group="A+")
        IdentificationNumber.objects.create(employee=self.emp, id_type="NID", id_number="111")
        parsed, errors, _ = self._run(
            "Employee ID,Father's Name,Blood Group,NID", "101,,B-,1234567890123",
        )
        self.assertEqual(errors, [])
        self._apply(parsed)
        pi = PersonalInfo.objects.get(employee=self.emp)
        self.assertEqual((pi.father_name, pi.blood_group), ("Keep Me", "B-"))
        nids = list(IdentificationNumber.objects.filter(employee=self.emp, id_type="NID"))
        self.assertEqual(len(nids), 1)
        self.assertEqual(nids[0].id_number, "1234567890123")

    def test_blood_group_unicode_minus_and_bad_values(self):
        from employees.upload import _coerce_blood_group, _coerce_nid

        self.assertEqual(_coerce_blood_group("O\u2212 (O negative)"), "O-")
        self.assertEqual(_coerce_blood_group("ab +"), "AB+")
        with self.assertRaises(ValueError):
            _coerce_blood_group("Z+")
        with self.assertRaises(ValueError):
            _coerce_nid(1.99026975570001e16)   # Excel float — digits already lost
        with self.assertRaises(ValueError):
            _coerce_nid("12345")               # wrong length
        self.assertEqual(_coerce_nid(1234567890), "1234567890")

    def test_education_level_mapping_and_create_once(self):
        from employees.models import Education

        parsed, errors, _ = self._run(
            "Employee ID,Last Educational Qualification,Professional Certification",
            "101,Below Secondary School Certificate,PMP",
        )
        self.assertEqual(errors, [])
        self.assertEqual(parsed[0].changes["education"], {
            "degree_level": "JSC", "degree_name": "Below Secondary School Certificate",
        })
        self._apply(parsed)
        self._apply(parsed)   # re-upload must not duplicate
        self.assertEqual(Education.objects.filter(employee=self.emp).count(), 1)
        edu = Education.objects.get(employee=self.emp)
        self.assertEqual((edu.degree_level, edu.degree_name, edu.passing_year, edu.academic_institution),
                         ("JSC", "Below Secondary School Certificate", None, ""))
        self.assertEqual(self.emp.certifications.count(), 1)
        self.assertEqual(self.emp.certifications.get().certificate_name, "PMP")
        self.assertIsNone(self.emp.certifications.get().certificate_year)

    def test_unknown_education_level_is_an_error(self):
        _, errors, _ = self._run("Employee ID,Highest Education", "101,Wizard")
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown education level", errors[0].message)

    def test_address_columns_with_district_alias_and_police_station_match(self):
        from employees.models import Address

        parsed, errors, warnings = self._run(
            "Employee ID,Permanent Village/Area,Permanent Post Office,Permanent Union,"
            "Permanent Post Code,Permanent District,Permanent Police Station,"
            "Present Village/Area,Present District,Present Police Station",
            "101,Khaskhama,Malgor Bazar,Haildor,4376,Chittagong District,Anowra,"
            "544/4 Nayatola Moghbazar,Dhaka,Nowhere Thana",
        )
        self.assertEqual(errors, [])
        perm = parsed[0].changes["address_permanent"]
        self.assertEqual(perm["state"], self.chattogram)
        self.assertEqual(perm["city"], self.anwara)
        self.assertEqual(perm["post_code"], "4376")
        self.assertEqual(perm["country"], self.bd)
        pres = parsed[0].changes["address_present"]
        self.assertEqual(pres["state"], self.dhaka)
        self.assertNotIn("city", pres)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Nowhere Thana", warnings[0].message)

        self._apply(parsed)
        self._apply(parsed)   # update in place, never a second row per type
        self.assertEqual(Address.objects.filter(employee=self.emp).count(), 2)
        a = Address.objects.get(employee=self.emp, address_type="PERMANENT")
        self.assertEqual((a.village_area, a.post_office, a.union_name, a.city), ("Khaskhama", "Malgor Bazar", "Haildor", self.anwara))

    def test_unknown_district_is_an_error_and_ps_without_district_warns(self):
        _, errors, warnings = self._run(
            "Employee ID,Permanent District,Present Police Station", "101,Atlantis,Mirpur",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown district", errors[0].message)
        self.assertEqual(len(warnings), 1)
        self.assertIn("district is needed", warnings[0].message)

    def test_height_dash_is_blank_and_dob_must_be_past(self):
        import datetime as dt

        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        parsed, errors, _ = self._run("Employee ID,Height (inches),Date of Birth", f"101,-,{tomorrow}")
        self.assertEqual(len(errors), 1)
        self.assertIn("not in the past", errors[0].message)
        parsed, errors, _ = self._run("Employee ID,Height (inches),Date of Birth", "101,-,1990-01-01")
        self.assertEqual(errors, [])
        self.assertNotIn("height_in_inch", parsed[0].changes)

    def test_police_station_matches_city_corporation_thana_and_sadar_suffixes(self):
        parsed, errors, warnings = self._run(
            "Employee ID,Present District,Present Police Station,Permanent District,Permanent Police Station",
            "101,Dhaka,Banani Thana,Bogra District,Bogura Sadar Thana",
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(parsed[0].changes["address_present"]["city"], self.banani)
        self.assertEqual(parsed[0].changes["address_permanent"]["city"], self.bogura_sadar)

    def test_union_ward_fk_set_when_it_matches_within_the_city(self):
        parsed, errors, warnings = self._run(
            "Employee ID,Permanent Union,Permanent District,Permanent Police Station,"
            "Present Union,Present District,Present Police Station",
            "101,haildhar,Chattogram,Anwara,Ward 6,Dhaka,Banani",
        )
        self.assertEqual(errors, [])
        perm = parsed[0].changes["address_permanent"]
        self.assertEqual((perm["union_name"], perm["union_ward"]), ("haildhar", self.haildhar))
        self.assertEqual(parsed[0].changes["address_present"]["union_ward"], self.ward6)
        parsed, errors, _ = self._run(
            "Employee ID,Permanent Union,Permanent District,Permanent Police Station",
            "101,7 No-Shorikal,Chattogram,Anwara",
        )
        self.assertEqual(errors, [])
        self.assertNotIn("union_ward", parsed[0].changes["address_permanent"])
        self.assertEqual(parsed[0].changes["address_permanent"]["union_name"], "7 No-Shorikal")
