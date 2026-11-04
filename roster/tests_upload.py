"""Unit tests for roster.upload — parser, applier, template builders."""

from __future__ import annotations

import io
from datetime import date, datetime, time

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings

from accounts.models import Department, JobLocation, WorkLocation
from accounts.locations import LOCATION_DROPDOWN_VALUES
from employees.models import JobInfo
from roster.models import Shift, ShiftAttribute, ShiftChangeLog, ShiftType
from roster.upload import (
    apply_upload,
    build_csv_template,
    build_flat_xlsx_template,
    build_xlsx_template,
    parse_upload,
    _coerce_employee_id,
    _parse_cell_date,
    _resolve_location,
    _resolve_shift,
)


User = get_user_model()


def assert_bulk_query_budget(
    test, ctx, *, rows, models, other_budget=8, what="apply_upload", once_tables=(),
):
    """A bulk apply is a fixed handful of reads plus one INSERT per model —
    except that the backend splits a bulk_create into batches by its own
    parameter limit (SQLite, which the tests run on, needs ~5 for 350 Shift
    rows; MySQL needs 1). So INSERTs are capped per table at the backend's
    batch count, and everything else at a flat budget. A per-row regression
    blows either cap by hundreds. ``once_tables`` are tables written exactly
    once per request whatever the batch size (e.g. ``audit_accesslog``)."""
    import math
    from django.db import connection

    sqls = [q["sql"] for q in ctx.captured_queries]
    dump = "\n".join(q[:120] for q in sqls)
    inserts = [q for q in sqls if q.lstrip().upper().startswith("INSERT")]
    others = [q for q in sqls if q not in inserts]
    test.assertLessEqual(
        len(others), other_budget,
        f"{what} ran {len(others)} non-INSERT queries — bulk path regressed.\n{dump}",
    )
    for model in models:
        table = model._meta.db_table
        fields = [f for f in model._meta.concrete_fields if not f.primary_key]
        batches = math.ceil(rows / connection.ops.bulk_batch_size(fields, [None] * rows))
        count = sum(1 for q in inserts if f'INTO "{table}"' in q or f"INTO `{table}`" in q)
        test.assertLessEqual(
            count, batches,
            f"{count} INSERTs into {table} for {rows} rows (backend batches: "
            f"{batches}) — bulk path regressed.\n{dump}",
        )
    def _into(q, table):
        return f'INTO "{table}"' in q or f"INTO `{table}`" in q

    for table in once_tables:
        test.assertLessEqual(
            sum(1 for q in inserts if _into(q, table)), 1,
            f"more than one INSERT into {table}.\n{dump}",
        )
    tables = [m._meta.db_table for m in models] + list(once_tables)
    test.assertEqual(
        len(inserts), sum(1 for q in inserts for t in tables if _into(q, t)),
        f"unexpected INSERT into another table.\n{dump}",
    )


def _ensure_groups():
    for name in ("User Admins", "HR", "All Employees", "Senior Leadership", "IT", "Finance"):
        Group.objects.get_or_create(name=name)


def _user(email, employee_id, **extra):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=email.split("@")[0].title(), last_name="T",
        employee_id=employee_id, **extra,
    )


def _seed_shifts():
    eve = ShiftType.objects.create(
        name="EVE", category=ShiftType.Category.WORK,
        shift_start_time=time(14, 0), shift_end_time=time(23, 0),
    )
    eve_attr = ShiftAttribute.objects.create(
        shift_type=eve, name="EVE 1-10",
        start_time=time(13, 0), end_time=time(22, 0),
    )
    off = ShiftType.objects.create(
        name="OFF", category=ShiftType.Category.OFF,
        is_working_shift=False,
    )
    return eve, eve_attr, off


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
        cls.eve, cls.eve_attr, cls.off = _seed_shifts()

    def test_resolve_shift_attribute_wins(self):
        st, attr = _resolve_shift("EVE 1-10")
        self.assertEqual(st, self.eve)
        self.assertEqual(attr, self.eve_attr)

    def test_resolve_shift_falls_back_to_type(self):
        st, attr = _resolve_shift("off")    # case-insensitive
        self.assertEqual(st, self.off)
        self.assertIsNone(attr)

    def test_resolve_shift_type_with_attribute_must_pick_variant(self):
        # EVE has at least one attribute → can't pick the bare type.
        with self.assertRaises(ValueError):
            _resolve_shift("EVE")

    def test_resolve_shift_unknown(self):
        with self.assertRaises(ValueError):
            _resolve_shift("FAKESHIFT")

    def test_resolve_shift_empty(self):
        with self.assertRaises(ValueError):
            _resolve_shift("")

    def test_resolve_location_empty(self):
        jl, wl = _resolve_location("")
        self.assertIsNone(jl)
        self.assertIsNone(wl)

    def test_resolve_location_wfh(self):
        jl, wl = _resolve_location("wfh")
        self.assertIsNone(jl)
        self.assertIsNotNone(wl)

    def test_resolve_location_office(self):
        jl, wl = _resolve_location("MHK")
        self.assertIsNotNone(jl)
        self.assertIsNotNone(wl)

    def test_resolve_location_unknown(self):
        with self.assertRaises(ValueError):
            _resolve_location("XYZ")

    def test_coerce_employee_id_int(self):
        self.assertEqual(_coerce_employee_id(539), "539")

    def test_coerce_employee_id_float_int(self):
        self.assertEqual(_coerce_employee_id(539.0), "539")

    def test_coerce_employee_id_float_decimal_rejected(self):
        with self.assertRaises(ValueError):
            _coerce_employee_id(539.5)

    def test_coerce_employee_id_string(self):
        self.assertEqual(_coerce_employee_id("  E0042  "), "E0042")

    def test_coerce_employee_id_empty(self):
        with self.assertRaises(ValueError):
            _coerce_employee_id("   ")

    def test_parse_cell_date_iso(self):
        d = _parse_cell_date("2026-05-18", today=date(2026, 5, 13))
        self.assertEqual(d, date(2026, 5, 18))

    def test_parse_cell_date_native(self):
        d = _parse_cell_date(datetime(2026, 5, 18, 0, 0), today=date(2026, 5, 13))
        self.assertEqual(d, date(2026, 5, 18))

    def test_parse_cell_date_common_text_formats(self):
        self.assertEqual(
            _parse_cell_date("18-05-2026", today=date(2026, 5, 13)),
            date(2026, 5, 18),
        )
        self.assertEqual(
            _parse_cell_date("May 18, 2026", today=date(2026, 5, 13)),
            date(2026, 5, 18),
        )

    def test_parse_cell_date_too_far(self):
        with self.assertRaises(ValueError):
            _parse_cell_date("2099-01-01", today=date(2026, 5, 13))


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
        cls.eve, cls.eve_attr, cls.off = _seed_shifts()

        # Manager + downline.
        cls.manager = _user("mgr@example.com", "M001")
        cls.alice = _user("alice@example.com", "100")
        cls.bob = _user("bob@example.com", "101")
        cls.outsider = _user("out@example.com", "999")

        JobInfo.objects.create(employee=cls.alice, reports_to=cls.manager)
        JobInfo.objects.create(employee=cls.bob, reports_to=cls.manager)
        JobInfo.objects.create(employee=cls.outsider, reports_to=None)

    def _csv(self, lines: list[str]) -> io.BytesIO:
        return io.BytesIO("\n".join(lines).encode("utf-8"))

    def test_parse_happy_csv(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,,2026-05-19,",
            ",,Location,Shift,Location,Shift",
            "100,Alice T,MHK,EVE 1-10,MHK,EVE 1-10",
            "101,Bob T,,EVE 1-10,,OFF",
        ])
        parsed, errors, dups = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 4)
        by_emp = {(p.employee_id, p.date.isoformat()) for p in parsed}
        self.assertIn(("100", "2026-05-18"), by_emp)
        self.assertIn(("101", "2026-05-19"), by_emp)
        bob_off = next(p for p in parsed if p.employee_id == "101" and p.date == date(2026, 5, 19))
        self.assertEqual(bob_off.shift_type, self.off)
        self.assertIsNone(bob_off.shift_attribute)
        self.assertIsNone(bob_off.job_location)
        self.assertIsNone(bob_off.work_location)

    def test_parse_flat_csv_accepts_tracker_status_layout(self):
        f = self._csv([
            "Date,EmployeeID,User Name,Location,Shift Status",
            "18-05-2026,100,alice,MHK,EVE",
            "\"May 19, 2026\",101,bob,,OFF",
        ])
        parsed, errors, dups = parse_upload(
            f, "Shift_Details_Flat.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 2)
        alice_eve = next(p for p in parsed if p.employee_id == "100")
        self.assertEqual(alice_eve.date, date(2026, 5, 18))
        self.assertEqual(alice_eve.shift_type, self.eve)
        self.assertIsNone(alice_eve.shift_attribute)
        self.assertIsNotNone(alice_eve.job_location)
        self.assertIsNotNone(alice_eve.work_location)
        bob_off = next(p for p in parsed if p.employee_id == "101")
        self.assertEqual(bob_off.shift_type, self.off)
        self.assertIsNone(bob_off.job_location)
        self.assertIsNone(bob_off.work_location)

    def test_parse_flat_xlsx_accepts_tracker_current_shift_header(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Shift Details Flat"
        ws.append(["Date", "EmployeeID", "User Name", "Location", "Tracker Current Shift Status"])
        ws.append(["2026/05/18", 100, "alice", "MHK", "EVE"])
        body = io.BytesIO()
        wb.save(body)
        body.seek(0)

        parsed, errors, dups = parse_upload(
            body, "Shift_Details_Flat.xlsx", today=date(2026, 5, 13),
        )
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].employee_id, "100")
        self.assertEqual(parsed[0].shift_type, self.eve)
        self.assertIsNotNone(parsed[0].job_location)

    def test_parse_xlsx_skips_hidden_helper_sheets(self):
        # Planner workbooks keep a hidden "Shift Guide" first and many weekly
        # sheets after it; the importer must read the sheet the file was saved
        # on, not worksheets[0].
        wb = openpyxl.Workbook()
        guide = wb.active
        guide.title = "Shift Guide"
        guide.append(["Shift Guide"])
        guide.sheet_state = "hidden"
        older = wb.create_sheet("May 11-17")
        older.append(["Employee ID", "Employee Name", date(2026, 5, 11), None])
        older.append([None, None, "Location", "Shift"])
        older.append([100, "Alice T", "MHK", "OFF"])
        week = wb.create_sheet("May 18-24")
        week.append(["Employee ID", "Employee Name", date(2026, 5, 18), None])
        week.append([None, None, "Location", "Shift"])
        week.append([100, "Alice T", "MHK", "EVE 1-10"])
        wb.active = wb.worksheets.index(week)
        body = io.BytesIO()
        wb.save(body)
        body.seek(0)

        parsed, errors, dups = parse_upload(
            body, "Orbis Style_Schedule.xlsx", today=date(2026, 5, 13),
        )
        self.assertEqual(errors, [])
        self.assertEqual([(p.employee_id, p.date) for p in parsed], [("100", date(2026, 5, 18))])
        self.assertEqual(parsed[0].shift_attribute, self.eve_attr)

    def test_parse_xlsx_refuses_non_roster_active_sheet(self):
        # Saved on a helper sheet: refuse, never fall through to another
        # roster-shaped sheet the user didn't pick.
        wb = openpyxl.Workbook()
        week = wb.active
        week.title = "May 18-24"
        week.append(["Employee ID", "Employee Name", date(2026, 5, 18), None])
        week.append([None, None, "Location", "Shift"])
        week.append([100, "Alice T", "MHK", "EVE 1-10"])
        lookups = wb.create_sheet("Lookups")
        lookups.append(["Shift Value", "Category"])
        wb.active = wb.worksheets.index(lookups)
        body = io.BytesIO()
        wb.save(body)
        body.seek(0)

        with self.assertRaisesMessage(ValueError, "The sheet 'Lookups'"):
            parse_upload(body, "Orbis Style_Schedule.xlsx", today=date(2026, 5, 13))

    def test_parse_skips_empty_pairs(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,,2026-05-19,",
            ",,Location,Shift,Location,Shift",
            "100,Alice T,,,,",            # entirely blank — produces 0 rows
        ])
        parsed, errors, dups = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(parsed, [])
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])

    def test_parse_location_without_shift_errors(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,MHK,",
        ])
        parsed, errors, dups = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Location filled but Shift", errors[0].message)

    def test_parse_unknown_shift_errors(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,MHK,FAKESHIFT",
        ])
        parsed, errors, _ = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown shift", errors[0].message)

    def test_parse_inactive_shift_attribute_errors(self):
        self.eve_attr.is_active = False
        self.eve_attr.save(update_fields=["is_active"])
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,MHK,EVE 1-10",
        ])
        parsed, errors, _ = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown shift", errors[0].message)

    def test_parse_unknown_location_errors(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,XYZ,EVE 1-10",
        ])
        parsed, errors, _ = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown location", errors[0].message)

    def test_parse_bad_employee_id_errors(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "999999,Bad Person,MHK,EVE 1-10",
        ])
        parsed, errors, _ = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(parsed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("No active employee", errors[0].message)

    def test_parse_duplicate_in_file(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,MHK,EVE 1-10",
            "100,Alice T (again),MHK,EVE 1-10",
        ])
        parsed, errors, dups = parse_upload(
            f, "roster.csv", today=date(2026, 5, 13),
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(errors, [])
        self.assertEqual(len(dups), 1)
        self.assertIn("Duplicate", dups[0].message)

    def test_parse_bad_subheader_aborts(self):
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Wrong,Shift",
            "100,Alice T,MHK,EVE 1-10",
        ])
        with self.assertRaises(ValueError):
            parse_upload(f, "roster.csv", today=date(2026, 5, 13))

    def test_apply_create_then_update(self):
        from accounts.permissions import visible_employee_pks

        # First pass: create 2 rows.
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,,2026-05-19,",
            ",,Location,Shift,Location,Shift",
            "100,Alice T,MHK,EVE 1-10,MHK,EVE 1-10",
        ])
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))
        visible_pks = visible_employee_pks(self.manager)
        report = apply_upload(self.manager, parsed, visible_pks)
        self.assertEqual(len(report.created), 2)
        self.assertEqual(len(report.updated), 0)
        self.assertEqual(len(report.skipped_scope), 0)
        self.assertEqual(Shift.objects.filter(employee=self.alice).count(), 2)

        # Second pass: same (employee, date), different shift → updates.
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,,OFF",
        ])
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))
        report2 = apply_upload(self.manager, parsed, visible_pks)
        self.assertEqual(len(report2.created), 0)
        self.assertEqual(len(report2.updated), 1)
        s = Shift.objects.get(employee=self.alice, date=date(2026, 5, 18))
        self.assertEqual(s.shift_type, self.off)
        self.assertIsNone(s.shift_attribute)
        self.assertIsNone(s.job_location)
        self.assertIsNone(s.work_location)
        self.assertEqual(s.updated_by, self.manager)

    def test_apply_outside_scope_is_skipped(self):
        from accounts.permissions import visible_employee_pks

        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,MHK,EVE 1-10",          # in scope
            "999,Outsider,MHK,EVE 1-10",          # NOT in scope
        ])
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))
        visible_pks = visible_employee_pks(self.manager)
        report = apply_upload(self.manager, parsed, visible_pks)
        self.assertEqual(len(report.created), 1)
        self.assertEqual(len(report.skipped_scope), 1)
        self.assertEqual(report.skipped_scope[0].raw["Employee ID"], "999")
        # Only the alice row should have landed.
        self.assertFalse(Shift.objects.filter(employee=self.outsider).exists())

    def test_apply_added_by_set_on_create(self):
        from accounts.permissions import visible_employee_pks
        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,",
            ",,Location,Shift",
            "100,Alice T,MHK,EVE 1-10",
        ])
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))
        apply_upload(self.manager, parsed, visible_employee_pks(self.manager))
        s = Shift.objects.get(employee=self.alice, date=date(2026, 5, 18))
        self.assertEqual(s.added_by, self.manager)
        self.assertEqual(s.updated_by, self.manager)
        log = ShiftChangeLog.objects.get(shift=s, action_type=ShiftChangeLog.ActionType.CREATED)
        self.assertEqual(log.changed_by_id, self.manager.pk)

    def test_apply_does_not_overwrite_approved_leave(self):
        """An approved-leave Shift wins over a later roster upload: the upload
        row is reported under skipped_leave and the leave Shift is untouched."""
        from accounts.permissions import visible_employee_pks

        cl = ShiftType.objects.create(name="CL", category=ShiftType.Category.LEAVE)
        Shift.objects.create(
            employee=self.alice, date=date(2026, 5, 18),
            shift_type=cl, note="Leave #41",
        )

        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,,2026-05-19,",
            ",,Location,Shift,Location,Shift",
            "100,Alice T,MHK,EVE 1-10,MHK,EVE 1-10",
        ])
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))
        report = apply_upload(self.manager, parsed, visible_employee_pks(self.manager))

        # The leave day is protected; the other day applies normally.
        self.assertEqual(len(report.skipped_leave), 1)
        self.assertEqual(report.skipped_leave[0].raw["Date"], "2026-05-18")
        self.assertEqual(len(report.created), 1)  # only 2026-05-19
        leave_shift = Shift.objects.get(employee=self.alice, date=date(2026, 5, 18))
        self.assertEqual(leave_shift.shift_type, cl)
        self.assertEqual(leave_shift.note, "Leave #41")

    def test_apply_override_leave_forces_the_write(self):
        """override_leave=True writes over an approved-leave Shift and lists
        the row under overridden_leave instead of skipped_leave."""
        from accounts.permissions import visible_employee_pks

        cl = ShiftType.objects.create(name="CL", category=ShiftType.Category.LEAVE)
        Shift.objects.create(
            employee=self.alice, date=date(2026, 5, 18),
            shift_type=cl, note="Leave #41",
        )

        f = self._csv([
            "Employee ID,Employee Name,2026-05-18,,2026-05-19,",
            ",,Location,Shift,Location,Shift",
            "100,Alice T,MHK,EVE 1-10,MHK,EVE 1-10",
        ])
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))
        report = apply_upload(
            self.manager, parsed, visible_employee_pks(self.manager),
            override_leave=True,
        )

        self.assertEqual(report.skipped_leave, [])
        self.assertEqual(len(report.overridden_leave), 1)
        self.assertEqual(report.overridden_leave[0].date, date(2026, 5, 18))
        self.assertEqual(len(report.updated), 1)   # the overridden day
        self.assertEqual(len(report.created), 1)   # the other day
        shift = Shift.objects.get(employee=self.alice, date=date(2026, 5, 18))
        self.assertEqual(shift.shift_type.name, "EVE")
        self.assertEqual(shift.shift_attribute.name, "EVE 1-10")


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class UploadScalePerfTests(TestCase):
    """Query-count budget for the 700-employee × 7-day department-head case.

    Asserts a hard upper bound so a future refactor can't silently regress
    the cached-lookup path back into per-cell DB hits.
    """

    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.eve, cls.eve_attr, cls.off = _seed_shifts()
        cls.manager = _user("mgr@example.com", "M001")
        # 50 employees is enough to surface a per-row/per-cell N+1 — the
        # cached path stays at constant query count regardless of N.
        cls.employees = []
        for i in range(50):
            u = _user(f"emp{i}@example.com", f"E{i:03d}")
            JobInfo.objects.create(employee=u, reports_to=cls.manager)
            cls.employees.append(u)

    def _wide_csv(self, employees, num_days: int) -> io.BytesIO:
        base = date(2026, 5, 18)
        date_headers = [(base + __import__("datetime").timedelta(days=i)).isoformat()
                        for i in range(num_days)]
        line1 = ["Employee ID", "Employee Name"]
        for d in date_headers:
            line1.extend([d, ""])
        line2 = ["", ""]
        for _ in date_headers:
            line2.extend(["Location", "Shift"])
        lines = [",".join(line1), ",".join(line2)]
        for e in employees:
            row = [e.employee_id, e.display_name]
            for _ in date_headers:
                row.extend(["MHK", "EVE 1-10"])
            lines.append(",".join(row))
        return io.BytesIO("\n".join(lines).encode("utf-8"))

    def test_parse_query_budget_is_constant(self):
        """Parsing 50 employees × 7 days = 350 cells must use a fixed number
        of queries (the cache build), not per-cell queries.
        """
        f = self._wide_csv(self.employees, num_days=7)
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        with CaptureQueriesContext(connection) as ctx:
            parsed, errors, dups = parse_upload(
                f, "roster.csv", today=date(2026, 5, 13),
            )

        self.assertEqual(len(parsed), 350)
        self.assertEqual(errors, [])
        self.assertEqual(dups, [])
        # Budget: 4 query for holidays/employees/shift-attrs/shift-types +
        # ~5-30 for the location-cache build (each token resolves to an
        # existing canonical row in 1 query on prod; cold test DB hits
        # get_or_create paths that add a few inserts). Whatever the absolute
        # number, it must be CONSTANT, not scale with the 350 cells.
        # 60 is a comfortable ceiling that still catches a per-cell regression
        # (which would put us in the hundreds at minimum).
        self.assertLess(
            len(ctx.captured_queries), 60,
            f"parse_upload used {len(ctx.captured_queries)} queries for 350 cells — "
            f"per-cell lookup regressed.\n" +
            "\n".join(q["sql"][:120] for q in ctx.captured_queries)
        )

    def test_apply_query_budget_is_constant(self):
        """Applying 350 rows must use one bulk INSERT-ON-CONFLICT, not 350
        update_or_create calls.
        """
        from accounts.permissions import visible_employee_pks
        f = self._wide_csv(self.employees, num_days=7)
        parsed, _, _ = parse_upload(f, "roster.csv", today=date(2026, 5, 13))

        visible_pks = visible_employee_pks(self.manager)
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            report = apply_upload(self.manager, parsed, visible_pks)

        self.assertEqual(len(report.created), 350)
        self.assertEqual(Shift.objects.count(), 350)
        assert_bulk_query_budget(self, ctx, rows=350, models=(Shift, ShiftChangeLog))

    def test_apply_overwrite_path_is_also_bulk(self):
        """A second apply over the same rows (pure overwrite path) keeps the
        same query budget — no per-row update fan-out.
        """
        from accounts.permissions import visible_employee_pks
        visible_pks = visible_employee_pks(self.manager)

        # First apply: create.
        f1 = self._wide_csv(self.employees, num_days=7)
        parsed1, _, _ = parse_upload(f1, "roster.csv", today=date(2026, 5, 13))
        apply_upload(self.manager, parsed1, visible_pks)
        self.assertEqual(Shift.objects.count(), 350)

        # Second apply: overwrite.
        f2 = self._wide_csv(self.employees, num_days=7)
        parsed2, _, _ = parse_upload(f2, "roster.csv", today=date(2026, 5, 13))
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            report = apply_upload(self.manager, parsed2, visible_pks)

        self.assertEqual(len(report.created), 0)
        self.assertEqual(len(report.updated), 350)
        self.assertEqual(Shift.objects.count(), 350)
        assert_bulk_query_budget(self, ctx, rows=350, models=(Shift, ShiftChangeLog))


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class UploadTemplateBuilderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_groups()
        cls.eve, cls.eve_attr, cls.off = _seed_shifts()
        cls.manager = _user("mgr@example.com", "M001")
        cls.alice = _user("alice@example.com", "100")
        JobInfo.objects.create(employee=cls.alice, reports_to=cls.manager)

    def test_csv_template_has_two_header_rows(self):
        body = build_csv_template(self.manager)
        lines = body.splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("Employee ID,Employee Name,"))
        self.assertIn("Location", lines[1])
        self.assertIn("Shift", lines[1])
        # Includes Alice as a downline row.
        self.assertTrue(any("100,Alice" in ln or ln.startswith("100,") for ln in lines))

    def test_xlsx_template_has_dropdowns(self):
        body = build_xlsx_template(self.manager)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        self.assertIn("Roster", wb.sheetnames)
        self.assertIn("Lookups", wb.sheetnames)
        self.assertIn("Shift Guide", wb.sheetnames)
        ws = wb["Roster"]
        # Row 1 cols: Employee ID | Employee Name | <date> | (empty) | ...
        self.assertEqual(ws.cell(row=1, column=1).value, "Employee ID")
        self.assertEqual(ws.cell(row=1, column=2).value, "Employee Name")
        # Row 2: Location / Shift sub-headers on the date pairs (cols 3+).
        self.assertEqual(ws.cell(row=2, column=3).value, "Location")
        self.assertEqual(ws.cell(row=2, column=4).value, "Shift")
        # Alice prefilled on row 3.
        self.assertEqual(ws.cell(row=3, column=1).value, "100")
        # Lookups sheet hidden.
        self.assertEqual(wb["Lookups"].sheet_state, "hidden")
        # Lookups B column = location tokens.
        loc_col = [
            wb["Lookups"].cell(row=i, column=2).value
            for i in range(2, 2 + len(LOCATION_DROPDOWN_VALUES))
        ]
        self.assertEqual(set(loc_col), set(LOCATION_DROPDOWN_VALUES))
        # At least one data validation attached.
        self.assertGreaterEqual(len(ws.data_validations.dataValidation), 1)
        guide = wb["Shift Guide"]
        self.assertEqual(guide.cell(row=4, column=1).value, "Shift Value")
        self.assertEqual(guide.cell(row=4, column=2).value, "Category")
        self.assertEqual(guide.cell(row=4, column=3).value, "Parent Type")
        guide_rows = [
            (guide.cell(row=i, column=1).value, guide.cell(row=i, column=3).value or "")
            for i in range(5, guide.max_row + 1)
        ]
        self.assertIn(("EVE", ""), guide_rows)
        self.assertIn(("OFF", ""), guide_rows)
        self.assertIn(("EVE 1-10", "EVE"), guide_rows)
        descriptions = [
            guide.cell(row=i, column=5).value
            for i in range(5, guide.max_row + 1)
        ]
        self.assertTrue(all(descriptions))

    def test_xlsx_template_excludes_inactive_shift_attributes(self):
        self.eve_attr.is_active = False
        self.eve_attr.save(update_fields=["is_active"])
        body = build_xlsx_template(self.manager)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        lookup_values = [
            wb["Lookups"].cell(row=i, column=1).value
            for i in range(2, wb["Lookups"].max_row + 1)
        ]
        self.assertNotIn("EVE 1-10", lookup_values)

    def test_flat_xlsx_template_has_tracker_columns(self):
        body = build_flat_xlsx_template(self.manager)
        wb = openpyxl.load_workbook(io.BytesIO(body))
        self.assertIn("Shift Details Flat", wb.sheetnames)
        self.assertIn("Lookups", wb.sheetnames)
        self.assertIn("Shift Guide", wb.sheetnames)
        ws = wb["Shift Details Flat"]
        headers = [ws.cell(row=1, column=i).value for i in range(1, 6)]
        self.assertEqual(headers, ["Date", "EmployeeID", "User Name", "Location", "Shift Status"])
        self.assertEqual(ws.cell(row=2, column=2).value, "100")
        self.assertIn("Alice", ws.cell(row=2, column=3).value)
        self.assertEqual(ws.cell(row=2, column=1).number_format, "yyyy-mm-dd")
        self.assertEqual(wb["Lookups"].sheet_state, "hidden")
        self.assertGreaterEqual(len(ws.data_validations.dataValidation), 2)
        self.assertEqual(wb["Shift Guide"].cell(row=4, column=1).value, "Shift Value")
