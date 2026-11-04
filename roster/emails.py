"""Weekly team roster email — build + send.

`build_team_roster_email(team, week_start)` renders the email (subject, HTML,
plain text) from the live roster; `send_team_roster_email(...)` logs and
queues it. Both the "Email roster" drawer on /roster/ and the Friday beat job
(`manage.py send_weekly_rosters`) go through these, so the two can't drift.

Read-only by design: cells resolve Shift row → weekly Roster pattern, the
same order the grid uses, but without `prefill_shifts_from_pattern`'s writes —
a scheduled job must not materialise roster rows as a side effect.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape

from roster.models import (
    Holiday, Roster, RosterEmailConfig, RosterEmailLog, Shift, ShiftType,
)

logger = logging.getLogger(__name__)


def _html_lines(text: str) -> str:
    return escape(text).replace("\n", "<br>")

US_TZ = ZoneInfo("America/New_York")

# Row tints per shift group, in order (first group blue, second pink — the
# layout teams already send by hand).
GROUP_TINTS = ["#E8F0FE", "#FDECEC", "#E9F7EF", "#FFF6E0", "#F1ECFB", "#E6F6F8"]


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def next_week_start(today: date | None = None) -> date:
    return monday_of(today or timezone.localdate()) + timedelta(days=7)


def week_label(week_start: date) -> str:
    """'Sep/28 - Oct/04'."""
    end = week_start + timedelta(days=6)
    return f"{week_start:%b/%d} - {end:%b/%d}"


def _fmt_clock(dt: datetime) -> str:
    """'4am', '10:30pm'."""
    h = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{h}{suffix}" if dt.minute == 0 else f"{h}:{dt.minute:02d}{suffix}"


# Cell wording that differs from the ShiftType's own name, matching what
# teams already send clients (the legend still reads "Night Shift"). Keyed by
# the stable seeded codes (see the canonical ShiftType codes note).
CELL_LABELS = {"NIG": "Overnight"}


def _short_label(st: ShiftType) -> str:
    """Cell text: 'Night Shift' → 'Night'; falls back to the code."""
    if st.name in CELL_LABELS:
        return CELL_LABELS[st.name]
    label = (st.full_name or st.name).strip()
    if label.lower().endswith(" shift"):
        label = label[: -len(" shift")].strip()
    return label or st.name


def _legend_label(st: ShiftType) -> str:
    label = (st.full_name or st.name).strip()
    return label if label.lower().endswith("shift") else f"{label} Shift"


@dataclass
class _Variant:
    """A WORK shift as it appears in the email: type + optional time variant."""
    shift_type: ShiftType
    attribute: object | None

    @property
    def key(self):
        return (self.shift_type.pk, getattr(self.attribute, "pk", None))

    @property
    def times(self):
        if self.attribute is not None and self.attribute.start_time:
            return self.attribute.start_time, self.attribute.end_time
        return self.shift_type.shift_start_time, self.shift_type.shift_end_time


def _legend_line(variant: _Variant, week_start: date, label: str) -> str:
    """'Evening Shift - 4am EDT - 1pm EDT | BD Time : 2pm to 11pm'.

    Converted on the week's Monday, so the US side is DST-aware (EDT until the
    first Sunday of November, EST after) — never a fixed offset.
    """
    start, end = variant.times
    if not start or not end:
        return label
    local_tz = ZoneInfo(variant.shift_type.shift_timezone or settings.TIME_ZONE)
    start_dt = datetime.combine(week_start, start, tzinfo=local_tz)
    end_dt = datetime.combine(week_start, end, tzinfo=local_tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    us_start, us_end = start_dt.astimezone(US_TZ), end_dt.astimezone(US_TZ)
    return (
        f"{label} - {_fmt_clock(us_start)} {us_start.tzname()} - "
        f"{_fmt_clock(us_end)} {us_end.tzname()} | "
        f"BD Time : {_fmt_clock(start_dt)} to {_fmt_clock(end_dt)}"
    )


def _employed_in_week(qs, week_start: date):
    week_end = week_start + timedelta(days=6)
    return (
        qs.filter(is_active=True)
        .filter(Q(joining_date__isnull=True) | Q(joining_date__lte=week_end))
        .filter(Q(last_working_date__isnull=True) | Q(last_working_date__gte=week_start))
    )


def resolve_members(team, week_start: date, config: RosterEmailConfig | None = None):
    """Who goes on the email, and which team members were left out.

    The team's own active members employed during the week, minus trainees
    (`JobInfo.resource_status == TRAINEE` — not live on the account yet;
    Bench stays, since bench covers sick calls), plus anyone the config
    marks `always_include` (a lead from another team, or a trainee working a
    live shift early). Returns (members, excluded) — `excluded` is the
    trainees, so the drawer can say why a name is missing.
    """
    from accounts.models import CustomUser
    from employees.models import JobInfo

    forced = set(
        config.members.filter(always_include=True).values_list("employee_id", flat=True)
    ) if config is not None and config.pk else set()
    trainee = JobInfo.ResourceStatus.TRAINEE

    team_qs = _employed_in_week(CustomUser.objects.filter(team=team), week_start)
    excluded = list(
        team_qs.filter(job_info__resource_status=trainee).exclude(pk__in=forced)
        .order_by("first_name", "last_name")
    )
    members = list(
        _employed_in_week(
            CustomUser.objects.filter(Q(team=team) | Q(pk__in=forced)), week_start,
        )
        .exclude(pk__in=[e.pk for e in excluded])
        .select_related("designation", "team")
        .order_by("first_name", "last_name")
    )
    return members, excluded


def _employed_on(emp, d: date) -> bool:
    if emp.joining_date and d < emp.joining_date:
        return False
    if emp.last_working_date and d > emp.last_working_date:
        return False
    return True


def build_context(
    team, week_start: date, config: RosterEmailConfig | None = None, *, team_copy: bool = False,
) -> dict:
    """Template context for the client email — or, with `team_copy`, the
    internal version, which also lists trainees (marked) so they know their
    shifts while shadowing. Per-recipient highlighting is layered on top by
    `personalise_team_copy`."""
    week_start = monday_of(week_start)
    days = [week_start + timedelta(days=i) for i in range(7)]
    if config is None:
        config = RosterEmailConfig.objects.filter(team=team).first() or RosterEmailConfig(team=team)
    employees, excluded = resolve_members(team, week_start, config)
    trainee_pks = set()
    if team_copy:
        trainee_pks = {e.pk for e in excluded}
        employees, excluded = employees + excluded, []
    emp_ids = [e.pk for e in employees]

    shift_map = {
        (s.employee_id, s.date): s
        for s in Shift.objects.filter(employee_id__in=emp_ids, date__range=(days[0], days[-1]))
        .select_related("shift_type", "shift_attribute")
    }
    pattern_map = {
        (r.employee_id, r.weekday): r
        for r in Roster.objects.filter(employee_id__in=emp_ids)
        .select_related("shift_type", "shift_attribute")
    }
    holidays = {
        h.date: h for h in Holiday.objects.filter(date__range=(days[0], days[-1]), is_active=True)
    }
    roles = {
        m.employee_id: m.role_label for m in config.members.all() if m.role_label
    } if config.pk else {}
    sort_orders = {m.employee_id: m.sort_order for m in config.members.all()} if config.pk else {}

    # Resolve every cell to a variant (or None). `legend_variants` is only
    # the team's own members' WORK shifts: someone listed from another team
    # (always_include) shows in the grid, but their shift times aren't this
    # team's to publish.
    grid: dict = {}
    legend_variants: dict = {}
    work_types: dict = {}
    for emp in employees:
        own_team = emp.team_id == team.pk
        for d in days:
            if not _employed_on(emp, d):
                grid[(emp.pk, d)] = None
                continue
            cell = shift_map.get((emp.pk, d)) or pattern_map.get((emp.pk, d.weekday()))
            if cell is None:
                grid[(emp.pk, d)] = None
                continue
            v = _Variant(cell.shift_type, cell.shift_attribute)
            grid[(emp.pk, d)] = v
            if cell.shift_type.category == ShiftType.Category.WORK:
                work_types.setdefault(cell.shift_type.pk, cell.shift_type)
                if own_team:
                    legend_variants.setdefault(v.key, v)

    def cell_label(v: _Variant) -> str:
        # Plain shift name only ("Evening", "Overnight") — never the time
        # variant ("NIG 9-6"); the legend carries the times.
        if v.shift_type.category != ShiftType.Category.WORK:
            return (v.shift_type.full_name or v.shift_type.name).strip()
        return _short_label(v.shift_type)

    def type_sort(st: ShiftType):
        return (st.sort_order if st.sort_order is not None else 10**6, st.name)

    legend = [
        _legend_line(v, week_start, _legend_label(v.shift_type))
        for v in sorted(
            legend_variants.values(),
            key=lambda v: (type_sort(v.shift_type), v.times[0] or time.max),
        )
    ]
    # Rows group (and tint) by shift *type*, so a lead on NIG 9-6 sits with
    # the team's NIG 10-7 rows — the cells read the same anyway.
    ordered_types = sorted(work_types.values(), key=type_sort)
    group_index = {st.pk: i for i, st in enumerate(ordered_types)}

    # Weekend merge: Sat+Sun collapse to one "Weekend" block only when every
    # employee is OFF on both days.
    weekend_days = [d for d in days if d.weekday() >= 5]
    merge_weekend = bool(employees) and all(
        grid[(e.pk, d)] is not None
        and grid[(e.pk, d)].shift_type.category == ShiftType.Category.OFF
        for e in employees for d in weekend_days
    )
    body_days = [d for d in days if not (merge_weekend and d in weekend_days)]

    rows = []
    for emp in employees:
        work = Counter(
            grid[(emp.pk, d)].shift_type.pk for d in days
            if grid[(emp.pk, d)] is not None
            and grid[(emp.pk, d)].shift_type.category == ShiftType.Category.WORK
        )
        primary = work.most_common(1)[0][0] if work else None
        group = group_index.get(primary, len(ordered_types))
        role = roles.get(emp.pk)
        cells = []
        for d in body_days:
            v = grid[(emp.pk, d)]
            cells.append(cell_label(v) if v else "—")
        rows.append({
            "employee": emp,
            "name": emp.display_name,
            "role": (role or (emp.designation.name if emp.designation_id else ""))
                    + (" (Trainee)" if emp.pk in trainee_pks else ""),
            "cells": cells,
            "_sort": (
                group,
                0 if role else 1,
                sort_orders.get(emp.pk, 0),
                emp.display_name.lower(),
            ),
            "group": group,
        })
    rows.sort(key=lambda r: r["_sort"])
    for i, r in enumerate(rows, start=1):
        r["sl"] = i
        r["tint"] = GROUP_TINTS[r["group"] % len(GROUP_TINTS)] if r["group"] < len(ordered_types) else "#FFFFFF"

    poc = config.effective_poc
    label = week_label(week_start)
    intro = config.intro_text or ""
    # Escape first, then drop the bold week label into the {week} slot.
    intro_html = _html_lines(intro).replace("{week}", f"<strong>{escape(label)}</strong>")
    signer = config.sender if config.sender_id else poc
    signature = config.signature.strip() or "\n".join(
        ["Best regards,", ""] + ([signer.display_name] if signer else []) + [f"SEBPO {team.name}"]
    )

    return {
        "team": team,
        "week_start": week_start,
        "week_end": days[-1],
        "week_label": label,
        "title": f"SEBPO {team.name} Weekly Schedule {week_start.year}, {label}",
        # HTML header breaks the title in two so it doesn't widen the table.
        "title_team": f"SEBPO {team.name} Weekly Schedule",
        "title_week": f"{week_start.year}, {label}",
        "subject": f"SEBPO {team.name} Weekly Roster Schedule {label}",
        "poc": poc,
        "sender": config.sender if config.sender_id else None,
        "legend": legend,
        "days": days,
        "day_headers": [
            {"date": d, "holiday": holidays.get(d)} for d in days
        ],
        "body_days": body_days,
        "merge_weekend": merge_weekend,
        "weekend_colspan": len(weekend_days),
        "rows": rows,
        "excluded": excluded,
        "team_copy": team_copy,
        "intro_text": intro.replace("{week}", label),
        "intro_html": intro_html,
        "signature": signature,
        "signature_html": _html_lines(signature),
    }


def build_team_roster_email(team, week_start: date, config: RosterEmailConfig | None = None):
    """Return (subject, html, text, context)."""
    ctx = build_context(team, week_start, config)
    html = render_to_string("roster/email/weekly_roster.html", ctx)
    text = render_to_string("roster/email/weekly_roster.txt", ctx)
    return ctx["subject"], html, text, ctx


HIGHLIGHT_TINT = "#FFE58F"


def personalise_team_copy(ctx: dict, recipient) -> tuple[str, str, str]:
    """(subject, html, text) of the team copy for one recipient: their row
    highlighted and an internal opening line. `ctx` is a `team_copy=True`
    context, built once per send and reused for every recipient."""
    contact = ctx["sender"] or ctx["poc"]
    label = ctx["week_label"]
    ask = (
        f" Please reply to {contact.display_name} for any changes."
        if contact and contact.pk != recipient.pk else ""
    )
    # Full name, not first_name — which is often just "Md." here.
    intro = f"Hi {recipient.display_name},\n\nHere's the team schedule for next week {{week}}. Your row is highlighted.{ask}"
    rows = [
        {**r, "highlight": r["employee"].pk == recipient.pk,
         "tint": HIGHLIGHT_TINT if r["employee"].pk == recipient.pk else r["tint"]}
        for r in ctx["rows"]
    ]
    pctx = {
        **ctx,
        "rows": rows,
        "intro_text": intro.replace("{week}", label),
        "intro_html": _html_lines(intro).replace("{week}", f"<strong>{escape(label)}</strong>"),
    }
    subject = f"[Team copy] {ctx['subject']}"
    return (
        subject,
        render_to_string("roster/email/weekly_roster.html", pctx),
        render_to_string("roster/email/weekly_roster.txt", pctx),
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def sender_headers(sender, poc) -> tuple[str, str]:
    """(From, Reply-To) for a roster email.

    Option D of the send-as discussion (2026-09-25): always the system
    address — no per-user mailbox credentials are stored — but under the
    named sender's display name, replies going to them. Without a sender,
    the plain system address with replies to the POC.
    """
    from email.utils import formataddr, parseaddr

    system_name, system_addr = parseaddr(settings.DEFAULT_FROM_EMAIL)
    if sender is not None and sender.email:
        return formataddr((f"{sender.display_name} (via Orbis)", system_addr)), sender.email
    return settings.DEFAULT_FROM_EMAIL, (poc.email if poc and poc.email else "")


def send_team_roster_email(
    team, week_start: date, *, trigger: str, to: list[str], cc: list[str] | None = None,
    sent_by=None, config: RosterEmailConfig | None = None,
) -> RosterEmailLog:
    """Render, log (QUEUED) and hand the SMTP call to the task queue."""
    from core.tasks import defer

    subject, html, text, ctx = build_team_roster_email(team, week_start, config)
    from_email, reply_to = sender_headers(ctx["sender"], ctx["poc"])
    cc = list(cc or [])
    # The named sender gets a copy of real sends (not tests), standing in for
    # the Sent-folder copy they'd have if they'd sent it themselves.
    sender = ctx["sender"]
    if (
        sender is not None and sender.email and trigger != RosterEmailLog.Trigger.TEST
        and sender.email.lower() not in {e.lower() for e in to + cc}
    ):
        cc.append(sender.email)
    log = RosterEmailLog.objects.create(
        team=team,
        week_start=ctx["week_start"],
        trigger=trigger,
        to_emails=", ".join(to),
        cc_emails=", ".join(cc),
        from_email=from_email,
        reply_to=reply_to,
        subject=subject,
        html_body=html,
        text_body=text,
        sent_by=sent_by,
    )
    defer(deliver_roster_email, log.pk)
    return log


def send_team_copies(
    team, week_start: date, *, trigger: str, sent_by=None, config: RosterEmailConfig | None = None,
    only=None,
) -> list[RosterEmailLog]:
    """One internal copy per person on the roster (trainees included), each
    with their own row highlighted, To: just them — no client addresses, so
    Reply All can never reach the client. `only` restricts to one recipient
    (a test send previews the copy to the tester, highlighting their row if
    they're on it)."""
    from core.tasks import defer

    ctx = build_context(team, week_start, config, team_copy=True)
    from_email, reply_to = sender_headers(ctx["sender"], ctx["poc"])
    if only is not None:
        on_roster = next((r["employee"] for r in ctx["rows"] if r["employee"].pk == only.pk), None)
        targets = [(on_roster or only, only.email)]
    else:
        targets = [(r["employee"], r["employee"].email) for r in ctx["rows"] if r["employee"].email]
    logs = []
    for person, address in targets:
        subject, html, text = personalise_team_copy(ctx, person)
        log = RosterEmailLog.objects.create(
            team=team, week_start=ctx["week_start"], trigger=trigger,
            kind=RosterEmailLog.Kind.TEAM_COPY,
            to_emails=address, from_email=from_email, reply_to=reply_to,
            subject=subject, html_body=html, text_body=text, sent_by=sent_by,
        )
        defer(deliver_roster_email, log.pk)
        logs.append(log)
    return logs


def send_roster(
    team, week_start: date, *, trigger: str, to: list[str], cc: list[str] | None = None,
    sent_by=None, config: RosterEmailConfig | None = None,
) -> tuple[RosterEmailLog, list[RosterEmailLog]]:
    """The one entry point the drawer and the Friday job both use: the client
    email, then — when the team has `send_team_copy` on — the team copies.
    A TEST send goes to `to` only: the client version plus one sample team
    copy, never to the team."""
    if config is None:
        config = RosterEmailConfig.objects.filter(team=team).first()
    client_log = send_team_roster_email(
        team, week_start, trigger=trigger, to=to, cc=cc, sent_by=sent_by, config=config,
    )
    copies = []
    if config is not None and config.send_team_copy:
        if trigger == RosterEmailLog.Trigger.TEST:
            from accounts.models import CustomUser

            tester = CustomUser.objects.filter(email__iexact=to[0]).first() if to else None
            if tester is not None:
                copies = send_team_copies(team, week_start, trigger=trigger, sent_by=sent_by,
                                          config=config, only=tester)
        else:
            copies = send_team_copies(team, week_start, trigger=trigger, sent_by=sent_by, config=config)
    return client_log, copies


def deliver_roster_email(log_pk: int) -> None:
    """SMTP send for one RosterEmailLog row; records SENT/FAILED on it.

    Swallows the exception after recording it (same posture as
    core.notifications.deliver_mail) — a Celery retry would re-send to a
    list that may have partially accepted the message.
    """
    log = RosterEmailLog.objects.filter(pk=log_pk).first()
    if log is None or log.status == RosterEmailLog.Status.SENT:
        return
    msg = EmailMultiAlternatives(
        subject=log.subject,
        body=log.text_body,
        from_email=log.from_email or settings.DEFAULT_FROM_EMAIL,
        to=RosterEmailConfig.split_emails(log.to_emails),
        cc=RosterEmailConfig.split_emails(log.cc_emails),
        reply_to=[log.reply_to] if log.reply_to else None,
    )
    msg.attach_alternative(log.html_body, "text/html")
    try:
        msg.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 — recorded on the log row
        logger.exception("Roster email %s failed", log_pk)
        log.status = RosterEmailLog.Status.FAILED
        log.error = str(exc)[:2000]
        log.save(update_fields=["status", "error"])
        return
    log.status = RosterEmailLog.Status.SENT
    log.sent_at = timezone.now()
    log.save(update_fields=["status", "sent_at"])
