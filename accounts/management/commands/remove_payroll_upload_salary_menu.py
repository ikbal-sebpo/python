"""One-time menu fix: delete the orphaned Payroll > 'Upload salary' submenu.

``SubMenu.objects.update_or_create`` (seed_orbis.py) only ever creates or
updates rows that are still present in ``ORBIS_SUB_MENUS`` — it never
deletes ones that drop out of that table. The 2026-09-21 Upload Center
consolidation replaced the standalone "Upload salary" submenu (pointing at
``payroll_salary_upload``) with a single "Upload Center" entry (pointing at
the new ``payroll_upload_center``), but the old row itself is a completely
different (title, url) pair — nothing in the seed will ever touch it again.
Left alone, it lingers as a duplicate-ish stale link forever, the same
class of bug fixed for Payroll > Runs/Payrolls the same day.

Run this once, on any environment that still has the old row (dev now,
prod whenever this ships there)::

    manage.py remove_payroll_upload_salary_menu            # dry run
    manage.py remove_payroll_upload_salary_menu --apply

Idempotent: if the row is already gone, the command reports that nothing
needed doing.
"""

from django.core.management.base import BaseCommand

from dynamic_menu.models import SubMenu

MAIN_MENU_TITLE = "Payroll"
STALE_TITLE = "Upload salary"
STALE_URL = "payroll_salary_upload"


class Command(BaseCommand):
    help = "Delete the orphaned Payroll > 'Upload salary' submenu row left behind by the Upload Center consolidation."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the change (default is a dry run).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        stale = SubMenu.objects.filter(
            main_menu__title=MAIN_MENU_TITLE, title=STALE_TITLE, url=STALE_URL,
        )
        if not stale.exists():
            self.stdout.write(self.style.SUCCESS(
                f"Nothing to do — no '{MAIN_MENU_TITLE} > {STALE_TITLE}' row found.",
            ))
            return

        for row in stale:
            self.stdout.write(f"  Delete pk={row.pk} {row.title!r} (url={row.url!r})")

        if not apply_changes:
            self.stdout.write(self.style.WARNING("Dry run — re-run with --apply to write."))
            return

        deleted, _ = stale.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s)."))
