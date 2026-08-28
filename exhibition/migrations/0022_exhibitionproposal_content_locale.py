import django.conf
from django.db import migrations, models


def backfill_content_locale(apps, schema_editor):
    ExhibitionProposal = apps.get_model("exhibition", "ExhibitionProposal")
    for proposal in ExhibitionProposal.objects.select_related("event").iterator():
        locale = getattr(proposal.event, "locale", None) or django.conf.settings.LANGUAGE_CODE
        if proposal.content_locale != locale:
            proposal.content_locale = locale
            proposal.save(update_fields=["content_locale"])


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0021_alter_exhibitionproposal_booth_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitionproposal",
            name="content_locale",
            field=models.CharField(default=django.conf.settings.LANGUAGE_CODE, max_length=32, verbose_name="Language"),
        ),
        migrations.RunPython(backfill_content_locale, migrations.RunPython.noop),
    ]
