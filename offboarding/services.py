"""Service layer for offboarding.

Houses the operations that mutate state across multiple models:

* ``create_case_from_resignation`` — builds an ``OffboardingCase`` + its
  ``ClearanceStep`` rows from the default workflow template
* ``_resolve_assignee`` — translates ``MANAGER/DEPT_HEAD/HR_HEAD`` step
  kinds into concrete ``(user, group)`` pairs at case-creation time
* ``refresh_case_status`` — recalculates case status after every step save
* ``auto_issue_letters`` — creates ``documents.DocumentRequest`` rows for
  the standard departing-employee letters when the final step completes
* ``build_separation_card_context`` — view-only payload for the
  Separation card injected into the employees profile page
* ``deactivate_past_lwd_users`` — nightly batch backstop for the middleware
* Notification helpers (``notify_resignation_submitted``, etc.) — all
  thin wrappers around ``core.notifications``
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from functools import wraps
from typing import Optional

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from accounts.models import directory_stamp

from offboarding.models import (
    ClearanceStep,
    ClearanceStepTemplate,
    ClearanceWorkflowTemplate,
    OffboardingCase,
    Resignation,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# ---------------------------------------------------------------------------
# Assignee resolution
# ---------------------------------------------------------------------------

def _resolve_assignee(template: ClearanceStepTemplate, employee) -> tuple:
    """Return ``(user, group)`` for a step template + departing employee.

    For GROUP-kind steps the user is None — the group's members compete.
    For MANAGER / DEPT_HEAD / HR_HEAD we materialise the actual user so
    ``ClearanceStep.objects.filter(assigned_user=request.user, status=PENDING)``
    becomes a single-index lookup. Falls back to ``assigned_group`` if the
    user lookup fails (e.g. a missing JobInfo.reports_to)."""
    kind = template.assigned_kind
    group = template.assigned_group  # fallback for all kinds

    if kind == ClearanceStepTemplate.AssignedKind.GROUP:
        return (None, group)

    if kind == ClearanceStepTemplate.AssignedKind.MANAGER:
        jobinfo = getattr(employee, "job_info", None)
        manager = getattr(jobinfo, "reports_to", None) if jobinfo else None
        return (manager, group)

    if kind == ClearanceStepTemplate.AssignedKind.DEPT_HEAD:
        dept = getattr(employee, "department", None)
        head = getattr(dept, "head_of_department", None) if dept else None
        return (head, group)

    if kind == ClearanceStepTemplate.AssignedKind.HR_HEAD:
        # Prefer the first active user in the "HR" group as the assignee.
        # This keeps historical workflow definitions (which still store the
        # HR_HEAD marker) working during the migration.
        hr_head_group = Group.objects.filter(name="HR").first()
        if hr_head_group:
            user = (hr_head_group.user_set
                    .filter(is_active=True)
                    .exclude(email="")
                    .order_by("date_joined")
                    .first())
            return (user, hr_head_group)
        return (None, group)

    # Unknown kind — be defensive.
    return (None, group)


# ---------------------------------------------------------------------------
# Case construction
# ---------------------------------------------------------------------------

@transaction.atomic
def create_case_from_resignation(resignation: Resignation) -> OffboardingCase:
    """Idempotent: returns the existing case if it already exists. Picks
    the default ``ClearanceWorkflowTemplate`` matching the resignation's
    separation type, or the generic default if none."""
    existing = OffboardingCase.objects.filter(resignation=resignation).first()
    if existing:
        return existing

    template = _pick_workflow_template(resignation.separation_type)
    if template is None:
        raise ValueError(
            "No default ClearanceWorkflowTemplate available. Run seed_offboarding_workflow."
        )

    case = OffboardingCase.objects.create(
        resignation=resignation,
        workflow_template=template,
        status=OffboardingCase.Status.IN_PROGRESS,
        current_step_order=1,
        started_at=timezone.now(),
    )

    employee = resignation.employee
    step_templates = template.step_templates.filter(is_active=True).order_by("order")
    for st in step_templates:
        assigned_user, assigned_group = _resolve_assignee(st, employee)
        ClearanceStep.objects.create(
            case=case,
            template=st,
            step_code=st.step_code,
            step_name=st.step_name,
            order=st.order,
            assigned_kind=st.assigned_kind,
            assigned_group=assigned_group,
            assigned_user=assigned_user,
            is_required=st.is_required,
        )

    return case


def _pick_workflow_template(
    separation_type: str,
) -> Optional[ClearanceWorkflowTemplate]:
    # Prefer a default specifically tagged for this separation_type, fall
    # back to the generic default (separation_type='').
    specific = ClearanceWorkflowTemplate.objects.filter(
        is_default=True, is_active=True, separation_type=separation_type,
    ).first()
    if specific:
        return specific
    return ClearanceWorkflowTemplate.objects.filter(
        is_default=True, is_active=True, separation_type="",
    ).first()


# ---------------------------------------------------------------------------
# Status recalculation
# ---------------------------------------------------------------------------

def refresh_case_status(case: OffboardingCase) -> str:
    """Recompute case.status from its steps. Returns the new status."""
    steps = list(case.steps.all().order_by("order"))
    if not steps:
        return case.status

    if any(s.status == ClearanceStep.Status.REJECTED and s.is_required for s in steps):
        new_status = OffboardingCase.Status.CANCELLED
    elif all(s.status in (ClearanceStep.Status.DONE, ClearanceStep.Status.SKIPPED)
             for s in steps if s.is_required):
        new_status = OffboardingCase.Status.COMPLETED
    elif any(s.status != ClearanceStep.Status.PENDING for s in steps):
        new_status = OffboardingCase.Status.IN_PROGRESS
    else:
        new_status = OffboardingCase.Status.NOT_STARTED

    # Advance current_step_order to the first non-done step.
    next_step = next(
        (s for s in steps
         if s.status in (ClearanceStep.Status.PENDING, ClearanceStep.Status.IN_PROGRESS)),
        None,
    )
    current_order = next_step.order if next_step else 0

    update_fields = {"status": new_status, "current_step_order": current_order,
                     "updated_at": timezone.now()}
    if new_status == OffboardingCase.Status.COMPLETED and not case.completed_at:
        update_fields["completed_at"] = timezone.now()
    OffboardingCase.objects.filter(pk=case.pk).update(**update_fields)
    case.status = new_status
    case.current_step_order = current_order
    return new_status


# ---------------------------------------------------------------------------
# Auto-issue letters when the final step completes
# ---------------------------------------------------------------------------

# Doc-type codes the auto-issuer creates when the case completes. The
# offboarding flow doesn't require all three to exist — missing ones are
# silently skipped (HR can configure additional types later via the
# documents app).
_AUTO_ISSUED_LETTER_CODES = ("EXP_LETTER", "RELEASE_LETTER", "NOC")


def auto_issue_letters(case: OffboardingCase) -> list:
    """Create DocumentRequest rows for the standard departing-employee
    letters. Pre-populates ``input_data`` from case + employee profile so
    HR doesn't re-type. Returns the list of created requests."""
    from documents.models import DocumentRequest, DocumentType

    employee = case.employee
    resignation = case.resignation
    base_input = {
        "reason": "Auto-issued at offboarding case completion.",
        "joining_date": (employee.joining_date.isoformat()
                         if employee.joining_date else ""),
        "last_working_date": resignation.last_working_date.isoformat(),
        "separation_type": resignation.separation_type,
    }

    created: list = []
    for code in _AUTO_ISSUED_LETTER_CODES:
        # Skip codes already issued for this case (idempotent).
        if DocumentRequest.objects.filter(
            offboarding_case=case, document_type__code=code,
        ).exists():
            continue
        try:
            doc_type = DocumentType.objects.get(code=code, is_active=True)
        except DocumentType.DoesNotExist:
            logger.info("auto_issue_letters: skipping %s — DocumentType missing.", code)
            continue
        req = DocumentRequest.objects.create(
            employee=employee,
            document_type=doc_type,
            input_data=dict(base_input),
            applied_by=case.updated_by or employee,
            added_by=case.updated_by or employee,
            updated_by=case.updated_by or employee,
            offboarding_case=case,
        )
        created.append(req)

    # Bind the experience letter as the case's primary_letter (convenience
    # for the case detail page).
    if created and not case.primary_letter_id:
        exp_letter = next(
            (r for r in created if r.document_type.code == "EXP_LETTER"),
            created[0],
        )
        OffboardingCase.objects.filter(pk=case.pk).update(primary_letter=exp_letter)
        case.primary_letter = exp_letter

    return created


# ---------------------------------------------------------------------------
# Separation card for the employees profile page
# ---------------------------------------------------------------------------

def build_separation_card_context(employee, request=None) -> dict:
    """View-only payload injected into employees/employee_detail.html.

    Returns ``{}`` (falsy) when the employee has no resignation and no LWD
    — the template treats this as 'do not render the card'."""
    from offboarding.permissions import can_view_resignation, can_view_case

    latest = (
        Resignation.objects.filter(employee=employee)
        .order_by("-applied_at").first()
    )
    if latest is None and not employee.last_working_date:
        return {}

    user = getattr(request, "user", None) if request else None
    if latest and not can_view_resignation(user, latest, request):
        return {}

    case = getattr(latest, "case", None) if latest else None
    next_step = None
    if case:
        next_step = (case.steps.filter(
            status__in=[ClearanceStep.Status.PENDING, ClearanceStep.Status.IN_PROGRESS],
        ).order_by("order").first())

    done_count, total_count = (case.progress if case else (0, 0))

    return {
        "resignation": latest,
        "case": case,
        "lwd": employee.last_working_date or (latest.last_working_date if latest else None),
        "days_remaining": _days_until(employee.last_working_date),
        "next_step": next_step,
        "progress_done": done_count,
        "progress_total": total_count,
        "can_open_case": bool(case) and bool(user) and can_view_case(user, case, request),
    }


def _days_until(d) -> Optional[int]:
    if not d:
        return None
    return (d - date.today()).days


# ---------------------------------------------------------------------------
# Unbacked separations — LWD set on CustomUser with no Resignation behind it
# ---------------------------------------------------------------------------

def find_unbacked_separations(cutoff_days: int | None = None):
    """Employees with a ``last_working_date`` but no active Resignation.

    ``CustomUser.last_working_date`` has three writers but only one of them
    (Resignation → ACCEPTED, see offboarding/signals.py) builds a clearance
    case. The other two — the user edit form and the Django admin Employment
    fieldset — write the date directly, so the employee is deactivated on
    schedule with no clearance, no exit interview, no asset return and no
    letters. This finds those.

    Detection only. Creating the Resignation stays a deliberate HR act via
    ``/offboarding/create/`` — separation_type and reason cannot be inferred
    from a date edit, and guessing them would put fiction into HR records.

    ``cutoff_days`` (default OFFBOARDING_BACKFILL_CUTOFF_DAYS) drops anyone
    whose LWD is further in the past than that: a long-closed separation is
    not actionable and would sit in the queue forever. Pass 0 for no cutoff.
    """
    from django.conf import settings as dj_settings

    if cutoff_days is None:
        cutoff_days = getattr(dj_settings, "OFFBOARDING_BACKFILL_CUTOFF_DAYS", 90)

    # Employees already represented by a live resignation are not orphans.
    # WITHDRAWN / REJECTED deliberately do NOT count — a rejected resignation
    # leaves the employee needing a fresh one if HR then sets an LWD by hand.
    claimed = Resignation.objects.filter(
        status__in=[Resignation.Status.PENDING, Resignation.Status.ACCEPTED],
    ).values_list("employee_id", flat=True)

    qs = (
        User.objects
        .filter(last_working_date__isnull=False)
        .exclude(pk__in=claimed)
        .select_related("department", "designation", "team")
    )
    if cutoff_days:
        qs = qs.filter(last_working_date__gte=date.today() - timedelta(days=cutoff_days))
    return qs.order_by("-last_working_date")


def offboarding_candidates(user, request=None):
    """Employees ``user`` may raise a resignation for.

    Single source of truth shared by HRCreateResignationForm's queryset and
    the search endpoint behind its picker. They must not diverge: a search
    that offers someone the form then rejects is worse than not offering
    them at all.

    = everyone in the viewer's normal (active) scope, PLUS already-deactivated
    unbacked separations. visible_employees() is is_active=True, so without
    the second half an orphan becomes unselectable the moment the nightly job
    deactivates them — exactly the people HR most needs to raise a case for.

    Scoped via managed_scope_pks rather than by department: that is the
    canonical "who may I act on" set and, unlike visible_employee_pks, is not
    itself is_active-clamped. Filtering by department would silently drop any
    orphan whose department is NULL.
    """
    from accounts.permissions import (
        can_see_all, managed_scope_pks, visible_employees,
    )
    from django.db.models import Q

    visible = visible_employees(user, request=request)
    inactive_orphans = find_unbacked_separations().filter(is_active=False)
    if not can_see_all(user, request=request):
        inactive_orphans = inactive_orphans.filter(
            pk__in=managed_scope_pks(user, request=request),
        )
    orphan_pks = list(inactive_orphans.values_list("pk", flat=True))
    if not orphan_pks:
        return visible
    return User.objects.filter(
        Q(pk__in=visible.values_list("pk", flat=True)) | Q(pk__in=orphan_pks)
    )


# ---------------------------------------------------------------------------
# Deactivation backstop (nightly celery-beat job + management command)
# ---------------------------------------------------------------------------

def deactivate_past_lwd_users(dry_run: bool = False) -> list:
    """Set ``is_active=False`` for every still-active user whose LWD has
    passed. Returns the list of (email, lwd) tuples touched.

    Also purges any roster shift dated past each user's LWD — a backstop for
    stray rows (e.g. a bulk roster upload that landed before the LWD was set).
    ``dry_run=True`` writes nothing at all, shifts included.

    Used by both the ``deactivate_past_lwd`` management command and ad-hoc
    admin recalculation."""
    from roster.services import purge_shifts_after_last_working_date

    qs = User.objects.filter(
        is_active=True,
        last_working_date__isnull=False,
        last_working_date__lt=date.today(),
    )
    rows = list(qs.values_list("pk", "email", "last_working_date"))
    if not dry_run:
        # Purge first — `qs` is lazy, and the .update() below would empty it.
        for user in User.objects.filter(pk__in=[pk for pk, _e, _l in rows]):
            removed = purge_shifts_after_last_working_date(user, actor=None)
            if removed:
                logger.info(
                    "Purged %s roster shift(s) past LWD %s for %s",
                    removed, user.last_working_date, user.email,
                )
        qs.update(is_active=False, **directory_stamp("is_active"))
    return [(email, lwd) for _pk, email, lwd in rows]


def purge_shifts_past_lwd(dry_run: bool = False) -> list:
    """Company-wide sweep: every user with an LWD that still has roster shifts
    dated past it.

    Returns a list of ``(display_name, email, lwd, shift_count)`` tuples,
    ordered by LWD.
    With ``dry_run=True`` nothing is deleted — the counts are a preview.

    On-demand backfill behind ``manage.py purge_shifts_past_lwd``. Deliberately
    NOT on the celery-beat schedule: the write paths (the resignation-accept
    branch and the ``CustomUser`` LWD signal in ``offboarding/signals.py``, the
    upload/prefill/leave guards, and ``deactivate_past_lwd_users`` above) keep
    this clean going forward, so a recurring sweep would only ever be a no-op.
    """
    from roster.models import Shift
    from roster.services import purge_shifts_after_last_working_date

    out: list[tuple] = []
    candidates = (
        User.objects
        .filter(last_working_date__isnull=False)
        .order_by("last_working_date", "email")
    )
    for user in candidates:
        count = Shift.objects.filter(
            employee_id=user.pk, date__gt=user.last_working_date,
        ).count()
        if not count:
            continue
        if not dry_run:
            count = purge_shifts_after_last_working_date(user, actor=None)
        out.append((
            getattr(user, "display_name", "") or user.email,
            user.email, user.last_working_date, count,
        ))
    return out


# ---------------------------------------------------------------------------
# Notification helpers — thin wrappers around core.notifications
#
# Every helper below is wrapped in @_muted_when_disabled, so
# settings.OFFBOARDING_NOTIFICATIONS_ENABLED=False turns the whole module
# silent in one place rather than each call site having to remember the
# check. Callers (signals, views, back-fill) stay unchanged and unaware.
# ---------------------------------------------------------------------------

def _notifications_enabled() -> bool:
    from django.conf import settings as dj_settings
    return getattr(dj_settings, "OFFBOARDING_NOTIFICATIONS_ENABLED", True)


def _muted_when_disabled(func):
    """No-op the wrapped notifier when offboarding mail is paused.

    Logs at DEBUG rather than staying silent — when HR asks "did they get
    an email?", the answer needs to be findable in the log."""
    @wraps(func)
    def _wrapper(*args, **kwargs):
        if not _notifications_enabled():
            logger.debug(
                "offboarding notifications disabled — skipping %s", func.__name__,
            )
            return None
        return func(*args, **kwargs)
    return _wrapper


@_muted_when_disabled
def notify_resignation_submitted(resignation: Resignation) -> None:
    """Manager + HR get notified when an employee submits a resignation."""
    from core.notifications import notify_group, notify_user

    ctx = {"resignation": resignation}

    # Manager (via JobInfo.reports_to) — direct email.
    jobinfo = getattr(resignation.employee, "job_info", None)
    manager = getattr(jobinfo, "reports_to", None) if jobinfo else None
    if manager:
        notify_user(
            manager,
            subject_template_path="offboarding/emails/resignation_submitted_subject.txt",
            body_template_path="offboarding/emails/resignation_submitted_body.txt",
            context=ctx,
        )

    # HR group broadcast.
    hr = Group.objects.filter(name="HR").first()
    if hr:
        notify_group(
            hr,
            subject_template_path="offboarding/emails/resignation_submitted_subject.txt",
            body_template_path="offboarding/emails/resignation_submitted_body.txt",
            context=ctx,
        )


@_muted_when_disabled
def notify_hr_initiated_resignation(resignation: Resignation) -> None:
    """Employee + manager get notified when HR creates a resignation on the
    employee's behalf (involuntary termination, absconding, retirement, etc.).
    HR doesn't get a separate notification — they're the ones who triggered it."""
    from core.notifications import notify_user

    ctx = {"resignation": resignation}

    # Employee — they may genuinely not know yet (absconding doesn't apply, but
    # an involuntary termination might need a paper trail).
    notify_user(
        resignation.employee,
        subject_template_path="offboarding/emails/resignation_initiated_by_hr_subject.txt",
        body_template_path="offboarding/emails/resignation_initiated_by_hr_body.txt",
        context=ctx,
    )

    # Manager (so they're not blindsided by clearance step assignments).
    jobinfo = getattr(resignation.employee, "job_info", None)
    manager = getattr(jobinfo, "reports_to", None) if jobinfo else None
    if manager and manager != resignation.applied_by:
        notify_user(
            manager,
            subject_template_path="offboarding/emails/resignation_initiated_by_hr_subject.txt",
            body_template_path="offboarding/emails/resignation_initiated_by_hr_body.txt",
            context=ctx,
        )


@_muted_when_disabled
def notify_step_assigned(step: ClearanceStep) -> None:
    """Notify the assignee of a pending step. Direct email to
    ``assigned_user`` if set; otherwise the assigned_group broadcast."""
    from core.notifications import notify_group, notify_user

    ctx = {"step": step, "case": step.case, "resignation": step.case.resignation}

    if step.assigned_user:
        notify_user(
            step.assigned_user,
            subject_template_path="offboarding/emails/step_assigned_subject.txt",
            body_template_path="offboarding/emails/step_assigned_body.txt",
            context=ctx,
        )
        return
    if step.assigned_group:
        notify_group(
            step.assigned_group,
            subject_template_path="offboarding/emails/step_assigned_subject.txt",
            body_template_path="offboarding/emails/step_assigned_body.txt",
            context=ctx,
        )


@_muted_when_disabled
def notify_case_completed(case: OffboardingCase) -> None:
    from core.notifications import notify_group, notify_user

    ctx = {"case": case, "resignation": case.resignation, "employee": case.employee}

    notify_user(
        case.employee,
        subject_template_path="offboarding/emails/case_completed_subject.txt",
        body_template_path="offboarding/emails/case_completed_body.txt",
        context=ctx,
    )
    hr = Group.objects.filter(name="HR").first()
    if hr:
        notify_group(
            hr,
            subject_template_path="offboarding/emails/case_completed_subject.txt",
            body_template_path="offboarding/emails/case_completed_body.txt",
            context=ctx,
        )
