"""`CustomUser.directory_updated_at` (the Directory API change feed) and the
Entra object id recorded at SSO login. docs/DIRECTORY_API_PLAN.md §5.4, §6.1."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.auth_backends import OrbisOIDCBackend
from accounts.models import Department, directory_stamp
from offboarding.services import deactivate_past_lwd_users

User = get_user_model()

_LONG_AGO = timezone.now() - timedelta(days=30)


class DirectoryUpdatedAtTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name="General BPO")

    def setUp(self):
        user = User.objects.create_user(email="a@sebpo.com", username="a@sebpo.com", employee_id="1")
        User.objects.filter(pk=user.pk).update(directory_updated_at=_LONG_AGO)
        self.user = User.objects.get(pk=user.pk)

    def stamp(self):
        return User.objects.values_list("directory_updated_at", flat=True).get(pk=self.user.pk)

    def test_new_user_is_stamped(self):
        new = User.objects.create_user(email="b@sebpo.com", username="b@sebpo.com")
        self.assertGreater(new.directory_updated_at, _LONG_AGO)

    def test_directory_field_change_bumps(self):
        self.user.department = self.dept
        self.user.save()
        self.assertGreater(self.stamp(), _LONG_AGO)

    def test_bumps_even_with_update_fields(self):
        self.user.department = self.dept
        self.user.save(update_fields=["department"])
        self.assertGreater(self.stamp(), _LONG_AGO)

    def test_login_does_not_bump(self):
        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])
        self.assertEqual(self.stamp(), _LONG_AGO)

    def test_untracked_field_does_not_bump(self):
        self.user.contact_number = "01700000000"
        self.user.save()
        self.assertEqual(self.stamp(), _LONG_AGO)

    def test_loading_a_deferred_field_keeps_an_earlier_edit(self):
        user = User.objects.only("pk", "first_name").get(pk=self.user.pk)
        user.first_name = "Renamed"
        _ = user.department_id  # deferred: loads via refresh_from_db(fields=[...])
        user.save()
        self.assertGreater(self.stamp(), _LONG_AGO)

    def test_deactivation_backstop_bumps(self):
        User.objects.filter(pk=self.user.pk).update(last_working_date=date.today() - timedelta(days=1))
        deactivate_past_lwd_users()
        self.assertGreater(self.stamp(), _LONG_AGO)

    def test_directory_stamp(self):
        self.assertIn("directory_updated_at", directory_stamp("team"))
        self.assertIn("directory_updated_at", directory_stamp("department_id"))
        self.assertEqual(directory_stamp("contact_number"), {})
        self.assertEqual(directory_stamp("department", model=Department), {})


OID = "3f2b8c1e-1d2a-4b5c-9e8f-0a1b2c3d4e5f"
OTHER_OID = "11111111-2222-3333-4444-555555555555"


@override_settings(
    OIDC_OP_TOKEN_ENDPOINT="https://login.example/token",
    OIDC_OP_USER_ENDPOINT="https://graph.example/me",
    OIDC_OP_JWKS_ENDPOINT="https://login.example/keys",
    OIDC_RP_CLIENT_ID="x", OIDC_RP_CLIENT_SECRET="x", OIDC_RP_SIGN_ALGO="RS256",
)
class EntraObjectIdTests(TestCase):
    def setUp(self):
        self.backend = OrbisOIDCBackend()
        self.user = User.objects.create_user(email="a@sebpo.com", username="a@sebpo.com")

    def login(self, user, **claims):
        claims.setdefault("mail", user.email)
        with self.assertLogs("accounts.auth_backends", level=logging.INFO) as logs:
            logging.getLogger("accounts.auth_backends").info("—")
            self.backend.update_user(user, claims)
        user.refresh_from_db()
        return logs.output

    def test_recorded_on_returning_login(self):
        self.login(self.user, id=OID.upper())
        self.assertEqual(self.user.entra_object_id, OID)

    def test_id_token_oid_claim_is_accepted(self):
        self.login(self.user, oid=OID)
        self.assertEqual(self.user.entra_object_id, OID)

    def test_recording_moves_the_change_feed(self):
        User.objects.filter(pk=self.user.pk).update(directory_updated_at=_LONG_AGO)
        self.login(self.user, id=OID)
        self.assertGreater(self.user.directory_updated_at, _LONG_AGO)

    def test_never_overwritten(self):
        User.objects.filter(pk=self.user.pk).update(entra_object_id=OTHER_OID)
        self.user.refresh_from_db()
        output = self.login(self.user, id=OID)
        self.assertEqual(self.user.entra_object_id, OTHER_OID)
        self.assertTrue(any("mismatch" in line for line in output))

    def test_never_moved_off_another_user(self):
        User.objects.create_user(email="b@sebpo.com", username="b@sebpo.com", entra_object_id=OID)
        output = self.login(self.user, id=OID)
        self.assertIsNone(self.user.entra_object_id)
        self.assertTrue(any("already belongs" in line for line in output))

    def test_malformed_id_is_ignored(self):
        self.login(self.user, id="not-a-uuid")
        self.assertIsNone(self.user.entra_object_id)

    def test_login_matches_object_id_before_email(self):
        User.objects.filter(pk=self.user.pk).update(entra_object_id=OID)
        # Email renamed in Entra: still the same person.
        found = self.backend.filter_users_by_claims({"id": OID, "mail": "renamed@sebpo.com"})
        self.assertEqual(list(found), [self.user])

    def test_unknown_object_id_falls_back_to_email(self):
        found = self.backend.filter_users_by_claims({"id": OID, "mail": "a@sebpo.com"})
        self.assertEqual(list(found), [self.user])
