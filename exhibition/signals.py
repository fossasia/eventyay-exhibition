from django.db.models import Prefetch
from django.dispatch import receiver
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from eventyay.control.signals import event_dashboard_components
from eventyay.presale.signals import (
    front_page_after_content,
    header_nav_tabs,
    html_head,
)

from .models import ExhibitorInfo, ExhibitorSettings, SponsorGroup
from .utils import add_external_image_csp_sources, public_exhibitors_queryset


@receiver(event_dashboard_components, dispatch_uid="exhibition_dashboard_component")
def exhibition_dashboard_component(sender, request=None, **kwargs):
    url = reverse(
        "plugins:exhibition:info",
        kwargs={
            "organizer": sender.organizer.slug,
            "event": sender.slug,
        },
    )
    return format_html(
        '<div class="panel panel-default widget-container widget-small no-padding last-column">'
        '<div class="panel-heading"><h3 class="panel-title">{}</h3></div>'
        '<div class="panel-body"><p>{}</p><p>{} <a href="{}">{}</a></p></div>'
        "</div>",
        str(_("Exhibitors & Sponsors")),
        str(_("Manage exhibitors and sponsors, maintain booth details, and create partner profiles for the event.")),
        str(_("Go to")),
        url,
        str(_("Exhibitors & Sponsors Dashboard")),
    )


@receiver(front_page_after_content, dispatch_uid="exhibition_front_page_supporters")
def presale_supported_by(sender, request=None, **kwargs):
    sponsor_groups = list(
        SponsorGroup.objects.filter(event=sender, show_on_front_page=True).prefetch_related(
            Prefetch(
                "partners",
                queryset=ExhibitorInfo.objects.filter(event=sender, is_sponsor=True).order_by("name"),
                to_attr="front_page_partners",
            )
        )
    )
    sponsor_groups = [
        group for group in sponsor_groups if any(partner.visible_logo_url for partner in group.front_page_partners)
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
    if not request:
        return ""

    links = []
    if public_exhibitors_queryset(sender).exists():
        links.append(
            format_html(
                '<a href="{}" class="header-tab {}"><i class="fa fa-building-o"></i> {}</a>',
                reverse(
                    "plugins:exhibition:public_list",
                    kwargs={
                        "organizer": sender.organizer.slug,
                        "event": sender.slug,
                    },
                ),
                "active"
                if "/exhibition/" in request.path_info and "/exhibition/call/" not in request.path_info
                else "",
                _("Exhibition"),
            )
        )

    settings = ExhibitorSettings.objects.filter(
        event=sender,
        call_enabled=True,
    ).first()
    if settings and (settings.call_is_open or not settings.call_hide_after_deadline):
        links.append(
            format_html(
                '<a href="{}" class="header-tab {}"><i class="fa fa-handshake-o"></i> {}</a>',
                reverse(
                    "plugins:exhibition:public_call",
                    kwargs={
                        "organizer": sender.organizer.slug,
                        "event": sender.slug,
                    },
                ),
                "active" if "/exhibition/call/" in request.path_info else "",
                _("Call for Exhibitors"),
            )
        )

    return format_html_join("", "{}", ((link,) for link in links))


@receiver(html_head, dispatch_uid="exhibition_front_page_supporters_styles")
def presale_supported_by_styles(sender, request=None, **kwargs):
    return format_html(
        '<link rel="stylesheet" type="text/css" href="{}">',
        static("exhibition/css/presale-supported-by.css"),
    )
