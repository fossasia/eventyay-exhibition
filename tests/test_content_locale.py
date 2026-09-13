import pytest
from django_scopes import scopes_disabled
from eventyay.base.models import Event
from eventyay.base.models.auth import User
from i18nfield.strings import LazyI18nString

from exhibition.forms import ExhibitionRequestForm
from exhibition.models import ExhibitionRequest, ExhibitorInfo
from exhibition.utils import sync_exhibitor_from_request


def _multilingual(event):
    event.content_locale_array = "en,de"
    event.save(update_fields=["content_locale_array"])
    return Event.objects.get(pk=event.pk)


def _request(event, **kwargs):
    user = User.objects.create_user(email="submitter@example.com", password="pw")
    return ExhibitionRequest.objects.create(event=event, user=user, **kwargs)


@pytest.mark.django_db
def test_language_selector_hidden_for_single_locale_event(event):
    with scopes_disabled():
        form = ExhibitionRequestForm(event=event)
        assert "content_locale" not in form.fields


@pytest.mark.django_db
def test_language_selector_lists_event_content_locales(event):
    with scopes_disabled():
        form = ExhibitionRequestForm(event=_multilingual(event))
        assert "content_locale" in form.fields
        assert {code for code, _label in form.fields["content_locale"].choices} == {"en", "de"}


@pytest.mark.django_db
def test_localized_fields_render_as_single_inputs(event):
    with scopes_disabled():
        form = ExhibitionRequestForm(event=_multilingual(event))
        for field_name in ("name", "description"):
            assert not hasattr(form.fields[field_name], "one_required")


@pytest.mark.django_db
def test_edit_shows_value_of_stored_content_locale(event):
    with scopes_disabled():
        exhibition_request = _request(
            event,
            name=LazyI18nString({"en": "Acme", "de": "Acme DE"}),
            content_locale="de",
        )
        form = ExhibitionRequestForm(instance=exhibition_request, event=_multilingual(event))
        assert form.initial["name"] == "Acme DE"
        assert form.initial["content_locale"] == "de"


@pytest.mark.django_db
def test_save_updates_chosen_locale_and_keeps_other_locales(event):
    with scopes_disabled():
        exhibition_request = _request(
            event,
            name=LazyI18nString({"en": "Acme", "de": "Acme DE"}),
            content_locale="en",
        )
        form = ExhibitionRequestForm(
            data={"name": "Acme Ltd", "content_locale": "en"},
            instance=exhibition_request,
            event=_multilingual(event),
        )
        assert form.is_valid(), form.errors
        form.save()

        exhibition_request.refresh_from_db()
        assert exhibition_request.name.data == {"en": "Acme Ltd", "de": "Acme DE"}
        assert exhibition_request.content_locale == "en"


@pytest.mark.django_db
def test_save_records_the_selected_language(event):
    with scopes_disabled():
        exhibition_request = _request(event, name=LazyI18nString({"en": "Acme"}), content_locale="en")
        form = ExhibitionRequestForm(
            data={"name": "Acme GmbH", "content_locale": "de"},
            instance=exhibition_request,
            event=_multilingual(event),
        )
        assert form.is_valid(), form.errors
        form.save()

        exhibition_request.refresh_from_db()
        assert exhibition_request.content_locale == "de"
        assert exhibition_request.name.data == {"en": "Acme", "de": "Acme GmbH"}


@pytest.mark.django_db
def test_text_fields_are_ltr_for_left_to_right_locale(event):
    with scopes_disabled():
        form = ExhibitionRequestForm(event=_multilingual(event))
        assert form.fields["name"].widget.attrs["dir"] == "ltr"


@pytest.mark.django_db
def test_text_fields_flip_to_rtl_for_right_to_left_locale(event):
    with scopes_disabled():
        event.content_locale_array = "en,ur"
        event.save(update_fields=["content_locale_array"])
        rtl_event = Event.objects.get(pk=event.pk)
        exhibition_request = _request(rtl_event, name=LazyI18nString({"ur": "ایکمی"}), content_locale="ur")

        form = ExhibitionRequestForm(instance=exhibition_request, event=rtl_event)

        assert form.fields["name"].widget.attrs["dir"] == "rtl"
        assert form.fields["description"].widget.attrs["dir"] == "rtl"
        assert "ur" in form.fields["content_locale"].widget.attrs["data-rtl-locales"]


@pytest.mark.django_db
def test_language_selector_itself_keeps_default_direction(event):
    with scopes_disabled():
        event.content_locale_array = "en,ur"
        event.save(update_fields=["content_locale_array"])
        rtl_event = Event.objects.get(pk=event.pk)
        exhibition_request = _request(rtl_event, name=LazyI18nString({"ur": "ایکمی"}), content_locale="ur")

        form = ExhibitionRequestForm(instance=exhibition_request, event=rtl_event)

        assert "dir" not in form.fields["content_locale"].widget.attrs


@pytest.mark.django_db
def test_sync_keeps_organizer_authored_translations(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(
            event=event,
            name=LazyI18nString({"en": "Acme", "de": "Acme DE"}),
            description=LazyI18nString({"en": "Booth", "de": "Stand"}),
        )
        exhibition_request = _request(
            event,
            name=LazyI18nString({"en": "Acme Ltd"}),
            description=LazyI18nString({"en": "New booth"}),
            content_locale="en",
            approved_exhibitor=exhibitor,
        )

        sync_exhibitor_from_request(exhibition_request)

        exhibitor.refresh_from_db()
        assert exhibitor.name.data == {"en": "Acme Ltd", "de": "Acme DE"}
        assert exhibitor.description.data == {"en": "New booth", "de": "Stand"}
