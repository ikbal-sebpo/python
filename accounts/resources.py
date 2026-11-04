from import_export import fields, resources
from import_export.widgets import ForeignKeyWidget, ManyToManyWidget

from accounts.models import AccessLevel, CustomUser, Department, Designation, Team


class TeamResource(resources.ModelResource):
    """Import/export for org teams, keyed by unique name.

    A spreadsheet only needs a ``name`` column; ``department`` (matched by its
    name), ``additional_managers`` (comma-separated ``employee_id``s), and
    ``description`` / ``is_active`` are optional. Re-importing the same name
    updates the existing row instead of creating a duplicate.

    ``additional_managers`` is grant-only (``m2m_add=True``): importing adds
    the listed employee(s) to the team's existing additional managers rather
    than replacing the set, so re-running an import (or importing a second
    file covering different teams) never silently drops a manager granted
    some other way — e.g. via the plain admin form.
    """

    department = fields.Field(
        column_name="department",
        attribute="department",
        widget=ForeignKeyWidget(Department, field="name"),
    )
    additional_managers = fields.Field(
        column_name="additional_managers",
        attribute="additional_managers",
        widget=ManyToManyWidget(CustomUser, field="employee_id"),
        m2m_add=True,
    )

    class Meta:
        model = Team
        import_id_fields = ("name",)
        fields = ("name", "department", "additional_managers", "description", "is_active")
        export_order = ("name", "department", "additional_managers", "description", "is_active")
        skip_unchanged = True
        report_skipped = True


class DesignationResource(resources.ModelResource):
    """Import/export for designations, keyed by unique name.

    Built so the ``access_level`` tier assigned to each designation in one
    environment (e.g. locally) can be exported and re-imported into another
    (e.g. production) — a spreadsheet only needs ``name`` + ``access_level``
    (matched by its name); ``description`` / ``is_active`` are optional.
    Re-importing the same name updates the existing row instead of creating
    a duplicate.
    """

    access_level = fields.Field(
        column_name="access_level",
        attribute="access_level",
        widget=ForeignKeyWidget(AccessLevel, field="name"),
    )

    class Meta:
        model = Designation
        import_id_fields = ("name",)
        fields = ("name", "access_level", "description", "is_active")
        export_order = ("name", "access_level", "description", "is_active")
        skip_unchanged = True
        report_skipped = True
