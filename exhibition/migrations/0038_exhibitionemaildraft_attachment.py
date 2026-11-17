import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0001_initial"),
        ("exhibition", "0037_exhibitor_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitionemaildraft",
            name="attachment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="base.cachedfile",
            ),
        ),
    ]
