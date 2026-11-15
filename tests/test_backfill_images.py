from io import BytesIO, StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User
from eventyay.common.management.commands.backfill_images import get_image_targets
from PIL import Image

from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitorInfo,
)


def make_png_bytes(size=(60, 60), color="blue"):
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def backfill(*args, **kwargs):
    out = StringIO()
    call_command("backfill_images", *args, "--min-size-kb", "0", stdout=out, **kwargs)
    return out.getvalue()


def test_exhibition_images_are_registered_with_core_backfill():
    targets = get_image_targets()

    assert targets["exhibition_exhibitor_logo"] == (ExhibitorInfo, "logo", False)
    assert targets["exhibition_exhibitor_banner"] == (ExhibitorInfo, "banner", False)
    assert targets["exhibition_request_logo"] == (ExhibitionRequest, "logo", False)
    assert targets["exhibition_request_banner"] == (ExhibitionRequest, "banner", False)
    assert targets["exhibition_answer"] == (ExhibitionAnswer, "file", False, True)


@pytest.mark.django_db
def test_dry_run_does_not_modify_files(event):
    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"})
        organization.logo.save("acme_logo.png", ContentFile(make_png_bytes()), save=True)

    output = backfill("--dry-run", "--model", "exhibition_exhibitor_logo")

    assert "WOULD compress ExhibitorInfo" in output
    assert "dry_run=1" in output
    with scopes_disabled():
        organization.refresh_from_db()
        assert organization.logo.name.endswith(".png")


@pytest.mark.django_db
def test_backfill_converts_exhibitor_logo_and_keeps_a_backup(event, tmp_path):
    backup_dir = tmp_path / "img_backups"
    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"})
        organization.logo.save("acme_logo.png", ContentFile(make_png_bytes()), save=True)

    output = backfill("--backup-dir", str(backup_dir), "--model", "exhibition_exhibitor_logo")

    assert "Compressed ExhibitorInfo" in output
    assert "compressed=1" in output
    assert len(list(backup_dir.glob("*acme_logo*"))) == 1
    with scopes_disabled():
        organization.refresh_from_db()
        assert organization.logo.name.endswith(".webp")
        with organization.logo.open("rb") as stored, Image.open(stored) as img:
            assert img.format == "WEBP"


@pytest.mark.django_db
def test_backfill_converts_only_the_selected_targets(event, tmp_path):
    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"})
        organization.logo.save("acme_logo.png", ContentFile(make_png_bytes()), save=True)
        user = User.objects.create_user(email="req@example.org", password="testpassword123")
        exhibition_request = ExhibitionRequest.objects.create(event=event, user=user, name={"en": "Acme Req"})
        exhibition_request.logo.save("req_logo.png", ContentFile(make_png_bytes()), save=True)
        exhibition_request.banner.save("req_banner.png", ContentFile(make_png_bytes()), save=True)

    output = backfill(
        "--backup-dir",
        str(tmp_path / "img_backups"),
        "--model",
        "exhibition_request_logo",
        "--model",
        "exhibition_request_banner",
    )

    assert "compressed=2" in output
    with scopes_disabled():
        exhibition_request.refresh_from_db()
        assert exhibition_request.logo.name.endswith(".webp")
        assert exhibition_request.banner.name.endswith(".webp")
        organization.refresh_from_db()
        assert organization.logo.name.endswith(".png")


@pytest.mark.django_db
def test_backfill_converts_image_answers_and_skips_documents(event, tmp_path):
    with scopes_disabled():
        user = User.objects.create_user(email="applicant@example.org", password="testpassword123")
        exhibition_request = ExhibitionRequest.objects.create(event=event, user=user, name={"en": "Acme"})
        image_question = ExhibitionQuestion.objects.create(
            event=event, question="Booth image", variant=ExhibitionQuestionVariant.FILE, active=True
        )
        document_question = ExhibitionQuestion.objects.create(
            event=event, question="Proposal PDF", variant=ExhibitionQuestionVariant.FILE, active=True
        )
        image_answer = ExhibitionAnswer.objects.create(question=image_question, exhibition_request=exhibition_request)
        image_answer.file.save("booth.png", ContentFile(make_png_bytes()), save=True)
        document_answer = ExhibitionAnswer.objects.create(
            question=document_question, exhibition_request=exhibition_request
        )
        document_answer.file.save("proposal.pdf", ContentFile(b"%PDF-1.4 test dummy"), save=True)

    output = backfill("--backup-dir", str(tmp_path / "img_backups"), "--model", "exhibition_answer")

    assert "compressed=1" in output
    assert "skipped=1" in output
    with scopes_disabled():
        image_answer.refresh_from_db()
        assert image_answer.file.name.endswith(".webp")
        # Pages and exports show the file answer by its stored file name.
        assert image_answer.answer_string.endswith(".webp")
        document_answer.refresh_from_db()
        assert document_answer.file.name.endswith(".pdf")
