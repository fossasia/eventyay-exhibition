import datetime as dt
import re
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, LogEntry
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionDiscountBulkForm, ExhibitionDiscountForm
from exhibition.mail import order_product_list
from exhibition.models import (
    LOG_DISCOUNT_REDEEMED,
    ExhibitionDiscount,
    ExhibitionDiscountError,
    ExhibitionDiscountPriceMode,
    ExhibitionOrder,
    ExhibitionOrderError,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    find_discount,
)
from exhibition.orders import place_exhibition_order

EIGHT_HUNDRED = re.compile(r"(?<![\d,.])800\.00")

CUSTOMER_DATA = {"organization_name": "Acme Inc", "name": "Ada Lovelace", "email": "ada@example.com"}


def _product(event, name, price="1000.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _discount(event, code="PARTNER20", products=(), **kwargs):
    kwargs.setdefault("price_mode", ExhibitionDiscountPriceMode.PERCENT)
    kwargs.setdefault("value", Decimal("20"))
    discount = ExhibitionDiscount.objects.create(event=event, code=code, **kwargs)
    discount.products.set(products)
    return discount


def _order(event, products, **kwargs):
    event.settings.set("payment_manual__enabled", True)
    kwargs.setdefault("payment_provider", "manual")
    return place_exhibition_order(event, products, **CUSTOMER_DATA, **kwargs)


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _packages_url(event):
    return _url(event, "public_call") + "#exhibition-packages"


def _enable_plugin(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()


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


# Prices


@pytest.mark.django_db
def test_each_price_effect_lowers_the_price_within_zero_and_the_product_price(event):
    with scopes_disabled():
        product = _product(event, "Gold", price="1000.00")
        cases = [
            (ExhibitionDiscountPriceMode.PERCENT, "20", "800.00"),
            (ExhibitionDiscountPriceMode.PERCENT, "100", "0.00"),
            (ExhibitionDiscountPriceMode.SUBTRACT, "250", "750.00"),
            (ExhibitionDiscountPriceMode.SUBTRACT, "5000", "0.00"),
            (ExhibitionDiscountPriceMode.SET, "300", "300.00"),
            (ExhibitionDiscountPriceMode.SET, "5000", "1000.00"),
        ]
        for index, (mode, value, expected) in enumerate(cases):
            discount = _discount(event, code=f"CODE{index}", price_mode=mode, value=Decimal(value))
            assert discount.price_for(product) == Decimal(expected), (mode, value)


@pytest.mark.django_db
def test_a_code_without_products_covers_every_product(event):
    with scopes_disabled():
        gold = _product(event, "Gold")
        booth = _product(event, "Booth")
        everything = _discount(event, code="ALL")
        gold_only = _discount(event, code="GOLD", products=[gold])

        assert everything.applies_to(gold) and everything.applies_to(booth)
        assert gold_only.applies_to(gold) and not gold_only.applies_to(booth)


@pytest.mark.django_db
def test_codes_are_stored_and_found_in_upper_case(event):
    with scopes_disabled():
        discount = _discount(event, code="partner20")

        assert discount.code == "PARTNER20"
        assert find_discount(event, " Partner20 ") == discount
        assert find_discount(event, "unknown") is None


# Orders


@pytest.mark.django_db
def test_an_order_records_the_discounted_price_and_the_code(event):
    with scopes_disabled():
        gold = _product(event, "Gold", price="1000.00")
        booth = _product(event, "Booth", price="500.00")
        discount = _discount(event, products=[gold], max_usages=5)

        order = _order(event, [gold, booth], discount=discount)

        gold_position, booth_position = order.positions.order_by("id")
        assert (gold_position.price, gold_position.original_price, gold_position.discount) == (
            Decimal("800.00"),
            Decimal("1000.00"),
            discount,
        )
        assert (booth_position.price, booth_position.original_price, booth_position.discount) == (
            Decimal("500.00"),
            None,
            None,
        )
        assert order.total == Decimal("1300.00")
        assert discount.count_used() == 1
        assert LogEntry.objects.filter(action_type=LOG_DISCOUNT_REDEEMED, object_id=discount.pk).exists()


@pytest.mark.django_db
def test_a_code_cannot_be_used_more_often_than_its_maximum(event):
    with scopes_disabled():
        gold = _product(event, "Gold")
        silver = _product(event, "Silver")
        discount = _discount(event, max_usages=1)

        with pytest.raises(ExhibitionDiscountError):
            _order(event, [gold, silver], discount=discount)
        _order(event, [gold], discount=discount)
        with pytest.raises(ExhibitionDiscountError):
            _order(event, [silver], discount=discount)

        assert ExhibitionOrder.objects.filter(event=event).count() == 1


@pytest.mark.django_db
def test_an_expired_code_cannot_be_used(event):
    with scopes_disabled():
        discount = _discount(event, valid_until=now() - dt.timedelta(days=1))

        with pytest.raises(ExhibitionDiscountError):
            _order(event, [_product(event, "Gold")], discount=discount)


@pytest.mark.django_db
def test_a_code_of_another_event_is_refused(event):
    with scopes_disabled():
        other = Event.objects.create(organizer=event.organizer, name="Other", slug="other", live=True, date_from=now())
        discount = _discount(other)

        with pytest.raises(ExhibitionDiscountError):
            _order(event, [_product(event, "Gold")], discount=discount)


@pytest.mark.django_db
def test_cancelled_and_expired_orders_give_their_uses_back(event):
    with scopes_disabled():
        product = _product(event, "Gold")
        discount = _discount(event, max_usages=1)
        first = _order(event, [product], discount=discount)
        first.cancel()

        second = _order(event, [product], discount=discount)
        assert discount.count_used() == 1
        second.expire()

        assert discount.count_used() == 0


@pytest.mark.django_db
def test_an_expired_order_needs_its_code_uses_back_to_be_paid(event):
    with scopes_disabled():
        product = _product(event, "Gold")
        discount = _discount(event, max_usages=1)
        expired = _order(event, [product], discount=discount)
        expired.expire()
        _order(event, [product], discount=discount)

        with pytest.raises(ExhibitionDiscountError):
            expired.mark_paid()

        expired.refresh_from_db()
        assert expired.status == ExhibitionOrderStatus.EXPIRED


@pytest.mark.django_db
def test_a_full_discount_makes_the_order_free(event):
    with scopes_disabled():
        discount = _discount(event, value=Decimal("100"))

        order = place_exhibition_order(event, [_product(event, "Gold")], **CUSTOMER_DATA, discount=discount)

        assert order.total == Decimal("0.00")
        assert order.status == ExhibitionOrderStatus.PAID


@pytest.mark.django_db
def test_a_hidden_product_is_only_sold_with_a_code_that_reveals_it(event):
    with scopes_disabled():
        secret = _product(event, "Negotiated Gold", hide_without_discount=True)
        plain = _discount(event, code="PLAIN")
        revealing = _discount(event, code="REVEAL", show_hidden_products=True)

        with pytest.raises(ExhibitionOrderError):
            _order(event, [secret])
        with pytest.raises(ExhibitionOrderError):
            _order(event, [secret], discount=plain)
        assert _order(event, [secret], discount=revealing).positions.get().discount == revealing
        assert _order(event, [secret], check_availability=False).positions.get().discount is None


@pytest.mark.django_db
def test_the_order_email_lists_the_discount(event):
    with scopes_disabled():
        discount = _discount(event)
        order = _order(event, [_product(event, "Gold", price="1000.00")], discount=discount)

        listed = order_product_list(order)

    assert "Gold" in listed
    assert "PARTNER20" in listed
    assert EIGHT_HUNDRED.search(listed)


# Buyers


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_buyers_apply_a_code_and_see_the_discounted_price(event):
    _enable_plugin(event)
    with scopes_disabled():
        _product(event, "Gold Sponsor", price="1000.00")
        _discount(event, max_usages=5)
    client = Client()

    assert "Have a discount code?" in client.get(_url(event, "public_call")).content.decode()

    response = client.get(_url(event, "public_call"), {"discount": "partner20"})
    assert response.url == _packages_url(event)

    content = client.get(_url(event, "public_call")).content.decode()
    assert "PARTNER20" in content
    assert "<del>" in content
    assert EIGHT_HUNDRED.search(content)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_unknown_or_used_up_code_is_refused(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor")
        used_up = _discount(event, code="USEDUP", max_usages=1)
        _order(event, [product], discount=used_up)
    client = Client()

    for code in ("NOPE", "USEDUP"):
        client.get(_url(event, "public_call"), {"discount": code})
        content = client.get(_url(event, "public_call")).content.decode()
        assert "<del>" not in content
        assert "is applied" not in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_redeem_link_reveals_a_hidden_product_and_the_checkout_uses_the_code(event):
    _enable_plugin(event)
    with scopes_disabled():
        secret = _product(event, "Negotiated Gold", price="1000.00", hide_without_discount=True)
        _discount(event, code="INVITE", value=Decimal("100"), show_hidden_products=True)
    client = Client()

    assert "Negotiated Gold" not in client.get(_url(event, "public_call")).content.decode()

    client.get(_url(event, "public_call"), {"discount": "INVITE"})
    assert "Negotiated Gold" in client.get(_url(event, "public_call")).content.decode()

    client.post(_url(event, "shop"), {"product": [secret.pk]})
    response = client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)
    assert response.url == _url(event, "shop.checkout.confirm")
    client.post(_url(event, "shop.checkout.confirm"))

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.total == Decimal("0.00")
        assert order.status == ExhibitionOrderStatus.PAID
        assert order.positions.get().discount.code == "INVITE"


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_code_used_up_before_confirming_sends_the_buyer_back(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor", price="0.00")
        discount = _discount(event, max_usages=1)
    client = Client()
    client.get(_url(event, "public_call"), {"discount": "PARTNER20"})
    client.post(_url(event, "shop"), {"product": [product.pk]})
    client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)
    with scopes_disabled():
        _order(event, [product], discount=discount)

    response = client.post(_url(event, "shop.checkout.confirm"))

    assert response.url == _packages_url(event)
    with scopes_disabled():
        assert ExhibitionOrder.objects.filter(event=event).count() == 1


# Organizers


@pytest.mark.django_db
def test_the_form_generates_a_code_and_rejects_a_taken_one(event):
    with scopes_disabled():
        _discount(event, code="TAKEN")
        assert ExhibitionDiscountForm(event=event).initial["code"]

        form = ExhibitionDiscountForm(
            data={"code": "taken", "price_mode": "percent", "value": "10", "max_usages": "1"}, event=event
        )
        assert not form.is_valid()
        assert "code" in form.errors

        form = ExhibitionDiscountForm(
            data={"code": "NEW", "price_mode": "percent", "value": "110", "max_usages": "1"}, event=event
        )
        assert not form.is_valid()
        assert "value" in form.errors


@pytest.mark.django_db
def test_multiple_codes_share_their_settings_and_prefix(event):
    with scopes_disabled():
        gold = _product(event, "Gold")
        form = ExhibitionDiscountBulkForm(
            data={
                "count": "3",
                "prefix": "sponsor-",
                "price_mode": "subtract",
                "value": "100",
                "max_usages": "2",
                "products": [gold.pk],
                "tag": "partners",
            },
            event=event,
        )
        assert form.is_valid(), form.errors
        discounts = form.create_discounts()

    assert len({discount.code for discount in discounts}) == 3
    for discount in discounts:
        assert discount.code.startswith("SPONSOR-")
        assert discount.max_usages == 2
        assert discount.tag == "partners"
        assert list(discount.products.all()) == [gold]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizers_create_list_and_edit_codes(event):
    client = _organizer_client(event)

    response = client.post(
        _url(event, "products.discounts.add"),
        {"code": "early", "price_mode": "percent", "value": "15", "max_usages": "10", "tag": "early-bird"},
    )
    assert response.url == _url(event, "products.discounts")

    with scopes_disabled():
        discount = ExhibitionDiscount.objects.get(event=event)
    assert discount.code == "EARLY"
    assert "EARLY" in client.get(_url(event, "products.discounts")).content.decode()
    assert "EARLY" in client.get(_url(event, "products.discounts"), {"status": "valid"}).content.decode()
    assert "EARLY" not in client.get(_url(event, "products.discounts"), {"status": "expired"}).content.decode()

    content = client.get(_url(event, "products.discounts.edit", pk=discount.pk)).content.decode()
    assert "discount=EARLY" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_used_code_cannot_be_deleted(event):
    client = _organizer_client(event)
    with scopes_disabled():
        used = _discount(event, code="USED", max_usages=5)
        unused = _discount(event, code="UNUSED")
        _order(event, [_product(event, "Gold")], discount=used)

    client.post(_url(event, "products.discounts.delete", pk=used.pk))
    client.post(_url(event, "products.discounts.delete", pk=unused.pk))

    with scopes_disabled():
        assert list(ExhibitionDiscount.objects.filter(event=event)) == [used]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizers_can_book_an_order_with_a_code(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Gold", price="1000.00")
        _discount(event, max_usages=5)

    response = client.post(
        _url(event, "orders.add"),
        {**CUSTOMER_DATA, "products": [product.pk], "payment_status": "paid", "discount_code": "nope"},
    )
    assert response.status_code == 200
    with scopes_disabled():
        assert not ExhibitionOrder.objects.filter(event=event).exists()

    client.post(
        _url(event, "orders.add"),
        {**CUSTOMER_DATA, "products": [product.pk], "payment_status": "paid", "discount_code": "partner20"},
    )

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.total == Decimal("800.00")
        assert order.positions.get().discount.code == "PARTNER20"


@pytest.mark.django_db
def test_the_product_api_leaves_out_products_only_sold_with_a_code(event):
    client = _organizer_client(event)
    with scopes_disabled():
        listed = _product(event, "Gold")
        hidden = _product(event, "Negotiated Gold", hide_without_discount=True)
    api_kwargs = {"organizer": event.organizer.slug, "event": event.slug}

    response = client.get(reverse("api-v1:exhibitionproduct-list", kwargs=api_kwargs))

    assert [row["id"] for row in response.json()["results"]] == [listed.pk]
    assert (
        client.get(reverse("api-v1:exhibitionproduct-detail", kwargs={**api_kwargs, "id": hidden.pk})).status_code
        == 404
    )


@pytest.mark.django_db
def test_multiple_codes_need_a_tag(event):
    with scopes_disabled():
        form = ExhibitionDiscountBulkForm(
            data={"count": "2", "price_mode": "percent", "value": "10", "max_usages": "1"}, event=event
        )

        assert not form.is_valid()
        assert "tag" in form.errors


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_codes_sharing_a_tag_are_listed_in_one_row(event):
    client = _organizer_client(event)
    with scopes_disabled():
        _discount(event, code="PARTNER-A", tag="partners", max_usages=2)
        _discount(event, code="PARTNER-B", tag="partners", max_usages=3)
        _discount(event, code="LOOSE")

    response = client.get(_url(event, "products.discounts"))
    content = response.content.decode()

    rows = response.context["discounts"]
    assert sorted(str(row) for row in rows) == ["LOOSE", "PARTNER-A"]
    group = next(row for row in rows if row.tag).group
    assert [str(member) for member in group["members"]] == ["PARTNER-A", "PARTNER-B"]
    assert (group["used"], group["max_usages"]) == (0, 5)
    assert "0 / 5" in content
    assert "PARTNER-B" in content

    filtered = client.get(_url(event, "products.discounts"), {"tag": "partners"})
    assert sorted(str(row) for row in filtered.context["discounts"]) == ["PARTNER-A", "PARTNER-B"]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_code_that_runs_out_while_the_order_is_placed_sends_the_buyer_back(event, monkeypatch):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor", price="0.00")
        _discount(event, max_usages=1)
    client = Client()
    client.get(_url(event, "public_call"), {"discount": "PARTNER20"})
    client.post(_url(event, "shop"), {"product": [product.pk]})
    client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)

    def used_up(discount, needed, **kwargs):
        raise ExhibitionDiscountError("used up")

    monkeypatch.setattr("exhibition.orders.reserve_discount_usage", used_up)
    response = client.post(_url(event, "shop.checkout.confirm"))

    assert response.url == _packages_url(event)
    with scopes_disabled():
        assert not ExhibitionOrder.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_the_database_refuses_malformed_discounts(event):
    with scopes_disabled():
        for settings in (
            {"price_mode": "free", "value": Decimal("10")},
            {"price_mode": ExhibitionDiscountPriceMode.SUBTRACT, "value": Decimal("-1")},
            {"price_mode": ExhibitionDiscountPriceMode.PERCENT, "value": Decimal("150")},
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                ExhibitionDiscount.objects.create(event=event, code="BROKEN", **settings)


@pytest.mark.django_db
def test_a_used_code_can_no_longer_be_renamed(event):
    with scopes_disabled():
        used = _discount(event, code="USED", max_usages=5)
        unused = _discount(event, code="UNUSED")
        _order(event, [_product(event, "Gold")], discount=used)

        assert ExhibitionDiscountForm(instance=used, event=event).fields["code"].disabled
        assert not ExhibitionDiscountForm(instance=unused, event=event).fields["code"].disabled


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_cannot_be_deleted_while_it_is_a_codes_only_product(event):
    client = _organizer_client(event)
    with scopes_disabled():
        gold = _product(event, "Gold")
        silver = _product(event, "Silver")
        _discount(event, code="GOLD", products=[gold])
        _discount(event, code="BOTH", products=[gold, silver])

    client.post(_url(event, "products.delete", pk=gold.pk))
    client.post(_url(event, "products.delete", pk=silver.pk))

    with scopes_disabled():
        assert list(ExhibitionProduct.objects.filter(event=event)) == [gold]
        assert list(ExhibitionDiscount.objects.get(code="BOTH").products.all()) == [gold]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_call_page_offers_the_code_field_when_every_product_needs_a_code(event):
    _enable_plugin(event)
    with scopes_disabled():
        _product(event, "Negotiated Gold", hide_without_discount=True)

    assert Client().get(_url(event, "public_call")).status_code == 404

    with scopes_disabled():
        _discount(event, show_hidden_products=True)

    content = Client().get(_url(event, "public_call")).content.decode()
    assert "Have a discount code?" in content
    assert "Negotiated Gold" not in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_grouped_rows_sort_by_their_combined_uses(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Gold", price="0.00")
        group_a = _discount(event, code="A1", tag="alpha", max_usages=10)
        _discount(event, code="A2", tag="alpha", max_usages=10)
        single = _discount(event, code="SINGLE", max_usages=10)
        group_b = _discount(event, code="B1", tag="beta", max_usages=10)
        _discount(event, code="B2", tag="beta", max_usages=10)
        for discount, count in ((group_a, 1), (single, 2), (group_b, 3)):
            for _round in range(count):
                place_exhibition_order(event, [product], **CUSTOMER_DATA, discount=discount)
        for _round in range(3):
            _order(event, [product], discount=ExhibitionDiscount.objects.get(code="A2"))

    response = client.get(_url(event, "products.discounts"), {"ordering": "used"})

    assert [str(row) for row in response.context["discounts"]] == ["SINGLE", "B1", "A1"]
