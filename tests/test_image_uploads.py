from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import STORED_IMAGES
from django.core.files.uploadedfile import SimpleUploadedFile
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User
from PIL import Image

from exhibition.forms import ExhibitionRequestForm, ExhibitorInfoForm
from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitorInfo,
    ExhibitorSettings,
)


def make_image(name, image_format, size=(40, 40), mode="RGB"):
    buffer = BytesIO()
    Image.new(mode, size, color="white").save(buffer, format=image_format)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=Image.MIME[image_format])


def stored_format(field_file):
    with field_file.open("rb") as stored, Image.open(stored) as image:
        return image.format, image.size


def exhibitor_form(files, **kwargs):
    return ExhibitorInfoForm(data={"name_0": "Acme"}, files=files, organization_type="exhibitor", **kwargs)


@pytest.mark.django_db
def test_logo_and_banner_only_offer_png_jpeg_and_webp(event):
    form = ExhibitorInfoForm(event=event, organization_type="exhibitor")

    for field_name in ("logo", "banner"):
        assert form.fields[field_name].widget.attrs["accept"] == "image/png,image/jpeg,image/webp"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("filename", "image_format", "mode"),
    [
        ("logo.png", "PNG", "RGB"),
        ("logo.png", "PNG", "RGBA"),
        ("logo.jpg", "JPEG", "RGB"),
        ("logo.webp", "WEBP", "RGB"),
    ],
)
def test_exhibitor_logo_and_banner_are_stored_as_webp(event, filename, image_format, mode):
    form = exhibitor_form(
        {
            "logo": make_image(filename, image_format, mode=mode),
            "banner": make_image(f"banner.{image_format.lower()}", image_format, mode=mode),
        },
        event=event,
    )
    assert form.is_valid(), form.errors
    organization = form.save(commit=False)
    organization.event = event
    organization.save()

    for image in (organization.logo, organization.banner):
        assert image.name.endswith(".webp")
        assert stored_format(image)[0] == "WEBP"


@pytest.mark.django_db
def test_banner_is_resized_to_the_core_header_width(event):
    form = exhibitor_form(
        {
            "logo": make_image("logo.png", "PNG", size=(2400, 2400)),
            "banner": make_image("banner.png", "PNG", size=(2400, 800)),
        },
        event=event,
    )
    assert form.is_valid(), form.errors
    organization = form.save(commit=False)
    organization.event = event
    organization.save()

    assert stored_format(organization.logo)[1] == (1000, 1000)
    assert stored_format(organization.banner)[1] == (1920, 640)


@pytest.mark.django_db
def test_other_image_formats_are_rejected(event):
    form = exhibitor_form(
        {"logo": make_image("logo.gif", "GIF"), "banner": make_image("banner.bmp", "BMP")},
        event=event,
    )

    assert not form.is_valid()
    assert form.errors["logo"] == ["Upload a PNG, JPG or WebP image."]
    assert form.errors["banner"] == ["Upload a PNG, JPG or WebP image."]


@pytest.mark.django_db
@pytest.mark.parametrize("image_format", ["PNG", "WEBP"])
def test_animated_images_are_rejected(event, image_format):
    buffer = BytesIO()
    frames = [Image.new("RGB", (40, 40), color=color) for color in ("white", "black")]
    frames[0].save(buffer, format=image_format, save_all=True, append_images=frames[1:])
    animated = SimpleUploadedFile(f"logo.{image_format.lower()}", buffer.getvalue())
    form = exhibitor_form({"logo": animated, "banner": make_image("banner.png", "PNG")}, event=event)

    assert not form.is_valid()
    assert form.errors["logo"] == ["Animated images are not supported. Please upload a still image."]
    assert "banner" not in form.errors


def make_mpo(name="photo.jpg"):
    """A 300 x 200 red photo with a small blue second frame, tagged as taken with the camera held upright."""
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: the photo shows rotated by 90 degrees.
    buffer = BytesIO()
    Image.new("RGB", (300, 200), color="red").save(
        buffer,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", (80, 60), color="blue")],
        exif=exif,
    )
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


@pytest.mark.django_db
def test_camera_mpo_photo_is_stored_as_its_first_frame(event):
    form = exhibitor_form({"logo": make_mpo(), "banner": make_image("banner.png", "PNG")}, event=event)
    assert form.is_valid(), form.errors
    organization = form.save(commit=False)
    organization.event = event
    organization.save()

    logo_path = Path(organization.logo.name)
    assert logo_path.stem.startswith("photo") and logo_path.suffix == ".webp"
    with organization.logo.open("rb") as stored, Image.open(stored) as image:
        assert (image.format, image.size) == ("WEBP", (200, 300))
        red, green, blue = image.convert("RGB").getpixel((100, 150))
        assert red > 200 and green < 60 and blue < 60


@pytest.mark.django_db
def test_mpo_frame_past_the_pixel_limit_is_not_decoded(event, monkeypatch):
    # 300 x 200 is between the limit and twice the limit, where Pillow only warns and ImageField lets it through.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 40_000)
    with patch("exhibition.forms.ImageOps") as image_ops:
        form = exhibitor_form({"logo": make_mpo(), "banner": make_image("banner.png", "PNG")}, event=event)
        assert not form.is_valid()

    assert form.errors["logo"] == ["This image could not be processed. Please upload a different file."]
    image_ops.exif_transpose.assert_not_called()


@pytest.mark.django_db
def test_image_core_cannot_process_is_a_field_error(event):
    files = {"logo": make_image("logo.png", "PNG"), "banner": make_image("banner.png", "PNG")}
    with patch("exhibition.forms.optimize_uploaded_image", side_effect=OSError("broken")):
        form = exhibitor_form(files, event=event)
        assert not form.is_valid()

    assert form.errors["logo"] == ["This image could not be processed. Please upload a different file."]


@pytest.mark.django_db
def test_stored_images_are_kept_when_nothing_new_is_uploaded(event):
    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"}, **STORED_IMAGES)
    form = exhibitor_form({}, event=event, instance=organization)

    assert form.is_valid(), form.errors
    organization = form.save()
    assert organization.logo.name == STORED_IMAGES["logo"]
    assert organization.banner.name == STORED_IMAGES["banner"]


@pytest.mark.django_db
@pytest.mark.parametrize(("answer_name", "answer_format"), [("floor-plan.png", "PNG"), ("floor-plan.webp", "WEBP")])
def test_request_form_stores_logo_banner_and_image_answers_as_webp(event, answer_name, answer_format):
    with scopes_disabled():
        ExhibitorSettings.objects.create(
            event=event,
            call_enabled=True,
            exhibitors_access_mail_subject="",
            exhibitors_access_mail_body="",
        )
        question = ExhibitionQuestion.objects.create(
            event=event,
            question="Booth floor plan",
            variant=ExhibitionQuestionVariant.FILE,
            active=True,
        )
        user = User.objects.create_user(email="webp@example.org", password="webp-uploads-1234")
        exhibition_request = ExhibitionRequest.objects.create(event=event, user=user, name={"en": "Acme"})

        form = ExhibitionRequestForm(
            event=event,
            instance=exhibition_request,
            data={"name": "Acme", "content_locale": "en"},
            files={
                "logo": make_image("logo.png", "PNG"),
                "banner": make_image("banner.jpg", "JPEG"),
                f"question_{question.pk}": make_image(answer_name, answer_format),
            },
        )
        assert form.is_valid(), form.errors
        exhibition_request = form.save()

        answer = ExhibitionAnswer.objects.get(exhibition_request=exhibition_request, question=question)

    for image in (exhibition_request.logo, exhibition_request.banner, answer.file):
        assert image.name.endswith(".webp")
        assert stored_format(image)[0] == "WEBP"
