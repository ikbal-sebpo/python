"""Template helpers for the dynamic menu.

Used by partials/topbar.html to compute which sidebar section contains the
active URL — so the mobile drawer can pre-expand it via <details open>.
"""

from django import template
from django.http import QueryDict

register = template.Library()


@register.filter
def is_section_active(main_menu, active_url_name: str) -> bool:
    """True if any submenu under ``main_menu`` matches the active URL name.

    Uses the already-prefetched ``main_menu.submenus.all()`` so no extra
    query fires per row.
    """
    if not active_url_name:
        return False
    return any(s.url == active_url_name for s in main_menu.submenus.all())


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs) -> str:
    """Return the current querystring with the given keys replaced.

    Lets pagination/filter links preserve every active GET param instead of
    hand-forwarding a single one. Usage: ``?{% qs_replace page=3 %}``. Keys
    set to ``None`` are removed. Returns the encoded querystring (no leading
    ``?``).
    """
    request = context.get("request")
    params = request.GET.copy() if request else QueryDict(mutable=True)
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


@register.filter
def elided_page_range(page_obj):
    """Compact page-number range with ellipses, e.g. ``1 … 4 5 6 … 20``.

    Wraps Django's ``Paginator.get_elided_page_range`` so the numbered pager
    template can iterate it directly. ``Paginator.ELLIPSIS`` marks the gaps.
    """
    paginator = page_obj.paginator
    return paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)
