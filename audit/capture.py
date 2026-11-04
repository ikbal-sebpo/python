"""Per-request access context — what the middleware writes at the end.

``AccessLogMiddleware.process_view`` hangs an ``AccessContext`` on the request
as ``request.access``, pre-filled from the registry (``audit.registry``). A
view that knows more than the URL does — the employee behind a loan or a
tax declaration, how many rows an export carried — enriches it with one
call::

    from audit.capture import note
    note(request, subject=loan.employee, rows=len(rows), file=filename)

There is no second insert path: the middleware's one INSERT per request is
the only writer, so a view can never double-log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from audit.models import Action, DataClass


@dataclass
class AccessContext:
    data_class: str = DataClass.PAGE
    action: str = Action.VIEW
    subject_id: str | None = None          # CustomUser pk (UUID) as a string
    subject_label: str = ""
    object_type: str = ""
    object_id: str = ""
    detail: dict = field(default_factory=dict)
    skip: bool = False                     # set by the registry for excluded routes

    def set_subject(self, user) -> None:
        if user is None:
            return
        self.subject_id = str(user.pk)
        self.subject_label = user.search_label

    def set_object(self, obj) -> None:
        if obj is None:
            return
        self.object_type = type(obj).__name__
        self.object_id = str(obj.pk)


def get_context(request) -> AccessContext:
    """The request's context, or a detached one when the middleware isn't
    installed (plain RequestFactory tests) so callers never have to guard."""
    ctx = getattr(request, "access", None)
    if ctx is None:
        ctx = AccessContext()
        try:
            request.access = ctx
        except AttributeError:  # pragma: no cover — exotic request objects
            pass
    return ctx


def note(request, *, subject=None, obj=None, action: str | None = None,
         data_class: str | None = None, **detail) -> AccessContext:
    """Enrich the current request's access record. Keyword-only on purpose."""
    ctx = get_context(request)
    if subject is not None:
        ctx.set_subject(subject)
    if obj is not None:
        ctx.set_object(obj)
    if action:
        ctx.action = action
    if data_class:
        ctx.data_class = data_class
    if detail:
        ctx.detail.update(detail)
    return ctx


# ---------------------------------------------------------------------------
# Request metadata shared by the middleware and the auth-signal receivers
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import ipaddress  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

#: Query-string keys never stored (credentials, CSRF, OIDC callback state).
SECRET_QUERY_KEYS = frozenset({
    "password", "token", "csrfmiddlewaretoken", "code", "state", "session_state",
    "id_token", "access_token", "next",
})


def client_ip(request) -> str | None:
    """First hop of X-Forwarded-For (nginx sets it in production) else REMOTE_ADDR."""
    raw = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    for candidate in (raw, request.META.get("REMOTE_ADDR", "")):
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def session_hash(request) -> str:
    session = getattr(request, "session", None)
    key = getattr(session, "session_key", None) if session is not None else None
    if not key:
        return ""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def user_agent(request) -> str:
    return (request.META.get("HTTP_USER_AGENT") or "")[:300]


def clean_query(request) -> str:
    if not request.GET:
        return ""
    pairs = [(k, v) for k, v in request.GET.lists() if k.lower() not in SECRET_QUERY_KEYS]
    return urlencode(pairs, doseq=True)[:500]


def actor_fields(user) -> tuple[object | None, str]:
    """(actor FK value, label). TimeLy's synthetic ``_ServiceAccount`` and
    DRF's AnonymousUser are not CustomUser rows, so they become a label only."""
    from accounts.models import CustomUser

    if user is None or not getattr(user, "is_authenticated", False):
        return None, "anonymous"
    if isinstance(user, CustomUser):
        return user, (user.email or user.username or str(user.pk))[:150]
    return None, str(user)[:150]
