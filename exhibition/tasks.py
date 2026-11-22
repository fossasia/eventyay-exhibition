import logging

from celery.exceptions import MaxRetriesExceededError
from django.db import transaction
from django.utils.timezone import now
from eventyay.base.models import Event
from eventyay.base.services.tasks import ProfiledEventTask
from eventyay.celery_app import app

from .operational_log import OUTCOME_FAILURE, OUTCOME_SUCCESS, log_operation

logger = logging.getLogger(__name__)


@app.task(base=ProfiledEventTask, bind=True, max_retries=5, default_retry_delay=60, acks_late=True)
def send_scheduled_email(self, event_id, queue_id, request_id=None):
    from .models import ExhibitionEmailQueue

    if isinstance(event_id, Event):
        event = event_id
        original_event_id = event.pk
    else:
        original_event_id = event_id
        try:
            event = Event.objects.get(pk=event_id)
        except Event.DoesNotExist:
            logger.error("[Exhibition] Event %s not found for queued email %s", event_id, queue_id)
            log_operation(
                "mail.send",
                OUTCOME_FAILURE,
                backend="exhibition",
                error_code="event_missing",
                event_id=original_event_id if isinstance(original_event_id, int) else None,
                object_id=queue_id,
                request_id=request_id,
            )
            return

    send_completed = False
    try:
        with transaction.atomic():
            queued = (
                ExhibitionEmailQueue.objects.select_for_update(skip_locked=True)
                .filter(pk=queue_id, event=event, sent_at__isnull=True)
                .first()
            )
            if queued is None:
                return

            if queued.scheduled_at is None:
                return

            if queued.scheduled_at > now():
                async_kwargs = {"args": [original_event_id, queue_id], "eta": queued.scheduled_at}
                if request_id:
                    async_kwargs["kwargs"] = {"request_id": request_id}
                send_scheduled_email.apply_async(**async_kwargs)
                return

            queued.send()
            send_completed = True
    except Exception as exc:
        logger.exception("[Exhibition] Failed to send scheduled email %s", queue_id)
        log_operation(
            "mail.send",
            OUTCOME_FAILURE,
            backend="exhibition",
            error_code="send_failed",
            event_id=event.pk,
            object_id=queue_id,
            retry_count=getattr(self.request, "retries", None),
            request_id=request_id,
        )
        try:
            retry_kwargs = {"request_id": request_id} if request_id else None
            self.retry(exc=exc, args=[original_event_id, queue_id], kwargs=retry_kwargs)
        except MaxRetriesExceededError:
            logger.error("[Exhibition] Max retries exceeded for scheduled email %s", queue_id)
            log_operation(
                "mail.send",
                OUTCOME_FAILURE,
                backend="exhibition",
                error_code="max_retries_exceeded",
                event_id=event.pk,
                object_id=queue_id,
                retry_count=getattr(self.request, "retries", None),
                request_id=request_id,
            )
    else:
        if send_completed:
            log_operation(
                "mail.send",
                OUTCOME_SUCCESS,
                backend="exhibition",
                event_id=event.pk,
                object_id=queue_id,
                request_id=request_id,
            )
