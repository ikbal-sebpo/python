"""Custom form widgets for the employees app."""

from __future__ import annotations

from django import forms
from django.urls import reverse

from accounts.models import CustomUser


class EmployeeSearchWidget(forms.Widget):
    """Single-select searchable employee picker — type a name or employee ID,
    pick from live suggestions showing both. Mirrors the htmx+Alpine idiom in
    templates/policies/_adherent_picker.html, simplified to single selection.

    Renders as a self-contained component (search box + results panel + a
    selected-value chip); `{{ field }}` in a generic form-field template just
    works, no drawer/template changes needed elsewhere.
    """

    template_name = "employees/widgets/_employee_search.html"

    def __init__(self, attrs=None, *, for_employee_pk=None, search_url_name="ajax_load_employees"):
        super().__init__(attrs)
        self.for_employee_pk = for_employee_pk
        self.search_url_name = search_url_name

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        selected = CustomUser.objects.filter(pk=value).first() if value else None
        context["widget"]["search_url"] = reverse(self.search_url_name)
        context["widget"]["for_employee_pk"] = self.for_employee_pk or ""
        context["widget"]["selected_label"] = selected.search_label if selected else ""
        return context

    def value_from_datadict(self, data, files, name):
        return data.get(name) or None

    def value_omitted_from_data(self, data, files, name):
        return name not in data
