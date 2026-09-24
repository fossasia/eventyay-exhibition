import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled

from exhibition.models import ExhibitorSettings
from exhibition.views import PublicCallView, call_auth_urls


def make_call_settings(event):
    return ExhibitorSettings.objects.create(
        event=event,
        call_enabled=True,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
    )


def _call_view(event, user):
    view = PublicCallView()
    request = RequestFactory().get("/")
    request.event = event
    request.session = {}
    request.user = user
    view.request = request
    view.kwargs = {}
    return view


@pytest.mark.django_db
def test_auth_urls_return_to_the_request_form(event):
    with scopes_disabled():
        submit_url = reverse(
            "plugins:exhibition:request.add",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        )
        urls = call_auth_urls(event)
        assert urls["call_login_url"].startswith(reverse("auth.login"))
        assert urls["call_register_url"].startswith(reverse("account_signup"))
        for url in urls.values():
            assert f"next={submit_url}" in url.replace("%2F", "/")


@pytest.mark.django_db
def test_anonymous_visitor_gets_auth_urls(event):
    with scopes_disabled():
        make_call_settings(event)
        context = _call_view(event, AnonymousUser()).get_context_data()
        assert context["call_login_url"]
        assert context["call_register_url"]
        assert "user_requests" not in context


@pytest.mark.django_db
def test_logged_in_visitor_gets_no_auth_urls(event):
    from eventyay.base.models.auth import User

    with scopes_disabled():
        make_call_settings(event)
        user = User.objects.create_user(email="exhibitor@e.com", password="pw")
        context = _call_view(event, user).get_context_data()
        assert "call_login_url" not in context
        assert "call_register_url" not in context
        assert context["user_requests"] is not None


# --- the rendered page, not just the context --------------------------------------------


def _call_page(event, client):
    event.plugins = "exhibition"
    event.live = True
    event.save(update_fields=["plugins", "live"])
    return client.get(
        reverse(
            "plugins:exhibition:public_call",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        )
    )


@pytest.mark.django_db
def test_open_call_renders_both_buttons_for_a_logged_out_visitor(event, client):
    with scopes_disabled():
        make_call_settings(event)
        submit_url = reverse(
            "plugins:exhibition:request.add",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        )

    response = _call_page(event, client)
    html = response.content.decode()

    assert response.status_code == 200
    assert "Log in or create an account to submit your application." in html
    assert f'href="{reverse("auth.login")}?next={submit_url}"' in html
    assert f'href="{reverse("account_signup")}?next={submit_url}"' in html


@pytest.mark.django_db
def test_closed_call_renders_no_prompt(event, client):
    from django.utils.timezone import now, timedelta

    with scopes_disabled():
        settings = make_call_settings(event)
        settings.call_deadline = now() - timedelta(days=1)
        settings.save()

    html = _call_page(event, client).content.decode()

    assert "Log in or create an account to submit your application." not in html
    assert "Requests are closed" in html


@pytest.mark.django_db
def test_logged_in_visitor_sees_the_submit_button_instead(event, client):
    from eventyay.base.models.auth import User

    with scopes_disabled():
        make_call_settings(event)
        applicant = User.objects.create_user(email="renders@example.org", password="pw")
    client.force_login(applicant)

    html = _call_page(event, client).content.decode()

    assert "Log in or create an account to submit your application." not in html
    assert "Submit a request" in html
    assert "View your requests" in html
