"""Weekly team roster email — roster/emails.py, RosterEmailView, and
``manage.py send_weekly_rosters``."""

from __future__ import annotations

from datetime import date, time, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, Team
from employees.models import JobInfo
from roster import emails
from roster.models import (
    Roster, RosterEmailConfig, RosterEmailLog, RosterEmailMember, Shift, ShiftAttribute, ShiftType,
)

User = get_user_model()

WEEK = date(2026, 9, 28)  # a Monday, EDT


def _user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test", **extra,
    )


@override_settings(TASK_QUEUE_ENABLED=False, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RosterEmailTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.head = _user("head@example.com")
        cls.dept = Department.objects.create(name="Email Dept", head_of_department=cls.head)
        cls.team = Team.objects.create(name="AP Team", department=cls.dept)
        cls.eve = ShiftType.objects.create(
            name="EVX", full_name="Evening Shift", category=ShiftType.Category.WORK,
            shift_start_time=time(14), shift_end_time=time(23), shift_timezone="Asia/Dhaka",
        )
        cls.nig = ShiftType.objects.create(
            name="NGX", full_name="Night Shift", category=ShiftType.Category.WORK,
            shift_start_time=time(22), shift_end_time=time(7), shift_timezone="Asia/Dhaka",
        )
        cls.off = ShiftType.objects.create(name="OFX", full_name="Weekly Off", category=ShiftType.Category.OFF)
        cls.a = _user("alpha@example.com", team=cls.team)
        cls.b = _user("bravo@example.com", team=cls.team)
        for emp, work in ((cls.a, cls.nig), (cls.b, cls.eve)):
            for wd in range(7):
                Roster.objects.create(employee=emp, weekday=wd, shift_type=work if wd < 5 else cls.off)
        cls.config = RosterEmailConfig.objects.create(team=cls.team, to_emails="to@example.com")


class BuildContextTests(RosterEmailTestBase):
    def test_pattern_fallback_groups_and_weekend_merge(self):
        ctx = emails.build_context(self.team, WEEK)
        self.assertTrue(ctx["merge_weekend"])
        self.assertEqual(len(ctx["body_days"]), 5)
        # Evening sorts before Night (name order); each row carries its shift.
        self.assertEqual([r["name"] for r in ctx["rows"]], [self.b.display_name, self.a.display_name])
        self.assertEqual(ctx["rows"][0]["cells"], ["Evening"] * 5)
        self.assertEqual(ctx["rows"][1]["cells"], ["Night"] * 5)
        self.assertEqual(ctx["poc"], self.head)

    def test_shift_row_wins_over_pattern(self):
        Shift.objects.create(employee=self.b, date=WEEK, shift_type=self.nig)
        ctx = emails.build_context(self.team, WEEK)
        row = next(r for r in ctx["rows"] if r["employee"] == self.b)
        self.assertEqual(row["cells"][0], "Night")

    def test_weekend_not_merged_when_someone_works(self):
        Shift.objects.create(employee=self.a, date=WEEK + timedelta(days=5), shift_type=self.nig)
        ctx = emails.build_context(self.team, WEEK)
        self.assertFalse(ctx["merge_weekend"])
        self.assertEqual(len(ctx["rows"][0]["cells"]), 7)

    def test_joining_date_blanks_earlier_days_and_excludes_later_joiners(self):
        self.a.joining_date = WEEK + timedelta(days=2)
        self.a.save()
        _user("future@example.com", team=self.team, joining_date=WEEK + timedelta(days=10))
        ctx = emails.build_context(self.team, WEEK)
        names = [r["name"] for r in ctx["rows"]]
        self.assertNotIn("Future Test", names)
        row = next(r for r in ctx["rows"] if r["employee"] == self.a)
        self.assertEqual(row["cells"][:3], ["—", "—", "Night"])

    def test_role_override_replaces_designation_and_sorts_first(self):
        RosterEmailMember.objects.create(config=self.config, employee=self.a, role_label="Team Lead")
        # Put both on the same shift so the role decides the order.
        for d in range(5):
            Shift.objects.create(employee=self.b, date=WEEK + timedelta(days=d), shift_type=self.nig)
        ctx = emails.build_context(self.team, WEEK)
        self.assertEqual(ctx["rows"][0]["employee"], self.a)
        self.assertEqual(ctx["rows"][0]["role"], "Team Lead")

    def test_nig_cells_read_overnight_but_legend_keeps_night_shift(self):
        self.nig.name = "NIG"
        self.nig.save()
        ctx = emails.build_context(self.team, WEEK)
        row = next(r for r in ctx["rows"] if r["employee"] == self.a)
        self.assertEqual(row["cells"], ["Overnight"] * 5)
        self.assertTrue(any(line.startswith("Night Shift - ") for line in ctx["legend"]))

    def test_legend_is_dst_aware(self):
        edt = emails.build_context(self.team, WEEK)["legend"]
        self.assertIn("Evening Shift - 4am EDT - 1pm EDT | BD Time : 2pm to 11pm", edt)
        self.assertIn("Night Shift - 12pm EDT - 9pm EDT | BD Time : 10pm to 7am", edt)
        est = emails.build_context(self.team, date(2026, 11, 2))["legend"]
        self.assertIn("Evening Shift - 3am EST - 12pm EST | BD Time : 2pm to 11pm", est)


class MembershipTests(RosterEmailTestBase):
    def _names(self):
        return [r["employee"] for r in emails.build_context(self.team, WEEK)["rows"]]

    def test_trainee_excluded_bench_included(self):
        JobInfo.objects.create(employee=self.a, resource_status=JobInfo.ResourceStatus.TRAINEE)
        JobInfo.objects.create(employee=self.b, resource_status=JobInfo.ResourceStatus.BENCH)
        ctx = emails.build_context(self.team, WEEK)
        self.assertEqual([r["employee"] for r in ctx["rows"]], [self.b])
        self.assertEqual(ctx["excluded"], [self.a])

    def test_trainee_listed_once_live(self):
        ji = JobInfo.objects.create(employee=self.a, resource_status=JobInfo.ResourceStatus.TRAINEE)
        self.assertNotIn(self.a, self._names())
        ji.resource_status = JobInfo.ResourceStatus.LIVE
        ji.save()
        self.assertIn(self.a, self._names())

    def test_always_include_trainee(self):
        JobInfo.objects.create(employee=self.a, resource_status=JobInfo.ResourceStatus.TRAINEE)
        RosterEmailMember.objects.create(config=self.config, employee=self.a, always_include=True)
        self.assertIn(self.a, self._names())

    def test_outsider_listed_with_own_roster(self):
        other = Team.objects.create(name="Other Team", department=self.dept)
        lead = _user("lead@example.com", team=other)
        for d in range(5):
            Shift.objects.create(employee=lead, date=WEEK + timedelta(days=d), shift_type=self.nig)
        self.assertNotIn(lead, self._names())
        RosterEmailMember.objects.create(
            config=self.config, employee=lead, always_include=True, role_label="Team Lead",
        )
        ctx = emails.build_context(self.team, WEEK)
        row = next(r for r in ctx["rows"] if r["employee"] == lead)
        self.assertEqual(row["role"], "Team Lead")
        self.assertEqual(row["cells"][:5], ["Night"] * 5)
        # Role-labelled rows lead their shift group.
        self.assertEqual(ctx["rows"][1]["employee"], lead)


    def test_outsider_shift_not_in_legend_and_variants_not_named(self):
        early = ShiftAttribute.objects.create(shift_type=self.nig, name="NGX 9-6", start_time=time(21), end_time=time(6))
        morning = ShiftType.objects.create(
            name="MRX", full_name="Morning Shift", category=ShiftType.Category.WORK,
            shift_start_time=time(7), shift_end_time=time(16), shift_timezone="Asia/Dhaka",
        )
        other = Team.objects.create(name="Other Team", department=self.dept)
        lead = _user("lead@example.com", team=other)
        Shift.objects.create(employee=lead, date=WEEK, shift_type=morning)
        for d in range(1, 5):
            Shift.objects.create(employee=lead, date=WEEK + timedelta(days=d),
                                 shift_type=self.nig, shift_attribute=early)
        RosterEmailMember.objects.create(config=self.config, employee=lead, always_include=True)
        ctx = emails.build_context(self.team, WEEK)
        row = next(r for r in ctx["rows"] if r["employee"] == lead)
        self.assertEqual(row["cells"][:5], ["Morning"] + ["Night"] * 4)
        # Only the team's own shifts are converted: Evening + Night (22–07).
        self.assertEqual(len(ctx["legend"]), 2)
        self.assertFalse(any("Morning" in line or "9pm to 6am" in line for line in ctx["legend"]))
        # Grouped with the team's Night rows despite the different variant.
        night_rows = [r for r in ctx["rows"] if r["employee"] in (self.a, lead)]
        self.assertEqual(night_rows[0]["group"], night_rows[1]["group"])


class SendTests(RosterEmailTestBase):
    def test_send_logs_and_sets_reply_to(self):
        log = emails.send_team_roster_email(
            self.team, WEEK, trigger=RosterEmailLog.Trigger.MANUAL, to=["to@example.com"],
        )
        log.refresh_from_db()
        self.assertEqual(log.status, RosterEmailLog.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].reply_to, ["head@example.com"])
        self.assertIn("Weekly Roster Schedule Sep/28 - Oct/04", mail.outbox[0].subject)

    @override_settings(DEFAULT_FROM_EMAIL="Orbis <no-reply@example.com>")
    def test_named_sender_display_name_reply_to_and_cc(self):
        self.config.sender = self.a
        self.config.save()
        emails.send_team_roster_email(
            self.team, WEEK, trigger=RosterEmailLog.Trigger.MANUAL, to=["to@example.com"],
        )
        msg = mail.outbox[0]
        self.assertEqual(msg.from_email, f'"{self.a.display_name} (via Orbis)" <no-reply@example.com>')
        self.assertEqual(msg.reply_to, [self.a.email])
        self.assertEqual(msg.cc, [self.a.email])
        self.assertIn(self.a.display_name, msg.body)  # default signature signs as the sender

    def test_sender_not_ccd_on_test_or_when_already_listed(self):
        self.config.sender = self.a
        self.config.save()
        emails.send_team_roster_email(self.team, WEEK, trigger=RosterEmailLog.Trigger.TEST, to=["me@example.com"])
        emails.send_team_roster_email(self.team, WEEK, trigger=RosterEmailLog.Trigger.MANUAL,
                                      to=["to@example.com"], cc=[self.a.email.upper()])
        self.assertEqual(mail.outbox[0].cc, [])
        self.assertEqual(mail.outbox[1].cc, [self.a.email.upper()])

    def test_test_sends_do_not_count_as_sent(self):
        emails.send_team_roster_email(self.team, WEEK, trigger=RosterEmailLog.Trigger.TEST, to=["me@example.com"])
        self.assertIsNone(RosterEmailLog.already_sent(self.team.pk, WEEK))


class TeamCopyTests(RosterEmailTestBase):
    def setUp(self):
        self.config.send_team_copy = True
        self.config.sender = self.a
        self.config.save()
        self.trainee = _user("trainee@example.com", team=self.team)
        JobInfo.objects.create(employee=self.trainee, resource_status=JobInfo.ResourceStatus.TRAINEE)

    def _send(self, trigger=RosterEmailLog.Trigger.MANUAL, to=("client@example.com",)):
        return emails.send_roster(self.team, WEEK, trigger=trigger, to=list(to), config=self.config)

    def test_one_private_copy_per_person_including_trainees(self):
        client_log, copies = self._send()
        self.assertEqual(client_log.kind, RosterEmailLog.Kind.CLIENT)
        self.assertEqual(len(copies), 3)
        client, *team = mail.outbox
        self.assertNotIn(self.trainee.email, client.to + client.cc)
        self.assertEqual(sorted(m.to[0] for m in team),
                         sorted([self.a.email, self.b.email, self.trainee.email]))
        for m in team:
            self.assertEqual(len(m.to), 1)
            self.assertEqual(m.cc, [])
            self.assertNotIn("client@example.com", m.message().as_string())
            self.assertTrue(m.subject.startswith("[Team copy] "))
            self.assertEqual(m.reply_to, [self.a.email])

    def test_recipient_row_highlighted_and_trainee_marked(self):
        self._send()
        copy = next(m for m in mail.outbox[1:] if m.to == [self.trainee.email])
        html = copy.alternatives[0][0]
        self.assertIn(emails.HIGHLIGHT_TINT, html)
        # Exactly one row: SL, name, role + 7 day cells (the trainee has no
        # pattern, so the weekend isn't merged).
        self.assertEqual(html.count(emails.HIGHLIGHT_TINT), 3 + 7)
        self.assertIn("(Trainee)", html)
        self.assertIn("<- you", copy.body)
        self.assertIn(f"Hi {self.trainee.display_name},", copy.body)
        self.assertIn(f"Please reply to {self.a.display_name}", copy.body)
        own = next(m for m in mail.outbox[1:] if m.to == [self.a.email])
        self.assertNotIn("Please reply to", own.body)  # the sender isn't told to reply to himself

    def test_team_copies_do_not_count_as_sent(self):
        RosterEmailLog.objects.all().delete()
        emails.send_team_copies(self.team, WEEK, trigger=RosterEmailLog.Trigger.MANUAL, config=self.config)
        self.assertIsNone(RosterEmailLog.already_sent(self.team.pk, WEEK))

    def test_test_send_only_samples_to_tester(self):
        _, copies = self._send(trigger=RosterEmailLog.Trigger.TEST, to=(self.b.email,))
        self.assertEqual(len(copies), 1)
        self.assertEqual([m.to for m in mail.outbox], [[self.b.email], [self.b.email]])

    def test_off_by_default(self):
        self.config.send_team_copy = False
        self.config.save()
        _, copies = self._send()
        self.assertEqual(copies, [])
        self.assertEqual(len(mail.outbox), 1)


class CommandTests(RosterEmailTestBase):
    def _run(self, *args):
        out = StringIO()
        call_command("send_weekly_rosters", "--week", WEEK.isoformat(), *args, stdout=out)
        return out.getvalue()

    def test_inactive_config_is_not_scheduled(self):
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_scheduled_run_sends_once(self):
        self.config.is_active = True
        self.config.save()
        self._run()
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            RosterEmailLog.objects.filter(trigger=RosterEmailLog.Trigger.SCHEDULED).count(), 1,
        )

    def test_skips_week_already_sent_manually(self):
        self.config.is_active = True
        self.config.save()
        emails.send_team_roster_email(self.team, WEEK, trigger=RosterEmailLog.Trigger.MANUAL, to=["to@example.com"])
        out = self._run()
        self.assertIn("already sent", out)
        self.assertEqual(len(mail.outbox), 1)

    def test_failed_manual_send_does_not_block_schedule(self):
        self.config.is_active = True
        self.config.save()
        RosterEmailLog.objects.create(
            team=self.team, week_start=WEEK, trigger=RosterEmailLog.Trigger.MANUAL,
            status=RosterEmailLog.Status.FAILED, to_emails="x@example.com",
            subject="s", html_body="h", text_body="t",
        )
        self._run()
        self.assertEqual(len(mail.outbox), 1)


class ViewTests(RosterEmailTestBase):
    def _url(self):
        return f"{reverse('roster_email')}?team={self.team.pk}&week={WEEK.isoformat()}"

    def test_outsider_forbidden(self):
        self.client.force_login(_user("outsider@example.com"))
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_team_member_without_scope_forbidden(self):
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_department_head_can_preview_and_send_with_confirm(self):
        self.client.force_login(self.head)
        self.assertEqual(self.client.get(self._url()).status_code, 200)
        data = {"team": self.team.pk, "week": WEEK.isoformat(), "to_emails": "to@example.com",
                "cc_emails": "", "poc": "", "intro_text": "Hi {week}", "signature": "",
                "action": "send", "confirm": "0",
                f"role_{self.a.pk.hex}": "Team Lead", f"role_{self.b.pk.hex}": ""}
        self.client.post(reverse("roster_email"), data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(RosterEmailMember.objects.get(config=self.config).role_label, "Team Lead")
        # Second send needs confirmation…
        resp = self.client.post(reverse("roster_email"), data)
        self.assertContains(resp, "already gone out")
        self.assertEqual(len(mail.outbox), 1)
        # …and goes out once confirmed.
        self.client.post(reverse("roster_email"), {**data, "confirm": "1"})
        self.assertEqual(len(mail.outbox), 2)

    def test_add_person_and_uncheck_to_remove(self):
        other = Team.objects.create(name="Other Team", department=self.dept)
        lead = _user("lead@example.com", team=other)
        self.client.force_login(self.head)
        base = {"team": self.team.pk, "week": WEEK.isoformat(), "to_emails": "to@example.com",
                "intro_text": "x", "action": "save"}
        self.client.post(reverse("roster_email"), {**base, "add_person": str(lead.pk)})
        row = RosterEmailMember.objects.get(config=self.config, employee=lead)
        self.assertTrue(row.always_include)
        # Re-saving with the box ticked keeps them; unticked drops them.
        self.client.post(reverse("roster_email"), {**base, f"include_{lead.pk.hex}": "on",
                                                   f"role_{lead.pk.hex}": "Team Lead"})
        self.assertEqual(RosterEmailMember.objects.get(pk=row.pk).role_label, "Team Lead")
        self.client.post(reverse("roster_email"), base)
        self.assertFalse(RosterEmailMember.objects.filter(pk=row.pk).exists())

    def test_add_person_unknown_pk_rejected(self):
        self.client.force_login(self.head)
        resp = self.client.post(reverse("roster_email"), {
            "team": self.team.pk, "week": WEEK.isoformat(), "to_emails": "to@example.com",
            "intro_text": "x", "action": "save", "add_person": "00000000-0000-0000-0000-000000000000",
        })
        self.assertEqual(resp.status_code, 400)

    def test_people_search(self):
        other = Team.objects.create(name="Other Team", department=self.dept)
        _user("zulu.lead@example.com", team=other)
        self.client.force_login(self.head)
        resp = self.client.get(reverse("roster_email_people_search"), {"q": "zulu"})
        self.assertContains(resp, "zulu.lead@example.com")
        self.client.force_login(_user("nobody@example.com"))
        resp = self.client.get(reverse("roster_email_people_search"), {"q": "zulu"})
        self.assertEqual(resp.status_code, 403)

    def test_hr_can_open(self):
        hr = _user("hr@example.com")
        hr.groups.add(Group.objects.get_or_create(name="HR")[0])
        self.client.force_login(hr)
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_bad_email_rejected(self):
        self.client.force_login(self.head)
        resp = self.client.post(reverse("roster_email"), {
            "team": self.team.pk, "week": WEEK.isoformat(), "to_emails": "not-an-email",
            "intro_text": "x", "action": "save",
        })
        self.assertEqual(resp.status_code, 400)
