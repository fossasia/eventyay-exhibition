import pytest
from django.test import RequestFactory
from django.urls import resolve, reverse

LEGACY_TO_CURRENT = (
    ("", "request.user_edit"),
    ("withdraw/", "request.user_withdraw"),
    ("reinstate/", "request.user_reinstate"),
)


def _legacy_path(event, suffix):
    return f"/{event.organizer.slug}/{event.slug}/exhibition/call/proposals/ABCD1234EFGH/{suffix}"


def _current_url(event, name):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "code": "ABCD1234EFGH"},
    )


@pytest.mark.django_db
@pytest.mark.parametrize("suffix, name", LEGACY_TO_CURRENT)
def test_legacy_request_url_redirects_permanently(event, suffix, name):
    match = resolve(_legacy_path(event, suffix))
    response = match.func(RequestFactory().get(_legacy_path(event, suffix)), **match.kwargs)
    assert response.status_code == 301
    assert response["Location"] == _current_url(event, name)


@pytest.mark.django_db
def test_legacy_request_url_keeps_the_query_string(event):
    path = _legacy_path(event, "") + "?tab=answers"
    match = resolve(_legacy_path(event, ""))
    response = match.func(RequestFactory().get(path), **match.kwargs)
    assert response["Location"] == _current_url(event, "request.user_edit") + "?tab=answers"
