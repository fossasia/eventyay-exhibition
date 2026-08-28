"""Email helpers for the exhibition plugin."""

import html
import json
import logging
import re
import uuid
from collections import defaultdict
from urllib.parse import urljoin

from django.conf import settings as django_settings
from django.urls import reverse
from django.utils.html import escape
from django.utils.translation import gettext, gettext_lazy as _lazy, gettext_noop, override
from i18nfield.strings import LazyI18nString

logger = logging.getLogger(__name__)

PROPOSAL_NEW = "proposal_new"
PROPOSAL_ACCEPTED = "proposal_accepted"
PROPOSAL_REJECTED = "proposal_rejected"
EXHIBITOR_ACCESS = "exhibitor_access"

LIFECYCLE_ROLES = (PROPOSAL_NEW, PROPOSAL_ACCEPTED, PROPOSAL_REJECTED, EXHIBITOR_ACCESS)

PLACEHOLDER_DOCS = (
    ("{event_name}", _lazy("The event's name")),
    ("{request_name}", _lazy("The request / organisation name")),
    ("{request_code}", _lazy("The request's unique code")),
    ("{request_url}", _lazy("Link for the applicant to view or edit the request")),
    ("{name}", _lazy("The applicant's name")),
    ("{exhibitor_name}", _lazy("The exhibitor / sponsor name (access email only)")),
    ("{booth_id}", _lazy("The exhibitor's booth ID (access email only)")),
    ("{exhibitor_access_code}", _lazy("The exhibitor's secret access code (access email only)")),
    (
        "{device_tokens}",
        _lazy(
            "Setup URL, token and QR code for each lead-scanning device provisioned "
            "for this exhibitor (access email only)"
        ),
    ),
)

_SETTINGS_PREFIX = "exhibition_mail_"


def subject_settings_key(role):
    return f"{_SETTINGS_PREFIX}{role}_subject"


def body_settings_key(role):
    return f"{_SETTINGS_PREFIX}{role}_body"


DEFAULT_TEMPLATE_SOURCES = {
    PROPOSAL_NEW: (
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
    PROPOSAL_ACCEPTED: (
        gettext_noop("Your request for {event_name} has been accepted"),
        gettext_noop(
            "Hello,\n\n"
            "we are happy to let you know that your request \u201c{request_name}\u201d "
            "for {event_name} has been accepted. We will be in touch with the next "
            "steps.\n\n"
            "Best regards,\n"
            "The {event_name} team"
        ),
    ),
    PROPOSAL_REJECTED: (
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
}

DEFAULT_TEMPLATES = {
    role: (LazyI18nString.from_gettext(subject), LazyI18nString.from_gettext(body))
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
                values[code] = translated
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

PROPOSAL_PLACEHOLDER_CONTEXT = ["event", "proposal"]
EXHIBITOR_PLACEHOLDER_CONTEXT = ["event", "exhibitor"]

ROLE_PLACEHOLDER_CONTEXT = {
    PROPOSAL_NEW: PROPOSAL_PLACEHOLDER_CONTEXT,
    PROPOSAL_ACCEPTED: PROPOSAL_PLACEHOLDER_CONTEXT,
    PROPOSAL_REJECTED: PROPOSAL_PLACEHOLDER_CONTEXT,
    EXHIBITOR_ACCESS: EXHIBITOR_PLACEHOLDER_CONTEXT,
}


def placeholder_names(event, context):
    """Placeholder names that actually resolve for the given email context."""
    from eventyay.base.email import get_available_placeholders

    return sorted(get_available_placeholders(event, list(context)).keys())


def role_placeholder_names(event, role):
    """Placeholder names resolvable for a lifecycle role's own render context."""
    return placeholder_names(event, ROLE_PLACEHOLDER_CONTEXT[role])


def build_preview_placeholders(event, context=PROPOSAL_PLACEHOLDER_CONTEXT):
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


def build_proposal_context(event, proposal):
    from eventyay.base.email import get_email_context

    context = get_email_context(event=event, proposal=proposal)
    context.setdefault("event_name", str(event.name))
    return context


def build_exhibitor_context(event, exhibitor):
    from eventyay.base.email import get_email_context

    context = get_email_context(event=event, exhibitor=exhibitor)
    context.setdefault("event_name", str(event.name))
    return context


def proposal_public_url(proposal):
    path = reverse(
        "plugins:exhibition:proposal.user_edit",
        kwargs={
            "organizer": proposal.event.organizer.slug,
            "event": proposal.event.slug,
            "code": proposal.code,
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


def render_device_tokens(exhibitor):
    """One setup URL, token and QR code per lead-scanning device linked to the exhibitor."""
    from .models import ExhibitorDevice

    setup_url = device_setup_url()
    blocks = [
        _render_device_block(link.device.name, setup_url, link.device.initialization_token)
        for link in ExhibitorDevice.objects.filter(exhibitor=exhibitor).select_related("device")
    ]
    return "".join(blocks)


def sample_device_tokens(event=None):
    return _render_device_block(str(_lazy("Acme Corp #1")), device_setup_url(), "SAMPLETOKEN123456")


def queue_proposal_email(event, proposal, role, *, send_now=False, requestor=None):
    """Queue a lifecycle email; ``send_now`` sends it instead of leaving it in the outbox."""
    from .models import ExhibitionEmailQueue

    to_email = (proposal.email or "").strip() or (proposal.user.email if proposal.user_id else "")
    if not to_email:
        return None

    user = proposal.user if proposal.user_id else None
    locale = recipient_locale(event, user)
    subject_tpl, body_tpl = get_email_template(event, role)
    context = build_proposal_context(event, proposal)

    queued = ExhibitionEmailQueue.objects.create(
        event=event,
        proposal=proposal,
        to_email=to_email,
        subject=_render(subject_tpl, context, locale),
        body=_render(body_tpl, context, locale),
        locale=locale or "",
    )
    if send_now:
        queued.send(requestor=requestor)
    return queued


def compose_recipients(event, states=None, partner_type=None, sponsor_group=None):
    from .models import ExhibitionProposal, ExhibitionProposalState

    queryset = ExhibitionProposal.objects.filter(event=event).exclude(state=ExhibitionProposalState.DRAFT)
    if states:
        queryset = queryset.filter(state__in=states)
    if partner_type == "exhibitor":
        queryset = queryset.filter(is_exhibitor=True)
    elif partner_type == "sponsor":
        queryset = queryset.filter(is_sponsor=True)
    if sponsor_group is not None:
        queryset = queryset.filter(sponsor_group=sponsor_group)
    return queryset.select_related("user", "sponsor_group").order_by("-updated")


def queue_compose_emails(event, proposals, subject, body, *, scheduled_at=None, send_now=False, requestor=None):
    """Fan a composed message out into per-recipient queued rows sharing a batch."""
    from .models import ExhibitionEmailQueue

    batch = uuid.uuid4()
    created = []
    seen_emails = set()
    for proposal in proposals:
        to_email = (proposal.email or "").strip() or (proposal.user.email if proposal.user_id else "")
        to_email = to_email.strip()
        if not to_email or to_email.lower() in seen_emails:
            continue
        seen_emails.add(to_email.lower())

        user = proposal.user if proposal.user_id else None
        locale = recipient_locale(event, user)
        context = build_proposal_context(event, proposal)

        queued = ExhibitionEmailQueue.objects.create(
            event=event,
            proposal=proposal,
            batch=batch,
            to_email=to_email,
            subject=_render(subject, context, locale),
            body=_render(body, context, locale),
            locale=locale or "",
            scheduled_at=scheduled_at,
        )
        if send_now:
            queued.send(requestor=requestor)
        created.append(queued)
    return created


def queue_exhibitor_access_email(event, exhibitor, *, requestor=None):
    """Queue the access-credentials email; ``None`` if the exhibitor has no email address."""
    from .models import ExhibitionEmailQueue

    to_email = (exhibitor.email or "").strip()
    if not to_email:
        return None

    subject_tpl, body_tpl = get_email_template(event, EXHIBITOR_ACCESS)
    locale = recipient_locale(event)
    context = build_exhibitor_context(event, exhibitor)

    return ExhibitionEmailQueue.objects.create(
        event=event,
        exhibitor=exhibitor,
        to_email=to_email,
        subject=_render(subject_tpl, context, locale),
        body=_render(body_tpl, context, locale),
        locale=locale or "",
    )
