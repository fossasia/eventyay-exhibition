from django.db.models import Prefetch
from django.dispatch import receiver
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import resolve, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from eventyay.control.signals import nav_event, nav_event_settings
from eventyay.presale.signals import (
    front_page_after_content,
    header_nav_tabs,
    html_head,
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
        SponsorGroup.objects.filter(
            event=sender, show_on_front_page=True
        ).prefetch_related(
            Prefetch(
                "partners",
                queryset=ExhibitorInfo.objects.filter(
                    event=sender, is_sponsor=True
                ).order_by("name"),
                to_attr="front_page_partners",
            )
        )
    )
    sponsor_groups = [
        group
        for group in sponsor_groups
        if any(partner.visible_logo_url for partner in group.front_page_partners)
    ]
    sponsor_groups.sort(key=lambda group: (group.level, group.pk))

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


@receiver(html_head, dispatch_uid="exhibition_front_page_supporters_styles")
def presale_supported_by_styles(sender, request=None, **kwargs):
    return format_html(
        '<link rel="stylesheet" type="text/css" href="{}">',
        static("exhibition/css/presale-supported-by.css"),
    )
