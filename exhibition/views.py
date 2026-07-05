import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DeleteView, DetailView, ListView, TemplateView
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views import CreateView, UpdateView

from .forms import (
    CallSettingsForm,
    ExhibitionProposalExtraLinkFormSet,
    ExhibitionProposalForm,
    ExhibitionProposalReviewForm,
    ExhibitionProposalReviewNotesForm,
    ExhibitionProposalSocialLinkFormSet,
    ExhibitionQuestionForm,
    ExhibitorExtraLinkFormSet,
    ExhibitorInfoForm,
    ExhibitorSocialLinkFormSet,
    SponsorGroupForm,
    social_link_prefixes,
)
from .models import (
    PROPOSAL_DEFAULT_FIELDS,
    ExhibitionProposal,
    ExhibitionProposalState,
    ExhibitionQuestion,
    ExhibitorInfo,
    ExhibitorSettings,
    SponsorGroup,
    generate_booth_id,
    get_next_sponsor_group_level,
)
from .social_links import serialize_social_link
from .utils import (
    add_external_image_csp_sources,
    build_exhibitor_video_embed,
    create_exhibitor_from_proposal,
    public_exhibitors_queryset,
)


def event_kwargs(event):
    return {
        "organizer": event.organizer.slug,
        "event": event.slug,
    }


class PublicEventLoginRequiredMixin(LoginRequiredMixin):
    def get_login_url(self):
        return reverse("cfp:event.login", kwargs=event_kwargs(self.request.event))


class PublicCallEnabledMixin:
    hide_after_deadline = False

    def get_exhibition_settings(self):
        return ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]

    def dispatch(self, request, *args, **kwargs):
        settings = self.get_exhibition_settings()
        if not settings.call_enabled:
            raise Http404()
        if self.hide_after_deadline and settings.call_hide_after_deadline and not settings.call_is_open:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)


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
        if tab not in {"exhibitors", "sponsors", "call"}:
            return "exhibitors"
        return tab

    def get_settings_url(self, tab):
        route_names = {
            "call": "plugins:exhibition:settings.call",
            "sponsors": "plugins:exhibition:settings.sponsors",
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
        ctx["default_fields"] = ["attendee_name", "attendee_email"]
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
        ctx["show_add_group_form"] = kwargs.get("show_add_group_form", False)
        ctx["expanded_group_pk"] = kwargs.get("expanded_group_pk")
        return ctx

    def get_next_sponsor_group_level(self):
        return get_next_sponsor_group_level(self.request.event)

    def post(self, request, *args, **kwargs):
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        action = request.POST.get("action", "save_exhibitor_settings")
        active_tab = self.get_active_tab()

        if action == "save_exhibitor_settings":
            allowed_fields = request.POST.getlist("exhibitors_access_voucher")
            settings.allowed_fields = allowed_fields
            settings.exhibitors_access_mail_subject = request.POST.get("exhibitors_access_mail_subject", "")
            settings.exhibitors_access_mail_body = request.POST.get("exhibitors_access_mail_body", "")
            settings.save()
            messages.success(self.request, _("Settings have been saved."))
            return redirect(self.get_settings_url("exhibitors"))

        if action == "save_call_settings":
            form = CallSettingsForm(
                request.POST,
                instance=settings,
                event=request.event,
            )
            if form.is_valid():
                form.save()
                messages.success(self.request, _("Call settings have been saved."))
                return redirect(self.get_settings_url("call"))
            return self.render_to_response(self.get_context_data(call_settings_form=form))

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
                messages.success(self.request, _("Sponsor group added."))
                return redirect(self.get_settings_url("sponsors"))

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
                messages.success(self.request, _("Sponsor group updated."))
                return redirect(self.get_settings_url("sponsors"))

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
                group.delete()
                messages.success(self.request, _("Sponsor group deleted."))
            return redirect(self.get_settings_url("sponsors"))

        messages.error(self.request, _("Unknown action."))
        return redirect(self.get_settings_url(active_tab))


class ExhibitorListView(EventPermissionRequiredMixin, ListView):
    model = ExhibitorInfo
    permission = ("can_change_event_settings", "can_view_orders")
    template_name = "exhibitors/exhibitor_info.html"
    context_object_name = "exhibitors"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event).select_related("sponsor_group")

    def get_success_url(self) -> str:
        return reverse(
            "plugins:exhibition:info",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class PublicExhibitorListView(ListView):
    model = ExhibitorInfo
    template_name = "exhibitors/public_list.html"
    context_object_name = "exhibitors"

    def get_queryset(self):
        return public_exhibitors_queryset(self.request.event)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.request.event
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
        context["extra_links"] = list(self.object.extra_links.all())
        context["video_embed"] = build_exhibitor_video_embed(self.object.video_url or "")
        context["slides_document_url"] = self.object.visible_slides_url

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


class UserProposalListView(PublicCallEnabledMixin, PublicEventLoginRequiredMixin, ListView):
    model = ExhibitionProposal
    template_name = "exhibitors/public_proposal_list.html"
    context_object_name = "proposals"

    def get_queryset(self):
        return ExhibitionProposal.objects.filter(
            event=self.request.event,
            user=self.request.user,
        ).order_by("-updated", "-created")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["settings"] = self.get_exhibition_settings()
        return context


class ProposalLinkFormsetMixin:
    social_formset_prefix = "social_links"
    extra_formset_prefix = "extra_links"

    def get_proposal_field_settings(self):
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        return settings.normalized_proposal_field_settings

    def proposal_field_is_active(self, key):
        return self.get_proposal_field_settings()[key]["active"]

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

    def get_extra_link_formset(self):
        return ExhibitionProposalExtraLinkFormSet(
            data=self.request.POST if self.request.method == "POST" else None,
            instance=self.get_formset_instance(),
            prefix=self.extra_formset_prefix,
        )

    def post_with_formsets(self):
        form = self.get_form()
        self.social_media_formset = self.get_social_formset() if self.proposal_field_is_active("social_links") else None
        self.extra_links_formset = (
            self.get_extra_link_formset() if self.proposal_field_is_active("extra_links") else None
        )

        if (
            form.is_valid()
            and (self.social_media_formset is None or self.social_media_formset.is_valid())
            and (self.extra_links_formset is None or self.extra_links_formset.is_valid())
        ):
            return self.form_valid(form)
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["social_media_formset"] = kwargs.get(
            "social_media_formset",
            getattr(self, "social_media_formset", None) or self.get_social_formset(),
        )
        context["extra_links_formset"] = kwargs.get(
            "extra_links_formset",
            getattr(self, "extra_links_formset", None) or self.get_extra_link_formset(),
        )
        context["social_link_prefixes"] = social_link_prefixes()
        context["settings"] = self.get_exhibition_settings()
        context["show_social_links"] = self.proposal_field_is_active("social_links")
        context["show_extra_links"] = self.proposal_field_is_active("extra_links")
        context.setdefault("can_edit", True)
        return context

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def save_link_formsets(self):
        if self.social_media_formset is not None:
            self.social_media_formset.instance = self.object
            self.social_media_formset.save()
        if self.extra_links_formset is not None:
            self.extra_links_formset.instance = self.object
            self.extra_links_formset.save()


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
        if not settings.call_is_open:
            if settings.call_hide_after_deadline:
                raise Http404()
            messages.error(request, _("The call for exhibitors is closed."))
            return redirect("plugins:exhibition:public_call", **event_kwargs(request.event))
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
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
        messages.success(self.request, _("Your proposal has been saved."))
        return response

    def get_success_url(self):
        return reverse("plugins:exhibition:proposal.user_list", kwargs=event_kwargs(self.request.event))


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

    def get_queryset(self):
        return ExhibitionProposal.objects.filter(
            event=self.request.event,
            user=self.request.user,
        ).prefetch_related("answers", "answers__options")

    def can_edit(self):
        settings = self.get_exhibition_settings()
        return self.object.editable and settings.call_is_open

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        kwargs["read_only"] = not self.can_edit()
        return kwargs

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.can_edit():
            messages.error(request, _("This proposal can no longer be edited."))
            return redirect(self.get_success_url())
        return self.post_with_formsets()

    @transaction.atomic
    def form_valid(self, form):
        if self.request.POST.get("action") == "draft":
            form.instance.state = ExhibitionProposalState.DRAFT
            form.instance.submitted = None
        else:
            form.instance.state = ExhibitionProposalState.SUBMITTED
            form.instance.submitted = form.instance.submitted or timezone.now()
        response = super().form_valid(form)
        self.save_link_formsets()
        messages.success(self.request, _("Your proposal has been saved."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = self.can_edit()
        return context

    def get_success_url(self):
        return reverse("plugins:exhibition:proposal.user_list", kwargs=event_kwargs(self.request.event))


class ExhibitorLinkFormsetMixin:
    social_formset_prefix = "social_links"
    extra_formset_prefix = "extra_links"

    def get_formset_instance(self):
        obj = getattr(self, "object", None)
        return obj if obj is not None else ExhibitorInfo(event=self.request.event)

    def get_social_formset(self):
        return ExhibitorSocialLinkFormSet(
            data=self.request.POST if self.request.method == "POST" else None,
            instance=self.get_formset_instance(),
            prefix=self.social_formset_prefix,
        )

    def get_extra_link_formset(self):
        return ExhibitorExtraLinkFormSet(
            data=self.request.POST if self.request.method == "POST" else None,
            instance=self.get_formset_instance(),
            prefix=self.extra_formset_prefix,
        )

    def post_with_formsets(self):
        form = self.get_form()
        self.social_media_formset = self.get_social_formset()
        self.extra_links_formset = self.get_extra_link_formset()

        if form.is_valid() and self.social_media_formset.is_valid() and self.extra_links_formset.is_valid():
            return self.form_valid(form)
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["social_media_formset"] = kwargs.get(
            "social_media_formset",
            getattr(self, "social_media_formset", self.get_social_formset()),
        )
        context["extra_links_formset"] = kwargs.get(
            "extra_links_formset",
            getattr(self, "extra_links_formset", self.get_extra_link_formset()),
        )
        context["social_link_prefixes"] = social_link_prefixes()
        return context

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def save_link_formsets(self):
        self.social_media_formset.instance = self.object
        self.extra_links_formset.instance = self.object
        self.social_media_formset.save()
        self.extra_links_formset.save()


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


class ProposalListView(EventPermissionRequiredMixin, ListView):
    model = ExhibitionProposal
    permission = ("can_change_event_settings", "can_change_exhibition_proposals", "is_exhibition_reviewer")
    template_name = "exhibitors/proposal_list.html"
    context_object_name = "proposals"

    def get_queryset(self):
        return (
            ExhibitionProposal.objects.filter(event=self.request.event)
            .select_related("user", "sponsor_group", "approved_exhibitor")
            .order_by("-updated", "-created")
        )


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

    def get_form_class(self):
        if self.can_manage():
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
                "extra_links",
            )
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["answers"] = self.object.answers.select_related("question").prefetch_related("options")
        context["can_manage"] = self.can_manage()
        context["can_edit_exhibitor"] = self.can_edit_exhibitor()
        return context

    @transaction.atomic
    def form_valid(self, form):
        self.object = form.save()
        action = self.request.POST.get("action", "save")
        if action in ("approve", "reject") and not self.can_manage():
            raise PermissionDenied()
        if action == "approve":
            exhibitor = create_exhibitor_from_proposal(self.object)
            messages.success(
                self.request,
                _("Proposal approved and partner profile created."),
            )
            if self.can_edit_exhibitor():
                return redirect(
                    "plugins:exhibition:edit",
                    **event_kwargs(self.request.event),
                    pk=exhibitor.pk,
                )
            return redirect(self.get_success_url())
        if action == "reject":
            if self.object.approved_exhibitor_id:
                messages.error(
                    self.request,
                    _("This proposal has already been approved and cannot be rejected."),
                )
            else:
                self.object.state = ExhibitionProposalState.REJECTED
                self.object.save(update_fields=["state", "updated"])
                messages.success(self.request, _("Proposal rejected."))
            return redirect(self.get_success_url())

        messages.success(self.request, _("Review details saved."))
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:proposal.detail",
            kwargs={**event_kwargs(self.request.event), "code": self.object.code},
        )


class ExhibitionQuestionListView(EventPermissionRequiredMixin, ListView):
    model = ExhibitionQuestion
    permission = "can_change_settings"
    template_name = "exhibitors/call_questions.html"
    context_object_name = "questions"

    def get_queryset(self):
        return ExhibitionQuestion.objects.filter(event=self.request.event).annotate(answer_count=Count("answers"))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        field_settings = settings.normalized_proposal_field_settings
        answer_counts = self.get_default_field_answer_counts()
        context["default_fields"] = [
            {
                **field,
                "active": field_settings[field["key"]]["active"],
                "required": field_settings[field["key"]]["required"],
                "supports_required": field.get("supports_required", True),
                "answer_count": answer_counts.get(field["key"], 0),
            }
            for field in PROPOSAL_DEFAULT_FIELDS
        ]
        return context

    def get_default_field_answer_counts(self):
        proposals = ExhibitionProposal.objects.filter(event=self.request.event).exclude(
            state=ExhibitionProposalState.DRAFT
        )
        file_has_value = {
            "slides": (Q(slides__isnull=False) & ~Q(slides="")) | (Q(slides_url__isnull=False) & ~Q(slides_url="")),
            "logo": (Q(logo__isnull=False) & ~Q(logo="")) | (Q(logo_url__isnull=False) & ~Q(logo_url="")),
            "header_image": (Q(header_image__isnull=False) & ~Q(header_image=""))
            | (Q(header_image_url__isnull=False) & ~Q(header_image_url="")),
        }
        text_fields = (
            "description",
            "email",
            "url",
            "contact_url",
            "video_url",
            "booth_name",
            "notes",
        )
        counts = {
            "applying_for": proposals.filter(Q(is_exhibitor=True) | Q(is_sponsor=True)).count(),
            "name": proposals.count(),
            "social_links": proposals.filter(social_links__isnull=False).distinct().count(),
            "extra_links": proposals.filter(extra_links__isnull=False).distinct().count(),
        }
        counts.update({key: proposals.filter(condition).count() for key, condition in file_has_value.items()})
        counts.update(
            {
                field: proposals.exclude(**{f"{field}__isnull": True}).exclude(**{field: ""}).count()
                for field in text_fields
            }
        )
        return counts

    def post(self, request, *args, **kwargs):
        settings = ExhibitorSettings.objects.get_or_create(event=request.event)[0]
        proposal_field_settings = settings.normalized_proposal_field_settings

        for field in PROPOSAL_DEFAULT_FIELDS:
            key = field["key"]
            is_active = field.get("active_locked") or request.POST.get(f"{key}_active") == "on"
            proposal_field_settings[key]["active"] = is_active
            proposal_field_settings[key]["required"] = is_active and (
                field.get("required_locked")
                or (field.get("supports_required", True) and request.POST.get(f"{key}_required") == "on")
            )
            if field.get("supports_required") is False:
                proposal_field_settings[key]["required"] = False

        settings.proposal_field_settings = proposal_field_settings
        settings.save(update_fields=["proposal_field_settings"])

        questions = list(ExhibitionQuestion.objects.filter(event=request.event))
        for question in questions:
            question.active = request.POST.get(f"question_{question.pk}_active") == "on"
            question.required = request.POST.get(f"question_{question.pk}_required") == "on"
        if questions:
            ExhibitionQuestion.objects.bulk_update(questions, ["active", "required"])

        messages.success(request, _("Proposal form settings have been saved."))
        return redirect("plugins:exhibition:call.questions", **event_kwargs(request.event))


class ExhibitionQuestionCreateView(EventPermissionRequiredMixin, CreateView):
    model = ExhibitionQuestion
    form_class = ExhibitionQuestionForm
    permission = "can_change_settings"
    template_name = "exhibitors/call_question_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:call.questions",
            kwargs=event_kwargs(self.request.event),
        )


class ExhibitionQuestionEditView(EventPermissionRequiredMixin, UpdateView):
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

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:call.questions",
            kwargs=event_kwargs(self.request.event),
        )


class ExhibitorCreateView(ExhibitorLinkFormsetMixin, EventPermissionRequiredMixin, CreateView):
    model = ExhibitorInfo
    form_class = ExhibitorInfoForm
    template_name = "exhibitors/add.html"
    permission = "can_change_event_settings"

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
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action"] = "create"
        return context

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:info",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


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
        # Generate booth_id only for exhibitors if none exists.
        if (
            form.cleaned_data.get("is_exhibitor", True)
            and not form.cleaned_data.get("booth_id")
            and not form.instance.booth_id
        ):
            form.instance.booth_id = generate_booth_id(event=self.request.event)

        response = super().form_valid(form)
        self.save_link_formsets()
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["action"] = "edit"
        return context

    def get_success_url(self):
        return reverse(
            "plugins:exhibition:info",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class ExhibitorDeleteView(EventPermissionRequiredMixin, DeleteView):
    model = ExhibitorInfo
    template_name = "exhibitors/delete.html"
    permission = ("can_change_event_settings",)

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def get_success_url(self) -> str:
        return reverse(
            "plugins:exhibition:info",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class ExhibitorCopyKeyView(EventPermissionRequiredMixin, View):
    permission = ("can_change_event_settings",)

    def get(self, request, *args, **kwargs):
        exhibitor = get_object_or_404(ExhibitorInfo, pk=kwargs["pk"], event=request.event)
        response = HttpResponse(exhibitor.key)
        response["Content-Disposition"] = 'attachment; filename="password.txt"'
        return response
