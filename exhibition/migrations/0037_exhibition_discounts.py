import django.core.validators
import django.db.models.deletion
import eventyay.base.models.base
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('exhibition', '0036_exhibition_sales'),
    ]

    operations = [
        migrations.AddField(
            model_name='exhibitionproduct',
            name='hide_without_discount',
            field=models.BooleanField(default=False, help_text='The product is hidden from the public list until a buyer applies a discount code that covers it and shows hidden products.', verbose_name='Only sold with a discount code'),
        ),
        migrations.CreateModel(
            name='ExhibitionDiscount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=255, validators=[django.core.validators.RegexValidator('^[A-Za-z0-9_.-]+$', 'Use only letters, numbers, dots, dashes and underscores.')], verbose_name='Discount code')),
                ('price_mode', models.CharField(choices=[('set', 'Set the product price to'), ('subtract', 'Subtract from the product price'), ('percent', 'Reduce the product price by (%)')], default='percent', max_length=16, verbose_name='Price effect')),
                ('value', models.DecimalField(decimal_places=2, default=0, max_digits=13, verbose_name='Value')),
                ('max_usages', models.PositiveIntegerField(default=1, help_text='How many products in total can be bought with this code.', verbose_name='Maximum uses')),
                ('valid_until', models.DateTimeField(blank=True, null=True, verbose_name='Valid until')),
                ('show_hidden_products', models.BooleanField(default=False, help_text='Lets the code reveal the products it covers that are only sold with a discount code.', verbose_name='Show hidden products')),
                ('tag', models.CharField(blank=True, db_index=True, help_text='Groups codes, for example by partner or campaign. Never shown to buyers.', max_length=255, verbose_name='Tag')),
                ('comment', models.TextField(blank=True, help_text='An internal note, never shown to buyers.', verbose_name='Comment')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exhibition_discounts', to='base.event')),
                ('products', models.ManyToManyField(blank=True, help_text='Leave all unchecked to apply the code to every product.', related_name='discounts', to='exhibition.exhibitionproduct', verbose_name='Products')),
            ],
            options={
                'verbose_name': 'Exhibition discount',
                'verbose_name_plural': 'Exhibition discounts',
                'ordering': ('code', 'id'),
                'constraints': [
                    models.UniqueConstraint(fields=('event', 'code'), name='exhibition_discount_code_unique_per_event'),
                    models.CheckConstraint(condition=models.Q(('price_mode__in', ['set', 'subtract', 'percent'])), name='exhibition_discount_price_mode_known'),
                    models.CheckConstraint(condition=models.Q(('value__gte', 0)), name='exhibition_discount_value_not_negative'),
                    models.CheckConstraint(condition=models.Q(models.Q(('price_mode', 'percent'), _negated=True), ('value__lte', 100), _connector='OR'), name='exhibition_discount_percent_at_most_100'),
                ],
            },
            bases=(models.Model, eventyay.base.models.base.LoggingMixin),
        ),
        migrations.AddField(
            model_name='exhibitionorderposition',
            name='original_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=13, null=True, verbose_name='Price before discount'),
        ),
        migrations.AddField(
            model_name='exhibitionorderposition',
            name='discount',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='order_positions', to='exhibition.exhibitiondiscount', verbose_name='Discount code'),
        ),
    ]
