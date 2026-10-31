import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0033_rename_booth_id_booth_number"),
    ]

    operations = [
        migrations.AlterField(
            model_name="exhibitorvoucher",
            name="exhibitor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="vouchers",
                to="exhibition.exhibitorinfo",
            ),
        ),
    ]
