import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionProposalForm
from exhibition.models import (
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitorInfo,
    ExhibitorSettings,
)
from exhibition.views import UserProposalEditView


def _settings_with_required_email(event):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        proposal_field_settings={"email": {"active": True, "required": True}},
    )


def _accepted_proposal(event):
    user = User.objects.create_user(email="submitter@example.com", password="pw")
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", email="old@example.com")
    proposal = ExhibitionProposal.objects.create(
        event=event,
        user=user,
        name="Acme",
        email="old@example.com",
        state=ExhibitionProposalState.ACCEPTED,
        approved_exhibitor=exhibitor,
    )
    return proposal, exhibitor


def _edit_view(proposal, request):
    view = UserProposalEditView()
    view.object = proposal
    view.request = request
    return view


@pytest.mark.django_db
def test_draft_action_is_ignored_by_get_form_kwargs_when_accepted(event):
    with scopes_disabled():
        _settings_with_required_email(event)
        proposal, _ = _accepted_proposal(event)
        request = RequestFactory().post("/", data={"action": "draft", "name_0": "Acme", "email": ""})
        request.user = proposal.user
        view = _edit_view(proposal, request)
        assert view.get_form_kwargs()["draft_save"] is False


@pytest.mark.django_db
def test_crafted_draft_action_cannot_bypass_required_field_validation(event):
    with scopes_disabled():
        _settings_with_required_email(event)
        proposal, exhibitor = _accepted_proposal(event)
        request = RequestFactory().post("/", data={"action": "draft", "name_0": "Acme", "email": ""})
        request.user = proposal.user

        view = _edit_view(proposal, request)
        form = ExhibitionProposalForm(**view.get_form_kwargs())

        assert not form.is_valid()
        assert "email" in form.errors

        exhibitor.refresh_from_db()
        assert exhibitor.email == "old@example.com"


@pytest.mark.django_db
def test_form_valid_syncs_accepted_proposal_and_marks_edited(event):
    with scopes_disabled():
        _settings_with_required_email(event)
        proposal, exhibitor = _accepted_proposal(event)
        assert proposal.profile_edited_at is None

        request = RequestFactory().post(
            "/",
            data={"action": "draft", "name_0": "Acme", "email": "new@example.com"},
        )
        request.user = proposal.user
        request.event = event
        setattr(request, "_messages", FallbackStorage(request))

        view = _edit_view(proposal, request)
        form = ExhibitionProposalForm(**view.get_form_kwargs())
        assert form.is_valid(), form.errors

        view.social_media_formset = None
        view.extra_links_formset = None
        view.form_valid(form)

        proposal.refresh_from_db()
        exhibitor.refresh_from_db()
        assert proposal.state == ExhibitionProposalState.ACCEPTED
        assert proposal.profile_edited_at is not None
        assert exhibitor.email == "new@example.com"
