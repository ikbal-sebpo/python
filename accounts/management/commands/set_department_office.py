"""Set a department's `job_location` to one canonical office.

Departments onboarded during the Oracle ERP -> Orbis transition arrive with
patchy office data: Microsoft Entra populates `job_location` from Azure AD's
free-text `officeLocation` for staff who have signed in over SSO, and leaves
it NULL for everyone else. A NULL `job_location` makes the roster cell's
location badge render empty (`_LocatedRosterCell.location_badge` falls back
to the employee's contract location, and there is nothing to fall back to).

For a department that sits in a single office, this fills that gap. By
default only NULL rows are written, since an existing value that already
buckets to the target office is correct as it stands -- Azure AD's
"House No - 8 Abbas Garden Rd Dhaka North 1206 BD" and the short code "MHK"
both bucket to MHK, and rewriting one into the other changes nothing a user
can see. Pass --overwrite to normalise every member onto the same row,
which also moves anyone currently sitting in a *different* office.

`work_location` (the WFO/WFH/Hybrid mode) is deliberately untouched: it is
a different axis from which office someone sits in, and NULL is the
company-wide norm, which `location_badge` already handles.

Dry-run by default -- pass --apply to write.

Usage::

    manage.py set_department_office --department "Data solution" --office MHK
    manage.py set_department_office --department "Data solution" --office MHK --apply
"""

from __future__ import annotations

from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.locations import OFFICE_CODES, get_canonical_office_jobloc, office_bucket
from accounts.models import Department, JobLocation

User = get_user_model()


class Command(BaseCommand):
    help = "Point a department's members at one canonical office JobLocation. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--department", required=True, help="Department name (substring match, must be unique)")
        parser.add_argument("--office", required=True, choices=sorted(OFFICE_CODES), help="Target office code")
        parser.add_argument(
            "--job-location-id", type=int,
            help="Use this exact JobLocation row instead of the canonical one for "
                 "--office. Must bucket to --office.",
        )
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Also move members whose job_location is already set (default: "
                 "only fill NULLs).",
        )
        parser.add_argument(
            "--include-inactive", action="store_true",
            help="Include deactivated accounts (default: active only).",
        )
        parser.add_argument("--apply", action="store_true", help="Actually write to the database (default: dry-run)")

    def handle(self, *args, **options):
        matches = list(Department.objects.filter(name__icontains=options["department"]))
        if not matches:
            raise CommandError(f"No department matching {options['department']!r}.")
        if len(matches) > 1:
            raise CommandError(
                f"{options['department']!r} matches {len(matches)} departments: "
                f"{', '.join(d.name for d in matches)}. Be more specific."
            )
        dept = matches[0]

        if options["job_location_id"]:
            target = JobLocation.objects.filter(pk=options["job_location_id"]).first()
            if target is None:
                raise CommandError(f"No JobLocation with id {options['job_location_id']}.")
            if office_bucket(target) != options["office"]:
                raise CommandError(
                    f"JobLocation {target.pk} ({target.name!r}) buckets to "
                    f"{office_bucket(target)}, not {options['office']}."
                )
        else:
            target = get_canonical_office_jobloc(options["office"])
            if target is None:
                raise CommandError(f"Could not resolve a JobLocation for {options['office']}.")

        members = User.objects.filter(department=dept)
        if not options["include_inactive"]:
            members = members.filter(is_active=True)
        members = list(members.select_related("job_location"))

        self.stdout.write(
            f"Department: {dept.name} -- {len(members)} member(s)\n"
            f"Target JobLocation: id={target.pk} {target.name!r} "
            f"(buckets to {office_bucket(target)})"
        )
        self.stdout.write("Current job_location: " + ", ".join(
            f"{name or 'NULL'}={n}" for name, n in
            Counter((u.job_location.name if u.job_location else None) for u in members).most_common()
        ))

        to_write = []
        already = 0
        elsewhere = []
        for u in members:
            if u.job_location_id is None:
                to_write.append(u)
            elif u.job_location_id == target.pk:
                already += 1
            elif office_bucket(u.job_location) == options["office"]:
                # Different row, same office -- indistinguishable to users.
                if options["overwrite"]:
                    to_write.append(u)
                else:
                    already += 1
            else:
                elsewhere.append(u)
                if options["overwrite"]:
                    to_write.append(u)

        self.stdout.write(
            f"\n{len(to_write)} to write, {already} already in {options['office']}, "
            f"{len(elsewhere)} in a different office "
            f"({'moved' if options['overwrite'] else 'left alone'})."
        )
        for u in elsewhere[:20]:
            self.stdout.write(
                f"    {u.employee_id} {u.get_full_name()}: "
                f"{u.job_location.name!r} ({office_bucket(u.job_location)})"
            )

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only -- no database changes made. Re-run with --apply to write."
            ))
            return

        with transaction.atomic():
            now = timezone.now()
            for u in to_write:
                u.job_location = target
                u.directory_updated_at = now  # bulk_update skips save()
            User.objects.bulk_update(to_write, ["job_location", "directory_updated_at"], batch_size=500)

        self.stdout.write(self.style.SUCCESS(f"Updated {len(to_write)} member(s)."))
