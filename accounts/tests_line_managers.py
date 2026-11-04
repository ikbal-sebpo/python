"""Line Managers group + the menu-ACL invariant that made it necessary."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase

from accounts.services import LINE_MANAGERS_GROUP, sync_line_managers
from employees.models import JobInfo

User = get_user_model()


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="T", **extra,
    )


class LineManagerSyncTests(TestCase):

    def setUp(self):
        self.manager = _user("mgr@example.com")
        self.report = _user("rep@example.com")

    def _group(self):
        return Group.objects.get(name=LINE_MANAGERS_GROUP)

    def test_signal_adds_manager_immediately(self):
        JobInfo.objects.create(employee=self.report, reports_to=self.manager)
        self.assertIn(self.manager, self._group().user_set.all())

    def test_reassigning_removes_the_old_manager(self):
        """The pre_save stash exists for this: when reports_to moves A→B,
        A may have just lost their last report."""
        other = _user("mgr2@example.com")
        ji = JobInfo.objects.create(employee=self.report, reports_to=self.manager)
        ji.reports_to = other
        ji.save()
        members = self._group().user_set.all()
        self.assertIn(other, members)
        self.assertNotIn(self.manager, members)

    def test_deleting_jobinfo_removes_manager(self):
        ji = JobInfo.objects.create(employee=self.report, reports_to=self.manager)
        ji.delete()
        self.assertNotIn(self.manager, self._group().user_set.all())

    def test_manager_of_only_inactive_reports_is_not_a_line_manager(self):
        JobInfo.objects.create(employee=self.report, reports_to=self.manager)
        self.report.is_active = False
        self.report.save(update_fields=["is_active"])
        sync_line_managers()
        self.assertNotIn(self.manager, self._group().user_set.all())

    def test_full_sync_removes_hand_added_members(self):
        """Membership is derived — anything added by hand is transient."""
        interloper = _user("nobody@example.com")
        Group.objects.get_or_create(name=LINE_MANAGERS_GROUP)[0].user_set.add(interloper)
        result = sync_line_managers()
        self.assertIn(interloper.pk, result["removed"])
        self.assertNotIn(interloper, self._group().user_set.all())

    def test_dry_run_writes_nothing(self):
        JobInfo.objects.create(employee=self.report, reports_to=self.manager)
        self._group().user_set.clear()
        result = sync_line_managers(dry_run=True)
        self.assertIn(self.manager.pk, result["added"])
        self.assertNotIn(self.manager, self._group().user_set.all())

    def test_command_runs(self):
        JobInfo.objects.create(employee=self.report, reports_to=self.manager)
        call_command("sync_line_managers", "--dry-run")


class MenuAclInvariantTests(TestCase):
    """A section-header main menu (blank url) only renders submenus for
    viewers who can already see the parent, so granting a submenu to a group
    the parent doesn't grant is a silent no-op. This is exactly how the
    Offboarding section became invisible to All Employees."""

    def test_section_headers_are_supersets_of_their_submenus(self):
        """main_menu_groups() derives the parent grant, so this should hold
        by construction — the test pins that the derivation is actually
        applied and nobody has gone back to hand-listing parent groups."""
        from accounts.management.commands.seed_orbis import (
            ORBIS_MAIN_MENUS, ORBIS_SUB_MENUS, main_menu_groups,
        )
        main_groups = {
            title: set(main_menu_groups(title, groups))
            for title, _i, _u, _r, groups in ORBIS_MAIN_MENUS
        }
        offenders = []
        for main_title, sub_title, _url, _rank, sub_groups in ORBIS_SUB_MENUS:
            parent = main_groups.get(main_title)
            if parent is None:
                continue
            missing = set(sub_groups) - parent
            if missing:
                offenders.append(f"{main_title} → {sub_title}: {sorted(missing)}")
        self.assertEqual(
            offenders, [],
            "submenu groups not granted on the parent menu render for nobody:\n"
            + "\n".join(offenders),
        )


    def test_every_section_except_users_is_open_to_all_staff(self):
        """Policy: main menus are visible to everyone; what you can DO is
        expressed on the submenus. Users is the one restricted section."""
        from accounts.management.commands.seed_orbis import (
            ORBIS_MAIN_MENUS, main_menu_groups,
        )
        # Users (account administration) and Audit (the access log, 2026-10-09)
        # are the two restricted sections: neither has any self-service
        # content for ordinary staff.
        restricted = {"Users", "Audit"}
        for title, _i, _u, _r, base in ORBIS_MAIN_MENUS:
            groups = main_menu_groups(title, base)
            if title in restricted:
                self.assertNotIn("All Employees", groups)
            else:
                self.assertIn("All Employees", groups,
                              f"{title} should be visible to all staff")

    def test_no_section_header_is_empty_for_a_plain_employee(self):
        """A blank-url main menu renders as an expandable section
        (sidebar.html:25). If none of its submenus is granted to
        All Employees the section opens onto nothing — an icon that does
        nothing. Every section must offer at least one thing to everyone."""
        from accounts.management.commands.seed_orbis import (
            ORBIS_MAIN_MENUS, ORBIS_SUB_MENUS, main_menu_groups,
        )
        # Only sections whose header ordinary staff can see: a section
        # restricted at the main-menu level (Audit) never renders for them.
        section_titles = [
            t for t, _i, url, _r, base in ORBIS_MAIN_MENUS
            if not url and "All Employees" in main_menu_groups(t, base)
        ]
        for title in section_titles:
            visible = [
                sub for main, sub, _u, _r, groups in ORBIS_SUB_MENUS
                if main == title and "All Employees" in groups
            ]
            self.assertTrue(
                visible,
                f"section '{title}' has no submenu visible to All Employees, "
                f"so it renders as an empty flyout for ordinary staff",
            )

    def test_no_two_submenus_in_a_section_point_at_the_same_route(self):
        """ORBIS_SUB_MENUS is keyed on (main_menu, title), so giving an entry
        a different title from the row already in the DB creates a SECOND
        menu item for the same route instead of updating the first. That is
        how "Leave list" appeared alongside "Leaves" — same URL, two rows."""
        from collections import Counter

        from accounts.management.commands.seed_orbis import ORBIS_SUB_MENUS

        by_section = {}
        for main, _sub, url, _rank, _groups in ORBIS_SUB_MENUS:
            by_section.setdefault(main, []).append(url)
        for section, urls in by_section.items():
            dupes = [u for u, n in Counter(urls).items() if n > 1]
            self.assertEqual(
                dupes, [],
                f"section '{section}' lists the same route twice: {dupes}",
            )

    def test_submenu_grants_do_not_exceed_their_protected_view(self):
        """A menu item you can see but cannot open redirects you home. For
        every view whose ACL we declare, the submenu's groups must be a
        subset — otherwise someone gets a dead link. "All Employees" is
        universal (accounts.signals auto-adds everyone), so a view granting
        it admits any menu grant."""
        from accounts.management.commands.seed_orbis import (
            ORBIS_SUB_MENUS, PROTECTED_VIEW_GROUPS,
        )
        for main, sub, url, _rank, menu_groups in ORBIS_SUB_MENUS:
            view_groups = PROTECTED_VIEW_GROUPS.get(url)
            if view_groups is None or "All Employees" in view_groups:
                continue
            extra = set(menu_groups) - set(view_groups)
            self.assertEqual(
                extra, set(),
                f"'{main} > {sub}' is shown to {sorted(extra)} but "
                f"{url} does not admit them - they would be redirected home",
            )
