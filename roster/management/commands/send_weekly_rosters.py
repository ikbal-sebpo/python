"""Email next week's roster to every team with an active RosterEmailConfig.

Fired by celery-beat every Friday 22:00 Asia/Dhaka (the "Send weekly team
rosters" periodic task — a django_celery_beat row, see
docs/SCHEDULER_ADMIN_PLAN.md). A team whose week already went out — a manual send
from the roster page, or an earlier run — is skipped, so re-running is safe.

    manage.py send_weekly_rosters                         # the scheduled run
    manage.py send_weekly_rosters --team 86 --dry-run     # what would go out
    manage.py send_weekly_rosters --team 86 --to me@sebpo.com   # test send

`--to` sends a TEST-trigger copy to just that address (plus a sample team
copy when the team has them on and the address is an employee's), ignores
`is_active` and the already-sent check, and never marks the week as sent.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from roster import emails
from roster.models import RosterEmailConfig, RosterEmailLog


class Command(BaseCommand):
    help = "Email next week's roster to every team with an active roster email config."

    def add_arguments(self, parser):
        parser.add_argument("--team", type=int, help="Only this Team id.")
        parser.add_argument("--week", help="Any date in the target week (default: next week).")
        parser.add_argument("--to", help="Send a test copy to this address only.")
        parser.add_argument("--dry-run", action="store_true", help="Report, don't send.")

    def handle(self, *args, team=None, week=None, to=None, dry_run=False, **opts):
        try:
            week_start = emails.monday_of(date.fromisoformat(week)) if week else emails.next_week_start()
        except ValueError as exc:
            raise CommandError(f"--week: {exc}")

        configs = RosterEmailConfig.objects.select_related("team", "team__department")
        if team:
            configs = configs.filter(team_id=team)
            if not configs.exists():
                raise CommandError(f"No roster email config for team {team}.")
        if not to:
            configs = configs.filter(is_active=True, team__is_active=True)
            if team and not configs.exists():
                self.stdout.write(f"Team {team}'s roster email is not set to send automatically.")

        label = emails.week_label(week_start)
        sent = skipped = 0
        for config in configs:
            t = config.team
            if to:
                recipients, cc, trigger = [to], [], RosterEmailLog.Trigger.TEST
            else:
                prior = RosterEmailLog.already_sent(t.pk, week_start)
                if prior:
                    who = prior.sent_by.display_name if prior.sent_by else "schedule"
                    self.stdout.write(f"skip  {t} {label}: already sent ({who}, {prior.created_at:%Y-%m-%d %H:%M})")
                    skipped += 1
                    continue
                recipients, cc, trigger = config.to_list, config.cc_list, RosterEmailLog.Trigger.SCHEDULED
                if not recipients:
                    self.stdout.write(f"skip  {t} {label}: no recipients configured")
                    skipped += 1
                    continue
            if dry_run:
                self.stdout.write(f"would send {t} {label} → {', '.join(recipients + cc)}")
                continue
            log, copies = emails.send_roster(t, week_start, trigger=trigger, to=recipients, cc=cc, config=config)
            team_note = f" + {len(copies)} team cop{'y' if len(copies) == 1 else 'ies'}" if copies else ""
            self.stdout.write(f"sent  {t} {label} → {', '.join(recipients + cc)} (log {log.pk}){team_note}")
            sent += 1
        self.stdout.write(self.style.SUCCESS(f"{label}: {sent} sent, {skipped} skipped."))
