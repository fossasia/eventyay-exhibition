import base64
import json
import re
from types import SimpleNamespace

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models import Question
from rest_framework import serializers

from exhibition.api import ExhibitorInfoSerializer, LeadCreateView
from exhibition.forms import ExhibitionProposalForm, ExhibitorInfoForm, SponsorGroupForm
from exhibition.models import (
    PROPOSAL_DEFAULT_FIELD_KEYS,
    ExhibitorInfo,
    ExhibitorSettings,
    SponsorGroup,
    get_next_sponsor_group_level,
)
from exhibition.views import (
    CallTextPreviewView,
    ExhibitionDefaultFieldEditView,
    ExhibitionDefaultFieldResetView,
    ExhibitionQuestionListView,
    ExhibitorListView,
    SettingsView,
    SponsorGroupReorderView,
)


def make_exhibitor_settings(event, **kwargs):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        **kwargs,
    )


def no_optional_profile_fields():
    """Deactivate every default field, leaving only the locked name, for tests that ignore profile fields."""
    return {key: {"active": False, "required": False} for key in PROPOSAL_DEFAULT_FIELD_KEYS}


_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _locked_image_uploads():
    """Logo and header image stay locked-active, so any valid form post must include them."""
    return {
        "logo": SimpleUploadedFile("logo.png", _PNG_BYTES, content_type="image/png"),
        "header_image": SimpleUploadedFile("header.png", _PNG_BYTES, content_type="image/png"),
    }


@pytest.mark.django_db
def test_create_exhibitor_info(event):
    # CREATE: Simulate an image upload and create an exhibitor
    logo = SimpleUploadedFile("test_logo.jpg", b"file_content", content_type="image/jpeg")

    exhibitor = ExhibitorInfo.objects.create(
        event=event,
        name="Test Exhibitor",
        description="This is a test exhibitor",
        url="http://testexhibitor.com",
        email="test@example.com",
        logo=logo,
        lead_scanning_enabled=True,
    )

    # Verify the exhibitor was created and the fields are correct
    assert exhibitor.name == "Test Exhibitor"
    assert exhibitor.description == "This is a test exhibitor"
    assert exhibitor.url == "http://testexhibitor.com"
    assert exhibitor.email == "test@example.com"
    assert re.fullmatch(
        r"exhibitors/logos/Test Exhibitor/test_logo(?:_[A-Za-z0-9]{7})?\.jpg",
        exhibitor.logo.name,
    )
    assert exhibitor.lead_scanning_enabled is True


@pytest.mark.django_db
def test_read_exhibitor_info(event):
    # CREATE an exhibitor first to test reading
    logo = SimpleUploadedFile("test_logo.jpg", b"file_content", content_type="image/jpeg")
    exhibitor = ExhibitorInfo.objects.create(
        event=event,
        name="Test Exhibitor",
        description="This is a test exhibitor",
        url="http://testexhibitor.com",
        email="test@example.com",
        logo=logo,
        lead_scanning_enabled=True,
    )

    # READ: Fetch the exhibitor from the database and verify fields
    exhibitor_from_db = ExhibitorInfo.objects.get(id=exhibitor.id)
    assert exhibitor_from_db.name == "Test Exhibitor"
    assert exhibitor_from_db.description == "This is a test exhibitor"
    assert exhibitor_from_db.url == "http://testexhibitor.com"
    assert exhibitor_from_db.email == "test@example.com"
    assert exhibitor_from_db.lead_scanning_enabled is True


@pytest.mark.django_db
def test_update_exhibitor_info(event):
    # CREATE an exhibitor first to test updating
    logo = SimpleUploadedFile("test_logo.jpg", b"file_content", content_type="image/jpeg")
    exhibitor = ExhibitorInfo.objects.create(
        event=event,
        name="Test Exhibitor",
        description="This is a test exhibitor",
        url="http://testexhibitor.com",
        email="test@example.com",
        logo=logo,
        lead_scanning_enabled=True,
    )

    # UPDATE: Modify some fields and save the changes
    exhibitor.name = "Updated Exhibitor"
    exhibitor.description = "This is an updated description"
    exhibitor.lead_scanning_enabled = False
    exhibitor.save()

    # Verify the updated fields
    updated_exhibitor = ExhibitorInfo.objects.get(id=exhibitor.id)
    assert updated_exhibitor.name == "Updated Exhibitor"
    assert updated_exhibitor.description == "This is an updated description"
    assert updated_exhibitor.lead_scanning_enabled is False


@pytest.mark.django_db
def test_delete_exhibitor_info(event):
    # CREATE an exhibitor first to test deleting
    logo = SimpleUploadedFile("test_logo.jpg", b"file_content", content_type="image/jpeg")
    exhibitor = ExhibitorInfo.objects.create(
        event=event,
        name="Test Exhibitor",
        description="This is a test exhibitor",
        url="http://testexhibitor.com",
        email="test@example.com",
        logo=logo,
        lead_scanning_enabled=True,
    )

    # DELETE: Delete the exhibitor and verify it no longer exists
    exhibitor_id = exhibitor.id
    exhibitor.delete()

    with pytest.raises(ExhibitorInfo.DoesNotExist):
        ExhibitorInfo.objects.get(id=exhibitor_id)


@pytest.mark.django_db
def test_sponsor_group_form_accepts_event_kwarg_and_preserves_existing_level(event):
    with scopes_disabled():
        form = SponsorGroupForm(event=event)
        assert form.event == event

        group = SponsorGroup.objects.create(event=event, name="Legacy Group", level=0)
        form = SponsorGroupForm(instance=group, event=event)
        form.cleaned_data = {"level": None}
        assert form.clean_level() == 0


@pytest.mark.django_db
def test_next_sponsor_group_level_uses_shared_helper(event):
    SponsorGroup.objects.create(event=event, name="Gold", level=2)
    SponsorGroup.objects.create(event=event, name="Silver", level=5)

    assert get_next_sponsor_group_level(event) == 6
    assert SponsorGroup._meta.get_field("level").default == 1


@pytest.mark.django_db
def test_exhibitor_serializer_exposes_sponsor_group_level_in_output(event):
    group = SponsorGroup.objects.create(event=event, name="Gold", level=3)
    exhibitor = ExhibitorInfo.objects.create(
        event=event,
        name="Test Exhibitor",
        is_sponsor=True,
        sponsor_group=group,
    )

    data = ExhibitorInfoSerializer(instance=exhibitor, context={"event": event}).data

    assert data["sponsor_group_name"] == group.localized_name
    assert data["sponsor_group_level"] == 3


@pytest.mark.django_db
def test_exhibitor_serializer_rejects_level_mismatch_without_mutating_group(event):
    group = SponsorGroup.objects.create(event=event, name="Gold", level=1)
    serializer = ExhibitorInfoSerializer(context={"event": event})

    with pytest.raises(serializers.ValidationError) as excinfo:
        serializer._resolve_sponsor_group("Gold", sponsor_group_level=2)

    group.refresh_from_db()
    assert str(excinfo.value.detail["sponsor_group_level"]) == "Level does not match existing sponsor group."
    assert group.level == 1


@pytest.mark.django_db
def test_sponsor_group_reorder_requires_complete_unique_group_ids(event):
    group_one = SponsorGroup.objects.create(event=event, name="Gold", level=1)
    group_two = SponsorGroup.objects.create(event=event, name="Silver", level=2)
    factory = RequestFactory()
    view = SponsorGroupReorderView()

    duplicate_request = factory.post(
        "/reorder",
        data=json.dumps({"group_ids": [group_one.pk, group_one.pk]}),
        content_type="application/json",
    )
    duplicate_request.event = event
    duplicate_response = view.post(duplicate_request)
    assert duplicate_response.status_code == 400

    subset_request = factory.post(
        "/reorder",
        data=json.dumps({"group_ids": [group_two.pk]}),
        content_type="application/json",
    )
    subset_request.event = event
    subset_response = view.post(subset_request)
    assert subset_response.status_code == 400

    valid_request = factory.post(
        "/reorder",
        data=json.dumps({"group_ids": [group_two.pk, group_one.pk]}),
        content_type="application/json",
    )
    valid_request.event = event
    valid_response = view.post(valid_request)
    assert valid_response.status_code == 200

    group_one.refresh_from_db()
    group_two.refresh_from_db()
    assert group_two.level == 1
    assert group_one.level == 2


@pytest.mark.django_db
def test_call_text_preview_renders_markdown_per_active_locale(event):
    event.settings.locales = ["en", "de"]
    factory = RequestFactory()
    view = CallTextPreviewView()

    request = factory.post(
        "/preview",
        data={
            "body_en": "# Hello",
            "body_de": "## Hallo",
        },
    )
    request.event = event
    response = view.post(request)

    assert response.status_code == 200
    previews = json.loads(response.content)["previews"]
    assert set(previews.keys()) == {"en", "de"}
    assert "<h1>Hello</h1>" in previews["en"]
    assert "<h2>Hallo</h2>" in previews["de"]


@pytest.mark.django_db
def test_call_text_preview_ignores_inactive_locales_and_blank_text(event):
    event.settings.locales = ["en"]
    factory = RequestFactory()
    view = CallTextPreviewView()

    request = factory.post(
        "/preview",
        data={
            "body_en": "",
            "body_de": "# Nope",
        },
    )
    request.event = event
    response = view.post(request)

    assert response.status_code == 200
    previews = json.loads(response.content)["previews"]
    assert set(previews.keys()) == {"en"}
    assert previews["en"] == ""


@pytest.mark.django_db
def test_save_field_order_persists_new_order(event):
    settings = make_exhibitor_settings(event)
    view = ExhibitionQuestionListView()
    view.save_field_order(settings, "social_links,logo,header_image,name")

    settings.refresh_from_db()
    assert settings.ordered_proposal_field_keys[:4] == ["social_links", "logo", "header_image", "name"]
    assert set(settings.ordered_proposal_field_keys) == set(PROPOSAL_DEFAULT_FIELD_KEYS)


@pytest.mark.django_db
def test_proposal_form_reflects_saved_field_order(event):
    settings = make_exhibitor_settings(event)
    view = ExhibitionQuestionListView()
    view.save_field_order(settings, "description,name")

    form = ExhibitionProposalForm(event=event)
    field_names = list(form.fields.keys())
    assert field_names.index("description") < field_names.index("name")


@pytest.mark.django_db
def test_proposal_items_interleaves_reordered_formset_field(event):
    settings = make_exhibitor_settings(event)
    stored = settings.proposal_field_settings
    stored["social_links"]["active"] = True
    settings.save(update_fields=["proposal_field_settings"])

    view = ExhibitionQuestionListView()
    view.save_field_order(settings, "social_links,name")

    form = ExhibitionProposalForm(event=event)
    items = form.proposal_items
    assert items[0]["kind"] == "social_links"
    assert items[1]["kind"] == "field"
    assert items[1]["key"] == "name"
    assert items[1]["field"].name == "name"


@pytest.mark.django_db
def test_required_dropdown_value_persists_as_boolean(event):
    settings = make_exhibitor_settings(event)
    view = ExhibitionQuestionListView()
    request = RequestFactory().post("/", data={"url_active": "on", "url_required": "required"})
    request.event = event
    request.session = {}
    setattr(request, "_messages", FallbackStorage(request))
    view.request = request
    view.post(request)

    settings.refresh_from_db()
    assert settings.normalized_proposal_field_settings["url"]["required"] is True


def _default_field_request(event, method="post", data=None):
    factory = RequestFactory()
    request = getattr(factory, method)("/", data=data or {})
    request.event = event
    request.session = {}
    setattr(request, "_messages", FallbackStorage(request))
    return request


@pytest.mark.django_db
def test_default_field_edit_overrides_label_and_help_text(event):
    settings = make_exhibitor_settings(event)
    view = ExhibitionDefaultFieldEditView()
    view.request = _default_field_request(event)
    view.kwargs = {"key": "name"}
    form = view.get_form_class()(
        data={"label": "University name", "help_text": "Use the official name."},
        field_setting=view.get_field_setting(),
    )
    assert form.is_valid(), form.errors
    view.form_valid(form)

    settings.refresh_from_db()
    normalized = settings.normalized_proposal_field_settings
    assert normalized["name"]["label"] == "University name"
    assert normalized["name"]["help_text"] == "Use the official name."

    proposal_form = ExhibitionProposalForm(event=event)
    assert str(proposal_form.fields["name"].label) == "University name"
    assert str(proposal_form.fields["name"].help_text) == "Use the official name."


@pytest.mark.django_db
def test_default_field_reset_restores_builtin_label(event):
    settings = make_exhibitor_settings(event)
    stored = settings.proposal_field_settings
    stored["name"]["label"] = "University name"
    settings.save(update_fields=["proposal_field_settings"])

    view = ExhibitionDefaultFieldResetView()
    view.request = _default_field_request(event)
    view.kwargs = {"key": "name"}
    view.post(view.request, key="name")

    settings.refresh_from_db()
    normalized = settings.normalized_proposal_field_settings
    assert normalized["name"]["custom_label"] is None
    assert str(normalized["name"]["label"]) == "Organization name"


@pytest.mark.django_db
def test_saving_settings_does_not_freeze_default_labels_as_overrides(event):
    settings = make_exhibitor_settings(event)
    view = ExhibitionQuestionListView()
    view.request = _default_field_request(event, data={"url_active": "on"})
    view.post(view.request)

    settings.refresh_from_db()
    assert settings.proposal_field_settings["url"]["label"] is None
    assert settings.normalized_proposal_field_settings["url"]["custom_label"] is None


@pytest.mark.django_db
def test_sponsor_form_hides_exhibitor_fields(event):
    form = ExhibitorInfoForm(event=event, partner_type="sponsor")
    assert "sponsor_group" in form.fields
    assert "is_exhibitor" in form.fields
    for name in ("booth_id", "booth_name", "lead_scanning_enabled", "is_sponsor"):
        assert name not in form.fields


@pytest.mark.django_db
def test_exhibitor_form_hides_sponsor_fields(event):
    form = ExhibitorInfoForm(event=event, partner_type="exhibitor")
    assert "booth_id" in form.fields
    assert "is_sponsor" in form.fields
    for name in ("sponsor_group", "is_exhibitor"):
        assert name not in form.fields


@pytest.mark.django_db
def test_scoped_forms_set_type_flags(event):
    make_exhibitor_settings(event, proposal_field_settings=no_optional_profile_fields())
    sponsor_form = ExhibitorInfoForm(
        data={"name_0": "Acme Sponsor"},
        files=_locked_image_uploads(),
        event=event,
        partner_type="sponsor",
    )
    assert sponsor_form.is_valid(), sponsor_form.errors
    sponsor = sponsor_form.save(commit=False)
    sponsor.event = event
    sponsor.save()
    assert sponsor.is_sponsor is True
    assert sponsor.is_exhibitor is False

    exhibitor_form = ExhibitorInfoForm(
        data={"name_0": "Acme Exhibitor"},
        files=_locked_image_uploads(),
        event=event,
        partner_type="exhibitor",
    )
    assert exhibitor_form.is_valid(), exhibitor_form.errors
    exhibitor = exhibitor_form.save(commit=False)
    exhibitor.event = event
    exhibitor.save()
    assert exhibitor.is_sponsor is False
    assert exhibitor.is_exhibitor is True


@pytest.mark.django_db
def test_partner_lists_filter_by_type_and_show_both(event):
    sponsor = ExhibitorInfo.objects.create(event=event, name="S", is_sponsor=True, is_exhibitor=False)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="E", is_sponsor=False, is_exhibitor=True)
    both = ExhibitorInfo.objects.create(event=event, name="B", is_sponsor=True, is_exhibitor=True)

    factory = RequestFactory()

    def ids_for(partner_type):
        view = ExhibitorListView(partner_type=partner_type)
        request = factory.get("/")
        request.event = event
        view.request = request
        return set(view.get_queryset().values_list("id", flat=True))

    assert ids_for("sponsor") == {sponsor.id, both.id}
    assert ids_for("exhibitor") == {exhibitor.id, both.id}


@pytest.mark.django_db
def test_new_settings_share_attendee_name_and_email_by_default(event):
    settings = make_exhibitor_settings(event)

    assert settings.is_field_allowed("attendee_name")
    assert settings.is_field_allowed("attendee_email")
    assert not settings.is_field_allowed("system_company")


@pytest.mark.django_db
def test_data_access_fields_reflect_required_ticket_configuration(event):
    settings = make_exhibitor_settings(event)
    event.settings.set("attendee_company_asked", True)
    event.settings.set("attendee_company_required", True)
    event.settings.set("attendee_job_title_asked", True)
    event.settings.set("attendee_job_title_required", False)

    with scopes_disabled():
        required_question = Question.objects.create(
            event=event, question="Job role", type="S", required=True, active=True, position=0
        )
        Question.objects.create(
            event=event, question="Dietary needs", type="S", required=False, active=True, position=1
        )

        view = SettingsView()
        view.request = RequestFactory().get("/")
        view.request.event = event
        values = [field["value"] for field in view.get_data_access_fields(settings)]

    assert values[:2] == ["attendee_name", "attendee_email"]
    assert "system_company" in values
    assert "system_job_title" not in values
    assert f"question_{required_question.pk}" in values
    assert len(values) == 4


@pytest.mark.django_db
def test_lead_data_only_includes_allowed_fields(event):
    settings = make_exhibitor_settings(event)
    settings.allowed_fields = ["attendee_name", "system_company"]
    settings.save()

    order_position = SimpleNamespace(
        attendee_name="Alice",
        attendee_email="alice@example.com",
        company="Acme",
        job_title="Engineer",
        street="Main St",
        zipcode="12345",
        city="Springfield",
        country="US",
        answers=SimpleNamespace(all=lambda: []),
        order=SimpleNamespace(event=event),
    )

    with scopes_disabled():
        data = LeadCreateView().get_allowed_attendee_data(order_position, settings, None)

    assert data["name"] == "Alice"
    assert data["company"] == "Acme"
    assert "email" not in data
    assert "job_title" not in data
    assert "address" not in data
