from unittest import mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models import User

from exhibition.forms import ExhibitionComposeForm
from exhibition.models import (
    ExhibitionEmailDraft,
    ExhibitionEmailQueue,
    ExhibitionRequest,
    ExhibitionRequestState,
)
from exhibition.views import EmailComposeView


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _request(event, name="Acme"):
    user = User.objects.create_user(email=f"{name.lower()}-user@example.com", password="pw")
    with scopes_disabled():
        return ExhibitionRequest.objects.create(
            event=event,
            user=user,
            name=name,
            state=ExhibitionRequestState.ACCEPTED,
            email=f"{name.lower()}@example.com",
        )


def _i18n(event, **values):
    form = ExhibitionComposeForm(event=event)
    return {f"{name}_{form.fields[name].widget.locales.index('en')}": value for name, value in values.items()}


def _view(event, post):
    request = RequestFactory().post("/", data=post)
    request.event = event
    request.user = AnonymousUser()
    request.session = {}
    request._messages = FallbackStorage(request)
    view = EmailComposeView()
    view.request = request
    view.kwargs = {}
    return view


def _data(event, **extra):
    return {
        "state": "accepted",
        "organization_type": "",
        **_i18n(event, subject="Hello", body="<p>Welcome</p>"),
        **extra,
    }


def _form(view, data):
    return ExhibitionComposeForm(data=data, **{k: v for k, v in view.get_form_kwargs().items() if k != "data"})


@pytest.mark.django_db
def test_reply_to_and_bcc_are_validated_and_tidied(mail_event):
    view = _view(mail_event, {})
    bad = _form(view, _data(mail_event, reply_to="nope", bcc="a@example.com, broken"))
    assert not bad.is_valid()
    assert set(bad.errors) == {"reply_to", "bcc"}

    good = _form(view, _data(mail_event, reply_to=" me@example.com ", bcc="a@example.com ,b@example.com,"))
    assert good.is_valid(), good.errors
    assert good.cleaned_data["reply_to"] == "me@example.com"
    assert good.cleaned_data["bcc"] == "a@example.com, b@example.com"


@pytest.mark.django_db
def test_queued_emails_carry_reply_to_and_bcc(mail_event):
    _request(mail_event)
    view = _view(mail_event, {"_save": "save"})
    with scopes_disabled():
        form = _form(view, _data(mail_event, reply_to="me@example.com", bcc="a@example.com"))
        assert form.is_valid(), form.errors
        view.form_valid(form)
        queued = ExhibitionEmailQueue.objects.get(event=mail_event)
    assert queued.reply_to == "me@example.com"
    assert queued.bcc == "a@example.com"


@pytest.mark.django_db
def test_sending_a_queued_email_passes_the_bcc_on(mail_event):
    with scopes_disabled():
        queued = ExhibitionEmailQueue.objects.create(
            event=mail_event,
            to_email="x@example.com",
            subject="S",
            body="B",
            reply_to="r@example.com",
            bcc="a@example.com",
        )
        with mock.patch("eventyay.base.services.mail.mail") as send:
            queued.send()
    assert send.call_args.kwargs["event_bcc"] == "a@example.com"
    assert send.call_args.kwargs["event_reply_to"] == "r@example.com"


@pytest.mark.django_db
def test_a_draft_keeps_reply_to_and_bcc(mail_event):
    view = _view(mail_event, {"_draft": "draft"})
    with scopes_disabled():
        form = _form(view, _data(mail_event, reply_to="me@example.com", bcc="a@example.com"))
        assert form.is_valid(), form.errors
        view.form_valid(form)
        draft = ExhibitionEmailDraft.objects.get(event=mail_event)
    assert draft.form_initial()["reply_to"] == "me@example.com"
    assert draft.form_initial()["bcc"] == "a@example.com"


@pytest.mark.django_db
def test_test_email_is_sent_without_queueing_anything(mail_event):
    _request(mail_event)
    view = _view(mail_event, {"_test": "test"})
    with scopes_disabled():
        form = _form(view, _data(mail_event, test_email="me@example.com", bcc="a@example.com"))
        assert form.is_valid(), form.errors
        with mock.patch("eventyay.base.services.mail.mail") as send:
            view.form_valid(form)
        assert not ExhibitionEmailQueue.objects.exists()
    assert send.call_args.kwargs["email"] == "me@example.com"
    assert send.call_args.kwargs["sync_send"] is True
    assert send.call_args.kwargs.get("event_bcc") is None


@pytest.mark.django_db
def test_test_email_needs_an_address(mail_event):
    view = _view(mail_event, {"_test": "test"})
    with scopes_disabled():
        form = _form(view, _data(mail_event))
        assert form.is_valid(), form.errors
        with mock.patch("eventyay.base.services.mail.mail") as send:
            view.form_valid(form)
    assert "test_email" in form.errors
    send.assert_not_called()


@pytest.mark.django_db
def test_test_email_ignores_a_send_time_that_has_passed(mail_event):
    past = {"scheduled_at": "2000-01-01 10:00"}
    queue_view = _view(mail_event, {"_send": "send"})
    with scopes_disabled():
        assert "scheduled_at" in _form(queue_view, _data(mail_event, **past)).errors

        view = _view(mail_event, {"_test": "test"})
        form = _form(view, _data(mail_event, test_email="me@example.com", **past))
        assert form.is_valid(), form.errors
        with mock.patch("eventyay.base.services.mail.mail") as send:
            view.form_valid(form)
    send.assert_called_once()


@pytest.mark.django_db
def test_test_email_is_not_blocked_by_an_unfinished_bcc_list(mail_event):
    data = _data(mail_event, test_email="me@example.com", bcc="archive@")
    with scopes_disabled():
        assert "bcc" in _form(_view(mail_event, {"_send": "send"}), data).errors
        assert _form(_view(mail_event, {"_test": "test"}), data).is_valid()
