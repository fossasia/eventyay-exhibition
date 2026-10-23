"""Email helpers for the exhibition plugin."""

import html
import json
import logging
import re
import uuid
from collections import defaultdict
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import gettext, gettext_lazy as _lazy, gettext_noop, ngettext, override
from eventyay.base.models import PriceModeChoices
from eventyay.base.services.mail import mail
from i18nfield.strings import LazyI18nString

logger = logging.getLogger(__name__)

REQUEST_NEW = "request_new"
REQUEST_ORGANIZER = "request_organizer"
REQUEST_ACCEPTED = "request_accepted"
REQUEST_REJECTED = "request_rejected"
EXHIBITOR_ACCESS = "exhibitor_access"
VOUCHERS = "vouchers"
EXHIBITOR_PROFILE = "exhibitor_profile"
EXHIBITOR_PROFILE_NEW_ACCOUNT = "exhibitor_profile_new_account"
REQUEST_MESSAGE = "request_message"

LIFECYCLE_ROLES = (
    REQUEST_NEW,
    REQUEST_ORGANIZER,
    REQUEST_ACCEPTED,
    REQUEST_REJECTED,
    EXHIBITOR_ACCESS,
    VOUCHERS,
    EXHIBITOR_PROFILE,
    EXHIBITOR_PROFILE_NEW_ACCOUNT,
)
TEMPLATE_ROLES = (*LIFECYCLE_ROLES, REQUEST_MESSAGE)

PLACEHOLDER_DOCS = (
    ("{event_name}", _lazy("The event's name")),
    ("{request_name}", _lazy("The request / organisation name")),
    ("{request_code}", _lazy("The request's unique code")),
    ("{request_url}", _lazy("Link for the applicant to view or edit the request")),
    ("{confirmation_url}", _lazy("Link for the applicant to confirm they will take part (accepted email only)")),
    ("{name}", _lazy("The applicant's name")),
    ("{exhibitor_name}", _lazy("The exhibitor / sponsor name (access email only)")),
    ("{booth_id}", _lazy("The exhibitor's booth ID (access email only)")),
    ("{login_email}", _lazy("The email address that opens the exhibitor's My Exhibitions page")),
    (
        "{invitation_link}",
        _lazy("Link to set a password for the account created for the exhibitor (new-account profile email only)"),
    ),
    ("{exhibitor_access_code}", _lazy("The exhibitor's secret access code (access email only)")),
    (
        "{device_tokens}",
        _lazy(
            "Setup URL, token and QR code for each lead-scanning device provisioned "
            "for this exhibitor (access email only)"
        ),
    ),
    ("{request_type}", _lazy("The request type, such as Exhibitor or Sponsor (organizer notification only)")),
    ("{request_action}", _lazy("The action taken on the request (organizer notification only)")),
    ("{contact_email}", _lazy("The applicant's contact email (organizer notification only)")),
    ("{request_date}", _lazy("The date and time of the request action (organizer notification only)")),
    ("{request_admin_url}", _lazy("The organizer backend URL for the request (organizer notification only)")),
    ("{pending_request_count}", _lazy("The current number of pending requests (organizer notification only)")),
    (
        "{my_exhibitions_url}",
        _lazy(
            "Link to My Exhibitions in the personal dashboard, where the exhibitor sees "
            "their profile, vouchers and redemptions (voucher and profile emails only)"
        ),
    ),
)

_SETTINGS_PREFIX = "exhibition_mail_"


def subject_settings_key(role):
    return f"{_SETTINGS_PREFIX}{role}_subject"


def body_settings_key(role):
    return f"{_SETTINGS_PREFIX}{role}_body"


DEFAULT_TEMPLATE_SOURCES = {
    REQUEST_NEW: (
        gettext_noop("We received your request for {event_name}"),
        gettext_noop(
            "Hello,\n\n"
            "thank you for submitting your request \u201c{request_name}\u201d to "
            "{event_name}. We have received it and will get back to you once it has "
            "been reviewed.\n\n"
            "You can review or edit your request here:\n{request_url}\n\n"
            "Best regards,\n"
            "The {event_name} team"
        ),
    ),
    REQUEST_ORGANIZER: (
        gettext_noop("New exhibition request: {request_name}"),
        gettext_noop(
            "Hello,\n\n"
            "A new {request_type} request from {request_name} has been {request_action}.\n\n"
            "Contact: {contact_name} ({contact_email})\n"
            "Date: {request_date}\n"
            "Review request: {request_admin_url}\n"
            "Pending requests: {pending_request_count}\n\n"
            "Best regards,\n"
            "The {event_name} team"
        ),
    ),
    REQUEST_ACCEPTED: (
        gettext_noop("Your request for {event_name} has been accepted"),
        gettext_noop(
            "Hello,\n\n"
            "we are happy to let you know that your request \u201c{request_name}\u201d "
            "for {event_name} has been accepted. Please confirm that you will take part:\n"
            "{confirmation_url}\n\n"
            "We will be in touch with the next steps.\n\n"
            "Best regards,\n"
            "The {event_name} team"
        ),
    ),
    REQUEST_REJECTED: (
        gettext_noop("Update on your request for {event_name}"),
        gettext_noop(
            "Hello,\n\n"
            "thank you for your interest in {event_name}. Unfortunately we are unable "
            "to accept your request \u201c{request_name}\u201d this time.\n\n"
            "We hope to see you at a future event.\n\n"
            "Best regards,\n"
            "The {event_name} team"
        ),
    ),
    EXHIBITOR_ACCESS: (
        gettext_noop("Lead Scanning Access for {event_name}"),
        gettext_noop(
            "Hello {exhibitor_name},\n\n"
            "Please use the information below to activate the **Lead Scanning app**:\n\n"
            "**Step 1 \u2014 Open the Web App:** access.eventyay.com\n\n"
            "**Step 2 \u2014 Enter the Exhibitor Key:** {exhibitor_access_code}\n\n"
            "**Step 3 \u2014 Set up each device** by scanning its QR code, or by entering its "
            "setup URL and token manually:\n\n"
            "{device_tokens}\n\n"
            "*Each token is unique to one device and can only be used once. "
            "If you set up additional devices, each device will require its own token.*\n\n"
            "Please share these details with the team members who will be scanning leads at the event.\n\n"
            "Best regards,\n"
            "The {event_name} Team"
        ),
    ),
    EXHIBITOR_PROFILE: (
        gettext_noop("You have been added as an exhibitor for {event_name}"),
        gettext_noop(
            "Dear {exhibitor_name},\n\n"
            "A profile for {exhibitor_name} has been created for {event_name}.\n\n"
            "You can edit your profile in My Exhibitions: {my_exhibitions_url}\n\n"
            "Log in with your account for {login_email} to open it. If you have not confirmed that "
            "address on your account yet, confirm it from your account's email settings first.\n\n"
            "If you have any questions, please don't hesitate to reach out.\n\n"
            "The {event_name} organisers"
        ),
    ),
    EXHIBITOR_PROFILE_NEW_ACCOUNT: (
        gettext_noop("You have been added as an exhibitor for {event_name}"),
        gettext_noop(
            "Dear {exhibitor_name},\n\n"
            "A profile for {exhibitor_name} has been created for {event_name}.\n\n"
            "An account has been created for you with {login_email} \u2013 please follow this link "
            "to set your account password:\n\n"
            "{invitation_link}\n\n"
            "Afterwards, you can edit your profile in My Exhibitions: {my_exhibitions_url}\n\n"
            "If you have any questions, please don't hesitate to reach out.\n\n"
            "The {event_name} organisers"
        ),
    ),
    VOUCHERS: (
        gettext_noop("Your vouchers for {event_name} — {exhibitor_name}"),
        gettext_noop(
            "Dear {exhibitor_name},\n\n"
            "Thank you for participating in {event_name}. Please share the voucher codes below with "
            "your audience — anyone who uses one gets credited to you as a lead.\n\n"
            "{voucher_list}\n\n"
            "They can redeem a code on the event ticket shop at checkout.\n\n"
            "You can see which codes have been redeemed, and download your vouchers, at any time in "
            "My Exhibitions: {my_exhibitions_url}\n\n"
            "To open it, log in or create an account with {login_email}. "
            "If you create a new account, confirm the address from the email we send you first.\n\n"
            "If you have any questions, please don't hesitate to reach out.\n\n"
            "Best regards,\n"
            "The {event_name} Team"
        ),
    ),
    REQUEST_MESSAGE: (
        gettext_noop("Your request for {event_name}"),
        gettext_noop(
            "Hello,\n\n"
            "thank you for submitting your request \u201c{request_name}\u201d to "
            "{event_name}. To continue reviewing it, we need some more information "
            "from you.\n\n"
            "You can review or edit your request here:\n{request_url}\n\n"
            "Best regards,\n"
            "The {event_name} team"
        ),
    ),
}

_SINGLE_NEWLINE_RE = re.compile(r"(?<!\n)\n(?!\n)")


def with_hard_line_breaks(text):
    """Turn single newlines into Markdown hard breaks so the editor keeps them on separate lines."""
    return _SINGLE_NEWLINE_RE.sub("  \n", text)


class _HardLineBreakGettextProxy(LazyI18nString.LazyGettextProxy):
    """Translate lazily like ``from_gettext`` and keep single newlines as line breaks."""

    def __getitem__(self, item):
        return with_hard_line_breaks(super().__getitem__(item))

    def __str__(self):
        return with_hard_line_breaks(super().__str__())


def _default_body(msgid):
    body = LazyI18nString({})
    body.data = _HardLineBreakGettextProxy(msgid)
    return body


DEFAULT_TEMPLATES = {
    role: (LazyI18nString.from_gettext(subject), _default_body(body))
    for role, (subject, body) in DEFAULT_TEMPLATE_SOURCES.items()
}


def default_template_initial(role, locales):
    """Default subject and body per locale, left blank where no translation exists."""
    source_language = django_settings.LANGUAGE_CODE.split("-")[0]
    initials = []
    for msgid in DEFAULT_TEMPLATE_SOURCES[role]:
        values = {}
        for code in locales:
            with override(code):
                translated = gettext(msgid)
            if translated != msgid or code.split("-")[0] == source_language:
                values[code] = with_hard_line_breaks(translated)
        initials.append(LazyI18nString(values))
    return tuple(initials)


def get_email_template(event, role):
    """Return ``(subject, body)`` for a role, falling back to the defaults."""
    default_subject, default_body = DEFAULT_TEMPLATES[role]
    subject = event.settings.get(subject_settings_key(role), as_type=LazyI18nString) or default_subject
    body = event.settings.get(body_settings_key(role), as_type=LazyI18nString) or default_body
    return subject, body


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


_PREVIEW_URL_RE = re.compile(r"^(https?://|www\.)[^\s]+$")

REQUEST_PLACEHOLDER_CONTEXT = ["event", "exhibition_request"]
EXHIBITOR_PLACEHOLDER_CONTEXT = ["event", "exhibitor"]

ROLE_PLACEHOLDER_CONTEXT = {
    REQUEST_NEW: REQUEST_PLACEHOLDER_CONTEXT,
    REQUEST_ORGANIZER: REQUEST_PLACEHOLDER_CONTEXT,
    REQUEST_ACCEPTED: REQUEST_PLACEHOLDER_CONTEXT,
    REQUEST_REJECTED: REQUEST_PLACEHOLDER_CONTEXT,
    EXHIBITOR_ACCESS: EXHIBITOR_PLACEHOLDER_CONTEXT,
    VOUCHERS: EXHIBITOR_PLACEHOLDER_CONTEXT,
    EXHIBITOR_PROFILE: EXHIBITOR_PLACEHOLDER_CONTEXT,
    EXHIBITOR_PROFILE_NEW_ACCOUNT: [*EXHIBITOR_PLACEHOLDER_CONTEXT, "invited_user"],
    REQUEST_MESSAGE: REQUEST_PLACEHOLDER_CONTEXT,
}


def placeholder_names(event, context):
    """Placeholder names that actually resolve for the given email context."""
    from eventyay.base.email import get_available_placeholders

    return sorted(get_available_placeholders(event, list(context)).keys())


def role_placeholder_names(event, role):
    """Placeholder names resolvable for a lifecycle role's own render context."""
    return placeholder_names(event, ROLE_PLACEHOLDER_CONTEXT[role])


def build_preview_placeholders(event, context=REQUEST_PLACEHOLDER_CONTEXT):
    """Sample placeholder values for previews, wrapped like the tickets preview."""
    from eventyay.base.email import get_available_placeholders
    from eventyay.base.templatetags.rich_text import is_placeholder_html_sample

    preview_context = {}
    title = html.escape(str(gettext("This value will be replaced based on dynamic parameters.")))
    for placeholder in get_available_placeholders(event, list(context)).values():
        sample = str(placeholder.render_sample(event)).strip()
        if sample.startswith("*") or is_placeholder_html_sample(sample):
            preview_context[placeholder.identifier] = sample
        elif _PREVIEW_URL_RE.match(sample):
            escaped = html.escape(sample)
            preview_context[placeholder.identifier] = (
                f'<a href="{escaped}" target="_blank" rel="noopener noreferrer">{escaped}</a>'
            )
        else:
            preview_context[placeholder.identifier] = (
                f'<span class="placeholder" title="{title}">{html.escape(sample)}</span>'
            )
    return _SafeDict(preview_context)


def recipient_locale(event, user=None):
    locale = getattr(user, "locale", None) if user else None
    return locale or event.settings.locale


def _render(text, context, locale):
    """Localise ``text`` and substitute ``{placeholder}`` values."""
    localized = str(LazyI18nString(text).localize(locale)) if locale else str(text)
    try:
        return localized.format_map(defaultdict(str, context))
    except (ValueError, IndexError):
        logger.warning("Could not render exhibition email template: %r", localized)
        return localized


def build_request_context(event, exhibition_request):
    from eventyay.base.email import get_email_context

    context = get_email_context(event=event, exhibition_request=exhibition_request)
    context.setdefault("event_name", str(event.name))
    return context


def build_exhibitor_context(event, exhibitor, **extra):
    from eventyay.base.email import get_email_context

    context = get_email_context(event=event, exhibitor=exhibitor, **extra)
    context.setdefault("event_name", str(event.name))
    return context


def request_public_url(exhibition_request):
    path = reverse(
        "plugins:exhibition:request.user_edit",
        kwargs={
            "organizer": exhibition_request.event.organizer.slug,
            "event": exhibition_request.event.slug,
            "code": exhibition_request.code,
        },
    )
    return urljoin(django_settings.SITE_URL, path)


def request_confirmation_url(exhibition_request):
    path = reverse(
        "plugins:exhibition:request.user_confirm",
        kwargs={
            "organizer": exhibition_request.event.organizer.slug,
            "event": exhibition_request.event.slug,
            "code": exhibition_request.code,
        },
    )
    return urljoin(django_settings.SITE_URL, path)


def request_admin_url(exhibition_request):
    path = reverse(
        "plugins:exhibition:request.detail",
        kwargs={
            "organizer": exhibition_request.event.organizer.slug,
            "event": exhibition_request.event.slug,
            "code": exhibition_request.code,
        },
    )
    return urljoin(django_settings.SITE_URL, path)


def device_setup_url():
    return django_settings.SITE_URL.rstrip("/")


def _render_device_block(name, setup_url, token):
    from eventyay.base.email import render_qr_code_img

    payload = json.dumps({"handshake_version": 1, "url": setup_url, "token": token})
    return (
        f"<p><strong>{escape(name)}</strong><br>"
        f"{escape(_lazy('Setup URL'))}: {escape(setup_url)}<br>"
        f"{escape(_lazy('Setup token'))}: <code>{escape(token)}</code><br>"
        f"{render_qr_code_img(payload, alt=str(_lazy('Device setup QR code')))}</p>"
    )


def devices_awaiting_setup(exhibitor):
    """Linked devices whose setup token has not been used yet."""
    from .models import ExhibitorDevice

    return ExhibitorDevice.objects.filter(exhibitor=exhibitor, device__initialized__isnull=True).select_related(
        "device"
    )


def render_device_tokens(exhibitor):
    """One setup URL, token and QR code per device that still needs setting up."""
    setup_url = device_setup_url()
    blocks = [
        _render_device_block(link.device.name, setup_url, link.device.initialization_token)
        for link in devices_awaiting_setup(exhibitor)
    ]
    return "".join(blocks)


def sample_device_tokens(event=None):
    return _render_device_block(str(_lazy("Acme Corp #1")), device_setup_url(), "SAMPLETOKEN123456")


def _voucher_applies_to_text(voucher):
    product_name = str(voucher.product) if voucher.product else str(_lazy("Any eligible product"))
    effect = ""
    if voucher.value is not None:
        if voucher.price_mode == PriceModeChoices.PERCENT:
            effect = f" — {voucher.value}% {_lazy('discount')}"
        elif voucher.price_mode == PriceModeChoices.SUBTRACT:
            effect = f" — {voucher.value} {_lazy('off')}"
        elif voucher.price_mode == PriceModeChoices.SET:
            effect = f" — {_lazy('price set to')} {voucher.value}"
    if voucher.max_usages > 1:
        effect = f"{effect} ({voucher.max_usages} {_lazy('uses')})"
    return f"{product_name}{effect}"


def _voucher_block_html(code, applies_to_text, redeem_url=None):
    block = (
        f"<p>{escape(str(_lazy('Voucher code')))}: <code>{escape(code)}</code><br>"
        f"{escape(str(_lazy('Applies to')))}: {escape(applies_to_text)}"
    )
    if redeem_url:
        block += f'<br>{escape(str(_lazy("Redeem")))}: <a href="{escape(redeem_url)}">{escape(redeem_url)}</a>'
    return f"{block}</p>"


VOUCHER_LIST_INLINE_LIMIT = 5


def _voucher_overflow_html(remaining):
    text = ngettext(
        "…and %(count)d more voucher code in the attached CSV file.",
        "…and %(count)d more voucher codes in the attached CSV file.",
        remaining,
    ) % {"count": remaining}
    return f"<p>{escape(text)}</p>"


def format_voucher_list(vouchers, event=None, *, limit=None):
    """One block per voucher: its code, what it applies to, and its redemption link.

    With ``limit``, only the first ``limit`` are listed and the rest are summarised; callers pass
    it only when the complete list travels with the mail as a CSV attachment.
    """
    from .utils import voucher_redeem_url

    vouchers = list(vouchers)
    shown = vouchers[:limit] if limit else vouchers
    blocks = "".join(
        _voucher_block_html(
            voucher.code,
            _voucher_applies_to_text(voucher),
            voucher_redeem_url(event or voucher.event, voucher),
        )
        for voucher in shown
    )
    remaining = len(vouchers) - len(shown)
    return f"{blocks}{_voucher_overflow_html(remaining)}" if remaining else blocks


def render_voucher_list(exhibitor):
    """All vouchers currently linked to this exhibitor, as a fallback when not overridden by the sender."""
    return format_voucher_list(exhibitor_vouchers(exhibitor), event=exhibitor.event)


def _sample_redeem_url(event, code):
    if event is None:
        return f"{django_settings.SITE_URL.rstrip('/')}/redeem?voucher={code}"
    from eventyay.multidomain.urlreverse import build_absolute_uri

    return f"{build_absolute_uri(event, 'presale:event.redeem')}?voucher={code}"


def my_exhibitions_url(exhibitor=None):
    return urljoin(django_settings.SITE_URL, reverse("plugins:exhibition:my_exhibitions"))


def invitation_url(invited_user):
    path = reverse("plugins:exhibition:my_exhibitions.invitation", kwargs={"token": invited_user.pw_reset_token})
    return urljoin(django_settings.SITE_URL, path)


def sample_invitation_url(event=None):
    path = reverse("plugins:exhibition:my_exhibitions.invitation", kwargs={"token": "SAMPLETOKEN123456"})
    return urljoin(django_settings.SITE_URL, path)


def sample_voucher_list(event=None):
    return _voucher_block_html(
        "ACME-3XKQ-7T2P",
        "Standard Public Ticket",
        _sample_redeem_url(event, "ACME-3XKQ-7T2P"),
    ) + _voucher_block_html(
        "ACME-9WFD-4M8N",
        "Standard Public Ticket — 20% discount",
        _sample_redeem_url(event, "ACME-9WFD-4M8N"),
    )


def request_recipient(exhibition_request):
    """Address an applicant is reached at: the request's contact email, else their login email."""
    to_email = (exhibition_request.email or "").strip() or (
        exhibition_request.user.email if exhibition_request.user_id else ""
    )
    return to_email.strip()


def queue_request_email(event, exhibition_request, role, *, send_now=False, requestor=None):
    """Queue a lifecycle email; ``send_now`` sends it instead of leaving it in the outbox."""
    from .models import ExhibitionEmailQueue

    to_email = request_recipient(exhibition_request)
    if not to_email:
        return None

    user = exhibition_request.user if exhibition_request.user_id else None
    locale = recipient_locale(event, user)
    subject_tpl, body_tpl = get_email_template(event, role)
    context = build_request_context(event, exhibition_request)

    queued = ExhibitionEmailQueue.objects.create(
        event=event,
        exhibition_request=exhibition_request,
        role=role,
        to_email=to_email,
        subject=_render(subject_tpl, context, locale),
        body=_render(body_tpl, context, locale),
        locale=locale or "",
    )
    if send_now:
        queued.send(requestor=requestor)
    return queued


def _collect_team_members(teams, members, seen):
    for team in teams:
        for member in team.members.all():
            email = (member.email or "").strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                members.append(member)


def send_request_organizer_emails(event, request, action):
    """Send a notification to organizers who can manage exhibition requests."""
    from .models import ExhibitionRequest, ExhibitionRequestState

    teams = event.teams.filter(
        Q(can_change_exhibition_proposals=True) | Q(is_exhibition_reviewer=True)
    ).prefetch_related("members")

    members = []
    seen = set()

    _collect_team_members(teams, members, seen)

    if not members:
        teams = event.teams.filter(can_change_event_settings=True).prefetch_related("members")

        _collect_team_members(teams, members, seen)

    if not members:
        return

    subject_tpl, body_tpl = get_email_template(event, REQUEST_ORGANIZER)

    context = build_request_context(event, request)
    context.update(
        {
            "contact_name": request.user.get_full_name() or request.user.email,
            "contact_email": request.email or request.user.email,
            "request_date": timezone.localtime(request.updated, ZoneInfo(event.timezone)).strftime("%Y-%m-%d %H:%M %Z"),
            "request_admin_url": request_admin_url(request),
            "pending_request_count": ExhibitionRequest.objects.filter(
                event=event,
                state=ExhibitionRequestState.SUBMITTED,
            ).count(),
        }
    )

    for member in members:
        locale = recipient_locale(event, member)
        with override(locale):
            member_context = context.copy()
            member_context.update(
                {
                    "request_type": gettext("Sponsor") if request.is_sponsor else gettext("Exhibitor"),
                    "request_action": gettext(action),
                }
            )
        try:
            mail(
                email=member.email.strip(),
                subject=_render(subject_tpl, member_context, locale),
                template=LazyI18nString(_render(body_tpl, member_context, locale)),
                context={},
                event=event,
                locale=locale or None,
                auto_email=False,
            )
        except Exception:
            logger.exception(
                "Failed to send organizer notification to member %s",
                member.pk,
            )


def compose_recipients(event, states=None, organization_type=None, sponsor_group=None):
    from .models import ExhibitionRequest, ExhibitionRequestState

    queryset = ExhibitionRequest.objects.filter(event=event).exclude(state=ExhibitionRequestState.DRAFT)
    if states:
        queryset = queryset.filter(state__in=states)
    if organization_type == "exhibitor":
        queryset = queryset.filter(is_exhibitor=True)
    elif organization_type == "sponsor":
        queryset = queryset.filter(is_sponsor=True)
    if sponsor_group is not None:
        queryset = queryset.filter(sponsor_group=sponsor_group)
    return queryset.select_related("user", "sponsor_group").order_by("-updated")


REQUEST_CHOICE_PREFIX = "request-"
PROFILE_CHOICE_PREFIX = "profile-"


def organizer_created_profiles(event):
    """Organization profiles an organizer added by hand, without an application behind them."""
    from .models import ExhibitorInfo

    return ExhibitorInfo.objects.filter(event=event, source_requests__isnull=True, email__isnull=False).exclude(
        email=""
    )


def resolve_compose_recipients(event, *, organizations=(), state="", organization_type="", sponsor_group=None):
    """Applications and profiles a composed email goes to: the chosen organizations, else everyone matching the filters.

    ``organizations`` holds the compose form's choice values, ``request-<pk>`` and ``profile-<pk>``.
    """
    from .models import ExhibitorInfo

    if organizations:

        def chosen(prefix):
            return [value[len(prefix) :] for value in organizations if value.startswith(prefix)]

        exhibition_requests = compose_recipients(event).filter(pk__in=chosen(REQUEST_CHOICE_PREFIX))
        profiles = organizer_created_profiles(event).filter(pk__in=chosen(PROFILE_CHOICE_PREFIX))
        return exhibition_requests, profiles
    exhibition_requests = compose_recipients(
        event,
        states=[state] if state else None,
        organization_type=organization_type,
        sponsor_group=sponsor_group,
    )
    return exhibition_requests, ExhibitorInfo.objects.none()


def compose_recipient_emails(exhibition_requests, exhibitors=()):
    """Distinct addresses a composed email reaches, deduplicated the way :func:`queue_compose_emails` does."""
    emails = {request_recipient(exhibition_request).lower() for exhibition_request in exhibition_requests}
    emails.update((exhibitor.recipient_email or "").strip().lower() for exhibitor in exhibitors)
    emails.discard("")
    return emails


def queue_compose_emails(
    event, exhibition_requests, subject, body, *, exhibitors=(), scheduled_at=None, send_now=False, requestor=None
):
    """Fan a composed message out into per-recipient queued rows sharing a batch."""
    from .models import ExhibitionEmailQueue

    batch = uuid.uuid4()
    created = []
    seen_emails = set()

    def queue(to_email, user, context, **link):
        to_email = to_email.strip()
        if not to_email or to_email.lower() in seen_emails:
            return
        seen_emails.add(to_email.lower())
        locale = recipient_locale(event, user)
        queued = ExhibitionEmailQueue.objects.create(
            event=event,
            batch=batch,
            to_email=to_email,
            subject=_render(subject, context, locale),
            body=_render(body, context, locale),
            locale=locale or "",
            scheduled_at=scheduled_at,
            **link,
        )
        if send_now:
            queued.send(requestor=requestor)
        created.append(queued)

    for exhibition_request in exhibition_requests:
        queue(
            request_recipient(exhibition_request),
            exhibition_request.user if exhibition_request.user_id else None,
            build_request_context(event, exhibition_request),
            exhibition_request=exhibition_request,
        )
    for exhibitor in exhibitors:
        context = build_exhibitor_context(event, exhibitor)
        if not context.get("request_name"):
            context["request_name"] = context.get("exhibitor_name") or str(exhibitor.name)
        queue(exhibitor.recipient_email, None, context, exhibitor=exhibitor)
    return created


def exhibitor_has_devices(exhibitor):
    """Whether any lead-scanning device is linked, and so the access email has tokens to carry."""
    from .models import ExhibitorDevice

    return ExhibitorDevice.objects.filter(exhibitor=exhibitor).exists()


def queue_exhibitor_access_email(event, exhibitor, *, requestor=None):
    """Queue the access-credentials email; ``None`` without a recipient or any device to set up."""
    from .models import ExhibitionEmailQueue

    to_email = exhibitor.recipient_email
    if not to_email:
        return None
    if not devices_awaiting_setup(exhibitor).exists():
        return None

    subject_tpl, body_tpl = get_email_template(event, EXHIBITOR_ACCESS)
    locale = recipient_locale(event)
    context = build_exhibitor_context(event, exhibitor)

    return ExhibitionEmailQueue.objects.create(
        event=event,
        exhibitor=exhibitor,
        role=EXHIBITOR_ACCESS,
        to_email=to_email,
        subject=_render(subject_tpl, context, locale),
        body=_render(body_tpl, context, locale),
        locale=locale or "",
    )


def queue_exhibitor_profile_email(event, exhibitor, *, invited_user=None, send_now=False, requestor=None):
    """Queue the invitation to edit a profile created for an exhibitor; ``None`` for sponsor-only profiles.

    With ``invited_user`` the new-account template is used, carrying that account's set-password link.
    """
    from .models import ExhibitionEmailQueue

    if not exhibitor.is_exhibitor:
        return None
    to_email = exhibitor.recipient_email
    if not to_email:
        return None

    role = EXHIBITOR_PROFILE_NEW_ACCOUNT if invited_user is not None else EXHIBITOR_PROFILE
    subject_tpl, body_tpl = get_email_template(event, role)
    locale = recipient_locale(event)
    extra = {"invited_user": invited_user} if invited_user is not None else {}
    context = build_exhibitor_context(event, exhibitor, **extra)

    queued = ExhibitionEmailQueue.objects.create(
        event=event,
        exhibitor=exhibitor,
        role=role,
        to_email=to_email,
        subject=_render(subject_tpl, context, locale),
        body=_render(body_tpl, context, locale),
        locale=locale or "",
    )
    if send_now:
        queued.send(requestor=requestor)
    return queued


def exhibitor_vouchers(exhibitor):
    """Every voucher currently linked to this exhibitor, in issue order."""
    from .models import ExhibitorVoucher

    links = ExhibitorVoucher.objects.filter(exhibitor=exhibitor).select_related("voucher", "voucher__product")
    return [link.voucher for link in links]


def organizations_with_voucher_email_in_outbox(exhibitors):
    from .models import ExhibitionEmailQueue

    return set(
        ExhibitionEmailQueue.objects.filter(
            exhibitor__in=[exhibitor.pk for exhibitor in exhibitors], role=VOUCHERS, sent_at__isnull=True
        ).values_list("exhibitor_id", flat=True)
    )


def unclaimed_vouchers(exhibitor, vouchers):
    from .utils import exhibitor_unredeemed_vouchers

    unused = set(exhibitor_unredeemed_vouchers(exhibitor).values_list("pk", flat=True))
    return [voucher for voucher in vouchers if voucher.pk in unused]


def queue_voucher_email(event, exhibitor, vouchers, *, send_now=False, requestor=None):
    """Queue the voucher email for one exhibitor; ``None`` if there is no recipient address."""
    from .models import ExhibitionEmailQueue, ExhibitorSettings
    from .utils import store_voucher_csv

    to_email = exhibitor.recipient_email
    if not to_email:
        return None

    subject_tpl, body_tpl = get_email_template(event, VOUCHERS)
    locale = recipient_locale(event)
    context = build_exhibitor_context(event, exhibitor)

    settings = ExhibitorSettings.objects.get_or_create(event=event)[0]
    attachment = store_voucher_csv(event, vouchers) if settings.voucher_attach_csv else None
    context["voucher_list"] = format_voucher_list(
        vouchers, event=event, limit=VOUCHER_LIST_INLINE_LIMIT if attachment else None
    )

    queued = ExhibitionEmailQueue.objects.create(
        event=event,
        exhibitor=exhibitor,
        role=VOUCHERS,
        to_email=to_email,
        subject=_render(subject_tpl, context, locale),
        body=_render(body_tpl, context, locale),
        locale=locale or "",
        attachment=attachment,
    )
    if send_now:
        queued.send(requestor=requestor)
    return queued


VOUCHER_SKIP_NO_EMAIL = "no_email"
VOUCHER_SKIP_NO_VOUCHERS = "no_vouchers"
VOUCHER_SKIP_POOL_EMPTY = "pool_empty"
VOUCHER_SKIP_ALL_CLAIMED = "all_claimed"
VOUCHER_SKIP_IN_OUTBOX = "in_outbox"


def claim_default_vouchers(exhibitor, *, event_settings=None):
    """Claim this exhibitor's share from their pool; empty when the count is 0 or the pool is short."""
    from .utils import claim_pool_vouchers, resolve_voucher_defaults

    defaults = resolve_voucher_defaults(exhibitor, event_settings=event_settings)
    return claim_pool_vouchers(exhibitor, defaults["count"], pool_tag=defaults["pool_tag"])


def queue_voucher_emails(event, exhibitors, *, requestor=None, issue_missing=False):
    """Queue one voucher email per exhibitor, reporting who was skipped and why.

    With ``issue_missing``, an exhibitor holding no vouchers gets a batch created from their
    defaults first, so a bulk send does not skip everyone who was never issued vouchers by hand.
    """
    from .utils import event_exhibitor_settings

    queued = []
    skipped = {
        VOUCHER_SKIP_NO_EMAIL: [],
        VOUCHER_SKIP_NO_VOUCHERS: [],
        VOUCHER_SKIP_POOL_EMPTY: [],
        VOUCHER_SKIP_ALL_CLAIMED: [],
        VOUCHER_SKIP_IN_OUTBOX: [],
    }
    event_settings = event_exhibitor_settings(event) if issue_missing else None
    for exhibitor in exhibitors:
        with transaction.atomic():
            email, _vouchers, reason = queue_unsent_voucher_email(
                event, exhibitor, requestor=requestor, issue_missing=issue_missing, event_settings=event_settings
            )
        if reason:
            skipped[reason].append(exhibitor)
        elif email is not None:
            queued.append(email)
    return queued, skipped


def queue_unsent_voucher_email(event, exhibitor, *, requestor=None, extra=0, issue_missing=False, event_settings=None):
    """Lock the organization and queue its unused codes; returns ``(email, vouchers, skip reason)``."""
    from .models import ExhibitorInfo, ExhibitorVoucher
    from .utils import claim_pool_vouchers, resolve_voucher_defaults

    ExhibitorInfo.objects.select_for_update().get(pk=exhibitor.pk)
    if not exhibitor.recipient_email:
        return None, [], VOUCHER_SKIP_NO_EMAIL
    if organizations_with_voucher_email_in_outbox([exhibitor]):
        return None, [], VOUCHER_SKIP_IN_OUTBOX
    if extra:
        pool_tag = resolve_voucher_defaults(exhibitor, event_settings=event_settings)["pool_tag"]
        if not claim_pool_vouchers(exhibitor, extra, pool_tag=pool_tag):
            return None, [], VOUCHER_SKIP_POOL_EMPTY
    list(ExhibitorVoucher.objects.filter(exhibitor=exhibitor).select_for_update().order_by("pk").values_list("pk"))
    vouchers = exhibitor_vouchers(exhibitor)
    if vouchers:
        vouchers = unclaimed_vouchers(exhibitor, vouchers)
        if not vouchers:
            return None, [], VOUCHER_SKIP_ALL_CLAIMED
    elif issue_missing:
        wanted = resolve_voucher_defaults(exhibitor, event_settings=event_settings)["count"]
        if claim_default_vouchers(exhibitor, event_settings=event_settings):
            vouchers = exhibitor_vouchers(exhibitor)
        elif wanted:
            return None, [], VOUCHER_SKIP_POOL_EMPTY
    if not vouchers:
        return None, [], VOUCHER_SKIP_NO_VOUCHERS
    return queue_voucher_email(event, exhibitor, vouchers, requestor=requestor), vouchers, None
