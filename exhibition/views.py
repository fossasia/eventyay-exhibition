from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DeleteView, DetailView, ListView
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views import CreateView, UpdateView

from .forms import (
    ExhibitorExtraLinkFormSet,
    ExhibitorInfoForm,
    ExhibitorSocialLinkFormSet,
    social_link_prefixes,
)
from .models import ExhibitorInfo, ExhibitorSettings, SponsorGroup, generate_booth_id
from .social_links import serialize_social_link
from .utils import (
    add_external_image_csp_sources,
    build_exhibitor_video_embed,
    public_exhibitors_queryset,
)


class SettingsView(EventPermissionRequiredMixin, ListView):
    model = ExhibitorInfo
    template_name = "exhibitors/settings.html"
    context_object_name = "exhibitors"
    permission = "can_change_settings"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event)

    def get_active_tab(self):
        tab = (
            self.request.GET.get("tab") or self.request.POST.get("tab") or "exhibitors"
        )
        if tab not in {"exhibitors", "sponsors"}:
            return "exhibitors"
        return tab

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        ctx["settings"] = settings
        ctx["default_fields"] = ["attendee_name", "attendee_email"]
        ctx["active_tab"] = self.get_active_tab()
        ctx["sponsor_groups"] = (
            SponsorGroup.objects.filter(event=self.request.event)
            .annotate(partner_count=Count("partners"))
            .order_by("name")
        )
        return ctx

    def post(self, request, *args, **kwargs):
        settings = ExhibitorSettings.objects.get_or_create(event=self.request.event)[0]
        action = request.POST.get("action", "save_exhibitor_settings")
        active_tab = self.get_active_tab()

        if action == "save_exhibitor_settings":
            allowed_fields = request.POST.getlist("exhibitors_access_voucher")
            settings.allowed_fields = allowed_fields
            settings.exhibitors_access_mail_subject = request.POST.get(
                "exhibitors_access_mail_subject", ""
            )
            settings.exhibitors_access_mail_body = request.POST.get(
                "exhibitors_access_mail_body", ""
            )
            settings.save()
            messages.success(self.request, _("Settings have been saved."))
            return redirect(f"{request.path}?tab=exhibitors")

        if action == "add_group":
            group_name = request.POST.get("group_name", "").strip()
            if not group_name:
                messages.error(self.request, _("Please enter a group name."))
            elif SponsorGroup.objects.filter(
                event=request.event, name__iexact=group_name
            ).exists():
                messages.error(
                    self.request, _("A sponsor group with this name already exists.")
                )
            else:
                SponsorGroup.objects.create(event=request.event, name=group_name)
                messages.success(self.request, _("Sponsor group added."))
            return redirect(f"{request.path}?tab=sponsors")

        if action == "rename_group":
            group = get_object_or_404(
                SponsorGroup, pk=request.POST.get("group_id"), event=request.event
            )
            group_name = request.POST.get("group_name", "").strip()
            if not group_name:
                messages.error(self.request, _("Please enter a group name."))
            elif (
                SponsorGroup.objects.filter(
                    event=request.event, name__iexact=group_name
                )
                .exclude(pk=group.pk)
                .exists()
            ):
                messages.error(
                    self.request, _("A sponsor group with this name already exists.")
                )
            else:
                group.name = group_name
                group.save(update_fields=["name"])
                messages.success(self.request, _("Sponsor group updated."))
            return redirect(f"{request.path}?tab=sponsors")

        if action == "delete_group":
            group = get_object_or_404(
                SponsorGroup, pk=request.POST.get("group_id"), event=request.event
            )
            if group.partners.exists():
                messages.error(
                    self.request,
                    _(
                        "This sponsor group cannot be deleted while it is assigned to partners."
                    ),
                )
            else:
                group.delete()
                messages.success(self.request, _("Sponsor group deleted."))
            return redirect(f"{request.path}?tab=sponsors")

        messages.error(self.request, _("Unknown action."))
        return redirect(f"{request.path}?tab={active_tab}")


class ExhibitorListView(EventPermissionRequiredMixin, ListView):
    model = ExhibitorInfo
    permission = ("can_change_event_settings", "can_view_orders")
    template_name = "exhibitors/exhibitor_info.html"
    context_object_name = "exhibitors"

    def get_queryset(self):
        return ExhibitorInfo.objects.filter(event=self.request.event).select_related(
            "sponsor_group"
        )

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
        context["social_image"] = (
            self.object.visible_header_image_url or self.object.visible_logo_url
        )
        if len(exhibitors) > 1:
            current_index = next(
                index
                for index, exhibitor in enumerate(exhibitors)
                if exhibitor.pk == self.object.pk
            )
            context["previous_exhibitor"] = exhibitors[current_index - 1]
            context["next_exhibitor"] = exhibitors[
                (current_index + 1) % len(exhibitors)
            ]
        else:
            context["previous_exhibitor"] = None
            context["next_exhibitor"] = None

        context["social_links"] = [
            serialize_social_link(link) for link in self.object.social_links.all()
        ]
        context["extra_links"] = list(self.object.extra_links.all())
        context["video_embed"] = build_exhibitor_video_embed(
            self.object.video_url or ""
        )
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

        if (
            form.is_valid()
            and self.social_media_formset.is_valid()
            and self.extra_links_formset.is_valid()
        ):
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


class ExhibitorCreateView(
    ExhibitorLinkFormsetMixin, EventPermissionRequiredMixin, CreateView
):
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
        if form.cleaned_data.get("is_exhibitor", True) and not form.cleaned_data.get(
            "booth_id"
        ):
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


class ExhibitorEditView(
    ExhibitorLinkFormsetMixin, EventPermissionRequiredMixin, UpdateView
):
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
        exhibitor = get_object_or_404(
            ExhibitorInfo, pk=kwargs["pk"], event=request.event
        )
        response = HttpResponse(exhibitor.key)
        response["Content-Disposition"] = 'attachment; filename="password.txt"'
        return response
