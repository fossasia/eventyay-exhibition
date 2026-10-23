from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models import LogEntry
from eventyay.base.models.auth import User

from exhibition.models import (
    LOG_QUOTA_ADDED,
    ExhibitionOrder,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionProductPurpose,
    ExhibitionQuota,
    ExhibitionQuotaExceeded,
)
from exhibition.orders import place_exhibition_order


def _product(event, name, price="1000.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _quota(event, name, size, *products, booth_capacity=False):
    quota = ExhibitionQuota.objects.create(event=event, name=name, size=size, booth_capacity=booth_capacity)
    quota.products.set(products)
    return quota


def _order(event, *products):
    return place_exhibition_order(
        event, products, name="Ada Lovelace", email="ada@example.com", payment_provider="manual"
    )


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


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


@pytest.fixture
def shop(event):
    with scopes_disabled():
        event.settings.set("payment_manual__enabled", True)
        booth = _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
        gold = _product(event, "Gold Sponsor")
        digital = _product(event, "Digital Sponsor", includes_booth=False)
    return {"booth": booth, "gold": gold, "digital": digital}


# Enforcement


@pytest.mark.django_db
def test_products_sell_until_their_quota_is_used_up(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", 2, shop["booth"], shop["gold"], booth_capacity=True)
        _order(event, shop["booth"])
        _order(event, shop["gold"])

        with pytest.raises(ExhibitionQuotaExceeded):
            _order(event, shop["booth"])
        assert ExhibitionOrder.objects.count() == 2


@pytest.mark.django_db
def test_one_order_cannot_take_more_than_is_left(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", 1, shop["booth"], shop["gold"], booth_capacity=True)

        with pytest.raises(ExhibitionQuotaExceeded):
            _order(event, shop["booth"], shop["gold"])
        assert not ExhibitionOrder.objects.exists()


@pytest.mark.django_db
def test_products_without_a_booth_do_not_use_booth_capacity(event, shop):
    with scopes_disabled():
        booths = _quota(event, "Booths", 1, shop["booth"], booth_capacity=True)
        for _ in range(3):
            _order(event, shop["digital"])

        _order(event, shop["booth"])

        assert booths.count_used() == 1


@pytest.mark.django_db
def test_a_shared_plain_quota_counts_both_product_kinds(event, shop):
    with scopes_disabled():
        gold_level = _quota(event, "Gold level", 2, shop["gold"], shop["digital"])
        _order(event, shop["gold"])
        _order(event, shop["digital"])

        with pytest.raises(ExhibitionQuotaExceeded):
            _order(event, shop["digital"])
        assert gold_level.count_used() == 2


@pytest.mark.django_db
def test_a_product_on_several_quotas_needs_room_in_all_of_them(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", 10, shop["gold"], booth_capacity=True)
        _quota(event, "Gold level", 1, shop["gold"])
        _order(event, shop["gold"])

        with pytest.raises(ExhibitionQuotaExceeded):
            _order(event, shop["gold"])


@pytest.mark.django_db
def test_an_unlimited_quota_never_blocks_a_sale(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", None, shop["booth"], booth_capacity=True)
        for _ in range(5):
            _order(event, shop["booth"])

        assert ExhibitionOrder.objects.count() == 5


@pytest.mark.django_db
def test_cancelled_and_expired_orders_release_their_capacity(event, shop):
    with scopes_disabled():
        booths = _quota(event, "Booths", 1, shop["booth"], booth_capacity=True)
        _order(event, shop["booth"]).cancel()
        _order(event, shop["booth"]).expire()

        _order(event, shop["booth"])

        assert booths.count_used() == 1


@pytest.mark.django_db
def test_an_expired_order_can_be_paid_while_its_quota_has_room(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", 1, shop["booth"], booth_capacity=True)
        order = _order(event, shop["booth"])
        order.expire()

        order.mark_paid()

        assert ExhibitionOrder.objects.get(pk=order.pk).status == ExhibitionOrderStatus.PAID


@pytest.mark.django_db
def test_an_expired_order_cannot_be_paid_once_its_place_was_sold(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", 1, shop["booth"], booth_capacity=True)
        expired = _order(event, shop["booth"])
        expired.expire()
        _order(event, shop["booth"])

        with pytest.raises(ExhibitionQuotaExceeded):
            expired.mark_paid()
        assert ExhibitionOrder.objects.get(pk=expired.pk).status == ExhibitionOrderStatus.EXPIRED


@pytest.mark.django_db
def test_usage_and_remaining_capacity_are_reported(event, shop):
    with scopes_disabled():
        _quota(event, "Booths", 3, shop["booth"], booth_capacity=True)
        _order(event, shop["booth"])
        _order(event, shop["booth"]).cancel()

        quota = ExhibitionQuota.objects.with_usage().get()

        assert quota.used == 1
        assert quota.remaining == 2
        assert shop["booth"].remaining_capacity() == 2
        assert shop["digital"].remaining_capacity() is None


# Organizer pages


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_creates_a_booth_capacity_quota(event, shop):
    client = _organizer_client(event)

    response = client.post(
        _url(event, "products.quotas.add"),
        {"name": "Booths", "size": "20", "booth_capacity": "on", "products": [shop["booth"].pk, shop["gold"].pk]},
    )

    assert response.status_code == 302
    with scopes_disabled():
        quota = ExhibitionQuota.objects.get(event=event)
        assert quota.booth_capacity
        assert set(quota.products.all()) == {shop["booth"], shop["gold"]}
        assert LogEntry.objects.filter(action_type=LOG_QUOTA_ADDED, object_id=quota.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_booth_capacity_quota_rejects_products_without_a_booth(event, shop):
    client = _organizer_client(event)

    response = client.post(
        _url(event, "products.quotas.add"),
        {"name": "Booths", "size": "20", "booth_capacity": "on", "products": [shop["booth"].pk, shop["digital"].pk]},
    )

    assert response.status_code == 200
    assert "products" in response.context["form"].errors
    with scopes_disabled():
        assert not ExhibitionQuota.objects.exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_plain_quota_takes_any_product(event, shop):
    client = _organizer_client(event)

    response = client.post(
        _url(event, "products.quotas.add"),
        {"name": "Gold level", "size": "3", "products": [shop["gold"].pk, shop["digital"].pk]},
    )

    assert response.status_code == 302


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_on_a_booth_quota_cannot_drop_its_booth(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        _quota(event, "Booths", 20, shop["gold"], booth_capacity=True)

    response = client.post(
        _url(event, "products.edit", pk=shop["gold"].pk),
        {
            "name_0": "Gold Sponsor",
            "purpose": ExhibitionProductPurpose.SPONSORSHIP,
            "price": "1000.00",
            "active": "on",
        },
    )

    assert response.status_code == 200
    assert "includes_booth" in response.context["form"].errors
    with scopes_disabled():
        assert ExhibitionProduct.objects.get(pk=shop["gold"].pk).includes_booth


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_quotas_page_lists_usage(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        _quota(event, "Booths", 2, shop["booth"], booth_capacity=True)
        _order(event, shop["booth"])

    response = client.get(_url(event, "products.quotas"))

    assert response.status_code == 200
    quota = response.context["quotas"][0]
    assert (quota.used, quota.remaining) == (1, 1)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_shows_booths_left_and_unlimited_booth_products(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        _quota(event, "Booths", 5, shop["booth"], booth_capacity=True)
        _order(event, shop["booth"])

    response = client.get(_url(event, "products"))

    content = response.content.decode()
    assert "4 of 5 booths left" in content
    assert response.context["booth_products_without_quota"] == 1


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_marking_a_sold_out_expired_order_paid_shows_an_error(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        _quota(event, "Booths", 1, shop["booth"], booth_capacity=True)
        expired = _order(event, shop["booth"])
        expired.expire()
        _order(event, shop["booth"])

    response = client.post(_url(event, "orders.mark_paid", code=expired.code), follow=True)

    assert response.status_code == 200
    with scopes_disabled():
        assert ExhibitionOrder.objects.get(pk=expired.pk).status == ExhibitionOrderStatus.EXPIRED
