import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionProposalForm
from exhibition.models import (
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitorSettings,
)
from exhibition.views import UserProposalCreateView, UserProposalEditView


def _settings_with_required_fields(event):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        call_enabled=True,
        proposal_field_settings={
            "name": {"active": True, "required": True},
            "email": {"active": True, "required": True},
            "logo": {"active": True, "required": True},
            "social_links": {"active": True, "required": True},
            "extra_links": {"active": True, "required": True},
        },
    )


def _proposal_post_data(**extra):
    data = {
        "social_links-TOTAL_FORMS": "0",
        "social_links-INITIAL_FORMS": "0",
        "extra_links-TOTAL_FORMS": "0",
        "extra_links-INITIAL_FORMS": "0",
    }
    data.update(extra)
    return data


@pytest.mark.django_db
def test_proposal_form_draft_save_allows_empty_mandatory_fields(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        question = ExhibitionQuestion.objects.create(
            event=event,
            question="Tell us about yourself",
            variant=ExhibitionQuestionVariant.TEXT,
            required=True,
            active=True,
        )

        submit_form = ExhibitionProposalForm(
            event=event,
            data={"action": "submit", "name": "", "email": ""},
            draft_save=False,
        )
        assert not submit_form.is_valid()
        assert "name" in submit_form.errors
        assert "email" in submit_form.errors
        assert f"question_{question.pk}" in submit_form.errors
        assert "logo" in submit_form.errors

        draft_form = ExhibitionProposalForm(
            event=event,
            data={"action": "draft", "name": "", "email": ""},
            draft_save=True,
        )
        assert draft_form.is_valid(), draft_form.errors
        proposal = draft_form.save(commit=False)
        assert proposal.name == ""


@pytest.mark.django_db
def test_user_proposal_create_view_draft_action_saves_incomplete_draft(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")

        request = RequestFactory().post("/", data=_proposal_post_data(action="draft", name=""))
        request.user = user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = UserProposalCreateView()
        view.request = request
        response = view.post(request)

        assert response.status_code == 302
        proposal = ExhibitionProposal.objects.filter(event=event, user=user).first()
        assert proposal is not None
        assert proposal.state == ExhibitionProposalState.DRAFT
        assert proposal.submitted is None
        assert str(proposal.name) == ""


@pytest.mark.django_db
def test_user_proposal_create_view_submit_action_enforces_mandatory_fields(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="applicant2@example.com", password="pw")

        request = RequestFactory().post("/", data=_proposal_post_data(action="submit", name=""))
        request.user = user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = UserProposalCreateView()
        view.request = request
        response = view.post(request)

        assert response.status_code == 200
        assert ExhibitionProposal.objects.filter(event=event, user=user).count() == 0


@pytest.mark.django_db
def test_user_proposal_edit_view_draft_and_submit_behavior(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="editor@example.com", password="pw")
        proposal = ExhibitionProposal.objects.create(
            event=event,
            user=user,
            name="",
            state=ExhibitionProposalState.DRAFT,
        )

        draft_request = RequestFactory().post("/", data=_proposal_post_data(action="draft", name=""))
        draft_request.user = user
        draft_request.event = event
        draft_request.session = {}
        setattr(draft_request, "_messages", FallbackStorage(draft_request))

        view = UserProposalEditView()
        view.object = proposal
        view.request = draft_request
        view.kwargs = {"code": proposal.code}
        response = view.post(draft_request, code=proposal.code)
        assert response.status_code == 302
        proposal.refresh_from_db()
        assert proposal.state == ExhibitionProposalState.DRAFT

        invalid_submit_request = RequestFactory().post(
            "/",
            data=_proposal_post_data(action="submit", name="", email=""),
        )
        invalid_submit_request.user = user
        invalid_submit_request.event = event
        invalid_submit_request.session = {}
        setattr(invalid_submit_request, "_messages", FallbackStorage(invalid_submit_request))

        view = UserProposalEditView()
        view.object = proposal
        view.request = invalid_submit_request
        view.kwargs = {"code": proposal.code}
        response = view.post(invalid_submit_request, code=proposal.code)
        assert response.status_code == 200
        proposal.refresh_from_db()
        assert proposal.state == ExhibitionProposalState.DRAFT


@pytest.mark.django_db
def test_post_with_formsets_draft_bypasses_link_formset_requirement(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="links@example.com", password="pw")

        request = RequestFactory().post(
            "/",
            data=_proposal_post_data(action="draft", name=""),
        )
        request.user = user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = UserProposalCreateView()
        view.request = request
        response = view.post(request)
        assert response.status_code == 302

        submit_request = RequestFactory().post(
            "/",
            data=_proposal_post_data(
                action="submit",
                name="Acme Corp",
                email="acme@example.com",
            ),
        )
        submit_request.user = user
        submit_request.event = event
        submit_request.session = {}
        setattr(submit_request, "_messages", FallbackStorage(submit_request))

        view = UserProposalCreateView()
        view.request = submit_request
        response = view.post(submit_request)
        assert response.status_code == 200
        assert view.social_media_formset.non_form_errors() == ["Add at least one social media link."]
        assert view.extra_links_formset.non_form_errors() == ["Add at least one extra link."]
