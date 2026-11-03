"""Compress stored raster exhibition images that exceed a size threshold.

Run on a server after deploying the image compression pipeline to shrink
existing uploads in place.

Usage::

    python manage.py backfill_exhibition_images --backup-dir /path/to/safe/backup
    python manage.py backfill_exhibition_images --dry-run
    python manage.py backfill_exhibition_images --min-size-kb 500 --backup-dir /path/to/backup
"""

import logging
import os
import shutil
from contextlib import nullcontext

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django_scopes import scopes_disabled
from eventyay.common.image import is_svg_filename, process_image
from eventyay.helpers.image_optimize import QUESTION_IMAGE_EXTENSIONS

from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionRequest,
    ExhibitorInfo,
)

logger = logging.getLogger(__name__)

IMAGE_TARGETS = {
    "exhibitor_logo": (ExhibitorInfo, "logo", False),
    "exhibitor_banner": (ExhibitorInfo, "banner", False),
    "request_logo": (ExhibitionRequest, "logo", False),
    "request_banner": (ExhibitionRequest, "banner", False),
    "exhibition_answer": (ExhibitionAnswer, "file", False, True),
}

MODEL_ALIASES = {
    "exhibitor_info": ["exhibitor_logo", "exhibitor_banner"],
    "exhibition_request": ["request_logo", "request_banner"],
    "exhibitorinfo": ["exhibitor_logo", "exhibitor_banner"],
    "exhibitionrequest": ["request_logo", "request_banner"],
    "exhibitionanswer": ["exhibition_answer"],
}


class Command(BaseCommand):
    help = "Compress stored raster exhibition images larger than a threshold."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List files that would be compressed without modifying storage.",
        )
        parser.add_argument(
            "--min-size-kb",
            type=int,
            default=getattr(settings, "IMAGE_BACKFILL_MIN_SIZE_KB", 500),
            help="Only process raster images larger than this many kilobytes.",
        )
        parser.add_argument(
            "--backup-dir",
            type=str,
            help=(
                "Directory outside of MEDIA_ROOT to store original files before compression. "
                "Required unless --dry-run is set."
            ),
        )
        parser.add_argument(
            "--model",
            action="append",
            choices=list(IMAGE_TARGETS.keys()) + list(MODEL_ALIASES.keys()),
            help="Only process specific models. Can be specified multiple times.",
        )

    def _backup_file(self, image, backup_dir, prefix):
        if not backup_dir:
            return
        os.makedirs(backup_dir, exist_ok=True)
        basename = os.path.basename(image.name)
        backup_filename = f"{prefix}_{timezone.now():%Y%m%d%H%M%S%f}_{basename}"
        backup_path = os.path.join(backup_dir, backup_filename)

        with image.open("rb") as f:
            with open(backup_path, "wb") as out_f:
                shutil.copyfileobj(f, out_f)

        self.stdout.write(f"Backed up to {backup_path}")

    def handle(self, *args, dry_run=False, min_size_kb=500, backup_dir=None, **options):
        if not dry_run and not backup_dir:
            raise CommandError("--backup-dir is required unless --dry-run is set to prevent data loss.")

        if backup_dir:
            backup_abs = os.path.abspath(backup_dir)
            media_abs = os.path.abspath(settings.MEDIA_ROOT)
            if backup_abs == media_abs or backup_abs.startswith(media_abs + os.sep):
                raise CommandError(f"--backup-dir ({backup_abs}) must not be inside MEDIA_ROOT ({media_abs}).")

        selected_models = options.get("model")
        if selected_models:
            models_to_process = []
            for item in selected_models:
                if item in MODEL_ALIASES:
                    models_to_process.extend(MODEL_ALIASES[item])
                else:
                    models_to_process.append(item)
            models_to_process = list(dict.fromkeys(models_to_process))
        else:
            models_to_process = list(IMAGE_TARGETS.keys())

        min_bytes = min_size_kb * 1024
        stats = {"compressed": 0, "failed": 0, "skipped": 0, "dry_run": 0}

        def process_image_field(
            image,
            model_name,
            pk,
            generate_thumbnail,
            raster_only=False,
            model=None,
            field_name=None,
        ):
            expected_name = image.name
            lock = transaction.atomic() if model else nullcontext()
            with lock:
                if model:
                    instance = model.objects.select_for_update().filter(pk=pk).first()
                    if not instance:
                        stats["skipped"] += 1
                        return
                    image = getattr(instance, field_name)
                    if not image or image.name != expected_name:
                        stats["skipped"] += 1
                        return

                if not image or not image.name or is_svg_filename(image.name):
                    stats["skipped"] += 1
                    return

                if raster_only and os.path.splitext(image.name)[1].lower() not in QUESTION_IMAGE_EXTENSIONS:
                    stats["skipped"] += 1
                    return

                try:
                    size = image.size
                except (NotImplementedError, AttributeError, OSError):
                    stats["failed"] += 1
                    logger.exception("Could not read size for %s on %s pk=%s", image.name, model_name, pk)
                    self.stderr.write(self.style.ERROR(f"FAILED size read: {model_name} pk={pk} file={image.name}"))
                    return

                if size <= min_bytes:
                    stats["skipped"] += 1
                    return

                try:
                    location = image.path
                except (NotImplementedError, AttributeError):
                    location = "remote storage"

                if dry_run:
                    stats["dry_run"] += 1
                    self.stdout.write(
                        f"WOULD compress {model_name} pk={pk} name={image.name} "
                        f"location={location} size={size / 1024:.2f} KB"
                    )
                    return

                try:
                    self._backup_file(image, backup_dir, prefix=f"{model_name}_{pk}")
                except OSError as e:
                    stats["failed"] += 1
                    self.stderr.write(
                        self.style.ERROR(f"FAILED backup: {model_name} pk={pk} file={image.name} error={e}")
                    )
                    return False

                if process_image(image=image, generate_thumbnail=generate_thumbnail):
                    stats["compressed"] += 1
                    try:
                        new_size = image.size
                    except (NotImplementedError, AttributeError, OSError):
                        new_size = "unknown"

                    size_kb = f"{size / 1024:.2f} KB"
                    new_size_kb = f"{new_size / 1024:.2f} KB" if isinstance(new_size, int) else "unknown"

                    if model is ExhibitionAnswer and instance:
                        new_file_name = image.name
                        if instance.answer == expected_name:
                            instance.answer = new_file_name
                            instance.save(update_fields=["answer"])
                        elif instance.answer == os.path.basename(expected_name):
                            instance.answer = os.path.basename(new_file_name)
                            instance.save(update_fields=["answer"])

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Compressed {model_name} pk={pk} name={image.name} "
                            f"location={location} (size: {size_kb} -> {new_size_kb})"
                        )
                    )
                    return True
                stats["failed"] += 1
                self.stderr.write(self.style.ERROR(f"FAILED compress: {model_name} pk={pk} file={image.name}"))
                return False

        with scopes_disabled():
            for model_key in models_to_process:
                target = IMAGE_TARGETS.get(model_key)
                if not target:
                    continue
                model, field_name, generate_thumbnail, *target_options = target
                raster_only = bool(target_options and target_options[0])
                queryset = model.objects.exclude(**{f"{field_name}__isnull": True}).exclude(**{field_name: ""})
                for instance in queryset.iterator(chunk_size=200):
                    image = getattr(instance, field_name)
                    process_image_field(
                        image,
                        model.__name__,
                        instance.pk,
                        generate_thumbnail,
                        raster_only,
                        model,
                        field_name,
                    )

        self.stdout.write(
            self.style.SUCCESS(
                "Done. compressed={compressed} failed={failed} skipped={skipped} dry_run={dry_run}".format(**stats)
            )
        )
