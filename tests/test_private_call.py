from datetime import timedelta

import pytest
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import ExhibitorSettings
from exhibition.signals import exhibition_presale_nav_tab
from exhibition.views import (
    PublicCallSecretView,
    PublicCallView,
    UserRequestEditView,
    UserRequestWithdrawView,
    call_access_session_key,
)


def make_call_settings(event, private=False):
    return ExhibitorSettings.objects.create(
        event=event,
        call_enabled=True,
        call_private=private,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
    )


def _request(event, path="/"):
    request = RequestFactory().get(path)
    request.event = event
    request.session = {}
    return request


def _call_view(event, session=None):
    view = PublicCallView()
    request = _request(event)
    if session:
        request.session.update(session)
    view.request = request
    return view


@pytest.mark.django_db
def test_private_access_requires_matching_session_secret(event):
    with scopes_disabled():
        settings = make_call_settings(event, private=True)
        view = _call_view(event)
        assert view.can_access_private_call(settings) is False
        view.request.session[call_access_session_key(event)] = settings.call_secret
        assert view.can_access_private_call(settings) is True


def _secret_view(event):
    view = PublicCallSecretView()
    view.request = _request(event)
    return view


@pytest.mark.django_db
def test_secret_view_grants_access(event):
    with scopes_disabled():
        settings = make_call_settings(event, private=True)
        view = _secret_view(event)
        result = view.grant_secret_access(view.request, settings.call_secret)
        assert result == settings
        assert view.request.session[call_access_session_key(event)] == settings.call_secret


@pytest.mark.django_db
def test_secret_view_rejects_wrong_secret(event):
    with scopes_disabled():
        make_call_settings(event, private=True)
        view = _secret_view(event)
        with pytest.raises(Http404):
            view.grant_secret_access(view.request, "not-the-secret")


@pytest.mark.django_db
def test_regenerate_invalidates_old_secret(event):
    with scopes_disabled():
        settings = make_call_settings(event, private=True)
        old_secret = settings.call_secret
        settings.regenerate_call_secret()
        assert settings.call_secret != old_secret

        view = _call_view(event, session={call_access_session_key(event): old_secret})
        assert view.has_private_call_access(settings) is False
        secret_view = _secret_view(event)
        with pytest.raises(Http404):
            secret_view.grant_secret_access(secret_view.request, old_secret)


@pytest.mark.django_db
def test_nav_tab_hidden_for_private_call(event):
    with scopes_disabled():
        make_call_settings(event, private=True)
        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/call/" not in str(html)


@pytest.mark.django_db
def test_nav_tab_shown_for_public_call(event):
    with scopes_disabled():
        make_call_settings(event, private=False)
        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/call/" in str(html)


@pytest.mark.django_db
def test_request_list_hidden_for_private_call_without_access(event):
    from eventyay.base.models.auth import User

    from exhibition.views import UserRequestListView

    with scopes_disabled():
        settings = make_call_settings(event, private=True)
        view = UserRequestListView()
        view.request = _request(event)
        view.request.user = User.objects.create_user(email="stranger@e.com", password="pw")
        assert view.can_access_private_call(settings) is False


@pytest.mark.django_db
def test_request_list_visible_to_existing_applicant(event):
    from eventyay.base.models.auth import User

    from exhibition.models import ExhibitionRequest, ExhibitionRequestState
    from exhibition.views import UserRequestListView

    with scopes_disabled():
        settings = make_call_settings(event, private=True)
        applicant = User.objects.create_user(email="applicant@e.com", password="pw")
        ExhibitionRequest.objects.create(
            event=event,
            user=applicant,
            name="Org",
            state=ExhibitionRequestState.SUBMITTED,
        )
        view = UserRequestListView()
        view.request = _request(event)
        view.request.user = applicant
        assert view.can_access_private_call(settings) is True


@pytest.mark.django_db
def test_secret_view_rejects_public_call(event):
    with scopes_disabled():
        settings = make_call_settings(event, private=False)
        view = _secret_view(event)
        with pytest.raises(Http404):
            view.grant_secret_access(view.request, settings.call_secret)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "view_class",
    [UserRequestEditView, UserRequestWithdrawView],
)
def test_owner_action_views_not_gated_by_private_secret(event, view_class):
    with scopes_disabled():
        settings = make_call_settings(event, private=True)
        view = view_class()
        view.request = _request(event)
        gate_blocks = view.enforce_private and settings.call_private and not view.has_private_call_access(settings)
        assert gate_blocks is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("enabled", "private", "status", "message"),
    [
        (
            True,
            True,
            "Open",
            "Requests can be submitted through the private link, even after the deadline.",
        ),
        (True, False, "Closed", "The deadline passed on"),
        (False, True, "Disabled", "The call is not published, so nobody can submit a request."),
    ],
)
def test_dashboard_shows_private_call_status(event, client, settings, enabled, private, status, message):
    settings.DEBUG = True
    settings.LANGUAGES = [("en", "English")]
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        ExhibitorSettings.objects.create(
            event=event,
            call_enabled=enabled,
            call_private=private,
            call_deadline=timezone.now() - timedelta(days=1),
            exhibitors_access_mail_subject="",
            exhibitors_access_mail_body="",
        )
        organizer = User.objects.create_user(email="organizer@example.org", password=None)
        team = event.organizer.teams.create(name="Organizers", all_events=True, can_change_event_settings=True)
        team.members.add(organizer)
    client.force_login(organizer)

    response = client.get(
        reverse(
            "plugins:exhibition:dashboard",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        ),
        follow=True,
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert f">{status}</span>" in html
    assert message in html
    if enabled and private:
        assert ">Closed</span>" not in html
        assert "The deadline passed on" not in html
