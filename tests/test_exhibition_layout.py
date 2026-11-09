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
    request.organizer = event.organizer
    request.user = None
    request.session = {}
    request.LANGUAGE_CODE = "en"
    request._messages = FallbackStorage(request)
    view = SettingsView()
    view.request = request
    view.kwargs = {}
    return view.post(request)


def _settings_get(event, tab="exhibitors"):
    request = RequestFactory().get(f"/?tab={tab}")
    request.event = event
    request.organizer = event.organizer
    request.user = None
    request.session = {}
    request.LANGUAGE_CODE = "en"
    request._messages = FallbackStorage(request)
    view = SettingsView()
    view.request = request
    view.kwargs = {"tab": tab}
    response = view.get(request)
    response.render()
    return response


def _layout_caption_inputs(event, captions_by_locale):
    settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
    form = ExhibitionLayoutForm(instance=settings, event=event)
    locales = form.fields["layout_caption"].widget.locales
    return {f"layout_caption_{locales.index(locale)}": caption for locale, caption in captions_by_locale.items()}


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
def test_exhibitor_list_keeps_the_layout_hidden_and_the_search_in_the_toggle(event):
    with scopes_disabled():
        _event_with_layout(event)

    html = _public_list_html(event, "?query=Acme")

    assert "?query=Acme&amp;view=layout" in html
    assert 'data-exhibition-view="layout" hidden' in html
    assert 'data-exhibition-view="list" hidden' not in html
    assert 'href="?"' not in html


@pytest.mark.django_db
def test_layout_view_shows_the_layout_and_links_back(event):
    with scopes_disabled():
        settings = _event_with_layout(event)

    html = _public_list_html(event, "?view=layout")

    assert 'data-exhibition-view="layout" hidden' not in html
    assert 'data-exhibition-view="list" hidden' in html
    assert f'src="{settings.visible_layout_url}"' in html
    assert 'alt="Exhibition layout: Hall 1, ground floor"' in html
    assert "<figcaption>Hall 1, ground floor</figcaption>" in html
    assert 'rel="noopener noreferrer"' in html


@pytest.mark.django_db
def test_failed_layout_upload_keeps_the_chosen_data_access(event):
    with scopes_disabled():
        response = _settings_post(
            event,
            {
                "action": "save_exhibitor_settings",
                "tab": "exhibitors",
                "layout_image": _layout(content=b"not an image"),
                "exhibitors_access_voucher": ["attendee_email"],
            },
        )
        checked = {field["value"] for field in response.context_data["data_access_fields"] if field["checked"]}

    assert response.status_code == 200
    assert checked == {"attendee_email"}


@pytest.mark.django_db
def test_public_exhibition_page_hides_the_layout_section_without_a_layout(event):
    with scopes_disabled():
        _enable_plugin(event)
        ExhibitorSettings.objects.get_or_create(event=event)

    html = _public_list_html(event, "?view=layout")

    assert 'id="exhibition-layout-heading"' not in html
    assert "View layout" not in html
    assert 'data-exhibition-view="list" hidden' not in html


@pytest.mark.django_db
def test_layout_is_stored_under_the_event_folder(event):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get_or_create(event=event)[0]

    assert exhibition_layout_path(settings, "plan.png") == f"exhibitors/layouts/{event.pk}/plan.png"


@pytest.mark.django_db
def test_settings_exhibitors_tab_shows_language_switcher_when_multiple_locales(event):
    event.settings.locales = ["en", "de"]
    with scopes_disabled():
        response = _settings_get(event, "exhibitors")
        caption_inputs = _layout_caption_inputs(event, {"en": "", "de": ""})
    html = response.content.decode()

    assert response.status_code == 200
    assert "data-i18n-switcher" in html
    assert 'data-locale="en"' in html
    assert 'data-locale="de"' in html
    for field_name in caption_inputs:
        assert f'name="{field_name}"' in html
    assert 'name="exhibitors_access_voucher"' in html


@pytest.mark.django_db
def test_settings_exhibitors_tab_hides_language_switcher_when_single_locale(event):
    event.settings.locales = ["en"]
    with scopes_disabled():
        response = _settings_get(event, "exhibitors")
    html = response.content.decode()

    assert response.status_code == 200
    assert "data-i18n-switcher" not in html
    assert 'data-locale="en"' not in html
    assert 'name="layout_caption_0"' in html
    assert "i18n-form-single-language" in html
    assert 'name="exhibitors_access_voucher"' in html


@pytest.mark.django_db
def test_organizer_saves_layout_caption_in_multiple_languages(event):
    event.settings.locales = ["en", "de"]
    with scopes_disabled():
        caption_inputs = _layout_caption_inputs(
            event,
            {"en": "Hall 1, ground floor", "de": "Halle 1, Erdgeschoss"},
        )
        response = _settings_post(
            event,
            {
                "action": "save_exhibitor_settings",
                "tab": "exhibitors",
                "layout_image": _layout(),
                **caption_inputs,
                "exhibitors_access_voucher": ["attendee_name"],
            },
        )
        settings = ExhibitorSettings.objects.get(event=event)

    assert response.status_code == 302
    assert settings.layout_caption.data == {"en": "Hall 1, ground floor", "de": "Halle 1, Erdgeschoss"}
    assert settings.allowed_fields == ["attendee_name"]


@pytest.mark.django_db
def test_failed_layout_caption_validation_keeps_data_access_and_shows_switcher(event):
    event.settings.locales = ["en", "de"]
    with scopes_disabled():
        caption_inputs = _layout_caption_inputs(
            event,
            {"en": "Hall 1, ground floor", "de": "Halle 1, Erdgeschoss"},
        )
        response = _settings_post(
            event,
            {
                "action": "save_exhibitor_settings",
                "tab": "exhibitors",
                "layout_image": _layout(content=b"not an image"),
                **caption_inputs,
                "exhibitors_access_voucher": ["attendee_email"],
            },
        )
        checked = {field["value"] for field in response.context_data["data_access_fields"] if field["checked"]}
        response.render()
        html = response.content.decode()

    assert response.status_code == 200
    assert checked == {"attendee_email"}
    assert "data-i18n-switcher" in html
    assert 'data-locale="en"' in html
    assert 'data-locale="de"' in html
    assert "Hall 1, ground floor" in html
    assert "Halle 1, Erdgeschoss" in html
