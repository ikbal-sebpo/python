"""Login / logout / failed-login rows. Hooked up via ``AuditConfig.ready()``.

mozilla-django-oidc calls ``django.contrib.auth.login()`` too, so SSO logins
fire the same ``user_logged_in`` as the password form; the backend name
``auth.login`` stamps on the user tells the two apart.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from audit.capture import actor_fields, client_ip, session_hash, user_agent
from audit.models import AccessLog, Action, DataClass

logger = logging.getLogger(__name__)


def _backend_label(user) -> str:
    backend = getattr(user, "backend", "") or ""
    return "sso" if "oidc" in backend.lower() else "password"


def _write(request, user, action: str, *, label: str = "", detail: dict | None = None) -> None:
    if not getattr(settings, "ACCESS_LOG_ENABLED", True):
        return
    try:
        actor, actor_label = actor_fields(user)
        AccessLog.objects.create(
            actor=actor,
            actor_label=(label or actor_label)[:150],
            session=session_hash(request) if request is not None else "",
            ip_address=client_ip(request) if request is not None else None,
            user_agent=user_agent(request) if request is not None else "",
            method=((getattr(request, "method", None) or "") if request is not None else "")[:8],
            route=action.lower(),
            path=((getattr(request, "path", None) or "") if request is not None else "")[:500],
            data_class=DataClass.AUTH,
            action=action,
            detail=detail or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Access log auth event failed: %s", action)


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    _write(request, user, Action.LOGIN, detail={"backend": _backend_label(user)})


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    _write(request, user, Action.LOGOUT)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request, **kwargs):
    attempted = (credentials or {}).get("username") or (credentials or {}).get("email") or ""
    _write(request, None, Action.LOGIN_FAILED, label=str(attempted) or "anonymous")
