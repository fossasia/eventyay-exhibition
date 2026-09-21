from django.core.exceptions import FieldDoesNotExist
from django.db.models import Prefetch
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import escape, format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from eventyay.base.email import SimpleFunctionalMailTextPlaceholder
from eventyay.base.signals import (
    logentry_display,
    logentry_object_link,
    register_mail_placeholders,
)
from eventyay.common.signals import user_menu_items
from eventyay.common.utils.language import localize_event_text
from eventyay.control.signals import event_dashboard_components, nav_event_common
from eventyay.presale.signals import (
    front_page_after_content,
    header_nav_tabs,
    html_head,
)

from .mail import (
    proposal_public_url,
    render_device_tokens,
    render_voucher_list,
    sample_device_tokens,
    sample_voucher_list,
)
from .models import (
    LOG_CALL_SECRET_REGENERATED,
    LOG_CALL_SETTINGS_CHANGED,
    LOG_EMAIL_SENT,
    LOG_GROUP_ADDED,
    LOG_GROUP_CHANGED,
    LOG_GROUP_DELETED,
    LOG_PARTNER_ADDED,
    LOG_PARTNER_CHANGED,
    LOG_PARTNER_CREATED,
    LOG_PARTNER_DELETED,
    LOG_PARTNER_REACTIVATED,
    LOG_PARTNER_SYNCED,
    LOG_PREFIX,
    LOG_PROPOSAL_CHANGED,
    LOG_QUESTION_ADDED,
    LOG_QUESTION_CHANGED,
    LOG_QUESTION_DELETED,
    LOG_SETTINGS_CHANGED,
    PROPOSAL_LOG_ACTIONS,
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitorDevice,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    SponsorGroup,
    clear_dependencies_on,
    prune_dependency_option,
)
from .utils import add_external_image_csp_sources, public_exhibitors_queryset


def exhibition_access(event, request):
    """``(can_view_exhibitors, can_review)`` for the requesting user, both False when anonymous."""
    if not request or not request.user.is_authenticated:
        return False, False
    can_view_exhibitors = request.user.has_event_permission(
        event.organizer,
        event,
        ("can_change_event_settings", "can_view_orders"),
        request=request,
    )
    can_review = request.user.has_event_permission(
        event.organizer,
        event,
        ("can_change_exhibition_proposals", "is_exhibition_reviewer"),
        request=request,
    )
    return can_view_exhibitors, can_review


@receiver(nav_event_common, dispatch_uid="exhibition_nav_event_common")
def exhibition_nav_event_common(sender, request=None, **kwargs):
    can_view_exhibitors, can_review = exhibition_access(sender, request)
    if not can_view_exhibitors and not can_review:
        return []
    kwargs_url = {"organizer": sender.organizer.slug, "event": sender.slug}
    route = "plugins:exhibition:dashboard" if can_view_exhibitors else "plugins:exhibition:proposal.list"
    match = request.resolver_match
    return [
        {
            "label": _("Exhibition"),
            "url": reverse(route, kwargs=kwargs_url),
            "icon": "building-o",
            "active": bool(match and match.namespace == "plugins:exhibition"),
        }
    ]


@receiver(event_dashboard_components, dispatch_uid="exhibition_dashboard_component")
def exhibition_dashboard_component(sender, request=None, **kwargs):
    kwargs_url = {"organizer": sender.organizer.slug, "event": sender.slug}
    can_view_exhibitors, can_review = exhibition_access(sender, request)
    if can_review and not can_view_exhibitors:
        url = reverse("plugins:exhibition:proposal.list", kwargs=kwargs_url)
        description = _("Screen and evaluate exhibitor and sponsor requests for the event.")
        link_label = _("Request Review Dashboard")
    else:
        url = reverse("plugins:exhibition:dashboard", kwargs=kwargs_url)
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
        SimpleFunctionalMailTextPlaceholder(
            "device_tokens",
            ["exhibitor"],
            render_device_tokens,
            sample_device_tokens,
        ),
        SimpleFunctionalMailTextPlaceholder(
            "voucher_list",
            ["exhibitor"],
            render_voucher_list,
            sample_voucher_list,
        ),
    ]


@receiver(post_delete, sender=ExhibitionQuestionOption, dispatch_uid="exhibition_option_dependency_cleanup")
def exhibition_option_dependency_cleanup(sender, instance, **kwargs):
    """Keep dependency values in step with the answer options they point at."""
    prune_dependency_option(instance)


@receiver(pre_delete, sender=ExhibitionQuestion, dispatch_uid="exhibition_question_dependency_cleanup")
def exhibition_question_dependency_cleanup(sender, instance, **kwargs):
    """Leave no dependency pointing at a field that is going away."""
    clear_dependencies_on(instance)


@receiver(pre_delete, sender=ExhibitorInfo, dispatch_uid="exhibition_exhibitor_voucher_cleanup")
def delete_exhibitor_vouchers(sender, instance, **kwargs):
    for link in ExhibitorVoucher.objects.filter(exhibitor=instance, voucher__redeemed=0).select_related("voucher"):
        link.voucher.delete()


@receiver(pre_delete, sender=ExhibitorInfo, dispatch_uid="exhibition_exhibitor_device_cleanup")
def revoke_exhibitor_devices(sender, instance, **kwargs):
    for link in ExhibitorDevice.objects.filter(exhibitor=instance).select_related("device"):
        device = link.device
        device.revoked = True
        device.save(update_fields=["revoked"])


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


LOG_ENTRY_LABELS = {
    PROPOSAL_LOG_ACTIONS["approve"]: _("Exhibition request approved."),
    PROPOSAL_LOG_ACTIONS["reject"]: _("Exhibition request rejected."),
    PROPOSAL_LOG_ACTIONS["withdraw"]: _("Exhibition request withdrawn."),
    PROPOSAL_LOG_ACTIONS["reopen"]: _("Exhibition request reopened for review."),
    LOG_PROPOSAL_CHANGED: _("Exhibition request changed."),
    LOG_PARTNER_CREATED: _("Organization profile created from an approved request."),
    LOG_PARTNER_REACTIVATED: _("Organization profile reactivated after re-approval."),
    LOG_PARTNER_SYNCED: _("Organization profile updated from the submitter's changes."),
    LOG_PARTNER_ADDED: _("Organization profile created."),
    LOG_PARTNER_CHANGED: _("Organization profile changed."),
    LOG_PARTNER_DELETED: _("Organization profile deleted."),
    LOG_SETTINGS_CHANGED: _("Exhibition settings changed."),
    LOG_CALL_SETTINGS_CHANGED: _("Call for exhibitors settings changed."),
    LOG_CALL_SECRET_REGENERATED: _("Private call link regenerated."),
    LOG_GROUP_ADDED: _("Sponsor group created."),
    LOG_GROUP_CHANGED: _("Sponsor group changed."),
    LOG_GROUP_DELETED: _("Sponsor group deleted."),
    LOG_QUESTION_ADDED: _("Exhibitor form question created."),
    LOG_QUESTION_CHANGED: _("Exhibitor form question changed."),
    LOG_QUESTION_DELETED: _("Exhibitor form question deleted."),
    LOG_EMAIL_SENT: _("Email sent."),
}


def changed_field_labels(logentry):
    """Render the stored field names of a change entry using their model labels."""
    names = logentry.parsed_data.get("changed") or []
    if not names:
        return ""
    model = type(logentry.content_object) if logentry.content_object else None
    labels = []
    for name in names:
        label = name.replace("_", " ")
        if model is not None:
            try:
                label = str(model._meta.get_field(name).verbose_name)
            except (FieldDoesNotExist, AttributeError):
                pass
        labels.append(label)
    return _("Updated: {fields}.").format(fields=", ".join(labels))


def proposal_state_label(value):
    """Render a stored state slug with its translated label."""
    try:
        return ExhibitionProposalState(value).label
    except ValueError:
        return value


@receiver(signal=logentry_display, dispatch_uid="exhibition_logentry_display")
def exhibition_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith(LOG_PREFIX):
        return

    label = LOG_ENTRY_LABELS.get(logentry.action_type)
    if not label:
        return

    if logentry.action_type in PROPOSAL_LOG_ACTIONS.values():
        data = logentry.parsed_data
        if data.get("from") and data.get("to"):
            transition = _("State changed from {old} to {new}.").format(
                old=proposal_state_label(data["from"]),
                new=proposal_state_label(data["to"]),
            )
            return f"{label} {transition}"

    changed = changed_field_labels(logentry)
    if changed:
        return f"{label} {changed}"
    return label


@receiver(signal=logentry_object_link, dispatch_uid="exhibition_logentry_object_link")
def exhibition_logentry_object_link(sender, logentry, **kwargs):
    if not logentry.action_type.startswith(LOG_PREFIX):
        return

    target = logentry.content_object
    a_text = None
    a_map = None

    if isinstance(target, ExhibitionProposal):
        a_text = _("Exhibition request {val}")
        a_map = {
            "href": reverse(
                "plugins:exhibition:proposal.detail",
                kwargs={"organizer": sender.organizer.slug, "event": sender.slug, "code": target.code},
            ),
            "val": escape(localize_event_text(target.name) or str(target.name)),
        }
    elif isinstance(target, ExhibitorInfo):
        a_text = _("Organization profile {val}")
        a_map = {
            "href": reverse(
                "plugins:exhibition:edit",
                kwargs={"organizer": sender.organizer.slug, "event": sender.slug, "pk": target.pk},
            ),
            "val": escape(localize_event_text(target.name) or str(target.name)),
        }
    elif isinstance(target, ExhibitionQuestion):
        a_text = _("Exhibitor form question {val}")
        a_map = {
            "href": reverse(
                "plugins:exhibition:call.questions.edit",
                kwargs={"organizer": sender.organizer.slug, "event": sender.slug, "pk": target.pk},
            ),
            "val": escape(target.localized_question),
        }
    elif isinstance(target, SponsorGroup):
        return _("Sponsor group {val}").format(val=escape(target.localized_name))

    if a_text and a_map:
        a_map["val"] = '<a href="{href}">{val}</a>'.format_map(a_map)
        return a_text.format_map(a_map)
