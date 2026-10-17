from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db.models import RestrictedError
from django.http import Http404
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.base.models import Event, Team
from eventyay.base.models.auth import User

from exhibition.forms import REQUEST_CHOICE_PREFIX, ExhibitionComposeForm
from exhibition.models import (
    ExhibitionEmailDraft,
    ExhibitionEmailQueue,
    ExhibitionRequest,
    ExhibitionRequestState,
    SponsorGroup,
)
from exhibition.views import EmailComposeView, EmailDraftDeleteView, EmailDraftListView, SponsorGroupDeleteView


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _request(event, name, state=ExhibitionRequestState.ACCEPTED, **kwargs):
    user = User.objects.create_user(email=f"{name.lower().replace(' ', '')}-user@example.com", password="pw")
    with scopes_disabled():
        return ExhibitionRequest.objects.create(event=event, user=user, name=name, state=state, **kwargs)


def _i18n(event, locale="en", **values):
    """POST data for the compose form's i18n subject/body, filled for one locale."""
    form = ExhibitionComposeForm(event=event)
    return {f"{name}_{form.fields[name].widget.locales.index(locale)}": value for name, value in values.items()}


def _compose_view(event, post=None, draft=None, request_code=None):
    request = RequestFactory().post("/", data=post or {})
    request.event = event
    request.user = AnonymousUser()
    request.session = {}
    request._messages = FallbackStorage(request)
    request.GET = request.GET.copy()
    if draft is not None:
        request.GET["draft"] = str(draft.pk)
    if request_code is not None:
        request.GET["request"] = request_code
    view = EmailComposeView()
    view.request = request
    view.kwargs = {}
    return view


def _submit(view, data):
    """Bind the compose form the way the view does, then run ``form_valid``."""
    form = ExhibitionComposeForm(data=data, **{k: v for k, v in view.get_form_kwargs().items() if k != "data"})
    assert form.is_valid(), form.errors
    return view.form_valid(form)


def _messages(view):
    return [str(message) for message in view.request._messages]


@pytest.mark.django_db
def test_draft_form_accepts_a_half_written_email(mail_event):
    data = {"scheduled_at": (timezone.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")}

    assert ExhibitionComposeForm(data=data, event=mail_event, saving_draft=True).is_valid()
    assert set(ExhibitionComposeForm(data=data, event=mail_event).errors) == {"subject", "body", "scheduled_at"}


@pytest.mark.django_db
def test_saving_a_draft_queues_nothing(mail_event):
    _request(mail_event, "Acme", email="a@example.com")
    view = _compose_view(mail_event, post={"_draft": "draft"})

    with scopes_disabled():
        response = _submit(view, {"state": "accepted", **_i18n(mail_event, subject="Hi", body="<p>Body</p>")})
        draft = ExhibitionEmailDraft.objects.get(event=mail_event)
        assert not ExhibitionEmailQueue.objects.filter(event=mail_event).exists()

    assert response.status_code == 302
    assert response.url.endswith("/emails/drafts")
    assert draft.state == "accepted"
    assert str(draft.subject) == "Hi"
    assert _messages(view) == ["The draft has been saved."]


@pytest.mark.django_db
def test_reopening_a_draft_restores_every_compose_field(mail_event):
    acme = _request(mail_event, "Acme", email="a@example.com")
    with scopes_disabled():
        group = SponsorGroup.objects.create(event=mail_event, name="Gold")
    when = timezone.now() + timedelta(days=2)
    view = _compose_view(mail_event, post={"_draft": "draft"})
    with scopes_disabled():
        _submit(
            view,
            {
                "state": "accepted",
                "organization_type": "sponsor",
                "sponsor_group": group.pk,
                "organizations": [f"{REQUEST_CHOICE_PREFIX}{acme.pk}"],
                "scheduled_at": when.strftime("%Y-%m-%dT%H:%M"),
                **_i18n(mail_event, subject="Hello {request_name}", body="<p>Welcome</p>"),
            },
        )
        draft = ExhibitionEmailDraft.objects.get(event=mail_event)

        initial = _compose_view(mail_event, draft=draft).get_initial()

    assert initial["state"] == "accepted"
    assert initial["organization_type"] == "sponsor"
    assert initial["sponsor_group"] == group
    assert initial["organizations"] == [f"{REQUEST_CHOICE_PREFIX}{acme.pk}"]
    assert str(initial["subject"]) == "Hello {request_name}"
    assert "Welcome" in str(initial["body"])
    assert initial["scheduled_at"].replace(second=0, microsecond=0) == when.replace(second=0, microsecond=0)


@pytest.mark.django_db
def test_saving_an_open_draft_updates_it_in_place(mail_event):
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Old")
        _submit(_compose_view(mail_event, post={"_draft": "draft"}, draft=draft), _i18n(mail_event, subject="New"))

        assert ExhibitionEmailDraft.objects.filter(event=mail_event).count() == 1
        draft.refresh_from_db()
    assert str(draft.subject) == "New"


@pytest.mark.django_db
def test_queuing_a_draft_moves_it_to_the_outbox(mail_event):
    _request(mail_event, "Acme", email="a@example.com")
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Hi", state="accepted")
        view = _compose_view(mail_event, post={"_save": "save"}, draft=draft)
        response = _submit(view, {"state": "accepted", **_i18n(mail_event, subject="Hi", body="<p>Body</p>")})

        assert not ExhibitionEmailDraft.objects.filter(pk=draft.pk).exists()
        queued = ExhibitionEmailQueue.objects.get(event=mail_event)
    assert queued.sent_at is None
    assert response.url.endswith("/emails/outbox")


@pytest.mark.django_db
def test_sending_a_draft_removes_it(mail_event):
    _request(mail_event, "Acme", email="a@example.com")
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Hi")
        view = _compose_view(mail_event, post={"_send": "send"}, draft=draft)
        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            _submit(view, {"state": "accepted", **_i18n(mail_event, subject="Hi", body="<p>Body</p>")})

        assert not ExhibitionEmailDraft.objects.filter(pk=draft.pk).exists()
    assert mocked_mail.call_count == 1


@pytest.mark.django_db
def test_a_draft_with_no_matching_recipients_is_kept(mail_event):
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Hi")
        view = _compose_view(mail_event, post={"_send": "send"}, draft=draft)
        _submit(view, {"state": "accepted", **_i18n(mail_event, subject="Hi", body="<p>Body</p>")})

        assert ExhibitionEmailDraft.objects.filter(pk=draft.pk).exists()


@pytest.mark.django_db
def test_single_applicant_draft_reopens_for_that_applicant(mail_event):
    acme = _request(mail_event, "Acme", email="a@example.com")
    view = _compose_view(mail_event, post={"_draft": "draft"}, request_code=acme.code)
    with scopes_disabled():
        _submit(view, _i18n(mail_event, subject="Just you"))
        draft = ExhibitionEmailDraft.objects.get(event=mail_event)

        reopened = _compose_view(mail_event, draft=draft)
        assert draft.exhibition_request == acme
        assert reopened.single_request == acme
        assert reopened.get_form_kwargs()["single_recipient"] is True


@pytest.mark.django_db
def test_drafts_of_another_event_cannot_be_opened(mail_event):
    with scopes_disabled():
        other = Event.objects.create(
            organizer=mail_event.organizer, name="Other", slug="other", date_from=timezone.now()
        )
        foreign = ExhibitionEmailDraft.objects.create(event=other, subject="Not yours")

        with pytest.raises(Http404):
            _compose_view(mail_event, draft=foreign).draft


@pytest.mark.django_db
def test_drafts_list_shows_only_this_events_drafts(mail_event):
    with scopes_disabled():
        mine = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Mine")
        other = Event.objects.create(
            organizer=mail_event.organizer, name="Other", slug="other", date_from=timezone.now()
        )
        ExhibitionEmailDraft.objects.create(event=other, subject="Theirs")

        view = EmailDraftListView()
        view.request = _compose_view(mail_event).request
        assert list(view.get_queryset()) == [mine]


@pytest.mark.django_db
def test_discarding_a_draft_deletes_it(mail_event):
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Bye")
        request = _compose_view(mail_event).request
        view = EmailDraftDeleteView()
        view.setup(request, pk=draft.pk)
        response = view.post(request, pk=draft.pk)

        assert response.status_code == 302
        assert not ExhibitionEmailDraft.objects.filter(pk=draft.pk).exists()


@pytest.mark.django_db
def test_recipient_count_resolves_recipients_like_the_compose_form(mail_event):
    with scopes_disabled():
        gold = SponsorGroup.objects.create(event=mail_event, name="Gold")
    acme = _request(mail_event, "Acme", email="a@example.com")
    _request(mail_event, "Globex", email="g@example.com", is_sponsor=True, sponsor_group=gold)
    _request(mail_event, "Initech", ExhibitionRequestState.SUBMITTED, email="i@example.com")
    _request(mail_event, "Acme Again", email="A@example.com")  # same address as Acme, emailed once

    with scopes_disabled():

        def count(**fields):
            return ExhibitionEmailDraft(event=mail_event, **fields).recipient_count()

        assert count() == 3
        assert count(state="accepted") == 2
        assert count(state="accepted", organization_type="sponsor", sponsor_group=gold) == 1
        assert count(state="submitted", organizations=[f"{REQUEST_CHOICE_PREFIX}{acme.pk}"]) == 1
        assert count(exhibition_request=acme, state="submitted") == 1


def _organizer_client(event, settings):
    """Logged-in organizer, with the same settings ``test_main`` uses to render control pages."""
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    settings.DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: False}
    user = User.objects.create_user(email="organizer@example.com", password="pw")
    with scopes_disabled():
        team = Team.objects.create(organizer=event.organizer, all_events=True, can_change_event_settings=True)
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_drafts_list_shows_how_many_recipients_each_draft_reaches(mail_event, settings):
    _request(mail_event, "Acme", email="a@example.com")
    _request(mail_event, "Globex", email="g@example.com")
    with scopes_disabled():
        ExhibitionEmailDraft.objects.create(event=mail_event, subject="To everyone", state="accepted")
    url = reverse(
        "plugins:exhibition:email.drafts",
        kwargs={"organizer": mail_event.organizer.slug, "event": mail_event.slug},
    )

    response = _organizer_client(mail_event, settings).get(url)

    assert response.status_code == 200
    assert "2 recipients" in response.content.decode()


@pytest.mark.django_db
def test_a_draft_already_queued_by_another_request_is_not_queued_again(mail_event):
    _request(mail_event, "Acme", email="a@example.com")
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Hi")
        late = _compose_view(mail_event, post={"_send": "send"}, draft=draft)
        late.draft  # loaded before the first submission went through
        _submit(
            _compose_view(mail_event, post={"_save": "save"}, draft=draft),
            {"state": "accepted", **_i18n(mail_event, subject="Hi", body="<p>Body</p>")},
        )

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            response = _submit(late, {"state": "accepted", **_i18n(mail_event, subject="Hi", body="<p>Body</p>")})

        assert ExhibitionEmailQueue.objects.filter(event=mail_event).count() == 1
    assert mocked_mail.call_count == 0
    assert response.url.endswith("/emails/outbox")
    assert _messages(late) == ["This draft has already been sent or placed in the outbox."]


@pytest.mark.django_db
def test_saving_a_draft_that_was_queued_meanwhile_does_not_bring_it_back(mail_event):
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Hi")
        late = _compose_view(mail_event, post={"_draft": "draft"}, draft=draft)
        late.draft  # loaded before the draft was queued by another request
        ExhibitionEmailDraft.objects.filter(pk=draft.pk).delete()

        response = _submit(late, _i18n(mail_event, subject="Edited"))

        assert not ExhibitionEmailDraft.objects.filter(event=mail_event).exists()
    assert response.url.endswith("/emails/outbox")
    assert _messages(late) == ["This draft has already been sent or placed in the outbox."]


@pytest.mark.django_db
def test_a_sponsor_group_that_drafts_target_cannot_be_deleted(mail_event):
    with scopes_disabled():
        gold = SponsorGroup.objects.create(event=mail_event, name="Gold")
        ExhibitionEmailDraft.objects.create(event=mail_event, subject="Gold only", sponsor_group=gold)

        with pytest.raises(RestrictedError):
            gold.delete()

        view = SponsorGroupDeleteView()
        view.setup(_compose_view(mail_event).request, pk=gold.pk)
        view.object = gold
        assert "email drafts are addressed to it" in str(view.deletion_blocker())


@pytest.mark.django_db
def test_a_sponsor_group_can_be_deleted_once_its_drafts_are_discarded(mail_event):
    with scopes_disabled():
        gold = SponsorGroup.objects.create(event=mail_event, name="Gold")
        draft = ExhibitionEmailDraft.objects.create(event=mail_event, subject="Gold only", sponsor_group=gold)

        draft.delete()
        gold.delete()

        assert not SponsorGroup.objects.filter(pk=gold.pk).exists()
