"""Project-wide notification helpers.

Thin wrappers around Django's ``send_mail`` + ``render_to_string``. Designed
to be called from signal handlers inside ``transaction.on_commit()`` so a
rolled-back transaction never fires a phantom email.

SMTP failures are logged but never re-raised — a flaky mail server must not
crash a user-facing flow (submitting a request, approving a step, etc.).

Subject and body templates are rendered with the same context dict plus a
``recipient`` key bound to the target user (when applicable). Subject
templates are single-line; any embedded newlines are collapsed.
"""

from __future__ import annotations

import logging
from typing import Iterable

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.mail import EmailMultiAlternatives, send_mail
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _render_subject(template_path: str, context: dict) -> str:
    return " ".join(render_to_string(template_path, context).split())


def _send(recipients: list[str], subject: str, body: str, html: str | None = None) -> None:
    """Queue-aware send: template rendering happens in the caller (cheap);
    only the SMTP round-trip is deferred. This is the choke point every
    notification in the project goes through, so a policy publish fanning
    out hundreds of mails costs the request N renders, not N SMTP calls."""
    if not recipients:
        return
    from core.tasks import defer

    defer(deliver_mail, recipients, subject, body, html)


def deliver_mail(recipients: list[str], subject: str, body: str, html: str | None = None) -> None:
    """The actual SMTP call — runs on the task queue in production."""
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            fail_silently=False,
            # When given, send_mail builds a multipart/alternative with the
            # plain-text `body` as fallback and this as the rich HTML part.
            html_message=html,
        )
    except Exception:
        logger.exception("Failed to send notification to %s", recipients)


def build_user_email(
    user,
    *,
    subject_template_path: str,
    body_template_path: str,
    context: dict,
    html_body_template_path: str | None = None,
) -> EmailMultiAlternatives | None:
    """Render a single-recipient message WITHOUT sending it.

    Used by bulk fan-out tasks (policies.tasks) that send many messages over
    one shared SMTP connection — notify_user's per-mail dispatch would open a
    connection per recipient. Returns None when the user has no email.
    """
    if not user or not getattr(user, "email", ""):
        return None
    ctx = {**context, "recipient": user}
    msg = EmailMultiAlternatives(
        subject=_render_subject(subject_template_path, ctx),
        body=render_to_string(body_template_path, ctx),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    if html_body_template_path:
        msg.attach_alternative(
            render_to_string(html_body_template_path, ctx), "text/html",
        )
    return msg


def notify_user(
    user,
    *,
    subject_template_path: str,
    body_template_path: str,
    context: dict,
    html_body_template_path: str | None = None,
) -> None:
    """Email a single user. No-op if user is None or lacks an email.

    Pass ``html_body_template_path`` to send a branded HTML email; the
    plain-text ``body_template_path`` is always rendered and kept as the
    multipart fallback for text-only clients.
    """
    if not user or not getattr(user, "email", ""):
        return
    ctx = {**context, "recipient": user}
    subject = _render_subject(subject_template_path, ctx)
    body = render_to_string(body_template_path, ctx)
    html = render_to_string(html_body_template_path, ctx) if html_body_template_path else None
    _send([user.email], subject, body, html=html)


def notify_group(
    group: Group,
    *,
    subject_template_path: str,
    body_template_path: str,
    context: dict,
) -> None:
    """Email every active member of a group with a non-empty email address."""
    if not group:
        return
    for user in group.user_set.filter(is_active=True).exclude(email=""):
        notify_user(
            user,
            subject_template_path=subject_template_path,
            body_template_path=body_template_path,
            context=context,
        )


def notify_hr(
    *,
    subject_template_path: str,
    body_template_path: str,
    context: dict,
) -> None:
    """Broadcast to settings.HR_NOTIFICATION_EMAILS (env-configured list)."""
    addresses: Iterable[str] = getattr(settings, "HR_NOTIFICATION_EMAILS", None) or []
    addresses = [a for a in addresses if a]
    if not addresses:
        return
    subject = _render_subject(subject_template_path, context)
    body = render_to_string(body_template_path, context)
    _send(list(addresses), subject, body)
