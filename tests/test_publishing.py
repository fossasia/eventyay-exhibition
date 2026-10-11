import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models import Team
from eventyay.base.models.auth import StaffSession, User

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


def _public_url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}", kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs}
    )


def _client(user=None):
    client = Client()
    if user:
        client.force_login(user)
    return client


def _organizer_with_settings_access(event):
    user = _organizer(event)
    team = Team.objects.create(organizer=event.organizer, all_events=True, can_change_event_settings=True)
    team.members.add(user)
    return user


def _staff_client(email, *, admin_mode):
    """Log in a staff account, optionally with admin mode (an active staff session) switched on."""
    client = _client(User.objects.create_user(email=email, password="pw", is_staff=True))
    if admin_mode:
        session = client.session
        session.save()
        StaffSession.objects.create(user=User.objects.get(email=email), session_key=session.session_key)
    return client


@pytest.mark.django_db
def test_preview_opens_the_page_of_an_unpublished_exhibitor(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        hidden = _exhibitor(event, name="Hidden", published=False)
        organizer = _organizer_with_settings_access(event)

    response = _client(organizer).get(_public_url(event, "public_detail", pk=hidden.pk) + "?preview=1")

    assert response.status_code == 200
    assert "Preview: this page also shows approved organizations" in response.content.decode()


@pytest.mark.django_db
def test_unpublished_exhibitor_page_stays_hidden_outside_an_organizer_preview(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        hidden = _exhibitor(event, name="Hidden", published=False)
        organizer = _organizer_with_settings_access(event)
        visitor = User.objects.create_user(email="visitor@example.com", password="pw")
        order_viewer = User.objects.create_user(email="orders@example.com", password="pw")
        team = Team.objects.create(organizer=event.organizer, all_events=True, can_view_orders=True)
        team.members.add(order_viewer)
        staff_outside_admin_mode = _staff_client("staff@example.com", admin_mode=False)
    url = _public_url(event, "public_detail", pk=hidden.pk)

    assert _client().get(url + "?preview=1").status_code == 404
    assert _client(visitor).get(url + "?preview=1").status_code == 404
    assert _client(order_viewer).get(url + "?preview=1").status_code == 404
    assert staff_outside_admin_mode.get(url + "?preview=1").status_code == 404
    assert _client(organizer).get(url).status_code == 404


@pytest.mark.django_db
def test_staff_in_admin_mode_can_preview_an_unpublished_exhibitor(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        hidden = _exhibitor(event, name="Hidden", published=False)
        staff = _staff_client("admin-mode@example.com", admin_mode=True)

    response = staff.get(_public_url(event, "public_detail", pk=hidden.pk) + "?preview=1")

    assert response.status_code == 200


@pytest.mark.django_db
def test_visitors_find_no_link_to_an_unpublished_exhibitor(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        shown = _exhibitor(event, name="Shown", published=True)
        _exhibitor(event, name="Second", published=True)
        hidden = _exhibitor(event, name="Hidden", published=False)
    client = _client()
    hidden_url = _public_url(event, "public_detail", pk=hidden.pk)

    list_html = client.get(_public_url(event, "public_list") + "?preview=1").content.decode()
    detail_html = client.get(_public_url(event, "public_detail", pk=shown.pk) + "?preview=1").content.decode()

    assert hidden_url not in list_html
    assert "Hidden" not in list_html
    assert hidden_url not in detail_html
    assert "Preview:" not in list_html


@pytest.mark.django_db
def test_preview_links_stay_in_preview(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        shown = _exhibitor(event, name="Shown", published=True, exhibitor_position=0)
        hidden = _exhibitor(event, name="Hidden", published=False, exhibitor_position=1)
        organizer = _organizer_with_settings_access(event)
    client = _client(organizer)

    list_html = client.get(_public_url(event, "public_list") + "?preview=1").content.decode()
    detail_html = client.get(_public_url(event, "public_detail", pk=hidden.pk) + "?preview=1").content.decode()

    assert f'href="{_public_url(event, "public_detail", pk=hidden.pk)}?preview=1"' in list_html
    assert f'href="{_public_url(event, "public_detail", pk=shown.pk)}?preview=1"' in detail_html


@pytest.mark.django_db
def test_detail_navigation_steps_through_the_filtered_list(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        north = _exhibitor(event, name="Acme North", published=True)
        other = _exhibitor(event, name="Globex", published=True)
        south = _exhibitor(event, name="Acme South", published=True)
    client = _client()

    list_html = client.get(_public_url(event, "public_list") + "?query=Acme").content.decode()
    detail_html = client.get(_public_url(event, "public_detail", pk=north.pk) + "?query=Acme").content.decode()

    assert f'href="{_public_url(event, "public_detail", pk=north.pk)}?query=Acme"' in list_html
    assert f'href="{_public_url(event, "public_detail", pk=south.pk)}?query=Acme"' in detail_html
    assert _public_url(event, "public_detail", pk=other.pk) not in detail_html


@pytest.mark.django_db
def test_detail_page_ignores_a_filter_it_no_longer_matches(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        north = _exhibitor(event, name="Acme North", published=True)
        other = _exhibitor(event, name="Globex", published=True)
        _exhibitor(event, name="Acme South", published=True)

    response = _client().get(_public_url(event, "public_detail", pk=other.pk) + "?query=Acme")

    assert response.status_code == 200
    assert f'href="{_public_url(event, "public_detail", pk=north.pk)}"' in response.content.decode()


def _organization_list(client, event, organization_type, settings):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    url = reverse(
        f"plugins:exhibition:{organization_type}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug},
    )
    return client.get(url)


@pytest.mark.django_db
def test_exhibitor_list_offers_the_public_preview(event, settings):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        _exhibitor(event, name="Hidden", published=False)
        organizer = _organizer_with_settings_access(event)

    response = _organization_list(_client(organizer), event, "exhibitors", settings)
    html = response.content.decode()

    assert response.status_code == 200
    assert "Preview public page" in html
    assert f'href="{_public_url(event, "public_list")}?preview=1"' in html


@pytest.mark.django_db
def test_sponsor_list_has_no_public_preview(event, settings):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        _exhibitor(event, name="Sponsor", published=False, is_exhibitor=False, is_sponsor=True)
        organizer = _organizer_with_settings_access(event)

    response = _organization_list(_client(organizer), event, "sponsors", settings)

    assert response.status_code == 200
    assert "Preview public page" not in response.content.decode()


@pytest.mark.django_db
def test_searching_the_preview_stays_in_the_preview(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        hidden = _exhibitor(event, name="Hidden", published=False)
        organizer = _organizer_with_settings_access(event)
    client = _client(organizer)
    list_url = _public_url(event, "public_list")

    preview_html = client.get(list_url + "?preview=1").content.decode()
    results_html = client.get(list_url + "?preview=1&query=Hidden").content.decode()

    assert '<input type="hidden" name="preview" value="1">' in preview_html
    assert f'href="{_public_url(event, "public_detail", pk=hidden.pk)}?preview=1&amp;query=Hidden"' in results_html


@pytest.mark.django_db
def test_clearing_the_preview_returns_to_the_unfiltered_preview(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        organizer = _organizer_with_settings_access(event)
    list_url = _public_url(event, "public_list")

    response = _client(organizer).get(list_url + "?preview=1&query=Hidden&clear=1")

    assert response.status_code == 302
    assert response.url == list_url + "?preview=1"


@pytest.mark.django_db
def test_visitors_search_and_clear_without_a_preview(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
    client = _client()
    list_url = _public_url(event, "public_list")

    html = client.get(list_url + "?preview=1").content.decode()
    cleared = client.get(list_url + "?preview=1&query=Acme&clear=1")

    assert 'name="preview"' not in html
    assert cleared.status_code == 302
    assert cleared.url == list_url
