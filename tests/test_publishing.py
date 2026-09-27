import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.filters import ExhibitorFilterForm
from exhibition.models import ExhibitionRequest, ExhibitionRequestState, ExhibitorInfo
from exhibition.utils import create_exhibitor_from_request, public_exhibitors_queryset
from exhibition.views import ExhibitorPublishView

_IMAGES = {"logo": "exhibitors/logos/Acme/logo.png", "banner": "exhibitors/banners/Acme/banner.png"}


def _exhibitor(event, name="Acme", **kwargs):
    return ExhibitorInfo.objects.create(event=event, name=name, **{**_IMAGES, **kwargs})


def _organizer(event):
    return User.objects.create_user(email=f"organizer-{event.pk}@example.com", password="pw")


def _publish_view(event, data, organization_type=None, user=None):
    request = RequestFactory().post("/publish", data=data)
    request.event = event
    request.user = user or _organizer(event)
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    view = ExhibitorPublishView()
    view.request = request
    view.organization_type = organization_type
    view.kwargs = {}
    return view, request


@pytest.mark.django_db
def test_approving_a_request_does_not_publish_it(event):
    with scopes_disabled():
        user = User.objects.create_user(email="submitter@example.com", password="pw")
        exhibition_request = ExhibitionRequest.objects.create(
            event=event, user=user, name="Acme", state=ExhibitionRequestState.SUBMITTED, **_IMAGES
        )

        exhibitor = create_exhibitor_from_request(exhibition_request)

        assert exhibitor.active is True
        assert exhibitor.published is False
        assert exhibitor not in public_exhibitors_queryset(event)


@pytest.mark.django_db
def test_only_published_exhibitors_reach_the_public_page(event):
    with scopes_disabled():
        shown = _exhibitor(event, name="Shown", published=True)
        hidden = _exhibitor(event, name="Hidden", published=False)

        public = list(public_exhibitors_queryset(event))

        assert shown in public
        assert hidden not in public


@pytest.mark.django_db
def test_preview_includes_unpublished_exhibitors(event):
    with scopes_disabled():
        hidden = _exhibitor(event, name="Hidden", published=False)

        assert hidden in public_exhibitors_queryset(event, include_unpublished=True)


@pytest.mark.django_db
def test_publish_all_reveals_every_approved_organization(event):
    with scopes_disabled():
        first = _exhibitor(event, name="First")
        second = _exhibitor(event, name="Second")
        withdrawn = _exhibitor(event, name="Withdrawn", active=False)

        view, request = _publish_view(event, {"action": "publish_all", "confirmed": "1"})
        view.post(request)

        first.refresh_from_db()
        second.refresh_from_db()
        withdrawn.refresh_from_db()
        assert first.published is True
        assert second.published is True
        assert withdrawn.published is False


@pytest.mark.django_db
def test_publish_all_without_confirmation_only_previews(event):
    with scopes_disabled():
        waiting = _exhibitor(event)

        view, request = _publish_view(event, {"action": "publish_all"})
        response = view.post(request)

        waiting.refresh_from_db()
        assert response.template_name == "exhibitors/publish_confirm.html"
        assert list(response.context_data["waiting"]) == [waiting]
        assert waiting.published is False


@pytest.mark.django_db
def test_selected_organizations_publish_and_unpublish(event):
    with scopes_disabled():
        chosen = _exhibitor(event, name="Chosen")
        other = _exhibitor(event, name="Other")

        organizer = _organizer(event)

        view, request = _publish_view(event, {"action": "publish", "selected": [str(chosen.pk)]}, user=organizer)
        view.post(request)
        chosen.refresh_from_db()
        other.refresh_from_db()
        assert chosen.published is True
        assert other.published is False

        view, request = _publish_view(event, {"action": "unpublish", "selected": [str(chosen.pk)]}, user=organizer)
        view.post(request)
        chosen.refresh_from_db()
        assert chosen.published is False


@pytest.mark.django_db
def test_publish_actions_stay_within_their_list(event):
    with scopes_disabled():
        sponsor = _exhibitor(event, name="Sponsor", is_exhibitor=False, is_sponsor=True)

        view, request = _publish_view(
            event, {"action": "publish", "selected": [str(sponsor.pk)]}, organization_type="exhibitor"
        )
        view.post(request)

        sponsor.refresh_from_db()
        assert sponsor.published is False


@pytest.mark.django_db
def test_list_can_be_filtered_by_publication_status(event):
    with scopes_disabled():
        published = _exhibitor(event, name="Published", published=True)
        unpublished = _exhibitor(event, name="Unpublished", published=False)

        form = ExhibitorFilterForm(data={"published": "1"}, event=event)
        assert form.is_valid(), form.errors
        rows = list(form.filter_qs(ExhibitorInfo.objects.filter(event=event)))

        assert published in rows
        assert unpublished not in rows


@pytest.mark.django_db
def test_inactive_organizations_cannot_be_published(event):
    with scopes_disabled():
        withdrawn = _exhibitor(event, name="Withdrawn", active=False)

        view, request = _publish_view(event, {"action": "publish", "selected": [str(withdrawn.pk)]})
        view.post(request)

        withdrawn.refresh_from_db()
        assert withdrawn.published is False


@pytest.mark.django_db
def test_withdrawing_a_request_unpublishes_its_organization(event):
    with scopes_disabled():
        user = User.objects.create_user(email="published@example.com", password="pw")
        exhibition_request = ExhibitionRequest.objects.create(
            event=event, user=user, name="Acme", state=ExhibitionRequestState.SUBMITTED, **_IMAGES
        )
        exhibitor = create_exhibitor_from_request(exhibition_request)
        exhibitor.published = True
        exhibitor.save(update_fields=["published"])

        exhibition_request.refresh_from_db()
        exhibition_request.withdraw()

        exhibitor.refresh_from_db()
        assert exhibitor.active is False
        assert exhibitor.published is False


@pytest.mark.django_db
def test_re_approval_does_not_republish_on_its_own(event):
    with scopes_disabled():
        user = User.objects.create_user(email="again@example.com", password="pw")
        exhibition_request = ExhibitionRequest.objects.create(
            event=event, user=user, name="Acme", state=ExhibitionRequestState.SUBMITTED, **_IMAGES
        )
        exhibitor = create_exhibitor_from_request(exhibition_request)
        exhibitor.published = True
        exhibitor.save(update_fields=["published"])

        exhibition_request.refresh_from_db()
        exhibition_request.reject()
        exhibition_request.refresh_from_db()
        create_exhibitor_from_request(exhibition_request)

        exhibitor.refresh_from_db()
        assert exhibitor.active is True
        assert exhibitor.published is False
