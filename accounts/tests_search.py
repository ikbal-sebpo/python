"""The open employee search box (`accounts.filters.employee_search_q`).

Employee ID and email are unique, so every search in the app matches them
exactly — "539" must never land on "1539" — and only names are a substring
search. One helper owns that rule; the guard test below fails on any view
that hand-rolls a partial ID/email match instead of calling it.
"""

from __future__ import annotations

import pathlib
import re

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounts.filters import employee_search_q

User = get_user_model()


def _user(email, employee_id, first_name="Qx", last_name="Qx"):
    return User.objects.create_user(
        email=email, username=email, password="x",
        first_name=first_name, last_name=last_name, employee_id=employee_id,
    )


class EmployeeSearchQTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.exact = _user("ab@example.com", "539")
        cls.near = _user("abc@example.com", "1539")
        cls.also_near = _user("xab@example.com", "5390")
        cls.named = _user("zz@example.com", "777", first_name="Rahim", last_name="Karim")

    def _hits(self, q, prefix=""):
        return set(User.objects.filter(employee_search_q(q, prefix=prefix)))

    def test_blank_is_no_filter(self):
        self.assertIsNone(employee_search_q("   "))

    def test_employee_id_is_exact(self):
        self.assertEqual(self._hits("539"), {self.exact})

    def test_email_matches_full_address_or_local_part_only(self):
        self.assertEqual(self._hits("AB@example.com"), {self.exact})
        self.assertEqual(self._hits("ab"), {self.exact})
        self.assertEqual(self._hits("example.com"), set())

    def test_name_stays_a_substring_search(self):
        self.assertEqual(self._hits("ahi"), {self.named})
        self.assertEqual(self._hits("kar"), {self.named})

    def test_prefix_reaches_through_a_foreign_key(self):
        from employees.models import JobInfo

        JobInfo.objects.create(employee=self.exact)
        JobInfo.objects.create(employee=self.near)
        hits = JobInfo.objects.filter(employee_search_q("539", prefix="employee__"))
        self.assertEqual([j.employee for j in hits], [self.exact])


class SearchGuardTests(SimpleTestCase):
    # A substring lookup on a unique identity field — `[<prefix>__]employee_id`,
    # `email`, `username` or a run's `employee_code` snapshot.
    PATTERN = re.compile(
        r"\b(?:\w+__)?(?:employee_id|email|username|employee_code)__i?contains\b"
    )

    def test_no_partial_match_on_employee_id_or_email(self):
        """Use `employee_search_q(q, prefix=...)` (OR it with any extra
        non-identity fields) instead of a hand-rolled icontains — that copy
        is what let the roster keep matching "539" to "1539"."""
        root = pathlib.Path(settings.BASE_DIR).resolve()
        hits = []
        for config in apps.get_app_configs():
            app_dir = pathlib.Path(config.path).resolve()
            if root not in app_dir.parents or "venv" in app_dir.parts:
                continue
            for path in app_dir.rglob("*.py"):
                if "migrations" in path.parts or path.name.startswith("tests"):
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if self.PATTERN.search(line):
                        hits.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        self.assertEqual(hits, [])
