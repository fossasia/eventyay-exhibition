from django.db.models import Prefetch
from django.dispatch import receiver
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from eventyay.base.email import SimpleFunctionalMailTextPlaceholder
from eventyay.base.signals import register_mail_placeholders
from eventyay.common.signals import user_menu_items
from eventyay.common.utils.language import localize_event_text
from eventyay.control.signals import event_dashboard_components
from eventyay.presale.signals import (
    front_page_after_content,
    header_nav_tabs,
    html_head,
)

from .mail import proposal_public_url
from .models import ExhibitionProposal, ExhibitorInfo, ExhibitorSettings, SponsorGroup
from .utils import add_external_image_csp_sources, public_exhibitors_queryset


@receiver(event_dashboard_components, dispatch_uid="exhibition_dashboard_component")
def exhibition_dashboard_component(sender, request=None, **kwargs):
    kwargs_url = {"organizer": sender.organizer.slug, "event": sender.slug}
    can_view_exhibitors = request and request.user.has_event_permission(
        sender.organizer,
        sender,
        ("can_change_event_settings", "can_view_orders"),
        request=request,
    )
    can_review = request and request.user.has_event_permission(
        sender.organizer,
        sender,
        ("can_change_exhibition_proposals", "is_exhibition_reviewer"),
        request=request,
    )
    if can_review and not can_view_exhibitors:
        url = reverse("plugins:exhibition:proposal.list", kwargs=kwargs_url)
        description = _("Screen and evaluate exhibitor and sponsor requests for the event.")
        link_label = _("Request Review Dashboard")
    else:
        url = reverse("plugins:exhibition:info", kwargs=kwargs_url)
        description = _(
            "Manage exhibitors and sponsors, maintain booth details, and create partner profiles for the event."
        )
        link_label = _("Exhibitors & Sponsors Dashboard")
    return format_html(
        '<div class="panel panel-default widget-container widget-small no-padding last-column">'
        '<div class="panel-heading"><h3 class="panel-title">{}</h3></div>'
        '<div class="panel-body"><p>{}</p><p>{} <a href="{}">{}</a></p></div>'
        "</div>",
        str(_("Exhibitors & Sponsors")),
        str(description),
        str(_("Go to")),
        url,
        str(link_label),
    )


@receiver(front_page_after_content, dispatch_uid="exhibition_front_page_supporters")
def presale_supported_by(sender, request=None, **kwargs):
    sponsor_groups = list(
        SponsorGroup.objects.filter(event=sender, show_on_front_page=True).prefetch_related(
            Prefetch(
                "partners",
                queryset=ExhibitorInfo.objects.filter(event=sender, is_sponsor=True, active=True).order_by(
                    "sponsor_position", "name"
                ),
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
    if settings and not settings.call_private and (settings.call_is_open or not settings.call_hide_after_deadline):
        call_label = localize_event_text(settings.call_headline) or _("Call for Exhibitors")
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
                call_label,
            )
        )

    return format_html_join("", "{}", ((link,) for link in links))


@receiver(html_head, dispatch_uid="exhibition_front_page_supporters_styles")
def presale_supported_by_styles(sender, request=None, **kwargs):
    return format_html(
        '<link rel="stylesheet" type="text/css" href="{}">',
        static("exhibition/css/presale-supported-by.css"),
    )


@receiver(register_mail_placeholders, dispatch_uid="exhibition_mail_placeholders")
def exhibition_mail_placeholders(sender, **kwargs):
    """Placeholders available in exhibition emails."""
    return [
        SimpleFunctionalMailTextPlaceholder(
            "event_name",
            ["event"],
            lambda event: str(event.name),
            lambda event: str(event.name),
        ),
        SimpleFunctionalMailTextPlaceholder(
            "request_name",
            ["proposal"],
            lambda proposal: localize_event_text(proposal.name) or str(proposal.name),
            _("Acme Corp"),
        ),
        SimpleFunctionalMailTextPlaceholder(
            "request_code",
            ["proposal"],
            lambda proposal: proposal.code,
            "ABCD1234EFGH",
        ),
        SimpleFunctionalMailTextPlaceholder(
            "request_url",
            ["proposal"],
            proposal_public_url,
            "https://example.com/orga/event/exhibition/call/proposals/ABCD1234EFGH/",
        ),
        SimpleFunctionalMailTextPlaceholder(
            "name",
            ["proposal"],
            lambda proposal: (proposal.user.get_display_name() if proposal.user_id else "") or "",
            _("Jane Doe"),
        ),
        SimpleFunctionalMailTextPlaceholder(
            "exhibitor_name",
            ["exhibitor"],
            lambda exhibitor: localize_event_text(exhibitor.name) or str(exhibitor.name),
            _("Acme Corp"),
        ),
        SimpleFunctionalMailTextPlaceholder(
            "booth_id",
            ["exhibitor"],
            lambda exhibitor: exhibitor.booth_id or "",
            "A-12",
        ),
        SimpleFunctionalMailTextPlaceholder(
            "access_key",
            ["exhibitor"],
            lambda exhibitor: exhibitor.key or "",
            "a1b2c3d4",
        ),
        SimpleFunctionalMailTextPlaceholder(
            "exhibitor_access_code",
            ["exhibitor"],
            lambda exhibitor: exhibitor.key or "",
            "a1b2c3d4",
        ),
    ]


@receiver(user_menu_items, dispatch_uid="exhibition_user_menu_item")
def exhibition_user_menu_item(sender, request=None, icon_class="", **kwargs):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return ""

    if not ExhibitionProposal.objects.filter(event=sender, user=user).exists():
        return ""

    return format_html(
        '<a href="{}" class="dropdown-item" role="menuitem" tabindex="-1"><i class="fa fa-handshake-o {}"></i> {}</a>',
        reverse(
            "plugins:exhibition:proposal.user_list",
            kwargs={
                "organizer": sender.organizer.slug,
                "event": sender.slug,
            },
        ),
        icon_class,
        _("Exhibition requests"),
    )
