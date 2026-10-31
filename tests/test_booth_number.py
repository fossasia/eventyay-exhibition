from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product
from eventyay.base.models.auth import User
from rest_framework.test import APIRequestFactory

from exhibition import mail as mail_helpers
from exhibition.api import ExhibitorAuthView, ExhibitorInfoSerializer, LeadCreateView, LeadRetrieveView
from exhibition.filters import ExhibitorFilterForm
from exhibition.forms import ExhibitorInfoForm
from exhibition.models import ExhibitionRequest, ExhibitionRequestState, ExhibitorGroup, ExhibitorInfo, Lead
from exhibition.utils import create_exhibitor_from_request


def _request(event, email, **kwargs):
    user = User.objects.create_user(email=email, password="pw")
    return ExhibitionRequest.objects.create(
        event=event,
        user=user,
        name=kwargs.pop("name", "Org"),
        state=ExhibitionRequestState.SUBMITTED,
        **kwargs,
    )


@pytest.mark.django_db
def test_new_exhibitors_start_without_a_booth(event):
    with scopes_disabled():
        first = ExhibitorInfo.objects.create(event=event, name="First", is_exhibitor=True)
        second = ExhibitorInfo.objects.create(event=event, name="Second", is_exhibitor=True, booth_number="  ")

    assert first.booth_number is None
    assert second.booth_number is None


@pytest.mark.django_db
def test_blank_booth_numbers_written_in_bulk_do_not_clash(event):
    with scopes_disabled():
        first = ExhibitorInfo.objects.create(event=event, name="First", is_exhibitor=True)
        second = ExhibitorInfo.objects.create(event=event, name="Second", is_exhibitor=True)
        ExhibitorInfo.objects.filter(pk__in=[first.pk, second.pk]).update(booth_number="  ")

        assert ExhibitorInfo.objects.filter(event=event, booth_number="  ").count() == 2


@pytest.mark.django_db
def test_exhibitor_form_saves_the_booth_number_as_typed(event, image_uploads):
    form = ExhibitorInfoForm(
        data={"name_0": "Acme", "booth_number": "Hall 1-04", "exhibitor_group": ExhibitorGroup.PAID},
        files=image_uploads(),
        event=event,
        organization_type="exhibitor",
    )
    assert form.is_valid(), form.errors
    with scopes_disabled():
        exhibitor = form.save(commit=False)
        exhibitor.event = event
        exhibitor.save()

    assert exhibitor.booth_number == "Hall 1-04"
    assert form.fields["booth_number"].label == "Booth number"


@pytest.mark.django_db
def test_exhibitor_form_leaves_a_blank_booth_unassigned(event, image_uploads):
    form = ExhibitorInfoForm(
        data={"name_0": "Acme", "booth_number": "", "exhibitor_group": ExhibitorGroup.PAID},
        files=image_uploads(),
        event=event,
        organization_type="exhibitor",
    )
    assert form.is_valid(), form.errors

    assert form.cleaned_data["booth_number"] is None


@pytest.mark.django_db
def test_accepting_a_request_does_not_invent_a_booth(event):
    with scopes_disabled():
        exhibition_request = _request(event, "a@example.com")
        exhibitor = create_exhibitor_from_request(exhibition_request)

    assert exhibitor.booth_number is None


@pytest.mark.django_db
def test_accepting_a_request_keeps_the_booth_set_during_review(event):
    with scopes_disabled():
        exhibition_request = _request(event, "a@example.com", booth_number="B12")
        exhibitor = create_exhibitor_from_request(exhibition_request)

    assert exhibitor.booth_number == "B12"


@pytest.mark.django_db
def test_reapproval_keeps_the_booth_already_assigned_to_the_profile(event):
    with scopes_disabled():
        exhibition_request = _request(event, "a@example.com")
        exhibitor = create_exhibitor_from_request(exhibition_request)
        exhibitor.booth_number = "B12"
        exhibitor.save(update_fields=["booth_number"])
        exhibition_request.refresh_from_db()

        reapproved = create_exhibitor_from_request(exhibition_request)

    assert reapproved.pk == exhibitor.pk
    assert reapproved.booth_number == "B12"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "ordering,expected",
    [
        ("booth", ["A1", "B12", "Unassigned one", "Unassigned two"]),
        ("-booth", ["B12", "A1", "Unassigned one", "Unassigned two"]),
    ],
)
def test_booth_sort_groups_unassigned_exhibitors_at_the_end(event, ordering, expected):
    with scopes_disabled():
        unassigned_two = ExhibitorInfo.objects.create(event=event, name="Unassigned two", is_exhibitor=True)
        ExhibitorInfo.objects.filter(pk=unassigned_two.pk).update(booth_number=" ")
        ExhibitorInfo.objects.create(event=event, name="B12", is_exhibitor=True, booth_number="B12")
        ExhibitorInfo.objects.create(event=event, name="Unassigned one", is_exhibitor=True)
        ExhibitorInfo.objects.create(event=event, name="A1", is_exhibitor=True, booth_number="A1")
        form = ExhibitorFilterForm(data={"ordering": ordering}, event=event, organization_type="exhibitor")
        assert form.is_valid(), form.errors

        names = [str(exhibitor.name) for exhibitor in form.filter_qs(ExhibitorInfo.objects.filter(event=event))]

    assert names == expected


@pytest.mark.django_db
def test_booth_search_matches_the_booth_number(event):
    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Acme", is_exhibitor=True, booth_number="Hall 1-04")
        ExhibitorInfo.objects.create(event=event, name="Other", is_exhibitor=True)
        form = ExhibitorFilterForm(data={"query": "hall 1"}, event=event, organization_type="exhibitor")
        assert form.is_valid(), form.errors

        names = [str(exhibitor.name) for exhibitor in form.filter_qs(ExhibitorInfo.objects.filter(event=event))]

    assert names == ["Acme"]


@pytest.mark.django_db
def test_booth_placeholder_reads_naturally_for_unassigned_exhibitors(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    with scopes_disabled():
        assigned = ExhibitorInfo.objects.create(event=event, name="Assigned", is_exhibitor=True, booth_number="B12")
        unassigned = ExhibitorInfo.objects.create(event=event, name="Unassigned", is_exhibitor=True)

        assert mail_helpers.build_exhibitor_context(event, assigned)["booth_number"] == "B12"
        assert mail_helpers.build_exhibitor_context(event, unassigned)["booth_number"] == "not assigned yet"


@pytest.mark.django_db
def test_lead_scanning_api_keeps_booth_id_for_the_app(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(
            event=event, name="Acme", is_exhibitor=True, booth_number="B12", allow_lead_access=True
        )
        Lead.objects.create(
            exhibitor=exhibitor,
            exhibitor_name="Acme",
            pseudonymization_id="P1",
            scanned=timezone.now(),
            scan_type="lead",
            device_name="Phone",
            booth_number="B12",
            booth_name="",
        )
        kwargs = {"organizer": event.organizer.slug, "event": event.slug}
        factory = APIRequestFactory()
        open_access = {"authentication_classes": [], "permission_classes": []}
        auth = ExhibitorAuthView.as_view(**open_access)(
            factory.post("/", {"key": exhibitor.key}, format="json"), **kwargs
        )
        leads = LeadRetrieveView.as_view(**open_access)(factory.get("/", HTTP_EXHIBITOR=exhibitor.key), **kwargs)

    assert auth.data["booth_number"] == auth.data["booth_id"] == "B12"
    assert leads.data["leads"][0]["booth_number"] == leads.data["leads"][0]["booth_id"] == "B12"


@pytest.mark.django_db
def test_leads_record_for_exhibitors_without_a_booth_number(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(
            event=event, name="Acme", is_exhibitor=True, lead_scanning_enabled=True
        )
        product = Product.objects.create(event=event, name="Ticket", default_price=10, active=True)
        order = Order.objects.create(
            code="LEAD1",
            event=event,
            email="attendee@example.com",
            status=Order.STATUS_PAID,
            datetime=timezone.now(),
            expires=timezone.now() + timedelta(days=10),
            total=Decimal("10.00"),
            locale="en",
        )
        position = OrderPosition.objects.create(order=order, product=product, price=Decimal("10.00"))
        request = APIRequestFactory().post(
            "/",
            {
                "lead": position.pseudonymization_id,
                "scanned": timezone.now().isoformat(),
                "scan_type": "lead",
                "device_name": "Phone",
            },
            format="json",
            HTTP_EXHIBITOR=exhibitor.key,
        )
        response = LeadCreateView.as_view(authentication_classes=[], permission_classes=[])(
            request, organizer=event.organizer.slug, event=event.slug
        )
        lead = Lead.objects.get(exhibitor=exhibitor)

    assert response.status_code == 201
    assert lead.booth_number == ""


@pytest.mark.django_db
def test_exhibitor_api_accepts_and_returns_the_legacy_booth_id(event):
    with scopes_disabled():
        serializer = ExhibitorInfoSerializer(partial=True, context={"event": event})
        assert serializer.to_internal_value({"booth_id": "B12"})["booth_number"] == "B12"
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", is_exhibitor=True, booth_number="A1")
        data = ExhibitorInfoSerializer(exhibitor, context={"event": event}).data

    assert data["booth_number"] == data["booth_id"] == "A1"
