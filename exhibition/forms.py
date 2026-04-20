from django import forms
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _
from eventyay.base.forms import I18nModelForm
from eventyay.common.urls import normalize_url_scheme

from .models import (
    ExhibitorExtraLink,
    ExhibitorInfo,
    ExhibitorSocialLink,
    SponsorGroup,
)
from .social_links import (
    SOCIAL_LINK_CHOICES,
    SOCIAL_LINK_SPECS,
    build_social_link_url,
    get_social_link_value,
)


class ExhibitorInfoForm(I18nModelForm):
    slides_url = forms.URLField(
        required=False,
        label=_("Slides URL"),
        help_text=_("Use an external PDF URL instead of uploading a slides file."),
    )
    logo_url = forms.URLField(
        required=False,
        label=_("Partner Logo URL"),
        help_text=_("Use an external image URL instead of uploading a logo file."),
    )
    header_image_url = forms.URLField(
        required=False,
        label=_("Partner Header Image URL"),
        help_text=_(
            "Use an external image URL instead of uploading a header image file."
        ),
    )
    sponsor_group = forms.ModelChoiceField(
        queryset=SponsorGroup.objects.none(),
        required=False,
        label=_("Sponsor Group"),
    )
    not_an_exhibitor = forms.BooleanField(
        required=False,
        label=_("This Partner is Not an Exhibitor"),
    )
    allow_voucher_access = forms.BooleanField(
        required=False,
        label=_("Allowed to access voucher data"),
    )
    allow_lead_access = forms.BooleanField(
        required=False,
        label=_("Allowed to access scanned lead data"),
    )
    lead_scanning_scope_by_device = forms.TypedChoiceField(
        label=_("Lead scanning behavior"),
        choices=(
            (
                False,
                _(
                    "Every attendee is one lead, even when scanned from multiple devices. "
                    "Notes and ratings are shared between devices."
                ),
            ),
            (
                True,
                _(
                    "Every attendee is a new lead when scanned from a new device. "
                    "Notes and ratings are specific to the device."
                ),
            ),
        ),
        coerce=lambda value: str(value) == "True",
        initial=False,
        required=False,
        widget=forms.RadioSelect,
    )
    comment = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 6}),
        required=False,
        label=_("Comment"),
        help_text=_(
            "The text entered in this field will not be visible to the user and is available for your convenience."
        ),
    )
    booth_id = forms.CharField(
        required=False,
        label=_("Booth ID"),
    )

    file_url_fields = {
        "slides": "slides_url",
        "logo": "logo_url",
        "header_image": "header_image_url",
    }

    class Meta:
        model = ExhibitorInfo
        localized_fields = "__all__"
        fields = [
            "name",
            "description",
            "url",
            "email",
            "contact_url",
            "video_url",
            "slides",
            "slides_url",
            "logo",
            "logo_url",
            "header_image",
            "header_image_url",
            "is_sponsor",
            "sponsor_group",
            "not_an_exhibitor",
            "booth_id",
            "booth_name",
            "lead_scanning_enabled",
            "allow_voucher_access",
            "allow_lead_access",
            "lead_scanning_scope_by_device",
        ]
        labels = {
            "name": _("Partner Name"),
            "description": _("Partner Description"),
            "email": _("Contact email"),
            "contact_url": _("Contact URL"),
            "video_url": _("Video URL"),
            "slides": _("Slides"),
            "logo": _("Partner Logo"),
            "header_image": _("Partner Header Image"),
            "url": _("Partner URL"),
            "is_sponsor": _("Mark this partner as an event sponsor"),
            "booth_name": _("Booth name"),
            "lead_scanning_enabled": _("Allow lead scanning"),
        }

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        self.event = kwargs.get("event") or getattr(instance, "event", None)
        super().__init__(*args, **kwargs)
        self.fields["sponsor_group"].queryset = SponsorGroup.objects.filter(
            event=self.event
        ).order_by("name")
        self.fields["sponsor_group"].empty_label = _("No sponsor group")
        for field_name in ("logo", "header_image"):
            self.fields[field_name].widget.attrs.setdefault("accept", "image/*")
        self.fields["slides"].widget.attrs.setdefault("accept", ".pdf,application/pdf")
        if self.instance and self.instance.pk:
            self.initial["lead_scanning_scope_by_device"] = (
                self.instance.lead_scanning_scope_by_device
            )
            self.initial["not_an_exhibitor"] = not self.instance.is_exhibitor
        description_field = self.fields.get("description")
        if description_field:
            widget = description_field.widget
            if isinstance(widget, forms.MultiWidget):
                for sub_widget in widget.widgets:
                    sub_widget.attrs.setdefault("rows", 4)
            else:
                widget.attrs.setdefault("rows", 4)

    def clean(self):
        cleaned_data = super().clean()

        video_url = cleaned_data.get("video_url") or ""
        if video_url:
            cleaned_data["video_url"] = normalize_url_scheme(video_url)

        slides_url = cleaned_data.get("slides_url") or ""
        submitted_slides = self.fields["slides"].widget.value_from_datadict(
            self.data,
            self.files,
            self.add_prefix("slides"),
        )
        has_new_slides_upload = isinstance(submitted_slides, UploadedFile)
        if slides_url and has_new_slides_upload:
            message = _("Either upload a PDF or enter an external PDF URL, not both.")
            self.add_error("slides", message)
            self.add_error("slides_url", message)
        else:
            if slides_url:
                normalized_slides_url = normalize_url_scheme(slides_url)
                if not normalized_slides_url.lower().split("?", 1)[0].endswith(".pdf"):
                    self.add_error(
                        "slides_url", _("Slides URL must point to a PDF file.")
                    )
                else:
                    cleaned_data["slides_url"] = normalized_slides_url

            if has_new_slides_upload:
                slides_file = self.files.get(self.add_prefix("slides"))
                filename = (slides_file.name or "").lower() if slides_file else ""
                content_type = (
                    (slides_file.content_type or "").lower() if slides_file else ""
                )
                if not filename.endswith(".pdf"):
                    self.add_error("slides", _("Slides upload must be a PDF file."))
                elif content_type and content_type not in {
                    "application/pdf",
                    "application/x-pdf",
                }:
                    self.add_error("slides", _("Slides upload must be a PDF file."))

        for image_field, url_field in self.file_url_fields.items():
            if image_field == "slides":
                continue
            image_url = cleaned_data.get(url_field) or ""
            submitted_image = self.fields[image_field].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix(image_field),
            )
            has_new_upload = isinstance(submitted_image, UploadedFile)

            if image_url and has_new_upload:
                message = _("Either upload a file or enter an external URL, not both.")
                self.add_error(image_field, message)
                self.add_error(url_field, message)
                continue

            if image_url:
                cleaned_data[url_field] = normalize_url_scheme(image_url)

        cleaned_data["is_exhibitor"] = not cleaned_data.get("not_an_exhibitor", False)

        if not cleaned_data.get("is_sponsor"):
            cleaned_data["sponsor_group"] = None

        if not cleaned_data["is_exhibitor"]:
            cleaned_data["booth_name"] = ""
            cleaned_data["booth_id"] = None
            cleaned_data["lead_scanning_enabled"] = False
            cleaned_data["allow_voucher_access"] = False
            cleaned_data["allow_lead_access"] = False
            cleaned_data["lead_scanning_scope_by_device"] = False

        return cleaned_data

    def save(self, commit=True):
        old_instance = None
        if self.instance and self.instance.pk:
            old_instance = ExhibitorInfo.objects.get(pk=self.instance.pk)

        instance = super().save(commit=False)
        instance.is_exhibitor = self.cleaned_data.get("is_exhibitor", True)

        for image_field, url_field in self.file_url_fields.items():
            if image_field == "slides":
                continue
            previous_file = (
                getattr(old_instance, image_field, None) if old_instance else None
            )
            uploaded_file = self.files.get(self.add_prefix(image_field))
            clear_selected = bool(
                self.data.get(self.add_prefix(f"{image_field}-clear"))
            )
            image_url = self.cleaned_data.get(url_field) or ""

            if image_url:
                if previous_file and previous_file.name:
                    default_storage.delete(previous_file.name)
                setattr(instance, image_field, None)
                setattr(instance, url_field, image_url)
                continue

            if uploaded_file:
                if previous_file and previous_file.name:
                    default_storage.delete(previous_file.name)
                setattr(instance, url_field, "")
                continue

            if clear_selected:
                if previous_file and previous_file.name:
                    default_storage.delete(previous_file.name)
                setattr(instance, image_field, None)
                setattr(instance, url_field, "")

        previous_slides = (
            getattr(old_instance, "slides", None) if old_instance else None
        )
        uploaded_slides = self.files.get(self.add_prefix("slides"))
        clear_slides = bool(self.data.get(self.add_prefix("slides-clear")))
        slides_url = self.cleaned_data.get("slides_url") or ""

        if slides_url:
            if previous_slides and previous_slides.name:
                default_storage.delete(previous_slides.name)
            instance.slides = None
            instance.slides_url = slides_url
        elif uploaded_slides:
            if previous_slides and previous_slides.name:
                default_storage.delete(previous_slides.name)
            instance.slides_url = ""
        elif clear_slides:
            if previous_slides and previous_slides.name:
                default_storage.delete(previous_slides.name)
            instance.slides = None
            instance.slides_url = ""

        if commit:
            instance.save()
            self.save_m2m()

        return instance


class ExhibitorSocialLinkForm(forms.ModelForm):
    network = forms.ChoiceField(
        choices=(("", _("Choose social platform")),) + SOCIAL_LINK_CHOICES,
        required=False,
        label=_("Social platform"),
    )
    path = forms.CharField(
        required=False,
        label=_("Profile or path"),
    )

    class Meta:
        model = ExhibitorSocialLink
        fields = ["network", "url"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["url"].required = False
        self.fields["network"].widget.attrs.update({"class": "form-control"})
        self.fields["path"].widget.attrs.update(
            {
                "class": "form-control",
                "placeholder": _("Profile, handle, or full URL"),
            }
        )

        network = self.initial.get("network") or getattr(self.instance, "network", "")
        if network:
            self.initial["path"] = get_social_link_value(self.instance.url, network)

    def clean(self):
        cleaned_data = super().clean()
        network = cleaned_data.get("network", "")
        path = (cleaned_data.get("path") or "").strip()

        if self.cleaned_data.get("DELETE"):
            return cleaned_data

        if not network and not path:
            if self.has_changed():
                self.add_error(
                    "path",
                    _("Please enter a profile, handle, or URL or remove this row."),
                )
            cleaned_data["url"] = ""
            return cleaned_data

        if not network:
            self.add_error("network", _("Please choose a social platform."))
            return cleaned_data

        if not path:
            self.add_error("path", _("Please enter a profile, handle, or URL."))
            return cleaned_data

        cleaned_data["url"] = build_social_link_url(network, path)
        return cleaned_data

    def save(self, commit=True):
        self.instance.url = self.cleaned_data.get("url", "")
        self.instance.network = self.cleaned_data.get("network", "")
        return super().save(commit=commit)


class ExhibitorExtraLinkForm(forms.ModelForm):
    class Meta:
        model = ExhibitorExtraLink
        fields = ["label", "url"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["label"].widget.attrs.update(
            {
                "class": "form-control",
                "placeholder": _("Link label"),
            }
        )
        self.fields["url"].widget.attrs.update(
            {
                "class": "form-control",
                "placeholder": _("https://example.com"),
            }
        )

    def clean_url(self):
        url = self.cleaned_data.get("url") or ""
        return normalize_url_scheme(url)


ExhibitorSocialLinkFormSet = inlineformset_factory(
    ExhibitorInfo,
    ExhibitorSocialLink,
    form=ExhibitorSocialLinkForm,
    can_delete=True,
    extra=0,
)

ExhibitorExtraLinkFormSet = inlineformset_factory(
    ExhibitorInfo,
    ExhibitorExtraLink,
    form=ExhibitorExtraLinkForm,
    can_delete=True,
    extra=0,
)


def social_link_prefixes() -> dict[str, str]:
    return {key: spec.prefix for key, spec in SOCIAL_LINK_SPECS.items()}
