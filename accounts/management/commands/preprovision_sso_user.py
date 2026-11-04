"""Pre-provision a CustomUser row so they can sign in via Company SSO.

The default Orbis policy is `OIDC_CREATE_USER=False` — admin pre-creates
users; the OIDC backend just links to the existing row by email. This
command is the friendly way to do that one-liner, instead of dropping
into `manage.py shell` and writing ORM code by hand.

Profile fields (name, employee_id, jobTitle, department, manager, etc.)
are left blank intentionally — they get populated automatically from
Microsoft Graph the first time the user signs in. You only need to
provide the email.

Usage:
    manage.py preprovision_sso_user alice@sebpo.com
    manage.py preprovision_sso_user alice@sebpo.com --name "Alice Rahman"
    manage.py preprovision_sso_user alice@sebpo.com --employee-id E1234
    manage.py preprovision_sso_user alice@sebpo.com --department "BD - Digital Media"
    manage.py preprovision_sso_user --from-file emails.txt   # one email per line
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from accounts.models import CustomUser, Department


class Command(BaseCommand):
    help = "Pre-create a CustomUser row by email so they can sign in via SSO."

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            nargs="?",
            help="Email / UPN of the user to provision (e.g. alice@sebpo.com).",
        )
        parser.add_argument(
            "--name",
            help='Optional display name; will be split on first space into '
                 'first/last (e.g. "Alice Rahman").',
        )
        parser.add_argument(
            "--employee-id",
            help="Optional employee_id to set up-front; "
                 "otherwise pulled from Graph on first SSO login.",
        )
        parser.add_argument(
            "--department",
            help="Optional department name to set up-front (exact match, e.g. "
                 "\"BD - Digital Media\"); otherwise pulled from Graph on first "
                 "SSO login.",
        )
        parser.add_argument(
            "--from-file",
            help="Path to a text file with one email per line (lines starting "
                 "with # are ignored). Use this for bulk onboarding.",
        )

    def handle(self, *args, **opts):
        emails: list[str] = []
        name = (opts.get("name") or "").strip()
        employee_id = (opts.get("employee_id") or "").strip()
        department_name = (opts.get("department") or "").strip()

        department = None
        if department_name:
            try:
                department = Department.objects.get(name__iexact=department_name)
            except Department.DoesNotExist:
                raise CommandError(
                    f"No department matching {department_name!r}. "
                    "Check accounts.Department for the exact name."
                )

        if opts.get("from_file"):
            path = Path(opts["from_file"])
            if not path.is_file():
                raise CommandError(f"File not found: {path}")
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    emails.append(line)
            if not emails:
                raise CommandError(f"{path} contained no emails.")
            if name or employee_id:
                self.stdout.write(self.style.WARNING(
                    "--name / --employee-id ignored when using --from-file."
                ))
                name, employee_id = "", ""
        elif opts.get("email"):
            emails = [opts["email"].strip()]
        else:
            raise CommandError(
                "Provide either an email argument or --from-file."
            )

        first_name = last_name = ""
        if name:
            first_name, _, last_name = name.partition(" ")

        created_count = existed_count = 0
        for email in emails:
            email_norm = email.lower()
            user, created = CustomUser.objects.get_or_create(
                email__iexact=email_norm,
                defaults={
                    "email": email_norm,
                    "username": email_norm[:150],
                    "first_name": first_name,
                    "last_name": last_name,
                    "employee_id": employee_id or None,
                    "department": department,
                    "is_active": True,
                },
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=["password"])
                created_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  created  {email_norm}  (pk={user.pk})"
                ))
            else:
                existed_count += 1
                self.stdout.write(
                    f"  exists   {email_norm}  (pk={user.pk})"
                )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. {created_count} created, {existed_count} already existed."
        ))
        if created_count:
            self.stdout.write(
                "Provisioned users can now sign in via 'Sign in with Company SSO'. "
                "Their profile (name, employee_id, jobTitle, department, manager, "
                "etc.) will be populated from Microsoft Graph on first login."
            )
