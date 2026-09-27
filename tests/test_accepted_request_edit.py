import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionRequestForm
from exhibition.models import (
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
    ExhibitorSettings,
)
from exhibition.views import UserRequestEditView


def _settings_with_required_email(event):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        request_field_settings={"email": {"active": True, "required": True}},
    )


def _accepted_request(event):
    user = User.objects.create_user(email="submitter@example.com", password="pw")
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", email="old@example.com")
    exhibition_request = ExhibitionRequest.objects.create(
        event=event,
        user=user,
        name="Acme",
        email="old@example.com",
        state=ExhibitionRequestState.ACCEPTED,
        approved_exhibitor=exhibitor,
    )
    return exhibition_request, exhibitor


def _edit_view(exhibition_request, request):
    view = UserRequestEditView()
    view.object = exhibition_request
    view.request = request
    return view


@pytest.mark.django_db
def test_draft_action_is_ignored_by_get_form_kwargs_when_accepted(event):
    with scopes_disabled():
        _settings_with_required_email(event)
        exhibition_request, _ = _accepted_request(event)
        request = RequestFactory().post("/", data={"action": "draft", "name": "Acme", "email": ""})
        request.user = exhibition_request.user
        request.event = event
        view = _edit_view(exhibition_request, request)
        assert view.get_form_kwargs()["draft_save"] is False


@pytest.mark.django_db
def test_crafted_draft_action_cannot_bypass_required_field_validation(event):
    with scopes_disabled():
        _settings_with_required_email(event)
        exhibition_request, exhibitor = _accepted_request(event)
        request = RequestFactory().post("/", data={"action": "draft", "name": "Acme", "email": ""})
        request.user = exhibition_request.user
        request.event = event

        view = _edit_view(exhibition_request, request)
        form = ExhibitionRequestForm(**view.get_form_kwargs())

        assert not form.is_valid()
        assert "email" in form.errors

        exhibitor.refresh_from_db()
        assert exhibitor.email == "old@example.com"


@pytest.mark.django_db
def test_form_valid_syncs_accepted_request_and_marks_edited(event):
    with scopes_disabled():
        _settings_with_required_email(event)
        exhibition_request, exhibitor = _accepted_request(event)
        assert exhibition_request.profile_edited_at is None
        assert exhibition_request.accepted_profile_snapshot is None

        request = RequestFactory().post(
            "/",
            data={"action": "draft", "name": "Acme", "email": "new@example.com"},
        )
        request.user = exhibition_request.user
        request.event = event
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))

        view = _edit_view(exhibition_request, request)
        form = ExhibitionRequestForm(**view.get_form_kwargs())
        assert form.is_valid(), form.errors

        view.social_media_formset = None
        view.extra_links_formset = None
        view.form_valid(form)

        exhibition_request.refresh_from_db()
        exhibitor.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED
        assert exhibition_request.profile_edited_at is not None
        assert exhibitor.email == "new@example.com"

        assert exhibition_request.accepted_profile_snapshot["email"] == "old@example.com"
        changes = {change["label"]: change for change in exhibition_request.profile_field_changes()}
        assert changes["Contact email"]["old"] == "old@example.com"
        assert changes["Contact email"]["new"] == "new@example.com"
