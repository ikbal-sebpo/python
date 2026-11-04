"""media_store — storage backend, serve view, commands. No network: the Drive
client is ``FakeDriveClient`` (injected through STORAGES OPTIONS)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from audit.models import AccessLog, DataClass
from audit.registry import EXCLUDED_PATH_PREFIXES, rule_for
from media_store import crypto
from media_store.cache import MediaCache
from media_store.drive import MARKER_KEY, MARKER_VALUE, DriveClient, DriveError, DriveNotFound
from media_store.models import DriveObject
from media_store.storage import GoogleDriveStorage, MediaTooLarge

User = get_user_model()


# ---------------------------------------------------------------------------
# Fake Drive
# ---------------------------------------------------------------------------

class FakeDriveClient:
    """In-memory stand-in for ``media_store.drive.DriveClient``. State is
    class-level so a test can inspect what the storage (which builds its own
    instance from the dotted path) handed over."""

    files: dict[str, dict] = {}
    downloads = 0
    _seq = 0
    root_folder_id = "root"

    @classmethod
    def reset(cls):
        cls.files = {}
        cls.downloads = 0
        cls._seq = 0

    def about(self):
        return {"user": {"emailAddress": "fake@example.com"}, "storageQuota": {}}

    def create_folder(self, name, parent=None):
        return self._new({"name": name, "folder": True, "parents": [parent or self.root_folder_id]})

    def upload(self, data, drive_name, *, description="", app_properties=None, parent=None):
        fid = self._new({
            "name": drive_name, "data": data, "description": description,
            "appProperties": {MARKER_KEY: MARKER_VALUE, **(app_properties or {})},
            "parents": [parent or self.root_folder_id], "trashed": False,
            "md5Checksum": hashlib.md5(data).hexdigest(), "size": str(len(data)),
            "createdTime": f"2026-10-10T00:00:{self._seq:02d}Z",
        })
        return {"id": fid, "md5Checksum": self.files[fid]["md5Checksum"], "size": str(len(data))}

    def download(self, file_id):
        row = self.files.get(file_id)
        if row is None or row.get("trashed"):
            raise DriveNotFound(404, "not found")
        type(self).downloads += 1
        return row["data"]

    def get(self, file_id, fields=""):
        row = self.files.get(file_id)
        if row is None:
            raise DriveNotFound(404, "not found")
        return {"id": file_id, **{k: v for k, v in row.items() if k != "data"}}

    def trash(self, file_id):
        if file_id not in self.files:
            raise DriveNotFound(404, "not found")
        self.files[file_id]["trashed"] = True

    def list_blobs(self, parent=None, page_size=1000):
        for fid, row in self.files.items():
            if row.get("folder") or row.get("trashed"):
                continue
            if row.get("appProperties", {}).get(MARKER_KEY) != MARKER_VALUE:
                continue
            yield {"id": fid, **{k: v for k, v in row.items() if k != "data"}}

    @classmethod
    def _new(cls, row):
        cls._seq += 1
        fid = f"fake{cls._seq}"
        cls.files[fid] = row
        return fid

    @classmethod
    def metadata_dump(cls) -> str:
        return json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "data"} for k, v in cls.files.items()})


_CACHE = tempfile.mkdtemp(prefix="orbis-media-cache-tests-")
DRIVE_STORAGES = {
    **settings.STORAGES,
    "default": {
        "BACKEND": "media_store.storage.GoogleDriveStorage",
        "OPTIONS": {
            "client": "media_store.tests.FakeDriveClient",
            "cache_root": _CACHE,
            "max_cache_bytes": 10**9,
            "max_upload_bytes": 1024 * 1024,
        },
    },
}


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test", **extra,
    )


class _DriveTestCase(TestCase):
    def setUp(self):
        FakeDriveClient.reset()
        shutil.rmtree(_CACHE, ignore_errors=True)
        Path(_CACHE).mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_CACHE, ignore_errors=True)


# ---------------------------------------------------------------------------
# Storage backend
# ---------------------------------------------------------------------------

@override_settings(STORAGES=DRIVE_STORAGES)
class StorageTests(_DriveTestCase):
    def test_round_trip_is_encrypted_and_opaque_on_drive(self):
        name = default_storage.save("profile_pictures/1234_Jane.jpg", ContentFile(b"JPEGDATA"))
        self.assertEqual(name, "profile_pictures/1234_Jane.jpg")

        row = DriveObject.objects.get(name=name)
        self.assertEqual(row.size, 8)
        self.assertEqual(row.mime, "image/jpeg")
        self.assertEqual(row.key_fingerprint, crypto.current_fingerprint())
        self.assertTrue(row.drive_name.endswith(".bin"))

        blob = FakeDriveClient.files[row.file_id]
        self.assertEqual(blob["name"], row.drive_name)
        self.assertNotEqual(blob["data"], b"JPEGDATA")
        # The bytes on Drive are a valid Fernet token under our key...
        self.assertEqual(crypto.decrypt_bytes(blob["data"]), b"JPEGDATA")
        # ...and the only reference back is itself encrypted.
        ref = json.loads(crypto.decrypt_text(blob["description"]))
        self.assertEqual(ref["name"], name)
        self.assertEqual(ref["mime"], "image/jpeg")
        dump = FakeDriveClient.metadata_dump()
        for leak in ("profile_pictures", "Jane", ".jpg", "image/jpeg"):
            self.assertNotIn(leak, dump)

        # The local cache holds ciphertext only.
        cached = Path(_CACHE, row.drive_name).read_bytes()
        self.assertEqual(cached, blob["data"])

        with default_storage.open(name) as fh:
            self.assertEqual(fh.read(), b"JPEGDATA")
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(default_storage.size(name), 8)

    def test_open_prefers_cache_then_falls_back_to_drive(self):
        name = default_storage.save("leave/doc.pdf", ContentFile(b"%PDF-1.4"))
        default_storage.open(name).read()
        self.assertEqual(FakeDriveClient.downloads, 0)
        shutil.rmtree(_CACHE)
        self.assertEqual(default_storage.open(name).read(), b"%PDF-1.4")
        self.assertEqual(FakeDriveClient.downloads, 1)
        default_storage.open(name).read()
        self.assertEqual(FakeDriveClient.downloads, 1, "second read refilled the cache")

    def test_delete_trashes_and_unindexes(self):
        name = default_storage.save("leave/doc.pdf", ContentFile(b"x"))
        row = DriveObject.objects.get(name=name)
        default_storage.delete(name)
        self.assertTrue(FakeDriveClient.files[row.file_id]["trashed"])
        self.assertFalse(DriveObject.objects.filter(name=name).exists())
        self.assertFalse(default_storage.exists(name))
        self.assertFalse(Path(_CACHE, row.drive_name).exists())
        default_storage.delete(name)   # idempotent

    def test_collision_gets_a_new_name(self):
        first = default_storage.save("leave/doc.pdf", ContentFile(b"1"))
        second = default_storage.save("leave/doc.pdf", ContentFile(b"2"))
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("leave/doc"))
        self.assertEqual(default_storage.open(second).read(), b"2")

    def test_names_differing_only_in_case_are_distinct(self):
        # The filesystem is case-sensitive, so both exist on disk; production
        # MySQL needs migration 0002 for this to hold (2026-10-16).
        default_storage._save("profile_pictures/Photo.jpg", ContentFile(b"upper"))
        default_storage._save("profile_pictures/photo.jpg", ContentFile(b"lower"))
        self.assertEqual(default_storage.open("profile_pictures/Photo.jpg").read(), b"upper")
        self.assertEqual(default_storage.open("profile_pictures/photo.jpg").read(), b"lower")

    def test_failed_index_write_trashes_the_uploaded_blob(self):
        default_storage.save("leave/doc.pdf", ContentFile(b"1"))
        with self.assertRaises(IntegrityError):
            default_storage._save("leave/doc.pdf", ContentFile(b"2"))   # bypasses get_available_name
        live = [f for f in FakeDriveClient.files.values() if not f.get("trashed") and not f.get("folder")]
        self.assertEqual(len(live), 1)

    def test_upload_size_guard(self):
        with self.assertRaises(MediaTooLarge):
            default_storage.save("leave/big.bin", ContentFile(b"x" * (1024 * 1024 + 1)))
        self.assertEqual(FakeDriveClient.files, {})

    def test_url_is_the_serve_view(self):
        self.assertEqual(default_storage.url("profile_pictures/a.jpg"), "/media/profile_pictures/a.jpg")

    def test_wrong_key_is_a_clear_error(self):
        name = default_storage.save("leave/doc.pdf", ContentFile(b"secret"))
        with override_settings(MEDIA_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            with self.assertRaises(crypto.MediaDecryptionError):
                default_storage.open(name).read()

    def test_field_to_data_uri_has_no_path_dependency(self):
        from documents.pdf import _field_to_data_uri
        user = _user("sig@example.com")
        user.signature.save("sig.png", ContentFile(b"\x89PNG"), save=True)
        uri = _field_to_data_uri(user.signature)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(_field_to_data_uri(None), "")


class CacheTests(SimpleTestCase):
    def test_prune_is_lru(self):
        root = Path(tempfile.mkdtemp(prefix="orbis-cache-prune-"))
        try:
            cache = MediaCache(root, max_bytes=25)
            cache.put("a.bin", b"x" * 10)
            time.sleep(0.01)
            cache.put("b.bin", b"x" * 10)
            time.sleep(0.01)
            cache.put("c.bin", b"x" * 10)
            cache.get("a.bin")                 # touch: a is now the most recent
            removed, removed_bytes = cache.prune()
            self.assertEqual((removed, removed_bytes), (1, 10))
            self.assertFalse((root / "b.bin").exists())
            self.assertTrue((root / "a.bin").exists())
            self.assertTrue((root / "c.bin").exists())
            self.assertEqual(cache.usage(), (2, 20))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_unwritable_root_never_raises(self):
        cache = MediaCache("/proc/orbis-cannot-write-here", max_bytes=10)
        cache.put("a.bin", b"x")
        self.assertIsNone(cache.get("a.bin"))
        self.assertEqual(cache.usage(), (0, 0))


# ---------------------------------------------------------------------------
# Serve view
# ---------------------------------------------------------------------------

@override_settings(STORAGES=DRIVE_STORAGES)
class ServeViewTests(_DriveTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = _user("alice@example.com")
        cls.bob = _user("bob@example.com")
        cls.boss = _user("boss@example.com", is_superuser=True, is_staff=True)

    def _save(self, name, data=b"bytes"):
        return default_storage.save(name, ContentFile(data))

    def test_anonymous_is_sent_to_login(self):
        name = self._save("profile_pictures/alice.jpg")
        resp = self.client.get(f"/media/{name}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)

    def test_profile_picture_for_any_authenticated_user_with_caching_headers(self):
        self.alice.profile_picture.save("alice.jpg", ContentFile(b"JPEG"), save=True)
        name = self.alice.profile_picture.name
        self.client.force_login(self.bob)
        resp = self.client.get(f"/media/{name}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"JPEG")
        self.assertEqual(resp["Content-Type"], "image/jpeg")
        self.assertEqual(resp["Cache-Control"], "private, max-age=86400")
        etag = resp["ETag"]
        self.assertTrue(etag)
        log = AccessLog.objects.order_by("-id").first()
        self.assertEqual(log.data_class, DataClass.PROFILE)
        self.assertEqual(log.subject_id, self.alice.pk)

        again = self.client.get(f"/media/{name}", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)

    def test_hidden_prefixes_are_404_even_for_a_superuser(self):
        self.client.force_login(self.boss)
        for name in ("payroll/batches/x.xlsx", "documents/generated/x.pdf", "policies/1/v1.pdf"):
            self._save(name)
            self.assertEqual(self.client.get(f"/media/{name}").status_code, 404, name)

    def test_unknown_or_traversal_names_are_404(self):
        self.client.force_login(self.boss)
        self._save("mystery/x.bin")
        self.assertEqual(self.client.get("/media/mystery/x.bin").status_code, 404)
        self.assertEqual(self.client.get("/media/profile_pictures/../x.jpg").status_code, 404)
        self.assertEqual(self.client.get("/media/profile_pictures/nope.jpg").status_code, 404)

    def test_signature_is_owner_or_org_wide_readers_only(self):
        self.alice.signature.save("alice.png", ContentFile(b"PNG"), save=True)
        name = self.alice.signature.name
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 200)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 403)
        self.assertEqual(AccessLog.objects.order_by("-id").first().action, "DENIED")
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 200)

    def test_leave_attachment_follows_leave_visibility(self):
        from leave.models import LeaveApplication
        from roster.models import ShiftType
        leave_type = ShiftType.objects.create(name="CL-MEDIA", category=ShiftType.Category.LEAVE)
        app = LeaveApplication(
            employee=self.alice, leave_type=leave_type, applied_by=self.alice,
            start_date=date(2026, 10, 12), end_date=date(2026, 10, 12),
        )
        app.attachment.save("note.pdf", ContentFile(b"%PDF"), save=False)
        app.save()
        name = app.attachment.name
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 200)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 403)
        self.client.force_login(self.boss)
        resp = self.client.get(f"/media/{name}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AccessLog.objects.order_by("-id").first().data_class, DataClass.LEAVE)

    def test_legal_document_follows_profile_visibility(self):
        from employees.models import LegalIssue
        issue = LegalIssue(employee=self.alice)
        issue.case_document.save("case.png", ContentFile(b"PNG"), save=False)
        issue.save()
        name = issue.case_document.name
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 403)
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 200)

    def test_root_level_ckeditor_upload_is_staff_only(self):
        self._save("pasted.png")
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/media/pasted.png").status_code, 403)
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get("/media/pasted.png").status_code, 200)


class FilesystemServeTests(TestCase):
    """The same view fronts FileSystemStorage (dev, and prod before the flip)."""

    def test_served_with_weak_etag(self):
        root = tempfile.mkdtemp(prefix="orbis-media-fs-")
        try:
            with override_settings(MEDIA_ROOT=root):
                user = _user("fs@example.com")
                user.profile_picture.save("fs.jpg", ContentFile(b"FSJPEG"), save=True)
                self.assertTrue(os.path.exists(os.path.join(root, user.profile_picture.name)))
                self.client.force_login(user)
                resp = self.client.get(f"/media/{user.profile_picture.name}")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(b"".join(resp.streaming_content), b"FSJPEG")
                self.assertTrue(resp["ETag"].startswith('W/"'))
                self.assertEqual(self.client.get("/media/payroll/batches/x.xlsx").status_code, 404)
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@override_settings(STORAGES=DRIVE_STORAGES)
class CommandTests(_DriveTestCase):
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix="orbis-media-migrate-")
        for rel, data in (("profile_pictures/a.jpg", b"A"), ("leave/b.pdf", b"BB")):
            path = Path(self.media_root, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def tearDown(self):
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _call(self, *args, **kwargs):
        out, err = StringIO(), StringIO()
        call_command(*args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_migrate_is_idempotent_and_keeps_names(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            out, _ = self._call("migrate_media_to_drive", "--dry-run")
            self.assertIn("would upload 2", out)
            self.assertEqual(DriveObject.objects.count(), 0)
            out, _ = self._call("migrate_media_to_drive")
            self.assertIn("uploaded 2, skipped 0", out)
            out, _ = self._call("migrate_media_to_drive")
            self.assertIn("uploaded 0, skipped 2", out)
        self.assertEqual(
            set(DriveObject.objects.values_list("name", flat=True)),
            {"profile_pictures/a.jpg", "leave/b.pdf"},
        )
        self.assertEqual(default_storage.open("leave/b.pdf").read(), b"BB")
        for blob in FakeDriveClient.files.values():
            self.assertNotIn(blob["data"], (b"A", b"BB"))

    def test_reverse_writes_blobs_back_to_disk(self):
        default_storage.save("offboarding/step_attachments/r.txt", ContentFile(b"back"))
        target = tempfile.mkdtemp(prefix="orbis-media-reverse-")
        try:
            with override_settings(MEDIA_ROOT=target):
                out, _ = self._call("migrate_media_to_drive", "--reverse")
            self.assertIn("wrote 1", out)
            self.assertEqual(Path(target, "offboarding/step_attachments/r.txt").read_bytes(), b"back")
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_rebuild_index_from_drive(self):
        default_storage.save("leave/one.pdf", ContentFile(b"1"))
        default_storage.save("leave/two.pdf", ContentFile(b"22"))
        before = {r.name: (r.file_id, r.size, r.mime, r.md5) for r in DriveObject.objects.all()}
        DriveObject.objects.all().delete()
        out, _ = self._call("rebuild_drive_index", "--dry-run")
        self.assertIn("would create 2", out)
        self.assertEqual(DriveObject.objects.count(), 0)
        out, _ = self._call("rebuild_drive_index")
        self.assertIn("created 2", out)
        after = {r.name: (r.file_id, r.size, r.mime, r.md5) for r in DriveObject.objects.all()}
        self.assertEqual(before, after)
        self.assertEqual(default_storage.open("leave/two.pdf").read(), b"22")

    def test_verify_passes_then_fails_when_a_blob_is_trashed(self):
        user = _user("v@example.com")
        user.profile_picture.save("v.jpg", ContentFile(b"V"), save=True)
        out, _ = self._call("verify_media_drive")
        self.assertIn("skipped", out, "filesystem backend: a no-op success for the beat job")
        with override_settings(MEDIA_BACKEND="drive"):
            out, _ = self._call("verify_media_drive")
            self.assertIn("OK", out)
            self.assertIn("referenced 1, indexed 1", out)
            row = DriveObject.objects.get(name=user.profile_picture.name)
            FakeDriveClient.files[row.file_id]["trashed"] = True
            with self.assertRaises(CommandError):
                self._call("verify_media_drive")
            row.delete()
            with self.assertRaises(CommandError):
                self._call("verify_media_drive", "--no-remote")
        with self.assertRaises(CommandError):
            self._call("verify_media_drive", "--force", "--no-remote")

    def test_prune_command(self):
        default_storage.save("leave/p.pdf", ContentFile(b"p" * 100))
        with override_settings(MEDIA_CACHE_ROOT=_CACHE, MEDIA_CACHE_MAX_BYTES=10):
            out, _ = self._call("prune_media_cache")
        self.assertIn("removed 1 files", out)


# ---------------------------------------------------------------------------
# Wiring guards
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _ScriptedSession:
    """Plays back responses in order and records each request's headers/body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, **kwargs):
        self.calls.append({"headers": headers or {}, "data": data})
        return self.responses.pop(0)


class ResumableUploadRecoveryTests(SimpleTestCase):
    """drive.DriveClient._put_session — the 2026-10-16 "operation was successful,
    but there was an error preparing the response" 500 → 410 on blind retry."""

    DONE = {"id": "f1", "md5Checksum": "abc", "size": "10"}

    def _client(self, session):
        client = DriveClient("unused.json", "root", session=session)
        client._headers = lambda: {"Authorization": "Bearer t"}
        return client

    def setUp(self):
        patcher = mock.patch("media_store.drive.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_500_then_status_check_says_complete(self):
        session = _ScriptedSession(_Resp(500), _Resp(200, self.DONE))
        self.assertEqual(self._client(session)._put_session("loc", b"0123456789"), self.DONE)
        self.assertEqual(session.calls[1]["headers"]["Content-Range"], "bytes */10")
        self.assertEqual(len(session.calls), 2, "did not resend a completed upload")

    def test_308_resends_only_the_missing_bytes(self):
        session = _ScriptedSession(
            _Resp(503), _Resp(308, headers={"Range": "bytes=0-3"}), _Resp(201, self.DONE),
        )
        self.assertEqual(self._client(session)._put_session("loc", b"0123456789"), self.DONE)
        resend = session.calls[2]
        self.assertEqual(resend["data"], b"456789")
        self.assertEqual(resend["headers"]["Content-Range"], "bytes 4-9/10")

    def test_308_without_range_resends_everything(self):
        session = _ScriptedSession(_Resp(502), _Resp(308), _Resp(200, self.DONE))
        self._client(session)._put_session("loc", b"0123456789")
        self.assertEqual(session.calls[2]["data"], b"0123456789")
        self.assertNotIn("Content-Range", session.calls[2]["headers"])

    def test_non_retryable_status_raises(self):
        session = _ScriptedSession(_Resp(400, {"error": "bad"}))
        with self.assertRaises(DriveError) as ctx:
            self._client(session)._put_session("loc", b"x")
        self.assertEqual(ctx.exception.status, 400)

    def test_expired_session_raises(self):
        session = _ScriptedSession(_Resp(500), _Resp(410))
        with self.assertRaises(DriveError) as ctx:
            self._client(session)._put_session("loc", b"x")
        self.assertEqual(ctx.exception.status, 410)


class ExactOwnerMatchTests(SimpleTestCase):
    """rules._exact — MySQL's _ai_ci collation returns case-variant rows from
    filter(); the rule must authorise against the exact one only."""

    class _Row:
        def __init__(self, label, name):
            self.label = label
            self.picture = mock.Mock(name=name)
            self.picture.name = name

    class _CaseInsensitiveQS:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, picture):
            return [r for r in self.rows if r.picture.name.lower() == picture.lower()]

    def test_picks_the_exact_row_not_the_case_variant(self):
        from media_store.rules import _exact
        qs = self._CaseInsensitiveQS([
            self._Row("upper", "profile_pictures/Photo.jpg"),
            self._Row("lower", "profile_pictures/photo.jpg"),
        ])
        self.assertEqual(_exact(qs, "picture", "profile_pictures/photo.jpg").label, "lower")
        self.assertEqual(_exact(qs, "picture", "profile_pictures/Photo.jpg").label, "upper")

    def test_only_a_case_variant_exists_is_no_match(self):
        from media_store.rules import _exact
        qs = self._CaseInsensitiveQS([self._Row("upper", "leave/Doc.pdf")])
        self.assertIsNone(_exact(qs, "picture", "leave/doc.pdf"))


class WiringTests(SimpleTestCase):
    def test_media_route_is_classified_and_logged(self):
        self.assertEqual(rule_for("media_serve").data_class, DataClass.DOCUMENT)
        self.assertNotIn("/media/", EXCLUDED_PATH_PREFIXES)

    def test_beat_jobs_seeded(self):
        from scheduler.schedule import JOBS
        by_command = {job.command: job for job in JOBS}
        self.assertIn("prune_media_cache", by_command)
        self.assertIn("verify_media_drive", by_command)
        self.assertTrue(by_command["verify_media_drive"].enabled)
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE, {})

    def test_key_fingerprint_is_stable_and_opaque(self):
        key = Fernet.generate_key().decode()
        self.assertEqual(crypto.fingerprint(key), crypto.fingerprint(key))
        self.assertNotIn(key[:8], crypto.fingerprint(key))
