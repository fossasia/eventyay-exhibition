import base64

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.timezone import now
from eventyay.base.models import Event, Organizer

_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

STORED_IMAGES = {"logo": "exhibitors/logos/Acme/logo.png", "banner": "exhibitors/banners/Acme/banner.png"}


@pytest.fixture
def event(db):
    """Create a test event with an organizer."""
    organizer = Organizer.objects.create(name="Test Organizer", slug="test-organizer")
    event = Event.objects.create(
        organizer=organizer,
        name="Test Event",
        slug="test-event",
        live=True,
        date_from=now(),
    )
    return event


@pytest.fixture
def image_uploads():
    """Logo and banner are locked-required, so any valid form post must carry both files."""

    def make():
        return {
            "logo": SimpleUploadedFile("logo.png", _PNG_BYTES, content_type="image/png"),
            "banner": SimpleUploadedFile("banner.png", _PNG_BYTES, content_type="image/png"),
        }

    return make
