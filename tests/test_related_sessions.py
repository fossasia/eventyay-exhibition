import datetime as dt

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client
from django.urls import reverse
from django_scopes import scope
from eventyay.base.models import Submission, SubmissionStates, SubmissionType, TalkSlot, User

from exhibition.forms import ExhibitorInfoForm
from exhibition.models import ExhibitorInfo
from exhibition.utils import public_exhibitor_sessions


def session_type(event):
    with scope(event=event):
        return SubmissionType.objects.create(event=event, name="Talk", default_duration=30)


def session(event, submission_type, title, state=SubmissionStates.CONFIRMED):
    with scope(event=event):
        return Submission.objects.create(event=event, title=title, submission_type=submission_type, state=state)


def release_with_slots(event, *slots):
    """Release the schedule and place one slot per ``(submission, is_visible)`` pair on it."""
    with scope(event=event):
        event.release_schedule("v1")
        return [
            TalkSlot.objects.create(
                submission=submission,
                schedule=event.current_schedule,
                is_visible=visible,
                start=event.datetime_from,
                end=event.datetime_from + dt.timedelta(minutes=30),
            )
            for submission, visible in slots
        ]


def publish_talks(event):
    event.talks_published = True
    event.save(update_fields=["talks_published"])


def exhibitor(event, **kwargs):
    return ExhibitorInfo.objects.create(event=event, name="Acme", **kwargs)


def link(exhibitor_info, *submissions):
    with scope(event=exhibitor_info.event):
        exhibitor_info.sessions.add(*submissions)


@pytest.mark.django_db
def test_only_visible_slots_of_linked_sessions_are_public(event):
    publish_talks(event)
    kind = session_type(event)
    linked_visible = session(event, kind, "Linked and visible")
    linked_hidden = session(event, kind, "Linked but hidden")
    unlinked = session(event, kind, "Not linked")
    visible_slot, _, _ = release_with_slots(event, (linked_visible, True), (linked_hidden, False), (unlinked, True))
    organization = exhibitor(event)
    link(organization, linked_visible, linked_hidden)

    with scope(organizer=event.organizer):
        assert public_exhibitor_sessions(organization, AnonymousUser()) == [visible_slot]


@pytest.mark.django_db
def test_no_public_sessions_while_talks_are_unpublished(event):
    kind = session_type(event)
    talk = session(event, kind, "Confirmed")
    release_with_slots(event, (talk, True))
    organization = exhibitor(event)
    link(organization, talk)

    with scope(organizer=event.organizer):
        assert public_exhibitor_sessions(organization, AnonymousUser()) == []


@pytest.mark.django_db
def test_no_public_sessions_without_a_released_schedule(event):
    publish_talks(event)
    kind = session_type(event)
    talk = session(event, kind, "Confirmed")
    organization = exhibitor(event)
    link(organization, talk)

    with scope(organizer=event.organizer):
        assert public_exhibitor_sessions(organization, AnonymousUser()) == []


@pytest.mark.django_db
def test_session_picker_offers_only_accepted_sessions_of_this_event(event):
    kind = session_type(event)
    confirmed = session(event, kind, "Confirmed")
    accepted = session(event, kind, "Accepted", state=SubmissionStates.ACCEPTED)
    session(event, kind, "Rejected", state=SubmissionStates.REJECTED)
    session(event, kind, "Withdrawn", state=SubmissionStates.WITHDRAWN)
    speaker = User.objects.create_user(
        email="speaker@example.com", password="secret", fullname="Ada Lovelace", locale="en"
    )
    with scope(event=event):
        confirmed.speakers.add(speaker)

    with scope(organizer=event.organizer):
        form = ExhibitorInfoForm(event=event, organization_type="exhibitor")
        choices = {str(value): label for value, label in form.fields["sessions"].choices}

    assert set(choices) == {str(confirmed.pk), str(accepted.pk)}
    assert choices[str(confirmed.pk)] == "Confirmed — Ada Lovelace"
    assert choices[str(accepted.pk)] == "Accepted"


@pytest.mark.django_db
def test_session_picker_reads_and_saves_links_under_organizer_scope(event):
    kind = session_type(event)
    first = session(event, kind, "First")
    second = session(event, kind, "Second")
    organization = exhibitor(event)
    link(organization, first)

    with scope(organizer=event.organizer):
        form = ExhibitorInfoForm(instance=organization, event=event, organization_type="exhibitor")
        assert list(form.initial["sessions"]) == [first]

        form = ExhibitorInfoForm(
            {"name_0": "Acme", "is_exhibitor": "on", "sessions": [second.pk]},
            instance=organization,
            event=event,
            organization_type="exhibitor",
        )
        assert form.is_valid(), form.errors
        form.save()

    with scope(event=event):
        assert list(organization.sessions.values_list("pk", flat=True)) == [second.pk]


@pytest.mark.django_db
def test_public_detail_page_lists_related_sessions(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    publish_talks(event)
    kind = session_type(event)
    talk = session(event, kind, "Opening keynote")
    release_with_slots(event, (talk, True))
    organization = exhibitor(event, logo="exhibitors/logos/Acme/logo.png", banner="exhibitors/banners/Acme/hero.png")
    link(organization, talk)

    url = reverse(
        "plugins:exhibition:public_detail",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "pk": organization.pk},
    )
    response = Client().get(url)
    html = response.content.decode()

    assert response.status_code == 200
    assert "Related sessions" in html
    assert "Opening keynote" in html


@pytest.mark.django_db
def test_public_detail_page_hides_section_without_sessions(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    publish_talks(event)
    release_with_slots(event)
    organization = exhibitor(event, logo="exhibitors/logos/Acme/logo.png", banner="exhibitors/banners/Acme/hero.png")

    url = reverse(
        "plugins:exhibition:public_detail",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "pk": organization.pk},
    )
    response = Client().get(url)

    assert response.status_code == 200
    assert "Related sessions" not in response.content.decode()
