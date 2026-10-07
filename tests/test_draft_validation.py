from io import BytesIO

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django_scopes import scopes_disabled
from eventyay.base.models import Team
from eventyay.base.models.auth import User
from PIL import Image

from exhibition.forms import ExhibitionRequestForm
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorSettings,
)
from exhibition.views import UserRequestCreateView, UserRequestEditView


def _settings_with_required_fields(event):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        call_enabled=True,
        request_field_settings={
            "name": {"active": True, "required": True},
            "email": {"active": True, "required": True},
            "logo": {"active": True, "required": True},
            "social_links": {"active": True, "required": True},
            "extra_links": {"active": True, "required": True},
        },
    )


def _request_post_data(**extra):
    data = {
        "social_links-TOTAL_FORMS": "0",
        "social_links-INITIAL_FORMS": "0",
        "extra_links-TOTAL_FORMS": "0",
        "extra_links-INITIAL_FORMS": "0",
    }
    data.update(extra)
    return data


@pytest.mark.django_db
def test_request_form_draft_save_requires_name_but_allows_empty_other_mandatory_fields(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        question = ExhibitionQuestion.objects.create(
            event=event,
            question="Tell us about yourself",
            variant=ExhibitionQuestionVariant.TEXT,
            required=True,
            active=True,
        )

        submit_form = ExhibitionRequestForm(
            event=event,
            data={"action": "submit", "name": "", "email": ""},
            draft_save=False,
        )
        assert not submit_form.is_valid()
        assert "name" in submit_form.errors
        assert "email" in submit_form.errors
        assert f"question_{question.pk}" in submit_form.errors
        assert "logo" in submit_form.errors

        draft_form_without_name = ExhibitionRequestForm(
            event=event,
            data={"action": "draft", "name": "", "email": ""},
            draft_save=True,
        )
        assert not draft_form_without_name.is_valid()
        assert "name" in draft_form_without_name.errors
        assert "email" not in draft_form_without_name.errors
        assert f"question_{question.pk}" not in draft_form_without_name.errors
        assert "logo" not in draft_form_without_name.errors

        draft_form_with_name = ExhibitionRequestForm(
            event=event,
            data={"action": "draft", "name": "Acme Corp", "email": ""},
            draft_save=True,
        )
        assert draft_form_with_name.is_valid(), draft_form_with_name.errors
        exhibition_request = draft_form_with_name.save(commit=False)
        assert str(exhibition_request.name) == "Acme Corp"


@pytest.mark.django_db
def test_user_request_create_view_draft_action_requires_name_and_saves_incomplete_draft(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")

        # Draft save without name fails
        invalid_request = RequestFactory().post("/", data=_request_post_data(action="draft", name=""))
        invalid_request.user = user
        invalid_request.event = event
        invalid_request.session = {}
        setattr(invalid_request, "_messages", FallbackStorage(invalid_request))

        view = UserRequestCreateView()
        view.request = invalid_request
        response = view.post(invalid_request)

        assert response.status_code == 200
        assert ExhibitionRequest.objects.filter(event=event, user=user).count() == 0

        # Draft save with organization name succeeds even if other required fields are missing
        valid_request = RequestFactory().post("/", data=_request_post_data(action="draft", name="Acme Corp"))
        valid_request.user = user
        valid_request.event = event
        valid_request.session = {}
        setattr(valid_request, "_messages", FallbackStorage(valid_request))

        view = UserRequestCreateView()
        view.request = valid_request
        response = view.post(valid_request)

        assert response.status_code == 302
        exhibition_request = ExhibitionRequest.objects.filter(event=event, user=user).first()
        assert exhibition_request is not None
        assert exhibition_request.state == ExhibitionRequestState.DRAFT
        assert exhibition_request.submitted is None
        assert str(exhibition_request.name) == "Acme Corp"


@pytest.mark.django_db
def test_user_request_create_view_submit_action_enforces_mandatory_fields(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="applicant2@example.com", password="pw")

        # Missing name fails
        request = RequestFactory().post("/", data=_request_post_data(action="submit", name=""))
        request.user = user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = UserRequestCreateView()
        view.request = request
        response = view.post(request)

        assert response.status_code == 200
        assert ExhibitionRequest.objects.filter(event=event, user=user).count() == 0

        # Has name but missing required email/logo fails
        request2 = RequestFactory().post("/", data=_request_post_data(action="submit", name="Acme Corp"))
        request2.user = user
        request2.event = event
        request2.session = {}
        setattr(request2, "_messages", FallbackStorage(request2))

        view2 = UserRequestCreateView()
        view2.request = request2
        response2 = view2.post(request2)

        assert response2.status_code == 200
        assert ExhibitionRequest.objects.filter(event=event, user=user).count() == 0


@pytest.mark.django_db
def test_user_request_edit_view_draft_and_submit_behavior(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="editor@example.com", password="pw")
        team = Team.objects.create(
            organizer=event.organizer,
            all_events=True,
            is_exhibition_reviewer=True,
        )
        member = User.objects.create_user(
            email="reviewer@example.com",
            password="pw",
        )
        team.members.add(member)
        exhibition_request = ExhibitionRequest.objects.create(
            event=event,
            user=user,
            name="Acme Corp",
            state=ExhibitionRequestState.DRAFT,
        )

        # Draft save with empty name fails
        empty_name_request = RequestFactory().post("/", data=_request_post_data(action="draft", name=""))
        empty_name_request.user = user
        empty_name_request.event = event
        empty_name_request.session = {}
        setattr(empty_name_request, "_messages", FallbackStorage(empty_name_request))

        view = UserRequestEditView()
        view.object = exhibition_request
        view.request = empty_name_request
        view.kwargs = {"code": exhibition_request.code}
        response = view.post(empty_name_request, code=exhibition_request.code)
        assert response.status_code == 200
        exhibition_request.refresh_from_db()
        assert str(exhibition_request.name) == "Acme Corp"

        # Draft save with name succeeds without requiring email
        draft_request = RequestFactory().post("/", data=_request_post_data(action="draft", name="Acme Corp Updated"))
        draft_request.user = user
        draft_request.event = event
        draft_request.session = {}
        setattr(draft_request, "_messages", FallbackStorage(draft_request))

        view = UserRequestEditView()
        view.object = exhibition_request
        view.request = draft_request
        view.kwargs = {"code": exhibition_request.code}
        response = view.post(draft_request, code=exhibition_request.code)
        assert response.status_code == 302
        exhibition_request.refresh_from_db()
        assert str(exhibition_request.name) == "Acme Corp Updated"
        assert exhibition_request.state == ExhibitionRequestState.DRAFT

        image = BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, format="JPEG")
        image.seek(0)

        submit_request = RequestFactory().post(
            "/",
            data=_request_post_data(
                action="submit",
                name="Acme Corp Updated",
                email="applicant@example.com",
                logo=SimpleUploadedFile(
                    "test_logo.jpg",
                    image.read(),
                    content_type="image/jpeg",
                ),
                **{
                    "social_links-TOTAL_FORMS": "1",
                    "social_links-INITIAL_FORMS": "0",
                    "social_links-0-network": "github",
                    "social_links-0-path": "https://github.com/example",
                    "extra_links-TOTAL_FORMS": "1",
                    "extra_links-INITIAL_FORMS": "0",
                    "extra_links-0-label": "Example",
                    "extra_links-0-url": "https://example.com/extra",
                },
            ),
        )
        submit_request.user = user
        submit_request.event = event
        submit_request.session = {}
        setattr(submit_request, "_messages", FallbackStorage(submit_request))

        view = UserRequestEditView()
        view.object = exhibition_request
        view.request = submit_request
        view.kwargs = {"code": exhibition_request.code}
        with TestCase.captureOnCommitCallbacks(execute=True):
            response = view.post(submit_request, code=exhibition_request.code)

        assert response.status_code == 302
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.SUBMITTED
        assert ExhibitionEmailQueue.objects.filter(
            exhibition_request=exhibition_request,
            to_email="reviewer@example.com",
        ).exists()

        # Submit save with missing email fails
        invalid_submit_request = RequestFactory().post(
            "/",
            data=_request_post_data(action="submit", name="Acme Corp Updated", email=""),
        )
        invalid_submit_request.user = user
        invalid_submit_request.event = event
        invalid_submit_request.session = {}
        setattr(invalid_submit_request, "_messages", FallbackStorage(invalid_submit_request))

        view = UserRequestEditView()
        view.object = exhibition_request
        view.request = invalid_submit_request
        view.kwargs = {"code": exhibition_request.code}
        response = view.post(invalid_submit_request, code=exhibition_request.code)
        assert response.status_code == 200
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.SUBMITTED


@pytest.mark.django_db
def test_post_with_formsets_draft_bypasses_link_formset_requirement(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="links@example.com", password="pw")

        # Draft save with name does not require link formsets
        request = RequestFactory().post(
            "/",
            data=_request_post_data(action="draft", name="Acme Corp"),
        )
        request.user = user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = UserRequestCreateView()
        view.request = request
        response = view.post(request)
        assert response.status_code == 302

        # Submit save requires link formsets
        submit_request = RequestFactory().post(
            "/",
            data=_request_post_data(
                action="submit",
                name="Acme Corp",
                email="acme@example.com",
            ),
        )
        submit_request.user = user
        submit_request.event = event
        submit_request.session = {}
        setattr(submit_request, "_messages", FallbackStorage(submit_request))

        view = UserRequestCreateView()
        view.request = submit_request
        response = view.post(submit_request)
        assert response.status_code == 200
        assert view.social_media_formset.non_form_errors() == ["Add at least one social media link."]
        assert view.extra_links_formset.non_form_errors() == ["Add at least one extra link."]


@pytest.mark.django_db
def test_post_with_formsets_validates_formsets_when_proposal_form_is_invalid(event):
    with scopes_disabled():
        _settings_with_required_fields(event)
        user = User.objects.create_user(email="formset_validation@example.com", password="pw")

        # Form has missing name (invalid), but formset has one valid entry.
        # This verifies that formsets are validated before formset_has_entries checks cleaned_data,
        # avoiding short-circuit evaluation errors.
        request = RequestFactory().post(
            "/",
            data=_request_post_data(
                action="submit",
                name="",
                **{
                    "social_links-TOTAL_FORMS": "1",
                    "social_links-INITIAL_FORMS": "0",
                    "social_links-0-link": "https://example.com/social",
                },
            ),
        )
        request.user = user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = UserRequestCreateView()
        view.request = request
        response = view.post(request)

        assert response.status_code == 200
        assert "name" in view.get_form().errors
