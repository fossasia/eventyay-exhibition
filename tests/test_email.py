import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.utils import timezone
from django.utils.translation import get_language
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User
from i18nfield.strings import LazyI18nString

from exhibition import mail as mail_helpers
from exhibition.forms import (
    ExhibitionComposeForm,
    ExhibitionEmailBodyFormField,
    ExhibitionEmailQueueForm,
    ExhibitionMailTemplatesForm,
)
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitorInfo,
    SponsorGroup,
)
from exhibition.views import (
    EmailComposeView,
    EmailDeleteView,
    EmailSendView,
    EmailTemplatePreviewView,
    group_email_entries,
)


def _locale_index(event, field_name, locale):
    """Index of an i18n sub-input, positioned by ``widget.locales``."""
    widget = ExhibitionMailTemplatesForm(obj=event).fields[field_name].widget
    return widget.locales.index(locale)


def _compose_data(event, locale="en", **values):
    """Compose form POST data with i18n subject/body posted for one locale."""
    form = ExhibitionComposeForm(event=event)
    data = {}
    for field_name, value in values.items():
        index = form.fields[field_name].widget.locales.index(locale)
        data[f"{field_name}_{index}"] = value
    return data


@pytest.fixture
def mail_event(event):
    """Event with the plugin enabled, so the placeholder signal is dispatched."""
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


@pytest.fixture
def applicant(db):
    return User.objects.create_user(email="applicant@example.com", password="pw", fullname="Jane Applicant")


@pytest.fixture
def proposal(mail_event, applicant):
    with scopes_disabled():
        return ExhibitionProposal.objects.create(
            event=mail_event,
            user=applicant,
            name="Acme Corp",
            state=ExhibitionProposalState.SUBMITTED,
        )


@pytest.fixture
def exhibitor(mail_event):
    with scopes_disabled():
        return ExhibitorInfo.objects.create(
            event=mail_event,
            name="Acme Corp",
            email="exhibitor@example.com",
            booth_id="B-9",
        )


@pytest.mark.django_db
def test_get_email_template_falls_back_to_defaults(mail_event):
    subject, body = mail_helpers.get_email_template(mail_event, mail_helpers.PROPOSAL_NEW)
    assert "{event_name}" in str(subject)
    assert "{request_name}" in str(body)


@pytest.mark.django_db
def test_get_email_template_uses_saved_override(mail_event):
    mail_event.settings.set(mail_helpers.subject_settings_key(mail_helpers.PROPOSAL_NEW), "Custom subject")
    subject, _body = mail_helpers.get_email_template(mail_event, mail_helpers.PROPOSAL_NEW)
    assert str(subject) == "Custom subject"


@pytest.mark.django_db
def test_templates_form_saves_to_event_settings(mail_event):
    subject_key = mail_helpers.subject_settings_key(mail_helpers.PROPOSAL_NEW)
    body_key = mail_helpers.body_settings_key(mail_helpers.PROPOSAL_NEW)
    form = ExhibitionMailTemplatesForm(
        data={
            f"{subject_key}_{_locale_index(mail_event, subject_key, 'en')}": "Saved subject",
            f"{body_key}_{_locale_index(mail_event, body_key, 'en')}": "Saved body",
        },
        obj=mail_event,
    )
    assert form.is_valid(), form.errors
    form.save()

    subject, body = mail_helpers.get_email_template(mail_event, mail_helpers.PROPOSAL_NEW)
    assert str(subject) == "Saved subject"
    assert str(body) == "Saved body"


@pytest.mark.django_db
def test_default_template_initial_blanks_locales_without_translation():
    """A locale with no catalog entry stays empty instead of inheriting the English msgid."""
    source_subject, source_body = mail_helpers.DEFAULT_TEMPLATE_SOURCES[mail_helpers.PROPOSAL_NEW]

    with patch("exhibition.mail.gettext", side_effect=lambda msgid: msgid):
        subject, body = mail_helpers.default_template_initial(mail_helpers.PROPOSAL_NEW, ["en", "th"])

    assert subject.data["en"] == source_subject
    assert body.data["en"] == source_body
    assert "th" not in subject.data
    assert "th" not in body.data


@pytest.mark.django_db
def test_default_template_initial_uses_available_translation():
    def fake_gettext(msgid):
        return f"TH:{msgid}" if get_language() == "th" else msgid

    with patch("exhibition.mail.gettext", side_effect=fake_gettext):
        subject, body = mail_helpers.default_template_initial(mail_helpers.PROPOSAL_NEW, ["en", "th"])

    source_subject, source_body = mail_helpers.DEFAULT_TEMPLATE_SOURCES[mail_helpers.PROPOSAL_NEW]
    assert subject.data["th"] == f"TH:{source_subject}"
    assert body.data["th"] == f"TH:{source_body}"
    assert subject.data["en"] == source_subject


@pytest.mark.django_db
def test_templates_form_does_not_prefill_untranslated_locale(mail_event):
    mail_event.settings.locales = ["en", "th"]

    with patch("exhibition.mail.gettext", side_effect=lambda msgid: msgid):
        form = ExhibitionMailTemplatesForm(obj=mail_event)

    for role in mail_helpers.LIFECYCLE_ROLES:
        for key in (mail_helpers.subject_settings_key(role), mail_helpers.body_settings_key(role)):
            initial = form.fields[key].initial
            assert initial.data["en"]
            assert "th" not in initial.data


@pytest.mark.django_db
def test_queue_proposal_email_resolves_placeholders(mail_event, proposal):
    queued = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_NEW)

    assert queued is not None
    assert queued.to_email == "applicant@example.com"
    assert "{request_name}" not in queued.body
    assert "{event_name}" not in queued.subject
    assert "Acme Corp" in queued.body
    assert str(mail_event.name) in queued.subject


@pytest.mark.django_db
def test_queue_proposal_email_is_unsent_by_default(mail_event, proposal):
    queued = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_ACCEPTED)
    assert queued.sent_at is None


@pytest.mark.django_db
def test_queue_proposal_email_send_now_sends_immediately(mail_event, proposal):
    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        queued = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_NEW, send_now=True)

    assert queued.sent_at is not None
    assert mocked_mail.call_count == 1
    assert mocked_mail.call_args.kwargs["email"] == "applicant@example.com"


@pytest.mark.django_db
def test_queue_proposal_email_prefers_proposal_email_over_user_email(mail_event, proposal):
    proposal.email = "contact@example.com"
    with scopes_disabled():
        proposal.save(update_fields=["email"])

    queued = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_REJECTED)
    assert queued.to_email == "contact@example.com"


@pytest.mark.django_db
def test_queue_proposal_email_returns_none_without_recipient(mail_event, applicant, proposal):
    applicant.email = ""
    applicant.save(update_fields=["email"])
    proposal.user.refresh_from_db()

    assert mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_NEW) is None


@pytest.mark.django_db
def test_send_marks_sent_and_calls_core_mail(mail_event, proposal):
    queued = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_ACCEPTED)

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        queued.send()

    assert queued.sent_at is not None
    kwargs = mocked_mail.call_args.kwargs
    assert kwargs["email"] == queued.to_email
    assert kwargs["subject"] == queued.subject
    assert kwargs["event"] == mail_event


@pytest.mark.django_db
def test_send_twice_is_a_noop(mail_event, proposal):
    queued = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_ACCEPTED)

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        queued.send()
        first_sent_at = queued.sent_at
        queued.send()

    assert mocked_mail.call_count == 1
    assert queued.sent_at == first_sent_at


@pytest.mark.django_db
def test_outbox_and_sent_querysets_do_not_overlap(mail_event, proposal):
    unsent = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_ACCEPTED)
    sent = mail_helpers.queue_proposal_email(mail_event, proposal, mail_helpers.PROPOSAL_REJECTED)
    with patch("eventyay.base.services.mail.mail"):
        sent.send()

    with scopes_disabled():
        outbox = ExhibitionEmailQueue.objects.filter(event=mail_event, sent_at__isnull=True)
        sent_list = ExhibitionEmailQueue.objects.filter(event=mail_event, sent_at__isnull=False)

    assert list(outbox) == [unsent]
    assert list(sent_list) == [sent]


@pytest.mark.django_db
def test_access_email_resolves_placeholders_and_keeps_newlines(mail_event, exhibitor):
    mail_event.settings.set(
        mail_helpers.subject_settings_key(mail_helpers.EXHIBITOR_ACCESS),
        "Access for {event_name}",
    )
    mail_event.settings.set(
        mail_helpers.body_settings_key(mail_helpers.EXHIBITOR_ACCESS),
        "Hello {exhibitor_name},\n\nBooth: {booth_id}\nCode: {exhibitor_access_code}",
    )

    queued = mail_helpers.queue_exhibitor_access_email(mail_event, exhibitor)

    assert queued is not None
    assert queued.sent_at is None
    assert queued.to_email == "exhibitor@example.com"
    assert "Acme Corp" in queued.body
    assert "B-9" in queued.body
    assert exhibitor.key in queued.body
    assert "\n" in queued.body


@pytest.mark.django_db
def test_access_email_falls_back_to_default_template(mail_event, exhibitor):
    queued = mail_helpers.queue_exhibitor_access_email(mail_event, exhibitor)

    assert queued is not None
    assert "Acme Corp" in queued.body
    assert exhibitor.key in queued.body


@pytest.mark.django_db
def test_access_email_returns_none_without_exhibitor_email(mail_event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=mail_event, name="No Email Co", email="")

    assert mail_helpers.queue_exhibitor_access_email(mail_event, exhibitor) is None


def _preview(event, role, body_by_locale):
    """POST draft body text to the preview endpoint, role passed as a query param."""
    data = {f"body_{locale}": text for locale, text in body_by_locale.items()}
    request = RequestFactory().post(f"/preview?role={role}", data=data)
    request.event = event
    return EmailTemplatePreviewView().post(request)


@pytest.mark.django_db
def test_preview_renders_markdown_and_highlights_placeholders(mail_event):
    mail_event.settings.locales = ["en"]
    response = _preview(mail_event, mail_helpers.PROPOSAL_NEW, {"en": "Hi {request_name}"})

    assert response.status_code == 200
    previews = json.loads(response.content)["previews"]
    assert "<p>" in previews["en"]
    assert 'class="placeholder"' in previews["en"]
    assert "{request_name}" not in previews["en"]


@pytest.mark.django_db
def test_preview_renders_each_active_locale(mail_event):
    mail_event.settings.locales = ["en", "de"]
    response = _preview(
        mail_event,
        mail_helpers.PROPOSAL_ACCEPTED,
        {"en": "Hello", "de": "Hallo"},
    )

    previews = json.loads(response.content)["previews"]
    assert set(previews.keys()) == {"en", "de"}
    assert "Hello" in previews["en"]
    assert "Hallo" in previews["de"]


@pytest.mark.django_db
def test_preview_sanitises_html(mail_event):
    mail_event.settings.locales = ["en"]
    response = _preview(mail_event, mail_helpers.PROPOSAL_NEW, {"en": "<script>alert(1)</script>"})

    previews = json.loads(response.content)["previews"]
    assert "<script>" not in previews["en"]


@pytest.mark.django_db
def test_preview_rejects_unknown_role(mail_event):
    request = RequestFactory().post("/preview?role=not_a_role")
    request.event = mail_event
    response = EmailTemplatePreviewView().post(request)

    assert response.status_code == 400


def _proposal(event, name, state, *, email="", user=None, is_exhibitor=True, is_sponsor=False, sponsor_group=None):
    if user is None:
        user = User.objects.create_user(email=f"{name.lower().replace(' ', '')}-user@example.com", password="pw")
    with scopes_disabled():
        return ExhibitionProposal.objects.create(
            event=event,
            user=user,
            name=name,
            state=state,
            email=email,
            is_exhibitor=is_exhibitor,
            is_sponsor=is_sponsor,
            sponsor_group=sponsor_group,
        )


@pytest.mark.django_db
def test_compose_recipients_filters_by_state(mail_event):
    accepted = _proposal(mail_event, "A", ExhibitionProposalState.ACCEPTED, email="a@example.com")
    _proposal(mail_event, "R", ExhibitionProposalState.REJECTED, email="r@example.com")
    _proposal(mail_event, "D", ExhibitionProposalState.DRAFT, email="d@example.com")

    with scopes_disabled():
        result = list(mail_helpers.compose_recipients(mail_event, states=[ExhibitionProposalState.ACCEPTED]))

    assert result == [accepted]


@pytest.mark.django_db
def test_compose_recipients_excludes_drafts_when_no_state_filter(mail_event):
    _proposal(mail_event, "D", ExhibitionProposalState.DRAFT, email="d@example.com")
    submitted = _proposal(mail_event, "S", ExhibitionProposalState.SUBMITTED, email="s@example.com")

    with scopes_disabled():
        result = list(mail_helpers.compose_recipients(mail_event))

    assert result == [submitted]


@pytest.mark.django_db
def test_compose_recipients_filters_by_type_and_group(mail_event):
    with scopes_disabled():
        group = SponsorGroup.objects.create(event=mail_event, name="Gold")
    sponsor = _proposal(
        mail_event,
        "Sp",
        ExhibitionProposalState.ACCEPTED,
        email="sp@example.com",
        is_exhibitor=False,
        is_sponsor=True,
        sponsor_group=group,
    )
    _proposal(mail_event, "Ex", ExhibitionProposalState.ACCEPTED, email="ex@example.com")

    with scopes_disabled():
        by_type = list(mail_helpers.compose_recipients(mail_event, partner_type="sponsor"))
        by_group = list(mail_helpers.compose_recipients(mail_event, sponsor_group=group))

    assert by_type == [sponsor]
    assert by_group == [sponsor]


@pytest.mark.django_db
def test_queue_compose_emails_fans_out_and_dedupes(mail_event):
    _proposal(mail_event, "One", ExhibitionProposalState.ACCEPTED, email="one@example.com")
    _proposal(mail_event, "Two", ExhibitionProposalState.ACCEPTED, email="two@example.com")
    _proposal(mail_event, "Dup", ExhibitionProposalState.ACCEPTED, email="One@Example.com")

    with scopes_disabled():
        recipients = list(mail_helpers.compose_recipients(mail_event))
        created = mail_helpers.queue_compose_emails(mail_event, recipients, "Hi", "Body")
        emails = {row.to_email for row in ExhibitionEmailQueue.objects.filter(event=mail_event)}

    assert len(created) == 2
    assert {email.lower() for email in emails} == {"one@example.com", "two@example.com"}
    assert all(row.sent_at is None for row in created)


@pytest.mark.django_db
def test_queue_compose_emails_resolves_placeholders(mail_event):
    proposal = _proposal(mail_event, "Acme Corp", ExhibitionProposalState.ACCEPTED, email="a@example.com")

    with scopes_disabled():
        created = mail_helpers.queue_compose_emails(
            mail_event, [proposal], "For {request_name}", "Hello from {event_name}"
        )

    assert created[0].subject == "For Acme Corp"
    assert str(mail_event.name) in created[0].body


@pytest.mark.django_db
def test_queue_compose_emails_send_now(mail_event):
    proposal = _proposal(mail_event, "Acme", ExhibitionProposalState.ACCEPTED, email="a@example.com")

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        with scopes_disabled():
            created = mail_helpers.queue_compose_emails(mail_event, [proposal], "S", "B", send_now=True)

    assert created[0].sent_at is not None
    assert mocked_mail.call_count == 1


@pytest.mark.django_db
def test_compose_form_requires_subject_and_body(mail_event):
    form = ExhibitionComposeForm(data={"states": [ExhibitionProposalState.ACCEPTED]}, event=mail_event)
    assert not form.is_valid()
    assert "subject" in form.errors
    assert "body" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "empty_body",
    [
        "",
        "<p></p>",
        "<p><br></p>",
        "<p>&nbsp;</p>",
        "<p>&#160;</p>",
        "<p>&#xA0;</p>",
    ],
)
def test_compose_form_rejects_empty_html_body(mail_event, empty_body):
    form = ExhibitionComposeForm(
        data={
            "states": [ExhibitionProposalState.ACCEPTED],
            **_compose_data(mail_event, subject="Hi", body=empty_body),
        },
        event=mail_event,
    )
    assert not form.is_valid()
    assert "body" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "valid_body",
    [
        '<img src="https://example.com/logo.png">',
        '<IMG SRC="https://example.com/logo.png">',
        "<p>Hello world</p>",
    ],
)
def test_compose_form_accepts_valid_body(mail_event, valid_body):
    form = ExhibitionComposeForm(
        data={
            "states": [ExhibitionProposalState.ACCEPTED],
            **_compose_data(mail_event, subject="Hi", body=valid_body),
        },
        event=mail_event,
    )
    assert form.is_valid(), form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "empty_body",
    [
        "",
        "<p></p>",
        "<p><br></p>",
        "<p>&nbsp;</p>",
        "<p>&#160;</p>",
        "<p>&#xA0;</p>",
    ],
)
def test_email_queue_edit_form_rejects_empty_html_body(mail_event, empty_body):
    form = ExhibitionEmailQueueForm(
        data={
            "to_email": "test@example.com",
            "subject": "Update",
            "body": empty_body,
        },
        event=mail_event,
    )
    assert not form.is_valid()
    assert "body" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "valid_body",
    [
        '<img src="https://example.com/logo.png">',
        '<IMG SRC="https://example.com/logo.png">',
        "<p>Hello world</p>",
    ],
)
def test_email_queue_edit_form_accepts_valid_body(mail_event, valid_body):
    form = ExhibitionEmailQueueForm(
        data={
            "to_email": "test@example.com",
            "subject": "Update",
            "body": valid_body,
        },
        event=mail_event,
    )
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_compose_view_saves_to_outbox(mail_event):
    _proposal(mail_event, "Acme", ExhibitionProposalState.ACCEPTED, email="a@example.com")
    form = ExhibitionComposeForm(
        data={
            "states": [ExhibitionProposalState.ACCEPTED],
            "partner_type": "",
            **_compose_data(mail_event, subject="Hi", body="Body"),
        },
        event=mail_event,
    )
    assert form.is_valid(), form.errors

    request = RequestFactory().post("/compose", data={})
    request.event = mail_event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)
    view = EmailComposeView()
    view.request = request
    response = view.form_valid(form)

    assert response.status_code == 302
    with scopes_disabled():
        assert ExhibitionEmailQueue.objects.filter(event=mail_event, sent_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_queue_compose_emails_stores_scheduled_at(mail_event):
    proposal = _proposal(mail_event, "Acme", ExhibitionProposalState.ACCEPTED, email="a@example.com")
    when = timezone.now() + timedelta(days=1)

    with scopes_disabled():
        created = mail_helpers.queue_compose_emails(mail_event, [proposal], "S", "B", scheduled_at=when)

    assert created[0].scheduled_at == when
    assert created[0].sent_at is None


@pytest.mark.django_db
def test_compose_form_rejects_past_scheduled_at(mail_event):
    form = ExhibitionComposeForm(
        data={
            "states": [ExhibitionProposalState.ACCEPTED],
            "scheduled_at": (timezone.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            **_compose_data(mail_event, subject="Hi", body="Body"),
        },
        event=mail_event,
    )
    assert not form.is_valid()
    assert list(form.errors) == ["scheduled_at"]


@pytest.mark.django_db
def test_compose_view_schedules_emails(mail_event):
    _proposal(mail_event, "Acme", ExhibitionProposalState.ACCEPTED, email="a@example.com")
    when = timezone.now() + timedelta(days=1)
    form = ExhibitionComposeForm(
        data={
            "states": [ExhibitionProposalState.ACCEPTED],
            "partner_type": "",
            "scheduled_at": when.strftime("%Y-%m-%dT%H:%M"),
            **_compose_data(mail_event, subject="Hi", body="Body"),
        },
        event=mail_event,
    )
    assert form.is_valid(), form.errors

    request = RequestFactory().post("/compose", data={"_send": "1"})
    request.event = mail_event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)
    view = EmailComposeView()
    view.request = request

    with patch("exhibition.tasks.send_scheduled_email.apply_async") as mocked_apply:
        response = view.form_valid(form)

    assert response.status_code == 302
    assert mocked_apply.call_count == 1
    with scopes_disabled():
        row = ExhibitionEmailQueue.objects.get(event=mail_event)
    assert row.scheduled_at is not None
    assert row.sent_at is None


@pytest.mark.django_db
def test_scheduled_task_sends_when_due(mail_event, proposal):
    from exhibition.tasks import send_scheduled_email

    with scopes_disabled():
        queued = ExhibitionEmailQueue.objects.create(
            event=mail_event,
            proposal=proposal,
            to_email="a@example.com",
            subject="S",
            body="B",
            scheduled_at=timezone.now() - timedelta(minutes=1),
        )

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        send_scheduled_email.run(mail_event.pk, queued.pk)

    with scopes_disabled():
        queued.refresh_from_db()
    assert queued.sent_at is not None
    assert queued.scheduled_at is None
    assert mocked_mail.call_count == 1


@pytest.mark.django_db
def test_group_email_entries_collapses_batches(mail_event):
    p1 = _proposal(mail_event, "One", ExhibitionProposalState.ACCEPTED, email="one@example.com")
    p2 = _proposal(mail_event, "Two", ExhibitionProposalState.ACCEPTED, email="two@example.com")
    with scopes_disabled():
        mail_helpers.queue_compose_emails(mail_event, [p1, p2], "Hi", "Body")
        lifecycle = mail_helpers.queue_proposal_email(mail_event, p1, mail_helpers.PROPOSAL_ACCEPTED)
        emails = list(ExhibitionEmailQueue.objects.filter(event=mail_event).order_by("-created"))

    entries = group_email_entries(emails)
    batch_entries = [e for e in entries if e["is_batch"]]
    single_entries = [e for e in entries if not e["is_batch"]]

    assert len(batch_entries) == 1
    assert sorted(batch_entries[0]["recipients"]) == ["one@example.com", "two@example.com"]
    assert len(single_entries) == 1
    assert single_entries[0]["pk"] == lifecycle.pk


@pytest.mark.django_db
def test_send_view_sends_whole_batch(mail_event):
    p1 = _proposal(mail_event, "One", ExhibitionProposalState.ACCEPTED, email="one@example.com")
    p2 = _proposal(mail_event, "Two", ExhibitionProposalState.ACCEPTED, email="two@example.com")
    with scopes_disabled():
        created = mail_helpers.queue_compose_emails(mail_event, [p1, p2], "Hi", "Body")

    request = RequestFactory().post("/send")
    request.event = mail_event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        EmailSendView().post(request, pk=created[0].pk)

    assert mocked_mail.call_count == 2
    with scopes_disabled():
        assert ExhibitionEmailQueue.objects.filter(event=mail_event, sent_at__isnull=True).count() == 0


@pytest.mark.django_db
def test_delete_view_discards_whole_batch(mail_event):
    p1 = _proposal(mail_event, "One", ExhibitionProposalState.ACCEPTED, email="one@example.com")
    p2 = _proposal(mail_event, "Two", ExhibitionProposalState.ACCEPTED, email="two@example.com")
    with scopes_disabled():
        created = mail_helpers.queue_compose_emails(mail_event, [p1, p2], "Hi", "Body")

    request = RequestFactory().post("/delete")
    request.event = mail_event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)
    view = EmailDeleteView()
    view.request = request
    view.kwargs = {"pk": created[0].pk}
    view.form_valid(None)

    with scopes_disabled():
        assert ExhibitionEmailQueue.objects.filter(event=mail_event).count() == 0


PLAIN_BODY = "First paragraph.\n\nSecond paragraph.\nSame paragraph, new line."


def test_i18n_email_body_widget_seeds_editor_with_html():
    """Plain-text bodies reach the Tiptap editor as block HTML, not collapsed whitespace."""
    field = ExhibitionEmailBodyFormField(required=False, locales=["en"])
    seeded = field.widget.decompress(LazyI18nString({"en": PLAIN_BODY}))[0]

    assert seeded.count("<p>") == 2
    assert "First paragraph." in seeded
    assert "Second paragraph." in seeded


def test_email_queue_body_widget_seeds_editor_with_html():
    widget = ExhibitionEmailQueueForm.base_fields["body"].widget
    seeded = widget.format_value(PLAIN_BODY)

    assert seeded.count("<p>") == 2
    assert "First paragraph." in seeded


def test_email_body_widget_leaves_editor_html_untouched():
    """Re-seeding already-Tiptap HTML must not wrap or re-escape it."""
    field = ExhibitionEmailBodyFormField(required=False, locales=["en"])
    html = "<p>First paragraph.</p><p>Second paragraph.</p>"
    seeded = field.widget.decompress(LazyI18nString({"en": html}))[0]

    assert seeded.count("<p>") == 2
    assert "&lt;p&gt;" not in seeded


def test_email_body_widget_keeps_markdown_emphasis():
    """The access template's **bold** markers must survive as markup, not literal asterisks."""
    field = ExhibitionEmailBodyFormField(required=False, locales=["en"])
    seeded = field.widget.decompress(LazyI18nString({"en": "**Step 1:** open the app"}))[0]

    assert "<strong>" in seeded
    assert "**" not in seeded
