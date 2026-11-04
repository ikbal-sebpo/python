"""SSO-only sign-in: with SSO configured, /login/ offers just the SSO button
and refuses a password POST; password login survives only for superusers
(accounts.auth_backends.SuperuserPasswordBackend) via /admin/login/."""

from __future__ import annotations

from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import include, path

from core.urls import urlpatterns as _core_urlpatterns

User = get_user_model()

# The suite runs without Azure creds, so core.urls never mounts /oidc/ —
# this urlconf adds it so the SSO button's {% url %} resolves.
urlpatterns = _core_urlpatterns + [path("oidc/", include("mozilla_django_oidc.urls"))]

_SSO_ON = dict(
    SSO_ENABLED=True,
    ROOT_URLCONF=__name__,
    AUTHENTICATION_BACKENDS=(
        "accounts.auth_backends.OrbisOIDCBackend",
        "accounts.auth_backends.SuperuserPasswordBackend",
    ),
    OIDC_RP_CLIENT_ID="test",
    OIDC_RP_CLIENT_SECRET="test",
    OIDC_OP_AUTHORIZATION_ENDPOINT="https://login.example.com/authorize",
    OIDC_OP_TOKEN_ENDPOINT="https://login.example.com/token",
    OIDC_OP_USER_ENDPOINT="https://login.example.com/me",
    OIDC_RP_SIGN_ALGO="HS256",
)


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!", **extra,
    )


@override_settings(**_SSO_ON)
class SsoOnlyLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = _user("alice@sebpo.com")
        cls.root = _user("root@sebpo.com", is_staff=True, is_superuser=True)

    def test_login_page_shows_only_sso(self):
        resp = self.client.get("/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in with Company SSO")
        self.assertNotContains(resp, 'type="password"')

    def test_password_post_refused(self):
        resp = self.client.post("/login/", {"username": "alice@sebpo.com", "password": "test-1234!"})
        self.assertEqual(resp.status_code, 405)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_password_backend_rejects_non_superuser(self):
        request = RequestFactory().post("/admin/login/")
        self.assertIsNone(authenticate(request, username="alice@sebpo.com", password="test-1234!"))

    def test_superuser_break_glass_via_admin(self):
        resp = self.client.post(
            "/admin/login/?next=/admin/",
            {"username": "root@sebpo.com", "password": "test-1234!"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(self.root.pk))


@override_settings(
    SSO_ENABLED=False,
    AUTHENTICATION_BACKENDS=("django.contrib.auth.backends.ModelBackend",),
)
class PasswordLoginWithoutSsoTests(TestCase):
    """No Azure creds (local dev): the password form stays."""

    def test_form_shown_and_login_works(self):
        user = _user("bob@sebpo.com")
        self.assertContains(self.client.get("/login/"), 'type="password"')
        resp = self.client.post("/login/", {"username": "bob@sebpo.com", "password": "test-1234!"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(user.pk))
