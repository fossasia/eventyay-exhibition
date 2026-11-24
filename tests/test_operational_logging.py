"""Privacy, fail-open, and call-site behavior of exhibition operational logs."""

import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from celery.exceptions import MaxRetriesExceededError

import exhibition.operational_log as operational_log
from exhibition.operational_log import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    log_operation,
    log_plugin_loaded,
)


@pytest.fixture
def captured(monkeypatch):
    records = []

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)
    return records


def test_drops_email_token_and_unknown_fields(captured):
    log_operation(
        "exhibition.order.place",
        OUTCOME_SUCCESS,
        backend="exhibition",
        email="person@example.com",
        token="secret-token",
        body="{raw}",
        webhook_id=99,
        raw_meta="Order",
        event_id=12,
        order_id=34,
        order_code="EXH-1",
        payment_provider="banktransfer",
        voucher_id=56,
        recipient_count=3,
        retry_count=1,
        duration_ms=42,
        job_name="exhibition.send_scheduled_email",
        mail_type="vouchers",
        route="plugins:exhibition:exhibitor-auth",
    )
    assert len(captured) == 1
    extra = captured[0][2]
    assert extra["backend"] == "exhibition"
    assert extra["event_id"] == 12
    assert extra["order_id"] == 34
    assert extra["order_code"] == "EXH-1"
    assert extra["payment_provider"] == "banktransfer"
    assert extra["voucher_id"] == 56
    assert extra["recipient_count"] == 3
    assert extra["retry_count"] == 1
    assert extra["duration_ms"] == 42
    assert extra["job_name"] == "exhibition.send_scheduled_email"
    assert extra["mail_type"] == "vouchers"
    assert extra["route"] == "plugins:exhibition:exhibitor-auth"
    assert extra["outcome"] == "success"
    for forbidden in ("email", "token", "body", "webhook_id", "raw_meta"):
        assert forbidden not in extra
    blob = str(extra)
    assert "person@example.com" not in blob
    assert "secret-token" not in blob


def test_rejects_unsafe_action_and_fields(captured):
    log_operation("has space", OUTCOME_SUCCESS, backend="exhibition")
    log_operation(
        "auth.exhibitor",
        OUTCOME_FAILURE,
        backend="exhi bition",
        error_code="not safe",
        route="bad route with spaces",
        event_id=True,
        object_id=False,
    )
    assert len(captured) == 1
    level, action, extra = captured[0]
    assert level == logging.WARNING
    assert action == "auth.exhibitor"
    assert extra["outcome"] == OUTCOME_FAILURE
    assert "backend" not in extra
    assert "error_code" not in extra
    assert "route" not in extra
    assert "event_id" not in extra
    assert "object_id" not in extra


def test_delegates_to_host_logger_when_available(monkeypatch):
    calls = []

    def fake_host(component, action, outcome, *, level=None, **payload):
        calls.append((component, action, outcome, level, payload))

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", fake_host)
    log_plugin_loaded("exhibition")
    assert calls == [
        ("plugins", "plugin.loaded", "success", logging.INFO, {"backend": "exhibition"}),
    ]


def test_logging_failure_is_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", boom)
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="exhibition")

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", boom)
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="exhibition")

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(operational_log, "_get_host_request_id", boom)
    monkeypatch.setattr(operational_log, "_get_host_job_id", boom)
    assert operational_log.current_request_id() is None
    assert operational_log._current_job_id() is None
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="exhibition")


def test_request_transition_omits_error_code_on_success(monkeypatch):
    import exhibition.models as exhibition_models

    calls = []
    commit_callbacks = []
    monkeypatch.setattr(exhibition_models, "log_operation", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(exhibition_models.transaction, "on_commit", commit_callbacks.append)
    fake_request = SimpleNamespace(
        event_id=7,
        pk=42,
        state="accepted",
        code="REQ123",
        log_action=lambda *a, **kw: None,
    )
    exhibition_models.ExhibitionRequest.log_transition(
        fake_request, "approve", "submitted", requestor=SimpleNamespace(pk=9)
    )
    assert calls == []
    assert len(commit_callbacks) == 1
    commit_callbacks.pop()()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("exhibition.request.approve", OUTCOME_SUCCESS)
    assert "error_code" not in kwargs
    assert kwargs == {"backend": "exhibition", "event_id": 7, "object_id": 42, "user_id": 9}


def test_publish_view_omits_error_code_on_success(monkeypatch):
    import exhibition.views as exhibition_views

    calls = []
    monkeypatch.setattr(exhibition_views, "log_operation", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(exhibition_views.messages, "info", lambda *a, **kw: None)
    monkeypatch.setattr(exhibition_views.messages, "success", lambda *a, **kw: None)
    monkeypatch.setattr(exhibition_views, "redirect", lambda url: url)

    view = exhibition_views.ExhibitorPublishView()
    view.apply = lambda *a, **kw: ([], [])
    view.scoped_queryset = lambda: SimpleNamespace(filter=lambda **kw: [])
    view.confirmed_ids = lambda: []
    view.unpublished_approved = lambda: []
    view.selected = lambda: []
    view.list_url = lambda: "/list"
    view.report_blocked = lambda *a, **kw: None

    request = SimpleNamespace(event=SimpleNamespace(pk=5), user=SimpleNamespace(pk=8))
    view.request = request
    view.publish_all(SimpleNamespace(**vars(request), POST={"confirmed": "1"}))
    view.publish_selected(request, published=True)
    view.publish_selected(request, published=False)

    assert [args for args, _ in calls] == [
        ("exhibition.publish_all", OUTCOME_SUCCESS),
        ("exhibition.publish", OUTCOME_SUCCESS),
        ("exhibition.unpublish", OUTCOME_SUCCESS),
    ]
    assert all("error_code" not in kw for _, kw in calls)


def test_permission_denied_skips_anonymous_users(monkeypatch):
    import exhibition.views as exhibition_views

    calls = []
    monkeypatch.setattr(exhibition_views, "log_operation", lambda *a, **kw: calls.append((a, kw)))

    anon_req = SimpleNamespace(event=SimpleNamespace(pk=1), user=SimpleNamespace(pk=None, is_authenticated=False))
    exhibition_views._log_permission_denied(anon_req, "review")
    assert calls == []

    auth_req = SimpleNamespace(event=SimpleNamespace(pk=1), user=SimpleNamespace(pk=11, is_authenticated=True))
    exhibition_views._log_permission_denied(auth_req, "review")
    assert calls == [
        (
            ("permission.denied", OUTCOME_FAILURE),
            {"backend": "exhibition", "error_code": "review", "event_id": 1, "user_id": 11},
        )
    ]


def test_scheduled_email_logs_success_only_after_commit(monkeypatch):
    import exhibition.models as exhibition_models
    import exhibition.tasks as exhibition_tasks

    calls = []
    monkeypatch.setattr(exhibition_tasks, "log_operation", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(exhibition_tasks.transaction, "atomic", nullcontext)
    monkeypatch.setattr(exhibition_tasks.Event.objects, "get", lambda pk: SimpleNamespace(pk=pk))

    class FakeQS:
        def __init__(self, row):
            self._row = row

        def filter(self, **kwargs):
            return self

        def first(self):
            return self._row

    queued = SimpleNamespace(scheduled_at=exhibition_tasks.now(), send=lambda: None)
    monkeypatch.setattr(
        exhibition_models.ExhibitionEmailQueue.objects,
        "select_for_update",
        lambda skip_locked=True: FakeQS(queued),
    )

    exhibition_tasks.send_scheduled_email.run(10, 20)
    assert len(calls) == 3
    assert calls[0] == (
        ("job.start", OUTCOME_SUCCESS),
        {
            "backend": "exhibition",
            "job_name": "exhibition.send_scheduled_email",
            "event_id": 10,
            "object_id": 20,
            "request_id": None,
        },
    )
    assert calls[1][0] == ("mail.send", OUTCOME_SUCCESS)
    assert calls[1][1]["backend"] == "exhibition"
    assert calls[1][1]["event_id"] == 10
    assert calls[1][1]["object_id"] == 20
    assert calls[2][0] == ("job.finish", OUTCOME_SUCCESS)
    assert calls[2][1]["backend"] == "exhibition"
    assert calls[2][1]["job_name"] == "exhibition.send_scheduled_email"
    assert calls[2][1]["event_id"] == 10
    assert calls[2][1]["object_id"] == 20
    assert "duration_ms" in calls[2][1]
    assert calls[1][1]["request_id"] is None
    assert isinstance(calls[1][1]["duration_ms"], int)

    # Commit-time failure must NOT emit a success log.
    calls.clear()

    class FailingCommit:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            raise RuntimeError("commit failed")

    monkeypatch.setattr(exhibition_tasks.transaction, "atomic", FailingCommit)
    monkeypatch.setattr(
        exhibition_tasks.send_scheduled_email,
        "retry",
        lambda *a, **kw: (_ for _ in ()).throw(MaxRetriesExceededError()),
    )
    exhibition_tasks.send_scheduled_email.run(10, 20)
    assert calls[0][0] == ("job.start", OUTCOME_SUCCESS)
    failure_calls = [c for c in calls[1:] if c[0][0] == "mail.send"]
    assert [kw["error_code"] for _, kw in failure_calls] == ["send_failed", "max_retries_exceeded"]
    assert all(args[1] == OUTCOME_FAILURE for args, _ in failure_calls)
    assert all(isinstance(kw["duration_ms"], int) for _, kw in failure_calls)


def test_api_auth_logs_without_raw_key(monkeypatch):
    import exhibition.api as exhibition_api

    calls = []
    monkeypatch.setattr(exhibition_api, "log_operation", lambda *a, **kw: calls.append((a, kw)))

    view = exhibition_api.ExhibitorAuthView()
    match = SimpleNamespace(view_name="plugins:exhibition:exhibitor-auth")
    event = SimpleNamespace(pk=3, locale="en")

    # Missing key
    req = SimpleNamespace(data={}, resolver_match=match, event=event)
    resp = view.post(req)
    assert resp.status_code == 400
    assert calls[-1][0] == ("auth.exhibitor", OUTCOME_FAILURE)
    assert calls[-1][1]["error_code"] == "missing_parameters"
    assert calls[-1][1]["route"] == "plugins:exhibition:exhibitor-auth"

    # Invalid key (must not leak secret key)
    def raise_not_found(**kwargs):
        raise exhibition_api.ExhibitorInfo.DoesNotExist

    monkeypatch.setattr(exhibition_api.ExhibitorInfo.objects, "get", raise_not_found)
    req_bad = SimpleNamespace(data={"key": "top-secret-key"}, resolver_match=match, event=event)
    resp_bad = view.post(req_bad)
    assert resp_bad.status_code == 401
    assert calls[-1][0] == ("auth.exhibitor", OUTCOME_FAILURE)
    assert calls[-1][1]["error_code"] == "invalid_credentials"
    assert "top-secret-key" not in str(calls)

    # Valid key
    exhibitor = SimpleNamespace(id=55, event_id=3, event=event, name="Acme", booth_number="B1", booth_name="Booth")
    monkeypatch.setattr(exhibition_api.ExhibitorInfo.objects, "get", lambda **kw: exhibitor)
    resp_ok = view.post(req_bad)
    assert resp_ok.status_code == 200
    assert calls[-1][0] == ("auth.exhibitor", OUTCOME_SUCCESS)
    assert calls[-1][1]["object_id"] == 55
    assert "top-secret-key" not in str(calls)


def test_voucher_claim_and_device_provisioning_logging(monkeypatch):
    from eventyay.base.models import Event, Organizer, Voucher

    import exhibition.models as exhibition_models
    import exhibition.utils as exhibition_utils

    calls = []
    monkeypatch.setattr(exhibition_utils, "log_operation", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(exhibition_utils.transaction, "atomic", nullcontext)
    monkeypatch.setattr(exhibition_utils, "resolve_voucher_pool_tag", lambda ex: "vip")

    class FakeVoucherQS:
        def __init__(self, rows):
            self._rows = rows

        def select_for_update(self, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def __getitem__(self, item):
            return self._rows[item]

    organizer = Organizer(pk=2, name="Org", slug="org")
    event = Event(pk=9, organizer=organizer, name="Ev", slug="ev")
    exhibitor = exhibition_models.ExhibitorInfo(pk=77, event=event, name="X")
    monkeypatch.setattr(
        exhibition_utils, "unassigned_pool_vouchers", lambda ev, tag: FakeVoucherQS([Voucher(pk=1, code="V1")])
    )
    assert exhibition_utils.claim_pool_vouchers(exhibitor, 2) == []
    assert calls[-1] == (
        ("exhibition.voucher.claim", OUTCOME_FAILURE),
        {"backend": "exhibition", "error_code": "pool_empty", "event_id": 9, "object_id": 77},
    )

    monkeypatch.setattr(
        exhibition_utils,
        "unassigned_pool_vouchers",
        lambda ev, tag: FakeVoucherQS([Voucher(pk=1, code="V1"), Voucher(pk=2, code="V2")]),
    )
    monkeypatch.setattr(exhibition_models.ExhibitorVoucher.objects, "bulk_create", list)
    claimed = exhibition_utils.claim_pool_vouchers(exhibitor, 2)
    assert len(claimed) == 2
    assert calls[-1] == (
        ("exhibition.voucher.claim", OUTCOME_SUCCESS),
        {"backend": "exhibition", "event_id": 9, "object_id": 77},
    )

    # Device provisioning and token reset
    calls.clear()

    class FakeCountQS:
        def count(self):
            return 0

    class FakeDevice:
        def __init__(self, **kwargs):
            self.limit_events = SimpleNamespace(add=lambda ev: None)
            self.api_token = None
            self.initialized = None
            self.revoked = False

        def save(self, **kwargs):
            pass

        def log_action(self, *args, **kwargs):
            pass

    class FakeExhibitorDevice:
        objects = SimpleNamespace(
            filter=lambda **kw: FakeCountQS(),
            bulk_create=list,
        )

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("eventyay.base.models.Device", FakeDevice)
    monkeypatch.setattr(exhibition_models, "ExhibitorDevice", FakeExhibitorDevice)
    created_devices = exhibition_utils.provision_exhibitor_devices(exhibitor, 2, user=SimpleNamespace(pk=15))
    assert len(created_devices) == 2
    assert calls[-1] == (
        ("exhibition.device.provision", OUTCOME_SUCCESS),
        {"backend": "exhibition", "event_id": 9, "object_id": 77, "user_id": 15},
    )

    monkeypatch.setattr("eventyay.base.models.devices.generate_initialization_token", lambda: "tok-1")
    fake_link = SimpleNamespace(device=FakeDevice())
    FakeExhibitorDevice.objects = SimpleNamespace(
        filter=lambda **kw: SimpleNamespace(select_related=lambda *a: [fake_link]),
    )
    exhibition_utils.reset_exhibitor_device_setup(exhibitor, user=SimpleNamespace(pk=15))
    assert calls[-1] == (
        ("exhibition.device.reset_tokens", OUTCOME_SUCCESS),
        {"backend": "exhibition", "event_id": 9, "object_id": 77, "user_id": 15},
    )


def test_order_operations_logging(monkeypatch):
    import exhibition.models as exhibition_models

    calls = []
    commit_callbacks = []
    monkeypatch.setattr(exhibition_models, "log_operation", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(exhibition_models.transaction, "atomic", nullcontext)
    monkeypatch.setattr(exhibition_models.transaction, "on_commit", commit_callbacks.append)

    order = exhibition_models.ExhibitionOrder(
        pk=101,
        event_id=4,
        code="EXH42",
        status=exhibition_models.ExhibitionOrderStatus.PENDING,
        payment_provider="manual",
    )
    monkeypatch.setattr(order, "save", lambda **kw: None)
    monkeypatch.setattr(order, "log_action", lambda *a, **kw: None)
    locked_order = SimpleNamespace(
        status=exhibition_models.ExhibitionOrderStatus.PENDING,
        payment_provider="banktransfer",
    )
    monkeypatch.setattr(
        exhibition_models.ExhibitionOrder.objects,
        "select_for_update",
        lambda: SimpleNamespace(get=lambda pk: locked_order),
    )

    order.change_payment_provider("banktransfer", user=SimpleNamespace(pk=8))
    assert calls[-1] == (
        ("exhibition.order.payment_changed", OUTCOME_SUCCESS),
        {
            "backend": "exhibition",
            "event_id": 4,
            "order_id": 101,
            "order_code": "EXH42",
            "payment_provider": "banktransfer",
            "user_id": 8,
        },
    )

    # _transition defers success logging to transaction.on_commit and uses locked payment_provider
    # even if self.payment_provider was stale ("manual").
    order.payment_provider = "manual"
    calls.clear()
    order._transition(
        {exhibition_models.ExhibitionOrderStatus.PENDING},
        exhibition_models.ExhibitionOrderStatus.CANCELLED,
        exhibition_models.LOG_ORDER_CANCELLED,
        SimpleNamespace(pk=8),
    )
    assert calls == []
    assert len(commit_callbacks) == 1
    commit_callbacks.pop()()
    assert calls[-1] == (
        ("exhibition.order.cancelled", OUTCOME_SUCCESS),
        {
            "backend": "exhibition",
            "event_id": 4,
            "order_id": 101,
            "order_code": "EXH42",
            "payment_provider": "banktransfer",
            "user_id": 8,
        },
    )


def test_api_forbidden_and_lead_scan_logging(monkeypatch):
    import exhibition.api as exhibition_api

    calls = []
    monkeypatch.setattr(exhibition_api, "log_operation", lambda *a, **kw: calls.append((a, kw)))

    match = SimpleNamespace(view_name="plugins:exhibition:lead-create")
    event = SimpleNamespace(pk=3, locale="en")
    req = SimpleNamespace(
        data={},
        headers={},
        resolver_match=match,
        event=event,
    )
    view = exhibition_api.LeadCreateView()
    resp = view.post(req)
    assert resp.status_code == 400
    assert calls[-1][0] == ("exhibition.lead.scan", OUTCOME_FAILURE)
    assert calls[-1][1]["error_code"] == "missing_parameters"

    exhibitor = SimpleNamespace(id=55, event_id=3, event=event, lead_scanning_enabled=False)
    monkeypatch.setattr(exhibition_api.ExhibitorInfo.objects, "get", lambda **kw: exhibitor)
    req_forbidden = SimpleNamespace(
        data={"lead": "P1", "scanned": "2026-01-01", "scan_type": "qr", "device_name": "D1"},
        headers={"Exhibitor": "secret-key"},
        resolver_match=match,
        event=event,
    )
    resp_forbidden = view.post(req_forbidden)
    assert resp_forbidden.status_code == 403
    assert calls[-1] == (
        ("permission.denied", OUTCOME_FAILURE),
        {
            "backend": "exhibition",
            "error_code": "lead_scanning_disabled",
            "status": 403,
            "event_id": 3,
            "object_id": 55,
            "route": "plugins:exhibition:lead-create",
        },
    )
    assert "secret-key" not in str(calls)


def test_correlation_ids_and_api_reads(monkeypatch):
    import exhibition.api as exhibition_api
    import exhibition.models as exhibition_models
    import exhibition.tasks as exhibition_tasks
    import exhibition.views as exhibition_views

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(operational_log, "_get_host_request_id", lambda: "req-abc-123")
    monkeypatch.setattr(operational_log, "_get_host_job_id", lambda: "job-xyz-789")

    records = []
    monkeypatch.setattr(
        logging.getLogger("eventyay.plugins"),
        "log",
        lambda level, action, extra=None: records.append((level, action, extra or {})),
    )
    log_operation("mail.send", OUTCOME_SUCCESS, backend="exhibition", event_id=1)
    assert records[-1][2]["request_id"] == "req-abc-123"
    assert records[-1][2]["job_id"] == "job-xyz-789"

    # Explicit request_id passed to scheduled task overrides/persists across process boundary.
    task_calls = []
    monkeypatch.setattr(exhibition_tasks, "log_operation", lambda *a, **kw: task_calls.append((a, kw)))
    monkeypatch.setattr(exhibition_tasks.transaction, "atomic", nullcontext)
    monkeypatch.setattr(exhibition_tasks.Event.objects, "get", lambda pk: SimpleNamespace(pk=pk))

    class FakeQS:
        def filter(self, **kwargs):
            return self

        def first(self):
            return SimpleNamespace(scheduled_at=exhibition_tasks.now(), send=lambda: None)

    monkeypatch.setattr(
        exhibition_models.ExhibitionEmailQueue.objects,
        "select_for_update",
        lambda skip_locked=True: FakeQS(),
    )
    exhibition_tasks.send_scheduled_email.run(10, 20, request_id="req-origin-1")
    assert task_calls[-1][1]["request_id"] == "req-origin-1"

    # _user_id handles request.user=None safely.
    assert exhibition_views._user_id(SimpleNamespace(user=None)) is None

    # API read endpoints emit result events without PII.
    api_calls = []
    monkeypatch.setattr(exhibition_api, "log_operation", lambda *a, **kw: api_calls.append((a, kw)))
    event = SimpleNamespace(pk=3, locale="en")
    exhibitor = SimpleNamespace(
        id=55, event_id=3, event=event, allow_lead_access=True, allow_voucher_access=True, lead_scanning_enabled=True
    )
    monkeypatch.setattr(exhibition_api.ExhibitorInfo.objects, "get", lambda **kw: exhibitor)
    monkeypatch.setattr(exhibition_api.Lead.objects, "filter", lambda **kw: SimpleNamespace(values=lambda *a, **k: []))
    monkeypatch.setattr(exhibition_api, "exhibitor_voucher_redemptions", lambda ex: [])
    monkeypatch.setattr(
        exhibition_api.ExhibitorSettings.objects, "get_or_create", lambda **kw: (SimpleNamespace(), False)
    )
    monkeypatch.setattr(exhibition_api.ExhibitorTag.objects, "filter", lambda **kw: [])

    req_read = SimpleNamespace(
        headers={"Exhibitor": "k"},
        resolver_match=SimpleNamespace(view_name="plugins:exhibition:lead-retrieve"),
        event=event,
    )
    assert exhibition_api.LeadRetrieveView().get(req_read).status_code == 200
    assert api_calls[-1][0] == ("exhibition.lead.retrieve", OUTCOME_SUCCESS)

    assert exhibition_api.VoucherRedemptionRetrieveView().get(req_read, organizer="o", event="e").status_code == 200
    assert api_calls[-1][0] == ("exhibition.voucher.redemptions", OUTCOME_SUCCESS)

    assert exhibition_api.TagListView().get(req_read, organizer="o", event="e").status_code == 200
    assert api_calls[-1][0] == ("exhibition.tag.list", OUTCOME_SUCCESS)


def test_email_send_view_logs_failure_and_reraises(monkeypatch):
    import exhibition.views as exhibition_views

    calls = []
    monkeypatch.setattr(exhibition_views, "log_operation", lambda *a, **kw: calls.append((a, kw)))

    sent = []

    def ok_send(requestor=None):
        sent.append("ok")

    def bad_send(requestor=None):
        raise RuntimeError("smtp down")

    row1 = SimpleNamespace(pk=11, batch="b1", send=ok_send)
    row2 = SimpleNamespace(pk=12, batch="b1", send=bad_send)
    monkeypatch.setattr(exhibition_views, "get_object_or_404", lambda *a, **kw: row1)
    monkeypatch.setattr(
        exhibition_views.ExhibitionEmailQueue.objects,
        "filter",
        lambda **kw: [row1, row2],
    )
    request = SimpleNamespace(event=SimpleNamespace(pk=5), user=SimpleNamespace(pk=8))
    with pytest.raises(RuntimeError, match="smtp down"):
        exhibition_views.EmailSendView().post(request, pk=11)

    assert len(calls) == 1
    (action, outcome), payload = calls[0]
    assert action == "mail.send"
    assert outcome == OUTCOME_FAILURE
    assert payload["backend"] == "exhibition"
    assert payload["error_code"] == "send_failed"
    assert payload["event_id"] == 5
    assert payload["object_id"] == 11
    assert payload["recipient_count"] == 1
    assert payload["user_id"] == 8
    assert payload["retry_count"] == 0
    assert payload["status"] == 500
    assert "duration_ms" in payload


def test_viewset_writes_and_task_finish_logs(monkeypatch):
    import exhibition.api as exhibition_api
    import exhibition.models as exhibition_models
    import exhibition.tasks as exhibition_tasks

    # 1. Test ExhibitorInfoViewSet writes
    api_calls = []
    monkeypatch.setattr(exhibition_api, "log_operation", lambda *a, **kw: api_calls.append((a, kw)))
    monkeypatch.setattr(exhibition_api, "queue_profile_invitation", lambda *a, **kw: None)

    viewset = exhibition_api.ExhibitorInfoViewSet()
    event = SimpleNamespace(pk=9)
    user = SimpleNamespace(pk=42)
    viewset.request = SimpleNamespace(
        event=event,
        user=user,
        resolver_match=SimpleNamespace(view_name="api:exhibitor-detail"),
    )

    serializer_mock = SimpleNamespace(save=lambda: SimpleNamespace(pk=101))
    perform_create_fn = getattr(viewset.perform_create, "__wrapped__", viewset.perform_create)
    perform_create_fn(viewset, serializer_mock)
    assert api_calls[-1] == (
        ("exhibition.organization.create", OUTCOME_SUCCESS),
        {
            "backend": "exhibition",
            "status": 201,
            "event_id": 9,
            "object_id": 101,
            "model": "organization",
            "user_id": 42,
            "route": "api:exhibitor-detail",
        },
    )

    viewset.perform_update(serializer_mock)
    assert api_calls[-1] == (
        ("exhibition.organization.update", OUTCOME_SUCCESS),
        {
            "backend": "exhibition",
            "status": 200,
            "event_id": 9,
            "object_id": 101,
            "model": "organization",
            "user_id": 42,
            "route": "api:exhibitor-detail",
        },
    )

    instance_mock = SimpleNamespace(pk=101, delete=lambda: None)
    viewset.perform_destroy(instance_mock)
    assert api_calls[-1] == (
        ("exhibition.organization.delete", OUTCOME_SUCCESS),
        {
            "backend": "exhibition",
            "status": 204,
            "event_id": 9,
            "object_id": 101,
            "model": "organization",
            "user_id": 42,
            "route": "api:exhibitor-detail",
        },
    )

    # 2. Test send_scheduled_email task early returns emit job.finish
    task_calls = []
    monkeypatch.setattr(exhibition_tasks, "log_operation", lambda *a, **kw: task_calls.append((a, kw)))
    monkeypatch.setattr(exhibition_tasks.transaction, "atomic", nullcontext)
    monkeypatch.setattr(exhibition_tasks.Event.objects, "get", lambda pk: SimpleNamespace(pk=pk))

    class EmptyQS:
        def filter(self, **kwargs):
            return self

        def first(self):
            return None

    monkeypatch.setattr(
        exhibition_models.ExhibitionEmailQueue.objects,
        "select_for_update",
        lambda skip_locked=True: EmptyQS(),
    )
    exhibition_tasks.send_scheduled_email.run(10, 20, request_id="req-early-1")
    assert any(action == "job.finish" and outcome == OUTCOME_SUCCESS for (action, outcome), _ in task_calls)
