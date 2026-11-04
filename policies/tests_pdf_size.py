from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from policies.forms import PolicyVersionUploadForm


def _pdf(size):
    return SimpleUploadedFile(
        "big.pdf", b"%PDF-" + b"0" * (size - 5), content_type="application/pdf",
    )


class PdfSizeCapTests(SimpleTestCase):
    """The policy PDF cap follows MEDIA_MAX_UPLOAD_BYTES (was a fixed 20 MB)."""

    def _errors(self, size):
        form = PolicyVersionUploadForm(data={"change_note": "x"}, files={"pdf": _pdf(size)})
        form.is_valid()
        return form.errors.get("pdf", [])

    @override_settings(MEDIA_MAX_UPLOAD_BYTES=2048)
    def test_within_cap_passes_size_check(self):
        self.assertEqual(self._errors(2048), [])

    @override_settings(MEDIA_MAX_UPLOAD_BYTES=2048)
    def test_over_cap_rejected(self):
        self.assertTrue(any("too large" in e for e in self._errors(2049)))

    def test_default_cap_admits_a_25_mb_pdf(self):
        self.assertEqual(self._errors(25 * 1024 * 1024), [])
