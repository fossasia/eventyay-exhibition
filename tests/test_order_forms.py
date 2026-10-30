from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, LogEntry
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionOrderForm, ExhibitionOrderQuestionForm
from exhibition.models import (
    LOG_ORDER_FORM_CHANGED,
    LOG_ORDER_QUESTION_ADDED,
    ExhibitionOrder,
    ExhibitionOrderAnswer,
    ExhibitionOrderError,
    ExhibitionOrderQuestion,
    ExhibitionOrderQuestionOption,
    ExhibitionProduct,
    ExhibitionProductPurpose,
    ExhibitionQuestionVariant,
    ExhibitorSettings,
    order_questions_for,
)
from exhibition.orders import place_exhibition_order

CUSTOMER_DATA = {"organization_name": "Acme Inc", "name": "Ada Lovelace", "email": "ada@example.com"}


def _product(event, name, price="1000.00", **kwargs):
    return ExhibitionProduct.objects.create(event=event, name={"en": name}, price=Decimal(price), **kwargs)


def _question(event, label, *products, variant=ExhibitionQuestionVariant.STRING, **kwargs):
    question = ExhibitionOrderQuestion.objects.create(event=event, question={"en": label}, variant=variant, **kwargs)
    question.products.set(products)
    return question


def _choice_question(event, label, *products, answers=("Yes", "No"), **kwargs):
    question = _question(event, label, *products, variant=ExhibitionQuestionVariant.SELECT, **kwargs)
    options = [
        ExhibitionOrderQuestionOption.objects.create(question=question, answer={"en": answer}, position=index)
        for index, answer in enumerate(answers)
    ]
    return question, options


def _checkout(event, products, data):
    return ExhibitionOrderForm(data={**CUSTOMER_DATA, **data}, event=event, products=products)


def _order(event, products, order_form):
    assert order_form.is_valid(), order_form.errors
    return place_exhibition_order(
        event,
        products,
        payment_provider="manual",
        order_form=order_form,
        **order_form.order_details(),
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


def _field_data(**overrides):
    data = {
        "question_0": "VAT ID",
        "variant": ExhibitionQuestionVariant.STRING,
        "active": "on",
        "options-TOTAL_FORMS": "0",
        "options-INITIAL_FORMS": "0",
        "options-MIN_NUM_FORMS": "0",
        "options-MAX_NUM_FORMS": "1000",
    }
    products = overrides.pop("products", None)
    data.update(overrides)
    if products is not None:
        data["products"] = [str(product.pk) for product in products]
    return data


@pytest.fixture
def shop(event):
    with scopes_disabled():
        event.settings.set("payment_manual__enabled", True)
        gold = _product(event, "Gold Sponsor")
        digital = _product(event, "Digital Sponsor", includes_booth=False)
        booth = _product(event, "Booth", purpose=ExhibitionProductPurpose.EXHIBITION)
    return {"gold": gold, "digital": digital, "booth": booth}


# Customer data


@pytest.mark.django_db
def test_customer_fields_follow_the_settings(event, shop):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.create(event=event)
        settings.order_field_settings = {
            "phone": {"active": False, "required": False, "position": 3},
            "vat_id": {"active": True, "required": True, "position": 6},
        }
        settings.save()

        form = ExhibitionOrderForm(event=event, products=[shop["gold"]])

        assert "phone" not in form.fields
        assert form.fields["vat_id"].required
        assert "billing_address" not in form.fields
        assert form.fields["organization_name"].required


@pytest.mark.django_db
def test_locked_customer_fields_cannot_be_turned_off(event):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.create(
            event=event,
            order_field_settings={"email": {"active": False, "required": False, "position": 2}},
        )

        email = next(field for field in settings.normalized_order_field_settings if field["key"] == "email")

        assert email["active"] and email["required"]


@pytest.mark.django_db
def test_customer_data_is_stored_on_the_order(event, shop):
    with scopes_disabled():
        ExhibitorSettings.objects.create(
            event=event,
            order_field_settings={"vat_id": {"active": True, "required": False, "position": 6}},
        )
        form = _checkout(event, [shop["gold"]], {"website": "https://acme.example", "vat_id": "GB123"})

        order = _order(event, [shop["gold"]], form)

        assert (order.organization_name, order.name, order.email) == ("Acme Inc", "Ada Lovelace", "ada@example.com")
        assert order.website == "https://acme.example"
        assert order.vat_id == "GB123"


# Which custom fields an order asks


@pytest.mark.django_db
def test_a_custom_field_is_asked_only_for_its_products(event, shop):
    with scopes_disabled():
        furniture = _question(event, "Furniture", shop["gold"], shop["booth"])
        logo = _question(event, "Logo", shop["gold"], shop["digital"])

        assert list(order_questions_for([shop["digital"]])) == [logo]
        assert list(order_questions_for([shop["booth"]])) == [furniture]
        assert list(order_questions_for([shop["gold"], shop["booth"]])) == [furniture, logo]


@pytest.mark.django_db
def test_inactive_fields_and_fields_without_products_are_not_asked(event, shop):
    with scopes_disabled():
        _question(event, "Old question", shop["gold"], active=False)
        _question(event, "Unassigned")

        assert list(order_questions_for([shop["gold"]])) == []


@pytest.mark.django_db
def test_a_required_booth_field_does_not_block_a_digital_order(event, shop):
    with scopes_disabled():
        _question(event, "Furniture", shop["booth"], required=True)

        assert _checkout(event, [shop["digital"]], {}).is_valid()
        assert not _checkout(event, [shop["booth"]], {}).is_valid()


# Answering at checkout


@pytest.mark.django_db
def test_answers_are_stored_with_the_order(event, shop):
    with scopes_disabled():
        vat = _question(event, "VAT ID", shop["gold"], required=True)
        size, (small, large) = _choice_question(event, "Booth size", shop["gold"], answers=("Small", "Large"))
        form = _checkout(event, [shop["gold"]], {f"question_{vat.pk}": "GB123", f"question_{size.pk}": str(large.pk)})

        order = _order(event, [shop["gold"]], form)

        answers = {answer.question_id: answer for answer in ExhibitionOrderAnswer.objects.filter(order=order)}
        assert answers[vat.pk].answer == "GB123"
        assert list(answers[size.pk].options.all()) == [large]
        assert answers[size.pk].answer_string == "Large"


@pytest.mark.django_db
def test_an_invalid_order_form_places_no_order(event, shop):
    with scopes_disabled():
        _question(event, "VAT ID", shop["gold"], required=True)
        form = _checkout(event, [shop["gold"]], {})

        with pytest.raises(ExhibitionOrderError):
            place_exhibition_order(event, [shop["gold"]], payment_provider="manual", order_form=form, **CUSTOMER_DATA)
        assert not ExhibitionOrder.objects.exists()


@pytest.mark.django_db
def test_a_hidden_dependent_field_is_neither_required_nor_stored(event, shop):
    with scopes_disabled():
        needs_power, (yes, no) = _choice_question(event, "Need electricity?", shop["gold"])
        watts = _question(
            event,
            "How many watts?",
            shop["gold"],
            required=True,
            dependency_question=needs_power,
            dependency_values=[str(yes.pk)],
        )
        form = _checkout(
            event,
            [shop["gold"]],
            {f"question_{needs_power.pk}": str(no.pk), f"question_{watts.pk}": "500"},
        )

        order = _order(event, [shop["gold"]], form)

        assert not ExhibitionOrderAnswer.objects.filter(order=order, question=watts).exists()


@pytest.mark.django_db
def test_a_shown_dependent_field_is_required(event, shop):
    with scopes_disabled():
        needs_power, (yes, no) = _choice_question(event, "Need electricity?", shop["gold"])
        watts = _question(
            event,
            "How many watts?",
            shop["gold"],
            required=True,
            dependency_question=needs_power,
            dependency_values=[str(yes.pk)],
        )
        form = _checkout(event, [shop["gold"]], {f"question_{needs_power.pk}": str(yes.pk)})

        assert not form.is_valid()
        assert f"question_{watts.pk}" in form.errors


# Editing custom fields


@pytest.mark.django_db
def test_a_field_can_only_depend_on_order_form_fields_of_its_event(event, shop):
    with scopes_disabled():
        own, _ = _choice_question(event, "Own choice", shop["gold"])
        other_event = Event.objects.create(
            organizer=event.organizer, name="Other Event", slug="other-event", live=True, date_from=now()
        )
        foreign, _ = _choice_question(other_event, "Other event choice")

        form = ExhibitionOrderQuestionForm(event=event)

        candidates = list(form.fields["dependency_question"].queryset)
        assert own in candidates
        assert foreign not in candidates


@pytest.mark.django_db
def test_new_fields_go_to_the_end_and_keep_their_products(event, shop):
    with scopes_disabled():
        _question(event, "First", shop["gold"], position=4)
        form = ExhibitionOrderQuestionForm(data=_field_data(products=[shop["booth"]]), event=event)
        assert form.is_valid(), form.errors

        question = form.save()

        assert question.event == event
        assert question.position == 7
        assert list(question.products.all()) == [shop["booth"]]


# Organizer pages


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_form_page_lists_customer_and_custom_fields(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        _question(event, "Furniture", shop["booth"])

    response = client.get(_url(event, "products.order_form"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Organization name" in content
    assert "VAT ID" in content
    assert "Furniture" in content
    assert "Booth" in content


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_saves_customer_and_custom_field_toggles(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        furniture = _question(event, "Furniture", shop["booth"])

    response = client.post(
        _url(event, "products.order_form"),
        {
            "website_active": "on",
            "vat_id_active": "on",
            "vat_id_required": "required",
            f"question_{furniture.pk}_active": "on",
            f"question_{furniture.pk}_required": "required",
        },
    )

    assert response.status_code == 302
    with scopes_disabled():
        settings = ExhibitorSettings.objects.get(event=event)
        fields = {field["key"]: field for field in settings.normalized_order_field_settings}
        assert fields["vat_id"]["active"] and fields["vat_id"]["required"]
        assert not fields["phone"]["active"]
        assert fields["email"]["active"] and fields["email"]["required"]
        furniture.refresh_from_db()
        assert furniture.active and furniture.required
        assert LogEntry.objects.filter(action_type=LOG_ORDER_FORM_CHANGED).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_customer_fields_and_custom_fields_can_be_reordered(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        first = _question(event, "First", shop["gold"], position=0)
        second = _question(event, "Second", shop["gold"], position=1)
    url = _url(event, "products.order_form")

    assert client.post(url, {"order": f"email,{second.pk},name,organization_name,{first.pk}"}).status_code == 204

    with scopes_disabled():
        keys = [field["key"] for field in ExhibitorSettings.objects.get(event=event).normalized_order_field_settings]
        assert keys[:3] == ["email", "name", "organization_name"]
        assert list(ExhibitionOrderQuestion.objects.filter(event=event).order_by("position")) == [second, first]
        form = ExhibitionOrderForm(event=event, products=[shop["gold"]])
        assert list(form.fields)[:5] == [
            "email",
            f"question_{second.pk}",
            "name",
            "organization_name",
            f"question_{first.pk}",
        ]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_creates_a_custom_field_for_booth_products(event, shop):
    client = _organizer_client(event)

    response = client.post(
        _url(event, "products.order_form.fields.add"),
        _field_data(question_0="Furniture", required="on", products=[shop["booth"], shop["gold"]]),
    )

    assert response.status_code == 302
    assert response.url == _url(event, "products.order_form")
    with scopes_disabled():
        question = ExhibitionOrderQuestion.objects.get(event=event)
        assert question.required
        assert set(question.products.all()) == {shop["booth"], shop["gold"]}
        assert LogEntry.objects.filter(action_type=LOG_ORDER_QUESTION_ADDED, object_id=question.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_of_other_events_cannot_be_selected(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        other_event = Event.objects.create(
            organizer=event.organizer, name="Other Event", slug="other-event", live=True, date_from=now()
        )
        foreign = _product(other_event, "Foreign")

    response = client.post(_url(event, "products.order_form.fields.add"), _field_data(products=[foreign]))

    assert response.status_code == 200
    assert "products" in response.context["form"].errors


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_answered_fields_cannot_be_deleted(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        vat = _question(event, "VAT ID", shop["gold"])
        _order(event, [shop["gold"]], _checkout(event, [shop["gold"]], {f"question_{vat.pk}": "GB123"}))

    client.post(_url(event, "products.order_form.fields.delete", pk=vat.pk))

    with scopes_disabled():
        assert ExhibitionOrderQuestion.objects.filter(pk=vat.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_an_unanswered_field_is_deleted(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        vat = _question(event, "VAT ID", shop["gold"])

    response = client.post(_url(event, "products.order_form.fields.delete", pk=vat.pk))

    assert response.status_code == 302
    with scopes_disabled():
        assert not ExhibitionOrderQuestion.objects.filter(pk=vat.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_order_page_shows_customer_data_and_answers(event, shop):
    client = _organizer_client(event)
    with scopes_disabled():
        ExhibitorSettings.objects.create(
            event=event,
            order_field_settings={"vat_id": {"active": True, "required": False, "position": 6}},
        )
        furniture = _question(event, "Furniture", shop["gold"])
        order = _order(
            event,
            [shop["gold"]],
            _checkout(event, [shop["gold"]], {"vat_id": "GB123", f"question_{furniture.pk}": "Two tables"}),
        )

    content = client.get(_url(event, "orders.detail", code=order.code)).content.decode()

    assert "GB123" in content
    assert "Furniture" in content
    assert "Two tables" in content
