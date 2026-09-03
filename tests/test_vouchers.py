from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django_scopes import scopes_disabled
from eventyay.base.models import PriceModeChoices, Product, Voucher

from exhibition import mail as mail_helpers
from exhibition.api import VoucherRedemptionRetrieveView, get_allowed_attendee_data
from exhibition.forms import ExhibitorVoucherBatchForm, ExhibitorVoucherDefaultsForm
from exhibition.models import (
    ExhibitionEmailQueue,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    SponsorGroup,
)
from exhibition.utils import (
    VOUCHER_CSV_FILENAME,
    build_voucher_csv,
    generate_exhibitor_vouchers,
    resolve_voucher_defaults,
    store_voucher_csv,
    voucher_redeem_url,
)
from exhibition.views import ExhibitorVoucherBulkSendView


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


@pytest.mark.django_db
def test_generate_exhibitor_vouchers_creates_links_and_vouchers(event):
    with scopes_disabled():
        exhibitor = _exhibitor(event)
        product = _product(event)
        generate_exhibitor_vouchers(
            exhibitor,
            product=product,
            count=3,
            price_mode=PriceModeChoices.PERCENT,
            value=100,
        )
        links = ExhibitorVoucher.objects.filter(exhibitor=exhibitor)
        assert links.count() == 3
        voucher = links.first().voucher
        assert voucher.max_usages == 1
        assert voucher.price_mode == PriceModeChoices.PERCENT
        assert voucher.tag == f"exhibitor-{exhibitor.key}"
        assert Voucher.objects.filter(event=event).count() == 3


def test_batch_form_accepts_zero_to_email_existing_codes():
    assert ExhibitorVoucherBatchForm(data={"count": 0}).is_valid()
    assert not ExhibitorVoucherBatchForm(data={"count": -1}).is_valid()


@pytest.mark.django_db
def test_voucher_defaults_form_requires_value_for_non_none_price_mode(event):
    with scopes_disabled():
        form = ExhibitorVoucherDefaultsForm(
            data={"voucher_default_count": 1, "voucher_default_price_mode": PriceModeChoices.PERCENT},
            event=event,
        )
        assert not form.is_valid()
        assert "voucher_default_value" in form.errors


@pytest.mark.django_db
def test_voucher_defaults_form_limits_products_to_event(event):
    with scopes_disabled():
        product = _product(event)
        form = ExhibitorVoucherDefaultsForm(event=event)
        assert list(form.fields["voucher_default_product"].queryset) == [product]


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
        assert defaults["product"] is None
        assert not ExhibitorSettings.objects.filter(event=event).exists()


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


def _issue(exhibitor, count=2, *, product=None, price_mode=PriceModeChoices.NONE, value=None):
    generate_exhibitor_vouchers(exhibitor, product=product, count=count, price_mode=price_mode, value=value)
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
def test_issue_default_vouchers_uses_the_resolved_defaults(voucher_event):
    with scopes_disabled():
        product = _product(voucher_event)
        ExhibitorSettings.objects.create(
            event=voucher_event,
            voucher_default_count=4,
            voucher_default_product=product,
            voucher_default_price_mode=PriceModeChoices.PERCENT,
            voucher_default_value=50,
        )
        exhibitor = _mailed_exhibitor(voucher_event)
        created = mail_helpers.issue_default_vouchers(exhibitor)
        vouchers = [link.voucher for link in ExhibitorVoucher.objects.filter(exhibitor=exhibitor)]

    assert len(created) == 4
    assert len(vouchers) == 4
    assert vouchers[0].product == product
    assert vouchers[0].price_mode == PriceModeChoices.PERCENT


@pytest.mark.django_db
def test_issue_default_vouchers_prefers_the_sponsor_group(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=1)
        group = SponsorGroup.objects.create(event=voucher_event, name="Gold", voucher_default_count=5)
        exhibitor = _mailed_exhibitor(voucher_event, sponsor_group=group)

        assert len(mail_helpers.issue_default_vouchers(exhibitor)) == 5


@pytest.mark.django_db
def test_issue_default_vouchers_creates_nothing_when_the_default_is_zero(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=0)
        exhibitor = _mailed_exhibitor(voucher_event)

        assert mail_helpers.issue_default_vouchers(exhibitor) == []
        assert not ExhibitorVoucher.objects.filter(exhibitor=exhibitor).exists()


@pytest.mark.django_db
def test_queue_voucher_emails_skips_the_voucherless_without_issue_missing(voucher_event):
    with scopes_disabled():
        exhibitor = _mailed_exhibitor(voucher_event)
        queued, skipped = mail_helpers.queue_voucher_emails(voucher_event, [exhibitor])

    assert queued == []
    assert skipped[mail_helpers.VOUCHER_SKIP_NO_VOUCHERS] == [exhibitor]


@pytest.mark.django_db
def test_queue_voucher_emails_issues_defaults_when_missing(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=3)
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
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=5)
        exhibitor = _mailed_exhibitor(voucher_event)
        _issue(exhibitor, count=2)

        mail_helpers.queue_voucher_emails(voucher_event, [exhibitor], issue_missing=True)

        assert ExhibitorVoucher.objects.filter(exhibitor=exhibitor).count() == 2


@pytest.mark.django_db
def test_queue_voucher_emails_still_skips_a_zero_default(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=0)
        exhibitor = _mailed_exhibitor(voucher_event)

        queued, skipped = mail_helpers.queue_voucher_emails(voucher_event, [exhibitor], issue_missing=True)

    assert queued == []
    assert skipped[mail_helpers.VOUCHER_SKIP_NO_VOUCHERS] == [exhibitor]


@pytest.mark.django_db
def test_queue_voucher_emails_reports_the_addressless_separately(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2)
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
        sendable, no_email, no_vouchers = view.preview([exhibitor])

    assert sendable == [exhibitor]
    assert (no_email, no_vouchers) == ([], [])
    assert exhibitor.voucher_total == 3
    assert exhibitor.voucher_new == 0


@pytest.mark.django_db
def test_bulk_preview_keeps_the_voucherless_sendable(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4)
        exhibitor = _mailed_exhibitor(voucher_event)
        view, _request = _bulk_view(voucher_event)
        sendable, _no_email, no_vouchers = view.preview([exhibitor])

    assert sendable == [exhibitor]
    assert no_vouchers == []
    assert exhibitor.voucher_new == 4
    assert exhibitor.voucher_total == 4


@pytest.mark.django_db
def test_bulk_preview_creates_nothing(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=4)
        exhibitor = _mailed_exhibitor(voucher_event)
        view, _request = _bulk_view(voucher_event)
        view.preview([exhibitor])

        assert not ExhibitorVoucher.objects.filter(exhibitor=exhibitor).exists()


@pytest.mark.django_db
def test_bulk_preview_sorts_the_unsendable_into_buckets(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=0)
        no_address = _exhibitor(voucher_event, name="No Address", email="")
        zero_default = _mailed_exhibitor(voucher_event, name="Zero Default")
        view, _request = _bulk_view(voucher_event)
        sendable, no_email, no_vouchers = view.preview([no_address, zero_default])

    assert sendable == []
    assert no_email == [no_address]
    assert no_vouchers == [zero_default]


@pytest.mark.django_db
def test_bulk_send_issues_defaults_and_queues_one_email_each(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=2)
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


@pytest.mark.django_db
def test_bulk_send_only_targets_its_own_partner_type(voucher_event):
    with scopes_disabled():
        ExhibitorSettings.objects.create(event=voucher_event, voucher_default_count=1)
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
