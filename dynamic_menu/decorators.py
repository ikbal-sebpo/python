from functools import wraps

from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from dynamic_menu.models import ProtectedView


def _denied(request, message):
    """Deny in a way the caller can actually see.

    A plain redirect to 'home' is right for a full page load, but wrong for
    an htmx request: htmx follows the 302 and swaps the *dashboard* into
    whatever target the fragment was bound to — a drawer body, a table cell.
    That's what a missing ProtectedView row looked like on
    /payroll/overtime/ (2026-09-15): the delete-confirmation drawer opened
    with the entire dashboard inside it, with nothing to say access had been
    refused. Answer htmx with a 403 + HX-Error-Message instead, which
    base.html's htmx:responseError handler already turns into an error toast
    and, crucially, does NOT swap.
    """
    # The access log can't tell a bounce-to-dashboard from a visit to the
    # dashboard by status code alone, so flag it (audit.middleware → DENIED).
    request.access_denied = True
    if getattr(request, "htmx", False):
        response = HttpResponse(status=403)
        response["HX-Error-Message"] = message
        return response
    return redirect("home")


def _login_required_response(request):
    """Same idea for an expired session: htmx can't render a login page into
    a fragment target, so tell the browser to navigate instead."""
    login_url = reverse("login")
    if getattr(request, "htmx", False):
        response = HttpResponse(status=200)
        response["HX-Redirect"] = login_url
        return response
    return redirect(login_url)


def dynamic_allowed_users(view_func):
    """Gate a view on ProtectedView.allowed_groups.

    If no ProtectedView row exists for this resolved view_name, access is denied.
    If the row exists but the user is not in any allowed group, redirect to
    'home' (or, for an htmx request, answer 403 — see _denied).
    """

    def _resolve_view_name(_view_func, request):
        resolver_match = getattr(request, "resolver_match", None)
        if resolver_match and resolver_match.view_name:
            return resolver_match.view_name

        view_class = getattr(_view_func, "view_class", None)
        if view_class:
            return view_class.__name__

        qualname = getattr(_view_func, "__qualname__", "")
        if "." in qualname:
            head, _, _ = qualname.rpartition(".")
            return head

        return getattr(_view_func, "__name__", None)

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _login_required_response(request)
        if getattr(request.user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        view_name = _resolve_view_name(view_func, request)
        if not view_name:
            return view_func(request, *args, **kwargs)

        try:
            pv = ProtectedView.objects.get(view_name=view_name, is_active=True)
        except ProtectedView.DoesNotExist:
            # A view with no row at all is almost always a deploy that ran
            # ahead of `sync_protected_views` / `seed_orbis`, not a real
            # policy decision — say so rather than bouncing to the dashboard.
            return _denied(request, "This screen isn't configured yet — ask an admin to grant access to it.")

        user_group_ids = request.user.groups.values_list("pk", flat=True)
        if pv.allowed_groups.filter(pk__in=user_group_ids).exists():
            return view_func(request, *args, **kwargs)
        return _denied(request, "You don't have permission to open this.")

    return wrapper
