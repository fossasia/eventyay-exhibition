import datetime as dt
from decimal import Decimal

import pytest
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import LogEntry

from exhibition import mail as mail_helpers
from exhibition.models import (
    LOG_ORDER_PAYMENT_CHANGED,
    ExhibitionEmailQueue,
    ExhibitionOrder,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionProductPurpose,
    ExhibitionQuota,
)
from exhibition.orders import place_exhibition_order
from exhibition.signals import exhibition_presale_nav_tab

CUSTOMER_DATA = {"organization_name": "Acme Inc", "name": "Ada Lovelace", "email": "ada@example.com"}


def _product(event, name, price="0.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


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


def _checkout(client, event, *products):
    client.post(_url(event, "shop"), {"product": [product.pk for product in products]})
    return client.post(_url(event, "shop.checkout.details"), CUSTOMER_DATA)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_sales_page_lists_products_on_sale_with_their_booth(event):
    _enable_plugin(event)
    with scopes_disabled():
        _product(event, "Gold Sponsor", price="5000.00")
        _product(event, "Digital Sponsor", includes_booth=False)
        _product(event, "Hidden Package", active=False)
        _product(event, "Late Package", available_from=now() + dt.timedelta(days=3))
        _product(event, "Past Package", available_until=now() - dt.timedelta(days=3))

    content = Client().get(_url(event, "public_call")).content.decode()

    assert "Gold Sponsor" in content
    assert "Digital Sponsor" in content
    assert "Includes exhibition booth" in content
    assert "No exhibition booth" in content
    assert "Hidden Package" not in content
    assert "Late Package" not in content
    assert "Past Package" not in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_sold_out_product_is_shown_but_cannot_be_selected(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
        quota = ExhibitionQuota.objects.create(event=event, name="Booths", size=0, booth_capacity=True)
        quota.products.set([product])
    client = Client()

    assert "SOLD OUT" in client.get(_url(event, "public_call")).content.decode()

    response = client.post(_url(event, "shop"), {"product": [product.pk]})

    assert response.url == _packages_url(event)
    assert client.get(_url(event, "shop.checkout.details")).url == _packages_url(event)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_free_product_is_ordered_without_a_payment_step(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Community Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
    client = Client()

    response = _checkout(client, event, product)
    assert response.url == _url(event, "shop.checkout.confirm")

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_url(event, "shop.checkout.confirm"))

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.status == ExhibitionOrderStatus.PAID
        assert order.organization.is_exhibitor
        assert ExhibitionEmailQueue.objects.get(exhibition_order=order).role == mail_helpers.ORDER_PAID
    assert response.url == _url(event, "shop.order", code=order.code, secret=order.secret) + "?thanks=1"


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_paid_product_goes_through_payment_and_stays_pending(event, django_capture_on_commit_callbacks):
    _enable_plugin(event)
    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        product = _product(event, "Gold Sponsor", price="5000.00")
    client = Client()

    response = _checkout(client, event, product)
    assert response.url == _url(event, "shop.checkout.payment")
    assert client.get(_url(event, "shop.checkout.confirm")).url == _url(event, "shop.checkout.payment")

    response = client.post(_url(event, "shop.checkout.payment"), {"payment": "manual"})
    assert response.url == _url(event, "shop.checkout.confirm")
    assert "Gold Sponsor" in client.get(_url(event, "shop.checkout.confirm")).content.decode()

    with django_capture_on_commit_callbacks(execute=True):
        client.post(_url(event, "shop.checkout.confirm"))

    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)
        assert order.status == ExhibitionOrderStatus.PENDING
        assert order.payment_provider == "manual"
        assert order.organization is None
        assert ExhibitionEmailQueue.objects.get(exhibition_order=order).role == mail_helpers.ORDER_PLACED


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_missing_details_send_the_buyer_back_to_the_order_form(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Community Booth")
    client = Client()
    client.post(_url(event, "shop"), {"product": [product.pk]})

    response = client.post(_url(event, "shop.checkout.details"), {"name": "Ada"})

    assert response.status_code == 200
    assert client.get(_url(event, "shop.checkout.confirm")).url == _url(event, "shop.checkout.details")


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_that_sells_out_during_checkout_is_not_ordered(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
        quota = ExhibitionQuota.objects.create(event=event, name="Booths", size=1, booth_capacity=True)
        quota.products.set([product])
    client = Client()
    _checkout(client, event, product)
    with scopes_disabled():
        quota.size = 0
        quota.save()

    client.post(_url(event, "shop.checkout.confirm"))

    with scopes_disabled():
        assert not ExhibitionOrder.objects.filter(event=event).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_order_page_needs_the_secret(event):
    _enable_plugin(event)
    with scopes_disabled():
        product = _product(event, "Community Booth")
    client = Client()
    _checkout(client, event, product)
    client.post(_url(event, "shop.checkout.confirm"))
    with scopes_disabled():
        order = ExhibitionOrder.objects.get(event=event)

    assert client.get(_url(event, "shop.order", code=order.code, secret=order.secret)).status_code == 200
    assert client.get(_url(event, "shop.order", code=order.code, secret="wrong")).status_code == 404


@pytest.mark.django_db
def test_packages_on_sale_open_the_call_page_without_a_call(event):
    _enable_plugin(event)
    with scopes_disabled():
        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/call/" not in str(html)
    assert Client().get(_url(event, "public_call")).status_code == 404

    with scopes_disabled():
        _product(event, "Gold Sponsor")
        html = exhibition_presale_nav_tab(sender=event, request=RequestFactory().get("/x/"))
        assert "/exhibition/call/" in str(html)
        assert "/exhibition/packages/" not in str(html)
    content = Client().get(_url(event, "public_call")).content.decode()
    assert "Gold Sponsor" in content
    assert "Submit a request" not in content


@pytest.mark.django_db
def test_the_packages_address_leads_to_the_call_page(event):
    _enable_plugin(event)

    assert Client().get(_url(event, "shop")).url == _packages_url(event)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_pending_order_can_switch_its_payment_method(event):
    with scopes_disabled():
        event.plugins = "exhibition,eventyay.plugins.banktransfer"
        event.save()
    event.settings.set("payment_manual__enabled", True)
    event.settings.set("payment_banktransfer__enabled", True)
    event.settings.set("payment_banktransfer_bank_details", "Acme Bank")
    with scopes_disabled():
        order = place_exhibition_order(
            event,
            [_product(event, "Gold Sponsor", price="5000.00")],
            **CUSTOMER_DATA,
            payment_provider="manual",
        )
    client = Client()
    order_url = _url(event, "shop.order", code=order.code, secret=order.secret)
    change_url = _url(event, "shop.order.pay_change", code=order.code, secret=order.secret)

    assert "Re-try payment or choose another payment method" in client.get(order_url).content.decode()

    response = client.post(change_url, {"payment": "banktransfer"})

    assert response.url == order_url
    with scopes_disabled():
        order.refresh_from_db()
        assert order.payment_provider == "banktransfer"
        assert LogEntry.objects.filter(action_type=LOG_ORDER_PAYMENT_CHANGED, object_id=order.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_paid_order_cannot_switch_its_payment_method(event):
    _enable_plugin(event)
    event.settings.set("payment_manual__enabled", True)
    with scopes_disabled():
        order = place_exhibition_order(event, [_product(event, "Community Booth")], **CUSTOMER_DATA)
    client = Client()
    change_url = _url(event, "shop.order.pay_change", code=order.code, secret=order.secret)

    assert client.post(change_url, {"payment": "manual"}).status_code == 302
    with scopes_disabled():
        order.refresh_from_db()
        assert order.payment_provider == "free"
    assert client.get(_url(event, "shop.order.pay_change", code=order.code, secret="wrong")).status_code == 404
