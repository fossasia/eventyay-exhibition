import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0001_initial"),
        ("exhibition", "0024_remove_optional_profile_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitorsettings",
            name="voucher_attach_csv",
            field=models.BooleanField(
                default=True,
                help_text="Adds a spreadsheet of the recipient's own voucher codes to the voucher email.",
                verbose_name="Attach voucher list as CSV",
            ),
        ),
        migrations.AddField(
            model_name="exhibitionemailqueue",
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
