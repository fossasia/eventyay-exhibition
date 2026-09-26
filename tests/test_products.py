import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, Product, Quota
from eventyay.base.models.auth import User

from exhibition.models import ExhibitionProduct, ExhibitionProductPurpose
from exhibition.utils import mixed_booth_quotas


def _product(event, name, **kwargs):
    return Product.objects.create(event=event, name={"en": name}, default_price=100, **kwargs)


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


def _formset_payload(*rows):
    """The table posts one formset row per product, so tests speak the same language."""
    data = {
        "form-TOTAL_FORMS": str(len(rows)),
        "form-INITIAL_FORMS": str(len(rows)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        for name, value in row.items():
            data[f"form-{index}-{name}"] = value
    return data


def _products_url(event):
    return reverse(
        "plugins:exhibition:products",
        kwargs={"organizer": event.organizer.slug, "event": event.slug},
    )


@pytest.mark.django_db
def test_sponsorship_product_includes_a_booth_by_default(event):
    with scopes_disabled():
        role = ExhibitionProduct.objects.create(product=_product(event, "Gold Sponsor"))

        assert role.purpose == ExhibitionProductPurpose.SPONSORSHIP
        assert role.includes_booth is True
        assert role.consumes_booth_capacity is True


@pytest.mark.django_db
def test_sponsorship_product_can_drop_the_booth(event):
    with scopes_disabled():
        role = ExhibitionProduct.objects.create(
            product=_product(event, "Digital Sponsor"),
            includes_booth=False,
        )
        role.refresh_from_db()

        assert role.is_sponsorship is True
        assert role.includes_booth is False
        assert role.consumes_booth_capacity is False


@pytest.mark.django_db
def test_exhibition_product_always_includes_a_booth(event):
    with scopes_disabled():
        role = ExhibitionProduct.objects.create(
            product=_product(event, "Standard Booth"),
            purpose=ExhibitionProductPurpose.EXHIBITION,
            includes_booth=False,
        )
        role.refresh_from_db()

        assert role.is_exhibition is True
        assert role.includes_booth is True
        assert role.consumes_booth_capacity is True


@pytest.mark.django_db
def test_only_booth_products_count_towards_booth_capacity(event):
    with scopes_disabled():
        booth = ExhibitionProduct.objects.create(product=_product(event, "Premium Booth"))
        digital = ExhibitionProduct.objects.create(
            product=_product(event, "Digital Sponsor"),
            includes_booth=False,
        )

        consuming = ExhibitionProduct.objects.for_event(event).consuming_booth_capacity()

        assert list(consuming) == [booth]
        assert digital not in consuming


@pytest.mark.django_db
def test_quota_mixing_booth_and_non_booth_products_is_flagged(event):
    with scopes_disabled():
        gold = _product(event, "Gold Sponsor")
        digital = _product(event, "Digital Sponsor")
        ExhibitionProduct.objects.create(product=gold)
        ExhibitionProduct.objects.create(product=digital, includes_booth=False)

        booth_quota = Quota.objects.create(event=event, name="Exhibition booths", size=40)
        booth_quota.products.add(gold, digital)
        sponsor_quota = Quota.objects.create(event=event, name="Gold sponsors", size=5)
        sponsor_quota.products.add(gold)

        assert list(mixed_booth_quotas(event)) == [booth_quota]


@pytest.mark.django_db
def test_quota_sharing_a_booth_with_a_product_without_a_role_is_flagged(event):
    with scopes_disabled():
        booth = _product(event, "Premium Booth")
        ticket = _product(event, "Visitor Ticket")
        ExhibitionProduct.objects.create(product=booth)

        shared_quota = Quota.objects.create(event=event, name="Shared", size=100)
        shared_quota.products.add(booth, ticket)

        assert list(mixed_booth_quotas(event)) == [shared_quota]


@pytest.mark.django_db
def test_quota_with_only_regular_products_is_not_flagged(event):
    with scopes_disabled():
        tickets_quota = Quota.objects.create(event=event, name="Tickets", size=500)
        tickets_quota.products.add(_product(event, "Visitor Ticket"), _product(event, "Student Ticket"))

        assert list(mixed_booth_quotas(event)) == []


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_lists_the_event_products(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        digital = _product(event, "Digital Sponsor")
        ExhibitionProduct.objects.create(product=digital, includes_booth=False)
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.get(_products_url(event))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Digital Sponsor" in content
    assert 'name="form-0-purpose"' in content
    assert f'name="form-0-product" value="{digital.pk}"' in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_assigns_a_purpose_and_booth_to_a_product(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        gold = _product(event, "Gold Sponsor")
        digital = _product(event, "Digital Sponsor")
        booth = _product(event, "Standard Booth")
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(
        _products_url(event),
        _formset_payload(
            {"product": gold.pk, "purpose": "sponsorship", "includes_booth": "on"},
            {"product": digital.pk, "purpose": "sponsorship"},
            {"product": booth.pk, "purpose": "exhibition"},
        ),
    )

    assert response.status_code == 302
    with scopes_disabled():
        assert gold.exhibition_product.purpose == ExhibitionProductPurpose.SPONSORSHIP
        assert gold.exhibition_product.includes_booth is True
        assert digital.exhibition_product.includes_booth is False
        assert booth.exhibition_product.purpose == ExhibitionProductPurpose.EXHIBITION
        assert booth.exhibition_product.includes_booth is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_clearing_the_purpose_drops_the_exhibition_role(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        product = _product(event, "Gold Sponsor")
        ExhibitionProduct.objects.create(product=product)
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(_products_url(event), _formset_payload({"product": product.pk, "purpose": ""}))

    assert response.status_code == 302
    with scopes_disabled():
        assert not ExhibitionProduct.objects.filter(product=product).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_unknown_purpose_leaves_the_whole_submission_unchanged(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        gold = _product(event, "Gold Sponsor")
        booth = _product(event, "Standard Booth")
        ExhibitionProduct.objects.create(product=gold)
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(
        _products_url(event),
        _formset_payload(
            {"product": gold.pk, "purpose": ""},
            {"product": booth.pk, "purpose": "keynote"},
        ),
    )

    assert response.status_code == 200
    with scopes_disabled():
        assert ExhibitionProduct.objects.filter(product=gold).exists()
        assert not ExhibitionProduct.objects.filter(product=booth).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_of_another_event_is_rejected(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        foreign = _product(_other_event(event), "Someone else's booth")
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(
        _products_url(event),
        _formset_payload({"product": foreign.pk, "purpose": "exhibition"}),
    )

    assert response.status_code == 200
    with scopes_disabled():
        assert not ExhibitionProduct.objects.filter(product=foreign).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_rejected_row_leaves_the_rest_of_the_table_submittable(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        gold = _product(event, "Gold Sponsor")
        foreign = _product(_other_event(event), "Someone else's booth")
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(
        _products_url(event),
        _formset_payload(
            {"product": foreign.pk, "purpose": "exhibition"},
            {"product": gold.pk, "purpose": "sponsorship"},
        ),
    )
    content = response.content.decode()

    assert response.status_code == 200
    # The row that named no product of this event is gone and the rest are renumbered,
    # so what the page shows back can be submitted again as it stands.
    assert 'name="form-TOTAL_FORMS" value="1"' in content
    assert f'name="form-0-product" value="{gold.pk}"' in content
    assert f'value="{foreign.pk}"' not in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_the_same_product_twice_in_one_submission_is_rejected(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        gold = _product(event, "Gold Sponsor")
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(
        _products_url(event),
        _formset_payload(
            {"product": gold.pk, "purpose": "exhibition"},
            {"product": gold.pk, "purpose": "sponsorship"},
        ),
    )

    assert response.status_code == 200
    with scopes_disabled():
        assert not ExhibitionProduct.objects.filter(product=gold).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_product_the_request_does_not_mention_keeps_its_role(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        gold = _product(event, "Gold Sponsor")
        digital = _product(event, "Digital Sponsor")
        ExhibitionProduct.objects.create(product=digital, includes_booth=False)
        user = _organizer(event, can_change_items=True)

    client = Client()
    client.force_login(user)
    response = client.post(
        _products_url(event),
        _formset_payload({"product": gold.pk, "purpose": "exhibition"}),
    )

    assert response.status_code == 302
    with scopes_disabled():
        assert ExhibitionProduct.objects.get(product=gold).purpose == ExhibitionProductPurpose.EXHIBITION
        assert ExhibitionProduct.objects.get(product=digital).includes_booth is False


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_needs_the_product_permission(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = _organizer(event, "viewer@example.com", can_view_orders=True)

    client = Client()
    client.force_login(user)
    response = client.get(_products_url(event))

    assert response.status_code == 403
