import base64

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models import LogEntry

from exhibition.forms import ExhibitionLayoutForm
from exhibition.models import LOG_SETTINGS_CHANGED, ExhibitorSettings, exhibition_layout_path
from exhibition.views import SettingsView

_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _layout(name="layout.png", content=_PNG_BYTES, content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def _settings_post(event, data):
    request = RequestFactory().post("/", data=data)
    request.event = event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)
    view = SettingsView()
    view.request = request
    view.kwargs = {}
    return view.post(request)


def _public_list_html(event, query=""):
    url = reverse("plugins:exhibition:public_list", kwargs={"organizer": event.organizer.slug, "event": event.slug})
    return Client().get(url + query).content.decode()


def _enable_plugin(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])


@pytest.mark.django_db
def test_organizer_uploads_a_layout_with_a_caption(event):
    with scopes_disabled():
        response = _settings_post(
            event,
            {
                "action": "save_exhibitor_settings",
                "tab": "exhibitors",
                "layout_image": _layout(),
                "layout_caption_0": "Hall 1, ground floor",
                "exhibitors_access_voucher": ["attendee_name"],
            },
        )
        settings = ExhibitorSettings.objects.get(event=event)
        logged = LogEntry.objects.filter(action_type=LOG_SETTINGS_CHANGED).last()

    assert response.status_code == 302
    assert settings.layout_image.name == f"exhibitors/layouts/{event.pk}/layout.png"
    assert str(settings.layout_caption) == "Hall 1, ground floor"
    assert settings.allowed_fields == ["attendee_name"]
    assert "layout_image" in logged.parsed_data["changed"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "content_type"),
    [("layout.gif", "image/gif"), ("layout.pdf", "application/pdf")],
)
def test_layout_rejects_other_file_types(event, name, content_type):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
        form = ExhibitionLayoutForm(
            data={},
            files={"layout_image": _layout(name=name, content_type=content_type)},
            instance=settings,
            event=event,
        )

        assert not form.is_valid()
    assert "layout_image" in form.errors


@pytest.mark.django_db
def test_layout_rejects_a_file_that_is_not_an_image(event):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
        form = ExhibitionLayoutForm(
            data={},
            files={"layout_image": _layout(content=b"not an image")},
            instance=settings,
            event=event,
        )

        assert not form.is_valid()
    assert "layout_image" in form.errors


@pytest.mark.django_db
def test_layout_rejects_a_gif_renamed_to_png(event):
    gif_bytes = base64.b64decode(b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
        form = ExhibitionLayoutForm(
            data={},
            files={"layout_image": _layout(content=gif_bytes)},
            instance=settings,
            event=event,
        )

        assert not form.is_valid()
    assert "layout_image" in form.errors


@pytest.mark.django_db
def test_replacing_and_removing_the_layout_deletes_the_old_file(event, django_capture_on_commit_callbacks):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
        first = ExhibitionLayoutForm(data={}, files={"layout_image": _layout()}, instance=settings, event=event)
        assert first.is_valid(), first.errors
        with django_capture_on_commit_callbacks(execute=True):
            first_name = first.save().layout_image.name

        second = ExhibitionLayoutForm(
            data={}, files={"layout_image": _layout(name="hall.png")}, instance=settings, event=event
        )
        assert second.is_valid(), second.errors
        with django_capture_on_commit_callbacks(execute=True):
            second_name = second.save().layout_image.name

        assert not default_storage.exists(first_name)
        assert default_storage.exists(second_name)

        cleared = ExhibitionLayoutForm(data={"layout_image-clear": "on"}, instance=settings, event=event)
        assert cleared.is_valid(), cleared.errors
        with django_capture_on_commit_callbacks(execute=True):
            cleared.save()
        settings.refresh_from_db()

    assert not settings.layout_image
    assert not default_storage.exists(second_name)


def _event_with_layout(event):
    _enable_plugin(event)
    settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
    settings.layout_image = _layout()
    settings.layout_caption = "Hall 1, ground floor"
    settings.save()
    return settings


@pytest.mark.django_db
def test_exhibitor_list_links_to_the_layout_without_showing_it(event):
    with scopes_disabled():
        _event_with_layout(event)

    html = _public_list_html(event)

    assert 'href="?view=layout"' in html
    assert "View layout" in html
    assert 'id="exhibition-layout-heading"' not in html
    assert 'role="search"' in html


@pytest.mark.django_db
def test_layout_view_shows_only_the_layout_and_links_back(event):
    with scopes_disabled():
        settings = _event_with_layout(event)

    html = _public_list_html(event, "?view=layout")

    assert 'id="exhibition-layout-heading"' in html
    assert f'src="{settings.visible_layout_url}"' in html
    assert f'href="{settings.visible_layout_url}"' in html
    assert "<figcaption>Hall 1, ground floor</figcaption>" in html
    assert "View exhibitors" in html
    assert 'role="search"' not in html


@pytest.mark.django_db
def test_public_exhibition_page_hides_the_layout_section_without_a_layout(event):
    with scopes_disabled():
        _enable_plugin(event)
        ExhibitorSettings.objects.get_or_create(event=event)

    html = _public_list_html(event, "?view=layout")

    assert 'id="exhibition-layout-heading"' not in html
    assert "View layout" not in html
    assert 'role="search"' in html


@pytest.mark.django_db
def test_layout_is_stored_under_the_event_folder(event):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get_or_create(event=event)[0]

    assert exhibition_layout_path(settings, "plan.png") == f"exhibitors/layouts/{event.pk}/plan.png"
