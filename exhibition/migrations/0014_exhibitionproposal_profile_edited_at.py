from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0013_exhibitionemailqueue_scheduled_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitionproposal",
            name="profile_edited_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
