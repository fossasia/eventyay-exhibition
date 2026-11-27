import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('exhibition', '0039_exhibitionorderposition_addon_to_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='exhibitionproduct',
            name='min_per_order',
            field=models.PositiveIntegerField(blank=True, help_text='This product can only be bought if it is added to the order at least this many times. If you keep the field empty or set it to 0, there is no special limit for this product.', null=True, validators=[django.core.validators.MaxValueValidator(100)], verbose_name='Minimum amount per order'),
        ),
        migrations.AddField(
            model_name='exhibitionproduct',
            name='max_per_order',
            field=models.PositiveIntegerField(blank=True, help_text='This product can only be bought at most this many times within one order. If you keep the field empty or set it to 0, there is no special limit for this product.', null=True, validators=[django.core.validators.MaxValueValidator(100)], verbose_name='Maximum amount per order'),
        ),
    ]
