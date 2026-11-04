from django.db import migrations


class Migration(migrations.Migration):
    """Fix schema drift: both sort_order columns are NOT NULL in the live
    MySQL DB despite the model declaring null=True since migration 0008
    (which added them nullable — the live columns never matched).
    """

    dependencies = [
        ('roster', '0008_alter_shiftattribute_options_alter_shifttype_options_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE roster_shiftattribute MODIFY sort_order int unsigned NULL DEFAULT NULL;",
                "ALTER TABLE roster_shifttype MODIFY sort_order int unsigned NULL DEFAULT NULL;",
            ],
            reverse_sql=[
                "ALTER TABLE roster_shifttype MODIFY sort_order int unsigned NOT NULL DEFAULT 0;",
                "ALTER TABLE roster_shiftattribute MODIFY sort_order int unsigned NOT NULL DEFAULT 0;",
            ],
        ),
    ]
