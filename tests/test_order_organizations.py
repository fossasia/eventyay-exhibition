from decimal import Decimal

import pytest
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionOrderForm
from exhibition.models import (
    ExhibitionOrderQuestion,
    ExhibitionProduct,
    ExhibitionProductPurpose,
    ExhibitionQuestionVariant,
    ExhibitionQuota,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
)
from exhibition.orders import place_exhibition_order


def _product(event, name, purpose=ExhibitionProductPurpose.SPONSORSHIP, includes_booth=True, price="0.00"):
    return ExhibitionProduct.objects.create(
        event=event,
        name={"en": name},
        price=Decimal(price),
        purpose=purpose,
        includes_booth=includes_booth,
    )


def _paid_order(event, *products, organization_name="Acme Inc", email="ada@example.com", **kwargs):
    kwargs.setdefault("name", "Ada Lovelace")
    return place_exhibition_order(event, products, organization_name=organization_name, email=email, **kwargs)


def _pending_order(event, *products, **kwargs):
    event.settings.set("payment_manual__enabled", True)
    return _paid_order(event, *products, payment_provider="manual", **kwargs)


@pytest.mark.django_db
def test_a_paid_exhibition_product_makes_an_exhibitor_without_sponsorship(event):
    with scopes_disabled():
        order = _paid_order(event, _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION))
        organization = order.organization

        assert organization.is_exhibitor
        assert not organization.is_sponsor
        assert organization.booth_id


@pytest.mark.django_db
def test_a_paid_sponsorship_with_booth_makes_a_sponsor_with_booth(event):
    with scopes_disabled():
        order = _paid_order(event, _product(event, "Gold"))

        assert order.organization.is_sponsor
        assert order.organization.is_exhibitor


@pytest.mark.django_db
def test_a_paid_sponsorship_without_booth_makes_a_sponsor_without_booth(event):
    with scopes_disabled():
        order = _paid_order(event, _product(event, "Digital", includes_booth=False))

        assert order.organization.is_sponsor
        assert not order.organization.is_exhibitor
        assert order.organization.booth_id is None


@pytest.mark.django_db
def test_the_profile_is_prefilled_from_the_order_and_records_the_order(event):
    with scopes_disabled():
        order = _paid_order(event, _product(event, "Gold"), website="https://acme.example")
        organization = order.organization

        assert str(organization.name) == "Acme Inc"
        assert organization.email == "ada@example.com"
        assert organization.url == "https://acme.example"
        assert organization.source_order == order
        assert not organization.published
        assert list(organization.exhibition_orders.all()) == [order]


@pytest.mark.django_db
def test_one_organisation_buying_twice_keeps_one_profile(event):
    with scopes_disabled():
        first = _paid_order(event, _product(event, "Digital", includes_booth=False))
        second = _paid_order(
            event,
            _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION),
            organization_name="ACME INC ",
        )

        assert ExhibitorInfo.objects.filter(event=event).count() == 1
        assert second.organization == first.organization
        organization = ExhibitorInfo.objects.get(event=event)
        assert organization.is_sponsor
        assert organization.is_exhibitor
        assert organization.booth_id
        assert organization.source_order == first


@pytest.mark.django_db
def test_another_organisation_of_the_same_email_gets_its_own_profile(event):
    with scopes_disabled():
        existing = ExhibitorInfo.objects.create(event=event, name="Acme", email="ADA@example.com", is_exhibitor=False)
        order = _paid_order(event, _product(event, "Gold"), organization_name="Globex")

        assert order.organization != existing
        assert str(order.organization.name) == "Globex"
        assert ExhibitorInfo.objects.filter(event=event).count() == 2


@pytest.mark.django_db
def test_a_pending_order_creates_no_profile_until_it_is_paid(event):
    with scopes_disabled():
        order = _pending_order(event, _product(event, "Gold", price="100.00"))

        assert order.organization is None
        assert not ExhibitorInfo.objects.filter(event=event).exists()

        order.mark_paid()

        assert order.organization is not None


@pytest.mark.django_db
def test_order_form_answers_are_reachable_from_the_profile(event):
    with scopes_disabled():
        product = _product(event, "Gold")
        question = ExhibitionOrderQuestion.objects.create(
            event=event, question={"en": "Booth power"}, variant=ExhibitionQuestionVariant.STRING
        )
        question.products.set([product])
        form = ExhibitionOrderForm(
            data={
                "organization_name": "Acme Inc",
                "name": "Ada",
                "email": "ada@example.com",
                f"question_{question.pk}": "16A",
            },
            event=event,
            products=[product],
        )
        assert form.is_valid(), form.errors
        order = _paid_order(event, product, order_form=form)

        answers = [
            answer.answer
            for linked_order in order.organization.exhibition_orders.all()
            for answer in linked_order.answers.all()
        ]
        assert answers == ["16A"]


@pytest.mark.django_db
def test_cancelling_the_only_paid_order_hides_the_profile_and_frees_the_booth(event):
    with scopes_disabled():
        product = _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
        quota = ExhibitionQuota.objects.create(event=event, name="Booths", size=1, booth_capacity=True)
        quota.products.set([product])
        order = _paid_order(event, product)
        organization = order.organization
        organization.published = True
        organization.save(update_fields=["published"])
        assert product.remaining_capacity() == 0

        order.cancel()

        organization.refresh_from_db()
        assert not organization.active
        assert not organization.published
        assert product.remaining_capacity() == 1


@pytest.mark.django_db
def test_another_paid_order_keeps_the_profile_active(event):
    with scopes_disabled():
        first = _paid_order(event, _product(event, "Gold"))
        second = _paid_order(event, _product(event, "Extra", includes_booth=False))

        first.cancel()

        second.organization.refresh_from_db()
        assert second.organization.active


@pytest.mark.django_db
def test_an_accepted_request_keeps_the_profile_active(event):
    with scopes_disabled():
        order = _paid_order(event, _product(event, "Gold"))
        user = User.objects.create_user(email="ada@example.com", password="secret")
        ExhibitionRequest.objects.create(
            event=event,
            user=user,
            name="Acme Inc",
            state=ExhibitionRequestState.ACCEPTED,
            approved_exhibitor=order.organization,
        )

        order.cancel()

        order.organization.refresh_from_db()
        assert order.organization.active


@pytest.mark.django_db
def test_paying_again_reactivates_a_hidden_profile(event):
    with scopes_disabled():
        first = _paid_order(event, _product(event, "Gold"))
        first.cancel()

        second = _paid_order(event, _product(event, "Silver"))

        assert second.organization == first.organization
        second.organization.refresh_from_db()
        assert second.organization.active


@pytest.mark.django_db
def test_cancelling_leaves_a_profile_the_order_did_not_create(event):
    with scopes_disabled():
        existing = ExhibitorInfo.objects.create(event=event, name="Acme Inc", published=True)
        order = _paid_order(event, _product(event, "Gold"))
        assert order.organization == existing

        order.cancel()

        existing.refresh_from_db()
        assert existing.active
        assert existing.published


@pytest.mark.django_db
def test_paying_does_not_reactivate_a_profile_the_organiser_hid(event):
    with scopes_disabled():
        hidden = ExhibitorInfo.objects.create(event=event, name="Acme Inc", active=False)

        order = _paid_order(event, _product(event, "Gold"))

        assert order.organization == hidden
        hidden.refresh_from_db()
        assert not hidden.active


@pytest.mark.django_db
def test_any_website_the_order_form_accepts_fits_on_the_profile(event):
    with scopes_disabled():
        product = _product(event, "Community Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
        prefix = "https://example.com/"
        longest = prefix + "a" * (200 - len(prefix))
        data = {"organization_name": "Acme Inc", "name": "Ada", "email": "ada@example.com"}

        too_long = ExhibitionOrderForm(data={**data, "website": longest + "a"}, event=event, products=[product])
        assert not too_long.is_valid()
        assert "website" in too_long.errors

        form = ExhibitionOrderForm(data={**data, "website": longest}, event=event, products=[product])
        assert form.is_valid(), form.errors
        order = _paid_order(event, product, website=longest)

        assert order.organization.url == longest
