"""ProtectedView gating — specifically how a denial is *delivered*.

The gate itself is simple (is the user in an allowed group?); what bit us in
production-like use was the response shape. A 302 to the dashboard is right
for a full page load and actively harmful for an htmx fragment request: htmx
follows the redirect and swaps the whole dashboard into the fragment's
target. On /payroll/overtime/ that put an entire dashboard inside the
delete-confirmation drawer, with nothing anywhere saying "denied"
(2026-09-15). See dynamic_menu/decorators.py::_denied.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from dynamic_menu.models import ProtectedView

User = get_user_model()

# Any @dynamic_allowed_users-decorated route works; this one is convenient
# because its denial paths run before the view body needs any fixtures.
GATED_URL_NAME = "payroll_overtime_list"

HTMX = {"HTTP_HX_REQUEST": "true"}


class ProtectedViewDenialTests(TestCase):
    def setUp(self):
        self.url = reverse(GATED_URL_NAME)
        self.user = User.objects.create_user(
            email="gate@example.com", username="gate@example.com", password="x",
        )

    # --- no ProtectedView row at all (the deploy-ran-ahead-of-seed case) ---

    def test_missing_row_redirects_a_normal_request(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(self.url), "/")

    def test_missing_row_gives_htmx_a_403_not_a_redirect(self):
        self.client.force_login(self.user)
        resp = self.client.get(self.url, **HTMX)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("HX-Error-Message", resp.headers)
        # Nothing swappable came back — that's the whole point.
        self.assertEqual(resp.content, b"")

    # --- row exists, user isn't in an allowed group ---

    def test_wrong_group_gives_htmx_a_403(self):
        pv = ProtectedView.objects.create(view_name=GATED_URL_NAME, registered_app="payroll")
        pv.allowed_groups.set([Group.objects.create(name="Some Other Group")])
        self.client.force_login(self.user)

        resp = self.client.get(self.url, **HTMX)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("permission", resp.headers["HX-Error-Message"].lower())

        self.assertRedirects(self.client.get(self.url), "/")

    # --- expired session ---

    def test_anonymous_htmx_request_is_told_to_navigate(self):
        """htmx can't render a login page into a fragment target, so the
        answer is an HX-Redirect the browser acts on, not login HTML."""
        resp = self.client.get(self.url, **HTMX)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("HX-Redirect"), reverse("login"))

    def test_anonymous_normal_request_redirects_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("login")))
