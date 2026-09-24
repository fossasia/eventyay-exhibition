import pytest
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.models import ExhibitionRequest, ExhibitionRequestState
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
def test_can_be_withdrawn_only_for_submitted_and_accepted(event):
    with scopes_disabled():
        assert _request(event, "s@e.com", state=ExhibitionRequestState.SUBMITTED).can_be_withdrawn is True
        assert _request(event, "a@e.com", state=ExhibitionRequestState.ACCEPTED).can_be_withdrawn is True
        assert _request(event, "d@e.com", state=ExhibitionRequestState.DRAFT).can_be_withdrawn is False
        assert _request(event, "r@e.com", state=ExhibitionRequestState.REJECTED).can_be_withdrawn is False
        assert _request(event, "w@e.com", state=ExhibitionRequestState.WITHDRAWN).can_be_withdrawn is False


@pytest.mark.django_db
def test_withdraw_submitted_sets_state(event):
    with scopes_disabled():
        exhibition_request = _request(event, "sub@e.com")
        exhibition_request.withdraw()
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.WITHDRAWN
        assert exhibition_request.approved_exhibitor_id is None


@pytest.mark.django_db
def test_withdraw_accepted_deactivates_organization_without_deleting(event):
    with scopes_disabled():
        exhibition_request = _request(event, "acc@e.com")
        exhibitor = create_exhibitor_from_request(exhibition_request)
        exhibition_request.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.ACCEPTED
        assert exhibition_request.approved_exhibitor_id == exhibitor.pk
        assert exhibitor.active is True

        exhibition_request.withdraw()
        exhibition_request.refresh_from_db()
        exhibitor.refresh_from_db()
        assert exhibition_request.state == ExhibitionRequestState.WITHDRAWN
        assert exhibition_request.approved_exhibitor_id == exhibitor.pk
        assert exhibitor.active is False


@pytest.mark.django_db
def test_withdrawn_organization_hidden_from_public_queryset(event):
    from exhibition.utils import public_exhibitors_queryset

    with scopes_disabled():
        exhibition_request = _request(event, "pub@e.com")
        exhibition_request.logo = "exhibition-requests/logos/logo.png"
        exhibition_request.banner = "exhibition-requests/banners/banner.png"
        exhibition_request.save(update_fields=["logo", "banner"])
        exhibitor = create_exhibitor_from_request(exhibition_request)
        assert exhibitor in public_exhibitors_queryset(event)

        exhibition_request.refresh_from_db()
        exhibition_request.withdraw()
        assert exhibitor not in public_exhibitors_queryset(event)
