"""``GoogleDriveStorage`` — Django's default file storage on an untrusted blob store.

Every byte is Fernet-encrypted before it leaves the VPS (``media_store.crypto``)
and lands on Drive under a random ``<32 hex>.bin`` with an encrypted
description; the real name/MIME/size exist only in ``DriveObject``. Reads come
from the bounded ciphertext cache (``media_store.cache``) and are decrypted in
memory, so plaintext exists only inside the Django/Celery process that passed
authorisation. ``url()`` points at the permission-checked serve view, never
at Drive.

Selected by ``MEDIA_BACKEND=drive`` (``core/settings.py``); dev and tests keep
``FileSystemStorage`` unless they opt in. Spec: docs/MEDIA_GOOGLE_DRIVE_PLAN.md §4.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile, File
from django.core.files.storage import Storage
from django.urls import reverse
from django.utils.deconstruct import deconstructible
from django.utils.module_loading import import_string

from media_store import crypto
from media_store.cache import MediaCache
from media_store.drive import DriveClient, DriveError, DriveNotFound
from media_store.models import DriveObject

logger = logging.getLogger(__name__)


class MediaTooLarge(SuspiciousFileOperation):
    pass


@deconstructible
class GoogleDriveStorage(Storage):
    def __init__(self, client=None, cache_root=None, max_cache_bytes=None, max_upload_bytes=None):
        # ``client`` may be an instance or a dotted path (STORAGES OPTIONS are
        # strings) — the tests inject ``media_store.tests.FakeDriveClient``.
        self._client = client
        self._cache: MediaCache | None = None
        self._cache_root = cache_root
        self._max_cache_bytes = max_cache_bytes
        self._max_upload_bytes = max_upload_bytes

    # -- lazy collaborators -------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            self._client = DriveClient.from_settings()
        elif isinstance(self._client, str):
            self._client = import_string(self._client)()
        return self._client

    @property
    def cache(self) -> MediaCache:
        if self._cache is None:
            self._cache = MediaCache(
                self._cache_root or settings.MEDIA_CACHE_ROOT,
                self._max_cache_bytes or settings.MEDIA_CACHE_MAX_BYTES,
            )
        return self._cache

    @property
    def max_upload_bytes(self) -> int:
        return int(self._max_upload_bytes or settings.MEDIA_MAX_UPLOAD_BYTES)

    # -- index --------------------------------------------------------------

    @staticmethod
    def _row(name: str) -> DriveObject | None:
        return DriveObject.objects.filter(name=name).first()

    def exists(self, name: str) -> bool:
        return DriveObject.objects.filter(name=name).exists()

    def size(self, name: str) -> int:
        row = self._row(name)
        if row is None:
            raise FileNotFoundError(name)
        return row.size

    def url(self, name: str) -> str:
        return reverse("media_serve", kwargs={"name": name})

    def get_created_time(self, name: str):
        row = self._row(name)
        if row is None:
            raise FileNotFoundError(name)
        return row.created_at

    get_modified_time = get_created_time

    def listdir(self, path):
        raise NotImplementedError("Drive media has no directory listing; query DriveObject instead.")

    # -- bytes --------------------------------------------------------------

    def _ciphertext(self, row: DriveObject) -> bytes:
        data = self.cache.get(row.drive_name)
        if data is None:
            data = self.client.download(row.file_id)
            self.cache.put(row.drive_name, data)
        return data

    def _open(self, name: str, mode: str = "rb") -> File:
        if "w" in mode or "a" in mode or "+" in mode:
            raise NotImplementedError("Drive media is write-once; save a new file instead.")
        row = self._row(name)
        if row is None:
            raise FileNotFoundError(name)
        plaintext = crypto.decrypt_bytes(self._ciphertext(row))
        return ContentFile(plaintext, name=name)

    def _save(self, name: str, content) -> str:
        if hasattr(content, "chunks"):
            data = b"".join(content.chunks())
        else:
            data = content.read()
        if len(data) > self.max_upload_bytes:
            raise MediaTooLarge(f"{name}: {len(data)} bytes exceeds MEDIA_MAX_UPLOAD_BYTES")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        ciphertext = crypto.encrypt_bytes(data)
        drive_name = f"{uuid.uuid4().hex}.bin"
        fingerprint = crypto.current_fingerprint()
        # Recovery reference: everything rebuild_drive_index needs, encrypted
        # under the same key — the Drive UI sees a token.
        description = crypto.encrypt_text(json.dumps({
            "name": name, "mime": mime, "size": len(data), "fp": fingerprint,
        }))
        meta = self.client.upload(ciphertext, drive_name, description=description)
        try:
            DriveObject.objects.create(
                name=name,
                file_id=meta["id"],
                drive_name=drive_name,
                size=len(data),
                md5=meta.get("md5Checksum", "") or "",
                mime=mime,
                key_fingerprint=fingerprint,
            )
        except Exception:
            # No index row → nothing can ever reach the blob; don't leave it behind.
            try:
                self.client.trash(meta["id"])
            except DriveError:
                logger.warning("_save %s: index write failed; blob %s left on Drive", name, meta["id"])
            raise
        self.cache.put(drive_name, ciphertext)
        return name

    def delete(self, name: str) -> None:
        row = self._row(name)
        if row is None:
            return
        try:
            self.client.trash(row.file_id)      # never a hard delete: 30-day undo
        except DriveNotFound:
            logger.warning("delete %s: Drive object %s already gone", name, row.file_id)
        self.cache.remove(row.drive_name)
        row.delete()
