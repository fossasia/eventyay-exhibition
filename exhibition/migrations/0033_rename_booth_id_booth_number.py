from django.db import migrations, models
from django.db.models import Q
from i18nfield.strings import LazyI18nString

TEMPLATE_FIELDS = {"ExhibitionCustomEmailTemplate": ("subject", "body"), "ExhibitionEmailDraft": ("subject", "body")}


def renamed_text(text, old, new):
    return text.replace("{" + old + "}", "{" + new + "}") if isinstance(text, str) else text


def renamed_value(value, old, new):
    data = value.data if isinstance(value, LazyI18nString) else value
    if isinstance(data, dict):
        return LazyI18nString({lang: renamed_text(text, old, new) for lang, text in data.items()})
    if isinstance(data, str):
        return LazyI18nString(renamed_text(data, old, new))
    return value


def rename_placeholder(apps, old, new):
    settings_store = apps.get_model("base", "Event_SettingsStore")
    for setting in settings_store.objects.filter(key__startswith="exhibition_mail_"):
        value = renamed_text(setting.value, old, new)
        if value != setting.value:
            setting.value = value
            setting.save(update_fields=["value"])

    for model_name, fields in TEMPLATE_FIELDS.items():
        model = apps.get_model("exhibition", model_name)
        for row in model.objects.all().iterator():
            changed = []
            for field in fields:
                value = getattr(row, field)
                if value is None:
                    continue
                new_value = renamed_value(value, old, new)
                if new_value.data != (value.data if isinstance(value, LazyI18nString) else value):
                    setattr(row, field, new_value)
                    changed.append(field)
            if changed:
                row.save(update_fields=changed)


def unassign_blank_booth_numbers(apps, schema_editor):
    for model_name in ("ExhibitorInfo", "ExhibitionRequest"):
        model = apps.get_model("exhibition", model_name)
        model.objects.filter(booth_number__regex=r"^\s*$").update(booth_number=None)


def forwards(apps, schema_editor):
    rename_placeholder(apps, "booth_id", "booth_number")


def backwards(apps, schema_editor):
    rename_placeholder(apps, "booth_number", "booth_id")


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0032_exhibitionemaildraft"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="exhibitorinfo",
            name="exhibition_event_booth_id_uniq",
        ),
        migrations.RenameField(
            model_name="exhibitorinfo",
            old_name="booth_id",
            new_name="booth_number",
        ),
        migrations.RenameField(
            model_name="exhibitionrequest",
            old_name="booth_id",
            new_name="booth_number",
        ),
        migrations.RenameField(
            model_name="lead",
            old_name="booth_id",
            new_name="booth_number",
        ),
        migrations.RunPython(unassign_blank_booth_numbers, reverse_code=migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="exhibitorinfo",
            constraint=models.UniqueConstraint(
                condition=Q(booth_number__regex=r"\S"),
                fields=("event", "booth_number"),
                name="exhibition_event_booth_number_uniq",
            ),
        ),
        migrations.RunPython(forwards, reverse_code=backwards),
    ]
