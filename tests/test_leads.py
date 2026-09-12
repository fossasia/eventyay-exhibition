from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django_scopes import scopes_disabled

from exhibition.api import (
    LeadCreateView,
    LeadRetrieveView,
    LeadUpdateView,
    TagListView,
    _visible_attendee,
)
from exhibition.models import ExhibitorInfo
from exhibition.utils import provision_exhibitor_devices


def _exhibitor(event, **flags):
    return ExhibitorInfo.objects.create(event=event, name="Acme", **flags)


def _get(key):
    return RequestFactory().get("/", HTTP_EXHIBITOR=key or "")


def _post(key, data):
    request = RequestFactory().post("/", HTTP_EXHIBITOR=key or "")
    request.data = data
    return request


def _scan_payload():
    return {"lead": "abc", "scanned": "now", "scan_type": "qr", "device_name": "phone"}


def test_visible_attendee_returns_full_when_access_allowed():
    data = {"name": "Jane", "email": "j@x.com", "note": "hi", "tags": ["vip"]}
    assert _visible_attendee(data, SimpleNamespace(allow_lead_access=True)) == data


def test_visible_attendee_strips_pii_when_access_denied():
    data = {"name": "Jane", "email": "j@x.com", "note": "hi", "tags": ["vip"]}
    assert _visible_attendee(data, SimpleNamespace(allow_lead_access=False)) == {"note": "hi", "tags": ["vip"]}


def test_visible_attendee_handles_none_without_access():
    assert _visible_attendee(None, SimpleNamespace(allow_lead_access=False)) == {"note": "", "tags": []}


@pytest.mark.django_db
def test_retrieve_rejects_invalid_key(event):
    with scopes_disabled():
        response = LeadRetrieveView().get(_get("nope"))
        assert response.status_code == 401


@pytest.mark.django_db
def test_retrieve_forbidden_when_lead_access_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=False)
        response = LeadRetrieveView().get(_get(exhibitor.key))
        assert response.status_code == 403


@pytest.mark.django_db
def test_retrieve_allowed_when_lead_access_enabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=True)
        response = LeadRetrieveView().get(_get(exhibitor.key))
        assert response.status_code == 200
        assert response.data["success"] is True
        assert response.data["leads"] == []


@pytest.mark.django_db
def test_tags_forbidden_when_scanning_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        response = TagListView().get(_get(exhibitor.key), organizer="o", event="e")
        assert response.status_code == 403


@pytest.mark.django_db
def test_tags_allowed_when_scanning_enabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True)
        response = TagListView().get(_get(exhibitor.key), organizer="o", event="e")
        assert response.status_code == 200
        assert response.data["tags"] == []


@pytest.mark.django_db
def test_create_forbidden_when_scanning_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        response = LeadCreateView().post(_post(exhibitor.key, _scan_payload()))
        assert response.status_code == 403


@pytest.mark.django_db
def test_update_forbidden_when_scanning_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        request = _post(exhibitor.key, {"note": "hi", "tags": []})
        response = LeadUpdateView().post(request, organizer="o", event="e", lead_id="x")
        assert response.status_code == 403


@pytest.mark.django_db
def test_provisioned_devices_get_full_access_without_a_manual_step(event):
    """Lead scanning needs the full profile; the check-in allowlist does not cover it."""
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", lead_scanning_enabled=True)
        provision_exhibitor_devices(exhibitor, 2)
        profiles = {link.device.security_profile for link in exhibitor.devices.select_related("device")}

    assert profiles == {"full"}


@pytest.mark.django_db
def test_provisioned_devices_stay_scoped_to_their_event(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", lead_scanning_enabled=True)
        provision_exhibitor_devices(exhibitor, 1)
        device = exhibitor.devices.select_related("device").first().device

        assert device.all_events is False
        assert list(device.limit_events.all()) == [event]
