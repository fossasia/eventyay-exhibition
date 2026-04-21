import os
import secrets
import string

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from eventyay.base.models import Event
from eventyay.common.utils.language import localize_event_text
from i18nfield.fields import I18nCharField, I18nTextField
from i18nfield.strings import LazyI18nString

from .social_links import SOCIAL_LINK_CHOICES, get_social_link_spec


def generate_key():
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


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


def exhibitor_slides_path(instance, filename):
    name = instance.name
    if isinstance(name, LazyI18nString):
        event = getattr(instance, "event", None)
        locale = getattr(event, "locale", None) if event is not None else None
        name = name.localize(locale) if locale else str(name)
    return os.path.join("exhibitors", "slides", str(name), filename)


class ExhibitorSettings(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    exhibitors_access_mail_subject = models.CharField(max_length=255)
    exhibitors_access_mail_body = models.TextField()
    allowed_fields = models.JSONField(default=list)

    @property
    def all_allowed_fields(self):
        """Return all allowed fields, including required default fields"""
        default_fields = ["attendee_name", "attendee_email"]
        return list(set(default_fields + self.allowed_fields))

    class Meta:
        unique_together = ("event",)


class SponsorGroup(models.Model):
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="sponsor_groups"
    )
    name = I18nCharField(max_length=120, verbose_name=_("Group Name"))
    show_on_front_page = models.BooleanField(
        default=False,
        verbose_name=_("Show this sponsor group on the front page."),
    )

    class Meta:
        ordering = ("pk",)

    @property
    def localized_name(self):
        return localize_event_text(self.name) or ""

    def __str__(self):
        return self.localized_name or str(self.name)


class ExhibitorInfo(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    name = I18nCharField(max_length=190, verbose_name=_("Name"))
    description = I18nTextField(verbose_name=_("Description"), null=True, blank=True)
    url = models.URLField(verbose_name=_("URL"), null=True, blank=True)
    email = models.EmailField(verbose_name=_("Email"), null=True, blank=True)
    contact_url = models.URLField(verbose_name=_("Contact URL"), null=True, blank=True)
    video_url = models.URLField(verbose_name=_("Video URL"), null=True, blank=True)
    slides = models.FileField(
        upload_to=exhibitor_slides_path,
        verbose_name=_("Slides"),
        null=True,
        blank=True,
    )
    slides_url = models.URLField(verbose_name=_("Slides URL"), null=True, blank=True)
    logo = models.ImageField(upload_to=exhibitor_logo_path, null=True, blank=True)
    logo_url = models.URLField(verbose_name=_("Logo URL"), null=True, blank=True)
    header_image = models.ImageField(
        upload_to=exhibitor_header_image_path, null=True, blank=True
    )
    header_image_url = models.URLField(
        verbose_name=_("Header Image URL"), null=True, blank=True
    )
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
    booth_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    booth_name = I18nCharField(
        max_length=100,
        verbose_name=_("Booth Name"),
        blank=True,
    )
    lead_scanning_enabled = models.BooleanField(default=False)
    allow_voucher_access = models.BooleanField(default=False)
    allow_lead_access = models.BooleanField(default=False)
    lead_scanning_scope_by_device = models.BooleanField(default=False)

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

    @property
    def visible_slides_url(self):
        if self.slides_url:
            return self.slides_url
        if self.slides:
            return self.slides.url
        return ""


class ExhibitorSocialLink(models.Model):
    exhibitor = models.ForeignKey(
        ExhibitorInfo, on_delete=models.CASCADE, related_name="social_links"
    )
    network = models.CharField(max_length=32, choices=SOCIAL_LINK_CHOICES)
    url = models.URLField(verbose_name=_("URL"))

    class Meta:
        ordering = ("network", "url")

    @property
    def spec(self):
        return get_social_link_spec(self.network)

    def __str__(self):
        return f"{self.get_network_display()}: {self.url}"


class ExhibitorExtraLink(models.Model):
    exhibitor = models.ForeignKey(
        ExhibitorInfo, on_delete=models.CASCADE, related_name="extra_links"
    )
    label = models.CharField(max_length=120, verbose_name=_("Label"))
    url = models.URLField(verbose_name=_("URL"))

    class Meta:
        ordering = ("label", "url")

    def __str__(self):
        return f"{self.label}: {self.url}"


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
        verbose_name=_("Booth Name"),
    )

    def __str__(self):
        return f"Lead scanned by {self.exhibitor.name}"


class ExhibitorTag(models.Model):
    exhibitor = models.ForeignKey(
        ExhibitorInfo, on_delete=models.CASCADE, related_name="tags"
    )
    name = models.CharField(max_length=50)
    use_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("exhibitor", "name")
        ordering = ["-use_count", "name"]

    def __str__(self):
        return f"{self.name} ({self.exhibitor.name})"
