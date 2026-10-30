import base64
from datetime import timedelta

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.utils.datastructures import MultiValueDict
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import CachedFile
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionRequestForm
from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitorSettings,
)
from exhibition.uploads import RETAINED_UPLOAD_SUFFIX, RetainedFileInput
from exhibition.views import UserRequestCreateView

_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _call_settings(event):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        call_enabled=True,
        request_field_settings={
            "name": {"active": True, "required": True},
            "logo": {"active": True, "required": True},
        },
    )


def _post(event, user, session, data, files=None):
    payload = {
        "action": "draft",
        "social_links-TOTAL_FORMS": "0",
        "social_links-INITIAL_FORMS": "0",
        "extra_links-TOTAL_FORMS": "0",
        "extra_links-INITIAL_FORMS": "0",
        **data,
        **(files or {}),
    }
    request = RequestFactory().post("/", data=payload)
    request.user = user
    request.event = event
    request.session = session
    request._messages = FallbackStorage(request)
    view = UserRequestCreateView()
    view.request = request
    view.kwargs = {}
    return view, view.post(request)


def _logo():
    return SimpleUploadedFile("logo.png", _PNG_BYTES, content_type="image/png")


def _session():
    session = SessionStore()
    session.create()
    return session


@pytest.mark.django_db
def test_failed_submit_keeps_the_chosen_files_and_saves_them_on_resubmit(event):
    with scopes_disabled():
        _call_settings(event)
        question = ExhibitionQuestion.objects.create(
            event=event,
            question="Brochure",
            variant=ExhibitionQuestionVariant.FILE,
            active=True,
        )
        user = User.objects.create_user(email="applicant@example.com", password="pw")
        session = _session()
        answer_field = f"question_{question.pk}"

        view, response = _post(
            event,
            user,
            session,
            {"name": ""},
            {"logo": _logo(), answer_field: SimpleUploadedFile("brochure.pdf", b"%PDF-1.4", "application/pdf")},
        )
        form = response.context_data["form"]
        kept_logo = form.fields["logo"].widget.retained
        kept_answer = form.fields[answer_field].widget.retained

        assert response.status_code == 200
        assert isinstance(form.fields["logo"].widget, RetainedFileInput)
        assert kept_logo.session_key == session.session_key
        assert kept_answer.filename == "brochure.pdf"
        assert f'name="logo{RETAINED_UPLOAD_SUFFIX}" value="{kept_logo.id}" checked' in form["logo"].as_widget()

        _view, response = _post(
            event,
            user,
            session,
            {
                "name": "Acme",
                f"logo{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id),
                f"{answer_field}{RETAINED_UPLOAD_SUFFIX}": str(kept_answer.id),
            },
        )
        exhibition_request = ExhibitionRequest.objects.get(event=event, user=user)
        answer = ExhibitionAnswer.objects.get(exhibition_request=exhibition_request, question=question)

    assert response.status_code == 302
    assert exhibition_request.logo.name.endswith(".png")
    assert answer.file.name.endswith(".pdf")


@pytest.mark.django_db
def test_kept_files_cannot_be_reused_from_another_session(event):
    with scopes_disabled():
        _call_settings(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")

        _view, response = _post(event, user, _session(), {"name": ""}, {"logo": _logo()})
        kept_logo = response.context_data["form"].fields["logo"].widget.retained

        _view, response = _post(
            event, user, _session(), {"name": "Acme", f"logo{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id)}
        )
        exhibition_request = ExhibitionRequest.objects.get(event=event, user=user)

    assert response.status_code == 302
    assert not exhibition_request.logo


@pytest.mark.django_db
def test_clearing_a_field_drops_the_kept_file(event):
    with scopes_disabled():
        _call_settings(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")
        session = _session()

        _view, response = _post(event, user, session, {"name": ""}, {"logo": _logo()})
        kept_logo = response.context_data["form"].fields["logo"].widget.retained

        _view, response = _post(
            event,
            user,
            session,
            {"name": "Acme", f"logo{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id), "logo-clear": "on"},
        )
        exhibition_request = ExhibitionRequest.objects.get(event=event, user=user)

    assert response.status_code == 302
    assert not exhibition_request.logo


@pytest.mark.django_db
def test_a_file_with_an_error_is_not_kept(event):
    with scopes_disabled():
        _call_settings(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")

        _view, response = _post(
            event,
            user,
            _session(),
            {"name": ""},
            {"logo": SimpleUploadedFile("logo.png", b"not an image", content_type="image/png")},
        )
        form = response.context_data["form"]

    assert "logo" in form.errors
    assert not isinstance(form.fields["logo"].widget, RetainedFileInput)
    assert not CachedFile.objects.exists()


@pytest.mark.django_db
def test_only_real_file_fields_are_restored(event):
    with scopes_disabled():
        _call_settings(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")
        session = _session()

        view, response = _post(event, user, session, {"name": ""}, {"logo": _logo()})
        kept_logo = response.context_data["form"].fields["logo"].widget.retained
        form = ExhibitionRequestForm(
            event=event,
            data={
                "name": "Acme",
                f"logo{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id),
                f"name{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id),
                f"made-up{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id),
            },
            files=MultiValueDict(),
            draft_save=True,
        )
        view.restore_retained_uploads(form)

    assert list(form.files) == ["logo"]
    assert form.files["logo"].retained_from == kept_logo


@pytest.mark.django_db
def test_an_expired_kept_file_asks_to_choose_the_file_again(event):
    with scopes_disabled():
        _call_settings(event)
        user = User.objects.create_user(email="applicant@example.com", password="pw")
        session = _session()

        _view, response = _post(event, user, session, {"name": ""}, {"logo": _logo()})
        kept_logo = response.context_data["form"].fields["logo"].widget.retained
        CachedFile.objects.filter(pk=kept_logo.pk).update(expires=now() - timedelta(minutes=1))

        _view, response = _post(
            event, user, session, {"name": "Acme", f"logo{RETAINED_UPLOAD_SUFFIX}": str(kept_logo.id)}
        )

    assert response.status_code == 200
    assert "logo" in response.context_data["form"].errors
    assert not ExhibitionRequest.objects.filter(event=event, user=user).exists()
