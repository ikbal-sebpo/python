"""``/media/<name>`` — the only way a browser gets a media file.

Replaces nginx's bare ``alias`` (served to anyone, deploy/README.md) and the
DEBUG-only ``static()`` helper: in every environment the request is
authenticated, resolved to its owning row (``media_store.rules``), logged by
the access-log middleware (enriched here with the subject), and streamed from
whatever ``STORAGES["default"]`` is — the Drive backend decrypts in memory,
``FileSystemStorage`` reads the dev file.
"""

from __future__ import annotations

import mimetypes
import posixpath

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponseNotModified
from django.views import View

from audit.capture import note
from media_store.models import DriveObject
from media_store.rules import resolve

CACHE_CONTROL = "private, max-age=86400"


def _etag(name: str) -> str | None:
    row = DriveObject.objects.filter(name=name).only("md5", "size").first()
    if row is not None and row.md5:
        return f'"{row.md5}"'
    try:
        stamp = default_storage.get_modified_time(name)
        return f'W/"{default_storage.size(name)}-{int(stamp.timestamp())}"'
    except (NotImplementedError, OSError, ValueError):
        return None


class MediaServeView(LoginRequiredMixin, View):
    def get(self, request, name: str):
        resolution = resolve(name, request.user, request)
        if resolution.hidden:
            raise Http404
        note(request, subject=resolution.subject, obj=resolution.obj, data_class=resolution.data_class)
        if not resolution.allowed:
            raise PermissionDenied
        if not default_storage.exists(name):
            raise Http404

        etag = _etag(name)
        if etag and request.headers.get("If-None-Match") == etag:
            response = HttpResponseNotModified()
            response["ETag"] = etag
            response["Cache-Control"] = CACHE_CONTROL
            return response

        try:
            handle = default_storage.open(name, "rb")
        except FileNotFoundError:
            raise Http404
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        response = FileResponse(
            handle, content_type=content_type, filename=posixpath.basename(name),
        )
        response["Cache-Control"] = CACHE_CONTROL
        response["X-Content-Type-Options"] = "nosniff"
        if etag:
            response["ETag"] = etag
        return response
