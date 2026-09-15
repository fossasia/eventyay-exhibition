import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0025_voucher_pools"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitorsettings",
            name="device_default_count",
            field=models.PositiveIntegerField(
                default=1,
                validators=[django.core.validators.MaxValueValidator(50)],
                help_text=(
                    "Created automatically when lead scanning is enabled for an exhibitor or sponsor that has no "
                    "devices yet, and the access email is queued right away. Set to 0 to add devices by hand."
                ),
                verbose_name="Devices per profile",
            ),
        ),
    ]
