import re

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models import Team
from eventyay.base.models.auth import StaffSession, User

from exhibition.filters import ExhibitorFilterForm
from exhibition.forms import ExhibitorInfoForm
from exhibition.models import ExhibitionRequest, ExhibitionRequestState, ExhibitorInfo, SponsorGroup
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
def test_publish_all_reveals_the_confirmed_organizations(event):
    with scopes_disabled():
        first = _exhibitor(event, name="First")
        second = _exhibitor(event, name="Second")
        withdrawn = _exhibitor(event, name="Withdrawn", active=False)
        approved_later = _exhibitor(event, name="Approved later")

        confirmed = f"{first.pk},{second.pk},{withdrawn.pk}"
        view, request = _publish_view(event, {"action": "publish_all", "confirmed": "1", "organizations": confirmed})
        view.post(request)

        first.refresh_from_db()
        second.refresh_from_db()
        withdrawn.refresh_from_db()
        approved_later.refresh_from_db()
        assert first.published is True
        assert second.published is True
        assert approved_later.published is False
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
def test_old_clear_links_open_the_preview_normally(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        hidden = _exhibitor(event, name="Hidden", published=False)
        organizer = _organizer_with_settings_access(event)
    list_url = _public_url(event, "public_list")

    response = _client(organizer).get(list_url + "?preview=1&query=Hidden&clear=1")
    html = response.content.decode()

    assert response.status_code == 200
    assert '<input type="hidden" name="preview" value="1">' in html
    assert f'href="{_public_url(event, "public_detail", pk=hidden.pk)}?preview=1&amp;query=Hidden"' in html


@pytest.mark.django_db
def test_public_page_has_only_the_search_box(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        group = SponsorGroup.objects.create(event=event, name="Gold", level=1)
        grouped = _exhibitor(event, name="Grouped", published=True, is_sponsor=True, sponsor_group=group)
        other = _exhibitor(event, name="Other", published=True)
    client = _client()
    list_url = _public_url(event, "public_list")

    html = client.get(list_url + "?preview=1").content.decode()
    old_link = client.get(list_url + f"?sponsor_group={group.pk}&clear=1")
    old_html = old_link.content.decode()

    assert 'name="query"' in html
    assert 'name="sponsor_group"' not in html
    assert 'name="clear"' not in html
    assert "exhibition-public-filter-submit" not in html
    assert 'name="preview"' not in html
    assert old_link.status_code == 200
    assert _public_url(event, "public_detail", pk=grouped.pk) in old_html
    assert _public_url(event, "public_detail", pk=other.pk) in old_html


@pytest.mark.django_db
def test_search_results_come_back_in_the_block_the_page_swaps_in(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        match = _exhibitor(event, name="Acme", published=True)
        other = _exhibitor(event, name="Globex", published=True)
    list_url = _public_url(event, "public_list")

    html = _client().get(list_url + "?query=Acme").content.decode()
    results = html[html.index("<div data-exhibition-results>") :]

    assert "public-exhibitor-search.js" in html
    assert _public_url(event, "public_detail", pk=match.pk) in results
    assert _public_url(event, "public_detail", pk=other.pk) not in results


def _messages(request):
    return [str(message) for message in request._messages]


@pytest.mark.django_db
def test_publishing_skips_exhibitors_missing_a_logo_or_banner(event):
    with scopes_disabled():
        complete = _exhibitor(event, name="Complete")
        no_banner = _exhibitor(event, name="No banner", banner="")

        view, request = _publish_view(event, {"action": "publish", "selected": [str(complete.pk), str(no_banner.pk)]})
        view.post(request)

        complete.refresh_from_db()
        no_banner.refresh_from_db()
        assert complete.published is True
        assert no_banner.published is False
        assert "1 exhibitor could not be published because it is missing a logo or banner." in _messages(request)


@pytest.mark.django_db
def test_publish_all_leaves_out_exhibitors_missing_images(event, settings):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        complete = _exhibitor(event, name="Complete")
        no_logo = _exhibitor(event, name="No logo", logo="")
        client = _client(_organizer_with_settings_access(event))
    url = reverse(
        "plugins:exhibition:exhibitors.publish",
        kwargs={"organizer": event.organizer.slug, "event": event.slug},
    )

    preview = client.post(url, {"action": "publish_all"})
    assert preview.context["waiting"] == [complete]
    assert preview.context["missing_images"] == [no_logo]
    listed = re.search(r'name="organizations" value="([^"]*)"', preview.content.decode())[1]
    assert listed == str(complete.pk)

    with scopes_disabled():
        no_logo.logo = _IMAGES["logo"]
        no_logo.save(update_fields=["logo"])
    client.post(url, {"action": "publish_all", "confirmed": "1", "organizations": listed})

    with scopes_disabled():
        complete.refresh_from_db()
        no_logo.refresh_from_db()
    assert complete.published is True
    assert no_logo.published is False


@pytest.mark.django_db
def test_publish_all_rechecks_images_and_ignores_malformed_ids(event):
    with scopes_disabled():
        complete = _exhibitor(event, name="Complete")
        no_logo = _exhibitor(event, name="No logo", logo="")

        confirmed = f"{complete.pk},{no_logo.pk},\u00b2,{2**64},{'9' * 5000},abc"
        view, request = _publish_view(event, {"action": "publish_all", "confirmed": "1", "organizations": confirmed})
        view.post(request)

        complete.refresh_from_db()
        no_logo.refresh_from_db()
        assert complete.published is True
        assert no_logo.published is False


@pytest.mark.django_db
@pytest.mark.parametrize(("is_exhibitor", "published"), [(False, True), (True, False)])
def test_only_sponsor_only_organizations_publish_without_a_banner(event, is_exhibitor, published):
    with scopes_disabled():
        group = SponsorGroup.objects.create(event=event, name="Gold", show_on_front_page=True)
        sponsor = _exhibitor(
            event, name="Sponsor", is_exhibitor=is_exhibitor, is_sponsor=True, sponsor_group=group, banner=""
        )

        view, request = _publish_view(event, {"action": "publish", "selected": [str(sponsor.pk)]}, "sponsor")
        view.post(request)

        sponsor.refresh_from_db()
        assert sponsor.published is published


def _edit_form(event, exhibitor, **data):
    return ExhibitorInfoForm(
        data={"name_0": exhibitor.name, "exhibitor_group": "paid", **data},
        instance=exhibitor,
        event=event,
        organization_type="exhibitor",
    )


@pytest.mark.django_db
def test_form_cannot_publish_an_exhibitor_without_images(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", exhibitor_group="paid")
        form = _edit_form(event, exhibitor, published="on")

        assert not form.is_valid()
        assert {"logo", "banner"} <= set(form.errors)
        assert "published" not in form.errors


@pytest.mark.django_db
def test_form_blocks_publishing_without_images_when_they_are_optional(event, monkeypatch):
    monkeypatch.setattr(ExhibitorInfoForm, "profile_key_is_required", lambda form, key: False)
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", exhibitor_group="paid")

        assert "published" in _edit_form(event, exhibitor, published="on").errors
        assert _edit_form(event, exhibitor).is_valid()


@pytest.mark.django_db
def test_form_requires_a_banner_for_an_exhibitor_that_also_sponsors(event, monkeypatch):
    monkeypatch.setattr(ExhibitorInfoForm, "profile_key_is_required", lambda form, key: False)
    with scopes_disabled():
        group = SponsorGroup.objects.create(event=event, name="Gold", show_on_front_page=True)
        exhibitor = _exhibitor(event, exhibitor_group="paid", is_sponsor=True, sponsor_group=group, banner="")
        form = ExhibitorInfoForm(
            data={
                "name_0": "Acme",
                "exhibitor_group": "paid",
                "is_exhibitor": "on",
                "published": "on",
                "sponsor_group": group.pk,
            },
            instance=exhibitor,
            event=event,
            organization_type="sponsor",
        )

        assert "published" in form.errors


@pytest.mark.django_db
def test_a_false_clear_flag_keeps_the_stored_banner(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, exhibitor_group="paid")
        form = _edit_form(event, exhibitor, **{"banner-clear": "false"})
        assert form.is_valid(), form.errors

        form.save()
        exhibitor.refresh_from_db()
        assert exhibitor.banner


@pytest.mark.django_db
def test_a_clear_flag_for_a_field_not_on_the_form_is_ignored(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, exhibitor_group="paid", slides="exhibitors/slides/Acme/deck.pdf")
        form = _edit_form(event, exhibitor, **{"slides-clear": "on"})
        assert "slides" not in form.fields
        assert form.is_valid(), form.errors

        form.save()
        exhibitor.refresh_from_db()
        assert exhibitor.slides


@pytest.mark.django_db
def test_banner_help_text_keeps_the_display_guidance(event):
    with scopes_disabled():
        help_text = str(ExhibitorInfoForm(event=event, organization_type="exhibitor").fields["banner"].help_text)

    assert "List cards crop it to fill." in help_text
    assert "1200 × 400" in help_text


@pytest.mark.django_db
def test_organizer_is_warned_about_exhibitors_missing_images(event, settings):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        exhibitor = _exhibitor(event, name="Acme", published=True, banner="")
        _exhibitor(event, name="Waiting", logo="")
        organizer = _organizer_with_settings_access(event)
    client = _client(organizer)

    list_html = _organization_list(client, event, "exhibitors", settings).content.decode()
    edit_url = reverse(
        "plugins:exhibition:edit",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "pk": exhibitor.pk},
    )
    edit_html = client.get(edit_url).content.decode()

    assert "Missing images (not visible publicly)" in list_html
    assert "label label-success" not in list_html
    assert "fa fa-exclamation-triangle text-warning" in list_html
    assert "it does not appear on the public Exhibition page" in edit_html
