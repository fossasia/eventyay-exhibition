import csv
import io
import itertools

import openpyxl
import pytest
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Team
from eventyay.base.models.auth import User

from exhibition.exports import RequestsExporter
from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
    SponsorGroup,
)

_codes = itertools.count(1)


@pytest.fixture(autouse=True)
def _organizer_pages(settings):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()


def _client(event, **flags):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        user = User.objects.create_user(email=f"team-{next(_codes)}@example.com", password="pw")
        team = Team.objects.create(
            organizer=event.organizer, all_events=True, **(flags or {"can_change_event_settings": True})
        )
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


def _request(event, name, state=ExhibitionRequestState.SUBMITTED, **kwargs):
    user = User.objects.create_user(email=f"{name.lower()}-{next(_codes)}@example.com", password="pw")
    return ExhibitionRequest.objects.create(event=event, user=user, name=name, state=state, submitted=now(), **kwargs)


def _download(client, event, query="", export_format="default"):
    url = reverse("plugins:exhibition:request.list", kwargs={"organizer": event.organizer.slug, "event": event.slug})
    return client.get(url + f"?download=yes&_format={export_format}" + query)


def _rows(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
    header, *body = rows
    return [dict(zip(header, row)) for row in body], header


@pytest.mark.django_db
def test_download_has_the_requested_columns_and_values(event):
    with scopes_disabled():
        gold = SponsorGroup.objects.create(event=event, name={"en": "Gold"})
        size = ExhibitionQuestion.objects.create(event=event, question={"en": "Booth size"}, position=1)
        power = ExhibitionQuestion.objects.create(
            event=event, question={"en": "Power"}, variant=ExhibitionQuestionVariant.MULTIPLE, position=2
        )
        options = [
            ExhibitionQuestionOption.objects.create(question=power, answer={"en": label}) for label in ("230V", "USB")
        ]
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", is_exhibitor=True)
        accepted = _request(
            event,
            "Acme",
            state=ExhibitionRequestState.ACCEPTED,
            sponsor_group=gold,
            is_sponsor=True,
            approved_exhibitor=exhibitor,
        )
        ExhibitionAnswer.objects.create(question=size, exhibition_request=accepted, answer="Large")
        ExhibitionAnswer.objects.create(question=power, exhibition_request=accepted).options.set(options)
        pending = _request(event, "Pending")
    rows, header = _rows(_download(_client(event), event))
    by_code = {row["Code"]: row for row in rows}

    assert header == [
        "Code",
        "Organization name",
        "Applicant email",
        "State",
        "Sponsor group",
        "Submitted",
        "Last updated",
        "Booth size",
        "Power",
        "Linked exhibitor",
        "Linked exhibitor ID",
    ]
    assert by_code[accepted.code]["Organization name"] == "Acme"
    assert by_code[accepted.code]["Applicant email"] == accepted.user.email
    assert by_code[accepted.code]["State"] == "accepted"
    assert by_code[accepted.code]["Sponsor group"] == "Gold"
    assert by_code[accepted.code]["Submitted"]
    assert by_code[accepted.code]["Last updated"]
    assert by_code[accepted.code]["Booth size"] == "Large"
    assert by_code[accepted.code]["Power"].split("\n") == ["230V", "USB"]
    assert by_code[accepted.code]["Linked exhibitor"] == "Acme"
    assert by_code[accepted.code]["Linked exhibitor ID"] == str(exhibitor.pk)
    for column in ("Sponsor group", "Booth size", "Power", "Linked exhibitor", "Linked exhibitor ID"):
        assert by_code[pending.code][column] == ""


@pytest.mark.django_db
def test_download_respects_the_filters(event):
    with scopes_disabled():
        pending = _request(event, "Pending")
        _request(event, "Rejected", state=ExhibitionRequestState.REJECTED)
    rows, _header = _rows(_download(_client(event), event, query="&state=submitted"))

    assert [row["Code"] for row in rows] == [pending.code]


@pytest.mark.django_db
def test_download_only_contains_the_selected_requests(event):
    with scopes_disabled():
        first = _request(event, "First")
        _request(event, "Second")
        third = _request(event, "Third")
    rows, _header = _rows(_download(_client(event), event, query=f"&code={first.code}&code={third.code}"))

    assert sorted(row["Code"] for row in rows) == sorted([first.code, third.code])


@pytest.mark.django_db
def test_download_as_xlsx(event):
    with scopes_disabled():
        acme = _request(event, "Acme")
    response = _download(_client(event), event, export_format="xlsx")

    assert response.status_code == 200
    assert response["Content-Disposition"].endswith('.xlsx"')
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:2] == ("Code", "Organization name")
    assert rows[1][:2] == (acme.code, "Acme")


@pytest.mark.django_db
def test_download_rejects_unknown_formats(event):
    assert _download(_client(event), event, export_format="json").status_code == 400


@pytest.mark.django_db
def test_proposal_managers_can_download(event):
    with scopes_disabled():
        _request(event, "Acme")
    rows, _header = _rows(_download(_client(event, can_change_exhibition_proposals=True), event))

    assert [row["Organization name"] for row in rows] == ["Acme"]


@pytest.mark.django_db
def test_reviewers_cannot_download(event):
    response = _download(_client(event, is_exhibition_reviewer=True), event)

    assert response.status_code == 403


@pytest.mark.django_db
def test_hidden_applicant_emails_drop_the_column(event):
    with scopes_disabled():
        acme = _request(event, "Acme")
        request = RequestFactory().get("/")
        request.event = event
        exporter = RequestsExporter(request, ExhibitionRequest.objects.filter(event=event), hide_emails=True)
        _filename, _content_type, payload = exporter.render({"_format": "default"})
    header, row = list(csv.reader(io.StringIO(payload.decode("utf-8"))))

    assert "Applicant email" not in header
    assert acme.user.email not in row
