from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import (
    MAX_UNITS_PER_PRODUCT,
    ExhibitionOrder,
    ExhibitionOrderError,
    ExhibitionProduct,
    ExhibitionProductAddOn,
    ExhibitionProductCategory,
    ExhibitionQuota,
)
from exhibition.orders import place_exhibition_order
from exhibition.shop import checkout_session_key

CUSTOMER_DATA = {"organization_name": "Acme Inc", "name": "Ada Lovelace", "email": "ada@example.com"}


def _product(event, name="Extra Badge", price="0.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _enable_plugin(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()


def _cart_products(client, event):
    return client.session.get(checkout_session_key(event), {}).get("products", [])


def _organizer_client(event):
    _enable_plugin(event)
    with scopes_disabled():
        user = User.objects.create_user(
            email="organizer@example.com", password="secret", fullname="Organizer", locale="en"
        )
        team = event.organizer.teams.create(
            name="Organizers",
            all_events=True,
            can_change_event_settings=True,
            can_view_orders=True,
            can_change_orders=True,
        )
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


def _product_form_data(**overrides):
    data = {"name_0": "Extra Badge", "purpose": "sponsorship", "price": "0.00", "active": "on"}
    data.update(overrides)
    return data


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_product_form_saves_limits_and_rejects_a_minimum_above_the_maximum(event):
    client = _organizer_client(event)

    response = client.post(_url(event, "products.add"), _product_form_data(min_per_order="3", max_per_order="2"))
    assert response.status_code == 200
    assert "can not be lower than the minimum" in response.content.decode()

    response = client.post(_url(event, "products.add"), _product_form_data(min_per_order="2", max_per_order="5"))
    assert response.status_code == 302
    with scopes_disabled():
        product = ExhibitionProduct.objects.get(event=event)
        assert (product.min_per_order, product.max_per_order) == (2, 5)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_sales_page_shows_a_checkbox_for_one_per_order_and_a_quantity_otherwise(event):
    _enable_plugin(event)
    with scopes_disabled():
        single = _product(event, "Gold Sponsor", max_per_order=1)
        multiple = _product(event, "Extra Badge", min_per_order=2, max_per_order=5)

    content = Client().get(_url(event, "public_call")).content.decode()

    assert f'type="checkbox" name="product" value="{single.pk}"' in content
    assert f'name="quantity_{multiple.pk}"' in content
    assert 'max="5"' in content
    assert "minimum amount to order: 2" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_buyer_can_order_several_of_a_product(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, max_per_order=5)
    client = Client()

    client.post(_url(event, "shop"), {f"quantity_{product.pk}": "3"})
    assert _cart_products(client, event) == [product.pk] * 3

    response = client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)
    assert response.url == _url(event, "shop.checkout.confirm")
    client.post(_url(event, "shop.checkout.confirm"))
    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.positions.filter(product=product).count() == 3


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
@pytest.mark.parametrize("quantity", ["1", "6"])
def test_the_sales_page_rejects_amounts_outside_the_limits(event, quantity):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, min_per_order=2, max_per_order=5)
    client = Client()

    response = client.post(_url(event, "shop"), {f"quantity_{product.pk}": quantity})

    assert response.url.startswith(_url(event, "public_call"))
    assert _cart_products(client, event) == []


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_sales_page_rejects_more_than_the_quota_has_left(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event)
        quota = ExhibitionQuota.objects.create(event=event, name="Badges", size=2)
        quota.products.set([product])
    client = Client()

    client.post(_url(event, "shop"), {f"quantity_{product.pk}": "3"})

    assert _cart_products(client, event) == []


@pytest.mark.django_db
def test_placing_an_order_enforces_the_limits(event):
    with scopes_disabled():
        product = _product(event, min_per_order=2, max_per_order=3)
        with pytest.raises(ExhibitionOrderError):
            place_exhibition_order(event, [product], **CUSTOMER_DATA, check_availability=False)
        with pytest.raises(ExhibitionOrderError):
            place_exhibition_order(event, [product] * 4, **CUSTOMER_DATA, check_availability=False)
        order = place_exhibition_order(event, [product] * 2, **CUSTOMER_DATA, check_availability=False)
        assert order.positions.count() == 2


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_back_office_form_books_quantities_within_the_limits(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, max_per_order=3)

    response = client.get(_url(event, "orders.add"), {f"quantity_{product.pk}": "4"})
    assert "less than or equal to 3" in response.content.decode()

    response = client.get(_url(event, "orders.add"), {f"quantity_{product.pk}": "2"})
    content = response.content.decode()
    assert "2 &times; Extra Badge" in content

    client.post(
        _url(event, "orders.add"), {**CUSTOMER_DATA, "products": [product.pk, product.pk], "payment_status": "paid"}
    )
    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.positions.count() == 2


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_back_office_form_rejects_too_many_through_the_chosen_products(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, max_per_order=1)

    client.post(
        _url(event, "orders.add"), {**CUSTOMER_DATA, "products": [product.pk, product.pk], "payment_status": "paid"}
    )

    with scopes_disabled():
        assert not ExhibitionOrder.objects.exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_huge_quantity_is_rejected_without_building_the_order(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event)
    client = Client()

    response = client.post(_url(event, "shop"), {f"quantity_{product.pk}": str(10**12)})

    assert response.url.startswith(_url(event, "public_call"))
    assert _cart_products(client, event) == []
    content = Client().get(_url(event, "public_call")).content.decode()
    assert f'max="{MAX_UNITS_PER_PRODUCT}"' in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_whose_quota_cannot_cover_its_minimum_is_sold_out(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, min_per_order=3)
        quota = ExhibitionQuota.objects.create(event=event, name="Badges", size=2)
        quota.products.set([product])

    content = Client().get(_url(event, "public_call")).content.decode()

    assert "SOLD OUT" in content
    assert f'name="quantity_{product.pk}"' not in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_add_on_step_reports_an_add_on_below_its_minimum(event):
    _enable_plugin(event)
    with scopes_disabled():
        passes = ExhibitionProductCategory.objects.create(event=event, name={"en": "Passes"}, is_addon=True)
        base = _product(event, "Main Booth", max_per_order=1)
        addon = _product(event, "Staff Pass", category=passes, min_per_order=2)
        rule = ExhibitionProductAddOn.objects.create(base_product=base, addon_category=passes, max_count=1)
    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    response = client.post(_url(event, "shop.checkout.addons"), {f"addon_0_{rule.pk}": str(addon.pk)})

    assert response.status_code == 200
    assert "You need to select at least 2 products of the product Staff Pass." in response.content.decode()
