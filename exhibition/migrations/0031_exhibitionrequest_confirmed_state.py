from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0030_exhibitorinfo_published"),
    ]

    operations = [
        migrations.AlterField(
            model_name="exhibitionrequest",
            name="state",
            field=models.CharField(
                choices=[
                    ("draft", "draft"),
                    ("submitted", "submitted"),
                    ("accepted", "accepted"),
                    ("confirmed", "confirmed"),
                    ("rejected", "rejected"),
                    ("withdrawn", "withdrawn"),
                ],
                db_index=True,
                default="submitted",
                max_length=16,
            ),
        ),
    ]
