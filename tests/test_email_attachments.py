import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models import CachedFile
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.forms import ExhibitionComposeForm, ExhibitionEmailQueueForm
from exhibition.models import ExhibitionEmailDraft, ExhibitionEmailQueue, ExhibitionRequest, ExhibitionRequestState
from exhibition.views import EmailComposeView, EmailEditView, group_email_entries


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _applicant(event, name):
    user = User.objects.create_user(email=f"{name.lower()}-user@example.com", password="pw")
    with scopes_disabled():
        return ExhibitionRequest.objects.create(
            event=event,
            user=user,
            name=name,
            email=f"{name.lower()}@example.com",
            state=ExhibitionRequestState.ACCEPTED,
        )


def _upload(name="guide.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 guide", content_type="application/pdf")


def _http_request(event, post=None, draft=None):
    request = RequestFactory().post("/", data=post or {})
    request.event = event
    request.user = AnonymousUser()
    request.session = {}
    request._messages = FallbackStorage(request)
    request.GET = request.GET.copy()
    if draft is not None:
        request.GET["draft"] = str(draft.pk)
    return request


def _compose_view(event, post=None, draft=None):
    view = EmailComposeView()
    view.request = _http_request(event, post, draft)
    view.kwargs = {}
    return view


def _compose_data(event, **values):
    form = ExhibitionComposeForm(event=event)
    return {f"{name}_{form.fields[name].widget.locales.index('en')}": value for name, value in values.items()}


def _submit(view, data, files=None):
    kwargs = {key: value for key, value in view.get_form_kwargs().items() if key not in ("data", "files")}
    form = ExhibitionComposeForm(data=data, files=files or {}, **kwargs)
    assert form.is_valid(), form.errors
    return view.form_valid(form), form


@pytest.mark.django_db
def test_every_recipient_gets_the_uploaded_attachment(mail_event):
    _applicant(mail_event, "Acme")
    _applicant(mail_event, "Globex")
    with scopes_disabled():
        _submit(
            _compose_view(mail_event),
            {"state": "accepted", **_compose_data(mail_event, subject="Hi", body="<p>Floor plan attached</p>")},
            {"attachment": _upload()},
        )
        rows = list(ExhibitionEmailQueue.objects.filter(event=mail_event))

    assert len(rows) == 2
    assert len({row.attachment_id for row in rows}) == 1
    assert rows[0].attachment.filename == "guide.pdf"
    assert rows[0].attachment.expires is None


@pytest.mark.django_db
def test_compose_form_rejects_unsupported_file_types(mail_event):
    form = ExhibitionComposeForm(
        data={"state": "accepted", **_compose_data(mail_event, subject="Hi", body="<p>Body</p>")},
        files={"attachment": SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream")},
        event=mail_event,
    )

    assert not form.is_valid()
    assert "attachment" in form.errors


@pytest.mark.django_db
def test_draft_keeps_its_attachment_until_queued(mail_event):
    _applicant(mail_event, "Acme")
    with scopes_disabled():
        _submit(
            _compose_view(mail_event, post={"_draft": "draft"}),
            {"state": "accepted", **_compose_data(mail_event, subject="Hi", body="<p>Body</p>")},
            {"attachment": _upload()},
        )
        draft = ExhibitionEmailDraft.objects.get(event=mail_event)
        assert draft.attachment.expires is None
        assert _compose_view(mail_event, draft=draft).get_initial()["attachment"] == draft.attachment

        view = _compose_view(mail_event, post={"_save": "save"}, draft=draft)
        _submit(
            view,
            {
                "state": "accepted",
                "attachment-cachedfile": str(draft.attachment_id),
                **_compose_data(mail_event, subject="Hi", body="<p>Body</p>"),
            },
        )
        queued = ExhibitionEmailQueue.objects.get(event=mail_event)

    assert not ExhibitionEmailDraft.objects.filter(pk=draft.pk).exists()
    assert queued.attachment_id == draft.attachment_id
    assert CachedFile.objects.get(pk=queued.attachment_id).expires is None


@pytest.mark.django_db
def test_discarding_the_last_email_lets_its_attachment_expire(mail_event):
    with scopes_disabled():
        attachment = CachedFile.objects.create(filename="guide.pdf", expires=None)
        first, second = (
            ExhibitionEmailQueue.objects.create(
                event=mail_event, to_email=email, subject="Hi", body="<p>Hi</p>", attachment=attachment
            )
            for email in ("a@example.com", "b@example.com")
        )

        first.delete()
        assert CachedFile.objects.get(pk=attachment.pk).expires is None

        second.delete()
        assert CachedFile.objects.get(pk=attachment.pk).expires <= timezone.now()


@pytest.mark.django_db
def test_replacing_an_outbox_attachment_releases_the_old_file(mail_event):
    with scopes_disabled():
        old = CachedFile.objects.create(filename="old.pdf", expires=None)
        email = ExhibitionEmailQueue.objects.create(
            event=mail_event, to_email="a@example.com", subject="Hi", body="<p>Hi</p>", attachment=old
        )
        form = ExhibitionEmailQueueForm(
            data={"to_email": "a@example.com", "subject": "Hi", "body": "<p>Hi</p>"},
            files={"attachment": _upload("new.pdf")},
            instance=email,
            event=mail_event,
        )
        assert form.is_valid(), form.errors
        view = EmailEditView()
        view.request = _http_request(mail_event, post={"_save": "save"})
        view.kwargs = {"pk": email.pk}
        view.object = email
        view.form_valid(form)
        email.refresh_from_db()

    assert email.attachment.filename == "new.pdf"
    assert email.attachment.expires is None
    assert CachedFile.objects.get(pk=old.pk).expires <= timezone.now()


@pytest.mark.django_db
def test_voucher_csv_keeps_its_own_expiry_when_its_email_is_discarded(mail_event):
    with scopes_disabled():
        expires = timezone.now() + timedelta(days=30)
        csv = CachedFile.objects.create(filename="vouchers.csv", expires=expires)
        email = ExhibitionEmailQueue.objects.create(
            event=mail_event,
            role=mail_helpers.VOUCHERS,
            to_email="a@example.com",
            subject="Vouchers",
            body="<p>Codes</p>",
            attachment=csv,
        )

        email.delete()

    assert CachedFile.objects.get(pk=csv.pk).expires == expires


@pytest.mark.django_db
def test_lists_mark_emails_with_an_attachment(mail_event):
    with scopes_disabled():
        attachment = CachedFile.objects.create(filename="guide.pdf", expires=None)
        with_file = ExhibitionEmailQueue.objects.create(
            event=mail_event, to_email="a@example.com", subject="With", body="<p>Hi</p>", attachment=attachment
        )
        without_file = ExhibitionEmailQueue.objects.create(
            event=mail_event, to_email="b@example.com", subject="Without", body="<p>Hi</p>"
        )

    entries = {entry["pk"]: entry for entry in group_email_entries([with_file, without_file])}

    assert entries[with_file.pk]["has_attachment"] is True
    assert entries[without_file.pk]["has_attachment"] is False


@pytest.mark.django_db
def test_upload_without_recipients_keeps_its_expiry(mail_event):
    with scopes_disabled():
        expires = timezone.now() + timedelta(days=1)
        upload = CachedFile.objects.create(filename="guide.pdf", expires=expires)

        created = mail_helpers.queue_compose_emails(mail_event, [], "Hi", "<p>Hi</p>", attachment=upload)

    assert created == []
    assert CachedFile.objects.get(pk=upload.pk).expires == expires


@pytest.mark.django_db
def test_editing_a_voucher_email_keeps_the_csv_expiry(mail_event):
    with scopes_disabled():
        expires = timezone.now() + timedelta(days=30)
        csv = CachedFile.objects.create(filename="vouchers.csv", expires=expires)
        email = ExhibitionEmailQueue.objects.create(
            event=mail_event,
            role=mail_helpers.VOUCHERS,
            to_email="a@example.com",
            subject="Vouchers",
            body="<p>Codes</p>",
            attachment=csv,
        )
        form = ExhibitionEmailQueueForm(
            data={"to_email": "a@example.com", "subject": "Your vouchers", "body": "<p>Codes</p>"},
            instance=email,
            event=mail_event,
        )
        assert form.is_valid(), form.errors
        view = EmailEditView()
        view.request = _http_request(mail_event, post={"_save": "save"})
        view.kwargs = {"pk": email.pk}
        view.object = email
        view.form_valid(form)
        email.refresh_from_db()

    assert email.attachment_id == csv.pk
    assert CachedFile.objects.get(pk=csv.pk).expires == expires


@pytest.mark.django_db
def test_sending_uses_the_content_saved_on_the_row(mail_event):
    with scopes_disabled():
        old = CachedFile.objects.create(filename="old.pdf", expires=None)
        new = CachedFile.objects.create(filename="new.pdf", expires=None)
        email = ExhibitionEmailQueue.objects.create(
            event=mail_event, to_email="a@example.com", subject="Hi", body="<p>Hi</p>", attachment=old
        )
        stale = ExhibitionEmailQueue.objects.get(pk=email.pk)
        ExhibitionEmailQueue.objects.filter(pk=email.pk).update(subject="Updated", body="<p>New</p>", attachment=new)

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            stale.send()

    sent = mocked_mail.call_args.kwargs
    assert sent["attach_cached_files"] == [new.pk]
    assert sent["subject"] == "Updated"
    assert str(sent["template"]) == "<p>New</p>"


def _edit(event, email, data, files=None, post=None):
    form = ExhibitionEmailQueueForm(data=data, files=files or {}, instance=email, event=event)
    assert form.is_valid(), form.errors
    view = EmailEditView()
    view.request = _http_request(event, post=post or {"_save": "save"})
    view.kwargs = {"pk": email.pk}
    view.object = email
    return view.form_valid(form), view


@pytest.mark.django_db
def test_editing_an_email_sent_meanwhile_keeps_it_sent(mail_event):
    with scopes_disabled():
        email = ExhibitionEmailQueue.objects.create(
            event=mail_event, to_email="a@example.com", subject="Hi", body="<p>Hi</p>"
        )
        stale = ExhibitionEmailQueue.objects.get(pk=email.pk)
        sent_at = timezone.now()
        ExhibitionEmailQueue.objects.filter(pk=email.pk).update(sent_at=sent_at)

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            _response, view = _edit(
                mail_event,
                stale,
                {"to_email": "a@example.com", "subject": "Changed", "body": "<p>Changed</p>"},
                post={"_send": "send"},
            )
        email.refresh_from_db()

    assert not mocked_mail.called
    assert email.sent_at == sent_at
    assert email.subject == "Hi"
    assert [str(message) for message in view.request._messages] == [
        "This email was already sent or discarded, so your changes were not saved."
    ]


@pytest.mark.django_db
def test_editing_a_batch_discarded_meanwhile_leaves_the_upload_to_expire(mail_event):
    with scopes_disabled():
        email = ExhibitionEmailQueue.objects.create(
            event=mail_event, batch=uuid.uuid4(), to_email="a@example.com", subject="Hi", body="<p>Hi</p>"
        )
        stale = ExhibitionEmailQueue.objects.get(pk=email.pk)
        ExhibitionEmailQueue.objects.filter(pk=email.pk).delete()

        _edit(
            mail_event,
            stale,
            {"to_email": "a@example.com", "subject": "Hi", "body": "<p>Hi</p>"},
            files={"attachment": _upload()},
        )

        assert CachedFile.objects.get(filename="guide.pdf").expires is not None
