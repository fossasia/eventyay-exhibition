from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionProductCategoryForm
from exhibition.models import (
    ExhibitionOrder,
    ExhibitionProduct,
    ExhibitionProductAddOn,
    ExhibitionProductCategory,
    ExhibitionQuota,
)
from exhibition.shop import checkout_session_key, shop_products


def _cart(client, event):
    session = client.session
    return session.get(checkout_session_key(event), {})


CUSTOMER_DATA = {
    "organization_name": "Acme Inc",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
}


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


def _organizer(event, email="organizer@example.com", **flags):
    user = User.objects.create_user(email=email, password="secret", fullname="Organizer", locale="en")
    team = event.organizer.teams.create(name=email, all_events=True, **flags)
    team.members.add(user)
    return user


def _organizer_client(event, **flags):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = _organizer(event, **(flags or {"can_change_event_settings": True}))
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


# ---------------------------------------------------------------------------
# 1. Product Form UI & Add-on Configuration
# ---------------------------------------------------------------------------


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
    assert "Add-On" in content
    assert "Add a new add-on" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_configures_addons_via_formset(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Base Package", price="1000.00")
        addon_cat = _category(event, "Booth Extras", is_addon=True)

    form_data = _product_form_data(
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

    response = client.post(_url(event, "products.edit", pk=product.pk), form_data)
    assert response.status_code == 302

    with scopes_disabled():
        addon_rule = ExhibitionProductAddOn.objects.get(base_product=product)
        assert addon_rule.addon_category == addon_cat
        assert addon_rule.min_count == 0
        assert addon_rule.max_count == 2
        assert addon_rule.multi_allowed is True
        assert addon_rule.price_included is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_formset_validation_min_greater_than_max(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Base Package")
        addon_cat = _category(event, "Booth Extras", is_addon=True)

    form_data = _product_form_data(
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

    response = client.post(_url(event, "products.edit", pk=product.pk), form_data)
    assert response.status_code == 200
    content = response.content.decode()
    assert "The minimum number of add-ons cannot be greater than the maximum number." in content

    with scopes_disabled():
        assert not ExhibitionProductAddOn.objects.filter(base_product=product).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_formset_validation_duplicate_category(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Base Package")
        addon_cat = _category(event, "Booth Extras", is_addon=True)

    form_data = _product_form_data(
        name_0="Base Package",
        **{
            "addons-TOTAL_FORMS": "2",
            "addons-INITIAL_FORMS": "0",
            "addons-MIN_NUM_FORMS": "0",
            "addons-MAX_NUM_FORMS": "1000",
            "addons-0-addon_category": str(addon_cat.pk),
            "addons-0-min_count": "0",
            "addons-0-max_count": "1",
            "addons-0-ORDER": "0",
            "addons-1-addon_category": str(addon_cat.pk),
            "addons-1-min_count": "0",
            "addons-1-max_count": "1",
            "addons-1-ORDER": "1",
        },
    )

    response = client.post(_url(event, "products.edit", pk=product.pk), form_data)
    assert response.status_code == 200
    content = response.content.decode()
    assert "You added the same add-on category twice." in content

    with scopes_disabled():
        assert not ExhibitionProductAddOn.objects.filter(base_product=product).exists()


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
    content = response.content.decode()
    assert "You cannot add add-ons to a product that is only available as an add-on itself." in content

    with scopes_disabled():
        assert not ExhibitionProductAddOn.objects.filter(base_product=addon_prod).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_moving_product_to_addon_category_without_formset_rejects_orphaned_rules(event):
    """Category change without addons-TOTAL_FORMS must not leave rules on an add-on product."""
    client = _organizer_client(event)
    with scopes_disabled():
        regular_cat = _category(event, "Packages", is_addon=False)
        addon_cat = _category(event, "Booth Extras", is_addon=True)
        product = _product(event, "Base Package", category=regular_cat)
        ExhibitionProductAddOn.objects.create(
            base_product=product,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
        )

    # Omit the add-ons formset management keys entirely.
    form_data = _product_form_data(name_0="Base Package", category=str(addon_cat.pk))
    response = client.post(_url(event, "products.edit", pk=product.pk), form_data)
    assert response.status_code == 200
    content = response.content.decode()
    assert "You cannot add add-ons to a product that is only available as an add-on itself." in content

    with scopes_disabled():
        product.refresh_from_db()
        assert product.category_id == regular_cat.pk
        assert product.addons.exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_moving_product_to_addon_category_clears_addons_when_formset_emptied(event):
    client = _organizer_client(event)
    with scopes_disabled():
        regular_cat = _category(event, "Packages", is_addon=False)
        addon_cat = _category(event, "Booth Extras", is_addon=True)
        product = _product(event, "Base Package", category=regular_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=product,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
        )

    form_data = _product_form_data(
        name_0="Base Package",
        category=str(addon_cat.pk),
        **{
            "addons-TOTAL_FORMS": "1",
            "addons-INITIAL_FORMS": "1",
            "addons-MIN_NUM_FORMS": "0",
            "addons-MAX_NUM_FORMS": "1000",
            "addons-0-id": str(rule.pk),
            "addons-0-addon_category": str(addon_cat.pk),
            "addons-0-min_count": "0",
            "addons-0-max_count": "1",
            "addons-0-ORDER": "0",
            "addons-0-DELETE": "on",
        },
    )
    response = client.post(_url(event, "products.edit", pk=product.pk), form_data)
    assert response.status_code == 302

    with scopes_disabled():
        product.refresh_from_db()
        assert product.category_id == addon_cat.pk
        assert not product.addons.exists()


# ---------------------------------------------------------------------------
# 2. Category is_addon Flag & Public Catalog Isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_category_form_handles_is_addon_field(event):
    with scopes_disabled():
        form = ExhibitionProductCategoryForm(
            data={"name_0": "Add-on Only Category", "is_addon": True},
            event=event,
        )
        assert form.is_valid(), form.errors
        form.instance.event = event
        cat = form.save()
        cat.refresh_from_db()
        assert cat.is_addon is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_category_products_excluded_from_public_catalog(event):
    _enable_plugin(event)
    with scopes_disabled():
        regular_cat = _category(event, "Standard Packages", is_addon=False)
        addon_cat = _category(event, "Add-on Accessories", is_addon=True)

        regular_prod = _product(event, "Exhibitor Booth", category=regular_cat)
        loose_prod = _product(event, "Standalone Sponsorship", category=None)
        addon_prod = _product(event, "Extra Table & Chairs", category=addon_cat)

        products = list(shop_products(event))
        assert regular_prod in products
        assert loose_prod in products
        assert addon_prod not in products

    content = Client().get(_url(event, "public_call")).content.decode()
    assert "Exhibitor Booth" in content
    assert "Standalone Sponsorship" in content
    assert "Extra Table &amp; Chairs" not in content


# ---------------------------------------------------------------------------
# 3. Checkout Add-on Flow (Shop Navigation & Validation)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_checkout_without_addons_skips_addons_step(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Simple Booth")

    client = Client()
    response = client.post(_url(event, "shop"), {"product": [product.pk]})
    assert response.url == _url(event, "shop.checkout.details")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_checkout_with_addons_redirects_to_addons_step(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Options", is_addon=True)
        base = _product(event, "Main Booth")
        _product(event, "Extra Screen", category=addon_cat)
        ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
        )

    client = Client()
    response = client.post(_url(event, "shop"), {"product": [base.pk]})
    assert response.url == _url(event, "shop.checkout.addons")

    addons_page = client.get(_url(event, "shop.checkout.addons"))
    assert addons_page.status_code == 200
    content = addons_page.content.decode()
    assert "choose additional options before you continue" in content
    assert "Main Booth" in content
    assert "Options" in content
    assert "Extra Screen" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_visiting_addons_step_directly_when_no_addons_needed_redirects(event):
    _enable_plugin(event)
    with scopes_disabled():
        base = _product(event, "Simple Booth")

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    response = client.get(_url(event, "shop.checkout.addons"))
    assert response.url == _url(event, "shop.checkout.details")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addons_single_choice_radio_selection(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Passes", is_addon=True)
        base = _product(event, "Main Booth", price="1000.00")
        addon = _product(event, "Staff Pass", price="50.00", category=addon_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
            multi_allowed=False,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    # Select the add-on
    field_name = f"addon_0_{rule.pk}"
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: str(addon.pk)})
    assert response.url == _url(event, "shop.checkout.details")
    assert _cart(client, event).get("addons") == [
        {
            "base_index": 0,
            "product_id": addon.pk,
            "price": "50.00",
            "rule_id": rule.pk,
        }
    ]

    # Or select none — previously selected add-on must leave the cart
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: ""})
    assert response.url == _url(event, "shop.checkout.details")
    assert _cart(client, event).get("addons") == []


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addons_post_rejects_product_outside_rule_category(event):
    """Manipulated product PKs from another category must not enter the session cart."""
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Passes", is_addon=True)
        other_cat = _category(event, "Other Extras", is_addon=True)
        base = _product(event, "Main Booth", price="1000.00")
        allowed = _product(event, "Staff Pass", price="50.00", category=addon_cat)
        foreign = _product(event, "Foreign Extra", price="75.00", category=other_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
            multi_allowed=False,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    response = client.post(
        _url(event, "shop.checkout.addons"),
        {f"addon_0_{rule.pk}": str(foreign.pk)},
    )
    assert response.status_code == 200
    assert "Invalid add-on product selected." in response.content.decode()
    assert _cart(client, event).get("addons") in (None, [])

    # A valid product from the rule category still works.
    response = client.post(
        _url(event, "shop.checkout.addons"),
        {f"addon_0_{rule.pk}": str(allowed.pk)},
    )
    assert response.url == _url(event, "shop.checkout.details")
    assert _cart(client, event).get("addons") == [
        {
            "base_index": 0,
            "product_id": allowed.pk,
            "price": "50.00",
            "rule_id": rule.pk,
        }
    ]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addons_required_single_choice_validation(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Mandatory Tiers", is_addon=True)
        base = _product(event, "Main Booth")
        _product(event, "Tier 1", category=addon_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=1,
            max_count=1,
            multi_allowed=False,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    field_name = f"addon_0_{rule.pk}"
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: ""})
    assert response.status_code == 200
    content = response.content.decode()
    assert "Please select at least 1 options for Mandatory Tiers on Main Booth." in content
    assert _cart(client, event).get("addons") in (None, [])


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addons_multi_quantity_validation(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Chairs", is_addon=True)
        base = _product(event, "Main Booth")
        chair = _product(event, "Extra Chair", category=addon_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=1,
            max_count=3,
            multi_allowed=True,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    field_name = f"addon_0_{rule.pk}_{chair.pk}"

    # Exceeding max_count (4 > 3)
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: "4"})
    assert response.status_code == 200
    assert "You can select at most 3 options for Chairs on Main Booth." in response.content.decode()
    assert _cart(client, event).get("addons") in (None, [])

    # Below min_count (0 < 1)
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: "0"})
    assert response.status_code == 200
    assert "Please select at least 1 options for Chairs on Main Booth." in response.content.decode()
    assert _cart(client, event).get("addons") in (None, [])

    # Valid count (2)
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: "2"})
    assert response.url == _url(event, "shop.checkout.details")
    assert len(_cart(client, event).get("addons") or []) == 2


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addons_sold_out_cannot_be_selected(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Monitors", is_addon=True)
        base = _product(event, "Main Booth")
        monitor = _product(event, "4K TV", category=addon_cat)
        quota = ExhibitionQuota.objects.create(event=event, name="Monitors Quota", size=0)
        quota.products.set([monitor])
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=0,
            max_count=1,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})

    field_name = f"addon_0_{rule.pk}"
    response = client.post(_url(event, "shop.checkout.addons"), {field_name: str(monitor.pk)})
    assert response.status_code == 200
    assert "4K TV is sold out." in response.content.decode()
    assert _cart(client, event).get("addons") in (None, [])


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_required_addons_cannot_skip_to_details(event):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Mandatory", is_addon=True)
        base = _product(event, "Main Booth")
        _product(event, "Required Extra", category=addon_cat)
        ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=1,
            max_count=1,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})
    response = client.get(_url(event, "shop.checkout.details"))
    assert response.url == _url(event, "shop.checkout.addons")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_cannot_delete_nonempty_addon_category(event):
    client = _organizer_client(event)
    with scopes_disabled():
        addon_cat = _category(event, "Extras", is_addon=True)
        _product(event, "Extra Chair", category=addon_cat)

    response = client.post(_url(event, "products.categories.delete", pk=addon_cat.pk))
    assert response.status_code == 302
    with scopes_disabled():
        assert ExhibitionProductCategory.objects.filter(pk=addon_cat.pk).exists()


# ---------------------------------------------------------------------------
# 4. Order Placement & Position Linking
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_placement_with_price_included_addon(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        addon_cat = _category(event, "Perks", is_addon=True)
        base = _product(event, "Gold Sponsor", price="5000.00")
        perk = _product(event, "VIP Lounge Access", price="500.00", category=addon_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=1,
            max_count=1,
            price_included=True,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})
    client.post(_url(event, "shop.checkout.addons"), {f"addon_0_{rule.pk}": str(perk.pk)})
    client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)
    client.post(_url(event, "shop.checkout.payment"), {"payment": "manual"})

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_url(event, "shop.checkout.confirm"))
    assert response.status_code == 302

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        positions = list(order.positions.all())
        assert len(positions) == 2

        base_pos = order.positions.get(addon_to__isnull=True)
        addon_pos = order.positions.get(addon_to__isnull=False)

        assert base_pos.product == base
        assert base_pos.price == Decimal("5000.00")
        assert not base_pos.is_addon

        assert addon_pos.product == perk
        assert addon_pos.addon_to == base_pos
        assert addon_pos.price == Decimal("0.00")
        assert addon_pos.is_addon

        # Order total must match base product since add-on was included in price
        assert order.total == Decimal("5000.00")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_placement_with_paid_addon(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        addon_cat = _category(event, "Furniture", is_addon=True)
        base = _product(event, "Standard Booth", price="1000.00")
        desk = _product(event, "Standing Desk", price="150.00", category=addon_cat)
        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=0,
            max_count=2,
            multi_allowed=True,
            price_included=False,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})
    client.post(_url(event, "shop.checkout.addons"), {f"addon_0_{rule.pk}_{desk.pk}": "2"})
    client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)
    client.post(_url(event, "shop.checkout.payment"), {"payment": "manual"})

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_url(event, "shop.checkout.confirm"))
    assert response.status_code == 302

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        base_pos = order.positions.get(addon_to__isnull=True)
        addon_positions = list(order.positions.filter(addon_to=base_pos))

        assert len(addon_positions) == 2
        for pos in addon_positions:
            assert pos.product == desk
            assert pos.price == Decimal("150.00")
            assert pos.is_addon

        # Order total: 1000 + (2 * 150) = 1300
        assert order.total == Decimal("1300.00")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_placement_reserves_quotas_for_base_and_addon(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    with scopes_disabled():
        addon_cat = _category(event, "Passes", is_addon=True)
        base = _product(event, "Free Community Booth", price="0.00")
        pass_item = _product(event, "Workshop Pass", price="0.00", category=addon_cat)

        base_quota = ExhibitionQuota.objects.create(event=event, name="Booths", size=2)
        base_quota.products.set([base])
        addon_quota = ExhibitionQuota.objects.create(event=event, name="Passes", size=3)
        addon_quota.products.set([pass_item])

        rule = ExhibitionProductAddOn.objects.create(
            base_product=base,
            addon_category=addon_cat,
            min_count=1,
            max_count=1,
        )

    client = Client()
    client.post(_url(event, "shop"), {"product": [base.pk]})
    client.post(_url(event, "shop.checkout.addons"), {f"addon_0_{rule.pk}": str(pass_item.pk)})
    client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)

    with django_capture_on_commit_callbacks(execute=True):
        client.post(_url(event, "shop.checkout.confirm"))

    with scopes_disabled():
        assert base_quota.count_used() == 1
        assert addon_quota.count_used() == 1
        assert base_quota.remaining == 1
        assert addon_quota.remaining == 2


# ---------------------------------------------------------------------------
# 5. Order Display (Summary, Detail, and Customer Shop Order)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_display_shows_addons_indented_under_base_product(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        addon_cat = _category(event, "Perks", is_addon=True)
        base = _product(event, "Sponsor Package", price="2000.00")
        perk = _product(event, "Logo On Stage", price="300.00", category=addon_cat)
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

    # Check confirm page summary
    confirm_page = client.get(_url(event, "shop.checkout.confirm"))
    confirm_content = confirm_page.content.decode()
    assert "Sponsor Package" in confirm_content
    assert "Logo On Stage" in confirm_content
    assert "Add-on" in confirm_content

    with django_capture_on_commit_callbacks(execute=True):
        client.post(_url(event, "shop.checkout.confirm"))

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)

    # Check customer order page
    order_page = client.get(_url(event, "shop.order", code=order.code, secret=order.secret))
    order_content = order_page.content.decode()
    assert "Sponsor Package" in order_content
    assert "Logo On Stage" in order_content
    assert "Add-on" in order_content

    # Check organizer order detail page
    org_client = _organizer_client(event)
    admin_order_page = org_client.get(_url(event, "orders.detail", code=order.code))
    admin_content = admin_order_page.content.decode()
    assert "Sponsor Package" in admin_content
    assert "Logo On Stage" in admin_content
    assert "Add-on" in admin_content
