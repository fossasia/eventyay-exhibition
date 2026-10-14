import django.db.models.deletion
import i18nfield.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0031_exhibitionrequest_confirmed_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExhibitionEmailDraft",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("state", models.CharField(blank=True, default="", max_length=40)),
                ("organization_type", models.CharField(blank=True, default="", max_length=40)),
                ("organizations", models.JSONField(blank=True, default=list)),
                (
                    "subject",
                    i18nfield.fields.I18nCharField(blank=True, max_length=255, null=True, verbose_name="Subject"),
                ),
                ("body", i18nfield.fields.I18nTextField(blank=True, null=True, verbose_name="Body")),
                ("scheduled_at", models.DateTimeField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="exhibition_email_drafts",
                        to="base.event",
                    ),
                ),
                (
                    "exhibition_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_drafts",
                        to="exhibition.exhibitionrequest",
                    ),
                ),
                (
                    "sponsor_group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="email_drafts",
                        to="exhibition.sponsorgroup",
                    ),
                ),
            ],
            options={
                "verbose_name": "Email draft",
                "verbose_name_plural": "Email drafts",
                "ordering": ("-updated", "-pk"),
            },
        ),
    ]
