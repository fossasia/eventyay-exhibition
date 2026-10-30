from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, LogEntry
from eventyay.base.models.auth import User

from exhibition.models import (
    LOG_ORDER_CANCELLED,
    LOG_ORDER_PAID,
    ExhibitionOrder,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionProductPurpose,
)
from exhibition.orders import place_exhibition_order


def _product(event, name="Gold Sponsor", price="1500.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _order(event, *products, **kwargs):
    kwargs.setdefault("name", "Ada Lovelace")
    kwargs.setdefault("email", "ada@example.com")
    kwargs.setdefault("organization_name", "Acme Inc")
    kwargs.setdefault("payment_provider", "manual")
    return place_exhibition_order(event, products, **kwargs)


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _client(event, email="organizer@example.com", **permissions):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = User.objects.create_user(email=email, password="secret", fullname="Organizer", locale="en")
        team = event.organizer.teams.create(name=email, all_events=True, **permissions)
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


def _organizer_client(event):
    return _client(event, can_change_event_settings=True)


@pytest.fixture
def shop(event):
    with scopes_disabled():
        event.settings.set("payment_manual__enabled", True)
        gold = _product(event, "Gold Sponsor")
        digital = _product(
            event,
            "Digital Sponsor",
            price="500.00",
            includes_booth=False,
        )
        booth = _product(event, "Booth", price="900.00", purpose=ExhibitionProductPurpose.EXHIBITION)
    return {"gold": gold, "digital": digital, "booth": booth}


def _codes(response):
    return [order.code for order in response.context["orders"]]


# The orders list


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_orders_list_shows_organization_products_and_status(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"], shop["booth"])

    response = client.get(_url(event, "orders"))

    assert response.status_code == 200
    content = response.content.decode()
    assert order.code in content
    assert "Acme Inc" in content
    assert "Gold Sponsor" in content
    assert "Booth" in content
    assert _url(event, "orders.detail", code=order.code) in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_orders_list_only_shows_the_events_own_orders(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        own = _order(event, shop["gold"])
        other_event = Event.objects.create(
            organizer=event.organizer, name="Other Event", slug="other-event", live=True, date_from=now()
        )
        other_event.settings.set("payment_manual__enabled", True)
        other = _order(other_event, _product(other_event))

    codes = _codes(client.get(_url(event, "orders")))

    assert own.code in codes
    assert other.code not in codes


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_orders_can_be_searched_by_code_and_organization(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        acme = _order(event, shop["gold"])
        globex = _order(event, shop["gold"], organization_name="Globex")

    assert _codes(client.get(_url(event, "orders"), {"query": "globex"})) == [globex.code]
    assert _codes(client.get(_url(event, "orders"), {"query": acme.code.lower()})) == [acme.code]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_orders_can_be_filtered_by_status(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        pending = _order(event, shop["gold"])
        paid = _order(event, shop["gold"])
        paid.mark_paid()

    assert _codes(client.get(_url(event, "orders"), {"status": "paid"})) == [paid.code]
    assert _codes(client.get(_url(event, "orders"), {"status": "pending"})) == [pending.code]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_orders_can_be_filtered_by_product_purpose_and_booth(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        gold = _order(event, shop["gold"])
        digital = _order(event, shop["digital"])
        booth = _order(event, shop["booth"])

    by_product = _codes(client.get(_url(event, "orders"), {"product": shop["digital"].pk}))
    by_purpose = _codes(client.get(_url(event, "orders"), {"purpose": "exhibition"}))
    with_booth = _codes(client.get(_url(event, "orders"), {"includes_booth": "1"}))
    without_booth = _codes(client.get(_url(event, "orders"), {"includes_booth": "0"}))

    assert by_product == [digital.code]
    assert by_purpose == [booth.code]
    assert sorted(with_booth) == sorted([gold.code, booth.code])
    assert without_booth == [digital.code]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_order_with_several_matching_products_is_listed_once(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"], shop["booth"])

    assert _codes(client.get(_url(event, "orders"), {"includes_booth": "1"})) == [order.code]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_filters_search_and_pagination_work_together(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        for _ in range(3):
            _order(event, shop["booth"], organization_name="Booth Co")
        _order(event, shop["digital"], organization_name="Booth Co")

    params = {"query": "booth co", "includes_booth": "1", "page_size": 2}
    first = client.get(_url(event, "orders"), params)
    second = client.get(_url(event, "orders"), {**params, "page": 2})

    assert len(_codes(first)) == 2
    assert len(_codes(second)) == 1
    assert first.context["paginator"].count == 3


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_orders_list_query_count_does_not_grow_with_orders(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        _order(event, shop["gold"], shop["booth"])
    client.get(_url(event, "orders"))

    with CaptureQueriesContext(connection) as single:
        client.get(_url(event, "orders"))
    with scopes_disabled():
        for _ in range(5):
            _order(event, shop["gold"], shop["booth"], shop["digital"])
    with CaptureQueriesContext(connection) as many:
        client.get(_url(event, "orders"))

    assert len(many.captured_queries) == len(single.captured_queries)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_teams_without_order_access_cannot_see_orders(event, shop):
    client = _client(event, email="voucher@example.com", can_change_vouchers=True)
    with scopes_disabled():
        order = _order(event, shop["gold"])

    assert client.get(_url(event, "orders")).status_code == 403
    assert client.get(_url(event, "orders.detail", code=order.code)).status_code == 403


# The order detail page


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_detail_shows_positions_total_and_payment(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"], shop["booth"], phone="+44 20 7946 0000")

    response = client.get(_url(event, "orders.detail", code=order.code))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Gold Sponsor" in content
    assert "Booth" in content
    assert "2,400.00" in content
    assert "+44 20 7946 0000" in content
    assert response.context["payment_method_name"] != "manual"
    assert _url(event, "orders.mark_paid", code=order.code) in content
    assert _url(event, "orders.cancel", code=order.code) in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_detail_of_another_event_is_not_found(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        other_event = Event.objects.create(
            organizer=event.organizer, name="Other Event", slug="other-event", live=True, date_from=now()
        )
        other_event.settings.set("payment_manual__enabled", True)
        other = _order(other_event, _product(other_event))

    assert client.get(_url(event, "orders.detail", code=other.code)).status_code == 404


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_view_only_teams_see_the_order_without_actions(event, shop):
    client = _client(event, email="viewer@example.com", can_view_orders=True)
    with scopes_disabled():
        order = _order(event, shop["gold"])

    response = client.get(_url(event, "orders.detail", code=order.code))

    assert response.status_code == 200
    assert _url(event, "orders.mark_paid", code=order.code) not in response.content.decode()
    assert client.post(_url(event, "orders.mark_paid", code=order.code)).status_code == 403
    assert client.post(_url(event, "orders.cancel", code=order.code)).status_code == 403
    with scopes_disabled():
        assert ExhibitionOrder.objects.get(pk=order.pk).status == ExhibitionOrderStatus.PENDING


# Marking paid and cancelling


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_marks_an_order_as_paid_with_a_reference(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"])

    assert client.get(_url(event, "orders.mark_paid", code=order.code)).status_code == 200
    response = client.post(_url(event, "orders.mark_paid", code=order.code), {"payment_reference": "TRX-42"})

    assert response.status_code == 302
    with scopes_disabled():
        order.refresh_from_db()
        assert order.status == ExhibitionOrderStatus.PAID
        assert order.payment_reference == "TRX-42"
        assert order.payment_provider == "manual"
        assert LogEntry.objects.filter(action_type=LOG_ORDER_PAID, object_id=order.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_cancelled_order_cannot_be_marked_as_paid(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"])
        order.cancel()

    assert client.get(_url(event, "orders.mark_paid", code=order.code)).status_code == 302
    client.post(_url(event, "orders.mark_paid", code=order.code))

    with scopes_disabled():
        assert ExhibitionOrder.objects.get(pk=order.pk).status == ExhibitionOrderStatus.CANCELLED


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_cancels_an_order(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"])

    assert client.get(_url(event, "orders.cancel", code=order.code)).status_code == 200
    response = client.post(_url(event, "orders.cancel", code=order.code))

    assert response.status_code == 302
    with scopes_disabled():
        assert ExhibitionOrder.objects.get(pk=order.pk).status == ExhibitionOrderStatus.CANCELLED
        assert LogEntry.objects.filter(action_type=LOG_ORDER_CANCELLED, object_id=order.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_cancelled_order_shows_no_actions(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        order = _order(event, shop["gold"])
        order.cancel()

    content = client.get(_url(event, "orders.detail", code=order.code)).content.decode()

    assert _url(event, "orders.mark_paid", code=order.code) not in content
    assert _url(event, "orders.cancel", code=order.code) not in content
