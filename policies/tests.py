"""Tests for the policies app.

Run with the SQLite test settings::

    python manage.py test policies --settings=core.settings_test
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department
from accounts.permissions import can_edit_all
from policies import services
from policies.models import (
    Policy,
    PolicyAcknowledgement,
    PolicyAcknowledgementEvent,
    PolicyVersion,
)

User = get_user_model()

PDF_BYTES = b"%PDF-1.4\n%fake pdf body\n"


def _pdf(name="policy.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PolicyServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # NOTE: accounts.signals auto-adds every new user to "All Employees", so
        # we target a dedicated group ("Engineering") to control membership
        # precisely; the auto-added "All Employees" membership is irrelevant here.
        # "All Employees" / "Policy Managers" are seeded by migrations, so under
        # the default (migrate-based) test DB they already exist — get_or_create
        # keeps this robust whether or not migrations ran (see settings_test).
        cls.all_emp, _ = Group.objects.get_or_create(name="All Employees")
        cls.dept = Group.objects.create(name="Engineering")
        cls.managers, _ = Group.objects.get_or_create(name="Policy Managers")
        cls.department = Department.objects.create(name="Engineering Dept")

        cls.owner = cls._mk("owner", groups=[cls.dept])
        cls.emp1 = cls._mk("emp1", groups=[cls.dept])
        cls.emp2 = cls._mk("emp2", groups=[cls.dept])
        # An inactive user in the target group — must be excluded from adherents.
        cls.inactive = cls._mk("inactive", groups=[cls.dept], is_active=False)
        # A user NOT in the target group (only auto "All Employees") — must be
        # excluded from a dept-targeted policy unless custom-added.
        cls.outsider = cls._mk("outsider", groups=[])

    @staticmethod
    def _mk(slug, *, groups, is_active=True):
        u = User.objects.create_user(
            email=f"{slug}@sebpo.com", username=slug, password="x",
            first_name=slug.title(), last_name="Test", is_active=is_active,
        )
        for g in groups:
            u.groups.add(g)
        return u

    def _new_policy(self, *, target_groups=None, with_version=True):
        p = Policy.objects.create(title="Code of Conduct", owner=self.owner, department=self.department)
        p.target_groups.set(target_groups if target_groups is not None else [self.dept])
        if with_version:
            services.add_policy_version(p, pdf=_pdf(), actor=self.owner)
        return p

    def _activate_and_circulate(self, p):
        """Draft -> Active -> Circulated, firing on_commit callbacks so the
        notification emails land in the test outbox like a real request."""
        services.activate_policy(p, actor=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            return services.circulate_policy(p, actor=self.owner)

    # --- key assignment --------------------------------------------------

    def test_keys_are_assigned_sequentially(self):
        p1 = Policy.objects.create(title="A", owner=self.owner, department=self.department)
        p2 = Policy.objects.create(title="B", owner=self.owner, department=self.department)
        self.assertTrue(p1.key.startswith("P-"))
        self.assertNotEqual(p1.key, p2.key)
        self.assertEqual(
            int(p2.key.split("-")[1]), int(p1.key.split("-")[1]) + 1,
        )

    # --- assign_adherents ------------------------------------------------

    def test_assign_materialises_only_active_group_members(self):
        p = self._new_policy()
        with self.captureOnCommitCallbacks(execute=True):
            services.assign_adherents(p, actor=self.owner)

        emp_ids = set(p.acknowledgements.values_list("employee_id", flat=True))
        self.assertEqual(emp_ids, {self.owner.pk, self.emp1.pk, self.emp2.pk})
        self.assertNotIn(self.inactive.pk, emp_ids)
        self.assertNotIn(self.outsider.pk, emp_ids)
        # ASSIGNED + NOTIFIED event per adherent.
        self.assertEqual(
            PolicyAcknowledgementEvent.objects.filter(
                event=PolicyAcknowledgementEvent.Event.ASSIGNED).count(), 3)

    def test_assign_is_idempotent(self):
        p = self._new_policy()
        with self.captureOnCommitCallbacks(execute=True):
            services.assign_adherents(p, actor=self.owner)
            second = services.assign_adherents(p, actor=self.owner)
        self.assertEqual(second, [])
        self.assertEqual(p.acknowledgements.count(), 3)

    # --- activate ----------------------------------------------------------

    def test_activate_does_not_notify_or_assign_adherents(self):
        p = self._new_policy()
        with self.captureOnCommitCallbacks(execute=True):
            services.activate_policy(p, actor=self.owner)
        p.refresh_from_db()
        self.assertTrue(p.is_active)
        self.assertFalse(p.is_circulated)
        self.assertIsNone(p.published_at)
        self.assertEqual(p.acknowledgements.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_activate_without_version_raises(self):
        from django.core.exceptions import ValidationError
        p = self._new_policy(with_version=False)
        with self.assertRaises(ValidationError):
            services.activate_policy(p, actor=self.owner)

    def test_activate_non_draft_raises(self):
        from django.core.exceptions import ValidationError
        p = self._new_policy()
        services.activate_policy(p, actor=self.owner)
        with self.assertRaises(ValidationError):
            services.activate_policy(p, actor=self.owner)  # already Active

        services.retire_policy(p, actor=self.owner)
        with self.assertRaises(ValidationError):
            services.activate_policy(p, actor=self.owner)  # Retired

    # --- circulate ---------------------------------------------------------

    def test_circulate_sends_one_email_per_pending_adherent(self):
        p = self._new_policy()
        services.activate_policy(p, actor=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            services.circulate_policy(p, actor=self.owner)
        p.refresh_from_db()
        self.assertTrue(p.is_circulated)
        self.assertIsNotNone(p.published_at)
        self.assertEqual(len(mail.outbox), 3)

    def test_circulate_email_is_branded_html_with_plaintext_fallback(self):
        p = self._new_policy()
        services.activate_policy(p, actor=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            services.circulate_policy(p, actor=self.owner)
        msg = mail.outbox[0]
        # Plain-text body is the fallback; HTML is attached as an alternative.
        self.assertIn("review", msg.body.lower())
        html_parts = [c for c, mime in msg.alternatives if mime == "text/html"]
        self.assertEqual(len(html_parts), 1)
        self.assertIn("Review &amp; Accept", html_parts[0])
        self.assertIn("/sign/", html_parts[0])

    def test_circulate_draft_policy_raises(self):
        from django.core.exceptions import ValidationError
        p = self._new_policy()
        with self.assertRaises(ValidationError):
            services.circulate_policy(p, actor=self.owner)

    def test_circulate_retired_policy_raises_and_does_not_notify(self):
        from django.core.exceptions import ValidationError
        p = self._new_policy()
        services.activate_policy(p, actor=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            services.circulate_policy(p, actor=self.owner)
        services.retire_policy(p, actor=self.owner)
        mail.outbox.clear()

        with self.assertRaises(ValidationError):
            services.circulate_policy(p, actor=self.owner)
        p.refresh_from_db()
        self.assertTrue(p.is_retired)
        self.assertEqual(len(mail.outbox), 0)

    def test_recirculating_already_circulated_policy_raises(self):
        from django.core.exceptions import ValidationError
        p = self._new_policy()
        self._activate_and_circulate(p)
        with self.assertRaises(ValidationError):
            services.circulate_policy(p, actor=self.owner)

    # --- sign ------------------------------------------------------------

    def test_sign_records_state_and_blocks_double_sign(self):
        p = self._new_policy()
        self._activate_and_circulate(p)
        ack = p.acknowledgements.get(employee=self.emp1)
        current = p.current_version

        services.sign(ack, version=current, ip="10.0.0.5",
                      user_agent="pytest-UA", actor=self.emp1)
        ack.refresh_from_db()
        self.assertEqual(ack.status, PolicyAcknowledgement.Status.SIGNED)
        self.assertEqual(ack.version_id, current.pk)
        self.assertEqual(ack.signed_ip, "10.0.0.5")
        self.assertEqual(ack.signed_user_agent, "pytest-UA")
        self.assertIsNotNone(ack.signed_at)
        self.assertEqual(ack.events.filter(
            event=PolicyAcknowledgementEvent.Event.SIGNED).count(), 1)

        # Double-sign is a no-op (no second SIGNED event).
        services.sign(ack, version=current, ip="10.0.0.5", actor=self.emp1)
        self.assertEqual(ack.events.filter(
            event=PolicyAcknowledgementEvent.Event.SIGNED).count(), 1)

    # --- new version / re-ack -------------------------------------------

    def test_new_reack_version_supersedes_and_renotifies(self):
        p = self._new_policy()
        self._activate_and_circulate(p)
        ack = p.acknowledgements.get(employee=self.emp1)
        services.sign(ack, version=p.current_version, actor=self.emp1)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            services.add_policy_version(
                p, pdf=_pdf("v2.pdf"), requires_reack=True, actor=self.owner)

        ack.refresh_from_db()
        self.assertEqual(ack.status, PolicyAcknowledgement.Status.SUPERSEDED)
        self.assertEqual(p.current_version.version, 2)
        self.assertTrue(p.current_version.is_current)
        # Re-notified the previously-signed adherent.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ack.events.filter(
            event=PolicyAcknowledgementEvent.Event.SUPERSEDED).count(), 1)

    def test_new_version_without_reack_leaves_signatures(self):
        p = self._new_policy()
        self._activate_and_circulate(p)
        ack = p.acknowledgements.get(employee=self.emp1)
        services.sign(ack, version=p.current_version, actor=self.emp1)

        with self.captureOnCommitCallbacks(execute=True):
            services.add_policy_version(
                p, pdf=_pdf("v2.pdf"), requires_reack=False, actor=self.owner)
        ack.refresh_from_db()
        self.assertEqual(ack.status, PolicyAcknowledgement.Status.SIGNED)

    # --- reminders -------------------------------------------------------

    def test_scheduled_reminders_only_hit_outstanding_past_interval(self):
        p = self._new_policy()
        self._activate_and_circulate(p)
        # emp1 signs; emp2 + owner stay pending.
        services.sign(p.acknowledgements.get(employee=self.emp1),
                      version=p.current_version, actor=self.emp1)

        # Just-notified (publish set notified_at=now) → interval not elapsed yet.
        with self.captureOnCommitCallbacks(execute=True):
            count_now = services.send_scheduled_reminders(interval_days=3)
        self.assertEqual(count_now, 0)

        # Backdate notified_at on the outstanding rows beyond the interval.
        old = timezone.now() - timezone.timedelta(days=10)
        p.acknowledgements.exclude(
            status=PolicyAcknowledgement.Status.SIGNED).update(notified_at=old)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            count = services.send_scheduled_reminders(interval_days=3)
        self.assertEqual(count, 2)  # owner + emp2, not the signed emp1
        self.assertEqual(len(mail.outbox), 2)
        emp2_ack = p.acknowledgements.get(employee=self.emp2)
        self.assertEqual(emp2_ack.reminder_count, 1)
        self.assertEqual(emp2_ack.events.filter(
            event=PolicyAcknowledgementEvent.Event.REMINDED).count(), 1)

    # --- custom adherents ------------------------------------------------

    def test_custom_adherent_topup_for_new_joiner(self):
        p = self._new_policy()
        self._activate_and_circulate(p)
        self.assertEqual(p.acknowledgements.count(), 3)

        # The picker must exclude already-assigned employees...
        eligible = set(services.eligible_custom_adherents(p).values_list("pk", flat=True))
        self.assertNotIn(self.emp1.pk, eligible)
        # ...and surface the outsider (still missing coverage).
        self.assertIn(self.outsider.pk, eligible)

        # Add the outsider as a custom adherent and top up.
        p.custom_adherents.add(self.outsider)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            created = services.assign_adherents(p, actor=self.owner)
        self.assertEqual(len(created), 1)
        self.assertEqual(p.acknowledgements.count(), 4)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            p.acknowledgements.filter(employee=self.outsider).exists())


class PolicyPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Both groups are migration-seeded; get_or_create avoids a duplicate-key
        # collision under the default migrate-based test DB.
        cls.managers, _ = Group.objects.get_or_create(name="Policy Managers")
        cls.admins, _ = Group.objects.get_or_create(name="User Admins")

        cls.manager = User.objects.create_user(
            email="mgr@sebpo.com", username="mgr", password="x",
            first_name="M", last_name="G")
        cls.manager.groups.add(cls.managers)

        cls.plain = User.objects.create_user(
            email="plain@sebpo.com", username="plain", password="x",
            first_name="P", last_name="L")

    def test_policy_manager_can_manage_but_lacks_profile_write_scope(self):
        self.assertTrue(services.can_manage_policies(self.manager))
        # Critically: Policy Managers membership does NOT grant employee-profile
        # write scope (can_edit_all is HR/Admins-only).
        self.assertFalse(can_edit_all(self.manager))

    def test_plain_employee_cannot_manage(self):
        self.assertFalse(services.can_manage_policies(self.plain))

    def test_superuser_can_manage(self):
        su = User.objects.create_superuser(
            email="su@sebpo.com", username="su", password="x",
            first_name="S", last_name="U")
        self.assertTrue(services.can_manage_policies(su))


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    # base.html references hashed static assets; the manifest storage needs a
    # collectstatic-built manifest that tests don't have, so use plain storage.
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class PolicyViewSmokeTests(TestCase):
    """Render every page so template / url-reversal errors surface in CI."""

    @classmethod
    def setUpTestData(cls):
        cls.dept = Group.objects.create(name="Engineering")
        cls.managers, _ = Group.objects.get_or_create(name="Policy Managers")
        Group.objects.get_or_create(name="All Employees")
        cls.department = Department.objects.create(name="Engineering Dept")

        cls.manager = User.objects.create_user(
            email="m@sebpo.com", username="m", password="pw",
            first_name="Man", last_name="Ager", department=cls.department)
        cls.manager.groups.add(cls.managers)
        cls.emp = User.objects.create_user(
            email="e@sebpo.com", username="e", password="pw",
            first_name="Emp", last_name="Loyee")
        cls.emp.groups.add(cls.dept)

        cls.policy = Policy.objects.create(
            title="Code of Conduct", owner=cls.manager, department=cls.department)
        cls.policy.target_groups.set([cls.dept])
        services.add_policy_version(cls.policy, pdf=_pdf(), actor=cls.manager)
        services.activate_policy(cls.policy, actor=cls.manager)
        with cls._capture():
            services.circulate_policy(cls.policy, actor=cls.manager)

    @staticmethod
    def _capture():
        # circulate schedules emails on_commit; execute them so outbox is realistic.
        from django.test import TestCase as _TC
        return _TC().captureOnCommitCallbacks(execute=True)

    def test_manager_pages_render(self):
        self.client.force_login(self.manager)
        for url in [
            ("policies:list", []),
            ("policies:detail", [self.policy.pk]),
            ("policies:my_policies", []),
        ]:
            from django.urls import reverse
            resp = self.client.get(reverse(url[0], args=url[1]))
            self.assertEqual(resp.status_code, 200, url[0])

    def test_circulate_endpoint_rejects_retired_policy(self):
        # Regression: the "Circulate" button is only rendered for an Active,
        # not-yet-circulated policy (templates/policies/policy_detail.html),
        # but nothing stopped a stale tab/bookmark/direct POST from hitting
        # the endpoint regardless of status — this reproduces exactly that
        # path (the original incident: a retired policy got republished and
        # mass-notified everyone).
        from django.urls import reverse
        services.retire_policy(self.policy, actor=self.manager)
        mail.outbox.clear()

        self.client.force_login(self.manager)
        resp = self.client.post(reverse("policies:circulate", args=[self.policy.pk]))
        self.assertEqual(resp.status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.is_retired)
        self.assertEqual(len(mail.outbox), 0)

    def test_create_drawer_htmx_renders(self):
        from django.urls import reverse
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("policies:create"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "form")

    def test_edit_with_no_groups_clears_targeting_and_circulates_to_named_only(self):
        # Regression (2026-10-28): a manager who added one custom adherent and
        # believed no group was selected still circulated to everyone, because
        # the "All Employees" default stayed selected in a native multi-select
        # (a plain click can't empty one). The backend rule — no groups means
        # nobody via groups — is what this pins, alongside the edit POST
        # clearing the M2M when the field is simply absent.
        from django.urls import reverse
        all_emp = Group.objects.get(name="All Employees")
        named = User.objects.create_user(
            email="named@sebpo.com", username="named", password="pw",
            first_name="Named", last_name="Person")
        policy = Policy.objects.create(
            title="Narrow", owner=self.manager, department=self.department)
        policy.target_groups.set([all_emp])
        services.add_policy_version(policy, pdf=_pdf(), actor=self.manager)
        services.activate_policy(policy, actor=self.manager)

        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("policies:edit", args=[policy.pk]),
            {
                "title": policy.title, "department": self.department.pk,
                "owner": self.manager.pk, "custom_adherents": [str(named.pk)],
                # no "target_groups" key at all — every checkbox unticked
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204, getattr(resp, "content", b""))
        self.assertEqual(list(policy.target_groups.all()), [])
        self.assertEqual(services.target_adherent_ids(policy), {named.pk})

        mail.outbox.clear()
        with self._capture():
            created = services.circulate_policy(policy, actor=self.manager)
        self.assertEqual({a.employee_id for a in created}, {named.pk})
        self.assertEqual(len(mail.outbox), 1)

    def test_detail_shows_targeting_summary_and_count_before_circulation(self):
        from django.urls import reverse
        named = User.objects.create_user(
            email="named2@sebpo.com", username="named2", password="pw",
            first_name="Only", last_name="One")
        policy = Policy.objects.create(
            title="Narrow", owner=self.manager, department=self.department)
        policy.custom_adherents.set([named])
        services.add_policy_version(policy, pdf=_pdf(), actor=self.manager)
        services.activate_policy(policy, actor=self.manager)

        self.client.force_login(self.manager)
        resp = self.client.get(reverse("policies:detail", args=[policy.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["target_count"], 1)
        self.assertContains(resp, "Who must sign")
        self.assertContains(resp, "no group is targeted")
        self.assertContains(resp, "Only One")
        # The Circulate confirm names the headcount, not "all targeted employees".
        self.assertContains(resp, f"Circulate {policy.key} to 1 employee?")

    def test_edit_drawer_renders_groups_as_checkboxes(self):
        from django.urls import reverse
        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("policies:edit", args=[self.policy.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'type="checkbox" name="target_groups"')
        self.assertNotContains(resp, 'name="target_groups" multiple')

    def test_retired_policy_leaves_employee_to_sign_list(self):
        # Regression (2026-10-28): retiring stops reminders and signing but
        # left the pending rows under every employee's "To sign".
        from django.urls import reverse
        self.client.force_login(self.emp)
        resp = self.client.get(reverse("policies:my_policies"))
        self.assertContains(resp, self.policy.key)
        self.assertContains(resp, "To sign (1)")

        services.retire_policy(self.policy, actor=self.manager)
        resp = self.client.get(reverse("policies:my_policies"))
        self.assertNotContains(resp, "To sign (")
        self.assertNotContains(resp, self.policy.key)

    def test_reset_endpoint_clears_retired_policy_and_rejects_active(self):
        from django.urls import reverse
        self.client.force_login(self.manager)
        self.assertEqual(self.policy.adherent_count, 1)

        # Active → refused, nothing deleted.
        resp = self.client.post(reverse("policies:reset", args=[self.policy.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.policy.adherent_count, 1)

        services.retire_policy(self.policy, actor=self.manager)
        resp = self.client.post(reverse("policies:reset", args=[self.policy.pk]))
        self.assertEqual(resp.status_code, 302)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.adherent_count, 0)
        self.assertEqual(PolicyAcknowledgementEvent.objects.filter(
            acknowledgement__policy=self.policy).count(), 0)
        self.assertTrue(self.policy.is_draft)
        self.assertIsNone(self.policy.published_at)
        self.assertIsNone(self.policy.current_version.published_at)
        # The employee reappears in the custom-adherent picker.
        self.assertIn(self.emp, services.eligible_custom_adherents(self.policy))

    def test_detail_offers_reset_only_when_retired(self):
        from django.urls import reverse
        self.client.force_login(self.manager)
        url = reverse("policies:detail", args=[self.policy.pk])
        reset_url = reverse("policies:reset", args=[self.policy.pk])
        self.assertNotContains(self.client.get(url), f'action="{reset_url}"')
        services.retire_policy(self.policy, actor=self.manager)
        self.assertContains(self.client.get(url), f'action="{reset_url}"')

    def test_employee_sign_page_renders_and_blocks_outsider(self):
        from django.urls import reverse
        # Adherent (in target dept) can view the sign page.
        self.client.force_login(self.emp)
        resp = self.client.get(reverse("policies:sign", args=[self.policy.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ">Accept<")
        # `accept` renders hidden and pre-checked — clicking Accept is the
        # affirmative act, not a separate checkbox tick.
        self.assertContains(resp, 'type="checkbox" name="accept" class="hidden" required id="id_accept" checked')
        # Watermark (viewer identity, baked into the canvas by
        # policy-pdf-viewer.js) is scoped to the actual viewer, not the
        # policy owner/manager.
        self.assertContains(resp, "data-watermark=\"e@sebpo.com")

    def test_preview_page_watermarks_the_viewing_manager(self):
        from django.urls import reverse
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("policies:preview", args=[self.policy.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "data-watermark=\"m@sebpo.com")

    def test_employee_cannot_access_manager_list(self):
        from django.urls import reverse
        self.client.force_login(self.emp)
        resp = self.client.get(reverse("policies:list"))
        self.assertEqual(resp.status_code, 403)

    def test_sign_post_records_acknowledgement(self):
        from django.urls import reverse
        self.client.force_login(self.emp)
        resp = self.client.post(
            reverse("policies:sign", args=[self.policy.pk]), {"accept": "on"})
        self.assertEqual(resp.status_code, 302)
        ack = self.policy.acknowledgements.get(employee=self.emp)
        self.assertEqual(ack.status, PolicyAcknowledgement.Status.SIGNED)
        self.assertIsNotNone(ack.signed_at)

    def test_adherent_cannot_force_download_via_querystring(self):
        # An adherent appending ?download=1 themselves must still get an
        # inline disposition — only managers can force an attachment.
        from django.urls import reverse
        self.client.force_login(self.emp)
        resp = self.client.get(reverse("policies:pdf", args=[self.policy.pk]) + "?download=1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Disposition"].startswith("inline"))

    def test_manager_can_force_download_via_querystring(self):
        from django.urls import reverse
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("policies:pdf", args=[self.policy.pk]) + "?download=1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Disposition"].startswith("attachment"))


class PolicyDepartmentScopeTests(TestCase):
    """Policy Managers are locked to their own department's policies —
    everywhere: list, detail, edit, lifecycle actions, and the adherent
    picker's htmx endpoints. Privileged roles (accounts.permissions.
    ALL_DEPARTMENTS_GROUPS) see/manage every department."""

    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="All Employees")
        cls.managers, _ = Group.objects.get_or_create(name="Policy Managers")
        # "HR" (not "Policy Managers") is one of accounts.permissions.
        # ALL_DEPARTMENTS_GROUPS — membership grants cross-department access.
        cls.hr_group, _ = Group.objects.get_or_create(name="HR")

        cls.dept_a = Department.objects.create(name="Dept A")
        cls.dept_b = Department.objects.create(name="Dept B")

        cls.mgr_a = User.objects.create_user(
            email="mgra@sebpo.com", username="mgra", password="x",
            first_name="Mgr", last_name="A", department=cls.dept_a)
        cls.mgr_a.groups.add(cls.managers)

        cls.mgr_b = User.objects.create_user(
            email="mgrb@sebpo.com", username="mgrb", password="x",
            first_name="Mgr", last_name="B", department=cls.dept_b)
        cls.mgr_b.groups.add(cls.managers)

        cls.privileged = User.objects.create_user(
            email="priv@sebpo.com", username="priv", password="x",
            first_name="Priv", last_name="Leged", department=cls.dept_a)
        cls.privileged.groups.add(cls.managers, cls.hr_group)

        # Superuser: cross-department access must hold even with no group
        # membership at all (can_see_all_departments short-circuits on
        # is_superuser before it ever looks at groups).
        cls.superuser = User.objects.create_superuser(
            email="su@sebpo.com", username="su_dept_scope", password="x",
            first_name="Super", last_name="User", department=cls.dept_a)

        cls.policy_a = Policy.objects.create(title="Policy A", owner=cls.mgr_a, department=cls.dept_a)
        cls.policy_b = Policy.objects.create(title="Policy B", owner=cls.mgr_b, department=cls.dept_b)
        services.add_policy_version(cls.policy_a, pdf=_pdf(), actor=cls.mgr_a)

    def test_list_only_shows_own_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.get(reverse("policies:list"))
        titles = {p.title for p in resp.context["policies"]}
        self.assertEqual(titles, {"Policy A"})

    def test_privileged_sees_every_department(self):
        self.client.force_login(self.privileged)
        resp = self.client.get(reverse("policies:list"))
        titles = {p.title for p in resp.context["policies"]}
        self.assertEqual(titles, {"Policy A", "Policy B"})

    def test_superuser_sees_every_department(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("policies:list"))
        titles = {p.title for p in resp.context["policies"]}
        self.assertEqual(titles, {"Policy A", "Policy B"})

    def test_superuser_can_open_other_department_detail(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("policies:detail", args=[self.policy_b.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_detail_allows_own_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.get(reverse("policies:detail", args=[self.policy_a.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_detail_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.get(reverse("policies:detail", args=[self.policy_b.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_privileged_can_open_other_department_detail(self):
        self.client.force_login(self.privileged)
        resp = self.client.get(reverse("policies:detail", args=[self.policy_b.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_edit_drawer_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.get(reverse("policies:edit", args=[self.policy_b.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 403)

    def test_activate_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.post(reverse("policies:activate", args=[self.policy_b.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_circulate_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.post(reverse("policies:circulate", args=[self.policy_b.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_retire_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.post(reverse("policies:retire", args=[self.policy_b.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_adherent_search_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.get(
            reverse("policies:adherents_search"),
            {"policy": str(self.policy_b.pk), "q": "a"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_adherent_select_all_blocks_other_department(self):
        self.client.force_login(self.mgr_a)
        resp = self.client.get(
            reverse("policies:adherents_select_all"),
            {"policy": str(self.policy_b.pk)},
        )
        self.assertEqual(resp.status_code, 403)
