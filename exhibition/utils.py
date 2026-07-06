from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from django.db.models import Q, QuerySet
from django.utils import timezone
from eventyay.common.urls import get_url_origin, normalize_url_scheme

if TYPE_CHECKING:
    from .models import ExhibitorInfo


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
        ExhibitorInfo.objects.filter(event=event, is_exhibitor=True)
        .filter(has_logo, has_header)
        .prefetch_related("social_links", "extra_links")
        .order_by("name", "pk")
    )


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


def create_exhibitor_from_proposal(proposal):
    from .models import (
        ExhibitionProposalState,
        ExhibitorExtraLink,
        ExhibitorInfo,
        ExhibitorSocialLink,
        generate_booth_id,
    )

    if proposal.approved_exhibitor_id:
        return proposal.approved_exhibitor

    booth_id = proposal.booth_id
    if proposal.is_exhibitor and not booth_id:
        booth_id = generate_booth_id(event=proposal.event)

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
    proposal.save(update_fields=["approved_exhibitor", "state", "submitted", "updated"])
    return exhibitor
