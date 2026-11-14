import datetime as dt
from decimal import Decimal
from io import BytesIO

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User
from pypdf import PdfReader

from exhibition import mail as mail_helpers
from exhibition.invoice import render_order_invoice_pdf
from exhibition.models import ExhibitionOrder, ExhibitionOrderStatus, ExhibitionProduct
from exhibition.orders import place_exhibition_order


def _url(event, name, **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def _paid_order(event, product_name="Gold Sponsor", **kwargs):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        event.settings.set("payment_manual__enabled", True)
        product = ExhibitionProduct.objects.create(event=event, name={"en": product_name}, price=Decimal("1500.00"))
        kwargs.setdefault("name", "Ada Lovelace")
        kwargs.setdefault("email", "ada@example.com")
        kwargs.setdefault("organization_name", "Acme Inc")
        kwargs.setdefault("payment_provider", "manual")
        order = place_exhibition_order(event, [product], **kwargs)
        order.mark_paid(provider="manual", reference="REF-1")
    return order


def _pdf_text(content):
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)


def _organizer_client(event, **permissions):
    with scopes_disabled():
        user = User.objects.create_user(email="organizer@example.com", password="secret", fullname="O", locale="en")
        team = event.organizer.teams.create(name="Team", all_events=True, **permissions)
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_invoice_lists_the_order_the_buyer_and_the_total(event):
    order = _paid_order(event, billing_address="1 Main Street\nBerlin", vat_id="DE123456789")

    with scopes_disabled():
        text = _pdf_text(render_order_invoice_pdf(order))

    for expected in (order.code, "Acme Inc", "Ada Lovelace", "1 Main Street", "Berlin", "DE123456789", "Gold Sponsor"):
        assert expected in text
    assert "1,500.00" in text


@pytest.mark.django_db
def test_the_invoice_dates_follow_the_event_timezone(event):
    order = _paid_order(event)
    with scopes_disabled():
        event.settings.set("timezone", "America/Los_Angeles")
        just_after_midnight_utc = dt.datetime(2026, 11, 1, 0, 30, tzinfo=dt.UTC)
        ExhibitionOrder.objects.filter(pk=order.pk).update(
            created=just_after_midnight_utc, payment_date=just_after_midnight_utc
        )
        order.refresh_from_db()
        text = _pdf_text(render_order_invoice_pdf(order))

    assert "Order date: 2026-10-31" in text
    assert "Payment date: 2026-10-31" in text
    assert "2026-11-01" not in text


@pytest.mark.django_db
def test_markup_and_foreign_characters_in_order_data_do_not_break_the_invoice(event):
    order = _paid_order(
        event,
        product_name="<b>Booth</b> & Шатёр",
        organization_name="Müller <script>&</script> GmbH",
    )

    with scopes_disabled():
        text = _pdf_text(render_order_invoice_pdf(order))

    assert "<b>Booth</b> & Шатёр" in text
    assert "Müller <script>&</script> GmbH" in text


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_buyer_downloads_the_invoice_of_a_paid_order(event):
    order = _paid_order(event)
    client = Client()
    order_url = _url(event, "shop.order", code=order.code, secret=order.secret)
    invoice_url = _url(event, "shop.order.invoice", code=order.code, secret=order.secret)

    assert invoice_url in client.get(order_url).content.decode()
    response = client.get(invoice_url)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == f'attachment; filename="invoice-{order.code}.pdf"'
    assert response.content.startswith(b"%PDF")
    assert order.code in _pdf_text(response.content)


@pytest.mark.django_db
def test_the_invoice_link_needs_the_order_secret(event):
    order = _paid_order(event)

    response = Client().get(_url(event, "shop.order.invoice", code=order.code, secret="wrong"))

    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [ExhibitionOrderStatus.PENDING, ExhibitionOrderStatus.CANCELLED, ExhibitionOrderStatus.EXPIRED],
)
def test_orders_that_are_not_paid_have_no_invoice(event, status):
    order = _paid_order(event)
    with scopes_disabled():
        order.status = status
        order.save()
    client = Client()

    page = client.get(_url(event, "shop.order", code=order.code, secret=order.secret))
    response = client.get(_url(event, "shop.order.invoice", code=order.code, secret=order.secret))

    assert "invoice/" not in page.content.decode()
    assert response.status_code == 302
    assert response["Location"] == _url(event, "shop.order", code=order.code, secret=order.secret)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_organizer_downloads_the_invoice_of_a_paid_order(event):
    order = _paid_order(event)
    client = _organizer_client(event, can_view_orders=True)
    invoice_url = _url(event, "orders.invoice", code=order.code)

    assert invoice_url in client.get(_url(event, "orders.detail", code=order.code)).content.decode()
    response = client.get(invoice_url)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == f'attachment; filename="invoice-{order.code}.pdf"'
    assert order.code in _pdf_text(response.content)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_organizer_gets_no_invoice_for_an_unpaid_order(event):
    order = _paid_order(event)
    with scopes_disabled():
        order.status = ExhibitionOrderStatus.CANCELLED
        order.save()
    client = _organizer_client(event, can_view_orders=True)

    page = client.get(_url(event, "orders.detail", code=order.code))
    response = client.get(_url(event, "orders.invoice", code=order.code))

    assert _url(event, "orders.invoice", code=order.code) not in page.content.decode()
    assert response.status_code == 302
    assert response["Location"] == _url(event, "orders.detail", code=order.code)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_invoice_needs_permission_to_view_orders(event):
    order = _paid_order(event)
    client = _organizer_client(event, can_change_vouchers=True)

    response = client.get(_url(event, "orders.invoice", code=order.code))

    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_organizer_cannot_open_an_invoice_of_another_event(event):
    order = _paid_order(event)
    with scopes_disabled():
        other = event.organizer.events.create(
            name="Other", slug="other", date_from=event.date_from, plugins="exhibition"
        )
    client = _organizer_client(other, can_view_orders=True)

    response = client.get(_url(other, "orders.invoice", code=order.code))

    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_invoice_placeholder_links_to_the_buyers_invoice(event):
    order = _paid_order(event)

    with scopes_disabled():
        url = mail_helpers.order_invoice_url(order)

    assert url == "https://testserver" + _url(event, "shop.order.invoice", code=order.code, secret=order.secret)


def test_the_confirmation_email_offers_the_invoice():
    _subject, body = mail_helpers.DEFAULT_TEMPLATE_SOURCES[mail_helpers.ORDER_PAID]

    assert "{order_invoice_url}" in body
    assert "{order_invoice_url}" not in mail_helpers.DEFAULT_TEMPLATE_SOURCES[mail_helpers.ORDER_PLACED][1]
