import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

MAIL_ROLE_RENAMES = (
    ("proposal_new", "request_new"),
    ("proposal_accepted", "request_accepted"),
    ("proposal_rejected", "request_rejected"),
)

FIELD_KEY_RENAMES = (("header_image", "banner"),)


def _rename_mail_settings_keys(apps, renames):
    Event_SettingsStore = apps.get_model("base", "Event_SettingsStore")
    for old_role, new_role in renames:
        for suffix in ("subject", "body"):
            Event_SettingsStore.objects.filter(key=f"exhibition_mail_{old_role}_{suffix}").update(
                key=f"exhibition_mail_{new_role}_{suffix}"
            )


def _rename_json_keys(rows, attribute, renames):
    for row in rows:
        data = getattr(row, attribute)
        if not isinstance(data, dict):
            continue
        changed = False
        for old_key, new_key in renames:
            if old_key in data and new_key not in data:
                data[new_key] = data.pop(old_key)
                changed = True
        if changed:
            setattr(row, attribute, data)
            row.save(update_fields=[attribute])


def _migrate_stored_names(apps, mail_renames, key_renames):
    _rename_mail_settings_keys(apps, mail_renames)
    ExhibitorSettings = apps.get_model("exhibition", "ExhibitorSettings")
    ExhibitionRequest = apps.get_model("exhibition", "ExhibitionRequest")
    _rename_json_keys(ExhibitorSettings.objects.all(), "request_field_settings", key_renames)
    _rename_json_keys(
        ExhibitionRequest.objects.exclude(accepted_profile_snapshot__isnull=True),
        "accepted_profile_snapshot",
        key_renames,
    )


def migrate_stored_names_forwards(apps, schema_editor):
    _migrate_stored_names(apps, MAIL_ROLE_RENAMES, FIELD_KEY_RENAMES)


def migrate_stored_names_backwards(apps, schema_editor):
    _migrate_stored_names(
        apps,
        tuple((new, old) for old, new in MAIL_ROLE_RENAMES),
        tuple((new, old) for old, new in FIELD_KEY_RENAMES),
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("base", "0001_initial"),
        ("exhibition", "0025_voucher_pools"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="exhibitorinfo",
            name="logo_url",
        ),
        migrations.RemoveField(
            model_name="exhibitorinfo",
            name="header_image_url",
        ),
        migrations.RemoveField(
            model_name="exhibitionproposal",
            name="logo_url",
        ),
        migrations.RemoveField(
            model_name="exhibitionproposal",
            name="header_image_url",
        ),
        migrations.AlterField(
            model_name="exhibitorinfo",
            name="sponsor_group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="organizations",
                to="exhibition.sponsorgroup",
            ),
        ),
        migrations.RenameField(
            model_name="exhibitorinfo",
            old_name="header_image",
            new_name="banner",
        ),
        migrations.RenameField(
            model_name="exhibitionproposal",
            old_name="header_image",
            new_name="banner",
        ),
        migrations.RenameModel(
            old_name="ExhibitionProposal",
            new_name="ExhibitionRequest",
        ),
        migrations.RenameModel(
            old_name="ExhibitionProposalSocialLink",
            new_name="ExhibitionRequestSocialLink",
        ),
        migrations.RenameModel(
            old_name="ExhibitionProposalExtraLink",
            new_name="ExhibitionRequestExtraLink",
        ),
        migrations.RenameField(
            model_name="exhibitorsettings",
            old_name="proposal_field_settings",
            new_name="request_field_settings",
        ),
        migrations.RenameField(
            model_name="exhibitionrequestsociallink",
            old_name="proposal",
            new_name="exhibition_request",
        ),
        migrations.RenameField(
            model_name="exhibitionrequestextralink",
            old_name="proposal",
            new_name="exhibition_request",
        ),
        migrations.RenameField(
            model_name="exhibitionanswer",
            old_name="proposal",
            new_name="exhibition_request",
        ),
        migrations.RenameField(
            model_name="exhibitionemailqueue",
            old_name="proposal",
            new_name="exhibition_request",
        ),
        migrations.AlterField(
            model_name="exhibitionrequest",
            name="approved_exhibitor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="source_requests",
                to="exhibition.exhibitorinfo",
            ),
        ),
        migrations.AlterField(
            model_name="exhibitionrequest",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="exhibition_requests",
                to="base.event",
            ),
        ),
        migrations.AlterField(
            model_name="exhibitionrequest",
            name="sponsor_group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="exhibition_requests",
                to="exhibition.sponsorgroup",
            ),
        ),
        migrations.AlterField(
            model_name="exhibitionrequest",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="exhibition_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(migrate_stored_names_forwards, migrate_stored_names_backwards),
    ]
