from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

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

    has_logo = (Q(logo__isnull=False) & ~Q(logo="")) | (Q(logo_url__isnull=False) & ~Q(logo_url=""))
    has_header = (Q(header_image__isnull=False) & ~Q(header_image="")) | (
        Q(header_image_url__isnull=False) & ~Q(header_image_url="")
    )
    return (
        ExhibitorInfo.objects.filter(event=event, is_exhibitor=True, active=True)
        .filter(has_logo, has_header)
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


def create_exhibitor_from_proposal(proposal, requestor=None):
    from .models import (
        LOG_PARTNER_CREATED,
        LOG_PARTNER_REACTIVATED,
        ExhibitionProposalState,
        ExhibitorExtraLink,
        ExhibitorInfo,
        ExhibitorSocialLink,
        generate_booth_id,
    )

    booth_id = proposal.booth_id
    if proposal.is_exhibitor and not booth_id:
        booth_id = generate_booth_id(event=proposal.event)

    if proposal.approved_exhibitor_id:
        exhibitor = proposal.approved_exhibitor
        exhibitor.active = True
        exhibitor.is_exhibitor = proposal.is_exhibitor
        exhibitor.is_sponsor = proposal.is_sponsor
        exhibitor.sponsor_group = proposal.sponsor_group if proposal.is_sponsor else None
        exhibitor.booth_id = booth_id if proposal.is_exhibitor else None
        exhibitor.booth_name = proposal.booth_name if proposal.is_exhibitor else ""
        exhibitor.save(
            update_fields=["active", "is_exhibitor", "is_sponsor", "sponsor_group", "booth_id", "booth_name"]
        )
        proposal.state = ExhibitionProposalState.ACCEPTED
        proposal.submitted = proposal.submitted or timezone.now()
        proposal.profile_edited_at = None
        proposal.capture_profile_snapshot()
        proposal.save(update_fields=["state", "submitted", "profile_edited_at", "accepted_profile_snapshot", "updated"])
        exhibitor.log_action(
            LOG_PARTNER_REACTIVATED,
            data={"proposal": proposal.code},
            user=requestor,
        )
        return exhibitor

    exhibitor = ExhibitorInfo.objects.create(
        event=proposal.event,
        name=proposal.name,
        description=proposal.description,
        url=proposal.url,
        email=proposal.email,
        contact_url=proposal.contact_url,
        video_url=proposal.video_url,
        slides=proposal.slides,
        slides_url=proposal.slides_url,
        logo=proposal.logo,
        logo_url=proposal.logo_url,
        header_image=proposal.header_image,
        header_image_url=proposal.header_image_url,
        is_sponsor=proposal.is_sponsor,
        sponsor_group=proposal.sponsor_group if proposal.is_sponsor else None,
        is_exhibitor=proposal.is_exhibitor,
        booth_id=booth_id if proposal.is_exhibitor else None,
        booth_name=proposal.booth_name if proposal.is_exhibitor else "",
    )
    ExhibitorSocialLink.objects.bulk_create(
        [
            ExhibitorSocialLink(
                exhibitor=exhibitor,
                network=link.network,
                url=link.url,
            )
            for link in proposal.social_links.all()
        ]
    )
    ExhibitorExtraLink.objects.bulk_create(
        [
            ExhibitorExtraLink(
                exhibitor=exhibitor,
                label=link.label,
                url=link.url,
            )
            for link in proposal.extra_links.all()
        ]
    )
    proposal.approved_exhibitor = exhibitor
    proposal.state = ExhibitionProposalState.ACCEPTED
    proposal.submitted = proposal.submitted or timezone.now()
    proposal.profile_edited_at = None
    proposal.capture_profile_snapshot()
    proposal.save(
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
        LOG_PARTNER_CREATED,
        data={"proposal": proposal.code, "booth_id": exhibitor.booth_id},
        user=requestor,
    )
    return exhibitor


def generate_exhibitor_vouchers(exhibitor, *, product, count, max_usages, price_mode, value, valid_until):
    from eventyay.base.models import Voucher

    from .models import ExhibitorVoucher

    tag = f"exhibitor-{exhibitor.key}"
    links = []
    for _ in range(count):
        voucher = Voucher.objects.create(
            event=exhibitor.event,
            product=product,
            max_usages=max_usages,
            price_mode=price_mode,
            value=value,
            valid_until=valid_until,
            tag=tag,
        )
        links.append(ExhibitorVoucher(exhibitor=exhibitor, voucher=voucher))
    return ExhibitorVoucher.objects.bulk_create(links)


PROPOSAL_LOCALIZED_PROFILE_FIELDS = ("name", "description")


def provision_exhibitor_devices(exhibitor, count, *, user=None):
    """Create ``count`` lead-scanning devices for an exhibitor and link them."""
    from eventyay.base.models import Device

    from .models import ExhibitorDevice

    partner_name = localize_event_text(exhibitor.name) or str(exhibitor.name)
    existing = ExhibitorDevice.objects.filter(exhibitor=exhibitor).count()
    links = []
    for index in range(count):
        device = Device(
            organizer=exhibitor.event.organizer,
            name=f"{partner_name} #{existing + index + 1}",
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


PROPOSAL_SYNCED_PROFILE_FIELDS = (
    "name",
    "description",
    "url",
    "email",
    "contact_url",
    "video_url",
    "slides",
    "slides_url",
    "logo",
    "logo_url",
    "header_image",
    "header_image_url",
)


def sync_exhibitor_from_proposal(proposal, requestor=None):
    """Push submitter-owned profile fields of an accepted proposal onto its partner profile."""
    from .models import LOG_PARTNER_SYNCED, ExhibitorExtraLink, ExhibitorSocialLink

    exhibitor = proposal.approved_exhibitor
    if not exhibitor:
        return None

    locale = proposal.content_locale
    for field in PROPOSAL_SYNCED_PROFILE_FIELDS:
        if field in PROPOSAL_LOCALIZED_PROFILE_FIELDS:
            setattr(
                exhibitor,
                field,
                merge_localized_value(
                    getattr(exhibitor, field),
                    locale,
                    localized_value_for(getattr(proposal, field), locale),
                ),
            )
        else:
            setattr(exhibitor, field, getattr(proposal, field))
    if exhibitor.is_exhibitor:
        exhibitor.booth_name = merge_localized_value(
            exhibitor.booth_name,
            locale,
            localized_value_for(proposal.booth_name, locale),
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
            for link in proposal.social_links.all()
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
            for link in proposal.extra_links.all()
        ]
    )
    exhibitor.log_action(
        LOG_PARTNER_SYNCED,
        data={"proposal": proposal.code},
        user=requestor,
    )
    return exhibitor
