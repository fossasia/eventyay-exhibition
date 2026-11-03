import itertools
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product, Voucher
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.forms import ExhibitionComposeForm
from exhibition.models import (
    ExhibitionEmailDraft,
    ExhibitionEmailQueue,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    SponsorGroup,
)
from exhibition.views import EmailOutboxListView, EmailSentListView, group_email_entries

_codes = itertools.count(1)


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _organization(event, name, **kwargs):
    kwargs.setdefault("is_exhibitor", True)
    kwargs.setdefault("email", f"{name.lower()}@example.com")
    return ExhibitorInfo.objects.create(event=event, name=name, **kwargs)


def _vouchers(event, organization, count, *, sent=True, emailed=True):
    product = Product.objects.filter(event=event).first() or Product.objects.create(
        event=event, name="Ticket", default_price=10, active=True
    )
    vouchers = []
    for _ in range(count):
        voucher = Voucher.objects.create(event=event, product=product, code=f"REMIND{next(_codes):05d}")
        ExhibitorVoucher.objects.create(exhibitor=organization, voucher=voucher)
        vouchers.append(voucher)
    if not emailed:
        return vouchers
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
    exhibition_requests, profiles = mail_helpers.resolve_compose_recipients(
        event, state=mail_helpers.VOUCHERS_NOT_ALL_USED, **filters
    )
    assert not exhibition_requests.exists()
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
        unmailed = _vouchers(mail_event, _organization(mail_event, "Unmailed"), 2, emailed=False)
        _use(unmailed[0])
        applicant = User.objects.create_user(email="applicant@example.com", password="pw")
        ExhibitionRequest.objects.create(
            event=mail_event, user=applicant, name="Applicant", state=ExhibitionRequestState.ACCEPTED
        )

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
        _vouchers(mail_event, _organization(mail_event, "Allocated"), 1, emailed=False)
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


def _scheduled_reminder(event, organizations, **filters):
    return mail_helpers.queue_compose_emails(
        event,
        [],
        "Reminder",
        "Please use your vouchers",
        exhibitors=organizations,
        scheduled_at=now() + timedelta(hours=1),
        role=mail_helpers.compose_role(mail_helpers.VOUCHERS_NOT_ALL_USED, **filters),
    )


@pytest.mark.django_db
def test_scheduled_reminder_keeps_the_sponsor_group_it_was_written_for(mail_event):
    with scopes_disabled():
        gold = SponsorGroup.objects.create(event=mail_event, name="Gold", level=1)
        silver = SponsorGroup.objects.create(event=mail_event, name="Silver", level=2)
        sponsor = _organization(mail_event, "Sponsor", is_exhibitor=False, is_sponsor=True, sponsor_group=gold)
        _vouchers(mail_event, sponsor, 1)
        (queued,) = _scheduled_reminder(mail_event, [sponsor], organization_type="sponsor", sponsor_group=gold)
        sponsor.sponsor_group = silver
        sponsor.save(update_fields=["sponsor_group"])

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            delivered = queued.send()

    assert not delivered
    assert not mocked_mail.called


@pytest.mark.django_db
def test_shared_address_still_gets_the_reminder_while_one_organization_qualifies(mail_event):
    with scopes_disabled():
        first = _organization(mail_event, "First", email="shared@example.com")
        second = _organization(mail_event, "Second", email="shared@example.com")
        first_codes = _vouchers(mail_event, first, 1)
        _vouchers(mail_event, second, 1)
        (queued,) = _scheduled_reminder(mail_event, [first, second])
        _use(first_codes[0])

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            delivered = queued.send()

    assert delivered
    assert mocked_mail.called


@pytest.mark.django_db
def test_reminder_matches_a_stored_address_with_spaces_and_capitals(mail_event):
    with scopes_disabled():
        organization = _organization(mail_event, "Acme", email="  Acme@Example.com ")
        _vouchers(mail_event, organization, 1)
        (queued,) = _scheduled_reminder(mail_event, [organization])

        with patch("eventyay.base.services.mail.mail") as mocked_mail:
            delivered = queued.send()

    assert queued.to_email == "Acme@Example.com"
    assert delivered
    assert mocked_mail.called


@pytest.mark.django_db
def test_reopened_draft_keeps_the_group_after_vouchers_stop_being_used(mail_event):
    with scopes_disabled():
        form = ExhibitionComposeForm(event=mail_event, initial={"state": mail_helpers.VOUCHERS_NOT_ALL_USED})

    assert mail_helpers.VOUCHERS_NOT_ALL_USED in dict(form.fields["state"].choices)


def _list_html(event, view_class, url_name):
    request = RequestFactory().get("/")
    request.event = event
    request.user = AnonymousUser()
    request.session = {}
    request.resolver_match = SimpleNamespace(url_name=url_name)
    view = view_class()
    view.setup(request)
    with scopes_disabled():
        view.object_list = view.get_queryset()
        context = view.get_context_data()
        return render_to_string(view.partial_template_name, {**context, "request": request})


@pytest.mark.django_db
def test_outbox_and_sent_lists_show_the_group_label(mail_event):
    with scopes_disabled():
        waiting = _organization(mail_event, "Waiting")
        delivered = _organization(mail_event, "Delivered")
        _vouchers(mail_event, waiting, 1)
        _vouchers(mail_event, delivered, 1)
        _scheduled_reminder(mail_event, [waiting])
        with patch("eventyay.base.services.mail.mail"):
            mail_helpers.queue_compose_emails(
                mail_event,
                [],
                "Reminder",
                "Body",
                exhibitors=[delivered],
                send_now=True,
                role=mail_helpers.compose_role(mail_helpers.VOUCHERS_NOT_ALL_USED),
            )

    assert "Vouchers not all used" in _list_html(mail_event, EmailOutboxListView, "email.outbox")
    assert "Vouchers not all used" in _list_html(mail_event, EmailSentListView, "email.sent")
