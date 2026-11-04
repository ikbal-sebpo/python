"""Encrypt DocumentRequest.input_data — docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.3.

The salary certificate's three months of gross/bonus/net live in this JSON
column, so it becomes a Fernet token in a TEXT column. Unlike the payroll
conversions this one does NOT purge: letter requests are real HR records, so
the existing JSON text is re-written encrypted in place (raw SQL, because the
historical model already expects tokens at that point). Re-run safe — a value
that is already a token is skipped.
"""

import json

import core.fields
from django.db import migrations

TABLE = "documents_documentrequest"


def encrypt_existing(apps, schema_editor):
    from core import crypto

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT id, input_data FROM {TABLE}")
        rows = cursor.fetchall()
        done = 0
        for pk, raw in rows:
            if raw is None:
                continue
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if text.startswith("gAAAA"):
                continue
            document = json.loads(text) if text.strip() else {}
            cursor.execute(
                f"UPDATE {TABLE} SET input_data = %s WHERE id = %s",
                [crypto.encrypt(json.dumps(document)), pk],
            )
            done += 1
    print(f"  re-encrypted {TABLE}.input_data: {done} of {len(rows)} row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0005_documentextrafieldtype_fillable_by"),
    ]

    operations = [
        migrations.AlterField(
            model_name='documentrequest',
            name='input_data',
            field=core.fields.EncryptedJSONField(blank=True, default=dict, help_text='Raw form data — both standard fields (reason, letter_date) and dynamic ones merged in. Flattened into the PDF context.'),
        ),
        migrations.RunPython(encrypt_existing, migrations.RunPython.noop),
    ]
