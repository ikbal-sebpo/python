"""One access-log row per request. See docs/ACCESS_LOG_PLAN.md §3.2.

``process_view`` classifies the resolved route and hangs ``request.access``
on the request; views may enrich it (``audit.capture.note``); the response
phase writes a single INSERT. A failure inside the write is logged and
swallowed — the audit record must never change what the user gets back.

Placed last in MIDDLEWARE so ``request.user`` is final (Authentication +
LastWorkingDate middlewares run before it) and the view's own transaction
has already committed or rolled back by the time the row is written.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings

from audit.capture import (
    AccessContext, actor_fields, clean_query, client_ip, session_hash, user_agent,
)
from audit.models import AccessLog, Action
from audit.registry import EXCLUDED_PATH_PREFIXES, anonymous_ok, rule_for

logger = logging.getLogger(__name__)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AccessLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "ACCESS_LOG_ENABLED", True))

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        if self.enabled:
            try:
                self._record(request, response, started)
            except Exception:  # noqa: BLE001 — never let auditing break a request
                logger.exception("Access log write failed for %s %s", request.method, request.path)
        return response

    # -- request phase ------------------------------------------------------

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not self.enabled or request.path.startswith(EXCLUDED_PATH_PREFIXES):
            return None
        match = getattr(request, "resolver_match", None)
        route = (match.view_name if match else "") or ""
        rule = rule_for(route)
        ctx = AccessContext()
        if rule is None:
            ctx.skip = True
        else:
            ctx.data_class = rule.data_class
            if rule.action:
                ctx.action = rule.action
            elif request.method in _READ_METHODS and request.GET.get("export"):
                ctx.action = Action.EXPORT
            elif request.method not in _READ_METHODS:
                ctx.action = Action.WRITE
            subject_kwarg = rule.subject_kwarg or ("employee_pk" if "employee_pk" in view_kwargs else None)
            if subject_kwarg and view_kwargs.get(subject_kwarg) is not None:
                ctx.subject_id = str(view_kwargs[subject_kwarg])
            if rule.object_type and view_kwargs.get(rule.object_kwarg) is not None:
                ctx.object_type = rule.object_type
                ctx.object_id = str(view_kwargs[rule.object_kwarg])[:64]
            if "employee_id" in view_kwargs:  # TimeLy's per-employee roster API
                ctx.detail.setdefault("employee_id", str(view_kwargs["employee_id"]))
        request.access = ctx
        request.access_route = route
        return None

    # -- response phase -----------------------------------------------------

    def _record(self, request, response, started: float) -> None:
        ctx: AccessContext | None = getattr(request, "access", None)
        if ctx is None or ctx.skip:
            return  # unresolved path (404), excluded route, or static/media
        route = getattr(request, "access_route", "")
        user = getattr(request, "user", None)
        authenticated = bool(user is not None and getattr(user, "is_authenticated", False))
        if not authenticated and not anonymous_ok(route):
            return
        actor, actor_label = actor_fields(user)

        status = getattr(response, "status_code", 0) or 0
        action = ctx.action
        if status == 403 or getattr(request, "access_denied", False):
            action = Action.DENIED

        subject, subject_label = self._resolve_subject(ctx)

        AccessLog.objects.create(
            actor=actor,
            actor_label=actor_label,
            session=session_hash(request),
            ip_address=client_ip(request),
            user_agent=user_agent(request),
            method=request.method[:8],
            route=route[:200],
            path=request.path[:500],
            query=clean_query(request),
            status_code=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            is_htmx=request.headers.get("HX-Request") == "true",
            data_class=ctx.data_class,
            action=action,
            subject=subject,
            subject_label=subject_label[:150],
            object_type=ctx.object_type[:60],
            object_id=ctx.object_id[:64],
            detail=ctx.detail,
        )

    @staticmethod
    def _resolve_subject(ctx: AccessContext):
        if not ctx.subject_id:
            return None, ""
        from accounts.models import CustomUser

        try:
            subject = CustomUser.objects.only("pk", "employee_id", "first_name", "last_name", "username", "email").get(pk=ctx.subject_id)
        except (CustomUser.DoesNotExist, ValueError, TypeError):
            return None, ctx.subject_label
        return subject, ctx.subject_label or subject.search_label
