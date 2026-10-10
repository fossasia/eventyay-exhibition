import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.forms import ExhibitionRequestForm
from exhibition.models import (
    REQUEST_LOG_ACTIONS,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
    ExhibitorSettings,
)
from exhibition.views import RequestActionView, UserRequestConfirmView


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _request(event, email="applicant@example.com", state=ExhibitionRequestState.ACCEPTED, **kwargs):
    user = User.objects.create_user(email=email, password="pw")
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme")
    return ExhibitionRequest.objects.create(
        event=event,
        user=user,
        name="Acme",
        state=state,
        approved_exhibitor=exhibitor,
        **kwargs,
    )


def _confirm_view(event, exhibition_request, user, method="get"):
    request = getattr(RequestFactory(), method)("/")
    request.user = user
    request.event = event
    request.session = {}
    request._messages = FallbackStorage(request)
    view = UserRequestConfirmView()
    view.request = request
    view.kwargs = {"code": exhibition_request.code}
    view.args = ()
    return view, request


@pytest.mark.django_db
def test_confirm_moves_an_accepted_request_to_confirmed_and_logs_it(event):
    with scopes_disabled():
        exhibition_request = _request(event)

        exhibition_request.confirm(requestor=exhibition_request.user)
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == ExhibitionRequestState.CONFIRMED
        assert exhibition_request.all_logentries().filter(action_type=REQUEST_LOG_ACTIONS["confirm"]).exists()


@pytest.mark.django_db
def test_confirm_does_not_overwrite_a_state_changed_in_the_meantime(event):
    with scopes_disabled():
        exhibition_request = _request(event)
        stale = ExhibitionRequest.objects.get(pk=exhibition_request.pk)
        exhibition_request.reject()

        assert stale.confirm(requestor=stale.user) is False
        exhibition_request.refresh_from_db()

        assert stale.state == ExhibitionRequestState.REJECTED
        assert exhibition_request.state == ExhibitionRequestState.REJECTED
        assert not exhibition_request.all_logentries().filter(action_type=REQUEST_LOG_ACTIONS["confirm"]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [
        ExhibitionRequestState.DRAFT,
        ExhibitionRequestState.SUBMITTED,
        ExhibitionRequestState.REJECTED,
        ExhibitionRequestState.WITHDRAWN,
        ExhibitionRequestState.CONFIRMED,
    ],
)
def test_only_accepted_requests_can_be_confirmed(event, state):
    with scopes_disabled():
        assert not _request(event, state=state).can_be_confirmed


@pytest.mark.django_db
@pytest.mark.parametrize("state", [ExhibitionRequestState.ACCEPTED, ExhibitionRequestState.CONFIRMED])
def test_confirmed_requests_behave_like_accepted_ones(event, state):
    with scopes_disabled():
        exhibition_request = _request(event, state=state)

        assert exhibition_request.is_accepted
        assert exhibition_request.editable
        assert not exhibition_request.requires_open_call_to_edit
        assert exhibition_request.can_be_withdrawn
        assert not exhibition_request.can_be_reinstated


@pytest.mark.django_db
def test_confirmed_request_locks_the_organization_name(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, exhibitors_access_mail_subject="", exhibitors_access_mail_body="")
        exhibition_request = _request(event, state=ExhibitionRequestState.CONFIRMED)

        form = ExhibitionRequestForm(instance=exhibition_request, event=event)

        assert form.fields["name"].disabled


@pytest.mark.django_db
def test_withdrawing_a_confirmed_request_hides_its_profile(event):
    with scopes_disabled():
        exhibition_request = _request(event, state=ExhibitionRequestState.CONFIRMED)

        exhibition_request.withdraw()
        exhibition_request.approved_exhibitor.refresh_from_db()

        assert not exhibition_request.approved_exhibitor.active


@pytest.mark.django_db
def test_requester_confirms_through_the_link(event):
    with scopes_disabled():
        exhibition_request = _request(event)
        view, request = _confirm_view(event, exhibition_request, exhibition_request.user, method="post")

        response = view.post(request)
        exhibition_request.refresh_from_db()

        assert response.status_code == 302
        assert exhibition_request.state == ExhibitionRequestState.CONFIRMED


@pytest.mark.django_db
def test_opening_the_link_does_not_confirm_by_itself(event):
    with scopes_disabled():
        exhibition_request = _request(event)
        view, request = _confirm_view(event, exhibition_request, exhibition_request.user)

        response = view.get(request)
        exhibition_request.refresh_from_db()

        assert response.status_code == 200
        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED


@pytest.mark.django_db
def test_confirming_twice_changes_nothing(event):
    with scopes_disabled():
        exhibition_request = _request(event, state=ExhibitionRequestState.CONFIRMED)
        view, request = _confirm_view(event, exhibition_request, exhibition_request.user, method="post")

        response = view.post(request)
        exhibition_request.refresh_from_db()

        assert response.status_code == 302
        assert exhibition_request.state == ExhibitionRequestState.CONFIRMED
        assert not exhibition_request.all_logentries().filter(action_type=REQUEST_LOG_ACTIONS["confirm"]).exists()


@pytest.mark.django_db
def test_someone_else_cannot_confirm_the_request(event):
    with scopes_disabled():
        exhibition_request = _request(event)
        stranger = User.objects.create_user(email="stranger@example.com", password="pw")
        view, _request_obj = _confirm_view(event, exhibition_request, stranger, method="post")

        with pytest.raises(Http404):
            view.post(_request_obj)
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [ExhibitionRequestState.SUBMITTED, ExhibitionRequestState.REJECTED, ExhibitionRequestState.WITHDRAWN],
)
def test_requests_that_are_not_accepted_cannot_be_confirmed(event, state):
    with scopes_disabled():
        exhibition_request = _request(event, state=state)
        view, request = _confirm_view(event, exhibition_request, exhibition_request.user, method="post")

        view.post(request)
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == state


@pytest.mark.django_db
def test_organizer_action_confirms_on_behalf_of_the_applicant(event):
    with scopes_disabled():
        exhibition_request = _request(event)
        view = RequestActionView()
        view.request = RequestFactory().post("/")
        view.request.user = User.objects.create_user(email="orga@example.com", password="pw")

        view.apply_action(exhibition_request, "confirm")
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == ExhibitionRequestState.CONFIRMED
        assert "confirm" in RequestActionView.valid_actions


@pytest.mark.django_db
def test_confirm_is_only_offered_for_accepted_requests(event):
    with scopes_disabled():
        confirmed = _request(event, "b@example.com", ExhibitionRequestState.CONFIRMED)
        submitted = _request(event, "c@example.com", ExhibitionRequestState.SUBMITTED)

        assert "confirm" in _request(event).available_review_actions()
        assert "confirm" not in confirmed.available_review_actions()
        assert "confirm" not in submitted.available_review_actions()


@pytest.mark.django_db
def test_acceptance_email_carries_the_confirmation_link(mail_event):
    with scopes_disabled():
        exhibition_request = _request(mail_event, state=ExhibitionRequestState.SUBMITTED)

        queued = mail_helpers.queue_request_email(mail_event, exhibition_request, mail_helpers.REQUEST_ACCEPTED)

        assert mail_helpers.request_confirmation_url(exhibition_request) in queued.body
        assert exhibition_request.code in queued.body
        assert "{confirmation_url}" not in queued.body


@pytest.mark.django_db
def test_confirmation_link_goes_to_the_confirm_page(mail_event):
    with scopes_disabled():
        exhibition_request = _request(mail_event)

        url = mail_helpers.request_confirmation_url(exhibition_request)

        assert url.endswith(f"/exhibition/call/requests/{exhibition_request.code}/confirm/")


@pytest.mark.django_db
def test_login_prompt_points_to_the_main_login_not_the_talks_one(event):
    view = UserRequestConfirmView()
    view.request = RequestFactory().get("/")
    view.request.event = event

    assert view.get_login_url() == reverse("auth.login")


@pytest.mark.django_db
def test_organizer_action_reports_a_stale_confirmation_as_not_applied(event):
    with scopes_disabled():
        exhibition_request = _request(event)
        stale = ExhibitionRequest.objects.get(pk=exhibition_request.pk)
        exhibition_request.withdraw()
        view = RequestActionView()
        view.request = RequestFactory().post("/")
        view.request.user = User.objects.create_user(email="orga@example.com", password="pw")

        assert view.apply_action(stale, "confirm") is False
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == ExhibitionRequestState.WITHDRAWN
