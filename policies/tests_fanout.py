"""Tests for the chunked, worker-side signing-email fan-out.

Pins the property that made the Celery move worthwhile: a publish request
does O(adherents / chunk) enqueue work — rendering and SMTP happen in
policies.tasks.send_acknowledgement_emails on the worker, with one shared
mail connection and bounded queries per chunk.
"""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.mail import EmailMultiAlternatives
from django.test import TestCase, override_settings

from accounts.models import Department
from policies import services
from policies.models import Policy
from policies.services import _email_acknowledgements, _FANOUT_CHUNK
from policies.tasks import send_acknowledgement_emails

User = get_user_model()


class FanoutChunkingTests(TestCase):
    def test_fanout_enqueues_pk_chunks_not_rendered_mail(self):
        """250 acks → 3 deferred chunk tasks (100/100/50); nothing rendered
        or sent in the caller."""
        fake_acks = [SimpleNamespace(pk=f"uuid-{i}") for i in range(250)]
        with mock.patch("core.tasks.defer") as deferred:
            _email_acknowledgements(fake_acks, "assigned")

        self.assertEqual(deferred.call_count, 3)
        sizes = [len(call.args[1]) for call in deferred.call_args_list]
        self.assertEqual(sizes, [_FANOUT_CHUNK, _FANOUT_CHUNK, 50])
        for call in deferred.call_args_list:
            self.assertIs(call.args[0], send_acknowledgement_emails)
            self.assertEqual(call.args[2], "assigned")
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class FanoutTaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="All Employees")
        cls.dept = Group.objects.create(name="Engineering")
        cls.department = Department.objects.create(name="Engineering Dept")
        cls.owner = cls._mk("owner")
        cls.employees = [cls._mk(f"emp{i}") for i in range(6)]

        cls.policy = Policy.objects.create(
            title="Code of Conduct", owner=cls.owner, department=cls.department)
        cls.policy.target_groups.set([cls.dept])
        for emp in cls.employees:
            emp.groups.add(cls.dept)
        services.add_policy_version(
            cls.policy,
            pdf=SimpleUploadedFile("p.pdf", b"%PDF-1.4\n", content_type="application/pdf"),
            actor=cls.owner,
        )
        cls.acks = services.assign_adherents(cls.policy, actor=cls.owner, notify=False)
        cls.ack_pks = [str(a.pk) for a in cls.acks]

    @staticmethod
    def _mk(slug):
        return User.objects.create_user(
            email=f"{slug}@sebpo.com", username=slug, password="x",
            first_name=slug.title(), last_name="Test",
        )

    def test_task_sends_one_mail_per_ack_with_bounded_queries(self):
        # 2 queries regardless of chunk size: the ack fetch (employee+policy
        # select_related) and the hoisted current_version lookup. A per-ack
        # query here means the old N+1 crept back.
        with self.assertNumQueries(2):
            send_acknowledgement_emails(self.ack_pks, "assigned")
        self.assertEqual(len(mail.outbox), len(self.acks))
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, sorted(e.email for e in self.employees))
        # Branded multipart: HTML alternative present on each message.
        self.assertTrue(all(m.alternatives for m in mail.outbox))

    def test_unknown_pks_are_a_noop(self):
        send_acknowledgement_emails(["00000000-0000-0000-0000-000000000000"], "assigned")
        self.assertEqual(len(mail.outbox), 0)

    def test_total_failure_raises_for_retry(self):
        with mock.patch.object(
            EmailMultiAlternatives, "send", side_effect=OSError("smtp down"),
        ):
            with self.assertRaises(RuntimeError):
                send_acknowledgement_emails(self.ack_pks, "assigned")

    def test_partial_failure_logs_but_does_not_raise(self):
        """One bad recipient must not trigger a chunk retry (retries would
        duplicate the mails that already went out)."""
        real_send = EmailMultiAlternatives.send
        calls = {"n": 0}

        def flaky_send(msg, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("mailbox unavailable")
            return real_send(msg, *args, **kwargs)

        with mock.patch.object(EmailMultiAlternatives, "send", flaky_send):
            with self.assertLogs("policies.tasks", level="WARNING"):
                send_acknowledgement_emails(self.ack_pks, "assigned")
        self.assertEqual(len(mail.outbox), len(self.acks) - 1)
