import pytest
from django.test import RequestFactory

from exhibition.views import PublicExhibitorListView


def make_request(path, data):
    request = RequestFactory().get(path, data=data)
    return request


@pytest.mark.django_db
def test_clear_button_redirects_to_the_unfiltered_list(event):
    request = make_request("/exhibition/", {"query": "acme", "clear": "1"})
    request.event = event

    response = PublicExhibitorListView.as_view()(request)

    assert response.status_code == 302
    assert response["Location"] == "/exhibition/"


@pytest.mark.django_db
def test_search_without_the_clear_button_keeps_filtering(event):
    request = make_request("/exhibition/", {"query": "acme"})
    request.event = event

    response = PublicExhibitorListView.as_view()(request)

    assert response.status_code == 200
    assert list(response.context_data["exhibitors"]) == []
