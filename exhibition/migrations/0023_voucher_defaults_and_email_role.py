import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0001_initial"),
        ("exhibition", "0022_exhibitionproposal_content_locale"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitionemailqueue",
            name="role",
            field=models.CharField(blank=True, db_index=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="exhibitorsettings",
            name="voucher_default_count",
            field=models.PositiveIntegerField(default=1, verbose_name="Default number of vouchers"),
        ),
        migrations.AddField(
            model_name="exhibitorsettings",
            name="voucher_default_price_mode",
            field=models.CharField(
                choices=[
                    ("none", "No effect"),
                    ("set", "Set product price to"),
                    ("subtract", "Subtract from product price"),
                    ("percent", "Reduce product price by (%)"),
                ],
                default="none",
                max_length=20,
                verbose_name="Default price effect",
            ),
        ),
        migrations.AddField(
            model_name="exhibitorsettings",
            name="voucher_default_product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="base.product",
                verbose_name="Default ticket product",
            ),
        ),
        migrations.AddField(
            model_name="exhibitorsettings",
            name="voucher_default_value",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True, verbose_name="Default value"
            ),
        ),
        migrations.AddField(
            model_name="sponsorgroup",
            name="voucher_default_count",
            field=models.PositiveIntegerField(default=1, verbose_name="Default number of vouchers"),
        ),
        migrations.AddField(
            model_name="sponsorgroup",
            name="voucher_default_price_mode",
            field=models.CharField(
                choices=[
                    ("none", "No effect"),
                    ("set", "Set product price to"),
                    ("subtract", "Subtract from product price"),
                    ("percent", "Reduce product price by (%)"),
                ],
                default="none",
                max_length=20,
                verbose_name="Default price effect",
            ),
        ),
        migrations.AddField(
            model_name="sponsorgroup",
            name="voucher_default_product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="base.product",
                verbose_name="Default ticket product",
            ),
        ),
        migrations.AddField(
            model_name="sponsorgroup",
            name="voucher_default_value",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True, verbose_name="Default value"
            ),
        ),
    ]
