from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import LogEntry
from eventyay.base.models.auth import User

from exhibition.models import (
    LOG_ORDER_APPROVED,
    LOG_ORDER_DENIED,
    ExhibitionOrderStateError,
    ExhibitionOrderStatus,
    ExhibitionProduct,
)
from exhibition.orders import ExhibitionOrderError, expire_overdue_orders, place_exhibition_order


def _product(event, name="Gold Sponsor", price="1500.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _enable_manual_payment(event):
    event.settings.set("payment_manual__enabled", True)


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


@pytest.mark.django_db
def test_public_order_with_approval_product_waits_for_approval(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event, require_approval=True)
        order = _order(event, product)

        assert order.require_approval is True
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.payment_provider == ""
        assert order.expires is None
        assert order.can_be_marked_paid is False
        assert order.can_be_approved is True

        with pytest.raises(ExhibitionOrderStateError):
            order.mark_paid(provider="manual")


@pytest.mark.django_db
def test_free_order_requiring_approval_is_not_auto_paid(event):
    with scopes_disabled():
        product = _product(event, "Invite-only Booth", price="0.00", require_approval=True)
        order = _order(event, product, payment_provider="")

        assert order.require_approval is True
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.can_be_marked_paid is False


@pytest.mark.django_db
def test_organizer_paid_booking_skips_approval(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event, require_approval=True)
        order = _order(event, product, paid=True, payment_provider="manual", check_availability=False)

        assert order.require_approval is False
        assert order.status == ExhibitionOrderStatus.PAID


@pytest.mark.django_db
def test_approve_paid_order_starts_payment_term(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        event.settings.set("payment_term_days", 10)
        product = _product(event, require_approval=True)
        order = _order(event, product)

        before = now()
        order.approve()
        after = now()
        order.refresh_from_db()

        assert order.require_approval is False
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.expires is not None
        assert before + timedelta(days=10) <= order.expires <= after + timedelta(days=10)
        assert order.can_be_marked_paid is True
        assert LogEntry.objects.filter(action_type=LOG_ORDER_APPROVED, object_id=order.pk).exists()


@pytest.mark.django_db
def test_paid_approval_order_needs_a_usable_payment_method(event):
    with scopes_disabled():
        product = _product(event, require_approval=True)
        with pytest.raises(ExhibitionOrderError):
            _order(event, product)


@pytest.mark.django_db
def test_approve_free_order_marks_it_paid(event):
    with scopes_disabled():
        product = _product(event, "Invite-only Booth", price="0.00", require_approval=True)
        order = _order(event, product, payment_provider="")

        order.approve()
        order.refresh_from_db()

        assert order.require_approval is False
        assert order.status == ExhibitionOrderStatus.PAID


@pytest.mark.django_db
def test_deny_cancels_order(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event, require_approval=True)
        order = _order(event, product)

        order.deny()
        order.refresh_from_db()

        assert order.require_approval is False
        assert order.status == ExhibitionOrderStatus.CANCELLED
        assert LogEntry.objects.filter(action_type=LOG_ORDER_DENIED, object_id=order.pk).exists()


@pytest.mark.django_db
def test_orders_awaiting_approval_do_not_expire(event):
    with scopes_disabled():
        _enable_manual_payment(event)
        product = _product(event, require_approval=True)
        order = _order(event, product)
        order.expires = now()
        order.save(update_fields=["expires"])

        assert expire_overdue_orders() == 0
        order.refresh_from_db()
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.require_approval is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_can_approve_and_deny_from_order_page(event):
    client = _organizer_client(event)
    with scopes_disabled():
        _enable_manual_payment(event)
        approve_product = _product(event, "Needs approval", require_approval=True)
        deny_product = _product(event, "Also needs approval", price="800.00", require_approval=True)
        to_approve = _order(event, approve_product)
        to_deny = _order(event, deny_product, email="bob@example.com", organization_name="Beta LLC")

    pending = client.get(_url(event, "orders.detail", code=to_approve.code))
    assert pending.status_code == 200
    assert b"Approve" in pending.content
    assert b"Deny" in pending.content

    response = client.post(_url(event, "orders.approve", code=to_approve.code))
    assert response.status_code == 302
    to_approve.refresh_from_db()
    assert to_approve.require_approval is False
    assert to_approve.status == ExhibitionOrderStatus.PENDING

    response = client.post(_url(event, "orders.deny", code=to_deny.code))
    assert response.status_code == 302
    to_deny.refresh_from_db()
    assert to_deny.status == ExhibitionOrderStatus.CANCELLED

    detail = client.get(_url(event, "orders.detail", code=to_approve.code))
    assert detail.status_code == 200
    assert b"Approve" not in detail.content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_product_form_exposes_require_approval(event):
    client = _organizer_client(event)
    response = client.get(_url(event, "products.add"))
    assert response.status_code == 200
    assert b"require_approval" in response.content
    assert b"Buying this product requires approval" in response.content
