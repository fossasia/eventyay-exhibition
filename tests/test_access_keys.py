import pytest
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django_scopes import scopes_disabled

from exhibition.models import ExhibitorInfo
from exhibition.views import ExhibitorListView, access_newly_granted


class _User:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.is_authenticated = True

    def has_event_permission(self, organizer, event, permission, request=None):
        return self.allowed


def _download(event, organization_type=None, allowed=True):
    request = RequestFactory().get("/", {"download": "yes"})
    request.event = event
    request.user = _User(allowed=allowed)
    view = ExhibitorListView(organization_type=organization_type)
    view.request = request
    return view.get(request)


@pytest.mark.django_db
def test_download_requires_change_settings_permission(event):
    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Acme", is_exhibitor=True)
        with pytest.raises(PermissionDenied):
            _download(event, organization_type="exhibitor", allowed=False)


@pytest.mark.django_db
def test_download_returns_csv_with_keys(event):
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(
            event=event, name="Acme", email="a@example.com", booth_id="B-9", is_exhibitor=True
        )
        response = _download(event, organization_type="exhibitor")
        body = response.content.decode("utf-8")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Cache-Control"] == "no-store"
    assert "exhibitor-keys.csv" in response["Content-Disposition"]
    assert "Acme" in body
    assert "B-9" in body
    assert exhibitor.key in body


@pytest.mark.django_db
def test_download_respects_organization_type_filter(event):
    with scopes_disabled():
        sponsor = ExhibitorInfo.objects.create(event=event, name="SponsorCo", is_sponsor=True, is_exhibitor=False)
        exhibitor = ExhibitorInfo.objects.create(event=event, name="ExhibitorCo", is_sponsor=False, is_exhibitor=True)

        sponsor_body = _download(event, organization_type="sponsor").content.decode("utf-8")
        exhibitor_body = _download(event, organization_type="exhibitor").content.decode("utf-8")

    assert sponsor.key in sponsor_body
    assert exhibitor.key not in sponsor_body
    assert exhibitor.key in exhibitor_body
    assert sponsor.key not in exhibitor_body


@pytest.mark.django_db
def test_download_filename_matches_organization_type(event):
    with scopes_disabled():
        ExhibitorInfo.objects.create(event=event, name="Acme", is_sponsor=True, is_exhibitor=True)
        sponsor = _download(event, organization_type="sponsor")
        combined = _download(event)

    assert "sponsor-keys.csv" in sponsor["Content-Disposition"]
    assert "organization-keys.csv" in combined["Content-Disposition"]


def _flags(lead=False, voucher=False):
    return ExhibitorInfo(lead_scanning_enabled=lead, allow_voucher_access=voucher)


def test_access_granted_on_create_for_either_flag():
    assert access_newly_granted(_flags(lead=True))
    assert access_newly_granted(_flags(voucher=True))
    assert not access_newly_granted(_flags())


def test_access_granted_when_voucher_access_newly_enabled():
    previous = {"lead_scanning_enabled": False, "allow_voucher_access": False}
    assert access_newly_granted(_flags(voucher=True), previous)


def test_access_granted_when_lead_scanning_newly_enabled():
    previous = {"lead_scanning_enabled": False, "allow_voucher_access": False}
    assert access_newly_granted(_flags(lead=True), previous)


def test_access_not_granted_when_flag_was_already_enabled():
    previous = {"lead_scanning_enabled": True, "allow_voucher_access": False}
    assert not access_newly_granted(_flags(lead=True), previous)


def test_access_granted_when_second_flag_added_to_existing_one():
    previous = {"lead_scanning_enabled": True, "allow_voucher_access": False}
    assert access_newly_granted(_flags(lead=True, voucher=True), previous)


@pytest.mark.django_db
def test_list_view_renders_normally_without_download_param(event):
    request = RequestFactory().get("/")
    request.event = event
    request.user = _User()
    view = ExhibitorListView(organization_type="exhibitor")
    view.request = request
    with scopes_disabled():
        assert view.get_queryset().count() == 0
