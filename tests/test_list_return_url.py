from urllib.parse import urlencode

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory
from django.urls import reverse
from django_scopes import scopes_disabled

from exhibition.models import ExhibitionEmailDraft, ExhibitionEmailQueue, ExhibitorInfo
from exhibition.views import (
    EmailDeleteView,
    EmailDraftDeleteView,
    EmailDraftListView,
    ExhibitorDeleteView,
    ExhibitorDeviceManageView,
    ExhibitorEditView,
    ExhibitorVoucherManageView,
    list_return_url,
    outbox_return_url,
)


def _kwargs(event):
    return {"organizer": event.organizer.slug, "event": event.slug}


def _request(event, method="get", **params):
    request = getattr(RequestFactory(), method)("/", params)
    request.event = event
    return request


def _view(view_class, request, obj):
    view = view_class()
    view.request = request
    view.object = obj
    view.kwargs = {}
    return view


@pytest.mark.django_db
def test_return_url_keeps_page_and_filters_of_the_same_list(event):
    list_url = reverse("plugins:exhibition:exhibitors", kwargs=_kwargs(event))
    next_url = f"{list_url}?page=3&query=acme"
    assert list_return_url(_request(event, next=next_url), list_url) == next_url
    assert list_return_url(_request(event, "post", next=next_url), list_url) == next_url


@pytest.mark.django_db
@pytest.mark.parametrize("next_url", ["", "https://evil.example/?page=2", "//evil.example/?page=2", "/other/?page=2"])
def test_return_url_ignores_anything_but_the_list(event, next_url):
    list_url = reverse("plugins:exhibition:exhibitors", kwargs=_kwargs(event))
    assert list_return_url(_request(event, next=next_url), list_url) == list_url


@pytest.mark.django_db
def test_outbox_return_url_drops_finished_bulk_marker(event):
    outbox = reverse("plugins:exhibition:email.outbox", kwargs=_kwargs(event))
    request = _request(event, next=f"{outbox}?page=2&bulk=done")
    assert outbox_return_url(request) == f"{outbox}?page=2"
    assert outbox_return_url(_request(event, next=f"{outbox}?bulk=done")) == outbox


@pytest.mark.django_db
@pytest.mark.parametrize("view_class", [ExhibitorEditView, ExhibitorDeleteView])
def test_organization_row_actions_return_to_the_page_they_came_from(event, view_class):
    with scopes_disabled():
        sponsor = ExhibitorInfo.objects.create(event=event, name="Acme", is_sponsor=True, is_exhibitor=True)
    sponsors = reverse("plugins:exhibition:sponsors", kwargs=_kwargs(event))
    next_url = f"{sponsors}?page=2"
    request = _request(event, type="sponsor", next=next_url)
    assert _view(view_class, request, sponsor).get_success_url() == next_url
    assert _view(view_class, _request(event, type="sponsor"), sponsor).get_success_url() == sponsors


@pytest.mark.django_db
def test_outbox_discard_returns_to_the_page_it_came_from(event):
    with scopes_disabled():
        email = ExhibitionEmailQueue.objects.create(
            event=event, to_email="a@example.com", subject="Hi", body="<p>Hi</p>"
        )
    outbox = reverse("plugins:exhibition:email.outbox", kwargs=_kwargs(event))
    request = _request(event, next=f"{outbox}?{urlencode({'page': 2, 'query': 'Hi'})}")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    assert _view(EmailDeleteView, request, email).get_success_url() == f"{outbox}?page=2&query=Hi"


@pytest.mark.django_db
def test_draft_discard_returns_to_the_page_it_came_from(event):
    with scopes_disabled():
        draft = ExhibitionEmailDraft.objects.create(event=event, subject="Draft")
    drafts = reverse("plugins:exhibition:email.drafts", kwargs=_kwargs(event))
    request = _request(event, next=f"{drafts}?page=4")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    assert _view(EmailDraftDeleteView, request, draft).get_success_url() == f"{drafts}?page=4"


@pytest.mark.django_db
def test_list_shows_last_page_when_returning_to_a_page_that_no_longer_exists(event):
    with scopes_disabled():
        for index in range(3):
            ExhibitionEmailDraft.objects.create(event=event, subject=f"Draft {index}")
        view = EmailDraftListView()
        view.request = _request(event, page=9)
        view.kwargs = {}
        paginator, page, _rows, _paged = view.paginate_queryset(view.get_queryset(), 2)
        assert page.number == paginator.num_pages == 2


@pytest.mark.django_db
@pytest.mark.parametrize("view_class", [ExhibitorVoucherManageView, ExhibitorDeviceManageView])
def test_voucher_and_device_pages_keep_the_list_they_came_from(event, view_class):
    with scopes_disabled():
        sponsor = ExhibitorInfo.objects.create(event=event, name="Acme", is_sponsor=True, is_exhibitor=True)
    sponsors = reverse("plugins:exhibition:sponsors", kwargs=_kwargs(event))
    request = _request(event, type="sponsor", next=f"{sponsors}?page=2")
    success_url = _view(view_class, request, sponsor).get_success_url()
    assert success_url.endswith(f"?{urlencode({'type': 'sponsor', 'next': f'{sponsors}?page=2'})}")
