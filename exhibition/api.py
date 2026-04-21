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


class ExhibitorAuthView(views.APIView):
    def post(self, request, *args, **kwargs):
        key = request.data.get("key")

        if not key:
            return Response(
                {"detail": "Missing parameters"}, status=status.HTTP_400_BAD_REQUEST
            )

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
    sponsor_group_name = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, write_only=True
    )
    social_links = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )
    extra_links = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

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
        data["sponsor_group_name"] = (
            instance.sponsor_group.localized_name if instance.sponsor_group else None
        )
        data["logo_url"] = instance.visible_logo_url
        data["header_image_url"] = instance.visible_header_image_url
        data["slides_url"] = instance.visible_slides_url
        data["social_links"] = [
            {"network": link.network, "url": link.url}
            for link in instance.social_links.all()
        ]
        data["extra_links"] = [
            {"label": link.label, "url": link.url}
            for link in instance.extra_links.all()
        ]
        return data

    def validate_social_links(self, value):
        normalized = []
        for item in value:
            network = str(item.get("network", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if not network or not url:
                raise serializers.ValidationError(
                    "Each social link requires network and url."
                )
            if network not in SOCIAL_LINK_SPECS:
                raise serializers.ValidationError(
                    f"Unsupported social network: {network}."
                )
            normalized.append({"network": network, "url": normalize_url_scheme(url)})
        return normalized

    def validate_extra_links(self, value):
        normalized = []
        for item in value:
            label = str(item.get("label", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if not label or not url:
                raise serializers.ValidationError(
                    "Each extra link requires label and url."
                )
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

        if data.get("slides_url") and not data["slides_url"].lower().split("?", 1)[
            0
        ].endswith(".pdf"):
            raise serializers.ValidationError(
                {"slides_url": "Slides URL must point to a PDF file."}
            )

        return data

    def _resolve_sponsor_group(self, sponsor_group_name):
        sponsor_group_name = str(sponsor_group_name or "").strip()
        if not sponsor_group_name:
            return None

        event = self.context["event"]
        for group in SponsorGroup.objects.filter(event=event):
            if group.localized_name == sponsor_group_name:
                return group

        return SponsorGroup.objects.create(
            event=event,
            name={event.locale or settings.LANGUAGE_CODE: sponsor_group_name},
        )

    def _apply_business_rules(self, instance, sponsor_group_name=UNSET):
        if instance.is_sponsor:
            if sponsor_group_name is not UNSET:
                instance.sponsor_group = self._resolve_sponsor_group(sponsor_group_name)
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
                [
                    ExhibitorSocialLink(exhibitor=instance, **item)
                    for item in social_links
                ]
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
        instance = ExhibitorInfo(event=self.context["event"], **validated_data)
        self._apply_business_rules(instance, sponsor_group_name=sponsor_group_name)
        instance.save()
        self._replace_links(
            instance, social_links=social_links, extra_links=extra_links
        )
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        social_links = validated_data.pop("social_links", UNSET)
        extra_links = validated_data.pop("extra_links", UNSET)
        sponsor_group_name = validated_data.pop("sponsor_group_name", UNSET)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        self._apply_business_rules(instance, sponsor_group_name=sponsor_group_name)
        instance.save()
        self._replace_links(
            instance, social_links=social_links, extra_links=extra_links
        )
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
        """Helper method to get allowed attendee data based on settings"""
        # Get all allowed fields including defaults
        allowed_fields = settings.all_allowed_fields
        attendee_data = {
            "name": order_position.attendee_name,  # Always included
            "email": order_position.attendee_email,  # Always included
            "company": order_position.company,  # Always included
            "city": order_position.city if "attendee_city" in allowed_fields else None,
            "country": str(order_position.country)
            if "attendee_country" in allowed_fields
            else None,
            "note": "",
            "tags": [],
        }

        return {k: v for k, v in attendee_data.items() if v is not None}

    def post(self, request, *args, **kwargs):
        # Extract parameters from the request
        pseudonymization_id = request.data.get("lead")
        scanned = request.data.get("scanned")
        scan_type = request.data.get("scan_type")
        device_name = request.data.get("device_name")
        open_event = request.data.get("open_event")
        key = request.headers.get("Exhibitor")

        if not all([pseudonymization_id, scanned, scan_type, device_name]):
            return Response(
                {"detail": "Missing parameters"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Authenticate the exhibitor
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            settings = ExhibitorSettings.objects.get(event=exhibitor.event)
        except (ExhibitorInfo.DoesNotExist, ExhibitorSettings.DoesNotExist):
            return Response(
                {"success": False, "error": "Invalid exhibitor key"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Get attendee details
        try:
            if open_event:
                order_position = OrderPosition.objects.get(secret=pseudonymization_id)
            else:
                order_position = OrderPosition.objects.get(
                    pseudonymization_id=pseudonymization_id
                )
        except OrderPosition.DoesNotExist:
            return Response(
                {"success": False, "error": "Attendee not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check for duplicate scan
        if Lead.objects.filter(
            exhibitor=exhibitor, pseudonymization_id=pseudonymization_id
        ).exists():
            attendee_data = self.get_allowed_attendee_data(
                order_position, settings, exhibitor
            )
            return Response(
                {
                    "success": False,
                    "error": "Lead already scanned",
                    "attendee": attendee_data,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Get allowed attendee data based on settings
        attendee_data = self.get_allowed_attendee_data(
            order_position, settings, exhibitor
        )
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

        return Response(
            {"success": True, "leads": list(leads)}, status=status.HTTP_200_OK
        )


class TagListView(views.APIView):
    def get(self, request, organizer, event, *args, **kwargs):
        key = request.headers.get("Exhibitor")
        try:
            exhibitor = ExhibitorInfo.objects.get(key=key)
            tags = ExhibitorTag.objects.filter(exhibitor=exhibitor)
            return Response({"success": True, "tags": [tag.name for tag in tags]})
        except ExhibitorInfo.DoesNotExist:
            return Response(
                {"success": False, "error": "Invalid exhibitor key"},
                status=status.HTTP_401_UNAUTHORIZED,
            )


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
                tag, created = ExhibitorTag.objects.get_or_create(
                    exhibitor=exhibitor, name=tag_name
                )
                if not created:
                    tag.use_count += 1
                    tag.save()

        lead.attendee = attendee_data
        lead.save()

        return Response(
            {"success": True, "lead_id": lead.id, "attendee": lead.attendee},
            status=status.HTTP_200_OK,
        )
