"""The name → Drive-file index.

Google Drive is not a filesystem: files have ids, names are not unique and a
"path" costs one API call per folder. Orbis therefore keeps its own index of
every blob it has written, keyed by the ``FileField`` value (the only place
that value exists — the blob on Drive is stored under a random name with an
opaque description). ``exists()``, ``size()``, ``url()`` and the serve view's
authorisation read only this table; Drive is touched only to upload, to
download a blob that is not in the local cache, and to trash.

Deliberately no ``CustomUser`` FK: the table is a pure index, which also keeps
its migration free of the MySQL collation CONVERT step.

Spec: docs/MEDIA_GOOGLE_DRIVE_PLAN.md §4.2.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class DriveObject(models.Model):
    #: The FileField value, e.g. ``profile_pictures/1234_Jane.jpg``. Never
    #: written to Drive in clear. Binary collation on MySQL (migration 0002):
    #: names are case- and accent-sensitive, like the filesystem they mirror.
    name = models.CharField(max_length=255, unique=True)
    #: Drive file id of the ciphertext blob.
    file_id = models.CharField(max_length=128)
    #: The random ``<32 hex>.bin`` the blob is stored under on Drive; also the
    #: local cache file name.
    drive_name = models.CharField(max_length=40, db_index=True)
    #: Plaintext size — what ``Storage.size()`` and ``Content-Length`` report.
    size = models.BigIntegerField()
    #: Drive's ``md5Checksum`` of the *ciphertext*, for ``verify_media_drive``
    #: and the serve view's ETag.
    md5 = models.CharField(max_length=32, blank=True)
    #: Real content type, served by Orbis. Drive only ever sees
    #: ``application/octet-stream``.
    mime = models.CharField(max_length=120, blank=True)
    #: Which ``MEDIA_ENCRYPTION_KEYS`` entry encrypted the blob
    #: (``media_store.crypto.fingerprint``), so a rotation knows what is stale.
    key_fingerprint = models.CharField(max_length=16)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Drive object"
        verbose_name_plural = "Drive objects"

    def __str__(self) -> str:
        return self.name
