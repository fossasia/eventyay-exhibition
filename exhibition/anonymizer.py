from django.utils.translation import gettext_lazy as _
from eventyay.base.anonymizer import BaseTicketingAnonymizer
from eventyay.base.models import Order

from .models import Lead

# Keys of ``Lead.attendee`` the exhibitor wrote themselves or that come from answers to questions. The rest
# (name, email, company, job title, address) identifies the attendee.
KEPT_ATTENDEE_KEYS = ("note", "tags", "questions")


class LeadAttendeeAnonymizer(BaseTicketingAnonymizer):
    identifier = "exhibition_leads"
    verbose_name = _("Exhibition lead scans")

    def anonymize_order(self, order: Order) -> None:
        # A lead is linked to an attendee only by the code on the ticket, which the lead scanning app reads
        # either from the ticket secret or from the pseudonymization ID.
        codes = set()
        for position in order.all_positions.all():
            codes.update((position.secret, position.pseudonymization_id))
        codes.discard(None)
        codes.discard("")
        if not codes:
            return
        leads = Lead.objects.filter(exhibitor__event=order.event, pseudonymization_id__in=codes)
        for lead in leads.exclude(attendee=None):
            kept = {key: value for key, value in lead.attendee.items() if key in KEPT_ATTENDEE_KEYS}
            if kept != lead.attendee:
                lead.attendee = kept
                lead.save(update_fields=["attendee"])
