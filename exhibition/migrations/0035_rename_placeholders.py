import re

from django.db import migrations
from i18nfield.strings import LazyI18nString

RENAMES = (("name", "applicant_name"), ("voucher_list", "voucher_details"))
FIELDS = {"ExhibitionCustomEmailTemplate": ("subject", "body"), "ExhibitionEmailDraft": ("subject", "body")}


def rename_placeholder(text, old, new):
    """Rename ``{old}`` to ``{new}`` in a stored email text, including the editor's ``data-variable`` chips."""
    if not text:
        return text
    text = text.replace(f"{{{old}}}", f"{{{new}}}")
    return re.sub(rf'(data-variable=\\?["\']){re.escape(old)}(\\?["\'])', rf"\g<1>{new}\g<2>", text)


def rename_all(text, renames):
    for old, new in renames:
        text = rename_placeholder(text, old, new)
    return text


def renamed(value, renames):
    """The value with the placeholders renamed, or ``None`` when its text does not change."""
    data = value.data if isinstance(value, LazyI18nString) else value
    if isinstance(data, dict):
        new_data = {lang: rename_all(text, renames) for lang, text in data.items()}
    elif isinstance(data, str):
        new_data = rename_all(data, renames)
    else:
        return None
    return LazyI18nString(new_data) if new_data != data else None


def rename_placeholders(apps, renames):
    settings_store = apps.get_model("base", "Event_SettingsStore")
    for setting in settings_store.objects.filter(key__startswith="exhibition_mail_"):
        value = rename_all(setting.value, renames)
        if value != setting.value:
            setting.value = value
            setting.save(update_fields=["value"])

    for model_name, fields in FIELDS.items():
        model = apps.get_model("exhibition", model_name)
        for row in model.objects.all().iterator():
            changed = []
            for field in fields:
                value = getattr(row, field)
                new_value = renamed(value, renames) if value else None
                if new_value is not None:
                    setattr(row, field, new_value)
                    changed.append(field)
            if changed:
                row.save(update_fields=changed)


def forwards(apps, schema_editor):
    rename_placeholders(apps, RENAMES)


def backwards(apps, schema_editor):
    rename_placeholders(apps, [(new, old) for old, new in RENAMES])


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0034_exhibitorvoucher_keep_after_delete"),
    ]

    operations = [
        migrations.RunPython(forwards, reverse_code=backwards),
    ]
