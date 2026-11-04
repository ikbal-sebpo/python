"""Upload Center — one page listing every payroll bulk uploader the viewer
can use. Added 2026-09-21, replacing the standalone "Upload salary" submenu.

Per-card visibility mirrors each destination's own real gate (has_managed_scope
for Overtime/SLA/Test Salary, the payroll_salary_upload ProtectedView row for
Gross Salary) rather than the broad ProtectedView group list every uploader
shares — these tests exist specifically to catch a card that would 403 on click.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from dynamic_menu.models import ProtectedView
from employees.models import JobInfo


User = get_user_model()


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test", **extra,
    )


class UploadCenterViewTests(TestCase):
    def setUp(self):
        for name in ("Supervisors", "Finance", "HR"):
            Group.objects.get_or_create(name=name)
        self.finance_group = Group.objects.get(name="Finance")
        self.supervisors_group = Group.objects.get(name="Supervisors")

        pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_upload_center", defaults={"registered_app": "payroll"},
        )
        pv.allowed_groups.set([self.supervisors_group, self.finance_group])

        salary_pv, _ = ProtectedView.objects.get_or_create(
            view_name="payroll_salary_upload", defaults={"registered_app": "payroll"},
        )
        salary_pv.allowed_groups.set([self.finance_group])

        self.supervisor = _user("sup@example.com", employee_id="1")
        self.supervisor.groups.add(self.supervisors_group)
        JobInfo.objects.create(employee=self.supervisor)
        self.reportee = _user("report@example.com", employee_id="2")
        JobInfo.objects.create(employee=self.reportee, reports_to=self.supervisor)

        self.finance_user = _user("fin@example.com", employee_id="3")
        self.finance_user.groups.add(self.finance_group)
        JobInfo.objects.create(employee=self.finance_user)

    def test_supervisor_with_reports_sees_managed_scope_uploaders_not_gross_salary(self):
        self.client.force_login(self.supervisor)
        resp = self.client.get("/payroll/uploads/")
        self.assertEqual(resp.status_code, 200)
        titles = [e.title for e in resp.context["entries"]]
        self.assertEqual(titles, ["Overtime", "SLA achievement", "Test salary"])

    def test_finance_user_with_no_reports_sees_only_gross_salary(self):
        self.client.force_login(self.finance_user)
        resp = self.client.get("/payroll/uploads/")
        self.assertEqual(resp.status_code, 200)
        titles = [e.title for e in resp.context["entries"]]
        self.assertEqual(titles, ["Gross salary"])

    def test_a_user_in_no_relevant_group_is_denied_the_page_itself(self):
        outsider = _user("out@example.com", employee_id="4")
        JobInfo.objects.create(employee=outsider)
        self.client.force_login(outsider)
        resp = self.client.get("/payroll/uploads/")
        self.assertRedirects(resp, "/")

    def test_every_card_links_resolve(self):
        """Guards against a template `{% url %}` typo breaking the whole page."""
        self.supervisor.groups.add(self.finance_group)
        self.client.force_login(self.supervisor)
        resp = self.client.get("/payroll/uploads/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["entries"]), 4)
