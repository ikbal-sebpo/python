"""Print a fresh Fernet key for ``PAYROLL_FIELD_KEYS``.

    PAYROLL_FIELD_KEYS=<new>,<old>   # newest first; then `rotate_payroll_keys`

The key never goes in the repo, the deploy scripts, email or chat — the
offline copy is a printed sheet held by the CTO
(docs/PAYROLL_DATA_PROTECTION_PLAN.md §3 item 5).
"""

from django.core.management.base import BaseCommand

from core.crypto import generate_key


class Command(BaseCommand):
    help = "Generate a new PAYROLL_FIELD_KEYS entry (prints it, nothing else)."

    def handle(self, *args, **options):
        self.stdout.write(generate_key())
