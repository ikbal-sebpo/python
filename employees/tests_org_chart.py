"""Org chart — the live reporting forest (employees/org_chart.py) and its
page/fragment views (docs/PLAN.md "Org chart", 2026-10-11)."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from accounts.models import Department, Designation
from employees.models import JobInfo
from employees.org_chart import build_org_tree

User = get_user_model()


def _user(email, first, last="Test", **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=first, last_name=last, **extra,
    )


def _reports(employee, manager):
    JobInfo.objects.update_or_create(employee=employee, defaults={"reports_to": manager})


class OrgTreeBuildTests(TestCase):
    """
    ceo
    ├── ops   (3 below: lead, ic1, ic2)
    │   └── lead
    │       ├── ic1
    │       └── ic2
    └── hr
    loner      (JobInfo, no manager, no reports → unlinked)
    orphan     (reports_to a deactivated user → top-level, flagged)
    shell      (no JobInfo row at all → unlinked, flagged)
    """

    @classmethod
    def setUpTestData(cls):
        cls.ceo = _user("ceo@x.com", "Ceo")
        cls.ops = _user("ops@x.com", "Ops")
        cls.hr = _user("hr@x.com", "Hr")
        cls.lead = _user("lead@x.com", "Lead")
        cls.ic1 = _user("ic1@x.com", "Ic1")
        cls.ic2 = _user("ic2@x.com", "Ic2")
        cls.loner = _user("loner@x.com", "Loner")
        cls.orphan = _user("orphan@x.com", "Orphan")
        cls.shell = _user("shell@x.com", "Shell")
        cls.gone = _user("gone@x.com", "Gone", is_active=False)
        _reports(cls.ceo, None)
        _reports(cls.ops, cls.ceo)
        _reports(cls.hr, cls.ceo)
        _reports(cls.lead, cls.ops)
        _reports(cls.ic1, cls.lead)
        _reports(cls.ic2, cls.lead)
        _reports(cls.loner, None)
        _reports(cls.orphan, cls.gone)
        _reports(cls.gone, cls.ceo)   # inactive — must not appear anywhere

    def test_roots_sizes_and_order(self):
        tree = build_org_tree()
        self.assertEqual([n.pk for n in tree.roots], [self.ceo.pk])
        ceo = tree.get(self.ceo.pk)
        self.assertEqual(ceo.team_size, 5)
        self.assertEqual(ceo.direct_count, 2)
        # Larger subtree first, then name.
        self.assertEqual([c.pk for c in ceo.children], [self.ops.pk, self.hr.pk])
        self.assertEqual(tree.get(self.ops.pk).team_size, 3)
        self.assertEqual(tree.get(self.lead.pk).team_size, 2)
        self.assertNotIn(self.gone.pk, tree.nodes)

    def test_managers_and_individuals_split(self):
        tree = build_org_tree()
        ceo = tree.get(self.ceo.pk)
        self.assertEqual([c.pk for c in ceo.managers], [self.ops.pk])
        self.assertEqual([c.pk for c in ceo.individuals], [self.hr.pk])

    def test_unlinked_and_flags(self):
        tree = build_org_tree()
        unlinked = {n.pk for n in tree.unlinked}
        self.assertEqual(unlinked, {self.loner.pk, self.orphan.pk, self.shell.pk})
        self.assertTrue(tree.get(self.orphan.pk).manager_inactive)
        self.assertTrue(tree.get(self.shell.pk).missing_job_info)
        self.assertFalse(tree.get(self.loner.pk).missing_job_info)
        self.assertEqual([n.pk for n in tree.manager_inactive_nodes], [self.orphan.pk])
        self.assertEqual([n.pk for n in tree.missing_job_info_nodes], [self.shell.pk])

    def test_ancestors_root_of_and_levels(self):
        tree = build_org_tree()
        self.assertEqual(tree.ancestors(self.ic1.pk), [self.lead.pk, self.ops.pk, self.ceo.pk])
        self.assertEqual(tree.root_of(self.ic1.pk), self.ceo.pk)
        self.assertEqual(tree.root_of(self.ceo.pk), self.ceo.pk)
        self.assertEqual(tree.max_depth, 4)
        self.assertEqual(tree.expanded_to_level(self.ceo.pk, 1), {self.ceo.pk})
        self.assertEqual(tree.expanded_to_level(self.ceo.pk, 2), {self.ceo.pk, self.ops.pk})
        self.assertEqual(
            tree.expanded_to_level(self.ceo.pk, 99),
            {self.ceo.pk, self.ops.pk, self.lead.pk},
        )

    def test_cycle_is_cut_not_fatal(self):
        _reports(self.ceo, self.ic1)   # ceo → ic1 → lead → ops → ceo
        tree = build_org_tree()
        self.assertEqual(sum(n.team_size for n in tree.roots) + len(tree.roots)
                         + len(tree.unlinked), len(tree.nodes))
        for pk in tree.nodes:
            tree.ancestors(pk)   # terminates
        self.assertTrue(tree.roots)

    def test_search(self):
        self.ic1.employee_id = "E-77"
        self.ic1.designation = Designation.objects.create(name="Senior Analyst")
        self.ic1.save()
        tree = build_org_tree()
        self.assertEqual([n.pk for n in tree.search("e-77")], [self.ic1.pk])
        self.assertEqual(tree.search("Ic")[0].pk, self.ic1.pk)
        self.assertIn(self.ic1.pk, [n.pk for n in tree.search("analyst")])
        self.assertEqual(tree.search("z"), [])


class OrgChartViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="HR")
        cls.dept = Department.objects.create(name="Ops")
        cls.ceo = _user("ceo@x.com", "Ceo", department=cls.dept)
        cls.mgr = _user("mgr@x.com", "Mgr", department=cls.dept)
        cls.ic = _user("ic@x.com", "Ic", department=cls.dept)
        cls.other = _user("other@x.com", "Other", department=cls.dept)
        cls.loner = _user("loner@x.com", "Loner")
        _reports(cls.ceo, None)
        _reports(cls.mgr, cls.ceo)
        _reports(cls.ic, cls.mgr)
        _reports(cls.other, cls.ceo)
        _reports(cls.loner, None)
        cls.url = reverse("employee_org_chart")

    def test_login_required(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)

    def test_plain_employee_sees_whole_tree_but_links_only_own_scope(self):
        self.client.force_login(self.ic)
        resp = self.client.get(self.url, {"levels": 99})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # Company-wide cards (names/titles) …
        for u in (self.ceo, self.mgr, self.other):
            self.assertIn(u.display_name, body)
        # … but a profile link only for people inside the viewer's scope.
        self.assertIn(reverse("employee_detail", args=[self.ic.pk]), body)
        self.assertNotIn(reverse("employee_detail", args=[self.ceo.pk]), body)
        self.assertNotIn("Data quality (HR)", body)

    def test_default_root_and_levels(self):
        self.client.force_login(self.ceo)
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["root"].pk, self.ceo.pk)
        self.assertEqual(resp.context["levels"], 1)
        self.assertTrue(resp.context["tree"].get(self.ceo.pk).open)
        self.assertFalse(resp.context["tree"].get(self.mgr.pk).open)
        resp = self.client.get(self.url, {"levels": "2"})
        self.assertTrue(resp.context["tree"].get(self.mgr.pk).open)
        # A nonsense level falls back to the default.
        resp = self.client.get(self.url, {"levels": "7"})
        self.assertEqual(resp.context["levels"], 1)

    def test_focus_expands_path_and_highlights(self):
        self.client.force_login(self.other)
        resp = self.client.get(self.url, {"focus": str(self.ic.pk)})
        tree = resp.context["tree"]
        self.assertEqual(resp.context["root"].pk, self.ceo.pk)
        self.assertTrue(tree.get(self.ceo.pk).open)
        self.assertTrue(tree.get(self.mgr.pk).open)
        self.assertTrue(tree.get(self.ic.pk).focused)
        self.assertIn("org-card--focus", resp.content.decode())

    def test_search_single_hit_focuses_many_hits_list(self):
        self.client.force_login(self.ceo)
        resp = self.client.get(self.url, {"q": "mgr"})
        self.assertEqual(resp.context["focus"].pk, self.mgr.pk)
        resp = self.client.get(self.url, {"q": "test"})   # every last name
        self.assertIsNone(resp.context["focus"])
        self.assertGreater(len(resp.context["results"]), 1)
        resp = self.client.get(self.url, {"q": "zzzz"})
        self.assertEqual(resp.context["results"], [])
        self.assertContains(resp, "No active employee matches")

    def test_unlinked_mode(self):
        self.client.force_login(self.ceo)
        resp = self.client.get(self.url, {"root": "unlinked"})
        self.assertEqual(resp.context["mode"], "unlinked")
        self.assertIsNone(resp.context["root"])
        self.assertContains(resp, self.loner.display_name)
        # Focusing an unlinked person lands in the same mode.
        resp = self.client.get(self.url, {"focus": str(self.loner.pk)})
        self.assertEqual(resp.context["mode"], "unlinked")

    def test_bad_root_falls_back_to_largest_tree(self):
        self.client.force_login(self.ceo)
        resp = self.client.get(self.url, {"root": "not-a-uuid"})
        self.assertEqual(resp.context["root"].pk, self.ceo.pk)
        resp = self.client.get(self.url, {"root": str(self.ic.pk)})   # a leaf, not a root
        self.assertEqual(resp.context["root"].pk, self.ceo.pk)

    def test_hr_sees_data_quality_strip(self):
        self.ceo.groups.add(Group.objects.get(name="HR"))
        self.client.force_login(self.ceo)
        resp = self.client.get(self.url)
        self.assertContains(resp, "Data quality (HR)")
        self.assertContains(resp, self.loner.display_name)

    def test_node_fragment(self):
        self.client.force_login(self.ic)
        url = reverse("employee_org_chart_node", args=[self.mgr.pk])
        resp = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "employees/_org_children.html")
        self.assertContains(resp, self.ic.display_name)
        self.assertNotContains(resp, "<html")
        # Unknown / inactive person → 404.
        gone = _user("gone@x.com", "Gone", is_active=False)
        resp = self.client.get(reverse("employee_org_chart_node", args=[gone.pk]))
        self.assertEqual(resp.status_code, 404)
