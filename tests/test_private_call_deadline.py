from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import ExhibitionRequest, ExhibitionRequestState, ExhibitorSettings
from exhibition.views import call_access_session_key


@pytest.fixture
def private_call(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        return ExhibitorSettings.objects.create(
            event=event,
            call_enabled=True,
            call_private=True,
            call_deadline=timezone.now() - timedelta(days=1),
            exhibitors_access_mail_subject="",
            exhibitors_access_mail_body="",
        )


@pytest.fixture
def applicant(db):
    return User.objects.create_user(email="late-applicant@example.org", password=None)


def call_url(event, name="public_call", **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def request_data():
    return {
        "action": "submit",
        "name": "Late exhibitor",
        "social_links-TOTAL_FORMS": "0",
        "social_links-INITIAL_FORMS": "0",
        "extra_links-TOTAL_FORMS": "0",
        "extra_links-INITIAL_FORMS": "0",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("hide_after_deadline", [False, True])
@pytest.mark.parametrize("authenticated", [False, True])
def test_private_link_allows_late_call_page(event, private_call, applicant, client, hide_after_deadline, authenticated):
    with scopes_disabled():
        private_call.call_hide_after_deadline = hide_after_deadline
        private_call.save()
    if authenticated:
        client.force_login(applicant)

    response = client.get(call_url(event, "public_call_secret", secret=private_call.call_secret))

    assert response.status_code == 200
    assert client.session[call_access_session_key(event)] == private_call.call_secret
    html = response.content.decode()
    assert "Requests are closed" not in html
    assert "This call closed on" not in html
    if authenticated:
        assert "Submit a request" in html
    else:
        assert "Log in or create an account to submit your application." in html


@pytest.mark.django_db
@pytest.mark.parametrize("hide_after_deadline", [False, True])
def test_private_link_allows_late_submission(
    event, private_call, applicant, client, hide_after_deadline, image_uploads
):
    with scopes_disabled():
        private_call.call_hide_after_deadline = hide_after_deadline
        private_call.save()
    client.get(call_url(event, "public_call_secret", secret=private_call.call_secret))
    client.force_login(applicant)

    form_response = client.get(call_url(event, "request.add"))
    assert form_response.status_code == 200
    response = client.post(call_url(event, "request.add"), {**request_data(), **image_uploads()})
    assert response.status_code == 302
    assert response.url == call_url(event, "request.user_list")
    with scopes_disabled():
        saved = ExhibitionRequest.objects.get(event=event, user=applicant)
        assert saved.state == ExhibitionRequestState.SUBMITTED
        assert saved.submitted is not None
        assert str(saved.name) == "Late exhibitor"
    listing = client.get(response.url)
    assert listing.status_code == 200
    assert f'href="{call_url(event, "request.add")}"' in listing.content.decode()


@pytest.mark.django_db
def test_private_link_allows_submitting_saved_draft_after_deadline(
    event, private_call, applicant, client, image_uploads
):
    with scopes_disabled():
        draft = ExhibitionRequest.objects.create(
            event=event, user=applicant, name="Draft exhibitor", state=ExhibitionRequestState.DRAFT
        )
    client.force_login(applicant)
    client.get(call_url(event, "public_call_secret", secret=private_call.call_secret))
    edit_url = call_url(event, "request.user_edit", code=draft.code)

    response = client.post(edit_url, {**request_data(), **image_uploads()})

    assert response.status_code == 302
    with scopes_disabled():
        draft.refresh_from_db()
        assert draft.state == ExhibitionRequestState.SUBMITTED
        assert draft.submitted is not None
        assert str(draft.name) == "Late exhibitor"


@pytest.mark.django_db
def test_existing_applicant_without_private_code_cannot_submit_late(event, private_call, applicant, client):
    with scopes_disabled():
        draft = ExhibitionRequest.objects.create(
            event=event, user=applicant, name="Draft exhibitor", state=ExhibitionRequestState.DRAFT
        )
    client.force_login(applicant)

    listing = client.get(call_url(event, "request.user_list"))

    assert listing.status_code == 200
    assert f'href="{call_url(event, "request.add")}"' not in listing.content.decode()
    assert client.post(call_url(event, "request.add"), request_data()).status_code == 404
    response = client.post(call_url(event, "request.user_edit", code=draft.code), request_data())
    assert response.status_code == 302
    with scopes_disabled():
        draft.refresh_from_db()
        assert draft.state == ExhibitionRequestState.DRAFT
        assert str(draft.name) == "Draft exhibitor"


@pytest.mark.django_db
@pytest.mark.parametrize("access", ["missing", "invalid", "rotated", "another_event"])
def test_late_submission_requires_current_event_private_code(event, private_call, applicant, client, access):
    client.force_login(applicant)
    session = client.session
    if access == "invalid":
        session[call_access_session_key(event)] = "invalid-code"
    elif access == "rotated":
        session[call_access_session_key(event)] = private_call.call_secret
        with scopes_disabled():
            private_call.regenerate_call_secret()
    elif access == "another_event":
        session[f"exhibition_call_access_{event.pk + 1}"] = private_call.call_secret
    session.save()

    assert client.get(call_url(event)).status_code == 404
    assert client.post(call_url(event, "request.add"), request_data()).status_code == 404
    with scopes_disabled():
        assert not ExhibitionRequest.objects.filter(event=event, user=applicant).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("hide_after_deadline", [False, True])
def test_public_call_still_enforces_deadline_with_old_private_session(
    event, private_call, applicant, client, hide_after_deadline
):
    with scopes_disabled():
        private_call.call_private = False
        private_call.call_hide_after_deadline = hide_after_deadline
        private_call.save()
    client.force_login(applicant)
    session = client.session
    session[call_access_session_key(event)] = private_call.call_secret
    session.save()

    response = client.post(call_url(event, "request.add"), request_data())

    assert response.status_code == (404 if hide_after_deadline else 302)
    if not hide_after_deadline:
        assert response.url == call_url(event)
        landing = client.get(response.url)
        assert "Requests are closed" in landing.content.decode()
    with scopes_disabled():
        assert not ExhibitionRequest.objects.filter(event=event, user=applicant).exists()


@pytest.mark.django_db
def test_disabled_call_rejects_previously_granted_private_access(event, private_call, applicant, client):
    with scopes_disabled():
        private_call.call_enabled = False
        private_call.save()
    client.force_login(applicant)
    session = client.session
    session[call_access_session_key(event)] = private_call.call_secret
    session.save()

    assert client.get(call_url(event, "public_call_secret", secret=private_call.call_secret)).status_code == 404
    assert client.post(call_url(event, "request.add"), request_data()).status_code == 404
    with scopes_disabled():
        assert not ExhibitionRequest.objects.filter(event=event, user=applicant).exists()
