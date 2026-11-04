"""Readiness check for the overtime review -> approve chain
(docs/OVERTIME_APPROVAL_CHAIN_PLAN.md, 2026-10-17).

Every overtime entry now waits on two people: a Manager-tier reviewer in the
employee's managed scope and the department head (or a department additional
manager) to approve. A department with nobody in either role doesn't fail
loudly — its overtime just sits unapproved and is never paid, which is the
exact silent non-payment Q55 was built to stop. So before the chain goes
live (and whenever the org changes) this lists:

1. Departments that filed overtime in the last ``--days`` (default 400) and
   have **no head and no additional managers** — nothing approves there
   except HR-Payroll / Payroll Admins by hand.
2. Teams in those departments where **no Manager-tier person** sits above
   any member in the reports-to chain — their entries can only be approved
   straight from Pending by the head (fine) or wait on nobody (not fine if
   the head is also missing).
3. Active employees whose Designation has no AccessLevel mapped, since the
   review tier is read off it.

Read-only. Exit status 1 when anything in group 1 is found, so a deploy
step can gate on it; the other groups are advisory.

Usage:
    python manage.py check_overtime_approvers
    python manage.py check_overtime_approvers --days 120 --all-departments
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import CustomUser, Department
from accounts.permissions import meets_overtime_review_tier
from employees.models import JobInfo
from payroll.models import OvertimeEntry


class Command(BaseCommand):
    help = "List departments/teams where the overtime review chain has nobody to act."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=400,
                            help="Only departments with overtime filed this many days back (default 400).")
        parser.add_argument("--all-departments", action="store_true",
                            help="Check every active department, not just those with recent overtime.")

    def handle(self, *args, **opts):
        since = timezone.localdate() - timedelta(days=opts["days"])
        if opts["all_departments"]:
            dept_ids = set(Department.objects.filter(is_active=True).values_list("pk", flat=True))
        else:
            dept_ids = set(
                OvertimeEntry.objects.filter(work_date__gte=since)
                .values_list("employee__department_id", flat=True)
            )
            dept_ids.discard(None)
        departments = (
            Department.objects.filter(pk__in=dept_ids)
            .select_related("head_of_department").prefetch_related("additional_managers")
            .order_by("name")
        )

        # Reporting chain, once.
        parent = dict(
            JobInfo.objects.filter(employee__is_active=True, reports_to__isnull=False)
            .values_list("employee_id", "reports_to_id")
        )
        people = {
            u.pk: u for u in CustomUser.objects.filter(is_active=True)
            .select_related("designation__access_level", "access_level", "team", "department")
        }
        reviewer_cache: dict = {}

        def is_reviewer(pk) -> bool:
            if pk not in reviewer_cache:
                u = people.get(pk)
                reviewer_cache[pk] = bool(u and meets_overtime_review_tier(u))
            return reviewer_cache[pk]

        def has_reviewer_above(pk) -> bool:
            seen = set()
            cur = parent.get(pk)
            while cur is not None and cur not in seen:
                if is_reviewer(cur):
                    return True
                seen.add(cur)
                cur = parent.get(cur)
            return False

        no_approver = []
        for dept in departments:
            head = dept.head_of_department
            extra = [m for m in dept.additional_managers.all() if m.is_active]
            if (head is None or not head.is_active) and not extra:
                no_approver.append(dept)
            names = ([head.display_name + " (head)"] if head and head.is_active else [])
            names += [m.display_name for m in extra]
            approvers = ", ".join(names) or "NOBODY"
            self.stdout.write(f"{dept.name}: approvers = {approvers}")

            members = [u for u in people.values() if u.department_id == dept.pk]
            stranded: dict[str, int] = {}
            for u in members:
                if not has_reviewer_above(u.pk):
                    key = u.team.name if u.team else "(no team)"
                    stranded[key] = stranded.get(key, 0) + 1
            for team, n in sorted(stranded.items()):
                self.stdout.write(f"    no Manager-tier reviewer above {n} member(s) of {team}")

        unmapped = [
            u for u in people.values()
            if u.designation_id and u.designation.access_level_id is None
        ]
        if unmapped:
            names = sorted({u.designation.name for u in unmapped})
            self.stdout.write(self.style.WARNING(
                f"\n{len(unmapped)} active employee(s) hold a designation with no access level "
                f"(review tier unknown): {', '.join(names)}"
            ))

        if no_approver:
            self.stdout.write(self.style.ERROR(
                "\nNo head or additional manager — overtime here can only be approved by "
                "HR-Payroll / Payroll Admins by hand:\n  "
                + "\n  ".join(d.name for d in no_approver)
            ))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nEvery checked department has an approver."))
