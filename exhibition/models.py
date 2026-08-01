import os
import secrets
import string

from django.conf import settings
from django.db import models
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.translation import gettext_lazy as _
from eventyay.base.models import Event
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

    characters = string.ascii_letters + string.digits
    while True:
        booth_id = "".join(random.choices(characters, k=8))
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
