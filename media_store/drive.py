"""Thin Google Drive v3 client over ``requests``.

Deliberately not ``google-api-python-client``: Orbis needs six calls (about,
create folder, resumable upload, download, trash, list) and already ships
``requests``; the discovery client would add four packages and an httplib2
transport for nothing. Authenticates **as the service mailbox** with the OAuth
refresh token that ``manage.py media_drive_authorize`` wrote
(``settings.GOOGLE_DRIVE_CREDENTIALS_FILE``, google-auth "authorized_user"
format), scope ``drive.file`` — the app sees only files it created.

Spec: docs/MEDIA_GOOGLE_DRIVE_PLAN.md §2, §4.3.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import urllib.parse
from collections.abc import Iterator

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
#: Loopback redirect for the one-time Desktop-app consent flow. Nothing
#: listens there; the operator copies the ``code`` out of the address bar.
REDIRECT_URI = "http://localhost:8765/"
FOLDER_MIME = "application/vnd.google-apps.folder"
BLOB_MIME = "application/octet-stream"
#: ``appProperties`` marker every Orbis blob carries, so ``rebuild_drive_index``
#: can list exactly our files. Carries no information about the file.
MARKER_KEY, MARKER_VALUE = "orbis", "1"

FILE_FIELDS = "id,name,size,md5Checksum,trashed,description,appProperties,createdTime"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TIMEOUT = 120


class DriveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google Drive {status}: {message}")
        self.status = status
        self.message = message


class DriveNotFound(DriveError):
    pass


class DriveClient:
    """One instance per process; the access token is cached until expiry."""

    def __init__(self, credentials_file: str, root_folder_id: str, session: requests.Session | None = None):
        self.credentials_file = credentials_file
        self.root_folder_id = root_folder_id
        self._session = session or requests.Session()
        self._cred: dict | None = None
        self._access_token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_settings(cls) -> "DriveClient":
        return cls(
            credentials_file=getattr(settings, "GOOGLE_DRIVE_CREDENTIALS_FILE", ""),
            root_folder_id=getattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER_ID", ""),
        )

    # -- auth ---------------------------------------------------------------

    def _credentials(self) -> dict:
        if self._cred is None:
            if not self.credentials_file:
                raise DriveError(0, "GOOGLE_DRIVE_CREDENTIALS_FILE is not set")
            with open(self.credentials_file) as fh:
                self._cred = json.load(fh)
        return self._cred

    def _token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        cred = self._credentials()
        resp = self._session.post(TOKEN_URL, data={
            "client_id": cred["client_id"],
            "client_secret": cred["client_secret"],
            "refresh_token": cred["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=30)
        if resp.status_code != 200:
            raise DriveError(resp.status_code, f"token refresh failed: {resp.text[:300]}")
        body = resp.json()
        self._access_token = body["access_token"]
        self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, url: str, *, retries: int = 5, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", _TIMEOUT)
        delay = 1.0
        for attempt in range(retries + 1):
            headers = {**self._headers(), **kwargs.pop("headers", {})}
            resp = self._session.request(method, url, headers=headers, **kwargs)
            if resp.status_code < 400:
                return resp
            if resp.status_code == 401 and attempt == 0:
                self._access_token = None       # expired early — refresh once
                continue
            rate_limited = resp.status_code == 403 and "ateLimit" in resp.text
            if (resp.status_code in _RETRY_STATUSES or rate_limited) and attempt < retries:
                logger.warning("Drive %s %s -> %s, retry in %.0fs", method, url, resp.status_code, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code == 404:
                raise DriveNotFound(404, resp.text[:300])
            raise DriveError(resp.status_code, resp.text[:300])
        raise DriveError(0, "unreachable")  # pragma: no cover

    # -- calls --------------------------------------------------------------

    def about(self) -> dict:
        return self._request("GET", f"{API}/about", params={
            "fields": "user(emailAddress),storageQuota",
        }).json()

    def create_folder(self, name: str, parent: str | None = None) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            body["parents"] = [parent]
        return self._request("POST", f"{API}/files", params={"fields": "id"}, json=body).json()["id"]

    def upload(self, data: bytes, drive_name: str, *, description: str = "",
               app_properties: dict | None = None, parent: str | None = None) -> dict:
        """Resumable upload of one blob; returns ``{id, md5Checksum, size}``."""
        meta = {
            "name": drive_name,
            "mimeType": BLOB_MIME,
            "parents": [parent or self.root_folder_id],
            "description": description,
            "appProperties": {MARKER_KEY: MARKER_VALUE, **(app_properties or {})},
        }
        start = self._request(
            "POST", UPLOAD_API,
            params={"uploadType": "resumable", "fields": "id,md5Checksum,size"},
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": BLOB_MIME,
                "X-Upload-Content-Length": str(len(data)),
            },
            json=meta,
        )
        location = start.headers.get("Location")
        if not location:
            raise DriveError(start.status_code, "resumable upload: no session URI")
        return self._put_session(location, data)

    def _put_session(self, location: str, data: bytes, attempts: int = 6) -> dict:
        """Send the bytes to a resumable session, recovering the documented way.

        A 5xx on the PUT does not mean the bytes were lost — Drive answers
        "The operation was successful, but there was an error preparing the
        response" with a 500, and blindly re-PUTting a completed session gets
        410 (2026-10-16, production migration). So after a 5xx, ask the session
        what it has (``Content-Range: bytes */N``): 200/201 is the finished file,
        308 says how much arrived and we send the rest.
        """
        total = len(data)
        offset = 0
        delay = 1.0
        for attempt in range(attempts):
            headers = {"Content-Type": BLOB_MIME, "Content-Length": str(total - offset)}
            if offset:
                headers["Content-Range"] = f"bytes {offset}-{total - 1}/{total}"
            resp = self._session.request(
                "PUT", location, data=data[offset:], timeout=_TIMEOUT,
                headers={**self._headers(), **headers},
            )
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code not in _RETRY_STATUSES or attempt == attempts - 1:
                raise DriveError(resp.status_code, resp.text[:300])
            logger.warning("Drive upload PUT -> %s, checking session status in %.0fs", resp.status_code, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
            status = self._session.request(
                "PUT", location, timeout=_TIMEOUT,
                headers={**self._headers(), "Content-Length": "0", "Content-Range": f"bytes */{total}"},
            )
            if status.status_code in (200, 201):
                return status.json()
            if status.status_code == 308:
                received = status.headers.get("Range")          # "bytes=0-N", absent when nothing arrived
                offset = int(received.rsplit("-", 1)[1]) + 1 if received else 0
                continue
            if status.status_code in _RETRY_STATUSES:
                continue                                         # status check itself flaked; resend as-is
            raise DriveError(status.status_code, f"resumable session status: {status.text[:300]}")
        raise DriveError(0, "unreachable")  # pragma: no cover

    def download(self, file_id: str) -> bytes:
        return self._request("GET", f"{API}/files/{file_id}", params={"alt": "media"}).content

    def get(self, file_id: str, fields: str = FILE_FIELDS) -> dict:
        return self._request("GET", f"{API}/files/{file_id}", params={"fields": fields}).json()

    def trash(self, file_id: str) -> None:
        self._request("PATCH", f"{API}/files/{file_id}", json={"trashed": True})

    def list_blobs(self, parent: str | None = None, page_size: int = 1000) -> Iterator[dict]:
        """Every non-trashed Orbis blob under ``parent`` (default: the root)."""
        parent = parent or self.root_folder_id
        query = (
            f"'{parent}' in parents and trashed = false and "
            f"appProperties has {{ key='{MARKER_KEY}' and value='{MARKER_VALUE}' }}"
        )
        token = None
        while True:
            params = {"q": query, "pageSize": page_size, "fields": f"nextPageToken,files({FILE_FIELDS})"}
            if token:
                params["pageToken"] = token
            page = self._request("GET", f"{API}/files", params=params).json()
            yield from page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return


# ---------------------------------------------------------------------------
# One-time consent flow (manage.py media_drive_authorize)
# ---------------------------------------------------------------------------

def load_oauth_client(path: str) -> tuple[str, str]:
    """``(client_id, client_secret)`` from the JSON Google's console downloads
    (wrapped under ``installed`` for a Desktop app)."""
    with open(path) as fh:
        data = json.load(fh)
    client = data.get("installed") or data.get("web") or data
    return client["client_id"], client["client_secret"]


def consent_url(client_id: str) -> str:
    return f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # → refresh token
        "prompt": "consent",        # → refresh token even if previously approved
    })


def exchange_code(client_id: str, client_secret: str, code: str, out_path: str) -> None:
    """Swap the pasted authorisation code for a refresh token and write the
    credentials file (0600) in google-auth's ``authorized_user`` format."""
    resp = requests.post(TOKEN_URL, data={
        "code": urllib.parse.unquote(code.strip()),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        raise DriveError(resp.status_code, f"token exchange failed: {resp.text[:300]}")
    body = resp.json()
    if "refresh_token" not in body:
        raise DriveError(0, "no refresh_token in the response — re-run the consent step")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), mode=0o700, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({
            "type": "authorized_user",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": body["refresh_token"],
            "scopes": [SCOPE],
        }, fh, indent=2)
    os.chmod(out_path, stat.S_IRUSR | stat.S_IWUSR)
