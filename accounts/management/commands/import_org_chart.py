"""Import employees + manager hierarchy from a CSV.

Use case: when Microsoft Graph's `User.Read.All` admin consent isn't (yet)
granted for the Orbis app registration, the auto-populated `JobInfo.reports_to`
from `$expand=manager` won't work. This command lets HR push the org chart
from a Workday / Oracle Fusion export instead — zero Azure dependency.

CSV format (headers required, columns ordered any way):

    email,manager_email,first_name,last_name,employee_id,department
    alice@sebpo.com,bob@sebpo.com,Alice,Rahman,E1001,Human Resources
    charlie@sebpo.com,bob@sebpo.com,Charlie,Khan,E1002,
    bob@sebpo.com,,Bob,Hossain,E1003,Corporate

Required columns: ``email``.
Optional columns: ``manager_email`` (if present and non-empty, sets
``JobInfo.reports_to``), ``first_name``, ``last_name``, ``employee_id``,
``department`` (matched against ``accounts.Department.name`` case-insensitively;
an unrecognized name is warned about and skipped, not fatal).

Behaviour:

- Each row's user is created if missing (same JIT pattern as SSO sign-in).
- Each manager referenced is also created if missing (stub with unusable
  password — gets enriched when they themselves first sign in via SSO).
- Optional fields are written only when the CSV column is non-empty —
  never overwrites with blanks.
- Reports against itself: if a row's manager_email == email, that row is
  skipped with a warning (no self-reporting allowed).
- Use ``--dry-run`` to preview without touching the database.

Usage::

    manage.py import_org_chart /tmp/orbis_org.csv
    manage.py import_org_chart /tmp/orbis_org.csv --dry-run
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import CustomUser, Department
from employees.models import JobInfo


REQUIRED_COLUMNS = {"email"}
OPTIONAL_COLUMNS = {"manager_email", "first_name", "last_name", "employee_id", "department"}


class Command(BaseCommand):
    help = "Import org chart (users + reports_to) from a CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to the CSV file.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate the CSV; print what would change without writing.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["csv_path"])
        if not path.is_file():
            raise CommandError(f"File not found: {path}")

        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise CommandError(f"{path} has no header row.")
            cols = {c.strip().lower() for c in reader.fieldnames}
            missing = REQUIRED_COLUMNS - cols
            if missing:
                raise CommandError(
                    f"CSV missing required column(s): {', '.join(sorted(missing))}. "
                    f"Got: {', '.join(reader.fieldnames)}"
                )
            unknown = cols - REQUIRED_COLUMNS - OPTIONAL_COLUMNS
            if unknown:
                self.stdout.write(self.style.WARNING(
                    f"Ignoring unknown column(s): {', '.join(sorted(unknown))}"
                ))
            rows = [
                {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                for row in reader
            ]

        if not rows:
            raise CommandError(f"{path} contained no data rows.")

        dry = opts["dry_run"]
        self.stdout.write(
            f"Parsed {len(rows)} row(s) from {path}"
            + (" (DRY RUN — no DB writes)" if dry else "")
        )
        self.stdout.write("")

        created_users = updated_users = linked_managers = 0
        skipped_self_report = 0

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):  # row 1 is the header
                email = row.get("email", "").lower()
                if not email:
                    self.stdout.write(self.style.WARNING(
                        f"  row {i}: missing email — skipped"
                    ))
                    continue

                manager_email = row.get("manager_email", "").lower()
                if manager_email == email:
                    self.stdout.write(self.style.WARNING(
                        f"  row {i}: {email} reports to itself — skipped"
                    ))
                    skipped_self_report += 1
                    continue

                user, was_created = self._upsert_user(
                    email=email,
                    first_name=row.get("first_name", ""),
                    last_name=row.get("last_name", ""),
                    employee_id=row.get("employee_id", ""),
                    department=row.get("department", ""),
                    dry=dry,
                    row_label=f"row {i}",
                )
                if was_created:
                    created_users += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"  row {i}: created  {email}"
                    ))
                else:
                    updated_users += 1

                if manager_email:
                    manager, manager_created = self._upsert_user(
                        email=manager_email, dry=dry,
                    )
                    if manager_created:
                        created_users += 1
                        self.stdout.write(self.style.SUCCESS(
                            f"           created stub for manager  {manager_email}"
                        ))
                    if not dry and user is not None and manager is not None:
                        ji, _ = JobInfo.objects.get_or_create(employee=user)
                        if ji.reports_to_id != manager.pk:
                            ji.reports_to = manager
                            ji.save(update_fields=["reports_to", "updated_at"])
                            linked_managers += 1
                    elif dry and email != manager_email:
                        # Just report what we'd do
                        linked_managers += 1
                        self.stdout.write(
                            f"           would link  {email} → {manager_email}"
                        )

            if dry:
                # Roll back any read-only side effects (shouldn't be any, but
                # safer to be explicit).
                transaction.set_rollback(True)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. {created_users} created · {updated_users} matched · "
            f"{linked_managers} reports_to links · {skipped_self_report} skipped"
            + (" (rolled back; nothing written)" if dry else "")
        ))

    def _resolve_department(self, name, row_label):
        if not name:
            return None
        dept = Department.objects.filter(name__iexact=name).first()
        if dept is None:
            self.stdout.write(self.style.WARNING(
                f"  {row_label}: no department matching {name!r} — left unset"
            ))
        return dept

    def _upsert_user(self, *, email, first_name="", last_name="", employee_id="",
                      department="", dry=False, row_label=""):
        """Get-or-create a user; optionally update name / employee_id / department when given.

        Returns (user_or_None_in_dry_run_create_path, was_created_bool).
        """
        dept = self._resolve_department(department, row_label)

        existing = CustomUser.objects.filter(email__iexact=email).first()
        if existing:
            updated_fields = []
            if first_name and existing.first_name != first_name:
                existing.first_name = first_name
                updated_fields.append("first_name")
            if last_name and existing.last_name != last_name:
                existing.last_name = last_name
                updated_fields.append("last_name")
            if employee_id and existing.employee_id != employee_id:
                existing.employee_id = employee_id
                updated_fields.append("employee_id")
            if dept and existing.department_id != dept.pk:
                existing.department = dept
                updated_fields.append("department")
            if updated_fields and not dry:
                existing.save(update_fields=updated_fields)
            return existing, False

        if dry:
            return None, True

        user = CustomUser.objects.create_user(
            email=email,
            username=email[:150],
            first_name=first_name or "",
            last_name=last_name or "",
        )
        if employee_id:
            user.employee_id = employee_id
        if dept:
            user.department = dept
        user.set_unusable_password()
        user.save()
        return user, True
