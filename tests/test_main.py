import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from rest_framework import serializers

from exhibition.api import ExhibitorInfoSerializer
from exhibition.forms import SponsorGroupForm
from exhibition.models import ExhibitorInfo, SponsorGroup, get_next_sponsor_group_level
from exhibition.views import SponsorGroupReorderView


@pytest.mark.django_db
def test_create_exhibitor_info(event):
    # CREATE: Simulate an image upload and create an exhibitor
    logo = SimpleUploadedFile(
        "test_logo.jpg", b"file_content", content_type="image/jpeg"
    )

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
    logo = SimpleUploadedFile(
        "test_logo.jpg", b"file_content", content_type="image/jpeg"
    )
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
    logo = SimpleUploadedFile(
        "test_logo.jpg", b"file_content", content_type="image/jpeg"
    )
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
    logo = SimpleUploadedFile(
        "test_logo.jpg", b"file_content", content_type="image/jpeg"
    )
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
    assert (
        str(excinfo.value.detail["sponsor_group_level"])
        == "Level does not match existing sponsor group."
    )
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
