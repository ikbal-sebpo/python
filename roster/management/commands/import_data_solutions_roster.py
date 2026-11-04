"""Backfill Data Solution & Due Diligence Shift history from the Oracle ERP export.

Part of the Oracle ERP -> Orbis transition. Data Solutions supplied
`roster-upload-template-2026-08-28.xlsx`, which is the standard Orbis wide
roster template (one row per employee, a Location/Shift column pair per
date) already filled in from their ERP extract. Covering 2026-01-01 ..
2026-07-15 for 386 active employees, it parses cleanly through the normal
uploader -- so this command reuses `roster.upload.parse_upload` verbatim
rather than reimplementing the parser.

Two things make it unsuitable for the web upload view:

1. **Volume.** ~68,500 (employee, date) cells is too much for one request.

2. **The CPL problem.** The ERP codes *public holidays* as CPL. ~6,400 of
   the file's 6,712 CPL cells land on dates where 87-100% of the whole
   department is marked CPL. Importing those verbatim would charge all 386
   employees ~17 days of compensatory leave they never took and corrupt
   the leave ledger and the shift-summary report. This command rewrites
   those cells to the HOLIDAY-category `GH` ShiftType instead.

A cell is rewritten only when its date satisfies *both* tests:

- it appears in `HOLIDAY_CALENDAR` below (SEBPO's published 2026 calendar), and
- CPL covers at least `--holiday-threshold` (default 0.5) of that day's
  filled cells -- i.e. the department was genuinely shut, not working.

Requiring both matters in each direction. 2026-02-21 is on the calendar but
149 people actually worked it, so its two stray CPL cells are real
compensatory leave and stay CPL. Conversely, a department-wide CPL date the
calendar doesn't list is left as CPL and logged to the exceptions CSV rather
than silently converted -- the calendar decides what a holiday is, not the
ERP extract.

Two dates in HOLIDAY_CALENDAR are not on the printed PDF, both confirmed
with HR:

- 2026-02-11 and 2026-02-12 -- the government declared these public
  holidays nationwide for the parliamentary election.
- 2026-03-18 and 2026-05-25 -- declared government public holidays by
  executive order, extending each Eid block a day earlier than the printed
  calendar. The roster data agrees: 334/343 and 364/364 department-wide CPL.

The `Holiday` table is seeded from the same calendar. It carried every
*fixed-date* national holiday but not one *lunar* Islamic holiday, which is
precisely the set the ERP data exposes: Shab-e-Barat, both Eid blocks and
Ashura were all missing, and 2026-03-17 was recorded under a stale name
(see HOLIDAY_RENAMES).

Conflict policy (confirmed with the user): the ERP export is authoritative
for this window, so an existing Shift row in range is overwritten and the
change is logged to the exceptions CSV. The one exception is a row tied to
an approved `LeaveApplication` (`leave_application_id` set) -- that
reflects the leave ledger and must go through the leave app's own
services, never a spreadsheet reconciliation. Overwrites keep the existing
row's job_location/work_location, since the file's Location column is
entirely blank.

Dry-run by default -- pass --apply to write.

Usage::

    manage.py import_data_solutions_roster \\
        --file "/home/smrashel/Downloads/roster-upload-template-2026-08-28.xlsx" \\
        --exceptions-csv /tmp/data_solutions_exceptions.csv

    manage.py import_data_solutions_roster --file ... \\
        --exceptions-csv /tmp/data_solutions_exceptions.csv \\
        --as hr@sebpo.com --apply
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from roster.models import Holiday, Shift, ShiftChangeLog, ShiftType
from roster.upload import _build_shift_cache, _resolve_shift_name, parse_upload

User = get_user_model()

START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 7, 15)

# The ERP's stand-in for "public holiday".
HOLIDAY_PROXY_CODE = "CPL"
# What a confirmed department-wide holiday becomes instead.
HOLIDAY_SHIFT_NAME = "GH"

# SEBPO's published "Holiday Calendar for 2026" (BD), one entry per date.
# Dates the calendar lists twice are joined with " / ". Four dates are not on
# the printed PDF -- Feb 11-12 (election) and Mar 18 / May 25 (Eid blocks
# starting a day early); see the module docstring.
HOLIDAY_CALENDAR: dict[date, str] = {
    date(2026, 2, 4): "Shab-e-Barat",
    date(2026, 2, 11): "National Parliamentary Election",
    date(2026, 2, 12): "National Parliamentary Election",
    date(2026, 2, 21): "International Mother Language Day",
    date(2026, 3, 17): "Shab-e-Qadar",
    date(2026, 3, 18): "Eid-ul-Fitr",
    date(2026, 3, 19): "Eid-ul-Fitr",
    date(2026, 3, 20): "Jum'atul Bidah / Eid-ul-Fitr",
    date(2026, 3, 21): "Eid-ul-Fitr",
    date(2026, 3, 22): "Eid-ul-Fitr",
    date(2026, 3, 23): "Eid-ul-Fitr",
    date(2026, 3, 26): "Independence Day",
    date(2026, 4, 14): "Bengali New Year (Pohela Boishakh)",
    date(2026, 5, 1): "May Day",
    date(2026, 5, 25): "Eid-ul-Azha",
    date(2026, 5, 26): "Eid-ul-Azha",
    date(2026, 5, 27): "Eid-ul-Azha",
    date(2026, 5, 28): "Eid-ul-Azha",
    date(2026, 5, 29): "Eid-ul-Azha",
    date(2026, 5, 30): "Eid-ul-Azha",
    date(2026, 5, 31): "Eid-ul-Azha",
    date(2026, 6, 26): "Muharram (Ashura)",
    date(2026, 8, 5): "July Mass Uprising Day",
    date(2026, 8, 26): "Eid e Milad un-Nabi",
    date(2026, 9, 4): "Jonmashtomi",
    date(2026, 10, 20): "Durga Puja (Nabami)",
    date(2026, 10, 21): "Durga Puja (Dashami)",
    date(2026, 12, 16): "Victory Day",
    date(2026, 12, 25): "Christmas Day",
}

# Existing Holiday rows whose name is wrong, as (expected_current, corrected).
# Renaming is not additive, so it only happens when the stored name still
# matches `expected_current` -- if someone has already fixed it by hand, this
# leaves their version alone. 2026-03-17 was recorded as Sheikh Mujib's
# Birthday / Children's Day, which is no longer a national holiday; the 2026
# calendar has Shab-e-Qadar on that date.
HOLIDAY_RENAMES: dict[date, tuple[str, str]] = {
    date(2026, 3, 17): ("Shiekh Mujib's Birthday / Children's Day", "Shab-e-Qadar"),
}


def _cpl_share(parsed_rows) -> dict[date, tuple[int, int]]:
    """Per date, (CPL cell count, total filled cell count).

    Computed from the parsed rows so it sees exactly the cells that would be
    written -- no second pass over the file.
    """
    cpl: Counter = Counter()
    total: Counter = Counter()
    for row in parsed_rows:
        total[row.date] += 1
        name = row.shift_attribute.name if row.shift_attribute else row.shift_type.name
        if name.upper() == HOLIDAY_PROXY_CODE:
            cpl[row.date] += 1
    return {d: (cpl[d], total[d]) for d in sorted(total)}


class Command(BaseCommand):
    help = (
        "Backfill Data Solution & Due Diligence Shift history "
        "(2026-01-01..2026-07-15) from the Oracle ERP roster export. "
        "Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the filled roster template .xlsx")
        parser.add_argument("--exceptions-csv", required=True, help="Path to write the exceptions/review CSV")
        parser.add_argument("--as", dest="actor_email", help="Email of the acting user (required with --apply)")
        parser.add_argument(
            "--holiday-threshold", type=float, default=0.5,
            help="CPL share of a day's cells above which a calendar date counts as a "
                 "department-wide shutdown and its CPL cells become GH (default: 0.5). "
                 "The real gap in the data is between 87%% and ~4%%, so this is not sensitive.",
        )
        parser.add_argument(
            "--keep-cpl", action="store_true",
            help="Skip the CPL->GH rewrite entirely and import CPL verbatim. This "
                 "charges every employee the ERP's holiday days as compensatory "
                 "leave -- only use if that is genuinely intended.",
        )
        parser.add_argument(
            "--skip-holiday-seed", action="store_true",
            help="Don't create or rename any Holiday rows; only write Shift rows.",
        )
        parser.add_argument("--apply", action="store_true", help="Actually write to the database (default: dry-run)")

    def handle(self, *args, **options):
        path = options["file"]
        apply_changes = options["apply"]
        exceptions_path = options["exceptions_csv"]
        threshold = options["holiday_threshold"]

        if apply_changes and not options["actor_email"]:
            raise CommandError("--as <email> is required with --apply")

        exceptions: list[dict] = []

        # --- Parse via the normal uploader ------------------------------------
        self.stdout.write(f"Parsing {os.path.basename(path)} ...")
        with open(path, "rb") as fh:
            parsed, parse_errors, dup_errors = parse_upload(
                fh, os.path.basename(path), today=timezone.localdate(),
            )
        if parse_errors or dup_errors:
            for err in (parse_errors + dup_errors)[:20]:
                self.stderr.write(f"  line {err.line_no} {err.col_letter or ''}: {err.message}")
            raise CommandError(
                f"{len(parse_errors)} parse error(s) and {len(dup_errors)} duplicate(s) "
                f"-- refusing to import a file that doesn't parse cleanly."
            )

        out_of_range = sorted({r.date for r in parsed if not (START_DATE <= r.date <= END_DATE)})
        if out_of_range:
            raise CommandError(
                f"{len(out_of_range)} date(s) fall outside {START_DATE}..{END_DATE} "
                f"(first: {out_of_range[0]}). Widen START_DATE/END_DATE deliberately "
                f"rather than importing an unexpected window."
            )

        self.stdout.write(
            f"  {len(parsed)} cells, {len({r.employee.pk for r in parsed})} employees, "
            f"{min(r.date for r in parsed)} .. {max(r.date for r in parsed)}"
        )

        # --- Which dates get their CPL rewritten ------------------------------
        shares = _cpl_share(parsed)
        shutdown_dates: dict[date, tuple[int, int]] = {}
        if options["keep_cpl"]:
            self.stdout.write(self.style.WARNING(
                "\n--keep-cpl: importing CPL verbatim, no holiday rewrite."
            ))
        else:
            self.stdout.write("\nCPL -> GH rewrite (calendar date AND department-wide CPL):")
            for d, (cpl, total) in shares.items():
                on_calendar = d in HOLIDAY_CALENDAR
                shut = bool(total) and cpl / total >= threshold
                if on_calendar and shut:
                    shutdown_dates[d] = (cpl, total)
                    self.stdout.write(
                        f"  {d} {d.strftime('%a')}  CPL {cpl}/{total} ({cpl / total:.0%})"
                        f"  -> GH   {HOLIDAY_CALENDAR[d]}"
                    )
                elif shut:
                    # Department-wide CPL on a date the calendar doesn't list.
                    # Left as CPL per HR; logged so the call stays visible.
                    self.stdout.write(self.style.WARNING(
                        f"  {d} {d.strftime('%a')}  CPL {cpl}/{total} ({cpl / total:.0%})"
                        f"  -> kept as CPL (not on the 2026 holiday calendar)"
                    ))
                    exceptions.append({
                        "employee_id": "", "date": d.isoformat(),
                        "issue": (
                            f"{cpl}/{total} ({cpl / total:.0%}) of the department is CPL on this "
                            f"date, but it is not on the 2026 holiday calendar -- kept as CPL. "
                            f"Add it to HOLIDAY_CALENDAR and re-run to convert it to GH."
                        ),
                    })
                elif on_calendar and cpl:
                    # A calendar holiday the department worked through: its
                    # stray CPL cells are real compensatory leave.
                    self.stdout.write(
                        f"  {d} {d.strftime('%a')}  CPL {cpl}/{total} ({cpl / total:.0%})"
                        f"  -> kept as CPL ({HOLIDAY_CALENDAR[d]}, but the department worked)"
                    )

        # --- Rewrite CPL -> GH on those dates ---------------------------------
        shift_cache = _build_shift_cache()
        rewritten = 0
        if shutdown_dates:
            try:
                gh_type, gh_attr = _resolve_shift_name(HOLIDAY_SHIFT_NAME, shift_cache)
            except ValueError as exc:
                raise CommandError(f"Cannot resolve '{HOLIDAY_SHIFT_NAME}' shift: {exc}")
            if gh_type.category != ShiftType.Category.HOLIDAY:
                raise CommandError(
                    f"'{HOLIDAY_SHIFT_NAME}' resolved to category {gh_type.category}, "
                    f"expected {ShiftType.Category.HOLIDAY}."
                )
            for row in parsed:
                if row.date not in shutdown_dates:
                    continue
                name = row.shift_attribute.name if row.shift_attribute else row.shift_type.name
                if name.upper() != HOLIDAY_PROXY_CODE:
                    continue
                row.shift_type, row.shift_attribute = gh_type, gh_attr
                rewritten += 1
            self.stdout.write(
                f"\n{rewritten} CPL cell(s) across {len(shutdown_dates)} date(s) "
                f"become {HOLIDAY_SHIFT_NAME}."
            )

        # --- Holiday table reconciliation -------------------------------------
        holiday_creates: list[tuple[date, str]] = []
        holiday_renames: list[tuple[date, str, str]] = []
        if not options["skip_holiday_seed"]:
            stored = {h.date: h.name for h in Holiday.objects.all()}
            for d, name in sorted(HOLIDAY_CALENDAR.items()):
                if d not in stored:
                    holiday_creates.append((d, name))
            for d, (expected, corrected) in sorted(HOLIDAY_RENAMES.items()):
                current = stored.get(d)
                if current is None:
                    continue
                if current == expected:
                    holiday_renames.append((d, current, corrected))
                elif current != corrected:
                    exceptions.append({
                        "employee_id": "", "date": d.isoformat(),
                        "issue": (
                            f"Holiday name is {current!r}, expected {expected!r} before "
                            f"renaming to {corrected!r} -- left untouched."
                        ),
                    })
            self.stdout.write(
                f"\nHoliday table: {len(holiday_creates)} to create, "
                f"{len(holiday_renames)} to rename ({len(stored)} rows now)."
            )
            for d, name in holiday_creates:
                self.stdout.write(f"  + {d} {name}")
            for d, old, new in holiday_renames:
                self.stdout.write(f"  ~ {d} {old!r} -> {new!r}")

        # --- Reconcile against what's already there ---------------------------
        emp_pks = {r.employee.pk for r in parsed}
        existing = {
            (row["employee_id"], row["date"]): row
            for row in Shift.objects.filter(
                employee_id__in=emp_pks, date__range=(START_DATE, END_DATE),
            ).values(
                "employee_id", "date", "shift_type_id", "shift_attribute_id",
                "shift_type__name", "shift_attribute__name",
                "job_location_id", "work_location_id", "leave_application_id",
            )
        }

        to_write: list[dict] = []
        skipped_ledger_locked = 0
        overwritten = 0
        unchanged = 0
        for row in parsed:
            key = (row.employee.pk, row.date)
            prev = existing.get(key)
            if prev and prev["leave_application_id"] is not None:
                skipped_ledger_locked += 1
                exceptions.append({
                    "employee_id": row.employee_id, "date": row.date.isoformat(),
                    "issue": "Existing Shift is tied to an approved LeaveApplication "
                             "-- not overwritten.",
                })
                continue
            new_value = row.shift_attribute.name if row.shift_attribute else row.shift_type.name
            if prev:
                old_ids = (prev["shift_attribute_id"], prev["shift_type_id"])
                new_ids = (
                    row.shift_attribute.id if row.shift_attribute else None,
                    row.shift_type.id,
                )
                if old_ids == new_ids:
                    # Identical to what's stored. Skip it entirely rather than
                    # re-writing the row and logging an UPDATED audit entry that
                    # records no change -- re-running this command over an
                    # already-imported window is expected (the file is delivered
                    # in instalments), and 68k empty audit rows per run would
                    # bury the real history. Same rule as roster.upload's
                    # apply_upload.
                    unchanged += 1
                    continue
                overwritten += 1
                exceptions.append({
                    "employee_id": row.employee_id, "date": row.date.isoformat(),
                    "issue": (
                        f"Overwrote existing "
                        f"'{prev['shift_attribute__name'] or prev['shift_type__name']}' "
                        f"with file value '{new_value}'."
                    ),
                })
            to_write.append({
                "employee": row.employee, "date": row.date,
                "shift_type": row.shift_type, "shift_attribute": row.shift_attribute,
                # The file's Location column is blank throughout, so an
                # overwrite keeps whatever location the existing row carried.
                "job_location_id": prev["job_location_id"] if prev else None,
                "work_location_id": prev["work_location_id"] if prev else None,
                "existed": prev is not None,
            })

        created = sum(1 for r in to_write if not r["existed"])
        by_value = Counter(
            (r["shift_attribute"].name if r["shift_attribute"] else r["shift_type"].name)
            for r in to_write
        )
        self.stdout.write(
            f"\n{len(to_write)} Shift row(s) to write: {created} created, "
            f"{overwritten} updated, {unchanged} already identical (skipped), "
            f"{skipped_ledger_locked} skipped (ledger-locked)."
        )
        self.stdout.write("By shift value: " + ", ".join(
            f"{name}={count}" for name, count in by_value.most_common()
        ))

        with open(exceptions_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["employee_id", "date", "issue"])
            writer.writeheader()
            writer.writerows(exceptions)
        self.stdout.write(f"{len(exceptions)} exception(s) written to {exceptions_path}")

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only -- no database changes made. Re-run with --apply to write."
            ))
            return

        actor = User.objects.filter(email__iexact=options["actor_email"]).first()
        if actor is None:
            raise CommandError(f"No user with email {options['actor_email']!r}")

        now = timezone.now()

        # bulk_create bypasses Shift.save(), which normally syncs shift_type
        # from the attribute's parent -- safe here because parse_upload already
        # populated shift_type from the parent at the source. It also bypasses
        # roster/signals.py, so ShiftChangeLog is built by hand, same as the
        # AdOps backfill.
        objs = [
            Shift(
                employee=r["employee"], date=r["date"],
                shift_type=r["shift_type"], shift_attribute=r["shift_attribute"],
                job_location_id=r["job_location_id"], work_location_id=r["work_location_id"],
                added_by=actor, added_at=now, updated_by=actor, updated_at=now,
            )
            for r in to_write
        ]

        bulk_kwargs: dict = {
            "update_conflicts": True,
            "update_fields": ["shift_type", "shift_attribute", "updated_by", "updated_at"],
        }
        if connection.features.supports_update_conflicts_with_target:
            bulk_kwargs["unique_fields"] = ["employee", "date"]

        with transaction.atomic():
            if holiday_creates:
                Holiday.objects.bulk_create([
                    Holiday(date=d, name=name, added_by=actor, added_at=now, updated_by=actor)
                    for d, name in holiday_creates
                ])
            for d, _old, new in holiday_renames:
                Holiday.objects.filter(date=d).update(name=new, updated_by=actor)

            Shift.objects.bulk_create(objs, batch_size=1000, **bulk_kwargs)
            ShiftChangeLog.objects.bulk_create([
                ShiftChangeLog(
                    employee=r["employee"], date=r["date"],
                    action_type=(
                        ShiftChangeLog.ActionType.UPDATED if r["existed"]
                        else ShiftChangeLog.ActionType.CREATED
                    ),
                    new_shift_type=r["shift_type"], new_shift_attribute=r["shift_attribute"],
                    changed_by=actor, changed_at=now,
                )
                for r in to_write
            ], batch_size=1000)

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(objs)} Shift row(s), created {len(holiday_creates)} Holiday row(s), "
            f"renamed {len(holiday_renames)}."
        ))
