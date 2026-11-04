"""Shared pagination helpers — numbered pager + page-size picker.

Two entry points, one for each kind of view we have:

- ``PageSizeMixin`` for Django ``ListView``s: honours ``?per_page=`` (among
  ``PAGE_SIZES``) and exposes the picker context.
- ``paginate_list`` for plain views that build a Python list (reports like
  payroll / shift-summary): slices the list and returns the same context keys.

Both feed ``templates/partials/_pagination.html`` and
``templates/partials/_result_count.html``.
"""

from django.core.paginator import Paginator

# The sizes offered in the per-page dropdown (Invelo-style).
PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 25


def resolve_page_size(request, default=DEFAULT_PAGE_SIZE):
    """Return the requested ``per_page`` if it's an allowed size, else default."""
    try:
        size = int(request.GET.get("per_page"))
    except (TypeError, ValueError):
        return default
    return size if size in PAGE_SIZES else default


class PageSizeMixin:
    """Add to a ``ListView`` to honour ``?per_page=`` and expose the picker.

    Uses the view's ``paginate_by`` as the default. Standardise ``paginate_by``
    to a value in ``PAGE_SIZES`` (25) so the dropdown always shows it selected.
    """

    def get_paginate_by(self, queryset):
        return resolve_page_size(self.request, self.paginate_by or DEFAULT_PAGE_SIZE)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_sizes"] = PAGE_SIZES
        ctx["current_page_size"] = self.get_paginate_by(None)
        return ctx


def paginate_list(request, items, default=DEFAULT_PAGE_SIZE):
    """Paginate a Python list for a non-ListView. Returns context keys.

    The caller renders the sliced ``rows``; ``paginator.count`` is the full
    total (so the count label and any export still reflect everything).
    """
    size = resolve_page_size(request, default)
    paginator = Paginator(items, size)
    page_obj = paginator.get_page(request.GET.get("page"))
    return {
        "rows": list(page_obj.object_list),
        "page_obj": page_obj,
        "paginator": paginator,
        "is_paginated": page_obj.has_other_pages(),
        "page_sizes": PAGE_SIZES,
        "current_page_size": size,
    }
