"""One-time cleanup: collapse JobLocation down to exactly MHK / MBD / RAOWA.

The office rows accumulated organically -- Azure AD's free-text
`officeLocation` auto-creates a new row on first SSO login (see
`auth_backends.py`), while `get_canonical_office_jobloc` separately
auto-creates short-code placeholder rows. The result was 7 active rows for
3 physical offices:

    House No - 8 Abbas Garden Rd Dhaka North 1206 BD   -> MHK   (canonical, id 4)
    MHK                                                 -> MHK  (stray, id 2)
    MBD                                                  -> MBD  (canonical, id 1)
    Monem Business District, Level-7 Dhaka 1205 BD      -> MBD  (stray, id 7)
    RAOWA                                                -> RAOWA (canonical, id 3)
    RAOWA Complex, Level-10, VIP Road Mohakhali ...      -> RAOWA (stray, id 5)
    Sheikh Hasina Software Technology Park ... Jashore   -> RAOWA (stray, id 6,
                                                             per manager confirmation --
                                                             does not bucket by address)

The canonical row per office (`accounts.locations.get_canonical_office_jobloc`)
already carries the bulk of the Roster/Shift/ShiftChangeLog history for that
office, so it is kept; the stray rows are merged into it rather than the
other way around, to avoid rewriting tens of thousands of roster/shift rows.

Dry-run by default -- pass --apply to write, --delete-strays to also remove
the now-empty stray rows (only once every FK reference has moved).

Usage::

    manage.py consolidate_job_locations
    manage.py consolidate_job_locations --apply
    manage.py consolidate_job_locations --apply --delete-strays

For one named duplicate (of any kind -- department, location, designation)
use ``manage.py merge_reference`` instead; this command is the one-time
address-pattern sweep.
"""

from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.locations import get_canonical_office_jobloc, office_bucket
from accounts.models import JobLocation, directory_stamp

# Rows that don't bucket by address text and need an explicit override.
_MANUAL_OVERRIDES = {
    "Sheikh Hasina Software Technology Park, Level-9 & 10 Nazir Songkorpur Road Jashore 7400 BD": "RAOWA",
}


class Command(BaseCommand):
    help = "Merge every JobLocation row into the canonical MHK/MBD/RAOWA row. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually write to the database (default: dry-run)")
        parser.add_argument(
            "--delete-strays", action="store_true",
            help="After merging, delete the stray rows (only if --apply and nothing references them anymore).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        targets = {code: get_canonical_office_jobloc(code) for code in ("MHK", "MBD", "RAOWA")}
        for code, jl in targets.items():
            self.stdout.write(f"Canonical {code}: id={jl.pk} {jl.name!r}")

        strays = []
        for jl in JobLocation.objects.filter(is_active=True).exclude(pk__in=[t.pk for t in targets.values()]):
            code = _MANUAL_OVERRIDES.get(jl.name) or office_bucket(jl)
            if code not in targets:
                self.stdout.write(self.style.WARNING(
                    f"Skipping id={jl.pk} {jl.name!r}: buckets to {code!r}, no target -- resolve manually."
                ))
                continue
            strays.append((jl, targets[code]))

        if not strays:
            self.stdout.write("Nothing to merge.")
            return

        # Every concrete FK onto JobLocation, discovered by reflection so we
        # don't miss one (accounts.CustomUser, roster.Roster/Shift/ShiftChangeLog x2 today).
        fk_fields = []
        for model in apps.get_models():
            for f in model._meta.get_fields():
                if getattr(f, "related_model", None) is JobLocation and getattr(f, "concrete", False):
                    fk_fields.append((model, f.name))

        self.stdout.write("\nFK fields onto JobLocation: " + ", ".join(f"{m.__name__}.{n}" for m, n in fk_fields))

        self.stdout.write("\nMerges:")
        plan = []
        for stray, target in strays:
            self.stdout.write(f"  id={stray.pk} {stray.name!r}  ->  id={target.pk} {target.name!r}")
            for model, field in fk_fields:
                count = model.objects.filter(**{f"{field}_id": stray.pk}).count()
                if count:
                    self.stdout.write(f"      {model.__name__}.{field}: {count} row(s)")
                    plan.append((model, field, stray, target, count))

        if not apply:
            self.stdout.write(self.style.WARNING("\nDry-run only -- no database changes made. Re-run with --apply to write."))
            return

        # Full addresses to preserve on the canonical rows' `description`,
        # gathered before any stray row is renamed or deleted.
        descriptions = {
            "MBD": next((s.name for s, _t in strays if "Monem Business District" in s.name), None),
            "RAOWA": next((s.name for s, _t in strays if "RAOWA Complex" in s.name), None),
        }

        with transaction.atomic():
            for model, field, stray, target, _count in plan:
                model.objects.filter(**{f"{field}_id": stray.pk}).update(
                    **{field: target}, **directory_stamp(field, model=model),
                )

            remaining = {}
            for stray, _target in strays:
                total = sum(
                    model.objects.filter(**{f"{field}_id": stray.pk}).count()
                    for model, field in fk_fields
                )
                remaining[stray.pk] = total

            if options["delete_strays"]:
                for stray, _target in strays:
                    if remaining[stray.pk] == 0:
                        self.stdout.write(f"Deleting id={stray.pk} {stray.name!r} (no remaining references).")
                        stray.delete()
                    else:
                        self.stdout.write(self.style.ERROR(
                            f"NOT deleting id={stray.pk} {stray.name!r}: "
                            f"{remaining[stray.pk]} reference(s) still remain."
                        ))
            else:
                self.stdout.write("\nPass --delete-strays to remove the now-empty rows.")
                for stray, _target in strays:
                    self.stdout.write(f"  id={stray.pk} {stray.name!r}: {remaining[stray.pk]} reference(s) remaining.")

            # Rename/relabel the canonical rows last, once any same-named
            # stray (e.g. the bare "MHK" row) is out of the way.
            mhk = targets["MHK"]
            if mhk.name != "MHK" and not (
                not options["delete_strays"] and JobLocation.objects.filter(name="MHK").exclude(pk=mhk.pk).exists()
            ):
                mhk.description = mhk.description or mhk.name
                mhk.name = "MHK"
                mhk.code = mhk.code or "MHK"
                mhk.save(update_fields=["name", "code", "description"])
            mbd = targets["MBD"]
            if not mbd.description and descriptions["MBD"]:
                mbd.description = descriptions["MBD"]
                mbd.save(update_fields=["description"])
            raowa = targets["RAOWA"]
            if not raowa.description and descriptions["RAOWA"]:
                raowa.description = descriptions["RAOWA"]
                raowa.save(update_fields=["description"])

        self.stdout.write(self.style.SUCCESS(f"\nMerged {len(strays)} stray location(s)."))
