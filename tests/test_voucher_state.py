import itertools
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.messages import get_messages
from django.db import DatabaseError
from django.test import Client
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product, Team, Voucher
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    OrganizationVoucherState,
)
from exhibition.utils import with_voucher_state

_codes = itertools.count(1)


def _organization(event, name, **kwargs):
    kwargs.setdefault("is_exhibitor", True)
    return ExhibitorInfo.objects.create(event=event, name=name, email=f"{name.lower()}@example.com", **kwargs)


def _vouchers(event, organization, count):
    product = Product.objects.filter(event=event).first() or Product.objects.create(
        event=event, name="Ticket", default_price=10, active=True
    )
    vouchers = []
    for _ in range(count):
        voucher = Voucher.objects.create(event=event, product=product, code=f"CODE{next(_codes):05d}")
        ExhibitorVoucher.objects.create(exhibitor=organization, voucher=voucher)
        vouchers.append(voucher)
    return vouchers


def _order_with(voucher, status=Order.STATUS_PAID):
    order = Order.objects.create(
        code=f"O{next(_codes):05d}",
        event=voucher.event,
        email="attendee@example.com",
        status=status,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("0.00"),
        locale="en",
    )
    return OrderPosition.objects.create(order=order, product=voucher.product, price=Decimal("0"), voucher=voucher)


def _voucher_email(event, organization, *, sent=True):
    return ExhibitionEmailQueue.objects.create(
        event=event,
        exhibitor=organization,
        role=mail_helpers.VOUCHERS,
        to_email=organization.email,
        subject="Your vouchers",
        body="Codes",
        sent_at=now() - timedelta(days=1) if sent else None,
    )


def _states(event):
    queryset = with_voucher_state(ExhibitorInfo.objects.filter(event=event), event)
    return {str(o.name): (o.voucher_state, o.voucher_claimed, o.voucher_total) for o in queryset}


@pytest.fixture
def journey(event):
    with scopes_disabled():
        _organization(event, "Fresh")

        outbox = _organization(event, "Waiting")
        _vouchers(event, outbox, 3)
        _voucher_email(event, outbox, sent=False)

        sent = _organization(event, "Unclaimed")
        _vouchers(event, sent, 5)
        _voucher_email(event, sent)

        partial = _organization(event, "Halfway")
        first, second, *_rest = _vouchers(event, partial, 5)
        _voucher_email(event, partial)
        _order_with(first)
        _order_with(second, status=Order.STATUS_PENDING)

        done = _organization(event, "Done")
        for voucher in _vouchers(event, done, 2):
            _order_with(voucher)
        _voucher_email(event, done)
    return event


@pytest.mark.django_db
def test_every_organization_gets_one_voucher_state(journey):
    with scopes_disabled():
        states = _states(journey)

    assert states == {
        "Fresh": (OrganizationVoucherState.NOT_SENT, 0, 0),
        "Waiting": (OrganizationVoucherState.OUTBOX, 0, 3),
        "Unclaimed": (OrganizationVoucherState.SENT, 0, 5),
        "Halfway": (OrganizationVoucherState.PARTIAL, 2, 5),
        "Done": (OrganizationVoucherState.CLAIMED, 2, 2),
    }


@pytest.mark.django_db
def test_cancelled_orders_do_not_claim_a_voucher(event):
    with scopes_disabled():
        organization = _organization(event, "Acme")
        voucher, _other = _vouchers(event, organization, 2)
        _voucher_email(event, organization)
        _order_with(voucher, status=Order.STATUS_CANCELED)

        assert _states(event)["Acme"] == (OrganizationVoucherState.SENT, 0, 2)


@pytest.mark.django_db
def test_a_new_order_moves_the_organization_on(event):
    with scopes_disabled():
        organization = _organization(event, "Acme")
        vouchers = _vouchers(event, organization, 2)
        _voucher_email(event, organization)
        assert _states(event)["Acme"][0] == OrganizationVoucherState.SENT

        _order_with(vouchers[0])
        assert _states(event)["Acme"] == (OrganizationVoucherState.PARTIAL, 1, 2)

        _order_with(vouchers[1])
        assert _states(event)["Acme"] == (OrganizationVoucherState.CLAIMED, 2, 2)


@pytest.mark.django_db
def test_an_email_waiting_in_the_outbox_comes_first(event):
    with scopes_disabled():
        organization = _organization(event, "Acme")
        (voucher,) = _vouchers(event, organization, 1)
        _order_with(voucher)
        _voucher_email(event, organization, sent=False)

        assert _states(event)["Acme"][0] == OrganizationVoucherState.OUTBOX


def _organizer_client(event):
    with scopes_disabled():
        event.plugins = "exhibition"
        event.save(update_fields=["plugins"])
        user = User.objects.create_user(email="organizer@example.com", password="pw")
        team = Team.objects.create(organizer=event.organizer, all_events=True, can_change_event_settings=True)
        team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


def _list(client, event, name, query, settings):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    url = reverse(f"plugins:exhibition:{name}", kwargs={"organizer": event.organizer.slug, "event": event.slug})
    response = client.get(url + query)
    assert response.status_code == 200
    return response


def _row_states(response):
    return {str(e.name): e.voucher_state for e in response.context["exhibitors"]}


@pytest.mark.django_db
def test_exhibitor_list_shows_each_voucher_state(journey, settings):
    response = _list(_organizer_client(journey), journey, "exhibitors", "", settings)
    html = response.content.decode()

    assert 'data-voucher-state="not_sent"' in html
    assert "In outbox" in html
    assert "Sent — not claimed" in html
    assert "0 of 5 · sent" in html
    assert "2 of 5 claimed" in html
    assert "Claimed" in html
    assert "2 of 2" in html
    assert "Last sent " in html and " · 2 of 5 claimed" in html


@pytest.mark.django_db
def test_exhibitor_list_filters_by_voucher_state(journey, settings):
    response = _list(_organizer_client(journey), journey, "exhibitors", "?voucher_state=sent", settings)

    assert _row_states(response) == {"Unclaimed": OrganizationVoucherState.SENT}


@pytest.mark.django_db
def test_exhibitor_list_sorts_by_voucher_state(journey, settings):
    response = _list(_organizer_client(journey), journey, "exhibitors", "?ordering=-vouchers", settings)

    assert list(_row_states(response)) == ["Done", "Halfway", "Unclaimed", "Waiting", "Fresh"]


@pytest.mark.django_db
def test_sponsor_list_uses_the_same_voucher_states(event, settings):
    with scopes_disabled():
        sponsor = _organization(event, "Gold", is_exhibitor=False, is_sponsor=True)
        vouchers = _vouchers(event, sponsor, 4)
        _voucher_email(event, sponsor)
        _order_with(vouchers[0])

    response = _list(_organizer_client(event), event, "sponsors", "", settings)

    assert _row_states(response) == {"Gold": OrganizationVoucherState.PARTIAL}
    assert "1 of 4 claimed" in response.content.decode()


@pytest.mark.django_db
def test_resending_vouchers_sends_only_the_unused_codes(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False)
        organization = _organization(event, "Acme")
        used, unused = _vouchers(event, organization, 2)
        _voucher_email(event, organization)
        _order_with(used)

        queued, skipped = mail_helpers.queue_voucher_emails(event, [organization], issue_missing=True)

    assert len(queued) == 1
    assert unused.code in queued[0].body
    assert used.code not in queued[0].body
    assert not any(skipped.values())


@pytest.mark.django_db
def test_a_fully_claimed_organization_is_skipped_and_not_issued_new_codes(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False, voucher_default_count=3)
        organization = _organization(event, "Acme")
        (voucher,) = _vouchers(event, organization, 1)
        _voucher_email(event, organization)
        _order_with(voucher)

        queued, skipped = mail_helpers.queue_voucher_emails(event, [organization], issue_missing=True)

        assert queued == []
        assert skipped[mail_helpers.VOUCHER_SKIP_ALL_CLAIMED] == [organization]
        assert ExhibitorVoucher.objects.filter(exhibitor=organization).count() == 1


@pytest.mark.django_db
def test_bulk_send_preview_lists_fully_claimed_organizations_separately(event):
    from django.test import RequestFactory

    from exhibition.views import ExhibitorVoucherBulkSendView

    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False)
        halfway = _organization(event, "Halfway")
        used, _unused = _vouchers(event, halfway, 2)
        _order_with(used)
        done = _organization(event, "Done")
        (claimed,) = _vouchers(event, done, 1)
        _order_with(claimed)

        request = RequestFactory().post("/")
        request.event = event
        view = ExhibitorVoucherBulkSendView()
        view.request = request
        view.organization_type = "exhibitor"
        sendable, _no_email, _no_vouchers, _pool_short, all_claimed, _in_outbox = view.preview([halfway, done])

    assert sendable == [halfway]
    assert halfway.voucher_total == 1
    assert all_claimed == [done]


def _bulk_send_view(event, data):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    from exhibition.views import ExhibitorVoucherBulkSendView

    request = RequestFactory().post("/", data=data)
    request.event = event
    request.user = User.objects.create_user(email=f"sender-{next(_codes)}@example.com", password="pw")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    view = ExhibitorVoucherBulkSendView()
    view.request = request
    view.organization_type = "exhibitor"
    view.kwargs = {}
    return view, request


@pytest.mark.django_db
def test_a_voucher_email_already_in_the_outbox_is_not_queued_again(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False)
        organization = _organization(event, "Acme")
        _vouchers(event, organization, 2)

        first, _skipped = mail_helpers.queue_voucher_emails(event, [organization], issue_missing=True)
        again, skipped = mail_helpers.queue_voucher_emails(event, [organization], issue_missing=True)

        assert len(first) == 1
        assert again == []
        assert skipped[mail_helpers.VOUCHER_SKIP_IN_OUTBOX] == [organization]
        assert ExhibitionEmailQueue.objects.filter(exhibitor=organization).count() == 1


@pytest.mark.django_db
def test_bulk_send_preview_lists_organizations_with_a_waiting_voucher_email(event):
    with scopes_disabled():
        organization = _organization(event, "Acme")
        _vouchers(event, organization, 2)
        _voucher_email(event, organization, sent=False)
        view, _request = _bulk_send_view(event, {})

        sendable, _no_email, _no_vouchers, _pool_short, _all_claimed, in_outbox = view.preview([organization])

    assert sendable == []
    assert in_outbox == [organization]


@pytest.mark.django_db
def test_confirming_only_fully_claimed_organizations_says_why(event):
    with scopes_disabled():
        done = _organization(event, "Done")
        (voucher,) = _vouchers(event, done, 1)
        _order_with(voucher)
        view, request = _bulk_send_view(event, {"selected": [done.pk], "confirmed": "1"})

        response = view.post(request)
        notices = [str(message) for message in request._messages]

    assert response.status_code == 302
    assert "There is nobody to send vouchers to in this selection." in notices
    assert "1 was skipped because all of their vouchers have already been claimed." in notices
    assert not ExhibitionEmailQueue.objects.filter(exhibitor=done).exists()


@pytest.mark.django_db
def test_a_failed_organization_keeps_the_emails_already_queued_for_others(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False)
        first = _organization(event, "First")
        _vouchers(event, first, 1)
        second = _organization(event, "Second")
        _vouchers(event, second, 1)
        view, request = _bulk_send_view(event, {})
        queue_voucher_email = mail_helpers.queue_voucher_email

        def deadlock_on_second(event, exhibitor, vouchers, **kwargs):
            if exhibitor == second:
                raise DatabaseError("deadlock detected")
            return queue_voucher_email(event, exhibitor, vouchers, **kwargs)

        with (
            patch.object(mail_helpers, "queue_voucher_email", side_effect=deadlock_on_second),
            pytest.raises(DatabaseError),
        ):
            view.queue_all([first, second], requestor=request.user)

        assert ExhibitionEmailQueue.objects.filter(exhibitor=first).count() == 1
        assert not ExhibitionEmailQueue.objects.filter(exhibitor=second).exists()


def _send_from_voucher_page(event, organization, settings, count=0):
    settings.DEBUG = True
    settings.COMPRESS_ENABLED = False
    settings.COMPRESS_PRECOMPILERS = ()
    url = reverse(
        "plugins:exhibition:vouchers",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "pk": organization.pk},
    )
    response = _organizer_client(event).post(url, {"action": "send", "count": count})
    return response, [str(message) for message in get_messages(response.wsgi_request)]


@pytest.mark.django_db
def test_the_voucher_page_emails_only_the_unused_codes(event, settings):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False)
        organization = _organization(event, "Acme")
        used, unused = _vouchers(event, organization, 2)
        _voucher_email(event, organization)
        _order_with(used)

    response, notices = _send_from_voucher_page(event, organization, settings)

    assert response.status_code == 302
    assert notices == ["An email with 1 voucher code was placed in the outbox."]
    with scopes_disabled():
        email = ExhibitionEmailQueue.objects.get(exhibitor=organization, sent_at__isnull=True)
    assert unused.code in email.body
    assert used.code not in email.body


@pytest.mark.django_db
def test_the_voucher_page_does_not_queue_a_second_email_while_one_waits(event, settings):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False, voucher_pool_tag="pool")
        organization = _organization(event, "Acme")
        _vouchers(event, organization, 2)
        _voucher_email(event, organization, sent=False)
        product = Product.objects.get(event=event)
        for _ in range(3):
            Voucher.objects.create(event=event, product=product, tag="pool")

    response, notices = _send_from_voucher_page(event, organization, settings, count=3)

    assert response.status_code == 302
    assert notices == [
        "A voucher email for this organization is already waiting in the outbox. "
        "Send or delete it there before preparing another one."
    ]
    with scopes_disabled():
        assert ExhibitionEmailQueue.objects.filter(exhibitor=organization).count() == 1
        assert ExhibitorVoucher.objects.filter(exhibitor=organization).count() == 2


@pytest.mark.django_db
def test_the_voucher_page_sends_nothing_once_every_code_is_claimed(event, settings):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_attach_csv=False)
        organization = _organization(event, "Acme")
        (voucher,) = _vouchers(event, organization, 1)
        _voucher_email(event, organization)
        _order_with(voucher)

    response, _notices = _send_from_voucher_page(event, organization, settings)

    assert response.status_code == 200
    assert response.context["form"].errors["count"] == [
        "Every voucher this organization holds has already been claimed. Take more from the pool to email new ones."
    ]
    with scopes_disabled():
        assert not ExhibitionEmailQueue.objects.filter(exhibitor=organization, sent_at__isnull=True).exists()
