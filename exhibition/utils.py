from urllib.parse import parse_qs, urlparse

from django.db.models import Q, QuerySet

from eventyay.common.urls import get_url_origin, normalize_url_scheme

from .models import ExhibitorInfo


def public_exhibitors_queryset(event) -> QuerySet[ExhibitorInfo]:
    has_logo = (Q(logo__isnull=False) & ~Q(logo="")) | (
        Q(logo_url__isnull=False) & ~Q(logo_url="")
    )
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

    sources = list(getattr(request, "_external_image_csp_sources", []))
    for image_url in image_urls:
        origin = get_url_origin(image_url)
        if origin:
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

    if any(
        parsed.path.lower().endswith(ext)
        for ext in (".mp4", ".m4v", ".webm", ".ogg", ".mov")
    ):
        return {"type": "video", "url": normalized}

    if "/embed/" in parsed.path and parsed.scheme == "https":
        return {"type": "iframe", "url": normalized}

    return None
