"""Custom admin site.

The only customization is the autocomplete endpoint. Django's stock
``AutocompleteJsonView`` labels each result with ``str(obj)`` — for a person
that is just their name. Multiple employees share a name, so when picking e.g.
a department's *additional managers* (or head), identical names are
indistinguishable and the wrong person can be assigned. The employee_id is
unique, so we append it to the label for ``CustomUser`` results.

Autocomplete is a single site-level view (``AdminSite.autocomplete_view``), so
this is the correct place to override it — wired in as the default admin site
via ``core.apps.OrbisAdminConfig``.
"""

from django.contrib.admin.sites import AdminSite
from django.contrib.admin.views.autocomplete import AutocompleteJsonView


class UserLabelAutocompleteJsonView(AutocompleteJsonView):
    def serialize_result(self, obj, to_field_name):
        data = super().serialize_result(obj, to_field_name)
        from accounts.models import CustomUser

        if isinstance(obj, CustomUser):
            bits = [
                (obj.get_full_name() or "").strip() or obj.username,
                obj.employee_id,
            ]
            data["text"] = " · ".join(b for b in bits if b)
        return data


class OrbisAdminSite(AdminSite):
    site_header = "Orbis Administration"
    site_title = "Orbis Admin"
    index_title = "Orbis Administration"

    def autocomplete_view(self, request):
        return UserLabelAutocompleteJsonView.as_view(admin_site=self)(request)
