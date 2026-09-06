from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0001_initial"),
        ("exhibition", "0025_voucher_email_attachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitorinfo",
            name="sessions",
            field=models.ManyToManyField(
                blank=True,
                related_name="exhibitors",
                to="base.submission",
                verbose_name="Related sessions",
            ),
        ),
    ]
