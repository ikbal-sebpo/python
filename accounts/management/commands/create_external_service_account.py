"""Idempotently create the CustomUser row that external write-API calls authenticate as.

`ExternalTokenAuthentication` (roster/api/shift_writes.py) resolves the
shared-secret bearer token to this row so `Shift.added_by`/`updated_by` —
real FKs to CustomUser — have something valid to point at. Unlike the
TimeLy integration (read-only, synthetic principal), this account never
signs in and is deliberately kept out of the employee roster:
`employee_id=None`, `is_active=False` so it can never show up in
employee-scoped listings/filters or the `employee__employee_id__isnull=False`
guards on the read APIs.

Usage:
    manage.py create_external_service_account
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts.models import CustomUser
from roster.api.shift_writes import SERVICE_ACCOUNT_EMAIL


class Command(BaseCommand):
    help = "Create (or verify) the shared external-integrations service-account CustomUser row. Safe to re-run."

    def handle(self, *args, **opts):
        user, created = CustomUser.objects.get_or_create(
            email__iexact=SERVICE_ACCOUNT_EMAIL,
            defaults={
                "email": SERVICE_ACCOUNT_EMAIL,
                "username": SERVICE_ACCOUNT_EMAIL,
                "first_name": "External",
                "last_name": "Integrations Service Account",
                "employee_id": None,
                "is_active": False,
                "is_staff": False,
                "is_superuser": False,
            },
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(
                f"created  {SERVICE_ACCOUNT_EMAIL}  (pk={user.pk})"
            ))
        else:
            self.stdout.write(f"exists   {SERVICE_ACCOUNT_EMAIL}  (pk={user.pk})")
