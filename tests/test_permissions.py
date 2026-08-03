from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User
from rest_framework import status

from exhibition.api import ExhibitorAuthView, LeadCreateView, LeadRetrieveView, LeadUpdateView, TagListView
from exhibition.forms import ExhibitionProposalReviewForm, ExhibitionProposalReviewNotesForm
from exhibition.models import (
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitorInfo,
    ExhibitorSettings,
    Lead,
)
from exhibition.utils import should_hide_applicant_emails
from exhibition.views import ProposalDetailView


def _proposal(event, email, state=ExhibitionProposalState.SUBMITTED):
    submitter = User.objects.create_user(email=email, password="pw")
    return ExhibitionProposal.objects.create(event=event, user=submitter, name="Org", state=state)


def _member(event, email, **flags):
    user = User.objects.create_user(email=email, password="pw")
    team = event.organizer.teams.create(name=email, all_events=True, **flags)
    team.members.add(user)
    return user


def _detail_view(event, user):
    request = RequestFactory().get("/")
    request.user = user
    request.event = event
    request.session = SimpleNamespace(session_key=None)
    view = ProposalDetailView()
    view.request = request
    return view


@pytest.mark.django_db
def test_hide_emails_for_reviewer_with_flag(event):
    with scopes_disabled():
        user = _member(event, "r-hide@e.com", is_exhibition_reviewer=True, hide_exhibition_applicant_emails=True)
        assert should_hide_applicant_emails(user, event) is True


@pytest.mark.django_db
def test_no_hide_for_reviewer_without_flag(event):
    with scopes_disabled():
        user = _member(event, "r-plain@e.com", is_exhibition_reviewer=True)
        assert should_hide_applicant_emails(user, event) is False


@pytest.mark.django_db
def test_no_hide_for_proposal_manager_even_with_flag(event):
    with scopes_disabled():
        user = _member(
            event,
            "m-hide@e.com",
            can_change_exhibition_proposals=True,
            is_exhibition_reviewer=True,
            hide_exhibition_applicant_emails=True,
        )
        assert should_hide_applicant_emails(user, event) is False


@pytest.mark.django_db
def test_no_hide_without_exhibition_team(event):
    with scopes_disabled():
        user = _member(event, "orders@e.com", can_view_orders=True)
        assert should_hide_applicant_emails(user, event) is False


@pytest.mark.django_db
def test_reviewer_gets_notes_only_form(event):
    with scopes_disabled():
        user = _member(event, "rv-form@e.com", is_exhibition_reviewer=True)
        view = _detail_view(event, user)
        assert view.can_manage() is False
        assert view.get_form_class() is ExhibitionProposalReviewNotesForm


@pytest.mark.django_db
def test_manager_gets_full_review_form(event):
    with scopes_disabled():
        user = _member(event, "mg-form@e.com", can_change_exhibition_proposals=True)
        proposal = _proposal(event, "sub-form@e.com", state=ExhibitionProposalState.SUBMITTED)
        view = _detail_view(event, user)
        view.object = proposal
        assert view.can_manage() is True
        assert view.can_review() is True
        assert view.get_form_class() is ExhibitionProposalReviewForm


@pytest.mark.django_db
def test_manager_gets_notes_form_for_non_submitted_proposal(event):
    with scopes_disabled():
        user = _member(event, "mg-terminal@e.com", can_change_exhibition_proposals=True)
        proposal = _proposal(event, "sub-terminal@e.com", state=ExhibitionProposalState.REJECTED)
        view = _detail_view(event, user)
        view.object = proposal
        assert view.can_manage() is True
        assert view.can_review() is False
        assert view.get_form_class() is ExhibitionProposalReviewNotesForm


def _exhibitor(event, **kwargs):
    return ExhibitorInfo.objects.create(event=event, name="Acme", **kwargs)


def _request_with_key(path, key):
    request = RequestFactory().get(path, **{"HTTP_EXHIBITOR": key})
    return request


@pytest.mark.django_db
def test_lead_retrieve_denied_without_allow_lead_access(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=False)
        request = _request_with_key("/", exhibitor.key)
        response = LeadRetrieveView.as_view()(request)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["success"] is False


@pytest.mark.django_db
def test_lead_retrieve_allowed_with_allow_lead_access(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=True)
        Lead.objects.create(
            exhibitor=exhibitor,
            exhibitor_name="Acme",
            pseudonymization_id="abc123",
            scanned=timezone.now(),
            scan_type="qr",
            device_name="scanner-1",
            booth_id=exhibitor.booth_id or "",
            booth_name="",
            attendee={"name": "Alice"},
        )
        request = _request_with_key("/", exhibitor.key)
        response = LeadRetrieveView.as_view()(request)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True
        assert len(response.data["leads"]) == 1


@pytest.mark.django_db
def test_lead_update_denied_without_allow_lead_access(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=False)
        lead = Lead.objects.create(
            exhibitor=exhibitor,
            exhibitor_name="Acme",
            pseudonymization_id="abc123",
            scanned=timezone.now(),
            scan_type="qr",
            device_name="scanner-1",
            booth_id=exhibitor.booth_id or "",
            booth_name="",
            attendee={},
        )
        request = RequestFactory().post(
            "/",
            data={"note": "hi", "tags": []},
            content_type="application/json",
            **{"HTTP_EXHIBITOR": exhibitor.key},
        )
        response = LeadUpdateView.as_view()(
            request, organizer="o", event="e", lead_id=lead.pseudonymization_id
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_tag_list_denied_without_allow_lead_access(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=False)
        request = _request_with_key("/", exhibitor.key)
        response = TagListView.as_view()(request, organizer="o", event="e")
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_tag_list_allowed_with_allow_lead_access(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_lead_access=True)
        request = _request_with_key("/", exhibitor.key)
        response = TagListView.as_view()(request, organizer="o", event="e")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True


@pytest.mark.django_db
def test_exhibitor_auth_exposes_access_flags(event):
    with scopes_disabled():
        exhibitor = _exhibitor(
            event,
            lead_scanning_enabled=True,
            allow_lead_access=True,
            allow_voucher_access=False,
        )
        request = RequestFactory().post("/", data={"key": exhibitor.key})
        response = ExhibitorAuthView.as_view()(request)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["lead_scanning_enabled"] is True
        assert response.data["allow_lead_access"] is True
        assert response.data["allow_voucher_access"] is False


def _lead_create_request(key, **overrides):
    data = {
        "lead": "abc123",
        "scanned": "2026-01-01T00:00:00Z",
        "scan_type": "qr",
        "device_name": "scanner-1",
    }
    data.update(overrides)
    return RequestFactory().post(
        "/",
        data=data,
        content_type="application/json",
        **{"HTTP_EXHIBITOR": key},
    )


@pytest.mark.django_db
def test_lead_create_denied_without_lead_scanning_enabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=False)
        request = _lead_create_request(exhibitor.key)
        response = LeadCreateView.as_view()(request)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["success"] is False


@pytest.mark.django_db
def test_lead_create_allowed_with_lead_scanning_enabled(event, monkeypatch):
    with scopes_disabled():
        exhibitor = _exhibitor(event, lead_scanning_enabled=True, allow_voucher_access=True)
        settings = ExhibitorSettings.objects.create(event=event)
        settings.allowed_fields = ["attendee_name"]
        settings.save()

        order_position = SimpleNamespace(
            attendee_name="Alice",
            attendee_email="alice@example.com",
            company="Acme",
            job_title="Engineer",
            street="Main St",
            zipcode="12345",
            city="Springfield",
            country="US",
            answers=SimpleNamespace(all=lambda: []),
            order=SimpleNamespace(event=event),
        )
        import exhibition.api as api_module

        monkeypatch.setattr(api_module.OrderPosition.objects, "get", lambda **kwargs: order_position)

        request = _lead_create_request(exhibitor.key)
        response = LeadCreateView.as_view()(request)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["success"] is True
        assert response.data["attendee"]["name"] == "Alice"
        assert Lead.objects.filter(exhibitor=exhibitor, pseudonymization_id="abc123").exists()
