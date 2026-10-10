import datetime as dt
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, LogEntry
from eventyay.base.models.auth import User

from exhibition.models import (
    LOG_ORDER_EXPIRED,
    LOG_ORDER_PAID,
    LOG_ORDER_PLACED,
    ExhibitionOrder,
    ExhibitionOrderStateError,
    ExhibitionOrderStatus,
    ExhibitionProduct,
)
from exhibition.orders import (
    ExhibitionOrderError,
    expire_overdue_orders,
    paid_products_lack_payment_method,
    place_exhibition_order,
    usable_payment_providers,
)
from exhibition.signals import exhibition_logentry_object_link, expire_overdue_exhibition_orders


def _product(event, name="Gold Sponsor", price="1500.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _enable_manual_payment(event):
    event.settings.set("payment_manual__enabled", True)


def _order(event, *products, **kwargs):
    kwargs.setdefault("name", "Ada Lovelace")
    kwargs.setdefault("email", "ada@example.com")
    kwargs.setdefault("payment_provider", "manual")
    return place_exhibition_order(event, products, **kwargs)


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


# Payment methods


@pytest.mark.django_db
def test_only_enabled_supported_payment_methods_are_usable(event):
    with scopes_disabled():
        event.settings.set("payment_boxoffice__enabled", True)
        assert usable_payment_providers(event) == {}

        _enable_manual_payment(event)
        assert list(usable_payment_providers(event)) == ["manual"]


@pytest.mark.django_db
def test_paid_products_without_a_usable_payment_method_are_flagged(event):
    with scopes_disabled():
        _product(event, "Community Booth", price="0.00")
        assert paid_products_lack_payment_method(event) is False

        _product(event, "Gold Sponsor", price="1500.00")
        assert paid_products_lack_payment_method(event) is True

        _enable_manual_payment(event)
        assert paid_products_lack_payment_method(event) is False


# Placing orders


@pytest.mark.django_db
def test_an_order_records_its_products_prices_total_and_contact(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        gold = _product(event, "Gold Sponsor", price="1500.00")
        booth = _product(event, "Standard Booth", price="250.50", purpose="exhibition")

        order = _order(event, gold, booth, phone="+44 20 7946 0000", answers={"vat_id": "GB123"})

        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.total == Decimal("1750.50")
        assert order.currency == event.currency
        assert (order.name, order.email, order.phone) == ("Ada Lovelace", "ada@example.com", "+44 20 7946 0000")
        assert order.answers == {"vat_id": "GB123"}
        assert order.payment_provider == "manual"
        assert [(position.product, position.price) for position in order.positions.all()] == [
            (gold, Decimal("1500.00")),
            (booth, Decimal("250.50")),
        ]
        assert LogEntry.objects.filter(action_type=LOG_ORDER_PLACED, object_id=order.pk).exists()


@pytest.mark.django_db
def test_positions_keep_the_price_at_the_time_of_purchase(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        gold = _product(event, price="1500.00")
        order = _order(event, gold)

        gold.price = Decimal("2000.00")
        gold.save()

        assert order.positions.get().price == Decimal("1500.00")
        assert ExhibitionOrder.objects.get(pk=order.pk).total == Decimal("1500.00")


@pytest.mark.django_db
def test_order_codes_are_generated_and_unique_per_event(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        gold = _product(event)
        codes = {_order(event, gold).code for _ in range(5)}

    assert len(codes) == 5
    assert all(len(code) == 5 for code in codes)


@pytest.mark.django_db
def test_a_pending_order_expires_after_the_payment_term(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        event.settings.set("payment_term_days", 7)
        before = now()
        order = _order(event, _product(event))

    assert before + dt.timedelta(days=7) <= order.expires <= now() + dt.timedelta(days=7)


@pytest.mark.django_db
def test_a_free_order_is_paid_at_once_without_a_payment_method(event):
    with scopes_disabled():
        order = _order(event, _product(event, "Community Booth", price="0.00"), payment_provider="")

        assert order.status == ExhibitionOrderStatus.PAID
        assert order.payment_provider == "free"
        assert order.expires is None
        assert order.payment_date is not None


@pytest.mark.django_db
def test_a_paid_order_needs_a_usable_payment_method(event):
    with scopes_disabled():
        gold = _product(event)

        with pytest.raises(ExhibitionOrderError):
            _order(event, gold)

        _enable_manual_payment(event)
        with pytest.raises(ExhibitionOrderError):
            _order(event, gold, payment_provider="stripe")

        assert not ExhibitionOrder.objects.exists()


@pytest.mark.django_db
def test_products_that_are_not_on_sale_cannot_be_ordered(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        inactive = _product(event, "Inactive", active=False)
        over = _product(event, "Over", available_until=now() - dt.timedelta(days=1))
        foreign = _product(_other_event(event), "Elsewhere")

        for product in (inactive, over, foreign):
            with pytest.raises(ExhibitionOrderError):
                _order(event, product)
        with pytest.raises(ExhibitionOrderError):
            _order(event)

        assert not ExhibitionOrder.objects.exists()


# Status changes


@pytest.mark.django_db
def test_a_pending_order_can_be_marked_paid(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        order = _order(event, _product(event))

        order.mark_paid(reference="TRANSFER-4711")
        order.refresh_from_db()

        assert order.status == ExhibitionOrderStatus.PAID
        assert order.payment_provider == "manual"
        assert order.payment_reference == "TRANSFER-4711"
        assert order.payment_date is not None
        assert LogEntry.objects.filter(action_type=LOG_ORDER_PAID, object_id=order.pk).exists()


@pytest.mark.django_db
def test_an_expired_order_can_still_be_paid(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        order = _order(event, _product(event))

        order.expire()
        order.mark_paid()

        assert ExhibitionOrder.objects.get(pk=order.pk).status == ExhibitionOrderStatus.PAID


@pytest.mark.django_db
def test_pending_and_paid_orders_can_be_cancelled(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event)
        pending = _order(event, product)
        paid = _order(event, product)
        paid.mark_paid()

        pending.cancel()
        paid.cancel()

        assert {order.status for order in ExhibitionOrder.objects.all()} == {ExhibitionOrderStatus.CANCELLED}


@pytest.mark.django_db
def test_impossible_status_changes_are_refused(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event)
        paid = _order(event, product)
        paid.mark_paid()
        cancelled = _order(event, product)
        cancelled.cancel()

        with pytest.raises(ExhibitionOrderStateError):
            paid.expire()
        with pytest.raises(ExhibitionOrderStateError):
            paid.mark_paid()
        with pytest.raises(ExhibitionOrderStateError):
            cancelled.mark_paid()
        with pytest.raises(ExhibitionOrderStateError):
            cancelled.cancel()

        assert ExhibitionOrder.objects.get(pk=paid.pk).status == ExhibitionOrderStatus.PAID
        assert ExhibitionOrder.objects.get(pk=cancelled.pk).status == ExhibitionOrderStatus.CANCELLED


@pytest.mark.django_db
def test_only_overdue_pending_orders_are_expired(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event)
        overdue = _order(event, product)
        current = _order(event, product)
        paid = _order(event, product)
        paid.mark_paid()
        ExhibitionOrder.objects.filter(pk__in=[overdue.pk, paid.pk]).update(expires=now() - dt.timedelta(hours=1))

        assert expire_overdue_orders() == 1

        statuses = dict(ExhibitionOrder.objects.values_list("pk", "status"))
        assert LogEntry.objects.filter(action_type=LOG_ORDER_EXPIRED, object_id=overdue.pk).exists()

    assert statuses == {
        overdue.pk: ExhibitionOrderStatus.EXPIRED,
        current.pk: ExhibitionOrderStatus.PENDING,
        paid.pk: ExhibitionOrderStatus.PAID,
    }


@pytest.mark.django_db
def test_the_periodic_task_expires_overdue_orders(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        order = _order(event, _product(event))
        ExhibitionOrder.objects.filter(pk=order.pk).update(expires=now() - dt.timedelta(hours=1))

    expire_overdue_exhibition_orders(sender=None)

    with scopes_disabled():
        assert ExhibitionOrder.objects.get(pk=order.pk).status == ExhibitionOrderStatus.EXPIRED


@pytest.mark.django_db
def test_order_log_entries_name_the_order(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        order = _order(event, _product(event))
        entry = LogEntry.objects.get(action_type=LOG_ORDER_PLACED, object_id=order.pk)

        assert order.code in exhibition_logentry_object_link(sender=event, logentry=entry)


# The organizer pages


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_warns_when_paid_products_have_no_payment_method(event):
    client = _organizer_client(event)
    with scopes_disabled():
        _product(event)
    warning = "no payment method that exhibition orders can"

    assert warning in client.get(_url(event, "products")).content.decode()

    with scopes_disabled():
        _enable_manual_payment(event)
    assert warning not in client.get(_url(event, "products")).content.decode()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_ordered_product_cannot_be_deleted(event):
    client = _organizer_client(event)
    with scopes_disabled():
        _enable_manual_payment(event)
        gold = _product(event)
        _order(event, gold)

    confirm_page = client.get(_url(event, "products.delete", pk=gold.pk)).content.decode()
    response = client.post(_url(event, "products.delete", pk=gold.pk))

    assert "has been ordered, so it cannot be deleted" in confirm_page
    assert response.status_code == 302
    with scopes_disabled():
        assert ExhibitionProduct.objects.filter(pk=gold.pk).exists()
