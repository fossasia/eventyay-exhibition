from decimal import Decimal

import pytest
from django.utils import timezone
from django_scopes import scope, scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product

from exhibition.anonymizer import LeadAttendeeAnonymizer
from exhibition.models import ExhibitorInfo, Lead

ATTENDEE = {
    "name": "Jane Doe",
    "email": "jane@example.org",
    "company": "Acme",
    "job_title": "CTO",
    "address": "Main St 1, 12345, Berlin, DE",
    "note": "Wants a demo",
    "tags": ["vip"],
    "questions": [{"question": "Interests", "answer": "Open source"}],
}


def _lead(exhibitor, code, attendee=None):
    return Lead.objects.create(
        exhibitor=exhibitor,
        exhibitor_name="Acme",
        pseudonymization_id=code,
        scanned=timezone.now(),
        scan_type="qr",
        device_name="phone",
        attendee=attendee,
        booth_number="1",
        booth_name="Booth",
    )


def _order_with_position(event, code):
    product = Product.objects.create(event=event, name="Ticket", default_price=Decimal("10.00"))
    order = Order.objects.create(
        event=event,
        code=code,
        email="buyer@example.org",
        total=Decimal("10.00"),
        datetime=timezone.now(),
        expires=timezone.now(),
        status=Order.STATUS_PAID,
    )
    position = OrderPosition.objects.create(order=order, product=product, price=Decimal("10.00"))
    return order, position


@pytest.mark.django_db
def test_plugin_registers_the_lead_anonymizer(event):
    with scopes_disabled():
        assert isinstance(event.get_ticketing_anonymizers()["exhibition_leads"], LeadAttendeeAnonymizer)


@pytest.mark.django_db
def test_lead_identity_is_removed_and_exhibitor_input_is_kept(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme")
        order, position = _order_with_position(event, "AAAAA")
        by_secret = _lead(exhibitor, position.secret, dict(ATTENDEE))
        by_pseudonym = _lead(exhibitor, position.pseudonymization_id, dict(ATTENDEE))
        LeadAttendeeAnonymizer(event).anonymize_order(order)
        for lead in (by_secret, by_pseudonym):
            lead.refresh_from_db()
            assert lead.attendee == {
                "note": "Wants a demo",
                "tags": ["vip"],
                "questions": [{"question": "Interests", "answer": "Open source"}],
            }
            assert lead.pseudonymization_id and lead.scanned


@pytest.mark.django_db
def test_leads_of_other_orders_and_events_are_untouched(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme")
        order, _position = _order_with_position(event, "AAAAA")
        _other_order, other_position = _order_with_position(event, "BBBBB")
        untouched = _lead(exhibitor, other_position.secret, dict(ATTENDEE))
        no_data = _lead(exhibitor, "scanned-without-data", None)
        LeadAttendeeAnonymizer(event).anonymize_order(order)
        untouched.refresh_from_db()
        no_data.refresh_from_db()
        assert untouched.attendee == ATTENDEE
        assert no_data.attendee is None


@pytest.mark.django_db
def test_anonymizing_twice_is_safe(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme")
        order, position = _order_with_position(event, "AAAAA")
        lead = _lead(exhibitor, position.secret, dict(ATTENDEE))
        anonymizer = LeadAttendeeAnonymizer(event)
        anonymizer.anonymize_order(order)
        anonymizer.anonymize_order(order)
        lead.refresh_from_db()
        assert set(lead.attendee) == {"note", "tags", "questions"}


@pytest.mark.django_db
def test_snapshot_contains_only_this_orders_attendee_fields(event):
    with scope(organizer=event.organizer, event=event):
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme")
        order, position = _order_with_position(event, "AAAAA")
        _, other_position = _order_with_position(event, "BBBBB")
        lead = _lead(exhibitor, position.secret, dict(ATTENDEE))
        _lead(exhibitor, other_position.secret, dict(ATTENDEE))
        rows, fields = LeadAttendeeAnonymizer(event).snapshot_groups(order)["leads"]
        assert fields == ("attendee",)
        assert list(rows.values_list("pk", *fields)) == [(lead.pk, ATTENDEE)]
