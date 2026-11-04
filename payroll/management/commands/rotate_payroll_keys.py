"""Re-encrypt every encrypted column under the newest ``PAYROLL_FIELD_KEYS`` entry.

Procedure (docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.1):

1. ``manage.py generate_payroll_key`` → prepend it to ``PAYROLL_FIELD_KEYS``
   in ``.env`` (comma-separated, newest first), restart gunicorn + Celery.
2. ``manage.py rotate_payroll_keys`` — every row is read (any key can open
   it) and written back under the first key.
3. Remove the old key from ``.env``, restart again, replace the CTO's
   printed copy.

Writes go through ``QuerySet.update()`` per row so no model ``save()`` hook
(``full_clean``, the calculated-run write guard) gets in the way. Prints
counts only — never a value.
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from core.fields import encrypted_fields


class Command(BaseCommand):
    help = "Re-save every encrypted field under the newest PAYROLL_FIELD_KEYS entry."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        total = 0
        for model in apps.get_models():
            fields = encrypted_fields(model)
            if not fields:
                continue
            names = [f.attname for f in fields]
            qs = model._default_manager.all().only("pk", *names).order_by("pk")
            count = 0
            with transaction.atomic():
                for obj in qs.iterator(chunk_size=batch_size):
                    model._default_manager.filter(pk=obj.pk).update(
                        **{name: getattr(obj, name) for name in names}
                    )
                    count += 1
            total += count
            self.stdout.write(f"{model._meta.label}: {count} row(s) re-encrypted ({', '.join(names)})")
        self.stdout.write(self.style.SUCCESS(f"Done — {total} row(s) now under the newest key."))
