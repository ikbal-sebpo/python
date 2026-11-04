"""Per-application API authentication — docs/DIRECTORY_API_PLAN.md §4.

`Authorization: Token orb_…`, matched against `accounts.ApiClient.token_hash`.
Same header as the two `.env` tokens (`roster/api/timely.py`,
`roster/api/shift_writes.py`), and like them it returns `None` on a token it
doesn't recognise, so all three authenticators compose on one endpoint while
consumers migrate.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import authentication, exceptions
from rest_framework.permissions import BasePermission

from accounts.models import ApiClient
from audit.capture import note

# `last_used_at` is written at most this often per client — not once a request.
_LAST_USED_RESOLUTION = timedelta(minutes=1)


class ApiClientPrincipal:
    """`request.user` for a read-only client: not a `CustomUser`, so the access
    log records it as the label `api:<name>` (`audit.capture.actor_fields`)."""

    is_authenticated = True
    is_active = True
    is_anonymous = False
    is_staff = False
    is_superuser = False
    pk = None

    def __init__(self, client: ApiClient):
        self.client = client

    def __str__(self) -> str:
        return f"api:{self.client.name}"


class ApiClientTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Token"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        try:
            token = header[1].decode()
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed("Malformed token.") from exc
        if not token.startswith(ApiClient.TOKEN_PREFIX):
            return None  # an .env token — let the next authenticator try

        client = (
            ApiClient.objects.select_related("service_account")
            .filter(token_hash=ApiClient.hash_token(token), is_active=True)
            .first()
        )
        if client is None:
            return None

        if client.has_scope(ApiClient.Scope.ROSTER_WRITE) and client.service_account is None:
            # Our misconfiguration, not "try the next authenticator": a write
            # client must act as a persisted user (Shift.added_by). §4.4.
            raise exceptions.AuthenticationFailed(
                f"API client {client.name!r} has roster:write but no service account — "
                "set one in the admin."
            )

        self._touch(client)
        note(request._request, api_client=client.name)
        user = client.service_account or ApiClientPrincipal(client)
        return (user, client)

    def authenticate_header(self, request):
        return self.keyword

    @staticmethod
    def _touch(client: ApiClient) -> None:
        now = timezone.now()
        if client.last_used_at and now - client.last_used_at < _LAST_USED_RESOLUTION:
            return
        ApiClient.objects.filter(pk=client.pk).update(last_used_at=now)
        client.last_used_at = now


class HasApiScope(BasePermission):
    """Allow only an `ApiClient` holding the view's `required_scope`.

    Views using it accept only `ApiClientTokenAuthentication`, so `request.auth`
    is always an `ApiClient` here — the `.env` tokens are deliberately not
    admitted to new endpoints (D6/§4.3).
    """

    message = "This API client is not allowed to use this endpoint."

    def has_permission(self, request, view) -> bool:
        client = request.auth
        scope = getattr(view, "required_scope", None)
        return isinstance(client, ApiClient) and bool(scope) and client.has_scope(scope)


class HasApiScopeOrEnvToken(HasApiScope):
    """For the roster endpoints during the token switch-over (phase 3): an
    `ApiClient` must hold the view's `required_scope`; a request
    authenticated by one of the two `.env` tokens (`TIMELY_API_TOKEN`,
    `EXTERNAL_API_TOKEN`) keeps the access it has today. Delete the `.env`
    branch together with those authenticators once both consumers have
    moved. docs/DIRECTORY_API_PLAN.md §4.3.
    """

    def has_permission(self, request, view) -> bool:
        if isinstance(request.auth, ApiClient):
            return super().has_permission(request, view)
        return bool(request.user and request.user.is_authenticated)
