import pytest
from django.core.files.base import ContentFile
from django.test import override_settings

from exhibition.models import ExhibitorInfo

LONG_NAME = "Indic Mediawiki Developers User Group(IMDUG) - affiliated with Wikimedia Foundation."


@pytest.mark.django_db
@pytest.mark.parametrize("field_name", ["logo", "banner", "slides"])
def test_long_exhibitor_name_still_fits_the_file_field_limit(event, tmp_path, field_name):
    exhibitor = ExhibitorInfo(event=event, name=LONG_NAME)
    field = ExhibitorInfo._meta.get_field(field_name)

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        getattr(exhibitor, field_name).save(f"{field_name}.png", ContentFile(b"x"), save=False)

    stored_name = getattr(exhibitor, field_name).name
    assert len(stored_name) <= field.max_length
    assert "Indic Mediawiki Developers User Group(IM" in stored_name


@pytest.mark.django_db
def test_short_exhibitor_name_keeps_the_existing_path_format(event):
    exhibitor = ExhibitorInfo(event=event, name="Acme")
    field = ExhibitorInfo._meta.get_field("logo")

    assert field.generate_filename(exhibitor, "logo.png") == "exhibitors/logos/Acme/logo.png"


@pytest.mark.django_db
def test_slashes_and_trailing_dots_in_the_name_do_not_change_the_folder_depth(event):
    exhibitor = ExhibitorInfo(event=event, name="R/D Team...")
    field = ExhibitorInfo._meta.get_field("banner")

    assert field.generate_filename(exhibitor, "b.png") == "exhibitors/banners/R-D Team/b.png"
