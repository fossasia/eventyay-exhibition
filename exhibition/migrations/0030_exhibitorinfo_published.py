from django.db import migrations, models
from django.db.models import Q


def publish_visible_organizations(apps, schema_editor):
    """Only what the public site actually rendered today stays visible after the split."""
    ExhibitorInfo = apps.get_model("exhibition", "ExhibitorInfo")
    has_logo = Q(logo__isnull=False) & ~Q(logo="")
    has_banner = Q(banner__isnull=False) & ~Q(banner="")
    on_exhibitor_page = Q(is_exhibitor=True) & has_logo & has_banner
    on_front_page = Q(is_sponsor=True) & has_logo & Q(sponsor_group__show_on_front_page=True)
    ExhibitorInfo.objects.filter(Q(active=True) & (on_exhibitor_page | on_front_page)).update(published=True)


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0029_organization_banner_and_request_rename"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitorinfo",
            name="published",
            field=models.BooleanField(
                default=False,
                help_text="Only published organizations appear on the public event website.",
                verbose_name="Published",
            ),
        ),
        migrations.RunPython(publish_visible_organizations, migrations.RunPython.noop),
    ]
