from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionRequestReviewForm, ExhibitionRequestReviewNotesForm
from exhibition.models import ExhibitionRequest, ExhibitionRequestState
from exhibition.utils import should_hide_applicant_emails
from exhibition.views import RequestDetailView


def _request(event, email, state=ExhibitionRequestState.SUBMITTED):
    submitter = User.objects.create_user(email=email, password="pw")
    return ExhibitionRequest.objects.create(event=event, user=submitter, name="Org", state=state)


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
    view = RequestDetailView()
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
def test_no_hide_for_request_manager_even_with_flag(event):
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
        assert view.get_form_class() is ExhibitionRequestReviewNotesForm


@pytest.mark.django_db
def test_manager_gets_full_review_form(event):
    with scopes_disabled():
        user = _member(event, "mg-form@e.com", can_change_exhibition_proposals=True)
        exhibition_request = _request(event, "sub-form@e.com", state=ExhibitionRequestState.SUBMITTED)
        view = _detail_view(event, user)
        view.object = exhibition_request
        assert view.can_manage() is True
        assert view.can_review() is True
        assert view.get_form_class() is ExhibitionRequestReviewForm


@pytest.mark.django_db
def test_manager_gets_notes_form_for_draft_request(event):
    with scopes_disabled():
        user = _member(event, "mg-draft@e.com", can_change_exhibition_proposals=True)
        exhibition_request = _request(event, "sub-draft@e.com", state=ExhibitionRequestState.DRAFT)
        view = _detail_view(event, user)
        view.object = exhibition_request
        assert view.can_manage() is True
        assert view.can_review() is False
        assert view.get_form_class() is ExhibitionRequestReviewNotesForm


@pytest.mark.django_db
def test_manager_gets_notes_form_for_rejected_request(event):
    with scopes_disabled():
        user = _member(event, "mg-rejected@e.com", can_change_exhibition_proposals=True)
        exhibition_request = _request(event, "sub-rejected@e.com", state=ExhibitionRequestState.REJECTED)
        view = _detail_view(event, user)
        view.object = exhibition_request
        assert view.can_manage() is True
        assert view.can_review() is False
        assert view.get_form_class() is ExhibitionRequestReviewNotesForm


@pytest.mark.django_db
def test_manager_gets_notes_form_for_accepted_request(event):
    with scopes_disabled():
        user = _member(event, "mg-accepted@e.com", can_change_exhibition_proposals=True)
        exhibition_request = _request(event, "sub-accepted@e.com", state=ExhibitionRequestState.ACCEPTED)
        view = _detail_view(event, user)
        view.object = exhibition_request
        assert view.can_manage() is True
        assert view.can_review() is False
        assert view.get_form_class() is ExhibitionRequestReviewNotesForm
