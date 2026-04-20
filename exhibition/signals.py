from django.db.models import Prefetch
from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import resolve, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from eventyay.control.signals import nav_event, nav_event_settings
from eventyay.presale.signals import (
    front_page_after_content,
    header_nav_tabs,
    sass_postamble,
)

from .models import ExhibitorInfo, SponsorGroup
from .utils import add_external_image_csp_sources, public_exhibitors_queryset


@receiver(nav_event, dispatch_uid="exhibitors_nav")
def control_nav_import(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            "label": _("Exhibitors/Sponsors"),
            "url": reverse(
                "plugins:exhibition:info",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.event.organizer.slug,
                },
            ),
            "active": url.namespace == "plugins:exhibition"
            and url.url_name != "settings",
            "icon": "map-pin",
        }
    ]


@receiver(nav_event_settings, dispatch_uid="exhibitors_nav")
def navbar_info(sender, request, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_event_settings", request=request
    ):
        return []
    return [
        {
            "label": _("Exhibitors/Sponsors"),
            "url": reverse(
                "plugins:exhibition:settings",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url.namespace == "plugins:exhibition"
            and url.url_name == "settings",
        }
    ]


@receiver(front_page_after_content, dispatch_uid="exhibition_front_page_supporters")
def presale_supported_by(sender, request=None, **kwargs):
    sponsor_groups = list(
        SponsorGroup.objects.filter(event=sender, show_on_front_page=True)
        .prefetch_related(
            Prefetch(
                "partners",
                queryset=ExhibitorInfo.objects.filter(
                    event=sender, is_sponsor=True
                ).order_by("name"),
                to_attr="front_page_partners",
            )
        )
        .order_by("name")
    )
    sponsor_groups = [
        group
        for group in sponsor_groups
        if any(partner.visible_logo_url for partner in group.front_page_partners)
    ]

    if not sponsor_groups:
        return ""

    add_external_image_csp_sources(
        request,
        [
            partner.visible_logo_url
            for group in sponsor_groups
            for partner in group.front_page_partners
            if partner.visible_logo_url
        ],
    )

    template = get_template("sponsors/presale_supported_by.html")
    return template.render(
        {
            "event": sender,
            "sponsor_groups": sponsor_groups,
        },
        request=request,
    )


@receiver(header_nav_tabs, dispatch_uid="exhibition_presale_nav_tab")
def exhibition_presale_nav_tab(sender, request=None, **kwargs):
    if not request or not public_exhibitors_queryset(sender).exists():
        return ""

    return format_html(
        '<a href="{}" class="header-tab {}"><i class="fa fa-building-o"></i> {}</a>',
        reverse(
            "plugins:exhibition:public_list",
            kwargs={
                "organizer": sender.organizer.slug,
                "event": sender.slug,
            },
        ),
        "active" if "/exhibition/" in request.path_info else "",
        _("Exhibition"),
    )


@receiver(sass_postamble, dispatch_uid="exhibition_front_page_supporters_styles")
def presale_supported_by_styles(sender, filename="", **kwargs):
    if filename != "main.scss":
        return ""

    return """
    .front-page.exhibition-supported-by {
        margin-top: 32px;
    }

    .front-page.exhibition-supported-by h3 {
        margin-top: 0;
        margin-bottom: 20px;
    }

    .exhibition-supported-by-group + .exhibition-supported-by-group {
        margin-top: 28px;
    }

    .exhibition-supported-by-group h4 {
        margin: 0 0 14px;
        font-size: 24px;
        font-weight: 600;
    }

    .exhibition-supported-by-grid {
        margin-left: -9px;
        margin-right: -9px;
    }

    .exhibition-supported-by-card {
        margin-bottom: 18px;
    }

    .exhibition-supported-by-card .thumbnail {
        margin-bottom: 0;
        border-radius: 6px;
        overflow: hidden;
    }

    .exhibition-supported-by-logo {
        display: flex;
        align-items: center;
        justify-content: center;
        aspect-ratio: 1 / 1;
        min-height: 220px;
        padding: 24px;
        background: #fff;
    }

    .exhibition-supported-by-logo img {
        width: 100%;
        height: 100%;
        max-width: 100%;
        max-height: 100%;
        object-fit: contain;
        display: block;
    }

    .exhibition-public-page {
        margin-top: 32px;
    }

    .exhibition-public-page h2 {
        margin-top: 0;
        margin-bottom: 24px;
    }

    .exhibition-public-card-link {
        display: block;
        color: inherit;
        text-decoration: none;
    }

    .exhibition-public-card-link:hover,
    .exhibition-public-card-link:focus {
        color: inherit;
        text-decoration: none;
    }

    .exhibition-public-card {
        margin-bottom: 30px;
    }

    .exhibition-public-card .thumbnail {
        margin-bottom: 0;
        border-radius: 8px;
        overflow: hidden;
        border: 1px solid #e6e6e6;
        transition: transform 0.18s ease, box-shadow 0.18s ease;
    }

    .exhibition-public-card-link:hover .thumbnail,
    .exhibition-public-card-link:focus .thumbnail {
        transform: translateY(-2px);
        box-shadow: 0 10px 24px rgba(0, 0, 0, 0.08);
    }

    .exhibition-public-card-image {
        background: #f5f5f5;
        aspect-ratio: 16 / 10;
        overflow: hidden;
    }

    .exhibition-public-card-image img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }

    .exhibition-public-card-meta {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 14px 18px 18px;
        background: #fff;
    }

    .exhibition-public-card-logo {
        width: 38px;
        height: 38px;
        object-fit: contain;
        flex: 0 0 38px;
    }

    .exhibition-public-card-name {
        font-size: 24px;
        font-weight: 600;
        line-height: 1.2;
    }

    .exhibition-detail-nav {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 24px;
    }

    .exhibition-detail-hero {
        border-radius: 8px;
        overflow: hidden;
        margin-bottom: 24px;
        background: #f5f5f5;
    }

    .exhibition-detail-hero img {
        width: 100%;
        max-height: 600px;
        display: block;
        object-fit: cover;
    }

    .exhibition-detail-header {
        text-align: center;
        margin-bottom: 24px;
    }

    .exhibition-detail-logo {
        width: 92px;
        height: 92px;
        object-fit: contain;
        margin: 0 auto 16px;
        display: block;
    }

    .exhibition-detail-title {
        margin: 0 0 12px;
        font-size: 48px;
        line-height: 1.1;
    }

    .exhibition-detail-description {
        max-width: 1100px;
        margin: 0 auto;
        font-size: 22px;
        line-height: 1.6;
    }

    .exhibition-detail-description p:last-child {
        margin-bottom: 0;
    }

    .exhibition-detail-actions {
        margin-top: 24px;
        text-align: center;
    }

    @media (max-width: 767px) {
        .exhibition-public-card-name {
            font-size: 20px;
        }

        .exhibition-detail-nav {
            flex-direction: column;
        }

        .exhibition-detail-nav .btn {
            width: 100%;
        }

        .exhibition-detail-title {
            font-size: 34px;
        }

        .exhibition-detail-description {
            font-size: 18px;
        }
    }

    """
