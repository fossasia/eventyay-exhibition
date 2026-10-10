from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.http import Http404
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Order, OrderPosition, Product, Voucher
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.dashboard import (
    MyExhibitionEditView,
    MyExhibitionsView,
    MyExhibitionVouchersView,
    user_has_exhibitions,
)
from exhibition.forms import ExhibitorSelfEditForm
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
)
from exhibition.utils import (
    build_voucher_redemption_csv,
    exhibitor_login_email,
    exhibitor_unredeemed_vouchers,
    exhibitor_voucher_redemptions,
    user_can_edit_profile,
    user_can_view_vouchers,
)
from exhibition.views import queue_profile_invitation


@pytest.fixture
def mail_event(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _settings(event, **kwargs):
    return ExhibitorSettings.objects.create(
        event=event,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
        **kwargs,
    )


def _user(email):
    user = User.objects.create_user(email=email, password="pw")
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def _accepted(event, *, allow_voucher_access=True, state=ExhibitionRequestState.ACCEPTED):
    user = _user("applicant@example.com")
    exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", allow_voucher_access=allow_voucher_access)
    ExhibitionRequest.objects.create(event=event, user=user, name="Acme", state=state, approved_exhibitor=exhibitor)
    return exhibitor, user


def _organizer_added(event, email="booth@example.com", **kwargs):
    kwargs.setdefault("allow_voucher_access", True)
    kwargs.setdefault("name", "Booth Co")
    return ExhibitorInfo.objects.create(event=event, email=email, **kwargs)


def _redeem(event, exhibitor, *, code, status=Order.STATUS_PAID):
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
        attendee_name_parts={"_legacy": "Dana Scully"},
        attendee_email="attendee@example.com",
    )


def _vouchers_view(exhibitor, user, query=""):
    request = RequestFactory().get(f"/{query}")
    request.user = user
    view = MyExhibitionVouchersView()
    view.request = request
    view.kwargs = {"pk": exhibitor.pk}
    view.args = ()
    return view, request


@pytest.mark.django_db
def test_request_owner_can_open_their_vouchers(event):
    with scopes_disabled():
        exhibitor, user = _accepted(event)

        assert user_can_view_vouchers(user, exhibitor)


@pytest.mark.django_db
def test_organizer_added_exhibitor_opens_for_the_matching_verified_email(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event, email="Booth@Example.com")
        owner = _user("booth@example.com")

        assert user_can_view_vouchers(owner, exhibitor)


@pytest.mark.django_db
def test_unverified_email_does_not_unlock_an_organizer_added_exhibitor(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event)
        claimant = _user("claimant@example.com")
        EmailAddress.objects.create(user=claimant, email="booth@example.com", verified=False)

        assert not user_can_view_vouchers(claimant, exhibitor)


@pytest.mark.django_db
def test_unverified_login_email_does_not_unlock_an_organizer_added_exhibitor(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event)
        claimant = User.objects.create_user(email="booth@example.com", password="pw")

        assert not user_can_view_vouchers(claimant, exhibitor)


@pytest.mark.django_db
def test_email_match_does_not_reach_an_exhibitor_that_came_through_a_request(event):
    with scopes_disabled():
        exhibitor, _owner = _accepted(event)
        exhibitor.email = "other@example.com"
        exhibitor.save(update_fields=["email"])
        other = _user("other@example.com")

        assert not user_can_view_vouchers(other, exhibitor)


@pytest.mark.django_db
@pytest.mark.parametrize("change", [{"allow_voucher_access": False}, {"active": False}])
def test_voucher_page_closes_when_access_is_withdrawn(event, change):
    with scopes_disabled():
        exhibitor = _organizer_added(event, **change)
        owner = _user("booth@example.com")
        view, _request = _vouchers_view(exhibitor, owner)

        with pytest.raises(Http404):
            view.exhibitor


@pytest.mark.django_db
def test_stranger_cannot_open_someone_elses_vouchers(event):
    with scopes_disabled():
        exhibitor, _owner = _accepted(event)
        stranger = _user("stranger@example.com")
        view, _request = _vouchers_view(exhibitor, stranger)

        with pytest.raises(Http404):
            view.exhibitor


@pytest.mark.django_db
def test_my_exhibitions_lists_requests_and_organizer_added_profiles(event):
    with scopes_disabled():
        _settings(event)
        exhibitor, user = _accepted(event)
        _organizer_added(event, email="applicant@example.com", name="Second Booth")
        request = RequestFactory().get("/")
        request.user = user
        view = MyExhibitionsView()
        view.request = request
        view.kwargs = {}

        entries = view.get_queryset()

        assert {str(entry["name"]) for entry in entries} == {"Acme", "Second Booth"}
        assert all(entry["vouchers_url"] for entry in entries)
        assert user_has_exhibitions(user)


def _my_exhibitions(user, query=""):
    request = RequestFactory().get(f"/{query}")
    request.user = user
    view = MyExhibitionsView()
    view.request = request
    view.kwargs = {}
    return [str(entry["name"]) for entry in view.get_queryset()]


@pytest.mark.django_db
def test_my_exhibitions_search_matches_the_organization_name(event):
    with scopes_disabled():
        _exhibitor, user = _accepted(event)
        _organizer_added(event, email="applicant@example.com", name="Second Booth")

        assert _my_exhibitions(user, "?search=second") == ["Second Booth"]


@pytest.mark.django_db
def test_my_exhibitions_filters_by_event(event):
    with scopes_disabled():
        _exhibitor, user = _accepted(event)
        _organizer_added(event, email="applicant@example.com", name="Second Booth")

        assert sorted(_my_exhibitions(user, f"?event={event.pk}")) == ["Acme", "Second Booth"]
        assert sorted(_my_exhibitions(user, "?event=999999")) == ["Acme", "Second Booth"]


@pytest.mark.django_db
def test_login_email_is_the_requesters_account_for_request_based_exhibitors(event):
    with scopes_disabled():
        exhibitor, _user_ = _accepted(event)
        exhibitor.email = "contact@example.com"
        exhibitor.save(update_fields=["email"])

        assert exhibitor_login_email(exhibitor) == "applicant@example.com"


@pytest.mark.django_db
def test_login_email_is_the_contact_email_for_organizer_added_exhibitors(event):
    with scopes_disabled():
        assert exhibitor_login_email(_organizer_added(event)) == "booth@example.com"


@pytest.mark.django_db
def test_dashboard_link_is_hidden_without_any_exhibition(event):
    with scopes_disabled():
        assert not user_has_exhibitions(_user("nobody@example.com"))


@pytest.mark.django_db
def test_redemptions_list_only_this_exhibitors_vouchers(event):
    with scopes_disabled():
        exhibitor, _user_ = _accepted(event)
        other = ExhibitorInfo.objects.create(event=event, name="Other")
        _redeem(event, exhibitor, code="MINE1234")
        _redeem(event, other, code="THEIRS12")

        assert [position.voucher.code for position in exhibitor_voucher_redemptions(exhibitor)] == ["MINE1234"]


@pytest.mark.django_db
def test_cancelled_orders_are_left_out(event):
    with scopes_disabled():
        exhibitor, _user_ = _accepted(event)
        _redeem(event, exhibitor, code="LIVE1234")
        _redeem(event, exhibitor, code="GONE1234", status=Order.STATUS_CANCELED)

        assert [position.voucher.code for position in exhibitor_voucher_redemptions(exhibitor)] == ["LIVE1234"]


@pytest.mark.django_db
def test_unredeemed_vouchers_exclude_the_redeemed_ones(event):
    with scopes_disabled():
        exhibitor, _user_ = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")
        spare = Voucher.objects.create(event=event, code="SPARE123")
        ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=spare)

        assert [voucher.code for voucher in exhibitor_unredeemed_vouchers(exhibitor)] == ["SPARE123"]


@pytest.mark.django_db
def test_exhibitor_without_vouchers_gets_empty_lists(event):
    with scopes_disabled():
        exhibitor, _user_ = _accepted(event)

        assert list(exhibitor_voucher_redemptions(exhibitor)) == []
        assert list(exhibitor_unredeemed_vouchers(exhibitor)) == []


@pytest.mark.django_db
def test_attendee_columns_follow_the_allowed_fields(event):
    with scopes_disabled():
        settings = _settings(event, allowed_fields=["attendee_name"])
        exhibitor, _user_ = _accepted(event)
        position = _redeem(event, exhibitor, code="SHOW1234")

        body = build_voucher_redemption_csv(event, [position], settings)

        assert "Name" in body.splitlines()[0]
        assert "Email" not in body.splitlines()[0]
        assert "attendee@example.com" not in body


@pytest.mark.django_db
def test_each_tab_downloads_its_own_csv(event):
    with scopes_disabled():
        _settings(event)
        exhibitor, user = _accepted(event)
        _redeem(event, exhibitor, code="USED1234")
        spare = Voucher.objects.create(event=event, code="SPARE123")
        ExhibitorVoucher.objects.create(exhibitor=exhibitor, voucher=spare)

        redeemed, _request = _vouchers_view(exhibitor, user, "?download=yes")
        pending, _request = _vouchers_view(exhibitor, user, "?status=pending&download=yes")
        redeemed_body = redeemed.download_csv().content.decode("utf-8")
        pending_response = pending.download_csv()

        assert "USED1234" in redeemed_body and "SPARE123" not in redeemed_body
        assert pending_response["Content-Disposition"].endswith('filename="exhibitor-vouchers.csv"')
        assert "SPARE123" in pending_response.content.decode("utf-8")


def _edit_view(exhibitor, user):
    request = RequestFactory().get("/")
    request.user = user
    view = MyExhibitionEditView()
    view.request = request
    view.kwargs = {"pk": exhibitor.pk}
    view.args = ()
    return view


@pytest.mark.django_db
def test_verified_email_owner_can_edit_an_organizer_added_profile(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event)
        owner = _user("booth@example.com")

        assert user_can_edit_profile(owner, exhibitor)
        assert _edit_view(exhibitor, owner).exhibitor == exhibitor


@pytest.mark.django_db
def test_stranger_cannot_edit_an_organizer_added_profile(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event)
        stranger = _user("stranger@example.com")

        with pytest.raises(Http404):
            _edit_view(exhibitor, stranger).exhibitor


@pytest.mark.django_db
def test_unverified_email_cannot_edit_an_organizer_added_profile(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event)
        claimant = User.objects.create_user(email="booth@example.com", password="pw")

        assert not user_can_edit_profile(claimant, exhibitor)


@pytest.mark.django_db
def test_request_based_profiles_are_not_editable_here(event):
    with scopes_disabled():
        exhibitor, owner = _accepted(event)

        assert not user_can_edit_profile(owner, exhibitor)
        with pytest.raises(Http404):
            _edit_view(exhibitor, owner).exhibitor


@pytest.mark.django_db
def test_inactive_profile_cannot_be_edited(event):
    with scopes_disabled():
        exhibitor = _organizer_added(event, active=False)
        owner = _user("booth@example.com")

        assert not user_can_edit_profile(owner, exhibitor)


@pytest.mark.django_db
def test_organizer_added_profiles_link_their_title_to_the_edit_page(event):
    with scopes_disabled():
        _settings(event)
        _exhibitor, user = _accepted(event)
        _organizer_added(event, email="applicant@example.com", name="Second Booth")
        view = MyExhibitionsView()
        view.request = RequestFactory().get("/")
        view.request.user = user
        view.kwargs = {}

        links = {str(entry["name"]): entry["request_url"] for entry in view.get_queryset()}

        assert "/edit/" not in links["Acme"]
        assert links["Second Booth"].endswith("/edit/")


@pytest.mark.django_db
def test_self_edit_form_leaves_out_organizer_only_fields(event):
    with scopes_disabled():
        _settings(event)
        exhibitor = _organizer_added(event)

        form = ExhibitorSelfEditForm(instance=exhibitor, event=event)

        for name in ("email", "published", "is_sponsor", "is_exhibitor", "allow_voucher_access", "booth_id", "comment"):
            assert name not in form.fields


@pytest.mark.django_db
def test_self_edit_saves_content_without_touching_organizer_settings(event):
    with scopes_disabled():
        _settings(event)
        exhibitor = _organizer_added(
            event,
            is_sponsor=True,
            is_exhibitor=False,
            booth_id="B-7",
            logo="exhibitors/logos/Booth Co/logo.png",
            banner="exhibitors/banners/Booth Co/banner.png",
        )
        form = ExhibitorSelfEditForm(
            {"name_0": "Booth Co", "description_0": "About our booth"},
            instance=exhibitor,
            event=event,
        )

        assert form.is_valid(), form.errors
        form.save()
        exhibitor.refresh_from_db()

        assert str(exhibitor.description) == "About our booth"
        assert exhibitor.email == "booth@example.com"
        assert exhibitor.is_sponsor and not exhibitor.is_exhibitor
        assert exhibitor.allow_voucher_access
        assert exhibitor.booth_id == "B-7"


@pytest.mark.django_db
def test_profile_email_is_queued_for_the_exhibitor(mail_event):
    with scopes_disabled():
        _settings(mail_event)
        exhibitor = _organizer_added(mail_event)

        queued = mail_helpers.queue_exhibitor_profile_email(mail_event, exhibitor)

        assert queued.role == mail_helpers.EXHIBITOR_PROFILE
        assert queued.to_email == "booth@example.com"
        assert mail_helpers.my_exhibitions_url() in queued.body
        assert "booth@example.com" in queued.body
        assert "{my_exhibitions_url}" not in queued.body
        assert "{login_email}" not in queued.body


@pytest.mark.django_db
def test_profile_email_is_skipped_without_an_address(event):
    with scopes_disabled():
        _settings(event)
        exhibitor = _organizer_added(event, email="")

        assert mail_helpers.queue_exhibitor_profile_email(event, exhibitor) is None
        assert not ExhibitionEmailQueue.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_creating_a_profile_queues_the_invitation_without_sending_it(event):
    with scopes_disabled():
        _settings(event)
        exhibitor = _organizer_added(event)

        with patch.object(ExhibitionEmailQueue, "send") as send:
            queue_profile_invitation(event, exhibitor)

        send.assert_not_called()
        queued = ExhibitionEmailQueue.objects.get(event=event, role=mail_helpers.EXHIBITOR_PROFILE)
        assert queued.sent_at is None
