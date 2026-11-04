"""Org chart — the reporting tree, built live from ``JobInfo.reports_to``.

Nothing here is stored. Every call to :func:`build_org_tree` reads the
active employees and their ``reports_to`` pointers as they are *right now*
and assembles the forest in Python (two queries, ~1,500 rows), so a manager
change made in the Job Info drawer is on the chart on the next request.

Shape of the data (dev snapshot, 2026-10-11): ~1,470 active employees, no
cycles, six levels deep, three independent trees (nobody above the C-level
is recorded), spans of control up to 34. Two consequences for the page:

* it is a **forest**, so the view offers one root per top-level leader
  rather than inventing a company node;
* fan-out is wide at the bottom, so the template lays managers side by
  side and stacks individual contributors in a column under their manager
  (``_org_children.html``) instead of one endless row.

Cycle safety: ``reports_to`` is free-form, so a loop (A → B → A) is
possible in principle. Subtree sizes are computed iteratively over an
explicit ``seen`` set and the root detection treats any node whose chain
never reaches a root as a root itself, so a bad edit degrades to a
detached subtree in the "unlinked" list instead of a recursion error.
Spec: docs/PLAN.md "Org chart".
"""

from __future__ import annotations

import zlib
from collections import deque
from dataclasses import dataclass, field

from accounts.models import CustomUser


@dataclass
class OrgNode:
    user: CustomUser
    parent_id: object = None
    children: list["OrgNode"] = field(default_factory=list)
    #: Everyone below this node, recursively (not counting the node itself).
    team_size: int = 0
    #: True when ``reports_to`` points at someone who is no longer active.
    manager_inactive: bool = False
    #: True when the employee has no JobInfo row at all (bulk-created shell).
    missing_job_info: bool = False
    #: Render flags set per request by the view (never persisted).
    open: bool = False
    focused: bool = False
    is_me: bool = False
    #: 0–7, stable per department name — picks the avatar colour.
    hue: int = 0

    @property
    def pk(self):
        return self.user.pk

    @property
    def direct_count(self) -> int:
        return len(self.children)

    @property
    def managers(self) -> list["OrgNode"]:
        """Children who themselves have reports — laid out side by side."""
        return [c for c in self.children if c.children]

    @property
    def individuals(self) -> list["OrgNode"]:
        """Children with no reports — stacked in a column."""
        return [c for c in self.children if not c.children]

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


@dataclass
class OrgTree:
    nodes: dict = field(default_factory=dict)
    #: Top-level nodes that lead a team, largest first.
    roots: list = field(default_factory=list)
    #: Top-level nodes with nobody under them — outside every tree.
    unlinked: list = field(default_factory=list)

    # --- lookups ----------------------------------------------------------

    def get(self, pk) -> OrgNode | None:
        return self.nodes.get(pk)

    def ancestors(self, pk) -> list:
        """PKs from the node's manager up to its root, nearest first."""
        out, seen = [], set()
        node = self.nodes.get(pk)
        while node is not None and node.parent_id is not None and node.parent_id not in seen:
            seen.add(node.parent_id)
            out.append(node.parent_id)
            node = self.nodes.get(node.parent_id)
        return out

    def root_of(self, pk):
        chain = self.ancestors(pk)
        return chain[-1] if chain else pk

    def depth_of(self, pk) -> int:
        return len(self.ancestors(pk))

    def expanded_to_level(self, root_pk, levels: int) -> set:
        """PKs whose children render open when the chart shows ``levels``
        tiers below ``root_pk`` (``levels=1`` opens the root only)."""
        opened = set()
        if levels <= 0:
            return opened
        frontier = deque([(root_pk, 0)])
        while frontier:
            pk, d = frontier.popleft()
            node = self.nodes.get(pk)
            if node is None or not node.children or d >= levels:
                continue
            opened.add(pk)
            frontier.extend((c.pk, d + 1) for c in node.children)
        return opened

    def search(self, term: str, limit: int = 12) -> list:
        """Name / designation substring or exact employee ID, best-first."""
        term = (term or "").strip().lower()
        if len(term) < 2:
            return []
        exact, starts, contains = [], [], []
        for node in self.nodes.values():
            u = node.user
            name = u.display_name.lower()
            eid = (u.employee_id or "").lower()
            title = str(u.designation).lower() if u.designation_id else ""
            if eid == term or name == term:
                exact.append(node)
            elif name.startswith(term):
                starts.append(node)
            elif term in name or term in title or (u.email or "").lower().startswith(term):
                contains.append(node)
        key = lambda n: (-n.team_size, n.user.display_name)  # noqa: E731
        return (sorted(exact, key=key) + sorted(starts, key=key) + sorted(contains, key=key))[:limit]

    # --- data-quality counters (HR strip) ---------------------------------

    @property
    def manager_inactive_nodes(self) -> list:
        return sorted(
            (n for n in self.nodes.values() if n.manager_inactive),
            key=lambda n: n.user.display_name,
        )

    @property
    def missing_job_info_nodes(self) -> list:
        return sorted(
            (n for n in self.nodes.values() if n.missing_job_info),
            key=lambda n: n.user.display_name,
        )

    def wide_spans(self, threshold: int = 20) -> list:
        return sorted(
            (n for n in self.nodes.values() if n.direct_count >= threshold),
            key=lambda n: -n.direct_count,
        )

    @property
    def linked_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.parent_id is not None)

    @property
    def max_depth(self) -> int:
        """Number of tiers in the deepest tree (a lone root counts as 1)."""
        best = 0
        for root in self.roots:
            stack = [(root, 1)]
            while stack:
                node, d = stack.pop()
                best = max(best, d)
                stack.extend((c, d + 1) for c in node.children)
        return best


def _sort_key(node: OrgNode):
    return (-node.team_size, node.user.display_name)


def build_org_tree() -> OrgTree:
    """Assemble the live reporting forest for every active employee."""
    from employees.models import JobInfo

    users = (
        CustomUser.objects.filter(is_active=True)
        .select_related("department", "designation", "team")
    )
    tree = OrgTree()
    for u in users:
        dept = u.department.name if u.department_id else ""
        tree.nodes[u.pk] = OrgNode(
            user=u, missing_job_info=True, hue=zlib.crc32(dept.encode()) % 8,
        )

    job_rows = JobInfo.objects.filter(employee__is_active=True).values_list(
        "employee_id", "reports_to_id",
    )
    for employee_id, manager_id in job_rows:
        node = tree.nodes.get(employee_id)
        if node is None:
            continue
        node.missing_job_info = False
        if manager_id is None or manager_id == employee_id:
            continue
        if manager_id in tree.nodes:
            node.parent_id = manager_id
        else:
            # Points at a separated/deleted user: a top-level node for the
            # chart, flagged for HR rather than silently re-parented.
            node.manager_inactive = True

    # Break any cycle: walk each node's chain; a chain that revisits itself
    # has its last edge cut so the member nearest the loop becomes a root.
    for pk, node in tree.nodes.items():
        seen = {pk}
        cur = node
        while cur.parent_id is not None:
            if cur.parent_id in seen:
                cur.parent_id = None
                break
            seen.add(cur.parent_id)
            cur = tree.nodes[cur.parent_id]

    for node in tree.nodes.values():
        if node.parent_id is not None:
            tree.nodes[node.parent_id].children.append(node)

    # Subtree sizes bottom-up: process nodes in decreasing depth order.
    by_depth = sorted(tree.nodes.values(), key=lambda n: tree.depth_of(n.pk), reverse=True)
    for node in by_depth:
        node.team_size = sum(c.team_size + 1 for c in node.children)
        node.children.sort(key=_sort_key)

    tops = [n for n in tree.nodes.values() if n.parent_id is None]
    tree.roots = sorted((n for n in tops if n.children), key=_sort_key)
    tree.unlinked = sorted((n for n in tops if not n.children), key=lambda n: n.user.display_name)
    return tree

