from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0038_exhibitionemaildraft_attachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitionemailqueue",
            name="bcc",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="exhibitionemaildraft",
            name="reply_to",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="exhibitionemaildraft",
            name="bcc",
            field=models.TextField(blank=True, default=""),
        ),
    ]
