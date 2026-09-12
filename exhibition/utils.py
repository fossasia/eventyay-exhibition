from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote_plus, urlparse

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from eventyay.common.urls import get_url_origin, normalize_url_scheme
from eventyay.common.utils.language import localize_event_text
from i18nfield.strings import LazyI18nString

if TYPE_CHECKING:
    from .models import ExhibitorInfo


def localized_value_for(value, locale) -> str:
    """Read one locale out of an internationalized value, falling back to any filled locale."""
    if value is None:
        return ""
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return str(data) if data else ""
    if data.get(locale):
        return data[locale]
    for candidate in data.values():
        if candidate:
            return candidate
    return ""


def merge_localized_value(existing, locale, text):
    """Write ``text`` into a single locale of an internationalized value, keeping the others."""
    data = getattr(existing, "data", existing)
    merged = {code: value for code, value in data.items() if value} if isinstance(data, dict) else {}
    if not isinstance(data, dict) and data:
        merged[locale] = str(data)
    text = (text or "").strip()
    if text:
        merged[locale] = text
    else:
        merged.pop(locale, None)
    if not merged:
        return ""
    return LazyI18nString(merged)


def should_hide_applicant_emails(user, event, request=None) -> bool:
    if not user.is_authenticated:
        return False
    if user.has_event_permission(
        event.organizer,
        event,
        ("can_change_event_settings", "can_change_exhibition_proposals"),
        request=request,
    ):
        return False
    reviewer_teams = event.teams.filter(members__in=[user], is_exhibition_reviewer=True)
    return bool(reviewer_teams) and all(team.hide_exhibition_applicant_emails for team in reviewer_teams)


def public_exhibitors_queryset(event) -> QuerySet["ExhibitorInfo"]:
    from .models import ExhibitorInfo

    has_logo = Q(logo__isnull=False) & ~Q(logo="")
    has_banner = Q(banner__isnull=False) & ~Q(banner="")
    return (
        ExhibitorInfo.objects.filter(event=event, is_exhibitor=True, active=True)
        .filter(has_logo, has_banner)
        .prefetch_related("social_links", "extra_links")
        .order_by("exhibitor_position", "name", "pk")
    )


def allow_blob_image_previews(request):
    """Permit blob: images in img-src so local file previews render on this page."""
    if not request:
        return
    sources = list(getattr(request, "_external_image_csp_sources", []))
    if "blob:" not in sources:
        sources.append("blob:")
    request._external_image_csp_sources = sources


def add_external_image_csp_sources(request, image_urls):
    if not request:
        return

    existing_sources = list(getattr(request, "_external_image_csp_sources", []))
    sources = []
    seen = set()
    for existing in existing_sources:
        if existing not in seen:
            seen.add(existing)
            sources.append(existing)

    for image_url in image_urls:
        origin = get_url_origin(image_url)
        if origin and origin not in seen:
            seen.add(origin)
            sources.append(origin)

    request._external_image_csp_sources = sources


def build_exhibitor_video_embed(url: str) -> dict | None:
    url = (url or "").strip()
    if not url:
        return None

    normalized = normalize_url_scheme(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    path_parts = [part for part in path.split("/") if part]

    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        return {
            "type": "iframe",
            "url": f"https://www.youtube.com/embed/{path_parts[0]}",
        }

    if host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }:
        video_id = ""
        if path_parts[:1] == ["watch"]:
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif path_parts[:1] in (["embed"], ["shorts"], ["live"]):
            video_id = path_parts[1] if len(path_parts) > 1 else ""
        if video_id:
            return {
                "type": "iframe",
                "url": f"https://www.youtube.com/embed/{video_id}",
            }

    if host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}:
        video_id = ""
        if path_parts[:2] == ["video", path_parts[1] if len(path_parts) > 1 else ""]:
            video_id = path_parts[1]
        elif path_parts:
            video_id = path_parts[-1]
        if video_id.isdigit():
            return {
                "type": "iframe",
                "url": f"https://player.vimeo.com/video/{video_id}",
            }

    if any(parsed.path.lower().endswith(ext) for ext in (".mp4", ".m4v", ".webm", ".ogg", ".mov")):
        return {"type": "video", "url": normalized}

    if "/embed/" in parsed.path and parsed.scheme == "https":
        return {"type": "iframe", "url": normalized}

    return None


def create_exhibitor_from_request(exhibition_request, requestor=None):
    from .models import (
        LOG_ORGANIZATION_CREATED,
        LOG_ORGANIZATION_REACTIVATED,
        ExhibitionRequestState,
        ExhibitorExtraLink,
        ExhibitorInfo,
        ExhibitorSocialLink,
        generate_booth_id,
    )

    booth_id = exhibition_request.booth_id
    if exhibition_request.is_exhibitor and not booth_id:
        booth_id = generate_booth_id(event=exhibition_request.event)

    if exhibition_request.approved_exhibitor_id:
        exhibitor = exhibition_request.approved_exhibitor
        exhibitor.active = True
        exhibitor.is_exhibitor = exhibition_request.is_exhibitor
        exhibitor.is_sponsor = exhibition_request.is_sponsor
        exhibitor.sponsor_group = exhibition_request.sponsor_group if exhibition_request.is_sponsor else None
        exhibitor.booth_id = booth_id if exhibition_request.is_exhibitor else None
        exhibitor.booth_name = exhibition_request.booth_name if exhibition_request.is_exhibitor else ""
        exhibitor.save(
            update_fields=["active", "is_exhibitor", "is_sponsor", "sponsor_group", "booth_id", "booth_name"]
        )
        exhibition_request.state = ExhibitionRequestState.ACCEPTED
        exhibition_request.submitted = exhibition_request.submitted or timezone.now()
        exhibition_request.profile_edited_at = None
        exhibition_request.capture_profile_snapshot()
        exhibition_request.save(
            update_fields=["state", "submitted", "profile_edited_at", "accepted_profile_snapshot", "updated"]
        )
        exhibitor.log_action(
            LOG_ORGANIZATION_REACTIVATED,
            data={"exhibition_request": exhibition_request.code},
            user=requestor,
        )
        return exhibitor

    exhibitor = ExhibitorInfo.objects.create(
        event=exhibition_request.event,
        name=exhibition_request.name,
        description=exhibition_request.description,
        url=exhibition_request.url,
        email=exhibition_request.email,
        contact_url=exhibition_request.contact_url,
        video_url=exhibition_request.video_url,
        slides=exhibition_request.slides,
        slides_url=exhibition_request.slides_url,
        logo=exhibition_request.logo,
        banner=exhibition_request.banner,
        is_sponsor=exhibition_request.is_sponsor,
        sponsor_group=exhibition_request.sponsor_group if exhibition_request.is_sponsor else None,
        is_exhibitor=exhibition_request.is_exhibitor,
        booth_id=booth_id if exhibition_request.is_exhibitor else None,
        booth_name=exhibition_request.booth_name if exhibition_request.is_exhibitor else "",
    )
    ExhibitorSocialLink.objects.bulk_create(
        [
            ExhibitorSocialLink(
                exhibitor=exhibitor,
                network=link.network,
                url=link.url,
            )
            for link in exhibition_request.social_links.all()
        ]
    )
    ExhibitorExtraLink.objects.bulk_create(
        [
            ExhibitorExtraLink(
                exhibitor=exhibitor,
                label=link.label,
                url=link.url,
            )
            for link in exhibition_request.extra_links.all()
        ]
    )
    exhibition_request.approved_exhibitor = exhibitor
    exhibition_request.state = ExhibitionRequestState.ACCEPTED
    exhibition_request.submitted = exhibition_request.submitted or timezone.now()
    exhibition_request.profile_edited_at = None
    exhibition_request.capture_profile_snapshot()
    exhibition_request.save(
        update_fields=[
            "approved_exhibitor",
            "state",
            "submitted",
            "profile_edited_at",
            "accepted_profile_snapshot",
            "updated",
        ]
    )
    exhibitor.log_action(
        LOG_ORGANIZATION_CREATED,
        data={"exhibition_request": exhibition_request.code, "booth_id": exhibitor.booth_id},
        user=requestor,
    )
    return exhibitor


def event_voucher_settings(event):
    """Event-wide voucher defaults, without creating a settings row on a read path."""
    from .models import ExhibitorSettings

    return ExhibitorSettings.objects.filter(event=event).first() or ExhibitorSettings(event=event)


def resolve_voucher_pool_tag(exhibitor, *, event_settings=None):
    """The pool an exhibitor draws from: the sponsor pool for sponsors, else the exhibitor pool."""
    settings = event_settings or event_voucher_settings(exhibitor.event)
    if exhibitor.is_sponsor and not exhibitor.is_exhibitor and settings.sponsor_voucher_pool_tag:
        return settings.sponsor_voucher_pool_tag
    return settings.voucher_pool_tag


def resolve_voucher_defaults(exhibitor, *, event_settings=None):
    """How many pool vouchers this exhibitor gets, and which pool they come from.

    The count is their sponsor group's when they have one, otherwise the event-wide default.
    Pass ``event_settings`` when resolving for many exhibitors to avoid a query per row.
    """
    settings = event_settings or event_voucher_settings(exhibitor.event)
    source = exhibitor.sponsor_group if exhibitor.sponsor_group_id else settings
    return {
        "count": source.voucher_default_count,
        "pool_tag": resolve_voucher_pool_tag(exhibitor, event_settings=settings),
    }


def pool_tag_choices(event):
    """Every voucher tag in use on this event, for the pool dropdowns."""
    from eventyay.base.models import Voucher

    return list(
        Voucher.objects.filter(event=event).exclude(tag="").values_list("tag", flat=True).distinct().order_by("tag")
    )


def unassigned_pool_vouchers(event, pool_tag):
    """Vouchers in the pool that no exhibitor holds yet.

    Excluded by subquery rather than ``exhibitor_link__isnull``: that builds an outer join, and
    Postgres refuses ``SELECT ... FOR UPDATE`` on the nullable side of one.
    """
    from eventyay.base.models import Voucher

    from .models import ExhibitorVoucher

    if not pool_tag:
        return Voucher.objects.none()
    linked_ids = ExhibitorVoucher.objects.filter(exhibitor__event=event).values("voucher_id")
    return Voucher.objects.filter(event=event, tag=pool_tag).exclude(pk__in=linked_ids)


def pool_remaining(event, pool_tag):
    return unassigned_pool_vouchers(event, pool_tag).count()


def claim_pool_vouchers(exhibitor, count, *, pool_tag=None):
    """Hand ``count`` unclaimed pool vouchers to this exhibitor, or none at all if the pool is short."""
    from .models import ExhibitorVoucher

    if not count:
        return []
    if pool_tag is None:
        pool_tag = resolve_voucher_pool_tag(exhibitor)
    with transaction.atomic():
        available = list(
            unassigned_pool_vouchers(exhibitor.event, pool_tag)
            .select_for_update(skip_locked=True)
            .order_by("pk")[:count]
        )
        if len(available) < count:
            return []
        return ExhibitorVoucher.objects.bulk_create(
            ExhibitorVoucher(exhibitor=exhibitor, voucher=voucher) for voucher in available
        )


REQUEST_LOCALIZED_PROFILE_FIELDS = ("name", "description")


def provision_exhibitor_devices(exhibitor, count, *, user=None):
    """Create ``count`` lead-scanning devices for an exhibitor and link them."""
    from eventyay.base.models import Device

    from .models import ExhibitorDevice

    organization_name = localize_event_text(exhibitor.name) or str(exhibitor.name)
    existing = ExhibitorDevice.objects.filter(exhibitor=exhibitor).count()
    links = []
    for index in range(count):
        device = Device(
            organizer=exhibitor.event.organizer,
            name=f"{organization_name} #{existing + index + 1}",
            all_events=False,
            security_profile="eventyay_checkin",
        )
        device.save()
        device.limit_events.add(exhibitor.event)
        device.log_action("eventyay.device.created", user=user, data={"exhibitor": exhibitor.pk})
        links.append(ExhibitorDevice(exhibitor=exhibitor, device=device))
    return ExhibitorDevice.objects.bulk_create(links)


def reset_exhibitor_device_setup(exhibitor, *, user=None):
    """Regenerate setup tokens for an exhibitor's devices; returns those that were live."""
    from eventyay.base.models.devices import generate_initialization_token

    from .models import ExhibitorDevice

    disconnected = []
    for link in ExhibitorDevice.objects.filter(exhibitor=exhibitor).select_related("device"):
        device = link.device
        if device.api_token and device.initialized:
            disconnected.append(device)
        device.initialization_token = generate_initialization_token()
        device.api_token = None
        device.initialized = None
        device.revoked = False
        device.save(update_fields=["initialization_token", "api_token", "initialized", "revoked"])
        device.log_action(
            "eventyay.device.setup_token_reset",
            user=user,
            data={"had_active_session": device in disconnected, "exhibitor": exhibitor.pk},
        )
    return disconnected


REQUEST_SYNCED_PROFILE_FIELDS = (
    "name",
    "description",
    "url",
    "email",
    "contact_url",
    "video_url",
    "slides",
    "slides_url",
    "logo",
    "banner",
)


def sync_exhibitor_from_request(exhibition_request, requestor=None):
    """Push submitter-owned profile fields of an accepted exhibition_request onto its organization profile."""
    from .models import LOG_ORGANIZATION_SYNCED, ExhibitorExtraLink, ExhibitorSocialLink

    exhibitor = exhibition_request.approved_exhibitor
    if not exhibitor:
        return None

    locale = exhibition_request.content_locale
    for field in REQUEST_SYNCED_PROFILE_FIELDS:
        if field in REQUEST_LOCALIZED_PROFILE_FIELDS:
            setattr(
                exhibitor,
                field,
                merge_localized_value(
                    getattr(exhibitor, field),
                    locale,
                    localized_value_for(getattr(exhibition_request, field), locale),
                ),
            )
        else:
            setattr(exhibitor, field, getattr(exhibition_request, field))
    if exhibitor.is_exhibitor:
        exhibitor.booth_name = merge_localized_value(
            exhibitor.booth_name,
            locale,
            localized_value_for(exhibition_request.booth_name, locale),
        )
    exhibitor.save()

    exhibitor.social_links.all().delete()
    ExhibitorSocialLink.objects.bulk_create(
        [
            ExhibitorSocialLink(
                exhibitor=exhibitor,
                network=link.network,
                url=link.url,
            )
            for link in exhibition_request.social_links.all()
        ]
    )
    exhibitor.extra_links.all().delete()
    ExhibitorExtraLink.objects.bulk_create(
        [
            ExhibitorExtraLink(
                exhibitor=exhibitor,
                label=link.label,
                url=link.url,
            )
            for link in exhibition_request.extra_links.all()
        ]
    )
    exhibitor.log_action(
        LOG_ORGANIZATION_SYNCED,
        data={"exhibition_request": exhibition_request.code},
        user=requestor,
    )
    return exhibitor


VOUCHER_CSV_FILENAME = "exhibitor-vouchers.csv"


def voucher_redeem_url(event, voucher):
    """Public checkout link that pre-applies this voucher code."""
    from eventyay.multidomain.urlreverse import build_absolute_uri

    url = f"{build_absolute_uri(event, 'presale:event.redeem')}?voucher={quote_plus(voucher.code)}"
    if voucher.subevent_id:
        url = f"{url}&subevent={voucher.subevent_id}"
    return url


def build_voucher_csv(event, vouchers) -> str:
    """Render an exhibitor's vouchers as CSV, shared by the download view and the voucher email."""
    import io

    from defusedcsv import csv
    from django.utils.translation import gettext_lazy as _

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC, delimiter=",")
    writer.writerow(
        [
            str(_("Voucher code")),
            str(_("Redeem link")),
            str(_("Product")),
            str(_("Price effect")),
            str(_("Value")),
            str(_("Valid until")),
            str(_("Redeemed")),
            str(_("Maximum usages")),
        ]
    )
    for voucher in vouchers:
        writer.writerow(
            [
                voucher.code,
                voucher_redeem_url(event, voucher),
                str(voucher.product) if voucher.product else "",
                str(voucher.get_price_mode_display()),
                str(voucher.value) if voucher.value is not None else "",
                voucher.valid_until.isoformat() if voucher.valid_until else "",
                str(voucher.redeemed),
                str(voucher.max_usages),
            ]
        )
    return output.getvalue()


VOUCHER_CSV_RETENTION = timedelta(days=30)


def store_voucher_csv(event, vouchers):
    """Persist the voucher CSV as a CachedFile so it can be attached to an outgoing email."""
    from django.core.files.base import ContentFile
    from eventyay.base.models import CachedFile

    cached = CachedFile.objects.create(
        filename=VOUCHER_CSV_FILENAME,
        type="text/csv",
        web_download=False,
        expires=timezone.now() + VOUCHER_CSV_RETENTION,
    )
    cached.file.save(VOUCHER_CSV_FILENAME, ContentFile(build_voucher_csv(event, vouchers).encode("utf-8")))
    cached.save()
    return cached
