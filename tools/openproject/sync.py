#!/usr/bin/env python
"""Make op.sebpo.net/projects/orbis match tools/openproject/inventory.yaml.

    python tools/openproject/sync.py --login          # once: sign in, save session
    python tools/openproject/sync.py --check          # project setup + inventory sanity
    python tools/openproject/sync.py                  # dry-run: what would change
    python tools/openproject/sync.py --apply [--only theme]
    python tools/openproject/sync.py --wiki [--apply]
    python tools/openproject/sync.py --rebuild-state  # recover state.json from OpenProject

Never deletes anything. See docs/OPENPROJECT.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from session import (HERE, ApiError, OpenProject, SessionExpired, commit_owners, die, href,
                     load_config, load_inventory, login)

STATE_FILE = HERE / "state.json"
WIKI_DIR = HERE / "wiki"
KEY_RE = re.compile(r"orbis-key:\s*([\w.\-]+)")
TODAY = dt.date.today()


# ---------------------------------------------------------------- inventory
def validate(inv: list[dict], cfg: dict) -> list[str]:
    errs, keys = [], set()
    for e in inv:
        k = e.get("key")
        if not k:
            errs.append(f"entry without key: {e.get('subject')}")
            continue
        if k in keys:
            errs.append(f"duplicate key {k}")
        keys.add(k)
        if e.get("type") not in cfg["types"]:
            errs.append(f"{k}: unknown type {e.get('type')}")
        if e.get("status") not in cfg["statuses"]:
            errs.append(f"{k}: unknown status {e.get('status')}")
        s, f = e.get("start"), e.get("finish")
        if s and f and str(f) < str(s):
            errs.append(f"{k}: finish {f} before start {s}")
        if (e.get("type") not in ("Epic", "Milestone") and e.get("status") not in ("Closed", "On hold")
                and f and str(f) < str(TODAY)):
            errs.append(f"{k}: open item with finish {f} in the past")
        if e.get("category") and e["category"] not in cfg["categories"]:
            errs.append(f"{k}: category {e['category']} not in config.yaml")
    for e in inv:
        p = e.get("parent")
        if p and p not in keys:
            errs.append(f"{e['key']}: parent {p} does not exist")
    return errs


def check_commits(inv: list[dict]) -> list[str]:
    """Every non-merge commit belongs to exactly one inventory entry."""
    out = subprocess.run(["git", "-C", str(HERE.parents[1]), "log", "--no-merges", "--format=%h %s"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    owner: dict[str, list[str]] = {}
    for e in inv:
        for h in e.get("commits") or []:
            owner.setdefault(h[:7], []).append(e["key"])
    for h, k in commit_owners(inv).items():
        owner.setdefault(h, [k])
    errs = [f"commit {h} listed under {ks}" for h, ks in owner.items() if len(ks) > 1]
    known = set()
    for line in out:
        h, subj = line.split(" ", 1)
        known.add(h[:7])
        if h[:7] not in owner:
            errs.append(f"unassigned commit {h[:7]} {subj}")
    errs += [f"{ks[0]}: commit {h} not in git history" for h, ks in owner.items() if h not in known]
    return errs


def ordered(inv: list[dict]) -> list[dict]:
    """Parents before children."""
    by_key = {e["key"]: e for e in inv}
    depth = {}

    def d(k):
        if k not in depth:
            p = by_key[k].get("parent")
            depth[k] = 0 if not p else d(p) + 1
        return depth[k]

    return sorted(inv, key=lambda e: d(e["key"]))


def select(inv: list[dict], only: str | None) -> list[dict]:
    if not only:
        return inv
    by_key = {e["key"]: e for e in inv}
    picked = {e["key"] for e in inv if e["key"] == only or e["key"].startswith(only + ".")}
    for k in list(picked):  # pull in ancestors so parents exist
        p = by_key[k].get("parent")
        while p:
            picked.add(p)
            p = by_key[p].get("parent")
    return [e for e in inv if e["key"] in picked]


def render_description(e: dict) -> str:
    parts = []
    if e.get("description"):
        parts.append(e["description"].strip())
    if e.get("blocked_on"):
        parts.append(f"**Blocked on:** {e['blocked_on']}")
    if e.get("commits"):
        parts.append("**Commits:** " + " ".join(f"`{h}`" for h in e["commits"]))
    parts.append(f"---\norbis-key: {e['key']}")
    return "\n\n".join(parts)


def desired(e: dict, cfg: dict) -> dict:
    """The comparable shape of an entry (names, not ids)."""
    d = {
        "subject": e["subject"],
        "description": render_description(e),
        "type": cfg["types"][e["type"]],
        "status": cfg["statuses"][e["status"]],
        "category": cfg["categories"].get(e.get("category")) if e.get("category") else None,
        "parent": e.get("parent"),
    }
    if e["type"] == "Milestone":
        d["date"] = str(e.get("start") or e.get("finish") or "") or None
    else:
        d["startDate"] = str(e["start"]) if e.get("start") else None
        d["dueDate"] = str(e["finish"]) if e.get("finish") else None
    return d


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"work_packages": {}, "wiki": {}}


def save_state(state: dict) -> None:
    state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# ---------------------------------------------------------------- remote
class Remote:
    def __init__(self, op: OpenProject, cfg: dict):
        self.op, self.cfg = op, cfg
        proj = op.get(f"/api/v3/projects/{cfg['project']}")
        self.project_id = proj["id"]
        self.types = {t["name"]: t["id"] for t in op.collect(f"/api/v3/projects/{self.project_id}/types")}
        self.statuses = {s["name"]: s["id"] for s in op.collect("/api/v3/statuses")}
        try:
            cats = op.collect(f"/api/v3/projects/{self.project_id}/categories")
        except ApiError:
            cats = []
        self.categories = {c["name"]: c["id"] for c in cats}

    def all_work_packages(self) -> dict[int, dict]:
        flt = json.dumps([{"status": {"operator": "*", "values": []}}])
        wps = self.op.collect(f"/api/v3/projects/{self.project_id}/work_packages",
                              {"filters": flt, "sortBy": json.dumps([["id", "asc"]])})
        return {w["id"]: w for w in wps}


def snapshot(wp: dict, id_to_key: dict[int, str]) -> dict:
    """The comparable shape of a remote work package."""
    links = wp.get("_links", {})
    parent = links.get("parent", {}).get("href")
    pid = int(parent.rsplit("/", 1)[1]) if parent else None
    s = {
        "subject": wp.get("subject"),
        "description": (wp.get("description") or {}).get("raw") or "",
        "type": links.get("type", {}).get("title"),
        "status": links.get("status", {}).get("title"),
        "category": links.get("category", {}).get("title"),
        "parent": id_to_key.get(pid, pid),
    }
    if "date" in wp:
        s["date"] = wp.get("date")
    else:
        s["startDate"], s["dueDate"] = wp.get("startDate"), wp.get("dueDate")
    return s


def payload(d: dict, remote: Remote, state: dict, warn) -> dict:
    links = {"type": href("types", remote.types[d["type"]])}
    if d["category"]:
        if d["category"] in remote.categories:
            links["category"] = href("categories", remote.categories[d["category"]])
        else:
            warn(f"category {d['category']!r} missing in OpenProject — skipped")
    if d["parent"]:
        links["parent"] = href("work_packages", state["work_packages"][d["parent"]]["id"])
    else:
        links["parent"] = {"href": None}
    body = {"subject": d["subject"], "description": {"format": "markdown", "raw": d["description"]},
            "scheduleManually": True, "_links": links}
    if "date" in d:
        body["date"] = d["date"]
    else:
        body["startDate"], body["dueDate"] = d["startDate"], d["dueDate"]
    return body


# ---------------------------------------------------------------- commands
def cmd_check(cfg, inv):
    problems = validate(inv, cfg) + check_commits(inv)
    for p in problems:
        print("  ✗", p)
    print(f"Inventory: {len(inv)} entries, {len(problems)} problem(s).")
    try:
        with OpenProject(cfg) as op:
            print(f"Signed in as {op.me.get('name')}.")
            r = Remote(op, cfg)
            print(f"Project {cfg['project']!r} → id {r.project_id}.")
            bad = 0
            for kind, wanted, have in (("type", cfg["types"].values(), r.types),
                                       ("status", cfg["statuses"].values(), r.statuses),
                                       ("category", cfg["categories"].values(), r.categories)):
                missing = [w for w in wanted if w not in have]
                if missing:
                    bad += kind != "category"
                    hint = " (optional — entries just go uncategorised)" if kind == "category" else ""
                    print(f"  ✗ {kind}(s) not available in the project: {missing}{hint}")
                else:
                    print(f"  ✓ all {kind}s present")
            for path, label in ((f"/projects/{cfg['project']}/wiki", "Wiki module"),
                                (f"/projects/{cfg['project']}/cost_reports", "Time and costs module")):
                resp = op.ctx.request.get(f"{op.base}{path}", max_redirects=0)
                print(f"  {'✓' if resp.status < 400 else '✗'} {label} ({path} → {resp.status})")
            return 1 if (problems or bad) else 0
    except SessionExpired as e:
        print(f"OpenProject: {e}")
        return 1


def plan_changes(cfg, inv, state, remote_wps=None, id_to_key=None):
    """Return [(action, entry, desired, reason)]."""
    out = []
    for e in ordered(inv):
        d = desired(e, cfg)
        rec = state["work_packages"].get(e["key"])
        if not rec:
            out.append(("create", e, d, "new"))
            continue
        changed_here = rec.get("hash") != digest(d)
        drift = None
        if remote_wps is not None:
            wp = remote_wps.get(rec["id"])
            if wp is None:
                out.append(("missing", e, d, f"#{rec['id']} no longer in OpenProject"))
                continue
            now = snapshot(wp, id_to_key)
            if rec.get("remote") and now != rec["remote"]:
                drift = sorted(k for k in now if now[k] != rec["remote"].get(k))
        if changed_here and drift:
            out.append(("conflict", e, d, f"changed in inventory AND in OpenProject ({', '.join(drift)})"))
        elif changed_here:
            out.append(("update", e, d, "inventory changed"))
        elif drift:
            out.append(("drift", e, d, f"edited in OpenProject: {', '.join(drift)}"))
    return out


def cmd_sync(cfg, inv, args):
    errs = validate(inv, cfg)
    if errs:
        die("Inventory invalid:\n  " + "\n  ".join(errs))
    state = load_state()
    sel = select(inv, args.only)
    all_keys = {e["key"] for e in inv}
    orphans = [k for k in state["work_packages"] if k not in all_keys]

    op = None
    try:
        op = OpenProject(cfg).__enter__()
    except SessionExpired as e:
        if args.apply:
            die(str(e))
        print(f"(offline dry-run — {e} Comparing against state.json only.)\n")

    try:
        remote = remote_wps = id_to_key = None
        if op:
            remote = Remote(op, cfg)
            remote_wps = remote.all_work_packages()
            id_to_key = {v["id"]: k for k, v in state["work_packages"].items()}
        changes = plan_changes(cfg, sel, state, remote_wps, id_to_key)
        counts = {}
        for action, e, d, why in changes:
            counts[action] = counts.get(action, 0) + 1
            print(f"  {action:8} {e['key']:45} {e['subject'][:60]}  [{why}]")
        unchanged = len(sel) - len(changes)
        print(f"\n{len(sel)} entries: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
              + f", {unchanged} unchanged.")
        if orphans:
            print(f"Orphans (in state.json, removed from inventory — close them by hand): {orphans}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to write.")
            return 0

        warnings = []
        for action, e, d, why in changes:
            if action in ("conflict", "missing") or (action == "drift" and not args.overwrite):
                continue
            try:
                apply_one(op, remote, state, e, d, action, warnings.append, id_to_key)
                save_state(state)  # after every write, so a crash loses nothing
            except ApiError as err:
                print(f"  ✗ {e['key']}: {err}")
        for w in sorted(set(warnings)):
            print("  warning:", w)
        skipped = [c for c in changes if c[0] in ("conflict", "missing")
                   or (c[0] == "drift" and not args.overwrite)]
        if skipped:
            print(f"{len(skipped)} skipped (conflict / drift / missing) — resolve in the inventory, "
                  "or pass --overwrite to push the inventory over OpenProject edits.")
        return 0
    finally:
        if op:
            op.__exit__(None, None, None)


def apply_one(op, remote, state, e, d, action, warn, id_to_key):
    body = payload(d, remote, state, warn)
    status_link = href("statuses", remote.statuses[d["status"]])
    if action == "create":
        wp = op.post(f"/api/v3/projects/{remote.project_id}/work_packages", body)
        # Record it before anything else can fail, so a re-run never duplicates it.
        id_to_key[wp["id"]] = e["key"]
        state["work_packages"][e["key"]] = {"id": wp["id"], "hash": None,
                                            "remote": snapshot(wp, id_to_key)}
        save_state(state)
        print(f"  + #{wp['id']} {e['key']}")
    else:
        wid = state["work_packages"][e["key"]]["id"]
        wp = op.get(f"/api/v3/work_packages/{wid}")
        body["lockVersion"] = wp["lockVersion"]
        wp = op.patch(f"/api/v3/work_packages/{wid}", body)
        print(f"  ~ #{wid} {e['key']}")
    synced = True
    if wp["_links"]["status"]["title"] != d["status"]:
        try:
            wp = op.patch(f"/api/v3/work_packages/{wp['id']}",
                          {"lockVersion": wp["lockVersion"], "_links": {"status": status_link}})
        except ApiError as err:
            synced = False  # hash stays stale → the next run retries the status
            warn(f"status {d['status']!r} refused by the workflow "
                 f"(left at {wp['_links']['status']['title']!r}): {err}")
    id_to_key[wp["id"]] = e["key"]
    state["work_packages"][e["key"]] = {"id": wp["id"], "hash": digest(d) if synced else None,
                                        "remote": snapshot(wp, id_to_key)}


def cmd_rebuild(cfg, inv):
    state = load_state()
    with OpenProject(cfg) as op:
        remote = Remote(op, cfg)
        wps = remote.all_work_packages()
        found = {}
        for wid, wp in wps.items():
            m = KEY_RE.search((wp.get("description") or {}).get("raw") or "")
            if m:
                found[m.group(1)] = wid
        id_to_key = {v: k for k, v in found.items()}
        by_key = {e["key"]: e for e in inv}
        state["work_packages"] = {}
        for k, wid in found.items():
            rec = {"id": wid, "remote": snapshot(wps[wid], id_to_key)}
            if k in by_key:
                rec["hash"] = digest(desired(by_key[k], cfg))
            state["work_packages"][k] = rec
        save_state(state)
    print(f"Rebuilt state.json: {len(found)} work packages carry an orbis-key "
          f"({len(wps) - len(found)} without one are left alone).")
    return 0


def wiki_pages():
    for path in sorted(WIKI_DIR.glob("*.md")):
        text = path.read_text()
        first, _, body = text.partition("\n")
        title = first.lstrip("# ").strip()
        yield path, title, text


def wiki_slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def cmd_wiki(cfg, args):
    state = load_state()
    state.setdefault("wiki", {})
    todo = [(p, t, txt) for p, t, txt in wiki_pages() if state["wiki"].get(t) != digest(txt)]
    for p, t, _ in todo:
        print(f"  {'update' if t in state['wiki'] else 'create'}  wiki/{wiki_slug(t)}  ({p.name})")
    if not todo:
        print("Wiki is up to date.")
        return 0
    if not args.apply:
        print("\nDry-run only. Re-run with --wiki --apply to write.")
        return 0
    with OpenProject(cfg) as op:
        for p, title, text in todo:
            url = f"{op.base}/projects/{cfg['project']}/wiki/{wiki_slug(title)}/edit"

            def mutate(fields, title=title, text=text):
                text_keys = [k for k in fields if k.endswith("[text]")]
                if not text_keys:
                    raise ApiError(422, f"wiki form has no [text] field: {sorted(fields)}")
                for k in text_keys:
                    fields[k] = text
                for k in [k for k in fields if k.endswith("[title]")]:
                    fields[k] = title
                return fields

            try:
                op.submit_form(url, "form#wiki_form, form.wiki-form, form:has(textarea[name$='[text]'])", mutate)
                state["wiki"][title] = digest(text)
                save_state(state)
                print(f"  ✓ {title}")
            except ApiError as err:
                print(f"  ✗ {title}: {err}")
    return 0


def cmd_categories(cfg, args):
    """Create the config.yaml categories that are missing (there is no write API)."""
    with OpenProject(cfg) as op:
        remote = Remote(op, cfg)
        missing = [n for n in cfg["categories"].values() if n not in remote.categories]
        if not missing:
            print("All categories exist.")
            return 0
        print("Missing categories: " + ", ".join(missing))
        if not args.apply:
            print("Dry-run only. Re-run with --create-categories --apply to create them.")
            return 0
        url = f"{op.base}/projects/{cfg['project']}/categories/new"
        for name in missing:
            def mutate(fields, name=name):
                fields["category[name]"] = name
                return fields
            try:
                op.submit_form(url, "form[action$='/categories']", mutate)
                print(f"  ✓ {name}")
            except ApiError as err:
                print(f"  ✗ {name}: {err}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true", help="sign in through a visible browser and save the session")
    ap.add_argument("--check", action="store_true", help="validate inventory + OpenProject project setup")
    ap.add_argument("--check-commits", action="store_true", help="only verify every commit is assigned")
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry-run)")
    ap.add_argument("--only", help="limit to a key and its children, e.g. theme or payroll.phase5")
    ap.add_argument("--overwrite", action="store_true", help="push the inventory over edits made in OpenProject")
    ap.add_argument("--wiki", action="store_true", help="sync tools/openproject/wiki/*.md to the project wiki")
    ap.add_argument("--create-categories", action="store_true", help="create missing project categories")
    ap.add_argument("--rebuild-state", action="store_true", help="recreate state.json from orbis-key footers")
    args = ap.parse_args()
    cfg, inv = load_config(), load_inventory()

    if args.login:
        login(cfg["base_url"])
        return 0
    if args.check_commits:
        errs = check_commits(inv)
        print("\n".join(errs) or f"All commits assigned ({len(inv)} entries).")
        return 1 if errs else 0
    if args.check:
        return cmd_check(cfg, inv)
    if args.rebuild_state:
        return cmd_rebuild(cfg, inv)
    if args.create_categories:
        return cmd_categories(cfg, args)
    if args.wiki:
        return cmd_wiki(cfg, args)
    return cmd_sync(cfg, inv, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SessionExpired as e:
        die(str(e))
