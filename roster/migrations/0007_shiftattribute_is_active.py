from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("roster", "0006_shiftchangelog"),
    ]

    operations = [
        migrations.AddField(
            model_name="shiftattribute",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
