from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, Organizer, Product, Voucher
from eventyay.base.models.auth import User

from exhibition import mail as mail_helpers
from exhibition.api import VoucherRedemptionRetrieveView, get_allowed_attendee_data
from exhibition.forms import ExhibitorVoucherBatchForm, ExhibitorVoucherDefaultsForm
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    SponsorGroup,
)
from exhibition.utils import (
    VOUCHER_CSV_FILENAME,
    build_voucher_csv,
    claim_pool_vouchers,
    pool_remaining,
    pool_tag_choices,
    resolve_voucher_defaults,
    store_voucher_csv,
    voucher_redeem_url,
)
from exhibition.views import ExhibitorVoucherBulkSendView, ExhibitorVoucherManageView


def _exhibitor(event, **kwargs):
    return ExhibitorInfo.objects.create(event=event, name=kwargs.pop("name", "Acme"), **kwargs)


def _product(event):
    return Product.objects.create(event=event, name="Ticket", default_price=10, active=True)


def _retrieve(event, key):
    request = RequestFactory().get("/", HTTP_EXHIBITOR=key or "")
    request.event = event
    view = VoucherRedemptionRetrieveView()
    view.request = request
    return view.get(request, organizer=event.organizer.slug, event=event.slug)


POOL = "exhibitor-pool"


def _pool(event, count=5, *, tag=POOL, product=None):
    """Vouchers created in Tickets and tagged as a pool, as an organizer would."""
    return [Voucher.objects.create(event=event, product=product, tag=tag) for _ in range(count)]


@pytest.mark.django_db
def test_claim_pool_vouchers_links_codes_without_creating_any(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_pool_tag=POOL)
        _pool(event, 5)
        exhibitor = _exhibitor(event)

        claimed = claim_pool_vouchers(exhibitor, 3)

        assert len(claimed) == 3
        assert ExhibitorVoucher.objects.filter(exhibitor=exhibitor).count() == 3
        assert Voucher.objects.filter(event=event).count() == 5
        assert pool_remaining(event, POOL) == 2


@pytest.mark.django_db
def test_claim_pool_vouchers_takes_nothing_when_the_pool_is_short(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_pool_tag=POOL)
        _pool(event, 2)
        exhibitor = _exhibitor(event)

        assert claim_pool_vouchers(exhibitor, 3) == []
        assert pool_remaining(event, POOL) == 2


@pytest.mark.django_db
def test_claim_pool_vouchers_never_hands_out_the_same_code_twice(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_pool_tag=POOL)
        _pool(event, 5)
        first = _exhibitor(event, name="First")
        second = _exhibitor(event, name="Second")

        claim_pool_vouchers(first, 3)
        claim_pool_vouchers(second, 2)

        codes = {link.voucher_id for link in ExhibitorVoucher.objects.all()}
        assert len(codes) == 5
        assert pool_remaining(event, POOL) == 0


@pytest.mark.django_db
def test_claim_pool_vouchers_ignores_other_pools(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_pool_tag=POOL)
        _pool(event, 2, tag="something-else")
        exhibitor = _exhibitor(event)

        assert claim_pool_vouchers(exhibitor, 1) == []


@pytest.mark.django_db
def test_pool_remaining_is_zero_without_a_pool(event):
    with scopes_disabled():
        assert pool_remaining(event, "") == 0


def test_batch_form_accepts_zero_to_email_existing_codes():
    assert ExhibitorVoucherBatchForm(data={"count": 0}).is_valid()
    assert not ExhibitorVoucherBatchForm(data={"count": -1}).is_valid()


@pytest.mark.django_db
def test_pool_tag_choices_lists_event_tags_once(event):
    with scopes_disabled():
        _pool(event, 3, tag="gold")
        _pool(event, 2, tag="silver")
        Voucher.objects.create(event=event, tag="")

        assert pool_tag_choices(event) == ["gold", "silver"]


@pytest.mark.django_db
def test_voucher_defaults_form_offers_the_event_pools(event):
    with scopes_disabled():
        _pool(event, 1, tag="gold")
        form = ExhibitorVoucherDefaultsForm(event=event)

        assert [value for value, _label in form.fields["voucher_pool_tag"].choices] == ["", "gold"]
        assert [value for value, _label in form.fields["sponsor_voucher_pool_tag"].choices] == ["", "gold"]


@pytest.mark.django_db
def test_voucher_defaults_form_keeps_a_pool_that_no_longer_has_vouchers(event):
    with scopes_disabled():
        settings = ExhibitorSettings.objects.create(event=event, voucher_pool_tag="emptied")
        form = ExhibitorVoucherDefaultsForm(instance=settings, event=event)

        assert "emptied" in [value for value, _label in form.fields["voucher_pool_tag"].choices]


@pytest.mark.django_db
def test_resolve_voucher_defaults_prefers_sponsor_group_over_event_settings(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_default_count=2)
        group = SponsorGroup.objects.create(event=event, name="Gold", voucher_default_count=7)
        assert resolve_voucher_defaults(_exhibitor(event, sponsor_group=group))["count"] == 7
        assert resolve_voucher_defaults(_exhibitor(event, name="Ungrouped"))["count"] == 2


@pytest.mark.django_db
def test_resolve_voucher_defaults_falls_back_without_settings_row(event):
    with scopes_disabled():
        defaults = resolve_voucher_defaults(_exhibitor(event))
        assert defaults["count"] == 1
        assert defaults["pool_tag"] == ""
        assert not ExhibitorSettings.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_sponsors_use_the_sponsor_pool_when_one_is_set(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(
            event=event, voucher_pool_tag="exhibitors", sponsor_voucher_pool_tag="sponsors"
        )
        sponsor = _exhibitor(event, name="Gold", is_exhibitor=False, is_sponsor=True)
        booth = _exhibitor(event, name="Booth", is_exhibitor=True, is_sponsor=False)

        assert resolve_voucher_defaults(sponsor)["pool_tag"] == "sponsors"
        assert resolve_voucher_defaults(booth)["pool_tag"] == "exhibitors"


@pytest.mark.django_db
def test_sponsors_share_the_exhibitor_pool_when_no_sponsor_pool_is_set(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=event, voucher_pool_tag="shared")
        sponsor = _exhibitor(event, name="Gold", is_exhibitor=False, is_sponsor=True)

        assert resolve_voucher_defaults(sponsor)["pool_tag"] == "shared"


@pytest.mark.django_db
def test_a_partner_that_is_both_draws_from_the_exhibitor_pool(event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(
            event=event, voucher_pool_tag="exhibitors", sponsor_voucher_pool_tag="sponsors"
        )
        both = _exhibitor(event, name="Both", is_exhibitor=True, is_sponsor=True)

        assert resolve_voucher_defaults(both)["pool_tag"] == "exhibitors"


def _settings(*allowed):
    allowed = set(allowed)
    return SimpleNamespace(is_field_allowed=lambda key: key in allowed)


def _position():
    no_questions = SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(order_by=lambda *args: []))
    return SimpleNamespace(
        attendee_name="N",
        attendee_email="e@x.com",
        company="Acme",
        job_title="",
        street="",
        zipcode="",
        city="",
        country="",
        answers=SimpleNamespace(all=lambda: []),
        order=SimpleNamespace(event=SimpleNamespace(questions=no_questions)),
    )


def test_attendee_data_gates_company_when_not_allowed():
    data = get_allowed_attendee_data(_position(), _settings("attendee_name", "attendee_email"))
    assert data == {"name": "N", "email": "e@x.com"}


def test_attendee_data_includes_company_when_allowed():
    data = get_allowed_attendee_data(_position(), _settings("attendee_name", "attendee_email", "system_company"))
    assert data["company"] == "Acme"


@pytest.mark.django_db
def test_retrieve_rejects_invalid_key(event):
    with scopes_disabled():
        response = _retrieve(event, "nope")
        assert response.status_code == 401


@pytest.mark.django_db
def test_retrieve_forbidden_when_access_disabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_voucher_access=False)
        response = _retrieve(event, exhibitor.key)
        assert response.status_code == 403


@pytest.mark.django_db
def test_retrieve_allows_when_access_enabled(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event, allow_voucher_access=True)
        response = _retrieve(event, exhibitor.key)
        assert response.status_code == 200
        assert response.data["success"] is True
        assert response.data["redemptions"] == []


@pytest.fixture
def voucher_event(event):
    """Event with the plugin enabled, so voucher email placeholders are dispatched."""
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    return event


def _mailed_exhibitor(event, **kwargs):
    kwargs.setdefault("email", "acme@example.com")
    return _exhibitor(event, **kwargs)


def _issue(exhibitor, count=2, *, product=None):
    """Give this exhibitor `count` codes, topping the pool up first so the claim always succeeds."""
    _pool(exhibitor.event, count, product=product)
    claim_pool_vouchers(exhibitor, count, pool_tag=POOL)
    return [link.voucher for link in ExhibitorVoucher.objects.filter(exhibitor=exhibitor)]


@pytest.mark.django_db
def test_voucher_redeem_url_carries_the_code(voucher_event):
    with scopes_disabled():
        voucher = _issue(_mailed_exhibitor(voucher_event), count=1)[0]
        url = voucher_redeem_url(voucher_event, voucher)

    assert f"voucher={voucher.code}" in url
    assert "/redeem" in url
    assert "subevent=" not in url


@pytest.mark.django_db
def test_voucher_redeem_url_carries_the_subevent(voucher_event):
    with scopes_disabled():
        voucher = _issue(_mailed_exhibitor(voucher_event), count=1)[0]
        voucher.subevent_id = 42
        url = voucher_redeem_url(voucher_event, voucher)

    assert "&subevent=42" in url


@pytest.mark.django_db
def test_format_voucher_list_renders_a_block_per_voucher(voucher_event):
    with scopes_disabled():
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=3)
        rendered = mail_helpers.format_voucher_list(vouchers, event=voucher_event)

    for voucher in vouchers:
        assert voucher.code in rendered
        assert voucher_redeem_url(voucher_event, voucher) in rendered


@pytest.mark.django_db
def test_queue_voucher_email_resolves_placeholders_with_the_vouchers_role(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        vouchers = _issue(exhibitor)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, vouchers)

    assert queued.role == mail_helpers.VOUCHERS
    assert queued.to_email == "acme@example.com"
    assert queued.sent_at is None
    assert "{voucher_list}" not in queued.body
    assert "{exhibitor_name}" not in queued.body
    assert vouchers[0].code in queued.body


@pytest.mark.django_db
def test_queue_voucher_email_returns_none_without_an_address(voucher_event):
    with scopes_disabled():
        exhibitor = _exhibitor(voucher_event, email="")
        assert mail_helpers.queue_voucher_email(voucher_event, exhibitor, _issue(exhibitor)) is None


@pytest.mark.django_db
def test_build_voucher_csv_writes_a_header_and_one_row_each(voucher_event):
    with scopes_disabled():
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=3)
        payload = build_voucher_csv(voucher_event, vouchers)

    rows = [row for row in payload.splitlines() if row.strip()]
    assert len(rows) == 4
    assert "Voucher code" in rows[0]
    for voucher in vouchers:
        assert voucher.code in payload
        assert voucher_redeem_url(voucher_event, voucher) in payload


@pytest.mark.django_db
def test_build_voucher_csv_names_the_product(voucher_event):
    with scopes_disabled():
        product = _product(voucher_event)
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=1, product=product)
        payload = build_voucher_csv(voucher_event, vouchers)

    assert str(product) in payload


@pytest.mark.django_db
def test_store_voucher_csv_persists_a_private_cached_file(voucher_event):
    with scopes_disabled():
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=2)
        cached = store_voucher_csv(voucher_event, vouchers)

    assert cached.filename == VOUCHER_CSV_FILENAME
    assert cached.type == "text/csv"
    assert cached.web_download is False
    assert cached.expires is not None
    assert vouchers[0].code in cached.file.read().decode("utf-8")


@pytest.mark.django_db
def test_queue_voucher_email_attaches_the_csv_by_default(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        vouchers = _issue(exhibitor)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, vouchers)

        assert queued.attachment is not None
        assert vouchers[0].code in queued.attachment.file.read().decode("utf-8")


@pytest.mark.django_db
def test_queue_voucher_email_omits_the_csv_when_the_setting_is_off(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_attach_csv=False)
        exhibitor = _mailed_exhibitor(voucher_event)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, _issue(exhibitor))

    assert queued.attachment is None


@pytest.mark.django_db
def test_send_forwards_the_attachment_to_core_mail(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, _issue(exhibitor))

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        queued.send()

    assert mocked_mail.call_args.kwargs["attach_cached_files"] == [queued.attachment_id]


@pytest.mark.django_db
def test_send_passes_no_attachment_when_there_is_none(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_attach_csv=False)
        exhibitor = _mailed_exhibitor(voucher_event)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, _issue(exhibitor))

    with patch("eventyay.base.services.mail.mail") as mocked_mail:
        queued.send()

    assert mocked_mail.call_args.kwargs["attach_cached_files"] is None


@pytest.mark.django_db
def test_claim_default_vouchers_takes_the_resolved_count_from_the_pool(voucher_event):
    with scopes_disabled():
        product = _product(voucher_event)
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4, voucher_pool_tag=POOL)
        _pool(voucher_event, 6, product=product)
        exhibitor = _mailed_exhibitor(voucher_event)

        claimed = mail_helpers.claim_default_vouchers(exhibitor)

        assert len(claimed) == 4
        assert pool_remaining(voucher_event, POOL) == 2
        assert ExhibitorVoucher.objects.filter(exhibitor=exhibitor).first().voucher.product == product


@pytest.mark.django_db
def test_claim_default_vouchers_prefers_the_sponsor_group_count(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=1, voucher_pool_tag=POOL)
        _pool(voucher_event, 10)
        group = SponsorGroup.objects.create(event=voucher_event, name="Gold", voucher_default_count=5)
        exhibitor = _mailed_exhibitor(voucher_event, sponsor_group=group)

        assert len(mail_helpers.claim_default_vouchers(exhibitor)) == 5


@pytest.mark.django_db
def test_claim_default_vouchers_takes_nothing_when_the_count_is_zero(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=0, voucher_pool_tag=POOL)
        _pool(voucher_event, 5)
        exhibitor = _mailed_exhibitor(voucher_event)

        assert mail_helpers.claim_default_vouchers(exhibitor) == []
        assert pool_remaining(voucher_event, POOL) == 5


@pytest.mark.django_db
def test_claim_default_vouchers_takes_nothing_when_the_pool_is_short(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4, voucher_pool_tag=POOL)
        _pool(voucher_event, 2)
        exhibitor = _mailed_exhibitor(voucher_event)

        assert mail_helpers.claim_default_vouchers(exhibitor) == []
        assert pool_remaining(voucher_event, POOL) == 2


@pytest.mark.django_db
def test_queue_voucher_emails_skips_the_voucherless_without_issue_missing(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        queued, skipped = mail_helpers.queue_voucher_emails(voucher_event, [exhibitor])

    assert queued == []
    assert skipped[mail_helpers.VOUCHER_SKIP_NO_VOUCHERS] == [exhibitor]


@pytest.mark.django_db
def test_queue_voucher_emails_claims_from_the_pool_when_missing(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=3, voucher_pool_tag=POOL)
        _pool(voucher_event, 3)
        exhibitor = _mailed_exhibitor(voucher_event)

        queued, skipped = mail_helpers.queue_voucher_emails(voucher_event, [exhibitor], issue_missing=True)
        codes = [link.voucher.code for link in ExhibitorVoucher.objects.filter(exhibitor=exhibitor)]

    assert len(queued) == 1
    assert skipped[mail_helpers.VOUCHER_SKIP_NO_VOUCHERS] == []
    assert len(codes) == 3
    assert all(code in queued[0].body for code in codes)


@pytest.mark.django_db
def test_queue_voucher_emails_does_not_top_up_existing_vouchers(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=5, voucher_pool_tag=POOL)
        exhibitor = _mailed_exhibitor(voucher_event)
        _issue(exhibitor, count=2)

        mail_helpers.queue_voucher_emails(voucher_event, [exhibitor], issue_missing=True)

        assert ExhibitorVoucher.objects.filter(exhibitor=exhibitor).count() == 2


@pytest.mark.django_db
def test_queue_voucher_emails_still_skips_a_zero_default(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=0, voucher_pool_tag=POOL)
        _pool(voucher_event, 5)
        exhibitor = _mailed_exhibitor(voucher_event)

        queued, skipped = mail_helpers.queue_voucher_emails(voucher_event, [exhibitor], issue_missing=True)

    assert queued == []
    assert skipped[mail_helpers.VOUCHER_SKIP_NO_VOUCHERS] == [exhibitor]


@pytest.mark.django_db
def test_queue_voucher_emails_reports_the_addressless_separately(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2, voucher_pool_tag=POOL)
        _pool(voucher_event, 2)
        no_address = _exhibitor(voucher_event, name="No Address", email="")
        mailable = _mailed_exhibitor(voucher_event, name="Acme")

        queued, skipped = mail_helpers.queue_voucher_emails(voucher_event, [no_address, mailable], issue_missing=True)

    assert len(queued) == 1
    assert skipped[mail_helpers.VOUCHER_SKIP_NO_EMAIL] == [no_address]
    assert not ExhibitorVoucher.objects.filter(exhibitor=no_address).exists()


def _bulk_view(event, partner_type="exhibitor", *, data=None):
    request = RequestFactory().post("/vouchers/send", data=data or {})
    request.event = event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)
    view = ExhibitorVoucherBulkSendView()
    view.request = request
    view.partner_type = partner_type
    return view, request


@pytest.mark.django_db
def test_bulk_preview_counts_existing_vouchers(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        _issue(exhibitor, count=3)
        view, _request = _bulk_view(voucher_event)
        sendable, no_email, no_vouchers, pool_short = view.preview([exhibitor])

    assert sendable == [exhibitor]
    assert (no_email, no_vouchers, pool_short) == ([], [], [])
    assert exhibitor.voucher_total == 3
    assert exhibitor.voucher_new == 0


@pytest.mark.django_db
def test_bulk_preview_keeps_the_voucherless_sendable(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4, voucher_pool_tag=POOL)
        _pool(voucher_event, 4)
        exhibitor = _mailed_exhibitor(voucher_event)
        view, _request = _bulk_view(voucher_event)
        sendable, _no_email, no_vouchers, _pool_short = view.preview([exhibitor])

    assert sendable == [exhibitor]
    assert no_vouchers == []
    assert exhibitor.voucher_new == 4
    assert exhibitor.voucher_total == 4


@pytest.mark.django_db
def test_bulk_preview_creates_nothing(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4, voucher_pool_tag=POOL)
        _pool(voucher_event, 4)
        exhibitor = _mailed_exhibitor(voucher_event)
        view, _request = _bulk_view(voucher_event)
        view.preview([exhibitor])

        assert not ExhibitorVoucher.objects.filter(exhibitor=exhibitor).exists()


@pytest.mark.django_db
def test_bulk_preview_sorts_the_unsendable_into_buckets(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=0, voucher_pool_tag=POOL)
        no_address = _exhibitor(voucher_event, name="No Address", email="")
        zero_default = _mailed_exhibitor(voucher_event, name="Zero Default")
        view, _request = _bulk_view(voucher_event)
        sendable, no_email, no_vouchers, _pool_short = view.preview([no_address, zero_default])

    assert sendable == []
    assert no_email == [no_address]
    assert no_vouchers == [zero_default]


@pytest.mark.django_db
def test_bulk_send_issues_defaults_and_queues_one_email_each(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2, voucher_pool_tag=POOL)
        _pool(voucher_event, 4)
        first = _mailed_exhibitor(voucher_event, name="First", email="first@example.com")
        second = _mailed_exhibitor(voucher_event, name="Second", email="second@example.com")
        view, request = _bulk_view(voucher_event, data={"confirmed": "1"})

        response = view.post(request)

        outbox = ExhibitionEmailQueue.objects.filter(event=voucher_event, role=mail_helpers.VOUCHERS)
        assert response.status_code == 302
        assert {row.to_email for row in outbox} == {"first@example.com", "second@example.com"}
        assert all(row.sent_at is None for row in outbox)
        assert ExhibitorVoucher.objects.filter(exhibitor=first).count() == 2
        assert ExhibitorVoucher.objects.filter(exhibitor=second).count() == 2
        assert pool_remaining(voucher_event, POOL) == 0


@pytest.mark.django_db
def test_bulk_send_only_targets_its_own_partner_type(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=1, voucher_pool_tag=POOL)
        _pool(voucher_event, 4)
        _exhibitor(voucher_event, name="Booth", email="booth@example.com", is_exhibitor=True, is_sponsor=False)
        _exhibitor(voucher_event, name="Gold", email="gold@example.com", is_exhibitor=False, is_sponsor=True)
        view, request = _bulk_view(voucher_event, partner_type="sponsor", data={"confirmed": "1"})

        view.post(request)

        outbox = ExhibitionEmailQueue.objects.filter(event=voucher_event, role=mail_helpers.VOUCHERS)
        assert [row.to_email for row in outbox] == ["gold@example.com"]


@pytest.mark.django_db
def test_bulk_send_queues_nothing_when_nobody_is_reachable(voucher_event):
    with scopes_disabled():
        _exhibitor(voucher_event, name="No Address", email="")
        view, request = _bulk_view(voucher_event, data={"confirmed": "1"})

        response = view.post(request)

        assert response.status_code == 302
        assert not ExhibitionEmailQueue.objects.filter(event=voucher_event).exists()


@pytest.mark.django_db
def test_format_voucher_list_lists_everything_without_a_limit(voucher_event):
    with scopes_disabled():
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=8)
        rendered = mail_helpers.format_voucher_list(vouchers, event=voucher_event)

    assert all(voucher.code in rendered for voucher in vouchers)
    assert "more voucher codes" not in rendered


@pytest.mark.django_db
def test_format_voucher_list_caps_and_summarises_the_rest(voucher_event):
    with scopes_disabled():
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=8)
        rendered = mail_helpers.format_voucher_list(vouchers, event=voucher_event, limit=5)

    assert all(voucher.code in rendered for voucher in vouchers[:5])
    assert not any(voucher.code in rendered for voucher in vouchers[5:])
    assert "3 more voucher codes" in rendered


@pytest.mark.django_db
def test_format_voucher_list_does_not_summarise_when_exactly_at_the_limit(voucher_event):
    with scopes_disabled():
        vouchers = _issue(_mailed_exhibitor(voucher_event), count=5)
        rendered = mail_helpers.format_voucher_list(vouchers, event=voucher_event, limit=5)

    assert all(voucher.code in rendered for voucher in vouchers)
    assert "more voucher" not in rendered


@pytest.mark.django_db
def test_voucher_email_caps_the_inline_list_when_the_csv_is_attached(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        vouchers = _issue(exhibitor, count=9)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, vouchers)

        assert queued.attachment is not None
        listed = [voucher for voucher in vouchers if voucher.code in queued.body]
        assert len(listed) == mail_helpers.VOUCHER_LIST_INLINE_LIMIT
        assert "4 more voucher codes" in queued.body

        payload = queued.attachment.file.read().decode("utf-8")
        assert all(voucher.code in payload for voucher in vouchers)


@pytest.mark.django_db
def test_voucher_email_lists_them_all_when_the_csv_is_off(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_attach_csv=False)
        exhibitor = _mailed_exhibitor(voucher_event)
        vouchers = _issue(exhibitor, count=9)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, vouchers)

    assert queued.attachment is None
    assert all(voucher.code in queued.body for voucher in vouchers)
    assert "more voucher" not in queued.body


@pytest.mark.django_db
def test_voucher_email_under_the_limit_is_unchanged_by_the_cap(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        vouchers = _issue(exhibitor, count=3)
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, vouchers)

    assert all(voucher.code in queued.body for voucher in vouchers)
    assert "more voucher" not in queued.body


@pytest.mark.django_db
def test_bulk_preview_skips_whoever_the_pool_cannot_cover(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4, voucher_pool_tag=POOL)
        _pool(voucher_event, 6)
        first = _mailed_exhibitor(voucher_event, name="First", email="first@example.com")
        second = _mailed_exhibitor(voucher_event, name="Second", email="second@example.com")
        view, _request = _bulk_view(voucher_event)

        sendable, _no_email, _no_vouchers, pool_short = view.preview([first, second])

    assert sendable == [first]
    assert pool_short == [second]


@pytest.mark.django_db
def test_bulk_preview_draws_the_pool_down_across_the_run(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2, voucher_pool_tag=POOL)
        _pool(voucher_event, 5)
        people = [
            _mailed_exhibitor(voucher_event, name=f"Org {index}", email=f"org{index}@example.com") for index in range(3)
        ]
        view, _request = _bulk_view(voucher_event)

        sendable, _no_email, _no_vouchers, pool_short = view.preview(people)

    assert len(sendable) == 2
    assert pool_short == [people[2]]


@pytest.mark.django_db
def test_bulk_preview_skips_everyone_when_no_pool_is_chosen(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2)
        exhibitor = _mailed_exhibitor(voucher_event)
        view, _request = _bulk_view(voucher_event)

        sendable, _no_email, _no_vouchers, pool_short = view.preview([exhibitor])

    assert sendable == []
    assert pool_short == [exhibitor]


@pytest.mark.django_db
def test_bulk_send_leaves_the_pool_alone_for_whoever_it_skips(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4, voucher_pool_tag=POOL)
        _pool(voucher_event, 6)
        _mailed_exhibitor(voucher_event, name="First", email="first@example.com")
        _mailed_exhibitor(voucher_event, name="Second", email="second@example.com")
        view, request = _bulk_view(voucher_event, data={"confirmed": "1"})

        view.post(request)

        assert ExhibitionEmailQueue.objects.filter(event=voucher_event).count() == 1
        assert pool_remaining(voucher_event, POOL) == 2


def _applied_via(exhibitor, *, login, contact=""):
    """Approve a proposal onto this exhibitor, as the call-for-exhibitors flow does."""
    user = User.objects.create_user(email=login, password="pw")
    return ExhibitionProposal.objects.create(
        event=exhibitor.event,
        user=user,
        name="Acme Corp",
        email=contact,
        state=ExhibitionProposalState.ACCEPTED,
        approved_exhibitor=exhibitor,
    )


@pytest.mark.django_db
def test_recipient_email_prefers_the_stored_address(voucher_event):
    with scopes_disabled():
        exhibitor = _exhibitor(voucher_event, email="stored@example.com")
        _applied_via(exhibitor, login="login@example.com")

        assert exhibitor.recipient_email == "stored@example.com"


@pytest.mark.django_db
def test_recipient_email_falls_back_to_the_login_address(voucher_event):
    with scopes_disabled():
        exhibitor = _exhibitor(voucher_event, email="")
        _applied_via(exhibitor, login="login@example.com")

        assert exhibitor.recipient_email == "login@example.com"


@pytest.mark.django_db
def test_recipient_email_prefers_the_proposal_contact_over_the_login(voucher_event):
    with scopes_disabled():
        exhibitor = _exhibitor(voucher_event, email="")
        _applied_via(exhibitor, login="login@example.com", contact="contact@example.com")

        assert exhibitor.recipient_email == "contact@example.com"


@pytest.mark.django_db
def test_recipient_email_is_blank_for_a_manually_added_partner(voucher_event):
    with scopes_disabled():
        assert _exhibitor(voucher_event, email="").recipient_email == ""


@pytest.mark.django_db
def test_voucher_email_goes_to_the_login_address_when_none_is_stored(voucher_event):
    with scopes_disabled():
        exhibitor = _exhibitor(voucher_event, email="")
        _applied_via(exhibitor, login="login@example.com")
        queued = mail_helpers.queue_voucher_email(voucher_event, exhibitor, _issue(exhibitor))

    assert queued is not None
    assert queued.to_email == "login@example.com"


@pytest.mark.django_db
def test_bulk_send_reaches_partners_with_only_a_login_address(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2, voucher_pool_tag=POOL)
        _pool(voucher_event, 2)
        exhibitor = _exhibitor(voucher_event, email="")
        _applied_via(exhibitor, login="login@example.com")
        view, request = _bulk_view(voucher_event, data={"confirmed": "1"})

        view.post(request)

        outbox = ExhibitionEmailQueue.objects.filter(event=voucher_event, role=mail_helpers.VOUCHERS)
        assert [row.to_email for row in outbox] == ["login@example.com"]


@pytest.mark.django_db
def test_a_voucher_cannot_be_linked_to_two_exhibitors(voucher_event):
    """The pool's real guarantee: the link table refuses a code that is already handed out."""
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_pool_tag=POOL)
        voucher = _pool(voucher_event, 1)[0]
        first = _exhibitor(voucher_event, name="First")
        second = _exhibitor(voucher_event, name="Second")

        ExhibitorVoucher.objects.create(exhibitor=first, voucher=voucher)
        with pytest.raises(IntegrityError), transaction.atomic():
            ExhibitorVoucher.objects.create(exhibitor=second, voucher=voucher)


@pytest.mark.django_db
def test_a_claimed_code_leaves_the_pool_for_everyone_else(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_pool_tag=POOL)
        _pool(voucher_event, 3)
        first = _exhibitor(voucher_event, name="First")
        second = _exhibitor(voucher_event, name="Second")

        taken = {link.voucher_id for link in claim_pool_vouchers(first, 2, pool_tag=POOL)}
        left = {link.voucher_id for link in claim_pool_vouchers(second, 1, pool_tag=POOL)}

    assert len(taken) == 2
    assert len(left) == 1
    assert not taken & left


def _voucher_view(exhibitor, data):
    request = RequestFactory().post("/vouchers", data=data)
    request.event = exhibitor.event
    request.user = None
    request.session = {}
    request._messages = FallbackStorage(request)
    view = ExhibitorVoucherManageView()
    view.request = request
    view.object = exhibitor
    view.kwargs = {"pk": exhibitor.pk}
    return view, request


@pytest.mark.django_db
def test_an_unemailed_code_goes_back_to_the_pool(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_pool_tag=POOL)
        exhibitor = _mailed_exhibitor(voucher_event)
        _issue(exhibitor, count=2)
        link = ExhibitorVoucher.objects.filter(exhibitor=exhibitor).first()
        view, request = _voucher_view(exhibitor, {"action": "delete", "voucher": link.pk})

        view.remove_voucher(request)

        assert not ExhibitorVoucher.objects.filter(pk=link.pk).exists()
        assert pool_remaining(voucher_event, POOL) == 1


@pytest.mark.django_db
def test_an_emailed_code_cannot_go_back_to_the_pool(voucher_event):
    """Returning it would hand the code to someone else while the first partner still holds it."""
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_pool_tag=POOL)
        exhibitor = _mailed_exhibitor(voucher_event)
        vouchers = _issue(exhibitor, count=2)
        mail_helpers.queue_voucher_email(voucher_event, exhibitor, vouchers)
        link = ExhibitorVoucher.objects.filter(exhibitor=exhibitor).first()
        view, request = _voucher_view(exhibitor, {"action": "delete", "voucher": link.pk})

        view.remove_voucher(request)

        assert ExhibitorVoucher.objects.filter(pk=link.pk).exists()
        assert pool_remaining(voucher_event, POOL) == 0


@pytest.mark.django_db
def test_a_code_assigned_after_the_last_email_still_goes_back(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_pool_tag=POOL)
        exhibitor = _mailed_exhibitor(voucher_event)
        mail_helpers.queue_voucher_email(voucher_event, exhibitor, _issue(exhibitor, count=1))
        _pool(voucher_event, 1)
        later = claim_pool_vouchers(exhibitor, 1, pool_tag=POOL)[0]
        view, request = _voucher_view(exhibitor, {"action": "delete", "voucher": later.pk})

        view.remove_voucher(request)

        assert not ExhibitorVoucher.objects.filter(pk=later.pk).exists()


@pytest.mark.django_db
def test_pool_lookup_ignores_links_from_other_events(voucher_event):
    with scopes_disabled():
        other = Organizer.objects.create(name="Other", slug="other-org")
        other_event = Event.objects.create(
            organizer=other, name="Other Event", slug="other-event", live=True, date_from=now()
        )
        _pool(other_event, 2)
        other_exhibitor = _exhibitor(other_event, name="Elsewhere")
        claim_pool_vouchers(other_exhibitor, 2, pool_tag=POOL)

        _pool(voucher_event, 3)

        assert pool_remaining(voucher_event, POOL) == 3
