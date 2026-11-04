from django.db import migrations, models
import django.utils.translation


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0004_alter_messagetemplate_body_html_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentextrafieldtype",
            name="fillable_by",
            field=models.CharField(
                choices=[
                    ("REQUESTER", django.utils.translation.gettext_lazy("Requester")),
                    ("APPROVER", django.utils.translation.gettext_lazy("Approver")),
                ],
                default="REQUESTER",
                help_text="Who fills this value in the document workflow.",
                max_length=20,
            ),
        ),
    ]
