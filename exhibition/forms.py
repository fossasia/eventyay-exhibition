import json
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
from django.utils.functional import cached_property
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django_countries.fields import CountryField
from django_scopes import scope
from eventyay.base.forms import I18nFormSet, I18nModelForm, SettingsForm
from eventyay.base.forms.questions import WrappedPhoneNumberPrefixWidget
from eventyay.base.forms.widgets import (
    DatePickerWidget,
    SplitDateTimePickerWidget,
    TimePickerWidget,
)
from eventyay.base.models import Submission, SubmissionStates
from eventyay.base.templatetags.rich_text import compile_email_body
from eventyay.common.forms.fields import I18nEmailBodyFormField
from eventyay.common.forms.mixins import (
    EventLocalizedModelChoiceField,
    EventLocalizedModelMultipleChoiceField,
)
from eventyay.common.forms.widgets import EmailEditorWidget, HtmlDateTimeInput, I18nEmailEditorWidget
from eventyay.common.urls import normalize_url_scheme
from eventyay.common.utils.language import localize_event_text
from eventyay.consts import SizeKey
from eventyay.control.forms import ExtFileField, SplitDateTimeField
from eventyay.helpers.countries import CachedCountries
from eventyay.helpers.i18n import get_format_without_seconds, is_rtl
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString
from phonenumber_field.formfields import PhoneNumberField

from . import mail as mail_helpers
from .models import (
    DEPENDENCY_PARENT_VARIANTS,
    QUESTION_OPTION_VARIANTS,
    REQUEST_DEFAULT_FIELD_KEYS,
    REQUEST_FORMSET_FIELD_KEYS,
    ExhibitionAnswer,
    ExhibitionCustomEmailTemplate,
    ExhibitionEmailQueue,
    ExhibitionProductPurpose,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitionRequestExtraLink,
    ExhibitionRequestSocialLink,
    ExhibitionRequestState,
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
from .utils import localized_value_for, merge_localized_value, pool_tag_choices


def get_tz_help(event):
    return _("Times are in the event timezone: %(tz)s.") % {"tz": event.timezone}


def delete_exhibition_answer(answer):
    """Delete an answer and the file it holds; Django leaves the file behind on its own."""
    if answer.file:
        answer.file.delete(save=False)
    answer.delete()


class ExhibitionQuestionDependencyMixin:
    """Conditional visibility for custom fields that depend on another field's answer.

    The rendered widgets carry the same ``data-question-dependency`` attributes the
    presale bundle already understands, so the show/hide behaviour is handled by
    eventyay's own ``questions.js``. This mixin owns the server side of it: a hidden
    field never blocks the form and its answer is never stored.
    """

    def apply_question_dependency(self, field, question):
        """Tag the widget so the presale bundle can show and hide it as the parent changes."""
        if not question.dependency_question_id:
            return
        field.widget.attrs["data-question-dependency"] = question.dependency_question_id
        field.widget.attrs["data-question-dependency-values"] = json.dumps(question.dependency_values)

    def question_fields(self):
        for name, field in self.fields.items():
            if name.startswith("question_") and getattr(field, "question", None) is not None:
                yield name, field

    def question_is_visible(self, question, cleaned_data, seen=None):
        """True when every dependency up the chain is satisfied by the submitted answers."""
        if not question.dependency_question_id:
            return True
        seen = seen or set()
        if question.pk in seen:
            return False
        seen.add(question.pk)

        parent_name = f"question_{question.dependency_question_id}"
        parent_field = self.fields.get(parent_name)
        if parent_field is None or getattr(parent_field, "question", None) is None:
            # The parent was deleted or deactivated, so the condition can never be met.
            return False
        if not self.question_is_visible(parent_field.question, cleaned_data, seen):
            return False
        return self.dependency_matches(cleaned_data.get(parent_name), question.dependency_values)

    @staticmethod
    def dependency_matches(parent_value, dependency_values):
        if parent_value is None or parent_value == "":
            return False
        if isinstance(parent_value, bool):
            return ("True" in dependency_values) if parent_value else ("False" in dependency_values)
        if hasattr(parent_value, "pk"):
            return str(parent_value.pk) in dependency_values
        if not isinstance(parent_value, str) and hasattr(parent_value, "__iter__"):
            return any(str(getattr(item, "pk", item)) in dependency_values for item in parent_value)
        return str(parent_value) in dependency_values

    def dependency_is_resolvable(self, question, seen=None):
        """True when every field up the dependency chain is on this form to answer."""
        if not question.dependency_question_id:
            return True
        seen = seen or set()
        if question.pk in seen:
            return False
        seen.add(question.pk)
        parent_field = self.fields.get(f"question_{question.dependency_question_id}")
        parent = getattr(parent_field, "question", None)
        if parent is None:
            return False
        return self.dependency_is_resolvable(parent, seen)

    @property
    def hidden_question_fields(self):
        return getattr(self, "_hidden_question_fields", set())

    @property
    def stale_question_fields(self):
        """Hidden fields whose condition the visitor could act on, so their answers no longer apply."""
        return getattr(self, "_stale_question_fields", set())

    def clean(self):
        """Drop whatever a hidden field contributed: its value and any error it raised."""
        cleaned_data = super().clean()
        hidden = set()
        stale = set()
        for name, field in self.question_fields():
            if self.question_is_visible(field.question, cleaned_data):
                continue
            hidden.add(name)
            if self.dependency_is_resolvable(field.question):
                stale.add(name)
            self.errors.pop(name, None)
            cleaned_data[name] = None
        self._hidden_question_fields = hidden
        self._stale_question_fields = stale
        return cleaned_data


class ExhibitionQuestionFieldsMixin(ExhibitionQuestionDependencyMixin):
    def inject_exhibition_questions(self, *, event, exhibition_request=None, readonly=False):
        answers_by_question = {}
        if exhibition_request and exhibition_request.pk:
            for answer in exhibition_request.answers.prefetch_related("options"):
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
            self.apply_question_dependency(field, question)
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

    def save_exhibition_questions(self, exhibition_request):
        for key, value in self.cleaned_data.items():
            if not key.startswith("question_"):
                continue
            if key in self.hidden_question_fields:
                # The field was not shown. Drop an answer the visitor themselves hid by
                # changing the parent, but keep one whose parent has since been
                # deactivated or deleted: they never got the chance to retract it.
                answer = self.fields[key].answer
                if answer and key in self.stale_question_fields:
                    delete_exhibition_answer(answer)
                continue
            field = self.fields[key]
            question = field.question
            answer = field.answer
            empty = value in ("", None, False) or (
                hasattr(value, "__len__") and not isinstance(value, str) and len(value) == 0
            )

            if empty:
                if answer:
                    delete_exhibition_answer(answer)
                continue

            if not answer:
                answer = ExhibitionAnswer(exhibition_request=exhibition_request, question=question)

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


class SessionSelectWidget(forms.CheckboxSelectMultiple):
    template_name = "exhibitors/session_select.html"
    option_template_name = "exhibitors/session_select_option.html"


class SessionChoiceField(forms.ModelMultipleChoiceField):
    widget = SessionSelectWidget

    def label_from_instance(self, obj: Submission) -> str:
        speakers = obj.display_speaker_names
        if speakers:
            return f"{obj.title} — {speakers}"
        return str(obj.title)


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
    sessions = SessionChoiceField(
        queryset=Submission.objects.none(),
        required=False,
        label=_("Related sessions"),
        help_text=_(
            "Sessions to show on this organization's public page. "
            "Only sessions on the published schedule are shown there."
        ),
    )

    file_fields = ("slides", "logo", "banner")
    file_url_fields = {"slides": "slides_url"}

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
            "logo",
            "banner",
            "is_exhibitor",
            "is_sponsor",
            "sponsor_group",
            "booth_id",
            "booth_name",
            "lead_scanning_enabled",
            "allow_voucher_access",
            "allow_lead_access",
            "lead_scanning_scope_by_device",
            "sessions",
            "published",
        ]
        labels = {
            "name": _("Organization name"),
            "description": _("Organization description"),
            "email": _("E-mail"),
            "contact_url": _("Contact page URL"),
            "video_url": _("Promotional video URL"),
            "slides": _("Promotional slides"),
            "logo": _("Logo"),
            "banner": _("Exhibition banner"),
            "url": _("Organization website"),
            "is_exhibitor": _("Mark this organization as an exhibitor"),
            "is_sponsor": _("Mark this organization as an event sponsor"),
            "booth_name": _("Preferred booth name"),
            "lead_scanning_enabled": _("Can scan attendee badges"),
            "published": _("Show on the public event website"),
        }
        help_texts = {
            "lead_scanning_enabled": _(
                "Lets this exhibitor sign in to the lead scanning app and scan attendees at their booth. "
                "Turn this off to block scanning entirely."
            ),
            "header_image": _(
                "Shown as the banner on the public exhibitor page. "
                "Use a wide 3:1 image, for example 1500 x 500 pixels. Other shapes are "
                "shown complete on a plain background there, but list cards crop to fill."
            ),
        }

    PROFILE_SETTING_FIELD_MAP = {
        "name": ("name",),
        "description": ("description",),
        "email": ("email",),
        "url": ("url",),
        "contact_url": ("contact_url",),
        "video_url": ("video_url",),
        "slides": ("slides",),
        "logo": ("logo",),
        "banner": ("banner",),
        "booth_name": ("booth_name",),
    }
    PROFILE_FORMSET_KEYS = ("social_links", "extra_links")
    PROFILE_COMPOSITE_KEYS = ("slides", "logo", "banner")

    SPONSOR_ONLY_FIELDS = ("sponsor_group",)
    EXHIBITOR_ONLY_FIELDS = (
        "booth_id",
        "booth_name",
        "lead_scanning_enabled",
        "allow_lead_access",
        "lead_scanning_scope_by_device",
    )

    def __init__(self, *args, **kwargs):
        self.organization_type = kwargs.pop("organization_type", None)
        event = kwargs.get("event")
        instance = kwargs.get("instance")
        self.event = event or getattr(instance, "event", None)
        with scope(event=self.event):
            super().__init__(*args, **kwargs)
        if self.organization_type == "sponsor":
            self._drop_fields(self.EXHIBITOR_ONLY_FIELDS + ("is_sponsor",))
        elif self.organization_type == "exhibitor":
            self._drop_fields(self.SPONSOR_ONLY_FIELDS + ("is_exhibitor",))
        if "sponsor_group" in self.fields:
            self.fields["sponsor_group"].queryset = SponsorGroup.objects.filter(event=self.event).order_by("pk")
            self.fields["sponsor_group"].empty_label = _("No sponsor group")
        if self.event:
            with scope(event=self.event):
                self.fields["sessions"].queryset = (
                    Submission.objects.filter(
                        event=self.event,
                        state__in=SubmissionStates.accepted_states,
                    )
                    .prefetch_related("speakers")
                    .order_by("title")
                )
        else:
            self._drop_fields(("sessions",))
        for field_name in ("logo", "banner"):
            self.fields[field_name].widget.attrs.setdefault("accept", "image/*")
        self.fields["slides"].widget.attrs.setdefault("accept", ".pdf,application/pdf")
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
            self.profile_field_settings = settings.normalized_request_field_settings
            self._apply_profile_field_settings()
            self.ordered_profile_keys = [
                key for key in settings.ordered_request_field_keys if self.profile_key_is_active(key)
            ]
            self._apply_profile_field_order()
        self._set_voucher_access_help_text()
        self.linked_request = self._resolve_linked_request()
        if self.event and self.linked_request:
            self.inject_exhibition_questions(event=self.event, exhibition_request=self.linked_request)

    def _resolve_linked_request(self):
        """The approved request this profile was created from, if any."""
        if not (self.instance and self.instance.pk):
            return None
        return self.instance.source_requests.order_by("pk").first()

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
        if self.organization_type in ("exhibitor", "sponsor"):
            return self.organization_type
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
                    if setting.get("custom_help_text"):
                        field.help_text = setting["custom_help_text"]
                field._required = is_required
                if key in self.PROFILE_COMPOSITE_KEYS or key == "booth_name":
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

        video_url = cleaned_data.get("video_url") or ""
        if video_url:
            cleaned_data["video_url"] = normalize_url_scheme(video_url)

        submitted_slides = None
        if "slides" in self.fields:
            submitted_slides = self.fields["slides"].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix("slides"),
            )
        has_new_slides_upload = isinstance(submitted_slides, UploadedFile)
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

        self._validate_required_file("slides", has_new_slides_upload)

        for image_field in self.file_fields:
            if image_field == "slides" or image_field not in self.fields:
                continue
            submitted_image = self.fields[image_field].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix(image_field),
            )
            self._validate_required_file(image_field, isinstance(submitted_image, UploadedFile))

        if self.organization_type == "sponsor":
            is_sponsor = True
            is_exhibitor = bool(cleaned_data.get("is_exhibitor"))
        elif self.organization_type == "exhibitor":
            is_sponsor = bool(cleaned_data.get("is_sponsor"))
            is_exhibitor = True
        else:
            is_sponsor = bool(cleaned_data.get("is_sponsor"))
            is_exhibitor = bool(cleaned_data.get("is_exhibitor"))
            if not is_sponsor and not is_exhibitor:
                self.add_error(None, _("An organization must be marked as an exhibitor, a sponsor, or both."))
        self._resolved_is_sponsor = is_sponsor
        cleaned_data["is_exhibitor"] = is_exhibitor

        if not is_sponsor:
            cleaned_data["sponsor_group"] = None

        if is_exhibitor:
            if (
                self.profile_key_is_required("booth_name")
                and "booth_name" in self.fields
                and not cleaned_data.get("booth_name")
            ):
                self.add_error("booth_name", _("This field is required."))
        else:
            cleaned_data["booth_name"] = ""
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

        for image_field in self.file_fields:
            url_field = self.file_url_fields.get(image_field)
            previous_file = getattr(old_instance, image_field, None) if old_instance else None
            uploaded_file = self.files.get(self.add_prefix(image_field))
            clear_selected = bool(self.data.get(self.add_prefix(f"{image_field}-clear")))

            if uploaded_file:
                if previous_file and previous_file.name:
                    files_to_delete.add(previous_file.name)
                if url_field:
                    setattr(instance, url_field, "")
                continue

            if clear_selected:
                if previous_file and previous_file.name:
                    files_to_delete.add(previous_file.name)
                setattr(instance, image_field, None)
                if url_field:
                    setattr(instance, url_field, "")

        if commit:
            instance.save()
            with scope(event=instance.event):
                self.save_m2m()
            if self.linked_request:
                self.save_exhibition_questions(self.linked_request)
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
    """How many more pool vouchers to hand this organization now."""

    count = forms.IntegerField(
        min_value=0,
        max_value=1000,
        initial=1,
        label=_("Vouchers to take from the pool"),
        help_text=_("Set to 0 to email the codes this organization already has without taking any more."),
    )


class VoucherDefaultsFormMixin:
    """Shared wiring for the forms that set how many pool vouchers an organization receives."""

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


class ExhibitorDeviceDefaultsForm(forms.ModelForm):
    """How many lead-scanning devices a profile gets automatically when scanning is enabled."""

    class Meta:
        model = ExhibitorSettings
        fields = ["device_default_count"]


class ExhibitorVoucherDefaultsForm(VoucherDefaultsFormMixin, forms.ModelForm):
    """Which pools organizations draw from, and how many codes each one gets by default."""

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


class ExhibitionQuestionFieldsMixin(ExhibitionQuestionDependencyMixin):
    def inject_exhibition_questions(self, *, event, exhibition_request=None, readonly=False):
        answers_by_question = {}
        if exhibition_request and exhibition_request.pk:
            for answer in exhibition_request.answers.prefetch_related("options"):
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
            self.apply_question_dependency(field, question)
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
                widget=WrappedPhoneNumberPrefixWidget(),
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

    def save_exhibition_questions(self, exhibition_request):
        for key, value in self.cleaned_data.items():
            if not key.startswith("question_"):
                continue
            if key in self.hidden_question_fields:
                # The field was not shown. Drop an answer the visitor themselves hid by
                # changing the parent, but keep one whose parent has since been
                # deactivated or deleted: they never got the chance to retract it.
                answer = self.fields[key].answer
                if answer and key in self.stale_question_fields:
                    delete_exhibition_answer(answer)
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
                    delete_exhibition_answer(answer)
                continue

            if not answer:
                answer = ExhibitionAnswer(exhibition_request=exhibition_request, question=question)

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


class ExhibitionRequestForm(ExhibitionQuestionFieldsMixin, I18nModelForm):
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

    file_fields = ("slides", "logo", "banner")
    file_url_fields = {"slides": "slides_url"}
    setting_field_map = {
        "name": ("name",),
        "description": ("description",),
        "email": ("email",),
        "url": ("url",),
        "contact_url": ("contact_url",),
        "video_url": ("video_url",),
        "slides": ("slides",),
        "logo": ("logo",),
        "banner": ("banner",),
        "booth_name": ("booth_name",),
        "notes": ("notes",),
    }
    DRAFT_REQUIRED_KEYS = ("name",)

    class Meta:
        model = ExhibitionRequest
        localized_fields = "__all__"
        fields = [
            "name",
            "description",
            "email",
            "contact_url",
            "video_url",
            "slides",
            "logo",
            "banner",
            "url",
            "booth_name",
            "notes",
        ]
        labels = {
            "name": _("Organization name"),
            "description": _("Organization description"),
            "email": _("E-mail"),
            "contact_url": _("Contact page URL"),
            "video_url": _("Promotional video URL"),
            "slides": _("Promotional slides"),
            "logo": _("Logo"),
            "banner": _("Exhibition banner"),
            "url": _("Organization website"),
            "booth_name": _("Preferred booth name"),
            "notes": _("Message to the organizers"),
        }
        help_texts = {
            "header_image": _(
                "Shown as the banner on the public exhibitor page. "
                "Use a wide 3:1 image, for example 1500 x 500 pixels. Other shapes are "
                "shown complete on a plain background there, but list cards crop to fill."
            ),
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
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
        self.request_field_settings = {}
        self.active_request_fields = {}
        self.required_request_fields = {}
        if self.event:
            self.exhibition_settings = ExhibitorSettings.objects.get_or_create(event=self.event)[0]
            self.request_field_settings = self.exhibition_settings.normalized_request_field_settings
            self.active_request_fields = {key: value["active"] for key, value in self.request_field_settings.items()}
            self.required_request_fields = {
                key: value["required"] for key, value in self.request_field_settings.items()
            }
        for field_name in ("logo", "banner"):
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
            self.apply_request_field_settings()
            self.inject_exhibition_questions(
                event=self.event,
                exhibition_request=instance,
                readonly=self.read_only,
            )
            self.apply_request_field_order()
        self._apply_content_text_direction()
        if self.read_only:
            for field in self.fields.values():
                field.disabled = True
        elif instance and instance.pk and instance.state == ExhibitionRequestState.ACCEPTED:
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

    def apply_request_field_settings(self):
        file_field_keys = set(self.file_fields)
        for key, form_fields in self.setting_field_map.items():
            is_active = self.active_request_fields.get(key, True)
            is_required = self.required_request_fields.get(key, False)
            if not is_active:
                for field_name in form_fields:
                    self.fields.pop(field_name, None)
                continue

            setting = self.request_field_settings.get(key, {})
            for index, field_name in enumerate(form_fields):
                field = self.fields.get(field_name)
                if field is None:
                    continue
                if index == 0:
                    if setting.get("custom_label"):
                        field.label = setting["custom_label"]
                    if setting.get("custom_help_text"):
                        field.help_text = setting["custom_help_text"]
                field._required = is_required
                if key in file_field_keys or key == "booth_name":
                    continue
                if isinstance(field, I18nFormField):
                    field.one_required = is_required
                else:
                    field.required = is_required

    def _ordered_request_entries(self):
        if not self.exhibition_settings:
            return []
        entries = []
        for key in REQUEST_DEFAULT_FIELD_KEYS:
            entries.append((self.request_field_settings[key]["position"], 0, key, self.setting_field_map.get(key, ())))
        for field_name, field in self.fields.items():
            question = getattr(field, "question", None)
            if question is not None:
                entries.append((question.position, 1, f"question_{question.pk}", (field_name,)))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return entries

    def apply_request_field_order(self):
        if not self.exhibition_settings:
            return
        ordered_field_names = [
            field_name
            for _position, _kind, _key, field_names in self._ordered_request_entries()
            for field_name in field_names
            if field_name in self.fields
        ]
        self.order_fields(ordered_field_names)

    @property
    def request_items(self):
        if not self.exhibition_settings:
            return [{"kind": "field", "key": field_name, "field": self[field_name]} for field_name in self.fields]
        formset_keys = set(REQUEST_FORMSET_FIELD_KEYS)
        composite_keys = {"slides", "logo", "banner"}
        items = []
        for _position, _kind, key, field_names in self._ordered_request_entries():
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
        return self.active_request_fields.get(key, True)

    def field_setting_is_required(self, key):
        return self.required_request_fields.get(key, False)

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

        if "video_url" in self.fields and (video_url := cleaned_data.get("video_url")):
            cleaned_data["video_url"] = normalize_url_scheme(video_url)

        submitted_slides = None
        if "slides" in self.fields:
            submitted_slides = self.fields["slides"].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix("slides"),
            )
        has_new_slides_upload = isinstance(submitted_slides, UploadedFile)
        self.validate_required_file("slides", has_new_slides_upload)
        for image_field in ("logo", "banner"):
            if image_field not in self.fields:
                continue
            submitted_image = self.fields[image_field].widget.value_from_datadict(
                self.data,
                self.files,
                self.add_prefix(image_field),
            )
            self.validate_required_file(image_field, isinstance(submitted_image, UploadedFile))

        if not cleaned_data["is_exhibitor"]:
            cleaned_data["booth_name"] = ""
        elif (
            not self.draft_save
            and self.field_setting_is_required("booth_name")
            and "booth_name" in self.fields
            and not cleaned_data.get("booth_name")
        ):
            self.add_error("booth_name", _("This field is required."))

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


class ExhibitionRequestReviewForm(I18nModelForm):
    sponsor_group = forms.ModelChoiceField(
        queryset=SponsorGroup.objects.none(),
        required=False,
        label=_("Sponsor group"),
    )

    class Meta:
        model = ExhibitionRequest
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


class ExhibitionRequestReviewNotesForm(I18nModelForm):
    class Meta:
        model = ExhibitionRequest
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
    """Mirrors the Tickets custom field form: same labels, same order, same dependency options."""

    class Meta:
        model = ExhibitionQuestion
        localized_fields = "__all__"
        fields = [
            "question",
            "variant",
            "required",
            "help_text",
            "dependency_question",
            "dependency_values",
            "active",
        ]
        labels = {
            "question": _("Custom field"),
            "variant": _("Type"),
            "required": _("Required field"),
            "help_text": _("Help text"),
            "dependency_question": _("Custom field dependency"),
            "active": _("Active"),
        }
        widgets = {
            "dependency_values": forms.SelectMultiple,
        }

    choice_variants = QUESTION_OPTION_VARIANTS

    def __init__(self, *args, **kwargs):
        self.event = kwargs.get("event")
        super().__init__(*args, **kwargs)
        self.fields["variant"].widget.attrs["data-question-variant"] = "1"
        self.fields["help_text"].widget.attrs["rows"] = 3
        self.fields["dependency_question"].queryset = self.dependency_candidates
        self.fields["dependency_question"].required = False
        self.fields["dependency_values"].required = False

    @property
    def choice_variant_values(self):
        return " ".join(sorted(str(variant) for variant in self.choice_variants))

    @cached_property
    def dependency_candidates(self):
        """Fields of this event that can be depended on: everything with a fixed answer set, minus itself."""
        event = self.event or getattr(self.instance, "event", None)
        if event is None:
            return ExhibitionQuestion.objects.none()
        queryset = ExhibitionQuestion.objects.filter(
            event=event,
            variant__in=DEPENDENCY_PARENT_VARIANTS,
        ).prefetch_related("options")
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        return queryset

    @property
    def dependency_value_map(self):
        """{question id: [{value, label}]} so the form can fill the value picker without a round trip."""
        return {
            str(question.pk): [
                {"value": value, "label": str(label)} for value, label in question.dependency_value_choices()
            ]
            for question in self.dependency_candidates
        }

    def clean_dependency_values(self):
        # The field is a MultiStringField rendered as a multi-select, so read every selected value.
        data = self.data
        if hasattr(data, "getlist"):
            return [value for value in data.getlist("dependency_values") if value]
        value = data.get("dependency_values") or []
        if isinstance(value, str):
            value = [value]
        return [item for item in value if item]

    def clean_dependency_question(self):
        dependency = value = self.cleaned_data.get("dependency_question")
        if not dependency:
            return None
        if dependency.variant not in DEPENDENCY_PARENT_VARIANTS:
            raise ValidationError(_("Only checkbox and choice fields can be used as a dependency."))
        seen = {self.instance.pk} if self.instance.pk else set()
        while dependency is not None:
            if dependency.pk in seen:
                raise ValidationError(_("Circular dependency between custom fields detected."))
            seen.add(dependency.pk)
            dependency = dependency.dependency_question
        return value

    def clean(self):
        cleaned_data = super().clean()
        dependency = cleaned_data.get("dependency_question")
        values = cleaned_data.get("dependency_values") or []
        if not dependency:
            cleaned_data["dependency_values"] = []
            return cleaned_data
        if not values:
            raise ValidationError({"dependency_values": [_("Please select at least one value.")]})
        allowed = {value for value, label in dependency.dependency_value_choices()}
        if not set(values) <= allowed:
            raise ValidationError({"dependency_values": [_("Select a valid value for the selected field.")]})
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.event:
            instance.event = self.event
        if not instance.pk and self.event:
            max_position = ExhibitionQuestion.objects.filter(event=self.event).aggregate(Max("position"))[
                "position__max"
            ]
            instance.position = max((max_position or -1) + 1, len(REQUEST_DEFAULT_FIELD_KEYS))
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


class ExhibitionRequestSocialLinkForm(forms.ModelForm):
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
        model = ExhibitionRequestSocialLink
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


class ExhibitionRequestExtraLinkForm(forms.ModelForm):
    class Meta:
        model = ExhibitionRequestExtraLink
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

ExhibitionRequestSocialLinkFormSet = inlineformset_factory(
    ExhibitionRequest,
    ExhibitionRequestSocialLink,
    form=ExhibitionRequestSocialLinkForm,
    can_delete=True,
    extra=0,
)

ExhibitionRequestExtraLinkFormSet = inlineformset_factory(
    ExhibitionRequest,
    ExhibitionRequestExtraLink,
    form=ExhibitionRequestExtraLinkForm,
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

    ORGANIZATION_TYPE_CHOICES = (
        ("", _("Exhibitors and sponsors")),
        ("exhibitor", _("Exhibitors only")),
        ("sponsor", _("Sponsors only")),
    )

    states = forms.MultipleChoiceField(
        label=_("Application state"),
        choices=[
            (state.value, state.label) for state in ExhibitionRequestState if state != ExhibitionRequestState.DRAFT
        ],
        initial=[ExhibitionRequestState.ACCEPTED],
        widget=forms.CheckboxSelectMultiple,
    )
    organization_type = forms.ChoiceField(
        label=_("Organization type"),
        choices=ORGANIZATION_TYPE_CHOICES,
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
            placeholders=mail_helpers.placeholder_names(self.event, mail_helpers.REQUEST_PLACEHOLDER_CONTEXT),
        )
        self.order_fields(["states", "organization_type", "sponsor_group", "subject", "body", "scheduled_at"])
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
        placeholder_names = mail_helpers.placeholder_names(self.event, mail_helpers.REQUEST_PLACEHOLDER_CONTEXT)
        self.fields["body"] = ExhibitionEmailBodyFormField(
            label=self.fields["body"].label,
            required=False,
            placeholders=placeholder_names,
        )
        if self.event:
            self.fields["body"].widget.enabled_locales = self.event.settings.get("locales")


class ExhibitionProductForm(forms.Form):
    """The exhibition role of one Tickets product, as one row of the products table.

    The product is carried in the row itself rather than in the field names, so the page
    can post as many or as few rows as it likes and each one still says what it is about.
    """

    product = forms.IntegerField(widget=forms.HiddenInput)
    purpose = forms.ChoiceField(
        required=False,
        choices=[("", _("Not an exhibition product"))] + ExhibitionProductPurpose.choices,
        widget=forms.Select(attrs={"class": "form-control exhibition-purpose-input"}),
    )
    includes_booth = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "exhibition-booth-input"}),
    )

    def __init__(self, *args, products=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.products = products or {}
        self.product_object = self.products.get(self._submitted_product_pk())
        if self.product_object is not None:
            self.fields["purpose"].widget.attrs["aria-label"] = _("Exhibition purpose for %(product)s") % {
                "product": self.product_object
            }
            self.fields["includes_booth"].widget.attrs["aria-label"] = _(
                "Includes an exhibition booth for %(product)s"
            ) % {"product": self.product_object}

    def _submitted_product_pk(self):
        try:
            return int(self.data.get(self.add_prefix("product"), self.initial.get("product")))
        except (TypeError, ValueError):
            return None

    def clean_product(self):
        product = self.products.get(self.cleaned_data["product"])
        if product is None:
            raise ValidationError(_("This product does not belong to this event."))
        return product

    def clean(self):
        """An exhibition product is the booth, so an unticked box still means "with booth"."""
        cleaned_data = super().clean()
        if cleaned_data.get("purpose") == ExhibitionProductPurpose.EXHIBITION:
            cleaned_data["includes_booth"] = True
        return cleaned_data


class BaseExhibitionProductFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        seen = set()
        for form in self.forms:
            product = form.cleaned_data.get("product")
            if product is None:
                continue
            if product.pk in seen:
                raise ValidationError(_("The same product was submitted more than once."))
            seen.add(product.pk)


ExhibitionProductFormSet = forms.formset_factory(
    ExhibitionProductForm,
    formset=BaseExhibitionProductFormSet,
    extra=0,
)
