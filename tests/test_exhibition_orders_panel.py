from decimal import Decimal

import pytest
from django.urls import reverse
from eventyay.base.models import Team
from eventyay.base.models.auth import User

from exhibition.models import (
    ExhibitionOrder,
    ExhibitionOrderAnswer,
    ExhibitionOrderPosition,
    ExhibitionOrderQuestion,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitorInfo,
    ExhibitorSettings,
)


def _setup(client, event, settings):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
    )
    user = User.objects.create_superuser("admin@dummy.dummy", "dummy")
    team = Team.objects.create(
        organizer=event.organizer,
        all_events=True,
        can_change_event_settings=True,
    )
    team.members.add(user)
    client.force_login(user)
    return user


def _edit_url(event, exhibitor):
    return reverse(
        "plugins:exhibition:edit",
        kwargs={
            "organizer": event.organizer.slug,
            "event": event.slug,
            "pk": exhibitor.pk,
        },
    )


def _order_detail_url(event, order):
    return reverse(
        "plugins:exhibition:orders.detail",
        kwargs={
            "organizer": event.organizer.slug,
            "event": event.slug,
            "code": order.code,
        },
    )


def _product(event, name="Gold Sponsor", price="1500.00"):
    return ExhibitionProduct.objects.create(
        event=event,
        name={"en": name},
        price=Decimal(price),
    )


def _order(
    event, exhibitor, *, name="Ada Lovelace", email="ada@example.com", status=ExhibitionOrderStatus.PAID, **kwargs
):
    return ExhibitionOrder.objects.create(
        event=event,
        organization=exhibitor,
        name=name,
        email=email,
        total=Decimal("0.00"),
        status=status,
        **kwargs,
    )


@pytest.mark.django_db
def test_panel_not_shown_when_no_orders(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")

    response = client.get(_edit_url(event, exhibitor))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Exhibition orders" not in content


@pytest.mark.django_db
def test_panel_shown_with_order(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    order = _order(event, exhibitor)

    response = client.get(_edit_url(event, exhibitor))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Exhibition orders" in content
    assert order.code in content


@pytest.mark.django_db
def test_panel_uses_3_9_grid_not_dl_horizontal(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    _order(event, exhibitor)

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "col-md-3" in content
    assert "col-md-9" in content
    assert "dl-horizontal" not in content


@pytest.mark.django_db
def test_panel_shows_products(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    order = _order(event, exhibitor)
    product = _product(event, "Gold Sponsor")
    ExhibitionOrderPosition.for_product(order, product).save()

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "Products" in content
    assert "Gold Sponsor" in content


@pytest.mark.django_db
def test_panel_shows_contact_person(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    _order(event, exhibitor, name="Ada Lovelace", email="ada@example.com")

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "Contact person" in content
    assert "Ada Lovelace" in content
    assert "ada@example.com" in content


@pytest.mark.django_db
def test_panel_shows_phone_when_present(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    _order(event, exhibitor, phone="+1-555-0199")

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "Phone number" in content
    assert "+1-555-0199" in content


@pytest.mark.django_db
def test_source_order_shows_marker(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    order = _order(event, exhibitor)
    exhibitor.source_order = order
    exhibitor.save(update_fields=["source_order"])

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "Profile created from this order" in content


@pytest.mark.django_db
def test_non_source_order_has_no_marker(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    order1 = _order(event, exhibitor, name="First", email="first@example.com")
    order2 = _order(event, exhibitor, name="Second", email="second@example.com")
    exhibitor.source_order = order1
    exhibitor.save(update_fields=["source_order"])

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert order1.code in content
    assert order2.code in content
    assert content.count("Profile created from this order") == 1


@pytest.mark.django_db
def test_order_code_links_to_order_detail(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    order = _order(event, exhibitor)

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    expected_url = _order_detail_url(event, order)
    assert f'href="{expected_url}"' in content


@pytest.mark.django_db
def test_panel_shows_form_answers(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    order = _order(event, exhibitor)
    product = _product(event)
    ExhibitionOrderPosition.for_product(order, product).save()
    question = ExhibitionOrderQuestion.objects.create(
        event=event,
        question={"en": "What is your booth size?"},
        position=0,
    )
    question.products.add(product)
    ExhibitionOrderAnswer.objects.create(
        question=question,
        order=order,
        answer="Large",
    )

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "What is your booth size?" in content
    assert "Large" in content


@pytest.mark.django_db
def test_panel_status_badges(client, event, settings):
    _setup(client, event, settings)
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme Corp")
    _order(event, exhibitor, status=ExhibitionOrderStatus.PAID)

    response = client.get(_edit_url(event, exhibitor))

    content = response.content.decode()
    assert "label-success" in content
