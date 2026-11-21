from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import (
    ExhibitionOrder,
    ExhibitionProduct,
    ExhibitionProductAddOn,
    ExhibitionProductCategory,
    ExhibitionQuota,
)
from exhibition.shop import checkout_session_key, shop_products

CUSTOMER_DATA = {
    "organization_name": "Acme Inc",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
}


def _cart(client, event):
    return client.session.get(checkout_session_key(event), {})


def _product(event, name="Gold Sponsor", price="1000.00", category=None, **kwargs):
    return ExhibitionProduct.objects.create(
        event=event,
        name={"en": name},
        price=Decimal(price),
        category=category,
        **kwargs,
    )


def _category(event, name="Upgrades", is_addon=False, **kwargs):
    return ExhibitionProductCategory.objects.create(
        event=event,
        name={"en": name},
        is_addon=is_addon,
        **kwargs,
    )


def _organizer_client(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = User.objects.create_user(
            email="organizer@example.com",
            password="secret",
            fullname="Organizer",
            locale="en",
        )
        team = event.organizer.teams.create(
            name="Organizers",
            all_events=True,
            can_change_event_settings=True,
        )
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


def _enable_plugin(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _product_form_data(**overrides):
    data = {
        "name_0": "Standard Booth",
        "purpose": "exhibition",
        "includes_booth": "on",
        "price": "1000.00",
        "active": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_product_form_renders_tabbed_ui_and_addons_section(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor")

    response = client.get(_url(event, "products.edit", pk=product.pk))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'class="tabbed-form"' in content
    assert "<legend>General</legend>" in content
    assert "<legend>Price</legend>" in content
    assert "<legend>Availability</legend>" in content
    assert "<legend>Add-ons</legend>" in content
    assert "data-formset" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_configures_and_validates_addon_formset(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Base Package", price="1000.00")
        addon_cat = _category(event, "Booth Extras", is_addon=True)

    invalid = _product_form_data(
        name_0="Base Package",
        **{
            "addons-TOTAL_FORMS": "1",
            "addons-INITIAL_FORMS": "0",
            "addons-MIN_NUM_FORMS": "0",
            "addons-MAX_NUM_FORMS": "1000",
            "addons-0-addon_category": str(addon_cat.pk),
            "addons-0-min_count": "3",
            "addons-0-max_count": "1",
            "addons-0-ORDER": "0",
        },
    )
    response = client.post(_url(event, "products.edit", pk=product.pk), invalid)
    assert response.status_code == 200
    assert "minimum number of add-ons cannot be greater than the maximum" in response.content.decode()

    valid = _product_form_data(
        name_0="Base Package",
        price="1000.00",
        **{
            "addons-TOTAL_FORMS": "1",
            "addons-INITIAL_FORMS": "0",
            "addons-MIN_NUM_FORMS": "0",
            "addons-MAX_NUM_FORMS": "1000",
            "addons-0-addon_category": str(addon_cat.pk),
            "addons-0-min_count": "0",
            "addons-0-max_count": "2",
            "addons-0-multi_allowed": "on",
            "addons-0-price_included": "on",
            "addons-0-ORDER": "0",
        },
    )
    response = client.post(_url(event, "products.edit", pk=product.pk), valid)
    assert response.status_code == 302

    with scopes_disabled():
        rule = ExhibitionProductAddOn.objects.get(base_product=product)
        assert rule.addon_category == addon_cat
        assert rule.max_count == 2
        assert rule.price_included is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_product_cannot_have_addons(event):
    client = _organizer_client(event)
    with scopes_disabled():
        addon_cat = _category(event, "Addon Category", is_addon=True)
        another_cat = _category(event, "Extra Category", is_addon=True)
        addon_prod = _product(event, "An Add-on Item", category=addon_cat)

    form_data = _product_form_data(
        name_0="An Add-on Item",
        category=str(addon_cat.pk),
        **{
            "addons-TOTAL_FORMS": "1",
            "addons-INITIAL_FORMS": "0",
            "addons-MIN_NUM_FORMS": "0",
            "addons-MAX_NUM_FORMS": "1000",
            "addons-0-addon_category": str(another_cat.pk),
            "addons-0-min_count": "0",
            "addons-0-max_count": "1",
            "addons-0-ORDER": "0",
        },
    )
    response = client.post(_url(event, "products.edit", pk=addon_prod.pk), form_data)
    assert response.status_code == 200
    assert "only available as an add-on itself" in response.content.decode()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_category_products_excluded_from_public_catalog(event):
    _enable_plugin(event)
    with scopes_disabled():
        regular_cat = _category(event, "Standard Packages", is_addon=False)
        addon_cat = _category(event, "Add-on Accessories", is_addon=True)
        regular_prod = _product(event, "Exhibitor Booth", category=regular_cat)
        addon_prod = _product(event, "Extra Table", category=addon_cat)
        products = list(shop_products(event))
        assert regular_prod in products
        assert addon_prod not in products


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_checkout_addon_selection_and_validation(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Passes", is_addon=True)
        other_cat = _category(event, "Other Extras", is_addon=True)
        base = _product(event, "Main Booth", price="1000.00")
        addon = _product(event, "Staff Pass", price="50.00", category=addon_cat)
        foreign = _product(event, "Foreign Extra", price="75.00", category=other_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
            multi_allowed=False,
        )

    client = Client()
    response = client.post(_url(event, "shop"), {"product": [base.pk]})
    assert response.url == _url(event, "shop.checkout.addons")

    field = f"addon_0_{rule.pk}"
    response = client.post(_url(event, "shop.checkout.addons"), {field: str(foreign.pk)})
    assert response.status_code == 200
    assert "Invalid add-on product selected." in response.content.decode()
    assert _cart(client, event).get("addons") in (None, [])

    response = client.post(_url(event, "shop.checkout.addons"), {field: str(addon.pk)})
    assert response.url == _url(event, "shop.checkout.details")
    assert _cart(client, event).get("addons") == [
        {
            "base_index": 0,
            "product_id": addon.pk,
            "price": "50.00",
            "rule_id": rule.pk,
        }
    ]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_places_addons_with_quota_and_nested_display(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        addon_cat = _category(event, "Perks", is_addon=True)
        base = _product(event, "Sponsor Package", price="2000.00")
        perk = _product(event, "Logo On Stage", price="300.00", category=addon_cat)
        base_quota = ExhibitionQuota.objects.create(event=event, name="Booths", size=2)
        base_quota.products.set([base])
        addon_quota = ExhibitionQuota.objects.create(event=event, name="Perks", size=3)
        addon_quota.products.set([perk])
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=1,
            max_count=1,
            price_included=False,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})
    client.post(_url(event, "shop.checkout.addons"), {f"addon_0_{rule.pk}": str(perk.pk)})
    client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)
    client.post(_url(event, "shop.checkout.payment"), {"payment": "manual"})

    confirm = client.get(_url(event, "shop.checkout.confirm")).content.decode()
    assert "Sponsor Package" in confirm
    assert "Logo On Stage" in confirm
    assert "Add-on" in confirm

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_url(event, "shop.checkout.confirm"))
    assert response.status_code == 302

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        base_pos = order.positions.get(addon_to__isnull=True)
        addon_pos = order.positions.get(addon_to__isnull=False)
        assert addon_pos.addon_to == base_pos
        assert addon_pos.price == Decimal("300.00")
        assert order.total == Decimal("2300.00")
        assert base_quota.count_used() == 1
        assert addon_quota.count_used() == 1
        names = [str(position.localized_name) for position in order.nested_positions()]
        assert names == ["Sponsor Package", "Logo On Stage"]

    order_page = client.get(_url(event, "shop.order", code=order.code, secret=order.secret)).content.decode()
    assert order_page.index("Sponsor Package") < order_page.index("Logo On Stage")
    assert "Add-on" in order_page


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_formset_preserves_rule_ids_when_swapping_categories(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Base Package")
        cat_a = _category(event, "Category A", is_addon=True)
        cat_b = _category(event, "Category B", is_addon=True)
        rule_a = ExhibitionProductAddOn.objects.create(
            base_product=product,
            addon_category=cat_a,
            min_count=0,
            max_count=1,
            position=0,
        )
        rule_b = ExhibitionProductAddOn.objects.create(
            base_product=product,
            addon_category=cat_b,
            min_count=0,
            max_count=1,
            position=1,
        )

    form_data = _product_form_data(
        name_0="Base Package",
        **{
            "addons-TOTAL_FORMS": "2",
            "addons-INITIAL_FORMS": "2",
            "addons-MIN_NUM_FORMS": "0",
            "addons-MAX_NUM_FORMS": "1000",
            "addons-0-id": str(rule_a.pk),
            "addons-0-addon_category": str(cat_b.pk),
            "addons-0-min_count": "0",
            "addons-0-max_count": "1",
            "addons-0-ORDER": "0",
            "addons-1-id": str(rule_b.pk),
            "addons-1-addon_category": str(cat_a.pk),
            "addons-1-min_count": "0",
            "addons-1-max_count": "1",
            "addons-1-ORDER": "1",
        },
    )
    response = client.post(_url(event, "products.edit", pk=product.pk), form_data)
    assert response.status_code == 302

    with scopes_disabled():
        rules = list(ExhibitionProductAddOn.objects.filter(base_product=product).order_by("position"))
        assert [rule.pk for rule in rules] == [rule_a.pk, rule_b.pk]
        assert [rule.addon_category_id for rule in rules] == [cat_b.pk, cat_a.pk]
