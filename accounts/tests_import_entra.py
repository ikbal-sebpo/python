"""`manage.py import_entra_object_ids` — docs/DIRECTORY_API_PLAN.md §6.2."""

from __future__ import annotations

import csv
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

User = get_user_model()

A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"
C = "cccccccc-0000-0000-0000-000000000003"
D = "dddddddd-0000-0000-0000-000000000004"
E = "eeeeeeee-0000-0000-0000-000000000005"


class ImportEntraObjectIdsTests(TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

        def person(email, eid, oid=None):
            return User.objects.create_user(email=email, username=email, employee_id=eid, entra_object_id=oid)

        self.ana = person("ana@sebpo.com", "1")
        self.ben = person("ben@sebpo.com", "2")
        self.cat = person("cat@sebpo.com", "3", oid=C)
        self.dan = person("dan@sebpo.com", "4")
        self.eve = person("eve@sebpo.com", "5")

    def csv(self, rows, header=("id", "userPrincipalName", "mail", "employeeId")):
        path = Path(self.dir.name) / "export.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return str(path)

    def run_command(self, *args):
        out = StringIO()
        call_command("import_entra_object_ids", *args, stdout=out)
        return out.getvalue()

    def oid(self, user):
        user.refresh_from_db()
        return user.entra_object_id

    def sample(self):
        return self.csv([
            (A, "ANA@sebpo.com", "", "1"),        # matched by UPN, case-insensitive
            (B, "x@sebpo.com", "ben@sebpo.com", "99"),  # employeeId disagrees
            (D, "cat@sebpo.com", "cat@sebpo.com", "3"),  # Orbis already has another id
            (E, "dan@sebpo.com", "dan@sebpo.com", "4"),
            (E, "eve@sebpo.com", "eve@sebpo.com", "5"),  # same id twice in the file
            ("nope", "ana@sebpo.com", "", "1"),
            (C, "ghost@sebpo.com", "ghost@sebpo.com", ""),
        ])

    def test_dry_run_writes_nothing(self):
        out = self.run_command(self.sample())
        self.assertIn("Dry-run only", out)
        self.assertIsNone(self.oid(self.ana))

    def test_apply(self):
        out = self.run_command(self.sample(), "--apply")
        self.assertIn("Set 1 object id(s).", out)
        self.assertEqual(self.oid(self.ana), A)
        self.assertIsNone(self.oid(self.ben))      # employee id mismatch
        self.assertEqual(self.oid(self.cat), C)    # never overwritten
        self.assertIsNone(self.oid(self.dan))      # duplicate id in file
        self.assertIsNone(self.oid(self.eve))

    def test_mismatch_can_be_overridden(self):
        self.run_command(self.csv([(B, "", "ben@sebpo.com", "99")]), "--apply", "--ignore-employee-id-mismatch")
        self.assertEqual(self.oid(self.ben), B)

    def test_id_owned_by_someone_else_is_a_conflict(self):
        out = self.run_command(self.csv([(C, "", "dan@sebpo.com", "4")]), "--apply")
        self.assertIn("already belongs to another Orbis user", out)
        self.assertIsNone(self.oid(self.dan))

    def test_rerun_is_a_no_op(self):
        path = self.csv([(A, "", "ana@sebpo.com", "1")])
        self.run_command(path, "--apply")
        out = self.run_command(path, "--apply")
        self.assertIn("already set", out)
        self.assertIn("Set 0 object id(s).", out)

    def test_display_label_headers_and_report(self):
        path = self.csv([(A, "ana@sebpo.com", "ana@sebpo.com", "1"), (B, "", "nobody@sebpo.com", "")],
                        header=("Object Id", "User principal name", "Mail", "Employee Id"))
        report = Path(self.dir.name) / "report.csv"
        self.run_command(path, "--report", str(report))
        lines = report.read_text().splitlines()
        self.assertEqual(len(lines), 2)  # header + the unmatched row
        self.assertIn("no Orbis user", lines[1])

    def test_missing_columns(self):
        with self.assertRaises(CommandError):
            self.run_command(self.csv([("x",)], header=("displayName",)))
