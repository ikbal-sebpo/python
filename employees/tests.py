from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AccessLevel, Designation
from employees.models import (
    DedicatedNightShiftAssignment,
    DedicatedNightShiftAssignmentEvent,
    JobInfo,
    NightShiftConsent,
    NightShiftConsentEvent,
    PersonalInfo,
)
from employees.services import (
    get_or_create_night_shift_consent,
    qualifies_for_night_shift_consent,
    record_night_shift_consent_decision,
)

User = get_user_model()


def _user(email):
    return User.objects.create_user(
        email=email, username=email, password="test-1234!",
        first_name=email.split("@")[0].title(), last_name="Test",
    )


class DedicatedNightShiftAssignmentTests(TestCase):
    """docs/PAYROLL_PLAN.md, 2026-09-07 — the model-layer overlap guard
    (MySQL doesn't enforce partial-unique/range constraints)."""

    def setUp(self):
        self.employee = _user("night@example.com")

    def test_end_date_before_start_date_rejected(self):
        assignment = DedicatedNightShiftAssignment(
            employee=self.employee, start_date=date(2026, 7, 1), end_date=date(2026, 6, 1),
        )
        with self.assertRaises(ValidationError):
            assignment.clean()

    def test_overlapping_approved_windows_rejected(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.employee, start_date=date(2026, 7, 1), end_date=date(2026, 9, 30),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        second = DedicatedNightShiftAssignment(
            employee=self.employee, start_date=date(2026, 9, 1), end_date=date(2026, 11, 30),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        with self.assertRaises(ValidationError):
            second.clean()

    def test_non_overlapping_approved_windows_allowed(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.employee, start_date=date(2026, 1, 1), end_date=date(2026, 3, 31),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        second = DedicatedNightShiftAssignment(
            employee=self.employee, start_date=date(2026, 7, 1), end_date=date(2026, 9, 30),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
        )
        second.clean()  # does not raise

    def test_overlapping_pending_windows_allowed(self):
        # Only APPROVED rows are checked for overlap — two PENDING requests
        # covering the same window can coexist until someone decides them.
        DedicatedNightShiftAssignment.objects.create(
            employee=self.employee, start_date=date(2026, 7, 1), end_date=date(2026, 9, 30),
            status=DedicatedNightShiftAssignment.Status.PENDING,
        )
        second = DedicatedNightShiftAssignment(
            employee=self.employee, start_date=date(2026, 7, 15), end_date=date(2026, 8, 15),
            status=DedicatedNightShiftAssignment.Status.PENDING,
        )
        second.clean()  # does not raise


class DedicatedNightShiftEditViewTests(TestCase):
    """The profile card's per-row Edit action (docs/PAYROLL_PLAN.md).

    2026-09-08: PENDING editable by the TL+ who could have requested it,
    APPROVED only by an APM+, DECLINED/CANCELLED not at all.

    2026-09-15: APPROVED is now editable at the TL+ bar too, because
    Supervisors are the ones maintaining these windows. The pay control
    moved rather than disappearing — a below-Manager editor's save demotes
    the row back to PENDING and clears the approval stamp, so the revised
    window earns nothing until an APM/PM re-approves it.
    """

    def setUp(self):
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        manager_level = AccessLevel.objects.create(name="Manager", rank=30)
        team_lead = Designation.objects.create(name="Team Lead", access_level=supervisor_level)
        program_manager = Designation.objects.create(name="Program Manager", access_level=manager_level)

        self.apm = _user("apm@example.com")
        self.apm.designation = program_manager
        self.apm.save()

        self.tl = _user("tl@example.com")
        self.tl.designation = team_lead
        self.tl.save()

        self.emp = _user("emp@example.com")

        JobInfo.objects.create(employee=self.tl, reports_to=self.apm)
        JobInfo.objects.create(employee=self.emp, reports_to=self.tl)

    def _pending(self):
        return DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
            status=DedicatedNightShiftAssignment.Status.PENDING, added_by=self.tl,
        )

    def _approved(self):
        return DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
            status=DedicatedNightShiftAssignment.Status.APPROVED, added_by=self.tl,
            approved_by=self.apm, approved_at=timezone.now(),
        )

    def test_tl_can_edit_pending(self):
        assignment = self._pending()
        self.client.force_login(self.tl)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-01-15", "note": "extended"})
        self.assertEqual(resp.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.end_date, date(2027, 1, 15))
        self.assertEqual(assignment.status, DedicatedNightShiftAssignment.Status.PENDING)

    def test_tl_can_open_approved_editor(self):
        assignment = self._approved()
        self.client.force_login(self.tl)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # The drawer warns before the save, not only after it.
        self.assertTrue(resp.context["demotes_on_save"])
        self.assertContains(resp, "sends this assignment back for approval")

    def test_tl_edit_of_approved_demotes_to_pending(self):
        assignment = self._approved()
        self.client.force_login(self.tl)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-01-15", "note": ""})
        self.assertEqual(resp.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.end_date, date(2027, 1, 15))
        self.assertEqual(assignment.status, DedicatedNightShiftAssignment.Status.PENDING)
        self.assertIsNone(assignment.approved_by)
        self.assertIsNone(assignment.approved_at)
        self.assertEqual(assignment.updated_by, self.tl)

    def test_tl_edit_of_approved_warns_about_re_approval(self):
        assignment = self._approved()
        self.client.force_login(self.tl)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-01-15", "note": ""})
        texts = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(
            any("sent back for approval" in t for t in texts),
            f"expected a re-approval warning, got {texts!r}",
        )

    def test_tl_demoted_row_returns_to_the_apm_pending_queue(self):
        assignment = self._approved()
        self.client.force_login(self.tl)
        self.client.post(
            reverse("dedicated_night_shift_edit", args=[assignment.pk]),
            {"start_date": "2026-10-01", "end_date": "2027-01-15", "note": ""},
        )
        self.client.force_login(self.apm)
        resp = self.client.get(f"{reverse('dedicated_night_shift_list')}?tab=pending")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(assignment.pk, [a.pk for a in resp.context["rows"]])
        self.assertIn(assignment.pk, resp.context["actionable_pks"])

    def test_tl_edit_of_approved_into_an_overlap_is_rejected_not_demoted(self):
        """The overlap guard survives the demotion path, and does so by
        design rather than by luck: form.is_valid() runs
        ModelForm._post_clean() -> instance.full_clean() while the instance
        still carries APPROVED (status isn't a form field), so the model's
        APPROVED-only overlap check fires before the demotion. The row is
        rejected outright rather than saved as a PENDING clash nobody could
        ever approve. Regression guard: demoting before the is_valid() call
        would silently turn this check off."""
        other = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2027, 3, 1), end_date=date(2027, 5, 31),
            status=DedicatedNightShiftAssignment.Status.APPROVED, approved_by=self.apm,
        )
        assignment = self._approved()
        self.client.force_login(self.tl)
        resp = self.client.post(
            reverse("dedicated_night_shift_edit", args=[assignment.pk]),
            {"start_date": "2026-10-01", "end_date": "2027-03-15", "note": ""},
        )
        self.assertEqual(resp.status_code, 400)
        assignment.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(assignment.end_date, date(2026, 12, 31))
        self.assertEqual(assignment.status, DedicatedNightShiftAssignment.Status.APPROVED)
        self.assertEqual(assignment.approved_by, self.apm)
        self.assertEqual(other.status, DedicatedNightShiftAssignment.Status.APPROVED)

    def test_apm_can_edit_approved_in_place(self):
        assignment = self._approved()
        self.client.force_login(self.apm)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.get(url)
        self.assertFalse(resp.context["demotes_on_save"])
        resp = self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-02-28", "note": ""})
        self.assertEqual(resp.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.end_date, date(2027, 2, 28))
        # An editor who could have approved it keeps it approved, stamp intact.
        self.assertEqual(assignment.status, DedicatedNightShiftAssignment.Status.APPROVED)
        self.assertEqual(assignment.approved_by, self.apm)
        self.assertIsNotNone(assignment.approved_at)

    def test_editing_into_an_overlap_is_rejected(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2027, 3, 1), end_date=date(2027, 5, 31),
            status=DedicatedNightShiftAssignment.Status.APPROVED, approved_by=self.apm,
        )
        assignment = self._approved()
        self.client.force_login(self.apm)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-03-15", "note": ""})
        self.assertEqual(resp.status_code, 400)
        assignment.refresh_from_db()
        self.assertEqual(assignment.end_date, date(2026, 12, 31))

    def test_declined_assignment_not_editable_even_by_apm(self):
        assignment = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
            status=DedicatedNightShiftAssignment.Status.DECLINED, added_by=self.tl,
        )
        self.client.force_login(self.apm)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_htmx_success_returns_204_with_hx_refresh(self):
        assignment = self._pending()
        self.client.force_login(self.tl)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        resp = self.client.post(
            url, {"start_date": "2026-10-01", "end_date": "2027-01-15", "note": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["HX-Refresh"], "true")


class DedicatedNightShiftAuditLedgerTests(TestCase):
    """2026-09-15 — DedicatedNightShiftAssignmentEvent, the append-only
    ledger this model shipped without.

    The gap it closes: `_Auditable` keeps one updated_by/updated_at pair,
    so a second edit overwrote the first and nothing recorded *what*
    changed. Every write path funnels through
    services.record_dedicated_night_shift_event, so the single-row and bulk
    approve/decline actions record without knowing they do.
    """

    def setUp(self):
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        manager_level = AccessLevel.objects.create(name="Manager", rank=30)
        team_lead = Designation.objects.create(name="Team Lead", access_level=supervisor_level)
        program_manager = Designation.objects.create(name="Program Manager", access_level=manager_level)

        self.apm = _user("apm-ledger@example.com")
        self.apm.designation = program_manager
        self.apm.save()

        self.tl = _user("tl-ledger@example.com")
        self.tl.designation = team_lead
        self.tl.save()

        self.emp = _user("emp-ledger@example.com")

        JobInfo.objects.create(employee=self.tl, reports_to=self.apm)
        JobInfo.objects.create(employee=self.emp, reports_to=self.tl)

    def _events(self, assignment):
        return list(assignment.events.order_by("occurred_at", "pk"))

    def _approved(self):
        return DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
            status=DedicatedNightShiftAssignment.Status.APPROVED, added_by=self.tl,
            approved_by=self.apm, approved_at=timezone.now(),
        )

    def test_request_records_a_requested_event(self):
        self.client.force_login(self.tl)
        self.client.post(
            f"{reverse('dedicated_night_shift_request')}?for={self.emp.pk}",
            {"start_date": "2026-10-01", "end_date": "2026-12-31", "note": ""},
        )
        assignment = DedicatedNightShiftAssignment.objects.get()
        events = self._events(assignment)
        self.assertEqual([e.event for e in events], [DedicatedNightShiftAssignmentEvent.Event.REQUESTED])
        self.assertEqual(events[0].actor, self.tl)
        self.assertEqual(events[0].new_end_date, date(2026, 12, 31))

    def test_tl_edit_of_approved_records_a_demoted_event_with_both_windows(self):
        assignment = self._approved()
        self.client.force_login(self.tl)
        self.client.post(
            reverse("dedicated_night_shift_edit", args=[assignment.pk]),
            {"start_date": "2026-10-01", "end_date": "2027-01-15", "note": ""},
        )
        event = self._events(assignment)[-1]
        self.assertEqual(event.event, DedicatedNightShiftAssignmentEvent.Event.DEMOTED)
        self.assertEqual(event.actor, self.tl)
        self.assertEqual(event.old_end_date, date(2026, 12, 31))
        self.assertEqual(event.new_end_date, date(2027, 1, 15))
        self.assertTrue(event.window_changed)

    def test_manager_edit_records_an_edited_event_not_a_demoted_one(self):
        assignment = self._approved()
        self.client.force_login(self.apm)
        self.client.post(
            reverse("dedicated_night_shift_edit", args=[assignment.pk]),
            {"start_date": "2026-10-01", "end_date": "2027-02-28", "note": ""},
        )
        event = self._events(assignment)[-1]
        self.assertEqual(event.event, DedicatedNightShiftAssignmentEvent.Event.EDITED)
        self.assertEqual(event.actor, self.apm)

    def test_second_edit_does_not_overwrite_the_first(self):
        """The whole point of the ledger — _Auditable's single updated_by
        pair loses the earlier editor, the ledger keeps both."""
        assignment = self._approved()
        self.client.force_login(self.apm)
        url = reverse("dedicated_night_shift_edit", args=[assignment.pk])
        self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-02-28", "note": ""})
        self.client.force_login(self.tl)
        self.client.post(url, {"start_date": "2026-10-01", "end_date": "2027-03-31", "note": ""})

        events = self._events(assignment)
        self.assertEqual(
            [e.event for e in events],
            [
                DedicatedNightShiftAssignmentEvent.Event.EDITED,
                DedicatedNightShiftAssignmentEvent.Event.DEMOTED,
            ],
        )
        self.assertEqual([e.actor for e in events], [self.apm, self.tl])
        # The row itself only remembers the last writer.
        assignment.refresh_from_db()
        self.assertEqual(assignment.updated_by, self.tl)

    def test_approve_records_an_event(self):
        assignment = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
            status=DedicatedNightShiftAssignment.Status.PENDING, added_by=self.tl,
        )
        self.client.force_login(self.apm)
        self.client.post(reverse("dedicated_night_shift_approve", args=[assignment.pk]))
        event = self._events(assignment)[-1]
        self.assertEqual(event.event, DedicatedNightShiftAssignmentEvent.Event.APPROVED)
        self.assertEqual(event.actor, self.apm)

    def test_decline_records_an_event_carrying_the_reason(self):
        assignment = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31),
            status=DedicatedNightShiftAssignment.Status.PENDING, added_by=self.tl,
        )
        self.client.force_login(self.apm)
        self.client.post(
            reverse("dedicated_night_shift_decline", args=[assignment.pk]),
            {"decline_reason": "Team already covered"},
        )
        event = self._events(assignment)[-1]
        self.assertEqual(event.event, DedicatedNightShiftAssignmentEvent.Event.DECLINED)
        self.assertEqual(event.note, "Team already covered")

    def test_bulk_approve_records_events_too(self):
        """_approve_night_shift is shared by the single and bulk views, so
        the bulk path can't silently skip the ledger."""
        assignments = [
            DedicatedNightShiftAssignment.objects.create(
                employee=self.emp, start_date=date(2026, 10, 1) + timedelta(days=200 * i),
                end_date=date(2026, 12, 31) + timedelta(days=200 * i),
                status=DedicatedNightShiftAssignment.Status.PENDING, added_by=self.tl,
            )
            for i in range(2)
        ]
        self.client.force_login(self.apm)
        self.client.post(
            reverse("dedicated_night_shift_bulk"),
            {"action": "approve", "ids": [a.pk for a in assignments], "tab": "pending"},
        )
        for a in assignments:
            a.refresh_from_db()
            self.assertEqual(a.status, DedicatedNightShiftAssignment.Status.APPROVED)
            self.assertEqual(
                self._events(a)[-1].event, DedicatedNightShiftAssignmentEvent.Event.APPROVED,
            )

    def test_history_view_is_open_to_anyone_who_can_view_the_employee(self):
        assignment = self._approved()
        self.client.force_login(self.tl)
        resp = self.client.get(reverse("dedicated_night_shift_history", args=[assignment.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["events"]), 0)

    def test_history_view_403s_for_someone_outside_the_employees_scope(self):
        assignment = self._approved()
        outsider = _user("outsider-ledger@example.com")
        self.client.force_login(outsider)
        resp = self.client.get(reverse("dedicated_night_shift_history", args=[assignment.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_window_changed_is_false_when_only_the_note_moved(self):
        assignment = self._approved()
        self.client.force_login(self.apm)
        self.client.post(
            reverse("dedicated_night_shift_edit", args=[assignment.pk]),
            {"start_date": "2026-10-01", "end_date": "2026-12-31", "note": "clarified"},
        )
        event = self._events(assignment)[-1]
        self.assertFalse(event.window_changed)


class DedicatedNightShiftBulkActionViewTests(TestCase):
    """Bulk approve/decline toolbar on /employees/dedicated-night-shifts/?tab=pending,
    added 2026-09-11 to match attendance.views.AttendanceCorrectionBulkActionView's
    shape. Only an APM+ can approve (can_approve_dedicated_night_shift); a TL
    can request but not approve, so should never act on anything via this view."""

    def setUp(self):
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        manager_level = AccessLevel.objects.create(name="Manager", rank=30)
        team_lead = Designation.objects.create(name="Team Lead", access_level=supervisor_level)
        program_manager = Designation.objects.create(name="Program Manager", access_level=manager_level)

        self.apm = _user("bulk_apm@example.com")
        self.apm.designation = program_manager
        self.apm.save()

        self.tl = _user("bulk_tl@example.com")
        self.tl.designation = team_lead
        self.tl.save()

        self.emp1 = _user("bulk_emp1@example.com")
        self.emp2 = _user("bulk_emp2@example.com")
        JobInfo.objects.create(employee=self.emp1, reports_to=self.apm)
        JobInfo.objects.create(employee=self.emp2, reports_to=self.apm)

        self.a1 = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp1, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31), added_by=self.apm,
        )
        self.a2 = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp2, start_date=date(2026, 10, 1), end_date=date(2026, 12, 31), added_by=self.apm,
        )

    def test_apm_bulk_approve(self):
        self.client.force_login(self.apm)
        resp = self.client.post(reverse("dedicated_night_shift_bulk"), {
            "action": "approve", "ids": [self.a1.pk, self.a2.pk], "tab": "pending",
        })
        self.assertRedirects(resp, f"{reverse('dedicated_night_shift_list')}?tab=pending")
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertEqual(self.a1.status, DedicatedNightShiftAssignment.Status.APPROVED)
        self.assertEqual(self.a2.status, DedicatedNightShiftAssignment.Status.APPROVED)
        self.assertEqual(self.a1.approved_by, self.apm)

    def test_apm_bulk_decline(self):
        self.client.force_login(self.apm)
        resp = self.client.post(reverse("dedicated_night_shift_bulk"), {
            "action": "decline", "ids": [self.a1.pk, self.a2.pk], "tab": "pending",
        })
        self.assertRedirects(resp, f"{reverse('dedicated_night_shift_list')}?tab=pending")
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertEqual(self.a1.status, DedicatedNightShiftAssignment.Status.DECLINED)
        self.assertEqual(self.a2.status, DedicatedNightShiftAssignment.Status.DECLINED)

    def test_tl_cannot_bulk_approve(self):
        """TL-tier can request a night shift but not approve one — the bulk
        view must re-check can_approve_dedicated_night_shift per id, not
        just trust the submitted list."""
        self.client.force_login(self.tl)
        resp = self.client.post(reverse("dedicated_night_shift_bulk"), {
            "action": "approve", "ids": [self.a1.pk, self.a2.pk], "tab": "pending",
        })
        self.assertEqual(resp.status_code, 302)
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertEqual(self.a1.status, DedicatedNightShiftAssignment.Status.PENDING)
        self.assertEqual(self.a2.status, DedicatedNightShiftAssignment.Status.PENDING)

    def test_bulk_approve_skips_row_that_would_overlap(self):
        DedicatedNightShiftAssignment.objects.create(
            employee=self.emp1, start_date=date(2026, 11, 1), end_date=date(2027, 1, 31),
            status=DedicatedNightShiftAssignment.Status.APPROVED, approved_by=self.apm,
        )
        self.client.force_login(self.apm)
        resp = self.client.post(reverse("dedicated_night_shift_bulk"), {
            "action": "approve", "ids": [self.a1.pk, self.a2.pk], "tab": "pending",
        })
        self.assertEqual(resp.status_code, 302)
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        # a1 overlaps the pre-existing approved window for emp1 and is skipped;
        # a2 (a different employee) still goes through.
        self.assertEqual(self.a1.status, DedicatedNightShiftAssignment.Status.PENDING)
        self.assertEqual(self.a2.status, DedicatedNightShiftAssignment.Status.APPROVED)

    def test_no_ids_selected_is_a_noop(self):
        self.client.force_login(self.apm)
        resp = self.client.post(reverse("dedicated_night_shift_bulk"), {"action": "approve", "tab": "pending"})
        self.assertEqual(resp.status_code, 302)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.status, DedicatedNightShiftAssignment.Status.PENDING)


def _shift_type(name, start, end):
    from roster.models import ShiftType

    return ShiftType.objects.create(
        name=name, category=ShiftType.Category.WORK,
        shift_start_time=start, shift_end_time=end,
    )


class NightShiftConsentTests(TestCase):
    """Bangladesh Labour Act 2006 s.109 (amended 2018) — female employees'
    monthly night-shift consent. See employees/models.py::NightShiftConsent
    and employees/services.py."""

    def setUp(self):
        from roster.models import Shift

        self.Shift = Shift
        self.employee = _user("kanta@example.com")
        PersonalInfo.objects.create(employee=self.employee, gender=PersonalInfo.Gender.FEMALE)
        self.male_employee = _user("shakil@example.com")
        PersonalInfo.objects.create(employee=self.male_employee, gender=PersonalInfo.Gender.MALE)
        # Crosses midnight -> night shift (payroll.inputs.is_night_shift).
        self.night = _shift_type("NIG", time(19, 0), time(4, 0))
        self.day = _shift_type("MOR", time(9, 0), time(18, 0))
        self.period = date.today().replace(day=1)

    def test_male_employee_never_qualifies(self):
        self.Shift.objects.create(employee=self.male_employee, date=date.today(), shift_type=self.night)
        self.assertFalse(qualifies_for_night_shift_consent(self.male_employee, self.period))

    def test_female_employee_without_night_shift_does_not_qualify(self):
        self.Shift.objects.create(employee=self.employee, date=date.today(), shift_type=self.day)
        self.assertFalse(qualifies_for_night_shift_consent(self.employee, self.period))

    def test_female_employee_with_night_shift_qualifies(self):
        self.Shift.objects.create(employee=self.employee, date=date.today(), shift_type=self.night)
        self.assertTrue(qualifies_for_night_shift_consent(self.employee, self.period))

    def test_get_or_create_defaults_to_true_and_logs_generated_event(self):
        self.Shift.objects.create(employee=self.employee, date=date.today(), shift_type=self.night)
        consent, created = get_or_create_night_shift_consent(self.employee, self.period)
        self.assertTrue(created)
        self.assertTrue(consent.consents)
        self.assertEqual(consent.source, NightShiftConsent.Source.AUTO_DEFAULT)
        self.assertEqual(
            list(consent.events.values_list("event", flat=True)),
            [NightShiftConsentEvent.Event.GENERATED],
        )

    def test_get_or_create_returns_none_for_ineligible_employee(self):
        consent, created = get_or_create_night_shift_consent(self.employee, self.period)
        self.assertIsNone(consent)
        self.assertFalse(created)

    def test_second_get_or_create_call_does_not_duplicate(self):
        self.Shift.objects.create(employee=self.employee, date=date.today(), shift_type=self.night)
        first, _ = get_or_create_night_shift_consent(self.employee, self.period)
        second, created = get_or_create_night_shift_consent(self.employee, self.period)
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(NightShiftConsent.objects.filter(employee=self.employee, period=self.period).count(), 1)

    def test_record_decision_decline_updates_state_and_ledger(self):
        self.Shift.objects.create(employee=self.employee, date=date.today(), shift_type=self.night)
        record_night_shift_consent_decision(
            self.employee, False, actor=self.employee, note="Can't work nights this month",
        )
        consent = NightShiftConsent.objects.get(employee=self.employee, period=self.period)
        self.assertFalse(consent.consents)
        self.assertEqual(consent.source, NightShiftConsent.Source.EMPLOYEE)
        self.assertEqual(consent.decided_by, self.employee)
        self.assertEqual(
            list(consent.events.order_by("occurred_at").values_list("event", flat=True)),
            [NightShiftConsentEvent.Event.GENERATED, NightShiftConsentEvent.Event.DECLINED],
        )

    def test_record_decision_by_hr_sets_hr_on_behalf_source(self):
        self.Shift.objects.create(employee=self.employee, date=date.today(), shift_type=self.night)
        hr_user = _user("hr@example.com")
        record_night_shift_consent_decision(self.employee, True, actor=hr_user, note="Captured verbally")
        consent = NightShiftConsent.objects.get(employee=self.employee, period=self.period)
        self.assertEqual(consent.source, NightShiftConsent.Source.HR_ON_BEHALF)
        self.assertEqual(consent.decided_by, hr_user)

    def test_record_decision_raises_for_ineligible_employee(self):
        with self.assertRaises(ValidationError):
            record_night_shift_consent_decision(self.employee, False, actor=self.employee)


class NightShiftConsentViewTests(TestCase):
    def setUp(self):
        from roster.models import Shift

        self.employee = _user("kanta2@example.com")
        PersonalInfo.objects.create(employee=self.employee, gender=PersonalInfo.Gender.FEMALE)
        night = _shift_type("NIG2", time(19, 0), time(4, 0))
        Shift.objects.create(employee=self.employee, date=date.today(), shift_type=night)
        self.other_employee = _user("someone-else@example.com")
        PersonalInfo.objects.create(employee=self.other_employee, gender=PersonalInfo.Gender.FEMALE)
        Shift.objects.create(employee=self.other_employee, date=date.today(), shift_type=night)

    def test_self_can_decline_own_consent(self):
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse("night_shift_consent_update"), {"consents": "false", "note": "test"},
        )
        self.assertEqual(resp.status_code, 302)
        consent = NightShiftConsent.objects.get(
            employee=self.employee, period=date.today().replace(day=1),
        )
        self.assertFalse(consent.consents)

    def test_non_hr_cannot_decide_for_someone_else(self):
        self.client.force_login(self.employee)
        resp = self.client.post(
            f"{reverse('night_shift_consent_update')}?for={self.other_employee.pk}",
            {"consents": "false"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_non_hr_cannot_view_audit_list(self):
        # No ProtectedView row is seeded in the test DB, so
        # dynamic_allowed_users redirects home before the view's own
        # can_manage_night_shift_consent check even runs — same "deny when
        # misconfigured" behaviour every other ProtectedView-gated page has.
        self.client.force_login(self.employee)
        resp = self.client.get(reverse("night_shift_consent_list"))
        self.assertEqual(resp.status_code, 302)


class DedicatedNightShiftManageFromListTests(TestCase):
    """Managing windows from the list page (Roster → Dedicated Night Shift,
    added 2026-11-03): the "My team" tab, "New request" with an employee
    picker, filters, and Cancel."""

    def setUp(self):
        supervisor_level = AccessLevel.objects.create(name="Supervisor", rank=20)
        manager_level = AccessLevel.objects.create(name="Manager", rank=30)
        team_lead = Designation.objects.create(name="Team Lead", access_level=supervisor_level)
        program_manager = Designation.objects.create(name="Program Manager", access_level=manager_level)

        self.apm = _user("mgr_apm@example.com")
        self.apm.designation = program_manager
        self.apm.save()
        self.tl = _user("mgr_tl@example.com")
        self.tl.designation = team_lead
        self.tl.save()
        self.emp = _user("mgr_emp@example.com")
        self.outsider = _user("mgr_out@example.com")
        self.hr = _user("mgr_hr@example.com")
        self.hr.is_superuser = True
        self.hr.save()

        JobInfo.objects.create(employee=self.tl, reports_to=self.apm)
        JobInfo.objects.create(employee=self.emp, reports_to=self.tl)

        self.today = timezone.localdate()
        self.approved = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=self.today - timedelta(days=10),
            end_date=self.today + timedelta(days=80),
            status=DedicatedNightShiftAssignment.Status.APPROVED,
            added_by=self.tl, approved_by=self.apm, approved_at=timezone.now(),
        )
        self.outsider_row = DedicatedNightShiftAssignment.objects.create(
            employee=self.outsider, start_date=self.today, end_date=self.today + timedelta(days=30),
        )

    def _list(self, user, query=""):
        self.client.force_login(user)
        resp = self.client.get(f"{reverse('dedicated_night_shift_list')}{query}")
        self.assertEqual(resp.status_code, 200)
        return resp

    # --- My team tab ------------------------------------------------------

    def test_tl_defaults_to_team_tab_and_sees_approved_rows(self):
        resp = self._list(self.tl)
        self.assertEqual(resp.context["tab"], "team")
        pks = [a.pk for a in resp.context["rows"]]
        self.assertIn(self.approved.pk, pks)
        self.assertNotIn(self.outsider_row.pk, pks)
        self.assertIn(self.approved.pk, resp.context["editable_pks"])
        self.assertTrue(resp.context["can_request"])

    def test_plain_employee_gets_only_mine(self):
        resp = self._list(self.emp)
        self.assertFalse(resp.context["show_team_tab"])
        self.assertEqual(resp.context["tab"], "mine")
        self.assertFalse(resp.context["can_request"])
        # A hand-typed ?tab=team falls back to their own rows.
        resp = self._list(self.emp, "?tab=team")
        self.assertEqual(resp.context["tab"], "mine")
        self.assertEqual([a.pk for a in resp.context["rows"]], [self.approved.pk])

    def test_filters_narrow_rows(self):
        resp = self._list(self.hr, "?tab=all&status=PENDING")
        self.assertEqual([a.pk for a in resp.context["rows"]], [self.outsider_row.pk])
        resp = self._list(self.hr, f"?tab=all&active_on={self.today - timedelta(days=5)}")
        self.assertEqual([a.pk for a in resp.context["rows"]], [self.approved.pk])
        resp = self._list(self.hr, "?tab=all&q=mgr_out@example.com")
        self.assertEqual([a.pk for a in resp.context["rows"]], [self.outsider_row.pk])

    # --- New request with picker -------------------------------------------

    def _request(self, user, employee):
        self.client.force_login(user)
        return self.client.post(reverse("dedicated_night_shift_request"), {
            "employee": str(employee.pk),
            "start_date": str(self.today + timedelta(days=100)),
            "end_date": str(self.today + timedelta(days=190)),
            "note": "",
        })

    def test_tl_requests_for_team_member_via_picker(self):
        resp = self._request(self.tl, self.emp)
        self.assertEqual(resp.status_code, 302)
        row = DedicatedNightShiftAssignment.objects.get(
            employee=self.emp, status=DedicatedNightShiftAssignment.Status.PENDING,
        )
        self.assertEqual(row.added_by, self.tl)
        self.assertEqual(row.events.get().event, DedicatedNightShiftAssignmentEvent.Event.REQUESTED)

    def test_tl_cannot_pick_outside_team_or_self(self):
        for target in (self.outsider, self.tl):
            resp = self._request(self.tl, target)
            self.assertEqual(resp.status_code, 400)
        self.assertFalse(DedicatedNightShiftAssignment.objects.filter(employee=self.tl).exists())
        self.assertEqual(DedicatedNightShiftAssignment.objects.filter(employee=self.outsider).count(), 1)

    def test_new_request_drawer_renders_picker(self):
        self.client.force_login(self.tl)
        resp = self.client.get(reverse("dedicated_night_shift_request"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("dedicated_night_shift_candidates"))
        self.assertNotContains(resp, 'name="for"')

    def test_plain_employee_cannot_open_picker_form(self):
        self.client.force_login(self.emp)
        resp = self.client.get(reverse("dedicated_night_shift_request"))
        self.assertEqual(resp.status_code, 403)

    def test_candidates_endpoint_is_scoped(self):
        self.client.force_login(self.tl)
        url = reverse("dedicated_night_shift_candidates")
        self.assertContains(self.client.get(url, {"q": "Mgr_Emp"}), "mgr_emp@example.com")
        self.assertNotContains(self.client.get(url, {"q": "Mgr_Out"}), "mgr_out@example.com")

    # --- Cancel -------------------------------------------------------------

    def _cancel(self, user, assignment, reason=""):
        self.client.force_login(user)
        return self.client.post(
            reverse("dedicated_night_shift_cancel", args=[assignment.pk]), {"reason": reason},
        )

    def test_tl_cancels_pending_with_reason_on_ledger(self):
        pending = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=self.today + timedelta(days=100),
            end_date=self.today + timedelta(days=190), added_by=self.tl,
        )
        resp = self._cancel(self.tl, pending, "Rota changed")
        self.assertEqual(resp.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.status, DedicatedNightShiftAssignment.Status.CANCELLED)
        event = pending.events.get()
        self.assertEqual(event.event, DedicatedNightShiftAssignmentEvent.Event.CANCELLED)
        self.assertEqual(event.note, "Rota changed")
        self.assertEqual(event.actor, self.tl)

    def test_tl_cannot_cancel_approved(self):
        self.assertEqual(self._cancel(self.tl, self.approved).status_code, 403)

    def test_apm_cancels_approved_only_before_it_starts(self):
        # Already started: an end_date edit is the way to end it early.
        self.assertEqual(self._cancel(self.apm, self.approved).status_code, 403)
        future = DedicatedNightShiftAssignment.objects.create(
            employee=self.emp, start_date=self.today + timedelta(days=100),
            end_date=self.today + timedelta(days=190),
            status=DedicatedNightShiftAssignment.Status.APPROVED, added_by=self.tl,
            approved_by=self.apm, approved_at=timezone.now(),
        )
        self.assertEqual(self._cancel(self.apm, future).status_code, 302)
        future.refresh_from_db()
        self.assertEqual(future.status, DedicatedNightShiftAssignment.Status.CANCELLED)

    def test_hr_can_cancel_started_approved(self):
        self.assertEqual(self._cancel(self.hr, self.approved).status_code, 302)
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.status, DedicatedNightShiftAssignment.Status.CANCELLED)

    def test_cancelled_is_terminal(self):
        self._cancel(self.hr, self.approved)
        self.assertEqual(self._cancel(self.hr, self.approved).status_code, 403)

    def test_cancel_next_rejects_offsite_redirect(self):
        self.client.force_login(self.hr)
        resp = self.client.post(
            reverse("dedicated_night_shift_cancel", args=[self.outsider_row.pk]),
            {"next": "https://evil.example.com/"},
        )
        self.assertRedirects(resp, reverse("dedicated_night_shift_list"), fetch_redirect_response=False)
