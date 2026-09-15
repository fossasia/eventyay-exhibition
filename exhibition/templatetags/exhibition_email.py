from django import template
from django.conf import settings

from ..models import ExhibitionEmailQueue

register = template.Library()


@register.simple_tag
def pending_email_count(event):
    """Number of unsent queued emails for the event, shown as a nav badge."""
    return ExhibitionEmailQueue.objects.filter(event=event, sent_at__isnull=True).count()


@register.filter
def locale_dir(locale):
    """Writing direction of a locale, ``rtl`` for the languages core marks as right-to-left."""
    code = str(locale)
    if code in settings.LANGUAGES_RTL or code.split("-")[0] in settings.LANGUAGES_RTL:
        return "rtl"
    return "ltr"


@register.filter
def locale_name(locale):
    """Human-readable language name, matching the titles the i18n widgets put on their inputs."""
    return dict(settings.LANGUAGES).get(locale, locale)
