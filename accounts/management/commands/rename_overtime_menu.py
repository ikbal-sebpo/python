"""One-time menu fix: point the Payroll section's overtime item at the new
overtime list instead of the uploader.

``SubMenu.objects.update_or_create`` (seed_orbis.py) keys on
``(main_menu, title)``, NOT on url — so re-running the seed with the row's
title changed from "Upload overtime" to "Overtime" creates a SECOND row
beside the untouched old one rather than renaming it. This is the same trap
the "Report" -> "Support Info" rename hit; the fix is to rename the existing
row in place FIRST, so the seed's update_or_create then matches and updates
it.

Run this once, before re-running ``seed_orbis``::

    manage.py rename_overtime_menu            # dry run
    manage.py rename_overtime_menu --apply

Idempotent: a row already renamed is left alone, and the command reports
that nothing needed doing.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from dynamic_menu.models import SubMenu

OLD_TITLE = "Upload overtime"
NEW_TITLE = "Overtime"
NEW_URL = "payroll_overtime_list"
MAIN_MENU_TITLE = "Payroll"


class Command(BaseCommand):
    help = "Rename the Payroll > 'Upload overtime' submenu to 'Overtime' and repoint its URL."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the change (default is a dry run).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        already = SubMenu.objects.filter(main_menu__title=MAIN_MENU_TITLE, title=NEW_TITLE)
        stale = SubMenu.objects.filter(main_menu__title=MAIN_MENU_TITLE, title=OLD_TITLE)

        if already.exists() and not stale.exists():
            self.stdout.write(self.style.SUCCESS(
                f"Nothing to do — '{MAIN_MENU_TITLE} > {NEW_TITLE}' already exists and no "
                f"'{OLD_TITLE}' row is left.",
            ))
            return

        if already.exists() and stale.exists():
            # Both present means a seed already ran ahead of this command and
            # created the duplicate this exists to prevent. Say so loudly
            # rather than guessing which row's group grants are the real ones.
            self.stdout.write(self.style.WARNING(
                f"Both '{OLD_TITLE}' and '{NEW_TITLE}' exist under {MAIN_MENU_TITLE}. "
                "A seed run created the duplicate; delete the stale "
                f"'{OLD_TITLE}' row by hand once you've confirmed the '{NEW_TITLE}' "
                "row carries the right allowed_groups.",
            ))
            return

        if not stale.exists():
            self.stdout.write(self.style.WARNING(
                f"No '{MAIN_MENU_TITLE} > {OLD_TITLE}' submenu found — nothing to rename.",
            ))
            return

        for row in stale:
            self.stdout.write(f"  {row.title!r} (url={row.url!r}) -> {NEW_TITLE!r} (url={NEW_URL!r})")

        if not apply_changes:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --apply to write."))
            return

        with transaction.atomic():
            updated = stale.update(title=NEW_TITLE, url=NEW_URL)
        self.stdout.write(self.style.SUCCESS(f"Renamed {updated} submenu row(s). Now re-run seed_orbis."))
