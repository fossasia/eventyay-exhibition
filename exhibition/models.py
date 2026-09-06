import os
import secrets
import string

from django.conf import settings
from django.db import models
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.translation import gettext_lazy as _
from django_countries import Countries
from eventyay.base.models import Device, Event, PriceModeChoices, Product, Voucher
from eventyay.base.models.base import LoggedModel
from eventyay.common.utils.language import localize_event_text
from i18nfield.fields import I18nCharField, I18nTextField
from i18nfield.strings import LazyI18nString

from .social_links import SOCIAL_LINK_CHOICES, get_social_link_spec


def generate_key():
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def generate_call_secret():
    return secrets.token_urlsafe(24)


def generate_proposal_code():
    alphabet = string.ascii_uppercase + string.digits
    return get_random_string(length=12, allowed_chars=alphabet)


def generate_booth_id(event=None):
    import random
    import string

    # Generate a random booth_id if none exists
    characters = string.ascii_letters + string.digits
    while True:
        booth_id = "".join(random.choices(characters, k=8))  # 8-character random string
        queryset = ExhibitorInfo.objects.filter(booth_id=booth_id)
        if event is not None:
            queryset = queryset.filter(event=event)
        if not queryset.exists():
            return booth_id


def get_next_sponsor_group_level(event):
    if not event:
        return 1
    return (SponsorGroup.objects.filter(event=event).aggregate(max_level=Max("level")).get("max_level") or 0) + 1


def get_next_exhibitor_position(event):
    if not event:
        return 0
    max_position = (
        ExhibitorInfo.objects.filter(event=event, is_exhibitor=True)
        .aggregate(value=Max("exhibitor_position"))
        .get("value")
    )
    return (max_position if max_position is not None else -1) + 1


def get_next_sponsor_position(event, sponsor_group):
    if not event:
        return 0
    max_position = (
        ExhibitorInfo.objects.filter(event=event, is_sponsor=True, sponsor_group=sponsor_group)
        .aggregate(value=Max("sponsor_position"))
        .get("value")
    )
    return (max_position if max_position is not None else -1) + 1


def exhibitor_logo_path(instance, filename):
    name = instance.name
    if isinstance(name, LazyI18nString):
        event = getattr(instance, "event", None)
        locale = getattr(event, "locale", None) if event is not None else None
        name = name.localize(locale) if locale else str(name)
    return os.path.join("exhibitors", "logos", str(name), filename)


def exhibitor_header_image_path(instance, filename):
    name = instance.name
    if isinstance(name, LazyI18nString):
        event = getattr(instance, "event", None)
        locale = getattr(event, "locale", None) if event is not None else None
        name = name.localize(locale) if locale else str(name)
    return os.path.join("exhibitors", "headers", str(name), filename)


def proposal_file_path(instance, filename, file_type):
    code = instance.code or "new"
    return os.path.join("exhibition-proposals", str(code), file_type, filename)


def proposal_logo_path(instance, filename):
    return proposal_file_path(instance, filename, "logos")


def proposal_header_image_path(instance, filename):
    return proposal_file_path(instance, filename, "headers")


def exhibition_answer_path(instance, filename):
    code = instance.proposal.code or "new"
    return os.path.join("exhibition-proposals", str(code), "answers", str(instance.question_id), filename)


LOCKED_FIELD_NOTICE = _(
    "This field is required for the exhibitor profile to display on the public event page and cannot be removed."
)

LOGO_HELP_TEXT = _("PNG, JPG or SVG, up to 10 MB. A square image of at least 400 × 400 pixels works best.")

HEADER_IMAGE_HELP_TEXT = _("PNG, JPG or SVG, up to 10 MB. A wide image of at least 1200 × 400 pixels works best.")

PROPOSAL_DEFAULT_FIELDS = (
    {
        "key": "name",
        "label": _("Organization name"),
        "active": True,
        "required": True,
        "active_locked": True,
        "required_locked": True,
    },
    {"key": "description", "label": _("Organization description"), "active": True, "required": True},
    {"key": "url", "label": _("Organization website"), "active": True, "required": True},
    {
        "key": "logo",
        "label": _("Logo"),
        "help_text": LOGO_HELP_TEXT,
        "lock_notice": LOCKED_FIELD_NOTICE,
        "active": True,
        "required": True,
        "active_locked": True,
        "required_locked": True,
    },
    {
        "key": "header_image",
        "label": _("Header image"),
        "help_text": HEADER_IMAGE_HELP_TEXT,
        "lock_notice": LOCKED_FIELD_NOTICE,
        "active": True,
        "required": True,
        "active_locked": True,
        "required_locked": True,
    },
    {
        "key": "social_links",
        "label": _("Social media"),
        "active": True,
        "required": True,
    },
)


PROPOSAL_DEFAULT_FIELD_KEYS = tuple(field["key"] for field in PROPOSAL_DEFAULT_FIELDS)

PROPOSAL_FORMSET_FIELD_KEYS = ("social_links",)


def default_proposal_field_settings():
    return {
        field["key"]: {
            "active": field.get("active", True),
            "required": field.get("required", False),
            "position": index,
            "label": None,
            "help_text": None,
        }
        for index, field in enumerate(PROPOSAL_DEFAULT_FIELDS)
    }


def storable_proposal_field_settings(normalized):
    """Strip the derived keys added by normalization so only raw overrides are persisted."""
    return {
        key: {
            "active": value["active"],
            "required": value["required"],
            "position": value["position"],
            "label": value["custom_label"],
            "help_text": value["custom_help_text"],
        }
        for key, value in normalized.items()
    }


def get_default_proposal_field_definition(key):
    return next(field for field in PROPOSAL_DEFAULT_FIELDS if field["key"] == key)


def default_allowed_fields():
    return ["attendee_name", "attendee_email"]


class VoucherDefaultsMixin(models.Model):
    """Default voucher settings applied when issuing/sending vouchers without overriding them."""

    voucher_default_count = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Default number of vouchers"),
    )
    voucher_default_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Default ticket product"),
    )
    voucher_default_price_mode = models.CharField(
        max_length=20,
        choices=PriceModeChoices.choices,
        default=PriceModeChoices.NONE,
        verbose_name=_("Default price effect"),
    )
    voucher_default_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Default value"),
    )

    class Meta:
        abstract = True


class ExhibitorSettings(VoucherDefaultsMixin, LoggedModel):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    exhibitors_access_mail_subject = models.CharField(max_length=255)
    exhibitors_access_mail_body = models.TextField()
    voucher_attach_csv = models.BooleanField(
        default=True,
        verbose_name=_("Attach voucher list as CSV"),
        help_text=_("Adds a spreadsheet of the recipient's own voucher codes to the voucher email."),
    )
    allowed_fields = models.JSONField(default=default_allowed_fields)
    call_enabled = models.BooleanField(default=False)
    call_headline = I18nCharField(
        max_length=200,
        blank=True,
        verbose_name=_("Call headline"),
    )
    call_text = I18nTextField(
        blank=True,
        verbose_name=_("Call text"),
    )
    call_deadline = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Submission deadline"),
    )
    call_hide_after_deadline = models.BooleanField(default=False)
    call_private = models.BooleanField(default=False)
    call_secret = models.CharField(max_length=64, default=generate_call_secret)
    proposal_field_settings = models.JSONField(default=default_proposal_field_settings)

    def is_field_allowed(self, identifier):
        return identifier in (self.allowed_fields or [])

    @property
    def call_is_open(self):
        if not self.call_enabled:
            return False
        return not self.call_deadline or self.call_deadline >= timezone.now()

    def regenerate_call_secret(self, requestor=None):
        self.call_secret = generate_call_secret()
        self.save(update_fields=["call_secret"])
        self.log_action(LOG_CALL_SECRET_REGENERATED, user=requestor)

    @property
    def normalized_proposal_field_settings(self):
        stored_settings = self.proposal_field_settings or {}
        normalized = default_proposal_field_settings()
        for index, field in enumerate(PROPOSAL_DEFAULT_FIELDS):
            key = field["key"]
            stored_field = stored_settings.get(key, {})
            normalized[key]["active"] = bool(stored_field.get("active", normalized[key]["active"]))
            normalized[key]["required"] = bool(stored_field.get("required", normalized[key]["required"]))
            normalized[key]["position"] = stored_field.get("position", index)
            custom_label = (stored_field.get("label") or "").strip() or None
            custom_help_text = (stored_field.get("help_text") or "").strip() or None
            normalized[key]["custom_label"] = custom_label
            normalized[key]["custom_help_text"] = custom_help_text
            normalized[key]["label"] = custom_label or field["label"]
            normalized[key]["help_text"] = custom_help_text or field.get("help_text") or ""
            normalized[key]["default_label"] = field["label"]
            normalized[key]["default_help_text"] = field.get("help_text") or ""
            normalized[key]["lock_notice"] = field.get("lock_notice") or ""
            if field.get("active_locked"):
                normalized[key]["active"] = True
            if field.get("required_locked"):
                normalized[key]["required"] = True
            if field.get("supports_required") is False:
                normalized[key]["required"] = False
            if not normalized[key]["active"]:
                normalized[key]["required"] = False
        return normalized

    @property
    def ordered_proposal_field_keys(self):
        normalized = self.normalized_proposal_field_settings
        default_index = {key: i for i, key in enumerate(PROPOSAL_DEFAULT_FIELD_KEYS)}
        return sorted(
            PROPOSAL_DEFAULT_FIELD_KEYS,
            key=lambda key: (normalized[key]["position"], default_index[key]),
        )

    def proposal_field_is_active(self, key):
        return self.normalized_proposal_field_settings[key]["active"]

    def proposal_field_is_required(self, key):
        return self.normalized_proposal_field_settings[key]["required"]

    class Meta:
        unique_together = ("event",)


class SponsorGroup(VoucherDefaultsMixin, LoggedModel):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="sponsor_groups")
    name = I18nCharField(max_length=120, verbose_name=_("Group name"))
    level = models.PositiveIntegerField(default=1, db_index=True, verbose_name=_("Level"))
    show_on_front_page = models.BooleanField(
        default=False,
        verbose_name=_("Show this sponsor group on the front page."),
    )

    class Meta:
        ordering = ("level", "pk")

    @property
    def localized_name(self):
        return localize_event_text(self.name) or ""

    def __str__(self):
        return self.localized_name or str(self.name)


class ExhibitorInfo(LoggedModel):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    name = I18nCharField(max_length=190, verbose_name=_("Name"))
    description = I18nTextField(verbose_name=_("Description"), null=True, blank=True)
    url = models.URLField(verbose_name=_("URL"), null=True, blank=True)
    email = models.EmailField(verbose_name=_("Email"), null=True, blank=True)
    logo = models.ImageField(upload_to=exhibitor_logo_path, null=True, blank=True)
    logo_url = models.URLField(verbose_name=_("Logo URL"), null=True, blank=True)
    header_image = models.ImageField(upload_to=exhibitor_header_image_path, null=True, blank=True)
    header_image_url = models.URLField(verbose_name=_("Header image URL"), null=True, blank=True)
    key = models.CharField(
        max_length=8,
        default=generate_key,
    )
    is_sponsor = models.BooleanField(default=False)
    sponsor_group = models.ForeignKey(
        SponsorGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partners",
    )
    is_exhibitor = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    booth_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    booth_name = I18nCharField(
        max_length=100,
        verbose_name=_("Booth name"),
        blank=True,
    )
    lead_scanning_enabled = models.BooleanField(default=False)
    allow_voucher_access = models.BooleanField(default=False)
    allow_lead_access = models.BooleanField(default=False)
    lead_scanning_scope_by_device = models.BooleanField(default=False)
    exhibitor_position = models.IntegerField(default=0)
    sponsor_position = models.IntegerField(default=0)
    sessions = models.ManyToManyField(
        "base.Submission",
        blank=True,
        related_name="exhibitors",
        verbose_name=_("Related sessions"),
    )

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["event", "booth_id"],
                condition=Q(booth_id__isnull=False),
                name="exhibition_event_booth_id_uniq",
            ),
        ]

    def __str__(self):
        return str(self.name)

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.is_exhibitor and not self.exhibitor_position:
                self.exhibitor_position = get_next_exhibitor_position(self.event)
            if self.is_sponsor and not self.sponsor_position:
                self.sponsor_position = get_next_sponsor_position(self.event, self.sponsor_group)
        super().save(*args, **kwargs)

    @property
    def localized_booth_name(self):
        booth_name = self.booth_name
        if isinstance(booth_name, LazyI18nString):
            locale = getattr(self.event, "locale", None)
            booth_name = booth_name.localize(locale) if locale else str(booth_name)
        return booth_name or ""

    @property
    def visible_logo_url(self):
        if self.logo_url:
            return self.logo_url
        if self.logo:
            return self.logo.url
        return ""

    @property
    def visible_header_image_url(self):
        if self.header_image_url:
            return self.header_image_url
        if self.header_image:
            return self.header_image.url
        return ""


class ExhibitorSocialLink(models.Model):
    exhibitor = models.ForeignKey(ExhibitorInfo, on_delete=models.CASCADE, related_name="social_links")
    network = models.CharField(max_length=32, choices=SOCIAL_LINK_CHOICES)
    url = models.URLField(verbose_name=_("URL"))

    class Meta:
        ordering = ("network", "url")

    @property
    def spec(self):
        return get_social_link_spec(self.network)

    def __str__(self):
        return f"{self.get_network_display()}: {self.url}"


class ExhibitionProposalState(models.TextChoices):
    DRAFT = "draft", _("draft")
    SUBMITTED = "submitted", _("submitted")
    ACCEPTED = "accepted", _("accepted")
    REJECTED = "rejected", _("rejected")
    WITHDRAWN = "withdrawn", _("withdrawn")


PROPOSAL_STATE_TRANSITIONS = {
    ExhibitionProposalState.DRAFT: frozenset({ExhibitionProposalState.SUBMITTED}),
    ExhibitionProposalState.SUBMITTED: frozenset(
        {
            ExhibitionProposalState.ACCEPTED,
            ExhibitionProposalState.REJECTED,
            ExhibitionProposalState.WITHDRAWN,
        }
    ),
    ExhibitionProposalState.ACCEPTED: frozenset(
        {
            ExhibitionProposalState.SUBMITTED,
            ExhibitionProposalState.REJECTED,
            ExhibitionProposalState.WITHDRAWN,
        }
    ),
    ExhibitionProposalState.REJECTED: frozenset(
        {
            ExhibitionProposalState.SUBMITTED,
            ExhibitionProposalState.ACCEPTED,
        }
    ),
    ExhibitionProposalState.WITHDRAWN: frozenset({ExhibitionProposalState.SUBMITTED}),
}

PROPOSAL_REVIEW_ACTIONS = {
    "approve": ExhibitionProposalState.ACCEPTED,
    "reject": ExhibitionProposalState.REJECTED,
    "withdraw": ExhibitionProposalState.WITHDRAWN,
    "reopen": ExhibitionProposalState.SUBMITTED,
}

PROPOSAL_BULK_ACTIONS = ("approve", "reject")

LOG_PREFIX = "eventyay.plugins.exhibition"

PROPOSAL_LOG_ACTIONS = {
    "approve": f"{LOG_PREFIX}.proposal.approved",
    "reject": f"{LOG_PREFIX}.proposal.rejected",
    "withdraw": f"{LOG_PREFIX}.proposal.withdrawn",
    "reopen": f"{LOG_PREFIX}.proposal.reopened",
}

LOG_PROPOSAL_CHANGED = f"{LOG_PREFIX}.proposal.changed"
LOG_PARTNER_CREATED = f"{LOG_PREFIX}.partner.created"
LOG_PARTNER_REACTIVATED = f"{LOG_PREFIX}.partner.reactivated"
LOG_PARTNER_ADDED = f"{LOG_PREFIX}.partner.added"
LOG_PARTNER_CHANGED = f"{LOG_PREFIX}.partner.changed"
LOG_PARTNER_DELETED = f"{LOG_PREFIX}.partner.deleted"
LOG_PARTNER_SYNCED = f"{LOG_PREFIX}.partner.synced"
LOG_SETTINGS_CHANGED = f"{LOG_PREFIX}.settings.changed"
LOG_CALL_SETTINGS_CHANGED = f"{LOG_PREFIX}.call.settings.changed"
LOG_CALL_SECRET_REGENERATED = f"{LOG_PREFIX}.call.secret.regenerated"
LOG_GROUP_ADDED = f"{LOG_PREFIX}.sponsorgroup.added"
LOG_GROUP_CHANGED = f"{LOG_PREFIX}.sponsorgroup.changed"
LOG_GROUP_DELETED = f"{LOG_PREFIX}.sponsorgroup.deleted"
LOG_QUESTION_ADDED = f"{LOG_PREFIX}.question.added"
LOG_QUESTION_CHANGED = f"{LOG_PREFIX}.question.changed"
LOG_QUESTION_DELETED = f"{LOG_PREFIX}.question.deleted"
LOG_EMAIL_SENT = f"{LOG_PREFIX}.email.sent"

SUBMITTER_PROFILE_FIELD_LABELS = {
    "description": _("Organization Description"),
    "url": _("Organization Website"),
    "logo": _("Logo"),
    "header_image": _("Header Image"),
    "social_links": _("Social Media"),
}


class ExhibitionProposal(LoggedModel):
    code = models.CharField(
        max_length=12,
        unique=True,
        default=generate_proposal_code,
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="exhibition_proposals",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="exhibition_proposals",
    )
    approved_exhibitor = models.ForeignKey(
        ExhibitorInfo,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_proposals",
    )
    state = models.CharField(
        max_length=16,
        choices=ExhibitionProposalState.choices,
        default=ExhibitionProposalState.SUBMITTED,
        db_index=True,
    )
    name = I18nCharField(max_length=190, verbose_name=_("Name"))
    description = I18nTextField(verbose_name=_("Description"), null=True, blank=True)
    content_locale = models.CharField(
        max_length=32,
        default=settings.LANGUAGE_CODE,
        verbose_name=_("Language"),
    )
    url = models.URLField(verbose_name=_("URL"), null=True, blank=True)
    email = models.EmailField(verbose_name=_("Email"), null=True, blank=True)
    logo = models.ImageField(upload_to=proposal_logo_path, null=True, blank=True)
    logo_url = models.URLField(verbose_name=_("Logo URL"), null=True, blank=True)
    header_image = models.ImageField(upload_to=proposal_header_image_path, null=True, blank=True)
    header_image_url = models.URLField(verbose_name=_("Header image URL"), null=True, blank=True)
    is_sponsor = models.BooleanField(default=False)
    sponsor_group = models.ForeignKey(
        SponsorGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposals",
    )
    is_exhibitor = models.BooleanField(default=True)
    booth_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    booth_name = I18nCharField(
        max_length=100,
        verbose_name=_("Booth name"),
        blank=True,
    )
    review_notes = models.TextField(
        null=True,
        blank=True,
        verbose_name=_("Internal review notes"),
    )
    submitted = models.DateTimeField(null=True, blank=True)
    profile_edited_at = models.DateTimeField(null=True, blank=True)
    accepted_profile_snapshot = models.JSONField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated", "-created")

    def __str__(self):
        return str(self.name)

    @property
    def editable(self):
        return self.state in {
            ExhibitionProposalState.DRAFT,
            ExhibitionProposalState.SUBMITTED,
            ExhibitionProposalState.ACCEPTED,
        }

    def can_transition_to(self, target_state):
        return target_state in PROPOSAL_STATE_TRANSITIONS.get(self.state, frozenset())

    def available_review_actions(self):
        return [action for action, target in PROPOSAL_REVIEW_ACTIONS.items() if self.can_transition_to(target)]

    def available_bulk_actions(self):
        return [action for action in PROPOSAL_BULK_ACTIONS if self.can_transition_to(PROPOSAL_REVIEW_ACTIONS[action])]

    def set_partner_active(self, active, requestor=None):
        if self.approved_exhibitor_id and self.approved_exhibitor.active != active:
            self.approved_exhibitor.active = active
            self.approved_exhibitor.save(update_fields=["active"])
            self.approved_exhibitor.log_action(
                LOG_PARTNER_CHANGED,
                data={"active": active, "reason": "proposal_state_change", "proposal": self.code},
                user=requestor,
            )

    def log_transition(self, action, previous, requestor=None):
        """Record who moved the request between states, and in which direction."""
        self.log_action(
            PROPOSAL_LOG_ACTIONS[action],
            data={"from": previous, "to": self.state, "code": self.code},
            user=requestor,
        )

    def approve(self, requestor=None):
        """Accept the request, create or reactivate its partner profile and queue the acceptance email."""
        from .mail import PROPOSAL_ACCEPTED, queue_proposal_email
        from .utils import create_exhibitor_from_proposal

        previous = self.state
        exhibitor = create_exhibitor_from_proposal(self, requestor=requestor)
        self.log_transition("approve", previous, requestor=requestor)
        queue_proposal_email(self.event, self, PROPOSAL_ACCEPTED, requestor=requestor)
        return exhibitor

    def reject(self, requestor=None):
        """Reject the request, hide any partner profile and queue the rejection email."""
        from .mail import PROPOSAL_REJECTED, queue_proposal_email

        previous = self.state
        self.state = ExhibitionProposalState.REJECTED
        self.save(update_fields=["state", "updated"])
        self.set_partner_active(False, requestor=requestor)
        self.log_transition("reject", previous, requestor=requestor)
        queue_proposal_email(self.event, self, PROPOSAL_REJECTED, requestor=requestor)

    @property
    def can_be_withdrawn(self):
        return self.can_transition_to(ExhibitionProposalState.WITHDRAWN)

    @property
    def can_be_reinstated(self):
        return self.state == ExhibitionProposalState.WITHDRAWN

    def withdraw(self, requestor=None):
        previous = self.state
        self.state = ExhibitionProposalState.WITHDRAWN
        self.save(update_fields=["state", "updated"])
        self.set_partner_active(False, requestor=requestor)
        self.log_transition("withdraw", previous, requestor=requestor)

    def reopen(self, requestor=None):
        """Move the request back to submitted for a fresh decision; sends no decision email."""
        previous = self.state
        self.state = ExhibitionProposalState.SUBMITTED
        self.submitted = self.submitted or timezone.now()
        self.save(update_fields=["state", "submitted", "updated"])
        self.set_partner_active(False, requestor=requestor)
        self.log_transition("reopen", previous, requestor=requestor)

    @property
    def requires_open_call_to_edit(self):
        return self.state != ExhibitionProposalState.ACCEPTED

    @property
    def edited_after_acceptance(self):
        return self.state == ExhibitionProposalState.ACCEPTED and self.profile_edited_at is not None

    def submitter_profile_values(self):
        """Serialise the submitter-owned profile fields into a comparable {key: text} mapping."""
        values = {
            "description": localize_event_text(self.description) or "",
            "url": self.url or "",
            "logo": self.visible_logo_url,
            "header_image": self.visible_header_image_url,
            "social_links": "\n".join(f"{link.get_network_display()}: {link.url}" for link in self.social_links.all()),
        }
        for answer in self.answers.all():
            values[f"answer_{answer.question_id}"] = str(answer.answer_string)
        return {key: str(value) for key, value in values.items()}

    def capture_profile_snapshot(self):
        self.accepted_profile_snapshot = self.submitter_profile_values()

    def profile_field_changes(self):
        """Return the field-level diff between the acceptance snapshot and the current profile."""
        if not self.accepted_profile_snapshot:
            return []
        old_values = self.accepted_profile_snapshot
        new_values = self.submitter_profile_values()
        all_keys = set(old_values) | set(new_values)
        answer_labels = self._answer_field_labels(all_keys)
        base_keys = list(SUBMITTER_PROFILE_FIELD_LABELS)
        answer_keys = sorted(
            (key for key in all_keys if key.startswith("answer_")),
            key=lambda key: int(key.removeprefix("answer_")),
        )
        other_keys = sorted(all_keys - set(base_keys) - set(answer_keys))
        ordered_keys = base_keys + answer_keys + other_keys
        changes = []
        for key in ordered_keys:
            old = old_values.get(key, "")
            new = new_values.get(key, "")
            if old == new:
                continue
            changes.append(
                {
                    "label": SUBMITTER_PROFILE_FIELD_LABELS.get(key) or answer_labels.get(key) or key,
                    "old": old,
                    "new": new,
                }
            )
        return changes

    def _answer_field_labels(self, keys):
        answer_ids = {
            key.removeprefix("answer_")
            for key in keys
            if key.startswith("answer_") and key.removeprefix("answer_").isdigit()
        }
        if not answer_ids:
            return {}
        questions = ExhibitionQuestion.objects.filter(id__in=answer_ids)
        return {f"answer_{question.id}": question.localized_question for question in questions}

    @property
    def content_locale_display(self):
        names = dict(getattr(self.event, "named_content_locales", None) or [])
        return names.get(self.content_locale, self.content_locale)

    @property
    def localized_booth_name(self):
        booth_name = self.booth_name
        if isinstance(booth_name, LazyI18nString):
            locale = getattr(self.event, "locale", None)
            booth_name = booth_name.localize(locale) if locale else str(booth_name)
        return booth_name or ""

    @property
    def visible_logo_url(self):
        if self.logo_url:
            return self.logo_url
        if self.logo:
            return self.logo.url
        return ""

    @property
    def visible_header_image_url(self):
        if self.header_image_url:
            return self.header_image_url
        if self.header_image:
            return self.header_image.url
        return ""


class ExhibitionProposalSocialLink(models.Model):
    proposal = models.ForeignKey(ExhibitionProposal, on_delete=models.CASCADE, related_name="social_links")
    network = models.CharField(max_length=32, choices=SOCIAL_LINK_CHOICES)
    url = models.URLField(verbose_name=_("URL"))

    class Meta:
        ordering = ("network", "url")

    @property
    def spec(self):
        return get_social_link_spec(self.network)

    def __str__(self):
        return f"{self.get_network_display()}: {self.url}"


class ExhibitionQuestionVariant(models.TextChoices):
    NUMBER = "number", _("Number")
    STRING = "string", _("Text (one line)")
    TEXT = "text", _("Multiline text")
    URL = "url", _("URL")
    EMAIL = "email", _("Email address")
    BOOLEAN = "boolean", _("Confirm Checkbox")
    CHOICES = "choices", _("Radio button (Choose one option)")
    SELECT = "select", _("Dropdown (Choose one option)")
    MULTIPLE = "multiple_choice", _("Checkbox (Choose one or several options)")
    FILE = "file", _("File upload")
    DATE = "date", _("Date")
    TIME = "time", _("Time")
    DATETIME = "datetime", _("Date and time")
    COUNTRY = "country", _("Country code (ISO 3166-1 alpha-2)")
    PHONE = "phone", _("Phone number")


QUESTION_OPTION_VARIANTS = frozenset(
    {
        ExhibitionQuestionVariant.CHOICES,
        ExhibitionQuestionVariant.MULTIPLE,
        ExhibitionQuestionVariant.SELECT,
    }
)


class ExhibitionQuestion(LoggedModel):
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="exhibition_questions",
    )
    variant = models.CharField(
        max_length=32,
        choices=ExhibitionQuestionVariant.choices,
        default=ExhibitionQuestionVariant.STRING,
    )
    question = I18nCharField(max_length=800, verbose_name=_("Custom question"))
    help_text = I18nCharField(
        null=True,
        blank=True,
        max_length=800,
        verbose_name=_("help text"),
    )
    required = models.BooleanField(default=False, verbose_name=_("required"))
    active = models.BooleanField(default=True, verbose_name=_("active"))
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ("position", "id")

    @property
    def localized_question(self):
        return localize_event_text(self.question) or ""

    def __str__(self):
        return self.localized_question or str(self.question)


class ExhibitionQuestionOption(models.Model):
    question = models.ForeignKey(
        ExhibitionQuestion,
        on_delete=models.CASCADE,
        related_name="options",
    )
    answer = I18nCharField(verbose_name=_("Response"))
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return localize_event_text(self.answer) or str(self.answer)


class ExhibitionAnswer(models.Model):
    question = models.ForeignKey(
        ExhibitionQuestion,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    proposal = models.ForeignKey(
        ExhibitionProposal,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    answer = models.TextField(blank=True)
    file = models.FileField(upload_to=exhibition_answer_path, null=True, blank=True)
    options = models.ManyToManyField(ExhibitionQuestionOption, related_name="answers")

    class Meta:
        unique_together = ("question", "proposal")

    @property
    def answer_string(self):
        if self.question.variant == ExhibitionQuestionVariant.BOOLEAN:
            if self.answer == "True":
                return _("Yes")
            if self.answer == "False":
                return _("No")
            return ""
        if self.question.variant in QUESTION_OPTION_VARIANTS:
            return ", ".join(str(option) for option in self.options.all())
        if self.question.variant == ExhibitionQuestionVariant.FILE:
            return os.path.basename(self.file.name) if self.file else ""
        if self.question.variant == ExhibitionQuestionVariant.COUNTRY:
            return Countries().name(self.answer) or self.answer or ""
        return self.answer or ""

    @property
    def file_url(self):
        return self.file.url if self.file else ""


class Lead(models.Model):
    exhibitor = models.ForeignKey(ExhibitorInfo, on_delete=models.CASCADE)
    exhibitor_name = models.CharField(max_length=190)
    pseudonymization_id = models.CharField(max_length=190)
    scanned = models.DateTimeField()
    scan_type = models.CharField(max_length=50)
    device_name = models.CharField(max_length=50)
    attendee = models.JSONField(null=True, blank=True)
    booth_id = models.CharField(max_length=100, editable=True)
    booth_name = models.CharField(
        max_length=100,
        verbose_name=_("Booth name"),
    )

    def __str__(self):
        return f"Lead scanned by {self.exhibitor.name}"


class ExhibitorTag(models.Model):
    exhibitor = models.ForeignKey(ExhibitorInfo, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=50)
    use_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("exhibitor", "name")
        ordering = ["-use_count", "name"]

    def __str__(self):
        return f"{self.name} ({self.exhibitor.name})"


class ExhibitorVoucher(models.Model):
    exhibitor = models.ForeignKey(ExhibitorInfo, on_delete=models.CASCADE, related_name="vouchers")
    voucher = models.OneToOneField(Voucher, on_delete=models.CASCADE, related_name="exhibitor_link")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created",)

    def __str__(self):
        return f"{self.voucher.code} ({self.exhibitor.name})"


class ExhibitorDevice(models.Model):
    exhibitor = models.ForeignKey(ExhibitorInfo, on_delete=models.CASCADE, related_name="devices")
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="exhibitor_link")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created", "pk")

    def __str__(self):
        return f"{self.device.name} ({self.exhibitor.name})"


class ExhibitionEmailQueue(LoggedModel):
    """A single email queued for one recipient, with placeholders already rendered."""

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="exhibition_email_queue",
    )
    proposal = models.ForeignKey(
        ExhibitionProposal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails",
    )
    exhibitor = models.ForeignKey(
        ExhibitorInfo,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails",
    )
    batch = models.UUIDField(null=True, blank=True, db_index=True)
    role = models.CharField(max_length=40, blank=True, default="", db_index=True)
    to_email = models.EmailField(verbose_name=_("Recipient"))
    subject = models.CharField(max_length=255, verbose_name=_("Subject"))
    body = models.TextField(verbose_name=_("Body"))
    reply_to = models.CharField(max_length=255, blank=True, default="")
    locale = models.CharField(max_length=32, blank=True, default="")
    attachment = models.ForeignKey(
        "base.CachedFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Queued email")
        verbose_name_plural = _("Queued emails")
        ordering = ("-created",)

    def __str__(self):
        state = "sent" if self.sent_at else "pending"
        return f"{self.to_email} · {state} · {self.subject}"

    def send(self, requestor=None):
        """Deliver the email via the core mail() service and mark it sent."""
        from eventyay.base.services.mail import mail

        if self.sent_at:
            return

        mail(
            email=self.to_email,
            subject=self.subject,
            template=LazyI18nString(self.body),
            context={},
            event=self.event,
            locale=self.locale or None,
            auto_email=False,
            event_reply_to=self.reply_to or None,
            attach_cached_files=[self.attachment_id] if self.attachment_id else None,
        )
        self.sent_at = timezone.now()
        self.scheduled_at = None
        self.save(update_fields=["sent_at", "scheduled_at", "updated"])
        self.log_action(
            LOG_EMAIL_SENT,
            user=requestor,
            data={"to": self.to_email, "subject": self.subject},
        )

    send.alters_data = True


class ExhibitionCustomEmailTemplate(models.Model):
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="exhibition_custom_email_templates",
    )
    name = models.CharField(max_length=190, verbose_name=_("Template name"))
    subject = I18nCharField(max_length=255, verbose_name=_("Subject"))
    body = I18nTextField(verbose_name=_("Body"), blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Custom email template")
        verbose_name_plural = _("Custom email templates")
        ordering = ("name",)

    def __str__(self):
        return self.name
