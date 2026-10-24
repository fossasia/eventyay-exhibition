"""Exhibition email placeholders stay in Exhibition and use the shared event placeholders from core."""

import pytest
from django_scopes import scopes_disabled
from eventyay.base.email import get_available_placeholders, shared_event_placeholders
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.models import ExhibitionRequest, ExhibitionRequestState

EXHIBITION_ONLY = ("request_name", "applicant_name", "pending_request_count", "exhibitor_name")


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


@pytest.fixture
def exhibition_request(mail_event):
    applicant = User.objects.create_user(email="applicant@example.com", password="pw", fullname="Jane Applicant")
    with scopes_disabled():
        return ExhibitionRequest.objects.create(
            event=mail_event, user=applicant, name="Acme Corp", state=ExhibitionRequestState.SUBMITTED
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "parameters", [["event"], ["event", "exhibition_request"], ["event", "exhibitor"], ["event", "user", "team"]]
)
def test_exhibition_placeholders_stay_out_of_other_editors(mail_event, parameters):
    with scopes_disabled():
        available = get_available_placeholders(mail_event, parameters)

    for identifier in EXHIBITION_ONLY:
        assert identifier not in available


@pytest.mark.django_db
def test_editor_lists_shared_event_placeholders_and_hides_old_names(mail_event):
    with scopes_disabled():
        names = mail_helpers.placeholder_names(mail_event, mail_helpers.REQUEST_PLACEHOLDER_CONTEXT)

    for placeholder in shared_event_placeholders():
        assert (placeholder.identifier in names) is placeholder.is_visible
    assert "applicant_name" in names
    assert "name" not in names


@pytest.mark.django_db
def test_request_emails_render_applicant_name_and_the_old_name(mail_event, exhibition_request):
    with scopes_disabled():
        context = mail_helpers.build_request_context(mail_event, exhibition_request)

    assert context["applicant_name"] == "Jane Applicant"
    assert context["name"] == "Jane Applicant"
    assert str(context["event_name"]) == "Test Event"
    for placeholder in shared_event_placeholders():
        assert placeholder.identifier in context
