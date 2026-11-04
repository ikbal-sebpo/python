#!/usr/bin/env python
"""Log Orbis work time into OpenProject from the roster + TimeLy exports.

    python tools/openproject/timelog.py --roster ~/Downloads/roster-flat-2026-11-03.xlsx \\
        --attendance ~/Downloads/attendance_539_2026-01-01_to_2026-11-03.csv          # dry-run
    python tools/openproject/timelog.py ... --apply --from 2026-04-24 --to 2026-04-30   # pilot week

Hours per day (config.yaml `timelog`):
  * working shift + TimeLy punch  → actual Duration (on/before `actual_until`),
                                    max(default_hours, actual) after it
  * working shift, no punch        → default_hours (worked from home, forgot to punch)
  * non-working day with a punch   → actual Duration
  * non-working day, no punch      → nothing
A person's hours follow their OWN commits only (config `people:` git emails;
teammates' commits are their hours, not yours). A commit before 04:00 counts
toward the previous evening's shift. Each logged day goes, first rule wins, to:
  1. that day's commits (split by commit count);
  2. the person's multi-commit features (span ≤ 14 days) whose first→last commit covers it;
  3. a `gap_windows` batch from config (work committed later in one go), else
     the features of the person's next commit day (work in progress in a gap);
  4. `fallback_key`, after their last commit.
Only --rebalance deletes, and only this tool's own entries whose feature moved.
See docs/OPENPROJECT.md.

The exports and the time-entry state are personal attendance data: they stay
outside the repo (state in ~/.config/orbis-openproject/timelog_state.<person>.json).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
from collections import Counter, defaultdict

from session import (CONFIG_DIR, HERE, ApiError, OpenProject, commit_owners, die, href,
                     load_config, load_inventory)

WP_STATE = HERE / "state.json"
MAX_SPAN_DAYS = 14


# ---------------------------------------------------------------- inputs
def read_roster(path, employee_id) -> dict[dt.date, str]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h else "" for h in next(rows)]
    i_date, i_emp, i_status = (header.index("Date"), header.index("EmployeeID"),
                               header.index("Shift Status"))
    out = {}
    for r in rows:
        if r[i_date] is None or str(r[i_emp]).strip() != str(employee_id):
            continue
        d = r[i_date].date() if hasattr(r[i_date], "date") else dt.date.fromisoformat(str(r[i_date])[:10])
        out[d] = str(r[i_status] or "").strip()
    return out


def read_punches(path, employee_id) -> dict[dt.date, int]:
    """Minutes in office per work date (rows for the same date are summed)."""
    out: dict[dt.date, int] = defaultdict(int)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("EID", "")).strip() != str(employee_id):
                continue
            dur = (r.get("Duration") or "").strip()
            if not dur:
                continue
            h, m, s = (int(x) for x in dur.split(":"))
            out[dt.date.fromisoformat(r["Work Date"].strip())] += h * 60 + m + round(s / 60)
    return dict(out)


def read_orbis(employee_id, lo: dt.date, hi: dt.date, host: str):
    """Roster, punches and working flags straight from Orbis production.

    Pipes orbis_source.py into `manage.py shell` over SSH (read-only). A day is
    working when its resolved ShiftType category is WORK — the Timesheet's own
    test — so no name list is involved. Punches exist only once Orbis's 10:00
    sync has run for that date.
    """
    env = f"TL_EMP={employee_id} TL_FROM={lo} TL_TO={hi}"
    with open(HERE / "orbis_source.py") as src:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                            f"cd ~/orbis && {env} venv/bin/python manage.py shell"],
                           stdin=src, capture_output=True, text=True, timeout=180)
    out = r.stdout
    if "@@TIMELOG@@" not in out:
        die(f"Could not read from Orbis over SSH ({host}): {(r.stderr or out)[-500:]}", 4)
    data = json.loads(out.split("@@TIMELOG@@", 1)[1].split("@@END@@", 1)[0])
    roster, punches, working = {}, {}, {}
    for d, v in data.items():
        day = dt.date.fromisoformat(d)
        roster[day] = v["shift"]
        working[day] = v["category"] == "WORK"
        if v["minutes"] is not None:
            punches[day] = v["minutes"]
    return roster, punches, working


def commit_days(emails: list[str], inv: list[dict]) -> dict[dt.date, Counter]:
    """{shift date: Counter(entry key → commit count)} for one person's commits."""
    owner = commit_owners(inv)
    log = subprocess.run(["git", "-C", str(HERE.parents[1]), "log", "--no-merges",
                          "--format=%h %ae %aI"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    mine = {e.lower() for e in emails}
    days: dict[dt.date, Counter] = defaultdict(Counter)
    for line in log:
        h, email, iso = line.split()
        if email.lower() not in mine:
            continue
        ts = dt.datetime.fromisoformat(iso)
        d = ts.date() - dt.timedelta(days=1) if ts.hour < 4 else ts.date()
        key = owner.get(h[:7])
        if key:
            days[d][key] += 1
    return days


def allocator(commits: dict[dt.date, Counter], fallback: str, gap_windows=()):
    """Return f(date) → (Counter of weights, rule label) — see the module docstring."""
    spans: dict[str, list[dt.date]] = defaultdict(list)
    for d, c in commits.items():
        for k in c:
            spans[k].append(d)
    # Only short features count as continuous work; a long span is a feature plus
    # later follow-ups, and would swallow every gap day in between.
    spans = {k: (min(v), max(v)) for k, v in spans.items() if 0 < (max(v) - min(v)).days <= MAX_SPAN_DAYS}
    commit_dates = sorted(commits)

    windows = []
    for w in gap_windows or ():
        lo, hi = (dt.date.fromisoformat(str(w[k])) for k in ("commits_from", "commits_to"))
        weights = Counter()
        for cd, c in commits.items():
            if lo <= cd <= hi:
                weights.update(c)
        windows.append((dt.date.fromisoformat(str(w["from"])), dt.date.fromisoformat(str(w["to"])),
                        weights, f"batch {lo}→{hi}"))

    def alloc(d: dt.date):
        if d in commits:
            return commits[d], "commits"
        for lo, hi, weights, label in windows:
            if lo <= d <= hi and weights:
                return weights, label
        covering = Counter({k: 1 for k, (a, b) in spans.items() if a < d < b})
        if covering:
            return covering, "in progress"
        nxt = next((c for c in commit_dates if c > d), None)
        if nxt:
            return commits[nxt], f"towards {nxt}"
        return Counter({fallback: 1}), "after last commit"

    return alloc


# ---------------------------------------------------------------- the rule
def day_minutes(d, shift, punched, tl, working=None) -> tuple[int, str]:
    default = int(tl["default_hours"] * 60)
    if working is None:
        working = bool(shift) and shift not in set(tl["non_working"])
    actual_until = dt.date.fromisoformat(str(tl["actual_until"]))
    if working and punched is None:
        return default, "no punch → default"
    if working:
        if d > actual_until and punched < default:
            return default, "short day → default"
        return punched, "punched"
    if punched:
        return punched, f"worked on {shift or 'unrostered day'}"
    return 0, ""


def split(minutes: int, weights: Counter) -> list[tuple[str, int]]:
    total = sum(weights.values())
    keys = sorted(weights, key=lambda k: (-weights[k], k))
    out, used = [], 0
    for i, k in enumerate(keys):
        share = minutes - used if i == len(keys) - 1 else round(minutes * weights[k] / total)
        used += share
        if share:
            out.append((k, share))
    return out


def build(args, cfg, inv):
    tl = cfg["timelog"]
    person = cfg["people"][args.person]
    if not person.get("employee_id"):
        die(f"people.{args.person}.employee_id is not set in config.yaml")
    first = dt.date.fromisoformat(str(tl["start"]))
    if args.from_orbis:
        yesterday = dt.date.today() - dt.timedelta(days=1)
        hi = args.date_to or yesterday
        lo = args.date_from or hi - dt.timedelta(days=args.days - 1)
        # Orbis holds punches only from its nightly sync's go-live; earlier days
        # would read as "no punch" and wrongly log 9h. Use the exports for those.
        floor = dt.date.fromisoformat(str(tl["orbis_punches_from"]))
        if lo < floor:
            print(f"(--from-orbis starts at {floor}: Orbis has no punch history before it — "
                  "use --roster/--attendance exports for earlier dates)")
            lo = floor
            args.date_from = max(args.date_from or floor, floor)
        args.date_from = max(lo, first)
        roster, punches, working = read_orbis(person["employee_id"], max(lo, first), hi,
                                              tl.get("orbis_ssh", "orbis-prod"))
        last_data = hi
    else:
        roster = read_roster(args.roster, person["employee_id"])
        punches = read_punches(args.attendance, person["employee_id"])
        nonwork = set(tl["non_working"])
        working = {d: bool(v) and v not in nonwork for d, v in roster.items()}
        last_data = max(punches) if punches else max(roster)
    alloc = allocator(commit_days(person["git_emails"], inv), tl["fallback_key"],
                      person.get("gap_windows"))
    start = max(first, args.date_from or dt.date.min)
    end = min(args.date_to or last_data, last_data, dt.date.today())
    days = []
    d = start
    while d <= end:
        minutes, why = day_minutes(d, roster.get(d), punches.get(d), tl, working.get(d, False))
        if minutes:
            weights, rule = alloc(d)
            days.append({"date": d, "shift": roster.get(d), "punched": punches.get(d),
                         "minutes": minutes, "why": f"{why}; {rule}", "split": split(minutes, weights)})
        d += dt.timedelta(days=1)
    return days, roster, punches, working, start, end


def hm(minutes) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def iso_duration(minutes) -> str:
    return f"PT{minutes // 60}H{minutes % 60}M"


def parse_iso_duration(s: str) -> int:
    import re

    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = (float(x) if x else 0 for x in m.groups())
    return int(round(d * 1440 + h * 60 + mi + sec / 60))


# ---------------------------------------------------------------- report
def report(days, roster, punches, is_working, start, end, tl):
    working = [d for d in roster if start <= d <= end and is_working.get(d)]
    no_punch = [d for d in working if d not in punches]
    extra = Counter(roster.get(d) or "unrostered" for d in punches
                    if start <= d <= end and d not in working)
    total = sum(x["minutes"] for x in days)
    print(f"{'date':10}  {'shift':10} {'punched':>7} {'logged':>6}  why / split")
    for x in days:
        sp = ", ".join(f"{k} {hm(m)}" for k, m in x["split"])
        p = hm(x["punched"]) if x["punched"] is not None else "—"
        print(f"{x['date']}  {str(x['shift'] or ''):10} {p:>7} {hm(x['minutes']):>6}  {x['why']}: {sp}")
    print(f"\n{start} → {end}: {len(working)} working shifts, {len(no_punch)} without a punch "
          f"({', '.join(str(d) for d in no_punch) or 'none'}) at {tl['default_hours']}h; "
          f"worked non-working days: {dict(extra) or 'none'}.")
    print(f"Total: {len(days)} days, {hm(total)} ({total / 60:.1f}h).")
    per_key = Counter()
    for x in days:
        for k, m in x["split"]:
            per_key[k.split(".")[0]] += m
    print("By epic: " + ", ".join(f"{k} {m / 60:.1f}h" for k, m in per_key.most_common()))
    return total


# ---------------------------------------------------------------- write
def state_file(person: str):
    return CONFIG_DIR / f"timelog_state.{person}.json"


def load_state(person):
    f = state_file(person)
    return json.loads(f.read_text()) if f.exists() else {}


def save_state(person, state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = state_file(person)
    f.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    f.chmod(0o600)


def find_activity(op, project_id, wp_id, name):
    form = op.post("/api/v3/time_entries/form",
                   {"_links": {"project": href("projects", project_id),
                               "workPackage": href("work_packages", wp_id)}})
    allowed = form["_embedded"]["schema"]["activity"]["_embedded"]["allowedValues"]
    for a in allowed:
        if a["name"] == name:
            return a["id"]
    die(f"Time-entry activity {name!r} not found; available: {[a['name'] for a in allowed]}")


def apply(days, cfg, args):
    wp_state = json.loads(WP_STATE.read_text())["work_packages"] if WP_STATE.exists() else {}
    needed = {k for x in days for k, _ in x["split"]}
    missing = sorted(k for k in needed if k not in wp_state)
    if missing:
        die("These work packages aren't in OpenProject yet — run sync.py --apply first:\n  "
            + "\n  ".join(missing))
    state = load_state(args.person)
    created = updated = skipped = deleted = 0
    with OpenProject(cfg) as op:
        project_id = op.get(f"/api/v3/projects/{cfg['project']}")["id"]
        any_wp = wp_state[next(iter(needed))]["id"]
        activity = find_activity(op, project_id, any_wp, cfg["timelog"]["activity"])
        login_name = cfg["people"][args.person].get("op_login")
        user_link = None
        if login_name:
            users = op.collect("/api/v3/users", {"filters": json.dumps(
                [{"login": {"operator": "=", "values": [login_name]}}])})
            if not users:
                die(f"OpenProject user {login_name!r} not found")
            user_link = href("users", users[0]["id"])
        for x in days:
            day = str(x["date"])
            have = state.get(day, {})
            for key, minutes in x["split"]:
                rec = have.get(key)
                if rec and rec["minutes"] == minutes:
                    skipped += 1
                    continue
                comment = f"orbis-timelog:{day}:{key}"
                try:
                    if rec:
                        cur = op.get(f"/api/v3/time_entries/{rec['id']}")
                        op.patch(f"/api/v3/time_entries/{rec['id']}",
                                 {"lockVersion": cur.get("lockVersion"), "hours": iso_duration(minutes)})
                        updated += 1
                        print(f"  ~ {day} {key} {hm(rec['minutes'])} → {hm(minutes)}")
                    else:
                        te = op.post("/api/v3/time_entries", {
                            "spentOn": day, "hours": iso_duration(minutes),
                            "comment": {"format": "plain", "raw": comment},
                            "_links": {"project": href("projects", project_id),
                                       "workPackage": href("work_packages", wp_state[key]["id"]),
                                       "activity": href("time_entries/activities", activity),
                                       **({"user": user_link} if user_link else {})}})
                        rec = {"id": te["id"]}
                        created += 1
                        print(f"  + {day} {key} {hm(minutes)}")
                    rec["minutes"] = minutes
                    state.setdefault(day, {})[key] = rec
                    save_state(args.person, state)
                except ApiError as err:
                    print(f"  ✗ {day} {key}: {err}")
            stale = sorted(set(have) - {k for k, _ in x["split"]})
            for key in stale:
                if not args.rebalance:
                    print(f"  ! {day}: entry for {key} no longer matches the split — "
                          "re-run with --rebalance to remove it")
                    continue
                try:
                    op.request("DELETE", f"/api/v3/time_entries/{have[key]['id']}")
                    del state[day][key]
                    save_state(args.person, state)
                    deleted += 1
                    print(f"  - {day} {key} {hm(have[key]['minutes'])}")
                except ApiError as err:
                    print(f"  ✗ {day} {key}: {err}")
    print(f"\n{created} created, {updated} updated, {deleted} removed, {skipped} already logged.")


def rebuild_state(cfg, person):
    """Recover timelog_state.json from the comments on your OpenProject time entries."""
    import re

    state = {}
    with OpenProject(cfg) as op:
        project_id = op.get(f"/api/v3/projects/{cfg['project']}")["id"]
        flt = json.dumps([{"project_id": {"operator": "=", "values": [str(project_id)]}},
                          {"user_id": {"operator": "=", "values": ["me"]}}])
        for te in op.collect("/api/v3/time_entries", {"filters": flt}):
            m = re.match(r"orbis-timelog:(\d{4}-\d\d-\d\d):(\S+)", (te.get("comment") or {}).get("raw") or "")
            if m:
                state.setdefault(m.group(1), {})[m.group(2)] = {
                    "id": te["id"], "minutes": parse_iso_duration(te.get("hours"))}
    save_state(person, state)
    print(f"Rebuilt {state_file(person)}: {sum(len(v) for v in state.values())} entries over {len(state)} days.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", default="smrashel", help="key under people: in config.yaml")
    ap.add_argument("--from-orbis", action="store_true",
                    help="read roster + punches from Orbis production over SSH (no exports)")
    ap.add_argument("--days", type=int, default=7,
                    help="with --from-orbis and no --from: how many days back from yesterday")
    ap.add_argument("--roster", help="roster flat export (.xlsx)")
    ap.add_argument("--attendance", help="TimeLy attendance export (.csv)")
    ap.add_argument("--from", dest="date_from", type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="date_to", type=dt.date.fromisoformat)
    ap.add_argument("--apply", action="store_true", help="write time entries (default is a dry-run)")
    ap.add_argument("--rebalance", action="store_true",
                    help="delete this tool's entries for a day whose split moved to other features")
    ap.add_argument("--quiet", action="store_true", help="summary only, no per-day table")
    ap.add_argument("--rebuild-state", action="store_true")
    args = ap.parse_args()
    cfg, inv = load_config(), load_inventory()
    if args.rebuild_state:
        return rebuild_state(cfg, args.person)
    if args.from_orbis:
        if args.date_from and not args.date_to:
            args.date_to = dt.date.today() - dt.timedelta(days=1)
    elif not (args.roster and args.attendance):
        ap.error("give --from-orbis, or both --roster and --attendance")
    days, roster, punches, working, start, end = build(args, cfg, inv)
    if args.quiet:
        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(days, roster, punches, working, start, end, cfg["timelog"])
        print("\n".join(buf.getvalue().splitlines()[-3:]))
    else:
        report(days, roster, punches, working, start, end, cfg["timelog"])
    if args.apply:
        apply(days, cfg, args)
    else:
        print("\nDry-run only. Re-run with --apply to write time entries.")
    return 0


if __name__ == "__main__":
    from session import SessionExpired

    try:
        sys.exit(main())
    except SessionExpired as e:
        die(str(e), 3)   # exit 3 = run `sync.py --login` (the daily timer notifies on it)
