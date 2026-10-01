import datetime as dt
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, LogEntry, Product
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionProductForm
from exhibition.models import (
    LOG_PRODUCT_ADDED,
    ExhibitionProduct,
    ExhibitionProductPurpose,
)


def _product(event, name="Gold Sponsor", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, **kwargs)


def _organizer(event, email="organizer@example.com", **flags):
    user = User.objects.create_user(email=email, password="secret", fullname="Organizer", locale="en")
    team = event.organizer.teams.create(name=email, all_events=True, **flags)
    team.members.add(user)
    return user


def _other_event(event, slug="other-event"):
    return Event.objects.create(
        organizer=event.organizer,
        name="Other Event",
        slug=slug,
        live=True,
        date_from=now(),
    )


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _api_url(event, **kwargs):
    name = "api-v1:exhibitionproduct-detail" if kwargs else "api-v1:exhibitionproduct-list"
    return reverse(name, kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs})


def _organizer_client(event, **flags):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = _organizer(event, **(flags or {"can_change_event_settings": True}))
    client = Client()
    client.force_login(user)
    return client


def _form_data(**overrides):
    data = {
        "name_0": "Gold Sponsor",
        "purpose": "sponsorship",
        "includes_booth": "on",
        "price": "1500.00",
        "active": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


# The booth rule


@pytest.mark.django_db
def test_sponsorship_product_includes_a_booth_by_default(event):
    with scopes_disabled():
        product = _product(event)

        assert product.purpose == ExhibitionProductPurpose.SPONSORSHIP
        assert product.includes_booth is True
        assert product.consumes_booth_capacity is True


@pytest.mark.django_db
def test_sponsorship_product_can_drop_the_booth(event):
    with scopes_disabled():
        product = _product(event, "Digital Sponsor", includes_booth=False)
        product.refresh_from_db()

        assert product.is_sponsorship is True
        assert product.includes_booth is False
        assert product.consumes_booth_capacity is False


@pytest.mark.django_db
def test_exhibition_product_always_includes_a_booth(event):
    with scopes_disabled():
        product = _product(event, "Standard Booth", purpose=ExhibitionProductPurpose.EXHIBITION, includes_booth=False)
        product.refresh_from_db()

        assert product.is_exhibition is True
        assert product.includes_booth is True
        assert product.consumes_booth_capacity is True


@pytest.mark.django_db
def test_only_booth_products_count_towards_booth_capacity(event):
    with scopes_disabled():
        booth = _product(event, "Premium Booth")
        digital = _product(event, "Digital Sponsor", includes_booth=False)
        _product(_other_event(event), "Someone else's booth")

        consuming = ExhibitionProduct.objects.for_event(event).consuming_booth_capacity()

        assert list(consuming) == [booth]
        assert digital not in consuming


# The model on its own


@pytest.mark.django_db
def test_exhibition_products_are_kept_apart_from_ticket_products(event):
    with scopes_disabled():
        ticket = Product.objects.create(event=event, name={"en": "Visitor Ticket"}, default_price=20)
        _product(event, "Gold Sponsor")

        assert list(Product.objects.filter(event=event)) == [ticket]
        assert [str(product) for product in ExhibitionProduct.objects.for_event(event)] == ["Gold Sponsor"]


@pytest.mark.django_db
def test_new_products_are_added_at_the_end(event):
    with scopes_disabled():
        first = _product(event, "Gold Sponsor")
        second = _product(event, "Silver Sponsor")
        elsewhere = _product(_other_event(event), "Other Gold")

        assert (first.position, second.position) == (0, 1)
        assert elsewhere.position == 0
        assert list(ExhibitionProduct.objects.for_event(event)) == [first, second]


@pytest.mark.django_db
def test_availability_follows_the_active_flag_and_the_sales_period(event):
    moment = now()
    with scopes_disabled():
        on_sale = _product(event, "On sale", available_from=moment - dt.timedelta(days=1))
        not_yet = _product(event, "Not yet", available_from=moment + dt.timedelta(days=1))
        over = _product(event, "Over", available_until=moment - dt.timedelta(days=1))
        inactive = _product(event, "Inactive", active=False)

    assert on_sale.is_available(moment) is True
    assert not_yet.is_available(moment) is False
    assert over.is_available(moment) is False
    assert inactive.is_available(moment) is False
    assert inactive.is_available_by_time(moment) is True


@pytest.mark.django_db
def test_available_products_query_agrees_with_is_available(event):
    moment = now()
    with scopes_disabled():
        _product(event, "Always")
        _product(event, "Starts now", available_from=moment)
        _product(event, "Ends now", available_until=moment)
        _product(event, "Not yet", available_from=moment + dt.timedelta(seconds=1))
        _product(event, "Over", available_until=moment - dt.timedelta(seconds=1))
        _product(event, "Inactive", active=False)
        products = list(ExhibitionProduct.objects.for_event(event))

        available = set(ExhibitionProduct.objects.for_event(event).available(moment))

    assert available == {product for product in products if product.is_available(moment)}
    assert {str(product) for product in available} == {"Always", "Starts now", "Ends now"}


# The form


@pytest.mark.django_db
def test_form_rejects_a_negative_price(event):
    with scopes_disabled():
        form = ExhibitionProductForm(data=_form_data(price="-1"), event=event)

        assert not form.is_valid()
        assert "price" in form.errors


@pytest.mark.django_db
def test_form_rejects_a_sales_period_that_ends_before_it_starts(event):
    with scopes_disabled():
        form = ExhibitionProductForm(
            data=_form_data(
                available_from_0="2026-10-10",
                available_from_1="10:00:00",
                available_until_0="2026-10-01",
                available_until_1="10:00:00",
            ),
            event=event,
        )

        assert not form.is_valid()
        assert "available_until" in form.errors


@pytest.mark.django_db
def test_form_saves_an_exhibition_product_with_a_booth_even_when_unticked(event):
    with scopes_disabled():
        form = ExhibitionProductForm(
            data=_form_data(name_0="Standard Booth", purpose="exhibition", includes_booth=None),
            event=event,
        )
        assert form.is_valid(), form.errors
        form.instance.event = event
        product = form.save()

        product.refresh_from_db()
        assert product.includes_booth is True


# The organizer pages


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_lists_only_this_events_exhibition_products(event):
    client = _organizer_client(event)
    with scopes_disabled():
        _product(event, "Gold Sponsor")
        _product(_other_event(event), "Someone else's booth")
        Product.objects.create(event=event, name={"en": "Visitor Ticket"}, default_price=20)

    response = client.get(_url(event, "products"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Gold Sponsor" in content
    assert "Someone else&#x27;s booth" not in content
    assert "Visitor Ticket" not in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_is_paginated_like_the_other_organizer_lists(event):
    client = _organizer_client(event)
    with scopes_disabled():
        for name in ("Gold Sponsor", "Silver Sponsor", "Bronze Sponsor"):
            _product(event, name)

    first_page = client.get(_url(event, "products") + "?page_size=2").content.decode()
    second_page = client.get(_url(event, "products") + "?page_size=2&page=2").content.decode()

    assert "Gold Sponsor" in first_page and "Silver Sponsor" in first_page
    assert "Bronze Sponsor" not in first_page
    assert "Bronze Sponsor" in second_page
    assert "Gold Sponsor" not in second_page


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_creates_a_product(event):
    client = _organizer_client(event)

    form_page = client.get(_url(event, "products.add"))
    assert form_page.status_code == 200
    assert "data-exhibition-product-booth" in form_page.content.decode()

    response = client.post(_url(event, "products.add"), _form_data(includes_booth=None))

    assert response.status_code == 302
    with scopes_disabled():
        product = ExhibitionProduct.objects.get(event=event)
        assert str(product) == "Gold Sponsor"
        assert product.price == Decimal("1500.00")
        assert product.includes_booth is False
        assert not Product.objects.filter(event=event).exists()
        assert LogEntry.objects.filter(action_type=LOG_PRODUCT_ADDED, object_id=product.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_edits_a_product(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor")

    assert client.get(_url(event, "products.edit", pk=product.pk)).status_code == 200

    response = client.post(
        _url(event, "products.edit", pk=product.pk),
        _form_data(name_0="Platinum Sponsor", price="2500.00"),
    )

    assert response.status_code == 302
    with scopes_disabled():
        product.refresh_from_db()
        assert str(product) == "Platinum Sponsor"
        assert product.price == Decimal("2500.00")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_deletes_a_product(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor")

    confirm_page = client.get(_url(event, "products.delete", pk=product.pk))
    assert confirm_page.status_code == 200
    assert "Gold Sponsor" in confirm_page.content.decode()

    response = client.post(_url(event, "products.delete", pk=product.pk))

    assert response.status_code == 302
    with scopes_disabled():
        assert not ExhibitionProduct.objects.filter(pk=product.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_of_another_event_cannot_be_changed(event):
    client = _organizer_client(event)
    with scopes_disabled():
        foreign = _product(_other_event(event), "Someone else's booth")

    assert client.get(_url(event, "products.edit", pk=foreign.pk)).status_code == 404
    assert client.post(_url(event, "products.edit", pk=foreign.pk), _form_data()).status_code == 404
    assert client.post(_url(event, "products.delete", pk=foreign.pk)).status_code == 404
    with scopes_disabled():
        foreign.refresh_from_db()
        assert str(foreign) == "Someone else's booth"


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_pages_need_the_exhibition_settings_permission(event):
    # The Tickets product permission alone is not enough: these are exhibition settings.
    client = _organizer_client(event, can_change_items=True)

    assert client.get(_url(event, "products")).status_code == 403
    assert client.get(_url(event, "products.add")).status_code == 403


# The API


@pytest.mark.django_db
def test_api_lists_this_events_products_with_their_exhibition_fields(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Digital Sponsor", includes_booth=False, price=Decimal("300.00"))
        _product(_other_event(event), "Someone else's booth")

    response = client.get(_api_url(event))

    assert response.status_code == 200
    results = response.json()["results"]
    assert [row["id"] for row in results] == [product.pk]
    assert results[0]["name"] == {"en": "Digital Sponsor"}
    assert results[0]["purpose"] == "sponsorship"
    assert results[0]["includes_booth"] is False
    assert results[0]["price"] == "300.00"
    assert set(results[0]) >= {"active", "available_from", "available_until", "position", "description"}


@pytest.mark.django_db
def test_api_leaves_out_products_that_are_not_on_sale(event):
    client = _organizer_client(event)
    moment = now()
    with scopes_disabled():
        on_sale = _product(event, "On sale")
        inactive = _product(event, "Inactive", active=False)
        not_yet = _product(event, "Not yet", available_from=moment + dt.timedelta(days=1))
        over = _product(event, "Over", available_until=moment - dt.timedelta(days=1))

    response = client.get(_api_url(event))

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["results"]] == [on_sale.pk]
    for hidden in (inactive, not_yet, over):
        assert client.get(_api_url(event, id=hidden.pk)).status_code == 404


@pytest.mark.django_db
def test_api_is_read_only(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor")

    assert client.post(_api_url(event), {"name": {"en": "New"}}, content_type="application/json").status_code == 405
    assert client.delete(_api_url(event, id=product.pk)).status_code == 405
    with scopes_disabled():
        assert ExhibitionProduct.objects.filter(pk=product.pk).exists()
