"""One-time OAuth consent as the service mailbox → refresh-token credentials file.

Runs in three steps because the Desktop-app flow needs a browser (signed in
as ``srv_orbis@sebpo.com`` in production, ``srv_meals@sebpo.com`` on a dev
box) and the server has none::

    manage.py media_drive_authorize --client ~/orbis-secrets/oauth-client.json --print-url
        # open the URL in a private window, approve as the service mailbox;
        # the browser lands on http://localhost:8765/?code=... (nothing is
        # listening — expected). Copy the `code` query value.
    manage.py media_drive_authorize --client ~/orbis-secrets/oauth-client.json --code '4/0A...'
        # writes ~/orbis-secrets/drive-credentials.json (0600)
    manage.py media_drive_authorize --mkroot
        # creates the "Orbis Media" root folder AS THE APP and prints its id.
        # Under the drive.file scope a folder made by hand in the Drive UI is
        # invisible to the app, so the root must be app-created.

Then set GOOGLE_DRIVE_CREDENTIALS_FILE and GOOGLE_DRIVE_ROOT_FOLDER_ID in
.env and run ``manage.py media_drive_probe``. Spec: docs/MEDIA_GOOGLE_DRIVE_PLAN.md §5 Phase 0.
"""

from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from media_store.drive import DriveClient, DriveError, consent_url, exchange_code, load_oauth_client

DEFAULT_OUT = os.path.expanduser("~/orbis-secrets/drive-credentials.json")


class Command(BaseCommand):
    help = "One-time Google Drive OAuth consent for the media store (see module docstring)."

    def add_arguments(self, parser):
        parser.add_argument("--client", help="OAuth client JSON downloaded from the Google Cloud console")
        parser.add_argument("--print-url", action="store_true", help="Print the consent URL and exit")
        parser.add_argument("--code", help="Authorisation code copied from the redirect URL")
        parser.add_argument("--out", default=DEFAULT_OUT, help=f"Credentials file to write (default {DEFAULT_OUT})")
        parser.add_argument("--mkroot", action="store_true", help="Create the root folder as the app and print its id")
        parser.add_argument("--root-name", default="Orbis Media")

    def handle(self, *args, **options):
        if options["print_url"]:
            if not options["client"]:
                raise CommandError("--print-url needs --client")
            client_id, _ = load_oauth_client(options["client"])
            self.stdout.write(consent_url(client_id))
            return
        if options["code"]:
            if not options["client"]:
                raise CommandError("--code needs --client")
            client_id, client_secret = load_oauth_client(options["client"])
            try:
                exchange_code(client_id, client_secret, options["code"], options["out"])
            except DriveError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(
                f"wrote {options['out']} (0600). Set GOOGLE_DRIVE_CREDENTIALS_FILE={options['out']} in .env"
            ))
            return
        if options["mkroot"]:
            cred = getattr(settings, "GOOGLE_DRIVE_CREDENTIALS_FILE", "") or options["out"]
            client = DriveClient(credentials_file=cred, root_folder_id="")
            try:
                who = client.about().get("user", {}).get("emailAddress")
                folder_id = client.create_folder(options["root_name"])
            except DriveError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(
                f"created folder {options['root_name']!r} in {who}'s My Drive -> "
                f"GOOGLE_DRIVE_ROOT_FOLDER_ID={folder_id}"
            ))
            return
        raise CommandError("nothing to do: pass --print-url, --code or --mkroot")
