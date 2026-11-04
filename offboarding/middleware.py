"""LastWorkingDateMiddleware — runtime guard for past-LWD users.

Sits after AuthenticationMiddleware. If an authenticated user's
``last_working_date`` is strictly before today, the middleware:

1. Flips ``is_active=False`` via a raw ``.update()`` (skips cascading signals
   so we don't trigger ``accounts._recompute_line_managers`` etc. on every
   request).
2. ``logout(request)`` — clears the session.
3. Redirects to ``offboarding:access_ended`` (no auth gate on that page).

An allowlist of URL names + a ``Resolver404`` fallback prevents redirect
loops on the access-ended page itself, the logout view, and on
static/media requests.

The middleware deliberately uses ``< today`` (not ``<= today``) — employees
keep access on their last working day, lose it the morning after.
"""

from __future__ import annotations

import logging
from datetime import date

from django.contrib.auth import get_user_model, logout
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

from accounts.models import directory_stamp

logger = logging.getLogger(__name__)

# URL names the middleware lets through even after lockout — otherwise the
# redirect to ``access_ended`` would itself trigger the middleware and loop.
_ALLOWED_PATH_NAMES = {"access_ended", "logout", "login"}


class LastWorkingDateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user is None
                or not user.is_authenticated
                or not getattr(user, "last_working_date", None)
                or user.last_working_date >= date.today()):
            return self.get_response(request)

        # User's LWD has passed. Decide whether to let this request through.
        try:
            match = resolve(request.path_info)
            if match.url_name in _ALLOWED_PATH_NAMES:
                return self.get_response(request)
        except Resolver404:
            # Static / media / other untracked path — let it pass.
            return self.get_response(request)

        # Demote + log out.
        get_user_model().objects.filter(pk=user.pk).update(
            is_active=False, **directory_stamp("is_active"),
        )
        logger.info(
            "Deactivating %s on request — LWD %s < today",
            user.email, user.last_working_date,
        )
        logout(request)
        try:
            target = reverse("offboarding:access_ended")
        except Exception:
            # offboarding URLs not yet mounted (e.g. test bootstrap): fall back to /.
            target = "/"
        return redirect(target)
