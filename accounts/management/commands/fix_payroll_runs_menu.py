"""One-time menu fix: collapse the duplicate Payroll > Payrolls submenu.

``SubMenu.objects.update_or_create`` (seed_orbis.py) keys on
``(main_menu, title)``, NOT on url — the same trap that hit the
"Report" -> "Support Info" and "Upload overtime" -> "Overtime" renames
(see the comments above ``ORBIS_SUB_MENUS``'s Payroll section). The seed
source originally said "Runs"; the row was renamed "Payrolls" via Django
admin without the seed source being updated at the same time, so a later
``seed_orbis``/``--menus-only`` run created a second row, "Runs", alongside
the untouched "Payrolls" one rather than updating it in place. Confirmed
2026-09-21 that "Payrolls" is the intended label going forward — the seed
source now says "Payrolls" too — so this consolidates any environment
still carrying the stale "Runs" title (or a leftover duplicate of either)
down to a single row titled "Payrolls".

Run this once, before re-running ``seed_orbis``, on any environment that
still shows a duplicate::

    manage.py fix_payroll_runs_menu            # dry run
    manage.py fix_payroll_runs_menu --apply

Idempotent: if only one "Payrolls" row already exists, the command reports
that nothing needed doing.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from dynamic_menu.models import SubMenu

CANONICAL_TITLE = "Payrolls"
URL = "payroll_run_list"
MAIN_MENU_TITLE = "Payroll"


class Command(BaseCommand):
    help = "Collapse the duplicate Payroll > 'Runs' / 'Payrolls' submenu rows into one 'Payrolls' row."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the change (default is a dry run).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        rows = list(
            SubMenu.objects.filter(main_menu__title=MAIN_MENU_TITLE, url=URL).order_by("pk")
        )
        if len(rows) <= 1:
            if rows and rows[0].title != CANONICAL_TITLE:
                self.stdout.write(f"  {rows[0].title!r} -> {CANONICAL_TITLE!r}")
                if apply_changes:
                    rows[0].title = CANONICAL_TITLE
                    rows[0].is_active = True
                    rows[0].save(update_fields=["title", "is_active"])
                    self.stdout.write(self.style.SUCCESS("Renamed. Now re-run seed_orbis."))
                else:
                    self.stdout.write(self.style.WARNING("Dry run — re-run with --apply to write."))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"Nothing to do — exactly one '{MAIN_MENU_TITLE} > {CANONICAL_TITLE}' row exists.",
                ))
            return

        # More than one row for the same url — the duplicate this command
        # exists to fix. Keep whichever is currently active (what users are
        # actually seeing), delete the rest, and make sure the survivor is
        # titled "Payrolls" so update_or_create matches it going forward.
        active = [r for r in rows if r.is_active]
        survivor = active[0] if active else rows[0]
        doomed = [r for r in rows if r.pk != survivor.pk]

        self.stdout.write(
            f"  Keep pk={survivor.pk} {survivor.title!r} (active={survivor.is_active}) "
            f"-> rename to {CANONICAL_TITLE!r}",
        )
        for row in doomed:
            self.stdout.write(f"  Delete pk={row.pk} {row.title!r} (active={row.is_active})")

        if not apply_changes:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --apply to write."))
            return

        with transaction.atomic():
            for row in doomed:
                row.delete()
            survivor.title = CANONICAL_TITLE
            survivor.is_active = True
            survivor.save(update_fields=["title", "is_active"])

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {len(doomed)} duplicate row(s), kept pk={survivor.pk} as {CANONICAL_TITLE!r}. "
            "Now re-run seed_orbis.",
        ))
