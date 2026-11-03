from io import BytesIO, StringIO
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django_scopes import scopes_disabled
from eventyay.base.models.auth import User
from PIL import Image

from exhibition.management.commands.backfill_exhibition_images import IMAGE_TARGETS, MODEL_ALIASES
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


def test_backfill_targets_and_aliases():
    assert IMAGE_TARGETS["exhibitor_logo"] == (ExhibitorInfo, "logo", False)
    assert IMAGE_TARGETS["exhibitor_banner"] == (ExhibitorInfo, "banner", False)
    assert IMAGE_TARGETS["request_logo"] == (ExhibitionRequest, "logo", False)
    assert IMAGE_TARGETS["request_banner"] == (ExhibitionRequest, "banner", False)
    assert IMAGE_TARGETS["exhibition_answer"] == (ExhibitionAnswer, "file", False, True)

    assert MODEL_ALIASES["exhibitor_info"] == ["exhibitor_logo", "exhibitor_banner"]
    assert MODEL_ALIASES["exhibition_request"] == ["request_logo", "request_banner"]
    assert MODEL_ALIASES["exhibitionanswer"] == ["exhibition_answer"]


@pytest.mark.django_db
def test_backup_dir_is_required_for_live_run():
    with pytest.raises(CommandError, match="--backup-dir is required"):
        call_command("backfill_exhibition_images")


@pytest.mark.django_db
def test_backup_dir_cannot_be_inside_media_root(tmp_path):
    media_backup = Path(settings.MEDIA_ROOT) / "backup"
    with pytest.raises(CommandError, match="must not be inside MEDIA_ROOT"):
        call_command("backfill_exhibition_images", backup_dir=str(media_backup))


@pytest.mark.django_db
def test_dry_run_does_not_modify_files_or_require_backup(event):
    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"})
        organization.logo.save("acme_logo.png", ContentFile(make_png_bytes()), save=True)

    out = StringIO()
    call_command("backfill_exhibition_images", dry_run=True, min_size_kb=0, stdout=out)
    output = out.getvalue()

    assert "WOULD compress ExhibitorInfo" in output
    assert "dry_run=1" in output

    with scopes_disabled():
        organization.refresh_from_db()
        assert organization.logo.name.endswith(".png")


@pytest.mark.django_db
def test_backfill_compresses_exhibitor_logo_and_creates_backup(event, tmp_path):
    backup_dir = tmp_path / "img_backups"

    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"})
        organization.logo.save("acme_logo.png", ContentFile(make_png_bytes()), save=True)

    out = StringIO()
    call_command(
        "backfill_exhibition_images",
        backup_dir=str(backup_dir),
        min_size_kb=0,
        model=["exhibitor_logo"],
        stdout=out,
    )
    output = out.getvalue()

    assert "Backed up to" in output
    assert "Compressed ExhibitorInfo" in output
    assert "compressed=1" in output

    backup_files = list(backup_dir.glob("*acme_logo.png"))
    assert len(backup_files) == 1

    with scopes_disabled():
        organization.refresh_from_db()
        assert organization.logo.name.endswith(".webp")
        with organization.logo.open("rb") as stored, Image.open(stored) as img:
            assert img.format == "WEBP"


@pytest.mark.django_db
def test_backfill_processes_raster_question_answers_and_skips_non_images(event, tmp_path):
    backup_dir = tmp_path / "img_backups"

    with scopes_disabled():
        user = User.objects.create_user(email="applicant@example.org", password="testpassword123")
        exhibition_request = ExhibitionRequest.objects.create(event=event, user=user, name={"en": "Acme"})

        q_img = ExhibitionQuestion.objects.create(
            event=event,
            question="Booth image",
            variant=ExhibitionQuestionVariant.FILE,
            active=True,
        )
        q_doc = ExhibitionQuestion.objects.create(
            event=event,
            question="Proposal PDF",
            variant=ExhibitionQuestionVariant.FILE,
            active=True,
        )

        ans_img = ExhibitionAnswer.objects.create(
            question=q_img,
            exhibition_request=exhibition_request,
            answer="booth.png",
        )
        ans_img.file.save("booth.png", ContentFile(make_png_bytes()), save=True)
        ans_img.answer = ans_img.file.name
        ans_img.save()

        ans_doc = ExhibitionAnswer.objects.create(
            question=q_doc,
            exhibition_request=exhibition_request,
            answer="proposal.pdf",
        )
        ans_doc.file.save("proposal.pdf", ContentFile(b"%PDF-1.4 test dummy"), save=True)

    out = StringIO()
    call_command(
        "backfill_exhibition_images",
        backup_dir=str(backup_dir),
        min_size_kb=0,
        model=["exhibition_answer"],
        stdout=out,
    )
    output = out.getvalue()

    assert "compressed=1" in output
    assert "skipped=1" in output

    with scopes_disabled():
        ans_img.refresh_from_db()
        assert ans_img.file.name.endswith(".webp")
        assert ans_img.answer == ans_img.file.name

        ans_doc.refresh_from_db()
        assert ans_doc.file.name.endswith(".pdf")


@pytest.mark.django_db
def test_model_alias_filters_targets(event, tmp_path):
    backup_dir = tmp_path / "img_backups"

    with scopes_disabled():
        organization = ExhibitorInfo.objects.create(event=event, name={"en": "Acme"})
        organization.logo.save("acme_logo.png", ContentFile(make_png_bytes()), save=True)
        organization.banner.save("acme_banner.png", ContentFile(make_png_bytes()), save=True)

        user = User.objects.create_user(email="req@example.org", password="testpassword123")
        req = ExhibitionRequest.objects.create(event=event, user=user, name={"en": "Acme Req"})
        req.logo.save("req_logo.png", ContentFile(make_png_bytes()), save=True)

    out = StringIO()
    call_command(
        "backfill_exhibition_images",
        backup_dir=str(backup_dir),
        min_size_kb=0,
        model=["exhibitor_info"],
        stdout=out,
    )
    output = out.getvalue()

    assert "compressed=2" in output

    with scopes_disabled():
        organization.refresh_from_db()
        assert organization.logo.name.endswith(".webp")
        assert organization.banner.name.endswith(".webp")

        req.refresh_from_db()
        assert req.logo.name.endswith(".png")
