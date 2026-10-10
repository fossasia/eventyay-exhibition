import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, LogEntry
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionProductForm
from exhibition.signals import exhibition_logentry_object_link
from exhibition.models import (
    LOG_PRODUCT_ADDED,
    LOG_PRODUCT_CATEGORY_ADDED,
    LOG_PRODUCT_CATEGORY_DELETED,
    ExhibitionProduct,
    ExhibitionProductCategory,
)


def _category(event, name="Sponsorship", **kwargs):
    return ExhibitionProductCategory.objects.create(event=event, name={"en": name}, **kwargs)


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


def _api_url(event, name):
    return reverse(f"api-v1:{name}-list", kwargs={"organizer": event.organizer.slug, "event": event.slug})


def _organizer_client(event, **flags):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save()
        user = _organizer(event, **(flags or {"can_change_event_settings": True}))
    client = Client()
    client.force_login(user)
    return client


def _product_form_data(**overrides):
    data = {
        "name_0": "Gold Sponsor",
        "purpose": "sponsorship",
        "includes_booth": "on",
        "price": "1500.00",
        "active": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


# The model


@pytest.mark.django_db
def test_new_categories_are_added_at_the_end(event):
    with scopes_disabled():
        first = _category(event, "Exhibition")
        second = _category(event, "Sponsorship")

        assert (first.position, second.position) == (0, 1)
        assert list(ExhibitionProductCategory.objects.filter(event=event)) == [first, second]


@pytest.mark.django_db
def test_backend_name_prefers_the_internal_name(event):
    with scopes_disabled():
        public_only = _category(event, "Sponsorship")
        with_internal = _category(event, "Community Partnerships", internal_name="Partners 2026")

    assert public_only.backend_name == "Sponsorship"
    assert with_internal.backend_name == "Partners 2026"


@pytest.mark.django_db
def test_deleting_a_category_leaves_its_products_uncategorised(event):
    with scopes_disabled():
        category = _category(event)
        product = _product(event, category=category)

        category.delete()
        product.refresh_from_db()

        assert product.category is None
        assert ExhibitionProduct.objects.filter(pk=product.pk).exists()


@pytest.mark.django_db
def test_products_follow_category_order_with_uncategorised_last(event):
    with scopes_disabled():
        sponsorship = _category(event, "Sponsorship")
        exhibition = _category(event, "Exhibition")
        sponsorship.position, exhibition.position = 1, 0
        sponsorship.save()
        exhibition.save()

        loose = _product(event, "Lanyard")
        gold = _product(event, "Gold Sponsor", category=sponsorship)
        booth = _product(event, "Standard Booth", category=exhibition, purpose="exhibition")
        silver = _product(event, "Silver Sponsor", category=sponsorship)

        ordered = list(ExhibitionProduct.objects.for_event(event).in_sales_order())

    assert ordered == [booth, gold, silver, loose]


# The product form


@pytest.mark.django_db
def test_product_form_offers_only_this_events_categories(event):
    with scopes_disabled():
        own = _category(event, "Sponsorship")
        foreign = _category(_other_event(event), "Elsewhere")

        form = ExhibitionProductForm(data=_product_form_data(category=str(foreign.pk)), event=event)

        assert list(form.fields["category"].queryset) == [own]
        assert not form.is_valid()
        assert "category" in form.errors


@pytest.mark.django_db
def test_product_can_be_assigned_a_category_or_left_without_one(event):
    with scopes_disabled():
        category = _category(event)

        with_category = ExhibitionProductForm(data=_product_form_data(category=str(category.pk)), event=event)
        without_category = ExhibitionProductForm(data=_product_form_data(name_0="Lanyard"), event=event)
        assert with_category.is_valid(), with_category.errors
        assert without_category.is_valid(), without_category.errors

        with_category.instance.event = event
        without_category.instance.event = event
        assert with_category.save().category == category
        assert without_category.save().category is None


# The organizer pages


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_categories_page_lists_this_events_categories_in_order_with_product_counts(event):
    client = _organizer_client(event)
    with scopes_disabled():
        tiers = _category(event, "Sponsor tiers")
        _category(event, "Booth packages")
        _category(_other_event(event), "Elsewhere")
        _product(event, "Gold Sponsor", category=tiers)
        _product(event, "Silver Sponsor", category=tiers)

    response = client.get(_url(event, "products.categories"))
    content = response.content.decode()

    assert response.status_code == 200
    assert content.index("Sponsor tiers") < content.index("Booth packages")
    assert "Elsewhere" not in content
    assert [category.product_count for category in response.context["categories"]] == [2, 0]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_creates_a_category(event):
    client = _organizer_client(event)

    assert client.get(_url(event, "products.categories.add")).status_code == 200
    response = client.post(
        _url(event, "products.categories.add"),
        {"name_0": "Add-ons", "internal_name": "Booth extras", "description_0": "Furniture and power"},
    )

    assert response.status_code == 302
    assert response.url == _url(event, "products.categories")
    with scopes_disabled():
        category = ExhibitionProductCategory.objects.get(event=event)
        assert str(category) == "Add-ons"
        assert category.internal_name == "Booth extras"
        assert LogEntry.objects.filter(action_type=LOG_PRODUCT_CATEGORY_ADDED, object_id=category.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_edits_a_category(event):
    client = _organizer_client(event)
    with scopes_disabled():
        category = _category(event, "Sponsorship")

    assert client.get(_url(event, "products.categories.edit", pk=category.pk)).status_code == 200
    response = client.post(
        _url(event, "products.categories.edit", pk=category.pk),
        {"name_0": "Sponsorship packages", "internal_name": ""},
    )

    assert response.status_code == 302
    with scopes_disabled():
        category.refresh_from_db()
    assert str(category) == "Sponsorship packages"


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_deletes_a_category_and_keeps_its_products(event):
    client = _organizer_client(event)
    with scopes_disabled():
        category = _category(event, "Sponsorship")
        gold = _product(event, "Gold Sponsor", category=category)
        silver = _product(event, "Silver Sponsor", category=category)

    confirm_page = client.get(_url(event, "products.categories.delete", pk=category.pk)).content.decode()
    response = client.post(_url(event, "products.categories.delete", pk=category.pk))

    assert "2 products in this category will not be deleted" in confirm_page
    assert response.status_code == 302
    with scopes_disabled():
        assert not ExhibitionProductCategory.objects.filter(pk=category.pk).exists()
        gold.refresh_from_db()
        silver.refresh_from_db()
        assert gold.category is None and silver.category is None
        assert LogEntry.objects.filter(action_type=LOG_PRODUCT_CATEGORY_DELETED, object_id=category.pk).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_reorder_saves_the_posted_order(event):
    client = _organizer_client(event)
    with scopes_disabled():
        first = _category(event, "Exhibition")
        second = _category(event, "Sponsorship")
        third = _category(event, "Add-ons")

    response = client.post(
        _url(event, "products.categories.reorder"),
        {"order": f"{third.pk},{first.pk},{second.pk}"},
    )

    assert response.status_code == 204
    with scopes_disabled():
        assert list(ExhibitionProductCategory.objects.filter(event=event)) == [third, first, second]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_reorder_rejects_an_incomplete_duplicated_or_foreign_order(event):
    client = _organizer_client(event)
    with scopes_disabled():
        first = _category(event, "Exhibition")
        second = _category(event, "Sponsorship")
        foreign = _category(_other_event(event), "Elsewhere")
    url = _url(event, "products.categories.reorder")

    assert client.post(url, {"order": f"{first.pk}"}).status_code == 400
    assert client.post(url, {"order": f"{first.pk},{first.pk}"}).status_code == 400
    assert client.post(url, {"order": f"{first.pk},{second.pk},{foreign.pk}"}).status_code == 400
    with scopes_disabled():
        assert list(ExhibitionProductCategory.objects.filter(event=event)) == [first, second]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_a_category_of_another_event_cannot_be_changed(event):
    client = _organizer_client(event)
    with scopes_disabled():
        foreign = _category(_other_event(event), "Elsewhere")

    assert client.get(_url(event, "products.categories.edit", pk=foreign.pk)).status_code == 404
    assert client.post(_url(event, "products.categories.edit", pk=foreign.pk), {"name_0": "Taken"}).status_code == 404
    assert client.post(_url(event, "products.categories.delete", pk=foreign.pk)).status_code == 404
    with scopes_disabled():
        foreign.refresh_from_db()
    assert str(foreign) == "Elsewhere"


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_category_pages_need_the_exhibition_settings_permission(event):
    client = _organizer_client(event, can_change_items=True)
    with scopes_disabled():
        category = _category(event)

    assert client.get(_url(event, "products.categories")).status_code == 403
    assert client.get(_url(event, "products.categories.add")).status_code == 403
    assert client.post(_url(event, "products.categories.reorder"), {"order": str(category.pk)}).status_code == 403


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_products_page_groups_products_under_their_categories(event):
    client = _organizer_client(event)
    with scopes_disabled():
        tiers = _category(event, "Sponsor tiers")
        booths = _category(event, "Booth packages")
        _product(event, "Gold Sponsor", category=tiers)
        _product(event, "Standard Booth", category=booths, purpose="exhibition")
        _product(event, "Lanyard")

    content = client.get(_url(event, "products")).content.decode()

    assert (
        content.index("Sponsor tiers")
        < content.index("Gold Sponsor")
        < content.index("Booth packages")
        < content.index("Standard Booth")
        < content.index("No category")
        < content.index("Lanyard")
    )


# The API


@pytest.mark.django_db
def test_api_lists_categories_in_order_and_products_with_their_category(event):
    client = _organizer_client(event)
    with scopes_disabled():
        sponsorship = _category(event, "Sponsorship")
        exhibition = _category(event, "Exhibition")
        sponsorship.position, exhibition.position = 1, 0
        sponsorship.save()
        exhibition.save()
        gold = _product(event, "Gold Sponsor", category=sponsorship)
        booth = _product(event, "Standard Booth", category=exhibition, purpose="exhibition")

    categories = client.get(_api_url(event, "exhibitionproductcategory")).json()["results"]
    products = client.get(_api_url(event, "exhibitionproduct")).json()["results"]

    assert [row["id"] for row in categories] == [exhibition.pk, sponsorship.pk]
    assert [(row["id"], row["category"]) for row in products] == [(booth.pk, exhibition.pk), (gold.pk, sponsorship.pk)]


# The activity log


@pytest.mark.django_db
def test_log_entries_link_to_the_product_and_category_edit_pages(event):
    with scopes_disabled():
        category = _category(event, "Sponsor tiers")
        product = _product(event, "Gold Sponsor", category=category)
        product.log_action(LOG_PRODUCT_ADDED, data={})
        category.log_action(LOG_PRODUCT_CATEGORY_ADDED, data={})
        product_entry = LogEntry.objects.get(action_type=LOG_PRODUCT_ADDED, object_id=product.pk)
        category_entry = LogEntry.objects.get(action_type=LOG_PRODUCT_CATEGORY_ADDED, object_id=category.pk)

        product_link = exhibition_logentry_object_link(sender=event, logentry=product_entry)
        category_link = exhibition_logentry_object_link(sender=event, logentry=category_entry)

    assert _url(event, "products.edit", pk=product.pk) in product_link
    assert "Gold Sponsor" in product_link
    assert _url(event, "products.categories.edit", pk=category.pk) in category_link
    assert "Sponsor tiers" in category_link
