import base64

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionProposalForm
from exhibition.models import (
    PROPOSAL_DEFAULT_FIELD_KEYS,
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitorInfo,
    ExhibitorSettings,
)
from exhibition.views import UserProposalEditView

_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _locked_image_uploads():
    """Logo and header image stay locked-active, so any valid form post must include them."""
    return {
        "logo": SimpleUploadedFile("logo.png", _PNG_BYTES, content_type="image/png"),
        "header_image": SimpleUploadedFile("header.png", _PNG_BYTES, content_type="image/png"),
    }


def _settings_with_required_url(event):
    field_settings = {key: {"active": False, "required": False} for key in PROPOSAL_DEFAULT_FIELD_KEYS}
    field_settings["url"] = {"active": True, "required": True}
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        proposal_field_settings=field_settings,
    )


def _accepted_proposal(event):
    user = User.objects.create_user(email="submitter@example.com", password="pw")
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", url="https://old.example.com")
    proposal = ExhibitionProposal.objects.create(
        event=event,
        user=user,
        name="Acme",
        url="https://old.example.com",
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
        _settings_with_required_url(event)
        proposal, _ = _accepted_proposal(event)
        request = RequestFactory().post("/", data={"action": "draft", "name": "Acme", "url": ""})
        request.user = proposal.user
        request.event = event
        view = _edit_view(proposal, request)
        assert view.get_form_kwargs()["draft_save"] is False


@pytest.mark.django_db
def test_crafted_draft_action_cannot_bypass_required_field_validation(event):
    with scopes_disabled():
        _settings_with_required_url(event)
        proposal, exhibitor = _accepted_proposal(event)
        request = RequestFactory().post("/", data={"action": "draft", "name": "Acme", "url": ""})
        request.user = proposal.user
        request.event = event

        view = _edit_view(proposal, request)
        form = ExhibitionProposalForm(**view.get_form_kwargs())

        assert not form.is_valid()
        assert "url" in form.errors

        exhibitor.refresh_from_db()
        assert exhibitor.url == "https://old.example.com"


@pytest.mark.django_db
def test_form_valid_syncs_accepted_proposal_and_marks_edited(event):
    with scopes_disabled():
        _settings_with_required_url(event)
        proposal, exhibitor = _accepted_proposal(event)
        assert proposal.profile_edited_at is None
        assert proposal.accepted_profile_snapshot is None

        request = RequestFactory().post(
            "/",
            data={
                "action": "draft",
                "name": "Acme",
                "url": "https://new.example.com",
                **_locked_image_uploads(),
            },
        )
        request.user = proposal.user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = _edit_view(proposal, request)
        form = ExhibitionProposalForm(**view.get_form_kwargs())
        assert form.is_valid(), form.errors

        view.social_media_formset = None
        view.form_valid(form)

        proposal.refresh_from_db()
        exhibitor.refresh_from_db()
        assert proposal.state == ExhibitionProposalState.ACCEPTED
        assert proposal.profile_edited_at is not None
        assert exhibitor.url == "https://new.example.com"

        assert proposal.accepted_profile_snapshot["url"] == "https://old.example.com"
        changes = {change["label"]: change for change in proposal.profile_field_changes()}
        assert changes["Organization Website"]["old"] == "https://old.example.com"
        assert changes["Organization Website"]["new"] == "https://new.example.com"
