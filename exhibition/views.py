import io
import json

from defusedcsv import csv
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _, ngettext
from django.views import View
from django.views.generic import DeleteView, DetailView, FormView, ListView, TemplateView
from eventyay.base.services.system_questions import (
    STATE_REQUIRED,
    get_system_question_base_state,
)
from eventyay.base.templatetags.rich_text import rich_text
from eventyay.common.utils.language import localize_event_text
from eventyay.control.forms.filter import advanced_filter_count, advanced_filters_open_from_get
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views import CreateView, PaginationMixin, UpdateView

from . import mail as mail_helpers
from .filters import (
    EmailFilterForm,
    ExhibitorFilterForm,
    ProposalFilterForm,
    PublicExhibitorFilterForm,
)
from .forms import (
    CallSettingsForm,
    ExhibitionComposeForm,
    ExhibitionCustomEmailTemplateForm,
    ExhibitionDefaultFieldForm,
    ExhibitionEmailQueueForm,
    ExhibitionMailTemplatesForm,
    ExhibitionProposalForm,
    ExhibitionProposalReviewForm,
    ExhibitionProposalReviewNotesForm,
    ExhibitionProposalSocialLinkFormSet,
    ExhibitionQuestionForm,
    ExhibitionQuestionOptionFormSet,
    ExhibitorDeviceProvisionForm,
    ExhibitorInfoForm,
    ExhibitorSocialLinkFormSet,
    ExhibitorVoucherBatchForm,
    ExhibitorVoucherDefaultsForm,
    SponsorGroupForm,
    social_link_prefixes,
)
from .models import (
    LOG_CALL_SETTINGS_CHANGED,
    LOG_GROUP_ADDED,
    LOG_GROUP_CHANGED,
    LOG_GROUP_DELETED,
    LOG_PARTNER_ADDED,
    LOG_PARTNER_CHANGED,
    LOG_PARTNER_DELETED,
    LOG_PROPOSAL_CHANGED,
    LOG_QUESTION_ADDED,
    LOG_QUESTION_CHANGED,
    LOG_QUESTION_DELETED,
    LOG_SETTINGS_CHANGED,
    PROPOSAL_DEFAULT_FIELD_KEYS,
    PROPOSAL_DEFAULT_FIELDS,
    PROPOSAL_REVIEW_ACTIONS,
    QUESTION_OPTION_VARIANTS,
    ExhibitionCustomEmailTemplate,
    ExhibitionEmailQueue,
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitorDevice,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorVoucher,
    SponsorGroup,
    generate_booth_id,
    get_next_sponsor_group_level,
    storable_proposal_field_settings,
)
from .social_links import serialize_social_link
from .utils import (
    VOUCHER_CSV_FILENAME,
    add_external_image_csp_sources,
    allow_blob_image_previews,
    build_voucher_csv,
    event_voucher_settings,
    generate_exhibitor_vouchers,
    provision_exhibitor_devices,
    public_exhibitor_sessions,
    public_exhibitors_queryset,
    reset_exhibitor_device_setup,
    resolve_voucher_defaults,
    should_hide_applicant_emails,
    sync_exhibitor_from_proposal,
)


def event_kwargs(event):
    return {
        "organizer": event.organizer.slug,
        "event": event.slug,
    }


def call_access_session_key(event):
    return f"exhibition_call_access_{event.pk}"


def partner_list_url(event, partner_type):
    """URL of the Sponsors or Exhibitors list, defaulting to Exhibitors."""
    route = {
        "sponsor": "plugins:exhibition:sponsors",
        "exhibitor": "plugins:exhibition:exhibitors",
    }.get(partner_type, "plugins:exhibition:exhibitors")
    return reverse(route, kwargs=event_kwargs(event))


def send_proposal_confirmation(event, proposal, requestor):
    """Send the submission confirmation email once the transaction commits."""
    transaction.on_commit(
        lambda: mail_helpers.queue_proposal_email(
            event,
            proposal,
            mail_helpers.PROPOSAL_NEW,
            send_now=True,
            requestor=requestor,
        )
    )


def queue_exhibitor_access_mail(event, exhibitor, requestor):
    """Queue the access-credentials email for organiser review in the outbox."""
    return mail_helpers.queue_exhibitor_access_email(event, exhibitor, requestor=requestor)


def access_newly_granted(exhibitor, previous=None):
    """True when lead scanning or voucher access is enabled and was not before."""
    previous = previous or {}
    return (exhibitor.lead_scanning_enabled and not previous.get("lead_scanning_enabled")) or (
        exhibitor.allow_voucher_access and not previous.get("allow_voucher_access")
    )


def partner_type_of(exhibitor):
    if exhibitor.is_sponsor and exhibitor.is_exhibitor:
        return "both"
    if exhibitor.is_sponsor:
        return "sponsor"
    if exhibitor.is_exhibitor:
        return "exhibitor"
    return None


class PublicEventLoginRequiredMixin(LoginRequiredMixin):
    def get_login_url(self):
        return reverse("cfp:event.login", kwargs=event_kwargs(self.request.event))


class PublicCallEnabledMixin:
    hide_after_deadline = False
    enforce_private = False
    require_call_enabled = True

    def get_exhibition_settings(self):
        return ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]

    def has_private_call_access(self, settings):
        return self.request.session.get(call_access_session_key(self.request.event)) == settings.call_secret

    def dispatch(self, request, *args, **kwargs):
        settings = self.get_exhibition_settings()
        if self.require_call_enabled and not settings.call_enabled:
            raise Http404()
        if self.hide_after_deadline and settings.call_hide_after_deadline and not settings.call_is_open:
            raise Http404()
        if self.enforce_private and settings.call_private and not self.has_private_call_access(settings):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)


class FilteredListMixin(PaginationMixin):
    """Wires a control-panel FilterForm and pagination into a ListView."""

    def build_filter_form(self):
        raise NotImplementedError

    @cached_property
    def filter_form(self):
        return self.build_filter_form()

    def apply_filters(self, queryset):
        if self.filter_form.is_valid():
            return self.filter_form.filter_qs(queryset)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        context["advanced_filters_open"] = advanced_filters_open_from_get(self.filter_form)
        context["advanced_filter_count"] = advanced_filter_count(self.filter_form)
        return context


class DashboardView(EventPermissionRequiredMixin, TemplateView):
    """Landing page for the plugin: headline counts plus the most recent requests."""

    template_name = "exhibitors/dashboard.html"
    permission = (
        "can_change_event_settings",
        "can_view_orders",
        "can_change_exhibition_proposals",
        "is_exhibition_reviewer",
    )

    RECENT_REQUEST_LIMIT = 5

    def can_view_partners(self):
        return self.request.user.has_event_permission(
            self.request.event.organizer,
            self.request.event,
            ("can_change_event_settings", "can_view_orders"),
            request=self.request,
        )

    def can_review_requests(self):
        return self.request.user.has_event_permission(
            self.request.event.organizer,
            self.request.event,
            ("can_change_event_settings", "can_change_exhibition_proposals", "is_exhibition_reviewer"),
            request=self.request,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event = self.request.event
        context["show_partners"] = self.can_view_partners()
        context["show_requests"] = self.can_review_requests()

        if context["show_partners"]:
            partners = ExhibitorInfo.objects.filter(event=event)
            context["exhibitor_count"] = partners.filter(is_exhibitor=True).count()
            context["sponsor_count"] = partners.filter(is_sponsor=True).count()

        if context["show_requests"]:
            proposals = ExhibitionProposal.objects.filter(event=event)
            context["pending_request_count"] = proposals.filter(state=ExhibitionProposalState.SUBMITTED).count()
            context["recent_requests"] = list(
                proposals.exclude(state=ExhibitionProposalState.DRAFT).order_by("-submitted", "-pk")[
                    : self.RECENT_REQUEST_LIMIT
                ]
            )

        if self.request.user.has_event_permission(
            event.organizer, event, "can_change_event_settings", request=self.request
        ):
            context["exhibition_settings"] = ExhibitorSettings.objects.filter(event=event).first()
        return context


class SettingsView(EventPermissionRequiredMixin, ListView):
    model = ExhibitorInfo
    template_name = "exhibitors/settings.html"
    context_object_name = "exhibitors"
    permission = "can_change_settings"
    active_tab = "exhibitors"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def get_active_tab(self):
        tab = self.request.GET.get("tab") or self.request.POST.get("tab") or self.active_tab
        if tab not in {"exhibitors", "sponsors", "call", "vouchers"}:
            return "exhibitors"
        return tab

    def get_settings_url(self, tab):
        route_names = {
            "call": "plugins:exhibition:settings.call",
            "sponsors": "plugins:exhibition:settings.sponsors",
            "vouchers": "plugins:exhibition:settings.vouchers",
        }
        route_name = route_names.get(tab, "plugins:exhibition:settings.exhibitors")
        return reverse(
            route_name,
            kwargs=event_kwargs(self.request.event),
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        ctx["settings"] = settings
        ctx["data_access_fields"] = self.get_data_access_fields(settings)
        ctx["active_tab"] = self.get_active_tab()

        edit_group_forms = kwargs.get("edit_group_forms", {})
        sponsor_groups = list(
            SponsorGroup.objects.filter(event=self.request.event)
            .annotate(partner_count=Count("partners"))
            .order_by("level", "pk")
        )
        for group in sponsor_groups:
            group.edit_form = edit_group_forms.get(group.pk) or SponsorGroupForm(
                instance=group,
                event=self.request.event,
                prefix=f"group-{group.pk}",
            )

        ctx["sponsor_groups"] = sponsor_groups
        ctx["add_group_form"] = kwargs.get("add_group_form") or SponsorGroupForm(
            event=self.request.event,
            initial={"level": self.get_next_sponsor_group_level()},
            prefix="new-group",
        )
        ctx["call_settings_form"] = kwargs.get("call_settings_form") or CallSettingsForm(
            instance=settings,
            event=self.request.event,
        )
        ctx["voucher_defaults_form"] = kwargs.get("voucher_defaults_form") or ExhibitorVoucherDefaultsForm(
            instance=settings,
            event=self.request.event,
        )
        ctx["show_add_group_form"] = kwargs.get("show_add_group_form", False)
        ctx["expanded_group_pk"] = kwargs.get("expanded_group_pk")
        return ctx

    SYSTEM_QUESTION_FIELD_LABELS = {
        "company": _("Company name"),
        "job_title": _("Job title"),
        "street": _("Address"),
    }

    def get_data_access_fields(self, settings):
        fields = [
            {
                "value": "attendee_name",
                "label": _("Attendee Name"),
                "checked": settings.is_field_allowed("attendee_name"),
            },
            {
                "value": "attendee_email",
                "label": _("Attendee Email"),
                "checked": settings.is_field_allowed("attendee_email"),
            },
        ]
        event = self.request.event
        for field_id, label in self.SYSTEM_QUESTION_FIELD_LABELS.items():
            if get_system_question_base_state(event, field_id) != STATE_REQUIRED:
                continue
            value = f"system_{field_id}"
            fields.append({"value": value, "label": label, "checked": settings.is_field_allowed(value)})

        required_questions = self.request.event.questions.filter(required=True, active=True).order_by("position", "id")
        for question in required_questions:
            value = f"question_{question.pk}"
            fields.append(
                {
                    "value": value,
                    "label": str(question.question),
                    "checked": settings.is_field_allowed(value),
                }
            )
        return fields

    def get_next_sponsor_group_level(self):
        return get_next_sponsor_group_level(self.request.event)

    def post(self, request, *args, **kwargs):
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        action = request.POST.get("action", "save_exhibitor_settings")
        active_tab = self.get_active_tab()

        if action == "save_exhibitor_settings":
            settings.allowed_fields = request.POST.getlist("exhibitors_access_voucher")
            settings.save(update_fields=["allowed_fields"])
            settings.log_action(
                LOG_SETTINGS_CHANGED,
                data={"allowed_fields": settings.allowed_fields},
                user=request.user,
            )
            messages.success(self.request, _("Settings have been saved."))
            return redirect(self.get_settings_url("exhibitors"))

        if action == "save_voucher_settings":
            voucher_defaults_form = ExhibitorVoucherDefaultsForm(
                request.POST,
                instance=settings,
                event=self.request.event,
            )
            if not voucher_defaults_form.is_valid():
                return self.render_to_response(self.get_context_data(voucher_defaults_form=voucher_defaults_form))
            voucher_defaults_form.save()
            settings.log_action(
                LOG_SETTINGS_CHANGED,
                data={"changed": voucher_defaults_form.changed_data},
                user=request.user,
            )
            messages.success(self.request, _("Settings have been saved."))
            return redirect(self.get_settings_url("vouchers"))

        if action == "save_call_settings":
            form = CallSettingsForm(
                request.POST,
                instance=settings,
                event=request.event,
            )
            if form.is_valid():
                form.save()
                settings.log_action(
                    LOG_CALL_SETTINGS_CHANGED,
                    data={"changed": form.changed_data},
                    user=request.user,
                )
                messages.success(self.request, _("Call settings have been saved."))
                return redirect(self.get_settings_url("call"))
            return self.render_to_response(self.get_context_data(call_settings_form=form))

        if action == "regenerate_call_secret":
            settings.regenerate_call_secret(requestor=request.user)
            messages.success(
                self.request,
                _("A new secret call link has been generated. The old link no longer works."),
            )
            return redirect(self.get_settings_url("call"))

        if action == "add_group":
            form = SponsorGroupForm(
                request.POST,
                event=request.event,
                prefix="new-group",
            )
            if form.is_valid():
                group = form.save(commit=False)
                group.event = request.event
                group.save()
                group.log_action(LOG_GROUP_ADDED, data={"name": group.localized_name}, user=request.user)
                messages.success(self.request, _("Sponsor group added."))
                return redirect(self.get_settings_url("sponsors"))

            messages.error(self.request, _("We could not save your changes. See below for details."))
            self.object_list = self.get_queryset()
            return self.render_to_response(
                self.get_context_data(
                    add_group_form=form,
                    show_add_group_form=True,
                )
            )

        if action == "rename_group":
            group = get_object_or_404(SponsorGroup, pk=request.POST.get("group_id"), event=request.event)
            form = SponsorGroupForm(
                request.POST,
                instance=group,
                event=request.event,
                prefix=f"group-{group.pk}",
            )
            if form.is_valid():
                form.save()
                group.log_action(LOG_GROUP_CHANGED, data={"changed": form.changed_data}, user=request.user)
                messages.success(self.request, _("Sponsor group updated."))
                return redirect(self.get_settings_url("sponsors"))

            messages.error(self.request, _("We could not save your changes. See below for details."))
            self.object_list = self.get_queryset()
            return self.render_to_response(
                self.get_context_data(
                    edit_group_forms={group.pk: form},
                    expanded_group_pk=group.pk,
                )
            )

        if action == "delete_group":
            group = get_object_or_404(SponsorGroup, pk=request.POST.get("group_id"), event=request.event)
            if group.partners.exists():
                messages.error(
                    self.request,
                    _("This sponsor group cannot be deleted while it is assigned to partners."),
                )
            else:
                group.log_action(LOG_GROUP_DELETED, data={"name": group.localized_name}, user=request.user)
                group.delete()
                messages.success(self.request, _("Sponsor group deleted."))
            return redirect(self.get_settings_url("sponsors"))

        messages.error(self.request, _("Unknown action."))
        return redirect(self.get_settings_url(active_tab))


class ExhibitorListView(EventPermissionRequiredMixin, FilteredListMixin, ListView):
    model = ExhibitorInfo
    permission = ("can_change_event_settings", "can_view_orders")
    template_name = "exhibitors/exhibitor_info.html"
    context_object_name = "exhibitors"
    partner_type = None

    def build_filter_form(self):
        return ExhibitorFilterForm(
            data=self.request.GET,
            event=self.request.event,
            organization_type=self.partner_type,
        )

    def get_queryset(self):
        queryset = ExhibitorInfo.objects.filter(event=self.request.event).select_related("sponsor_group")
        if self.partner_type == "sponsor":
            queryset = queryset.filter(is_sponsor=True).order_by("sponsor_position", "name", "pk")
        elif self.partner_type == "exhibitor":
            queryset = queryset.filter(is_exhibitor=True).order_by("exhibitor_position", "name", "pk")
        else:
            queryset = queryset.order_by("name", "pk")
        return self.apply_filters(queryset)

    def get(self, request, *args, **kwargs):
        if request.GET.get("download") == "yes":
            if not request.user.has_event_permission(
                request.event.organizer, request.event, "can_change_event_settings", request=request
            ):
                raise PermissionDenied()
            return self.download_keys_csv()
        return super().get(request, *args, **kwargs)

    def download_keys_csv(self):
        queryset = self.get_queryset()
        selected_pks = self.request.GET.getlist("pk")
        if selected_pks:
            queryset = queryset.filter(pk__in=selected_pks)

        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC, delimiter=",")
        writer.writerow([_("Name"), _("Booth ID"), _("Booth name"), _("Email"), _("Access key")])
        for exhibitor in queryset:
            writer.writerow(
                [
                    localize_event_text(exhibitor.name) or str(exhibitor.name),
                    exhibitor.booth_id or "",
                    exhibitor.localized_booth_name,
                    exhibitor.email or "",
                    exhibitor.key,
                ]
            )
        filename = {
            "sponsor": "sponsor-keys.csv",
            "exhibitor": "exhibitor-keys.csv",
        }.get(self.partner_type, "partner-keys.csv")
        response = HttpResponse(output.getvalue().encode("utf-8"), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["partner_type"] = self.partner_type
        context["reorder_enabled"] = not self.filter_form.filtered and not context["is_paginated"]
        context["send_vouchers_url"] = self.send_vouchers_url()
        context["query_string"] = self.request.GET.urlencode()
        if self.partner_type == "sponsor":
            context["sponsor_group_sections"] = self.build_sponsor_group_sections(context["exhibitors"])
        self.annotate_voucher_status(context["exhibitors"])
        return context

    def send_vouchers_url(self):
        """URL of the bulk voucher send action, or ``None`` when the user may not use it."""
        if not self.request.user.has_event_permission(
            self.request.event.organizer, self.request.event, "can_change_event_settings", request=self.request
        ):
            return None
        route = {
            "sponsor": "plugins:exhibition:sponsors.send_vouchers",
            "exhibitor": "plugins:exhibition:exhibitors.send_vouchers",
        }.get(self.partner_type)
        if route is None:
            return None
        return reverse(route, kwargs=event_kwargs(self.request.event))

    def annotate_voucher_status(self, exhibitors):
        ids = [exhibitor.pk for exhibitor in exhibitors]
        if not ids:
            return
        rows = ExhibitionEmailQueue.objects.filter(exhibitor_id__in=ids, role=mail_helpers.VOUCHERS)
        last_sent = dict(
            rows.filter(sent_at__isnull=False).values_list("exhibitor_id").annotate(last_sent=Max("sent_at"))
        )
        pending = set(rows.filter(sent_at__isnull=True).values_list("exhibitor_id", flat=True))
        for exhibitor in exhibitors:
            exhibitor.voucher_sent_at = last_sent.get(exhibitor.pk)
            exhibitor.voucher_pending = exhibitor.pk in pending

    def build_sponsor_group_sections(self, sponsors):
        groups = list(SponsorGroup.objects.filter(event=self.request.event).order_by("level", "pk"))
        sections = [{"group": group, "partners": []} for group in groups]
        ungrouped = {"group": None, "partners": []}
        section_by_group = {group.pk: section for group, section in zip(groups, sections)}
        for sponsor in sponsors:
            section = section_by_group.get(sponsor.sponsor_group_id, ungrouped)
            section["partners"].append(sponsor)
        return sections + [ungrouped]


class PublicExhibitorListView(ListView):
    model = ExhibitorInfo
    template_name = "exhibitors/public_list.html"
    context_object_name = "exhibitors"

    @cached_property
    def filter_form(self):
        return PublicExhibitorFilterForm(data=self.request.GET, event=self.request.event)

    def get_queryset(self):
        return self.filter_form.filter_qs(public_exhibitors_queryset(self.request.event))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.request.event
        context["filter_form"] = self.filter_form
        context["social_image"] = self.request.event.visible_header_image_url
        add_external_image_csp_sources(
            self.request,
            [
                image_url
                for exhibitor in context["exhibitors"]
                for image_url in (
                    exhibitor.visible_header_image_url,
                    exhibitor.visible_logo_url,
                )
                if image_url
            ],
        )
        return context


class PublicExhibitorDetailView(DetailView):
    model = ExhibitorInfo
    template_name = "exhibitors/public_detail.html"
    context_object_name = "exhibitor"

    def get_queryset(self):
        return public_exhibitors_queryset(self.request.event)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exhibitors = list(public_exhibitors_queryset(self.request.event))
        context["event"] = self.request.event
        context["social_image"] = self.object.visible_header_image_url or self.object.visible_logo_url
        if len(exhibitors) > 1:
            current_index = next(index for index, exhibitor in enumerate(exhibitors) if exhibitor.pk == self.object.pk)
            context["previous_exhibitor"] = exhibitors[current_index - 1]
            context["next_exhibitor"] = exhibitors[(current_index + 1) % len(exhibitors)]
        else:
            context["previous_exhibitor"] = None
            context["next_exhibitor"] = None

        context["social_links"] = [serialize_social_link(link) for link in self.object.social_links.all()]
        context["related_sessions"] = public_exhibitor_sessions(self.object, self.request.user)

        add_external_image_csp_sources(
            self.request,
            [
                image_url
                for image_url in (
                    self.object.visible_header_image_url,
                    self.object.visible_logo_url,
                )
                if image_url
            ],
        )
        return context


class PublicCallView(PublicCallEnabledMixin, TemplateView):
    template_name = "exhibitors/public_call.html"
    hide_after_deadline = True
    enforce_private = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.request.event
        context["settings"] = self.get_exhibition_settings()
        if self.request.user.is_authenticated:
            context["user_proposals"] = ExhibitionProposal.objects.filter(
                event=self.request.event,
                user=self.request.user,
            )
        return context


class PublicCallSecretView(PublicCallView):
    enforce_private = False

    def grant_secret_access(self, request, secret):
        settings = self.get_exhibition_settings()
        if not settings.call_enabled or not settings.call_private or not secret or secret != settings.call_secret:
            raise Http404()
        request.session[call_access_session_key(request.event)] = secret
        return settings

    def dispatch(self, request, *args, **kwargs):
        self.grant_secret_access(request, kwargs.get("secret"))
        return super().dispatch(request, *args, **kwargs)


class UserProposalListView(PublicCallEnabledMixin, PublicEventLoginRequiredMixin, ListView):
    model = ExhibitionProposal
    template_name = "exhibitors/public_proposal_list.html"
    context_object_name = "proposals"
    enforce_private = True
    require_call_enabled = False

    def has_private_call_access(self, settings):
        if super().has_private_call_access(settings):
            return True
        user = self.request.user
        if not user.is_authenticated:
            return False
        return ExhibitionProposal.objects.filter(event=self.request.event, user=user).exists()

    def get_queryset(self):
        return ExhibitionProposal.objects.filter(
            event=self.request.event,
            user=self.request.user,
        ).order_by("-updated", "-created")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings = self.get_exhibition_settings()
        context["settings"] = settings
        for proposal in context["proposals"]:
            proposal.submitter_can_edit = proposal.editable and (
                not proposal.requires_open_call_to_edit or settings.call_is_open
            )
        return context


def formset_has_entries(formset):
    """True when a link formset holds at least one row that is filled in and not deleted."""
    if formset is None:
        return True
    for form in formset.forms:
        if not form.cleaned_data or form.cleaned_data.get("DELETE"):
            continue
        if any(value for name, value in form.cleaned_data.items() if name != "DELETE"):
            return True
    return False


class ProposalLinkFormsetMixin:
    social_formset_prefix = "social_links"

    def get_proposal_field_settings(self):
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        return settings.normalized_proposal_field_settings

    def proposal_field_is_active(self, key):
        return self.get_proposal_field_settings()[key]["active"]

    def proposal_field_is_required(self, key):
        return self.get_proposal_field_settings()[key]["required"]

    def get_formset_instance(self):
        obj = getattr(self, "object", None)
        if obj is not None:
            return obj
        return ExhibitionProposal(event=self.request.event, user=self.request.user)

    def get_social_formset(self):
        return ExhibitionProposalSocialLinkFormSet(
            data=self.request.POST if self.request.method == "POST" else None,
            instance=self.get_formset_instance(),
            prefix=self.social_formset_prefix,
        )

    def post_with_formsets(self):
        form = self.get_form()
        self.social_media_formset = self.get_social_formset() if self.proposal_field_is_active("social_links") else None

        valid = form.is_valid() and (self.social_media_formset is None or self.social_media_formset.is_valid())

        if (
            valid
            and self.proposal_field_is_required("social_links")
            and not formset_has_entries(self.social_media_formset)
        ):
            self.social_media_formset._non_form_errors = self.social_media_formset.error_class(
                [_("Add at least one social media link.")]
            )
            valid = False

        if valid:
            return self.form_valid(form)
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        allow_blob_image_previews(self.request)
        context["social_media_formset"] = kwargs.get(
            "social_media_formset",
            getattr(self, "social_media_formset", None) or self.get_social_formset(),
        )
        context["social_link_prefixes"] = social_link_prefixes()
        context["settings"] = self.get_exhibition_settings()
        context.setdefault("can_edit", True)
        return context

    def form_invalid(self, form):
        messages.error(self.request, _("We could not save your changes. See below for details."))
        return self.render_to_response(self.get_context_data(form=form))

    def save_link_formsets(self):
        if self.social_media_formset is not None:
            self.social_media_formset.instance = self.object
            self.social_media_formset.save()


class UserProposalCreateView(
    ProposalLinkFormsetMixin,
    PublicCallEnabledMixin,
    PublicEventLoginRequiredMixin,
    CreateView,
):
    model = ExhibitionProposal
    form_class = ExhibitionProposalForm
    template_name = "exhibitors/public_proposal_form.html"

    def dispatch(self, request, *args, **kwargs):
        settings = ExhibitorSettings.objects.get_or_create(event=request.event)[0]
        if not settings.call_enabled:
            raise Http404()
        if settings.call_private and not self.has_private_call_access(settings):
            raise Http404()
        if not settings.call_is_open:
            if settings.call_hide_after_deadline:
                raise Http404()
            messages.error(request, _("The call for exhibitors is closed."))
            return redirect("plugins:exhibition:public_call", **event_kwargs(request.event))
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        kwargs["draft_save"] = self.request.POST.get("action") == "draft"
        return kwargs

    def post(self, request, *args, **kwargs):
        self.object = None
        return self.post_with_formsets()

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        form.instance.user = self.request.user
        if self.request.POST.get("action") == "draft":
            form.instance.state = ExhibitionProposalState.DRAFT
            form.instance.submitted = None
        else:
            form.instance.state = ExhibitionProposalState.SUBMITTED
            form.instance.submitted = timezone.now()
        response = super().form_valid(form)
        self.save_link_formsets()
        if form.instance.state == ExhibitionProposalState.SUBMITTED:
            send_proposal_confirmation(self.request.event, self.object, self.request.user)
        messages.success(self.request, _("Your request has been saved."))
        return response

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:proposal.user_list",
            kwargs=event_kwargs(self.request.event),
        )


class UserProposalEditView(
    ProposalLinkFormsetMixin,
    PublicCallEnabledMixin,
    PublicEventLoginRequiredMixin,
    UpdateView,
):
    model = ExhibitionProposal
    form_class = ExhibitionProposalForm
    template_name = "exhibitors/public_proposal_form.html"
    slug_field = "code"
    slug_url_kwarg = "code"
    require_call_enabled = False

    def get_queryset(self):
        return ExhibitionProposal.objects.filter(
            event=self.request.event,
            user=self.request.user,
        ).prefetch_related("answers", "answers__options")

    def can_edit(self):
        if not self.object.editable:
            return False
        if not self.object.requires_open_call_to_edit:
            return True
        return self.get_exhibition_settings().call_is_open

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        kwargs["read_only"] = not self.can_edit()
        kwargs["draft_save"] = self.request.POST.get("action") == "draft" and not self.state_is_locked()
        return kwargs

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.can_edit():
            messages.error(request, _("This request can no longer be edited."))
            return redirect(self.get_success_url())
        return self.post_with_formsets()

    def state_is_locked(self):
        return self.object.state == ExhibitionProposalState.ACCEPTED

    @transaction.atomic
    def form_valid(self, form):
        previous_state = self.object.state
        if not self.state_is_locked():
            if self.request.POST.get("action") == "draft":
                form.instance.state = ExhibitionProposalState.DRAFT
                form.instance.submitted = None
            else:
                form.instance.state = ExhibitionProposalState.SUBMITTED
                form.instance.submitted = form.instance.submitted or timezone.now()
        else:
            form.instance.profile_edited_at = timezone.now()
            if not form.instance.accepted_profile_snapshot:
                baseline = ExhibitionProposal.objects.get(pk=form.instance.pk)
                form.instance.accepted_profile_snapshot = baseline.submitter_profile_values()
        response = super().form_valid(form)
        self.save_link_formsets()
        if (
            form.instance.state == ExhibitionProposalState.SUBMITTED
            and previous_state != ExhibitionProposalState.SUBMITTED
        ):
            send_proposal_confirmation(self.request.event, self.object, self.request.user)
        if form.changed_data:
            self.object.log_action(
                LOG_PROPOSAL_CHANGED,
                data={"changed": form.changed_data, "by": "submitter"},
                user=self.request.user,
            )
        if self.object.approved_exhibitor_id:
            sync_exhibitor_from_proposal(self.object, requestor=self.request.user)
        messages.success(self.request, _("Your changes have been saved."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = self.can_edit()
        context["state_locked"] = self.state_is_locked()
        context["already_submitted"] = self.object.state == ExhibitionProposalState.SUBMITTED
        return context

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:proposal.user_list",
            kwargs=event_kwargs(self.request.event),
        )


class UserProposalWithdrawView(PublicCallEnabledMixin, PublicEventLoginRequiredMixin, DetailView):
    model = ExhibitionProposal
    template_name = "exhibitors/public_proposal_withdraw.html"
    context_object_name = "proposal"
    slug_field = "code"
    slug_url_kwarg = "code"

    def get_queryset(self):
        return ExhibitionProposal.objects.filter(
            event=self.request.event,
            user=self.request.user,
        )

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.can_be_withdrawn:
            messages.error(request, _("This proposal can no longer be withdrawn."))
            return redirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.can_be_withdrawn:
            self.object.withdraw(requestor=request.user)
            messages.success(request, _("Your request has been withdrawn."))
        else:
            messages.error(request, _("This request can no longer be withdrawn."))
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:proposal.user_list",
            kwargs=event_kwargs(self.request.event),
        )


class UserProposalReinstateView(PublicCallEnabledMixin, PublicEventLoginRequiredMixin, DetailView):
    model = ExhibitionProposal
    template_name = "exhibitors/public_proposal_reinstate.html"
    context_object_name = "proposal"
    slug_field = "code"
    slug_url_kwarg = "code"

    def get_queryset(self):
        return ExhibitionProposal.objects.filter(
            event=self.request.event,
            user=self.request.user,
        )

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.can_be_reinstated:
            messages.error(request, _("This request can no longer be reinstated."))
            return redirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.can_be_reinstated:
            self.object.reopen(requestor=request.user)
            messages.success(request, _("Your request has been reinstated and is pending review again."))
        else:
            messages.error(request, _("This request can no longer be reinstated."))
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:proposal.user_list",
            kwargs=event_kwargs(self.request.event),
        )


class ExhibitorLinkFormsetMixin:
    social_formset_prefix = "social_links"

    def get_proposal_field_settings(self):
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        return settings.normalized_proposal_field_settings

    def proposal_field_is_active(self, key):
        return self.get_proposal_field_settings()[key]["active"]

    def proposal_field_is_required(self, key):
        return self.get_proposal_field_settings()[key]["required"]

    def get_formset_instance(self):
        obj = getattr(self, "object", None)
        return obj if obj is not None else ExhibitorInfo(event=self.request.event)

    def get_social_formset(self):
        return ExhibitorSocialLinkFormSet(
            data=self.request.POST if self.request.method == "POST" else None,
            instance=self.get_formset_instance(),
            prefix=self.social_formset_prefix,
        )

    def post_with_formsets(self):
        form = self.get_form()
        self.social_media_formset = self.get_social_formset() if self.proposal_field_is_active("social_links") else None

        valid = form.is_valid() and (self.social_media_formset is None or self.social_media_formset.is_valid())

        if (
            valid
            and self.proposal_field_is_required("social_links")
            and not formset_has_entries(self.social_media_formset)
        ):
            self.social_media_formset._non_form_errors = self.social_media_formset.error_class(
                [_("Add at least one social media link.")]
            )
            valid = False

        if valid:
            return self.form_valid(form)
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        allow_blob_image_previews(self.request)
        show_social_links = self.proposal_field_is_active("social_links")
        context["social_media_formset"] = kwargs.get(
            "social_media_formset",
            getattr(self, "social_media_formset", self.get_social_formset() if show_social_links else None),
        )
        context["social_link_prefixes"] = social_link_prefixes()
        return context

    def form_invalid(self, form):
        messages.error(self.request, _("We could not save your changes. See below for details."))
        return self.render_to_response(self.get_context_data(form=form))

    def save_link_formsets(self):
        if self.social_media_formset is not None:
            self.social_media_formset.instance = self.object
            self.social_media_formset.save()


class SponsorGroupFrontPageToggleView(EventPermissionRequiredMixin, View):
    permission = "can_change_settings"

    def post(self, request, *args, **kwargs):
        group = get_object_or_404(SponsorGroup, pk=kwargs["pk"], event=request.event)
        group.show_on_front_page = not group.show_on_front_page
        group.save(update_fields=["show_on_front_page"])
        return JsonResponse({"show_on_front_page": group.show_on_front_page})


class SponsorGroupReorderView(EventPermissionRequiredMixin, View):
    permission = "can_change_settings"

    def post(self, request, *args, **kwargs):
        try:
            group_ids = json.loads(request.body.decode("utf-8")).get("group_ids", [])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"detail": _("Invalid request body.")}, status=400)

        if not isinstance(group_ids, list):
            return JsonResponse({"detail": _("Invalid sponsor group IDs.")}, status=400)

        try:
            group_ids = [int(group_id) for group_id in group_ids]
        except (TypeError, ValueError):
            return JsonResponse({"detail": _("Invalid sponsor group IDs.")}, status=400)

        if len(group_ids) != len(set(group_ids)):
            return JsonResponse(
                {"detail": _("Sponsor group IDs must be unique.")},
                status=400,
            )

        groups = list(SponsorGroup.objects.filter(event=request.event).order_by("level", "pk"))
        known_group_ids = [group.pk for group in groups]
        if len(group_ids) != len(known_group_ids) or set(group_ids) != set(known_group_ids):
            return JsonResponse(
                {"detail": _("Reorder request must include each sponsor group exactly once.")},
                status=400,
            )

        group_lookup = {group.pk: group for group in groups}
        ordered_groups = [group_lookup[group_id] for group_id in group_ids]

        with transaction.atomic():
            for index, group in enumerate(ordered_groups, start=1):
                group.level = index
            SponsorGroup.objects.bulk_update(ordered_groups, ["level"])

        return JsonResponse({"levels": [{"id": group.pk, "level": group.level} for group in ordered_groups]})


class PartnerReorderMixin(EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"
    position_field = None

    def get_scope_queryset(self, request):
        raise NotImplementedError

    def post(self, request, *args, **kwargs):
        order_param = (request.POST.get("order") or "").strip()
        if not order_param:
            return HttpResponse(status=400)

        try:
            ids = [int(token) for token in order_param.split(",") if token.strip()]
        except ValueError:
            return HttpResponse(status=400)
        if not ids or len(ids) != len(set(ids)):
            return HttpResponse(status=400)

        partners = {partner.pk: partner for partner in self.get_scope_queryset(request)}
        if set(ids) != set(partners):
            return HttpResponse(status=400)

        ordered = [partners[value] for value in ids]
        with transaction.atomic():
            for index, partner in enumerate(ordered):
                setattr(partner, self.position_field, index)
            ExhibitorInfo.objects.bulk_update(ordered, [self.position_field])

        return HttpResponse(status=204)


class ExhibitorReorderView(PartnerReorderMixin):
    position_field = "exhibitor_position"

    def get_scope_queryset(self, request):
        return ExhibitorInfo.objects.filter(event=request.event, is_exhibitor=True)


class SponsorReorderView(PartnerReorderMixin):
    position_field = "sponsor_position"

    def get_scope_queryset(self, request):
        group_id = request.GET.get("group_id")
        queryset = ExhibitorInfo.objects.filter(event=request.event, is_sponsor=True)
        if group_id in (None, "", "none"):
            return queryset.filter(sponsor_group__isnull=True)
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return queryset.none()
        return queryset.filter(sponsor_group_id=group_id)


class CallTextPreviewView(EventPermissionRequiredMixin, View):
    """Render draft Call text with the same styling as the public call page.

    Consumed by core's shared ``richtextPreview.js`` (``data-email-preview-*``
    attributes): the body text is posted as one ``body_<locale>`` field per
    rendered locale.
    """

    permission = "can_change_settings"

    def post(self, request, *args, **kwargs):
        event_locales = request.event.settings.locales
        previews = {}
        for locale in event_locales:
            text = request.POST.get(f"body_{locale}", "")
            previews[locale] = str(rich_text(text)) if text else ""
        return JsonResponse({"previews": previews})


class ProposalListView(EventPermissionRequiredMixin, FilteredListMixin, ListView):
    model = ExhibitionProposal
    permission = ("can_change_event_settings", "can_change_exhibition_proposals", "is_exhibition_reviewer")
    template_name = "exhibitors/proposal_list.html"
    context_object_name = "proposals"

    @cached_property
    def hide_applicant_emails(self):
        return should_hide_applicant_emails(self.request.user, self.request.event, request=self.request)

    def build_filter_form(self):
        return ProposalFilterForm(data=self.request.GET, hide_emails=self.hide_applicant_emails)

    def get_queryset(self):
        queryset = (
            ExhibitionProposal.objects.filter(event=self.request.event)
            .select_related("user", "sponsor_group", "approved_exhibitor")
            .order_by("-updated", "-created", "-pk")
        )
        return self.apply_filters(queryset)

    def can_manage(self):
        return self.request.user.has_event_permission(
            self.request.event.organizer,
            self.request.event,
            ("can_change_event_settings", "can_change_exhibition_proposals"),
            request=self.request,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_applicant_emails"] = self.hide_applicant_emails
        can_manage = self.can_manage()
        context["can_manage"] = can_manage
        if can_manage:
            for proposal in context["proposals"]:
                proposal.review_actions = proposal.available_review_actions()
                proposal.bulk_actions = proposal.available_bulk_actions()
        return context


class ProposalDetailView(EventPermissionRequiredMixin, UpdateView):
    model = ExhibitionProposal
    permission = ("can_change_event_settings", "can_change_exhibition_proposals", "is_exhibition_reviewer")
    template_name = "exhibitors/proposal_detail.html"
    context_object_name = "proposal"
    slug_field = "code"
    slug_url_kwarg = "code"

    def can_manage(self):
        return self.request.user.has_event_permission(
            self.request.event.organizer,
            self.request.event,
            ("can_change_event_settings", "can_change_exhibition_proposals"),
            request=self.request,
        )

    def can_edit_exhibitor(self):
        return self.request.user.has_event_permission(
            self.request.event.organizer,
            self.request.event,
            "can_change_event_settings",
            request=self.request,
        )

    def can_review(self):
        return self.can_manage() and self.object.state == ExhibitionProposalState.SUBMITTED

    def get_form_class(self):
        if self.can_review():
            return ExhibitionProposalReviewForm
        return ExhibitionProposalReviewNotesForm

    def get_queryset(self):
        return (
            ExhibitionProposal.objects.filter(event=self.request.event)
            .select_related("user", "sponsor_group", "approved_exhibitor")
            .prefetch_related(
                "answers",
                "answers__options",
                "answers__question",
                "social_links",
            )
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["answers"] = self.object.answers.select_related("question").prefetch_related("options")
        context["profile_changes"] = self.object.profile_field_changes() if self.object.edited_after_acceptance else []
        context["can_manage"] = self.can_manage()
        context["can_review"] = self.can_review()
        context["can_edit_exhibitor"] = self.can_edit_exhibitor()
        context["hide_applicant_emails"] = should_hide_applicant_emails(
            self.request.user, self.request.event, request=self.request
        )
        return context

    @transaction.atomic
    def form_valid(self, form):
        self.object = form.save()
        if form.changed_data:
            self.object.log_action(
                LOG_PROPOSAL_CHANGED,
                data={"changed": form.changed_data},
                user=self.request.user,
            )
        action = self.request.POST.get("action", "save")
        if action in PROPOSAL_REVIEW_ACTIONS:
            if not self.can_manage():
                raise PermissionDenied()
            if not self.object.can_transition_to(PROPOSAL_REVIEW_ACTIONS[action]):
                messages.error(self.request, _("This request can no longer be changed to that state."))
                return redirect(self.get_success_url())
            return self.perform_review_action(action)

        messages.success(self.request, _("Review details saved."))
        return redirect(self.get_success_url())

    def perform_review_action(self, action):
        requestor = self.request.user
        if action == "approve":
            exhibitor = self.object.approve(requestor=requestor)
            messages.success(
                self.request,
                _("Request approved and partner profile created. An acceptance email was placed in the outbox."),
            )
            if self.can_edit_exhibitor():
                return redirect(
                    "plugins:exhibition:edit",
                    **event_kwargs(self.request.event),
                    pk=exhibitor.pk,
                )
        elif action == "reject":
            self.object.reject(requestor=requestor)
            messages.success(self.request, _("Request rejected. A rejection email was placed in the outbox."))
        elif action == "withdraw":
            self.object.withdraw(requestor=requestor)
            messages.success(self.request, _("Request withdrawn."))
        elif action == "reopen":
            self.object.reopen(requestor=requestor)
            messages.success(self.request, _("Request reopened for review."))
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:proposal.detail",
            kwargs={**event_kwargs(self.request.event), "code": self.object.code},
        )


class ProposalActionView(EventPermissionRequiredMixin, View):
    permission = ("can_change_event_settings", "can_change_exhibition_proposals")
    valid_actions = set(PROPOSAL_REVIEW_ACTIONS)

    def get_proposals(self, request, select_all, codes):
        """Every request matching the active filters when selecting across pages, else the checked rows."""
        queryset = ExhibitionProposal.objects.filter(event=request.event).select_related("approved_exhibitor")
        if not select_all:
            return queryset.filter(code__in=codes)
        filter_form = ProposalFilterForm(
            data=request.POST,
            hide_emails=should_hide_applicant_emails(request.user, request.event, request=request),
        )
        if filter_form.is_valid():
            queryset = filter_form.filter_qs(queryset)
        return queryset

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        codes = request.POST.getlist("proposal")
        select_all = request.POST.get("all") == "1"
        if action not in self.valid_actions or not (codes or select_all):
            return self.respond(request, False, _("No valid action was selected."), [], 0)

        target_state = PROPOSAL_REVIEW_ACTIONS[action]
        proposals = self.get_proposals(request, select_all, codes)
        results = []
        changed = 0
        skipped = 0
        with transaction.atomic():
            for proposal in proposals:
                if not proposal.can_transition_to(target_state):
                    skipped += 1
                    continue
                self.apply_action(proposal, action)
                changed += 1
                if select_all:
                    continue
                results.append(
                    {
                        "code": proposal.code,
                        "state": proposal.state,
                        "state_display": proposal.get_state_display(),
                        "actions": proposal.available_review_actions(),
                        "bulk_actions": proposal.available_bulk_actions(),
                    }
                )
        return self.respond(
            request,
            True,
            self.build_message(action, changed, skipped),
            results,
            skipped,
            reload=select_all and changed > 0,
        )

    def apply_action(self, proposal, action):
        if action == "approve":
            proposal.approve(requestor=self.request.user)
        elif action == "reject":
            proposal.reject(requestor=self.request.user)
        elif action == "withdraw":
            proposal.withdraw(requestor=self.request.user)
        elif action == "reopen":
            proposal.reopen(requestor=self.request.user)

    def build_message(self, action, count, skipped):
        if count:
            templates = {
                "approve": ngettext("%(count)d request was approved.", "%(count)d requests were approved.", count),
                "reject": ngettext("%(count)d request was rejected.", "%(count)d requests were rejected.", count),
                "withdraw": ngettext("%(count)d request was withdrawn.", "%(count)d requests were withdrawn.", count),
                "reopen": ngettext("%(count)d request was reopened.", "%(count)d requests were reopened.", count),
            }
            message = templates[action] % {"count": count}
        else:
            message = _("No proposals were updated.")
        if skipped:
            skipped_message = ngettext(
                "%(skipped)d was skipped because it was already processed.",
                "%(skipped)d were skipped because they were already processed.",
                skipped,
            ) % {"skipped": skipped}
            message = f"{message} {skipped_message}"
        return message

    def respond(self, request, ok, message, results, skipped, reload=False):
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(
                {"ok": ok, "message": str(message), "results": results, "skipped": skipped, "reload": reload},
                status=200 if ok else 400,
            )
        if ok:
            messages.success(request, message)
        else:
            messages.error(request, message)
        return redirect("plugins:exhibition:proposal.list", **event_kwargs(request.event))


class ExhibitionQuestionListView(EventPermissionRequiredMixin, ListView):
    model = ExhibitionQuestion
    permission = "can_change_settings"
    template_name = "exhibitors/call_questions.html"
    context_object_name = "questions"

    def get_queryset(self):
        return ExhibitionQuestion.objects.filter(event=self.request.event)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        field_settings = settings.normalized_proposal_field_settings
        field_definitions = {field["key"]: field for field in PROPOSAL_DEFAULT_FIELDS}

        rows = []
        for key in PROPOSAL_DEFAULT_FIELD_KEYS:
            definition = field_definitions[key]
            rows.append(
                {
                    "sort_position": field_settings[key]["position"],
                    "sort_kind": 0,
                    "dragsort_id": key,
                    "input_prefix": key,
                    "label": field_settings[key]["label"],
                    "active": field_settings[key]["active"],
                    "required": field_settings[key]["required"],
                    "supports_required": definition.get("supports_required", True),
                    "active_locked": definition.get("active_locked", False),
                    "required_locked": definition.get("required_locked", False),
                    "lock_notice": field_settings[key]["lock_notice"],
                    "is_custom": False,
                }
            )
        for question in context["questions"]:
            rows.append(
                {
                    "sort_position": question.position,
                    "sort_kind": 1,
                    "dragsort_id": question.pk,
                    "input_prefix": f"question_{question.pk}",
                    "label": question.localized_question,
                    "active": question.active,
                    "required": question.required,
                    "supports_required": True,
                    "active_locked": False,
                    "required_locked": False,
                    "lock_notice": "",
                    "is_custom": True,
                    "pk": question.pk,
                }
            )
        rows.sort(key=lambda row: (row["sort_position"], row["sort_kind"]))
        context["proposal_fields"] = rows
        return context

    def post(self, request, *args, **kwargs):
        settings = ExhibitorSettings.objects.get_or_create(event=request.event)[0]

        order_param = request.POST.get("order")
        if order_param:
            self.save_field_order(settings, order_param)
            return HttpResponse(status=204)

        proposal_field_settings = settings.normalized_proposal_field_settings

        for field in PROPOSAL_DEFAULT_FIELDS:
            key = field["key"]
            is_active = field.get("active_locked") or request.POST.get(f"{key}_active") == "on"
            proposal_field_settings[key]["active"] = is_active
            proposal_field_settings[key]["required"] = is_active and (
                field.get("required_locked")
                or (field.get("supports_required", True) and request.POST.get(f"{key}_required") == "required")
            )
            if field.get("supports_required") is False:
                proposal_field_settings[key]["required"] = False

        settings.proposal_field_settings = storable_proposal_field_settings(proposal_field_settings)
        settings.save(update_fields=["proposal_field_settings"])

        questions = list(ExhibitionQuestion.objects.filter(event=request.event))
        for question in questions:
            question.active = request.POST.get(f"question_{question.pk}_active") == "on"
            question.required = request.POST.get(f"question_{question.pk}_required") == "required"
        if questions:
            ExhibitionQuestion.objects.bulk_update(questions, ["active", "required"])

        messages.success(request, _("Exhibitor form settings have been saved."))
        return redirect("plugins:exhibition:call.questions", **event_kwargs(request.event))

    def save_field_order(self, settings, order_str):
        proposal_field_settings = settings.normalized_proposal_field_settings
        orderable_key_set = set(PROPOSAL_DEFAULT_FIELD_KEYS)
        questions = {question.pk: question for question in ExhibitionQuestion.objects.filter(event=settings.event)}
        seen_keys = set()
        seen_question_pks = set()
        reordered_questions = []
        position = 0
        for token in (raw_token.strip() for raw_token in order_str.split(",")):
            if token in orderable_key_set and token not in seen_keys:
                seen_keys.add(token)
                proposal_field_settings[token]["position"] = position
                position += 1
            elif token.isdigit() and int(token) in questions and int(token) not in seen_question_pks:
                question = questions[int(token)]
                question.position = position
                seen_question_pks.add(question.pk)
                reordered_questions.append(question)
                position += 1
        for key in PROPOSAL_DEFAULT_FIELD_KEYS:
            if key not in seen_keys:
                proposal_field_settings[key]["position"] = position
                position += 1
        remaining_questions = sorted(
            (question for pk, question in questions.items() if pk not in seen_question_pks),
            key=lambda question: (question.position, question.pk),
        )
        for question in remaining_questions:
            question.position = position
            reordered_questions.append(question)
            position += 1
        settings.proposal_field_settings = storable_proposal_field_settings(proposal_field_settings)
        settings.save(update_fields=["proposal_field_settings"])
        if reordered_questions:
            ExhibitionQuestion.objects.bulk_update(reordered_questions, ["position"])


class ExhibitionQuestionOptionFormSetMixin:
    option_formset_prefix = "options"

    @cached_property
    def option_formset(self):
        requires_option = (
            self.request.POST.get("variant") in QUESTION_OPTION_VARIANTS
            if self.request.method == "POST"
            else self.object is not None and self.object.variant in QUESTION_OPTION_VARIANTS
        )
        return ExhibitionQuestionOptionFormSet(
            self.request.POST if self.request.method == "POST" else None,
            queryset=self.object.options.all() if self.object else ExhibitionQuestionOption.objects.none(),
            event=self.request.event,
            prefix=self.option_formset_prefix,
            requires_option=requires_option,
        )

    def save_option_formset(self):
        if self.object.variant not in QUESTION_OPTION_VARIANTS:
            self.object.options.all().delete()
            return

        deleted_forms = self.option_formset.deleted_forms
        for option_form in deleted_forms:
            if option_form.instance.pk is not None:
                option_form.instance.delete()

        ordered_forms = self.option_formset.ordered_forms + [
            option_form
            for option_form in self.option_formset.extra_forms
            if option_form not in self.option_formset.ordered_forms and option_form not in deleted_forms
        ]
        option_forms = [
            option_form
            for option_form in ordered_forms
            if option_form not in deleted_forms and option_form.cleaned_data.get("answer")
        ]
        for position, option_form in enumerate(option_forms):
            option = option_form.save(commit=False)
            option.question = self.object
            option.position = position
            option.save()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["option_formset"] = self.option_formset
        return context


class ExhibitionQuestionCreateView(EventPermissionRequiredMixin, ExhibitionQuestionOptionFormSetMixin, CreateView):
    model = ExhibitionQuestion
    form_class = ExhibitionQuestionForm
    permission = "can_change_settings"
    template_name = "exhibitors/call_question_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def form_valid(self, form):
        if not self.option_formset.is_valid():
            return self.form_invalid(form)
        with transaction.atomic():
            response = super().form_valid(form)
            self.save_option_formset()
            self.object.log_action(
                LOG_QUESTION_ADDED,
                data={"question": self.object.localized_question},
                user=self.request.user,
            )
        return response

    def form_invalid(self, form):
        messages.error(self.request, _("We could not save your changes. See below for details."))
        return super().form_invalid(form)

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:call.questions",
            kwargs=event_kwargs(self.request.event),
        )


class ExhibitionQuestionEditView(EventPermissionRequiredMixin, ExhibitionQuestionOptionFormSetMixin, UpdateView):
    model = ExhibitionQuestion
    form_class = ExhibitionQuestionForm
    permission = "can_change_settings"
    template_name = "exhibitors/call_question_form.html"

    def get_queryset(self):
        return ExhibitionQuestion.objects.filter(event=self.request.event)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def form_valid(self, form):
        if not self.option_formset.is_valid():
            return self.form_invalid(form)
        with transaction.atomic():
            response = super().form_valid(form)
            self.save_option_formset()
            self.object.log_action(
                LOG_QUESTION_CHANGED,
                data={"changed": form.changed_data},
                user=self.request.user,
            )
        return response

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:call.questions",
            kwargs=event_kwargs(self.request.event),
        )


class ExhibitionQuestionDeleteView(EventPermissionRequiredMixin, DeleteView):
    model = ExhibitionQuestion
    permission = "can_change_settings"
    template_name = "exhibitors/call_question_delete.html"

    def get_queryset(self):
        return ExhibitionQuestion.objects.filter(event=self.request.event)

    def form_valid(self, form):
        self.object.log_action(
            LOG_QUESTION_DELETED,
            data={"question": self.object.localized_question},
            user=self.request.user,
        )
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:call.questions",
            kwargs=event_kwargs(self.request.event),
        )


class DefaultFieldMixin(EventPermissionRequiredMixin):
    permission = "can_change_settings"

    def dispatch(self, request, *args, **kwargs):
        if kwargs.get("key") not in PROPOSAL_DEFAULT_FIELD_KEYS:
            raise Http404(_("The requested form field does not exist."))
        return super().dispatch(request, *args, **kwargs)

    def get_exhibition_settings(self):
        return ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]

    def get_field_setting(self):
        return self.get_exhibition_settings().normalized_proposal_field_settings[self.kwargs["key"]]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["field_setting"] = self.get_field_setting()
        return context

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:call.questions",
            kwargs=event_kwargs(self.request.event),
        )


class ExhibitionDefaultFieldEditView(DefaultFieldMixin, FormView):
    form_class = ExhibitionDefaultFieldForm
    template_name = "exhibitors/call_default_field_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        field_setting = self.get_field_setting()
        kwargs["field_setting"] = field_setting
        kwargs.setdefault(
            "initial",
            {
                "label": field_setting["custom_label"] or "",
                "help_text": field_setting["custom_help_text"] or "",
            },
        )
        return kwargs

    def form_valid(self, form):
        settings = self.get_exhibition_settings()
        proposal_field_settings = settings.normalized_proposal_field_settings
        key = self.kwargs["key"]
        proposal_field_settings[key]["custom_label"] = form.cleaned_data["label"].strip() or None
        proposal_field_settings[key]["custom_help_text"] = form.cleaned_data["help_text"].strip() or None
        settings.proposal_field_settings = storable_proposal_field_settings(proposal_field_settings)
        settings.save(update_fields=["proposal_field_settings"])
        messages.success(self.request, _("Your changes have been saved."))
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        messages.error(self.request, _("We could not save your changes. See below for details."))
        return super().form_invalid(form)


class ExhibitionDefaultFieldResetView(DefaultFieldMixin, TemplateView):
    template_name = "exhibitors/call_default_field_reset.html"

    def post(self, request, *args, **kwargs):
        settings = self.get_exhibition_settings()
        proposal_field_settings = settings.normalized_proposal_field_settings
        key = kwargs["key"]
        proposal_field_settings[key]["custom_label"] = None
        proposal_field_settings[key]["custom_help_text"] = None
        settings.proposal_field_settings = storable_proposal_field_settings(proposal_field_settings)
        settings.save(update_fields=["proposal_field_settings"])
        messages.success(request, _("The field has been reset to its default."))
        return redirect(self.get_success_url())


class ExhibitorCreateView(ExhibitorLinkFormsetMixin, EventPermissionRequiredMixin, CreateView):
    model = ExhibitorInfo
    form_class = ExhibitorInfoForm
    template_name = "exhibitors/add.html"
    permission = "can_change_event_settings"
    partner_type = None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["partner_type"] = self.partner_type
        return kwargs

    def post(self, request, *args, **kwargs):
        self.object = None
        return self.post_with_formsets()

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event

        # Only generate booth_id for exhibitors if none was provided.
        if form.cleaned_data.get("is_exhibitor", True) and not form.cleaned_data.get("booth_id"):
            form.instance.booth_id = generate_booth_id(event=self.request.event)

        response = super().form_valid(form)
        self.save_link_formsets()
        self.object.log_action(
            LOG_PARTNER_ADDED,
            data={"name": localize_event_text(self.object.name), "booth_id": self.object.booth_id},
            user=self.request.user,
        )
        if access_newly_granted(form.instance) and queue_exhibitor_access_mail(
            self.request.event, self.object, self.request.user
        ):
            messages.info(self.request, _("An access-credentials email was placed in the outbox."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action"] = "create"
        context["partner_type"] = self.partner_type
        context["page_title"] = {
            "sponsor": _("Add a Sponsor"),
            "exhibitor": _("Add an Exhibitor"),
        }.get(self.partner_type, _("Add an Exhibitor or Sponsor"))
        return context

    def get_success_url(self):
        return partner_list_url(self.request.event, self.partner_type)


class ExhibitorEditView(ExhibitorLinkFormsetMixin, EventPermissionRequiredMixin, UpdateView):
    model = ExhibitorInfo
    form_class = ExhibitorInfoForm
    template_name = "exhibitors/add.html"
    permission = "can_change_event_settings"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.post_with_formsets()

    def get_initial(self):
        initial = super().get_initial()
        obj = self.get_object()
        initial["lead_scanning_enabled"] = obj.lead_scanning_enabled
        return initial

    @transaction.atomic
    def form_valid(self, form):
        previous = (
            ExhibitorInfo.objects.filter(pk=self.object.pk)
            .values("lead_scanning_enabled", "allow_voucher_access")
            .first()
        ) or {}

        # Generate booth_id only for exhibitors if none exists.
        if (
            form.cleaned_data.get("is_exhibitor", True)
            and not form.cleaned_data.get("booth_id")
            and not form.instance.booth_id
        ):
            form.instance.booth_id = generate_booth_id(event=self.request.event)

        response = super().form_valid(form)
        self.save_link_formsets()
        profile_changes = [key for key in form.changed_data if not key.startswith("question_")]
        question_changes = [int(key.split("_", 1)[1]) for key in form.changed_data if key.startswith("question_")]
        if profile_changes:
            self.object.log_action(
                LOG_PARTNER_CHANGED,
                data={"changed": profile_changes},
                user=self.request.user,
            )
        if question_changes and form.linked_proposal:
            form.linked_proposal.log_action(
                LOG_PROPOSAL_CHANGED,
                data={"changed_questions": question_changes},
                user=self.request.user,
            )
        if access_newly_granted(form.instance, previous) and queue_exhibitor_access_mail(
            self.request.event, self.object, self.request.user
        ):
            messages.info(self.request, _("An access-credentials email was placed in the outbox."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action"] = "edit"
        context["page_title"] = {
            "sponsor": _("Edit Sponsor"),
            "exhibitor": _("Edit Exhibitor"),
            "both": _("Edit Exhibitor & Sponsor"),
        }.get(partner_type_of(self.object), _("Edit Exhibitor or Sponsor"))
        return context

    def get_success_url(self):
        # Return to the list the partner was edited from; fall back to its type.
        partner_type = self.request.GET.get("type")
        if partner_type not in ("sponsor", "exhibitor"):
            partner_type = "sponsor" if self.object.is_sponsor and not self.object.is_exhibitor else "exhibitor"
        return partner_list_url(self.request.event, partner_type)


class ExhibitorDeleteView(EventPermissionRequiredMixin, DeleteView):
    model = ExhibitorInfo
    template_name = "exhibitors/delete.html"
    permission = ("can_change_event_settings",)

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def form_valid(self, form):
        self.object.log_action(
            LOG_PARTNER_DELETED,
            data={"name": localize_event_text(self.object.name), "booth_id": self.object.booth_id},
            user=self.request.user,
        )
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = {
            "sponsor": _("Delete Sponsor"),
            "exhibitor": _("Delete Exhibitor"),
            "both": _("Delete Exhibitor & Sponsor"),
        }.get(partner_type_of(self.object), _("Delete Exhibitor or Sponsor"))
        return context

    def get_success_url(self) -> str:
        return partner_list_url(self.request.event, partner_type_of(self.object))


class ExhibitorCopyKeyView(EventPermissionRequiredMixin, View):
    permission = ("can_change_event_settings",)

    def get(self, request, *args, **kwargs):
        exhibitor = get_object_or_404(ExhibitorInfo, pk=kwargs["pk"], event=request.event)
        response = JsonResponse({"key": exhibitor.key})
        response["Cache-Control"] = "no-store"
        return response


class ExhibitorVoucherManageView(EventPermissionRequiredMixin, DetailView):
    model = ExhibitorInfo
    template_name = "exhibitors/vouchers.html"
    permission = ("can_change_event_settings",)
    context_object_name = "exhibitor"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if request.GET.get("download") == "yes":
            return self.download_csv()
        return self.render_to_response(self.get_context_data())

    def voucher_links(self):
        return ExhibitorVoucher.objects.filter(exhibitor=self.object).select_related("voucher", "voucher__product")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        default_count = resolve_voucher_defaults(self.object)["count"]
        context.setdefault("form", ExhibitorVoucherBatchForm(initial={"count": default_count}))
        context["vouchers"] = self.voucher_links()
        emails = ExhibitionEmailQueue.objects.filter(exhibitor=self.object, role=mail_helpers.VOUCHERS)
        context["voucher_sent_at"] = emails.filter(sent_at__isnull=False).aggregate(last=Max("sent_at"))["last"]
        context["voucher_pending"] = emails.filter(sent_at__isnull=True).exists()
        return context

    def download_csv(self):
        vouchers = [link.voucher for link in self.voucher_links()]
        payload = build_voucher_csv(self.request.event, vouchers)
        response = HttpResponse(payload.encode("utf-8"), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{VOUCHER_CSV_FILENAME}"'
        response["Cache-Control"] = "no-store"
        return response

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:vouchers",
            kwargs={**event_kwargs(self.request.event), "pk": self.object.pk},
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        action = request.POST.get("action")
        if action == "delete":
            return self.remove_voucher(request)
        if action == "send":
            return self.send_vouchers(request)
        return self.create_vouchers(request)

    def remove_voucher(self, request):
        link = get_object_or_404(ExhibitorVoucher, pk=request.POST.get("voucher"), exhibitor=self.object)
        if link.voucher.redeemed:
            messages.error(request, _("This voucher has already been redeemed and cannot be removed."))
            return redirect(self.get_success_url())
        voucher = link.voucher
        link.delete()
        voucher.delete()
        messages.success(request, _("Voucher removed."))
        return redirect(self.get_success_url())

    @transaction.atomic
    def create_vouchers(self, request):
        form = ExhibitorVoucherBatchForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        count = form.cleaned_data["count"]
        if not count:
            form.add_error("count", _("Enter how many vouchers to create."))
            return self.render_to_response(self.get_context_data(form=form))
        self.issue_vouchers(count)
        messages.success(
            request,
            ngettext("%(count)d voucher created.", "%(count)d vouchers created.", count) % {"count": count},
        )
        return redirect(self.get_success_url())

    def issue_vouchers(self, count):
        defaults = resolve_voucher_defaults(self.object)
        return generate_exhibitor_vouchers(
            self.object,
            product=defaults["product"],
            count=count,
            price_mode=defaults["price_mode"],
            value=defaults["value"],
        )

    @transaction.atomic
    def send_vouchers(self, request):
        """Create any requested vouchers, then outbox an email with the complete list of codes."""
        form = ExhibitorVoucherBatchForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        if not (self.object.email or "").strip():
            messages.error(request, _("No email address is on file, so vouchers cannot be emailed."))
            return redirect(self.get_success_url())
        count = form.cleaned_data["count"]
        if count:
            self.issue_vouchers(count)
        vouchers = [link.voucher for link in self.voucher_links()]
        if not vouchers:
            form.add_error("count", _("There are no vouchers yet, so there is nothing to email."))
            return self.render_to_response(self.get_context_data(form=form))
        mail_helpers.queue_voucher_email(request.event, self.object, vouchers, requestor=request.user)
        messages.success(
            request,
            ngettext(
                "An email with %(count)d voucher code was placed in the outbox.",
                "An email with %(count)d voucher codes was placed in the outbox.",
                len(vouchers),
            )
            % {"count": len(vouchers)},
        )
        return redirect(self.get_success_url())


class ExhibitorVoucherBulkSendView(EventPermissionRequiredMixin, View):
    """Queue voucher emails for everyone in the current list, after confirmation."""

    permission = ("can_change_event_settings",)
    partner_type = None

    def target_queryset(self):
        queryset = ExhibitorInfo.objects.filter(event=self.request.event)
        if self.partner_type == "sponsor":
            queryset = queryset.filter(is_sponsor=True).order_by("sponsor_position", "name", "pk")
        elif self.partner_type == "exhibitor":
            queryset = queryset.filter(is_exhibitor=True).order_by("exhibitor_position", "name", "pk")
        else:
            queryset = queryset.order_by("name", "pk")
        form = ExhibitorFilterForm(
            data=self.request.GET,
            event=self.request.event,
            organization_type=self.partner_type,
        )
        if form.is_valid():
            queryset = form.filter_qs(queryset)
        return queryset

    def list_url(self):
        return partner_list_url(self.request.event, self.partner_type)

    def preview(self, exhibitors):
        """Split the list into who will be emailed and who cannot be, without creating anything.

        Anyone holding no vouchers is still sendable when their defaults would issue some; the
        counts annotated here are what the confirmation page reports.
        """
        event_settings = event_voucher_settings(self.request.event)
        sendable, no_email, no_vouchers = [], [], []
        for exhibitor in exhibitors:
            if not (exhibitor.email or "").strip():
                no_email.append(exhibitor)
                continue
            existing = len(mail_helpers.exhibitor_vouchers(exhibitor))
            planned = 0 if existing else resolve_voucher_defaults(exhibitor, event_settings=event_settings)["count"]
            if not existing and not planned:
                no_vouchers.append(exhibitor)
                continue
            exhibitor.voucher_total = existing or planned
            exhibitor.voucher_new = planned
            sendable.append(exhibitor)
        return sendable, no_email, no_vouchers

    def post(self, request, *args, **kwargs):
        exhibitors = list(self.target_queryset())
        sendable, no_email, no_vouchers = self.preview(exhibitors)

        if not request.POST.get("confirmed"):
            return render(
                request,
                "exhibitors/voucher_bulk_send.html",
                {
                    "partner_type": self.partner_type,
                    "sendable": sendable,
                    "no_email": no_email,
                    "no_vouchers": no_vouchers,
                    "list_url": self.list_url(),
                    "query_string": request.GET.urlencode(),
                },
            )

        if not sendable:
            messages.info(request, _("There is nobody to send vouchers to in this list."))
            return redirect(self.list_url())

        queued, skipped = self.queue_all(sendable, requestor=request.user)
        messages.success(
            request,
            ngettext(
                "%(count)d voucher email was placed in the outbox.",
                "%(count)d voucher emails were placed in the outbox.",
                len(queued),
            )
            % {"count": len(queued)},
        )
        missing_email = len(no_email) + len(skipped[mail_helpers.VOUCHER_SKIP_NO_EMAIL])
        missing_vouchers = len(no_vouchers) + len(skipped[mail_helpers.VOUCHER_SKIP_NO_VOUCHERS])
        if missing_email:
            messages.warning(request, self.skipped_no_email_message(missing_email))
        if missing_vouchers:
            messages.warning(request, self.skipped_no_vouchers_message(missing_vouchers))
        return redirect(self.list_url())

    @transaction.atomic
    def queue_all(self, sendable, *, requestor):
        return mail_helpers.queue_voucher_emails(self.request.event, sendable, requestor=requestor, issue_missing=True)

    def skipped_no_email_message(self, count):
        if self.partner_type == "sponsor":
            text = ngettext(
                "%(count)d sponsor was skipped because it has no email address.",
                "%(count)d sponsors were skipped because they have no email address.",
                count,
            )
        else:
            text = ngettext(
                "%(count)d exhibitor was skipped because it has no email address.",
                "%(count)d exhibitors were skipped because they have no email address.",
                count,
            )
        return text % {"count": count}

    def skipped_no_vouchers_message(self, count):
        if self.partner_type == "sponsor":
            text = ngettext(
                "%(count)d sponsor was skipped because their default number of vouchers is 0.",
                "%(count)d sponsors were skipped because their default number of vouchers is 0.",
                count,
            )
        else:
            text = ngettext(
                "%(count)d exhibitor was skipped because their default number of vouchers is 0.",
                "%(count)d exhibitors were skipped because their default number of vouchers is 0.",
                count,
            )
        return text % {"count": count}


class ExhibitorDeviceManageView(EventPermissionRequiredMixin, DetailView):
    model = ExhibitorInfo
    template_name = "exhibitors/devices.html"
    permission = ("can_change_event_settings",)
    context_object_name = "exhibitor"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def can_provision(self):
        return self.request.user.has_organizer_permission(
            self.request.event.organizer, "can_change_organizer_settings", request=self.request
        )

    def device_links(self):
        return ExhibitorDevice.objects.filter(exhibitor=self.object).select_related("device")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", ExhibitorDeviceProvisionForm())
        context["device_links"] = self.device_links()
        context["can_provision"] = self.can_provision()
        return context

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:devices",
            kwargs={**event_kwargs(self.request.event), "pk": self.object.pk},
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.can_provision():
            raise PermissionDenied()
        if request.POST.get("action") == "reset":
            return self.reset_tokens(request)
        return self.provision_devices(request)

    @transaction.atomic
    def provision_devices(self, request):
        form = ExhibitorDeviceProvisionForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        count = form.cleaned_data["count"]
        provision_exhibitor_devices(self.object, count, user=request.user)
        messages.success(
            request,
            ngettext("%(count)d device added.", "%(count)d devices added.", count) % {"count": count},
        )
        return redirect(self.get_success_url())

    @transaction.atomic
    def reset_tokens(self, request):
        disconnected = reset_exhibitor_device_setup(self.object, user=request.user)
        if disconnected:
            messages.warning(
                request,
                ngettext(
                    "New setup tokens generated. %(count)d device that was already scanning is now disconnected "
                    "and must be set up again.",
                    "New setup tokens generated. %(count)d devices that were already scanning are now disconnected "
                    "and must be set up again.",
                    len(disconnected),
                )
                % {"count": len(disconnected)},
            )
        else:
            messages.success(request, _("New setup tokens generated."))
        return redirect(self.get_success_url())


EMAIL_MANAGE_PERMISSION = (
    "can_change_event_settings",
    "can_change_exhibition_proposals",
    "is_exhibition_reviewer",
)


class EmailComposeView(EventPermissionRequiredMixin, FormView):
    """Compose a broadcast email to a filtered group of applicants."""

    permission = EMAIL_MANAGE_PERMISSION
    template_name = "exhibitors/email_compose.html"
    form_class = ExhibitionComposeForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        template_pk = self.request.GET.get("template")
        if template_pk:
            template = ExhibitionCustomEmailTemplate.objects.filter(event=self.request.event, pk=template_pk).first()
            if template:
                initial["subject"] = template.subject
                initial["body"] = template.body
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["custom_templates"] = ExhibitionCustomEmailTemplate.objects.filter(event=self.request.event)
        context["locales"] = self.request.event.settings.locales
        return context

    def form_valid(self, form):
        from .tasks import send_scheduled_email

        event = self.request.event
        scheduled_at = form.cleaned_data.get("scheduled_at")
        send_now = "_send" in self.request.POST and not scheduled_at

        recipients = mail_helpers.compose_recipients(
            event,
            states=form.cleaned_data["states"],
            partner_type=form.cleaned_data["partner_type"],
            sponsor_group=form.cleaned_data["sponsor_group"],
        )
        created = mail_helpers.queue_compose_emails(
            event,
            recipients,
            form.cleaned_data["subject"],
            form.cleaned_data["body"],
            scheduled_at=scheduled_at,
            send_now=send_now,
            requestor=self.request.user,
        )
        if not created:
            messages.warning(self.request, _("No applicants matched the selected filters."))
            return self.form_invalid(form)

        if scheduled_at:
            for queued in created:
                send_scheduled_email.apply_async(args=[event.pk, queued.pk], eta=scheduled_at)
            messages.success(
                self.request,
                _("%(count)d emails have been scheduled.") % {"count": len(created)},
            )
            return redirect("plugins:exhibition:email.outbox", **event_kwargs(event))
        if send_now:
            messages.success(self.request, _("%(count)d emails have been sent.") % {"count": len(created)})
            return redirect("plugins:exhibition:email.sent", **event_kwargs(event))
        messages.success(
            self.request,
            _("%(count)d emails have been placed in the outbox.") % {"count": len(created)},
        )
        return redirect("plugins:exhibition:email.outbox", **event_kwargs(event))

    def form_invalid(self, form):
        messages.error(self.request, _("We could not save your changes. See below for details."))
        return super().form_invalid(form)


def group_email_entries(emails):
    """Collapse rows that share a compose batch into one entry per message."""
    entries = []
    by_batch = {}
    for email in emails:
        if email.batch is None:
            entries.append(
                {
                    "pk": email.pk,
                    "subject": email.subject,
                    "recipients": [email.to_email],
                    "created": email.created,
                    "sent_at": email.sent_at,
                    "scheduled_at": email.scheduled_at,
                    "is_batch": False,
                }
            )
            continue
        key = str(email.batch)
        entry = by_batch.get(key)
        if entry is None:
            entry = {
                "pk": email.pk,
                "subject": email.subject,
                "recipients": [],
                "created": email.created,
                "sent_at": email.sent_at,
                "scheduled_at": email.scheduled_at,
                "is_batch": True,
            }
            by_batch[key] = entry
            entries.append(entry)
        entry["recipients"].append(email.to_email)
    return entries


class EmailListMixin(FilteredListMixin):
    """Shared search, ordering and batch-aware pagination for the outbox and sent lists."""

    context_object_name = "emails"
    date_field = "created"
    partial_template_name = None

    def base_queryset(self):
        raise NotImplementedError

    def build_filter_form(self):
        return EmailFilterForm(data=self.request.GET, date_field=self.date_field)

    def batch_representatives(self, base, matched):
        """One row per message: the lowest-numbered row of each batch, plus every unbatched row."""
        batches = matched.filter(batch__isnull=False).order_by().values("batch")
        representatives = base.filter(batch__in=batches).order_by().values("batch").annotate(first=Min("pk"))
        return base.filter(
            Q(pk__in=matched.filter(batch__isnull=True).order_by().values("pk"))
            | Q(pk__in=representatives.values("first"))
        )

    def get_queryset(self):
        base = self.base_queryset()
        queryset = self.batch_representatives(base, self.apply_filters(base))
        if self.filter_form.is_valid() and self.filter_form.cleaned_data.get("ordering"):
            return self.filter_form.apply_ordering(queryset)
        return queryset.order_by("-" + self.date_field, "-pk")

    def expand_batches(self, representatives):
        """Restore every recipient row of the batches shown on the current page."""
        rows = list(representatives)
        batches = [row.batch for row in rows if row.batch]
        if not batches:
            return rows
        siblings = {}
        for email in self.base_queryset().filter(batch__in=batches).order_by("pk"):
            siblings.setdefault(email.batch, []).append(email)
        expanded = []
        for row in rows:
            expanded.extend(siblings.get(row.batch, [row]) if row.batch else [row])
        return expanded

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entries"] = group_email_entries(self.expand_batches(context["emails"]))
        context["date_field"] = self.date_field
        return context

    def get_template_names(self):
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return [self.partial_template_name]
        return [self.template_name]


class EmailOutboxListView(EmailListMixin, EventPermissionRequiredMixin, ListView):
    """Unsent queued emails awaiting organiser review."""

    model = ExhibitionEmailQueue
    permission = EMAIL_MANAGE_PERMISSION
    template_name = "exhibitors/email_outbox.html"
    partial_template_name = "exhibitors/_email_outbox_body.html"
    date_field = "created"

    def base_queryset(self):
        return ExhibitionEmailQueue.objects.filter(event=self.request.event, sent_at__isnull=True)


class EmailSentListView(EmailListMixin, EventPermissionRequiredMixin, ListView):
    """Read-only list of already-sent emails."""

    model = ExhibitionEmailQueue
    permission = EMAIL_MANAGE_PERMISSION
    template_name = "exhibitors/email_sent.html"
    partial_template_name = "exhibitors/_email_sent_body.html"
    date_field = "sent_at"

    def base_queryset(self):
        return ExhibitionEmailQueue.objects.filter(event=self.request.event, sent_at__isnull=False)


class EmailEditView(EventPermissionRequiredMixin, UpdateView):
    """Preview and edit a queued (unsent) email before sending."""

    model = ExhibitionEmailQueue
    form_class = ExhibitionEmailQueueForm
    permission = EMAIL_MANAGE_PERMISSION
    template_name = "exhibitors/email_edit.html"
    context_object_name = "email"

    def get_queryset(self):
        return ExhibitionEmailQueue.objects.filter(event=self.request.event, sent_at__isnull=True)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def batch_queryset(self):
        return ExhibitionEmailQueue.objects.filter(
            event=self.request.event, batch=self.object.batch, sent_at__isnull=True
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.object.batch:
            context["recipients"] = list(self.batch_queryset().values_list("to_email", flat=True))
        else:
            context["recipients"] = [self.object.to_email]
        return context

    def reschedule(self, rows, scheduled_at):
        from .tasks import send_scheduled_email

        if not scheduled_at:
            return
        for row in rows:
            send_scheduled_email.apply_async(args=[self.request.event.pk, row.pk], eta=scheduled_at)

    def form_valid(self, form):
        self.object = form.save(commit=False)
        subject = form.cleaned_data["subject"]
        body = form.cleaned_data["body"]
        scheduled_at = form.cleaned_data["scheduled_at"]
        reschedule = "scheduled_at" in form.changed_data

        if self.object.batch:
            rows = list(self.batch_queryset())
            self.batch_queryset().update(subject=subject, body=body, scheduled_at=scheduled_at)
            if "_send" in self.request.POST:
                for row in rows:
                    row.subject = subject
                    row.body = body
                    row.send(requestor=self.request.user)
                messages.success(self.request, _("The emails have been saved and sent."))
            else:
                if reschedule:
                    self.reschedule(rows, scheduled_at)
                messages.success(self.request, _("The emails have been saved."))
            return redirect(self.get_success_url())

        self.object.save()
        if "_send" in self.request.POST:
            self.object.send(requestor=self.request.user)
            messages.success(self.request, _("The email has been saved and sent."))
        else:
            if reschedule:
                self.reschedule([self.object], scheduled_at)
            messages.success(self.request, _("The email has been saved."))
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("plugins:exhibition:email.outbox", kwargs=event_kwargs(self.request.event))


class EmailSendView(EventPermissionRequiredMixin, View):
    """Send a queued email (or the whole batch it belongs to)."""

    permission = EMAIL_MANAGE_PERMISSION

    def post(self, request, *args, **kwargs):
        email = get_object_or_404(ExhibitionEmailQueue, pk=kwargs["pk"], event=request.event, sent_at__isnull=True)
        if email.batch:
            rows = ExhibitionEmailQueue.objects.filter(event=request.event, batch=email.batch, sent_at__isnull=True)
        else:
            rows = [email]
        count = 0
        for row in rows:
            row.send(requestor=request.user)
            count += 1
        messages.success(
            request,
            ngettext("%(count)d email has been sent.", "%(count)d emails have been sent.", count) % {"count": count},
        )
        return redirect("plugins:exhibition:email.outbox", **event_kwargs(request.event))


class EmailDeleteView(EventPermissionRequiredMixin, DeleteView):
    """Discard a queued email (or the whole batch it belongs to)."""

    model = ExhibitionEmailQueue
    permission = EMAIL_MANAGE_PERMISSION
    template_name = "exhibitors/email_delete.html"
    context_object_name = "email"

    def get_queryset(self):
        return ExhibitionEmailQueue.objects.filter(event=self.request.event, sent_at__isnull=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.object.batch:
            context["recipients"] = list(
                self.get_queryset().filter(batch=self.object.batch).values_list("to_email", flat=True)
            )
        else:
            context["recipients"] = [self.object.to_email]
        return context

    def form_valid(self, form):
        self.object = self.get_object()
        success_url = self.get_success_url()
        if self.object.batch:
            self.get_queryset().filter(batch=self.object.batch).delete()
        else:
            self.object.delete()
        return redirect(success_url)

    def get_success_url(self):
        messages.success(self.request, _("The email has been discarded."))
        return reverse("plugins:exhibition:email.outbox", kwargs=event_kwargs(self.request.event))


class EmailBulkActionView(EventPermissionRequiredMixin, View):
    """Send or discard several queued emails at once (selected rows or all)."""

    permission = EMAIL_MANAGE_PERMISSION

    def target_rows(self, request, scope):
        base = ExhibitionEmailQueue.objects.filter(event=request.event, sent_at__isnull=True)
        if scope == "all":
            return base
        selected = request.POST.getlist("selected")
        if not selected:
            return base.none()
        batches = [batch for batch in base.filter(pk__in=selected).values_list("batch", flat=True) if batch]
        return base.filter(Q(pk__in=selected) | Q(batch__in=batches))

    def post(self, request, *args, **kwargs):
        op = request.POST.get("op", "")
        action = "send" if op.startswith("send") else "discard" if op.startswith("discard") else None
        scope = "all" if op.endswith("_all") else "selected"
        outbox_url = redirect("plugins:exhibition:email.outbox", **event_kwargs(request.event))

        if action is None:
            return outbox_url

        rows = self.target_rows(request, scope)

        if action == "send":
            count = 0
            for row in rows:
                row.send(requestor=request.user)
                count += 1
            if count:
                messages.success(
                    request,
                    ngettext("%(count)d email has been sent.", "%(count)d emails have been sent.", count)
                    % {"count": count},
                )
            else:
                messages.info(request, _("No emails were selected."))
            return outbox_url

        if request.POST.get("confirmed"):
            count = rows.count()
            rows.delete()
            if count:
                messages.success(
                    request,
                    ngettext("%(count)d email has been discarded.", "%(count)d emails have been discarded.", count)
                    % {"count": count},
                )
            else:
                messages.info(request, _("No emails were selected."))
            return outbox_url

        count = rows.count()
        if not count:
            messages.info(request, _("No emails were selected."))
            return outbox_url
        return render(
            request,
            "exhibitors/email_bulk_discard.html",
            {
                "count": count,
                "scope": scope,
                "selected": request.POST.getlist("selected"),
            },
        )


class EmailTemplatesView(EventPermissionRequiredMixin, TemplateView):
    """Edit the lifecycle email templates and organizer-defined custom templates."""

    permission = "can_change_event_settings"
    template_name = "exhibitors/email_templates.html"

    def get_form(self, data=None):
        return ExhibitionMailTemplatesForm(data=data, obj=self.request.event)

    def get_custom_panels(self, data=None):
        templates = ExhibitionCustomEmailTemplate.objects.filter(event=self.request.event)
        panels = []
        for template in templates:
            form = ExhibitionCustomEmailTemplateForm(
                data,
                instance=template,
                prefix=f"custom_{template.pk}",
                event=self.request.event,
            )
            panels.append(
                {
                    "pk": template.pk,
                    "label": template.name,
                    "role_slug": f"custom_{template.pk}",
                    "form": form,
                }
            )
        return panels

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = kwargs.get("form") or self.get_form()
        custom_panels = kwargs.get("custom_panels")
        if custom_panels is None:
            custom_panels = self.get_custom_panels()
        context["form"] = form
        context["template_panels"] = [
            {
                "role": role,
                "label": label,
                "subject_field": form[mail_helpers.subject_settings_key(role)],
                "body_field": form[mail_helpers.body_settings_key(role)],
            }
            for role, label in (
                (mail_helpers.PROPOSAL_NEW, _("Request received (confirmation)")),
                (mail_helpers.PROPOSAL_ACCEPTED, _("Request accepted")),
                (mail_helpers.PROPOSAL_REJECTED, _("Request rejected")),
                (mail_helpers.EXHIBITOR_ACCESS, _("Exhibitor lead scanning key")),
                (mail_helpers.VOUCHERS, _("Vouchers")),
            )
        ]
        context["custom_panels"] = custom_panels
        context["locales"] = self.request.event.settings.locales
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form(data=request.POST)
        custom_panels = self.get_custom_panels(data=request.POST)
        custom_valid = all(panel["form"].is_valid() for panel in custom_panels)
        if form.is_valid() and custom_valid:
            form.save()
            for panel in custom_panels:
                panel["form"].save()
            messages.success(request, _("Email templates have been saved."))
            return redirect("plugins:exhibition:email.templates", **event_kwargs(request.event))
        return self.render_to_response(self.get_context_data(form=form, custom_panels=custom_panels))


class EmailTemplatePreviewView(EventPermissionRequiredMixin, View):
    """Render draft template text with sample placeholder values, per locale.

    Consumed by core's shared ``richtextPreview.js`` (``data-email-preview-*``
    attributes): the role is passed as a query parameter on each panel's own
    preview URL, and the body text is posted as one ``body_<locale>`` field
    per rendered locale.
    """

    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        role = request.GET.get("role", "")
        custom_pk = role[len("custom_") :] if role.startswith("custom_") else None
        if role in ("compose", "custom"):
            pass
        elif custom_pk is not None:
            if not ExhibitionCustomEmailTemplate.objects.filter(event=request.event, pk=custom_pk).exists():
                return JsonResponse({"detail": _("Unknown template.")}, status=400)
        elif role in mail_helpers.LIFECYCLE_ROLES:
            pass
        else:
            return JsonResponse({"detail": _("Unknown template.")}, status=400)

        placeholder_context = mail_helpers.ROLE_PLACEHOLDER_CONTEXT.get(role, mail_helpers.PROPOSAL_PLACEHOLDER_CONTEXT)

        from eventyay.base.i18n import language
        from eventyay.base.services.mail import expand_email_variable_chips
        from eventyay.base.templatetags.rich_text import compile_email_body

        placeholders = mail_helpers.build_preview_placeholders(request.event, placeholder_context)
        event_locales = request.event.settings.locales
        region = request.event.settings.region

        def render(text):
            try:
                expanded = text.format_map(placeholders)
            except (KeyError, IndexError, ValueError):
                expanded = text
            expanded = expand_email_variable_chips(expanded, dict(placeholders))
            return compile_email_body(expanded)

        previews = {}
        for locale in event_locales:
            body = request.POST.get(f"body_{locale}", "")
            with language(locale, region):
                previews[locale] = render(body) if body else ""
        return JsonResponse({"previews": previews})


class CustomEmailTemplateCreateView(EventPermissionRequiredMixin, CreateView):
    model = ExhibitionCustomEmailTemplate
    form_class = ExhibitionCustomEmailTemplateForm
    permission = "can_change_event_settings"
    template_name = "exhibitors/email_custom_template_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["locales"] = self.request.event.settings.locales
        return context

    def form_valid(self, form):
        form.instance.event = self.request.event
        messages.success(self.request, _("Custom template has been created."))
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, _("We could not save your changes. See below for details."))
        return super().form_invalid(form)

    def get_success_url(self):
        return reverse("plugins:exhibition:email.templates", kwargs=event_kwargs(self.request.event))


class CustomEmailTemplateDeleteView(EventPermissionRequiredMixin, DeleteView):
    model = ExhibitionCustomEmailTemplate
    permission = "can_change_event_settings"
    template_name = "exhibitors/email_custom_template_delete.html"

    def get_queryset(self):
        return ExhibitionCustomEmailTemplate.objects.filter(event=self.request.event)

    def form_valid(self, form):
        messages.success(self.request, _("Custom template has been deleted."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("plugins:exhibition:email.templates", kwargs=event_kwargs(self.request.event))
