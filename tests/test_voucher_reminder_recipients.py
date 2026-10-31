import itertools
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product, Voucher

from exhibition import mail as mail_helpers
from exhibition.forms import ExhibitionComposeForm
from exhibition.models import (
    ExhibitionEmailDraft,
    ExhibitionEmailQueue,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    SponsorGroup,
)
from exhibition.views import group_email_entries

_codes = itertools.count(1)


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _organization(event, name, **kwargs):
    kwargs.setdefault("is_exhibitor", True)
    return ExhibitorInfo.objects.create(event=event, name=name, email=f"{name.lower()}@example.com", **kwargs)


def _vouchers(event, organization, count, *, sent=True):
    product = Product.objects.filter(event=event).first() or Product.objects.create(
        event=event, name="Ticket", default_price=10, active=True
    )
    vouchers = []
    for _ in range(count):
        voucher = Voucher.objects.create(event=event, product=product, code=f"REMIND{next(_codes):05d}")
        ExhibitorVoucher.objects.create(exhibitor=organization, voucher=voucher)
        vouchers.append(voucher)
    ExhibitionEmailQueue.objects.create(
        event=event,
        exhibitor=organization,
        role=mail_helpers.VOUCHERS,
        to_email=organization.email,
        subject="Your vouchers",
        body="Codes",
        sent_at=now() - timedelta(days=1) if sent else None,
    )
    return vouchers


def _use(voucher):
    order = Order.objects.create(
        code=f"R{next(_codes):05d}",
        event=voucher.event,
        email="attendee@example.com",
        status=Order.STATUS_PAID,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("0.00"),
        locale="en",
    )
    OrderPosition.objects.create(order=order, product=voucher.product, price=Decimal("0"), voucher=voucher)


def _group_names(event, **filters):
    _requests, profiles = mail_helpers.resolve_compose_recipients(
        event, state=mail_helpers.VOUCHERS_NOT_ALL_USED, **filters
    )
    return sorted(str(profile.name) for profile in profiles)


@pytest.mark.django_db
def test_group_holds_organizations_with_sent_vouchers_not_all_used(mail_event):
    with scopes_disabled():
        _vouchers(mail_event, _organization(mail_event, "Unused"), 2)
        partly = _vouchers(mail_event, _organization(mail_event, "Partly"), 2)
        _use(partly[0])
        claimed = _vouchers(mail_event, _organization(mail_event, "Claimed"), 1)
        _use(claimed[0])
        _vouchers(mail_event, _organization(mail_event, "Outbox"), 1, sent=False)
        _organization(mail_event, "Never")
        _vouchers(mail_event, _organization(mail_event, "Inactive", active=False), 1)

        assert _group_names(mail_event) == ["Partly", "Unused"]


@pytest.mark.django_db
def test_group_narrows_by_organization_type_and_sponsor_group(mail_event):
    with scopes_disabled():
        gold = SponsorGroup.objects.create(event=mail_event, name="Gold", level=1)
        _vouchers(mail_event, _organization(mail_event, "Booth"), 1)
        _vouchers(
            mail_event,
            _organization(mail_event, "Sponsor", is_exhibitor=False, is_sponsor=True, sponsor_group=gold),
            1,
        )

        assert _group_names(mail_event, organization_type="exhibitor") == ["Booth"]
        assert _group_names(mail_event, organization_type="sponsor") == ["Sponsor"]
        assert _group_names(mail_event, sponsor_group=gold) == ["Sponsor"]


@pytest.mark.django_db
def test_option_is_offered_only_when_the_event_uses_vouchers(mail_event):
    with scopes_disabled():

        def offered():
            form = ExhibitionComposeForm(event=mail_event)
            return mail_helpers.VOUCHERS_NOT_ALL_USED in dict(form.fields["state"].choices)

        assert not offered()
        ExhibitorSettings.objects.update_or_create(event=mail_event, defaults={"voucher_pool_tag": "exhibitors"})
        assert offered()


@pytest.mark.django_db
def test_scheduled_email_is_dropped_once_all_vouchers_are_used(mail_event):
    with scopes_disabled():
        organization = _organization(mail_event, "Acme")
        vouchers = _vouchers(mail_event, organization, 1)
        _requests, profiles = mail_helpers.resolve_compose_recipients(
            mail_event, state=mail_helpers.VOUCHERS_NOT_ALL_USED
        )
        (queued,) = mail_helpers.queue_compose_emails(
            mail_event,
            [],
            "Reminder",
            "Please use your vouchers",
            exhibitors=profiles,
            scheduled_at=now() + timedelta(hours=1),
            role=mail_helpers.compose_role(mail_helpers.VOUCHERS_NOT_ALL_USED),
        )
        _use(vouchers[0])

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            queued.send()

        assert not mocked_mail.called
        assert not ExhibitionEmailQueue.objects.filter(pk=queued.pk).exists()


@pytest.mark.django_db
def test_scheduled_email_still_goes_out_while_vouchers_remain(mail_event):
    with scopes_disabled():
        organization = _organization(mail_event, "Acme")
        _vouchers(mail_event, organization, 2)
        (queued,) = mail_helpers.queue_compose_emails(
            mail_event,
            [],
            "Reminder",
            "Please use your vouchers",
            exhibitors=[organization],
            role=mail_helpers.VOUCHERS_NOT_ALL_USED,
        )

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            queued.send()
        queued.refresh_from_db()

    assert mocked_mail.called
    assert queued.sent_at is not None


@pytest.mark.django_db
def test_lists_label_the_group_and_drafts_keep_it(mail_event):
    with scopes_disabled():
        organization = _organization(mail_event, "Acme")
        _vouchers(mail_event, organization, 1)
        queued = mail_helpers.queue_compose_emails(
            mail_event,
            [],
            "Reminder",
            "Body",
            exhibitors=[organization],
            role=mail_helpers.VOUCHERS_NOT_ALL_USED,
        )
        draft = ExhibitionEmailDraft(event=mail_event)
        draft.fill_from_form({"state": mail_helpers.VOUCHERS_NOT_ALL_USED, "subject": "Reminder"})
        draft.save()
        draft.refresh_from_db()

    (entry,) = group_email_entries(queued)
    assert str(entry["group_label"]) == "Vouchers not all used"
    assert draft.form_initial()["state"] == mail_helpers.VOUCHERS_NOT_ALL_USED
