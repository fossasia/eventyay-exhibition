from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models import Event, Order, OrderPosition, Product, Team, User
from rest_framework.test import APIRequestFactory
from django.urls import reverse

pytestmark = pytest.mark.django_db
from exhibition.api import (
    ExhibitorAuthView,
    LeadCreateView,
    LeadRetrieveView,
    LeadUpdateView,
    TagListView,
    _visible_attendee,
)
from exhibition.models import ExhibitorInfo, ExhibitorTag, Lead
from exhibition.utils import provision_exhibitor_devices


def _exhibitor(event, **flags):
    return ExhibitorInfo.objects.create(event=event, name="Acme", **flags)


def _get(key):
    return RequestFactory().get("/", HTTP_EXHIBITOR=key or "")


def _post(key, data):
    request = RequestFactory().post("/", HTTP_EXHIBITOR=key or "")
    request.data = data
    return request


def _api_get(key):
    return APIRequestFactory().get("/", HTTP_EXHIBITOR=key or "")


def _api_post(key, data):
    return APIRequestFactory().post("/", data, format="json", HTTP_EXHIBITOR=key or "")


def _dispatch(view, request, *args, **kwargs):
    return view.as_view(authentication_classes=[], permission_classes=[])(request, *args, **kwargs)


def _scan_payload():
    return {"lead": "abc", "scanned": "now", "scan_type": "qr", "device_name": "phone"}


def _event_kwargs(event):
    return {"organizer": event.organizer.slug, "event": event.slug}


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
        response = LeadRetrieveView().get(_get("nope"), **_event_kwargs(event))
        assert response.status_code == 401


@pytest.mark.django_db
def test_retrieve_forbidden_when_lead_access_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=False)
        response = LeadRetrieveView().get(_get(exhibitor.key), **_event_kwargs(event))
        assert response.status_code == 403


@pytest.mark.django_db
def test_retrieve_allowed_when_lead_access_enabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=True)
        response = LeadRetrieveView().get(_get(exhibitor.key), **_event_kwargs(event))
        assert response.status_code == 200
        assert response.data["success"] is True
        assert response.data["leads"] == []


@pytest.mark.django_db
def test_tags_forbidden_when_scanning_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        response = TagListView().get(_get(exhibitor.key), **_event_kwargs(event))
        assert response.status_code == 403


@pytest.mark.django_db
def test_tags_allowed_when_scanning_enabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True)
        response = TagListView().get(_get(exhibitor.key), **_event_kwargs(event))
        assert response.status_code == 200
        assert response.data["tags"] == []


@pytest.mark.django_db
def test_create_forbidden_when_scanning_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        response = LeadCreateView().post(_post(exhibitor.key, _scan_payload()), **_event_kwargs(event))
        assert response.status_code == 403


@pytest.mark.django_db
def test_update_forbidden_when_scanning_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        request = _post(exhibitor.key, {"note": "hi", "tags": []})
        response = LeadUpdateView().post(request, lead_id="x", **_event_kwargs(event))
        assert response.status_code == 403


@pytest.mark.django_db
def test_exhibitor_api_keys_are_scoped_to_the_requested_event(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True, allow_lead_access=True)
        other_event = Event.objects.create(
            organizer=event.organizer,
            name="Other Event",
            slug="other-event",
            live=True,
            date_from=event.date_from,
        )
        bad_event_kwargs = {"organizer": other_event.organizer.slug, "event": other_event.slug}

        assert exhibitor.event.slug != bad_event_kwargs["event"]

        responses = [
            (
                "ExhibitorAuthView",
                _dispatch(
                    ExhibitorAuthView,
                    _api_post(None, {"key": exhibitor.key}),
                    **bad_event_kwargs,
                ),
            ),
            (
                "LeadCreateView",
                _dispatch(LeadCreateView, _api_post(exhibitor.key, _scan_payload()), **bad_event_kwargs),
            ),
            ("LeadRetrieveView", _dispatch(LeadRetrieveView, _api_get(exhibitor.key), **bad_event_kwargs)),
            ("TagListView", _dispatch(TagListView, _api_get(exhibitor.key), **bad_event_kwargs)),
            (
                "LeadUpdateView",
                _dispatch(
                    LeadUpdateView,
                    _api_post(exhibitor.key, {"note": "hi"}),
                    lead_id="abc",
                    **bad_event_kwargs,
                ),
            ),
        ]

    for view_name, response in responses:
        assert response.status_code == 403, f"{view_name} returned {response.status_code}"
        assert response.data == {"detail": "Forbidden: API key does not belong to this event."}


@pytest.mark.django_db
def test_exhibitor_api_keys_are_forbidden_when_event_parameters_are_missing(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True, allow_lead_access=True)

        responses = [
            ("ExhibitorAuthView", _dispatch(ExhibitorAuthView, _api_post(None, {"key": exhibitor.key}), None, None)),
            ("LeadCreateView", _dispatch(LeadCreateView, _api_post(exhibitor.key, _scan_payload()), None, None)),
            ("LeadRetrieveView", _dispatch(LeadRetrieveView, _api_get(exhibitor.key), None, None)),
            ("TagListView", _dispatch(TagListView, _api_get(exhibitor.key), None, None)),
            (
                "LeadUpdateView",
                _dispatch(
                    LeadUpdateView,
                    _api_post(exhibitor.key, {"note": "hi"}),
                    None,
                    None,
                    lead_id="abc",
                ),
            ),
        ]

    for view_name, response in responses:
        assert response.status_code == 403, f"{view_name} returned {response.status_code}"
        assert response.data == {"detail": "Forbidden: API key does not belong to this event."}


@pytest.mark.django_db
def test_exhibitor_auth_view_accepts_valid_key(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True)
        response = _dispatch(
            ExhibitorAuthView,
            _api_post(None, {"key": exhibitor.key}),
            **_event_kwargs(event),
        )

    assert response.status_code == 200
    assert response.data["success"] is True
    assert response.data["exhibitor_id"] == exhibitor.id


@pytest.mark.django_db
def test_lead_creation_cannot_scan_attendee_from_another_event(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True)

        other_event = Event.objects.create(
            organizer=event.organizer, name="Other Event", slug="other-event", live=True, date_from=event.date_from
        )
        product = Product.objects.create(event=other_event, name="Ticket", default_price=10, active=True)
        order = Order.objects.create(
            code="LEAD2",
            event=other_event,
            email="attendee@example.com",
            status=Order.STATUS_PAID,
            datetime=timezone.now(),
            expires=timezone.now() + timedelta(days=10),
            total=Decimal("10.00"),
            locale="en",
        )
        position = OrderPosition.objects.create(order=order, product=product, price=Decimal("10.00"))
        assert position.order.event_id != exhibitor.event_id

        payload = {
            "lead": position.pseudonymization_id,
            "scanned": timezone.now().isoformat(),
            "scan_type": "lead",
            "device_name": "Phone",
        }

        response = _dispatch(
            LeadCreateView,
            _api_post(exhibitor.key, payload),
            organizer=event.organizer.slug,
            event=event.slug,
        )

    assert response.status_code == 404
    assert response.data == {"success": False, "error": "Attendee not found"}


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


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload, expected_tags",
    [
        ({"note": "Call next week"}, ["vip"]),
        ({"note": "Call next week", "tags": None}, ["vip"]),
        ({"note": "Call next week", "tags": []}, []),
        ({"note": "Call next week", "tags": ["follow-up"]}, ["follow-up"]),
    ],
    ids=["omitted", "null", "cleared", "replaced"],
)
def test_note_update_only_changes_tags_when_supplied(client, event, payload, expected_tags):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = User.objects.create_user("organizer@example.com", "test-password")
        team = Team.objects.create(organizer=event.organizer, all_events=True, can_change_event_settings=True)
        team.members.add(user)
        client.force_login(user)
        exhibitor = _exhibitor(event, lead_scanning_enabled=True)
        lead = Lead.objects.create(
            exhibitor=exhibitor,
            exhibitor_name="Acme",
            pseudonymization_id="attendee-code",
            scanned=timezone.now(),
            scan_type="qr",
            device_name="phone",
            attendee={"name": "Jane", "note": "Old note", "tags": ["vip"]},
        )
        original_tag = ExhibitorTag.objects.create(exhibitor=exhibitor, name="vip", use_count=3)
        url = reverse(
            "plugins:exhibition:lead-update",
            kwargs={
                "organizer": event.organizer.slug,
                "event": event.slug,
                "lead_id": "attendee-code",
            },
        )
        response = client.post(
            url,
            payload,
            content_type="application/json",
            HTTP_EXHIBITOR=exhibitor.key,
        )
        lead.refresh_from_db()
        original_tag.refresh_from_db()

    assert response.status_code == 200, response.json()
    assert lead.attendee == {"name": "Jane", "note": "Call next week", "tags": expected_tags}
    assert response.json()["attendee"]["tags"] == lead.attendee["tags"]
    assert original_tag.use_count == 3
