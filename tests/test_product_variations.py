from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import (
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionProductVariation,
    ExhibitionQuota,
    ExhibitionQuotaExceeded,
)
from exhibition.orders import ExhibitionOrderError, place_exhibition_order
from exhibition.shop import checkout_session_key, shop_products

CUSTOMER_DATA = {
    "organization_name": "Acme Inc",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
}


def _product(event, name="Booth", price="1000.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _variation(product, value="Large", price=None, **kwargs):
    return ExhibitionProductVariation.objects.create(
        product=product,
        value={"en": value},
        price=Decimal(price) if price is not None else None,
        **kwargs,
    )


def _enable_manual_payment(event):
    event.settings.set("payment_manual__enabled", True)


def _organizer_client(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = User.objects.create_user(
            email="organizer@example.com", password="secret", fullname="Organizer", locale="en"
        )
        team = event.organizer.teams.create(name="Organizers", all_events=True, can_change_event_settings=True)
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


@pytest.mark.django_db
def test_variation_price_overrides_product_price(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event, price="1000.00")
        large = _variation(product, "Large", price="1500.00")
        _variation(product, "Small")  # uses product price

        order = place_exhibition_order(event, [product], variations=[large], payment_provider="manual", **CUSTOMER_DATA)
        position = order.positions.get()
        assert position.variation_id == large.pk
        assert position.price == Decimal("1500.00")
        assert "Large" in str(position.name)


@pytest.mark.django_db
def test_product_with_variations_requires_a_choice(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event)
        _variation(product, "Large")

        with pytest.raises(ExhibitionOrderError):
            place_exhibition_order(event, [product], payment_provider="manual", **CUSTOMER_DATA)


@pytest.mark.django_db
def test_variation_quota_sold_out_blocks_order(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event)
        large = _variation(product, "Large", price="1200.00")
        small = _variation(product, "Small", price="800.00")
        quota = ExhibitionQuota.objects.create(event=event, name="Large booths", size=1)
        quota.variations.add(large)

        place_exhibition_order(event, [product], variations=[large], payment_provider="manual", **CUSTOMER_DATA)
        assert large.remaining_capacity() == 0
        assert small.remaining_capacity() is None

        with pytest.raises(ExhibitionQuotaExceeded):
            place_exhibition_order(
                event,
                [product],
                variations=[large],
                payment_provider="manual",
                email="bob@example.com",
                name="Bob",
                organization_name="Beta",
            )


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_can_manage_variations_on_product_form(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Standard Booth")

    response = client.get(_url(event, "products.edit", pk=product.pk))
    assert response.status_code == 200
    assert b"Variations" in response.content
    assert b"variations-TOTAL_FORMS" in response.content

    data = {
        "name_0": "Standard Booth",
        "purpose": "exhibition",
        "includes_booth": "on",
        "price": "1000.00",
        "active": "on",
        "addons-TOTAL_FORMS": "0",
        "addons-INITIAL_FORMS": "0",
        "addons-MIN_NUM_FORMS": "0",
        "addons-MAX_NUM_FORMS": "1000",
        "variations-TOTAL_FORMS": "2",
        "variations-INITIAL_FORMS": "0",
        "variations-MIN_NUM_FORMS": "0",
        "variations-MAX_NUM_FORMS": "1000",
        "variations-0-value_0": "Large",
        "variations-0-price": "1500.00",
        "variations-0-active": "on",
        "variations-0-ORDER": "0",
        "variations-1-value_0": "Small",
        "variations-1-active": "on",
        "variations-1-ORDER": "1",
    }
    response = client.post(_url(event, "products.edit", pk=product.pk), data)
    assert response.status_code == 302
    with scopes_disabled():
        variations = list(product.variations.order_by("position"))
        assert [v.localized_name for v in variations] == ["Large", "Small"]
        assert variations[0].price == Decimal("1500.00")
        assert variations[1].price is None


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_shop_lists_variations_and_checkout_stores_selection(event):
    client = Client()
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        _enable_manual_payment(event)
        product = _product(event, "Booth Package")
        large = _variation(product, "Large", price="1500.00")
        packages = shop_products(event)
        assert packages[0].has_variations
        assert {v.pk for v in packages[0].shop_variations} == {large.pk}

    response = client.post(
        _url(event, "shop"),
        {"variation": str(large.pk)},
    )
    assert response.status_code == 302
    cart = client.session.get(checkout_session_key(event), {})
    assert cart["products"] == [product.pk]
    assert cart["variations"] == [large.pk]


@pytest.mark.django_db
def test_product_without_variations_still_orders_normally(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event, "Digital", price="500.00", includes_booth=False)
        order = place_exhibition_order(event, [product], payment_provider="manual", **CUSTOMER_DATA)
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.positions.get().variation_id is None
