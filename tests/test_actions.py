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


def _detail_view(event, req, post_data, email="admin_detail@e.com"):
    from unittest.mock import MagicMock

    from django.contrib.messages.storage.fallback import FallbackStorage
    from eventyay.base.models import Team

    user = User.objects.create_user(email, "pw")
    team = Team.objects.create(
        organizer=event.organizer,
        all_events=True,
        can_change_event_settings=True,
        can_change_exhibition_proposals=True,
    )
    team.members.add(user)

    request = RequestFactory().post(
        f"/control/event/{event.organizer.slug}/{event.slug}/exhibition/requests/{req.code}/",
        data=post_data,
    )
    request.user = user
    request.event = event
    mock_session = MagicMock()
    mock_session.session_key = f"session_{req.code}"
    request.session = mock_session
    messages_storage = FallbackStorage(request)
    setattr(request, "_messages", messages_storage)

    view = RequestDetailView()
    view.request = request
    view.kwargs = {"organizer": event.organizer.slug, "event": event.slug, "code": req.code}
    view.object = req
    return view, messages_storage


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

        view, messages_storage = _detail_view(
            event,
            req,
            {"action": "approve", "is_exhibitor": True, "booth_number": "B-202"},
            email="admin_concurrent@e.com",
        )
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
    from unittest.mock import patch

    from django.db import IntegrityError

    from exhibition.forms import ExhibitionRequestReviewForm

    with scopes_disabled():
        req = _request(event, "negative@example.com")
        view, messages_storage = _detail_view(
            event,
            req,
            {"action": "approve", "is_exhibitor": True, "booth_number": "B-303"},
            email="admin_negative@e.com",
        )
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


@pytest.mark.django_db
def test_request_detail_view_rolls_back_form_edits_and_logs_on_booth_conflict(event):
    from eventyay.base.models import LogEntry

    from exhibition.forms import ExhibitionRequestReviewForm
    from exhibition.models import LOG_REQUEST_CHANGED

    with scopes_disabled():
        req = _request(
            event,
            "stale-edit@example.com",
            booth_number="B-101",
            review_notes="Original review notes",
        )
        view, _ = _detail_view(
            event,
            req,
            {
                "action": "approve",
                "is_exhibitor": True,
                "booth_number": "B-202",
                "review_notes": "Updated review notes",
            },
            email="admin_stale@e.com",
        )
        form = ExhibitionRequestReviewForm(
            data={
                "is_exhibitor": True,
                "booth_number": "B-202",
                "review_notes": "Updated review notes",
            },
            instance=req,
            event=event,
        )
        assert form.is_valid(), form.errors

        ExhibitorInfo.objects.create(event=event, name="Winner B202", is_exhibitor=True, booth_number="B-202")
        exhibitor_count_before = ExhibitorInfo.objects.filter(event=event).count()
        log_count_before = LogEntry.objects.filter(event=event, action_type=LOG_REQUEST_CHANGED).count()

        response = view.form_valid(form)

        assert response.status_code == 302
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.SUBMITTED
        assert req.approved_exhibitor_id is None
        assert req.booth_number == "B-101"
        assert req.review_notes == "Original review notes"
        assert ExhibitorInfo.objects.filter(event=event).count() == exhibitor_count_before
        assert LogEntry.objects.filter(event=event, action_type=LOG_REQUEST_CHANGED).count() == log_count_before
        assert not ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=req).exists()


@pytest.mark.django_db
def test_bulk_approve_skips_duplicate_booth_and_approves_valid_requests(event):
    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Existing B101", is_exhibitor=True, booth_number="B-101")
        req_conflict = _request(event, "bulk-conflict@e.com", name="Conflict Org", booth_number="B-101")
        req_valid = _request(event, "bulk-valid@e.com", name="Valid Org", booth_number="B-202")

        view, request = _action_post_view(
            event,
            "approve",
            [req_conflict.code, req_valid.code],
            email="org-bulk-approve@e.com",
        )
        response = view.post(request)

        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["ok"] is True
        assert payload["skipped"] == 1
        assert len(payload["results"]) == 1
        assert payload["results"][0]["code"] == req_valid.code
        assert payload["results"][0]["state"] == ExhibitionRequestState.ACCEPTED

        req_conflict.refresh_from_db()
        req_valid.refresh_from_db()
        assert req_conflict.state == ExhibitionRequestState.SUBMITTED
        assert req_conflict.approved_exhibitor_id is None
        assert req_valid.state == ExhibitionRequestState.ACCEPTED
        assert req_valid.approved_exhibitor_id is not None
        assert ExhibitorInfo.objects.filter(event=event, booth_number="B-101").count() == 1
        assert ExhibitorInfo.objects.filter(event=event, booth_number="B-202").count() == 1
        assert not ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=req_conflict).exists()
        assert ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=req_valid).count() == 1


@pytest.mark.django_db
def test_bulk_approve_reraises_unrelated_integrity_error_and_rolls_back(event):
    from unittest.mock import patch

    from django.db import IntegrityError

    with scopes_disabled():
        req_first = _request(event, "bulk-first@e.com", name="First Org", booth_number="B-101")
        req_second = _request(event, "bulk-second@e.com", name="Second Org", booth_number="B-202")

        view, request = _action_post_view(
            event,
            "approve",
            [req_first.code, req_second.code],
            email="org-bulk-err@e.com",
        )

        original_approve = ExhibitionRequest.approve
        call_count = 0

        def _approve_with_failure(self, requestor=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IntegrityError('null value in column "some_other_col" violates not-null constraint')
            exhibitor = original_approve(self, requestor=requestor)
            assert ExhibitorInfo.objects.filter(pk=exhibitor.pk).exists()
            return exhibitor

        with patch.object(ExhibitionRequest, "approve", _approve_with_failure):
            with pytest.raises(IntegrityError):
                view.post(request)

        req_first.refresh_from_db()
        req_second.refresh_from_db()
        assert req_first.state == ExhibitionRequestState.SUBMITTED
        assert req_first.approved_exhibitor_id is None
        assert req_second.state == ExhibitionRequestState.SUBMITTED
        assert req_second.approved_exhibitor_id is None
        assert not ExhibitorInfo.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_request_detail_view_approve_rejects_duplicate_booth(event):
    from exhibition.forms import ExhibitionRequestReviewForm

    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Existing", is_exhibitor=True, booth_number="B-101")
        req = _request(event, "dup-approve@e.com", booth_number="B-101")
        view, _ = _detail_view(
            event,
            req,
            {"action": "approve", "is_exhibitor": True, "booth_number": "B-101"},
            email="org-dup-approve@e.com",
        )
        kwargs = view.get_form_kwargs()
        assert kwargs["review_action"] == "approve"
        form = ExhibitionRequestReviewForm(
            data={"is_exhibitor": True, "booth_number": "B-101"},
            instance=req,
            **{k: v for k, v in kwargs.items() if k in ("event", "review_action")},
        )
        assert not form.is_valid()
        assert "booth_number" in form.errors


@pytest.mark.django_db
def test_request_detail_view_reject_succeeds_with_claimed_booth(event):
    from exhibition.forms import ExhibitionRequestReviewForm

    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Existing", is_exhibitor=True, booth_number="B-101")
        req = _request(event, "dup-reject@e.com", booth_number="B-101")
        view, _ = _detail_view(
            event,
            req,
            {"action": "reject", "is_exhibitor": True, "booth_number": "B-101", "review_notes": "Declined"},
            email="org-dup-reject@e.com",
        )
        kwargs = view.get_form_kwargs()
        assert kwargs["review_action"] == "reject"
        form = ExhibitionRequestReviewForm(
            data={"is_exhibitor": True, "booth_number": "B-101", "review_notes": "Declined"},
            instance=req,
            **{k: v for k, v in kwargs.items() if k in ("event", "review_action")},
        )
        assert form.is_valid(), form.errors
        response = view.form_valid(form)
        assert response.status_code == 302
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.REJECTED
        assert req.review_notes == "Declined"


@pytest.mark.django_db
def test_request_detail_view_save_succeeds_with_claimed_booth(event):
    from exhibition.forms import ExhibitionRequestReviewForm

    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Existing", is_exhibitor=True, booth_number="B-101")
        req = _request(event, "dup-save@e.com", booth_number="B-101")
        view, _ = _detail_view(
            event,
            req,
            {"action": "save", "is_exhibitor": True, "booth_number": "B-101", "review_notes": "Checked notes"},
            email="org-dup-save@e.com",
        )
        kwargs = view.get_form_kwargs()
        assert kwargs["review_action"] == "save"
        form = ExhibitionRequestReviewForm(
            data={"is_exhibitor": True, "booth_number": "B-101", "review_notes": "Checked notes"},
            instance=req,
            **{k: v for k, v in kwargs.items() if k in ("event", "review_action")},
        )
        assert form.is_valid(), form.errors
        response = view.form_valid(form)
        assert response.status_code == 302
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.SUBMITTED
        assert req.review_notes == "Checked notes"


@pytest.mark.django_db
def test_request_detail_view_approve_succeeds_with_unique_booth(event):
    from exhibition.forms import ExhibitionRequestReviewForm

    with scopes_disabled():
        req = _request(event, "uniq-approve@e.com", booth_number="B-202")
        view, _ = _detail_view(
            event,
            req,
            {"action": "approve", "is_exhibitor": True, "booth_number": "B-202"},
            email="org-uniq-approve@e.com",
        )
        kwargs = view.get_form_kwargs()
        form = ExhibitionRequestReviewForm(
            data={"is_exhibitor": True, "booth_number": "B-202"},
            instance=req,
            **{k: v for k, v in kwargs.items() if k in ("event", "review_action")},
        )
        assert form.is_valid(), form.errors
        response = view.form_valid(form)
        assert response.status_code == 302
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.ACCEPTED
        assert req.approved_exhibitor_id is not None
        assert req.approved_exhibitor.booth_number == "B-202"


@pytest.mark.django_db
def test_bulk_approve_orders_requests_deterministically_by_booth_and_pk(event):
    with scopes_disabled():
        req_b202 = _request(event, "ord-b202@e.com", name="B202", booth_number="B-202")
        req_b101 = _request(event, "ord-b101@e.com", name="B101", booth_number="B-101")
        req_none = _request(event, "ord-none@e.com", name="None", booth_number=None)

        view = _action_view(event, "org-order@e.com")
        ordered = list(view.get_requests(view.request, False, [req_none.code, req_b202.code, req_b101.code]))
        assert [r.pk for r in ordered] == [req_b101.pk, req_b202.pk, req_none.pk]


@pytest.mark.django_db
def test_bulk_approve_retries_on_40p01_deadlock_and_reraises_other_operational_errors(event):
    from unittest.mock import patch

    from django.db import OperationalError

    class _PgCause(Exception):
        def __init__(self, sqlstate):
            super().__init__(sqlstate)
            self.sqlstate = sqlstate

    def _op_error(sqlstate):
        err = OperationalError(f"db error {sqlstate}")
        err.__cause__ = _PgCause(sqlstate)
        return err

    with scopes_disabled():
        req = _request(event, "deadlock-retry@e.com", name="Retry Org", booth_number="B-101")
        view, request = _action_post_view(event, "approve", [req.code], email="org-deadlock@e.com")

        original_approve = ExhibitionRequest.approve
        attempts = 0

        def _fail_once_with_deadlock(self, requestor=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _op_error("40P01")
            return original_approve(self, requestor=requestor)

        with patch.object(ExhibitionRequest, "approve", _fail_once_with_deadlock):
            response = view.post(request)

        assert response.status_code == 200
        assert attempts == 2
        req.refresh_from_db()
        assert req.state == ExhibitionRequestState.ACCEPTED

        req_other = _request(event, "other-op@e.com", name="Other Op", booth_number="B-202")
        view2, request2 = _action_post_view(event, "approve", [req_other.code], email="org-other-op@e.com")
        other_attempts = 0

        def _fail_with_other_op_error(self, requestor=None):
            nonlocal other_attempts
            other_attempts += 1
            raise _op_error("57014")

        with patch.object(ExhibitionRequest, "approve", _fail_with_other_op_error):
            with pytest.raises(OperationalError):
                view2.post(request2)

        assert other_attempts == 1
        req_other.refresh_from_db()
        assert req_other.state == ExhibitionRequestState.SUBMITTED
