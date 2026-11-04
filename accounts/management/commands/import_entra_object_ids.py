"""Backfill `CustomUser.entra_object_id` from an Entra user export.

IT downloads the CSV from the Entra admin centre (Users → Download users).
Each row is matched to an Orbis user by email — `mail`, then
`userPrincipalName`, case-insensitive — never by Entra's `employeeId`, which
is the stale Oracle-ERP field. docs/DIRECTORY_API_PLAN.md §6.2.

Only an **empty** `entra_object_id` is ever filled; an existing value is left
alone (a different one is reported as a conflict). A row whose Entra
`employeeId` disagrees with Orbis's `employee_id` is reported and skipped
unless `--ignore-employee-id-mismatch` — the email match is probably right,
but a person should look first.

Dry-run by default; `--apply` writes. Safe to re-run with a newer export.

Usage:
    manage.py import_entra_object_ids exportUsers.csv
    manage.py import_entra_object_ids exportUsers.csv --report problems.csv
    manage.py import_entra_object_ids exportUsers.csv --apply
"""

from __future__ import annotations

import csv
import re
import uuid
from collections import Counter, defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import directory_stamp

User = get_user_model()

# Normalised header → our field. Entra's download uses Graph names (`id`,
# `userPrincipalName`, `mail`, `employeeId`); the display labels ("Object
# Id", "User principal name", …) are accepted too.
_HEADERS = {
    "id": "oid", "objectid": "oid",
    "userprincipalname": "upn",
    "mail": "mail", "email": "mail",
    "employeeid": "employee_id",
}

# Outcome → (label, applied?)
APPLY = "apply"
OUTCOMES = {
    APPLY: "will be set",
    "already_set": "already set (same id)",
    "conflict_existing": "conflict — Orbis already has a different id",
    "conflict_taken": "conflict — id already belongs to another Orbis user",
    "employee_id_mismatch": "skipped — Entra employeeId disagrees with Orbis",
    "duplicate": "skipped — id or person appears more than once in the file",
    "unmatched": "no Orbis user with this email",
    "bad_oid": "skipped — missing or malformed object id",
}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z]", "", (header or "").lower())


class Command(BaseCommand):
    help = "Fill empty CustomUser.entra_object_id values from an Entra user-export CSV. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry-run).")
        parser.add_argument(
            "--ignore-employee-id-mismatch", action="store_true",
            help="Apply rows even when Entra's employeeId disagrees with Orbis's employee_id.",
        )
        parser.add_argument("--report", help="Write every row that wasn't applied to this CSV.")

    def handle(self, *args, **options):
        rows = self._read(options["csv_path"])
        results = self._classify(rows, options["ignore_employee_id_mismatch"])

        counts = Counter(outcome for outcome, _row, _user in results)
        self.stdout.write(f"{len(rows)} row(s) read from {options['csv_path']}")
        for outcome, label in OUTCOMES.items():
            if counts[outcome]:
                self.stdout.write(f"  {counts[outcome]:>5}  {label}")
        for outcome, row, user in results:
            if outcome not in (APPLY, "already_set", "unmatched"):
                who = user.email if user else (row["mail"] or row["upn"])
                self.stdout.write(f"    {OUTCOMES[outcome]}: {who} ({row['oid'] or '—'})")

        if options["report"]:
            self._write_report(options["report"], results)
            self.stdout.write(f"Report written to {options['report']}")

        to_apply = [(row, user) for outcome, row, user in results if outcome == APPLY]
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only -- no database changes made. Re-run with --apply to write."
            ))
            return

        written = 0
        with transaction.atomic():
            for row, user in to_apply:
                # Re-check emptiness at write time; queryset update skips the
                # post_save handlers, as the SSO capture does.
                written += User.objects.filter(pk=user.pk, entra_object_id__isnull=True).update(
                    entra_object_id=row["oid"], **directory_stamp("entra_object_id"),
                )
        self.stdout.write(self.style.SUCCESS(f"Set {written} object id(s)."))

    # -- parsing ------------------------------------------------------------

    def _read(self, path: str) -> list[dict]:
        try:
            with open(path, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                columns = {_HEADERS[_norm(h)]: h for h in reader.fieldnames or [] if _norm(h) in _HEADERS}
                if "oid" not in columns or not ({"mail", "upn"} & set(columns)):
                    raise CommandError(
                        "The CSV needs an object id column (id / Object Id) and a mail or "
                        f"userPrincipalName column. Found: {reader.fieldnames}"
                    )
                rows = []
                for line_no, raw in enumerate(reader, start=2):
                    row = {field: (raw.get(header) or "").strip() for field, header in columns.items()}
                    rows.append({
                        "line": line_no,
                        "oid": self._oid(row.get("oid", "")),
                        "mail": row.get("mail", "").lower(),
                        "upn": row.get("upn", "").lower(),
                        "employee_id": row.get("employee_id", ""),
                    })
                return rows
        except FileNotFoundError as exc:
            raise CommandError(f"No such file: {path}") from exc

    @staticmethod
    def _oid(raw: str) -> str | None:
        try:
            return str(uuid.UUID(raw)) if raw else None
        except ValueError:
            return None

    # -- matching -------------------------------------------------------------

    def _classify(self, rows: list[dict], ignore_id_mismatch: bool) -> list[tuple]:
        by_email = {
            u.email.lower(): u
            for u in User.objects.only("pk", "email", "employee_id", "entra_object_id")
            if u.email
        }
        owners = dict(
            User.objects.exclude(entra_object_id__isnull=True).values_list("entra_object_id", "pk")
        )

        matched = [(row, by_email.get(row["mail"]) or by_email.get(row["upn"])) for row in rows]
        oid_count = Counter(row["oid"] for row in rows if row["oid"])
        user_rows = defaultdict(int)
        for row, user in matched:
            if user is not None and row["oid"]:  # a row with no usable id claims nobody
                user_rows[user.pk] += 1

        results = []
        for row, user in matched:
            if not row["oid"]:
                outcome = "bad_oid"
            elif user is None:
                outcome = "unmatched"
            elif oid_count[row["oid"]] > 1 or user_rows[user.pk] > 1:
                outcome = "duplicate"
            elif user.entra_object_id == row["oid"]:
                outcome = "already_set"
            elif user.entra_object_id:
                outcome = "conflict_existing"
            elif owners.get(row["oid"]) not in (None, user.pk):
                outcome = "conflict_taken"
            elif (
                not ignore_id_mismatch and row["employee_id"] and user.employee_id
                and row["employee_id"] != user.employee_id
            ):
                outcome = "employee_id_mismatch"
            else:
                outcome = APPLY
            results.append((outcome, row, user))
        return results

    @staticmethod
    def _write_report(path: str, results: list[tuple]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "line", "outcome", "entra_mail", "entra_upn", "entra_object_id", "entra_employee_id",
                "orbis_email", "orbis_employee_id", "orbis_object_id",
            ])
            for outcome, row, user in results:
                if outcome == APPLY:
                    continue
                writer.writerow([
                    row["line"], OUTCOMES[outcome], row["mail"], row["upn"], row["oid"] or "",
                    row["employee_id"], user.email if user else "", user.employee_id if user else "",
                    (user.entra_object_id or "") if user else "",
                ])
