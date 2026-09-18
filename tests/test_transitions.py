import pytest
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
)
from exhibition.utils import create_exhibitor_from_request


def _request(event, email, state=ExhibitionRequestState.SUBMITTED, **kwargs):
    user = User.objects.create_user(email=email, password="pw")
    return ExhibitionRequest.objects.create(
        event=event,
        user=user,
        name=kwargs.pop("name", "Org"),
        state=state,
        **kwargs,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,expected",
    [
        (ExhibitionRequestState.DRAFT, {"submitted"}),
        (ExhibitionRequestState.SUBMITTED, {"accepted", "rejected", "withdrawn"}),
        (ExhibitionRequestState.ACCEPTED, {"submitted", "rejected", "withdrawn"}),
        (ExhibitionRequestState.REJECTED, {"submitted", "accepted"}),
        (ExhibitionRequestState.WITHDRAWN, {"submitted"}),
    ],
)
def test_transition_matrix(event, state, expected):
    with scopes_disabled():
        exhibition_request = _request(event, f"{state}@e.com", state=state)
        candidates = (
            ExhibitionRequestState.SUBMITTED,
            ExhibitionRequestState.ACCEPTED,
            ExhibitionRequestState.REJECTED,
            ExhibitionRequestState.WITHDRAWN,
        )
        allowed = {target.value for target in candidates if exhibition_request.can_transition_to(target)}
        assert allowed == expected


@pytest.mark.django_db
def test_available_review_actions_for_rejected_includes_approve(event):
    with scopes_disabled():
        exhibition_request = _request(event, "rej@e.com", state=ExhibitionRequestState.REJECTED)
        assert set(exhibition_request.available_review_actions()) == {"approve", "reopen"}


@pytest.mark.django_db
def test_available_review_actions_for_accepted_has_no_approve(event):
    with scopes_disabled():
        exhibition_request = _request(event, "acc@e.com", state=ExhibitionRequestState.ACCEPTED)
        assert set(exhibition_request.available_review_actions()) == {"reject", "withdraw", "reopen"}


@pytest.mark.django_db
def test_create_exhibitor_from_request_captures_profile_snapshot(event):
    with scopes_disabled():
        exhibition_request = _request(event, "snap@e.com", description="Original description")
        create_exhibitor_from_request(exhibition_request)
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED
        assert exhibition_request.profile_edited_at is None
        assert exhibition_request.accepted_profile_snapshot == exhibition_request.submitter_profile_values()
        assert exhibition_request.accepted_profile_snapshot["description"] == "Original description"


@pytest.mark.django_db
def test_reapprove_after_reject_refreshes_profile_snapshot_and_clears_edited_flag(event):
    with scopes_disabled():
        exhibition_request = _request(event, "refresh@e.com", description="Original description")
        create_exhibitor_from_request(exhibition_request)
        exhibition_request.refresh_from_db()

        exhibition_request.description = "Edited after acceptance"
        exhibition_request.profile_edited_at = exhibition_request.updated
        exhibition_request.save(update_fields=["description", "profile_edited_at"])

        exhibition_request.reject()
        exhibition_request.refresh_from_db()
        exhibition_request.approve()
        exhibition_request.refresh_from_db()

        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED
        assert exhibition_request.profile_edited_at is None
        assert exhibition_request.accepted_profile_snapshot["description"] == "Edited after acceptance"
        assert exhibition_request.profile_field_changes() == []


@pytest.mark.django_db
def test_reject_after_accept_deactivates_organization(event):
    with scopes_disabled():
        exhibition_request = _request(event, "flip@e.com")
        exhibitor = create_exhibitor_from_request(exhibition_request)
        assert exhibitor.active is True

        exhibition_request.refresh_from_db()
        exhibition_request.reject()
        exhibitor.refresh_from_db()
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.REJECTED
        assert exhibitor.active is False
        assert ExhibitorInfo.objects.filter(pk=exhibitor.pk).exists()


@pytest.mark.django_db
def test_reapprove_after_reject_reactivates_same_organization(event):
    with scopes_disabled():
        exhibition_request = _request(event, "cycle@e.com")
        exhibitor = create_exhibitor_from_request(exhibition_request)
        exhibition_request.refresh_from_db()

        exhibition_request.reject()
        exhibitor.refresh_from_db()
        assert exhibitor.active is False

        exhibition_request.refresh_from_db()
        reapproved = exhibition_request.approve()
        exhibitor.refresh_from_db()
        exhibition_request.refresh_from_db()
        assert reapproved.pk == exhibitor.pk
        assert exhibitor.active is True
        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED
        assert exhibition_request.approved_exhibitor_id == exhibitor.pk
        assert ExhibitorInfo.objects.filter(event=event).count() == 1


@pytest.mark.django_db
def test_reopen_moves_accepted_back_to_submitted_and_hides_organization(event):
    with scopes_disabled():
        exhibition_request = _request(event, "reopen@e.com")
        exhibitor = create_exhibitor_from_request(exhibition_request)
        exhibition_request.refresh_from_db()

        exhibition_request.reopen()
        exhibition_request.refresh_from_db()
        exhibitor.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.SUBMITTED
        assert exhibitor.active is False


@pytest.mark.django_db
def test_reopen_sends_no_decision_email(event):
    with scopes_disabled():
        exhibition_request = _request(event, "quiet@e.com", state=ExhibitionRequestState.REJECTED)
        exhibition_request.reopen()
        assert not ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=exhibition_request).exists()


@pytest.mark.django_db
def test_can_be_reinstated_only_when_withdrawn(event):
    with scopes_disabled():
        assert _request(event, "w@e.com", state=ExhibitionRequestState.WITHDRAWN).can_be_reinstated is True
        assert _request(event, "s@e.com", state=ExhibitionRequestState.SUBMITTED).can_be_reinstated is False
        assert _request(event, "a@e.com", state=ExhibitionRequestState.ACCEPTED).can_be_reinstated is False


@pytest.mark.django_db
def test_reinstate_withdrawn_returns_to_submitted(event):
    with scopes_disabled():
        exhibition_request = _request(event, "back@e.com", state=ExhibitionRequestState.WITHDRAWN)
        exhibition_request.reopen()
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.SUBMITTED
        assert not ExhibitionEmailQueue.objects.filter(event=event, exhibition_request=exhibition_request).exists()
