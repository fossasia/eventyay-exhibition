from html import unescape

import dateutil.parser
from django import forms
from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Max
from django.forms import inlineformset_factory
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django_countries.fields import CountryField
from eventyay.base.forms import I18nFormSet, I18nModelForm, SettingsForm
from eventyay.base.forms.widgets import (
    DatePickerWidget,
    SplitDateTimePickerWidget,
    TimePickerWidget,
)
from eventyay.base.templatetags.rich_text import compile_email_body
from eventyay.common.forms.fields import I18nEmailBodyFormField
from eventyay.common.forms.mixins import (
    EventLocalizedModelChoiceField,
    EventLocalizedModelMultipleChoiceField,
)
from eventyay.common.forms.widgets import EmailEditorWidget, HtmlDateTimeInput, I18nEmailEditorWidget
from eventyay.common.utils.language import localize_event_text
from eventyay.consts import SizeKey
from eventyay.control.forms import ExtFileField, SplitDateTimeField
from eventyay.helpers.countries import CachedCountries
from eventyay.helpers.i18n import get_format_without_seconds, is_rtl
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString
from phonenumber_field.formfields import PhoneNumberField
from phonenumber_field.widgets import PhoneNumberPrefixWidget

from . import mail as mail_helpers
from .models import (
    PROPOSAL_DEFAULT_FIELD_KEYS,
    PROPOSAL_FORMSET_FIELD_KEYS,
    QUESTION_OPTION_VARIANTS,
    ExhibitionAnswer,
    ExhibitionCustomEmailTemplate,
    ExhibitionEmailQueue,
    ExhibitionProposal,
    ExhibitionProposalSocialLink,
    ExhibitionProposalState,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitionQuestionVariant,
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
from .utils import localized_value_for, merge_localized_value, pool_tag_choices


def get_tz_help(event):
    return _("Times are in the event timezone: %(tz)s.") % {"tz": event.timezone}


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


class ExhibitorInfoForm(ExhibitionQuestionFieldsMixin, I18nModelForm):
    sponsor_group = forms.ModelChoiceField(
        queryset=SponsorGroup.objects.none(),
        required=False,
        label=_("Sponsor group"),
    )
    allow_voucher_access = forms.BooleanField(
        required=False,
        label=_("Can view voucher redemptions"),
        help_text=_(
            "Lets this exhibitor or sponsor retrieve the attendees who redeemed vouchers issued to them. "
            "Separate from lead scanning, which covers attendees scanned at the booth."
        ),
    )
    allow_lead_access = forms.BooleanField(
        required=False,
        label=_("Can view and export collected leads"),
        help_text=_(
            "Lets this exhibitor retrieve the full list of leads they have already scanned. "
            "Turn this off to let them keep scanning without seeing the collected attendee data."
        ),
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
            "logo",
            "header_image",
            "is_exhibitor",
            "is_sponsor",
            "sponsor_group",
            "booth_id",
            "lead_scanning_enabled",
            "allow_voucher_access",
            "allow_lead_access",
            "lead_scanning_scope_by_device",
        ]
        labels = {
            "name": _("Organization name"),
            "description": _("Organization description"),
            "logo": _("Logo"),
            "header_image": _("Header image"),
            "url": _("Organization website"),
            "is_exhibitor": _("Mark this partner as an exhibitor"),
            "is_sponsor": _("Mark this partner as an event sponsor"),
            "lead_scanning_enabled": _("Can scan attendee badges"),
        }
        help_texts = {
            "lead_scanning_enabled": _(
                "Lets this exhibitor sign in to the lead scanning app and scan attendees at their booth. "
                "Turn this off to block scanning entirely."
            ),
        }

    PROFILE_SETTING_FIELD_MAP = {
        "name": ("name",),
        "description": ("description",),
        "url": ("url",),
        "logo": ("logo",),
        "header_image": ("header_image",),
    }
    PROFILE_FORMSET_KEYS = ("social_links",)
    PROFILE_COMPOSITE_KEYS = ("logo", "header_image")

    SPONSOR_ONLY_FIELDS = ("sponsor_group",)
    EXHIBITOR_ONLY_FIELDS = (
        "booth_id",
        "lead_scanning_enabled",
        "allow_lead_access",
        "lead_scanning_scope_by_device",
    )

    def __init__(self, *args, **kwargs):
        self.partner_type = kwargs.pop("partner_type", None)
        event = kwargs.get("event")
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        self.event = event or getattr(instance, "event", None)
        if self.partner_type == "sponsor":
            self._drop_fields(self.EXHIBITOR_ONLY_FIELDS + ("is_sponsor",))
        elif self.partner_type == "exhibitor":
            self._drop_fields(self.SPONSOR_ONLY_FIELDS + ("is_exhibitor",))
        if "sponsor_group" in self.fields:
            self.fields["sponsor_group"].queryset = SponsorGroup.objects.filter(event=self.event).order_by("pk")
            self.fields["sponsor_group"].empty_label = _("No sponsor group")
        for field_name in ("logo", "header_image"):
            self.fields[field_name].widget.attrs.setdefault("accept", "image/*")
        if self.instance and self.instance.pk:
            self.initial["lead_scanning_scope_by_device"] = self.instance.lead_scanning_scope_by_device
        description_field = self.fields.get("description")
        if description_field:
            widget = description_field.widget
            if isinstance(widget, forms.MultiWidget):
                for sub_widget in widget.widgets:
                    sub_widget.attrs.setdefault("rows", 4)
            else:
                widget.attrs.setdefault("rows", 4)
        self.profile_field_settings = {}
        self.ordered_profile_keys = []
        if self.event:
            settings = ExhibitorSettings.objects.get_or_create(event=self.event)[0]
            self.profile_field_settings = settings.normalized_proposal_field_settings
            self._apply_profile_field_settings()
            self.ordered_profile_keys = [
                key for key in settings.ordered_proposal_field_keys if self.profile_key_is_active(key)
            ]
            self._apply_profile_field_order()
        self._set_voucher_access_help_text()
        self.linked_proposal = self._resolve_linked_proposal()
        if self.event and self.linked_proposal:
            self.inject_exhibition_questions(event=self.event, proposal=self.linked_proposal)

    def _resolve_linked_proposal(self):
        """The approved request this profile was created from, if any."""
        if not (self.instance and self.instance.pk):
            return None
        return self.instance.source_proposals.order_by("pk").first()

    VOUCHER_ACCESS_HELP_TEXTS = {
        "exhibitor": _(
            "Lets this exhibitor retrieve the attendees who redeemed vouchers issued to them. "
            "Separate from lead scanning, which covers attendees scanned at the booth."
        ),
        "sponsor": _(
            "Lets this sponsor retrieve the attendees who redeemed vouchers issued to them. "
            "Separate from lead scanning, which covers attendees scanned at the booth."
        ),
        "both": _(
            "Lets this exhibitor and sponsor retrieve the attendees who redeemed vouchers issued to them. "
            "Separate from lead scanning, which covers attendees scanned at the booth."
        ),
        "unset": _(
            "Lets this exhibitor or sponsor retrieve the attendees who redeemed vouchers issued to them. "
            "Separate from lead scanning, which covers attendees scanned at the booth."
        ),
    }

    def _voucher_access_audience(self):
        if self.partner_type in ("exhibitor", "sponsor"):
            return self.partner_type
        if self.instance and self.instance.pk:
            if self.instance.is_exhibitor and self.instance.is_sponsor:
                return "both"
            if self.instance.is_exhibitor:
                return "exhibitor"
            if self.instance.is_sponsor:
                return "sponsor"
        return "unset"

    def _set_voucher_access_help_text(self):
        field = self.fields.get("allow_voucher_access")
        if field is not None:
            field.help_text = self.VOUCHER_ACCESS_HELP_TEXTS[self._voucher_access_audience()]

    def _apply_profile_field_settings(self):
        for key, field_names in self.PROFILE_SETTING_FIELD_MAP.items():
            if not self.profile_key_is_active(key):
                self._drop_fields(field_names)
                continue

            setting = self.profile_field_settings[key]
            is_required = bool(setting["required"])
            for index, field_name in enumerate(field_names):
                field = self.fields.get(field_name)
                if field is None:
                    continue
                if index == 0:
                    if setting.get("custom_label"):
                        field.label = setting["custom_label"]
                    if setting.get("help_text"):
                        field.help_text = setting["help_text"]
                field._required = is_required
                if key in self.PROFILE_COMPOSITE_KEYS:
                    continue
                if isinstance(field, I18nFormField):
                    field.one_required = is_required
                else:
                    field.required = is_required

    def _apply_profile_field_order(self):
        ordered_field_names = []
        for key in self.ordered_profile_keys:
            for field_name in self.PROFILE_SETTING_FIELD_MAP.get(key, ()):
                if field_name in self.fields:
                    ordered_field_names.append(field_name)
        self.order_fields(ordered_field_names)

    def profile_key_is_active(self, key):
        setting = self.profile_field_settings.get(key)
        return bool(setting["active"]) if setting else False

    def profile_key_is_required(self, key):
        setting = self.profile_field_settings.get(key)
        return bool(setting["active"] and setting["required"]) if setting else False

    def _validate_required_file(self, field_name, has_new_upload):
        """Flag a required file field when no upload or existing file is present."""
        if not self.profile_key_is_required(field_name) or field_name not in self.fields:
            return
        has_existing = bool(getattr(self.instance, f"visible_{field_name}_url", ""))
        if not has_new_upload and not has_existing:
            self.add_error(field_name, _("This field is required."))

    @property
    def profile_items(self):
        items = []
        for key in self.ordered_profile_keys:
            if key in self.PROFILE_FORMSET_KEYS:
                items.append({"kind": key, "key": key})
                continue
            field_names = [name for name in self.PROFILE_SETTING_FIELD_MAP.get(key, ()) if name in self.fields]
            if not field_names:
                continue
            if key in self.PROFILE_COMPOSITE_KEYS:
                items.append({"kind": key, "key": key})
            else:
                items.append({"kind": "field", "key": key, "field": self[field_names[0]]})
        for name in self.fields:
            if name.startswith("question_"):
                items.append({"kind": "field", "key": name, "field": self[name]})
        return items

    def _drop_fields(self, names):
        for name in names:
            self.fields.pop(name, None)

    def clean(self):
        cleaned_data = super().clean()

        for image_field in self.file_url_fields:
            if image_field not in self.fields:
                continue
            submitted_image = self.fields[image_field].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix(image_field),
            )
            self._validate_required_file(image_field, isinstance(submitted_image, UploadedFile))

        if self.partner_type == "sponsor":
            is_sponsor = True
            is_exhibitor = bool(cleaned_data.get("is_exhibitor"))
        elif self.partner_type == "exhibitor":
            is_sponsor = bool(cleaned_data.get("is_sponsor"))
            is_exhibitor = True
        else:
            is_sponsor = bool(cleaned_data.get("is_sponsor"))
            is_exhibitor = bool(cleaned_data.get("is_exhibitor"))
            if not is_sponsor and not is_exhibitor:
                self.add_error(None, _("A partner must be marked as an exhibitor, a sponsor, or both."))
        self._resolved_is_sponsor = is_sponsor
        cleaned_data["is_exhibitor"] = is_exhibitor

        if not is_sponsor:
            cleaned_data["sponsor_group"] = None

        if not is_exhibitor:
            cleaned_data["booth_id"] = None
            cleaned_data["lead_scanning_enabled"] = False
            cleaned_data["allow_lead_access"] = False
            cleaned_data["lead_scanning_scope_by_device"] = False

        for name in ("is_exhibitor", "is_sponsor") + self.SPONSOR_ONLY_FIELDS + self.EXHIBITOR_ONLY_FIELDS:
            if name not in self.fields:
                cleaned_data.pop(name, None)

        return cleaned_data

    def save(self, commit=True):
        old_instance = None
        if self.instance and self.instance.pk:
            old_instance = ExhibitorInfo.objects.get(pk=self.instance.pk)

        instance = super().save(commit=False)
        instance.is_exhibitor = self.cleaned_data.get("is_exhibitor", True)
        instance.is_sponsor = getattr(self, "_resolved_is_sponsor", instance.is_sponsor)
        files_to_delete: set[str] = set()

        for image_field, url_field in self.file_url_fields.items():
            previous_file = getattr(old_instance, image_field, None) if old_instance else None
            uploaded_file = self.files.get(self.add_prefix(image_field))
            clear_selected = bool(self.data.get(self.add_prefix(f"{image_field}-clear")))

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

        if commit:
            instance.save()
            self.save_m2m()
            if self.linked_proposal:
                self.save_exhibition_questions(self.linked_proposal)
            if files_to_delete:

                def delete_replaced_files():
                    for file_name in files_to_delete:
                        default_storage.delete(file_name)

                transaction.on_commit(delete_replaced_files)

        return instance


class ExhibitorDeviceProvisionForm(forms.Form):
    count = forms.IntegerField(
        min_value=1,
        max_value=50,
        initial=1,
        label=_("Devices to add"),
        help_text=_(
            "How many new devices to provision now, in addition to any already listed above. "
            "Each device gets its own single-use setup token and QR code."
        ),
    )


class ExhibitorVoucherBatchForm(forms.Form):
    """How many more pool vouchers to hand this partner now."""

    count = forms.IntegerField(
        min_value=0,
        max_value=1000,
        initial=1,
        label=_("Vouchers to take from the pool"),
        help_text=_("Set to 0 to email the codes this partner already has without taking any more."),
    )


class VoucherDefaultsFormMixin:
    """Shared wiring for the forms that set how many pool vouchers a partner receives."""

    voucher_default_fields = ["voucher_default_count"]


class SponsorGroupForm(VoucherDefaultsFormMixin, I18nModelForm):
    level = forms.IntegerField(min_value=1, required=False, label=_("Level"))

    class Meta:
        model = SponsorGroup
        localized_fields = "__all__"
        fields = ["name", "level", *VoucherDefaultsFormMixin.voucher_default_fields]
        labels = {
            "name": _("Group name"),
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


class ExhibitorVoucherDefaultsForm(VoucherDefaultsFormMixin, forms.ModelForm):
    """Which pools partners draw from, and how many codes each one gets by default."""

    voucher_pool_tag = forms.ChoiceField(required=False, label=_("Exhibitor voucher pool"))
    sponsor_voucher_pool_tag = forms.ChoiceField(required=False, label=_("Sponsor voucher pool"))

    class Meta:
        model = ExhibitorSettings
        fields = [
            "voucher_pool_tag",
            "sponsor_voucher_pool_tag",
            *VoucherDefaultsFormMixin.voucher_default_fields,
            "voucher_attach_csv",
        ]

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event", None)
        super().__init__(*args, **kwargs)
        self._wire_pool_fields()

    def _wire_pool_fields(self):
        """Offer the tags that already exist on the event, keeping any pool that has since been emptied."""
        tags = pool_tag_choices(self.event) if self.event else []
        for name, empty_label in (
            ("voucher_pool_tag", _("— No pool selected —")),
            ("sponsor_voucher_pool_tag", _("— Use the exhibitor pool —")),
        ):
            current = self.get_initial_for_field(self.fields[name], name)
            known = tags if not current or current in tags else [*tags, current]
            self.fields[name].choices = [("", empty_label), *((tag, tag) for tag in known)]


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
            "call_private",
        ]
        labels = {
            "call_enabled": _("Enable call"),
            "call_hide_after_deadline": _("Hide call page after the deadline"),
            "call_private": _("Make this call private (accessible only via a secret link)"),
        }
        help_texts = {
            "call_enabled": _(
                "Turn the call on. Keep it enabled for private calls too, otherwise the secret link stops working."
            ),
            "call_private": _(
                "The call page is not linked anywhere public and can only be opened with the secret link shown below."
            ),
        }
        widgets = {
            "call_deadline": HtmlDateTimeInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["call_text"] = I18nFormField(
            label=self.fields["call_text"].label,
            required=False,
            widget=I18nEmailEditorWidget,
            widget_kwargs={"attrs": {"rows": 8, "data-tiptap-profile": "richtext"}},
        )
        if self.event:
            self.fields["call_text"].widget.enabled_locales = self.event.settings.get("locales")
            self.fields["call_deadline"].help_text = get_tz_help(self.event)
            self.fields["call_deadline"].widget.attrs.update(
                {
                    "data-schedule-datetime": "1",
                    "data-event-timezone": self.event.timezone,
                }
            )


ANSWER_FILE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".jfif",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".svg",
    ".pdf",
    ".txt",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pages",
)


def parse_answer_datetime(value):
    if not value:
        return None
    try:
        return dateutil.parser.parse(value).astimezone(timezone.get_current_timezone())
    except (ValueError, OverflowError):
        return None


def parse_answer_date(value):
    parsed = parse_answer_datetime(value)
    return parsed.date() if parsed else None


def parse_answer_time(value):
    if not value:
        return None
    try:
        return dateutil.parser.parse(value).time()
    except (ValueError, OverflowError):
        return None


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
        if question.variant == ExhibitionQuestionVariant.EMAIL:
            return forms.EmailField(
                disabled=readonly,
                help_text=help_text,
                initial=initial,
                label=label,
                required=question.required,
            )
        if question.variant == ExhibitionQuestionVariant.NUMBER:
            return forms.DecimalField(
                disabled=readonly,
                help_text=help_text,
                initial=initial or None,
                label=label,
                required=question.required,
                widget=forms.NumberInput(attrs={"placeholder": _("Your answer")}),
            )
        if question.variant == ExhibitionQuestionVariant.PHONE:
            return PhoneNumberField(
                disabled=readonly,
                help_text=help_text,
                initial=initial or None,
                label=label,
                required=question.required,
                widget=PhoneNumberPrefixWidget(),
            )
        if question.variant == ExhibitionQuestionVariant.COUNTRY:
            return CountryField(countries=CachedCountries, blank=True, blank_label=" ").formfield(
                disabled=readonly,
                empty_label=" ",
                help_text=help_text,
                initial=initial or None,
                label=label,
                required=question.required,
                widget=forms.Select,
            )
        if question.variant == ExhibitionQuestionVariant.DATE:
            return forms.DateField(
                disabled=readonly,
                help_text=help_text,
                initial=parse_answer_date(initial),
                label=label,
                required=question.required,
                widget=DatePickerWidget(),
            )
        if question.variant == ExhibitionQuestionVariant.TIME:
            return forms.TimeField(
                disabled=readonly,
                help_text=help_text,
                initial=parse_answer_time(initial),
                label=label,
                required=question.required,
                widget=TimePickerWidget(time_format=get_format_without_seconds("TIME_INPUT_FORMATS")),
            )
        if question.variant == ExhibitionQuestionVariant.DATETIME:
            return SplitDateTimeField(
                disabled=readonly,
                help_text=help_text,
                initial=parse_answer_datetime(initial),
                label=label,
                required=question.required,
                widget=SplitDateTimePickerWidget(
                    time_format=get_format_without_seconds("TIME_INPUT_FORMATS"),
                ),
            )
        if question.variant == ExhibitionQuestionVariant.FILE:
            return ExtFileField(
                disabled=readonly,
                ext_whitelist=ANSWER_FILE_EXTENSIONS,
                help_text=help_text,
                initial=answer.file if answer else None,
                label=label,
                max_size=django_settings.MAX_SIZE_CONFIG[SizeKey.UPLOAD_SIZE_QUESTION],
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
            empty = value in ("", None) or (
                hasattr(value, "__len__") and not isinstance(value, str) and len(value) == 0
            )
            if isinstance(field, ExtFileField):
                empty = value is None
            elif value is False:
                empty = True

            if empty:
                if answer:
                    answer.delete()
                continue

            if not answer:
                answer = ExhibitionAnswer(proposal=proposal, question=question)

            if isinstance(field, ExtFileField):
                if value is False:
                    answer.file.delete(save=False)
                    answer.file = None
                    answer.answer = ""
                elif isinstance(value, UploadedFile):
                    answer.file = value
                    answer.answer = value.name
                answer.save()
                answer.options.clear()
            elif isinstance(field, forms.ModelMultipleChoiceField):
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
                answer.answer = value.isoformat() if hasattr(value, "isoformat") else str(value)
                answer.save()
                answer.options.clear()


class ExhibitionProposalForm(ExhibitionQuestionFieldsMixin, I18nModelForm):
    content_locale = forms.ChoiceField(
        label=_("Language"),
        help_text=_("The language you are filling in this form with."),
    )
    name = forms.CharField(max_length=190, label=_("Organization name"))
    description = forms.CharField(
        required=False,
        label=_("Organization description"),
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    booth_name = forms.CharField(
        max_length=100,
        required=False,
        label=_("Preferred booth name"),
    )

    file_url_fields = {
        "logo": "logo_url",
        "header_image": "header_image_url",
    }
    setting_field_map = {
        "name": ("name",),
        "description": ("description",),
        "url": ("url",),
        "logo": ("logo",),
        "header_image": ("header_image",),
    }
    DRAFT_REQUIRED_KEYS = ("name",)

    class Meta:
        model = ExhibitionProposal
        localized_fields = "__all__"
        fields = [
            "name",
            "description",
            "logo",
            "header_image",
            "url",
        ]
        labels = {
            "name": _("Organization name"),
            "description": _("Organization description"),
            "logo": _("Logo"),
            "header_image": _("Header image"),
            "url": _("Organization website"),
        }

    SINGLE_LOCALE_FIELDS = ("name", "description", "booth_name")

    def __init__(self, *args, **kwargs):
        event = kwargs.get("event")
        self.read_only = kwargs.pop("read_only", False)
        self.draft_save = kwargs.pop("draft_save", False)
        instance = kwargs.get("instance")
        resolved_event = event or getattr(instance, "event", None)
        self.selected_content_locale = self._resolve_content_locale(resolved_event, instance)
        kwargs["initial"] = self._build_localized_initial(kwargs.pop("initial", None), instance)
        super().__init__(*args, **kwargs)
        self.event = resolved_event
        self._stored_localized_values = {
            field_name: getattr(self.instance, field_name, None) for field_name in self.SINGLE_LOCALE_FIELDS
        }
        self._set_content_locale_choices()
        self.exhibition_settings = None
        self.proposal_field_settings = {}
        self.active_proposal_fields = {}
        self.required_proposal_fields = {}
        if self.event:
            self.exhibition_settings = ExhibitorSettings.objects.get_or_create(event=self.event)[0]
            self.proposal_field_settings = self.exhibition_settings.normalized_proposal_field_settings
            self.active_proposal_fields = {key: value["active"] for key, value in self.proposal_field_settings.items()}
            self.required_proposal_fields = {
                key: value["required"] for key, value in self.proposal_field_settings.items()
            }
        for field_name in ("logo", "header_image"):
            if field_name in self.fields:
                self.fields[field_name].widget.attrs.setdefault("accept", "image/*")
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
        self._apply_content_text_direction()
        if self.read_only:
            for field in self.fields.values():
                field.disabled = True
        elif instance and instance.pk and instance.state == ExhibitionProposalState.ACCEPTED:
            name_field = self.fields.get("name")
            if name_field is not None:
                name_field.disabled = True
                name_field.help_text = _(
                    "The organization name is locked after acceptance. Contact the organizers to change it."
                )

    @staticmethod
    def _resolve_content_locale(event, instance):
        if instance is not None and getattr(instance, "content_locale", None):
            return instance.content_locale
        if event is None:
            return django_settings.LANGUAGE_CODE
        content_locales = list(getattr(event, "content_locales", None) or [])
        if content_locales:
            return content_locales[0] if len(content_locales) == 1 else event.locale
        return event.locale

    def _build_localized_initial(self, initial, instance):
        initial = dict(initial or {})
        initial.setdefault("content_locale", self.selected_content_locale)
        if instance is None or not instance.pk:
            return initial
        for field_name in self.SINGLE_LOCALE_FIELDS:
            initial.setdefault(
                field_name,
                localized_value_for(getattr(instance, field_name, None), self.selected_content_locale),
            )
        return initial

    def _set_content_locale_choices(self):
        if "content_locale" not in self.fields:
            return
        content_locales = list(getattr(self.event, "content_locales", None) or []) if self.event else []
        if len(content_locales) <= 1:
            self.fields.pop("content_locale")
            return
        choices = list(self.event.named_content_locales)
        if self.selected_content_locale not in {code for code, _label in choices}:
            if self.instance.pk:
                choices.append((self.selected_content_locale, self.selected_content_locale))
            else:
                self.selected_content_locale = content_locales[0]
                self.initial["content_locale"] = self.selected_content_locale
        self.fields["content_locale"].choices = choices
        self.fields["content_locale"].widget.attrs["data-rtl-locales"] = ",".join(
            code for code, _label in choices if is_rtl(code)
        )

    def _content_text_field_names(self):
        names = [name for name in self.SINGLE_LOCALE_FIELDS if name in self.fields]
        if "notes" in self.fields:
            names.append("notes")
        for name, field in self.fields.items():
            if getattr(field, "question", None) is None:
                continue
            if isinstance(field, forms.URLField):
                continue
            if isinstance(field.widget, forms.TextInput | forms.Textarea):
                names.append(name)
        return names

    def _apply_content_text_direction(self):
        direction = "rtl" if is_rtl(self.selected_content_locale) else "ltr"
        for field_name in self._content_text_field_names():
            widget = self.fields[field_name].widget
            widget.attrs["dir"] = direction
            widget.attrs["data-content-text"] = "1"

    def apply_proposal_field_settings(self):
        file_field_keys = set(self.file_url_fields)
        for key, form_fields in self.setting_field_map.items():
            is_active = self.active_proposal_fields.get(key, True)
            is_required = self.required_proposal_fields.get(key, False)
            if not is_active:
                for field_name in form_fields:
                    self.fields.pop(field_name, None)
                continue

            setting = self.proposal_field_settings.get(key, {})
            for index, field_name in enumerate(form_fields):
                field = self.fields.get(field_name)
                if field is None:
                    continue
                if index == 0:
                    if setting.get("custom_label"):
                        field.label = setting["custom_label"]
                    if setting.get("help_text"):
                        field.help_text = setting["help_text"]
                field._required = is_required
                if key in file_field_keys:
                    continue
                if isinstance(field, I18nFormField):
                    field.one_required = is_required
                else:
                    field.required = is_required

    def _ordered_proposal_entries(self):
        if not self.exhibition_settings:
            return []
        entries = []
        for key in PROPOSAL_DEFAULT_FIELD_KEYS:
            entries.append((self.proposal_field_settings[key]["position"], 0, key, self.setting_field_map.get(key, ())))
        for field_name, field in self.fields.items():
            question = getattr(field, "question", None)
            if question is not None:
                entries.append((question.position, 1, f"question_{question.pk}", (field_name,)))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return entries

    def apply_proposal_field_order(self):
        if not self.exhibition_settings:
            return
        ordered_field_names = [
            field_name
            for _position, _kind, _key, field_names in self._ordered_proposal_entries()
            for field_name in field_names
            if field_name in self.fields
        ]
        self.order_fields(ordered_field_names)

    @property
    def proposal_items(self):
        if not self.exhibition_settings:
            return [{"kind": "field", "key": field_name, "field": self[field_name]} for field_name in self.fields]
        formset_keys = set(PROPOSAL_FORMSET_FIELD_KEYS)
        composite_keys = {"logo", "header_image"}
        items = []
        for _position, _kind, key, field_names in self._ordered_proposal_entries():
            if key in formset_keys:
                if self.field_setting_is_active(key):
                    items.append({"kind": key, "key": key})
                continue
            visible_field_names = [name for name in field_names if name in self.fields]
            if not visible_field_names:
                continue
            if key in composite_keys:
                items.append({"kind": key, "key": key})
            else:
                items.append({"kind": "field", "key": key, "field": self[visible_field_names[0]]})
        return items

    def field_setting_is_active(self, key):
        return self.active_proposal_fields.get(key, True)

    def field_setting_is_required(self, key):
        return self.required_proposal_fields.get(key, False)

    def full_clean(self):
        if not self.draft_save:
            return super().full_clean()
        keep_required = set()
        for key in self.DRAFT_REQUIRED_KEYS:
            keep_required.update(self.setting_field_map.get(key, ()))
        original = {}
        for field_name, field in self.fields.items():
            if field_name in keep_required:
                continue
            original[field_name] = (field.required, getattr(field, "one_required", None))
            field.required = False
            if isinstance(field, I18nFormField):
                field.one_required = False
            if hasattr(field.widget, "is_required"):
                field.widget.is_required = False
        try:
            super().full_clean()
        finally:
            for field_name, field in self.fields.items():
                if field_name not in original:
                    continue
                required, one_required = original[field_name]
                field.required = required
                if one_required is not None:
                    field.one_required = one_required
                if hasattr(field.widget, "is_required"):
                    field.widget.is_required = required

    def clean(self):
        cleaned_data = super().clean()
        if self.instance.pk:
            cleaned_data["is_exhibitor"] = self.instance.is_exhibitor
            cleaned_data["is_sponsor"] = self.instance.is_sponsor
        else:
            cleaned_data["is_exhibitor"] = True
            cleaned_data["is_sponsor"] = False

        for image_field in ("logo", "header_image"):
            if image_field not in self.fields:
                continue
            submitted_image = self.fields[image_field].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix(image_field),
            )
            self.validate_required_file(image_field, isinstance(submitted_image, UploadedFile))

        return cleaned_data

    def validate_required_file(self, field_name, has_new_upload):
        """Flag a required file field when nothing is uploaded and nothing is stored."""
        if self.draft_save:
            return
        if not self.field_setting_is_active(field_name) or not self.field_setting_is_required(field_name):
            return
        if field_name not in self.fields:
            return
        has_existing = bool(getattr(self.instance, f"visible_{field_name}_url", ""))
        if not has_new_upload and not has_existing:
            self.add_error(field_name, _("This field is required."))

    def save(self, commit=True):
        instance = super().save(commit=False)
        locale = self.cleaned_data.get("content_locale") or self.selected_content_locale
        instance.content_locale = locale
        for field_name in self.SINGLE_LOCALE_FIELDS:
            if field_name not in self.fields:
                setattr(instance, field_name, self._stored_localized_values.get(field_name) or "")
                continue
            setattr(
                instance,
                field_name,
                merge_localized_value(
                    self._stored_localized_values.get(field_name),
                    locale,
                    self.cleaned_data.get(field_name),
                ),
            )
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
        label=_("Sponsor group"),
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


class ExhibitionDefaultFieldForm(forms.Form):
    label = forms.CharField(
        required=False,
        max_length=200,
        label=_("Field label"),
    )
    help_text = forms.CharField(
        required=False,
        max_length=500,
        label=_("Help text"),
        help_text=_("Shown below the field on the exhibitor form."),
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        self.field_setting = kwargs.pop("field_setting")
        super().__init__(*args, **kwargs)
        self.fields["label"].help_text = _("Leave empty to use the default: %(label)s") % {
            "label": self.field_setting["default_label"]
        }
        self.fields["label"].widget.attrs.setdefault("placeholder", self.field_setting["default_label"])
        default_help_text = self.field_setting.get("default_help_text")
        if default_help_text:
            self.fields["help_text"].help_text = _("Leave empty to use the default: %(help_text)s") % {
                "help_text": default_help_text
            }
            self.fields["help_text"].widget.attrs.setdefault("placeholder", default_help_text)


class ExhibitionQuestionOptionForm(I18nModelForm):
    def has_changed(self):
        """Ignore the automatically submitted ordering value on blank extra rows."""
        for name, field in self.fields.items():
            if name in {"ORDER", "id"}:
                continue

            prefixed_name = self.add_prefix(name)
            data_value = field.widget.value_from_datadict(self.data, self.files, prefixed_name)
            initial_value = self.initial.get(name, field.initial)
            if callable(initial_value):
                initial_value = initial_value()
            if field.has_changed(initial_value, data_value):
                return True
        return False

    class Meta:
        model = ExhibitionQuestionOption
        localized_fields = "__all__"
        fields = ["answer"]


class BaseExhibitionQuestionOptionFormSet(I18nFormSet):
    def __init__(self, *args, requires_option=False, **kwargs):
        self.requires_option = requires_option
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors) or not self.requires_option:
            return

        if not any(form.cleaned_data.get("answer") and not form.cleaned_data.get("DELETE") for form in self.forms):
            raise ValidationError(_("Please provide at least one option for this question type."))


ExhibitionQuestionOptionFormSet = inlineformset_factory(
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    form=ExhibitionQuestionOptionForm,
    formset=BaseExhibitionQuestionOptionFormSet,
    can_order=True,
    can_delete=True,
    extra=0,
)


class ExhibitionQuestionForm(I18nModelForm):
    class Meta:
        model = ExhibitionQuestion
        localized_fields = "__all__"
        fields = [
            "variant",
            "question",
            "help_text",
            "required",
            "active",
        ]
        labels = {
            "variant": _("Field type"),
            "question": _("Custom question"),
            "help_text": _("Help text"),
            "required": _("Required"),
            "active": _("Active"),
        }

    choice_variants = QUESTION_OPTION_VARIANTS

    def __init__(self, *args, **kwargs):
        self.event = kwargs.get("event")
        super().__init__(*args, **kwargs)
        self.fields["variant"].widget.attrs["data-question-variant"] = "1"

    @property
    def choice_variant_values(self):
        return " ".join(sorted(str(variant) for variant in self.choice_variants))

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.event:
            instance.event = self.event
        if not instance.pk and self.event:
            max_position = ExhibitionQuestion.objects.filter(event=self.event).aggregate(Max("position"))[
                "position__max"
            ]
            instance.position = max((max_position or -1) + 1, len(PROPOSAL_DEFAULT_FIELD_KEYS))
        if commit:
            instance.save()
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


ExhibitorSocialLinkFormSet = inlineformset_factory(
    ExhibitorInfo,
    ExhibitorSocialLink,
    form=ExhibitorSocialLinkForm,
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


def social_link_prefixes() -> dict[str, str]:
    return {key: spec.prefix for key, spec in SOCIAL_LINK_SPECS.items()}


class _EmailBodyEditorWidget(I18nEmailEditorWidget):
    """Seed each locale editor with rendered HTML so stored plain text keeps its line breaks."""

    def decompress(self, value):
        return [compile_email_body(item) if item else item for item in super().decompress(value)]


class _EmailBodyEditorTextarea(EmailEditorWidget):
    """Non-i18n counterpart of :class:`_EmailBodyEditorWidget`."""

    def format_value(self, value):
        return compile_email_body(value) if value else value


class ExhibitionEmailBodyFormField(I18nEmailBodyFormField):
    """Email body field whose editor is seeded with rendered HTML."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", _EmailBodyEditorWidget)
        super().__init__(*args, **kwargs)


def _is_html_empty(html: str) -> bool:
    """Check whether an HTML snippet contains no substantive text or media."""
    if not html:
        return True
    text = unescape(strip_tags(html)).replace("\xa0", " ").strip()
    if text:
        return False
    if "<img" in html.lower():
        return False
    return True


class ExhibitionEmailQueueForm(forms.ModelForm):
    """Edit a queued email's recipient / subject / body / schedule before sending."""

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event", None)
        super().__init__(*args, **kwargs)
        if self.event:
            self.fields["scheduled_at"].widget.attrs["data-event-timezone"] = self.event.timezone

    class Meta:
        model = ExhibitionEmailQueue
        fields = ("to_email", "subject", "body", "scheduled_at")
        widgets = {
            "body": _EmailBodyEditorTextarea(attrs={"rows": 12}),
            "scheduled_at": HtmlDateTimeInput,
        }
        help_texts = {
            "scheduled_at": _(
                "Leave empty to keep this in the outbox until sent manually. Time is interpreted in the event timezone."
            ),
        }

    def clean_body(self):
        body = self.cleaned_data.get("body")
        if not body or _is_html_empty(body):
            raise forms.ValidationError(_("This field is required."))
        return body

    def clean_scheduled_at(self):
        scheduled_at = self.cleaned_data.get("scheduled_at")
        if scheduled_at and scheduled_at <= timezone.now():
            raise forms.ValidationError(_("The scheduled time must be in the future."))
        return scheduled_at


class ExhibitionComposeForm(forms.Form):
    """Compose a broadcast email to a filtered group of applicants."""

    PARTNER_TYPE_CHOICES = (
        ("", _("Exhibitors and sponsors")),
        ("exhibitor", _("Exhibitors only")),
        ("sponsor", _("Sponsors only")),
    )

    states = forms.MultipleChoiceField(
        label=_("Application state"),
        choices=[
            (state.value, state.label) for state in ExhibitionProposalState if state != ExhibitionProposalState.DRAFT
        ],
        initial=[ExhibitionProposalState.ACCEPTED],
        widget=forms.CheckboxSelectMultiple,
    )
    partner_type = forms.ChoiceField(
        label=_("Partner type"),
        choices=PARTNER_TYPE_CHOICES,
        required=False,
    )
    sponsor_group = forms.ModelChoiceField(
        label=_("Sponsor group"),
        queryset=SponsorGroup.objects.none(),
        required=False,
        empty_label=_("Any sponsor group"),
    )
    subject = I18nFormField(label=_("Subject"), widget=I18nTextInput, max_length=255)
    scheduled_at = forms.DateTimeField(
        label=_("Send at"),
        required=False,
        widget=HtmlDateTimeInput,
        help_text=_("Leave empty to send immediately or save to the outbox."),
    )

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop("event")
        super().__init__(*args, **kwargs)
        self.fields["sponsor_group"].queryset = SponsorGroup.objects.filter(event=self.event).order_by("level", "pk")
        self.fields["body"] = ExhibitionEmailBodyFormField(
            label=_("Body"),
            placeholders=mail_helpers.placeholder_names(self.event, mail_helpers.PROPOSAL_PLACEHOLDER_CONTEXT),
        )
        self.order_fields(["states", "partner_type", "sponsor_group", "subject", "body", "scheduled_at"])
        locales = self.event.settings.get("locales")
        self.fields["subject"].widget.enabled_locales = locales
        self.fields["body"].widget.enabled_locales = locales
        self.fields["scheduled_at"].help_text = f"{self.fields['scheduled_at'].help_text} {get_tz_help(self.event)}"
        self.fields["scheduled_at"].widget.attrs.update(
            {
                "data-schedule-datetime": "1",
                "data-event-timezone": self.event.timezone,
            }
        )

    def clean_body(self):
        body = self.cleaned_data.get("body")
        if not body:
            raise forms.ValidationError(_("This field is required."))
        if isinstance(body, LazyI18nString):
            data = body.data
            if isinstance(data, dict):
                has_content = any(not _is_html_empty(v) for v in data.values() if v)
                if not has_content:
                    raise forms.ValidationError(_("This field is required."))
            elif isinstance(data, str) and _is_html_empty(data):
                raise forms.ValidationError(_("This field is required."))
        elif isinstance(body, str) and _is_html_empty(body):
            raise forms.ValidationError(_("This field is required."))
        return body

    def clean_scheduled_at(self):
        scheduled_at = self.cleaned_data.get("scheduled_at")
        if scheduled_at and scheduled_at <= timezone.now():
            raise forms.ValidationError(_("The scheduled time must be in the future."))
        return scheduled_at


class ExhibitionMailTemplatesForm(SettingsForm):
    """Editable lifecycle email templates, stored in ``event.settings``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for role in mail_helpers.LIFECYCLE_ROLES:
            default_subject, default_body = mail_helpers.default_template_initial(role, self.locales)
            # The panel heading already names the template, so the fields are not prefixed with it.
            self.fields[mail_helpers.subject_settings_key(role)] = I18nFormField(
                label=_("Subject"),
                required=False,
                widget=I18nTextInput,
                initial=default_subject,
                locales=self.locales,
            )
            self.fields[mail_helpers.body_settings_key(role)] = ExhibitionEmailBodyFormField(
                label=_("Body"),
                required=False,
                placeholders=mail_helpers.role_placeholder_names(self.obj, role),
                initial=default_body,
            )
            self.fields[mail_helpers.body_settings_key(role)].widget.enabled_locales = self.locales


class ExhibitionCustomEmailTemplateForm(I18nModelForm):
    """Organizer-defined email template, independent of the fixed lifecycle templates."""

    class Meta:
        model = ExhibitionCustomEmailTemplate
        localized_fields = "__all__"
        fields = ["name", "subject", "body"]
        widgets = {
            "subject": I18nTextInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholder_names = mail_helpers.placeholder_names(self.event, mail_helpers.PROPOSAL_PLACEHOLDER_CONTEXT)
        self.fields["body"] = ExhibitionEmailBodyFormField(
            label=self.fields["body"].label,
            required=False,
            placeholders=placeholder_names,
        )
        if self.event:
            self.fields["body"].widget.enabled_locales = self.event.settings.get("locales")
