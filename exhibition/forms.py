from django import forms
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Max
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _
from eventyay.base.forms import I18nModelForm
from eventyay.common.forms.mixins import (
    EventLocalizedModelChoiceField,
    EventLocalizedModelMultipleChoiceField,
)
from eventyay.common.forms.widgets import HtmlDateTimeInput
from eventyay.common.urls import normalize_url_scheme
from eventyay.common.utils.language import localize_event_text

from .models import (
    PROPOSAL_DEFAULT_FIELD_KEYS,
    PROPOSAL_FORMSET_FIELD_KEYS,
    ExhibitionAnswer,
    ExhibitionProposal,
    ExhibitionProposalExtraLink,
    ExhibitionProposalSocialLink,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitionQuestionVariant,
    ExhibitorExtraLink,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorSocialLink,
    SponsorGroup,
    get_next_sponsor_group_level,
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
        label=_("Logo URL"),
        help_text=_("Use an external image URL instead of uploading a logo file."),
    )
    header_image_url = forms.URLField(
        required=False,
        label=_("Header Image URL"),
        help_text=_("Use an external image URL instead of uploading a header image file."),
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
            "booth_id",
            "booth_name",
            "lead_scanning_enabled",
            "allow_voucher_access",
            "allow_lead_access",
            "lead_scanning_scope_by_device",
        ]
        labels = {
            "name": _("Organization Name"),
            "description": _("Organization Description"),
            "email": _("Contact email"),
            "contact_url": _("Contact Page URL"),
            "video_url": _("Promotional Video URL"),
            "slides": _("Promotional Slides"),
            "logo": _("Logo"),
            "header_image": _("Header Image"),
            "url": _("Organization Website"),
            "is_sponsor": _("Mark this partner as an event sponsor"),
            "booth_name": _("Booth name"),
            "lead_scanning_enabled": _("Allow lead scanning"),
        }

    def __init__(self, *args, **kwargs):
        event = kwargs.get("event")
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        self.event = event or getattr(instance, "event", None)
        self.fields["sponsor_group"].queryset = SponsorGroup.objects.filter(event=self.event).order_by("pk")
        self.fields["sponsor_group"].empty_label = _("No sponsor group")
        for field_name in ("logo", "header_image"):
            self.fields[field_name].widget.attrs.setdefault("accept", "image/*")
        self.fields["slides"].widget.attrs.setdefault("accept", ".pdf,application/pdf")
        if self.instance and self.instance.pk:
            self.initial["lead_scanning_scope_by_device"] = self.instance.lead_scanning_scope_by_device
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
                    self.add_error("slides_url", _("Slides URL must point to a PDF file."))
                else:
                    cleaned_data["slides_url"] = normalized_slides_url

            if has_new_slides_upload:
                slides_file = self.files.get(self.add_prefix("slides"))
                filename = (slides_file.name or "").lower() if slides_file else ""
                content_type = (slides_file.content_type or "").lower() if slides_file else ""
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
        files_to_delete: set[str] = set()

        for image_field, url_field in self.file_url_fields.items():
            if image_field == "slides":
                continue
            previous_file = getattr(old_instance, image_field, None) if old_instance else None
            uploaded_file = self.files.get(self.add_prefix(image_field))
            clear_selected = bool(self.data.get(self.add_prefix(f"{image_field}-clear")))
            image_url = self.cleaned_data.get(url_field) or ""

            if image_url:
                if previous_file and previous_file.name:
                    files_to_delete.add(previous_file.name)
                setattr(instance, image_field, None)
                setattr(instance, url_field, image_url)
                continue

            if uploaded_file:
                if previous_file and previous_file.name:
                    files_to_delete.add(previous_file.name)
                setattr(instance, url_field, "")
                continue

            if clear_selected:
                if previous_file and previous_file.name:
                    files_to_delete.add(previous_file.name)
                setattr(instance, image_field, None)
                setattr(instance, url_field, "")

        previous_slides = getattr(old_instance, "slides", None) if old_instance else None
        uploaded_slides = self.files.get(self.add_prefix("slides"))
        clear_slides = bool(self.data.get(self.add_prefix("slides-clear")))
        slides_url = self.cleaned_data.get("slides_url") or ""

        if slides_url:
            if previous_slides and previous_slides.name:
                files_to_delete.add(previous_slides.name)
            instance.slides = None
            instance.slides_url = slides_url
        elif uploaded_slides:
            if previous_slides and previous_slides.name:
                files_to_delete.add(previous_slides.name)
            instance.slides_url = ""
        elif clear_slides:
            if previous_slides and previous_slides.name:
                files_to_delete.add(previous_slides.name)
            instance.slides = None
            instance.slides_url = ""

        if commit:
            instance.save()
            self.save_m2m()
            if files_to_delete:

                def delete_replaced_files():
                    for file_name in files_to_delete:
                        default_storage.delete(file_name)

                transaction.on_commit(delete_replaced_files)

        return instance


class SponsorGroupForm(I18nModelForm):
    level = forms.IntegerField(min_value=1, required=False, label=_("Level"))

    class Meta:
        model = SponsorGroup
        localized_fields = "__all__"
        fields = ["name", "level"]
        labels = {
            "name": _("Group Name"),
        }

    def __init__(self, *args, **kwargs):
        event = kwargs.get("event")
        super().__init__(*args, **kwargs)
        self.event = event or getattr(self.instance, "event", None)

    def clean_level(self):
        level = self.cleaned_data.get("level")
        if level is not None:
            return level
        if self.instance and self.instance.pk:
            return self.instance.level
        return self._default_level()

    def _default_level(self):
        return get_next_sponsor_group_level(self.event)


class CallSettingsForm(I18nModelForm):
    class Meta:
        model = ExhibitorSettings
        localized_fields = "__all__"
        fields = [
            "call_enabled",
            "call_headline",
            "call_text",
            "call_deadline",
            "call_hide_after_deadline",
        ]
        labels = {
            "call_enabled": _("Publish Call"),
            "call_hide_after_deadline": _("Hide call page after the deadline"),
        }
        widgets = {
            "call_deadline": HtmlDateTimeInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        widget = self.fields["call_text"].widget
        if isinstance(widget, forms.MultiWidget):
            for sub_widget in widget.widgets:
                sub_widget.attrs.setdefault("rows", 8)
        else:
            widget.attrs.setdefault("rows", 8)


class ExhibitionQuestionFieldsMixin:
    def inject_exhibition_questions(self, *, event, proposal=None, readonly=False):
        answers_by_question = {}
        if proposal and proposal.pk:
            for answer in proposal.answers.prefetch_related("options"):
                answers_by_question[answer.question_id] = answer

        questions = (
            ExhibitionQuestion.objects.filter(event=event, active=True)
            .prefetch_related("options")
            .order_by("position", "pk")
        )
        for question in questions:
            answer = answers_by_question.get(question.pk)
            field = self.get_exhibition_question_field(
                question=question,
                answer=answer,
                readonly=readonly,
            )
            field.question = question
            field.answer = answer
            self.fields[f"question_{question.pk}"] = field

    def get_exhibition_question_field(self, *, question, answer, readonly):
        label = localize_event_text(question.question)
        help_text = localize_event_text(question.help_text) or ""
        initial = answer.answer if answer else ""

        if question.variant == ExhibitionQuestionVariant.BOOLEAN:
            return forms.BooleanField(
                disabled=readonly,
                help_text=help_text,
                initial=initial == "True",
                label=label,
                required=question.required,
            )
        if question.variant == ExhibitionQuestionVariant.TEXT:
            return forms.CharField(
                disabled=readonly,
                help_text=help_text,
                initial=initial,
                label=label,
                required=question.required,
                widget=forms.Textarea(attrs={"rows": 4}),
            )
        if question.variant == ExhibitionQuestionVariant.URL:
            return forms.URLField(
                disabled=readonly,
                help_text=help_text,
                initial=initial,
                label=label,
                required=question.required,
            )

        choices = question.options.all()
        if question.variant == ExhibitionQuestionVariant.CHOICES:
            return EventLocalizedModelChoiceField(
                disabled=readonly,
                empty_label=None if question.required else _("— No selection —"),
                help_text=help_text,
                initial=answer.options.first() if answer else None,
                label=label,
                queryset=choices,
                required=question.required,
                widget=forms.RadioSelect,
            )
        if question.variant == ExhibitionQuestionVariant.SELECT:
            return EventLocalizedModelChoiceField(
                disabled=readonly,
                empty_label=None if question.required else _("— No selection —"),
                help_text=help_text,
                initial=answer.options.first() if answer else None,
                label=label,
                queryset=choices,
                required=question.required,
            )
        if question.variant == ExhibitionQuestionVariant.MULTIPLE:
            return EventLocalizedModelMultipleChoiceField(
                disabled=readonly,
                help_text=help_text,
                initial=list(answer.options.all()) if answer else [],
                label=label,
                queryset=choices,
                required=question.required,
                widget=forms.CheckboxSelectMultiple,
            )

        return forms.CharField(
            disabled=readonly,
            help_text=help_text,
            initial=initial,
            label=label,
            required=question.required,
        )

    def save_exhibition_questions(self, proposal):
        for key, value in self.cleaned_data.items():
            if not key.startswith("question_"):
                continue
            field = self.fields[key]
            question = field.question
            answer = field.answer
            empty = value in ("", None, False) or (
                hasattr(value, "__len__") and not isinstance(value, str) and len(value) == 0
            )

            if empty:
                if answer:
                    answer.delete()
                continue

            if not answer:
                answer = ExhibitionAnswer(proposal=proposal, question=question)

            if isinstance(field, forms.ModelMultipleChoiceField):
                selected_options = list(value)
                answer.answer = ", ".join(str(option) for option in selected_options)
                answer.save()
                answer.options.set(selected_options)
            elif isinstance(field, forms.ModelChoiceField):
                answer.answer = str(value.answer) if value else ""
                answer.save()
                answer.options.set([value] if value else [])
            elif isinstance(field, forms.BooleanField):
                answer.answer = "True" if value else "False"
                answer.save()
                answer.options.clear()
            else:
                answer.answer = value
                answer.save()
                answer.options.clear()


class ExhibitionProposalForm(ExhibitionQuestionFieldsMixin, I18nModelForm):
    slides_url = forms.URLField(
        required=False,
        label=_("Slides URL"),
        help_text=_("Use an external PDF URL instead of uploading a slides file."),
    )
    logo_url = forms.URLField(
        required=False,
        label=_("Logo URL"),
        help_text=_("Use an external image URL instead of uploading a logo file."),
    )
    header_image_url = forms.URLField(
        required=False,
        label=_("Header Image URL"),
        help_text=_("Use an external image URL instead of uploading a header image file."),
    )

    file_url_fields = {
        "slides": "slides_url",
        "logo": "logo_url",
        "header_image": "header_image_url",
    }
    setting_field_map = {
        "name": ("name",),
        "description": ("description",),
        "email": ("email",),
        "url": ("url",),
        "contact_url": ("contact_url",),
        "video_url": ("video_url",),
        "slides": ("slides", "slides_url"),
        "logo": ("logo", "logo_url"),
        "header_image": ("header_image", "header_image_url"),
        "booth_name": ("booth_name",),
        "notes": ("notes",),
    }

    class Meta:
        model = ExhibitionProposal
        localized_fields = "__all__"
        fields = [
            "name",
            "description",
            "email",
            "contact_url",
            "video_url",
            "slides",
            "slides_url",
            "logo",
            "logo_url",
            "header_image",
            "header_image_url",
            "url",
            "booth_name",
            "notes",
        ]
        labels = {
            "name": _("Organization Name"),
            "description": _("Organization Description"),
            "email": _("Contact email"),
            "contact_url": _("Contact Page URL"),
            "video_url": _("Promotional Video URL"),
            "slides": _("Promotional Slides"),
            "logo": _("Logo"),
            "header_image": _("Header Image"),
            "url": _("Organization Website"),
            "booth_name": _("Preferred booth name"),
            "notes": _("Message to the organizers"),
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        event = kwargs.get("event")
        self.read_only = kwargs.pop("read_only", False)
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        self.event = event or getattr(instance, "event", None)
        self.exhibition_settings = None
        self.active_proposal_fields = {}
        self.required_proposal_fields = {}
        if self.event:
            self.exhibition_settings = ExhibitorSettings.objects.get_or_create(event=self.event)[0]
            proposal_field_settings = self.exhibition_settings.normalized_proposal_field_settings
            self.active_proposal_fields = {key: value["active"] for key, value in proposal_field_settings.items()}
            self.required_proposal_fields = {key: value["required"] for key, value in proposal_field_settings.items()}
        for field_name in ("logo", "header_image"):
            if field_name in self.fields:
                self.fields[field_name].widget.attrs.setdefault("accept", "image/*")
        if "slides" in self.fields:
            self.fields["slides"].widget.attrs.setdefault("accept", ".pdf,application/pdf")
        description_field = self.fields.get("description")
        if description_field:
            widget = description_field.widget
            if isinstance(widget, forms.MultiWidget):
                for sub_widget in widget.widgets:
                    sub_widget.attrs.setdefault("rows", 4)
            else:
                widget.attrs.setdefault("rows", 4)
        if self.event:
            self.apply_proposal_field_settings()
            self.inject_exhibition_questions(
                event=self.event,
                proposal=instance,
                readonly=self.read_only,
            )
            self.apply_proposal_field_order()
        if self.read_only:
            for field in self.fields.values():
                field.disabled = True

    def apply_proposal_field_settings(self):
        file_field_keys = set(self.file_url_fields)
        for key, form_fields in self.setting_field_map.items():
            is_active = self.active_proposal_fields.get(key, True)
            is_required = self.required_proposal_fields.get(key, False)
            if not is_active:
                for field_name in form_fields:
                    self.fields.pop(field_name, None)
                continue

            if key in file_field_keys or key == "booth_name":
                continue

            for field_name in form_fields:
                if field_name in self.fields:
                    self.fields[field_name].required = is_required

    def apply_proposal_field_order(self):
        if not self.exhibition_settings:
            return
        field_settings = self.exhibition_settings.normalized_proposal_field_settings
        formset_keys = set(PROPOSAL_FORMSET_FIELD_KEYS)
        entries = []
        for key in PROPOSAL_DEFAULT_FIELD_KEYS:
            if key in formset_keys:
                continue
            entries.append((field_settings[key]["position"], 0, self.setting_field_map.get(key, ())))
        for field_name, field in self.fields.items():
            question = getattr(field, "question", None)
            if question is not None:
                entries.append((question.position, 1, (field_name,)))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        ordered_field_names = []
        for _position, _kind, field_names in entries:
            for field_name in field_names:
                if field_name in self.fields:
                    ordered_field_names.append(field_name)
        self.order_fields(ordered_field_names)

    def field_setting_is_active(self, key):
        return self.active_proposal_fields.get(key, True)

    def field_setting_is_required(self, key):
        return self.required_proposal_fields.get(key, False)

    def clean(self):
        cleaned_data = super().clean()
        if self.instance.pk:
            cleaned_data["is_exhibitor"] = self.instance.is_exhibitor
            cleaned_data["is_sponsor"] = self.instance.is_sponsor
        else:
            cleaned_data["is_exhibitor"] = True
            cleaned_data["is_sponsor"] = False

        if "video_url" in self.fields and (video_url := cleaned_data.get("video_url")):
            cleaned_data["video_url"] = normalize_url_scheme(video_url)

        slides_url = cleaned_data.get("slides_url") or ""
        submitted_slides = None
        if "slides" in self.fields:
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
        elif slides_url:
            normalized_slides_url = normalize_url_scheme(slides_url)
            if not normalized_slides_url.lower().split("?", 1)[0].endswith(".pdf"):
                self.add_error("slides_url", _("Slides URL must point to a PDF file."))
            else:
                cleaned_data["slides_url"] = normalized_slides_url

        for image_field, url_field in self.file_url_fields.items():
            if image_field == "slides":
                continue
            if image_field not in self.fields and url_field not in self.fields:
                continue
            image_url = cleaned_data.get(url_field) or ""
            submitted_image = None
            if image_field in self.fields:
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
            elif image_url:
                cleaned_data[url_field] = normalize_url_scheme(image_url)

        self.validate_required_file_or_url("slides", has_new_slides_upload)
        for image_field in ("logo", "header_image"):
            if image_field not in self.fields and self.file_url_fields[image_field] not in self.fields:
                continue
            submitted_image = None
            if image_field in self.fields:
                submitted_image = self.fields[image_field].widget.value_from_datadict(
                    self.data,
                    self.files,
                    self.add_prefix(image_field),
                )
            self.validate_required_file_or_url(
                image_field,
                isinstance(submitted_image, UploadedFile),
            )

        if not cleaned_data["is_exhibitor"]:
            cleaned_data["booth_name"] = ""
        elif (
            self.field_setting_is_required("booth_name")
            and "booth_name" in self.fields
            and not cleaned_data.get("booth_name")
        ):
            self.add_error("booth_name", _("This field is required."))

        return cleaned_data

    def validate_required_file_or_url(self, field_name, has_new_upload):
        if not self.field_setting_is_active(field_name) or not self.field_setting_is_required(field_name):
            return
        file_field = self.fields.get(field_name)
        url_field_name = self.file_url_fields[field_name]
        if file_field is None and url_field_name not in self.fields:
            return
        has_url = bool(self.cleaned_data.get(url_field_name))
        has_existing = bool(getattr(self.instance, f"visible_{field_name}_url", ""))
        if not has_new_upload and not has_url and not has_existing:
            self.add_error(
                field_name if file_field else url_field_name,
                _("This field is required."),
            )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.is_exhibitor = self.cleaned_data.get("is_exhibitor", True)
        instance.is_sponsor = self.cleaned_data.get("is_sponsor", False)
        if not instance.is_exhibitor:
            instance.booth_name = ""
            instance.booth_id = None
        if commit:
            instance.save()
            self.save_m2m()
            self.save_exhibition_questions(instance)
        return instance


class ExhibitionProposalReviewForm(I18nModelForm):
    sponsor_group = forms.ModelChoiceField(
        queryset=SponsorGroup.objects.none(),
        required=False,
        label=_("Sponsor Group"),
    )

    class Meta:
        model = ExhibitionProposal
        localized_fields = "__all__"
        fields = [
            "is_exhibitor",
            "is_sponsor",
            "sponsor_group",
            "booth_id",
            "booth_name",
            "review_notes",
        ]
        labels = {
            "is_exhibitor": _("Approve as exhibitor"),
            "is_sponsor": _("Approve as sponsor"),
            "booth_id": _("Booth ID"),
            "booth_name": _("Booth name"),
            "review_notes": _("Internal review notes"),
        }
        widgets = {
            "review_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        event = kwargs.get("event")
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        self.event = event or getattr(instance, "event", None)
        self.fields["sponsor_group"].queryset = SponsorGroup.objects.filter(event=self.event).order_by("level", "pk")
        self.fields["sponsor_group"].empty_label = _("No sponsor group")

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("is_sponsor"):
            cleaned_data["sponsor_group"] = None
        if not cleaned_data.get("is_exhibitor"):
            cleaned_data["booth_id"] = None
            cleaned_data["booth_name"] = ""
        return cleaned_data


class ExhibitionProposalReviewNotesForm(I18nModelForm):
    class Meta:
        model = ExhibitionProposal
        localized_fields = "__all__"
        fields = ["review_notes"]
        labels = {
            "review_notes": _("Internal review notes"),
        }
        widgets = {
            "review_notes": forms.Textarea(attrs={"rows": 4}),
        }


class ExhibitionQuestionForm(I18nModelForm):
    options_text = forms.CharField(
        required=False,
        label=_("Options"),
        help_text=_("For choice fields, enter one option per line."),
        widget=forms.Textarea(attrs={"rows": 6}),
    )

    class Meta:
        model = ExhibitionQuestion
        localized_fields = "__all__"
        fields = [
            "variant",
            "question",
            "help_text",
            "required",
            "active",
            "position",
        ]
        labels = {
            "variant": _("Field type"),
            "question": _("Custom question"),
            "help_text": _("Help text"),
            "required": _("Required"),
            "active": _("Active"),
            "position": _("Position"),
        }

    choice_variants = {
        ExhibitionQuestionVariant.CHOICES,
        ExhibitionQuestionVariant.MULTIPLE,
        ExhibitionQuestionVariant.SELECT,
    }

    def __init__(self, *args, **kwargs):
        self.event = kwargs.get("event")
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["options_text"].initial = "\n".join(str(option) for option in self.instance.options.all())
        elif not self.initial.get("position") and self.event:
            max_position = ExhibitionQuestion.objects.filter(event=self.event).aggregate(Max("position"))[
                "position__max"
            ]
            self.initial["position"] = max((max_position or -1) + 1, len(PROPOSAL_DEFAULT_FIELD_KEYS))

    def clean(self):
        cleaned_data = super().clean()
        variant = cleaned_data.get("variant")
        options = [option.strip() for option in (cleaned_data.get("options_text") or "").splitlines() if option.strip()]
        if variant in self.choice_variants and not options:
            self.add_error(
                "options_text",
                _("Please provide at least one option for this question type."),
            )
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.event:
            instance.event = self.event
        if commit:
            instance.save()
            self.save_options(instance)
        return instance

    def save_options(self, question):
        if question.variant not in self.choice_variants:
            question.options.all().delete()
            return
        options = [
            option.strip() for option in (self.cleaned_data.get("options_text") or "").splitlines() if option.strip()
        ]
        question.options.all().delete()
        locale = self.event.locale if self.event else "en"
        ExhibitionQuestionOption.objects.bulk_create(
            [
                ExhibitionQuestionOption(
                    question=question,
                    answer={locale: option},
                    position=index,
                )
                for index, option in enumerate(options)
            ]
        )


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


class ExhibitionProposalSocialLinkForm(forms.ModelForm):
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
        model = ExhibitionProposalSocialLink
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


class ExhibitionProposalExtraLinkForm(forms.ModelForm):
    class Meta:
        model = ExhibitionProposalExtraLink
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

ExhibitionProposalSocialLinkFormSet = inlineformset_factory(
    ExhibitionProposal,
    ExhibitionProposalSocialLink,
    form=ExhibitionProposalSocialLinkForm,
    can_delete=True,
    extra=0,
)

ExhibitionProposalExtraLinkFormSet = inlineformset_factory(
    ExhibitionProposal,
    ExhibitionProposalExtraLink,
    form=ExhibitionProposalExtraLinkForm,
    can_delete=True,
    extra=0,
)


def social_link_prefixes() -> dict[str, str]:
    return {key: spec.prefix for key, spec in SOCIAL_LINK_SPECS.items()}
