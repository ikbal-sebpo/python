"""Apply the SEBPO salary certificate template and dynamic fields.

Run on a server with:

    python manage.py apply_salary_certificate_template

The command is idempotent and only touches the salary certificate message
template plus its request-form dynamic fields.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from documents.management.commands.seed_document_types import (
    SALARY_AUTO_FIELD_KEYS,
    SALARY_EXTRA_FIELDS,
    SALARY_KEY,
    _SALARY_CERT_BODY_HTML,
)
from documents.models import DocumentExtraFieldType, DocumentType, MessageTemplate


class Command(BaseCommand):
    help = "Apply the SEBPO salary certificate template and dynamic fields."

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            doc_type = DocumentType.objects.select_related("message_template").get(
                code="SALARY_CERT",
            )
        except DocumentType.DoesNotExist as exc:
            raise CommandError(
                "SALARY_CERT document type does not exist. "
                "Run `python manage.py seed_document_types` first."
            ) from exc

        template, made = MessageTemplate.objects.get_or_create(
            key=SALARY_KEY,
            defaults={
                "name": "Salary Certificate Body",
                "body_html": _SALARY_CERT_BODY_HTML,
                "is_active": True,
            },
        )
        if made:
            template_status = "created"
        else:
            updates = []
            for field, value in (
                ("name", "Salary Certificate Body"),
                ("body_html", _SALARY_CERT_BODY_HTML),
                ("is_active", True),
            ):
                if getattr(template, field) != value:
                    setattr(template, field, value)
                    updates.append(field)
            if updates:
                updates.append("updated_at")
                template.save(update_fields=updates)
            template_status = "updated" if updates else "exists"

        if doc_type.message_template_id != template.pk:
            doc_type.message_template = template
            doc_type.save(update_fields=["message_template", "updated_at"])

        DocumentExtraFieldType.objects.filter(
            document_type=doc_type,
            field_key__in=SALARY_AUTO_FIELD_KEYS,
        ).update(is_active=False)

        for spec in SALARY_EXTRA_FIELDS:
            DocumentExtraFieldType.objects.update_or_create(
                document_type=doc_type,
                field_key=spec["field_key"],
                defaults={
                    **spec,
                    "fillable_by": DocumentExtraFieldType.FillableBy.APPROVER,
                    "is_active": True,
                },
            )

        self.stdout.write(f"salary template: {template_status}")
        self.stdout.write(f"salary fields: ensured {len(SALARY_EXTRA_FIELDS)}")
        self.stdout.write(self.style.SUCCESS("Salary certificate template applied."))
