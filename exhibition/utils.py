from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from django.db.models import Q, QuerySet
from django.utils import timezone
from django_scopes import scope
from eventyay.base.models import TalkSlot
from eventyay.common.urls import get_url_origin
from eventyay.common.utils.language import localize_event_text
from eventyay.talk_rules.agenda import is_agenda_visible
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
        .prefetch_related("social_links")
        .order_by("exhibitor_position", "name", "pk")
    )


def public_exhibitor_sessions(exhibitor: "ExhibitorInfo", user) -> list[TalkSlot]:
    """Scheduled slots for an exhibitor's sessions that are live on the published schedule."""
    event = exhibitor.event
    with scope(event=event):
        if not is_agenda_visible(user, event):
            return []
        return list(
            TalkSlot.objects.filter(
                schedule=event.current_schedule,
                is_visible=True,
                submission__in=exhibitor.sessions.all(),
            )
            .select_related("submission", "submission__track", "room")
            .prefetch_related("submission__speakers")
            .order_by("start", "pk")
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


def create_exhibitor_from_proposal(proposal, requestor=None):
    from .models import (
        LOG_PARTNER_CREATED,
        LOG_PARTNER_REACTIVATED,
        ExhibitionProposalState,
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
        email=(proposal.email or "").strip() or (proposal.user.email if proposal.user_id else ""),
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


def event_voucher_settings(event):
    """Event-wide voucher defaults, without creating a settings row on a read path."""
    from .models import ExhibitorSettings

    return ExhibitorSettings.objects.filter(event=event).first() or ExhibitorSettings(event=event)


def resolve_voucher_defaults(exhibitor, *, event_settings=None):
    """Voucher settings for this exhibitor: their sponsor group's, or the event-wide default.

    Pass ``event_settings`` when resolving for many exhibitors to avoid a query per row.
    """
    source = (
        exhibitor.sponsor_group
        if exhibitor.sponsor_group_id
        else (event_settings or event_voucher_settings(exhibitor.event))
    )
    return {
        "product": source.voucher_default_product,
        "count": source.voucher_default_count,
        "price_mode": source.voucher_default_price_mode,
        "value": source.voucher_default_value,
    }


def generate_exhibitor_vouchers(exhibitor, *, product, count, price_mode, value):
    from eventyay.base.models import Voucher

    from .models import ExhibitorVoucher

    tag = f"exhibitor-{exhibitor.key}"
    links = []
    for _ in range(count):
        voucher = Voucher.objects.create(
            event=exhibitor.event,
            product=product,
            price_mode=price_mode,
            value=value,
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
    "logo",
    "logo_url",
    "header_image",
    "header_image_url",
)


def sync_exhibitor_from_proposal(proposal, requestor=None):
    """Push submitter-owned profile fields of an accepted proposal onto its partner profile."""
    from .models import LOG_PARTNER_SYNCED, ExhibitorSocialLink

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
    exhibitor.log_action(
        LOG_PARTNER_SYNCED,
        data={"proposal": proposal.code},
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
