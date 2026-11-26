import json

import pytest
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
)
from exhibition.views import RequestActionView, RequestDetailView


def _request(event, email, state=ExhibitionRequestState.SUBMITTED, **kwargs):
    user = User.objects.create_user(email=email, password="pw")
    return ExhibitionRequest.objects.create(
        event=event,
        user=user,
        name=kwargs.pop("name", "Org"),
        state=state,
        **kwargs,
    )


def _action_view(event, email="organizer@e.com"):
    request = RequestFactory().post("/")
    request.user = User.objects.create_user(email=email, password="pw")
    request.event = event
    view = RequestActionView()
    view.request = request
    return view


def _action_post_view(event, action, codes, email="organizer@e.com", ajax=True):
    data = {"action": action, "exhibition_request": codes}
    request = RequestFactory().post("/", data=data)
    request.user = User.objects.create_user(email=email, password="pw")
    request.event = event
    if ajax:
        request.headers = {"x-requested-with": "XMLHttpRequest"}
    view = RequestActionView()
    view.request = request
    return view, request


@pytest.mark.django_db
def test_apply_action_approve_creates_exhibitor(event):
    with scopes_disabled():
        exhibition_request = _request(event, "approve@e.com")
        _action_view(event, "org-approve@e.com").apply_action(exhibition_request, "approve")
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED
        assert exhibition_request.approved_exhibitor_id is not None
        assert ExhibitorInfo.objects.filter(event=event, pk=exhibition_request.approved_exhibitor_id).exists()


@pytest.mark.django_db
def test_apply_action_approve_queues_acceptance_email(event):
    with scopes_disabled():
        exhibition_request = _request(event, "approve-mail@e.com")
        _action_view(event, "org-approve-mail@e.com").apply_action(exhibition_request, "approve")
        queued = ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=exhibition_request)
        assert queued.count() == 1
        assert queued.first().to_email == "approve-mail@e.com"


@pytest.mark.django_db
def test_apply_action_approve_sponsor_creates_sponsor(event):
    with scopes_disabled():
        exhibition_request = _request(event, "sponsor@e.com", is_sponsor=True, is_exhibitor=False)
        _action_view(event, "org-sponsor@e.com").apply_action(exhibition_request, "approve")
        exhibition_request.refresh_from_db()
        exhibitor = ExhibitorInfo.objects.get(pk=exhibition_request.approved_exhibitor_id)
        assert exhibitor.is_sponsor is True
        assert exhibitor.is_exhibitor is False


@pytest.mark.django_db
def test_apply_action_reject_sets_state_without_organization(event):
    with scopes_disabled():
        exhibition_request = _request(event, "reject@e.com")
        _action_view(event, "org-reject@e.com").apply_action(exhibition_request, "reject")
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.REJECTED
        assert exhibition_request.approved_exhibitor_id is None
        assert not ExhibitorInfo.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_apply_action_reject_queues_rejection_email(event):
    with scopes_disabled():
        exhibition_request = _request(event, "reject-mail@e.com")
        _action_view(event, "org-reject-mail@e.com").apply_action(exhibition_request, "reject")
        queued = ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=exhibition_request)
        assert queued.count() == 1
        assert queued.first().to_email == "reject-mail@e.com"


@pytest.mark.django_db
def test_apply_action_withdraw_sets_state_without_email(event):
    with scopes_disabled():
        exhibition_request = _request(event, "withdraw@e.com")
        _action_view(event, "org-withdraw@e.com").apply_action(exhibition_request, "withdraw")
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.WITHDRAWN
        assert not ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=exhibition_request).exists()


def test_build_message_reports_count_and_skips():
    view = RequestActionView()
    message = view.build_message("approve", 2, 1)
    assert "2 requests were approved." in message
    assert "1 was skipped" in message


def test_build_message_no_updates():
    view = RequestActionView()
    assert str(view.build_message("reject", 0, 0)) == "No requests were updated."


@pytest.mark.django_db
def test_apply_action_reopen_from_rejected_sets_submitted(event):
    with scopes_disabled():
        exhibition_request = _request(event, "reopen-flow@e.com", state=ExhibitionRequestState.REJECTED)
        _action_view(event, "org-reopen@e.com").apply_action(exhibition_request, "reopen")
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.SUBMITTED
        assert not ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=exhibition_request).exists()


@pytest.mark.django_db
def test_post_skips_request_not_eligible_for_action(event):
    with scopes_disabled():
        exhibition_request = _request(event, "skip@e.com", state=ExhibitionRequestState.DRAFT)
        view, request = _action_post_view(event, "approve", [exhibition_request.code], email="org-skip@e.com")
        response = view.post(request)
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.DRAFT
        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["skipped"] == 1
        assert payload["results"] == []


@pytest.mark.django_db
def test_post_returns_updated_actions_and_bulk_actions(event):
    with scopes_disabled():
        exhibition_request = _request(event, "json@e.com")
        view, request = _action_post_view(event, "approve", [exhibition_request.code], email="org-json@e.com")
        response = view.post(request)
        payload = json.loads(response.content)
        assert payload["ok"] is True
        result = payload["results"][0]
        assert result["state"] == "accepted"
        assert set(result["actions"]) == {"confirm", "reject", "withdraw", "reopen"}
        assert result["bulk_actions"] == ["reject"]


@pytest.mark.django_db
def test_request_detail_view_handles_real_database_duplicate_booth_conflict(event):
    from unittest.mock import MagicMock

    from django.contrib.messages.storage.fallback import FallbackStorage
    from eventyay.base.models import Team

    from exhibition.forms import ExhibitionRequestReviewForm
    from exhibition.models import (
        ExhibitionRequestExtraLink,
        ExhibitionRequestSocialLink,
        ExhibitorExtraLink,
        ExhibitorSocialLink,
    )

    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Existing B101", is_exhibitor=True, booth_number="B-101")
        req = _request(event, "concurrent@example.com")
        ExhibitionRequestSocialLink.objects.create(
            exhibition_request=req, network="github", url="https://github.com/example"
        )
        ExhibitionRequestExtraLink.objects.create(exhibition_request=req, label="Website", url="https://example.com")

        user = User.objects.create_user("admin_concurrent@e.com", "pw")
        team = Team.objects.create(
            organizer=event.organizer,
            all_events=True,
            can_change_event_settings=True,
            can_change_exhibition_proposals=True,
        )
        team.members.add(user)

        request = RequestFactory().post(
            f"/control/event/{event.organizer.slug}/{event.slug}/exhibition/requests/{req.code}/",
            data={
                "action": "approve",
                "is_exhibitor": True,
                "booth_number": "B-202",
            },
        )
        request.user = user
        request.event = event
        mock_session = MagicMock()
        mock_session.session_key = "session_concurrent"
        request.session = mock_session
        messages_storage = FallbackStorage(request)
        setattr(request, "_messages", messages_storage)

        view = RequestDetailView()
        view.request = request
        view.kwargs = {"organizer": event.organizer.slug, "event": event.slug, "code": req.code}
        view.object = req

        form = ExhibitionRequestReviewForm(
            data={"is_exhibitor": True, "booth_number": "B-202"},
            instance=req,
            event=event,
        )
        assert form.is_valid(), form.errors

        # Another organizer assigns B-202 before approval runs
        ExhibitorInfo.objects.create(event=event, name="Winner B202", is_exhibitor=True, booth_number="B-202")
        exhibitor_count_before = ExhibitorInfo.objects.filter(event=event).count()
        social_count_before = ExhibitorSocialLink.objects.filter(exhibitor__event=event).count()
        extra_count_before = ExhibitorExtraLink.objects.filter(exhibitor__event=event).count()

        # Execute real approval workflow without mocking approve()
        response = view.form_valid(form)

        assert response.status_code == 302
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.SUBMITTED
        assert req.approved_exhibitor_id is None
        assert ExhibitorInfo.objects.filter(event=event).count() == exhibitor_count_before
        assert ExhibitorSocialLink.objects.filter(exhibitor__event=event).count() == social_count_before
        assert ExhibitorExtraLink.objects.filter(exhibitor__event=event).count() == extra_count_before
        message_texts = [str(m) for m in messages_storage]
        assert any("already in use" in msg for msg in message_texts)


@pytest.mark.django_db
def test_request_detail_view_reraises_unrelated_integrity_error(event):
    from unittest.mock import MagicMock, patch

    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.db import IntegrityError
    from eventyay.base.models import Team

    from exhibition.forms import ExhibitionRequestReviewForm

    with scopes_disabled():
        req = _request(event, "negative@example.com")
        user = User.objects.create_user("admin_negative@e.com", "pw")
        team = Team.objects.create(
            organizer=event.organizer,
            all_events=True,
            can_change_event_settings=True,
            can_change_exhibition_proposals=True,
        )
        team.members.add(user)

        request = RequestFactory().post(
            f"/control/event/{event.organizer.slug}/{event.slug}/exhibition/requests/{req.code}/",
            data={
                "action": "approve",
                "is_exhibitor": True,
                "booth_number": "B-303",
            },
        )
        request.user = user
        request.event = event
        mock_session = MagicMock()
        mock_session.session_key = "session_negative"
        request.session = mock_session
        messages_storage = FallbackStorage(request)
        setattr(request, "_messages", messages_storage)

        view = RequestDetailView()
        view.request = request
        view.kwargs = {"organizer": event.organizer.slug, "event": event.slug, "code": req.code}
        view.object = req

        form = ExhibitionRequestReviewForm(
            data={"is_exhibitor": True, "booth_number": "B-303"},
            instance=req,
            event=event,
        )
        assert form.is_valid(), form.errors

        def _fail_after_write(requestor=None):
            ExhibitorInfo.objects.create(event=event, name="Partial", is_exhibitor=True, booth_number="B-303")
            raise IntegrityError('null value in column "some_other_col" violates not-null constraint')

        with patch.object(req, "approve", side_effect=_fail_after_write):
            with pytest.raises(IntegrityError):
                view.form_valid(form)

        assert not ExhibitorInfo.objects.filter(event=event, name="Partial").exists()
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.SUBMITTED
        message_texts = [str(m) for m in messages_storage]
        assert not any("already in use" in msg for msg in message_texts)
