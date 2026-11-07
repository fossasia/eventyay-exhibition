from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionOrder,
    ExhibitionOrderQuestion,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionQuestionVariant,
    ExhibitionQuota,
)
from exhibition.orders import place_exhibition_order, send_order_email

CUSTOMER_DATA = {"organization_name": "Acme Inc", "name": "Ada Lovelace", "email": "ada@example.com"}


def _product(event, name, price="0.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _organizer_client(event, can_change_orders=True):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = User.objects.create_user(
            email="organizer@example.com", password="secret", fullname="Organizer", locale="en"
        )
        team = event.organizer.teams.create(
            name="Organizers",
            all_events=True,
            can_view_orders=True,
            can_change_orders=can_change_orders,
        )
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_a_free_order_is_confirmed_and_emailed(event, django_capture_on_commit_callbacks):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        product = _product(event, "Community Booth")
        with django_capture_on_commit_callbacks(execute=True):
            order = place_exhibition_order(event, [product], **CUSTOMER_DATA)
            send_order_email(order)

        assert order.status == ExhibitionOrderStatus.PAID
        assert order.payment_provider == "free"
        assert order.organization is not None
        email = ExhibitionEmailQueue.objects.get(exhibition_order=order)
        assert email.role == mail_helpers.ORDER_PAID
        assert email.sent_at is not None
        assert order.code in email.subject
        assert "Community Booth" in email.body
        assert order.secret in email.body


@pytest.mark.django_db
def test_an_organiser_can_book_an_inactive_product_as_paid(event):
    with scopes_disabled():
        product = _product(event, "Negotiated Gold", price="9000.00", active=False)
        order = place_exhibition_order(
            event,
            [product],
            **CUSTOMER_DATA,
            check_availability=False,
            paid=True,
            payment_reference="Contract 42",
        )

        assert order.status == ExhibitionOrderStatus.PAID
        assert order.payment_reference == "Contract 42"
        assert order.expires is None
        assert order.organization.is_sponsor


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_back_office_form_creates_a_pending_order(event, django_capture_on_commit_callbacks):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Silver", price="2000.00")
        question = ExhibitionOrderQuestion.objects.create(
            event=event, question={"en": "Booth power"}, variant=ExhibitionQuestionVariant.STRING, required=True
        )
        question.products.set([product])

    response = client.get(_url(event, "orders.add"), {"products": [product.pk]})
    assert "Booth power" in response.content.decode()

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _url(event, "orders.add"),
            {**CUSTOMER_DATA, "products": [product.pk], "payment_status": "pending", "send_email": "on"},
        )

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.organization is None
        assert ExhibitionEmailQueue.objects.get(exhibition_order=order).role == mail_helpers.ORDER_PLACED
    assert response.url == _url(event, "orders.detail", code=order.code)
    assert order.code in client.get(_url(event, "orders")).content.decode()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_back_office_form_still_respects_quotas(event):
    client = _organizer_client(event)
    with scopes_disabled():
        product = _product(event, "Booth", price="100.00")
        quota = ExhibitionQuota.objects.create(event=event, name="Booths", size=0, booth_capacity=True)
        quota.products.set([product])

    client.post(_url(event, "orders.add"), {**CUSTOMER_DATA, "products": [product.pk], "payment_status": "paid"})

    with scopes_disabled():
        assert not ExhibitionOrder.objects.filter(event=event).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_creating_orders_needs_the_change_orders_permission(event):
    client = _organizer_client(event, can_change_orders=False)

    assert client.get(_url(event, "orders.add")).status_code == 403


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_order_link_can_be_resent(event, django_capture_on_commit_callbacks):
    client = _organizer_client(event)
    with scopes_disabled():
        order = place_exhibition_order(event, [_product(event, "Community Booth")], **CUSTOMER_DATA)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_url(event, "orders.resend", code=order.code))

    assert response.url == _url(event, "orders.detail", code=order.code)
    with scopes_disabled():
        email = ExhibitionEmailQueue.objects.get(exhibition_order=order)
        assert email.role == mail_helpers.ORDER_PAID
        assert order.secret in email.body
