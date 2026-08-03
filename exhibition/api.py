from django.conf import settings
from django.db import transaction
from django.utils import timezone
from eventyay.api.serializers.i18n import I18nAwareModelSerializer
from eventyay.base.models import OrderPosition
from eventyay.common.urls import normalize_url_scheme
from i18nfield.strings import LazyI18nString
from rest_framework import serializers, status, views, viewsets
from rest_framework.response import Response

from .models import (
    ExhibitorExtraLink,
    ExhibitorInfo,
    ExhibitorSettings,
    ExhibitorSocialLink,
    ExhibitorTag,
    Lead,
    SponsorGroup,
    generate_booth_id,
    get_next_sponsor_group_level,
)
from .social_links import SOCIAL_LINK_SPECS

UNSET = object()


def _localize_i18n_value(value, locale):
    if isinstance(value, LazyI18nString):
        return value.localize(locale)
    return value


def _get_exhibitor_locale(exhibitor):
    event = getattr(exhibitor, "event", None)
    return getattr(event, "locale", None) or settings.LANGUAGE_CODE


class SponsorGroupNameField(serializers.CharField):
    def get_attribute(self, instance):
        return instance

    def to_representation(self, value):
        return value.sponsor_group.localized_name if value.sponsor_group else None


class SponsorGroupLevelField(serializers.IntegerField):
    def get_attribute(self, instance):
        return instance

    def to_representation(self, value):
        return value.sponsor_group.level if value.sponsor_group else None


class ExhibitorAuthView(views.APIView):
    """Authenticate an exhibitor key.

    This is the single entry point any client (this plugin's own frontend,
    the check-in app, or an external voucher-issuing service) uses to
    resolve an exhibitor key. Besides identifying the exhibitor, the
    response also exposes the three access-control flags stored on
    ``ExhibitorInfo`` so that whichever system is enforcing access to a
    given resource can make that decision:

    - ``lead_scanning_enabled``: this exhibitor's devices may scan badges
      and create new Lead records at all (enforced by ``LeadCreateView``).
    - ``allow_lead_access``: this exhibitor may read/export/annotate the
      leads that were already scanned for them (enforced in this plugin
      by ``LeadRetrieveView``, ``LeadUpdateView`` and ``TagListView``).
    - ``allow_voucher_access``: whether personal attendee data (name,
      email, company, address, question answers) is attached to a scan
      at all. When off, scanning still records a lead (so duplicate-scan
      detection keeps working), but only an empty note/tags shell is
      stored, with no personal data. Enforced by
      ``LeadCreateView.get_allowed_attendee_data``.
    """

    def post(self, request, *args, **kwargs):
        key = request.data.get("key")

        if not key:
            return Response({"detail": "Missing parameters"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            locale = _get_exhibitor_locale(exhibitor)
            return Response(
                {
                    "success": True,
                    "exhibitor_id": exhibitor.id,
                    "exhibitor_name": _localize_i18n_value(exhibitor.name, locale),
                    "booth_id": exhibitor.booth_id,
                    "booth_name": _localize_i18n_value(exhibitor.booth_name, locale),
                    "lead_scanning_enabled": exhibitor.lead_scanning_enabled,
                    "allow_lead_access": exhibitor.allow_lead_access,
                    "allow_voucher_access": exhibitor.allow_voucher_access,
                },
                status=status.HTTP_200_OK,
            )
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {"success": False, "error": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class ExhibitorInfoSerializer(I18nAwareModelSerializer):
    sponsor_group = serializers.PrimaryKeyRelatedField(read_only=True)
    sponsor_group_name = SponsorGroupNameField(required=False, allow_blank=True, allow_null=True)
    sponsor_group_level = SponsorGroupLevelField(required=False, allow_null=True, min_value=1)
    social_links = serializers.ListField(child=serializers.DictField(), required=False, write_only=True)
    extra_links = serializers.ListField(child=serializers.DictField(), required=False, write_only=True)

    class Meta:
        model = ExhibitorInfo
        fields = (
            "id",
            "name",
            "description",
            "url",
            "email",
            "contact_url",
            "video_url",
            "slides_url",
            "logo_url",
            "header_image_url",
            "key",
            "is_sponsor",
            "sponsor_group",
            "sponsor_group_name",
            "sponsor_group_level",
            "is_exhibitor",
            "booth_id",
            "booth_name",
            "lead_scanning_enabled",
            "allow_voucher_access",
            "allow_lead_access",
            "lead_scanning_scope_by_device",
            "social_links",
            "extra_links",
        )
        read_only_fields = ("id", "key", "sponsor_group")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["logo_url"] = instance.visible_logo_url
        data["header_image_url"] = instance.visible_header_image_url
        data["slides_url"] = instance.visible_slides_url
        data["social_links"] = [{"network": link.network, "url": link.url} for link in instance.social_links.all()]
        data["extra_links"] = [{"label": link.label, "url": link.url} for link in instance.extra_links.all()]
        return data

    def validate_social_links(self, value):
        normalized = []
        for item in value:
            network = str(item.get("network", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if not network or not url:
                raise serializers.ValidationError("Each social link requires network and url.")
            if network not in SOCIAL_LINK_SPECS:
                raise serializers.ValidationError(f"Unsupported social network: {network}.")
            normalized.append({"network": network, "url": normalize_url_scheme(url)})
        return normalized

    def validate_extra_links(self, value):
        normalized = []
        for item in value:
            label = str(item.get("label", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if not label or not url:
                raise serializers.ValidationError("Each extra link requires label and url.")
            normalized.append({"label": label, "url": normalize_url_scheme(url)})
        return normalized

    def validate(self, data):
        data = super().validate(data)

        for field in (
            "url",
            "contact_url",
            "video_url",
            "logo_url",
            "header_image_url",
            "slides_url",
        ):
            if data.get(field):
                data[field] = normalize_url_scheme(data[field])

        if data.get("slides_url") and not data["slides_url"].lower().split("?", 1)[0].endswith(".pdf"):
            raise serializers.ValidationError({"slides_url": "Slides URL must point to a PDF file."})

        return data

    def _resolve_sponsor_group(self, sponsor_group_name, sponsor_group_level=UNSET):
        sponsor_group_name = str(sponsor_group_name or "").strip()
        if not sponsor_group_name:
            return None

        event = self.context["event"]
        groups = list(SponsorGroup.objects.filter(event=event).order_by("level", "pk"))
        name_matches = [group for group in groups if group.localized_name == sponsor_group_name]

        if sponsor_group_level is not UNSET and sponsor_group_level is not None:
            exact_matches = [group for group in name_matches if group.level == sponsor_group_level]
            if len(exact_matches) == 1:
                return exact_matches[0]
            if len(exact_matches) > 1:
                raise serializers.ValidationError(
                    {"sponsor_group_name": ("Multiple sponsor groups match this name and level.")}
                )
            if name_matches:
                raise serializers.ValidationError(
                    {"sponsor_group_level": ("Level does not match existing sponsor group.")}
                )
        else:
            if len(name_matches) == 1:
                return name_matches[0]
            if len(name_matches) > 1:
                raise serializers.ValidationError(
                    {"sponsor_group_name": ("Multiple sponsor groups match this name. Provide sponsor_group_level.")}
                )

        if sponsor_group_level is UNSET or sponsor_group_level is None:
            sponsor_group_level = get_next_sponsor_group_level(event)

        return SponsorGroup.objects.create(
            event=event,
            name={event.locale or settings.LANGUAGE_CODE: sponsor_group_name},
            level=sponsor_group_level,
            show_on_front_page=True,
        )

    def _apply_business_rules(self, instance, sponsor_group_name=UNSET, sponsor_group_level=UNSET):
        if instance.is_sponsor:
            if sponsor_group_name is not UNSET:
                instance.sponsor_group = self._resolve_sponsor_group(
                    sponsor_group_name, sponsor_group_level=sponsor_group_level
                )
        else:
            instance.sponsor_group = None

        if not instance.is_exhibitor:
            instance.booth_name = ""
            instance.booth_id = None
            instance.lead_scanning_enabled = False
            instance.allow_voucher_access = False
            instance.allow_lead_access = False
            instance.lead_scanning_scope_by_device = False
        elif not instance.booth_id:
            instance.booth_id = generate_booth_id(event=self.context["event"])

    def _replace_links(self, instance, social_links=UNSET, extra_links=UNSET):
        if social_links is not UNSET:
            instance.social_links.all().delete()
            ExhibitorSocialLink.objects.bulk_create(
                [ExhibitorSocialLink(exhibitor=instance, **item) for item in social_links]
            )

        if extra_links is not UNSET:
            instance.extra_links.all().delete()
            ExhibitorExtraLink.objects.bulk_create(
                [ExhibitorExtraLink(exhibitor=instance, **item) for item in extra_links]
            )

    @transaction.atomic
    def create(self, validated_data):
        social_links = validated_data.pop("social_links", [])
        extra_links = validated_data.pop("extra_links", [])
        sponsor_group_name = validated_data.pop("sponsor_group_name", UNSET)
        sponsor_group_level = validated_data.pop("sponsor_group_level", UNSET)
        instance = ExhibitorInfo(event=self.context["event"], **validated_data)
        self._apply_business_rules(
            instance,
            sponsor_group_name=sponsor_group_name,
            sponsor_group_level=sponsor_group_level,
        )
        instance.save()
        self._replace_links(instance, social_links=social_links, extra_links=extra_links)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        social_links = validated_data.pop("social_links", UNSET)
        extra_links = validated_data.pop("extra_links", UNSET)
        sponsor_group_name = validated_data.pop("sponsor_group_name", UNSET)
        sponsor_group_level = validated_data.pop("sponsor_group_level", UNSET)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        self._apply_business_rules(
            instance,
            sponsor_group_name=sponsor_group_name,
            sponsor_group_level=sponsor_group_level,
        )
        instance.save()
        self._replace_links(instance, social_links=social_links, extra_links=extra_links)
        return instance


class ExhibitorInfoViewSet(viewsets.ModelViewSet):
    serializer_class = ExhibitorInfoSerializer
    queryset = ExhibitorInfo.objects.none()
    lookup_field = "id"
    permission = None
    write_permission = "can_change_event_settings"

    def get_queryset(self):
        return (
            ExhibitorInfo.objects.filter(event=self.request.event)
            .select_related("sponsor_group")
            .prefetch_related("social_links", "extra_links")
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["event"] = self.request.event
        return context

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()


class LeadCreateView(views.APIView):
    def get_allowed_attendee_data(self, order_position, settings, exhibitor):
        attendee_data = {"note": "", "tags": []}

        # allow_voucher_access gates whether *personal* attendee data is
        # attached to a scan at all. When it's off, the exhibitor still
        # gets a lead record (so duplicate-scan detection keeps working),
        # but no name/email/company/address/answers are attached to it.
        # `exhibitor` is only ever None from unit tests exercising this
        # method in isolation; real calls always pass the authenticated
        # exhibitor, so that case is treated as ungated for backward
        # compatibility with those tests.
        if exhibitor is not None and not exhibitor.allow_voucher_access:
            return attendee_data

        if settings.is_field_allowed("attendee_name"):
            attendee_data["name"] = order_position.attendee_name
        if settings.is_field_allowed("attendee_email"):
            attendee_data["email"] = order_position.attendee_email
        if settings.is_field_allowed("system_company"):
            attendee_data["company"] = order_position.company
        if settings.is_field_allowed("system_job_title"):
            attendee_data["job_title"] = order_position.job_title
        if settings.is_field_allowed("system_street"):
            address_parts = [
                order_position.street,
                order_position.zipcode,
                order_position.city,
                str(order_position.country) if order_position.country else "",
            ]
            attendee_data["address"] = ", ".join(part for part in address_parts if part)

        answers = {answer.question_id: answer for answer in order_position.answers.all()}
        required_questions = order_position.order.event.questions.filter(required=True, active=True).order_by(
            "position", "id"
        )
        question_data = []
        for question in required_questions:
            if not settings.is_field_allowed(f"question_{question.pk}"):
                continue
            answer = answers.get(question.pk)
            question_data.append(
                {
                    "question": str(question.question),
                    "answer": str(answer.answer) if answer else "",
                }
            )
        if question_data:
            attendee_data["questions"] = question_data

        return attendee_data

    def post(self, request, *args, **kwargs):
        # Extract parameters from the request
        pseudonymization_id = request.data.get("lead")
        scanned = request.data.get("scanned")
        scan_type = request.data.get("scan_type")
        device_name = request.data.get("device_name")
        open_event = request.data.get("open_event")
        key = request.headers.get("Exhibitor")

        if not all([pseudonymization_id, scanned, scan_type, device_name]):
            return Response({"detail": "Missing parameters"}, status=status.HTTP_400_BAD_REQUEST)

        # Authenticate the exhibitor
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {"success": False, "error": "Invalid exhibitor key"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not exhibitor.lead_scanning_enabled:
            return Response(
                {"success": False, "error": "Lead scanning is not enabled for this exhibitor"},
                status=status.HTTP_403_FORBIDDEN,
            )

        settings = ExhibitorSettings.objects.get_or_create(event=exhibitor.event)[0]

        # Get attendee details
        try:
            if open_event:
                order_position = OrderPosition.objects.get(secret=pseudonymization_id)
            else:
                order_position = OrderPosition.objects.get(pseudonymization_id=pseudonymization_id)
        except OrderPosition.DoesNotExist:
            return Response(
                {"success": False, "error": "Attendee not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check for duplicate scan
        if Lead.objects.filter(exhibitor=exhibitor, pseudonymization_id=pseudonymization_id).exists():
            attendee_data = self.get_allowed_attendee_data(order_position, settings, exhibitor)
            return Response(
                {
                    "success": False,
                    "error": "Lead already scanned",
                    "attendee": attendee_data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Get allowed attendee data based on settings
        attendee_data = self.get_allowed_attendee_data(order_position, settings, exhibitor)
        # Create the lead entry
        locale = _get_exhibitor_locale(exhibitor)
        lead = Lead.objects.create(
            exhibitor=exhibitor,
            exhibitor_name=_localize_i18n_value(exhibitor.name, locale),
            pseudonymization_id=pseudonymization_id,
            scanned=timezone.now(),
            scan_type=scan_type,
            device_name=device_name,
            booth_id=exhibitor.booth_id,
            booth_name=_localize_i18n_value(exhibitor.booth_name, locale),
            attendee=attendee_data,
        )

        return Response(
            {"success": True, "lead_id": lead.id, "attendee": attendee_data},
            status=status.HTTP_201_CREATED,
        )


def _require_lead_access(exhibitor):
    """Return a 403 Response if this exhibitor may not access already-scanned
    lead data, or None if access is allowed.

    ``lead_scanning_enabled`` only controls whether new leads can be
    *created* (see ``LeadCreateView``). ``allow_lead_access`` is the
    separate flag that controls whether the exhibitor can *read, export or
    annotate* leads that were already collected, which is what this guards.
    """
    if not exhibitor.allow_lead_access:
        return Response(
            {"success": False, "error": "This exhibitor is not allowed to access lead data"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


class LeadRetrieveView(views.APIView):
    def get(self, request, *args, **kwargs):
        # Authenticate the exhibitor using the key
        key = request.headers.get("Exhibitor")
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {"success": False, "error": "Invalid exhibitor key"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        denied = _require_lead_access(exhibitor)
        if denied is not None:
            return denied

        # Fetch all leads associated with the exhibitor
        leads = Lead.objects.filter(exhibitor=exhibitor).values(
            "id",
            "pseudonymization_id",
            "exhibitor_name",
            "scanned",
            "scan_type",
            "device_name",
            "booth_id",
            "booth_name",
            "attendee",
        )

        return Response({"success": True, "leads": list(leads)}, status=status.HTTP_200_OK)


class TagListView(views.APIView):
    def get(self, request, organizer, event, *args, **kwargs):
        key = request.headers.get("Exhibitor")
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {"success": False, "error": "Invalid exhibitor key"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        denied = _require_lead_access(exhibitor)
        if denied is not None:
            return denied

        tags = ExhibitorTag.objects.filter(exhibitor=exhibitor)
        return Response({"success": True, "tags": [tag.name for tag in tags]})


class LeadUpdateView(views.APIView):
    def post(self, request, organizer, event, lead_id, *args, **kwargs):
        key = request.headers.get("Exhibitor")
        note = request.data.get("note")
        tags = request.data.get("tags", [])

        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {"success": False, "error": "Invalid exhibitor key"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        denied = _require_lead_access(exhibitor)
        if denied is not None:
            return denied

        try:
            lead = Lead.objects.get(pseudonymization_id=lead_id, exhibitor=exhibitor)
        except Lead.DoesNotExist:
            return Response(
                {"success": False, "error": "Lead not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Update lead's attendee info
        attendee_data = lead.attendee or {}
        if note is not None:
            attendee_data["note"] = note
        if tags is not None:
            attendee_data["tags"] = tags

            # Update tag usage counts and create new tags
            for tag_name in tags:
                tag, created = ExhibitorTag.objects.get_or_create(exhibitor=exhibitor, name=tag_name)
                if not created:
                    tag.use_count += 1
                    tag.save()

        lead.attendee = attendee_data
        lead.save()

        return Response(
            {"success": True, "lead_id": lead.id, "attendee": lead.attendee},
            status=status.HTTP_200_OK,
        )
