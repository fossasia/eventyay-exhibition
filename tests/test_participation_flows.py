import pytest
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models import Team, User

from exhibition.forms import PublicParticipationForm
from exhibition.models import ExhibitionProduct, ExhibitorSettings
from exhibition.signals import exhibition_presale_nav_tab


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _setup_event(event, call_enabled=False, shop_enabled=False):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        settings, _ = ExhibitorSettings.objects.get_or_create(event=event)
        settings.call_enabled = call_enabled
        settings.shop_enabled = shop_enabled
        settings.save()
        return settings


def _login_organizer(client, event, settings):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    user = User.objects.create_superuser("organizer@dummy.dummy", "dummy")
    team = Team.objects.create(
        organizer=event.organizer,
        all_events=True,
        can_change_event_settings=True,
    )
    team.members.add(user)
    client.force_login(user)
    return user


@pytest.mark.django_db
class TestParticipationFlowHub:
    """Tests the 4 flag combinations on /exhibition/join/."""

    def test_call_enabled_only_redirects_to_call(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=False)
        client = Client()

        response = client.get(_url(event, "public_join"))
        assert response.status_code == 302
        assert response.url == _url(event, "public_call")

        # Test alias 'join' URL name as well
        response_alias = client.get(_url(event, "join"))
        assert response_alias.status_code == 302
        assert response_alias.url == _url(event, "public_call")

    def test_shop_enabled_only_redirects_to_packages(self, event):
        _setup_event(event, call_enabled=False, shop_enabled=True)
        client = Client()

        response = client.get(_url(event, "public_join"))
        assert response.status_code == 302
        assert response.url == _url(event, "shop")

    def test_both_enabled_renders_hub_page(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=True)
        client = Client()

        response = client.get(_url(event, "public_join"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Join the Exhibition" in content
        assert "Community & open source" in content
        assert "Companies & sponsors" in content
        assert _url(event, "public_call") in content
        assert _url(event, "shop") in content

    def test_neither_enabled_returns_404(self, event):
        _setup_event(event, call_enabled=False, shop_enabled=False)
        client = Client()

        response = client.get(_url(event, "public_join"))
        assert response.status_code == 404


@pytest.mark.django_db
class TestGuardedUrls:
    """Tests view-level enforcement on /exhibition/call/ and /exhibition/packages/."""

    def test_call_page_accessible_when_call_enabled(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=False)
        client = Client()

        response = client.get(_url(event, "public_call"))
        assert response.status_code == 200

    def test_call_page_returns_404_when_call_disabled(self, event):
        _setup_event(event, call_enabled=False, shop_enabled=True)
        client = Client()

        response = client.get(_url(event, "public_call"))
        assert response.status_code == 404

    def test_shop_page_accessible_when_shop_enabled_and_call_disabled(self, event):
        _setup_event(event, call_enabled=False, shop_enabled=True)
        with scopes_disabled():
            ExhibitionProduct.objects.create(event=event, name={"en": "Booth Package"}, price=100)
        client = Client()

        response = client.get(_url(event, "shop"))
        assert response.status_code == 200
        assert "Booth Package" in response.content.decode()

    def test_shop_page_redirects_to_call_anchor_when_both_enabled(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=True)
        client = Client()

        response = client.get(_url(event, "shop"))
        assert response.status_code == 302
        assert response.url == _url(event, "public_call") + "#exhibition-packages"

    def test_shop_page_returns_404_when_shop_disabled(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=False)
        client = Client()

        response = client.get(_url(event, "shop"))
        assert response.status_code == 404

    def test_shop_checkout_returns_404_when_shop_disabled(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=False)
        client = Client()

        response = client.get(_url(event, "shop.checkout.details"))
        assert response.status_code == 404


@pytest.mark.django_db
class TestPresaleNavTabSignal:
    """Tests the presale header navigation tab pointing to the right flow."""

    def test_nav_tab_points_to_hub_when_both_enabled(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=True)
        with scopes_disabled():
            ExhibitionProduct.objects.create(event=event, name={"en": "Booth Package"}, price=100)

        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/join/" in str(html)

    def test_nav_tab_points_to_call_when_only_call_enabled(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=False)

        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/call/" in str(html)
        assert "/exhibition/join/" not in str(html)

    def test_nav_tab_points_to_packages_when_only_shop_enabled(self, event):
        _setup_event(event, call_enabled=False, shop_enabled=True)
        with scopes_disabled():
            ExhibitionProduct.objects.create(event=event, name={"en": "Booth Package"}, price=100)

        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/packages/" in str(html)
        assert "/exhibition/call/" not in str(html)
        assert "/exhibition/join/" not in str(html)

    def test_nav_tab_empty_when_neither_enabled(self, event):
        _setup_event(event, call_enabled=False, shop_enabled=False)

        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert str(html) == ""

    def test_nav_tab_active_states(self, event):
        _setup_event(event, call_enabled=True, shop_enabled=True)
        with scopes_disabled():
            ExhibitionProduct.objects.create(event=event, name={"en": "Booth Package"}, price=100)
            from exhibition.models import ExhibitorInfo

            ExhibitorInfo.objects.create(
                event=event,
                name="Test Exhibitor",
                is_exhibitor=True,
                active=True,
                published=True,
                logo="test_logo.png",
                banner="test_banner.png",
            )

        # When on /exhibition/join/
        join_path = f"/{event.organizer.slug}/{event.slug}/exhibition/join/"
        html_join = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get(join_path))
        # Call for Exhibitors (hub) tab must be active, Exhibition directory tab must NOT be active
        assert f'href="/{event.organizer.slug}/{event.slug}/exhibition/" class="header-tab "' in str(html_join)
        assert f'href="/{event.organizer.slug}/{event.slug}/exhibition/join/" class="header-tab active"' in str(
            html_join
        )

        # When on /exhibition/ (directory)
        dir_path = f"/{event.organizer.slug}/{event.slug}/exhibition/"
        html_dir = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get(dir_path))
        assert f'href="/{event.organizer.slug}/{event.slug}/exhibition/" class="header-tab active"' in str(html_dir)
        assert f'href="/{event.organizer.slug}/{event.slug}/exhibition/join/" class="header-tab "' in str(html_dir)


@pytest.mark.django_db
class TestOrganizerSettingsParticipation:
    """Tests organizer settings for Public participation."""

    def test_public_participation_form_validation(self, event):
        settings = _setup_event(event)
        form = PublicParticipationForm(
            data={"call_enabled": True, "shop_enabled": False},
            instance=settings,
        )
        assert form.is_valid()
        saved = form.save()
        assert saved.call_enabled is True
        assert saved.shop_enabled is False

    def test_organizer_settings_save_both_toggles(self, event, settings):
        _setup_event(event, call_enabled=False, shop_enabled=False)
        client = Client()
        _login_organizer(client, event, settings)

        url = reverse(
            "plugins:exhibition:settings",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        )
        response = client.post(
            url,
            {
                "action": "save_exhibitor_settings",
                "call_enabled": "on",
                "shop_enabled": "on",
                "device_default_count": 1,
            },
        )
        assert response.status_code == 302

        with scopes_disabled():
            saved_settings = ExhibitorSettings.objects.get(event=event)
            assert saved_settings.call_enabled is True
            assert saved_settings.shop_enabled is True

    def test_saving_with_both_disabled_shows_warning(self, event, settings):
        _setup_event(event, call_enabled=True, shop_enabled=True)
        client = Client()
        _login_organizer(client, event, settings)

        url = reverse(
            "plugins:exhibition:settings",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        )
        response = client.post(
            url,
            {
                "action": "save_exhibitor_settings",
                # Both checkboxes omitted -> both False
                "device_default_count": 1,
            },
        )
        assert response.status_code == 302

        with scopes_disabled():
            saved_settings = ExhibitorSettings.objects.get(event=event)
            assert saved_settings.call_enabled is False
            assert saved_settings.shop_enabled is False

        messages = [m.message for m in get_messages(response.wsgi_request)]
        assert any("disabled" in str(msg).lower() for msg in messages)
