from datetime import timedelta
from decimal import Decimal

import pytest
from django.http import Http404
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product, Voucher
from eventyay.base.models.auth import User

from exhibition.models import (
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
)
from exhibition.utils import (
    build_voucher_redemption_csv,
    exhibitor_unredeemed_vouchers,
    exhibitor_voucher_redemptions,
)
from exhibition.views import UserVoucherListView


def _settings(event, **kwargs):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        **kwargs,
    )


def _accepted(event, *, allow_voucher_access=True, state=ExhibitionRequestState.ACCEPTED):
    user = User.objects.create_user(email="exhibitor@example.com", password="pw")
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", allow_voucher_access=allow_voucher_access)
    exhibition_request = ExhibitionRequest.objects.create(
        event=event,
        user=user,
        name="Acme",
        state=state,
        approved_exhibitor=exhibitor,
    )
    return exhibition_request, exhibitor, user


def _redeem(event, exhibitor, *, code, attendee_name="Dana Scully", status=Order.STATUS_PAID):
    product = Product.objects.create(event=event, name="Ticket", default_price=10, active=True)
    voucher = Voucher.objects.create(event=event, product=product, code=code)
    ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=voucher)
    order = Order.objects.create(
        code=code[:5].upper(),
        event=event,
        email="attendee@example.com",
        status=status,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("10.00"),
        locale="en",
    )
    return OrderPosition.objects.create(
        order=order,
        product=product,
        price=Decimal("0"),
        voucher=voucher,
        attendee_name_parts={"_legacy": attendee_name},
        attendee_email="attendee@example.com",
    )


def _view(exhibition_request, user, event, query=""):
    request = RequestFactory().get(f"/{query}")
    request.user = user
    request.event = event
    request.session = {}
    view = UserVoucherListView()
    view.request = request
    view.kwargs = {"code": exhibition_request.code}
    return view


@pytest.mark.django_db
def test_redemptions_list_only_this_exhibitors_vouchers(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        other = ExhibitorInfo.objects.create(event=event, name="Other")
        _redeem(event, exhibitor, code="MINE1234")
        _redeem(event, other, code="THEIRS12")

        codes = [position.voucher.code for position in exhibitor_voucher_redemptions(exhibitor)]

        assert codes == ["MINE1234"]


@pytest.mark.django_db
def test_cancelled_orders_are_left_out(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="LIVE1234")
        _redeem(event, exhibitor, code="GONE1234", status=Order.STATUS_CANCELED)

        codes = [position.voucher.code for position in exhibitor_voucher_redemptions(exhibitor)]

        assert codes == ["LIVE1234"]


@pytest.mark.django_db
def test_page_is_hidden_without_voucher_access(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event, allow_voucher_access=False)

        with pytest.raises(Http404):
            _view(exhibition_request, user, event).exhibitor


@pytest.mark.django_db
def test_page_is_hidden_until_the_request_is_accepted(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event, state=ExhibitionRequestState.SUBMITTED)

        with pytest.raises(Http404):
            _view(exhibition_request, user, event).exhibitor


@pytest.mark.django_db
def test_another_submitter_cannot_open_the_page(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, _owner = _accepted(event)
        intruder = User.objects.create_user(email="intruder@example.com", password="pw")

        with pytest.raises(Http404):
            _view(exhibition_request, intruder, event).exhibition_request


@pytest.mark.django_db
def test_attendee_columns_follow_the_allowed_fields(event):
    with scopes_disabled():
        settings = _settings(event, allowed_fields=["attendee_name"])
        exhibition_request, exhibitor, user = _accepted(event)
        position = _redeem(event, exhibitor, code="SHOW1234")

        body = build_voucher_redemption_csv(event, [position], settings)

        assert "Name" in body.splitlines()[0]
        assert "Email" not in body.splitlines()[0]
        assert "attendee@example.com" not in body


@pytest.mark.django_db
def test_csv_download_returns_a_file(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="CSV12345")

        view = _view(exhibition_request, user, event, query="?download=yes")
        response = view.download_csv()

        assert response.status_code == 200
        assert response["Content-Disposition"].endswith('filename="voucher-redemptions.csv"')
        assert "CSV12345" in response.content.decode("utf-8")


@pytest.mark.django_db
def test_unredeemed_vouchers_exclude_the_redeemed_ones(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")
        spare = Voucher.objects.create(event=event, code="SPARE123")
        ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=spare)

        assert [voucher.code for voucher in exhibitor_unredeemed_vouchers(exhibitor)] == ["SPARE123"]


@pytest.mark.django_db
def test_unredeemed_download_uses_the_emailed_voucher_columns(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")
        spare = Voucher.objects.create(event=event, code="SPARE123")
        ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=spare)

        response = _view(exhibition_request, user, event, query="?status=pending&download=yes").download_csv()
        body = response.content.decode("utf-8")

        assert response["Content-Disposition"].endswith('filename="exhibitor-vouchers.csv"')
        assert "Maximum usages" in body.splitlines()[0]
        assert "SPARE123" in body
        assert "USED1234" not in body


@pytest.mark.django_db
def test_each_tab_shows_only_its_own_vouchers(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")
        spare = Voucher.objects.create(event=event, code="SPARE123")
        ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=spare)

        pending = _view(exhibition_request, user, event, query="?status=pending")
        redeemed = _view(exhibition_request, user, event)

        assert [voucher.code for voucher in pending.get_queryset()] == ["SPARE123"]
        assert [position.voucher.code for position in redeemed.get_queryset()] == ["USED1234"]


@pytest.mark.django_db
def test_download_is_not_swallowed_by_the_filter_form(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")

        view = _view(exhibition_request, user, event, query="?status=&download=yes")
        response = view.get(view.request)

        assert response["Content-Disposition"].endswith('filename="voucher-redemptions.csv"')


@pytest.mark.django_db
def test_blank_status_shows_the_redeemed_tab(event):
    with scopes_disabled():
        _settings(event)
        exhibition_request, exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")
        spare = Voucher.objects.create(event=event, code="SPARE123")
        ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=spare)

        view = _view(exhibition_request, user, event, query="?status=")

        assert view.showing_unredeemed is False
        assert [position.voucher.code for position in view.get_queryset()] == ["USED1234"]
