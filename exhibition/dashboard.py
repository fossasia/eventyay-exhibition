from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, UpdateView
from eventyay.base.models import Event

from .forms import ExhibitorSelfEditForm
from .models import LOG_ORGANIZATION_CHANGED, ExhibitionRequest, ExhibitionRequestState, ExhibitorInfo
from .utils import (
    VOUCHER_CSV_FILENAME,
    VOUCHER_REDEMPTION_CSV_FILENAME,
    attendee_field_labels,
    attendee_field_values,
    build_voucher_csv,
    build_voucher_redemption_csv,
    event_exhibitor_settings,
    exhibitor_unredeemed_vouchers,
    exhibitor_voucher_redemptions,
    user_can_edit_profile,
    user_can_view_vouchers,
    user_exhibitors,
    voucher_redeem_url,
)
from .views import ExhibitorLinkFormsetMixin

REQUEST_STATE_LABELS = {
    ExhibitionRequestState.ACCEPTED: "label-success",
    ExhibitionRequestState.REJECTED: "label-danger",
    ExhibitionRequestState.WITHDRAWN: "label-default",
    ExhibitionRequestState.DRAFT: "label-default",
}


def _event_kwargs(event):
    return {"organizer": event.organizer.slug, "event": event.slug}


def user_has_exhibitions(user) -> bool:
    """Whether the dashboard should offer My Exhibitions to this account at all."""
    if user is None or not user.is_authenticated:
        return False
    if ExhibitionRequest.objects.filter(user=user).exists():
        return True
    return user_exhibitors(user).filter(source_requests__isnull=True).exists()


class MyExhibitionsFilterForm(forms.Form):
    search = forms.CharField(required=False, label=_("Search"))
    event = forms.ModelChoiceField(
        queryset=Event.objects.none(),
        required=False,
        label=_("Event"),
        widget=forms.Select(attrs={"class": "form-control"}),
        empty_label=_("Select an Event"),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            request_events = ExhibitionRequest.objects.filter(user=user).values("event")
            added_events = user_exhibitors(user).filter(source_requests__isnull=True).values("event")
            self.fields["event"].queryset = Event.objects.filter(
                Q(pk__in=request_events) | Q(pk__in=added_events)
            ).order_by("-date_from")

    def has_active_filters(self) -> bool:
        if not self.is_bound or not self.is_valid():
            return False
        return bool(self.cleaned_data.get("event") or (self.cleaned_data.get("search") or "").strip())


def _edit_url(exhibitor):
    if not exhibitor.active:
        return None
    return reverse("plugins:exhibition:my_exhibitions.edit", kwargs={"pk": exhibitor.pk})


def _vouchers_url(exhibitor):
    if exhibitor is None or not exhibitor.active or not exhibitor.allow_voucher_access:
        return None
    return reverse("plugins:exhibition:my_exhibitions.vouchers", kwargs={"pk": exhibitor.pk})


class MyExhibitionsView(LoginRequiredMixin, ListView):
    """Every exhibition a user takes part in, across events, on their personal dashboard."""

    template_name = "exhibitors/my_exhibitions.html"
    context_object_name = "entries"
    paginate_by = 25

    def request_entries(self):
        requests = (
            ExhibitionRequest.objects.filter(user=self.request.user)
            .select_related("event", "event__organizer", "approved_exhibitor")
            .order_by("-created")
        )
        for exhibition_request in requests:
            exhibitor = (
                exhibition_request.approved_exhibitor
                if exhibition_request.state == ExhibitionRequestState.ACCEPTED
                else None
            )
            yield {
                "name": exhibition_request.name,
                "event": exhibition_request.event,
                "status": exhibition_request.get_state_display(),
                "status_class": REQUEST_STATE_LABELS.get(exhibition_request.state, "label-info"),
                "request_url": reverse(
                    "plugins:exhibition:request.user_edit",
                    kwargs={**_event_kwargs(exhibition_request.event), "code": exhibition_request.code},
                ),
                "vouchers_url": _vouchers_url(exhibitor),
            }

    def organizer_added_entries(self):
        exhibitors = (
            user_exhibitors(self.request.user)
            .filter(source_requests__isnull=True)
            .select_related("event", "event__organizer")
            .order_by("-pk")
        )
        for exhibitor in exhibitors:
            yield {
                "name": exhibitor.name,
                "event": exhibitor.event,
                "status": _("Added by the organizer") if exhibitor.active else _("Inactive"),
                "status_class": "label-success" if exhibitor.active else "label-default",
                "request_url": _edit_url(exhibitor),
                "vouchers_url": _vouchers_url(exhibitor),
            }

    @cached_property
    def filter_form(self):
        return MyExhibitionsFilterForm(self.request.GET, user=self.request.user)

    def get_queryset(self):
        entries = [*self.request_entries(), *self.organizer_added_entries()]
        if self.filter_form.is_valid():
            event = self.filter_form.cleaned_data.get("event")
            search = (self.filter_form.cleaned_data.get("search") or "").strip().lower()
            if event:
                entries = [entry for entry in entries if entry["event"].pk == event.pk]
            if search:
                entries = [entry for entry in entries if search in str(entry["name"]).lower()]
        entries.sort(key=lambda entry: entry["event"].date_from, reverse=True)
        return entries

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        context["has_active_filters"] = self.filter_form.has_active_filters()
        return context


class MyExhibitionVouchersView(LoginRequiredMixin, ListView):
    """An exhibitor's own vouchers: what has been redeemed, and what is still theirs to hand out."""

    template_name = "exhibitors/my_exhibition_vouchers.html"
    context_object_name = "voucher_rows"
    paginate_by = 50

    @cached_property
    def exhibitor(self):
        exhibitor = get_object_or_404(ExhibitorInfo.objects.select_related("event__organizer"), pk=self.kwargs["pk"])
        if not user_can_view_vouchers(self.request.user, exhibitor):
            raise Http404
        return exhibitor

    @cached_property
    def event(self):
        return self.exhibitor.event

    @cached_property
    def exhibition_settings(self):
        return event_exhibitor_settings(self.event)

    @property
    def showing_unredeemed(self):
        return self.request.GET.get("status") == "pending"

    @cached_property
    def redemptions(self):
        return exhibitor_voucher_redemptions(self.exhibitor)

    @cached_property
    def unredeemed(self):
        return exhibitor_unredeemed_vouchers(self.exhibitor)

    def get_queryset(self):
        return self.unredeemed if self.showing_unredeemed else self.redemptions

    def get(self, request, *args, **kwargs):
        if request.GET.get("download") == "yes":
            return self.download_csv()
        return super().get(request, *args, **kwargs)

    def download_csv(self):
        if self.showing_unredeemed:
            body = build_voucher_csv(self.event, self.unredeemed)
            filename = VOUCHER_CSV_FILENAME
        else:
            body = build_voucher_redemption_csv(self.event, self.redemptions, self.exhibition_settings)
            filename = VOUCHER_REDEMPTION_CSV_FILENAME
        response = HttpResponse(body.encode("utf-8"), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings = self.exhibition_settings
        context["exhibitor"] = self.exhibitor
        context["event"] = self.event
        context["showing_unredeemed"] = self.showing_unredeemed
        context["attendee_labels"] = attendee_field_labels(settings)
        if self.showing_unredeemed:
            context["vouchers"] = [
                {
                    "code": voucher.code,
                    "product": str(voucher.product) if voucher.product else "",
                    "price_mode": voucher.get_price_mode_display(),
                    "value": voucher.value,
                    "valid_until": voucher.valid_until,
                    "max_usages": voucher.max_usages,
                    "redeem_url": voucher_redeem_url(self.event, voucher),
                }
                for voucher in context["voucher_rows"]
            ]
        else:
            context["rows"] = [
                {
                    "voucher_code": position.voucher.code if position.voucher else "",
                    "attendee": attendee_field_values(position, settings),
                    "order": position.order,
                    "redeemed_at": position.order.datetime,
                }
                for position in context["voucher_rows"]
            ]
        redeemed_vouchers = self.redemptions.order_by().values("voucher_id").distinct().count()
        unredeemed_count = self.unredeemed.count()
        context["redemption_count"] = self.redemptions.count()
        context["redeemed_count"] = redeemed_vouchers
        context["unredeemed_count"] = unredeemed_count
        context["issued_count"] = redeemed_vouchers + unredeemed_count
        return context


class MyExhibitionEditView(LoginRequiredMixin, ExhibitorLinkFormsetMixin, UpdateView):
    """Lets the account behind an organizer-created profile complete and maintain it."""

    model = ExhibitorInfo
    form_class = ExhibitorSelfEditForm
    template_name = "exhibitors/my_exhibition_edit.html"

    @cached_property
    def exhibitor(self):
        exhibitor = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        if not user_can_edit_profile(self.request.user, exhibitor):
            raise Http404
        return exhibitor

    @property
    def exhibition_event(self):
        return self.exhibitor.event

    def get_queryset(self):
        return (
            user_exhibitors(self.request.user).filter(source_requests__isnull=True).select_related("event__organizer")
        )

    def get_object(self, queryset=None):
        return self.exhibitor

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.exhibitor.event
        return kwargs

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.post_with_formsets()

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        self.save_link_formsets()
        changes = [key for key in form.changed_data if not key.startswith("question_")]
        if changes:
            self.object.log_action(
                LOG_ORGANIZATION_CHANGED,
                data={"changed": changes, "by": "exhibitor"},
                user=self.request.user,
            )
        messages.success(self.request, _("Your changes have been saved."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["exhibitor"] = self.object
        context["event"] = self.object.event
        return context

    def get_success_url(self):
        return reverse("plugins:exhibition:my_exhibitions")
