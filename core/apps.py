from django.contrib.admin.apps import AdminConfig


class OrbisAdminConfig(AdminConfig):
    """Replaces django.contrib.admin so the default admin site is OrbisAdminSite
    (people-picker autocompletes show email + employee id)."""

    default_site = "core.admin_site.OrbisAdminSite"
