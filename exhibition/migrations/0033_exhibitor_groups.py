from django.db import migrations, models
from django.db.models import F


def start_group_counts_at_the_event_count(apps, schema_editor):
    ExhibitorSettings = apps.get_model('exhibition', 'ExhibitorSettings')
    ExhibitorSettings.objects.update(
        paid_voucher_default_count=F('voucher_default_count'),
        non_paid_voucher_default_count=F('voucher_default_count'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('exhibition', '0032_exhibitionemaildraft'),
    ]

    operations = [
        migrations.AddField(
            model_name='exhibitorinfo',
            name='exhibitor_group',
            field=models.CharField(blank=True, choices=[('paid', 'Paid exhibitor'), ('non_paid', 'Non-paid exhibitor')], db_default='', default='', max_length=16, verbose_name='Exhibitor group'),
        ),
        migrations.AddField(
            model_name='exhibitorsettings',
            name='paid_voucher_default_count',
            field=models.PositiveIntegerField(db_default=1, default=1, verbose_name='Vouchers per paid exhibitor'),
        ),
        migrations.AddField(
            model_name='exhibitorsettings',
            name='non_paid_voucher_default_count',
            field=models.PositiveIntegerField(db_default=1, default=1, verbose_name='Vouchers per non-paid exhibitor'),
        ),
        migrations.RunPython(start_group_counts_at_the_event_count, migrations.RunPython.noop),
    ]
