from datetime import timedelta
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.db import IntegrityError
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.dashboard import MyExhibitionInvitationView
from exhibition.forms import ExhibitionMailTemplatesForm
from exhibition.models import ExhibitionEmailQueue, ExhibitorInfo, ExhibitorSettings
from exhibition.utils import invitation_account, user_can_edit_profile, user_exhibitors
from exhibition.views import queue_profile_invitation

PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    ExhibitorSettings.objects.create(event=event, exhibitors_access_mail_subject="", exhibitors_access_mail_body="")
    return event


def _profile(event, email="Booth@Example.com", **kwargs):
    kwargs.setdefault("name", "Booth Co")
    return ExhibitorInfo.objects.create(event=event, email=email, **kwargs)


def _invite(event, email="Booth@Example.com", **kwargs):
    exhibitor = _profile(event, email=email, **kwargs)
    return exhibitor, queue_profile_invitation(event, exhibitor)


def _request(method="get", data=None, user=None):
    request = getattr(RequestFactory(), method)("/", data=data or {})
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    request.user = user or AnonymousUser()
    return request


def _set_password(token, password=PASSWORD):
    request = _request("post", {"password": password, "password_repeat": password})
    return MyExhibitionInvitationView().post(request, token=token)


@pytest.mark.django_db
def test_new_address_gets_an_account_and_the_new_account_email(mail_event):
    with scopes_disabled():
        _exhibitor, queued = _invite(mail_event)
        user = User.objects.get(email__iexact="booth@example.com")

        assert queued.role == mail_helpers.EXHIBITOR_PROFILE_NEW_ACCOUNT
        assert f"{settings.SITE_URL.rstrip('/')}/exhibitor-invitation/{user.pw_reset_token}/" in queued.body
        assert mail_helpers.my_exhibitions_url() in queued.body
        for placeholder in ("{invitation_link}", "{my_exhibitions_url}", "{login_email}", "{exhibitor_name}"):
            assert placeholder not in queued.body
        assert queued.sent_at is None


@pytest.mark.django_db
def test_existing_account_is_matched_ignoring_case_and_gets_the_existing_account_email(mail_event):
    with scopes_disabled():
        User.objects.create_user(email="booth@example.com", password="pw")

        _exhibitor, queued = _invite(mail_event, email="BOOTH@example.COM")

        assert User.objects.filter(email__iexact="booth@example.com").count() == 1
        assert queued.role == mail_helpers.EXHIBITOR_PROFILE
        assert "/exhibitor-invitation/" not in queued.body
        assert mail_helpers.my_exhibitions_url() in queued.body


@pytest.mark.django_db
def test_second_profile_before_the_password_is_set_resends_the_same_link(mail_event):
    with scopes_disabled():
        _invite(mail_event)
        token = User.objects.get(email__iexact="booth@example.com").pw_reset_token

        _exhibitor, queued = _invite(mail_event, name="Second Booth")

        assert User.objects.filter(email__iexact="booth@example.com").count() == 1
        assert queued.role == mail_helpers.EXHIBITOR_PROFILE_NEW_ACCOUNT
        assert token in queued.body


@pytest.mark.django_db
def test_profile_without_an_email_creates_no_account_and_queues_nothing(mail_event):
    with scopes_disabled():
        users_before = User.objects.count()

        _exhibitor, queued = _invite(mail_event, email="")

        assert queued is None
        assert User.objects.count() == users_before
        assert not ExhibitionEmailQueue.objects.filter(event=mail_event).exists()


@pytest.mark.django_db
def test_sponsor_only_profile_creates_no_account(mail_event):
    with scopes_disabled():
        _exhibitor, queued = _invite(mail_event, is_sponsor=True, is_exhibitor=False)

        assert queued is None
        assert not User.objects.filter(email__iexact="booth@example.com").exists()


@pytest.mark.django_db
def test_setting_the_password_verifies_the_address_and_opens_the_profile(mail_event):
    with scopes_disabled():
        exhibitor, _queued = _invite(mail_event)
        user = User.objects.get(email__iexact="booth@example.com")
        assert not user_can_edit_profile(user, exhibitor)

        response = _set_password(user.pw_reset_token)
        user.refresh_from_db()

        assert response.status_code == 302
        assert response.url.startswith(reverse("auth.login"))
        assert "next=" in response.url
        assert user.check_password(PASSWORD)
        assert user.pw_reset_token is None
        assert EmailAddress.objects.get(user=user).verified
        assert exhibitor in user_exhibitors(user)
        assert user_can_edit_profile(user, exhibitor)


@pytest.mark.django_db
def test_the_link_works_only_once(mail_event):
    with scopes_disabled():
        _invite(mail_event)
        user = User.objects.get(email__iexact="booth@example.com")
        token = user.pw_reset_token
        _set_password(token)

        response = _set_password(token, password="Another-Password-7")
        user.refresh_from_db()

        assert response.url == reverse("auth.login")
        assert user.check_password(PASSWORD)


@pytest.mark.django_db
def test_an_expired_link_neither_sets_a_password_nor_verifies(mail_event):
    with scopes_disabled():
        exhibitor, _queued = _invite(mail_event)
        user = User.objects.get(email__iexact="booth@example.com")
        user.pw_reset_time = now() - timedelta(minutes=1)
        user.save(update_fields=["pw_reset_time"])

        response = _set_password(user.pw_reset_token)
        user.refresh_from_db()

        assert response.url == reverse("auth.login")
        assert not user.check_password(PASSWORD)
        assert not EmailAddress.objects.filter(user=user, verified=True).exists()
        assert not user_can_edit_profile(user, exhibitor)


@pytest.mark.django_db
def test_an_unknown_link_redirects_to_login(mail_event):
    with scopes_disabled():
        response = MyExhibitionInvitationView().get(_request(), token="not-a-real-token")

        assert response.status_code == 302
        assert response.url == reverse("auth.login")


@pytest.mark.django_db
def test_a_valid_link_shows_the_password_form(mail_event):
    with scopes_disabled():
        _invite(mail_event)
        token = User.objects.get(email__iexact="booth@example.com").pw_reset_token

        with patch.object(MyExhibitionInvitationView, "render_form", return_value=HttpResponse("form")) as render:
            response = MyExhibitionInvitationView().get(_request(), token=token)

        assert response.content == b"form"
        render.assert_called_once()


@pytest.mark.django_db
def test_mismatched_passwords_keep_the_link_usable(mail_event):
    with scopes_disabled():
        _invite(mail_event)
        user = User.objects.get(email__iexact="booth@example.com")
        token = user.pw_reset_token
        request = _request("post", {"password": PASSWORD, "password_repeat": "something-else"})

        with patch.object(MyExhibitionInvitationView, "render_form", return_value=HttpResponse("form")):
            MyExhibitionInvitationView().post(request, token=token)
        user.refresh_from_db()

        assert user.pw_reset_token == token
        assert not EmailAddress.objects.filter(user=user, verified=True).exists()


@pytest.mark.django_db
def test_an_account_created_elsewhere_still_needs_confirmation(mail_event):
    with scopes_disabled():
        exhibitor = _profile(mail_event)
        user = User.objects.create_user(email="booth@example.com", password="pw")
        EmailAddress.objects.create(user=user, email="booth@example.com", verified=False, primary=True)

        assert not user_can_edit_profile(user, exhibitor)


@pytest.mark.django_db
def test_both_templates_are_editable_and_only_the_new_one_offers_the_invitation_link(mail_event):
    form = ExhibitionMailTemplatesForm(obj=mail_event)

    for role in (mail_helpers.EXHIBITOR_PROFILE, mail_helpers.EXHIBITOR_PROFILE_NEW_ACCOUNT):
        assert mail_helpers.subject_settings_key(role) in form.fields
        assert mail_helpers.body_settings_key(role) in form.fields
    new_account = mail_helpers.role_placeholder_names(mail_event, mail_helpers.EXHIBITOR_PROFILE_NEW_ACCOUNT)
    existing = mail_helpers.role_placeholder_names(mail_event, mail_helpers.EXHIBITOR_PROFILE)
    assert {"invitation_link", "my_exhibitions_url", "login_email", "exhibitor_name", "event_name"} <= set(new_account)
    assert "invitation_link" not in existing


@pytest.mark.django_db
def test_an_expired_link_is_renewed_when_the_exhibitor_is_invited_again(mail_event):
    with scopes_disabled():
        _invite(mail_event)
        user = User.objects.get(email__iexact="booth@example.com")
        old_token = user.pw_reset_token
        user.pw_reset_time = now() - timedelta(days=1)
        user.save(update_fields=["pw_reset_time"])

        _exhibitor, queued = _invite(mail_event, name="Second Booth")
        user.refresh_from_db()

        assert queued.role == mail_helpers.EXHIBITOR_PROFILE_NEW_ACCOUNT
        assert user.pw_reset_token != old_token
        assert user.pw_reset_time > now()
        assert user.pw_reset_token in queued.body


@pytest.mark.django_db
def test_an_account_that_set_its_password_gets_the_existing_account_email(mail_event):
    with scopes_disabled():
        _invite(mail_event)
        _set_password(User.objects.get(email__iexact="booth@example.com").pw_reset_token)

        _exhibitor, queued = _invite(mail_event, name="Second Booth")

        assert queued.role == mail_helpers.EXHIBITOR_PROFILE


@pytest.mark.django_db
def test_an_account_created_by_a_concurrent_invitation_is_reused(mail_event):
    with scopes_disabled():
        winner = User.objects.create_user(email="booth@example.com", password="pw")
        missing = type("Missing", (), {"first": lambda self: None})()

        with patch.object(User.objects, "filter", return_value=missing):
            with patch("eventyay.person.services.create_user", side_effect=IntegrityError):
                account, needs_password = invitation_account("booth@example.com")

        assert account == winner
        assert needs_password is False
        assert User.objects.filter(email__iexact="booth@example.com").count() == 1


@pytest.mark.django_db
def test_profiles_created_through_the_api_get_the_invitation(mail_event):
    from exhibition.api import ExhibitorInfoViewSet

    with scopes_disabled():
        exhibitor = _profile(mail_event)
        serializer = type("Serializer", (), {"save": lambda self: exhibitor})()
        view = ExhibitorInfoViewSet()
        view.request = type("Request", (), {"event": mail_event})()

        view.perform_create(serializer)

        assert ExhibitionEmailQueue.objects.filter(exhibitor=exhibitor).exists()
