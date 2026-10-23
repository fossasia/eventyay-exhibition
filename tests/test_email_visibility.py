from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User

from exhibition.forms import ExhibitionEmailQueueForm
from exhibition.models import ExhibitionEmailQueue
from exhibition.views import (
    EmailBulkActionView,
    EmailDeleteView,
    EmailEditView,
    EmailOutboxListView,
    EmailSentListView,
)


def _member(event, email, **flags):
    user = User.objects.create_user(email=email, password="pw")
    team = event.organizer.teams.create(name=email, all_events=True, **flags)
    team.members.add(user)
    return user


def _view(view_class, event, user, **params):
    request = RequestFactory().get("/", params)
    request.user = user
    request.event = event
    request.session = SimpleNamespace(session_key=None)
    view = view_class()
    view.request = request
    return view


def _email(event):
    return ExhibitionEmailQueue.objects.create(
        event=event, to_email="applicant@example.com", subject="Hello", body="<p>Body</p>"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("view_class", [EmailEditView, EmailDeleteView, EmailOutboxListView, EmailSentListView])
def test_email_views_hide_recipients_for_hiding_reviewer(event, view_class):
    with scopes_disabled():
        user = _member(event, "r-hide@e.com", is_exhibition_reviewer=True, hide_exhibition_applicant_emails=True)
        assert _view(view_class, event, user).hide_applicant_emails is True


@pytest.mark.django_db
@pytest.mark.parametrize("view_class", [EmailEditView, EmailDeleteView, EmailOutboxListView, EmailSentListView])
def test_email_views_show_recipients_to_request_managers(event, view_class):
    with scopes_disabled():
        user = _member(event, "m@e.com", can_change_exhibition_proposals=True)
        assert _view(view_class, event, user).hide_applicant_emails is False


@pytest.mark.django_db
def test_edit_form_drops_recipient_when_hidden(event):
    with scopes_disabled():
        email = _email(event)
        form = ExhibitionEmailQueueForm(instance=email, event=event, hide_recipient=True)
        assert "to_email" not in form.fields


@pytest.mark.django_db
def test_edit_form_ignores_posted_recipient_when_hidden(event):
    with scopes_disabled():
        email = _email(event)
        form = ExhibitionEmailQueueForm(
            data={"to_email": "other@example.com", "subject": "Changed", "body": "<p>New</p>"},
            instance=email,
            event=event,
            hide_recipient=True,
        )
        assert form.is_valid(), form.errors
        form.save()
        email.refresh_from_db()
        assert email.to_email == "applicant@example.com"
        assert email.subject == "Changed"


@pytest.mark.django_db
def test_edit_form_keeps_recipient_for_managers(event):
    with scopes_disabled():
        email = _email(event)
        form = ExhibitionEmailQueueForm(instance=email, event=event)
        assert "to_email" in form.fields


@pytest.mark.django_db
@pytest.mark.parametrize("view_class", [EmailOutboxListView, EmailSentListView])
def test_list_search_skips_recipient_for_hiding_reviewer(event, view_class):
    with scopes_disabled():
        _email(event)
        queryset = ExhibitionEmailQueue.objects.filter(event=event)
        reviewer = _member(event, "r-search@e.com", is_exhibition_reviewer=True, hide_exhibition_applicant_emails=True)
        hidden = _view(view_class, event, reviewer, query="applicant@").build_filter_form()
        assert hidden.is_valid()
        assert not hidden.filter_qs(queryset).exists()
        manager = _member(event, "m-search@e.com", can_change_exhibition_proposals=True)
        shown = _view(view_class, event, manager, query="applicant@").build_filter_form()
        assert shown.is_valid()
        assert shown.filter_qs(queryset).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("view_class", [EmailOutboxListView, EmailSentListView])
@pytest.mark.parametrize("ordering", ["to_email", "-to_email"])
def test_list_sort_by_recipient_rejected_for_hiding_reviewer(event, view_class, ordering):
    with scopes_disabled():
        reviewer = _member(event, "r-sort@e.com", is_exhibition_reviewer=True, hide_exhibition_applicant_emails=True)
        assert not _view(view_class, event, reviewer, ordering=ordering).build_filter_form().is_valid()
        manager = _member(event, "m-sort@e.com", can_change_exhibition_proposals=True)
        assert _view(view_class, event, manager, ordering=ordering).build_filter_form().is_valid()


@pytest.mark.django_db
def test_bulk_select_all_skips_recipient_search_for_hiding_reviewer(event):
    with scopes_disabled():
        _email(event)
        reviewer = _member(event, "r-bulk@e.com", is_exhibition_reviewer=True, hide_exhibition_applicant_emails=True)
        view = _view(EmailBulkActionView, event, reviewer, query="applicant@", select_all_pages="true")
        assert not view.target_rows(view.request, "all").exists()
        manager = _member(event, "m-bulk@e.com", can_change_exhibition_proposals=True)
        view = _view(EmailBulkActionView, event, manager, query="applicant@", select_all_pages="true")
        assert view.target_rows(view.request, "all").exists()


@pytest.mark.django_db
def test_bulk_select_all_with_rejected_ordering_targets_nothing(event):
    with scopes_disabled():
        _email(event)
        reviewer = _member(event, "r-order@e.com", is_exhibition_reviewer=True, hide_exhibition_applicant_emails=True)
        view = _view(EmailBulkActionView, event, reviewer, query="Hello", ordering="to_email", select_all_pages="true")
        assert not view.target_rows(view.request, "all").exists()
