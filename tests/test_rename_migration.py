import importlib

import pytest
from django.apps import apps
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import ExhibitionRequest, ExhibitorSettings

migration = importlib.import_module("exhibition.migrations.0026_organization_banner_and_request_rename")


@pytest.mark.django_db
def test_forwards_moves_customized_lifecycle_mail_settings_to_the_new_role_keys(event):
    event.settings.set("exhibition_mail_proposal_accepted_subject", "You are in")
    event.settings.set("exhibition_mail_proposal_accepted_body", "Welcome {request_name}")

    migration.migrate_stored_names_forwards(apps, None)
    event.settings.flush()

    assert event.settings.get("exhibition_mail_request_accepted_subject") == "You are in"
    assert event.settings.get("exhibition_mail_request_accepted_body") == "Welcome {request_name}"
    assert event.settings.get("exhibition_mail_proposal_accepted_subject") is None


@pytest.mark.django_db
def test_forwards_renames_header_image_keys_in_persisted_json(event):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.create(
            event=event,
            exhibitors_access_mail_subject="",
            exhibitors_access_mail_body="",
            request_field_settings={"header_image": {"active": True, "required": False, "position": 4}},
        )
        user = User.objects.create_user(email="snap@example.com", password="pw")
        exhibition_request = ExhibitionRequest.objects.create(
            event=event,
            user=user,
            name="Acme",
            accepted_profile_snapshot={"header_image": "https://example.com/h.png", "logo": ""},
        )

        migration.migrate_stored_names_forwards(apps, None)

        settings.refresh_from_db()
        exhibition_request.refresh_from_db()
        assert settings.request_field_settings == {"banner": {"active": True, "required": False, "position": 4}}
        assert exhibition_request.accepted_profile_snapshot == {"banner": "https://example.com/h.png", "logo": ""}


@pytest.mark.django_db
def test_backwards_restores_the_old_names(event):
    event.settings.set("exhibition_mail_request_new_subject", "New one")
    with scopes_disabled():
        settings = ExhibitorSettings.objects.create(
            event=event,
            exhibitors_access_mail_subject="",
            exhibitors_access_mail_body="",
            request_field_settings={"banner": {"active": True}},
        )

        migration.migrate_stored_names_backwards(apps, None)
        event.settings.flush()
        settings.refresh_from_db()

    assert event.settings.get("exhibition_mail_proposal_new_subject") == "New one"
    assert settings.request_field_settings == {"header_image": {"active": True}}
