"""Deferred policy tasks (dispatched via core.tasks.defer).

The signing-email fan-out is the largest email burst in the app — a policy
published to "All Employees" is one mail per active employee. The publish
request only enqueues chunks of acknowledgement pks; everything expensive
(template rendering AND SMTP) happens here on the Celery worker.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def send_acknowledgement_emails(ack_pks: list[str], kind: str) -> None:
    """Render + deliver one signing email per acknowledgement over a single
    shared SMTP connection.

    Retry semantics (via core.tasks.run_deferred): raises only when the
    whole chunk failed with nothing sent (a connection-level outage), so a
    retry can't duplicate mails that already went out. Per-recipient
    failures are logged and skipped — same policy as core.notifications.
    """
    from django.core.mail import get_connection

    from core.notifications import build_user_email
    from policies.models import PolicyAcknowledgement
    from policies.services import _EMAIL_KINDS, policy_sign_url

    subject_path, body_path, html_path = _EMAIL_KINDS[kind]
    acks = list(
        PolicyAcknowledgement.objects
        .filter(pk__in=ack_pks)
        .select_related("employee", "policy")
    )
    if not acks:
        return

    # Hoist the per-policy lookups — `policy.current_version` is a property
    # that queries per call; reading it per ack was an N+1 in the old
    # in-request loop.
    version_by_policy: dict = {}
    sign_url_by_policy: dict = {}
    messages = []
    for ack in acks:
        policy = ack.policy
        if policy.pk not in version_by_policy:
            version_by_policy[policy.pk] = policy.current_version
            sign_url_by_policy[policy.pk] = policy_sign_url(policy)
        msg = build_user_email(
            ack.employee,
            subject_template_path=subject_path,
            body_template_path=body_path,
            html_body_template_path=html_path,
            context={
                "policy": policy,
                "acknowledgement": ack,
                "version": version_by_policy[policy.pk],
                "sign_url": sign_url_by_policy[policy.pk],
            },
        )
        if msg is not None:
            messages.append(msg)
    if not messages:
        return

    sent = failed = 0
    connection = get_connection()
    connection.open()
    try:
        for msg in messages:
            msg.connection = connection
            try:
                msg.send()
                sent += 1
            except Exception:
                failed += 1
                logger.exception(
                    "Policy %s email to %s failed", kind, msg.to,
                )
    finally:
        connection.close()

    if failed:
        logger.warning(
            "Policy %s fan-out chunk: %d sent, %d failed", kind, sent, failed,
        )
    if failed and sent == 0:
        # Nothing went out — almost certainly the SMTP server, not the
        # recipients. Raise so the task retries the whole chunk safely.
        raise RuntimeError(
            f"Policy {kind} fan-out chunk failed entirely ({failed} messages)."
        )
