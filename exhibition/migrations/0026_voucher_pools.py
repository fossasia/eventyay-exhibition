from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0025_voucher_email_attachment"),
    ]

    operations = [
        migrations.RemoveField(model_name="exhibitorsettings", name="voucher_default_product"),
        migrations.RemoveField(model_name="exhibitorsettings", name="voucher_default_price_mode"),
        migrations.RemoveField(model_name="exhibitorsettings", name="voucher_default_value"),
        migrations.RemoveField(model_name="sponsorgroup", name="voucher_default_product"),
        migrations.RemoveField(model_name="sponsorgroup", name="voucher_default_price_mode"),
        migrations.RemoveField(model_name="sponsorgroup", name="voucher_default_value"),
        migrations.AlterField(
            model_name="exhibitorsettings",
            name="voucher_default_count",
            field=models.PositiveIntegerField(default=1, verbose_name="Vouchers per exhibitor"),
        ),
        migrations.AlterField(
            model_name="sponsorgroup",
            name="voucher_default_count",
            field=models.PositiveIntegerField(default=1, verbose_name="Vouchers per exhibitor"),
        ),
        migrations.AddField(
            model_name="exhibitorsettings",
            name="voucher_pool_tag",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The voucher tag created under Tickets → Vouchers that exhibitors draw their codes from.",
                max_length=255,
                verbose_name="Exhibitor voucher pool",
            ),
        ),
        migrations.AddField(
            model_name="exhibitorsettings",
            name="sponsor_voucher_pool_tag",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Leave empty to draw sponsor codes from the exhibitor pool as well.",
                max_length=255,
                verbose_name="Sponsor voucher pool",
            ),
        ),
    ]
